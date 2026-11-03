"""启动器入口。

    python main.py                 # 有边框的桌面窗口（native）
    python main.py --web           # 浏览器模式（无桌面环境 / 调试用）

所有数据（游戏文件 / 配置 / 账户）统一保存在运行目录下的 data。

打包（PyInstaller）时必须先调用 multiprocessing.freeze_support()，
否则 native 模式的子进程会反复重启主程序。
"""

from __future__ import annotations

import argparse
import atexit
import multiprocessing
import socket
import sys
from pathlib import Path

from nicegui import app, ui

from app.api import register_api
from app.config import ConfigStore, runtime_dir
from app.secrets import SecretStore
from app.services import LauncherService
from app.tasks import TaskManager
from app.ui import native_maximize_guard, setup_pages
from engine.version import APP_NAME, APP_VERSION


def _webview_available() -> bool:
    try:
        import webview  # noqa: F401  pywebview，NiceGUI native 模式依赖它

        return True
    except Exception:  # noqa: BLE001 - 缺少 GUI 依赖时降级
        return False


def build_app(*, debug: bool = False) -> LauncherService:
    store = ConfigStore()

    tasks = TaskManager()
    service = LauncherService(store, SecretStore(), tasks)
    service.paths().ensure()
    service.log(f"游戏目录：{service.config.root}")

    register_api(service)
    setup_pages(service, debug=debug)
    return service


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()

    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    parser.add_argument("--web", action="store_true", help="使用浏览器而非桌面窗口")
    parser.add_argument("--port", type=int, default=0, help="监听端口，0 表示自动")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--reload", action="store_true", help="开发模式热重载")
    parser.add_argument("--debug", action="store_true", help="显示运行日志面板")
    args = parser.parse_args(argv)

    service = build_app(debug=args.debug)
    # 关闭窗口 / 系统关机时结束游戏并回收哈希存储的工作目录。
    atexit.register(service.shutdown_storage)
    app.on_shutdown(service.shutdown_storage)
    port = args.port or _free_port()

    native = not args.web
    if native and not _webview_available():
        service.log("未检测到 pywebview，自动降级为浏览器模式")
        native = False

    if native:
        service.log("以桌面窗口模式启动（无边框，内容按比例缩放）")
    else:
        service.log(f"以浏览器模式启动：http://{args.host}:{port}")

    run_kwargs: dict = dict(
        host=args.host,
        port=port,
        title=APP_NAME,
        native=native,
        reload=args.reload,
        show=native,
        favicon=_icon_path(),
        uvicorn_logging_level="info" if args.debug else "warning",
    )
    if native:
        # window_size / frameless 只要传值就会激活 native，必须只在 native 模式下传。
        run_kwargs["window_size"] = (1100, 720)
        run_kwargs["frameless"] = True  # 无边框窗口，标题栏与窗口按钮由界面自绘
        # 拖动区域只响应自身，避免点击导航/窗口按钮时误触发拖拽。
        app.native.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = True
        app.native.window_args["min_size"] = (880, 576)  # 与 1100x720 同比例
        app.native.window_args["easy_drag"] = False  # 仅允许在拖动区域拖动窗口
        # 窗口运行在 NiceGUI 的子进程里，最大化范围只能在那里设置：
        # 否则无边框窗口最大化会盖住任务栏。
        app.native.start_args["func"] = native_maximize_guard

    ui.run(**run_kwargs)
    return 0


def _icon_path() -> str:
    """优先使用打包/源码目录下的 AML 图标。"""
    base = Path(getattr(sys, "_MEIPASS", runtime_dir()))
    icon = base / "assets" / "aml.ico"
    return str(icon) if icon.exists() else "🎮"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    sys.exit(main())