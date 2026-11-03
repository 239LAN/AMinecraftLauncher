"""版本元数据与目录操作：自定义命名、复制、重命名。

游戏版本目录名就是版本 ID；为了让「同一原版版本 + 不同整合包 / 加载器」共存，
这里给每个版本目录写一份 ``aml_version.json``，记录真实的游戏版本与加载器，
避免自定义名称后启发式识别失效。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Optional

from .models import GamePaths

META_FILENAME = "aml_version.json"


class VersionMetaError(Exception):
    pass


def meta_path(paths: GamePaths, version_id: str) -> Path:
    return paths.version_dir(version_id) / META_FILENAME


def read_meta(paths: GamePaths, version_id: str) -> dict:
    path = meta_path(paths, version_id)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_meta(
    paths: GamePaths,
    version_id: str,
    *,
    game_version: str = "",
    loader: str = "vanilla",
    loader_version: str = "",
    pack_name: str = "",
) -> None:
    target = meta_path(paths, version_id)
    if not target.parent.is_dir():
        return
    payload = {
        "game_version": game_version,
        "loader": loader or "vanilla",
        "loader_version": loader_version,
        "pack_name": pack_name,
    }
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)


def _patch_json(path: Path, version_id: str) -> None:
    """把版本 JSON 的 id 改成目录名；JSON 不存在时直接跳过。"""
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    data["id"] = version_id
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def clone_version(paths: GamePaths, source_id: str, target_id: str, meta: dict) -> None:
    """把一个已安装版本完整复制成新的版本目录（用于同版本共存）。"""
    if not target_id or Path(target_id).name != target_id:
        raise VersionMetaError(f"版本名不合法：{target_id}")
    source = paths.version_dir(source_id)
    target = paths.version_dir(target_id)
    if not source.is_dir():
        raise VersionMetaError(f"源版本不存在：{source_id}")
    if target.exists():
        raise VersionMetaError(f"版本名已存在：{target_id}")

    shutil.copytree(source, target)
    old_json = target / f"{source_id}.json"
    new_json = target / f"{target_id}.json"
    if old_json.exists() and old_json != new_json:
        old_json.replace(new_json)
    _patch_json(new_json, target_id)
    write_meta(
        paths,
        target_id,
        game_version=str(meta.get("game_version") or ""),
        loader=str(meta.get("loader") or "vanilla"),
        loader_version=str(meta.get("loader_version") or ""),
        pack_name=str(meta.get("pack_name") or ""),
    )


def rename_version(paths: GamePaths, old_id: str, new_id: str) -> None:
    """重命名版本目录，并同步 JSON id 与元数据。"""
    if not new_id or Path(new_id).name != new_id:
        raise VersionMetaError(f"版本名不合法：{new_id}")
    if old_id == new_id:
        return
    source = paths.version_dir(old_id)
    target = paths.version_dir(new_id)
    if not source.is_dir():
        raise VersionMetaError(f"版本不存在：{old_id}")
    if target.exists():
        raise VersionMetaError(f"版本名已存在：{new_id}")

    source.replace(target)
    old_json = target / f"{old_id}.json"
    new_json = target / f"{new_id}.json"
    if old_json.exists():
        old_json.replace(new_json)
    _patch_json(new_json, new_id)
    meta = read_meta(paths, new_id)
    write_meta(
        paths,
        new_id,
        game_version=str(meta.get("game_version") or ""),
        loader=str(meta.get("loader") or "vanilla"),
        loader_version=str(meta.get("loader_version") or ""),
        pack_name=str(meta.get("pack_name") or ""),
    )


def detect_from_meta(paths: GamePaths, version_id: str) -> Optional[dict]:
    meta = read_meta(paths, version_id)
    if not meta:
        return None
    game_version = str(meta.get("game_version") or "")
    loader = str(meta.get("loader") or "vanilla")
    if not game_version and not loader:
        return None
    return {
        "game_version": game_version,
        "loader": loader,
        "loader_version": str(meta.get("loader_version") or ""),
        "pack_name": str(meta.get("pack_name") or ""),
    }
