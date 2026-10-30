"""内容管理：本地模组 / 资源包 / 光影 / 数据包 / 整合包 的扫描与增删。

设计要点：
- 本地内容按「文件」管理，类型决定所在目录（见 :func:`ContentManager.dir_for`）；
- 「启停」通过文件名后缀 ``.disabled`` 实现，禁用即重命名，启用即还原；
- 远程检索统一走 Modrinth（支持全部 5 种类型），CurseForge 仍只用于 mod；
- 存档 / 截图为只读扫描，不解析 ``level.dat``，保持零依赖。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .mod_provider import ModFile, ModSearchResult, build_provider
from .models import GamePaths

# ---------------------------------------------------------------------------
# 内容类型常量
# ---------------------------------------------------------------------------
CONTENT_MOD = "mod"
CONTENT_RESOURCEPACK = "resourcepack"
CONTENT_SHADER = "shader"
CONTENT_DATAPACK = "datapack"
CONTENT_MODPACK = "modpack"
CONTENT_KINDS = (
    CONTENT_MOD,
    CONTENT_RESOURCEPACK,
    CONTENT_SHADER,
    CONTENT_DATAPACK,
    CONTENT_MODPACK,
)

# 内容类型 -> 游戏目录下的子目录名（modpack 另行处理）。
_KIND_SUBDIR = {
    CONTENT_MOD: "mods",
    CONTENT_RESOURCEPACK: "resourcepacks",
    CONTENT_SHADER: "shaderpacks",
    CONTENT_DATAPACK: "datapacks",
}

# 视作「内容文件」的扩展名；其余（目录、临时文件）一律忽略。
_CONTENT_EXTENSIONS = {".jar", ".zip", ".mrpack"}
# 临时 / 未完成文件后缀，扫描时忽略。
_IGNORED_SUFFIXES = (".part", ".tmp")
# 禁用状态后缀：禁用即改名，启用即还原。
_DISABLED_SUFFIX = ".disabled"
# 截图扩展名。
_SCREENSHOT_EXTENSIONS = {".png", ".jpg", ".jpeg"}


@dataclass
class LocalContent:
    """某个本地内容文件（模组 / 资源包 / 光影 / 数据包 / 整合包归档）。"""

    kind: str
    filename: str
    path: Path
    enabled: bool = True
    size: int = 0
    modified: float = 0.0


@dataclass
class SaveEntry:
    """单个存档目录。"""

    name: str
    path: Path
    size: int = 0
    modified: float = 0.0


@dataclass
class ScreenshotEntry:
    """单张截图文件。"""

    name: str
    path: Path
    size: int = 0
    modified: float = 0.0


class ContentError(Exception):
    """内容管理相关错误（未知类型、文件不存在、目标已存在等）。"""


def _stat_meta(path: Path) -> tuple[int, float]:
    """读取文件大小与修改时间，文件缺失时返回 (0, 0.0)。"""
    try:
        stat = path.stat()
    except OSError:
        return 0, 0.0
    return stat.st_size, stat.st_mtime


class ContentManager:
    """本地内容 + 存档 / 截图的统一入口，同时代理远程检索。"""

    def __init__(self, paths: GamePaths) -> None:
        self._paths = paths

    # ------------------------------------------------------------------ 目录
    def dir_for(
        self,
        kind: str,
        *,
        version_id: Optional[str] = None,
        isolated: bool = False,
    ) -> Path:
        """返回某类型内容的本地目录。

        - modpack 归档固定放在 ``root/modpacks``；
        - 其余类型在版本隔离时用版本目录作为游戏目录，否则用全局根目录。
        """
        if kind == CONTENT_MODPACK:
            return self._paths.root / "modpacks"
        if kind not in _KIND_SUBDIR:
            raise ContentError(f"未知的内容类型：{kind}")
        if isolated and version_id:
            base = self._paths.version_game_dir(version_id, isolated=True)
        else:
            base = self._paths.root
        return base / _KIND_SUBDIR[kind]

    # ------------------------------------------------------------------ 本地
    def list_local(
        self,
        kind: str,
        *,
        version_id: Optional[str] = None,
        isolated: bool = False,
    ) -> list[LocalContent]:
        """列出某类型目录下的内容文件（含被禁用的 ``.disabled`` 文件）。"""
        directory = self.dir_for(kind, version_id=version_id, isolated=isolated)
        items: list[LocalContent] = []
        if not directory.is_dir():
            return items

        for entry in directory.iterdir():
            if not entry.is_file():
                continue
            name = entry.name
            if name.endswith(_IGNORED_SUFFIXES):
                continue

            enabled = True
            base = name
            if base.endswith(_DISABLED_SUFFIX):
                enabled = False
                base = base[: -len(_DISABLED_SUFFIX)]
            if Path(base).suffix.lower() not in _CONTENT_EXTENSIONS:
                continue

            size, modified = _stat_meta(entry)
            items.append(
                LocalContent(
                    kind=kind,
                    filename=base,
                    path=entry,
                    enabled=enabled,
                    size=size,
                    modified=modified,
                )
            )

        items.sort(key=lambda item: item.filename.lower())
        return items

    def set_enabled(
        self,
        kind: str,
        filename: str,
        enabled: bool,
        *,
        version_id: Optional[str] = None,
        isolated: bool = False,
    ) -> LocalContent:
        """启用 / 禁用内容文件（重命名在 ``foo.jar`` 与 ``foo.jar.disabled`` 间切换）。"""
        directory = self.dir_for(kind, version_id=version_id, isolated=isolated)
        active = directory / filename
        disabled = directory / f"{filename}{_DISABLED_SUFFIX}"

        if enabled:
            if not active.exists() and not disabled.exists():
                raise ContentError(f"内容不存在：{filename}")
            if disabled.exists():
                disabled.replace(active)
            path = active
        else:
            if not active.exists() and not disabled.exists():
                raise ContentError(f"内容不存在：{filename}")
            if active.exists():
                active.replace(disabled)
            path = disabled

        size, modified = _stat_meta(path)
        return LocalContent(
            kind=kind,
            filename=filename,
            path=path,
            enabled=enabled,
            size=size,
            modified=modified,
        )

    def delete(
        self,
        kind: str,
        filename: str,
        *,
        version_id: Optional[str] = None,
        isolated: bool = False,
    ) -> None:
        """删除内容文件（无论当前是启用还是禁用状态）。"""
        directory = self.dir_for(kind, version_id=version_id, isolated=isolated)
        targets = (directory / filename, directory / f"{filename}{_DISABLED_SUFFIX}")
        removed = False
        for target in targets:
            if target.exists():
                target.unlink()
                removed = True
        if not removed:
            raise ContentError(f"内容不存在：{filename}")

    def import_file(
        self,
        kind: str,
        source: Path,
        *,
        version_id: Optional[str] = None,
        isolated: bool = False,
        overwrite: bool = False,
    ) -> Path:
        """把 ``source`` 复制到目标目录，文件名沿用 ``source.name``，返回目标路径。"""
        source = Path(source)
        if not source.is_file():
            raise ContentError(f"源文件不存在：{source}")

        directory = self.dir_for(kind, version_id=version_id, isolated=isolated)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / source.name
        if target.exists() and not overwrite:
            raise ContentError(f"目标已存在：{source.name}")

        shutil.copy2(source, target)
        return target

    # ------------------------------------------------------------------ 存档
    def list_saves(
        self,
        *,
        version_id: Optional[str] = None,
        isolated: bool = False,
    ) -> list[SaveEntry]:
        """列出存档目录下的每个子目录；大小取子目录内文件总和，时间为目录 mtime。"""
        if isolated and version_id:
            directory = self._paths.saves_dir_for(version_id, isolated=True)
        else:
            directory = self._paths.saves_dir

        entries: list[SaveEntry] = []
        if not directory.is_dir():
            return entries

        for child in directory.iterdir():
            if not child.is_dir():
                continue
            total = 0
            for item in child.rglob("*"):
                if item.is_file():
                    size, _ = _stat_meta(item)
                    total += size
            modified = _stat_meta(child)[1]
            entries.append(
                SaveEntry(name=child.name, path=child, size=total, modified=modified)
            )

        entries.sort(key=lambda entry: entry.name.lower())
        return entries

    def delete_save(
        self,
        name: str,
        *,
        version_id: Optional[str] = None,
        isolated: bool = False,
    ) -> None:
        """删除整个存档目录。"""
        if isolated and version_id:
            directory = self._paths.saves_dir_for(version_id, isolated=True)
        else:
            directory = self._paths.saves_dir

        target = directory / name
        if not target.is_dir():
            raise ContentError(f"存档不存在：{name}")
        shutil.rmtree(target)

    # ------------------------------------------------------------------ 截图
    def list_screenshots(
        self,
        *,
        version_id: Optional[str] = None,
        isolated: bool = False,
    ) -> list[ScreenshotEntry]:
        """列出全局截图目录下的图片文件（截图为全局资源，不受版本隔离影响）。"""
        directory = self._paths.screenshots_dir
        entries: list[ScreenshotEntry] = []
        if not directory.is_dir():
            return entries

        for child in directory.iterdir():
            if not child.is_file():
                continue
            if child.suffix.lower() not in _SCREENSHOT_EXTENSIONS:
                continue
            size, modified = _stat_meta(child)
            entries.append(
                ScreenshotEntry(name=child.name, path=child, size=size, modified=modified)
            )

        entries.sort(key=lambda entry: entry.name.lower())
        return entries

    # ------------------------------------------------------------------ 远程
    def search(
        self,
        kind: str,
        query: str,
        *,
        limit: int = 20,
        offset: int = 0,
        game_version: Optional[str] = None,
        loader: Optional[str] = None,
        source: str = "modrinth",
    ) -> list[ModSearchResult]:
        """远程检索内容；Modrinth 按 ``kind`` 映射 project_type，CurseForge 仅检索 mod。"""
        if kind not in CONTENT_KINDS:
            raise ContentError(f"未知的内容类型：{kind}")
        provider = build_provider(source)
        return provider.search(
            query,
            limit=limit,
            offset=offset,
            game_version=game_version,
            loader=loader,
            project_type=kind,
        )

    def resolve_files(
        self,
        kind: str,
        project_id: str,
        *,
        game_version: Optional[str] = None,
        loader: Optional[str] = None,
        source: str = "modrinth",
    ) -> list[ModFile]:
        """解析某项目在指定游戏版本 / 加载器下可下载的文件列表。"""
        if kind not in CONTENT_KINDS:
            raise ContentError(f"未知的内容类型：{kind}")
        provider = build_provider(source)
        return provider.resolve_files(
            project_id,
            game_version=game_version,
            loader=loader,
        )