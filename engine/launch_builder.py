"""LaunchBuilder：把解析结果拼成 Java 命令行，并导出原生库。"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import Optional, Sequence

from .models import Account, GamePaths, ResolvedVersion
from .rules import rules_allow
from .version import APP_NAME as LAUNCHER_NAME
from .version import APP_VERSION as LAUNCHER_VERSION

_UNRESOLVED = re.compile(r"\$\{[^}]+\}")

DEFAULT_WIDTH = 854
DEFAULT_HEIGHT = 480


def offline_uuid(username: str) -> str:
    """离线模式 UUID：等价于 Java 的 UUID.nameUUIDFromBytes("OfflinePlayer:"+name)。"""
    digest = bytearray(hashlib.md5(f"OfflinePlayer:{username}".encode("utf-8")).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30  # version 3
    digest[8] = (digest[8] & 0x3F) | 0x80  # IETF variant
    return str(uuid.UUID(bytes=bytes(digest)))


class LaunchBuilder:
    def __init__(self, paths: GamePaths) -> None:
        self.paths = paths

    def build(
        self,
        resolved: ResolvedVersion,
        account: Account,
        *,
        memory_mb: Optional[int] = None,
        min_memory_mb: Optional[int] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        extra_jvm: Optional[Sequence[str]] = None,
        extra_game: Optional[Sequence[str]] = None,
        prepare_natives: bool = True,
        game_directory: Optional[Path] = None,
        java: Optional[str] = None,
    ) -> list[str]:
        """输出完整命令行 list[str]，供 subprocess.Popen 直接使用。

        ``game_directory`` 用于版本隔离：开启隔离时游戏目录为该版本的目录，
        而不是游戏根目录。``java`` 用于按版本指定 Java 可执行文件。
        """
        if prepare_natives:
            self.extract_natives(resolved)

        mapping = self._placeholder_map(resolved, account, width, height, game_directory)
        substitute = _make_substituter(mapping)
        features = {
            "is_demo_user": False,
            "has_custom_resolution": bool(width and height),
            "has_quick_plays_support": False,
            "is_quick_play_singleplayer": False,
            "is_quick_play_multiplayer": False,
            "is_quick_play_realms": False,
        }

        jvm: list[str] = []
        if memory_mb:
            jvm.append(f"-Xmx{int(memory_mb)}M")
        if min_memory_mb:
            jvm.append(f"-Xms{int(min_memory_mb)}M")

        version_jvm = self._resolve_args(resolved, resolved.jvm_args, features)
        if version_jvm:
            jvm.extend(substitute(arg) for arg in version_jvm)
        else:
            # 旧版本（<=1.12）没有 arguments.jvm，手工补默认项。
            jvm.extend(
                [
                    f"-Djava.library.path={resolved.natives_dir}",
                    "-cp",
                    mapping["classpath"],
                ]
            )

        if resolved.log_config and resolved.log_config.get("argument"):
            jvm.append(substitute(resolved.log_config["argument"]))

        if extra_jvm:
            jvm.extend(extra_jvm)

        game = [
            substitute(arg)
            for arg in self._resolve_args(resolved, resolved.game_args, features)
        ]
        if extra_game:
            game.extend(extra_game)

        return [java or self.paths.java, *jvm, resolved.main_class, *game]

    def extract_natives(self, resolved: ResolvedVersion) -> Path:
        """清空并重建 natives 目录，解压所有原生库。"""
        natives_dir = resolved.natives_dir
        if natives_dir.exists():
            shutil.rmtree(natives_dir)
        natives_dir.mkdir(parents=True, exist_ok=True)

        for lib in resolved.libraries:
            native = lib.native_jar
            if not native:
                continue
            native_path = Path(native.target_path)
            if not native_path.exists():
                continue
            with zipfile.ZipFile(native_path) as archive:
                for member in archive.namelist():
                    if member.endswith("/"):
                        continue
                    if any(member.startswith(prefix) for prefix in lib.exclude):
                        continue
                    archive.extract(member, natives_dir)
        return natives_dir

    def describe(self, command: Sequence[str]) -> str:
        """用于日志面板展示的可读命令（路径含空格时加引号）。"""
        return " ".join(f'"{part}"' if " " in part else part for part in command)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _resolve_args(
        resolved: ResolvedVersion, args: list, features: dict[str, bool]
    ) -> list[str]:
        """按 rules 过滤启动参数，并展开 dict 形式的条目。"""
        collected: list[str] = []
        for arg in args:
            if isinstance(arg, str):
                collected.append(arg)
                continue
            if not isinstance(arg, dict):
                continue
            if not rules_allow(
                arg.get("rules"), resolved.os_name, resolved.os_arch, features
            ):
                continue
            value = arg.get("value")
            if isinstance(value, list):
                collected.extend(str(item) for item in value)
            elif value is not None:
                collected.append(str(value))
        return collected

    def _placeholder_map(
        self,
        resolved: ResolvedVersion,
        account: Account,
        width: Optional[int],
        height: Optional[int],
        game_directory: Optional[Path] = None,
    ) -> dict[str, str]:
        log_path = ""
        if resolved.log_config and resolved.log_config.get("file"):
            log_path = str(
                resolved.assets_root / "log_configs" / str(resolved.log_config["file"].get("id"))
            )

        return {
            "auth_player_name": account.username,
            "auth_uuid": account.uuid,
            "auth_access_token": account.access_token or "0",
            "auth_session": account.access_token or "-",
            "user_type": account.user_type,
            "version_name": resolved.id,
            "version_type": resolved.version_type,
            "game_directory": str(game_directory or self.paths.root),
            "assets_root": str(resolved.assets_root),
            "game_assets": str(resolved.assets_root / "virtual" / "legacy"),
            "assets_index_name": resolved.asset_index_id,
            "natives_directory": str(resolved.natives_dir),
            "classpath": os.pathsep.join(str(path) for path in resolved.classpath),
            "classpath_separator": os.pathsep,
            "library_directory": str(self.paths.libraries_dir),
            "launcher_name": LAUNCHER_NAME,
            "launcher_version": LAUNCHER_VERSION,
            "user_properties": "{}",
            "resolution_width": str(width or DEFAULT_WIDTH),
            "resolution_height": str(height or DEFAULT_HEIGHT),
            "path": log_path,
        }


def _make_substituter(mapping: dict[str, str]):
    def substitute(value: str) -> str:
        for key, replacement in mapping.items():
            value = value.replace(f"${{{key}}}", replacement)
        # 清掉未知占位符，避免把字面量传给 Java。
        return _UNRESOLVED.sub("", value)

    return substitute