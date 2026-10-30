"""自研 Minecraft 引擎层。

该包不依赖 Web/UI 框架，可独立测试与复用：
    VersionResolver -> Downloader -> LaunchBuilder
"""

from .models import Account, FileEntry, GamePaths, LibraryInfo, ResolvedVersion
from .accounts import (
    AccountError,
    AccountStore,
    ExternalAuthClient,
    SkinService,
    StoredAccount,
    normalize_uuid,
)
from .auth_client import (
    AuthClient,
    AuthError,
    AuthResult,
    DeviceCode,
    KeyringTokenStore,
    MemoryTokenStore,
)
from .content_manager import (
    CONTENT_DATAPACK,
    CONTENT_KINDS,
    CONTENT_MOD,
    CONTENT_MODPACK,
    CONTENT_RESOURCEPACK,
    CONTENT_SHADER,
    ContentError,
    ContentManager,
    LocalContent,
    SaveEntry,
    ScreenshotEntry,
)
from .crash_analyzer import Cause, CrashAnalyzer, CrashReport
from .java_manager import JavaError, JavaInfo, JavaManager, JavaRuntime
from .loader_installer import (
    SUPPORTED_LOADERS,
    LoaderError,
    LoaderInstallResult,
    LoaderInstaller,
    LoaderVersion,
)
from .mirror import SOURCE_AUTO, SOURCE_BMCLAPI, SOURCE_OFFICIAL, MirrorManager, MirrorStatus
from .modpack import ModpackError, ModpackInfo, ModpackInstaller
from .downloader import DownloadStats, Downloader
from .launch_builder import LaunchBuilder, offline_uuid
from .mod_provider import (
    CurseForgeProvider,
    ModFile,
    ModProvider,
    ModProviderError,
    ModSearchResult,
    ModrinthProvider,
    build_provider,
)
from .version_resolver import VersionResolver, VersionInfo

__all__ = [
    "Account",
    "AccountError",
    "AccountStore",
    "AuthClient",
    "AuthError",
    "AuthResult",
    "Cause",
    "CONTENT_DATAPACK",
    "CONTENT_KINDS",
    "CONTENT_MOD",
    "CONTENT_MODPACK",
    "CONTENT_RESOURCEPACK",
    "CONTENT_SHADER",
    "ContentError",
    "ContentManager",
    "CrashAnalyzer",
    "CrashReport",
    "CurseForgeProvider",
    "DeviceCode",
    "ExternalAuthClient",
    "FileEntry",
    "GamePaths",
    "JavaError",
    "JavaInfo",
    "JavaManager",
    "JavaRuntime",
    "KeyringTokenStore",
    "LibraryInfo",
    "LaunchBuilder",
    "LoaderError",
    "LoaderInstallResult",
    "LoaderInstaller",
    "LoaderVersion",
    "LocalContent",
    "MemoryTokenStore",
    "MirrorManager",
    "MirrorStatus",
    "ModFile",
    "ModpackError",
    "ModpackInfo",
    "ModpackInstaller",
    "ModProvider",
    "ModProviderError",
    "ModSearchResult",
    "ModrinthProvider",
    "ResolvedVersion",
    "SOURCE_AUTO",
    "SOURCE_BMCLAPI",
    "SOURCE_OFFICIAL",
    "SUPPORTED_LOADERS",
    "SaveEntry",
    "ScreenshotEntry",
    "SkinService",
    "StoredAccount",
    "DownloadStats",
    "Downloader",
    "VersionInfo",
    "VersionResolver",
    "build_provider",
    "normalize_uuid",
    "offline_uuid",
]