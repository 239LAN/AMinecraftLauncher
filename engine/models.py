"""引擎层共享数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# 文件类别：client(客户端 jar) / library(普通库) / asset(游戏资源)
# / asset_index(资源索引) / native(需解压的原生库) / log_config(日志配置)
FileKind = str


@dataclass
class FileEntry:
    """扁平化后的单个待下载文件。"""

    url: str
    target_path: Path
    sha1: Optional[str] = None
    size: int = 0
    kind: FileKind = "library"
    # 需要鉴权 / 自定义 UA 的 CDN 可在此注入请求头。
    headers: Optional[dict[str, str]] = None

    def __post_init__(self) -> None:
        self.target_path = Path(self.target_path)


@dataclass
class GamePaths:
    """游戏根目录布局，对应设计文档第 6 节。"""

    root: Path
    java: str = "java"

    def __post_init__(self) -> None:
        self.root = Path(self.root)

    @property
    def versions_dir(self) -> Path:
        return self.root / "versions"

    @property
    def libraries_dir(self) -> Path:
        return self.root / "libraries"

    @property
    def assets_dir(self) -> Path:
        return self.root / "assets"

    @property
    def mods_dir(self) -> Path:
        return self.root / "mods"

    @property
    def resourcepacks_dir(self) -> Path:
        return self.root / "resourcepacks"

    @property
    def shaderpacks_dir(self) -> Path:
        return self.root / "shaderpacks"

    @property
    def saves_dir(self) -> Path:
        return self.root / "saves"

    @property
    def screenshots_dir(self) -> Path:
        return self.root / "screenshots"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def crash_reports_dir(self) -> Path:
        return self.root / "crash-reports"

    @property
    def datapacks_dir(self) -> Path:
        return self.root / "datapacks"

    @property
    def config_path(self) -> Path:
        return self.root / "launcher_config.json"

    def version_dir(self, version_id: str) -> Path:
        return self.versions_dir / version_id

    def version_json(self, version_id: str) -> Path:
        return self.version_dir(version_id) / f"{version_id}.json"

    def version_jar(self, version_id: str) -> Path:
        return self.version_dir(version_id) / f"{version_id}.jar"

    def natives_dir(self, version_id: str) -> Path:
        return self.version_dir(version_id) / "natives"

    # ---------------------------------------------------------------- 版本隔离
    def version_game_dir(self, version_id: str, *, isolated: bool = False) -> Path:
        """开启版本隔离时，以版本目录作为游戏目录（等价于 .minecraft）。"""
        return self.version_dir(version_id) if isolated else self.root

    def mods_dir_for(self, version_id: str, *, isolated: bool = False) -> Path:
        return self.version_game_dir(version_id, isolated=isolated) / "mods"

    def resourcepacks_dir_for(self, version_id: str, *, isolated: bool = False) -> Path:
        return self.version_game_dir(version_id, isolated=isolated) / "resourcepacks"

    def shaderpacks_dir_for(self, version_id: str, *, isolated: bool = False) -> Path:
        return self.version_game_dir(version_id, isolated=isolated) / "shaderpacks"

    def saves_dir_for(self, version_id: str, *, isolated: bool = False) -> Path:
        return self.version_game_dir(version_id, isolated=isolated) / "saves"

    def ensure(self) -> None:
        for directory in (
            self.versions_dir,
            self.libraries_dir,
            self.assets_dir,
            self.mods_dir,
            self.resourcepacks_dir,
            self.shaderpacks_dir,
            self.saves_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


@dataclass
class LibraryInfo:
    """过滤规则后保留的单个库（classpath 用 artifact，原生库用 native_jar）。"""

    name: str
    on_classpath: bool = False
    artifact_path: Optional[Path] = None
    native_jar: Optional[FileEntry] = None
    exclude: tuple[str, ...] = ("META-INF/",)


@dataclass
class Account:
    """账号信息。离线模式 token 填非空占位值即可。"""

    username: str
    uuid: str
    access_token: str = "0"
    user_type: str = "legacy"
    offline: bool = True

    @classmethod
    def offline_account(cls, username: str) -> "Account":
        from .launch_builder import offline_uuid

        return cls(
            username=username,
            uuid=offline_uuid(username),
            access_token="0",
            user_type="legacy",
            offline=True,
        )


@dataclass
class ResolvedVersion:
    """版本解析结果：既包含待下载清单，也包含启动所需元数据。"""

    id: str
    version_type: str
    main_class: str
    java_major: int

    jar_path: Path
    natives_dir: Path
    assets_root: Path
    asset_index_id: str
    asset_index_path: Path

    libraries: list[LibraryInfo]
    classpath: list[Path]
    # 启动参数保留原始结构（str 或带 rules 的 dict），
    # 因为 has_custom_resolution 等特性取决于启动时的选择，需由 LaunchBuilder 求值。
    jvm_args: list[Any]
    game_args: list[Any]
    os_name: str
    os_arch: str
    log_config: Optional[dict]

    files: list[FileEntry]
    raw: dict = field(default_factory=dict)