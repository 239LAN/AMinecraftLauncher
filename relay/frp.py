"""frp STCP 隧道配置生成与本地端口分配。"""

from __future__ import annotations

import secrets
import socket
import string
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from relay.config import RelayConfig

# 去掉 I / O / 0 / 1 等易混字符。
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
BIND_PORT_START = 25600
BIND_PORT_END = 26000


ROOM_CODE_MIN = 1000
ROOM_CODE_MAX = 9999


def new_room_id() -> str:
    """8 位大写字母数字的内部房间 ID（不对外展示，用于 API 路径与代理名）。"""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))


def new_join_code() -> str:
    """4 位数字房间号，供玩家搜索与加入。"""
    return str(secrets.randbelow(ROOM_CODE_MAX - ROOM_CODE_MIN + 1) + ROOM_CODE_MIN)


def new_secret_key() -> str:
    """STCP 代理的共享密钥。"""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(24))


def guest_suffix(guest_uuid: str) -> str:
    """访客 uuid 的 6 位短标识，用于区分同一房间内的 visitor 代理。"""
    suffix = "".join(ch for ch in guest_uuid.lower() if ch.isalnum())[:6]
    return suffix or "guest0"


def host_frpc(config: RelayConfig, room_id: str, secret_key: str, local_port: int) -> dict:
    """房主侧 frpc 配置：把本机游戏端口以 STCP 代理暴露给访客。"""
    return {
        "server_addr": config.frps_addr,
        "server_port": config.frps_port,
        "tls": True,
        "auth_token": config.frps_token,
        "proxy": {
            "name": f"aml_{room_id}",
            "type": "stcp",
            "secret_key": secret_key,
            "local_ip": "127.0.0.1",
            "local_port": local_port,
        },
    }


def visitor_frpc(
    config: RelayConfig, room_id: str, secret_key: str, guest_uuid: str, bind_port: int
) -> dict:
    """访客侧 frpc 配置：把房主的 STCP 代理映射到本机端口。"""
    return {
        "server_addr": config.frps_addr,
        "server_port": config.frps_port,
        "tls": True,
        "auth_token": config.frps_token,
        "proxy": {
            "name": f"aml_v_{room_id}_{guest_suffix(guest_uuid)}",
            "type": "stcp",
            "role": "visitor",
            "server_name": f"aml_{room_id}",
            "secret_key": secret_key,
            "bind_addr": "127.0.0.1",
            "bind_port": bind_port,
        },
    }


def port_available(port: int) -> bool:
    """检查本机 127.0.0.1 上端口能否绑定。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def pick_bind_port(used: set[int]) -> int:
    """在 25600-25999 内挑一个既未被房间占用、本机也空闲的端口。"""
    for port in range(BIND_PORT_START, BIND_PORT_END):
        if port in used:
            continue
        if port_available(port):
            return port
    raise RuntimeError("没有可用的本地端口")
