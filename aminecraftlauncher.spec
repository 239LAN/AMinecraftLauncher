# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：``pyinstaller aminecraftlauncher.spec``

单文件便携版（onefile）：所有依赖打进 ``A Minecraft Launcher.exe``，
首次运行会解压到临时目录；数据仍保存在 exe 同级的 data 目录，便于整体拷贝。

要点：
- 入口 ``main.py`` 已在 ``__main__`` 里调用 ``multiprocessing.freeze_support()``，
  native 模式（pywebview 在子进程中运行）才不会反复重启主程序；
- NiceGUI 的静态资源、uvicorn 的动态协议/循环、keyring 后端都是运行时才被导入，
  必须显式收集，否则打包后会缺文件或报 ImportError。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []

# 这三个包都依赖运行时才发现的数据文件 / 子模块。
for package in ("nicegui", "keyring", "webview"):
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    except Exception:  # 未安装 webview 时仍可打包为纯浏览器模式
        continue
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

hiddenimports += [
    # uvicorn 按字符串动态加载这些模块
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    # keyring 各平台后端
    "keyring.backends.SecretService",
    "keyring.backends.Windows",
    "keyring.backends.macOS",
    "keyring.backends.chainer",
    # 自研包
    "engine",
    "app",
]

# 图标（窗口 / exe）与界面 favicon；README 与许可证随包分发
datas += [
    ("assets/aml.ico", "assets"),
    ("assets/aml.png", "assets"),
    ("assets/steve.png", "assets"),
    ("README.md", "."),
    ("LICENSE", "."),
]

# 内置 frpc：联机时直接复制使用，不再联网下载
if Path("frpc.exe").is_file():
    datas.append(("frpc.exe", "."))

# 主节点地址：该文件不入库，只在打包时写进 exe
if Path("relay_endpoint.txt").is_file():
    datas.append(("relay_endpoint.txt", "."))

analysis = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PySide2", "PySide6"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    exclude_binaries=False,
    name="A Minecraft Launcher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # 桌面启动器不弹控制台；调试时改成 True 可看到日志
    icon="assets/aml.ico",
    version="version_info.txt",
)