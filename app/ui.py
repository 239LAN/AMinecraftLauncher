"""NiceGUI 界面：启动 / 版本 / 下载 / 设置。

页面职责：
- 启动：只放登录（离线）与启动游戏；
- 版本：左侧本地版本列表，右侧该版本的 Mod 管理与启动设置；
- 下载：游戏本体 / Mod / 整合包 / 资源包 / 光影包，按所选版本自动筛选；
- 设置：启动器设置与游戏设置两类。
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import webbrowser
from functools import wraps
from pathlib import Path
from typing import Callable, Optional

from nicegui import app, background_tasks, run, ui

from engine.version import APP_NAME, APP_VERSION

from .services import LauncherService

# 启动器更新方式：固定三档，界面与后台检查共用。
UPDATE_MODES = {
    "auto": "帮我自动更新",
    "notify": "提醒我有更新",
    "off": "不检查更新",
}
UPDATE_MODE_HINTS = {
    "auto": "启动时自动下载并安装更新",
    "notify": "启动时检查更新，同一版本只提醒一次",
    "off": "已关闭启动器更新检查",
}


def _relay_ui_task(func):
    """让由 background_tasks 触发的联机异步方法仍能在页面插槽中创建 UI。"""
    @wraps(func)
    async def wrapper(self, *args, **kwargs):
        key = "relay-create" if self.current_view == "relay-create" else "multiplayer"
        slot = self.pages.get(key) or self.pages.get("multiplayer")
        if slot is None:
            return await func(self, *args, **kwargs)
        with slot:
            return await func(self, *args, **kwargs)

    return wrapper


NAV_ITEMS = [
    ("home", "home", "启动"),
    ("versions", "inventory_2", "版本"),
    ("download", "cloud_download", "下载"),
    ("multiplayer", "groups", "联机"),
    ("settings", "settings", "设置"),
]

# 更新日志：主页从 GitHub Releases 读取；下面只保留最新一版作为离线兜底。
CHANGELOG_LIMIT = 8
CHANGELOG_FOOTER = "> 最低系统要求： Windows 10(22H2)及以上。"

CHANGELOG_FALLBACK = [
    {
        "version": "1.5.0",
        "title": "联机",
        "notes": """\
### 新增
- 新增联机页：无需正版账号即可创建房间，好友输入房间码即可加入
- 联机页改为搜索框 + 公开房间卡片列表，右上角显示「我的房间」
- 房间号改为 4 位不重复数字，隐藏房间不出现在列表，仅能凭房间号搜索
- 同一网络同时只能开一个房间，接口只接受启动器签名请求
- 创建房间进入独立页面：选择服务商、扫描并选择游戏端口、2-16 人滑块、隐藏房间开关
- 支持读取或上传模组清单，并自动查询 Modrinth 链接
- 加入前比对客户端模组；一致则自动启动并进入服务器，不一致可下载缺失 Mod 或忽略风险加入
- 基于 frp STCP 隧道，访客连接本机回环地址，公网只暴露 API 与 frps 端口
- 仅允许真正 Minecraft 端口进入隧道，并限制房间号查询频率
- 支持多节点部署：主节点下发可用节点，启动器自动选择
- 自动读取游戏「对局域网开放」的端口，无需手动填写
- frpc 随安装包内置，无需单独安装或下载

### 调整
- frps 强制 TLS、限制每客户端代理数量与远程端口段，降低隧道滥用风险
- 公开接口不再返回 frps 地址
- 最大房间人数上限调整为 16
- 客户端隧道组件与服务端版本对齐

### 修复
- 无
""",
    },
]

DOWNLOAD_TYPES = {
    "game": "游戏",
    "mod": "Mod",
    "modpack": "整合包",
    "resourcepack": "资源包",
    "shader": "光影包",
}

CONTENT_LABELS = {
    "mod": "Mod",
    "resourcepack": "资源包",
    "shader": "光影包",
    "modpack": "整合包",
}

# 引擎阶段名 -> 界面中文文案（任务详情 / 进度展示用）。
PHASE_LABELS = {
    "assets": "解析资源文件",
    "profile": "获取版本配置",
    "java": "准备 Java",
    "download": "下载安装器",
    "install": "运行安装器",
    "mods": "下载整合包文件",
    "storage": "迁移文件库",
    "hashsync": "同步哈希库",
    "wait": "等待其它整合包任务",
    "waitfiles": "等待其它文件安装任务",
}

SEVERITY_LABELS = {"high": "严重", "medium": "中等", "low": "提示"}

LOADERS = ["fabric", "forge", "neoforge", "quilt"]
LOADER_LABELS = {
    "vanilla": "原版",
    "fabric": "Fabric",
    "forge": "Forge",
    "neoforge": "NeoForge",
    "quilt": "Quilt",
    "optifine": "OptiFine",
}

AML_CSS = """
:root {
  --aml-bg: #f6f7f9;
  --aml-surface: #ffffff;
  --aml-border: #e5e7eb;
  --aml-surface-2: #f9fafb;
  --aml-text: #111827;
  --aml-muted: #6b7280;
  --aml-accent: #2563eb;
  --aml-accent-soft: #eff6ff;
  --aml-radius: 14px;
}
html, body {
  height: 100%; margin: 0; overflow: hidden;
  background: var(--aml-bg);
}
body {
  color: var(--aml-text);
  font-family: "Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", system-ui, sans-serif;
  -webkit-font-smoothing: antialiased;
}
/* 固定 1100x720 设计稿，按窗口分辨率等比缩放 */
#aml-stage {
  position: fixed; left: 0; top: 0;
  width: 1100px; height: 720px; overflow: hidden;
  background: var(--aml-bg);
  transform-origin: top left;
}
.aml-app { position: relative; }

/* 按钮：轻微按压反馈 */
.q-btn {
  text-transform: none !important; letter-spacing: 0;
  border-radius: 10px; font-weight: 500;
  transition: background-color .16s ease, color .16s ease,
              border-color .16s ease, transform .16s ease, box-shadow .16s ease;
}
.q-btn:active { transform: scale(.98); }
.q-btn--round { border-radius: 50% !important; }
/* 紧凑按钮也要有正常的水平内边距，避免中文被挤成方块 */
.q-btn--dense:not(.q-btn--round) { padding: 0 12px !important; min-width: 56px; }
.q-btn__content { white-space: nowrap; }

