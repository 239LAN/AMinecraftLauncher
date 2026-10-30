"""NiceGUI 界面：启动 / 版本 / 下载 / 设置。

页面职责：
- 启动：只放登录（离线）与启动游戏；
- 版本：左侧本地版本列表，右侧该版本的 Mod 管理与启动设置；
- 下载：游戏本体 / Mod / 整合包 / 资源包 / 光影包，按所选版本自动筛选；
- 设置：启动器设置与游戏设置两类。
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from nicegui import app, background_tasks, run, ui

from engine.version import APP_NAME, APP_VERSION

from .services import LauncherService

NAV_ITEMS = [
    ("home", "home", "启动"),
    ("versions", "inventory_2", "版本"),
    ("download", "cloud_download", "下载"),
    ("settings", "settings", "设置"),
]

# 更新日志：从新到旧，主页默认展开最新一条，其余折叠。
CHANGELOG = [
    {
        "version": "1.0.1",
        "title": "界面重构 · CurseForge 镜像",
        "notes": """\
- 界面重构为简洁浅色风格：白色卡片 + 单一蓝色强调，去掉渐变与光晕
- 页面切换淡入、按钮按压缩放动画；导航更名「主页」为「启动」
- 主页左侧改为更新日志，默认展开最新版本，收起只显示版本号
- 导入本地 Mod / 整合包改为单个拖放卡片，导入进度原位展示
- 启动页拖入 .jar 或整合包可自动识别，确认后直接导入
- 启动过程改为启动页内联展示步骤与进度，支持取消，不再弹窗
- 创建任务后自动跳转任务页，结束后 3 秒自动移除
- CurseForge 改走 mcimirror 公共镜像，无需 API Key
- 修复边缘 CDN 证书链缺失导致的下载失败
- 构建产物按版本目录分开保存，保留历史版本
""",
    },
    {
        "version": "1.0.0",
        "title": "首个正式版本",
        "notes": """\
- 离线登录、一键启动，启动过程实时展示进度
- 版本安装、补全缺失文件、版本隔离、删除与崩溃日志诊断
- Mod / 资源包 / 光影包 / 整合包下载，支持本地整合包导入
- 自动检测本机 Java，缺失时自动下载安装
- 多线程并发 + 大文件分片 + 断点续传，镜像失败自动回退官方源
- 任务中心统一展示进度、速度与已完成大小
- 应用图标、版本信息与单文件便携版
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

LOADERS = ["fabric", "forge", "neoforge", "quilt"]

