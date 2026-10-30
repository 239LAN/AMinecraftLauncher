"""轻量 HTTP 工具，供解析与下载复用。"""

from __future__ import annotations

import json
import shutil
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional

from .version import user_agent

DEFAULT_TIMEOUT = 30
DEFAULT_UA = user_agent()
CHUNK_SIZE = 1 << 16

# 部分 CDN（如 forgecdn）的证书链在 Windows 自带信任库中不完整，
# 优先使用 certifi 的 CA 包，缺失时回退系统默认。
try:
    import certifi

    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except Exception:  # pragma: no cover - certifi 缺失时保持系统默认
    _SSL_CONTEXT = ssl.create_default_context()

# 可被外部（如 API 层）覆盖，用于统一注入代理 / 自定义 UA。
opener: urllib.request.OpenerDirector = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=_SSL_CONTEXT)
)

# 由 engine.mirror 安装：把官方 URL 展开成「镜像优先 + 官方兜底」的候选列表。
_url_candidates: Callable[[str], list[str]] = lambda url: [url]

_NET_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError, OSError)


class DownloadCancelled(Exception):
    """调用方请求取消下载（由上层捕获并标记任务已取消）。"""


def set_url_candidates(fn: Callable[[str], list[str]]) -> None:
    """安装 URL 候选生成器（供下载源镜像使用）。"""
    global _url_candidates
    _url_candidates = fn


def url_candidates(url: str) -> list[str]:
    return _url_candidates(url) or [url]


def _open(url: str, headers: Optional[dict[str, str]] = None, timeout: int = DEFAULT_TIMEOUT):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": DEFAULT_UA, **(headers or {})},
    )
    return opener.open(request, timeout=timeout)


def _open_candidates(
    url: str,
    headers: Optional[dict[str, str]] = None,
    timeout: int = DEFAULT_TIMEOUT,
):
    """依次尝试候选地址，镜像失败时自动落到官方源。

    非最后一个候选（通常是镜像）使用更短的超时，避免镜像卡住时每个请求
    都要等满 30 秒才回退官方源。
    """
    last_error: Optional[BaseException] = None
    candidates = url_candidates(url)
    for index, candidate in enumerate(candidates):
        attempt_timeout = timeout if index == len(candidates) - 1 else min(timeout, 8)
        try:
            return _open(candidate, headers, attempt_timeout)
        except _NET_ERRORS as exc:  # noqa: PERF203 - 候选地址逐个尝试
            last_error = exc
    assert last_error is not None
    raise last_error


