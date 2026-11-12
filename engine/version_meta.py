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
# 安装全部成功后才写入的完成标记；用于区分「装到一半」的版本目录。
COMPLETE_FILENAME = ".aml-installed"


class VersionMetaError(Exception):
    pass


def complete_path(paths: GamePaths, version_id: str) -> Path:
    return paths.version_dir(version_id) / COMPLETE_FILENAME


def mark_complete(paths: GamePaths, version_id: str) -> None:
    """标记版本已安装完成；目录不存在时忽略。"""
    target = complete_path(paths, version_id)
    if target.parent.is_dir():
        target.write_text("1", encoding="utf-8")


def is_complete(paths: GamePaths, version_id: str) -> bool:
    return complete_path(paths, version_id).is_file()


def files_complete(
    paths: GamePaths, version_id: str, *, asset_cache: Optional[set[str]] = None
) -> bool:
    """核对版本声明的 jar / 库 / 资源索引与资源对象是否齐全。

    仅用于识别没有完成标记的旧版本目录：文件确实齐全的才视为已安装，
    否则（例如安装中断）不出现在版本列表里。
    ``asset_cache`` 用于在同一次扫描中复用已确认存在的资源对象。
    """
    from .version_resolver import VersionResolver  # 延迟导入，避免模块循环依赖

    # 先确认本地 JSON 与继承链完整，避免解析时回落到联网获取清单。
    chain_ids: list[str] = []
    current_id = version_id
    for _ in range(16):
        path = paths.version_json(current_id)
        if not path.is_file():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        chain_ids.append(current_id)
        parent = str(data.get("inheritsFrom") or "").strip()
        if not parent:
            break
        current_id = parent

    def _present(path: Path) -> bool:
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    try:
        resolved = VersionResolver(paths).resolve(
            version_id, include_assets=False, prefer_local=True
        )
    except Exception:  # noqa: BLE001 - 版本 JSON 缺失 / 损坏都按未完成处理
        return False
    jar_ok = False
    for entry in resolved.files:
        if entry.kind == "client":
            # 加载器版本通常没有自己的客户端 jar，jar 在继承链的父版本目录里。
            jar_ok = _present(entry.target_path) or any(
                _present(paths.version_jar(item)) for item in chain_ids
            )
            if not jar_ok:
                return False
            continue
        if not _present(entry.target_path):
            return False
    if not jar_ok and not any(_present(paths.version_jar(item)) for item in chain_ids):
        # JSON 没有声明客户端下载时，至少要求版本目录或继承链上存在 jar。
        return False
    if not resolved.asset_index_id:
        return True
    index_path = resolved.asset_index_path
    try:
        if not index_path.is_file():
            return False
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    for obj in (index.get("objects") or {}).values():
        digest = str(obj.get("hash") or "")
        if not digest:
            return False
        if asset_cache is not None and digest in asset_cache:
            continue
        if not (resolved.assets_root / "objects" / digest[:2] / digest).is_file():
            return False
        if asset_cache is not None:
            asset_cache.add(digest)
    return True


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
