"""读取 Mod JAR 元数据（名称 / 版本 / 图标），供本地 Mod 列表展示。

支持 Fabric / Quilt 的 JSON、Forge / NeoForge 的 TOML、以及 MANIFEST 回退。
结果按 (路径, 大小, mtime) 缓存，避免每次刷新都重新解压。
"""

from __future__ import annotations

import hashlib
import json
import threading
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_ICON_MAX = 512 * 1024
_cache: dict[tuple[str, int, float], "ModInfo"] = {}
_sha1_cache: dict[tuple[str, int, float], str] = {}
_lock = threading.Lock()

_FABRIC = ("fabric.mod.json", "quilt.mod.json")
_FORGE = ("META-INF/neoforge.mods.toml", "META-INF/mods.toml")
_MANIFEST = "META-INF/MANIFEST.MF"


@dataclass
class ModInfo:
    name: str
    version: str = ""
    mod_id: str = ""
    loader: str = ""
    icon: Optional[bytes] = None
    icon_ext: str = "png"


def read_mod_info(path: Path) -> ModInfo:
    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        return ModInfo(name=path.name)
    key = (str(path), stat.st_size, stat.st_mtime)
    with _lock:
        cached = _cache.get(key)
    if cached is not None:
        return cached

    info = _read(path)
    with _lock:
        _cache[key] = info
    return info


def sha1_of(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cached_sha1(path: Path) -> str:
    """按 (大小, mtime) 缓存 SHA1，避免列表刷新时重复读整个 JAR。"""
    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        return ""
    key = (str(path), stat.st_size, stat.st_mtime)
    with _lock:
        cached = _sha1_cache.get(key)
    if cached:
        return cached
    digest = sha1_of(path)
    with _lock:
        _sha1_cache[key] = digest
    return digest


def _read(path: Path) -> ModInfo:
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            for candidate in _FABRIC:
                if candidate in names:
                    return _from_fabric(archive, candidate, path)
            for candidate in _FORGE:
                if candidate in names:
                    return _from_toml(archive, candidate, path)
            if "mcmod.info" in names:
                return _from_mcmod(archive, path)
            if _MANIFEST in names:
                return _from_manifest(archive, path)
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError, tomllib.TOMLDecodeError):
        pass
    return ModInfo(name=path.stem)


def _icon_from(archive: zipfile.ZipFile, name: str) -> tuple[Optional[bytes], str]:
    if not name:
        return None, "png"
    normalized = name.replace("\\", "/")
    try:
        data = archive.read(normalized)
    except KeyError:
        return None, "png"
    if not data or len(data) > _ICON_MAX:
        return None, "png"
    ext = Path(normalized).suffix.lower().lstrip(".") or "png"
    if ext not in {"png", "jpg", "jpeg", "gif", "webp"}:
        return None, "png"
    return data, "jpeg" if ext == "jpg" else ext


def _from_fabric(archive: zipfile.ZipFile, member: str, path: Path) -> ModInfo:
    data = json.loads(archive.read(member).decode("utf-8", errors="replace"))
    icon = data.get("icon")
    if isinstance(icon, dict):
        icon = icon.get("64") or icon.get("128") or next(iter(icon.values()), "")
    icon_bytes, ext = _icon_from(archive, str(icon or ""))
    loader = "quilt" if member.startswith("quilt") else "fabric"
    return ModInfo(
        name=str(data.get("name") or data.get("id") or path.stem),
        version=str(data.get("version") or ""),
        mod_id=str(data.get("id") or ""),
        loader=loader,
        icon=icon_bytes,
        icon_ext=ext,
    )


def _from_toml(archive: zipfile.ZipFile, member: str, path: Path) -> ModInfo:
    data = tomllib.loads(archive.read(member).decode("utf-8", errors="replace"))
    mods = data.get("mods") or []
    first = mods[0] if mods else {}
    icon_bytes, ext = _icon_from(archive, str(first.get("logoFile") or ""))
    loader = "neoforge" if "neoforge" in member else "forge"
    return ModInfo(
        name=str(first.get("displayName") or first.get("modId") or path.stem),
        version=str(first.get("version") or ""),
        mod_id=str(first.get("modId") or ""),
        loader=loader,
        icon=icon_bytes,
        icon_ext=ext,
    )


def _from_mcmod(archive: zipfile.ZipFile, path: Path) -> ModInfo:
    data = json.loads(archive.read("mcmod.info").decode("utf-8", errors="replace"))
    if isinstance(data, dict):
        data = data.get("modList") or [data]
    first = data[0] if data else {}
    icon_bytes, ext = _icon_from(archive, str(first.get("logoFile") or ""))
    return ModInfo(
        name=str(first.get("name") or first.get("modid") or path.stem),
        version=str(first.get("version") or ""),
        mod_id=str(first.get("modid") or ""),
        loader="forge",
        icon=icon_bytes,
        icon_ext=ext,
    )


def _from_manifest(archive: zipfile.ZipFile, path: Path) -> ModInfo:
    text = archive.read(_MANIFEST).decode("utf-8", errors="replace")
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip().lower()] = value.strip()
    return ModInfo(
        name=fields.get("implementation-title") or path.stem,
        version=fields.get("implementation-version") or "",
    )
