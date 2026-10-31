"""轻量 HTTP 工具，供解析与下载复用。"""

from __future__ import annotations

import http.client
import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional, Sequence

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
# 镜像与官方源都可用时，下载器会同时从两个源下载不同分片。
_dual_source_policy: Callable[[], bool] = lambda: False

_NET_ERRORS = (
    urllib.error.URLError,
    http.client.HTTPException,
    TimeoutError,
    ConnectionError,
    OSError,
)


class DownloadCancelled(Exception):
    """调用方请求取消下载（由上层捕获并标记任务已取消）。"""


class DownloadAborted(Exception):
    """同一文件的其它分片已失败，当前分片提前结束。"""


class RangeNotSupported(OSError):
    """下载源不支持 HTTP Range（返回 200），需要回退整文件下载。"""


class _PartRestarted(Exception):
    """分片文件已作废，需要从区间起点重新下载。"""


def set_url_candidates(fn: Callable[[str], list[str]]) -> None:
    """安装 URL 候选生成器（供下载源镜像使用）。"""
    global _url_candidates
    _url_candidates = fn


def set_dual_source_policy(fn: Callable[[], bool]) -> None:
    """安装「是否允许镜像与官方源并行下载」的判定函数。"""
    global _dual_source_policy
    _dual_source_policy = fn


def dual_source_enabled() -> bool:
    try:
        return bool(_dual_source_policy())
    except Exception:  # noqa: BLE001 - 判定函数异常时退回单源
        return False


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
    candidates: Optional[Sequence[str]] = None,
) -> int:
    """下载文件到 dest，支持 Range 断点续传。

    先写入 ``dest.part``，成功后再原子重命名，避免半成品污染目标文件。
    返回本次调用写入的字节数；``on_chunk(delta)`` 用于进度上报。
    ``headers`` 用于需要鉴权或自定义 UA 的 CDN。
    ``cancel`` 返回 True 时立即中断并抛出 :class:`DownloadCancelled`。

    ``candidates`` 可显式指定候选地址列表（镜像与官方源同时下载时由上层轮换）；
    默认按镜像优先、官方兜底的顺序依次尝试。
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    candidate_list = [str(item) for item in candidates] if candidates else url_candidates(url)

    # 记录本次调用已上报的字节数，供中途重下时回退进度，避免进度超过 100%。
    reported = 0

    def track(delta: int) -> None:
        nonlocal reported
        if not delta:
            return
        reported += delta
        if on_chunk:
            on_chunk(delta)

    last_error: Optional[BaseException] = None
    for attempt in range(1, retries + 1):
        if cancel and cancel():
            raise DownloadCancelled()
        for index, candidate in enumerate(candidate_list):
            # 镜像候选先给较短超时，失败立即回退官方源。
            attempt_timeout = timeout if index == len(candidate_list) - 1 else min(timeout, 12)
            try:
                return _download_once(
                    candidate, dest, part, expected_size, track, resume, attempt_timeout,
                    headers, cancel, reported,
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

    if last_error is not None:
        raise last_error
    raise OSError("下载失败")


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
    already_reported: int = 0,
) -> int:
    start = part.stat().st_size if (resume and part.exists()) else 0

    def rewind(removed: int) -> None:
        """丢弃本地半成品时，把本次调用已上报的字节数从进度里扣回。"""
        nonlocal already_reported
        amount = min(removed, already_reported)
        if amount and on_chunk:
            on_chunk(-amount)
            already_reported -= amount

    # 本地半成品比远端文件还大，说明是无效残留，直接丢掉重新下载。
    if expected_size and start > expected_size:
        part.unlink(missing_ok=True)
        rewind(start)
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
        rewind(start)
        start = 0
        response = _open(url, headers=dict(headers or {}), timeout=timeout)

    try:
        status = getattr(response, "status", 200) or 200
        if status == 416 and start > 0:
            response.close()
            part.unlink(missing_ok=True)
            rewind(start)
            start = 0
            response = _open(url, headers=dict(headers or {}), timeout=timeout)
            status = getattr(response, "status", 200) or 200
        # 服务端忽略了 Range（返回 200），需要从头下载；此前写入的字节要扣回。
        if start > 0 and status != 206:
            rewind(start)
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
        # 大小不符，丢弃半成品，交由上层重试；同步回退本次已上报的字节。
        actual = part.stat().st_size
        part.unlink(missing_ok=True)
        rewind(actual)
        raise OSError(
            f"size mismatch for {dest.name}: {actual} != {expected_size}"
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    part.replace(dest)
    return written


def download_part(
    candidates: Sequence[str],
    dest: Path,
    *,
    start: int,
    end: int,
    on_chunk: Optional[Callable[[int], None]] = None,
    timeout: int = DEFAULT_TIMEOUT,
    headers: Optional[dict[str, str]] = None,
    cancel: Optional[Callable[[], bool]] = None,
    abort: Optional[threading.Event] = None,
    retries: int = 3,
) -> int:
    """下载文件的 ``[start, end]`` 区间到分片文件 dest，支持断点续传与换源重试。

    - 分片文件已存在时只请求剩余区间，未下载部分继续续传；
    - 候选源按传入顺序依次尝试，因此上层可以让不同分片分别从镜像和官方源下载；
    - 所有候选源都不支持 Range（返回 200）时抛 :class:`RangeNotSupported`，
      由上层回退到整文件单连接下载；
    - ``abort`` 置位表示同文件其它分片已失败，当前分片立即停止。
    返回本次调用写入的字节数。
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    length = end - start + 1
    if length <= 0:
        return 0
    candidate_list = [str(item) for item in candidates]
    if not candidate_list:
        raise OSError("没有可用的下载源")

    # 只记录本次调用上报的字节；中途重下 / 作废分片时同步回退。
    reported = 0
    written_total = 0

    def emit(delta: int) -> None:
        nonlocal reported, written_total
        if not delta:
            return
        reported += delta
        if delta > 0:
            written_total += delta
        if on_chunk:
            on_chunk(delta)

    def reset_progress() -> None:
        nonlocal reported
        if reported:
            amount, reported = -reported, 0
            if on_chunk:
                on_chunk(amount)

    last_error: Optional[BaseException] = None
    range_error: Optional[RangeNotSupported] = None
    for attempt in range(1, max(1, retries) + 1):
        if cancel and cancel():
            raise DownloadCancelled()
        if abort and abort.is_set():
            raise DownloadAborted()
        range_only = True
        for index, candidate in enumerate(candidate_list):
            if cancel and cancel():
                raise DownloadCancelled()
            if abort and abort.is_set():
                raise DownloadAborted()
            # 首个候选给较短超时，镜像/官方之一不可用时快速换源。
            attempt_timeout = timeout if index == len(candidate_list) - 1 else min(timeout, 10)
            try:
                _download_part_once(
                    candidate, dest, start, end, emit, attempt_timeout, headers, cancel, abort
                )
                return written_total
            except DownloadCancelled:
                raise
            except DownloadAborted:
                raise
            except RangeNotSupported as exc:
                # 该源忽略 Range：换下一个源试试，两个源都不支持才整文件回退。
                range_error = exc
                continue
            except _PartRestarted:
                reset_progress()
                range_only = False
                continue
            except _NET_ERRORS as exc:
                range_only = False
                last_error = exc
        if range_only and range_error is not None:
            raise range_error
        if attempt < retries:
            deadline = time.monotonic() + min(2**attempt, 8)
            while time.monotonic() < deadline:
                if cancel and cancel():
                    raise DownloadCancelled()
                if abort and abort.is_set():
                    raise DownloadAborted()
                time.sleep(0.2)

    if last_error is not None:
        raise last_error
    if range_error is not None:
        raise range_error
    raise OSError("分片下载失败")


