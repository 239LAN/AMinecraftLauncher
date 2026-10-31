"""ModProvider：Mod 检索与文件解析的抽象，实现 Modrinth 与 CurseForge。

CurseForge 目前支持 Mod 与整合包的检索（classId 分别为 6 / 4471），
其它类型（资源包 / 光影 / 数据包）请使用 Modrinth。

CurseForge 改走 mcimirror 公共镜像，无需 API Key：

- API：``api.curseforge.com/v1`` → ``mod.mcimirror.top/curseforge/v1``
- 文件：``edge.forgecdn.net/...`` → ``mod.mcimirror.top/...``（镜像 302 到 CDN）

``mediafilez.forgecdn.net`` 不在镜像范围内，保持原地址。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote, urlsplit, urlunsplit

from . import net

MODRINTH_API = "https://api.modrinth.com/v2"
# CurseForge 官方 API 需要 Key，统一走 mcimirror 公共镜像。
CURSEFORGE_API = "https://mod.mcimirror.top/curseforge/v1"
CURSEFORGE_MIRROR = "https://mod.mcimirror.top"
# 仅重写 edge CDN；mediafilez 不在镜像范围内。
CURSEFORGE_CDN_HOST = "edge.forgecdn.net"
CURSEFORGE_GAME_ID = 432  # Minecraft
CURSEFORGE_MOD_CLASS = 6  # Mod
# CurseForge 的 classId：目前只覆盖 Mod 与整合包，其余类型请走 Modrinth。
CURSEFORGE_CLASS_IDS = {
    "mod": CURSEFORGE_MOD_CLASS,
    "modpack": 4471,
}

# Modrinth 项目类型 -> 网页路径段（默认回落到 mod）。
MODRINTH_PAGE_SEGMENT = {
    "mod": "mod",
    "resourcepack": "resourcepack",
    "shader": "shader",
    "datapack": "datapack",
    "modpack": "modpack",
}

# Modrinth loader 关键词 <-> CurseForge modLoaderType
LOADER_TO_CF = {
    "forge": 1,
    "fabric": 4,
    "quilt": 5,
    "neoforge": 6,
}


class ModProviderError(Exception):
    pass


@dataclass
class ModSearchResult:
    source: str
    project_id: str
    name: str
    author: str = ""
    description: str = ""
    downloads: int = 0
    icon_url: str = ""
    page_url: str = ""


@dataclass
class ModFile:
    source: str
    project_id: str
    file_id: str
    filename: str
    url: str
    size: int = 0
    sha1: Optional[str] = None
    version: str = ""
    game_versions: list[str] = field(default_factory=list)
    dependencies: list[dict] = field(default_factory=list)


class ModProvider(ABC):
    """Mod 源抽象：检索 + 解析出可直接下载的文件。"""

    name: str = "abstract"
    display_name: str = "抽象源"

    @abstractmethod
    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        offset: int = 0,
        game_version: Optional[str] = None,
        loader: Optional[str] = None,
        project_type: str = "mod",
    ) -> list[ModSearchResult]:
        """按关键词检索内容源（默认项目类型为 mod）。"""

    @abstractmethod
    def resolve_files(
        self,
        project_id: str,
        *,
        game_version: Optional[str] = None,
        loader: Optional[str] = None,
    ) -> list[ModFile]:
        """解析某个 Mod 在该游戏版本 / 加载器下可用的文件（按新到旧）。"""

    def download_headers(self) -> dict[str, str]:
        """下载该源文件时需要携带的请求头。"""
        return {}


class ModrinthProvider(ModProvider):
    """Modrinth 开放 API，无需 Key。"""

    name = "modrinth"
    display_name = "Modrinth"

    def __init__(self, *, timeout: int = 30, user_agent: str = "MiniCraftLauncher/0.1") -> None:
        self.timeout = timeout
        self.user_agent = user_agent

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self.user_agent, "Accept": "application/json"}

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        offset: int = 0,
        game_version: Optional[str] = None,
        loader: Optional[str] = None,
        project_type: str = "mod",
    ) -> list[ModSearchResult]:
        kind = (project_type or "mod").lower()
        segment = MODRINTH_PAGE_SEGMENT.get(kind, "mod")
        facets = [[f"project_type:{kind}"]]
        if game_version:
            facets.append([f"versions:{game_version}"])
        if loader:
            facets.append([f"categories:{loader.lower()}"])

        import json as _json

        url = (
            f"{MODRINTH_API}/search?query={quote(query)}"
            f"&limit={min(limit, 100)}&offset={offset}"
            f"&index=relevance&facets={quote(_json.dumps(facets))}"
        )
        status, payload = net.get_json_auth(url, headers=self._headers(), timeout=self.timeout)
        if status != 200:
            raise ModProviderError(_err(payload, "Modrinth 检索失败"))

        results = []
        for hit in payload.get("hits", []):
            results.append(
                ModSearchResult(
                    source=self.name,
                    project_id=hit.get("project_id", ""),
                    name=hit.get("title", ""),
                    author=hit.get("author", ""),
                    description=hit.get("description", ""),
                    downloads=int(hit.get("downloads") or 0),
                    icon_url=hit.get("icon_url") or "",
                    page_url=f"https://modrinth.com/{segment}/{hit.get('slug') or hit.get('project_id')}",
                )
            )
        return results

    def resolve_files(
        self,
        project_id: str,
        *,
        game_version: Optional[str] = None,
        loader: Optional[str] = None,
    ) -> list[ModFile]:
        import json as _json

        params = []
        if game_version:
            params.append(f"game_versions={quote(_json.dumps([game_version]))}")
        if loader:
            params.append(f"loaders={quote(_json.dumps([loader.lower()]))}")
        url = f"{MODRINTH_API}/project/{project_id}/version"
        if params:
            url += "?" + "&".join(params)

        status, payload = net.get_json_auth(url, headers=self._headers(), timeout=self.timeout)
        if status != 200 or not isinstance(payload, list):
            raise ModProviderError(_err(payload, "Modrinth 文件解析失败"))

        files: list[ModFile] = []
        for version in payload:
            for entry in version.get("files", []):
                hashes = entry.get("hashes", {})
                files.append(
                    ModFile(
                        source=self.name,
                        project_id=project_id,
                        file_id=str(version.get("id") or hashes.get("sha1") or ""),
                        filename=entry.get("filename", ""),
                        url=entry.get("url", ""),
                        size=int(entry.get("size") or 0),
                        sha1=hashes.get("sha1"),
                        version=version.get("version_number", ""),
                        game_versions=version.get("game_versions", []),
                        dependencies=version.get("dependencies", []),
                    )
                )
        return files


class CurseForgeProvider(ModProvider):
    """CurseForge 检索与文件解析，统一走 mcimirror 镜像，无需 API Key。"""

    name = "curseforge"
    display_name = "CurseForge"

    def __init__(self, *, timeout: int = 30) -> None:
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"Accept": "application/json"}

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        offset: int = 0,
        game_version: Optional[str] = None,
        loader: Optional[str] = None,
        project_type: str = "mod",
    ) -> list[ModSearchResult]:
        kind = (project_type or "mod").lower()
        class_id = CURSEFORGE_CLASS_IDS.get(kind)
        if class_id is None:
            raise ModProviderError("CurseForge 仅支持 Mod 与整合包检索，请改用 Modrinth")
        params = [
            f"gameId={CURSEFORGE_GAME_ID}",
            f"classId={class_id}",
            f"searchFilter={quote(query)}",
            f"index={offset}",
            f"pageSize={min(limit, 50)}",
        ]
        if game_version:
            params.append(f"gameVersion={quote(game_version)}")
        if loader and loader.lower() in LOADER_TO_CF:
            params.append(f"modLoaderType={LOADER_TO_CF[loader.lower()]}")

        status, payload = net.get_json_auth(
            f"{CURSEFORGE_API}/mods/search?" + "&".join(params),
            headers=self._headers(),
            timeout=self.timeout,
        )
        if status != 200:
            raise ModProviderError(_err(payload, "CurseForge 检索失败"))

        results = []
        for item in payload.get("data", []):
            authors = item.get("authors") or []
            results.append(
                ModSearchResult(
                    source=self.name,
                    project_id=str(item.get("id", "")),
                    name=item.get("name", ""),
                    author=", ".join(a.get("name", "") for a in authors),
                    description=item.get("summary", ""),
                    downloads=int(item.get("downloadCount") or 0),
                    icon_url=(item.get("logo") or {}).get("thumbnailUrl") or "",
                    page_url=(item.get("links") or {}).get("websiteUrl") or "",
                )
            )
        return results

    def resolve_files(
        self,
        project_id: str,
        *,
        game_version: Optional[str] = None,
        loader: Optional[str] = None,
    ) -> list[ModFile]:
        params = []
        if game_version:
            params.append(f"gameVersion={quote(game_version)}")
        if loader and loader.lower() in LOADER_TO_CF:
            params.append(f"modLoaderType={LOADER_TO_CF[loader.lower()]}")
        query = ("?" + "&".join(params)) if params else ""

        status, payload = net.get_json_auth(
            f"{CURSEFORGE_API}/mods/{project_id}/files{query}",
            headers=self._headers(),
            timeout=self.timeout,
        )
        if status != 200:
            raise ModProviderError(_err(payload, "CurseForge 文件解析失败"))

        files: list[ModFile] = []
        for item in payload.get("data", []):
            mod_file = self._file_from_item(project_id, item)
            if mod_file is not None:
                files.append(mod_file)
        return files

    def fetch_file(self, project_id: str, file_id) -> Optional[ModFile]:
        """按文件 ID 直接查询文件信息（整合包文件可能不在默认列表的前若干页）。"""
        status, payload = net.get_json_auth(
            f"{CURSEFORGE_API}/mods/{project_id}/files/{file_id}",
            headers=self._headers(),
            timeout=self.timeout,
        )
        if status != 200:
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        return self._file_from_item(project_id, data)

    def _file_from_item(self, project_id: str, item: dict) -> Optional[ModFile]:
        url = item.get("downloadUrl") or self._fetch_download_url(project_id, item.get("id"))
        if not url:
            return None
        hashes = {h.get("algo"): h.get("value") for h in item.get("hashes", [])}
        return ModFile(
            source=self.name,
            project_id=project_id,
            file_id=str(item.get("id", "")),
            filename=item.get("fileName", ""),
            url=mirror_cdn_url(str(url)),
            size=int(item.get("fileLength") or 0),
            sha1=hashes.get(1) or hashes.get("sha1"),  # algo=1 表示 SHA1
            version=item.get("displayName", ""),
            game_versions=item.get("gameVersions", []),
            dependencies=item.get("dependencies", []),
        )

    def _fetch_download_url(self, project_id: str, file_id) -> Optional[str]:
        """作者关闭 API 分发时 downloadUrl 为 null，需单独换取。"""
        if file_id is None:
            return None
        status, payload = net.get_json_auth(
            f"{CURSEFORGE_API}/mods/{project_id}/files/{file_id}/download-url",
            headers=self._headers(),
            timeout=self.timeout,
        )
        if status == 200:
            data = payload.get("data")
            return mirror_cdn_url(str(data)) if data else None
        return None


def mirror_cdn_url(url: str) -> str:
    """把 CurseForge 的 edge CDN 地址换成 mcimirror；其它地址保持不变。"""
    parts = urlsplit(url)
    if parts.netloc.lower() != CURSEFORGE_CDN_HOST:
        return url
    return urlunsplit(("https", "mod.mcimirror.top", parts.path, parts.query, parts.fragment))


def _err(payload: dict, prefix: str) -> str:
    detail = payload.get("error") or payload.get("errorMessage") or payload.get("description")
    return f"{prefix}：{detail}" if detail else prefix


def build_provider(source: str, *, timeout: int = 30) -> ModProvider:
    """按名称构造 Mod 源实例。"""
    source = (source or "modrinth").lower()
    if source == "modrinth":
        return ModrinthProvider(timeout=timeout)
    if source == "curseforge":
        return CurseForgeProvider(timeout=timeout)
    raise ModProviderError(f"未知的 Mod 源: {source}")