"""构建单文件便携版：输出到 ``dist/<版本号>/A Minecraft Launcher.exe``。

用法：
    python build.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from engine.version import APP_VERSION

ROOT = Path(__file__).resolve().parent


def main() -> int:
    dist = ROOT / "dist" / APP_VERSION
    work = ROOT / "build" / APP_VERSION
    dist.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "aminecraftlauncher.spec",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(dist),
        "--workpath",
        str(work),
    ]
    print(f"构建 {APP_VERSION} -> {dist}")
    code = subprocess.call(command, cwd=ROOT)
    if code == 0:
        # 清理其它版本目录。
        for child in (ROOT / "dist").iterdir():
            if child.is_dir() and child.name != APP_VERSION:
                shutil.rmtree(child, ignore_errors=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
