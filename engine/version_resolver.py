"""VersionResolver：读取 Mojang 清单并把版本 JSON 解析成扁平文件清单。

只做"解析"这一件事：不下载、不启动、不依赖 Web 层。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from . import net
from .models import FileEntry, GamePaths, LibraryInfo, ResolvedVersion
from .rules import detect_os_arch, detect_os_name, rules_allow

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
RESOURCES_URL = "https://resources.download.minecraft.net/"
LIBRARIES_MIRROR = "https://libraries.minecraft.net/"

ProgressFn = Callable[[str, int, int], None]


@dataclass
class VersionInfo:
    id: str
    type: str
    url: str
    release_time: str = ""
    sha1: Optional[str] = None
    size: int = 0


class VersionResolver:
    def __init__(
        self,
        paths: GamePaths,
        *,
        os_name: Optional[str] = None,
        os_arch: Optional[str] = None,
        features: Optional[dict[str, bool]] = None,
    ) -> None:
        self.paths = paths
        self.os_name = os_name or detect_os_name()
        self.os_arch = os_arch or detect_os_arch()
        self.features = features or {}
        self._manifest: Optional[dict[str, Any]] = None

    # ---------------------------------------------------------------- manifest
    def load_manifest(self, *, force: bool = False) -> dict[str, Any]:
        """加载 version_manifest_v2.json，带本地缓存。"""
        if self._manifest is not None and not force:
            return self._manifest

        cache = self.paths.versions_dir / "version_manifest_v2.json"
        if cache.exists() and not force:
            try:
                self._manifest = json.loads(cache.read_text(encoding="utf-8"))
                return self._manifest
            except (OSError, json.JSONDecodeError):
                pass

        data = net.get_json(MANIFEST_URL)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data), encoding="utf-8")
        self._manifest = data
        return data

    def list_versions(self, *, include_snapshots: bool = True) -> list[VersionInfo]:
        manifest = self.load_manifest()
        result: list[VersionInfo] = []
        for item in manifest.get("versions", []):
            if not include_snapshots and item.get("type") != "release":
                continue
            result.append(
                VersionInfo(
                    id=item.get("id", ""),
                    type=item.get("type", ""),
                    url=item.get("url", ""),
                    release_time=item.get("releaseTime", ""),
                    sha1=item.get("sha1"),
                    size=item.get("size", 0),
                )
            )
        return result

    def find_version(self, version_id: str) -> VersionInfo:
        for info in self.list_versions():
            if info.id == version_id:
                return info
        raise KeyError(f"版本不存在: {version_id}")

    # ---------------------------------------------------------------- resolve
    def resolve(
        self,
        version_id: str,
        *,
        include_assets: bool = True,
        progress: Optional[ProgressFn] = None,
        prefer_local: bool = False,
        cancel: Optional[Callable[[], bool]] = None,
    ) -> ResolvedVersion:
        """把某个版本解析成下载清单 + 启动元数据。

        ``prefer_local=True`` 时优先读取已安装的版本 JSON，避免启动时再联网。
        ``cancel`` 返回 True 时尽早中断解析（异常类型与下载取消一致）。
        """
        _check_cancel(cancel)
        version_json: Optional[dict[str, Any]] = None
        if prefer_local:
            local = self.paths.version_json(version_id)
            if local.exists():
                try:
                    version_json = json.loads(local.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    version_json = None
        if version_json is None:
            info = self.find_version(version_id)
            version_json = self._load_version_json(info)

        _check_cancel(cancel)
        if version_json.get("inheritsFrom"):
            version_json = self._resolve_inherited(version_id, version_json)

        return self._parse(version_id, version_json, include_assets, progress, cancel)

    # -------------------------------------------------------------- inheritsFrom
    def _load_json_for(self, version_id: str) -> dict[str, Any]:
        """优先读本地已安装 JSON，缺失时回退官方清单。"""
        local = self.paths.version_json(version_id)
        if local.exists():
            try:
                return json.loads(local.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        info = self.find_version(version_id)
        return self._load_version_json(info)

    def _resolve_inherited(
        self, version_id: str, version_json: dict[str, Any]
    ) -> dict[str, Any]:
        """把 inheritsFrom 链（加载器 -> 原版）逐层合并成一份完整 JSON。

        与官方启动器一致：子版本覆盖父版本的标量字段，libraries 与
        arguments 采用「父在前、子在后」且子版本覆盖同名库。
        """
        chain: list[dict[str, Any]] = []
        current = version_json
        seen: set[str] = set()
        while current.get("inheritsFrom"):
            parent_id = str(current["inheritsFrom"])
            if parent_id in seen:
                raise ValueError(f"版本继承出现环：{version_id} -> {parent_id}")
            seen.add(parent_id)
            chain.append(current)
            current = self._load_json_for(parent_id)

        merged = current
        for child in reversed(chain):
            merged = _merge(merged, child)
        merged["id"] = version_id
        return merged

    def _load_version_json(self, info: VersionInfo) -> dict[str, Any]:
        target = self.paths.version_json(info.id)
        raw = net.fetch_bytes(info.url)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        return json.loads(raw.decode("utf-8"))

    # ------------------------------------------------------------------ parse
    def _parse(
        self,
        version_id: str,
        version_json: dict[str, Any],
        include_assets: bool,
        progress: Optional[ProgressFn],
        cancel: Optional[Callable[[], bool]] = None,
    ) -> ResolvedVersion:
        _check_cancel(cancel)
        vid = version_json.get("id") or version_id
        jar_path = self.paths.version_jar(vid)
        natives_dir = self.paths.natives_dir(vid)
        assets_root = self.paths.assets_dir

        java_major = int(version_json.get("javaVersion", {}).get("majorVersion") or 8)
        files: list[FileEntry] = []

        # 1) 客户端 jar
        client = version_json.get("downloads", {}).get("client") or {}
        if client.get("url"):
            files.append(
                FileEntry(
                    url=client["url"],
                    target_path=jar_path,
                    sha1=client.get("sha1"),
                    size=int(client.get("size") or 0),
                    kind="client",
                )
            )

        # 2) libraries（含原生库）
        libraries: list[LibraryInfo] = []
        for raw_lib in version_json.get("libraries", []):
            if not rules_allow(
                raw_lib.get("rules"), self.os_name, self.os_arch, self.features
            ):
                continue
            lib_files, lib_info = self._process_library(raw_lib)
            files.extend(lib_files)
            if lib_info is not None:
                libraries.append(lib_info)

        classpath: list[Path] = [
            lib.artifact_path for lib in libraries if lib.on_classpath and lib.artifact_path
        ]
        if client.get("url"):
            classpath.append(jar_path)

        # 3) 资源索引 + 资源对象
        asset_index = version_json.get("assetIndex")
        asset_index_id = ""
        asset_index_path = Path()
        if asset_index:
            asset_index_id = asset_index.get("id") or version_json.get("assets", "legacy")
            asset_index_path = assets_root / "indexes" / f"{asset_index_id}.json"
            files.append(
                FileEntry(
                    url=asset_index["url"],
                    target_path=asset_index_path,
                    sha1=asset_index.get("sha1"),
                    size=int(asset_index.get("size") or 0),
                    kind="asset_index",
                )
            )
            if include_assets:
                files.extend(
                    self._collect_assets(
                        asset_index["url"], asset_index_path, assets_root, progress, cancel
                    )
                )

        # 4) 日志配置
        log_config = version_json.get("logging", {}).get("client")
        if log_config:
            log_file = log_config.get("file") or {}
            if log_file.get("url"):
                files.append(
                    FileEntry(
                        url=log_file["url"],
                        target_path=assets_root / "log_configs" / str(log_file.get("id")),
                        sha1=log_file.get("sha1"),
                        size=int(log_file.get("size") or 0),
                        kind="log_config",
                    )
                )

        # 5) 启动参数（保留原始结构，规则与占位符交由 LaunchBuilder 处理）
        arguments = version_json.get("arguments")
        if arguments:
            jvm_args: list[Any] = list(arguments.get("jvm") or [])
            game_args: list[Any] = list(arguments.get("game") or [])
        else:
            jvm_args = []
            game_args: list[Any] = str(version_json.get("minecraftArguments", "")).split()

        return ResolvedVersion(
            id=vid,
            version_type=version_json.get("type", "release"),
            main_class=version_json.get("mainClass", "net.minecraft.client.main.Main"),
            java_major=java_major,
            jar_path=jar_path,
            natives_dir=natives_dir,
            assets_root=assets_root,
            asset_index_id=asset_index_id,
            asset_index_path=asset_index_path,
            libraries=libraries,
            classpath=classpath,
            jvm_args=jvm_args,
            game_args=game_args,
            os_name=self.os_name,
            os_arch=self.os_arch,
            log_config=log_config,
            files=files,
            raw=version_json,
        )

    # -------------------------------------------------------------- libraries
    def _process_library(self, raw_lib: dict[str, Any]) -> tuple[list[FileEntry], Optional[LibraryInfo]]:
        name = raw_lib.get("name", "")
        downloads = raw_lib.get("downloads", {}) or {}
        artifact = downloads.get("artifact") or {}
        natives_map = raw_lib.get("natives")
        exclude = tuple((raw_lib.get("extract") or {}).get("exclude", ["META-INF/"]))

        entries: list[FileEntry] = []
        info = LibraryInfo(name=name, exclude=exclude)

        if natives_map:
            # 老式原生库：artifact 进 classpath，classifier 需解压
            if artifact.get("url"):
                path = self.paths.libraries_dir / artifact["path"]
                entries.append(self._entry(artifact, path, "library"))
                info.on_classpath = True
                info.artifact_path = path
            classifier = self._pick_classifier(natives_map)
            native = (downloads.get("classifiers") or {}).get(classifier) if classifier else None
            if native and native.get("url"):
                native_path = self.paths.libraries_dir / native["path"]
                native_entry = self._entry(native, native_path, "native")
                entries.append(native_entry)
                info.native_jar = native_entry
        elif artifact.get("url"):
            path = self.paths.libraries_dir / artifact["path"]
            is_native = _has_classifier(name)
            entry = self._entry(artifact, path, "native" if is_native else "library")
            entries.append(entry)
            if is_native:
                info.native_jar = entry
            else:
                info.on_classpath = True
                info.artifact_path = path
        else:
            # 无 downloads 的库：尝试用 Maven 坐标拼 URL
            fallback = self._maven_fallback(name)
            if fallback is None:
                return [], None
            entries.append(fallback)
            if _has_classifier(name):
                info.native_jar = fallback
            else:
                info.on_classpath = True
                info.artifact_path = fallback.target_path

        return entries, info

    def _entry(self, download: dict[str, Any], path: Path, kind: str) -> FileEntry:
        return FileEntry(
            url=download["url"],
            target_path=path,
            sha1=download.get("sha1"),
            size=int(download.get("size") or 0),
            kind=kind,
        )

    def _maven_fallback(self, name: str) -> Optional[FileEntry]:
        coords = _parse_maven(name)
        if coords is None:
            return None
        group, artifact, version, classifier, ext = coords
        relative = (
            f"{group.replace('.', '/')}/{artifact}/{version}/"
            f"{artifact}-{version}"
            + (f"-{classifier}" if classifier else "")
            + f".{ext}"
        )
        return FileEntry(
            url=LIBRARIES_MIRROR + relative,
            target_path=self.paths.libraries_dir / relative,
            kind="library",
        )

    def _pick_classifier(self, natives_map: dict[str, str]) -> Optional[str]:
        base = natives_map.get(self.os_name)
        if not base:
            return None
        if self.os_arch == "arm64" and f"{base}-arm64" in natives_map.values():
            return f"{base}-arm64"
        return base

    # ----------------------------------------------------------------- assets
    def _collect_assets(
        self,
        index_url: str,
        index_path: Path,
        assets_root: Path,
        progress: Optional[ProgressFn],
        cancel: Optional[Callable[[], bool]] = None,
    ) -> list[FileEntry]:
        _check_cancel(cancel)
        # 本地已有索引时直接读盘，避免重复联网；缺失时按可取消的方式下载。
        index: Optional[dict[str, Any]] = None
        if index_path.exists():
            try:
                index = json.loads(index_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                index = None
        if index is None:
            net.download_file(index_url, index_path, cancel=cancel)
            index = json.loads(index_path.read_text(encoding="utf-8"))

        objects = index.get("objects", {})
        total = len(objects)
        if progress:
            progress("assets", 0, total)

        entries: list[FileEntry] = []
        seen: set[str] = set()
        for i, obj in enumerate(objects.values(), start=1):
            if i % 256 == 0:
                _check_cancel(cancel)
            digest = obj.get("hash")
            if not digest or digest in seen:
                continue
            seen.add(digest)
            entries.append(
                FileEntry(
                    url=f"{RESOURCES_URL}{digest[:2]}/{digest}",
                    target_path=assets_root / "objects" / digest[:2] / digest,
                    sha1=digest,
                    size=int(obj.get("size") or 0),
                    kind="asset",
                )
            )
            if progress and i % 500 == 0:
                progress("assets", i, total)
        if progress:
            progress("assets", total, total)
        return entries


def _lib_key(name: str) -> str:
    """库去重键：group:artifact:classifier（忽略版本，便于子版本覆盖）。"""
    raw = name.split("@", 1)[0]
    parts = raw.split(":")
    if len(parts) < 2:
        return raw
    group, artifact = parts[0], parts[1]
    classifier = parts[3] if len(parts) >= 4 else ""
    return f"{group}:{artifact}:{classifier}"


def _merge_libraries(parent: list[dict], child: list[dict]) -> list[dict]:
    """父在前子在后，同名库由子版本覆盖，同时保持父版本原有顺序。"""
    merged: dict[str, dict] = {}
    order: list[str] = []
    for lib in [*parent, *child]:
        key = _lib_key(str(lib.get("name", "")))
        if key not in merged:
            order.append(key)
        merged[key] = lib
    return [merged[key] for key in order]


def _merge(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    """把子版本 JSON 合并到父版本之上（子优先）。"""
    merged: dict[str, Any] = dict(parent)
    for key, value in child.items():
        if key in ("libraries", "arguments", "id"):
            continue
        merged[key] = value

    merged["libraries"] = _merge_libraries(
        list(parent.get("libraries") or []), list(child.get("libraries") or [])
    )

    parent_args = parent.get("arguments") or {}
    child_args = child.get("arguments") or {}
    if parent.get("arguments") or child.get("arguments"):
        merged["arguments"] = {
            section: list(parent_args.get(section) or []) + list(child_args.get(section) or [])
            for section in ("jvm", "game")
        }
        # 有了结构化 arguments 后，旧的 minecraftArguments 必须移除，避免重复拼接。
        merged.pop("minecraftArguments", None)

    merged["id"] = child.get("id") or parent.get("id")
    return merged


def _has_classifier(name: str) -> bool:
    """Maven 坐标含 classifier，例如 org.lwjgl:lwjgl:3.3.1:natives-linux。"""
    return len(name.split(":")) >= 4


def _check_cancel(cancel: Optional[Callable[[], bool]]) -> None:
    if cancel and cancel():
        raise net.DownloadCancelled("已取消")


def _parse_maven(name: str) -> Optional[tuple[str, str, str, str, str]]:
    ext = "jar"
    if "@" in name:
        name, ext = name.rsplit("@", 1)
    parts = name.split(":")
    if len(parts) < 3:
        return None
    group, artifact, version = parts[0], parts[1], parts[2]
    classifier = parts[3] if len(parts) >= 4 else ""
    return group, artifact, version, classifier, ext