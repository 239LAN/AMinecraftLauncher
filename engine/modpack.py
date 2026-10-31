"""整合包导入导出：支持 Modrinth ``.mrpack`` 与 CurseForge 整合包（zip）。

导入流程完全在临时目录中进行，避免污染游戏目录：

1. 解压整合包到临时目录；
2. 复制 ``overrides/``（mrpack 还包括 ``client-overrides/``）到版本隔离目录；
3. 依据索引清单远程下载文件到版本隔离目录。

导出时把某个版本目录 ``mods/`` 下启用状态的 jar 打包成 ``overrides/mods/*``，
并生成最小的 ``modrinth.index.json``。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from . import net
from .mod_provider import ModProviderError, build_provider
from .models import GamePaths

# 索引 / 清单文件名
MODRINTH_INDEX = "modrinth.index.json"
CF_MANIFEST = "manifest.json"

# mrpack 依赖键 -> 加载器名
LOADER_FROM_DEP = {
    "fabric-loader": "fabric",
    "quilt-loader": "quilt",
    "forge": "forge",
    "neoforge": "neoforge",
}
# 加载器名 -> mrpack 依赖键（导出时使用）
DEP_FROM_LOADER = {value: key for key, value in LOADER_FROM_DEP.items()}

# 需要复制到游戏目录的 overrides 子目录
_OVERRIDE_DIRS_MRPACK = ("overrides", "client-overrides")
_OVERRIDE_DIRS_CF = ("overrides",)
# 统计 override 文件数时的路径前缀
_OVERRIDE_PREFIXES_MRPACK = ("overrides/", "client-overrides/", "server-overrides/")
_OVERRIDE_PREFIXES_CF = ("overrides/",)
# 整合包文件并发下载数：整合包动辄几十上百个 Mod，顺序下载会非常慢。
MAX_PARALLEL_DOWNLOADS = 8

ProgressFn = Callable[[str, int, int], None]
LogFn = Callable[[str], None]
CancelFn = Callable[[], bool]


class ModpackError(Exception):
    """整合包导入导出过程中的错误。"""


@dataclass
class ModpackInfo:
    """整合包解析结果。"""

    path: Path
    format: str            # "mrpack" 或 "curseforge"
    name: str
    version: str = ""
    game_version: str = ""
    loader: str = ""       # "fabric"/"forge"/"neoforge"/"quilt"/""
    loader_version: str = ""
    file_count: int = 0    # 需要远程下载的文件数
    override_count: int = 0  # 包内自带的 overrides 文件数


class ModpackInstaller:
    """读取整合包元数据、安装到版本隔离目录，以及导出 mrpack。"""

    def __init__(self, paths: GamePaths, *, timeout: int = 30) -> None:
        self.paths = paths
        self.timeout = timeout

    # ------------------------------------------------------------------ 解析
    def inspect(self, archive: Path) -> ModpackInfo:
        """读取整合包元数据，不产生任何下载或落盘。"""
        archive = Path(archive)
        if not archive.is_file():
            raise ModpackError(f"整合包不存在：{archive}")
        try:
            with zipfile.ZipFile(archive) as zf:
                names = zf.namelist()
                fmt = _detect_format(archive, names)
                if fmt == "mrpack":
                    return self._inspect_mrpack(archive, zf, names)
                return self._inspect_curseforge(archive, zf, names)
        except zipfile.BadZipFile as exc:
            raise ModpackError(f"无法识别的整合包格式：{exc}") from exc

    def _inspect_mrpack(
        self, archive: Path, zf: zipfile.ZipFile, names: list[str]
    ) -> ModpackInfo:
        index = _read_json_member(zf, MODRINTH_INDEX)
        deps = index.get("dependencies", {}) or {}

        loader = ""
        loader_version = ""
        for key, name in LOADER_FROM_DEP.items():
            if key in deps:
                loader = name
                loader_version = str(deps.get(key) or "")
                break

        files = index.get("files", []) or []
        # env.client == "unsupported" 的条目无需下载。
        file_count = sum(
            1 for entry in files if (entry.get("env") or {}).get("client") != "unsupported"
        )
        return ModpackInfo(
            path=archive,
            format="mrpack",
            name=index.get("name") or archive.stem,
            version=str(index.get("versionId", "") or ""),
            game_version=str(deps.get("minecraft", "") or ""),
            loader=loader,
            loader_version=loader_version,
            file_count=file_count,
            override_count=_count_override_files(names, _OVERRIDE_PREFIXES_MRPACK),
        )

    def _inspect_curseforge(
        self, archive: Path, zf: zipfile.ZipFile, names: list[str]
    ) -> ModpackInfo:
        manifest = _read_json_member(zf, CF_MANIFEST)
        minecraft = manifest.get("minecraft", {}) or {}

        loaders = minecraft.get("modLoaders", []) or []
        primary = next(
            (item for item in loaders if item.get("primary")),
            loaders[0] if loaders else {},
        )
        # modLoaders[].id 形如 "forge-47.2.0" / "fabric-0.15.11"。
        loader, _, loader_version = str(primary.get("id", "") or "").partition("-")

        files = manifest.get("files", []) or []
        return ModpackInfo(
            path=archive,
            format="curseforge",
            name=manifest.get("name") or archive.stem,
            version=str(manifest.get("version", "") or ""),
            game_version=str(minecraft.get("version", "") or ""),
            loader=loader,
            loader_version=loader_version,
            file_count=len(files),
            override_count=_count_override_files(names, _OVERRIDE_PREFIXES_CF),
        )

    # ------------------------------------------------------------------ 安装
    def install(
        self,
        archive: Path,
        *,
        version_id: str,
        progress: Optional[ProgressFn] = None,
        log: Optional[LogFn] = None,
        cancel: Optional[CancelFn] = None,
    ) -> ModpackInfo:
        """把整合包安装到 ``paths.version_dir(version_id)`` 隔离游戏目录。"""
        archive = Path(archive)
        info = self.inspect(archive)
        if log:
            log(f"开始导入整合包：{info.name}（{info.format}）")

        game_dir = self.paths.version_dir(version_id)
        game_dir.mkdir(parents=True, exist_ok=True)
        self._raise_if_cancelled(cancel)

        tmp = Path(tempfile.mkdtemp(prefix="modpack-import-"))
        try:
            with zipfile.ZipFile(archive) as zf:
                _safe_extract(zf, tmp)

            override_dirs = (
                _OVERRIDE_DIRS_MRPACK if info.format == "mrpack" else _OVERRIDE_DIRS_CF
            )
            for sub in override_dirs:
                source = tmp / sub
                if source.is_dir():
                    shutil.copytree(source, game_dir, dirs_exist_ok=True)
            self._raise_if_cancelled(cancel)

            if info.format == "mrpack":
                self._install_mrpack_files(archive, game_dir, progress, cancel)
            else:
                self._install_cf_files(archive, game_dir, progress, cancel)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        if log:
            log(f"整合包导入完成：{info.name}")
        return info

    def _install_mrpack_files(
        self,
        archive: Path,
        game_dir: Path,
        progress: Optional[ProgressFn],
        cancel: Optional[CancelFn],
    ) -> None:
        with zipfile.ZipFile(archive) as zf:
            index = _read_json_member(zf, MODRINTH_INDEX)

        entries = [
            entry
            for entry in (index.get("files", []) or [])
            if (entry.get("env") or {}).get("client") != "unsupported"
        ]
        items: list[tuple[str, Path, int, Optional[str], Optional[dict[str, str]]]] = []
        for entry in entries:
            relative = str(entry.get("path", "") or "").replace("\\", "/").strip()
            if not relative:
                continue
            # 清单里的路径来自整合包本身，必须拒绝绝对路径与 ../，避免写出游戏目录。
            pure = PurePosixPath(relative)
            if pure.is_absolute() or ".." in pure.parts:
                raise ModpackError(f"整合包条目包含非法路径：{relative}")
            downloads = entry.get("downloads", []) or []
            if not downloads:
                raise ModpackError(f"整合包条目缺少下载地址：{relative}")
            hashes = entry.get("hashes", {}) or {}
            items.append(
                (
                    downloads[0],
                    game_dir / relative,
                    int(entry.get("fileSize") or 0),
                    hashes.get("sha1"),
                    None,
                )
            )
        self._download_items(items, progress, cancel)

    def _install_cf_files(
        self,
        archive: Path,
        game_dir: Path,
        progress: Optional[ProgressFn],
        cancel: Optional[CancelFn],
    ) -> None:
        with zipfile.ZipFile(archive) as zf:
            manifest = _read_json_member(zf, CF_MANIFEST)

        entries = manifest.get("files", []) or []

        try:
            provider = build_provider("curseforge", timeout=self.timeout)
        except ModProviderError as exc:
            raise ModpackError(str(exc)) from exc

        headers = provider.download_headers()
        mods_dir = game_dir / "mods"
        cache: dict[str, list] = {}
        items: list[tuple[str, Path, int, Optional[str], Optional[dict[str, str]]]] = []

        for entry in entries:
            self._raise_if_cancelled(cancel)
            project_id = str(entry.get("projectID", "") or "")
            file_id = str(entry.get("fileID", "") or "")

            files = cache.get(project_id)
            if files is None:
                try:
                    files = provider.resolve_files(project_id)
                except ModProviderError as exc:
                    raise ModpackError(str(exc)) from exc
                cache[project_id] = files

            match = next((item for item in files if item.file_id == file_id), None)
            if match is None:
                # 文件列表默认分页，整合包引用的旧文件可能不在前几页，按 ID 直接查。
                fetch_file = getattr(provider, "fetch_file", None)
                if callable(fetch_file):
                    match = fetch_file(project_id, file_id)
            if match is None:
                raise ModpackError(f"CurseForge 项目 {project_id} 未找到文件 {file_id}")

            filename = Path(str(match.filename or "")).name
            if not filename:
                raise ModpackError(f"CurseForge 项目 {project_id} 的文件名无效")
            items.append(
                (match.url, mods_dir / filename, match.size, match.sha1, headers)
            )

        self._download_items(items, progress, cancel)

    def _download(
        self,
        url: str,
        dest: Path,
        *,
        size: int = 0,
        sha1: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
        cancel: Optional[CancelFn] = None,
    ) -> None:
        """下载单个文件并按 sha1 校验，失败则删除半成品。"""
        net.download_file(
            url,
            dest,
            expected_size=size,
            timeout=self.timeout,
            headers=headers,
            cancel=cancel,
        )
        if sha1 and _sha1_file(dest).lower() != str(sha1).lower():
            Path(dest).unlink(missing_ok=True)
            raise ModpackError(f"文件校验失败：{Path(dest).name}")

    def _download_items(
        self,
        items: list[tuple[str, Path, int, Optional[str], Optional[dict[str, str]]]],
        progress: Optional[ProgressFn],
        cancel: Optional[CancelFn],
    ) -> None:
        """并发下载整合包文件，按完成个数上报 ``mods`` 阶段进度。"""
        # 同一目标路径可能在清单里出现多次，去重避免多个线程同时写一个文件。
        unique: dict[str, tuple[str, Path, int, Optional[str], Optional[dict[str, str]]]] = {}
        for item in items:
            unique.setdefault(str(item[1]), item)
        items = list(unique.values())
        total = len(items)
        if progress:
            progress("mods", 0, total)
        if not items:
            return
        done = 0
        if total == 1:
            url, dest, size, sha1, headers = items[0]
            self._download(url, dest, size=size, sha1=sha1, headers=headers, cancel=cancel)
            if progress:
                progress("mods", 1, 1)
            return

        workers = min(MAX_PARALLEL_DOWNLOADS, total)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {}
            for url, dest, size, sha1, headers in items:
                future = pool.submit(
                    self._download,
                    url,
                    dest,
                    size=size,
                    sha1=sha1,
                    headers=headers,
                    cancel=cancel,
                )
                futures[future] = dest
            try:
                for future in as_completed(futures):
                    future.result()
                    done += 1
                    if progress:
                        progress("mods", done, total)
            except BaseException:
                for future in futures:
                    future.cancel()
                raise

    @staticmethod
    def _raise_if_cancelled(cancel: Optional[CancelFn]) -> None:
        if cancel and cancel():
            raise ModpackError("已取消")

    # ------------------------------------------------------------------ 导出
    def export_mrpack(
        self,
        *,
        version_id: str,
        dest_zip: Path,
        name: str = "",
        pack_version: str = "1.0",
        game_version: str = "",
        loader: str = "",
        loader_version: str = "",
        isolated: bool = True,
    ) -> Path:
        """把版本目录下启用状态的 mod 打包为 mrpack（overrides + 最小索引）。"""
        dest_zip = Path(dest_zip)
        mods_dir = self.paths.mods_dir_for(version_id, isolated=isolated)

        dependencies: dict[str, str] = {}
        if game_version:
            dependencies["minecraft"] = game_version
        if loader and loader != "vanilla":
            dependencies[DEP_FROM_LOADER.get(loader, loader)] = loader_version

        index = {
            "formatVersion": 1,
            "game": "minecraft",
            "versionId": pack_version,
            "name": name or version_id,
            "files": [],
            "dependencies": dependencies,
        }

        dest_zip.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(MODRINTH_INDEX, json.dumps(index, ensure_ascii=False, indent=2))
            if mods_dir.is_dir():
                # 启用状态的 mod 为 *.jar，禁用状态为 *.jar.disabled，不会命中该 glob。
                for jar in sorted(mods_dir.glob("*.jar")):
                    if jar.is_file():
                        zf.write(jar, f"overrides/mods/{jar.name}")
        return dest_zip


# ---------------------------------------------------------------------- 工具
def _detect_format(archive: Path, names: list[str]) -> str:
    """识别整合包格式；无法识别时抛错。"""
    if archive.suffix.lower() == ".mrpack":
        if MODRINTH_INDEX in names:
            return "mrpack"
        raise ModpackError("无法识别的整合包格式")
    if CF_MANIFEST in names:
        return "curseforge"
    if MODRINTH_INDEX in names:
        return "mrpack"
    raise ModpackError("无法识别的整合包格式")


def _read_json_member(zf: zipfile.ZipFile, name: str) -> dict:
    """读取 zip 内某个 JSON 成员并解析。"""
    try:
        raw = zf.read(name).decode("utf-8")
    except KeyError as exc:
        raise ModpackError(f"整合包缺少 {name}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ModpackError(f"整合包 {name} 不是合法 JSON") from exc


def _count_override_files(names: list[str], prefixes: tuple[str, ...]) -> int:
    """统计 zip 内 overrides 相关文件数量（不含目录项）。"""
    count = 0
    for name in names:
        if name.endswith("/"):
            continue
        if any(name.startswith(prefix) for prefix in prefixes):
            count += 1
    return count


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """安全解压：拒绝会把文件写到目标目录之外的路径（Zip Slip）。"""
    base = Path(dest).resolve()
    for member in zf.infolist():
        target = (base / member.filename).resolve()
        if target != base and base not in target.parents:
            raise ModpackError(f"整合包内包含非法路径：{member.filename}")
        zf.extract(member, base)


def _sha1_file(path: Path) -> str:
    """计算文件 SHA1。"""
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()