def get_json(url: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    with _open_candidates(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_bytes(url: str, timeout: int = DEFAULT_TIMEOUT) -> bytes:
    with _open_candidates(url, timeout=timeout) as response:
        return response.read()


def request_json(
    url: str,
    *,
    method: str = "POST",
    json_body: Optional[dict] = None,
    form: Optional[dict] = None,
    headers: Optional[dict[str, str]] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[int, dict]:
    """发送 JSON 或表单请求，返回 (status, payload)。

    4xx/5xx 也会把响应体解析成 JSON 返回，便于处理 OAuth 的
    ``authorization_pending`` 等业务错误。
    """
    final_headers = dict(headers or {})
    data: Optional[bytes] = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        final_headers.setdefault("Content-Type", "application/json")
        final_headers.setdefault("Accept", "application/json")
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        final_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        final_headers.setdefault("Accept", "application/json")

    request = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": DEFAULT_UA, **final_headers},
        method=method,
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return response.status or 200, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"error": raw[:500]}
        return exc.code, payload


def get_json_auth(
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[int, dict]:
    """带自定义头的 GET（如 Mod 源的 Accept / User-Agent）。"""
    return request_json(url, method="GET", headers=headers, timeout=timeout)


def download_file(
    url: str,
    dest: Path,
    *,
    expected_size: int = 0,
    on_chunk: Optional[Callable[[int], None]] = None,
    resume: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = 3,
    headers: Optional[dict[str, str]] = None,
    cancel: Optional[Callable[[], bool]] = None,
) -> int:
    """下载文件到 dest，支持 Range 断点续传。

    先写入 ``dest.part``，成功后再原子重命名，避免半成品污染目标文件。
    返回本次调用写入的字节数；``on_chunk(delta)`` 用于进度上报。
    ``headers`` 用于需要鉴权或自定义 UA 的 CDN。
    ``cancel`` 返回 True 时立即中断并抛出 :class:`DownloadCancelled`。

    地址会按候选列表依次尝试：镜像优先，镜像不可用时自动回退官方源。
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    candidates = url_candidates(url)

    last_error: Optional[BaseException] = None
    for attempt in range(1, retries + 1):
        if cancel and cancel():
            raise DownloadCancelled()
        for index, candidate in enumerate(candidates):
            # 镜像候选先给较短超时，失败立即回退官方源。
            attempt_timeout = timeout if index == len(candidates) - 1 else min(timeout, 12)
            try:
                return _download_once(
                    candidate, dest, part, expected_size, on_chunk, resume, attempt_timeout, headers, cancel
                )
            except DownloadCancelled:
                raise
            except _NET_ERRORS as exc:
                last_error = exc
        if attempt < retries:
            # 退避期间也响应取消，避免点「取消」后还要等好几秒。
            deadline = time.monotonic() + min(2**attempt, 10)
            while time.monotonic() < deadline:
                if cancel and cancel():
                    raise DownloadCancelled()
                time.sleep(0.2)

    assert last_error is not None
    raise last_error


def _download_once(
    url: str,
    dest: Path,
    part: Path,
    expected_size: int,
    on_chunk: Optional[Callable[[int], None]],
    resume: bool,
    timeout: int,
    headers: Optional[dict[str, str]] = None,
    cancel: Optional[Callable[[], bool]] = None,
) -> int:
    start = part.stat().st_size if (resume and part.exists()) else 0
    # 本地半成品比远端文件还大，说明是无效残留，直接丢掉重新下载。
    if expected_size and start > expected_size:
        part.unlink(missing_ok=True)
        start = 0

    request_headers: dict[str, str] = dict(headers or {})
    if start > 0:
        request_headers["Range"] = f"bytes={start}-"

    try:
        response = _open(url, headers=request_headers, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code != 416 or start <= 0:
            raise
        # 416：Range 起点超出远端文件（.part 残留 / 远端文件已变短），清掉重新下载。
        part.unlink(missing_ok=True)
        start = 0
        response = _open(url, headers=dict(headers or {}), timeout=timeout)

    try:
        status = getattr(response, "status", 200) or 200
        if status == 416 and start > 0:
            response.close()
            part.unlink(missing_ok=True)
            start = 0
            response = _open(url, headers=dict(headers or {}), timeout=timeout)
            status = getattr(response, "status", 200) or 200
        # 服务端忽略了 Range（返回 200），需要从头下载。
        if start > 0 and status != 206:
            start = 0

        mode = "ab" if start > 0 else "wb"
        written = 0
        with open(part, mode) as handle:
            while True:
                if cancel and cancel():
                    raise DownloadCancelled()
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                handle.write(chunk)
                written += len(chunk)
                if on_chunk:
                    on_chunk(len(chunk))
    finally:
        response.close()

    if expected_size and part.stat().st_size != expected_size:
        # 大小不符，丢弃半成品，交由上层重试。
        actual = part.stat().st_size
        part.unlink(missing_ok=True)
        raise OSError(
            f"size mismatch for {dest.name}: {actual} != {expected_size}"
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    part.replace(dest)
    return written


MIN_SEGMENT_SIZE = 512 * 1024


def download_file_segmented(
    url: str,
    dest: Path,
    *,
    size: int,
    segments: int = 4,
    on_chunk: Optional[Callable[[int], None]] = None,
    timeout: int = DEFAULT_TIMEOUT,
    headers: Optional[dict[str, str]] = None,
    cancel: Optional[Callable[[], bool]] = None,
) -> int:
    """分片并发下载单个大文件（HTTP Range）。

    服务端不支持 Range（返回非 206）时抛错，由调用方回退到普通下载。
    先写 ``*.part<i>`` 分片，全部完成后合并为 ``*.part`` 再原子替换目标文件。
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")

    count = max(1, min(segments, max(1, size // MIN_SEGMENT_SIZE)))
    chunk = (size + count - 1) // count
    ranges: list[tuple[int, int, int]] = []
    for index in range(count):
        start = index * chunk
        end = min(size, start + chunk) - 1
        if start <= end:
            ranges.append((index, start, end))

    if len(ranges) <= 1:
        raise OSError("文件过小，无需分片")

    def segment_path(index: int) -> Path:
        return dest.with_name(f"{dest.name}.part{index}")

    abort = threading.Event()

    def fetch(index: int, start: int, end: int) -> None:
        if cancel and cancel():
            raise DownloadCancelled()
        target = segment_path(index)
        expected = end - start + 1
        if target.exists() and target.stat().st_size == expected:
            if on_chunk:
                on_chunk(expected)
            return

        request_headers = dict(headers or {})
        request_headers["Range"] = f"bytes={start}-{end}"
        written = 0
        with _open_candidates(url, request_headers, timeout) as response:
            status = getattr(response, "status", 200) or 200
            if status != 206:
                raise OSError(f"服务端不支持 Range（HTTP {status}）")
            with open(target, "wb") as handle:
                while True:
                    if abort.is_set():
                        raise OSError("分片下载已中止")
                    if cancel and cancel():
                        raise DownloadCancelled()
                    block = response.read(CHUNK_SIZE)
                    if not block:
                        break
                    handle.write(block)
                    written += len(block)
                    if on_chunk:
                        on_chunk(len(block))
        if written != expected:
            raise OSError(f"分片长度不符：{written} != {expected}")

    try:
        with ThreadPoolExecutor(max_workers=len(ranges)) as pool:
            futures = [pool.submit(fetch, index, start, end) for index, start, end in ranges]
            try:
                # 任一分片失败就尽快中止其它分片，直接回退单连接，而不是等全部下完。
                for future in as_completed(futures):
                    future.result()
            except BaseException:
                abort.set()
                raise

        with open(part, "wb") as output:
            for index, _, _ in ranges:
                with open(segment_path(index), "rb") as source:
                    shutil.copyfileobj(source, output)

        if part.stat().st_size != size:
            raise OSError(f"合并后大小不符：{part.stat().st_size} != {size}")
        if dest.exists():
            dest.unlink()
        part.replace(dest)
        return size
    except Exception as exc:
        part.unlink(missing_ok=True)
        # 中止其它分片后可能抛出 OSError；若用户已取消，统一按取消处理。
        if cancel and cancel():
            raise DownloadCancelled() from exc
        raise
    finally:
        for index, _, _ in ranges:
            segment_path(index).unlink(missing_ok=True)