def _download_part_once(
    url: str,
    dest: Path,
    start: int,
    end: int,
    on_bytes: Callable[[int], None],
    timeout: int,
    headers: Optional[dict[str, str]] = None,
    cancel: Optional[Callable[[], bool]] = None,
    abort: Optional[threading.Event] = None,
) -> None:
    expected = end - start + 1
    existing = dest.stat().st_size if dest.exists() else 0
    if existing > expected:
        # 残留分片比区间还大，作废后从头下载。
        dest.unlink(missing_ok=True)
        raise _PartRestarted()
    if existing == expected:
        # 上次已下载完整，直接复用（无需再上报进度）。
        return

    offset = start + existing
    remaining = expected - existing
    request_headers = dict(headers or {})
    request_headers["Range"] = f"bytes={offset}-{end}"

    try:
        response = _open(url, headers=request_headers, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and existing > 0:
            dest.unlink(missing_ok=True)
            raise _PartRestarted() from exc
        raise

    try:
        status = getattr(response, "status", None) or 200
        if status != 206:
            # 该源忽略 Range 并返回整文件，无法按区间切片，交给上层整文件回退。
            raise RangeNotSupported(f"HTTP {status}")
        content_range = ""
        try:
            content_range = response.headers.get("Content-Range", "") or ""
        except AttributeError:
            pass
        if content_range:
            try:
                first = int(content_range.split(" ", 1)[1].split("-")[0])
            except (IndexError, ValueError):
                first = offset
            if first != offset:
                # 返回的区间与请求不一致，视为该源不支持可靠的分片下载；
                # 保留已写入的分片，换源或整文件回退时再统一清理。
                raise RangeNotSupported(f"Content-Range 不匹配：{content_range}")
        written = 0
        with open(dest, "ab" if existing else "wb") as handle:
            while remaining > 0:
                if cancel and cancel():
                    raise DownloadCancelled()
                if abort and abort.is_set():
                    raise DownloadAborted()
                block = response.read(min(CHUNK_SIZE, remaining))
                if not block:
                    break
                handle.write(block)
                written += len(block)
                remaining -= len(block)
                on_bytes(len(block))
        if written != expected - existing:
            # 连接提前断开：保留已写分片，由上层重试时续传剩余部分。
            raise OSError(f"分片未完成：{written} != {expected - existing}")
    finally:
        response.close()