"""JavaManager：探测本地 Java、从 Adoptium 下载并安装 JRE。

只做「找 Java / 装 Java」这一件事：不启动游戏、不依赖 Web/UI 层。
下载走 ``engine.net``，因此会自动应用 ``engine.mirror`` 的镜像候选。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tarfile
import threading
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Optional, Sequence

from . import net
from .models import GamePaths
from .rules import detect_os_arch, detect_os_name

# Adoptium API v3：取某个 feature version 的最新 JRE。
ADOPTIUM_API = "https://api.adoptium.net/v3/assets/latest"
PROBE_TIMEOUT = 10
# Adoptium API 在部分网络下不可达，超时设短一点，失败时尽快给出明确提示。
ADOPTIUM_TIMEOUT = 12

# 进度回调：入参为本次新增的字节数。
ProgressFn = Callable[[int], None]
CancelFn = Callable[[], bool]

# JavaManager 每次调用都会新建实例，用模块级锁串行化「下载并解压同一个运行时」。
_INSTALL_LOCK = threading.Lock()


class JavaError(Exception):
    """Java 探测 / 安装过程中的可预期错误。"""


@dataclass
class JavaInfo:
    """一个可用的本地 Java 运行时。"""

    path: str            # java 可执行文件绝对路径
    version: str         # 完整版本字符串，如 "17.0.9"
    major: int           # 主版本号，如 17
    vendor: str = ""     # 如 "Eclipse Adoptium" / "Oracle Corporation"，解析不到就空串
    arch: str = ""       # "x86_64" / "arm64" / "x86" / ""
    source: str = ""     # "PATH" / "JAVA_HOME" / "system" / "downloaded"，来源标记
    extra: dict = field(default_factory=dict)


@dataclass
class JavaRuntime:
    """可从 Adoptium 下载的运行时。"""

    major: int
    version: str
    os: str
    arch: str
    url: str
    size: int
    sha1: str = ""
    vendor: str = ""


# --------------------------------------------------------------------- 纯函数

_ADOPTIUM_OS = {
    "windows": "windows",
    "linux": "linux",
    "osx": "mac",
}

_ADOPTIUM_ARCH = {
    "x86_64": "x64",
    "x64": "x64",
    "arm64": "aarch64",
    "aarch64": "aarch64",
    "x86": "x86",
    "i386": "x86",
    "arm": "arm",
    "arm32": "arm",
}

_ARCH_ALIASES = {
    "amd64": "x86_64",
    "x86_64": "x86_64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "x86": "x86",
    "i386": "x86",
    "i486": "x86",
    "i586": "x86",
    "i686": "x86",
    "arm": "arm",
}

# 支持的压缩格式后缀（顺序敏感，长的放前面）。
_ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".zip")


def adoptium_os_name(os_name: str) -> str:
    """把 Mojang 的 OS 名映射成 Adoptium 的 OS 参数。"""
    return _ADOPTIUM_OS.get((os_name or "").lower(), os_name)


def adoptium_arch(os_arch: str) -> str:
    """把 Mojang 的架构名映射成 Adoptium 的 arch 参数。"""
    return _ADOPTIUM_ARCH.get((os_arch or "").lower(), os_arch)


def _extract_property(text: str, key: str) -> str:
    """从 ``-XshowSettings:properties`` 输出里取某个属性值，取不到返回空串。"""
    match = re.search(
        rf"^\s*{re.escape(key)}\s*=\s*(.+?)\s*$", text, re.MULTILINE
    )
    return match.group(1).strip() if match else ""


def _normalize_arch(raw: str) -> str:
    """把 java 的 os.arch 归一化成 x86_64 / arm64 / x86 / arm，未知返回空串。"""
    return _ARCH_ALIASES.get((raw or "").strip().lower(), "")


def _major_from_version(version: str) -> int:
    """从版本串推导主版本号：1.8.0_292 -> 8，17.0.9 -> 17。"""
    numbers = re.findall(r"\d+", version or "")
    if not numbers:
        return 0
    first = int(numbers[0])
    if first == 1 and len(numbers) > 1:
        return int(numbers[1])
    return first


def _guess_vendor(text: str) -> str:
    """当 java.vendor 缺失时，从 banner 文本里猜一个厂商标识。"""
    lowered = text.lower()
    if "adoptium" in lowered or "temurin" in lowered:
        return "Eclipse Adoptium"
    if "oracle" in lowered:
        return "Oracle Corporation"
    if "zulu" in lowered:
        return "Azul Systems, Inc."
    if "microsoft" in lowered:
        return "Microsoft"
    if "corretto" in lowered or "amazon" in lowered:
        return "Amazon.com Inc."
    return ""


def parse_java_version_output(text: str, executable: str) -> Optional[JavaInfo]:
    """解析 ``java -XshowSettings:properties -version`` 的输出文本。

    优先读取 ``java.version`` 属性，缺失时回退 banner 里引号中的版本号；
    解析不出 ``java.version`` 时返回 ``None``。
    """
    if not text:
        return None

    version = _extract_property(text, "java.version")
    if not version:
        match = re.search(r'version "([^"]+)"', text)
        if match:
            version = match.group(1)
    if not version:
        return None

    major = _major_from_version(version)
    if major <= 0:
        return None

    vendor = _extract_property(text, "java.vendor") or _guess_vendor(text)
    arch = _normalize_arch(_extract_property(text, "os.arch"))

    return JavaInfo(
        path=executable,
        version=version,
        major=major,
        vendor=vendor,
        arch=arch,
    )


def _version_key(version: str) -> tuple[int, ...]:
    """把版本串拆成数字元组，用于同 major 内比较高低。"""
    numbers = re.findall(r"\d+", version or "")
    return tuple(int(n) for n in numbers)


def _dedupe_by_major(infos: list[JavaInfo]) -> list[JavaInfo]:
    """按 major 去重：同一 major 保留版本号最高者。"""
    best: dict[int, JavaInfo] = {}
    for info in infos:
        current = best.get(info.major)
        if current is None or _version_key(info.version) > _version_key(current.version):
            best[info.major] = info
    return sorted(best.values(), key=lambda item: item.major)


def _archive_suffix(url: str) -> str:
    """按 URL 后缀判断压缩格式，返回统一的后缀名。"""
    lowered = url.split("?", 1)[0].lower()
    for suffix in _ARCHIVE_SUFFIXES:
        if lowered.endswith(suffix):
            return ".tar.gz" if suffix == ".tgz" else suffix
    return ".zip"


def _archive_kind(url: str) -> str:
    """返回解压方式：tar 或 zip。"""
    return "tar" if _archive_suffix(url) in (".tar.gz", ".tgz") else "zip"


def _strip_top_component(name: str) -> Optional[str]:
    """剥掉压缩包内的顶层目录，返回安全的相对路径；不合法返回 None。"""
    normalized = (name or "").replace("\\", "/")
    parts = normalized.split("/", 1)
    if len(parts) < 2:
        return None
    rel = parts[1]
    if not rel:
        return None
    pure = PurePosixPath(rel)
    if pure.is_absolute() or any(part == ".." for part in pure.parts):
        return None
    return pure.as_posix()


def _extract_zip(archive: Path, target_dir: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            rel = _strip_top_component(info.filename)
            if rel is None:
                continue
            dest = target_dir / rel
            if info.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)


def _extract_tar(archive: Path, target_dir: Path) -> None:
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            rel = _strip_top_component(member.name)
            if rel is None:
                continue
            dest = target_dir / rel
            if member.isdir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            if member.issym() or member.islnk():
                _safe_symlink(dest, member.linkname)
                continue
            if not member.isfile():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            if src is None:
                continue
            with src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)


def _safe_symlink(dest: Path, target: str) -> None:
    """尽力创建符号链接；Windows 或异常情况下静默跳过。"""
    if not target or target.startswith("/") or target.startswith("\\"):
        return
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() or dest.is_symlink():
            dest.unlink()
        os.symlink(target, dest)
    except OSError:
        pass


class JavaManager:
    """探测本机 Java 并从 Adoptium 安装 JRE。"""

    def __init__(self, paths: GamePaths, *, cache_file: Path | None = None) -> None:
        self.paths = paths
        # 预留：调用方可注入一个缓存文件路径，供未来持久化扫描结果使用。
        self.cache_file = Path(cache_file) if cache_file is not None else None

    @property
    def runtimes_dir(self) -> Path:
        return self.paths.root / "runtime"

    # -------------------------------------------------------------- 探测本机
    def probe(self, executable: str) -> Optional[JavaInfo]:
        """运行 ``executable -XshowSettings:properties -version`` 并解析结果。

        任何失败（不存在、超时、无权限等）都返回 ``None``，不抛异常。
        """
        kwargs: dict = {}
        if os.name == "nt":
            # 避免探测 Java 时在桌面上闪出黑色控制台窗口。
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            proc = subprocess.run(
                [executable, "-XshowSettings:properties", "-version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=PROBE_TIMEOUT,
                **kwargs,
            )
        except (OSError, subprocess.SubprocessError):
            return None

        text = (proc.stdout or "") + "\n" + (proc.stderr or "")
        return parse_java_version_output(text, executable)

    def scan(self) -> list[JavaInfo]:
        """扫描 PATH / JAVA_HOME / 系统安装目录 / 已下载运行时。

        结果按 major 去重，同一 major 只保留版本号最高者。
        """
        infos: list[JavaInfo] = []
        seen: set[str] = set()
        for candidate, source in self._iter_candidates():
            # PATH / 系统目录 / 已下载运行时可能指向同一个可执行文件，先去重再探测。
            key = os.path.normcase(os.path.abspath(candidate))
            if key in seen:
                continue
            seen.add(key)
            info = self.probe(candidate)
            if info is None:
                continue
            if not info.source:
                info.source = source
            infos.append(info)
        return _dedupe_by_major(infos)

    def find_for(self, major: int, *, extra: Sequence[str] = ()) -> Optional[JavaInfo]:
        """为指定 major 找 Java：extra 优先，其次 scan() 结果。

        没有 exact 匹配时返回 major 最大的那个，交由调用方决定是否接受。
        """
        infos: list[JavaInfo] = []
        seen: set[str] = set()

        for candidate in extra:
            info = self.probe(candidate)
            if info is None:
                continue
            key = os.path.normcase(os.path.abspath(info.path))
            if key in seen:
                continue
            seen.add(key)
            # 配置的 Java 正好满足要求时直接返回，不再扫描全机 Java（启动更快）。
            if info.major == major:
                return info
            infos.append(info)

        for info in self.scan():
            key = os.path.normcase(os.path.abspath(info.path))
            if key in seen:
                continue
            seen.add(key)
            infos.append(info)

        for info in infos:
            if info.major == major:
                return info
        if not infos:
            return None
        return max(infos, key=lambda item: item.major)

    # ---------------------------------------------------------------- 候选枚举
    def _iter_candidates(self):
        """按优先级产出 (可执行文件路径, 来源标记)。"""
        windows = detect_os_name() == "windows"
        exe_name = "java.exe" if windows else "java"

        # 1) PATH
        found = shutil.which("java")
        if found:
            yield found, "PATH"

        # 2) JAVA_HOME
        java_home = os.environ.get("JAVA_HOME")
        if java_home:
            candidate = Path(java_home) / "bin" / exe_name
            if candidate.exists():
                yield str(candidate), "JAVA_HOME"

        # 3) 系统安装目录
        yield from self._iter_system(windows, exe_name)

        # 4) 已下载的运行时（runtimes_dir 下每个目录视为一个运行时）
        if self.runtimes_dir.is_dir():
            for candidate in sorted(self.runtimes_dir.glob(f"*/bin/{exe_name}")):
                yield str(candidate), "downloaded"

    def _iter_system(self, windows: bool, exe_name: str):
        if windows:
            bases = [
                os.environ.get("ProgramFiles"),
                os.environ.get("ProgramFiles(x86)"),
                os.environ.get("ProgramW6432"),
            ]
            vendor_dirs = ("Java", "Eclipse Adoptium", "Zulu", "Microsoft", "AdoptOpenJDK")
            for base in bases:
                if not base:
                    continue
                for vendor in vendor_dirs:
                    root = Path(base) / vendor
                    if not root.is_dir():
                        continue
                    for candidate in sorted(root.glob(f"*/bin/{exe_name}")):
                        yield str(candidate), "system"
            return

        if detect_os_name() == "osx":
            root = Path("/Library/Java/JavaVirtualMachines")
            for candidate in sorted(root.glob(f"*/Contents/Home/bin/{exe_name}")):
                yield str(candidate), "system"
            return

        for base in ("/usr/lib/jvm", "/usr/java", "/usr/lib64/jvm", "/opt/java"):
            root = Path(base)
            if not root.is_dir():
                continue
            for candidate in sorted(root.glob(f"*/bin/{exe_name}")):
                yield str(candidate), "system"

    # ---------------------------------------------------------------- Adoptium
    def list_available(
        self, major: int, *, cancel: Optional[CancelFn] = None
    ) -> list[JavaRuntime]:
        """查询 Adoptium API v3，返回该 major 可下载的 JRE 列表。

        传入 ``cancel`` 时，网络请求会在后台线程执行并轮询取消状态，
        避免 Adoptium 不可达时「点取消没反应」。
        """
        os_name = adoptium_os_name(detect_os_name())
        arch = adoptium_arch(detect_os_arch())
        url = (
            f"{ADOPTIUM_API}/{major}/hotspot"
            f"?os={os_name}&arch={arch}&image_type=jre&heap_size=normal"
        )
        data = None
        if cancel is None:
            data = net.get_json(url, timeout=ADOPTIUM_TIMEOUT)
        else:
            if cancel():
                raise net.DownloadCancelled("已取消")
            holder: dict = {}
            errors: list[BaseException] = []

            def fetch() -> None:
                try:
                    holder["data"] = net.get_json(url, timeout=ADOPTIUM_TIMEOUT)
                except BaseException as exc:  # noqa: BLE001 - 转交给调用线程
                    errors.append(exc)

            worker = threading.Thread(target=fetch, daemon=True)
            worker.start()
            while worker.is_alive():
                if cancel():
                    raise net.DownloadCancelled("已取消")
                worker.join(0.15)
            if errors:
                raise errors[0]
            data = holder.get("data")
        if not data:
            raise JavaError(f"Adoptium 未提供 Java {major} 的 JRE（{os_name}/{arch}）")

        runtimes: list[JavaRuntime] = []
        for item in data:
            binary = (item or {}).get("binary") or {}
            package = binary.get("package") or {}
            version = (item or {}).get("version") or {}
            link = package.get("link")
            if not link:
                continue
            runtimes.append(
                JavaRuntime(
                    major=int(version.get("major") or major),
                    version=str(version.get("semver") or ""),
                    os=os_name,
                    arch=arch,
                    url=str(link),
                    size=int(package.get("size") or 0),
                    sha1=str(package.get("checksum") or ""),
                    vendor="Eclipse Adoptium",
                )
            )
        if not runtimes:
            raise JavaError(f"Adoptium 返回的数据无法解析（Java {major}）")
        return runtimes

    def install(
        self,
        major: int,
        *,
        progress: Optional[ProgressFn] = None,
        cancel: Optional[CancelFn] = None,
        runtime: Optional[JavaRuntime] = None,
    ) -> JavaInfo:
        """下载并解压 JRE 到 runtimes_dir，返回装好的 JavaInfo。

        多个任务同时需要同一个 Java 时串行执行，避免并发写坏运行时目录。
        """
        # 等待其它任务安装 Java 时也响应取消。
        while not _INSTALL_LOCK.acquire(timeout=0.5):
            if cancel and cancel():
                raise net.DownloadCancelled("已取消")
        try:
            return self._install_locked(
                major, progress=progress, cancel=cancel, runtime=runtime
            )
        finally:
            _INSTALL_LOCK.release()

    def _install_locked(
        self,
        major: int,
        *,
        progress: Optional[ProgressFn] = None,
        cancel: Optional[CancelFn] = None,
        runtime: Optional[JavaRuntime] = None,
    ) -> JavaInfo:
        """下载并解压 JRE 到 runtimes_dir，返回装好的 JavaInfo。

        目标目录已存在且能 probe 成功时直接复用（幂等，不重复下载）。
        ``runtime`` 可由调用方传入，避免重复请求 Adoptium API。
        """
        if cancel and cancel():
            raise net.DownloadCancelled("已取消")

        target_dir = self.runtimes_dir / f"temurin-{major}"
        java_bin = target_dir / "bin" / ("java.exe" if detect_os_name() == "windows" else "java")
        if java_bin.exists():
            info = self.probe(str(java_bin))
            if info is not None:
                info.source = "downloaded"
                return info

        runtime = runtime or self.list_available(major, cancel=cancel)[0]
        if cancel and cancel():
            raise net.DownloadCancelled("已取消")

        suffix = _archive_suffix(runtime.url)
        archive = self.runtimes_dir / f"temurin-{major}-jre{suffix}"
        self.runtimes_dir.mkdir(parents=True, exist_ok=True)

        def _on_chunk(delta: int) -> None:
            if cancel and cancel():
                raise net.DownloadCancelled("已取消")
            if progress:
                progress(delta)

        net.download_file(
            runtime.url,
            archive,
            expected_size=runtime.size,
            on_chunk=_on_chunk,
            cancel=cancel,
        )

        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)
        target_dir.mkdir(parents=True, exist_ok=True)
        if _archive_kind(runtime.url) == "tar":
            _extract_tar(archive, target_dir)
        else:
            _extract_zip(archive, target_dir)

        if java_bin.exists():
            try:
                os.chmod(java_bin, 0o755)
            except OSError:
                pass

        archive.unlink(missing_ok=True)

        if not java_bin.exists():
            raise JavaError(f"Java {major} 解压失败：未找到 {java_bin}")
        info = self.probe(str(java_bin))
        if info is None:
            # 不再伪造一个「安装成功」的结果；装完跑不起来时直接给出明确错误。
            raise JavaError(
                f"Java {major} 安装后无法运行：{java_bin}"
                "（可能缺少系统依赖，请手动安装 Java 后在设置中指定路径）"
            )
        info.source = "downloaded"
        return info