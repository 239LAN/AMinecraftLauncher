"""下载源镜像：默认镜像优先，镜像不可用时自动回退官方源。

设计要点：
- 只做「URL 重写 + 候选列表」，不改变上层调用方式；
- ``download_file`` / ``get_json`` / ``fetch_bytes`` 会依次尝试候选地址，
  因此单个文件可以先走镜像、失败再走官方，无需上层感知；
- 大文件分片下载时，下载器会轮换每个分片的候选顺序：相邻分片分别优先走
  镜像 / 官方源，两个源都可用时同时下载；任一源失败时该分片再回退另一个源；
- 不参与重写的是 OAuth / API 类请求（Modrinth、微软登录等），
  这些接口本就没有镜像，走 ``request_json`` 不经过候选逻辑。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from . import net

BMCLAPI_ROOT = "https://bmclapi2.bangbang93.com"

# 前缀重写规则：命中后拼到镜像根上。顺序敏感，长的前缀必须排在短的前面。
BMCLAPI_RULES: list[tuple[str, str]] = [
    ("https://launchermeta.mojang.com", BMCLAPI_ROOT),
    ("https://piston-meta.mojang.com", BMCLAPI_ROOT),
    ("https://piston-data.mojang.com", BMCLAPI_ROOT),
    ("https://launcher.mojang.com", BMCLAPI_ROOT),
    ("https://libraries.minecraft.net", f"{BMCLAPI_ROOT}/maven"),
    ("https://maven.minecraftforge.net", f"{BMCLAPI_ROOT}/maven"),
    ("https://files.minecraftforge.net/maven", f"{BMCLAPI_ROOT}/maven"),
    ("https://maven.neoforged.net/releases", f"{BMCLAPI_ROOT}/maven"),
    ("https://maven.neoforged.net", f"{BMCLAPI_ROOT}/maven"),
    ("https://maven.fabricmc.net", f"{BMCLAPI_ROOT}/maven"),
    ("https://maven.quiltmc.org", f"{BMCLAPI_ROOT}/maven"),
    ("https://resources.download.minecraft.net", f"{BMCLAPI_ROOT}/assets"),
    ("https://meta.fabricmc.net", f"{BMCLAPI_ROOT}/fabric-meta"),
    ("https://meta.quiltmc.org", f"{BMCLAPI_ROOT}/quilt-meta"),
]
BMCLAPI_RULES.sort(key=lambda rule: len(rule[0]), reverse=True)

# 探针：一个体积小、必定存在的镜像地址。
PROBE_URL = f"{BMCLAPI_ROOT}/mc/game/version_manifest_v2.json"
# 官方源探针：用于判断能否让镜像与官方源同时下载（官方不可达时只用镜像）。
OFFICIAL_PROBE_URL = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"

SOURCE_OFFICIAL = "official"
SOURCE_BMCLAPI = "bmclapi"
SOURCE_AUTO = "auto"


@dataclass
class MirrorStatus:
    source: str
    active: str
    available: bool
    latency_ms: int
    checked_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "active": self.active,
            "available": self.available,
            "latency_ms": self.latency_ms,
            "checked_at": self.checked_at,
            "sources": [SOURCE_OFFICIAL, SOURCE_BMCLAPI],
        }


def rewrite(url: str) -> Optional[str]:
    """把官方地址重写成 BMCLAPI 地址；无法重写返回 None。"""
    for prefix, replacement in BMCLAPI_RULES:
        if url.startswith(prefix):
            return replacement + url[len(prefix) :]
    return None


class MirrorManager:
    """管理当前启用的下载源，并把自己安装进 net 层。"""

    def __init__(self, source: str = SOURCE_AUTO, *, probe_timeout: float = 4.0) -> None:
        self.source = source if source in {SOURCE_AUTO, SOURCE_OFFICIAL, SOURCE_BMCLAPI} else SOURCE_AUTO
        self.probe_timeout = probe_timeout
        self.mirror_enabled = self.source != SOURCE_OFFICIAL
        self.latency_ms = 0
        self.checked_at = 0.0
        # 官方源是否可达：可达时允许镜像与官方源同时下载不同分片。
        self.official_available = False
        self.official_latency_ms = 0
        self.official_checked_at = 0.0
        self._official_probe: Optional[threading.Thread] = None

    # ------------------------------------------------------------- 候选择序
    @property
    def active(self) -> str:
        if self.source == SOURCE_OFFICIAL or not self.mirror_enabled:
            return SOURCE_OFFICIAL
        return SOURCE_BMCLAPI

    def candidates(self, url: str) -> list[str]:
        """返回该 URL 的候选地址列表（镜像优先，官方兜底）。"""
        if self.active != SOURCE_BMCLAPI:
            return [url]
        mirrored = rewrite(url)
        if not mirrored or mirrored == url:
            return [url]
        return [mirrored, url]

    def dual_source(self) -> bool:
        """镜像与官方源是否都可用（决定是否让两个源同时承担分片下载）。"""
        return self.active == SOURCE_BMCLAPI and self.official_available

    def install(self) -> None:
        net.set_url_candidates(self.candidates)
        net.set_dual_source_policy(self.dual_source)

    def set_source(self, source: str) -> None:
        self.source = source if source in {SOURCE_AUTO, SOURCE_OFFICIAL, SOURCE_BMCLAPI} else SOURCE_AUTO
        self.mirror_enabled = self.source != SOURCE_OFFICIAL

    # ------------------------------------------------------------------ 测速
    def probe(self) -> MirrorStatus:
        """探测下载源可用性；镜像失败时本次会话内退回官方源。"""
        if self.source == SOURCE_OFFICIAL:
            self.mirror_enabled = False
            return self.status()

        self._probe_official_in_background()
        start = time.monotonic()
        try:
            with net._open(PROBE_URL, timeout=self.probe_timeout) as response:  # noqa: SLF001
                response.read(256)
            self.latency_ms = int((time.monotonic() - start) * 1000)
            self.mirror_enabled = True
        except Exception:  # noqa: BLE001 - 任何失败都视为镜像不可用
            self.latency_ms = 0
            # 自动模式探测失败直接用官方源，避免每个文件都先等镜像超时。
            self.mirror_enabled = self.source == SOURCE_BMCLAPI
        self.checked_at = time.time()
        return self.status()

    def _probe_official_in_background(self) -> None:
        """官方源探测放到后台线程，避免拖慢镜像探测 / 启动。"""
        if self._official_probe is not None and self._official_probe.is_alive():
            return
        self._official_probe = threading.Thread(
            target=self._probe_official, daemon=True
        )
        self._official_probe.start()

    def _probe_official(self) -> None:
        start = time.monotonic()
        try:
            with net._open(  # noqa: SLF001
                OFFICIAL_PROBE_URL, timeout=min(self.probe_timeout, 5.0)
            ) as response:
                response.read(256)
            self.official_latency_ms = int((time.monotonic() - start) * 1000)
            self.official_available = True
        except Exception:  # noqa: BLE001 - 官方不可达时保持单镜像下载
            self.official_latency_ms = 0
            self.official_available = False
        self.official_checked_at = time.time()

    def status(self) -> MirrorStatus:
        return MirrorStatus(
            source=self.source,
            active=self.active,
            available=self.latency_ms > 0,
            latency_ms=self.latency_ms,
            checked_at=self.checked_at or time.time(),
        )


# 进程级默认实例，供 UI / 服务层共享。
manager = MirrorManager()


def install(source: str = SOURCE_AUTO) -> MirrorStatus:
    """按给定来源安装镜像重写，并做一次探测。"""
    manager.set_source(source)
    manager.install()
    return manager.probe()