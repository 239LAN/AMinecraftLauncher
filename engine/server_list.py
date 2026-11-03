"""servers.dat（gzip 压缩的 NBT）读写，用于服务器列表管理。"""

from __future__ import annotations

import gzip
import struct
from pathlib import Path
from typing import Any


class ServerListError(Exception):
    pass


TAG_END = 0
TAG_BYTE = 1
TAG_SHORT = 2
TAG_INT = 3
TAG_LONG = 4
TAG_FLOAT = 5
TAG_DOUBLE = 6
TAG_BYTE_ARRAY = 7
TAG_STRING = 8
TAG_LIST = 9
TAG_COMPOUND = 10

_FIXED = {
    TAG_BYTE: (1, ">b"),
    TAG_SHORT: (2, ">h"),
    TAG_INT: (4, ">i"),
    TAG_LONG: (8, ">q"),
    TAG_FLOAT: (4, ">f"),
    TAG_DOUBLE: (8, ">d"),
}


def _read_string(buf: bytes, pos: int) -> tuple[str, int]:
    if pos + 2 > len(buf):
        raise ServerListError("NBT 数据不完整")
    length = struct.unpack_from(">H", buf, pos)[0]
    pos += 2
    raw = buf[pos : pos + length]
    if len(raw) != length:
        raise ServerListError("NBT 字符串越界")
    return raw.decode("utf-8", errors="replace"), pos + length


def _read_payload(buf: bytes, pos: int, tag: int) -> tuple[Any, int]:
    if tag in _FIXED:
        size, fmt = _FIXED[tag]
        return struct.unpack_from(fmt, buf, pos)[0], pos + size
    if tag == TAG_STRING:
        return _read_string(buf, pos)
    if tag == TAG_BYTE_ARRAY:
        length = struct.unpack_from(">i", buf, pos)[0]
        pos += 4
        return list(buf[pos : pos + length]), pos + length
    if tag == TAG_LIST:
        child = buf[pos]
        length = struct.unpack_from(">i", buf, pos + 1)[0]
        pos += 5
        values = []
        for _ in range(max(0, length)):
            value, pos = _read_payload(buf, pos, child)
            values.append(value)
        return values, pos
    if tag == TAG_COMPOUND:
        values: dict[str, Any] = {}
        while True:
            child = buf[pos]
            pos += 1
            if child == TAG_END:
                return values, pos
            name, pos = _read_string(buf, pos)
            value, pos = _read_payload(buf, pos, child)
            values[name] = value
    raise ServerListError(f"不支持的 NBT 标签：{tag}")


def _parse(buf: bytes) -> dict[str, Any]:
    if not buf:
        return {}
    tag = buf[0]
    if tag != TAG_COMPOUND:
        raise ServerListError("servers.dat 根标签不是 Compound")
    _, pos = _read_string(buf, 1)
    value, _ = _read_payload(buf, pos, TAG_COMPOUND)
    return value if isinstance(value, dict) else {}


def _write_string(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack(">H", len(raw)) + raw


def _write_payload(tag: int, value: Any) -> bytes:
    if tag in _FIXED:
        return struct.pack(_FIXED[tag][1], value)
    if tag == TAG_STRING:
        return _write_string(str(value))
    if tag == TAG_LIST:
        child = TAG_STRING
        items = list(value or [])
        if items and isinstance(items[0], dict):
            child = TAG_COMPOUND
        elif items and isinstance(items[0], (int, float)) and not isinstance(items[0], bool):
            child = TAG_INT
        body = b"".join(_write_payload(child, item) for item in items)
        return bytes([child]) + struct.pack(">i", len(items)) + body
    if tag == TAG_COMPOUND:
        body = b""
        for key, item in (value or {}).items():
            body += bytes([_infer_tag(item)]) + _write_string(str(key)) + _write_payload(
                _infer_tag(item), item
            )
        return body + b"\x00"
    raise ServerListError(f"不支持的 NBT 标签：{tag}")


def _infer_tag(value: Any) -> int:
    if isinstance(value, str):
        return TAG_STRING
    if isinstance(value, dict):
        return TAG_COMPOUND
    if isinstance(value, list):
        return TAG_LIST
    if isinstance(value, bool):
        return TAG_BYTE
    if isinstance(value, int):
        return TAG_INT
    if isinstance(value, float):
        return TAG_DOUBLE
    return TAG_STRING


def _compound(value: dict[str, Any]) -> bytes:
    body = b""
    for key, item in value.items():
        tag = _infer_tag(item)
        body += bytes([tag]) + _write_string(str(key)) + _write_payload(tag, item)
    return body + b"\x00"


def load_servers(path: Path) -> list[dict]:
    """读取 servers.dat，返回 [{name, ip, icon, accept_textures}]。"""
    path = Path(path)
    if not path.exists():
        return []
    try:
        with gzip.open(path, "rb") as handle:
            root = _parse(handle.read())
    except (OSError, EOFError, struct.error) as exc:
        raise ServerListError(f"无法解析 servers.dat：{exc}") from exc

    servers = []
    for item in root.get("servers") or []:
        if not isinstance(item, dict):
            continue
        servers.append(
            {
                "name": str(item.get("name") or ""),
                "ip": str(item.get("ip") or ""),
                "icon": str(item.get("icon") or ""),
                "accept_textures": bool(item.get("acceptTextures") or 0),
            }
        )
    return servers


def save_servers(path: Path, servers: list[dict]) -> None:
    """把服务器列表写回 servers.dat（保持 Vanilla 的字段结构）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = []
    for server in servers:
        ip = str(server.get("ip") or "").strip()
        if not ip:
            continue
        entry = {
            "name": str(server.get("name") or ip),
            "ip": ip,
            "icon": str(server.get("icon") or ""),
            "acceptTextures": bool(server.get("accept_textures")),
        }
        entries.append(entry)
    root = {"servers": entries}
    raw = bytes([TAG_COMPOUND]) + _write_string("") + _compound(root)
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wb") as handle:
        handle.write(raw)
    tmp.replace(path)
