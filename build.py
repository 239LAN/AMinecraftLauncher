"""构建单文件便携版。

Windows 输出 ``dist/<版本号>/A Minecraft Launcher.exe``（桌面窗口）；
Linux 输出 ``dist/<版本号>/A Minecraft Launcher``（浏览器模式，单文件 ELF）。

用法：
    python build.py                      # 按当前系统选择打包配置
    python build.py --spec xxx.spec      # 手动指定 spec
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from engine.version import APP_VERSION

ROOT = Path(__file__).resolve().parent

APP_NAME = "A Minecraft Launcher"


def default_target() -> tuple[str, str]:
    """返回 (spec 文件, 产物文件名)。"""
    if sys.platform.startswith("linux"):
        return "aminecraftlauncher-linux.spec", APP_NAME
    return "aminecraftlauncher.spec", f"{APP_NAME}.exe"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建单文件启动器")
    parser.add_argument("--spec", default="", help="自定义 PyInstaller spec")
    args = parser.parse_args(argv)

    spec, artifact = default_target()
    if args.spec:
        spec = args.spec

    dist = ROOT / "dist" / APP_VERSION
    work = ROOT / "build" / APP_VERSION
    dist.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        spec,
        "--noconfirm",
        "--clean",
        "--distpath",
        str(dist),
        "--workpath",
        str(work),
    ]
    print(f"构建 {APP_VERSION}（{sys.platform}，spec={spec}）-> {dist}")
    code = subprocess.call(command, cwd=ROOT)
    if code == 0:
        target = dist / artifact
        if target.is_file():
            print(f"产物：{target}（{target.stat().st_size} 字节）")
        else:
            print(f"警告：未找到预期产物 {target}")
        # 清理其它版本目录；带 data/ 的目录保留，避免误删存档、账号与配置。
        for child in (ROOT / "dist").iterdir():
            if child.is_dir() and child.name != APP_VERSION:
                if (child / "data").exists():
                    print(f"保留 {child.name}/data（运行数据不删除）")
                    continue
                shutil.rmtree(child, ignore_errors=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
