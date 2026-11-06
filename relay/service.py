"""节点业务逻辑：房间、访客、心跳与会话。"""

from __future__ import annotations

import json
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timezone

from relay import frp
from relay.auth import SESSION_TTL, hash_token, issue_session
from relay.config import RelayConfig
from relay.errors import RelayError
from relay.store import RelayStore

# 服务商：目前仅 AMLauncher 官方可用，其余保留占位避免客户端乱传。
PROVIDERS = {
    "aml": "AMLauncher 官方",
    "chmlfrp": "ChMLFRP",
    "loliafrp": "LoliaFRP",
}
AVAILABLE_PROVIDERS = {"aml"}
MAX_MODS = 300
MAX_MODS_BYTES = 64 * 1024
MIN_PLAYERS = 2
MAX_PLAYERS = 16


def iso_time(timestamp: float) -> str:
    """时间戳转 ISO 8601（UTC），便于客户端直接展示。"""
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_code(code: str) -> str:
    """把用户输入统一成 4 位数字房间号。"""
    raw = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(raw) != 4 or not (frp.ROOM_CODE_MIN <= int(raw) <= frp.ROOM_CODE_MAX):
        raise RelayError("ROOM_NOT_FOUND", "房间号应为 4 位数字", 404)
    return raw


class NodeService:
    """子节点服务；所有公开方法都在线程池中调用，内部用存储层锁保证一致。"""

    def __init__(self, config: RelayConfig, store: RelayStore) -> None:
        self.config = config
        self.store = store
        self._lookup_lock = threading.Lock()
        self._lookup_failures: dict[str, deque[float]] = {}

    # ---- 鉴权 ----

    def create_session(self, name: str = "") -> dict:
        """签发 relay 会话令牌；联机不再要求正版账号。"""
        display = str(name or "").strip()[:32] or "玩家"
        uuid = f"c-{secrets.token_hex(8)}"
        token, expires_at = issue_session()
        self.store.save_session(hash_token(token), uuid, display, expires_at)
        return {
            "session_token": token,
            "uuid": uuid,
            "name": display,
            "expires_in": SESSION_TTL,
        }

    def authenticate(self, authorization: str | None) -> dict:
        """解析 Authorization: Bearer <token>，返回会话信息。"""
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise RelayError("UNAUTHORIZED", "缺少 Bearer 会话令牌", 401)
        session = self.store.get_session(hash_token(token.strip()), time.time())
        if not session:
            raise RelayError("UNAUTHORIZED", "会话令牌无效或已过期", 401)
        return session

    # ---- 节点信息 ----

    def meta(self) -> dict:
        # 不返回 frps 地址 / 端口，避免公开接口泄露节点入口。
        return {
            "node_id": self.config.node_id,
            "name": self.config.node_name,
            "region": self.config.region,
            "rooms": self.store.count_open_rooms(),
            "max_rooms": self.config.max_rooms_per_host,
        }

    # ---- 房间 ----

    def create_room(
        self,
        session: dict,
        *,
        name: str,
        version: str,
        loader: str,
        max_players: int,
        local_port: int,
        mode: str,
        provider: str = "aml",
        hidden: bool = False,
        mods: list[dict] | None = None,
        mods_declared: bool = False,
        client_ip: str = "",
    ) -> dict:
        if mode != "stcp":
            raise RelayError("UNSUPPORTED_MODE", "当前仅支持 stcp 联机模式", 400)
        provider = str(provider or "aml").strip().lower()
        if provider not in PROVIDERS:
            raise RelayError("UNSUPPORTED_PROVIDER", "未知的联机服务提供商", 400)
        if provider not in AVAILABLE_PROVIDERS:
            raise RelayError(
                "PROVIDER_UNAVAILABLE",
                f"{PROVIDERS[provider]} 暂不可用，请选择 AMLauncher 官方",
                400,
            )
        if not (MIN_PLAYERS <= int(max_players) <= min(MAX_PLAYERS, self.config.max_guests)):
            raise RelayError(
                "MAX_GUESTS_EXCEEDED",
                f"房间人数需在 {MIN_PLAYERS}-{min(MAX_PLAYERS, self.config.max_guests)} 人之间",
                400,
            )
        if not (1024 <= int(local_port) <= 65535):
            raise RelayError("INVALID_PORT", "本地端口不合法", 400)
        if self.store.count_host_rooms(session["uuid"]) >= self.config.max_rooms_per_host:
            raise RelayError(
                "ROOM_LIMIT",
                f"同一账号最多同时开启 {self.config.max_rooms_per_host} 个房间",
                409,
            )
        ip = str(client_ip or "").strip()
        if ip and self.store.count_ip_rooms(ip) >= self.config.max_rooms_per_ip:
            raise RelayError(
                "IP_LIMIT",
                f"同一网络最多同时开启 {self.config.max_rooms_per_ip} 个房间",
                409,
            )

        room_mods = self._normalize_mods(mods)
        now = time.time()
        room_id, join_code = self._unique_ids()
        room = {
            "room_id": room_id,
            "join_code": join_code,
            "host_uuid": session["uuid"],
            "host_name": session["name"],
            "host_ip": ip,
            "name": name,
            "version": version,
            "loader": loader,
            "max_players": int(max_players),
            "status": "open",
            "node_id": self.config.node_id,
            "local_port": int(local_port),
            "secret_key": frp.new_secret_key(),
            "hidden": 1 if hidden else 0,
            "provider": provider,
            "mods": json.dumps(room_mods, ensure_ascii=False, separators=(",", ":")),
            "mods_declared": 1 if (mods_declared or room_mods) else 0,
            "created_at": now,
            "updated_at": now,
            "expires_at": now + self.config.room_ttl,
            "max_expires_at": now + self.config.room_ttl * 2,
        }
        self.store.create_room(room)
        return {
            "room_id": room_id,
            "join_code": join_code,
            "expires_at": iso_time(room["expires_at"]),
            "hidden": bool(hidden),
            "provider": provider,
            "host_frpc": frp.host_frpc(self.config, room_id, room["secret_key"], int(local_port)),
        }

    def _unique_ids(self) -> tuple[str, str]:
        """生成不冲突的内部 ID 与 4 位数字房间号。"""
        for _ in range(16):
            room_id = frp.new_room_id()
            if self.store.get_room(room_id):
                continue
            used = self.store.used_join_codes()
            available = [
                str(number)
                for number in range(frp.ROOM_CODE_MIN, frp.ROOM_CODE_MAX + 1)
                if str(number) not in used
            ]
            if not available:
                raise RelayError("ID_EXHAUSTED", "房间号已用尽，请稍后重试", 503)
            join_code = frp.new_join_code()
            if join_code not in available:
                join_code = available[0]
            return room_id, join_code
        raise RelayError("ID_EXHAUSTED", "房间号生成失败，请稍后重试", 503)

    def list_public(self, session: dict, limit: int = 60) -> list[dict]:
        """联机页公开房间列表（隐藏房间不出现）。"""
        del session  # 仅要求已登录，防止匿名抓取
        now = time.time()
        rooms = self.store.list_public_rooms(now, self.config.heartbeat_timeout, limit)
        return [self._summary(room) for room in rooms]

    def lookup(self, code: str, *, client_ip: str = "") -> dict:
        ip = client_ip or "unknown"
        self._check_lookup_limit(ip)
        try:
            room = self._room_by_code(code)
        except RelayError:
            self._record_lookup_failure(ip)
            raise
        return self._summary(room, include_mods=True)

    def _check_lookup_limit(self, ip: str) -> None:
        """同一 IP 在窗口期内失败次数过多时直接拒绝，降低房间号穷举。"""
        now = time.time()
        window = float(self.config.lookup_window)
        with self._lookup_lock:
            failures = self._lookup_failures.setdefault(ip, deque())
            while failures and failures[0] < now - window:
                failures.popleft()
            if len(failures) >= self.config.lookup_fail_limit:
                raise RelayError(
                    "RATE_LIMITED",
                    "房间号查询过于频繁，请稍后再试",
                    429,
                )

    def _record_lookup_failure(self, ip: str) -> None:
        with self._lookup_lock:
            self._lookup_failures.setdefault(ip, deque()).append(time.time())

    def join(self, session: dict, room_id: str, bind_port: int | None = None) -> dict:
        room = self._room(room_id)
        existing = self.store.get_guest(room_id, session["uuid"])
        if bind_port is None:
            bind_port = (existing or {}).get("bind_port") or self._pick_port()

        now = time.time()
        guest = {
            "guest_uuid": session["uuid"],
            "guest_name": session["name"],
            "bind_port": bind_port,
            "joined_at": (existing or {}).get("joined_at", now),
            "updated_at": now,
        }
        if not self.store.upsert_guest(room_id, guest, room["max_players"]):
            raise RelayError("ROOM_FULL", "房间人数已满", 409)

        return {
            "room": self._summary(self.store.get_room(room_id) or room, include_mods=True),
            "visitor_frpc": frp.visitor_frpc(
                self.config, room_id, room["secret_key"], session["uuid"], bind_port
            ),
            "bind_port": bind_port,
        }

    def _pick_port(self) -> int:
        used = self.store.used_bind_ports()
        try:
            return frp.pick_bind_port(used)
        except RuntimeError as exc:
            raise RelayError("NO_FREE_PORT", str(exc), 503) from exc

    def heartbeat(self, session: dict, room_id: str) -> dict:
        room = self._host_room(session, room_id)
        now = time.time()
        # 只延长不缩短，且总时长不超过创建后的 2 倍 TTL。
        expires_at = min(max(now + self.config.room_ttl, room["expires_at"]), room["max_expires_at"])
        self.store.update_room_expiry(room_id, expires_at, now)
        return {
            "status": "open",
            "players": 1 + self.store.count_guests(room_id),
            "expires_at": iso_time(expires_at),
        }

    def leave(self, session: dict, room_id: str) -> None:
        self.store.remove_guest(room_id, session["uuid"])

    def close(self, session: dict, room_id: str) -> None:
        room = self._host_room(session, room_id)
        self.store.set_room_status(room["room_id"], "closed")

    def detail(self, session: dict, room_id: str) -> dict:
        room = self._host_room(session, room_id)
        guests = [
            {"name": guest["guest_name"], "joined_at": iso_time(guest["joined_at"])}
            for guest in self.store.list_guests(room_id)
        ]
        data = self._summary(room, include_mods=True)
        data.update(
            {
                "join_code": room["join_code"],
                "local_port": room["local_port"],
                "hidden": bool(room.get("hidden")),
                "guests": guests,
            }
        )
        return data

    def room_info(self, session: dict, room_id: str) -> dict:
        """已登录玩家查看房间详情（用于从列表加入时获取模组清单）。"""
        del session
        return self._summary(self._room(room_id), include_mods=True)

    # ---- 内部工具 ----

    def _summary(self, room: dict, *, include_mods: bool = False) -> dict:
        mods = self._room_mods(room)
        data = {
            "room_id": room["room_id"],
            "name": room["name"],
            "host_name": room["host_name"],
            "version": room["version"],
            "loader": room["loader"],
            "players": 1 + self.store.count_guests(room["room_id"]),
            "max_players": room["max_players"],
            "status": room["status"],
            "node_id": room["node_id"],
            "provider": room.get("provider") or "aml",
            "hidden": bool(room.get("hidden")),
            "mod_count": len(mods),
            "mods_declared": bool(room.get("mods_declared")),
            "expires_at": iso_time(room["expires_at"]),
        }
        if include_mods:
            data["mods"] = mods
        return data

    def _room_mods(self, room: dict) -> list[dict]:
        try:
            data = json.loads(room.get("mods") or "[]")
        except ValueError:
            return []
        return data if isinstance(data, list) else []

    def _normalize_mods(self, mods: list[dict] | None) -> list[dict]:
        """限制长度与字段大小，避免房间元数据被滥用。"""
        if not mods:
            return []
        if not isinstance(mods, (list, tuple)):
            raise RelayError("INVALID_MODS", "模组列表格式不正确", 400)
        result: list[dict] = []
        for item in list(mods)[:MAX_MODS]:
            if not isinstance(item, dict):
                continue
            entry: dict = {}
            for key, limit in (
                ("filename", 200),
                ("name", 200),
                ("version", 80),
                ("sha1", 64),
                ("modrinth_url", 300),
                ("curseforge_url", 300),
                ("download_url", 500),
            ):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    entry[key] = value.strip()[:limit]
            if not entry.get("filename") and not entry.get("name"):
                continue
            entry["enabled"] = bool(item.get("enabled", True))
            result.append(entry)
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_MODS_BYTES:
            raise RelayError("MODS_TOO_LARGE", "模组列表过大", 400)
        return result

    def _room_by_code(self, code: str) -> dict:
        room = self.store.get_room_by_code(normalize_code(code))
        if not room:
            raise RelayError("ROOM_NOT_FOUND", "房间不存在", 404)
        return self._ensure_active(room)

    def _room(self, room_id: str) -> dict:
        room = self.store.get_room(room_id)
        if not room:
            raise RelayError("ROOM_NOT_FOUND", "房间不存在", 404)
        return self._ensure_active(room)

    def _host_room(self, session: dict, room_id: str) -> dict:
        room = self._room(room_id)
        if room["host_uuid"] != session["uuid"]:
            raise RelayError("FORBIDDEN", "只有房主可以执行该操作", 403)
        return room

    def _ensure_active(self, room: dict) -> dict:
        if room["status"] == "closed":
            raise RelayError("ROOM_CLOSED", "房间已关闭", 410)
        now = time.time()
        # 心跳超时同样视为过期，后台任务会补记状态。
        stale = room["updated_at"] + self.config.heartbeat_timeout <= now
        if room["status"] != "open" or room["expires_at"] <= now or stale:
            if room["status"] == "open":
                self.store.set_room_status(room["room_id"], "expired")
            raise RelayError("ROOM_EXPIRED", "房间已过期", 410)
        return room

    def expire_rooms(self) -> int:
        return self.store.expire_stale_rooms(time.time(), self.config.heartbeat_timeout)
