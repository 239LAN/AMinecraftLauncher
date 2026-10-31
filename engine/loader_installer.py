"""LoaderInstaller：安装 Mod 加载器（Fabric / Quilt / Forge / NeoForge）。

本模块只做两件事：
- 列出某个加载器在指定原版版本下可用的加载器版本（``list_versions``）；
- 把加载器版本写进 ``versions/``（``install``）。

实现方式严格对齐各加载器官方约定，不臆造接口：
- fabric / quilt 走官方 meta 的 profile JSON，写出带 ``inheritsFrom`` 的
  版本 JSON，并把父版本客户端 jar 硬链接到加载器版本目录，避免后续解析时
  重复下载约 25MB 的客户端 jar；
- forge / neoforge 下载官方 installer 并用 Java 运行，由安装器自行在
  ``versions/`` 下生成版本目录，之后扫描出该目录。

``inheritsFrom`` 的合并与文件下载分别交给 ``engine.version_resolver`` 与
``engine.downloader``，本模块不解析、不启动游戏。HTTP 走 ``engine.net``，
因此会自动应用 ``engine.mirror`` 的镜像候选
（forge / neoforge / fabric / quilt 的地址都已在镜像规则中覆盖）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional

from . import net
from .models import GamePaths

FABRIC_META = "https://meta.fabricmc.net"
QUILT_META = "https://meta.quiltmc.org"
FORGE_PROMOTIONS_URL = (
    "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
)
FORGE_MAVEN = "https://maven.minecraftforge.net/net/minecraftforge/forge"
NEOFORGE_METADATA_URL = (
    "https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml"
)
NEOFORGE_MAVEN = "https://maven.neoforged.net/releases/net/neoforged/neoforge"

# 安装器（forge / neoforge）运行超时，单位秒。
INSTALLER_TIMEOUT = 600

SUPPORTED_LOADERS = ("fabric", "quilt", "forge", "neoforge")

# 进度回调：stage 阶段名，done / total 为进度分子分母（total 为 0 表示未知）。
ProgressFn = Callable[[str, int, int], None]
# 日志回调：每次一条日志行。
LogFn = Callable[[str], None]
# 取消回调：返回 True 表示需要取消。
CancelFn = Callable[[], bool]


class LoaderError(Exception):
    """加载器列表拉取 / 安装过程中的可预期错误。"""


@dataclass
class LoaderVersion:
    """某个加载器在指定原版版本下的一个可用版本。"""

    loader: str          # "fabric" / "quilt" / "forge" / "neoforge"
    version: str         # 加载器版本号
    game_version: str
    stable: bool = True


@dataclass
class LoaderInstallResult:
    """一次加载器安装的结果。"""

    version_id: str      # 写入 versions/<version_id>/ 的目录名
    loader: str
    loader_version: str
    game_version: str
    profile_path: Path   # 写出的版本 JSON 路径


# --------------------------------------------------------------------- 纯函数


def _neoforge_prefix(game_version: str) -> str:
    """推导 NeoForge 版本号前缀：MC 1.20.4 -> ``20.4``，1.21.1 -> ``21.1``。

    取 ``game_version`` 去掉开头 ``1.`` 的部分；无法确定时返回空串。
    """
    parts = (game_version or "").split(".")
    if len(parts) >= 3 and parts[0] == "1":
        return f"{parts[1]}.{parts[2]}"
    # 兜底：直接给成 20.4 这类前缀（非 1.x 的两段版本）。
    if len(parts) >= 2 and parts[0].isdigit() and parts[0] != "1":
        return f"{parts[0]}.{parts[1]}"
    return ""


def neoforge_version_matches(game_version: str, neoforge_version: str) -> bool:
    """判断某 NeoForge 版本号是否属于给定 MC 版本。

    NeoForge 1.20.2+ 的版本号形如 ``20.4.237``（对应 MC 1.20.4）或
    ``21.1.66``（对应 MC 1.21.1）。匹配规则是「版本号以 ``<minor>.<patch>``
    开头」：MC 1.20.4 -> ``20.4``、MC 1.21.1 -> ``21.1``。
    无法确定前缀（例如只给了 1.20）时一律返回 False。
    """
    prefix = _neoforge_prefix(game_version)
    if not prefix:
        return False
    return neoforge_version == prefix or neoforge_version.startswith(prefix + ".")


def parse_maven_metadata(raw: "str | bytes") -> list[str]:
    """解析 maven-metadata.xml，返回 ``<versioning>/<versions>`` 下的版本号列表。

    解析失败时返回空列表，不抛异常。
    """
    text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    container = root.find("versioning/versions")
    if container is None:
        return []
    versions: list[str] = []
    for element in container.findall("version"):
        value = (element.text or "").strip()
        if value:
            versions.append(value)
    return versions


def parse_forge_promotions(promotions: dict, game_version: str) -> list[str]:
    """从 promotions_slim.json 中取出属于 game_version 的版本号（去重）。

    promo 的 key 形如 ``1.20.1-latest`` / ``1.20.1-recommended``，
    按最后一个 ``-`` 切分后匹配 game_version；同一版本号只保留一次。
    """
    promos = (promotions or {}).get("promos") or {}
    result: list[str] = []
    seen: set[str] = set()
    for key, version in promos.items():
        if not version:
            continue
        if str(key).rsplit("-", 1)[0] != game_version:
            continue
        text = str(version)
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def conventional_version_id(loader: str, game_version: str, loader_version: str) -> str:
    """按官方约定拼出默认版本目录名（供无网络时的 ``installed`` 判定使用）。

    fabric / quilt 的 profile ``id`` 实际就是该形式，因此这里能命中。
    """
    if loader in ("fabric", "quilt"):
        return f"{loader}-loader-{loader_version}-{game_version}"
    if loader == "forge":
        return f"{game_version}-forge-{loader_version}"
    if loader == "neoforge":
        return f"neoforge-{loader_version}"
    return ""


# ------------------------------------------------------------------ 安装器


class LoaderInstaller:
    """安装 Mod 加载器并维护 ``versions/`` 下的版本目录。"""

    def __init__(
        self,
        paths: GamePaths,
        *,
        java: str = "java",
        timeout: int = 30,
    ) -> None:
        self.paths = paths
        self.java = java
        self.timeout = timeout

    # ------------------------------------------------------------ 版本列表
    def list_versions(self, loader: str, game_version: str) -> list[LoaderVersion]:
        """列出加载器在指定原版版本下可用的版本。"""
        self._require_supported(loader)
        if loader == "fabric":
            return self._list_fabric(game_version)
        if loader == "quilt":
            return self._list_quilt(game_version)
        if loader == "forge":
            return self._list_forge(game_version)
        return self._list_neoforge(game_version)

    def _list_fabric(self, game_version: str) -> list[LoaderVersion]:
        url = f"{FABRIC_META}/v2/versions/loader/{game_version}"
        data = net.get_json(url, timeout=self.timeout)
        return self._parse_loader_array("fabric", game_version, data)

    def _list_quilt(self, game_version: str) -> list[LoaderVersion]:
        url = f"{QUILT_META}/v3/versions/loader/{game_version}"
        data = net.get_json(url, timeout=self.timeout)
        return self._parse_loader_array("quilt", game_version, data)

    def _parse_loader_array(
        self, loader: str, game_version: str, data: object
    ) -> list[LoaderVersion]:
        """解析 fabric / quilt 的 ``[{"loader": {...}}, ...]`` 结构。"""
        result: list[LoaderVersion] = []
        for item in data or []:
            meta = (item or {}).get("loader") or {}
            version = meta.get("version")
            if not version:
                continue
            result.append(
                LoaderVersion(
                    loader=loader,
                    version=str(version),
                    game_version=game_version,
                    stable=bool(meta.get("stable", True)),
                )
            )
        return result

    def _list_forge(self, game_version: str) -> list[LoaderVersion]:
        data = net.get_json(FORGE_PROMOTIONS_URL, timeout=self.timeout)
        versions = parse_forge_promotions(data if isinstance(data, dict) else {}, game_version)
        return [
            LoaderVersion("forge", version, game_version, True) for version in versions
        ]

    def _list_neoforge(self, game_version: str) -> list[LoaderVersion]:
        raw = net.fetch_bytes(NEOFORGE_METADATA_URL, timeout=self.timeout)
        versions = parse_maven_metadata(raw)
        return [
            LoaderVersion("neoforge", version, game_version, True)
            for version in versions
            if neoforge_version_matches(game_version, version)
        ]

    # -------------------------------------------------------------- 安装
    def install(
        self,
        loader: str,
        game_version: str,
        loader_version: str,
        *,
        progress: Optional[ProgressFn] = None,
        log: Optional[LogFn] = None,
        cancel: Optional[CancelFn] = None,
    ) -> LoaderInstallResult:
        """安装加载器指定版本，返回安装结果。

        幂等：已安装时直接返回，不重复联网 / 重复运行安装器。
        """
        self._require_supported(loader)
        self._check_cancel(cancel)

        existing = self.installed(loader, game_version, loader_version)
        if existing is not None:
            if log:
                log(f"已安装，跳过：{existing.version_id}")
            return existing

        if loader in ("fabric", "quilt"):
            return self._install_profile(
                loader, game_version, loader_version, progress, log, cancel
            )
        return self._install_with_java(
            loader, game_version, loader_version, progress, log, cancel
        )

    def installed(
        self, loader: str, game_version: str, loader_version: str
    ) -> Optional[LoaderInstallResult]:
        """判断加载器是否已安装：目录存在且版本 JSON 存在即算已安装。"""
        self._require_supported(loader)
        for version_id in self._candidate_version_ids(loader, game_version, loader_version):
            profile_path = self.paths.version_json(version_id)
            if self.paths.version_dir(version_id).is_dir() and profile_path.exists():
                return LoaderInstallResult(
                    version_id=version_id,
                    loader=loader,
                    loader_version=loader_version,
                    game_version=game_version,
                    profile_path=profile_path,
                )
        return None

    # ---------------------------------------------------- fabric / quilt
    def _install_profile(
        self,
        loader: str,
        game_version: str,
        loader_version: str,
        progress: Optional[ProgressFn],
        log: Optional[LogFn],
        cancel: Optional[CancelFn],
    ) -> LoaderInstallResult:
        base = FABRIC_META if loader == "fabric" else QUILT_META
        meta_version = "v2" if loader == "fabric" else "v3"
        url = (
            f"{base}/{meta_version}/versions/loader/"
            f"{game_version}/{loader_version}/profile/json"
        )

        if progress:
            progress("profile", 0, 1)
        if log:
            log(f"获取 {loader} profile：{url}")
        self._check_cancel(cancel)
        profile = net.get_json(url, timeout=self.timeout)
        self._check_cancel(cancel)
        if not isinstance(profile, dict) or not profile:
            raise LoaderError(f"{loader} profile 拉取失败：{url}")

        # 版本目录名以 profile 里的 id 为准，缺失才回退拼接。
        version_id = str(profile.get("id") or "").strip() or conventional_version_id(
            loader, game_version, loader_version
        )

        version_dir = self.paths.version_dir(version_id)
        version_dir.mkdir(parents=True, exist_ok=True)
        profile_path = self.paths.version_json(version_id)
        tmp = profile_path.with_name(profile_path.name + ".tmp")
        tmp.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, profile_path)
        if progress:
            progress("profile", 1, 1)
        if log:
            log(f"已写出版本 JSON：{profile_path}")

        self._link_parent_jar(game_version, version_id, log)
        return LoaderInstallResult(
            version_id=version_id,
            loader=loader,
            loader_version=loader_version,
            game_version=game_version,
            profile_path=profile_path,
        )

    def _link_parent_jar(
        self, game_version: str, version_id: str, log: Optional[LogFn]
    ) -> None:
        """把父版本客户端 jar 硬链接到加载器版本目录（失败则复制）。

        这样后续解析加载器版本时，客户端 jar 的 SHA1 校验会直接命中本地文件，
        避免重复下载约 25MB；父 jar 不存在时交给解析阶段自行下载。
        """
        parent_jar = self.paths.version_jar(game_version)
        target_jar = self.paths.version_jar(version_id)
        if target_jar.exists() or not parent_jar.exists():
            return
        try:
            os.link(parent_jar, target_jar)
            if log:
                log(f"已硬链接原版 jar：{target_jar}")
            return
        except OSError:
            pass
        try:
            shutil.copy2(parent_jar, target_jar)
            if log:
                log(f"硬链接失败，已复制原版 jar：{target_jar}")
        except OSError as exc:  # 复制也失败就放弃，交由解析阶段下载
            if log:
                log(f"准备原版 jar 失败（将由解析阶段下载）：{exc}")

    # ---------------------------------------------------- forge / neoforge
    def _install_with_java(
        self,
        loader: str,
        game_version: str,
        loader_version: str,
        progress: Optional[ProgressFn],
        log: Optional[LogFn],
        cancel: Optional[CancelFn],
    ) -> LoaderInstallResult:
        url, filename = self._installer_url(loader, game_version, loader_version)
        installers_dir = self.paths.root / "installers"
        installers_dir.mkdir(parents=True, exist_ok=True)
        installer_path = installers_dir / filename

        if installer_path.exists():
            if log:
                log(f"安装器已存在，跳过下载：{installer_path}")
        else:
            if log:
                log(f"下载安装器：{url}")
            self._download_installer(url, installer_path, progress, cancel)

        self._check_cancel(cancel)
        if progress:
            progress("install", 0, 1)
        if log:
            log(f"运行安装器：{installer_path}")
        self._run_installer(installer_path, log)
        if progress:
            progress("install", 1, 1)

        version_id = self._find_installed_version(loader, game_version, loader_version)
        if version_id is None:
            raise LoaderError(
                f"{loader} 安装器未生成版本目录（{game_version} / {loader_version}）"
            )
        if log:
            log(f"安装完成：{version_id}")
        return LoaderInstallResult(
            version_id=version_id,
            loader=loader,
            loader_version=loader_version,
            game_version=game_version,
            profile_path=self.paths.version_json(version_id),
        )

    def _installer_url(
        self, loader: str, game_version: str, loader_version: str
    ) -> tuple[str, str]:
        """返回 (安装器 URL, 本地文件名)。"""
        if loader == "forge":
            stem = f"{game_version}-{loader_version}"
            return (
                f"{FORGE_MAVEN}/{stem}/forge-{stem}-installer.jar",
                f"forge-{stem}-installer.jar",
            )
        stem = loader_version
        return (
            f"{NEOFORGE_MAVEN}/{stem}/neoforge-{stem}-installer.jar",
            f"neoforge-{stem}-installer.jar",
        )

    def _download_installer(
        self,
        url: str,
        dest: Path,
        progress: Optional[ProgressFn],
        cancel: Optional[CancelFn],
    ) -> None:
        done = 0

        def _on_chunk(delta: int) -> None:
            nonlocal done
            self._check_cancel(cancel)
            done += delta
            if progress:
                progress("download", done, 0)

        net.download_file(
            url, dest, on_chunk=_on_chunk, timeout=self.timeout, cancel=cancel
        )

    def _run_installer(self, installer_path: Path, log: Optional[LogFn]) -> None:
        """用 Java 运行安装器，stdout 逐行喂给 log。"""
        kwargs: dict = {}
        if os.name == "nt":
            # 安装器在后台运行，不弹出黑色控制台窗口。
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            proc = subprocess.run(
                [
                    self.java,
                    "-jar",
                    str(installer_path),
                    "--installClient",
                    str(self.paths.root),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=INSTALLER_TIMEOUT,
                **kwargs,
            )
        except FileNotFoundError as exc:
            raise LoaderError(f"找不到 Java：{self.java}") from exc
        except subprocess.TimeoutExpired as exc:
            raise LoaderError("安装器执行超时") from exc
        except OSError as exc:
            raise LoaderError(f"启动安装器失败：{exc}") from exc

        if log:
            for line in (proc.stdout or "").splitlines():
                log(line)
            for line in (proc.stderr or "").splitlines():
                log(line)
            if proc.returncode != 0:
                log(f"安装器退出码：{proc.returncode}")

    def _find_installed_version(
        self, loader: str, game_version: str, loader_version: str
    ) -> Optional[str]:
        """安装后从 ``versions_dir`` 中找出安装器生成的版本目录名。

        优先按精确名查找；找不到时扫描目录，取「名字里同时含 game_version 与
        forge」者（neoforge 则为以 ``neoforge`` 开头者）中修改时间最新者。
        """
        versions_dir = self.paths.versions_dir
        exact = conventional_version_id(loader, game_version, loader_version)
        if exact and (versions_dir / exact).is_dir():
            return exact
        if not versions_dir.is_dir():
            return None

        best_name: Optional[str] = None
        best_mtime = -1.0
        for entry in versions_dir.iterdir():
            if not entry.is_dir() or not self._dir_matches(loader, entry.name, game_version, loader_version):
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                mtime = 0.0
            if mtime > best_mtime:
                best_mtime = mtime
                best_name = entry.name
        return best_name

    # -------------------------------------------------------------- 内部工具
    @staticmethod
    def _dir_matches(
        loader: str, name: str, game_version: str, loader_version: str
    ) -> bool:
        """判断某个目录名是否为该加载器版本生成的目录。"""
        if loader == "forge":
            if "forge" not in name or "neoforge" in name:
                return False
            return game_version in name
        if loader == "neoforge":
            if not name.startswith("neoforge"):
                return False
            return not loader_version or loader_version in name
        return False

    def _candidate_version_ids(
        self, loader: str, game_version: str, loader_version: str
    ) -> Iterator[str]:
        """产出可能的版本目录名：先约定名，再扫描目录兜底。"""
        conventional = conventional_version_id(loader, game_version, loader_version)
        if conventional:
            yield conventional

        versions_dir = self.paths.versions_dir
        if not versions_dir.is_dir():
            return
        for entry in sorted(versions_dir.iterdir()):
            if not entry.is_dir() or entry.name == conventional:
                continue
            name = entry.name
            if loader in ("fabric", "quilt"):
                if not name.startswith(f"{loader}-loader-"):
                    continue
                if loader_version not in name or game_version not in name:
                    continue
            elif loader == "forge":
                if "forge" not in name or "neoforge" in name:
                    continue
                if loader_version not in name or game_version not in name:
                    continue
            else:  # neoforge
                if not name.startswith("neoforge") or loader_version not in name:
                    continue
            yield name

    @staticmethod
    def _require_supported(loader: str) -> None:
        if loader not in SUPPORTED_LOADERS:
            raise LoaderError(f"不支持的加载器：{loader}")

    @staticmethod
    def _check_cancel(cancel: Optional[CancelFn]) -> None:
        if cancel and cancel():
            raise LoaderError("已取消")