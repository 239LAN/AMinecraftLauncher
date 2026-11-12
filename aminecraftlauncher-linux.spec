# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（Linux）：单文件 ELF，原生桌面窗口。

窗口层使用 pywebview 的 Qt 后端（PyQt5 + QtWebEngine），与 GTK 方案相比
依赖全部来自 pip，PyInstaller 钩子也更成熟，便于在 CI 里稳定复现。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []

# NiceGUI 静态资源、keyring 后端、pywebview 各平台实现都在运行时才导入。
for package in ("nicegui", "keyring", "webview"):
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    except Exception:  # 缺少可选依赖时跳过
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
    # keyring 的 Linux 后端
    "keyring.backends.SecretService",
    "keyring.backends.chainer",
    # pywebview 的 Qt 后端（Linux 原生窗口）
    "webview.platforms.qt",
    "qtpy",
    "qtpy.QtCore",
    "qtpy.QtGui",
    "qtpy.QtWidgets",
    "PyQt5",
    "PyQt5.QtCore",
    "PyQt5.QtGui",
    "PyQt5.QtWidgets",
    "PyQt5.QtWebEngineWidgets",
    "PyQt5.QtWebEngineCore",
    # 自研包
    "engine",
    "app",
]

datas += [
    ("assets/aml.png", "assets"),
    ("assets/steve.png", "assets"),
    ("README.md", "."),
    ("LICENSE", "."),
]

# 联机用的 frpc（Linux 版）
if Path("frpc").is_file():
    datas.append(("frpc", "."))

# 主节点地址：该文件不入库，只在打包时写进可执行文件
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
    excludes=["tkinter", "PySide2", "PySide6"],
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
    console=False,
)