/* 顶部导航 */
.aml-topbar {
  background: var(--aml-surface);
  border-bottom: 1px solid var(--aml-border);
}
.aml-topnav {
  display: flex; align-items: center; gap: 6px; padding: 6px 12px;
  border-radius: 999px; cursor: pointer; color: #4b5563;
  transition: background-color .16s ease, color .16s ease;
  user-select: none;
}
.aml-topnav.q-btn { min-height: 34px; padding: 4px 14px; border-radius: 999px; }
.aml-topnav .q-icon { color: #4b5563 !important; }
.aml-topnav:hover { background: #f3f4f6; color: var(--aml-text); }
.aml-topnav:hover .q-icon { color: var(--aml-text) !important; }
.aml-topnav.active { background: var(--aml-accent-soft); color: var(--aml-accent); font-weight: 600; }
.aml-topnav.active .q-icon { color: var(--aml-accent) !important; }
.aml-close-btn { color: #e5484d !important; }
.aml-close-btn .q-icon { color: #e5484d !important; }

/* 页面切换 */
@keyframes aml-page-in {
  from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: none; }
}
.aml-page-in { animation: aml-page-in .22s ease both; }

.aml-card {
  background: var(--aml-surface);
  border: 1px solid var(--aml-border);
  border-radius: var(--aml-radius);
  transition: border-color .16s ease, box-shadow .16s ease;
}
.aml-card:hover {
  border-color: #d1d5db;
  box-shadow: 0 4px 12px rgba(17, 24, 39, 0.06);
}
.aml-card.flat:hover { border-color: var(--aml-border); box-shadow: none; }

/* 更新日志：浅色卡片，收起时只显示版本号 */
.aml-log-card {
  background: #f9fafb;
  border: 1px solid var(--aml-border);
  box-shadow: 0 2px 10px rgba(17, 24, 39, .05);
}
.aml-logitem { border-bottom: 1px solid var(--aml-border); }
.aml-logitem:last-child { border-bottom: none; }
.aml-logitem .q-item { padding: 0 4px; min-height: 30px; }
.aml-logitem .q-item__label { font-size: 12.5px; font-weight: 600; }
.aml-logitem .q-expansion-item__content { padding: 0; }
.aml-logitem .q-item__section--avatar { min-width: 22px; padding-right: 2px; }
.aml-logitem .q-item__section--avatar .q-icon { font-size: 18px; }
.aml-log-title { font-size: 12px; font-weight: 600; color: var(--aml-muted); padding: 0 4px 2px 26px; }
.aml-log-body { color: #374151; font-size: 13px; line-height: 1.7; padding: 0 4px 10px 26px; }
.aml-log-body p { margin: 4px 0; }
.aml-log-body ul, .aml-log-body ol { margin: 4px 0; padding-left: 18px; }
.aml-log-body li { margin: 3px 0; }
.aml-log-body code { background: #eef0f3; color: #374151; padding: 1px 6px; border-radius: 6px; font-size: 12px; }
.aml-log-body h3 { font-size: 12.5px; font-weight: 600; color: var(--aml-text); margin: 10px 0 3px; }
.aml-log-body h3:first-child { margin-top: 0; }
.aml-log-body blockquote { margin: 10px 0 0; padding: 7px 10px; border-left: 3px solid var(--aml-accent); background: var(--aml-surface-2); color: var(--aml-muted); font-size: 12px; border-radius: 6px; }
.aml-log-body blockquote p { margin: 0; }

.aml-hero-login {
  background: var(--aml-surface);
  border: 1px solid var(--aml-border);
  border-radius: 16px;
  box-shadow: 0 1px 2px rgba(17, 24, 39, 0.04);
}
.aml-login-toggle {
  background: var(--aml-surface-2);
  border: 1px solid var(--aml-border);
  border-radius: 999px;
  padding: 3px;
}
.aml-login-toggle .q-btn { border-radius: 999px !important; }

.aml-launch {
  background: var(--aml-accent) !important;
  color: #fff !important;
  font-weight: 600;
  border-radius: 12px;
  box-shadow: none;
  transition: background-color .16s ease, transform .16s ease;
}
.aml-launch:hover { background: #1d4ed8 !important; }
.aml-launch:active { transform: scale(.98); }
.aml-launch-cancel {
  background: transparent !important;
  color: var(--aml-accent) !important;
  font-weight: 600;
  border: 1.5px solid var(--aml-accent) !important;
  border-radius: 12px;
}
.aml-launch-cancel:hover { background: var(--aml-accent-soft) !important; }
.aml-launch-cancel:active { transform: scale(.98); }

/* 启动中的播放图标：蓝底实心 <-> 蓝描边空心 缓慢切换 */
.aml-play-pulse {
  width: 84px; height: 84px; border-radius: 14px;
  display: flex; align-items: center; justify-content: center;
  border: 2px solid var(--aml-accent);
  animation: aml-play-pulse 2.6s ease-in-out infinite;
}
.aml-play-pulse .q-icon { font-size: 42px !important; }
@keyframes aml-play-pulse {
  0%, 100% { background: var(--aml-accent); color: #fff; }
  50% { background: transparent; color: var(--aml-accent); }
}

/* 导入：虚线拖放区 + 原位进度 */
.aml-dropzone {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 4px; padding: 20px 12px;
  border: 1.5px dashed #d1d5db; border-radius: 12px;
  background: #fbfcfd; color: var(--aml-muted); cursor: pointer;
  transition: border-color .16s ease, background-color .16s ease, color .16s ease;
}
.aml-dropzone:hover, .aml-dropzone.aml-drop-active {
  border-color: var(--aml-accent); background: var(--aml-accent-soft); color: var(--aml-accent);
}
.aml-dropzone .q-icon { font-size: 24px !important; }
.aml-bar-indeterminate {
  width: 35% !important;
  animation: aml-indeterminate 1.1s ease-in-out infinite;
}
@keyframes aml-indeterminate {
  from { margin-left: -35%; }
  to { margin-left: 100%; }
}

/* 版本列表 */
.aml-vitem {
  border: 1px solid transparent; border-radius: 10px;
  transition: background-color .15s ease, border-color .15s ease;
}
.aml-vitem:hover { background: #f3f4f6; }
.aml-vitem.active { background: var(--aml-accent-soft); border-color: #dbeafe; }

.aml-sidebar {
  background: var(--aml-surface);
  border-right: 1px solid var(--aml-border);
}
.aml-version-badge {
  width: 36px; height: 36px; border-radius: 10px;
  display: flex; align-items: center; justify-content: center;
  background: var(--aml-accent); color: #fff;
}
.aml-detail-head {
  background: var(--aml-surface);
  border: 1px solid var(--aml-border);
  border-radius: var(--aml-radius);
}

/* 进度条 */
.aml-bar-track {
  position: relative; height: 8px;
  background: #e5e7eb; border-radius: 999px; overflow: hidden;
}
.aml-bar-fill {
  height: 100%; width: 0%; border-radius: 999px;
  background: var(--aml-accent);
  transition: width .3s ease;
}

.aml-fade { animation: aml-fade .2s ease backwards; }
@keyframes aml-fade {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: none; }
}

.aml-chip {
  font-size: 11px; font-weight: 500; padding: 2px 10px; border-radius: 999px;
  background: #f3f4f6; color: #4b5563;
  border: 1px solid var(--aml-border); white-space: nowrap;
}
.aml-chip.installed { background: #ecfdf5; color: #047857; border-color: #d1fae5; }
.aml-chip.primary { background: var(--aml-accent-soft); color: var(--aml-accent); border-color: #dbeafe; }
.aml-chip.warn { background: #fffbeb; color: #b45309; border-color: #fde68a; }

.aml-title { font-size: 18px; font-weight: 600; letter-spacing: -0.2px; }
.aml-sub { font-size: 12px; color: var(--aml-muted); }
.aml-link { color: var(--aml-accent); cursor: pointer; text-decoration: none; }
.aml-link:hover { text-decoration: underline; }
.aml-big-num { font-size: 24px; font-weight: 600; color: var(--aml-accent); }
.aml-live-dot { width: 6px; height: 6px; border-radius: 50%; background: #22c55e; }
.aml-task-row {
  background: var(--aml-surface);
  border: 1px solid var(--aml-border);
  border-radius: var(--aml-radius);
  transition: border-color .15s ease;
}
.aml-task-row:hover { border-color: #d1d5db; }
.aml-empty {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 6px; padding: 28px 12px; color: var(--aml-muted);
}
.aml-empty .q-icon { font-size: 32px; opacity: .35; }

.aml-scroll { overflow-y: auto; scrollbar-width: thin; scrollbar-color: #d1d5db transparent; }
.aml-scroll::-webkit-scrollbar { width: 8px; }
.aml-scroll::-webkit-scrollbar-thumb { background: #d1d5db; border-radius: 999px; }
.aml-scroll::-webkit-scrollbar-thumb:hover { background: #b6bcc6; }

/* 输入框 */
.q-field--outlined .q-field__control {
  border-radius: 10px; background: #fff;
  transition: box-shadow .16s ease;
}
.q-field--outlined .q-field__control:before { border-color: #d1d5db; }
.q-field--outlined.q-field--focused .q-field__control:after { border-color: var(--aml-accent); }
.q-field--outlined.q-field--focused .q-field__control { box-shadow: none; }
.switch__track, .q-toggle__thumb { transition: background-color .16s ease; }

/* 分段切换 / TAB：胶囊 */
.q-btn-group { background: #f3f4f6; border-radius: 999px; padding: 3px; gap: 2px; }
.q-btn-group .q-btn { border-radius: 999px !important; }
.q-btn-group .q-btn.bg-primary { background: var(--aml-accent) !important; box-shadow: none; }

/* 弹窗、通知与菜单 */
.q-dialog__inner > .q-card {
  border-radius: 18px !important;
  background: var(--aml-surface) !important;
  border: 1px solid var(--aml-border);
  box-shadow: 0 20px 50px -20px rgba(17, 24, 39, 0.35) !important;
  animation: aml-dialog-in .18s ease both;
}
@keyframes aml-dialog-in {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: none; }
}
.q-notification {
  margin-top: 64px;
  border-radius: 12px !important;
  box-shadow: 0 10px 30px -12px rgba(17, 24, 39, 0.3) !important;
}
.q-menu {
  border-radius: 12px !important;
  background: #fff !important;
  border: 1px solid var(--aml-border);
  box-shadow: 0 12px 32px -12px rgba(17, 24, 39, 0.25) !important;
}

.aml-log {
  background: #111827; color: #d1d5db;
  font-family: "Cascadia Code", Consolas, Menlo, monospace;
  font-size: 12px; border-radius: 10px;
}
.aml-cause { border-left: 3px solid var(--aml-accent); padding-left: 10px; }
.aml-cause.high { border-color: #dc2626; }
.aml-cause.medium { border-color: #f59e0b; }

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: .001ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: .001ms !important;
  }
}
"""

# 内容按窗口分辨率等比缩放（保持 1100x720 比例，多余区域留白居中）。
FIT_SCRIPT = """
<script>
(function () {
  function fit() {
    var stage = document.getElementById('aml-stage');
    if (!stage) { window.requestAnimationFrame(fit); return; }
    var scale = Math.min(window.innerWidth / 1100, window.innerHeight / 720);
    stage.style.transform = 'scale(' + scale + ')';
    stage.style.left = ((window.innerWidth - 1100 * scale) / 2) + 'px';
    stage.style.top = ((window.innerHeight - 720 * scale) / 2) + 'px';
  }
  window.addEventListener('resize', fit);
  window.requestAnimationFrame(fit);

  // 把拖入的文件转交给隐藏的上传控件，由 NiceGUI 统一接收。
  function bindZones() {
    document.querySelectorAll('[data-aml-drop]').forEach(function (zone) {
      if (zone.dataset.amlBound) return;
      var root = document.getElementById(zone.dataset.amlDrop);
      if (!root) return;
      var input = root.querySelector('input[type=file]');
      if (!input) return;
      zone.dataset.amlBound = '1';
      function active() { zone.classList.add('aml-drop-active'); }
      function idle() { zone.classList.remove('aml-drop-active'); }
      zone.addEventListener('dragover', function (e) {
        e.preventDefault(); e.stopPropagation(); active();
      });
      zone.addEventListener('dragleave', function (e) {
        e.preventDefault(); idle();
      });
      zone.addEventListener('drop', function (e) {
        e.preventDefault(); e.stopPropagation(); idle();
        var files = e.dataTransfer && e.dataTransfer.files;
        if (!files || !files.length) return;
        var dt = new DataTransfer();
        for (var i = 0; i < files.length; i++) dt.items.add(files[i]);
        input.files = dt.files;
        input.dispatchEvent(new Event('change', { bubbles: true }));
      });
      if (zone.dataset.amlClick) {
        zone.addEventListener('click', function () { input.click(); });
      }
    });
  }
  setInterval(bindZones, 600);

  // 后端不可达（连续 3 次心跳失败）时自动关闭窗口，避免留下一个死界面。
  var misses = 0;
  setInterval(async function () {
    try {
      var res = await fetch(location.origin + '/api/supports', { cache: 'no-store' });
      misses = res && res.ok ? 0 : misses + 1;
    } catch (err) {
      misses += 1;
    }
    if (misses < 3) return;
    misses = 0;
    try {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.close) {
        window.pywebview.api.close();
        return;
      }
    } catch (err) {}
    try { window.close(); } catch (err) {}
  }, 4000);
})();
</script>
"""


def _fmt_count(value: int) -> str:
    if value >= 100_000_000:
        return f"{value / 100_000_000:.1f}亿"
    if value >= 10_000:
        return f"{value / 10_000:.1f}万"
    return str(value)


def _fmt_size(value: int) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def _fmt_speed(value: float) -> str:
    speed = max(0.0, float(value or 0))
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if speed < 1024 or unit == "GB/s":
            return f"{speed:.0f} {unit}" if unit == "B/s" else f"{speed:.1f} {unit}"
        speed /= 1024
    return f"{speed:.1f} GB/s"


def _sanitize_version_name(text: str) -> str:
    """去掉 Windows 目录名不允许的字符。"""
    bad = '\\/:*?"<>|'
    cleaned = "".join("-" if ch in bad else ch for ch in str(text or ""))
    return cleaned.strip().strip(".")


class ImportDrop:
    """文件导入卡片：空闲时显示虚线拖放区，导入时原位显示进度。"""

    def __init__(
        self,
        *,
        upload_id: str,
        hint: str,
        handler: Callable,
        multiple: bool = True,
    ) -> None:
        self.upload_id = upload_id
        self.hint = hint
        self.handler = handler
        self.multiple = multiple

    def build(self) -> "ImportDrop":
        self.zone = ui.element("div").classes("aml-dropzone w-full").props(
            f"data-aml-drop={self.upload_id} data-aml-click=1"
        )
        with self.zone:
            ui.icon("upload_file")
            ui.label(self.hint).classes("aml-sub")
        self.progress = ui.column().classes("w-full gap-1")
        self.progress.set_visibility(False)
        with self.progress:
            self.label = ui.label("").classes("aml-sub")
            with ui.element("div").classes("aml-bar-track w-full"):
                self.fill = ui.element("div").classes("aml-bar-fill")
        # 真正的文件选择器保持隐藏，由拖放区点击 / 拖入触发。
        ui.upload(
            on_multi_upload=self.handler,
            auto_upload=True,
            multiple=self.multiple,
        ).props(f"id={self.upload_id}").classes("hidden")
        return self

    def busy(self, text: str, percent: Optional[float] = None) -> None:
        self.zone.set_visibility(False)
        self.progress.set_visibility(True)
        self.label.text = text
        if percent is None:
            self.fill.classes(add="aml-bar-indeterminate")
        else:
            self.fill.classes(remove="aml-bar-indeterminate")
            value = max(0.0, min(100.0, float(percent)))
            self.fill.style(f"width: {value:.1f}%")

    def idle(self) -> None:
        self.progress.set_visibility(False)
        self.zone.set_visibility(True)
        self.fill.classes(remove="aml-bar-indeterminate")
        self.fill.style("width: 0%")


def app_icon_file() -> Optional[Path]:
    """返回打包 / 源码目录下的 AML 图标文件。"""
    from .config import runtime_dir

    base = Path(getattr(sys, "_MEIPASS", runtime_dir()))
    icon = base / "assets" / "aml.ico"
    return icon if icon.exists() else None


def _apply_native_window_icon(form) -> None:
    """按当前 DPI 分别加载大 / 小图标，避免任务栏把 32px 图标拉伸而发糊。"""
    if sys.platform != "win32":
        return
    icon = app_icon_file()
    if icon is None:
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.LoadImageW.argtypes = [
        wintypes.HINSTANCE,
        wintypes.LPCWSTR,
        wintypes.UINT,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.SendMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.SendMessageW.restype = wintypes.LPARAM

    try:
        hwnd = int(form.Handle.ToInt32())
    except Exception:  # noqa: BLE001 - 窗体销毁期间忽略
        return
    try:
        dpi = int(user32.GetDpiForWindow(hwnd)) or 96
    except Exception:  # noqa: BLE001 - 旧系统没有该接口
        dpi = 96

    def metric(index: int, fallback: int) -> int:
        value = 0
        try:
            value = int(user32.GetSystemMetricsForDpi(index, dpi))
        except Exception:  # noqa: BLE001
            value = 0
        if not value:
            try:
                value = int(user32.GetSystemMetrics(index))
            except Exception:  # noqa: BLE001
                value = fallback
        return max(16, value)

    # SM_CXICON / SM_CXSMICON：随 DPI 变化，图标文件里已有对应尺寸的原始帧。
    sizes = ((1, metric(11, 32)), (0, metric(49, 16)))  # ICON_BIG / ICON_SMALL
    for index, size in sizes:
        handle = user32.LoadImageW(
            None, str(icon), 1, size, size, 0x00000010  # IMAGE_ICON / LR_LOADFROMFILE
        )
        if handle:
            user32.SendMessageW(hwnd, 0x0080, index, handle)  # WM_SETICON


def native_maximize_guard() -> None:
    """无边框窗口最大化时会盖住任务栏，这里把最大化范围限制在屏幕工作区。

    NiceGUI 原生模式的窗口运行在独立子进程，主进程只拿到代理对象，
    因此必须通过 pywebview.start(func=...) 在这个进程里操作真实窗体。
    """
    try:
        import clr

        clr.AddReference("System.Windows.Forms")
        import webview
        from System import Func, Type
        from System.Windows.Forms import Screen
    except Exception:  # noqa: BLE001 - 非 Windows / 缺少 pythonnet 时直接跳过
        return

    windows = []
    form = None
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        windows = getattr(webview, "windows", None) or []
        if windows:
            form = getattr(windows[0], "native", None)
            if form is not None:
                break
        time.sleep(0.1)
    if form is None:
        return

    def _limit() -> None:
        form.MaximizedBounds = Screen.FromControl(form).WorkingArea

    def _refresh(*_args) -> None:
        try:
            _limit()
        except Exception:  # noqa: BLE001 - 窗体销毁期间忽略
            return

    try:
        form.Invoke(Func[Type](_limit))
        # 窗口换到另一块屏幕时，重新按那块屏幕的工作区限制最大化范围。
        windows[0].events.moved += _refresh
        windows[0].events.restored += _refresh
    except Exception:  # noqa: BLE001 - 安装失败时退回系统默认行为
        return

    # NiceGUI 默认把小图标和大图标都设成同一个 32px 句柄，任务栏会拉伸发糊；
    # 这里按 DPI 重设一次，并在窗口显示后（NiceGUI 设置图标之后）再补一次。
    _apply_native_window_icon(form)
    threading.Timer(1.0, lambda: _apply_native_window_icon(form)).start()


class LauncherUI:
    def __init__(self, service: LauncherService, *, debug: bool = False) -> None:
        self.service = service
        self.debug = debug
        self.log_element = None
        self.log_visible = False
        self.pages: dict[str, object] = {}
        self.nav_buttons: dict[str, object] = {}
        self.settings_bodies: dict[str, object] = {}
        self.detail_version_id: str = ""
        self.version_tab: str = "mods"
        self.version_tab_boxes: dict[str, object] = {}
        self.download_type: str = "game"
        self.dl_version_value: Optional[str] = None
        self._download_built = False
        self.current_view: str = "home"
        self._task_status: dict[str, str] = {}
        self._task_speed: dict[str, float] = {}
        self._task_widgets: dict[str, dict] = {}
        self._cancelling: set[str] = set()
        self._speed_sample: Optional[tuple[float, dict[str, int]]] = None
        self._is_maximized = False
        self._download_stale = False
        self._launch_timer = None
        self._home_launching = False
        self._home_launch_timer = None
        self._login_mode: str = ""
        self._launch_cancel = threading.Event()
        self._launch_progress: dict = {"percent": 0.0, "detail": "准备中…"}
        self._launch_done = False
        self._launch_error = ""
        self._log_revision = -1
        # 联机页状态（服务层是唯一事实来源，这里只缓存控件引用）。
        self.relay_body = None
        self.relay_my_room_button = None
        self.relay_search_input = None
        self.relay_heartbeat_timer = None
        self.relay_players_label = None
        self.relay_tunnel_label = None
        self.relay_tick_timer = None
        self._relay_rooms: Optional[list[dict]] = None
        self._relay_rooms_loading = False
        self._relay_rooms_error = ""
        # 创建房间独立页
        self.relay_create_body = None
        self._relay_create_ports: list[dict] = []
        self._relay_create_version = ""
        self._relay_create_mods: list[dict] = []
        self._relay_create_mods_found = 0
        self._relay_create_mods_missing = 0
        self.relay_create_name = None
        self.relay_create_provider = None
        self.relay_create_port = None
        self.relay_create_players = None
        self.relay_create_players_label = None
        self.relay_create_hidden = None
        self.relay_create_mods_label = None
        self.relay_create_primary = None

    # ===================================================================== 骨架
    async def build(self) -> None:
        ui.add_head_html(f"<style>{AML_CSS}</style>")
        ui.add_head_html(FIT_SCRIPT)
        ui.dark_mode().value = False
        ui.colors(
            primary="#2563eb",
            secondary="#4f46e5",
            accent="#0ea5e9",
            positive="#16a34a",
            negative="#dc2626",
            warning="#f59e0b",
            info="#0284c7",
        )

        with ui.element("div").props("id=aml-stage"):
            with ui.column().classes("aml-app w-full h-full no-wrap gap-0"):
                self._build_topbar()
                with ui.column().classes("w-full flex-1 min-h-0 gap-0"):
                    self._build_pages()
                    if self.debug:
                        self._build_log_drawer()

        self._build_dialog()
        self._build_launch_dialog()
        if self.debug:
            ui.timer(1.0, self._refresh_logs)
        ui.timer(0.5, self._poll_tasks)
        self._select_view("home")
        # 启动后静默检查一次更新；不阻塞启动，也避免频繁请求。
        ui.timer(2.0, self._silent_update_check, once=True)

    def _build_topbar(self) -> None:
        with ui.row().classes(
            "aml-topbar w-full h-[56px] shrink-0 items-center no-wrap px-4 gap-2"
        ):
            with ui.row().classes("items-center gap-2.5 no-wrap shrink-0"):
                with ui.column().classes("gap-0 no-wrap").style("line-height: 1.15"):
                    ui.label(APP_NAME).classes("text-[14px] font-semibold")
                    ui.label(f"v{APP_VERSION}").classes("text-[10px]").style(
                        "color: var(--aml-muted); letter-spacing: .08em"
                    )

            # 无边框窗口的拖动区域（原生模式下生效，浏览器里没有副作用）。
            ui.element("div").classes("pywebview-drag-region flex-1 h-full").style(
                "min-width: 24px; cursor: default"
            )

            for key, icon, text in NAV_ITEMS:
                button = ui.button(text, icon=icon, on_click=lambda _=None, k=key: self._select_view(k)).props(
                    "flat no-caps dense"
                ).classes("aml-topnav")
                self.nav_buttons[key] = button

            # 关闭按钮始终可用：原生模式退出桌面程序，Web 模式尝试关闭标签页。
            with ui.row().classes("items-center gap-0 no-wrap shrink-0 pl-2"):
                if self._native_window() is not None:
                    ui.button(icon="remove", on_click=self._win_minimize).props(
                        "flat dense round size=sm"
                    )
                    ui.button(icon="crop_square", on_click=self._win_toggle_maximize).props(
                        "flat dense round size=sm"
                    )
                ui.button(icon="close", on_click=self._win_close).props(
                    "flat dense round size=sm"
                ).classes("aml-close-btn")

    def _native_window(self):
        try:
            return app.native.main_window
        except Exception:  # noqa: BLE001 - 非原生模式或窗口尚未创建
            return None

    def _win_minimize(self) -> None:
        window = self._native_window()
        if window is not None:
            window.minimize()

    def _win_toggle_maximize(self) -> None:
        window = self._native_window()
        if window is None:
            return
        if self._is_maximized:
            window.restore()
            self._is_maximized = False
        else:
            window.maximize()
            self._is_maximized = True

    def _win_close(self) -> None:
        if self._native_window() is None:
            # Web 模式：关标签页不代表后端退出，不能在这里回收哈希存储。
            self._close_window()
            return
        if self.service.has_running_game():
            self._confirm_close_with_game()
            return
        if self.service.config.hash_store:
            background_tasks.create(self._close_after_sync())
            return
        self._close_window()

    def _close_window(self) -> None:
        window = self._native_window()
        if window is None:
            # Web 模式退化为尝试关闭浏览器标签（浏览器可能拦截非脚本打开的窗口）。
            ui.run_javascript("window.close()")
            return
        # 桌面模式：window.close() 在 WebView2 中通常无效，而且一旦它抛错，
        # 后面的逻辑就不会执行，窗口反而关不掉。这里用 NiceGUI 的 shutdown：
        # 它会销毁原生窗口并停止服务端，窗口子进程随父进程退出而结束。
        try:
            app.shutdown()
        except Exception as exc:  # noqa: BLE001 - shutdown 失败时兜底直接销毁窗口
            self.service.log(f"关闭窗口失败，尝试直接销毁：{exc}")
            try:
                window.destroy()
            except Exception as inner:  # noqa: BLE001
                self.service.log(f"销毁窗口失败：{inner}")

    def _confirm_close_with_game(self) -> None:
        info = self.service.running_game_info()

        def builder() -> None:
            ui.label(
                "为了避免存档和数据丢失，关闭启动器前需结束游戏以保存游戏存档等数据。"
            ).classes("text-[13px]")
            ui.label(
                f"正在运行：{info.get('version') or '游戏'}（PID {info.get('pid')}）"
            ).classes("aml-sub")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("取消", on_click=self.dialog.close).props("flat dense no-caps")
                ui.button(
                    "结束游戏并关闭", icon="power_settings_new",
                    on_click=lambda: background_tasks.create(self._close_with_game()),
                ).props("unelevated dense no-caps color=primary")

        self.open_dialog("结束游戏并退出", builder)

    async def _close_with_game(self) -> None:
        self._force_exit_after()
        self.dialog.close()
        ui.notify("正在结束游戏并同步存档…", position="top-right")
        await run.io_bound(self.service.shutdown_storage)
        self._close_window()

    async def _close_after_sync(self) -> None:
        self._force_exit_after()
        ui.notify("正在同步哈希文件库…", position="top-right")
        await run.io_bound(self.service.shutdown_storage)
        self._close_window()

    def _force_exit_after(self, seconds: float = 20.0) -> None:
        """关闭流程卡住时兜底强退，避免「点了关闭却关不掉」。"""
        def killer() -> None:
            time.sleep(seconds)
            os._exit(0)

        threading.Thread(target=killer, daemon=True).start()

    def _build_pages(self) -> None:
        with ui.element("div").classes("w-full flex-1 min-h-0"):
            self._build_home_page()
            self._build_versions_page()
            self._build_download_page()
            self._build_tasks_page()
            self._build_settings_page()
            self._build_multiplayer_page()
            self._build_relay_create_page()

    def _build_log_drawer(self) -> None:
        with ui.column().classes(
            "w-full shrink-0 border-t bg-white gap-0"
        ).style("border-color: var(--aml-border)"):
            with ui.row().classes("w-full items-center justify-between px-4 py-1 no-wrap"):
                with ui.row().classes("items-center gap-2 no-wrap"):
                    ui.icon("terminal").classes("text-[14px]").style(
                        "color: var(--aml-muted)"
                    )
                    ui.label("运行日志").classes("aml-sub")
                ui.button(icon="expand_less", on_click=self._toggle_log).props(
                    "flat dense round size=sm"
                )
            self.log_element = ui.log(max_lines=300).classes(
                "aml-log w-full m-0 p-3"
            ).style("height: 150px")
            self.log_element.set_visibility(False)
            self.log_visible = False

    def _build_dialog(self) -> None:
        self.dialog = ui.dialog()
        with self.dialog, ui.card().classes("w-[620px] max-w-full gap-3 p-5"):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                self.dialog_title = ui.label("").classes("aml-title")
                ui.button(icon="close", on_click=self.dialog.close).props("flat dense round size=sm")
            self.dialog_body = ui.column().classes("w-full gap-3 aml-scroll").style(
                "max-height: 66vh"
            )

    def open_dialog(self, title: str, builder: Callable[[], None]) -> None:
        self.dialog_title.text = title
        self.dialog_body.clear()
        with self.dialog_body:
            builder()
        self.dialog.open()

    # ------------------------------------------------------------------ 启动弹窗
    def _build_launch_dialog(self) -> None:
        self.launch_dialog = ui.dialog().props("persistent")
        with self.launch_dialog, ui.card().classes("w-[520px] max-w-full gap-3 p-5"):
            self.launch_title = ui.label("正在启动").classes("text-[16px] font-semibold")
            self.launch_detail = ui.label("准备中…").classes("aml-sub")
            with ui.element("div").classes("aml-bar-track w-full"):
                self.launch_fill = ui.element("div").classes("aml-bar-fill")
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                self.launch_percent = ui.label("0%").classes("text-[12px] font-semibold").style(
                    "color: var(--aml-accent)"
                )
                self.launch_cancel_btn = ui.button("取消", on_click=self._cancel_launch).props(
                    "flat dense no-caps color=negative"
                )
            self.launch_error = ui.label("").classes("aml-sub").style("color: #e5484d")
            self.launch_error.set_visibility(False)
            self.launch_close_btn = ui.button("关闭", on_click=self.launch_dialog.close).props(
                "flat dense no-caps"
            )
            self.launch_close_btn.set_visibility(False)
        self._launch_timer = ui.timer(0.15, self._sync_launch_dialog, active=False)
        self._home_launch_timer = ui.timer(0.2, self._sync_home_launch, active=False)

    def _apply_launch_progress(self) -> None:
        percent = max(0.0, min(100.0, float(self._launch_progress.get("percent") or 0.0)))
        self.launch_fill.style(f"width: {percent:.1f}%")
        self.launch_percent.text = f"{percent:.1f}%"
        if self._launch_cancel.is_set() and not self._launch_done:
            self.launch_detail.text = "正在取消…"
        else:
            self.launch_detail.text = str(self._launch_progress.get("detail") or "准备中…")

    def _sync_launch_dialog(self) -> None:
        self._apply_launch_progress()
        if not self._launch_done:
            return

        self._launch_timer.active = False
        self.launch_cancel_btn.set_visibility(False)
        self.launch_close_btn.set_visibility(True)
        if self._launch_error:
            self.launch_title.text = "启动未完成"
            if self._launch_cancel.is_set():
                self.launch_detail.text = "已取消启动"
            self.launch_error.text = self._launch_error
            self.launch_error.set_visibility(True)
            return

        self.launch_fill.style("width: 100%")
        self.launch_percent.text = "100%"
        ui.notify(str(self._launch_progress.get("detail") or "已启动"), type="positive", position="top-right")
        self._refresh_home()
        ui.timer(0.9, self.launch_dialog.close, once=True)

    def _cancel_launch(self) -> None:
        self._launch_cancel.set()
        self.launch_cancel_btn.set_visibility(False)
        self.launch_detail.text = "正在取消…"

    def _toggle_log(self) -> None:
        if self.log_element is None:
            return
        self.log_visible = not self.log_visible
        self.log_element.set_visibility(self.log_visible)

    def _refresh_logs(self) -> None:
        if self.log_element is None:
            return
        logs, revision = self.service.tasks.global_logs()
        if revision == self._log_revision:
            return
        self._log_revision = revision
        self.log_element.clear()
        for line in logs[-200:]:
            self.log_element.push(line)

    def _play(self, element_id: str) -> None:
        """重新触发指定元素的入场动画（同一 class 需先移除并强制回流）。"""
        ui.run_javascript(
            "const el = document.getElementById('" + element_id + "');"
            "if (el) { el.classList.remove('aml-page-in'); void el.offsetWidth;"
            " el.classList.add('aml-page-in'); }"
        )

    def _select_view(self, key: str) -> None:
        self.current_view = key
        for name, page in self.pages.items():
            page.set_visibility(name == key)
        # 任务页从下载页进入，导航上保持「下载」高亮。
        active_key = "download" if key == "tasks" else ("multiplayer" if key == "relay-create" else key)
        for name, button in self.nav_buttons.items():
            button.classes(add="active" if name == active_key else None, remove=None if name == active_key else "active")

        self._play(f"aml-page-{key}")

        if key == "home":
            self._refresh_home()
        elif key == "versions":
            self._reload_version_list()
        elif key == "download":
            if not self._download_built:
                self._download_built = True
                self._download_stale = False
                self._refresh_download_bar()
            else:
                # 复用已加载的列表，避免每次切回来都重新联网拉取版本清单。
                self._sync_download_versions()
                if self._download_stale:
                    self._download_stale = False
                    if self.download_type == "game":
                        # 安装完成后刷新一次，让「已安装」标记即时可见。
                        self._build_download_body()
        elif key == "tasks":
            self._render_tasks()
        elif key == "multiplayer":
            self._render_relay_body()
            background_tasks.create(self._refresh_relay_rooms())
        elif key == "relay-create":
            self._render_relay_create_page()

    # ==================================================================== 启动
    def _build_home_page(self) -> None:
        with ui.column().classes("w-full h-full p-5 gap-0 no-wrap") as page:
            self.pages["home"] = page
            # 整个启动页接受拖入：文件由隐藏的上传控件接收后再分类处理。
            page.props("id=aml-page-home data-aml-drop=aml-home-upload")
            page.set_visibility(False)

            with ui.row().classes("w-full flex-1 min-h-0 no-wrap gap-5"):
                # 左侧：更新日志（手风琴，默认展开最新一条）
                with ui.column().classes(
                    "aml-card aml-log-card flex-1 min-w-0 h-full p-0 gap-0 no-wrap"
                ):
                    with ui.row().classes(
                        "w-full items-center justify-between no-wrap px-6 pt-5 pb-2 shrink-0"
                    ):
                        ui.label("更新日志").classes("text-[14px] font-semibold")
                        ui.label(f"当前 v{APP_VERSION}").classes("aml-chip primary")
                    with ui.column().classes(
                        "w-full flex-1 min-h-0 px-6 pb-3 gap-0 no-wrap aml-scroll"
                    ) as changelog_box:
                        self.home_changelog = changelog_box
                        ui.label("正在获取更新日志…").classes("aml-sub py-2")
                    with ui.row().classes(
                        "w-full items-center gap-2 no-wrap px-6 py-3 shrink-0"
                    ).style("border-top: 1px solid var(--aml-border)"):
                        self.home_versions_chip = ui.label("0 个版本").classes(
                            "aml-chip primary"
                        )
                        self.home_current_chip = ui.label("未选择版本").classes("aml-chip")
                        self.home_account_chip = ui.label("未登录").classes("aml-chip")

                # 右侧：登录 + 启动
                with ui.column().classes(
                    "aml-hero-login w-[360px] h-full p-7 gap-3 items-center no-wrap"
                ):
                    ui.element("div").classes("w-full shrink-0").style("height: 3.2vh")
                    self.home_identity = ui.column().classes("w-full items-center gap-3")
                    ui.space()
                    self.home_version = ui.select(
                        options=[], value=None, label="启动版本"
                    ).classes("w-full").props("dense outlined")
                    self.home_launch = ui.button(
                        "启动游戏", icon="play_circle", on_click=self._on_home_launch_click
                    ).classes("aml-launch w-full h-[50px] text-[15px]").props("unelevated no-caps")

            # 启动页拖入的 Mod / 整合包由这个隐藏控件接收。
            ui.upload(
                on_multi_upload=self._handle_home_files,
                auto_upload=True,
                multiple=True,
            ).props("id=aml-home-upload").classes("hidden")

        background_tasks.create(self._load_changelog())

    # ---- 更新日志（GitHub Releases） ----
    async def _load_changelog(self) -> None:
        try:
            entries = await run.io_bound(
                self.service.launcher_changelog, CHANGELOG_LIMIT
            )
        except Exception as exc:  # noqa: BLE001 - 网络失败时退回内置文案
            self._render_changelog(error=str(exc) or "网络不可用")
            return
        self._render_changelog(entries=entries)

    def _render_changelog(
        self, entries: Optional[list[dict]] = None, error: str = ""
    ) -> None:
        box = getattr(self, "home_changelog", None)
        if box is None:
            return
        box.clear()
        with box:
            if not entries:
                if error:
                    ui.label(f"更新日志获取失败：{error}").classes("aml-sub py-1")
                    ui.button("重新获取", on_click=self._reload_changelog).props(
                        "flat dense no-caps"
                    ).classes("text-[12px] justify-start")
                    ui.label("以下为内置的最近一版说明").classes("aml-sub py-1")
                entries = CHANGELOG_FALLBACK
            for index, entry in enumerate(entries):
                with ui.expansion(
                    f"v{entry['version']}",
                    group="aml-changelog",
                    value=index == 0,
                ).classes("aml-logitem w-full"):
                    if entry.get("title"):
                        ui.label(entry["title"]).classes("aml-log-title")
                    ui.markdown(entry.get("notes") or "本次更新未填写说明。").classes(
                        "aml-log-body"
                    )
            ui.markdown(CHANGELOG_FOOTER).classes("aml-log-body")

    async def _reload_changelog(self) -> None:
        box = getattr(self, "home_changelog", None)
        if box is None:
            return
        box.clear()
        with box:
            ui.label("正在获取更新日志…").classes("aml-sub py-2")
        await self._load_changelog()

    def _refresh_home(self) -> None:
        self._render_home_identity()

        versions = [v["id"] for v in self.service.local_versions()]
        current = self.home_version.value
        if current not in versions:
            preferred = self.service.config.last_version
            current = preferred if preferred in versions else (versions[0] if versions else None)
        self.home_version.options = versions
        self.home_version.value = current
        self.home_version.set_visibility(not self._home_launching)
        self.home_version.update()

        if hasattr(self, "home_versions_chip"):
            self.home_versions_chip.text = f"{len(versions)} 个版本"
            self.home_current_chip.text = f"当前：{current}" if current else "未选择版本"
            account = self._active_account()
            self.home_account_chip.text = (
                f"玩家：{account['name']}" if account else "未登录"
            )

    def _render_home_identity(self) -> None:
        # 刷新登录区时保留用户已经输入但还没提交的用户名。
        draft = ""
        name_input = getattr(self, "home_name", None)
        if name_input is not None:
            try:
                draft = str(name_input.value or "")
            except Exception:  # noqa: BLE001 - 元素已被清理时忽略
                draft = ""
        self.home_identity.clear()
        if self._home_launching:
            with self.home_identity:
                with ui.element("div").classes("aml-play-pulse"):
                    ui.icon("play_arrow")
                ui.label("正在启动游戏").classes("text-[17px] font-semibold")
                self.home_launch_step = ui.label("准备中…").classes("aml-sub")
                with ui.element("div").classes("aml-bar-track w-[220px]"):
                    self.home_launch_fill = ui.element("div").classes("aml-bar-fill")
            return

        account = self._active_account()
        if not getattr(self, "_login_mode", ""):
            # 优先沿用上次的登录方式；没有记录时沿用上次登录账号的类型；
            # 都没有时默认正版登录。
            saved = str(getattr(self.service.config, "login_mode", "") or "")
            if saved not in ("offline", "microsoft"):
                kind = account.get("kind") if account else ""
                saved = kind if kind in ("offline", "microsoft") else "microsoft"
            self._login_mode = saved
        mode = self._login_mode
        # 正版与离线账号互相独立，只展示当前登录方式对应的账号。
        if account and account.get("kind") != mode:
            self._activate_mode_account(mode)
            account = self._active_account()
        if account and account.get("kind") != mode:
            account = None

        with self.home_identity:
            ui.toggle(
                {"microsoft": "正版登录", "offline": "离线登录"},
                value=mode,
                on_change=self._on_login_mode_change,
            ).props("no-caps dense unelevated").classes("aml-login-toggle")
            self._render_avatar(account)
            if account:
                ui.label(account["name"]).classes("text-[18px] font-semibold")
                with ui.row().classes("items-center gap-2 no-wrap"):
                    ui.label(
                        "正版登录" if account.get("kind") == "microsoft" else "离线登录"
                    ).classes("aml-chip")
                    ui.button("退出登录", on_click=self._logout_home).props(
                        "flat dense no-caps"
                    ).classes("aml-sub")
            elif mode == "microsoft":
                ui.button("登录", on_click=self._home_ms_login).classes(
                    "w-[220px]"
                ).props("unelevated dense no-caps color=primary")
            else:
                self.home_name = ui.input(
                    placeholder="输入用户名", value=draft
                ).classes("w-[220px]").props("dense outlined")
                self.home_name.on(
                    "keydown.enter", lambda: background_tasks.create(self._home_login())
                )
                ui.button("离线登录", icon="login", on_click=self._home_login).classes(
                    "w-[220px]"
                ).props("unelevated dense no-caps color=primary")

    def _on_login_mode_change(self, event) -> None:
        mode = str(getattr(event, "value", "") or "")
        if mode not in ("offline", "microsoft"):
            return
        self._set_login_mode(mode)
        self._activate_mode_account(mode)
        self._refresh_home()

    def _set_login_mode(self, mode: str) -> None:
        """切换并记住登录方式，下次启动优先使用。"""
        self._login_mode = mode
        if getattr(self.service.config, "login_mode", "") == mode:
            return
        try:
            self.service.update_config({"login_mode": mode})
        except Exception:  # noqa: BLE001 - 配置写入失败不影响本次登录
            pass

    def _activate_mode_account(self, mode: str) -> None:
        """切换登录方式时，直接切换到该方式下最近使用的账号。"""
        try:
            candidates = [a for a in self.service.accounts() if a.get("kind") == mode]
        except Exception:  # noqa: BLE001 - 账号读取失败时停留在当前账号
            return
        if mode == "microsoft":
            # 跳过上次登录中断、没有写入令牌的账号。
            store = self.service.accounts_store
            candidates = [a for a in candidates if store.get_tokens(a["id"]) is not None]
        if not candidates:
            return
        latest = max(candidates, key=lambda a: a.get("last_used") or 0)
        active = self._active_account()
        if active and active.get("id") == latest.get("id"):
            return
        try:
            self.service.account_set_active(latest["id"])
        except Exception:  # noqa: BLE001
            pass

    def _render_avatar(self, account: Optional[dict]) -> None:
        # 正版账号头像来自皮肤 API；离线账号与未登录状态使用本地史蒂夫头像。
        url = "/aml-assets/steve.png"
        if account and account.get("kind") == "microsoft":
            try:
                url = self.service.account_skin(account["id"]).get("avatar") or url
            except Exception:  # noqa: BLE001 - 皮肤服务不可用时用默认头像
                pass
        with ui.element("div").classes(
            "aml-avatar overflow-hidden flex items-center justify-center"
        ).style(
            "width:84px;height:84px;border-radius:0;"
            "background:#f3f4f6;border:1px solid #e5e7eb"
        ):
            ui.image(url).style("width:84px;height:84px;object-fit:cover")

    async def _home_login(self) -> None:
        name = (getattr(self, "home_name", None).value or "").strip() if hasattr(self, "home_name") else ""
        if not name:
            ui.notify("请输入用户名", type="warning", position="top-right")
            return
        try:
            await run.io_bound(self.service.account_add_offline, name)
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"登录失败：{exc}", type="negative", position="top-right")
            return
        ui.notify(f"已登录：{name}", type="positive", position="top-right")
        self._set_login_mode("offline")
        self._refresh_home()

    async def _home_ms_login(self) -> None:
        """微软正版登录：设备码授权 + 定时轮询。"""
        if getattr(self, "_ms_login_open", False):
            ui.notify("正版登录窗口已打开，请在浏览器完成授权", type="warning", position="top-right")
            return
        try:
            info = await run.io_bound(self.service.auth_start)
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"申请设备码失败：{exc}", type="negative", position="top-right")
            return

        self._ms_login_open = True
        started = time.monotonic()
        timer_holder: dict = {}

        def stop() -> None:
            active = timer_holder.get("timer")
            if active is not None:
                active.active = False
            self._ms_login_open = False

        def cancel() -> None:
            stop()
            self.dialog.close()

        def builder() -> None:
            ui.label("微软账号登录").classes("text-[16px] font-semibold")
            ui.label(info["verification_uri"]).classes("text-[13px] select-all")
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                ui.label(info["user_code"]).classes("text-[22px] font-semibold").style(
                    "font-family: Consolas, monospace; letter-spacing: 2px"
                )
                ui.button(
                    "复制设备码", icon="content_copy",
                    on_click=lambda: (
                        ui.clipboard.write(info["user_code"]),
                        ui.notify("设备码已复制", position="top-right"),
                    ),
                ).props("flat dense no-caps")
            status = ui.label("等待浏览器授权…").classes("aml-sub")

            async def poll() -> None:
                if time.monotonic() - started > float(info.get("expires_in") or 900):
                    status.text = "设备码已过期，请重新发起登录"
                    stop()
                    return
                try:
                    result = await run.io_bound(
                        self.service.auth_poll, info["device_code"]
                    )
                except Exception as exc:  # noqa: BLE001
                    status.text = f"登录失败：{exc}"
                    stop()
                    return
                if result.get("status") == "ok":
                    stop()
                    self.dialog.close()
                    name = (result.get("stored") or {}).get("name") or "微软账号"
                    self._set_login_mode("microsoft")
                    ui.notify(f"正版登录成功：{name}", type="positive", position="top-right")
                    self._refresh_home()

            with ui.row().classes("w-full justify-between items-center no-wrap"):
                ui.button(
                    "打开浏览器", icon="open_in_new",
                    on_click=lambda: webbrowser.open(info["verification_uri"]),
                ).props("flat dense no-caps")
                ui.button("取消", on_click=cancel).props("flat dense no-caps")
            timer_holder["timer"] = ui.timer(
                max(5, int(info.get("interval") or 5)), poll
            )

        self._set_login_mode("microsoft")
        self.open_dialog("正版登录", builder)
        # 设备码自动复制，1 秒后自动打开授权页（也保留手动按钮作为兜底）。
        ui.clipboard.write(info["user_code"])
        ui.timer(
            1.0,
            lambda: webbrowser.open(info["verification_uri"]),
            once=True,
        )

    async def _logout_home(self) -> None:
        account = self._active_account()
        if account is None:
            return
        try:
            await run.io_bound(self.service.account_remove, account["id"])
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"退出失败：{exc}", type="negative", position="top-right")
            return
        ui.notify("已退出登录", position="top-right")
        self._refresh_home()

    def _on_home_launch_click(self) -> None:
        if self._home_launching:
            self._cancel_home_launch()
            return
        background_tasks.create(self._on_home_launch())

    async def _on_home_launch(self) -> None:
        if self._home_launching:
            return
        account = self._active_account()
        if account is None:
            ui.notify("请先输入用户名登录", type="warning", position="top-right")
            return
        version = self.home_version.value
        if not version:
            ui.notify("请先在下载页安装游戏版本", type="warning", position="top-right")
            return
        if self.service.has_running_game():
            info = self.service.running_game_info()
            ui.notify(
                f"游戏正在运行（{info.get('version') or '未知版本'}），请先关闭游戏窗口",
                type="warning",
                position="top-right",
            )
            return
        self._start_home_launch()
        await self._launch(
            version, username=account["name"], account_id=account["id"], inline=True
        )

    def _start_home_launch(self) -> None:
        self._home_launching = True
        self._launch_progress = {"percent": 0.0, "detail": "准备中…"}
        self._render_home_identity()
        self.home_version.set_visibility(False)
        self.home_launch.text = "取消"
        self.home_launch.props("icon=close")
        self.home_launch.classes(remove="aml-launch", add="aml-launch-cancel")
        if self._home_launch_timer is None:
            self._home_launch_timer = ui.timer(0.2, self._sync_home_launch, active=False)
        self._home_launch_timer.active = True

    def _sync_home_launch(self) -> None:
        if not self._home_launching:
            if self._home_launch_timer is not None:
                self._home_launch_timer.active = False
            return
        percent = max(0.0, min(100.0, float(self._launch_progress.get("percent") or 0.0)))
        step = getattr(self, "home_launch_step", None)
        if step is not None:
            step.text = (
                "正在取消…"
                if self._launch_cancel.is_set()
                else str(self._launch_progress.get("detail") or "准备中…")
            )
        fill = getattr(self, "home_launch_fill", None)
        if fill is not None:
            fill.style(f"width: {percent:.1f}%")

    def _cancel_home_launch(self) -> None:
        self._launch_cancel.set()
        self.home_launch.text = "取消中…"

    def _finish_home_launch(self, error: str) -> None:
        self._home_launching = False
        self._launch_done = True
        if self._home_launch_timer is not None:
            self._home_launch_timer.active = False
        self._render_home_identity()
        self.home_version.set_visibility(True)
        self.home_launch.text = "启动游戏"
        self.home_launch.props("icon=play_circle")
        self.home_launch.classes(remove="aml-launch-cancel", add="aml-launch")
        if error:
            cancelled = self._launch_cancel.is_set() and error == "已取消启动"
            ui.notify(
                error,
                type="warning" if cancelled else "negative",
                position="top-right",
            )
        else:
            ui.notify(
                str(self._launch_progress.get("detail") or "游戏已启动"),
                type="positive",
                position="top-right",
            )
        self._refresh_home()

    # ------------------------------------------------------------ 启动页拖入导入
    MOD_SUFFIXES = {".jar"}
    PACK_SUFFIXES = {".mrpack", ".zip"}

    @staticmethod
    async def _save_upload(file, path: Path) -> None:
        """优先使用上传对象的流式保存接口，兼容旧版 NiceGUI 回退到 read()。"""
        save = getattr(file, "save", None)
        if callable(save):
            await save(path)
            return
        data = await file.read()
        await run.io_bound(path.write_bytes, data)

    async def _handle_home_files(self, event) -> None:
        """启动页拖入：按扩展名自动识别 Mod / 整合包，解析后弹窗确认。"""
        files = list(getattr(event, "files", []) or [])
        if not files:
            return
        version = self.home_version.value or self.service.config.last_version
        items: list[dict] = []
        errors: list[str] = []
        ui.notify(f"正在读取 {len(files)} 个文件…", position="top-right")

        for file in files:
            filename = Path(file.name).name
            suffix = Path(filename).suffix.lower()
            if suffix in self.MOD_SUFFIXES:
                if not version:
                    errors.append(f"{filename}：请先选择启动版本")
                    continue
                with tempfile.NamedTemporaryFile(delete=False, suffix=".jar") as handle:
                    temp_path = Path(handle.name)
                try:
                    # 流式落盘，避免大文件整体读进内存。
                    await self._save_upload(file, temp_path)
                except Exception as exc:  # noqa: BLE001
                    temp_path.unlink(missing_ok=True)
                    errors.append(f"{filename}：{exc}")
                    continue
                items.append(
                    {"kind": "mod", "name": filename, "path": temp_path, "target": version}
                )
            elif suffix in self.PACK_SUFFIXES:
                target_dir = self.service.modpack_dir()
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / filename
                try:
                    await self._save_upload(file, target)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{filename}：{exc}")
                    continue
                try:
                    info = await run.io_bound(self.service.inspect_modpack, str(target))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{filename}：{exc}")
                    continue
                pack_version = info.get("game_version") or ""
                if not pack_version:
                    errors.append(f"{filename}：无法确定游戏版本")
                    continue
                items.append(
                    {
                        "kind": "modpack",
                        "name": filename,
                        "path": target,
                        "target": pack_version,
                        "loader": info.get("loader") or "",
                    }
                )
            else:
                errors.append(f"{filename}：不支持的文件类型")

        if errors:
            ui.notify("；".join(errors[:3]), type="warning", position="top-right")
        if items:
            self._confirm_home_import(items)

    def _confirm_home_import(self, items: list[dict]) -> None:
        def builder() -> None:
            ui.label("已识别以下文件，确认后开始导入：").classes("aml-sub")
            for item in items:
                is_mod = item["kind"] == "mod"
                with ui.row().classes(
                    "w-full items-center gap-2 no-wrap px-2 py-1 rounded-lg"
                ).style("background: var(--aml-surface-2, #f9fafb)"):
                    ui.label("Mod" if is_mod else "整合包").classes(
                        "aml-chip primary" if is_mod else "aml-chip"
                    )
                    ui.label(item["name"]).classes("text-[13px] flex-1 min-w-0 truncate")
                    target = item["target"] + (f" · {item['loader']}" if item.get("loader") else "")
                    ui.label(f"→ {target}").classes("aml-sub shrink-0")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button(
                    "取消", on_click=lambda: self._cancel_home_import(items)
                ).props("flat dense no-caps")
                ui.button(
                    "确认导入", icon="check",
                    on_click=lambda: background_tasks.create(self._run_home_import(items)),
                ).props("unelevated dense no-caps color=primary")

        self.open_dialog("确认导入", builder)

    def _cancel_home_import(self, items: list[dict]) -> None:
        self.dialog.close()
        for item in items:
            if item["kind"] == "mod":
                Path(item["path"]).unlink(missing_ok=True)

    async def _run_home_import(self, items: list[dict]) -> None:
        self.dialog.close()
        added = 0
        packs = 0
        errors: list[str] = []
        for item in items:
            if item["kind"] == "mod":
                try:
                    await run.io_bound(
                        self.service.content_import, "mod", str(item["path"]),
                        version_id=item["target"], overwrite=True,
                        filename=item["name"],
                    )
                    added += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{item['name']}：{exc}")
                finally:
                    Path(item["path"]).unlink(missing_ok=True)
            else:
                try:
                    self.service.start_modpack_install(
                        str(item["path"]), version_id=item["target"]
                    )
                    packs += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{item['name']}：{exc}")
        if added:
            ui.notify(f"已添加 {added} 个 Mod", type="positive", position="top-right")
        if packs:
            ui.notify(f"已开始安装 {packs} 个整合包，进度见「任务」页", position="top-right")
        if errors:
            ui.notify("；".join(errors[:3]), type="negative", position="top-right")
        self._download_stale = True
        self._refresh_home()
        if self.current_view == "versions":
            self._reload_version_list()

    # ==================================================================== 版本
    def _build_versions_page(self) -> None:
        with ui.row().classes("w-full h-full no-wrap gap-0") as page:
            self.pages["versions"] = page
            page.props("id=aml-page-versions")
            page.set_visibility(False)

            with ui.column().classes(
                "w-[240px] h-full shrink-0 p-3 gap-2 aml-scroll aml-sidebar"
            ):
                with ui.row().classes("w-full items-center justify-between no-wrap px-1"):
                    ui.label("本地版本").classes("text-[13px] font-semibold")
                    ui.button(icon="refresh", on_click=self._reload_version_list).props(
                        "flat dense round size=sm"
                    )
                self.version_list = ui.column().classes("w-full gap-1")

            with ui.column().classes("flex-1 h-full min-w-0 p-5 gap-3 aml-scroll"):
                self.version_detail = ui.column().classes("w-full gap-3").props(
                    "id=aml-version-detail"
                )

    def _reload_version_list(self) -> None:
        versions = self.service.local_versions()
        ids = [v["id"] for v in versions]
        if self.detail_version_id not in ids:
            self.detail_version_id = ids[0] if ids else ""
        self._render_version_list(versions)
        self._render_version_detail(self.detail_version_id)

    def _render_version_list(self, versions: list[dict]) -> None:
        self.version_list.clear()
        with self.version_list:
            if not versions:
                with ui.column().classes("aml-empty"):
                    ui.icon("inventory_2")
                    ui.label("暂无已安装版本").classes("aml-sub")
                    ui.label("去下载页安装一个吧").classes("aml-sub")
            for version in versions:
                active = version["id"] == self.detail_version_id
                with ui.column().classes(
                    "aml-vitem w-full p-2.5 gap-1"
                    + (" active" if active else "")
                ) as row:
                    ui.label(version["id"]).classes("text-[13px] font-medium")
                    with ui.row().classes("items-center gap-1 no-wrap"):
                        ui.label(LOADER_LABELS.get(version["loader"], version["loader"])).classes(
                            "aml-chip primary"
                        )
                        if version.get("isolated"):
                            ui.label("隔离").classes("aml-chip")
                row.on("click", lambda _=None, vid=version["id"]: self._select_version(vid))

    def _select_version(self, version_id: str) -> None:
        self.detail_version_id = version_id
        self._render_version_list(self.service.local_versions())
        self._render_version_detail(version_id)

    def _render_version_detail(self, version_id: str) -> None:
        self.version_detail.clear()
        if not version_id:
            with self.version_detail:
                with ui.column().classes("aml-empty aml-card flat w-full").style(
                    "min-height: 420px"
                ):
                    ui.icon("rocket_launch")
                    ui.label("左侧还没有版本").classes("text-[14px] font-medium")
                    ui.label("先在下载页安装游戏，或导入整合包").classes("aml-sub")
            self._play("aml-version-detail")
            return

        profile = self.service.version_profile(version_id)
        with self.version_detail:
            with ui.row().classes(
                "aml-detail-head w-full items-center justify-between no-wrap p-4 gap-3"
            ):
                with ui.row().classes("items-center gap-3 no-wrap min-w-0"):
                    with ui.element("div").classes("aml-version-badge"):
                        ui.icon("inventory_2").classes("text-[20px]")
                    with ui.column().classes("gap-0.5 min-w-0"):
                        ui.label(version_id).classes("text-[17px] font-semibold")
                        with ui.row().classes("items-center gap-1 no-wrap"):
                            ui.label(
                                LOADER_LABELS.get(profile["loader"], profile["loader"])
                            ).classes("aml-chip primary")
                            ui.label(profile.get("game_version") or "").classes("aml-chip")
                with ui.row().classes("gap-2 no-wrap shrink-0"):
                    ui.button(
                        "启动", icon="play_arrow",
                        on_click=lambda _=None, v=version_id: background_tasks.create(self._launch_selected(v)),
                    ).props("unelevated dense no-caps color=primary")
                    ui.button(
                        "设为当前", icon="check",
                        on_click=lambda _=None, v=version_id: self._set_current_version(v),
                    ).props("flat dense no-caps")
                    ui.button(
                        "重命名", icon="edit",
                        on_click=lambda _=None, v=version_id: self._open_rename_version(v),
                    ).props("flat dense no-caps")
                    ui.button(
                        "删除", icon="delete",
                        on_click=lambda _=None, v=version_id: self._open_delete_version(v),
                    ).props("flat dense no-caps color=negative")

            ui.toggle(
                {
                    "mods": "Mod 管理",
                    "resourcepacks": "资源包",
                    "shaders": "光影",
                    "saves": "存档",
                    "screenshots": "截图",
                    "servers": "服务器",
                    "settings": "版本设置",
                },
                value=self.version_tab,
                on_change=lambda e: self._switch_version_tab(e.value),
            ).props("unelevated dense no-caps toggle-color=primary").classes("self-start")

            mods_box = ui.column().classes("w-full gap-3").props("id=aml-vtab-mods")
            resourcepacks_box = ui.column().classes("w-full gap-3").props("id=aml-vtab-resourcepacks")
            shaders_box = ui.column().classes("w-full gap-3").props("id=aml-vtab-shaders")
            saves_box = ui.column().classes("w-full gap-3").props("id=aml-vtab-saves")
            screenshots_box = ui.column().classes("w-full gap-3").props("id=aml-vtab-screenshots")
            servers_box = ui.column().classes("w-full gap-3").props("id=aml-vtab-servers")
            settings_box = ui.column().classes("w-full gap-3").props("id=aml-vtab-settings")
            with mods_box:
                self._build_version_mods(version_id)
            with resourcepacks_box:
                self._build_version_contents("resourcepack", version_id)
            with shaders_box:
                self._build_version_contents("shader", version_id)
            with saves_box:
                self._build_version_saves(version_id)
            with screenshots_box:
                self._build_version_screenshots(version_id)
            with servers_box:
                self._build_version_servers(version_id)
            with settings_box:
                self._build_version_repair(version_id)
                self._build_version_settings(version_id)
                self._build_version_loader(version_id)
                self._build_version_crash(version_id)

        self.version_tab_boxes = {
            "mods": mods_box,
            "resourcepacks": resourcepacks_box,
            "shaders": shaders_box,
            "saves": saves_box,
            "screenshots": screenshots_box,
            "servers": servers_box,
            "settings": settings_box,
        }
        self._switch_version_tab(self.version_tab)
        self._play("aml-version-detail")

    def _switch_version_tab(self, value: str) -> None:
        self.version_tab = value or "mods"
        for key, box in self.version_tab_boxes.items():
            box.set_visibility(key == self.version_tab)
        self._play(f"aml-vtab-{self.version_tab}")

    def _open_delete_version(self, version_id: str) -> None:
        def builder() -> None:
            ui.label(f"确定删除版本 {version_id}？").classes("text-[15px] font-semibold")
            ui.label(
                "该版本目录下的所有文件（含隔离的 Mod / 存档 / 配置）都会被删除，且无法恢复。"
            ).classes("aml-sub")

            async def do_delete() -> None:
                try:
                    await run.io_bound(self.service.delete_version, version_id)
                except Exception as exc:  # noqa: BLE001
                    ui.notify(f"删除失败：{exc}", type="negative", position="top-right")
                    return
                self.dialog.close()
                if self.detail_version_id == version_id:
                    self.detail_version_id = ""
                ui.notify(f"已删除版本 {version_id}", position="top-right")
                self._refresh_home()
                self._reload_version_list()

            ui.button("确认删除", icon="delete", on_click=do_delete).props(
                "unelevated dense no-caps color=negative"
            )

        self.open_dialog("删除版本", builder)

    def _open_rename_version(self, version_id: str) -> None:
        def builder() -> None:
            name = ui.input(label="新版本名称", value=version_id).classes("w-full").props(
                "dense outlined"
            )
            ui.label("重命名会同步修改版本目录、JSON 与版本设置。").classes("aml-sub")

            async def do_rename() -> None:
                new_id = _sanitize_version_name((name.value or "").strip())
                if not new_id or new_id == version_id:
                    self.dialog.close()
                    return
                try:
                    await run.io_bound(self.service.rename_version, version_id, new_id)
                except Exception as exc:  # noqa: BLE001
                    ui.notify(f"重命名失败：{exc}", type="negative", position="top-right")
                    return
                self.dialog.close()
                self.detail_version_id = new_id
                self._refresh_home()
                self._reload_version_list()
                ui.notify(f"已重命名为 {new_id}", type="positive", position="top-right")

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("取消", on_click=self.dialog.close).props("flat dense no-caps")
                ui.button("确认重命名", icon="edit", on_click=do_rename).props(
                    "unelevated dense no-caps color=primary"
                )

        self.open_dialog("重命名版本", builder)

    def _build_version_repair(self, version_id: str) -> None:
        with ui.column().classes("aml-card flat w-full p-4 gap-2"):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                with ui.column().classes("gap-0"):
                    ui.label("补全文件").classes("text-[14px] font-semibold")
                    ui.label("校验客户端 jar / 库 / 资源，缺失或损坏的文件会自动重新下载").classes("aml-sub")
                ui.button(
                    "开始校验", icon="build",
                    on_click=lambda _=None, v=version_id: self._repair_version(v),
                ).props("unelevated dense no-caps color=primary")

    def _repair_version(self, version_id: str) -> None:
        self.service.start_repair(version_id)
        self._select_view("tasks")
        ui.notify(f"已开始校验 {version_id}，进度见「任务」页", position="top-right")

    async def _launch_selected(self, version_id: str) -> None:
        account = self._active_account()
        if account is None:
            ui.notify("请先在「启动」页登录", type="warning", position="top-right")
            return
        await self._launch(version_id, username=account["name"], account_id=account["id"])

    def _set_current_version(self, version_id: str) -> None:
        self.service.update_config({"last_version": version_id})
        ui.notify(f"当前版本：{version_id}", type="positive", position="top-right")
        self._refresh_home()

    # ------------------------------------------------------------ 版本：Mod 管理
    def _build_version_mods(self, version_id: str) -> None:
        mods_dir = self.service.content_manager().dir_for(
            "mod", version_id=version_id, isolated=self.service._is_isolated(version_id)
        )
        safe_id = "".join(ch if ch.isalnum() else "-" for ch in version_id)
        updates: dict[str, dict] = {}

        async def refresh_mods(check: bool = False, notify: bool = False) -> None:
            mod_list.clear()
            with mod_list:
                with ui.row().classes("w-full items-center justify-center p-4"):
                    ui.spinner(size="sm").style("color: var(--aml-accent)")
            try:
                items = await run.io_bound(self.service.content_list, "mod", version_id)
            except Exception as exc:  # noqa: BLE001
                items = []
                ui.notify(f"读取 Mod 失败：{exc}", type="negative", position="top-right")
            mod_list.clear()
            with mod_list:
                if not items:
                    ui.label("暂无 Mod").classes("aml-sub")
                for item in items:
                    self._mod_row(version_id, item, updates, reload_mods)
            if check:
                try:
                    found = await run.io_bound(self.service.check_mod_updates, version_id)
                except Exception:  # noqa: BLE001
                    found = {}
                updates.clear()
                updates.update(found or {})
                if found:
                    ui.notify(
                        f"{len(found)} 个 Mod 有可用更新",
                        type="info",
                        position="top-right",
                    )
                    await refresh_mods(False)
                elif notify:
                    ui.notify("所有 Mod 都是最新的", type="positive", position="top-right")

        def reload_mods() -> None:
            background_tasks.create(refresh_mods(False))

        with ui.column().classes("aml-card flat w-full p-4 gap-2"):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                ui.label("Mod 管理").classes("text-[14px] font-semibold")
                with ui.row().classes("gap-1 no-wrap"):
                    ui.button(
                        "检查更新", icon="update",
                        on_click=lambda: background_tasks.create(refresh_mods(True, True)),
                    ).props("flat dense no-caps")
                    ui.button("刷新", icon="refresh", on_click=reload_mods).props(
                        "flat dense no-caps"
                    )
            ui.label(f"目录：{mods_dir}").classes("aml-sub")

            async def on_mod_files(event) -> None:
                await self._handle_mod_uploads(event, version_id, reload_mods, drop)

            drop = ImportDrop(
                upload_id=f"aml-mod-upload-{safe_id}",
                hint="点击选择或拖入 Mod（.jar，可多选）",
                handler=on_mod_files,
            ).build()
            mod_list = ui.column().classes("w-full gap-1")

        background_tasks.create(refresh_mods(True))

    def _mod_row(
        self,
        version_id: str,
        item: dict,
        updates: dict[str, dict],
        reload_mods: Callable[[], None],
    ) -> None:
        update = updates.get(item["filename"])
        with ui.row().classes(
            "w-full items-center gap-3 no-wrap px-2 py-2 rounded-lg"
        ).style("background: var(--aml-surface-2)"):
            if item.get("icon"):
                ui.image(item["icon"]).classes("w-[40px] h-[40px] rounded-md shrink-0").style(
                    "object-fit: cover"
                )
            else:
                with ui.element("div").classes(
                    "w-[40px] h-[40px] rounded-md flex items-center justify-center shrink-0"
                ).style("background: #eef0f3"):
                    ui.icon("extension").classes("text-[22px]").style(
                        "color: var(--aml-muted)"
                    )
            with ui.column().classes("flex-1 min-w-0 gap-0.5"):
                with ui.row().classes("items-center gap-2 no-wrap"):
                    ui.label(item.get("display_name") or item["filename"]).classes(
                        "text-[13px] font-medium truncate"
                    )
                    if item.get("mod_version"):
                        ui.label(f"v{item['mod_version']}").classes("aml-chip")
                    if update:
                        ui.label("有更新").classes("aml-chip warn")
                    ui.label("已启用" if item["enabled"] else "已禁用").classes(
                        "aml-chip installed" if item["enabled"] else "aml-chip"
                    )
                ui.label(
                    f"{item['filename']} · {_fmt_size(item.get('size') or 0)}"
                ).classes("aml-sub truncate")
                if update:
                    ui.label(
                        f"可更新：{update.get('current_version') or '当前版本'} → "
                        f"{update.get('new_version') or '新版本'}"
                    ).classes("aml-sub")
            with ui.row().classes("gap-1 shrink-0 no-wrap"):
                if update and item["enabled"]:
                    ui.button(
                        "更新", icon="upgrade",
                        on_click=lambda _=None, i=item, u=update: background_tasks.create(
                            self._do_update_mod(version_id, i, u, reload_mods)
                        ),
                    ).props("flat dense no-caps color=primary")
                ui.button(
                    "禁用" if item["enabled"] else "启用",
                    on_click=lambda _=None, i=item: self._toggle_mod(
                        version_id, i, reload_mods
                    ),
                ).props("flat dense no-caps")
                ui.button(
                    "删除",
                    on_click=lambda _=None, f=item["filename"]: self._delete_mod(
                        version_id, f, reload_mods
                    ),
                ).props("flat dense no-caps color=negative")

    async def _do_update_mod(
        self,
        version_id: str,
        item: dict,
        update: dict,
        reload_mods: Callable[[], None],
    ) -> None:
        try:
            await run.io_bound(
                self.service.update_mod,
                version_id,
                item["filename"],
                update["url"],
                update["filename"],
                expected_sha1=update.get("sha1") or "",
            )
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"更新失败：{exc}", type="negative", position="top-right")
            return
        ui.notify(
            f"已更新 {item.get('display_name') or item['filename']}",
            type="positive",
            position="top-right",
        )
        reload_mods()

    async def _handle_mod_uploads(
        self, event, version_id: str, reload_mods: Callable[[], None], drop: ImportDrop
    ) -> None:
        files = list(getattr(event, "files", []) or [])
        total = len(files)
        if not total:
            return
        done = 0
        added = 0
        errors: list[str] = []
        drop.busy(f"正在导入 0/{total}…", percent=0)
        for file in files:
            filename = Path(file.name).name
            done += 1
            if Path(filename).suffix.lower() != ".jar":
                errors.append(f"{filename} 不是 .jar")
                continue
            drop.busy(f"正在导入 {done}/{total}：{filename}", percent=(done - 1) / total * 100)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".jar") as handle:
                temp_path = Path(handle.name)
            try:
                await self._save_upload(file, temp_path)
                await run.io_bound(
                    self.service.content_import, "mod", str(temp_path),
                    version_id=version_id, overwrite=True, filename=filename,
                )
                added += 1
            except Exception as exc:  # noqa: BLE001 - 单个失败不影响其它文件
                errors.append(f"{filename}：{exc}")
            finally:
                temp_path.unlink(missing_ok=True)
            drop.busy(f"正在导入 {done}/{total}…", percent=done / total * 100)

        drop.idle()
        reload_mods()
        if added:
            ui.notify(f"已添加 {added} 个 Mod", type="positive", position="top-right")
        if errors:
            ui.notify("；".join(errors[:3]), type="negative", position="top-right")

    def _toggle_mod(self, version_id: str, item: dict, reload_mods: Callable[[], None]) -> None:
        try:
            self.service.content_toggle("mod", item["filename"], not item["enabled"], version_id)
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"操作失败：{exc}", type="negative", position="top-right")
            return
        reload_mods()

    def _delete_mod(self, version_id: str, filename: str, reload_mods: Callable[[], None]) -> None:
        try:
            self.service.content_delete("mod", filename, version_id)
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"删除失败：{exc}", type="negative", position="top-right")
            return
        ui.notify(f"已删除 {filename}", position="top-right")
        reload_mods()

    # ------------------------------------------------- 版本：资源包 / 光影
    def _build_version_contents(self, kind: str, version_id: str) -> None:
        label = CONTENT_LABELS.get(kind, kind)
        isolated = self.service._is_isolated(version_id)
        directory = self.service.content_manager().dir_for(
            kind, version_id=version_id, isolated=isolated
        )
        safe_id = "".join(ch if ch.isalnum() else "-" for ch in version_id)
        content_box = None

        async def refresh_contents() -> None:
            content_box.clear()
            with content_box:
                with ui.row().classes("w-full items-center justify-center p-4"):
                    ui.spinner(size="sm").style("color: var(--aml-accent)")
            try:
                items = await run.io_bound(self.service.content_list, kind, version_id)
            except Exception as exc:  # noqa: BLE001
                items = []
                ui.notify(f"读取{label}失败：{exc}", type="negative", position="top-right")
            content_box.clear()
            with content_box:
                if not items:
                    ui.label(f"暂无{label}").classes("aml-sub")
                for item in items:
                    self._content_row(kind, version_id, item, reload_contents)

        def reload_contents() -> None:
            background_tasks.create(refresh_contents())

        async def on_content_files(event) -> None:
            files = list(getattr(event, "files", []) or [])
            total = len(files)
            if not total:
                return
            done = 0
            added = 0
            errors: list[str] = []
            drop.busy(f"正在导入 0/{total}…", percent=0)
            for file in files:
                filename = Path(file.name).name
                done += 1
                if Path(filename).suffix.lower() != ".zip":
                    errors.append(f"{filename} 不是 .zip")
                    continue
                drop.busy(
                    f"正在导入 {done}/{total}：{filename}", percent=(done - 1) / total * 100
                )
                with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as handle:
                    temp_path = Path(handle.name)
                try:
                    await self._save_upload(file, temp_path)
                    await run.io_bound(
                        self.service.content_import, kind, str(temp_path),
                        version_id=version_id, overwrite=True, filename=filename,
                    )
                    added += 1
                except Exception as exc:  # noqa: BLE001 - 单个失败不影响其它文件
                    errors.append(f"{filename}：{exc}")
                finally:
                    temp_path.unlink(missing_ok=True)
                drop.busy(f"正在导入 {done}/{total}…", percent=done / total * 100)
            drop.idle()
            reload_contents()
            if added:
                ui.notify(f"已添加 {added} 个{label}", type="positive", position="top-right")
            if errors:
                ui.notify("；".join(errors[:3]), type="negative", position="top-right")

        with ui.column().classes("aml-card flat w-full p-4 gap-2"):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                ui.label(f"{label}管理").classes("text-[14px] font-semibold")
                with ui.row().classes("gap-1 no-wrap"):
                    ui.button(
                        "打开目录", icon="folder_open",
                        on_click=lambda: self._open_content_dir(kind, version_id),
                    ).props("flat dense no-caps")
                    ui.button("刷新", icon="refresh", on_click=reload_contents).props(
                        "flat dense no-caps"
                    )
            ui.label(f"目录：{directory}").classes("aml-sub")
            drop = ImportDrop(
                upload_id=f"aml-{kind}-upload-{safe_id}",
                hint=f"点击选择或拖入{label}（.zip，可多选）",
                handler=on_content_files,
            ).build()
            content_box = ui.column().classes("w-full gap-1")

        background_tasks.create(refresh_contents())

    def _content_row(
        self, kind: str, version_id: str, item: dict, reload_contents: Callable[[], None]
    ) -> None:
        icon = "palette" if kind == "resourcepack" else "auto_awesome"
        with ui.row().classes(
            "w-full items-center gap-3 no-wrap px-2 py-2 rounded-lg"
        ).style("background: var(--aml-surface-2)"):
            with ui.element("div").classes(
                "w-[36px] h-[36px] rounded-md flex items-center justify-center shrink-0"
            ).style("background: #eef0f3"):
                ui.icon(icon).classes("text-[20px]").style("color: var(--aml-muted)")
            with ui.column().classes("flex-1 min-w-0 gap-0.5"):
                with ui.row().classes("items-center gap-2 no-wrap"):
                    ui.label(item["filename"]).classes("text-[13px] font-medium truncate")
                    ui.label("已启用" if item["enabled"] else "已禁用").classes(
                        "aml-chip installed" if item["enabled"] else "aml-chip"
                    )
                ui.label(_fmt_size(item.get("size") or 0)).classes("aml-sub")
            with ui.row().classes("gap-1 shrink-0 no-wrap"):
                ui.button(
                    "禁用" if item["enabled"] else "启用",
                    on_click=lambda _=None, i=item: self._toggle_content(
                        kind, version_id, i, reload_contents
                    ),
                ).props("flat dense no-caps")
                ui.button(
                    "删除",
                    on_click=lambda _=None, i=item: self._delete_content(
                        kind, version_id, i["filename"], reload_contents
                    ),
                ).props("flat dense no-caps color=negative")

    def _open_content_dir(self, kind: str, version_id: str) -> None:
        isolated = self.service._is_isolated(version_id)
        directory = self.service.content_manager().dir_for(
            kind, version_id=version_id, isolated=isolated
        )
        try:
            directory.mkdir(parents=True, exist_ok=True)
            self.service.open_path(str(directory))
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"打开目录失败：{exc}", type="negative", position="top-right")

    def _toggle_content(
        self, kind: str, version_id: str, item: dict, reload_contents: Callable[[], None]
    ) -> None:
        try:
            self.service.content_toggle(kind, item["filename"], not item["enabled"], version_id)
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"操作失败：{exc}", type="negative", position="top-right")
            return
        reload_contents()

    def _delete_content(
        self,
        kind: str,
        version_id: str,
        filename: str,
        reload_contents: Callable[[], None],
    ) -> None:
        try:
            self.service.content_delete(kind, filename, version_id)
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"删除失败：{exc}", type="negative", position="top-right")
            return
        ui.notify(f"已删除 {filename}", position="top-right")
        reload_contents()

    # ------------------------------------------------------------ 版本：截图
    def _build_version_screenshots(self, version_id: str) -> None:
        shot_box = None

        async def refresh_shots() -> None:
            shot_box.clear()
            with shot_box:
                with ui.row().classes("w-full items-center justify-center p-4"):
                    ui.spinner(size="sm").style("color: var(--aml-accent)")
            try:
                shots = await run.io_bound(self.service.list_screenshots, version_id)
            except Exception as exc:  # noqa: BLE001
                shots = []
                ui.notify(f"读取截图失败：{exc}", type="negative", position="top-right")
            shot_box.clear()
            with shot_box:
                if not shots:
                    ui.label("暂无截图").classes("aml-sub")
                with ui.element("div").classes("w-full grid grid-cols-3 gap-2"):
                    for shot in shots:
                        self._screenshot_card(version_id, shot, reload_shots)

        def reload_shots() -> None:
            background_tasks.create(refresh_shots())

        with ui.column().classes("aml-card flat w-full p-4 gap-2"):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                ui.label("截图管理").classes("text-[14px] font-semibold")
                with ui.row().classes("gap-1 no-wrap"):
                    ui.button(
                        "打开目录", icon="folder_open",
                        on_click=lambda: self._open_screenshots_dir(version_id),
                    ).props("flat dense no-caps")
                    ui.button("刷新", icon="refresh", on_click=reload_shots).props(
                        "flat dense no-caps"
                    )
            ui.label("游戏内按 F2 截图会保存在该目录").classes("aml-sub")
            shot_box = ui.column().classes("w-full gap-2")

        background_tasks.create(refresh_shots())

    def _screenshot_url(self, path: str) -> str:
        cache = getattr(self, "_screenshot_urls", None)
        if cache is None:
            cache = {}
            self._screenshot_urls = cache
        if path not in cache:
            try:
                cache[path] = app.add_static_file(local_file=path, max_cache_age=0)
            except Exception:  # noqa: BLE001 - 缩略图不可用时退化为图标
                cache[path] = ""
        return cache[path]

    def _screenshot_card(
        self, version_id: str, shot: dict, reload_shots: Callable[[], None]
    ) -> None:
        with ui.column().classes("aml-card flat p-2 gap-1 min-w-0"):
            url = self._screenshot_url(str(shot["path"]))
            if url:
                ui.image(url).props("fit=cover").classes("w-full h-[110px] rounded-md")
            else:
                with ui.element("div").classes(
                    "w-full h-[110px] rounded-md flex items-center justify-center"
                ).style("background: #eef0f3"):
                    ui.icon("image").classes("text-[32px]").style("color: var(--aml-muted)")
            with ui.row().classes("w-full items-center justify-between no-wrap gap-1"):
                with ui.column().classes("min-w-0 gap-0"):
                    ui.label(shot["name"]).classes("text-[12px] truncate")
                    ui.label(_fmt_size(shot.get("size") or 0)).classes("aml-sub")
                ui.button(
                    icon="delete",
                    on_click=lambda _=None, s=shot: self._confirm_delete_screenshot(
                        version_id, s, reload_shots
                    ),
                ).props("flat dense round size=sm color=negative")

    def _open_screenshots_dir(self, version_id: str) -> None:
        path = self.service.screenshots_dir(version_id)
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
            self.service.open_path(path)
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"打开目录失败：{exc}", type="negative", position="top-right")

    def _confirm_delete_screenshot(
        self, version_id: str, shot: dict, reload_shots: Callable[[], None]
    ) -> None:
        def builder() -> None:
            ui.label(f"确定删除截图 {shot['name']}？").classes("text-[15px] font-semibold")
            ui.label("图片文件会被永久删除。").classes("aml-sub")

            async def do_delete() -> None:
                try:
                    await run.io_bound(
                        self.service.delete_screenshot, shot["name"], version_id
                    )
                except Exception as exc:  # noqa: BLE001
                    ui.notify(f"删除失败：{exc}", type="negative", position="top-right")
                    return
                self.dialog.close()
                reload_shots()
                ui.notify("截图已删除", position="top-right")

            ui.button("确认删除", icon="delete", on_click=do_delete).props(
                "unelevated dense no-caps color=negative"
            )

        self.open_dialog("删除截图", builder)

    # ------------------------------------------------------------ 版本：存档
    def _build_version_saves(self, version_id: str) -> None:
        safe_id = "".join(ch if ch.isalnum() else "-" for ch in version_id)
        save_list = None

        async def refresh_saves() -> None:
            save_list.clear()
            with save_list:
                with ui.row().classes("w-full items-center justify-center p-4"):
                    ui.spinner(size="sm").style("color: var(--aml-accent)")
            try:
                saves = await run.io_bound(self.service.list_saves, version_id)
            except Exception as exc:  # noqa: BLE001
                saves = []
                ui.notify(f"读取存档失败：{exc}", type="negative", position="top-right")
            save_list.clear()
            with save_list:
                if not saves:
                    ui.label("暂无存档").classes("aml-sub")
                for save in saves:
                    with ui.row().classes(
                        "w-full items-center gap-3 no-wrap px-2 py-2 rounded-lg"
                    ).style("background: var(--aml-surface-2)"):
                        with ui.element("div").classes(
                            "w-[36px] h-[36px] rounded-md flex items-center justify-center shrink-0"
                        ).style("background: #eef0f3"):
                            ui.icon("save").classes("text-[20px]").style(
                                "color: var(--aml-muted)"
                            )
                        with ui.column().classes("flex-1 min-w-0 gap-0.5"):
                            ui.label(save["name"]).classes("text-[13px] font-medium truncate")
                            ui.label(
                                f"{_fmt_size(save.get('size') or 0)} · "
                                f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(save.get('modified') or 0))}"
                            ).classes("aml-sub")
                        with ui.row().classes("gap-1 shrink-0 no-wrap"):
                            ui.button(
                                "备份", icon="archive",
                                on_click=lambda _=None, n=save["name"]: background_tasks.create(
                                    self._backup_save(version_id, n)
                                ),
                            ).props("flat dense no-caps")
                            ui.button(
                                "删除",
                                on_click=lambda _=None, n=save["name"]: self._confirm_delete_save(
                                    version_id, n, reload_saves
                                ),
                            ).props("flat dense no-caps color=negative")

        def reload_saves() -> None:
            background_tasks.create(refresh_saves())

        async def on_save_files(event) -> None:
            files = list(getattr(event, "files", []) or [])
            if not files:
                return
            file = files[0]
            filename = Path(file.name).name
            drop.busy(f"正在导入 {filename}…")
            with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as handle:
                temp_path = Path(handle.name)
            try:
                await self._save_upload(file, temp_path)
                await run.io_bound(self.service.import_save, str(temp_path), version_id)
            except Exception as exc:  # noqa: BLE001
                ui.notify(f"导入存档失败：{exc}", type="negative", position="top-right")
            else:
                ui.notify(f"已导入存档 {Path(filename).stem}", type="positive", position="top-right")
                reload_saves()
            finally:
                temp_path.unlink(missing_ok=True)
                drop.idle()

        with ui.column().classes("aml-card flat w-full p-4 gap-2"):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                ui.label("存档管理").classes("text-[14px] font-semibold")
                ui.button(
                    "打开目录", icon="folder_open",
                    on_click=lambda: self._open_saves_dir(version_id),
                ).props("flat dense no-caps")
            drop = ImportDrop(
                upload_id=f"aml-save-upload-{safe_id}",
                hint="点击选择或拖入存档 zip（解压到 saves/<文件名>）",
                handler=on_save_files,
                multiple=False,
            ).build()
            save_list = ui.column().classes("w-full gap-1")

        background_tasks.create(refresh_saves())

    def _open_saves_dir(self, version_id: str) -> None:
        base = self.service.paths().version_game_dir(
            version_id, isolated=self.service._is_isolated(version_id)
        )
        try:
            self.service.open_path(str(base / "saves"))
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"打开目录失败：{exc}", type="negative", position="top-right")

    async def _backup_save(self, version_id: str, name: str) -> None:
        try:
            result = await run.io_bound(self.service.backup_save, name, version_id)
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"备份失败：{exc}", type="negative", position="top-right")
            return
        ui.notify(f"已备份到 {result['path']}", type="positive", position="top-right")

    def _confirm_delete_save(
        self, version_id: str, name: str, refresh: Callable[[], None]
    ) -> None:
        def builder() -> None:
            ui.label(f"确定删除存档 {name}？").classes("text-[15px] font-semibold")
            ui.label("存档目录会被永久删除。").classes("aml-sub")

            async def do_delete() -> None:
                try:
                    await run.io_bound(self.service.delete_save, name, version_id)
                except Exception as exc:  # noqa: BLE001
                    ui.notify(f"删除失败：{exc}", type="negative", position="top-right")
                    return
                self.dialog.close()
                refresh()
                ui.notify(f"已删除存档 {name}", position="top-right")

            ui.button("确认删除", icon="delete", on_click=do_delete).props(
                "unelevated dense no-caps color=negative"
            )

        self.open_dialog("删除存档", builder)

    # ------------------------------------------------------------ 版本：服务器
    def _build_version_servers(self, version_id: str) -> None:
        server_list = None

        async def refresh_servers() -> None:
            server_list.clear()
            try:
                servers = await run.io_bound(self.service.list_servers, version_id)
            except Exception as exc:  # noqa: BLE001
                servers = []
                ui.notify(f"读取服务器失败：{exc}", type="negative", position="top-right")
            server_list.clear()
            with server_list:
                if not servers:
                    ui.label("暂无服务器").classes("aml-sub")
                for index, server in enumerate(servers):
                    with ui.row().classes(
                        "w-full items-center gap-3 no-wrap px-2 py-2 rounded-lg"
                    ).style("background: var(--aml-surface-2)"):
                        with ui.element("div").classes(
                            "w-[36px] h-[36px] rounded-md flex items-center justify-center shrink-0"
                        ).style("background: #eef0f3"):
                            ui.icon("dns").classes("text-[20px]").style(
                                "color: var(--aml-muted)"
                            )
                        with ui.column().classes("flex-1 min-w-0 gap-0.5"):
                            ui.label(server.get("name") or server.get("ip")).classes(
                                "text-[13px] font-medium truncate"
                            )
                            ui.label(server.get("ip") or "").classes("aml-sub truncate")
                        ui.button(
                            "删除",
                            on_click=lambda _=None, i=index: self._confirm_delete_server(
                                version_id, i, reload_servers
                            ),
                        ).props("flat dense no-caps color=negative")

        def reload_servers() -> None:
            background_tasks.create(refresh_servers())

        with ui.column().classes("aml-card flat w-full p-4 gap-2"):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                ui.label("服务器管理").classes("text-[14px] font-semibold")
                ui.button(
                    "添加服务器", icon="add",
                    on_click=lambda: self._open_add_server(version_id, reload_servers),
                ).props("unelevated dense no-caps color=primary")
            ui.label("写入当前游戏目录的 servers.dat，启动游戏后可在多人游戏中看到").classes(
                "aml-sub"
            )
            server_list = ui.column().classes("w-full gap-1")

        background_tasks.create(refresh_servers())

    def _open_add_server(self, version_id: str, refresh: Callable[[], None]) -> None:
        def builder() -> None:
            name = ui.input(label="名称（可留空）").classes("w-full").props("dense outlined")
            address = ui.input(label="服务器地址", value="localhost:25565").classes(
                "w-full"
            ).props("dense outlined")

            async def do_add() -> None:
                try:
                    await run.io_bound(
                        self.service.add_server,
                        name.value or "",
                        address.value or "",
                        version_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    ui.notify(f"添加失败：{exc}", type="negative", position="top-right")
                    return
                self.dialog.close()
                refresh()
                ui.notify("服务器已添加", type="positive", position="top-right")

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("取消", on_click=self.dialog.close).props("flat dense no-caps")
                ui.button("添加", icon="add", on_click=do_add).props(
                    "unelevated dense no-caps color=primary"
                )

        self.open_dialog("添加服务器", builder)

    def _confirm_delete_server(
        self, version_id: str, index: int, refresh: Callable[[], None]
    ) -> None:
        def builder() -> None:
            ui.label("确定删除这个服务器？").classes("text-[15px] font-semibold")

            async def do_delete() -> None:
                try:
                    await run.io_bound(self.service.delete_server, index, version_id)
                except Exception as exc:  # noqa: BLE001
                    ui.notify(f"删除失败：{exc}", type="negative", position="top-right")
                    return
                self.dialog.close()
                refresh()
                ui.notify("服务器已删除", position="top-right")

            ui.button("确认删除", icon="delete", on_click=do_delete).props(
                "unelevated dense no-caps color=negative"
            )

        self.open_dialog("删除服务器", builder)

    # ------------------------------------------------------------ 版本：启动设置
    def _build_version_settings(self, version_id: str) -> None:
        settings = self.service.version_settings(version_id)
        glob = settings["global"]
        override = settings["override"]

        with ui.column().classes("aml-card flat w-full p-4 gap-2"):
            ui.label("启动设置").classes("text-[14px] font-semibold")
            ui.label("默认值在「设置 → 游戏设置」中配置；这里留空或选「跟随全局」即使用默认值").classes(
                "aml-sub"
            )

            isolated = ui.select(
                {
                    "": f"跟随全局（当前{'开启' if glob['isolated'] else '关闭'}）",
                    "on": "开启版本隔离",
                    "off": "关闭版本隔离",
                },
                value=("on" if override.get("isolated") is True else "off" if override.get("isolated") is False else ""),
                label="版本隔离（独立 mods / 存档 / 配置）",
            ).classes("w-full").props("dense outlined")

            java = ui.input(
                label="Java 可执行文件",
                value=override.get("java", ""),
                placeholder=f"跟随全局：{glob['java']}",
            ).classes("w-full").props("dense outlined")

            with ui.row().classes("w-full gap-3 no-wrap"):
                memory = ui.number(
                    label=f"最大内存 (MB，全局 {glob['memory_mb']})",
                    value=override.get("memory_mb"),
                    min=512, max=65536, step=512,
                ).classes("flex-1").props("dense outlined clearable")
                min_memory = ui.number(
                    label=f"最小内存 (MB，全局 {glob['min_memory_mb']})",
                    value=override.get("min_memory_mb"),
                    min=256, max=32768, step=256,
                ).classes("flex-1").props("dense outlined clearable")
            with ui.row().classes("w-full gap-3 no-wrap"):
                width = ui.number(
                    label=f"窗口宽（全局 {glob['width']}）",
                    value=override.get("width"), min=320, max=7680,
                ).classes("flex-1").props("dense outlined")
                height = ui.number(
                    label=f"窗口高（全局 {glob['height']}）",
                    value=override.get("height"), min=240, max=4320,
                ).classes("flex-1").props("dense outlined")
            extra_jvm = ui.input(
                label="额外 JVM 参数（空格分隔）", value=" ".join(override.get("extra_jvm") or [])
            ).classes("w-full").props("dense outlined")
            extra_game = ui.input(
                label="额外游戏参数（空格分隔）", value=" ".join(override.get("extra_game") or [])
            ).classes("w-full").props("dense outlined")

            def save_settings() -> None:
                changes = {
                    "isolated": {"": None, "on": True, "off": False}[isolated.value or ""],
                    "java": (java.value or "").strip(),
                    "memory_mb": int(memory.value) if memory.value else None,
                    "min_memory_mb": int(min_memory.value) if min_memory.value else None,
                    "width": int(width.value) if width.value else None,
                    "height": int(height.value) if height.value else None,
                    "extra_jvm": (extra_jvm.value or "").split(),
                    "extra_game": (extra_game.value or "").split(),
                }
                try:
                    self.service.update_version_settings(version_id, changes)
                except Exception as exc:  # noqa: BLE001
                    ui.notify(f"保存失败：{exc}", type="negative", position="top-right")
                    return
                ui.notify("版本设置已保存", type="positive", position="top-right")
                self._reload_version_list()

            ui.button("保存版本设置", icon="save", on_click=save_settings).props(
                "unelevated dense no-caps color=primary"
            ).classes("self-start")

    # ------------------------------------------------------------ 版本：加载器
    def _build_version_loader(self, version_id: str) -> None:
        base_version = self.service.version_profile(version_id)["game_version"]
        with ui.column().classes("aml-card flat w-full p-4 gap-2"):
            ui.label("安装模组加载器").classes("text-[14px] font-semibold")
            with ui.row().classes("w-full items-end gap-3 no-wrap"):
                loader = ui.select(
                    {key: LOADER_LABELS[key] for key in LOADERS},
                    value="fabric",
                    label="加载器",
                ).classes("w-[130px]").props("dense outlined")
                game_version = ui.input(label="原版版本", value=base_version).classes(
                    "w-[140px]"
                ).props("dense outlined")
                loader_versions = ui.select(options=[], label="加载器版本", with_input=True).classes(
                    "flex-1"
                ).props("dense outlined")

            async def fetch_loader_versions() -> None:
                target = (game_version.value or "").strip()
                if not target:
                    ui.notify("请先填写原版版本", type="warning", position="top-right")
                    return
                try:
                    # 联网请求放到后台线程，避免拉取列表时界面卡住。
                    items = await run.io_bound(
                        self.service.loader_versions, loader.value, target
                    )
                except Exception as exc:  # noqa: BLE001
                    ui.notify(f"获取加载器版本失败：{exc}", type="negative", position="top-right")
                    return
                loader_versions.options = [item["version"] for item in items]
                stable = [item["version"] for item in items if item.get("stable")]
                loader_versions.value = (stable or loader_versions.options or [None])[0]
                loader_versions.update()

            with ui.row().classes("gap-2 no-wrap"):
                ui.button("获取版本列表", on_click=fetch_loader_versions).props("flat dense no-caps")
                ui.button(
                    "安装加载器",
                    on_click=lambda: self._install_loader(
                        loader.value, game_version.value, loader_versions.value
                    ),
                ).props("unelevated dense no-caps color=primary")

    def _install_loader(self, loader: str, game_version: str, loader_version: Optional[str]) -> None:
        if not (loader and game_version and loader_version):
            ui.notify("请先获取并选择加载器版本", type="warning", position="top-right")
            return
        self.service.start_loader_install(loader, game_version.strip(), loader_version)
        self._select_view("tasks")
        ui.notify(f"已开始安装 {loader} {loader_version}，进度见「任务」页", position="top-right")

    # ------------------------------------------------------------ 版本：崩溃诊断
    def _build_version_crash(self, version_id: str) -> None:
        with ui.column().classes("aml-card flat w-full p-4 gap-2"):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                ui.label("崩溃诊断").classes("text-[14px] font-semibold")
                ui.button(
                    "分析最近日志", icon="bug_report",
                    on_click=lambda: run_crash(),
                ).props("flat dense no-caps")
            crash_box = ui.column().classes("w-full gap-2")

        async def run_crash() -> None:
            crash_box.clear()
            with crash_box:
                ui.spinner(size="sm").style("color: var(--aml-accent)")
            try:
                # 读取崩溃报告 / latest.log 可能较大，放到后台线程避免界面卡顿。
                report = await run.io_bound(self.service.analyze_crash, version_id)
            except Exception as exc:  # noqa: BLE001
                crash_box.clear()
                with crash_box:
                    ui.label(f"诊断失败：{exc}").classes("aml-sub")
                return
            crash_box.clear()
            with crash_box:
                self._render_crash(report)

    def _render_crash(self, report: dict) -> None:
        causes = report.get("causes") or []
        if not causes:
            ui.label(report.get("summary") or "未发现明显问题").classes("aml-sub")
        for cause in causes:
            severity = cause.get("severity") or "low"
            with ui.column().classes(
                "aml-cause w-full gap-0.5 "
                + ("high" if severity == "high" else "medium" if severity == "medium" else "")
            ):
                with ui.row().classes("items-center gap-2 no-wrap"):
                    ui.label(cause.get("title") or cause.get("code")).classes("text-[13px] font-medium")
                    ui.label(SEVERITY_LABELS.get(severity, severity)).classes(
                        "aml-chip warn" if severity != "low" else "aml-chip"
                    )
                ui.label(cause.get("suggestion") or "").classes("aml-sub")
                if cause.get("evidence"):
                    evidence = cause["evidence"]
                    text = "\n".join(evidence) if isinstance(evidence, list) else str(evidence)
                    ui.label(text).classes("aml-sub").style(
                        "font-family: monospace; white-space: pre-wrap;"
                    )
        if report.get("sources"):
            ui.label("来源：" + "，".join(report["sources"])).classes("aml-sub")

    # ==================================================================== 下载
    def _build_download_page(self) -> None:
        with ui.column().classes("w-full h-full p-5 gap-3 aml-scroll") as page:
            self.pages["download"] = page
            page.props("id=aml-page-download")
            page.set_visibility(False)

            with ui.row().classes("w-full items-center justify-between no-wrap"):
                with ui.column().classes("gap-0.5"):
                    ui.label("下载").classes("text-[20px] font-semibold")
                    ui.label("选择本地版本后自动筛选兼容资源，并自动选择下载目录").classes("aml-sub")
                ui.button(
                    "任务", icon="task_alt",
                    on_click=lambda: self._select_view("tasks"),
                ).props("flat dense no-caps").classes("aml-topnav")

            self.download_type_toggle = ui.toggle(
                DOWNLOAD_TYPES, value="game", on_change=lambda e: self._on_download_type(e.value)
            ).props("unelevated dense no-caps toggle-color=primary")

            self.download_bar = ui.row().classes(
                "aml-card flat w-full items-center gap-3 p-3 no-wrap"
            )
            self.download_body = ui.column().classes("w-full gap-2").props(
                "id=aml-download-body"
            )

    def _on_download_type(self, value: str) -> None:
        self.download_type = value or "game"
        self._refresh_download_bar()

    def _toggle_snapshots(self, event) -> None:
        try:
            self.service.update_config({"include_snapshots": bool(event.value)})
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"保存失败：{exc}", type="negative", position="top-right")
        # 设置页有同名开关，保持同步，避免之后保存设置时把这里的修改覆盖回去。
        if hasattr(self, "set_include_snapshots"):
            self.set_include_snapshots.value = bool(event.value)
            self.set_include_snapshots.update()
        self._build_download_body()

    def _sync_download_versions(self) -> None:
        """回到下载页时只同步版本下拉框，保留已有搜索结果。"""
        if self.download_type not in CONTENT_LABELS or self.download_type == "modpack":
            return
        if not hasattr(self, "dl_version"):
            return
        versions = [v["id"] for v in self.service.local_versions()]
        current = self.dl_version_value if self.dl_version_value in versions else (
            versions[0] if versions else None
        )
        self.dl_version.options = versions
        self.dl_version.value = current
        self.dl_version.update()
        self.dl_version_value = current
        self._update_folder_hint()

    def _refresh_download_bar(self) -> None:
        self.download_bar.clear()
        kind = self.download_type
        versions = [v["id"] for v in self.service.local_versions()]

        with self.download_bar:
            if kind == "game":
                ui.label("安装官方游戏版本（含快照可选）").classes("aml-sub flex-1")
                self.snapshot_switch = ui.switch(
                    "包含快照", value=self.service.config.include_snapshots,
                    on_change=self._toggle_snapshots,
                ).props("dense")
                ui.button("刷新", icon="refresh", on_click=self._build_download_body).props(
                    "flat dense no-caps"
                )
            elif kind == "modpack":
                ui.label("整合包无需筛选，按下包内游戏版本与加载器安装").classes("aml-sub flex-1")
                ui.label(f"归档目录：{self.service.modpack_dir()}").classes("aml-sub")
            else:
                current = self.dl_version_value
                if current not in versions:
                    current = self.service.config.last_version if self.service.config.last_version in versions else (
                        versions[0] if versions else None
                    )
                self.dl_version_value = current
                self.dl_version = ui.select(
                    options=versions, value=current, label="目标版本",
                    on_change=lambda e: self._on_download_version(e.value),
                ).classes("w-[200px]").props("dense outlined")
                self.dl_folder = ui.label("").classes("aml-sub flex-1")
                ui.button(icon="refresh", on_click=self._refresh_download_bar).props(
                    "flat dense round size=sm"
                )

        self._build_download_body()
        if kind in CONTENT_LABELS and kind != "modpack":
            self._update_folder_hint()

    def _on_download_version(self, value: Optional[str]) -> None:
        self.dl_version_value = value
        self._update_folder_hint()
        # 版本变了，搜索条件（游戏版本 / 加载器）也要跟着变。
        if self.download_type in CONTENT_LABELS and self.download_type != "modpack":
            self._build_download_body()

    def _selected_download_version(self) -> Optional[str]:
        return self.dl_version_value

    def _update_folder_hint(self) -> None:
        version_id = self._selected_download_version()
        kind = self.download_type
        if not hasattr(self, "dl_folder"):
            return
        if not version_id:
            self.dl_folder.text = "未选择版本"
            return
        folder = self.service.content_manager().dir_for(
            kind, version_id=version_id, isolated=self.service._is_isolated(version_id)
        )
        self.dl_folder.text = f"下载目录：{folder}"

    def _build_download_body(self) -> None:
        self.download_body.clear()
        kind = self.download_type
        with self.download_body:
            if kind == "game":
                self._build_game_download()
            elif kind == "modpack":
                self._build_modpack_download()
            else:
                self._build_content_download(kind)
        self._play("aml-download-body")

    # ------------------------------------------------------------ 下载：游戏本体
    def _build_game_download(self) -> None:
        include_snapshots = bool(getattr(self, "snapshot_switch", None).value) if hasattr(
            self, "snapshot_switch"
        ) else False
        version_box = ui.column().classes("w-full gap-2")
        with version_box:
            with ui.row().classes("w-full items-center justify-center p-6"):
                ui.spinner(size="lg").style("color: var(--aml-accent)")
        background_tasks.create(self._load_game_versions(version_box, include_snapshots))

    async def _load_game_versions(self, box, include_snapshots: bool) -> None:
        try:
            versions = await run.io_bound(self.service.list_versions, include_snapshots)
        except Exception as exc:  # noqa: BLE001
            versions = []
            ui.notify(f"获取版本列表失败：{exc}", type="negative", position="top-right")

        if self.download_type != "game":
            return
        try:
            box.clear()
        except Exception:  # noqa: BLE001 - 切换下载类型时容器可能已被移除
            return
        with box:
            if not versions:
                ui.label("暂无版本数据").classes("aml-sub p-2")
            for index, version in enumerate(versions):
                with ui.row().classes(
                    "aml-card aml-fade w-full items-center justify-between no-wrap px-4 py-2"
                ).style(f"animation-delay: {min(index * 8, 200)}ms"):
                    with ui.column().classes("gap-0.5 min-w-0"):
                        with ui.row().classes("items-center gap-2 no-wrap"):
                            ui.label(version["id"]).classes("text-[14px] font-medium")
                            ui.label(version["type"]).classes("aml-chip")
                            if version.get("installed"):
                                ui.label("已安装").classes("aml-chip installed")
                        ui.label((version.get("release_time") or "")[:10]).classes("aml-sub")
                    if not version.get("installed"):
                        ui.button(
                            "安装",
                            on_click=lambda _=None, v=version: self._open_install_dialog(v["id"]),
                        ).props("unelevated dense no-caps color=primary").classes("shrink-0")

    def _unique_version_name(self, base: str) -> str:
        name = _sanitize_version_name(base) or "version"
        used = set(self.service.installed_versions())
        if name not in used:
            return name
        index = 2
        while f"{name}-{index}" in used:
            index += 1
        return f"{name}-{index}"

    def _open_install_dialog(self, version_id: str) -> None:
        default_name = self._unique_version_name(version_id)

        def builder() -> None:
            ui.label(
                "可自定义版本名称，安装到独立目录；同一原版版本可以共存多份。"
            ).classes("aml-sub")
            name = ui.input(label="版本名称", value=default_name).classes("w-full").props(
                "dense outlined"
            )
            loader = ui.select(
                {"": "原版（不安装加载器）", **{key: LOADER_LABELS[key] for key in LOADERS}},
                value="",
                label="模组加载器",
            ).classes("w-full").props("dense outlined")
            loader_version = ui.select(
                options=[], label="加载器版本", with_input=True
            ).classes("w-full").props("dense outlined")

            async def refresh_loader_versions() -> None:
                if not loader.value:
                    loader_version.options = []
                    loader_version.value = None
                    loader_version.update()
                    return
                try:
                    items = await run.io_bound(
                        self.service.loader_versions, loader.value, version_id
                    )
                except Exception as exc:  # noqa: BLE001
                    ui.notify(
                        f"获取加载器版本失败：{exc}", type="negative", position="top-right"
                    )
                    return
                options = [item["version"] for item in items]
                stable = [item["version"] for item in items if item.get("stable")]
                loader_version.options = options
                loader_version.value = (stable or options or [None])[0]
                loader_version.update()

            loader.on_value_change(
                lambda _=None: background_tasks.create(refresh_loader_versions())
            )

            def start() -> None:
                target = _sanitize_version_name((name.value or "").strip()) or default_name
                if target in self.service.installed_versions():
                    ui.notify(f"版本名 {target} 已存在，请换一个名称", type="warning", position="top-right")
                    return
                chosen = loader.value or ""
                chosen_version = (loader_version.value or "") if chosen else ""
                if chosen and not chosen_version:
                    ui.notify("请选择加载器版本", type="warning", position="top-right")
                    return
                self.dialog.close()
                try:
                    self.service.start_install(
                        version_id,
                        name=target,
                        loader=chosen,
                        loader_version=chosen_version,
                    )
                except Exception as exc:  # noqa: BLE001
                    ui.notify(f"开始安装失败：{exc}", type="negative", position="top-right")
                    return
                self._select_view("tasks")
                ui.notify(f"已开始安装 {target}，进度见「任务」页", position="top-right")

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("取消", on_click=self.dialog.close).props("flat dense no-caps")
                ui.button(
                    "开始安装", icon="download", on_click=start
                ).props("unelevated dense no-caps color=primary")

        self.open_dialog(f"安装 {version_id}", builder)

    # ------------------------------------------------- 下载：Mod / 资源包 / 光影
    def _build_content_download(self, kind: str) -> None:
        version_id = self._selected_download_version()
        if not version_id:
            ui.label("请先安装并在上方选择本地游戏版本").classes("aml-sub p-2")
            return
        profile = self.service.version_profile(version_id)
        # OptiFine 不是模组加载器，按没加载器处理，避免向 Modrinth 传不存在的分类。
        loader = (
            profile["loader"]
            if profile["loader"] not in ("vanilla", "optifine")
            else None
        )

        # CurseForge 只支持 Mod 与整合包检索，其它类型只提供 Modrinth。
        source_options = {"modrinth": "Modrinth"}
        if kind == "mod":
            source_options["curseforge"] = "CurseForge"
        default_source = (
            self.service.config.mod_source
            if self.service.config.mod_source in source_options
            else "modrinth"
        )
        with ui.row().classes("aml-card flat w-full items-center gap-3 p-3 no-wrap"):
            self.content_source = ui.select(
                source_options,
                value=default_source,
                label="来源",
            ).classes("w-[130px]").props("dense outlined")
            self.content_query = ui.input(placeholder="搜索关键词").classes("flex-1").props(
                "dense outlined"
            )
            self.content_query.on(
                "keydown.enter",
                lambda: background_tasks.create(self._search_content(kind, profile.get("game_version"), loader)),
            )
            ui.button(
                "搜索", icon="search",
                on_click=lambda: background_tasks.create(
                    self._search_content(kind, profile.get("game_version"), loader)
                ),
            ).props("unelevated dense no-caps color=primary")

        ui.label(
            f"已按 {version_id} 自动筛选：游戏版本 {profile.get('game_version')}"
            + (f" · 加载器 {LOADER_LABELS.get(loader, loader)}" if loader else " · 原版")
        ).classes("aml-sub")
        if kind == "mod" and loader is None:
            ui.label(
                "当前版本未安装模组加载器，下载的 Mod 需要先在「版本 → 版本设置」中安装 Fabric / Forge 等加载器才能生效"
            ).classes("aml-sub").style("color: #b45309")

        self.content_results = ui.column().classes("w-full gap-2")
        with self.content_results:
            ui.label("输入关键词开始搜索").classes("aml-sub p-2")

    async def _search_content(self, kind: str, game_version: Optional[str], loader: Optional[str]) -> None:
        query = (self.content_query.value or "").strip()
        if not query:
            ui.notify("请输入搜索关键词", type="warning", position="top-right")
            return
        self.content_results.clear()
        with self.content_results:
            with ui.row().classes("w-full items-center justify-center p-6"):
                ui.spinner(size="lg").style("color: var(--aml-accent)")
        try:
            results = await run.io_bound(
                self.service.search_content, kind, query,
                source=self.content_source.value,
                game_version=game_version,
                loader=loader,
                limit=20,
            )
        except Exception as exc:  # noqa: BLE001
            results = []
            ui.notify(f"搜索失败：{exc}", type="negative", position="top-right")

        # 搜索期间用户可能已切换下载类型，结果不再适用。
        if self.download_type != kind:
            return
        self.content_results.clear()
        with self.content_results:
            if not results:
                ui.label("没有找到匹配的结果").classes("aml-sub p-2")
            for index, item in enumerate(results):
                self._content_row(item, index, kind, game_version, loader)

    def _content_row(self, item: dict, index: int, kind: str, game_version: Optional[str], loader: Optional[str]) -> None:
        with ui.row().classes(
            "aml-card aml-fade w-full items-start gap-3 p-3 no-wrap"
        ).style(f"animation-delay: {min(index * 10, 200)}ms"):
            if item.get("icon_url"):
                ui.image(item["icon_url"]).classes("w-[44px] h-[44px] rounded-md shrink-0 object-cover")
            else:
                ui.icon("extension").classes("text-[32px] shrink-0").style("color: var(--aml-muted)")
            with ui.column().classes("flex-1 min-w-0 gap-0.5"):
                with ui.row().classes("items-center gap-2 no-wrap"):
                    ui.label(item["name"]).classes("text-[14px] font-medium")
                    ui.label("CurseForge" if item["source"] == "curseforge" else "Modrinth").classes("aml-chip")
                ui.label(item.get("description") or "").classes("aml-sub w-full").style(
                    "display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;"
                )
                ui.label(
                    f"{item.get('author') or '未知作者'} · 下载 {_fmt_count(int(item.get('downloads') or 0))}"
                ).classes("aml-sub")
            ui.button(
                "下载",
                on_click=lambda _=None, i=item: self._install_content(
                    i, kind, game_version, loader
                ),
            ).props("unelevated dense no-caps color=primary").classes("shrink-0 mt-1")

    def _install_content(self, item: dict, kind: str, game_version: Optional[str], loader: Optional[str]) -> None:
        version_id = self._selected_download_version()
        self.service.start_content_install(
            kind,
            item["source"],
            item["project_id"],
            name=item["name"],
            game_version=game_version,
            loader=loader,
            version_id=version_id,
        )
        self._select_view("tasks")
        ui.notify(f"已开始下载 {item['name']}，进度见「任务」页", position="top-right")
        if kind == "mod":
            background_tasks.create(
                self._prompt_mod_dependencies(item, game_version, loader, version_id)
            )

    async def _prompt_mod_dependencies(
        self,
        item: dict,
        game_version: Optional[str],
        loader: Optional[str],
        version_id: Optional[str],
    ) -> None:
        """下载开始后检查前置 Mod，缺失时弹窗提供一键安装。"""
        try:
            deps = await run.io_bound(
                lambda: self.service.mod_dependencies(
                    item["source"],
                    item["project_id"],
                    game_version=game_version,
                    loader=loader,
                )
            )
            installed = {
                str(entry.get("filename") or "").lower()
                for entry in await run.io_bound(self.service.content_list, "mod", version_id)
            }
        except Exception:  # noqa: BLE001 - 依赖查询失败不阻塞主下载
            return
        missing = [
            dep
            for dep in deps
            if str(dep.get("filename") or "").lower() not in installed
        ]
        if not missing:
            return

        def builder() -> None:
            ui.label(f"{item['name']} 需要以下前置 Mod").classes("text-[15px] font-semibold")
            ui.label("已自动匹配当前游戏版本与加载器；一键安装会下载并放入 mods 目录。").classes(
                "aml-sub"
            )
            with ui.column().classes("w-full gap-1"):
                for dep in missing:
                    with ui.row().classes(
                        "w-full items-center gap-2 no-wrap px-2 py-1.5 rounded-lg"
                    ).style("background: var(--aml-surface-2)"):
                        if dep.get("icon_url"):
                            ui.image(dep["icon_url"]).classes(
                                "w-[28px] h-[28px] rounded-md shrink-0 object-cover"
                            )
                        else:
                            ui.icon("extension").classes("text-[20px] shrink-0").style(
                                "color: var(--aml-muted)"
                            )
                        ui.label(dep["name"]).classes("text-[13px] flex-1 truncate")
                        ui.label(
                            "CurseForge" if dep["source"] == "curseforge" else "Modrinth"
                        ).classes("aml-chip")

            async def install_all() -> None:
                errors: list[str] = []
                for dep in missing:
                    try:
                        self.service.start_content_install(
                            "mod",
                            dep["source"],
                            dep["project_id"],
                            name=dep["name"],
                            game_version=game_version,
                            loader=loader,
                            version_id=version_id,
                        )
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{dep['name']}：{exc}")
                self.dialog.close()
                self._select_view("tasks")
                if errors:
                    ui.notify("；".join(errors[:3]), type="negative", position="top-right")
                else:
                    ui.notify(f"已开始安装 {len(missing)} 个前置 Mod", position="top-right")

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("跳过", on_click=self.dialog.close).props("flat dense no-caps")
                ui.button("一键安装前置", icon="download", on_click=install_all).props(
                    "unelevated dense no-caps color=primary"
                )

        self.open_dialog("安装前置 Mod", builder)

    # ------------------------------------------------------------ 下载：整合包
    def _build_modpack_download(self) -> None:
        with ui.row().classes("aml-card flat w-full items-center gap-3 p-3 no-wrap"):
            self.pack_source = ui.select(
                {"modrinth": "Modrinth", "curseforge": "CurseForge"},
                value=self.service.config.mod_source, label="来源",
            ).classes("w-[130px]").props("dense outlined")
            self.pack_query = ui.input(placeholder="搜索整合包").classes("flex-1").props(
                "dense outlined"
            )
            self.pack_query.on(
                "keydown.enter", lambda: background_tasks.create(self._search_modpacks())
            )
            ui.button(
                "搜索", icon="search",
                on_click=lambda: background_tasks.create(self._search_modpacks()),
            ).props("unelevated dense no-caps color=primary")

        with ui.column().classes("aml-card flat w-full p-4 gap-2"):
            ui.label("导入本地整合包").classes("text-[14px] font-semibold")

            async def on_pack_files(event) -> None:
                await self._handle_modpack_files(event, drop)

            drop = ImportDrop(
                upload_id="aml-modpack-upload",
                hint="点击选择或拖入整合包（.mrpack / .zip）",
                handler=on_pack_files,
                multiple=False,
            ).build()

        self.modpack_results = ui.column().classes("w-full gap-2")
        with self.modpack_results:
            ui.label("输入关键词搜索在线整合包").classes("aml-sub p-2")

    async def _search_modpacks(self) -> None:
        query = (self.pack_query.value or "").strip()
        if not query:
            ui.notify("请输入搜索关键词", type="warning", position="top-right")
            return
        self.modpack_results.clear()
        with self.modpack_results:
            with ui.row().classes("w-full items-center justify-center p-6"):
                ui.spinner(size="lg").style("color: var(--aml-accent)")
        try:
            results = await run.io_bound(
                self.service.search_content, "modpack", query,
                source=self.pack_source.value, limit=20,
            )
        except Exception as exc:  # noqa: BLE001
            results = []
            ui.notify(f"搜索失败：{exc}", type="negative", position="top-right")

        if self.download_type != "modpack":
            return
        self.modpack_results.clear()
        with self.modpack_results:
            if not results:
                ui.label("没有找到匹配的整合包").classes("aml-sub p-2")
            for index, item in enumerate(results):
                self._modpack_row(item, index)

    def _modpack_row(self, item: dict, index: int) -> None:
        with ui.row().classes(
            "aml-card aml-fade w-full items-start gap-3 p-3 no-wrap"
        ).style(f"animation-delay: {min(index * 10, 200)}ms"):
            if item.get("icon_url"):
                ui.image(item["icon_url"]).classes("w-[44px] h-[44px] rounded-md shrink-0 object-cover")
            else:
                ui.icon("inventory_2").classes("text-[32px] shrink-0").style("color: var(--aml-muted)")
            with ui.column().classes("flex-1 min-w-0 gap-0.5"):
                with ui.row().classes("items-center gap-2 no-wrap"):
                    ui.label(item["name"]).classes("text-[14px] font-medium")
                    ui.label("CurseForge" if item["source"] == "curseforge" else "Modrinth").classes("aml-chip")
                ui.label(item.get("description") or "").classes("aml-sub w-full").style(
                    "display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;"
                )
                ui.label(
                    f"下载 {_fmt_count(int(item.get('downloads') or 0))} · 无需筛选，按包内版本安装"
                ).classes("aml-sub")
            ui.button(
                "安装",
                on_click=lambda _=None, i=item: self._install_online_modpack(i),
            ).props("unelevated dense no-caps color=primary").classes("shrink-0 mt-1")

    def _install_online_modpack(self, item: dict) -> None:
        default_name = self._unique_version_name(item.get("name") or "modpack")

        def builder() -> None:
            ui.label(item.get("name") or item.get("project_id")).classes(
                "text-[15px] font-semibold"
            )
            ui.label(
                "按整合包内声明的游戏版本与加载器安装到独立版本目录，并自动开启版本隔离。"
            ).classes("aml-sub")
            name = ui.input(label="版本名称（可自定义）", value=default_name).classes(
                "w-full"
            ).props("dense outlined")

            def do_install() -> None:
                target = _sanitize_version_name((name.value or "").strip()) or default_name
                if target in self.service.installed_versions():
                    ui.notify(f"版本名 {target} 已存在，请换一个名称", type="warning", position="top-right")
                    return
                self.dialog.close()
                try:
                    self.service.start_online_modpack_install(
                        item["source"],
                        item["project_id"],
                        name=item["name"],
                        version_name=target,
                    )
                except Exception as exc:  # noqa: BLE001
                    ui.notify(f"开始安装失败：{exc}", type="negative", position="top-right")
                    return
                self._select_view("tasks")
                ui.notify(f"已开始安装 {target}，进度见「任务」页", position="top-right")

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("取消", on_click=self.dialog.close).props("flat dense no-caps")
                ui.button("确认安装", icon="download", on_click=do_install).props(
                    "unelevated dense no-caps color=primary"
                )

        self.open_dialog("安装整合包", builder)

    async def _handle_modpack_files(self, event, drop: ImportDrop) -> None:
        files = list(getattr(event, "files", []) or [])
        if not files:
            return
        file = files[0]
        filename = Path(file.name).name
        if Path(filename).suffix.lower() not in {".mrpack", ".zip"}:
            ui.notify("只支持 .mrpack / .zip 整合包", type="warning", position="top-right")
            return
        target_dir = self.service.modpack_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / filename
        # 整合包可能有几百 MB，直接用上传对象流式落盘。
        drop.busy(f"正在保存 {filename}…")
        try:
            await self._save_upload(file, target)
        except Exception as exc:  # noqa: BLE001 - 磁盘写入失败
            drop.idle()
            ui.notify(f"保存整合包失败：{exc}", type="negative", position="top-right")
            return

        drop.busy(f"正在检查 {filename}…")
        try:
            info = await run.io_bound(self.service.inspect_modpack, str(target))
        except Exception as exc:  # noqa: BLE001
            drop.idle()
            ui.notify(f"检查整合包失败：{exc}", type="negative", position="top-right")
            return

        version_id = info.get("game_version") or ""
        if not version_id:
            drop.idle()
            ui.notify("无法从整合包确定游戏版本", type="warning", position="top-right")
            return

        drop.idle()
        self._confirm_modpack_install(target, info, filename)

    def _confirm_modpack_install(
        self, archive: Path, info: dict, filename: str
    ) -> None:
        game_version = str(info.get("game_version") or "")
        default_name = self._unique_version_name(
            str(info.get("name") or Path(filename).stem or game_version)
        )
        loader = str(info.get("loader") or "")
        loader_version = str(info.get("loader_version") or "")

        def builder() -> None:
            with ui.row().classes("items-center gap-2 no-wrap"):
                ui.label(str(info.get("name") or filename)).classes("text-[15px] font-semibold")
                ui.label(str(info.get("format") or "")).classes("aml-chip")
                ui.label(game_version).classes("aml-chip primary")
                if loader:
                    ui.label(f"{loader} {loader_version}".strip()).classes("aml-chip")
            ui.label(
                f"游戏版本 {game_version or '未知'} · 文件 {info.get('file_count', 0)} 个 · "
                f"覆盖 {info.get('override_count', 0)} 个"
            ).classes("aml-sub")
            name = ui.input(label="版本名称（可自定义）", value=default_name).classes(
                "w-full"
            ).props("dense outlined")
            ui.label("整合包会安装到独立版本目录并自动开启版本隔离。").classes("aml-sub")

            def do_install() -> None:
                target = _sanitize_version_name((name.value or "").strip()) or default_name
                if target in self.service.installed_versions():
                    ui.notify(f"版本名 {target} 已存在，请换一个名称", type="warning", position="top-right")
                    return
                self.dialog.close()
                try:
                    self.service.start_modpack_install(
                        str(archive), version_id=game_version, name=target
                    )
                except Exception as exc:  # noqa: BLE001
                    ui.notify(f"开始安装失败：{exc}", type="negative", position="top-right")
                    return
                self._select_view("tasks")
                ui.notify(f"已开始安装 {target}，进度见「任务」页", position="top-right")

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("取消", on_click=self.dialog.close).props("flat dense no-caps")
                ui.button("确认安装", icon="download", on_click=do_install).props(
                    "unelevated dense no-caps color=primary"
                )

        self.open_dialog("确认安装整合包", builder)

    # ==================================================================== 任务
    def _build_tasks_page(self) -> None:
        with ui.column().classes("w-full h-full p-5 gap-3 aml-scroll") as page:
            self.pages["tasks"] = page
            page.props("id=aml-page-tasks")
            page.set_visibility(False)
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                with ui.column().classes("gap-0.5"):
                    ui.label("任务").classes("text-[20px] font-semibold")
                    ui.label("游戏 / Mod / 整合包 / Java 的下载与安装进度").classes("aml-sub")
                ui.button(
                    "返回下载", icon="arrow_back",
                    on_click=lambda: self._select_view("download"),
                ).props("flat dense no-caps")
            self.task_overview = ui.column().classes("w-full gap-2")
            self.task_list = ui.column().classes("w-full gap-2")
            with self.task_list:
                with ui.column().classes("aml-empty aml-card flat w-full"):
                    ui.icon("task_alt")
                    ui.label("暂无任务").classes("text-[14px] font-medium")
                    ui.label("去下载页开始一个吧").classes("aml-sub")

    def _poll_tasks(self) -> None:
        snapshots = self.service.tasks.list_snapshots(limit=30)
        current_ids = {snapshot["id"] for snapshot in snapshots}
        new_task = False
        for snapshot in snapshots:
            task_id = snapshot["id"]
            status = snapshot["status"]
            previous = self._task_status.get(task_id)
            if previous == status:
                continue
            self._task_status[task_id] = status
            if previous is None:
                new_task = True
                continue
            if status == "done":
                ui.notify(f"{snapshot['name']} 完成", type="positive", position="top-right")
                result = snapshot.get("result") or {}
                # 安装 / 补全 / 导入整合包完成后，自动刷新启动页与版本列表，
                # 不用手动点刷新才能看到新版本。
                if (result.get("version") or result.get("version_id")) and not result.get("major"):
                    self._download_stale = True
                    self._refresh_home()
                    if self.current_view == "versions":
                        self._reload_version_list()
                if "hash_store" in result and hasattr(self, "set_hash_store"):
                    self.set_hash_store.value = bool(result["hash_store"])
                    self.set_hash_store.update()
                    self.storage_hint.text = self._storage_text(self.service.storage_status())
            elif status == "failed":
                if snapshot.get("name") == "切换哈希存储" and hasattr(self, "set_hash_store"):
                    self.set_hash_store.value = bool(self.service.config.hash_store)
                    self.set_hash_store.update()
                ui.notify(
                    f"{snapshot['name']} 失败：{snapshot.get('error') or '未知错误'}",
                    type="negative", position="top-right",
                )
            elif status == "cancelled":
                ui.notify(f"{snapshot['name']} 已取消", type="warning", position="top-right")

        # 任务结束后会在 3 秒后被移除，这里同步清掉残留状态。
        for task_id in list(self._task_status):
            if task_id not in current_ids:
                self._task_status.pop(task_id, None)
                self._cancelling.discard(task_id)
                self._task_speed.pop(task_id, None)

        self._sample_speeds(snapshots)
        if new_task and self.current_view != "tasks":
            # 新建任务后自动切到任务页；_select_view 里会完成渲染。
            self._select_view("tasks")
            return
        if self.current_view == "tasks":
            self._render_tasks()

    def _sample_speeds(self, snapshots: list[dict]) -> None:
        """按两次采样的字节差估算每个任务与整体的下载速度。"""
        now = time.monotonic()
        running_bytes: dict[str, int] = {}
        for snapshot in snapshots:
            if snapshot["status"] != "running":
                continue
            progress = snapshot.get("progress") or {}
            running_bytes[snapshot["id"]] = int(progress.get("bytes_done") or 0)

        if self._speed_sample is not None:
            previous_ts, previous_bytes = self._speed_sample
            elapsed = now - previous_ts
            if elapsed > 0.05:
                for task_id, value in running_bytes.items():
                    instant = max(0.0, (value - previous_bytes.get(task_id, value)) / elapsed)
                    previous = self._task_speed.get(task_id, 0.0)
                    # 简单平滑，避免单次采样抖动
                    self._task_speed[task_id] = (
                        instant if previous <= 0 else previous * 0.5 + instant * 0.5
                    )
                for task_id in list(self._task_speed):
                    if task_id not in running_bytes:
                        self._task_speed[task_id] = 0.0
        self._speed_sample = (now, running_bytes)

    def _total_speed(self, snapshots: list[dict]) -> float:
        return sum(
            self._task_speed.get(snapshot["id"], 0.0)
            for snapshot in snapshots
            if snapshot["status"] == "running"
        )

    def _render_tasks(self) -> None:
        snapshots = self.service.tasks.list_snapshots(limit=30)
        self._render_task_overview(snapshots)
        ids = [snapshot["id"] for snapshot in snapshots]
        if ids != list(self._task_widgets):
            # 任务集合变化时才重建结构；平时只原地更新，保证取消按钮可点击。
            self.task_list.clear()
            self._task_widgets = {}
            with self.task_list:
                if not snapshots:
                    with ui.column().classes("aml-empty aml-card flat w-full"):
                        ui.icon("task_alt")
                        ui.label("暂无任务").classes("text-[14px] font-medium")
                        ui.label("去下载页开始一个吧").classes("aml-sub")
                for snapshot in snapshots:
                    self._create_task_row(snapshot)
            return
        for snapshot in snapshots:
            self._update_task_row(snapshot)

    def _task_percent(self, snapshot: dict) -> float:
        progress = snapshot.get("progress") or {}
        percent = float(snapshot.get("percent") or 0.0)
        if progress.get("type") == "phase" and progress.get("total"):
            percent = progress.get("done", 0) / progress["total"] * 100
        if snapshot["status"] == "done":
            percent = 100.0
        # 进度统一收敛到 0~100：分片回退、未知大小文件等都不应显示超过 100%。
        return min(100.0, max(0.0, percent))

    def _render_task_overview(self, snapshots: list[dict]) -> None:
        self.task_overview.clear()
        if not snapshots:
            return

        running = [snapshot for snapshot in snapshots if snapshot["status"] == "running"]
        done_bytes = total_bytes = 0
        for snapshot in running:
            progress = snapshot.get("progress") or {}
            done_bytes += int(progress.get("bytes_done") or 0)
            total_bytes += int(progress.get("bytes_total") or 0)

        if running and total_bytes > 0:
            percent = done_bytes / total_bytes * 100
        elif running:
            percent = sum(float(s.get("percent") or 0) for s in running) / len(running)
        else:
            percent = sum(
                100.0 if s["status"] == "done" else float(s.get("percent") or 0)
                for s in snapshots
            ) / len(snapshots)
        percent = min(100.0, max(0.0, percent))

        speed = self._total_speed(snapshots)
        with self.task_overview:
            with ui.column().classes("aml-card flat w-full p-5 gap-4"):
                with ui.row().classes("w-full items-end gap-8 no-wrap"):
                    with ui.column().classes("gap-0"):
                        with ui.row().classes("items-center gap-2 no-wrap"):
                            if running:
                                ui.element("div").classes("aml-live-dot")
                            ui.label("总进度").classes("aml-sub")
                        ui.label(f"{percent:.1f}%").classes("aml-big-num").style(
                            "font-size: 34px"
                        )
                    with ui.column().classes("gap-0"):
                        ui.label("总速度").classes("aml-sub")
                        ui.label(_fmt_speed(speed)).classes("text-[20px] font-semibold")
                    with ui.column().classes("gap-0"):
                        ui.label("进行中").classes("aml-sub")
                        ui.label(f"{len(running)} 个").classes("text-[20px] font-semibold")
                    ui.space()
                    with ui.column().classes("items-end gap-0"):
                        ui.label("已传输").classes("aml-sub")
                        ui.label(
                            f"{_fmt_size(done_bytes)} / {_fmt_size(total_bytes)}"
                            if total_bytes
                            else f"{len(snapshots)} 个任务"
                        ).classes("text-[13px] font-medium")
                with ui.element("div").classes("aml-bar-track w-full"):
                    ui.element("div").classes("aml-bar-fill").style(f"width: {percent:.1f}%")

    def _create_task_row(self, snapshot: dict) -> None:
        with ui.column().classes("aml-task-row aml-fade w-full p-3.5 gap-2"):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                with ui.row().classes("items-center gap-2 no-wrap min-w-0"):
                    ui.label(snapshot["name"]).classes("text-[14px] font-medium")
                    status_label = ui.label("").classes("aml-chip")
                with ui.row().classes("items-center gap-2 no-wrap shrink-0"):
                    percent_label = ui.label("").classes("text-[12px] font-semibold").style(
                        "color: var(--aml-accent)"
                    )
                    speed_label = ui.label("").classes("aml-sub")
                    cancel_button = ui.button(
                        "取消",
                        on_click=lambda _=None, t=snapshot["id"]: self._cancel_task(t),
                    ).props("flat dense no-caps color=negative")
            with ui.element("div").classes("aml-bar-track w-full"):
                fill = ui.element("div").classes("aml-bar-fill")
            detail_label = ui.label("").classes("aml-sub w-full").style(
                "white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
            )
        self._task_widgets[snapshot["id"]] = {
            "status": status_label,
            "percent": percent_label,
            "speed": speed_label,
            "cancel": cancel_button,
            "fill": fill,
            "detail": detail_label,
        }

    def _update_task_row(self, snapshot: dict) -> None:
        widgets = self._task_widgets.get(snapshot["id"])
        if not widgets:
            return
        status = snapshot["status"]
        labels = {
            "pending": "等待中", "running": "进行中", "done": "已完成",
            "failed": "失败", "cancelled": "已取消",
        }
        if status in {"done", "failed", "cancelled"}:
            self._cancelling.discard(snapshot["id"])
        cancelling = snapshot["id"] in self._cancelling and status == "running"

        extra = {"running": "primary", "done": "installed", "failed": "warn"}.get(status)
        widgets["status"].text = "取消中…" if cancelling else labels.get(status, status)
        widgets["status"].classes(remove="primary installed warn", add="warn" if cancelling else extra)

        percent = self._task_percent(snapshot)
        widgets["percent"].set_visibility(status in {"running", "done"})
        widgets["percent"].text = f"{percent:.1f}%"

        speed = self._task_speed.get(snapshot["id"], 0.0)
        show_speed = status == "running" and speed > 0 and not cancelling
        widgets["speed"].set_visibility(show_speed)
        if show_speed:
            widgets["speed"].text = _fmt_speed(speed)

        widgets["cancel"].set_visibility(status in {"pending", "running"} and not cancelling)
        widgets["fill"].style(f"width: {percent:.1f}%")
        widgets["detail"].text = self._task_detail(snapshot)

    def _task_detail(self, snapshot: dict) -> str:
        status = snapshot["status"]
        progress = snapshot.get("progress") or {}
        if status == "pending":
            return "等待开始"
        if status == "failed":
            return snapshot.get("error") or "未知错误"
        if status == "cancelled":
            return "已取消"
        if status == "done":
            result = snapshot.get("result") or {}
            if result.get("repaired") is not None:
                return f"修复 {result['repaired']} 个，跳过 {result.get('skipped', 0)} 个"
            if result.get("filename"):
                return f"已安装 {result['filename']}"
            if result.get("files") is not None:
                downloaded = result.get("downloaded")
                if downloaded is None:
                    downloaded = result["files"] - result.get("skipped", 0)
                return f"下载 {downloaded} 个，跳过 {result.get('skipped', 0)} 个"
            if result.get("name"):
                return f"已完成 {result['name']}"
            if result.get("version_id") and result.get("loader"):
                return f"已安装 {result['loader']} → {result['version_id']}"
            if result.get("major") and result.get("path"):
                return f"Java {result['major']} 已就绪：{result['path']}"
            if result.get("path"):
                return str(result["path"])
            return "已完成"
        if progress.get("type") == "phase":
            phase = str(progress.get("phase") or "处理中")
            label = PHASE_LABELS.get(phase, phase)
            done = int(progress.get("done", 0) or 0)
            total = int(progress.get("total", 0) or 0)
            if total:
                return f"{label} {done}/{total}"
            # 下载安装器等阶段总大小未知，展示已下载的大小而不是「0/0」。
            return f"{label} {_fmt_size(done)}" if done else label
        done = progress.get("files_done", 0)
        total = progress.get("files_total", 0)
        current = progress.get("current") or ""
        if total:
            return f"{done}/{total} 个文件  {current[:60]}"
        bytes_total = int(progress.get("bytes_total") or 0)
        if bytes_total:
            return f"{_fmt_size(int(progress.get('bytes_done') or 0))} / {_fmt_size(bytes_total)}"
        if current:
            return current
        return "准备中…"

    def _cancel_task(self, task_id: str) -> None:
        if not self.service.tasks.cancel(task_id):
            return
        self._cancelling.add(task_id)
        widgets = self._task_widgets.get(task_id)
        if widgets:
            widgets["status"].text = "取消中…"
            widgets["status"].classes(remove="primary installed", add="warn")
            widgets["cancel"].set_visibility(False)
            widgets["speed"].set_visibility(False)
        ui.notify("正在取消…", position="top-right")

    # ==================================================================== 设置
    def _build_settings_page(self) -> None:
        view = self.service.config_view()
        with ui.column().classes("w-full h-full p-5 gap-3 aml-scroll") as page:
            self.pages["settings"] = page
            page.props("id=aml-page-settings")
            page.set_visibility(False)

            with ui.row().classes("w-full items-center justify-between no-wrap"):
                with ui.column().classes("gap-0.5"):
                    ui.label("设置").classes("text-[20px] font-semibold")
                    ui.label(
                        f"{APP_NAME} v{APP_VERSION} · 所有数据保存在运行目录下的 data"
                    ).classes("aml-sub")
                self.settings_toggle = ui.toggle(
                    {"launcher": "启动器设置", "game": "游戏设置"},
                    value="launcher",
                    on_change=lambda e: self._switch_settings(e.value),
                ).props("unelevated dense no-caps toggle-color=primary")

            with ui.column().classes("w-full gap-3") as launcher_body:
                self._build_launcher_settings(view)
            with ui.column().classes("w-full gap-3") as game_body:
                self._build_game_settings(view)

        self.settings_bodies = {"launcher": launcher_body, "game": game_body}
        game_body.set_visibility(False)

    def _switch_settings(self, value: str) -> None:
        for key, body in self.settings_bodies.items():
            body.set_visibility(key == value)
    def _build_launcher_settings(self, view: dict) -> None:
        with ui.column().classes("aml-card flat w-full p-4 gap-2"):
            ui.label("数据与下载").classes("text-[14px] font-semibold")
            ui.label(f"启动器数据目录：{view['data_dir']}").classes("aml-sub")
            with ui.row().classes("w-full items-end gap-3 no-wrap"):
                self.set_root = ui.input(
                    label="游戏目录（.minecraft）", value=view["root"]
                ).classes("flex-1").props("dense outlined")
                ui.button("应用", icon="check", on_click=self._apply_game_dir).props(
                    "unelevated dense no-caps color=primary"
                )
            ui.label("游戏文件（版本 / libraries / assets / mods）统一放在该目录，修改后立即生效").classes("aml-sub")
            with ui.row().classes("w-full items-center gap-3 no-wrap"):
                self.set_mirror = ui.switch(
                    "BMCLAPI 镜像加速（镜像与官方可用时双源并行，失败自动回退）",
                    value=view["download_source"] != "official",
                ).props("dense")
                ui.space()
                ui.button("探测镜像", icon="network_check", on_click=self._probe_mirror).props(
                    "flat dense no-caps"
                )
            self.mirror_hint = ui.label(
                f"当前下载源：{view['mirror'].get('active') or view['mirror'].get('source')}"
            ).classes("aml-sub")
            ui.label("CurseForge 通过 mcimirror 公共镜像访问，无需 API Key").classes("aml-sub")
            with ui.row().classes("w-full items-center gap-3 no-wrap"):
                self.set_workers = ui.number(
                    label="下载线程数", value=view["download_workers"], min=1, max=64, step=1
                ).classes("flex-1").props("dense outlined")
                self.set_mod_source = ui.select(
                    {"modrinth": "Modrinth", "curseforge": "CurseForge"},
                    value=view["mod_source"], label="默认 Mod 来源",
                ).classes("flex-1").props("dense outlined")
                self.set_mod_loader = ui.select(
                    {key: LOADER_LABELS[key] for key in LOADERS},
                    value=view["mod_loader"] if view["mod_loader"] in LOADERS else "fabric",
                    label="默认加载器",
                ).classes("flex-1").props("dense outlined")
            self.set_include_snapshots = ui.switch(
                "版本列表包含快照", value=bool(view["include_snapshots"])
            ).props("dense")

        with ui.column().classes("aml-card flat w-full p-4 gap-2"):
            ui.label("存储方式（实验性）").classes("text-[14px] font-semibold")
            self.set_hash_store = ui.switch(
                "哈希文件库（按 SHA1 去重，启动时复制到临时目录）",
                value=bool(view["storage"].get("enabled")),
                on_change=self._on_hash_store_toggle,
            ).props("dense")
            self.storage_hint = ui.label(self._storage_text(view["storage"])).classes("aml-sub")
            ui.label(
                "注意：开启后游戏文件无法被其它启动器读取；关闭启动器前需结束游戏以保存存档。"
            ).classes("aml-sub")

        with ui.column().classes("aml-card flat w-full p-4 gap-2"):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                with ui.column().classes("gap-0"):
                    ui.label("启动器更新").classes("text-[14px] font-semibold")
                    ui.label(
                        f"当前 v{APP_VERSION} · 下载优先走公共 GH 代理"
                    ).classes("aml-sub")
                ui.button(
                    "检查更新", icon="system_update_alt",
                    on_click=lambda: background_tasks.create(self._check_update(True)),
                ).props("flat dense no-caps")
            with ui.row().classes("w-full items-center gap-3 no-wrap"):
                self.set_update_mode = ui.select(
                    UPDATE_MODES,
                    value=view.get("launcher_update_mode") or "notify",
                    label="更新方式",
                ).classes("w-56").props("dense outlined")
                ui.label("同一版本只提醒一次；Release 附件被替换会再次提醒").classes(
                    "aml-sub"
                )
            self.update_hint = ui.label(self._update_mode_text()).classes("aml-sub")

        with ui.column().classes("aml-card flat w-full p-4 gap-2"):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                with ui.column().classes("gap-0"):
                    ui.label("运行日志").classes("text-[14px] font-semibold")
                    ui.label("记录启动、下载与安装过程，保存在 data/logs/launcher.log").classes(
                        "aml-sub"
                    )
                with ui.row().classes("gap-1 no-wrap"):
                    ui.button(
                        "打开目录", icon="folder_open",
                        on_click=lambda: self._open_logs_dir(),
                    ).props("flat dense no-caps")
                    ui.button(
                        "查看日志", icon="receipt_long", on_click=self._open_logs_dialog
                    ).props("unelevated dense no-caps color=primary")

        ui.button("保存启动器设置", icon="save", on_click=self._save_launcher_settings).props(
            "unelevated dense no-caps color=primary"
        ).classes("self-start")

    def _build_game_settings(self, view: dict) -> None:
        ui.label("以下为全局默认值；版本设置中留空或「跟随全局」的项会使用这里的配置").classes("aml-sub")
        with ui.column().classes("aml-card flat w-full p-4 gap-2"):
            ui.label("Java 与内存").classes("text-[14px] font-semibold")
            with ui.row().classes("w-full items-center gap-3 no-wrap"):
                self.set_java = ui.input(label="Java 可执行文件", value=view["java"]).classes(
                    "flex-1"
                ).props("dense outlined")
                ui.button("测试 Java", on_click=self._test_java).props("flat dense no-caps")
                ui.button("检测已装 Java", on_click=self._scan_java).props("flat dense no-caps")
            ui.label("留空或填 java 时按版本要求自动选择；缺少对应版本会自动下载安装").classes("aml-sub")
            with ui.row().classes("w-full items-center gap-3 no-wrap"):
                self.set_memory = ui.number(
                    label="最大内存 (MB)", value=view["memory_mb"], min=512, max=65536, step=512
                ).classes("flex-1").props("dense outlined")
                self.set_min_memory = ui.number(
                    label="最小内存 (MB)", value=view["min_memory_mb"], min=256, max=32768, step=256
                ).classes("flex-1").props("dense outlined")
            self.java_list = ui.column().classes("w-full gap-1")

        with ui.column().classes("aml-card flat w-full p-4 gap-2"):
            ui.label("游戏窗口").classes("text-[14px] font-semibold")
            with ui.row().classes("w-full items-center gap-3 no-wrap"):
                self.set_width = ui.number(
                    label="窗口宽", value=view["width"], min=320, max=7680
                ).classes("flex-1").props("dense outlined")
                self.set_height = ui.number(
                    label="窗口高", value=view["height"], min=240, max=4320
                ).classes("flex-1").props("dense outlined")
            self.set_isolated = ui.switch(
                "新版本默认启用版本隔离", value=view["isolated"]
            ).props("dense")

        ui.button("保存游戏设置", icon="save", on_click=self._save_game_settings).props(
            "unelevated dense no-caps color=primary"
        ).classes("self-start")

    async def _save_launcher_settings(self) -> None:
        changes = {
            "download_source": "auto" if self.set_mirror.value else "official",
            "download_workers": int(self.set_workers.value or 32),
            "mod_source": self.set_mod_source.value,
            "mod_loader": self.set_mod_loader.value,
            "include_snapshots": bool(self.set_include_snapshots.value),
            "launcher_update_mode": str(self.set_update_mode.value or "notify"),
        }
        try:
            view = await run.io_bound(self.service.update_config, changes)
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"保存失败：{exc}", type="negative", position="top-right")
            return
        self.mirror_hint.text = (
            f"当前下载源：{view['mirror'].get('active') or view['mirror'].get('source')}"
        )
        if hasattr(self, "update_hint"):
            self.update_hint.text = self._update_mode_text()
        # 下载页有同名快照开关，保持同步，避免显示过期状态。
        if hasattr(self, "snapshot_switch"):
            self.snapshot_switch.value = bool(self.service.config.include_snapshots)
            self.snapshot_switch.update()
        ui.notify("启动器设置已保存", type="positive", position="top-right")

    # ---------------------------------------------------------- 更新与日志
    def _update_mode(self) -> str:
        mode = str(getattr(self.service.config, "launcher_update_mode", "notify") or "notify")
        return mode if mode in UPDATE_MODES else "notify"

    def _update_mode_text(self) -> str:
        return UPDATE_MODE_HINTS.get(self._update_mode(), "")

    def _silent_update_check(self) -> None:
        background_tasks.create(self._auto_update_check())

    async def _auto_update_check(self) -> None:
        """按设置处理启动时的更新：自动安装 / 只提醒一次 / 不检查。"""
        mode = self._update_mode()
        if mode == "off":
            return
        info = await self._check_update(False, notify=False)
        if not info or not info.get("available"):
            return
        identity = str(info.get("identity") or "")
        if identity and identity == str(self.service.config.launcher_update_seen or ""):
            return
        if mode == "notify":
            self.service.mark_launcher_update_seen(identity)
            ui.notify(
                f"发现新版本 v{info.get('version')}，可在「设置 → 启动器更新」查看",
                type="info",
                position="top-right",
            )
            return
        await self._auto_apply_update(info, identity)

    async def _auto_apply_update(self, info: dict, identity: str) -> None:
        if self.service.has_running_game() or self._home_launching:
            ui.notify(
                f"发现新版本 v{info.get('version')}，当前有游戏在运行，稍后再更新",
                type="info",
                position="top-right",
            )
            return
        try:
            result = await run.io_bound(
                lambda: self.service.download_launcher_update(info)
            )
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"自动更新下载失败：{exc}", type="negative", position="top-right")
            return
        path = str(result.get("path") or "")
        if not info.get("frozen"):
            self.service.mark_launcher_update_seen(identity)
            ui.notify(
                f"更新已下载到 {path}（源码模式不会替换程序）",
                type="positive",
                position="top-right",
            )
            return
        if self.service.has_running_game() or self._home_launching:
            ui.notify("更新已下载，退出启动器后生效", type="info", position="top-right")
            return
        try:
            await run.io_bound(self.service.apply_launcher_update, path)
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"自动更新安装失败：{exc}", type="negative", position="top-right")
            return
        self.service.mark_launcher_update_seen(identity)
        ui.notify("正在重启并安装更新…", position="top-right")
        ui.timer(0.6, app.shutdown, once=True)

    async def _check_update(self, manual: bool, notify: bool = True) -> dict:
        if getattr(self, "_update_checking", False):
            return {}
        self._update_checking = True
        hint = getattr(self, "update_hint", None)
        if hint is not None:
            hint.text = "正在检查更新…"
        try:
            info = await run.io_bound(self.service.check_launcher_update)
        except Exception as exc:  # noqa: BLE001
            if hint is not None:
                hint.text = f"检查更新失败：{exc}"
            if manual:
                ui.notify(f"检查更新失败：{exc}", type="negative", position="top-right")
            return {}
        finally:
            self._update_checking = False

        self._update_info = info
        if info.get("available"):
            if hint is not None:
                hint.text = f"发现新版本 v{info['version']}（当前 v{APP_VERSION}）"
            if manual:
                self._open_update_dialog(info)
            elif notify:
                ui.notify(
                    f"发现新版本 v{info['version']}，可在「设置 → 启动器更新」查看",
                    type="info",
                    position="top-right",
                )
        else:
            if hint is not None:
                hint.text = f"已是最新版本（v{APP_VERSION}）"
            if manual:
                ui.notify("已是最新版本", type="positive", position="top-right")
        return info

    def _open_update_dialog(self, info: dict) -> None:
        def builder() -> None:
            with ui.row().classes("items-center gap-2 no-wrap"):
                ui.label(f"v{info.get('version')}").classes("text-[18px] font-semibold")
                ui.label("新版本可用").classes("aml-chip installed")
            ui.label(
                f"当前版本 v{APP_VERSION} · 安装包 {_fmt_size(info.get('size') or 0)}"
            ).classes("aml-sub")
            notes = (info.get("notes") or "").strip()
            if notes:
                ui.label(notes).classes("text-[12.5px]").style(
                    "white-space: pre-wrap; max-height: 220px; overflow-y: auto; "
                    "background: var(--aml-surface-2); border-radius: 10px; padding: 8px 10px;"
                )
            ui.label("下载优先使用公共 GH 代理，失败时自动回退 GitHub 原始地址。").classes(
                "aml-sub"
            )

            def start() -> None:
                self.dialog.close()
                self._open_update_progress(info)

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("稍后", on_click=self.dialog.close).props("flat dense no-caps")
                if info.get("frozen"):
                    ui.button(
                        "下载并安装", icon="system_update_alt", on_click=start
                    ).props("unelevated dense no-caps color=primary")
                else:
                    ui.button(
                        "下载到本地", icon="download", on_click=start
                    ).props("unelevated dense no-caps color=primary")

        self.open_dialog("启动器更新", builder)

    def _open_update_progress(self, info: dict) -> None:
        state = {"done": 0, "total": int(info.get("size") or 0), "path": "", "error": ""}
        cancel = threading.Event()
        timer_ref: dict = {}

        def on_chunk(delta: int) -> None:
            state["done"] = max(0, state["done"] + int(delta))

        def cancel_download() -> None:
            cancel.set()
            active = timer_ref.get("timer")
            if active is not None:
                active.active = False
            self.dialog.close()

        def builder() -> None:
            ui.label(f"正在下载 v{info.get('version')}…").classes("text-[14px] font-semibold")
            bar = ui.linear_progress(value=0, show_value=False).classes("w-full")
            detail = ui.label("0%").classes("aml-sub")
            with ui.row().classes("w-full justify-between items-center no-wrap"):
                ui.label("下载完成后可立即重启并替换程序").classes("aml-sub")
                ui.button("取消", on_click=cancel_download).props("flat dense no-caps")

            async def job() -> None:
                try:
                    result = await run.io_bound(
                        lambda: self.service.download_launcher_update(
                            info, on_chunk=on_chunk, cancel=cancel.is_set
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    state["error"] = str(exc)
                    return
                state["path"] = result["path"]
                if state["total"]:
                    state["done"] = state["total"]

            def stop_timer() -> None:
                active = timer_ref.get("timer")
                if active is not None:
                    active.active = False

            def tick() -> None:
                total = state["total"]
                done = state["done"]
                percent = (done / total * 100) if total else 0.0
                bar.value = min(1.0, percent / 100)
                detail.text = f"{percent:.1f}% · {_fmt_size(done)} / {_fmt_size(total)}"
                if cancel.is_set():
                    stop_timer()
                    return
                if state["error"]:
                    stop_timer()
                    self.dialog.close()
                    ui.notify(
                        f"下载更新失败：{state['error']}", type="negative", position="top-right"
                    )
                elif state["path"]:
                    stop_timer()
                    self.dialog.close()
                    if info.get("frozen"):
                        self._confirm_apply_update(state["path"], info)
                    else:
                        ui.notify(
                            f"更新已下载到 {state['path']}（源码模式不会替换程序）",
                            type="positive",
                            position="top-right",
                        )

            ui.timer(0.1, job, once=True)
            timer_ref["timer"] = ui.timer(0.2, tick)

        self.open_dialog(f"下载更新 v{info.get('version')}", builder)

    def _confirm_apply_update(self, path: str, info: dict) -> None:
        def builder() -> None:
            ui.label(f"v{info.get('version')} 已下载完成").classes("text-[15px] font-semibold")
            ui.label("立即关闭启动器，替换程序文件后自动重启？").classes("aml-sub")

            async def apply() -> None:
                self.dialog.close()
                try:
                    await run.io_bound(self.service.apply_launcher_update, path)
                except Exception as exc:  # noqa: BLE001
                    ui.notify(f"启动更新失败：{exc}", type="negative", position="top-right")
                    return
                ui.notify("正在重启并安装更新…", position="top-right")
                ui.timer(0.6, app.shutdown, once=True)

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("稍后", on_click=self.dialog.close).props("flat dense no-caps")
                ui.button(
                    "立即重启安装", icon="restart_alt", on_click=apply
                ).props("unelevated dense no-caps color=primary")

        self.open_dialog("安装更新", builder)

    def _open_logs_dir(self) -> None:
        try:
            self.service.open_path(self.service.logs_dir())
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"打开日志目录失败：{exc}", type="negative", position="top-right")

    def _open_logs_dialog(self) -> None:
        def builder() -> None:
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                keyword = ui.input(placeholder="筛选关键字").props("dense outlined").classes(
                    "flex-1"
                )
            log_holder = ui.column().classes("w-full")

            async def refresh() -> None:
                lines = await run.io_bound(
                    self.service.launcher_logs,
                    limit=500,
                    keyword=keyword.value or "",
                )
                log_holder.clear()
                with log_holder:
                    log_view = ui.log(max_lines=1000).classes("w-full").style(
                        "height: 340px; font-family: Consolas, 'Courier New', monospace; "
                        "font-size: 12px; background: #0f172a; color: #e2e8f0; "
                        "border-radius: 10px; padding: 8px;"
                    )
                    if not lines:
                        log_view.push("（暂无日志）")
                    for line in lines:
                        log_view.push(line)

            async def export() -> None:
                lines = await run.io_bound(
                    self.service.launcher_logs,
                    limit=100000,
                    keyword=keyword.value or "",
                )
                result = await run.io_bound(self.service.export_launcher_logs, lines)
                ui.notify(f"日志已导出：{result['path']}", type="positive", position="top-right")

            keyword.on("keydown.enter", lambda: background_tasks.create(refresh()))
            with ui.row().classes("w-full justify-between items-center no-wrap"):
                with ui.row().classes("gap-1 no-wrap"):
                    ui.button("刷新", icon="refresh", on_click=lambda: background_tasks.create(refresh())).props(
                        "flat dense no-caps"
                    )
                    ui.button("导出", icon="file_download", on_click=lambda: background_tasks.create(export())).props(
                        "flat dense no-caps"
                    )
                    ui.button("打开目录", icon="folder_open", on_click=self._open_logs_dir).props(
                        "flat dense no-caps"
                    )
                ui.button("关闭", on_click=self.dialog.close).props("flat dense no-caps")

            background_tasks.create(refresh())

        self.open_dialog("运行日志", builder)

    def _storage_text(self, storage: dict) -> str:
        if not storage.get("enabled"):
            return "当前：普通目录（游戏文件直接保存在 .minecraft）"
        return (
            f"当前：哈希库 {storage.get('files', 0)} 个文件 · "
            f"逻辑 {_fmt_size(storage.get('logical_bytes', 0))} · "
            f"实际 {_fmt_size(storage.get('stored_bytes', 0))} · "
            f"去重节省 {_fmt_size(storage.get('saved_bytes', 0))}"
        )

    def _on_hash_store_toggle(self, event) -> None:
        enabled = bool(event.value)
        if enabled == bool(self.service.config.hash_store):
            return
        title = "开启哈希存储" if enabled else "关闭哈希存储"

        def builder() -> None:
            if enabled:
                ui.label(
                    "将立即把现有游戏文件迁移进哈希库，需要约 2 倍磁盘空间，期间请勿关闭启动器。"
                ).classes("text-[13px]")
                ui.label(
                    "迁移后这些文件无法被其它启动器读取；启动器关闭时会自动回收工作目录。"
                ).classes("aml-sub")
            else:
                ui.label("将把哈希库还原成普通 .minecraft 文件，期间请勿关闭启动器。").classes(
                    "text-[13px]"
                )
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button(
                    "取消", on_click=lambda: self._cancel_hash_store(enabled)
                ).props("flat dense no-caps")
                ui.button(
                    "确认切换", icon="swap_horiz",
                    on_click=lambda: self._start_hash_store(enabled),
                ).props("unelevated dense no-caps color=primary")

        self.open_dialog(title, builder)

    def _cancel_hash_store(self, enabled: bool) -> None:
        self.dialog.close()
        self.set_hash_store.value = not enabled
        self.set_hash_store.update()

    def _start_hash_store(self, enabled: bool) -> None:
        self.dialog.close()
        self.service.set_hash_store(enabled)
        ui.notify("已开始切换存储方式，进度见「任务」页", position="top-right")

    async def _apply_game_dir(self) -> None:
        raw = (self.set_root.value or "").strip()
        if not raw:
            ui.notify("请输入游戏目录", type="warning", position="top-right")
            return
        try:
            await run.io_bound(self.service.update_config, {"root": raw})
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"切换失败：{exc}", type="negative", position="top-right")
            return
        self.set_root.value = self.service.config.root
        self.set_root.update()
        self._refresh_home()
        self._reload_version_list()
        ui.notify(f"游戏目录已切换到 {self.service.config.root}", type="positive", position="top-right")

    async def _save_game_settings(self) -> None:
        changes = {
            "java": (self.set_java.value or "").strip() or "java",
            "memory_mb": int(self.set_memory.value or 2048),
            "min_memory_mb": int(self.set_min_memory.value or 512),
            "width": int(self.set_width.value or 854),
            "height": int(self.set_height.value or 480),
            "isolated": bool(self.set_isolated.value),
        }
        try:
            await run.io_bound(self.service.update_config, changes)
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"保存失败：{exc}", type="negative", position="top-right")
            return
        ui.notify("游戏设置已保存", type="positive", position="top-right")

    async def _test_java(self) -> None:
        java = (self.set_java.value or "java").strip()
        try:
            result = await run.io_bound(self.service.test_java, java)
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"测试失败：{exc}", type="negative", position="top-right")
            return
        if result.get("ok"):
            ui.notify(
                f"{result['path']}：Java {result['version']}（{result['major']}）",
                type="positive", position="top-right",
            )
        else:
            ui.notify(result.get("message") or "不是有效的 Java", type="negative", position="top-right")

    async def _scan_java(self) -> None:
        try:
            runtimes = await run.io_bound(self.service.java_scan)
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"检测失败：{exc}", type="negative", position="top-right")
            return

        self.java_list.clear()
        with self.java_list:
            if not runtimes:
                ui.label("未检测到已安装的 Java，可在下方下载").classes("aml-sub")
            for runtime in runtimes:
                with ui.row().classes("w-full items-center justify-between no-wrap"):
                    with ui.column().classes("gap-0"):
                        ui.label(runtime["path"]).classes("text-[12px]")
                        ui.label(
                            f"Java {runtime['version']} · {runtime['major']} · "
                            f"{runtime['vendor']} · {runtime['source']}"
                        ).classes("aml-sub")
                    with ui.row().classes("gap-2 no-wrap"):
                        if runtime.get("current"):
                            ui.label("当前").classes("aml-chip primary")
                        ui.button(
                            "使用", on_click=lambda _=None, p=runtime["path"]: self._use_java(p)
                        ).props("flat dense no-caps")
            ui.separator()
            with ui.row().classes("w-full items-center gap-3 no-wrap"):
                self.java_major = ui.select(
                    [8, 11, 17, 21], value=21, label="下载 Java 主版本"
                ).classes("flex-1").props("dense outlined")
                ui.button("下载并安装", icon="download", on_click=self._install_java).props(
                    "unelevated dense no-caps color=primary"
                )

    def _use_java(self, path: str) -> None:
        self.set_java.value = path
        ui.notify(f"已选择 {path}（记得保存）", position="top-right")

    def _install_java(self) -> None:
        major = int(self.java_major.value or 21)
        self.service.start_java_install(major)
        self._select_view("tasks")
        ui.notify(f"已开始下载 Java {major}，进度见「任务」页", position="top-right")

    async def _probe_mirror(self) -> None:
        try:
            status = await run.io_bound(self.service.probe_mirror)
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"探测失败：{exc}", type="negative", position="top-right")
            return
        self.mirror_hint.text = (
            f"当前下载源：{status.get('active')}"
            + (f"（{status.get('latency_ms')} ms）" if status.get("available") else "（镜像不可用）")
        )
        ui.notify(
            f"当前下载源：{status.get('active')}（{status.get('latency_ms')} ms）"
            if status.get("available") else f"镜像不可用，已回退：{status.get('active')}",
            type="positive" if status.get("available") else "warning",
            position="top-right",
        )

    # ==================================================================== 启动
    def _active_account(self) -> Optional[dict]:
        try:
            for account in self.service.accounts():
                if account.get("active"):
                    return account
        except Exception:  # noqa: BLE001
            return None
        return None

    async def _launch(
        self,
        version: str,
        *,
        username: str,
        account_id: Optional[str] = None,
        inline: bool = False,
        extra_game: Optional[list[str]] = None,
    ) -> None:
        if not inline and (self._launch_timer.active or self._home_launching):
            ui.notify("正在启动中，请稍候", type="warning", position="top-right")
            return

        self._launch_cancel.clear()
        self._launch_done = False
        self._launch_error = ""
        self._launch_progress = {"percent": 0.0, "detail": "准备中…"}
        if inline:
            self._sync_home_launch()
        else:
            self.launch_title.text = f"正在启动 {version}"
            self.launch_error.set_visibility(False)
            self.launch_close_btn.set_visibility(False)
            self.launch_cancel_btn.set_visibility(True)
            self._apply_launch_progress()
            self.launch_dialog.open()
            self._launch_timer.active = True
        background_tasks.create(
            self._run_launch(
                version, username, account_id, inline=inline, extra_game=extra_game
            )
        )

    async def _run_launch(
        self,
        version: str,
        username: str,
        account_id: Optional[str],
        *,
        inline: bool = False,
        extra_game: Optional[list[str]] = None,
    ) -> None:
        def on_progress(event: dict) -> None:
            self._launch_progress = {
                "percent": float(event.get("percent") or 0.0),
                "detail": str(event.get("detail") or ""),
            }

        try:
            await run.io_bound(
                lambda: self.service.launch(
                    version,
                    offline=True,
                    username=username or "Steve",
                    account_id=account_id,
                    progress=on_progress,
                    cancel=self._launch_cancel.is_set,
                    extra_game=extra_game,
                )
            )
        except Exception as exc:  # noqa: BLE001
            error = "已取消启动" if self._launch_cancel.is_set() else str(exc)
            if inline:
                self._finish_home_launch(error)
                return
            self._launch_error = error
            self._launch_done = True
            return
        if inline:
            self._finish_home_launch("")
            # 有些崩溃（缺原生库等）发生在启动后的几秒内，此时已经提示过成功，
            # 这里补一次延迟检查，避免「界面没反应、也没报错」。
            ui.timer(10.0, self._warn_game_exited, once=True)
            return
        self._launch_done = True

    def _warn_game_exited(self) -> None:
        if self._home_launching or self.service.has_running_game():
            return
        ui.notify(
            "游戏已退出，启动输出保存在 data/logs/game-*.log",
            type="warning",
            position="top-right",
        )


    # =================================================================== 联机
    def _build_multiplayer_page(self) -> None:
        with ui.column().classes("w-full h-full p-5 gap-3 aml-scroll") as page:
            self.pages["multiplayer"] = page
            page.props("id=aml-page-multiplayer")
            page.set_visibility(False)
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                with ui.column().classes("gap-0 no-wrap"):
                    ui.label("联机").classes("text-[18px] font-semibold")
                    self.relay_sub = ui.label("").classes("aml-sub")
                with ui.row().classes("items-center gap-2 no-wrap"):
                    self.relay_my_room_button = ui.button(
                        "我的房间", on_click=self._relay_open_host_dialog
                    ).props("unelevated dense no-caps color=primary")
                    self.relay_my_room_button.set_visibility(False)

            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                self.relay_search_input = ui.input(
                    placeholder="输入 4 位房间号搜索"
                ).props("dense outlined").classes("w-[320px]")
                self.relay_search_input.on(
                    "keydown.enter", lambda _: background_tasks.create(self._relay_search_room())
                )
                ui.button(
                    "搜索", icon="search",
                    on_click=lambda: background_tasks.create(self._relay_search_room()),
                ).props("unelevated dense no-caps color=primary")
                ui.button(
                    icon="refresh",
                    on_click=lambda: background_tasks.create(self._refresh_relay_rooms(force=True)),
                ).props("flat dense round size=sm").tooltip("刷新房间列表")

            self.relay_body = ui.column().classes("w-full gap-3")
            self.relay_tick_timer = ui.timer(2.0, self._relay_tick)
            self._render_relay_body()

    def _build_relay_create_page(self) -> None:
        with ui.column().classes("w-full h-full p-5 gap-3 aml-scroll") as page:
            self.pages["relay-create"] = page
            page.props("id=aml-page-relay-create")
            page.set_visibility(False)
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                ui.button(
                    icon="arrow_back", on_click=lambda: self._select_view("multiplayer")
                ).props("flat dense round size=sm").tooltip("返回联机")
                with ui.column().classes("gap-0 no-wrap"):
                    ui.label("创建联机房间").classes("text-[18px] font-semibold")
                    ui.label("选择服务商、端口与人数上限，生成 4 位房间号").classes("aml-sub")
            self.relay_create_body = ui.column().classes("w-full gap-3")
            self._render_relay_create_page()

    def _render_relay_body(self, *, notify: str = "") -> None:
        if self.relay_body is None:
            return
        self.relay_body.clear()
        self.relay_players_label = None
        self.relay_tunnel_label = None
        master = self.service.relay_master()
        self.relay_sub.text = "子节点由主节点自动分配" if master else "联机服务不可用"
        status = self.service.relay_status()
        host_room = status.get("room")
        guest = status.get("guest")
        if self.relay_my_room_button is not None:
            if host_room:
                code = str(host_room.get("join_code") or "")
                self.relay_my_room_button.text = f"我的房间 {code}"
                self.relay_my_room_button.set_visibility(True)
            else:
                self.relay_my_room_button.set_visibility(False)

        with self.relay_body:
            if not master:
                with ui.card().classes("aml-card w-full p-5 gap-1"):
                    ui.label("尚未配置联机主节点").classes("text-[15px] font-semibold")
                    ui.label(
                        "点击右上角齿轮填写主节点地址，例如 https://relay.example.com:3380"
                    ).classes("aml-sub")
                with ui.row().classes("w-full gap-3 flex-wrap items-stretch"):
                    self._render_create_card()
                if notify:
                    ui.notify(notify, type="positive")
                return

            if guest:
                self._render_guest_room(guest, status.get("tunnel") or {})

            with ui.row().classes("w-full gap-3 flex-wrap items-stretch"):
                self._render_create_card()
                self._render_room_list()
            if notify:
                ui.notify(notify, type="positive")

    def _render_create_card(self) -> None:
        with ui.card().classes(
            "aml-card p-4 gap-1 w-[240px] min-h-[132px] items-center justify-center cursor-pointer"
        ).on("click", lambda: background_tasks.create(self._relay_start_create())):
            ui.icon("add_circle_outline").classes("text-[30px]").style(
                "color: var(--aml-primary, #2563eb)"
            )
            ui.label("创建房间").classes("text-[15px] font-semibold")
            ui.label("先启动游戏并对局域网开放").classes("aml-sub text-center")

    def _render_room_list(self) -> None:
        if self._relay_rooms is None:
            with ui.card().classes("aml-card p-4 gap-2 w-[240px] min-h-[132px] items-center justify-center"):
                ui.spinner(size="22px")
                ui.label("正在加载房间…" if self._relay_rooms_loading else "暂无房间数据").classes(
                    "aml-sub"
                )
            return
        if self._relay_rooms_error:
            with ui.card().classes("aml-card p-4 gap-2 w-[240px] min-h-[132px] items-center justify-center"):
                ui.icon("cloud_off").classes("text-[26px]").style("color: var(--aml-muted)")
                ui.label(self._relay_rooms_error).classes("aml-sub text-center")
                ui.button(
                    "重试",
                    on_click=lambda: background_tasks.create(
                        self._refresh_relay_rooms(force=True)
                    ),
                ).props("flat dense no-caps")
            return
        if not self._relay_rooms:
            with ui.card().classes("aml-card p-4 gap-2 w-[240px] min-h-[132px] items-center justify-center"):
                ui.icon("meeting_room").classes("text-[26px]").style("color: var(--aml-muted)")
                ui.label("暂无公开房间").classes("aml-sub")
            return
        for room in self._relay_rooms:
            self._render_room_card(room)

    def _render_room_card(self, room: dict) -> None:
        players = int(room.get("players") or 1)
        limit = int(room.get("max_players") or 8)
        version = str(room.get("version") or "未知")
        loader = str(room.get("loader") or "vanilla")
        mods = int(room.get("mod_count") or 0)
        with ui.card().classes("aml-card p-4 gap-2 w-[240px] min-h-[132px]"):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                ui.label(str(room.get("name") or "未命名房间")).classes(
                    "text-[15px] font-semibold truncate"
                )
                ui.label(f"{players}/{limit}").classes("aml-chip primary")
            ui.label(f"房主：{room.get('host_name') or '未知'}").classes("aml-sub")
            ui.label(f"{version} · {loader}").classes("aml-sub truncate")
            extra = f" · {mods} 个 Mod" if mods else ""
            ui.label(f"节点：{room.get('node_name') or room.get('node_id') or '未知'}{extra}").classes(
                "aml-sub truncate"
            )
            ui.button(
                "加入房间",
                on_click=lambda r=room: background_tasks.create(self._relay_begin_join(r)),
            ).props("unelevated dense no-caps color=primary").classes("w-full mt-1")

    def _render_guest_room(self, guest: dict, tunnel: dict) -> None:
        summary = guest.get("room") or {}
        connect = str(guest.get("connect") or "")
        with ui.card().classes("aml-card w-full p-5 gap-2"):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                with ui.column().classes("gap-0 no-wrap"):
                    ui.label("已加入房间").classes("aml-sub")
                    ui.label(str(summary.get("name") or guest.get("room_id") or "")).classes(
                        "text-[16px] font-semibold"
                    )
                ui.button("退出房间", on_click=self._relay_leave_room).props(
                    "flat dense no-caps"
                )
            with ui.row().classes("items-center gap-3 no-wrap"):
                ui.label(connect).classes("text-[22px] font-semibold").style(
                    "font-family: Consolas, monospace"
                )
                ui.button(
                    "复制地址", on_click=lambda: self._copy(connect, "连接地址已复制")
                ).props("unelevated dense no-caps color=primary")
            ui.label("游戏会通过本机回环地址自动进入服务器，不会暴露房主或节点 IP。").classes(
                "aml-sub"
            )
            self.relay_tunnel_label = ui.label("").classes("aml-sub")
            self._sync_relay_labels(None, tunnel)

    def _sync_relay_labels(self, room: Optional[dict], tunnel: dict) -> None:
        if self.relay_tunnel_label is not None:
            self.relay_tunnel_label.text = (
                "隧道已连接" if tunnel.get("running") else "隧道未运行"
            )
        if room and self.relay_players_label is not None:
            self.relay_players_label.text = f"在线玩家：{room.get('players') or 1} 人"

    async def _relay_tick(self) -> None:
        if self.current_view != "multiplayer" or self.relay_tunnel_label is None:
            return
        tunnel = self.service.relay_status().get("tunnel") or {}
        self.relay_tunnel_label.text = "隧道已连接" if tunnel.get("running") else "隧道未运行"

    @_relay_ui_task
    async def _refresh_relay_rooms(self, *, force: bool = False) -> None:
        if self.relay_body is None:
            return
        if not self.service.relay_master():
            self._relay_rooms = []
            self._relay_rooms_error = ""
            self._render_relay_body()
            return
        if self._relay_rooms_loading:
            return
        self._relay_rooms_loading = True
        self._relay_rooms_error = ""
        self._render_relay_body()
        try:
            rooms = await run.io_bound(self.service.relay_rooms)
            self._relay_rooms = rooms
        except Exception as exc:  # noqa: BLE001
            self._relay_rooms = []
            self._relay_rooms_error = f"房间列表加载失败：{exc}"
        finally:
            self._relay_rooms_loading = False
        if self.current_view in ("multiplayer", "relay-create"):
            self._render_relay_body()

    @_relay_ui_task
    async def _relay_search_room(self) -> None:
        code = "".join(ch for ch in str(self.relay_search_input.value or "") if ch.isdigit())
        if len(code) != 4:
            ui.notify("请输入完整的 4 位房间号", type="warning")
            return
        ui.notify("正在查找房间…")
        try:
            api, summary = await run.io_bound(self.service.relay_find_room, code)
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"查找失败：{exc}", type="negative")
            return
        await self._relay_join_dialog(api, summary)

    @_relay_ui_task
    async def _relay_begin_join(self, room: dict) -> None:
        api = str(room.get("api") or "")
        room_id = str(room.get("room_id") or "")
        if not api or not room_id:
            ui.notify("房间信息不完整，请刷新后重试", type="negative")
            return
        ui.notify("正在读取房间信息…")
        try:
            summary = await run.io_bound(self.service.relay_room_info, api, room_id)
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"读取房间失败：{exc}", type="negative")
            return
        await self._relay_join_dialog(api, summary)

    @_relay_ui_task
    async def _relay_join_dialog(self, api: str, summary: dict, *, retry: bool = False) -> None:
        host_mods = summary.get("mods") or []
        declared = bool(summary.get("mods_declared"))
        try:
            options = await run.io_bound(self.service.relay_join_options, host_mods)
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"检查模组失败：{exc}", type="negative")
            return
        state = {"version": "", "option": None}

        def best_option() -> dict:
            for item in options:
                if item.get("match"):
                    return item
            return options[0] if options else {
                "version": "",
                "match": False,
                "missing": [],
                "extra": [],
                "local_count": 0,
                "host_count": len(host_mods),
            }

        chosen = best_option()
        state["version"] = str(chosen.get("version") or "")
        state["option"] = chosen

        with ui.dialog() as dialog, ui.card().classes(
            "w-[620px] max-h-[82vh] overflow-auto gap-3"
        ):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                with ui.column().classes("gap-0 no-wrap"):
                    ui.label("加入房间").classes("text-[18px] font-semibold")
                    ui.label(str(summary.get("name") or "未命名房间")).classes("aml-sub")
                ui.label(
                    f"{summary.get('players') or 1}/{summary.get('max_players') or 8} 人"
                ).classes("aml-chip primary")

            with ui.column().classes("w-full gap-0"):
                ui.label(
                    f"房主 {summary.get('host_name') or '未知'} · "
                    f"{summary.get('version') or '未知版本'} · {summary.get('loader') or 'vanilla'}"
                ).classes("aml-sub")
                ui.label(
                    f"服务商 {summary.get('provider') or 'aml'} · 节点 {summary.get('node_id') or '未知'}"
                ).classes("aml-sub")

            details = ui.column().classes("w-full gap-2")

            def render_details() -> None:
                details.clear()
                option = state.get("option") or {}
                with details:
                    if not declared:
                        ui.label("房主未上传模组清单，将跳过模组校验直接启动。").classes(
                            "aml-sub"
                        )
                    elif options and option.get("match"):
                        ui.label("本机客户端与房主模组一致，可以自动启动并进入服务器。").classes(
                            "text-[13px]"
                        ).style("color: #16a34a")
                    else:
                        missing = option.get("missing") or []
                        extra = option.get("extra") or []
                        ui.label(
                            f"模组不一致：缺少 {len(missing)} 个，多出 {len(extra)} 个。"
                            "继续加入可能被服务器拒绝或出现异常。"
                        ).classes("text-[13px]").style("color: #d97706")
                        for item in missing[:8]:
                            name = str(item.get("filename") or item.get("name") or "未知 Mod")
                            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                                ui.label(name).classes("aml-sub truncate flex-1")
                                if item.get("modrinth_url"):
                                    ui.link("Modrinth", item["modrinth_url"]).classes("text-[12px]")
                                if item.get("curseforge_url"):
                                    ui.link("CurseForge", item["curseforge_url"]).classes(
                                        "text-[12px]"
                                    )
                                if not (item.get("modrinth_url") or item.get("curseforge_url")):
                                    ui.label("无链接").classes("aml-sub")
                        if len(missing) > 8:
                            ui.label(f"其余 {len(missing) - 8} 个已省略").classes("aml-sub")
                        unresolved = [
                            item
                            for item in missing
                            if not (item.get("modrinth_url") or item.get("curseforge_url"))
                        ]
                        if unresolved:
                            ui.label(
                                f"有 {len(unresolved)} 个 Mod 查不到链接，成员需自行安装或禁用后加入。"
                            ).classes("aml-sub")

            version_options = {
                str(item.get("version")): (
                    f"{item.get('version')} · {item.get('game_version') or ''}"
                    + (" · 一致" if item.get("match") else "")
                )
                for item in options
            }
            with ui.row().classes("w-full items-center gap-3 no-wrap"):
                version_select = ui.select(
                    version_options,
                    value=state["version"] or None,
                    label="选择启动的客户端",
                ).props("dense outlined").classes("flex-1")
                if not options:
                    ui.label("本机没有已安装客户端").classes("aml-sub")
            render_details()

            def on_version_change(_=None) -> None:
                version = str(version_select.value or "")
                state["version"] = version
                state["option"] = next(
                    (item for item in options if str(item.get("version")) == version), None
                )
                render_details()

            async def download_missing() -> None:
                option = state.get("option") or {}
                missing = option.get("missing") or []
                version = state.get("version") or ""
                if not version:
                    ui.notify("请先选择客户端", type="warning")
                    return
                if not missing:
                    ui.notify("没有缺失的 Mod")
                    return
                ui.notify("正在下载缺失 Mod…")
                try:
                    result = await run.io_bound(
                        self.service.relay_download_missing, version, missing
                    )
                except Exception as exc:  # noqa: BLE001
                    ui.notify(f"下载失败：{exc}", type="negative")
                    return
                installed = result.get("installed") or []
                skipped = result.get("skipped") or []
                msg = f"已安装 {len(installed)} 个 Mod"
                if skipped:
                    msg += f"，{len(skipped)} 个未安装"
                ui.notify(msg, type="positive" if not skipped else "warning")
                dialog.close()
                await self._relay_join_dialog(api, summary, retry=True)

            async def do_join() -> None:
                option = state.get("option") or {}
                version = state.get("version") or ""
                mismatch = declared and bool(options) and not option.get("match")
                if mismatch:
                    dialog.close()
                    # 确认框挂在联机页插槽上，避免嵌入已关闭的对话框导致不可见。
                    with self.pages["multiplayer"]:
                        with ui.dialog() as confirm, ui.card().classes("w-[420px] gap-3"):
                            ui.label("模组不一致").classes("text-[16px] font-semibold")
                            ui.label(
                                "继续加入将使用当前客户端，服务器可能拒绝连接或出现异常。是否继续？"
                            ).classes("aml-sub")
                            with ui.row().classes("w-full justify-end gap-2"):
                                ui.button("取消", on_click=confirm.close).props(
                                    "flat dense no-caps"
                                )

                                async def confirm_join() -> None:
                                    confirm.close()
                                    await self._relay_do_join(
                                        api, summary, version, launch=bool(version)
                                    )

                                ui.button("忽略风险并加入", on_click=confirm_join).props(
                                    "unelevated dense no-caps color=warning"
                                )
                    confirm.open()
                    return
                dialog.close()
                await self._relay_do_join(api, summary, version, launch=bool(version))

            async def join_only() -> None:
                dialog.close()
                await self._relay_do_join(api, summary, state.get("version") or "", launch=False)

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("取消", on_click=dialog.close).props("flat dense no-caps")
                ui.button("仅加入（不启动）", on_click=join_only).props("flat dense no-caps")
                download_button = ui.button(
                    "下载缺失 Mod", on_click=download_missing
                ).props("flat dense no-caps")
                primary_button = ui.button("启动并加入", on_click=do_join).props(
                    "unelevated dense no-caps color=primary"
                )

            def sync_buttons() -> None:
                option = state.get("option") or {}
                missing = option.get("missing") or []
                download_button.set_visibility(bool(missing))
                matched = not declared or bool(option.get("match"))
                primary_button.text = "启动并加入" if matched else "仍然加入"

            sync_buttons()
            version_select.on_value_change(
                lambda _: (on_version_change(), sync_buttons())
            )
        dialog.open()

    @_relay_ui_task
    async def _relay_do_join(
        self, api: str, summary: dict, version: str, *, launch: bool
    ) -> None:
        account = self._active_account() if launch else None
        if launch and not version:
            ui.notify("本机没有可启动的客户端，请安装后再加入", type="warning")
            return
        if launch and not account:
            ui.notify("请先登录正版账号后再启动客户端", type="warning")
            return
        ui.notify("正在建立联机隧道…")
        try:
            entry = await run.io_bound(
                lambda: self.service.relay_join_room(api, str(summary.get("room_id") or ""))
            )
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"加入房间失败：{exc}", type="negative")
            return
        self._render_relay_body(notify="已加入房间")
        if not launch or not account:
            return
        info = self.service.relay_launch_args(version, int(entry.get("bind_port") or 0))
        if not info.get("auto"):
            ui.notify(
                "当前游戏版本不支持自动进服，请在多人游戏中添加 "
                f"127.0.0.1:{entry.get('bind_port')}",
                type="warning",
            )
        await self._launch(
            version,
            username=str(account.get("name") or ""),
            account_id=account.get("id"),
            extra_game=info.get("args") or [],
        )

    @_relay_ui_task
    async def _relay_open_host_dialog(self) -> None:
        room = self.service.relay_status().get("room")
        if not room:
            ui.notify("当前没有进行中的房间", type="warning")
            return
        try:
            detail = await run.io_bound(self.service.relay_room_detail)
        except Exception:  # noqa: BLE001
            detail = room
        code = str(detail.get("join_code") or room.get("join_code") or "")
        hidden = bool(detail.get("hidden"))
        guests = detail.get("guests") or []
        with ui.dialog() as dialog, ui.card().classes("w-[480px] gap-3"):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                ui.label("我的房间").classes("text-[16px] font-semibold")
                ui.button("关闭房间", on_click=lambda: background_tasks.create(
                    self._relay_close_room_dialog(dialog)
                )).props("flat dense no-caps")
            ui.label(code).classes("text-[36px] font-semibold text-center w-full").style(
                "font-family: Consolas, monospace; letter-spacing: .18em"
            )
            ui.label(
                "房间号已隐藏，好友只能通过搜索完整房间号找到并加入。"
                if hidden else "房间已显示在联机页，好友也可以直接点击加入。"
            ).classes("aml-sub text-center")
            ui.label(
                f"版本 {detail.get('version') or '未知'} · 上限 {detail.get('max_players') or 8} 人"
                f" · 本地端口 {detail.get('local_port') or '?'}"
            ).classes("aml-sub")
            ui.label(f"已加入玩家：{len(guests)} 人").classes("aml-sub")
            self.relay_tunnel_label = ui.label(
                "隧道已连接"
                if (self.service.relay_status().get("tunnel") or {}).get("running")
                else "隧道未运行"
            ).classes("aml-sub")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button(
                    "复制房间号", on_click=lambda: self._copy(code, "房间号已复制")
                ).props("unelevated dense no-caps color=primary")
                ui.button("完成", on_click=dialog.close).props("flat dense no-caps")
        dialog.open()

    async def _relay_close_room_dialog(self, dialog) -> None:
        dialog.close()
        await self._relay_close_room()

    @_relay_ui_task
    async def _relay_start_create(self) -> None:
        if not self.service.relay_master():
            ui.notify("当前版本未内置联机服务，请更新启动器", type="warning")
            return
        ui.notify("正在扫描已启动的 Minecraft 端口…")
        try:
            info = await run.io_bound(self.service.relay_scan_ports)
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"扫描端口失败：{exc}", type="negative")
            return
        ports = [item for item in (info.get("ports") or []) if item.get("minecraft")]
        if not ports:
            self._relay_need_game_dialog()
            return
        version = str(info.get("version") or self.service.config.last_version or "")
        self._relay_create_ports = ports
        self._relay_create_version = version
        self._relay_create_mods = []
        self._relay_create_mods_found = 0
        self._relay_create_mods_missing = 0
        if version:
            try:
                self._relay_create_mods = await run.io_bound(
                    self.service.relay_local_mods, version
                )
            except Exception:  # noqa: BLE001
                self._relay_create_mods = []
        self._select_view("relay-create")

    def _relay_need_game_dialog(self) -> None:
        with ui.dialog() as dialog, ui.card().classes("w-[440px] gap-3"):
            ui.label("请先启动游戏并开放局域网").classes("text-[16px] font-semibold")
            ui.label(
                "未检测到可用的 Minecraft 监听端口。请启动游戏，按 ESC 打开菜单，"
                "点击「对局域网开放」，再回来创建房间。"
            ).classes("aml-sub")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("取消", on_click=dialog.close).props("flat dense no-caps")
                ui.button(
                    "重新检测",
                    on_click=lambda: (dialog.close(), background_tasks.create(
                        self._relay_start_create()
                    )),
                ).props("unelevated dense no-caps color=primary")
        dialog.open()

    def _render_relay_create_page(self) -> None:
        if self.relay_create_body is None:
            return
        self.relay_create_body.clear()
        with self.relay_create_body:
            if not self._relay_create_ports:
                with ui.card().classes("aml-card w-full p-6 gap-2"):
                    ui.label("尚未扫描到 Minecraft 端口").classes("text-[15px] font-semibold")
                    ui.label(
                        "请先在游戏内「对局域网开放」，然后重新扫描。"
                    ).classes("aml-sub")
                    ui.button(
                        "重新扫描",
                        on_click=lambda: background_tasks.create(self._relay_start_create()),
                    ).props("unelevated dense no-caps color=primary")
                return

            version = self._relay_create_version or self.service.config.last_version or "unknown"
            with ui.card().classes("aml-card w-full p-5 gap-3"):
                ui.label("房间信息").classes("text-[15px] font-semibold")
                with ui.row().classes("w-full gap-3 no-wrap"):
                    self.relay_create_name = ui.input(
                        "房间名称", value="我的世界联机"
                    ).props("dense outlined").classes("flex-1")
                    self.relay_create_provider = ui.select(
                        {
                            "aml": "AMLauncher 官方",
                            "chmlfrp": "ChmlFRP（暂不可用）",
                            "loliafrp": "LoliaFRP（暂不可用）",
                        },
                        value="aml",
                        label="联机服务商",
                    ).props("dense outlined").classes("w-[240px]")
                with ui.row().classes("w-full items-end gap-3 no-wrap"):
                    self.relay_create_port = ui.select(
                        {
                            int(item["port"]): f"127.0.0.1:{item['port']} · Minecraft"
                            for item in self._relay_create_ports
                        },
                        value=int(self._relay_create_ports[0]["port"]),
                        label="游戏监听端口",
                    ).props("dense outlined").classes("flex-1")
                    ui.button(
                        icon="refresh",
                        on_click=lambda: background_tasks.create(self._relay_rescan_create_ports()),
                    ).props("flat dense round size=sm").tooltip("重新扫描端口")
                ui.label(
                    f"当前客户端：{version} · 已检测端口 {len(self._relay_create_ports)} 个"
                ).classes("aml-sub")

                with ui.row().classes("w-full items-center gap-3 no-wrap"):
                    self.relay_create_players = ui.slider(
                        min=2, max=16, value=8, step=1
                    ).props("label").classes("flex-1")
                    self.relay_create_players_label = ui.label("上限 8 人").classes(
                        "aml-sub w-[68px] text-right"
                    )
                    self.relay_create_players.on_value_change(
                        lambda e: setattr(
                            self.relay_create_players_label, "text", f"上限 {e.value} 人"
                        )
                    )
                self.relay_create_hidden = ui.switch(
                    "隐藏房间（仅可通过完整房间号搜索加入）"
                ).props("dense")

            with ui.card().classes("aml-card w-full p-5 gap-2"):
                with ui.row().classes("w-full items-center justify-between no-wrap"):
                    ui.label("模组清单").classes("text-[15px] font-semibold")
                    ui.button(
                        "检查 Modrinth 链接",
                        on_click=lambda: background_tasks.create(self._relay_resolve_create_mods()),
                    ).props("flat dense no-caps")
                self.relay_create_mods_label = ui.label(
                    self._relay_create_mods_text()
                ).classes("aml-sub")
                with ui.element("div").classes(
                    "aml-dropzone w-full items-center justify-center gap-1"
                ).props("data-aml-drop=aml-relay-mods data-aml-click=1"):
                    ui.icon("upload_file").classes("text-[24px]")
                    ui.label("点击或拖入模组清单（.json / .txt）").classes("aml-sub")
                ui.upload(
                    on_upload=self._relay_upload_mods,
                    auto_upload=True,
                    multiple=False,
                ).props("id=aml-relay-mods accept=.json,.txt").classes("hidden")
                if self._relay_create_mods:
                    names = "、".join(
                        str(item.get("filename") or item.get("name") or "")
                        for item in self._relay_create_mods[:6]
                    )
                    if len(self._relay_create_mods) > 6:
                        names += f" 等 {len(self._relay_create_mods)} 个"
                    ui.label(names).classes("aml-sub truncate")
                if self._relay_create_mods_missing:
                    ui.label(
                        f"有 {self._relay_create_mods_missing} 个 Mod 未查到 Modrinth/CurseForge 链接。"
                        "成员未安装或禁用这些 Mod 时可能无法正常进入，请提示成员自行安装或接受风险。"
                    ).classes("text-[12px]").style("color: #d97706")
                ui.label(
                    "未上传清单时，成员加入将跳过模组校验；上传后会自动比对并提示缺失项。"
                ).classes("aml-sub")

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button(
                    "取消", on_click=lambda: self._select_view("multiplayer")
                ).props("flat dense no-caps")
                self.relay_create_primary = ui.button(
                    "创建房间",
                    on_click=lambda: background_tasks.create(self._relay_submit_create()),
                ).props("unelevated dense no-caps color=primary")
                self.relay_create_primary.set_enabled(bool(self._relay_create_ports))

    @_relay_ui_task
    async def _relay_rescan_create_ports(self) -> None:
        try:
            info = await run.io_bound(self.service.relay_scan_ports)
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"扫描端口失败：{exc}", type="negative")
            return
        ports = [item for item in (info.get("ports") or []) if item.get("minecraft")]
        if not ports:
            ui.notify("仍未检测到 Minecraft 端口，请先在游戏内对局域网开放", type="warning")
            return
        self._relay_create_ports = ports
        if info.get("version"):
            self._relay_create_version = str(info["version"])
        self._render_relay_create_page()
        ui.notify(f"已检测到 {len(ports)} 个 Minecraft 端口", type="positive")

    def _relay_create_mods_text(self) -> str:
        count = len(self._relay_create_mods)
        text = f"已读取 {count} 个已启用的 Mod"
        if self._relay_create_mods_found:
            text += f"，其中 {self._relay_create_mods_found} 个已找到链接"
        if self._relay_create_mods_missing:
            text += f"，{self._relay_create_mods_missing} 个没有链接"
        return text

    @_relay_ui_task
    async def _relay_upload_mods(self, event) -> None:
        try:
            raw = await event.file.read()
            text = raw.decode("utf-8-sig", errors="replace")
            mods = self._relay_parse_mod_list(text, event.file.filename or "")
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"读取模组清单失败：{exc}", type="negative")
            return
        if not mods:
            ui.notify("清单中没有可识别的 Mod", type="warning")
            return
        self._relay_create_mods = mods
        self._relay_create_mods_found = 0
        self._relay_create_mods_missing = 0
        if self.relay_create_mods_label is not None:
            self.relay_create_mods_label.text = self._relay_create_mods_text()
        ui.notify(f"已导入 {len(mods)} 个 Mod", type="positive")
        self._render_relay_create_page()

    @staticmethod
    def _relay_parse_mod_list(text: str, filename: str) -> list[dict]:
        text = str(text or "").strip()
        if not text:
            return []
        if filename.lower().endswith(".json") or text.startswith("[") or text.startswith("{"):
            import json

            payload = json.loads(text)
            if isinstance(payload, dict):
                payload = payload.get("mods") or payload.get("files") or []
            if not isinstance(payload, list):
                return []
            result = []
            for item in payload[:300]:
                if isinstance(item, str):
                    result.append({"filename": item, "name": item, "enabled": True})
                elif isinstance(item, dict):
                    entry = {
                        key: item.get(key)
                        for key in (
                            "filename",
                            "name",
                            "version",
                            "sha1",
                            "modrinth_url",
                            "curseforge_url",
                            "download_url",
                        )
                        if item.get(key)
                    }
                    entry.setdefault("enabled", bool(item.get("enabled", True)))
                    if entry.get("filename") or entry.get("name"):
                        result.append(entry)
            return result
        result = []
        for line in text.splitlines():
            name = line.strip()
            if not name or name.startswith("#"):
                continue
            result.append({"filename": name, "name": name, "enabled": True})
        return result[:300]

    @_relay_ui_task
    async def _relay_resolve_create_mods(self) -> None:
        if not self._relay_create_mods:
            ui.notify("请先读取或上传模组清单", type="warning")
            return
        if self.relay_create_mods_label is not None:
            self.relay_create_mods_label.text = "正在查询 Modrinth 链接…"
        try:
            result = await run.io_bound(
                self.service.relay_resolve_mods, self._relay_create_mods
            )
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"查询链接失败：{exc}", type="negative")
            return
        self._relay_create_mods = result.get("mods") or self._relay_create_mods
        self._relay_create_mods_found = int(result.get("found") or 0)
        self._relay_create_mods_missing = int(result.get("missing") or 0)
        self._render_relay_create_page()
        ui.notify(
            f"已找到 {self._relay_create_mods_found} 个链接，"
            f"{self._relay_create_mods_missing} 个未找到",
            type="positive" if not self._relay_create_mods_missing else "warning",
        )

    @_relay_ui_task
    async def _relay_submit_create(self) -> None:
        provider = str(self.relay_create_provider.value or "")
        if provider != "aml":
            ui.notify("ChmlFRP / LoliaFRP 暂不可用，请选择 AMLauncher 官方", type="warning")
            return
        port = int(self.relay_create_port.value or 0)
        if not port:
            ui.notify("请选择游戏监听端口", type="warning")
            return
        name = str(self.relay_create_name.value or "").strip() or "我的世界联机"
        players = int(self.relay_create_players.value or 8)
        hidden = bool(self.relay_create_hidden.value)
        mods = list(self._relay_create_mods)
        if mods and self._relay_create_mods_missing:
            with ui.dialog() as dialog, ui.card().classes("w-[440px] gap-3"):
                ui.label("部分 Mod 没有链接").classes("text-[16px] font-semibold")
                ui.label(
                    f"有 {self._relay_create_mods_missing} 个 Mod 无法提供下载链接，"
                    "成员未安装时可能无法进入。是否仍然创建？"
                ).classes("aml-sub")
                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("取消", on_click=dialog.close).props("flat dense no-caps")

                    async def confirm() -> None:
                        dialog.close()
                        await self._relay_create_now(
                            name, port, players, hidden, mods
                        )

                    ui.button("仍然创建", on_click=confirm).props(
                        "unelevated dense no-caps color=warning"
                    )
            dialog.open()
            return
        await self._relay_create_now(name, port, players, hidden, mods)

    @_relay_ui_task
    async def _relay_create_now(
        self, name: str, port: int, players: int, hidden: bool, mods: list[dict]
    ) -> None:
        version = self._relay_create_version or "unknown"
        ui.notify("正在创建房间，首次使用会下载 frpc…")
        try:
            room = await run.io_bound(
                lambda: self.service.relay_create_room(
                    name=name,
                    version=version,
                    loader=self.service.config.mod_loader,
                    max_players=players,
                    local_port=port,
                    provider="aml",
                    hidden=hidden,
                    mods=mods,
                    mods_declared=True,
                )
            )
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"创建房间失败：{exc}", type="negative")
            return
        self._start_heartbeat()
        self._select_view("multiplayer")
        await self._relay_refresh_rooms()
        self._relay_show_code(room, hidden)

    def _relay_show_code(self, room: dict, hidden: bool) -> None:
        code = str(room.get("join_code") or "")
        # 创建页会切回联机页，弹窗必须挂在联机页插槽上才会显示。
        with self.pages["multiplayer"]:
            with ui.dialog() as dialog, ui.card().classes("w-[420px] gap-3 items-center"):
                ui.label("房间已创建").classes("text-[16px] font-semibold")
                ui.label(code).classes("text-[40px] font-semibold").style(
                    "font-family: Consolas, monospace; letter-spacing: .16em"
                )
                ui.label(
                    "房间已隐藏，好友需搜索完整房间号加入。"
                    if hidden else "把房间号发给好友，或让好友在联机页直接点击加入。"
                ).classes("aml-sub text-center")
                with ui.row().classes("justify-center gap-2"):
                    ui.button(
                        "复制房间号", on_click=lambda: self._copy(code, "房间号已复制")
                    ).props("unelevated dense no-caps color=primary")
                    ui.button("完成", on_click=dialog.close).props("flat dense no-caps")
        dialog.open()

    def _start_heartbeat(self) -> None:
        if self.relay_heartbeat_timer is None:
            self.relay_heartbeat_timer = ui.timer(15.0, self._relay_heartbeat)
        self.relay_heartbeat_timer.active = True

    async def _relay_heartbeat(self) -> None:
        room = self.service.relay_status().get("room")
        if not room:
            self.relay_heartbeat_timer.active = False
            return
        try:
            info = await run.io_bound(
                lambda: self.service.relay_heartbeat(room["api"], room["room_id"])
            )
        except Exception as exc:  # noqa: BLE001
            self.relay_heartbeat_timer.active = False
            ui.notify(f"房间连接已断开：{exc}", type="negative")
            return
        if self.relay_players_label is not None:
            self.relay_players_label.text = f"在线玩家：{info.get('players') or 1} 人"

    async def _relay_close_room(self) -> None:
        await run.io_bound(self.service.relay_close_room)
        if self.relay_heartbeat_timer is not None:
            self.relay_heartbeat_timer.active = False
        self._render_relay_body(notify="房间已关闭")

    async def _relay_leave_room(self) -> None:
        await run.io_bound(self.service.relay_leave_room)
        self._render_relay_body(notify="已退出房间")

    def _copy(self, text: str, message: str) -> None:
        if not text:
            return
        ui.clipboard.write(text)
        ui.notify(message, type="positive")


def setup_pages(service: LauncherService, *, debug: bool = False) -> None:
    """注册 NiceGUI 页面。"""

    @ui.page("/")
    async def index() -> None:
        ui.page_title(APP_NAME)
        launcher = LauncherUI(service, debug=debug)
        await launcher.build()

    service.log("界面已就绪：启动 / 版本 / 下载 / 设置")