AML_CSS = """
:root {
  --aml-bg: #f6f7f9;
  --aml-surface: #ffffff;
  --aml-border: #e5e7eb;
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
.aml-logo {
  width: 36px; height: 36px; border-radius: 11px;
  display: flex; align-items: center; justify-content: center;
  background: var(--aml-accent); color: #fff;
}
.aml-logo .q-icon { color: #fff; font-size: 24px !important; }
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

.aml-hero-login {
  background: var(--aml-surface);
  border: 1px solid var(--aml-border);
  border-radius: 16px;
  box-shadow: 0 1px 2px rgba(17, 24, 39, 0.04);
}

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
        self._launch_cancel = threading.Event()
        self._launch_progress: dict = {"percent": 0.0, "detail": "准备中…"}
        self._launch_done = False
        self._launch_error = ""
        self._log_revision = -1

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

    def _build_topbar(self) -> None:
        with ui.row().classes(
            "aml-topbar w-full h-[56px] shrink-0 items-center no-wrap px-4 gap-2"
        ):
            with ui.row().classes("items-center gap-2.5 no-wrap shrink-0"):
                with ui.element("div").classes("aml-logo"):
                    ui.icon("auto_awesome").classes("text-[19px]")
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

            # 关闭按钮始终可用：原生模式销毁窗口，Web 模式尝试关闭标签页。
            with ui.row().classes("items-center gap-0 no-wrap shrink-0 pl-2"):
                if app.native.main_window is not None:
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
        return app.native.main_window

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
        # 原生窗口直接销毁；Web 模式退化为尝试关闭浏览器标签。
        ui.run_javascript("window.close()")
        window = self._native_window()
        if window is not None:
            window.destroy()

    def _build_pages(self) -> None:
        with ui.element("div").classes("w-full flex-1 min-h-0"):
            self._build_home_page()
            self._build_versions_page()
            self._build_download_page()
            self._build_tasks_page()
            self._build_settings_page()

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
        active_key = "download" if key == "tasks" else key
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
                    ):
                        for index, entry in enumerate(CHANGELOG):
                            with ui.expansion(
                                f"v{entry['version']}",
                                group="aml-changelog",
                                value=index == 0,
                            ).classes("aml-logitem w-full"):
                                ui.label(entry["title"]).classes("aml-log-title")
                                ui.markdown(entry["notes"]).classes("aml-log-body")
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
        with self.home_identity:
            self._render_avatar(account)
            if account:
                ui.label(account["name"]).classes("text-[18px] font-semibold")
                with ui.row().classes("items-center gap-2 no-wrap"):
                    ui.label("离线登录").classes("aml-chip")
                    ui.button("退出登录", on_click=self._logout_home).props(
                        "flat dense no-caps"
                    ).classes("aml-sub")
            else:
                self.home_name = ui.input(placeholder="输入用户名").classes(
                    "w-[220px]"
                ).props("dense outlined")
                self.home_name.on(
                    "keydown.enter", lambda: background_tasks.create(self._home_login())
                )
                ui.button("登录", icon="login", on_click=self._home_login).classes(
                    "w-[220px]"
                ).props("unelevated dense no-caps color=primary")
                ui.label("暂支持离线登录").classes("aml-sub")

    def _render_avatar(self, account: Optional[dict]) -> None:
        url = ""
        if account:
            try:
                url = self.service.account_skin(account["id"]).get("avatar") or ""
            except Exception:  # noqa: BLE001 - 皮肤服务不可用时用占位头像
                url = ""
        with ui.element("div").classes(
            "aml-avatar overflow-hidden flex items-center justify-center"
        ).style(
            "width:84px;height:84px;border-radius:14px;"
            "background:#f3f4f6;border:1px solid #e5e7eb"
        ):
            if url:
                ui.image(url).style("width:84px;height:84px;object-fit:cover")
            else:
                ui.icon("person").classes("text-[44px]").style("color: var(--aml-muted)")

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
        self._refresh_home()

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

    async def _handle_home_files(self, event) -> None:
        """启动页拖入：按扩展名自动识别 Mod / 整合包，解析后弹窗确认。"""
        files = list(getattr(event, "files", []) or [])
        if not files:
            return
        version = self.home_version.value or self.service.config.last_version
        items: list[dict] = []
        errors: list[str] = []

        for file in files:
            filename = Path(file.name).name
            suffix = Path(filename).suffix.lower()
            data = await file.read()
            if suffix in self.MOD_SUFFIXES:
                if not version:
                    errors.append(f"{filename}：请先选择启动版本")
                    continue
                with tempfile.NamedTemporaryFile(delete=False, suffix=".jar") as handle:
                    handle.write(data)
                    temp_path = Path(handle.name)
                items.append(
                    {"kind": "mod", "name": filename, "path": temp_path, "target": version}
                )
            elif suffix in self.PACK_SUFFIXES:
                target_dir = self.service.modpack_dir()
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / filename
                target.write_bytes(data)
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
        self._refresh_home()

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
                        ui.label(version["loader"]).classes("aml-chip primary")
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
                            ui.label(profile["loader"]).classes("aml-chip primary")
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
                        "删除", icon="delete",
                        on_click=lambda _=None, v=version_id: self._open_delete_version(v),
                    ).props("flat dense no-caps color=negative")

            ui.toggle(
                {"mods": "Mod 管理", "settings": "版本设置"},
                value=self.version_tab,
                on_change=lambda e: self._switch_version_tab(e.value),
            ).props("unelevated dense no-caps toggle-color=primary").classes("self-start")

            mods_box = ui.column().classes("w-full gap-3").props("id=aml-vtab-mods")
            settings_box = ui.column().classes("w-full gap-3").props("id=aml-vtab-settings")
            with mods_box:
                self._build_version_mods(version_id)
            with settings_box:
                self._build_version_repair(version_id)
                self._build_version_settings(version_id)
                self._build_version_loader(version_id)
                self._build_version_crash(version_id)

        self.version_tab_boxes = {"mods": mods_box, "settings": settings_box}
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
        with ui.column().classes("aml-card flat w-full p-4 gap-2"):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                ui.label("Mod 管理").classes("text-[14px] font-semibold")
                ui.button("刷新", icon="refresh", on_click=lambda: reload_mods()).props("flat dense no-caps")
            ui.label(f"目录：{mods_dir}").classes("aml-sub")

            async def on_mod_files(event) -> None:
                await self._handle_mod_uploads(event, version_id, reload_mods, drop)

            drop = ImportDrop(
                upload_id=f"aml-mod-upload-{safe_id}",
                hint="点击选择或拖入 Mod（.jar，可多选）",
                handler=on_mod_files,
            ).build()
            mod_list = ui.column().classes("w-full gap-1")

        def reload_mods() -> None:
            mod_list.clear()
            try:
                items = self.service.content_list("mod", version_id)
            except Exception as exc:  # noqa: BLE001
                with mod_list:
                    ui.label(f"读取失败：{exc}").classes("aml-sub")
                return
            with mod_list:
                if not items:
                    ui.label("暂无 Mod").classes("aml-sub")
                for item in items:
                    with ui.row().classes(
                        "w-full items-center justify-between no-wrap px-2 py-1 rounded"
                    ).style("background: var(--aml-surface-2)"):
                        with ui.column().classes("gap-0 min-w-0"):
                            with ui.row().classes("items-center gap-2 no-wrap"):
                                ui.label(item["filename"]).classes("text-[13px]")
                                ui.label("已启用" if item["enabled"] else "已禁用").classes(
                                    "aml-chip installed" if item["enabled"] else "aml-chip"
                                )
                            ui.label(_fmt_size(item.get("size") or 0)).classes("aml-sub")
                        with ui.row().classes("gap-1 shrink-0 no-wrap"):
                            ui.button(
                                "禁用" if item["enabled"] else "启用",
                                on_click=lambda _=None, i=item: self._toggle_mod(version_id, i, reload_mods),
                            ).props("flat dense no-caps")
                            ui.button(
                                "删除",
                                on_click=lambda _=None, f=item["filename"]: self._delete_mod(
                                    version_id, f, reload_mods
                                ),
                            ).props("flat dense no-caps color=negative")

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
            data = await file.read()
            with tempfile.NamedTemporaryFile(delete=False, suffix=".jar") as handle:
                handle.write(data)
                temp_path = Path(handle.name)
            try:
                await run.io_bound(
                    self.service.content_import, "mod", str(temp_path),
                    version_id=version_id, overwrite=True,
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
                loader = ui.select(LOADERS, value="fabric", label="加载器").classes(
                    "w-[130px]"
                ).props("dense outlined")
                game_version = ui.input(label="原版版本", value=base_version).classes(
                    "w-[140px]"
                ).props("dense outlined")
                loader_versions = ui.select(options=[], label="加载器版本", with_input=True).classes(
                    "flex-1"
                ).props("dense outlined")

            def fetch_loader_versions() -> None:
                try:
                    items = self.service.loader_versions(
                        loader.value, (game_version.value or "").strip()
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

        def run_crash() -> None:
            crash_box.clear()
            with crash_box:
                ui.spinner(size="sm").style("color: var(--aml-accent)")
            try:
                report = self.service.analyze_crash(version_id)
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
                    ui.label(severity).classes("aml-chip warn" if severity != "low" else "aml-chip")
                ui.label(cause.get("suggestion") or "").classes("aml-sub")
                if cause.get("evidence"):
                    ui.label(cause["evidence"]).classes("aml-sub").style(
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
                            on_click=lambda _=None, v=version: self._install_game(v["id"]),
                        ).props("unelevated dense no-caps color=primary").classes("shrink-0")

    def _install_game(self, version_id: str) -> None:
        self.service.start_install(version_id)
        self._select_view("tasks")
        ui.notify(f"已开始安装 {version_id}，进度见「任务」页", position="top-right")

    # ------------------------------------------------- 下载：Mod / 资源包 / 光影
    def _build_content_download(self, kind: str) -> None:
        version_id = self._selected_download_version()
        if not version_id:
            ui.label("请先安装并在上方选择本地游戏版本").classes("aml-sub p-2")
            return
        profile = self.service.version_profile(version_id)
        loader = profile["loader"] if profile["loader"] != "vanilla" else None

        with ui.row().classes("aml-card flat w-full items-center gap-3 p-3 no-wrap"):
            self.content_source = ui.select(
                {"modrinth": "Modrinth", "curseforge": "CurseForge"},
                value=self.service.config.mod_source, label="来源",
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
            + (f" · 加载器 {loader}" if loader else " · 原版")
        ).classes("aml-sub")

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
        self.service.start_online_modpack_install(
            item["source"], item["project_id"], name=item["name"]
        )
        self._select_view("tasks")
        ui.notify(f"已开始安装 {item['name']}，进度见「任务」页", position="top-right")

    async def _handle_modpack_files(self, event, drop: ImportDrop) -> None:
        files = list(getattr(event, "files", []) or [])
        if not files:
            return
        file = files[0]
        filename = Path(file.name).name
        if Path(filename).suffix.lower() not in {".mrpack", ".zip"}:
            ui.notify("只支持 .mrpack / .zip 整合包", type="warning", position="top-right")
            return
        data = await file.read()
        target_dir = self.service.modpack_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / filename
        target.write_bytes(data)

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

        label = f"{info.get('name') or filename} · {version_id}"
        if info.get("loader"):
            label += f" {info['loader']}"
        drop.busy(f"正在导入 {label}", percent=0)
        task = self.service.start_modpack_install(str(target), version_id=version_id)
        status, error = await self._await_import_task(task, drop, label)
        drop.idle()
        self._refresh_home()
        if status == "done":
            ui.notify(f"整合包已导入：{label}", type="positive", position="top-right")
        elif error:
            ui.notify(f"导入失败：{error}", type="negative", position="top-right")
        else:
            ui.notify("导入已取消", type="warning", position="top-right")

    async def _await_import_task(self, task, drop: ImportDrop, label: str) -> tuple[str, str]:
        """轮询导入任务，把进度直接写回导入卡片。"""
        while True:
            snapshot = self.service.tasks.snapshot(task.id)
            if snapshot is None:
                return "failed", "任务已结束"
            status = str(snapshot.get("status") or "running")
            percent = float(snapshot.get("percent") or 0.0)
            if status not in {"done", "failed", "cancelled"}:
                drop.busy(
                    f"正在导入 {label}" + (f" · {percent:.0f}%" if percent > 0 else ""),
                    percent=percent if percent > 0 else None,
                )
                await asyncio.sleep(0.4)
                continue
            return status, str(snapshot.get("error") or "")

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
            elif status == "failed":
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
            percent = min(100.0, progress.get("done", 0) / progress["total"] * 100)
        if snapshot["status"] == "done":
            percent = 100.0
        return percent

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
            percent = min(100.0, done_bytes / total_bytes * 100)
        elif running:
            percent = sum(float(s.get("percent") or 0) for s in running) / len(running)
        else:
            percent = sum(
                100.0 if s["status"] == "done" else float(s.get("percent") or 0)
                for s in snapshots
            ) / len(snapshots)

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
            if result.get("path"):
                return str(result["path"])
            return "已完成"
        if progress.get("type") == "phase":
            return f"{progress.get('phase', '处理中')} {progress.get('done', 0)}/{progress.get('total', 0)}"
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
                    "BMCLAPI 镜像加速（镜像优先，失败自动回退官方）",
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
                    LOADERS, value=view["mod_loader"], label="默认加载器"
                ).classes("flex-1").props("dense outlined")
            self.set_include_snapshots = ui.switch(
                "版本列表包含快照", value=bool(view["include_snapshots"])
            ).props("dense")

        with ui.column().classes("aml-card flat w-full p-4 gap-2"):
            ui.label("账号").classes("text-[14px] font-semibold")
            self.set_client_id = ui.input(
                label="微软客户端 ID", value=view["client_id"]
            ).classes("w-full").props("dense outlined")

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
            "client_id": self.set_client_id.value,
        }
        try:
            view = await run.io_bound(self.service.update_config, changes)
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"保存失败：{exc}", type="negative", position="top-right")
            return
        self.mirror_hint.text = (
            f"当前下载源：{view['mirror'].get('active') or view['mirror'].get('source')}"
        )
        ui.notify("启动器设置已保存", type="positive", position="top-right")

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
        background_tasks.create(self._run_launch(version, username, account_id, inline=inline))

    async def _run_launch(
        self, version: str, username: str, account_id: Optional[str], *, inline: bool = False
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
            return
        self._launch_done = True


def setup_pages(service: LauncherService, *, debug: bool = False) -> None:
    """注册 NiceGUI 页面。"""

    @ui.page("/")
    async def index() -> None:
        ui.page_title(APP_NAME)
        launcher = LauncherUI(service, debug=debug)
        await launcher.build()

    service.log("界面已就绪：启动 / 版本 / 下载 / 设置")
