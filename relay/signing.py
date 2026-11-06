"""启动器与 relay 服务之间的请求签名。

节点和主节点只接受携带有效签名的请求，避免有人绕过启动器直接用脚本
抓房间列表、建房或探测节点。签名算法与客户端 ``engine/relay.py`` 保持一致：

    sign = HMAC-SHA256(APP_KEY, "METHOD\\nPATH\\nTS\\nNONCE")

时间戳超出窗口或 nonce 重复都会被拒绝。
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# 与客户端内置的密钥一致；改动时两端必须同时发布。
APP_KEY = "aml-relay-2026-8f4b1c7d9e2a6350"
SIGN_WINDOW = 120
NONCE_LIMIT = 20000

HEADER_TS = "X-AML-Ts"
HEADER_NONCE = "X-AML-Nonce"
HEADER_SIGN = "X-AML-Sign"

# 健康检查与文档不参与鉴权，方便运维和探活。
EXEMPT_PATHS = {"/healthz", "/docs", "/openapi.json", "/redoc"}


def sign(method: str, path: str, timestamp: str, nonce: str, key: str = APP_KEY) -> str:
    message = f"{method.upper()}\n{path}\n{timestamp}\n{nonce}".encode("utf-8")
    return hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()


class NonceCache:
    """记录已用过的 nonce，过期后自动清理，避免重放。"""

    def __init__(self, window: int = SIGN_WINDOW, limit: int = NONCE_LIMIT) -> None:
        self.window = window
        self.limit = limit
        self._lock = threading.Lock()
        self._seen: dict[str, float] = {}

    def consume(self, nonce: str, now: float) -> bool:
        with self._lock:
            if len(self._seen) > self.limit:
                expired = [key for key, until in self._seen.items() if until < now]
                for key in expired:
                    self._seen.pop(key, None)
                if len(self._seen) > self.limit:
                    self._seen.clear()
            if nonce in self._seen:
                return False
            self._seen[nonce] = now + self.window * 2
            return True


def verify_request(
    request: Request,
    cache: NonceCache,
    *,
    path: str | None = None,
    key: str = APP_KEY,
) -> bool:
    timestamp = request.headers.get(HEADER_TS, "")
    nonce = request.headers.get(HEADER_NONCE, "")
    signature = request.headers.get(HEADER_SIGN, "")
    if not timestamp or not nonce or not signature:
        return False
    try:
        sent_at = float(timestamp)
    except ValueError:
        return False
    now = time.time()
    if abs(now - sent_at) > SIGN_WINDOW:
        return False
    expected = sign(request.method, path or request.url.path, timestamp, nonce, key)
    if not hmac.compare_digest(expected, signature):
        return False
    return cache.consume(nonce, now)


def install_signature_guard(app: FastAPI, *, exempt: set[str] | None = None) -> None:
    """给应用加上请求签名校验；未签名的请求一律 401。"""
    cache = NonceCache()
    skip = set(EXEMPT_PATHS)
    if exempt:
        skip.update(exempt)

    @app.middleware("http")
    async def _guard(request: Request, call_next):  # noqa: ANN001 - FastAPI 中间件签名
        if request.url.path in skip or request.method == "OPTIONS":
            return await call_next(request)
        if not verify_request(request, cache):
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "code": "UNAUTHORIZED",
                        "message": "请求必须由 AMLauncher 启动器发起",
                    }
                },
            )
        return await call_next(request)
