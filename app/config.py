"""配置持久化。

所有数据（游戏文件、配置、账户）统一放在启动器运行目录下的 ``data``。
源码运行时运行目录是项目根目录，打包后是 exe 所在目录。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from engine.auth_client import DEFAULT_CLIENT_ID
from engine.models import GamePaths

CONFIG_FILENAME = "launcher_config.json"


def runtime_dir() -> Path:
    """启动器运行目录：打包后为 exe 所在目录，否则为项目根目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


# 启动器数据放在运行目录/data（配置、账户等）；游戏文件统一放在 data/.minecraft。
DATA_DIR = runtime_dir() / "data"
DEFAULT_ROOT = DATA_DIR / ".minecraft"

# 版本级设置的可覆盖项；None 表示"跟随全局"。
VERSION_SETTING_KEYS = (
    "isolated",
    "java",
    "memory_mb",
    "min_memory_mb",
    "width",
    "height",
    "extra_jvm",
    "extra_game",
)


@dataclass
class LauncherConfig:
    root: str = str(DEFAULT_ROOT)
    java: str = "java"
    memory_mb: int = 2048
    min_memory_mb: int = 512
    width: int = 854
    height: int = 480
    client_id: str = DEFAULT_CLIENT_ID
    last_version: str = ""
    include_snapshots: bool = False
    download_workers: int = 32
    mod_source: str = "modrinth"
    mod_loader: str = "fabric"
    # 下载源：auto（镜像优先，失败回退官方）/ bmclapi / official
    download_source: str = "auto"
    # 全局默认是否开启版本隔离（每个版本可单独覆盖）
    isolated: bool = False
    # 哈希文件库（实验性）：游戏文件按 SHA1 去重存放，启动时物化到临时目录。
    hash_store: bool = False
    last_account: str = ""
    # 启动页登录方式：offline / microsoft，空表示按上次登录账号决定。
    login_mode: str = ""
    # 版本级覆盖：{version_id: {key: value}}，只存被显式修改过的项。
    version_settings: dict = field(default_factory=dict)

    def to_game_paths(self) -> GamePaths:
        return GamePaths(Path(self.root), java=self.java)

    def as_dict(self) -> dict:
        return asdict(self)

    def override_for(self, version_id: str) -> dict:
        """取某个版本的覆盖项（自动去掉空值）。"""
        raw = self.version_settings.get(version_id) or {}
        return {key: value for key, value in raw.items() if key in VERSION_SETTING_KEYS}

    def merged_settings(self, version_id: str) -> dict:
        """全局默认 + 版本覆盖后的最终启动参数。"""
        override = self.override_for(version_id)
        merged = {
            "isolated": self.isolated,
            "java": self.java,
            "memory_mb": self.memory_mb,
            "min_memory_mb": self.min_memory_mb,
            "width": self.width,
            "height": self.height,
            "extra_jvm": [],
            "extra_game": [],
        }
        for key, value in override.items():
            if value is None or value == "" or value == []:
                continue
            merged[key] = value
        return merged

    def set_version_override(self, version_id: str, changes: dict) -> dict:
        override = self.override_for(version_id)
        for key in VERSION_SETTING_KEYS:
            if key not in changes:
                continue
            value = changes[key]
            # None / 空串 / 空列表 表示清除覆盖、回落到全局。
            if value is None or value == "" or value == []:
                override.pop(key, None)
            else:
                override[key] = value
        if override:
            self.version_settings[version_id] = override
        else:
            self.version_settings.pop(version_id, None)
        return self.merged_settings(version_id)


@dataclass
class ConfigStore:
    """读写配置。

    配置固定放在启动器数据目录（运行目录/data），游戏目录由配置项 ``root`` 决定，
    因此修改游戏目录不会丢失启动器设置。
    """

    data_dir: Path = field(default_factory=lambda: DATA_DIR)

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)

    @property
    def config_path(self) -> Path:
        return self.data_dir / CONFIG_FILENAME

    def load(self) -> LauncherConfig:
        if not self.config_path.exists():
            config = LauncherConfig(root=str(DEFAULT_ROOT))
            self.save(config)
            return config
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return LauncherConfig(root=str(DEFAULT_ROOT))

        known = {f.name for f in fields(LauncherConfig)}
        data = {k: v for k, v in raw.items() if k in known}
        original_root = str(raw.get("root") or "")
        data["root"] = _normalize_root(raw.get("root"), self.data_dir)
        # 下载线程数是用户显式可配置的项，不能再把 16 强行改回 32，
        # 否则用户在设置中调成 16 后每次启动都会被覆盖。
        config = LauncherConfig(**data)
        if original_root != data["root"]:
            # 迁移后的游戏目录立即写回，避免下次启动重复判断。
            self.save(config)
        return config

    def save(self, config: LauncherConfig) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再原子替换，避免关闭 / 断电时留下半截配置文件。
        tmp = self.config_path.with_name(self.config_path.name + ".tmp")
        tmp.write_text(
            json.dumps(config.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(tmp, self.config_path)


def _normalize_root(raw_root: object, data_dir: Path) -> str:
    """解析游戏目录；旧版把 data 目录本身当游戏目录，这里迁移到 data/.minecraft。"""
    root = str(raw_root or DEFAULT_ROOT)
    try:
        if Path(root).expanduser().resolve() == data_dir.resolve():
            return str(DEFAULT_ROOT)
    except OSError:
        pass
    return root