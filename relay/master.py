"""主节点（master）：合并节点注册表与各节点 meta 的节点列表接口。"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import FastAPI

from relay.config import RelayConfig
from relay.errors import register_error_handlers
from relay.signing import install_signature_guard, sign

logger = logging.getLogger(__name__)

PROBE_TIMEOUT = 3.0
CACHE_TTL = 10.0


def load_nodes(config: RelayConfig) -> list[dict]:
    """从 AML_NODES_JSON 或 AML_NODES_FILE 读取节点注册表。"""
    raw = config.nodes_json
    if not raw and config.nodes_file:
        try:
            raw = Path(config.nodes_file).read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("节点注册表读取失败：%s", exc)
            return []
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError as exc:
        logger.warning("节点注册表不是合法 JSON：%s", exc)
        return []
    if not isinstance(data, list):
        logger.warning("节点注册表应是 JSON 数组")
        return []
    nodes = [
        item
        for item in data
        if isinstance(item, dict) and item.get("id") and item.get("api")
    ]
    return nodes


def fetch_meta(api: str, timeout: float = PROBE_TIMEOUT) -> dict | None:
    """探测节点 /api/v1/meta；失败返回 None（节点视为离线）。"""
    path = "/api/v1/meta"
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(8)
    request = urllib.request.Request(
        api.rstrip("/") + path,
        headers={
            "X-AML-Ts": timestamp,
            "X-AML-Nonce": nonce,
            "X-AML-Sign": sign("GET", path, timestamp, nonce),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class MasterService:
    """并发探测节点并缓存 10 秒，避免每次请求都打到各节点。"""

    def __init__(
        self,
        nodes: list[dict],
        timeout: float = PROBE_TIMEOUT,
        cache_ttl: float = CACHE_TTL,
    ) -> None:
        self.nodes = nodes
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self._cache: list[dict] | None = None
        self._cache_at = 0.0
        self._lock = asyncio.Lock()

    async def list_nodes(self) -> list[dict]:
        if self._cache is not None and time.monotonic() - self._cache_at < self.cache_ttl:
            return self._cache
        async with self._lock:
            # 等锁期间可能已有别的请求刷新过缓存。
            if self._cache is not None and time.monotonic() - self._cache_at < self.cache_ttl:
                return self._cache
            self._cache = await asyncio.gather(*(self._probe(node) for node in self.nodes))
            self._cache_at = time.monotonic()
            return self._cache

    async def _probe(self, node: dict) -> dict:
        meta = await asyncio.to_thread(fetch_meta, str(node.get("api", "")), self.timeout)
        entry = {
            "id": node.get("id"),
            "name": node.get("name") or node.get("id"),
            "region": node.get("region") or "",
            "api": node.get("api"),
            "online": meta is not None,
            "rooms": 0,
            "max_rooms": 0,
        }
        if meta:
            entry.update(
                {
                    "node_id": meta.get("node_id") or entry["id"],
                    "name": meta.get("name") or entry["name"],
                    "region": meta.get("region") or entry["region"],
                    "rooms": int(meta.get("rooms") or 0),
                    "max_rooms": int(meta.get("max_rooms") or 0),
                }
            )
        return entry


def create_master_app(config: RelayConfig) -> FastAPI:
    """构建主节点应用，只提供节点列表与健康检查。"""
    service = MasterService(load_nodes(config))
    app = FastAPI(title="AML Relay Master", version="1.0")
    app.state.service = service
    register_error_handlers(app)
    install_signature_guard(app)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "role": "master", "nodes": len(service.nodes)}

    @app.get("/api/v1/nodes")
    async def list_nodes() -> dict:
        return {"nodes": await service.list_nodes()}

    return app
