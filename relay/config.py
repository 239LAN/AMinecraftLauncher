"""relay 服务配置，全部来自环境变量，便于用 systemd 的 EnvironmentFile 注入。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# 两种角色的默认监听端口。
DEFAULT_NODE_PORT = 3375
DEFAULT_MASTER_PORT = 3380


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class RelayConfig:
    """relay 运行配置。"""

    role: str = "node"
    api_port: int = DEFAULT_NODE_PORT
    node_id: str = "node-1"
    node_name: str = "AML 节点"
    region: str = "cn"
    # 节点对外可访问的 API 地址，写进 meta 供主节点展示。
    public_api_url: str = ""
    frps_addr: str = "127.0.0.1"
    frps_port: int = 3300
    frps_token: str = ""
    db_path: Path = Path("relay.db")
    room_ttl: int = 7200
    heartbeat_timeout: int = 45
    max_rooms_per_host: int = 1
    # 同一公网 IP 同时只能有一个房间，防止刷房间占号。
    max_rooms_per_ip: int = 1
    max_guests: int = 16
    # 房间号只有 4 位，按 IP 限制失败的查询次数，降低穷举风险。
    lookup_fail_limit: int = 10
    lookup_window: int = 60
    # 主节点使用：节点注册表（JSON 文件或内联 JSON 数组）。
    nodes_file: str = ""
    nodes_json: str = ""

    @property
    def is_master(self) -> bool:
        return self.role == "master"


def load_config() -> RelayConfig:
    """从环境变量读取配置；未设置时按角色取默认端口。"""
    role = os.environ.get("AML_ROLE", "node").strip().lower() or "node"
    if role not in {"master", "node"}:
        role = "node"
    default_port = DEFAULT_MASTER_PORT if role == "master" else DEFAULT_NODE_PORT

    api_port = _env_int("AML_API_PORT", default_port)
    frps_port = _env_int("AML_FRPS_PORT", 3300)
    room_ttl = _env_int("AML_ROOM_TTL", 7200)
    heartbeat_timeout = _env_int("AML_HEARTBEAT_TIMEOUT", 45)
    max_rooms = _env_int("AML_MAX_ROOMS_PER_HOST", 1)
    max_rooms_per_ip = max(1, _env_int("AML_MAX_ROOMS_PER_IP", 1))
    max_guests = max(2, min(16, _env_int("AML_MAX_GUESTS", 16)))
    lookup_fail_limit = max(3, _env_int("AML_LOOKUP_FAIL_LIMIT", 10))
    lookup_window = max(10, _env_int("AML_LOOKUP_WINDOW", 60))

    return RelayConfig(
        role=role,
        api_port=api_port,
        node_id=os.environ.get("AML_NODE_ID", "node-1").strip() or "node-1",
        node_name=os.environ.get("AML_NODE_NAME", "AML 节点").strip() or "AML 节点",
        region=os.environ.get("AML_REGION", "cn").strip() or "cn",
        public_api_url=os.environ.get("AML_PUBLIC_API_URL", "").strip(),
        frps_addr=os.environ.get("AML_FRPS_ADDR", "127.0.0.1").strip() or "127.0.0.1",
        frps_port=frps_port,
        frps_token=os.environ.get("AML_FRPS_TOKEN", ""),
        db_path=Path(os.environ.get("AML_DB_PATH", "relay.db")),
        room_ttl=room_ttl,
        heartbeat_timeout=heartbeat_timeout,
        max_rooms_per_host=max_rooms,
        max_rooms_per_ip=max_rooms_per_ip,
        max_guests=max_guests,
        lookup_fail_limit=lookup_fail_limit,
        lookup_window=lookup_window,
        nodes_file=os.environ.get("AML_NODES_FILE", "").strip(),
        nodes_json=os.environ.get("AML_NODES_JSON", "").strip(),
    )
