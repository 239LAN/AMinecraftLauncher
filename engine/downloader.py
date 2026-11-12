"""Downloader：SHA1 预校验跳过 + 全局分片并发 + .part 断点续传。

下载以「分片」为单位进入同一个线程池：大文件会按剩余大小切分，未开始的文件
也会被切分，因此只要还有数据没下完，线程池就能保持满负荷。
镜像与官方源同时可用时，同一文件的不同分片会分别从两个源下载；
某个源不可用时，该分片会自动回退到另一个源。

只通过回调上报进度，不依赖 Web 层；API 层的 SSE 只是消费者。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

from . import net
from .models import FileEntry

HASH_CHUNK = 1 << 20
# 进度上报节流间隔（秒），避免高频 SSE 事件。
REPORT_INTERVAL = 0.15
# 单个分片的最小体积：分得太碎会增加请求开销，1MB 在并发与开销之间比较平衡。
MIN_PART_SIZE = 1 << 20

ProgressFn = Callable[[dict], None]
CancelFn = Callable[[], bool]

# 取消异常定义在 net 层（下载中途也要能中断），这里保持原有导入路径可用。
DownloadCancelled = net.DownloadCancelled


def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class DownloadStats:
    files_total: int = 0
    files_done: int = 0
    files_skipped: int = 0
    bytes_total: int = 0
    bytes_done: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed

    @property
    def files_downloaded(self) -> int:
        """实际下载的文件数（不含校验通过而跳过的文件）。"""
        return max(0, self.files_done - self.files_skipped)

    def as_event(self, *, current: str = "") -> dict:
        percent = 0.0
        if self.bytes_total:
            # 未知大小的文件（bytes_total 为 0）以及失败重试都可能让累计字节
            # 短暂超过总量，这里统一收敛到 0~100，界面永远不会显示超 100%。
            percent = min(100.0, max(0.0, self.bytes_done / self.bytes_total * 100))
        return {
            "type": "progress",
            "files_total": self.files_total,
            "files_done": self.files_done,
            "files_downloaded": self.files_downloaded,
            "files_skipped": self.files_skipped,
            "bytes_total": self.bytes_total,
            "bytes_done": self.bytes_done,
            "current": current,
            "percent": round(percent, 2),
        }


@dataclass
class _Part:
    """单个分片的下载计划。"""

    index: int
    start: int
    end: int
    path: Path
    whole: bool = False
    # 在整批任务中的序号，用于让相邻分片分别优先走镜像 / 官方源。
    order: int = 0
    # 本次运行已上报的字节数（用于失败回退时扣回）。
    written: int = 0


@dataclass
class _Plan:
    """一个目标文件的下载计划。"""

    entry: FileEntry
    dest: Path
    part: Path
    whole: bool
    parts: list[_Part]
    abort: threading.Event = field(default_factory=threading.Event)
    error: Optional[BaseException] = None
    remaining: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class Downloader:
    def __init__(
        self,
        *,
        workers: int = 256,
        verify: bool = True,
        progress: Optional[ProgressFn] = None,
        timeout: int = 30,
        retries: int = 3,
        segment_threshold: int = 4 * 1024 * 1024,
    ) -> None:
        # 上限 1024：线程数可由配置调整，但避免异常值起上千个连接。
        self.workers = max(1, min(1024, int(workers or 256)))
        self.verify = verify
        self.progress = progress
        self.timeout = timeout
        self.retries = retries
        # 达到该体积的文件才会被切分；小于阈值的文件整体下载更划算。
        self.segment_threshold = max(0, segment_threshold)

        # 一次 download 调用期间共享的统计数据与进度回调。
        self._stats = DownloadStats()
        self._lock = threading.Lock()
        self._report_fn: Callable[[str], None] = lambda _name: None

    def download(
        self,
        files: Sequence[FileEntry],
        *,
        cancel: Optional[CancelFn] = None,
    ) -> DownloadStats:
        files = list(files)
        # 版本清单里同一个目标文件可能被重复声明（库 / 资源继承等）。
        # 这里按目标路径去重，避免重复下载、线程互相覆盖，以及文件总数虚高。
        unique: dict[str, FileEntry] = {}
        for entry in files:
            key = os.path.normcase(os.path.abspath(str(entry.target_path)))
            unique.setdefault(key, entry)
        files = list(unique.values())

        stats = DownloadStats(
            files_total=len(files),
            bytes_total=sum(max(f.size, 0) for f in files),
        )
        self._stats = stats
        self._lock = threading.Lock()

        last_report = 0.0

        def report(current: str = "") -> None:
            nonlocal last_report
            if not self.progress:
                return
            now = time.monotonic()
            if now - last_report < REPORT_INTERVAL:
                return
            last_report = now
            with self._lock:
                event = stats.as_event(current=current)
            self.progress(event)

        self._report_fn = report
        raise_if_cancelled(cancel)

        # 1) 并行校验已存在的文件，通过的直接跳过。
        pending = self._verify_existing(files, cancel)

        # 2) 规划分片：大文件优先，让线程池尽早开始满载工作。
        plans = self._build_plans(pending)

        # 3) 分片统一进入同一个线程池；某个文件的分片失败后回退整文件下载。
        raise_if_cancelled(cancel)
        futures: dict = {}
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for plan in plans:
                if plan.whole:
                    futures[pool.submit(self._fetch_whole, plan, cancel)] = plan
                else:
                    for part in plan.parts:
                        futures[pool.submit(self._fetch_part, plan, part, cancel)] = plan
            for future in as_completed(futures):
                try:
                    future.result()
                except DownloadCancelled:
                    for item in futures:
                        item.cancel()
                    raise
                except Exception as exc:  # noqa: BLE001 - 兜底，避免单文件拖垮整批
                    self._record_failure(futures[future], exc)

        # 任务刚好在最后几个分片完成时被取消，也要按取消结束而不是「成功」。
        raise_if_cancelled(cancel)
        if self.progress:
            with self._lock:
                # 断点续传时本次运行只统计了新下载的字节；全部完成后直接标记 100%，
                # 否则「已完成」的任务可能停在 90% 之类的数字上。
                if not stats.failed and stats.bytes_total:
                    stats.bytes_done = stats.bytes_total
                event = stats.as_event(current="done")
            self.progress(event)
        return stats

    # ------------------------------------------------------------------ 规划
    def _verify_existing(self, files: list[FileEntry], cancel: Optional[CancelFn]) -> list[FileEntry]:
        if not files:
            return []
        pending: list[FileEntry] = []
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {
                pool.submit(self._is_valid, Path(entry.target_path), entry): entry
                for entry in files
            }
            for future in as_completed(futures):
                entry = futures[future]
                try:
                    valid = future.result()
                except Exception:  # noqa: BLE001 - 读不了就当作需要重新下载
                    valid = False
                if valid:
                    size = Path(entry.target_path).stat().st_size
                    with self._lock:
                        self._stats.files_done += 1
                        self._stats.files_skipped += 1
                        self._stats.bytes_done += size
                    self._report_fn(Path(entry.target_path).name)
                else:
                    pending.append(entry)
        raise_if_cancelled(cancel)
        return pending

    def _build_plans(self, entries: list[FileEntry]) -> list[_Plan]:
        entries = sorted(entries, key=lambda item: item.size, reverse=True)
        plans: list[_Plan] = []
        order = 0
        for entry in entries:
            dest = Path(entry.target_path)
            parts = self._split(entry, dest)
            if len(parts) <= 1:
                parts = [_Part(index=0, start=0, end=-1, path=dest, whole=True)]
            else:
                for part in parts:
                    part.order = order
                    order += 1
            plans.append(
                _Plan(
                    entry=entry,
                    dest=dest,
                    part=dest.with_name(dest.name + ".part"),
                    whole=len(parts) == 1 and parts[0].whole,
                    parts=parts,
                    remaining=0 if (len(parts) == 1 and parts[0].whole) else len(parts),
                )
            )
        return plans

    def _split(self, entry: FileEntry, dest: Path) -> list[_Part]:
        """把大文件切成分片，小文件整体下载。

        分片数上限取线程数的两倍：队列里始终有富余分片，某个分片失败或提前
        完成时线程不会空转；断点续传时也能把未下载部分继续拆开并发下载。
        """
        size = max(int(entry.size or 0), 0)
        if self.workers < 2 or size <= 0 or size < self.segment_threshold:
            return []
        max_parts = min(
            self.workers * 2,
            max(1, (size + MIN_PART_SIZE - 1) // MIN_PART_SIZE),
        )
        if max_parts < 2:
            return []
        chunk = (size + max_parts - 1) // max_parts
        parts: list[_Part] = []
        index = 0
        for start in range(0, size, chunk):
            end = min(size, start + chunk) - 1
            if start > end:
                break
            parts.append(
                _Part(
                    index=index,
                    start=start,
                    end=end,
                    path=dest.with_name(f"{dest.name}.part{index}"),
                )
            )
            index += 1
        return parts

    # ------------------------------------------------------------------ 执行
    def _track(self, part: Optional[_Part], delta: int, name: str) -> None:
        if not delta:
            return
        with self._lock:
            self._stats.bytes_done = max(0, self._stats.bytes_done + delta)
            if part is not None:
                part.written = max(0, part.written + delta)
        self._report_fn(name)

    def _part_candidates(self, url: str, order: int) -> list[str]:
        """镜像与官方源都可用时，让相邻分片分别从两个源下载。

        只有一个源可用时仍按原有顺序（镜像优先、官方兜底），避免向不可达的
        源发起无谓的请求。
        """
        candidates = net.url_candidates(url) or [url]
        if len(candidates) <= 1 or not net.dual_source_enabled():
            return candidates
        shift = order % len(candidates)
        return candidates[shift:] + candidates[:shift]

    def _fetch_whole(self, plan: _Plan, cancel: Optional[CancelFn]) -> None:
        try:
            raise_if_cancelled(cancel)
            self._download_whole(plan, cancel)
            self._verify_dest(plan)
            self._mark_done(plan)
        except DownloadCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - 汇总失败项，不中断其它文件
            self._record_failure(plan, exc)

    def _fetch_part(self, plan: _Plan, part: _Part, cancel: Optional[CancelFn]) -> None:
        try:
            raise_if_cancelled(cancel)
            if plan.abort.is_set():
                return
            net.download_part(
                self._part_candidates(plan.entry.url, part.order),
                part.path,
                start=part.start,
                end=part.end,
                on_chunk=lambda delta, p=part, n=plan.dest.name: self._track(p, delta, n),
                timeout=self.timeout,
                headers=plan.entry.headers,
                cancel=cancel,
                abort=plan.abort,
                retries=self.retries,
            )
        except DownloadCancelled:
            raise
        except (net.DownloadAborted, net.RangeNotSupported) as exc:
            with plan.lock:
                if plan.error is None:
                    plan.error = exc
            plan.abort.set()
        except Exception as exc:  # noqa: BLE001 - 分片失败，稍后整文件回退
            with plan.lock:
                if plan.error is None:
                    plan.error = exc
            plan.abort.set()
        finally:
            with plan.lock:
                plan.remaining -= 1
                last = plan.remaining <= 0
            if last:
                self._finalize(plan, cancel)

    def _finalize(self, plan: _Plan, cancel: Optional[CancelFn]) -> None:
        """所有分片结束后：合并落盘，失败则丢弃分片并回退整文件下载。"""
        try:
            raise_if_cancelled(cancel)
            if plan.error is not None:
                self._discard_parts(plan)
                # 分片失败（常见于镜像不支持 Range）：改用整文件单连接下载，
                # 仍然可以断点续传，并且会依次尝试镜像与官方源。
                self._download_whole(plan, cancel)
                self._verify_dest(plan)
                self._mark_done(plan)
                return
            self._merge_parts(plan)
            self._verify_part(plan)
            self._move_into_place(plan)
            self._mark_done(plan)
        except DownloadCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - 汇总失败项，不中断其它文件
            self._discard_parts(plan)
            self._record_failure(plan, exc)

    # -------------------------------------------------------------- 具体动作
    def _download_whole(self, plan: _Plan, cancel: Optional[CancelFn]) -> None:
        name = plan.dest.name
        net.download_file(
            plan.entry.url,
            plan.dest,
            expected_size=plan.entry.size,
            on_chunk=lambda delta: self._track(None, delta, name),
            timeout=self.timeout,
            retries=self.retries,
            headers=plan.entry.headers,
            cancel=cancel,
        )

    def _merge_parts(self, plan: _Plan) -> None:
        plan.dest.parent.mkdir(parents=True, exist_ok=True)
        with open(plan.part, "wb") as output:
            for part in plan.parts:
                with open(part.path, "rb") as source:
                    shutil.copyfileobj(source, output)
                part.path.unlink(missing_ok=True)
        if plan.entry.size and plan.part.stat().st_size != plan.entry.size:
            actual = plan.part.stat().st_size
            raise OSError(f"合并后大小不符：{actual} != {plan.entry.size}")

    def _discard_parts(self, plan: _Plan) -> None:
        """丢弃分片半成品，并把本次已上报的字节从总进度中扣回。"""
        total = 0
        with self._lock:
            for part in plan.parts:
                total += part.written
                part.written = 0
            if total:
                self._stats.bytes_done = max(0, self._stats.bytes_done - total)
        for part in plan.parts:
            part.path.unlink(missing_ok=True)
        plan.part.unlink(missing_ok=True)

    def _verify_dest(self, plan: _Plan) -> None:
        if not (self.verify and plan.entry.sha1):
            return
        actual = sha1_file(plan.dest)
        if actual.lower() != plan.entry.sha1.lower():
            plan.dest.unlink(missing_ok=True)
            raise ValueError(
                f"SHA1 校验失败: {plan.dest.name} ({actual} != {plan.entry.sha1})"
            )

    def _verify_part(self, plan: _Plan) -> None:
        if not (self.verify and plan.entry.sha1):
            return
        actual = sha1_file(plan.part)
        if actual.lower() != plan.entry.sha1.lower():
            plan.part.unlink(missing_ok=True)
            raise ValueError(
                f"SHA1 校验失败: {plan.dest.name} ({actual} != {plan.entry.sha1})"
            )

    def _move_into_place(self, plan: _Plan) -> None:
        plan.dest.parent.mkdir(parents=True, exist_ok=True)
        if plan.dest.exists():
            plan.dest.unlink()
        plan.part.replace(plan.dest)

    def _mark_done(self, plan: _Plan) -> None:
        with self._lock:
            self._stats.files_done += 1
        self._report_fn(plan.dest.name)

    def _record_failure(self, plan: Optional[_Plan], exc: BaseException) -> None:
        if plan is None:
            return
        with self._lock:
            self._stats.failed.append((str(plan.dest), str(exc)))
        self._report_fn(plan.dest.name)

    # ------------------------------------------------------------------ 校验
    def _is_valid(self, dest: Path, entry: FileEntry) -> bool:
        if not dest.exists():
            return False
        if entry.size and dest.stat().st_size != entry.size:
            return False
        if self.verify and entry.sha1:
            return sha1_file(dest).lower() == entry.sha1.lower()
        return True


def raise_if_cancelled(cancel: Optional[CancelFn]) -> None:
    if cancel and cancel():
        raise DownloadCancelled("下载已取消")
