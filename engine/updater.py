"""启动器自更新：查询 GitHub Release，下载走公共 GH 代理，退出后替换 exe。

下载地址优先使用 ``https://v6.gh-proxy.org/https://github.com/...``，
代理不可用时自动回退 GitHub 原始地址。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from . import net

REPOSITORY = "239LAN/AMinecraftLauncher"
RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
GH_PROXY = "https://v6.gh-proxy.org/"


class UpdateError(Exception):
    """检查或下载更新失败。"""


def parse_version(text: str) -> tuple[int, ...]:
    """把 ``v1.4.0`` / ``1.4`` 解析成可比较的元组。"""
    numbers = re.findall(r"\d+", str(text or ""))
    return tuple(int(item) for item in numbers) or (0,)


def is_newer(latest: str, current: str) -> bool:
    """比较版本号；不足位补 0，避免 1.4 与 1.4.0 被判成不同。"""
    left = parse_version(latest)
    right = parse_version(current)
    width = max(len(left), len(right))
    left = left + (0,) * (width - len(left))
    right = right + (0,) * (width - len(right))
    return left > right


def fetch_latest(*, timeout: int = 15) -> dict:
    """读取最新 Release，返回版本与 exe 资产信息。"""
    status, payload = net.get_json_auth(
        RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"{REPOSITORY}/updater",
        },
        timeout=timeout,
    )
    if status != 200 or not isinstance(payload, dict):
        detail = payload.get("message") if isinstance(payload, dict) else payload
        raise UpdateError(f"获取最新版本失败：{detail or status}")

    tag = str(payload.get("tag_name") or "").strip()
    version = tag.lstrip("vV")
    if not version:
        raise UpdateError("最新版本缺少 tag")

    asset = _pick_asset(payload.get("assets") or [])
    if asset is None:
        raise UpdateError("最新版本没有可用的 exe 资产")

    return {
        "version": version,
        "tag": tag,
        "notes": str(payload.get("body") or ""),
        "page_url": str(payload.get("html_url") or ""),
        "asset_name": str(asset.get("name") or ""),
        "url": str(asset.get("browser_download_url") or ""),
        "size": int(asset.get("size") or 0),
        "asset_id": int(asset.get("id") or 0),
        "asset_updated_at": str(asset.get("updated_at") or ""),
        "published_at": str(payload.get("published_at") or ""),
    }


def asset_identity(info: dict) -> str:
    """更新标识：同一版本 Release 的附件被替换时也要算作一次新更新。"""
    return "|".join(
        [
            str(info.get("version") or ""),
            str(info.get("asset_name") or ""),
            str(info.get("size") or 0),
            str(info.get("asset_updated_at") or ""),
            str(info.get("asset_id") or 0),
        ]
    )


def fetch_releases(limit: int = 8, *, timeout: int = 10) -> list[dict]:
    """读取最近的 Release 列表，供主页更新日志展示。"""
    count = max(1, min(int(limit), 20))
    status, payload = net.get_json_auth(
        f"https://api.github.com/repos/{REPOSITORY}/releases?per_page={count}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"{REPOSITORY}/changelog",
        },
        timeout=timeout,
    )
    if status != 200 or not isinstance(payload, list):
        detail = payload.get("message") if isinstance(payload, dict) else payload
        raise UpdateError(f"获取更新日志失败：{detail or status}")

    entries = []
    for item in payload:
        if not isinstance(item, dict) or item.get("draft"):
            continue
        version = str(item.get("tag_name") or "").strip().lstrip("vV")
        if not version:
            continue
        name = str(item.get("name") or "").strip()
        entries.append(
            {
                "version": version,
                "title": "" if name.lower() in (f"v{version}".lower(), version.lower()) else name,
                "notes": _strip_title(str(item.get("body") or "")),
                "url": str(item.get("html_url") or ""),
                "published_at": str(item.get("published_at") or ""),
            }
        )
    return entries


def _strip_title(body: str) -> str:
    """去掉描述开头重复的标题行，如 ``## A Minecraft Launcher v1.4.2``。"""
    text = body.strip()
    lines = text.splitlines()
    if lines and lines[0].startswith("#") and "launcher" in lines[0].lower():
        return "\n".join(lines[1:]).strip()
    return text


def _pick_asset(assets: list) -> Optional[dict]:
    executables = [
        item
        for item in assets
        if isinstance(item, dict)
        and str(item.get("name") or "").lower().endswith(".exe")
        and item.get("browser_download_url")
    ]
    if not executables:
        return None
    preferred = [
        item for item in executables if "aminecraftlauncher" in str(item.get("name") or "").lower()
    ]
    return (preferred or executables)[0]


def download_urls(url: str) -> list[str]:
    """代理优先，GitHub 原始地址兜底。"""
    if not url:
        return []
    proxied = url if url.startswith(GH_PROXY) else f"{GH_PROXY}{url}"
    return [proxied, url] if proxied != url else [url]


def download(
    info: dict,
    dest: Path,
    *,
    on_chunk: Optional[Callable[[int], None]] = None,
    cancel: Optional[Callable[[], bool]] = None,
    timeout: int = 60,
) -> Path:
    """下载更新 exe；返回落盘后的路径。"""
    url = str(info.get("url") or "")
    if not url:
        raise UpdateError("缺少更新下载地址")
    dest = Path(dest)
    net.download_file(
        url,
        dest,
        expected_size=int(info.get("size") or 0),
        on_chunk=on_chunk,
        timeout=timeout,
        retries=3,
        candidates=download_urls(url),
        cancel=cancel,
    )
    return dest


def apply_update(new_exe: Path) -> Path:
    """生成替换脚本并在后台启动，退出当前进程后覆盖原 exe 并重启。

    返回脚本路径，调用方随后应关闭启动器。
    """
    if not getattr(sys, "frozen", False):
        raise UpdateError("自动更新仅在打包版可用")
    exe = Path(sys.executable)
    script = exe.parent / "aml-update.ps1"
    name = exe.stem.replace("'", "''")
    new_path = Path(new_exe).resolve()
    lines = [
        "$ErrorActionPreference = 'SilentlyContinue'",
        "Start-Sleep -Seconds 1",
        f"while (Get-Process -Name '{name}' -ErrorAction SilentlyContinue) {{ Start-Sleep -Milliseconds 500 }}",
        "for ($i = 0; $i -lt 30; $i++) {",
        f"  try {{ Move-Item -LiteralPath '{_ps_literal(new_path)}' -Destination '{_ps_literal(exe)}' -Force -ErrorAction Stop; break }}",
        "  catch { Start-Sleep -Seconds 1 }",
        "}",
        # 无论替换是否成功都启动原路径：失败时回退到旧版本，避免启动器消失。
        f"Start-Process -FilePath '{_ps_literal(exe)}'",
        "Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force",
    ]
    script.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8-sig")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-WindowStyle",
            "Hidden",
            "-File",
            str(script),
        ],
        creationflags=flags,
        close_fds=True,
    )
    return script


def _ps_literal(path: Path) -> str:
    """PowerShell 单引号字符串：单引号需要写成两个。"""
    return str(path).replace("'", "''")


def update_target() -> Path:
    """更新文件落盘位置：打包版放在 exe 同级，源码模式放在 data/updates。"""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        return exe.with_name(f"{exe.stem}-update.exe")
    return Path(os.getcwd()) / "data" / "updates" / "A Minecraft Launcher-update.exe"
