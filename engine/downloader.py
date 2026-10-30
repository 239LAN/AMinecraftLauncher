"""Downloader：SHA1 预校验跳过 + 线程池并发 + .part 断点续传。

只通过回调上报进度，不依赖 Web 层；API 层的 SSE 只是消费者。
"""

from __future__ import annotations

import hashlib
import os
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
        return {
            "type": "progress",
            "files_total": self.files_total,
            "files_done": self.files_done,
            "files_downloaded": self.files_downloaded,
            "files_skipped": self.files_skipped,
            "bytes_total": self.bytes_total,
            "bytes_done": self.bytes_done,
            "current": current,
            "percent": (
                round(self.bytes_done / self.bytes_total * 100, 2)
                if self.bytes_total
                else 0.0
            ),
        }


class Downloader:
    def __init__(
        self,
        *,
        workers: int = 32,
        verify: bool = True,
        progress: Optional[ProgressFn] = None,
        timeout: int = 30,
        retries: int = 3,
        segments: int = 6,
        segment_threshold: int = 4 * 1024 * 1024,
    ) -> None:
        self.workers = max(1, workers)
        self.verify = verify
        self.progress = progress
        self.timeout = timeout
        self.retries = retries
        # 单个大文件的分片并发数；小于阈值的文件仍走单连接。
        self.segments = max(1, segments)
        self.segment_threshold = max(0, segment_threshold)

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
        lock = threading.Lock()
        last_report = 0.0

        def report(current: str = "") -> None:
            nonlocal last_report
            now = time.monotonic()
            if self.progress and (now - last_report >= REPORT_INTERVAL):
                last_report = now
                self.progress(stats.as_event(current=current))

        # 大文件优先下载，缩短整体等待时间。
        ordered = sorted(files, key=lambda f: f.size, reverse=True)

        raise_if_cancelled(cancel)

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {
                pool.submit(self._fetch_one, entry, stats, lock, report, cancel): entry
                for entry in ordered
            }
            for future in as_completed(futures):
                entry = futures[future]
                try:
                    future.result()
                except DownloadCancelled:
                    for pending in futures:
                        pending.cancel()
                    raise
                except Exception as exc:  # noqa: BLE001 - 汇总失败项，不中断其它文件
                    with lock:
                        stats.failed.append((str(entry.target_path), str(exc)))

        if self.progress:
            self.progress(stats.as_event(current="done"))
        return stats

    # ------------------------------------------------------------------ worker
    def _fetch_one(
        self,
        entry: FileEntry,
        stats: DownloadStats,
        lock: threading.Lock,
        report: Callable[[str], None],
        cancel: Optional[CancelFn],
    ) -> None:
        raise_if_cancelled(cancel)
        dest = Path(entry.target_path)
        name = dest.name

        if self._is_valid(dest, entry):
            with lock:
                stats.files_done += 1
                stats.files_skipped += 1
                stats.bytes_done += dest.stat().st_size
            report(name)
            return

        def on_chunk(delta: int) -> None:
            with lock:
                stats.bytes_done += delta
            report(name)

        downloaded = 0
        if self.segments > 1 and entry.size >= self.segment_threshold > 0:
            # 大文件：分片并发下载；服务端不支持 Range 时回退单连接。
            def segmented_chunk(delta: int) -> None:
                nonlocal downloaded
                downloaded += delta
                on_chunk(delta)

            try:
                net.download_file_segmented(
                    entry.url,
                    dest,
                    size=entry.size,
                    segments=self.segments,
                    on_chunk=segmented_chunk,
                    timeout=self.timeout,
                    headers=entry.headers,
                    cancel=cancel,
                )
            except DownloadCancelled:
                raise
            except Exception:  # noqa: BLE001 - 回退到普通下载（含断点续传）
                if downloaded:
                    with lock:
                        stats.bytes_done -= downloaded
                    downloaded = 0
                _single_connection = True
            else:
                _single_connection = False
        else:
            _single_connection = True

        if _single_connection:
            net.download_file(
                entry.url,
                dest,
                expected_size=entry.size,
                on_chunk=on_chunk,
                timeout=self.timeout,
                retries=self.retries,
                headers=entry.headers,
                cancel=cancel,
            )

        if self.verify and entry.sha1:
            actual = sha1_file(dest)
            if actual.lower() != entry.sha1.lower():
                dest.unlink(missing_ok=True)
                raise ValueError(f"SHA1 校验失败: {name} ({actual} != {entry.sha1})")

        with lock:
            stats.files_done += 1
        report(name)

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