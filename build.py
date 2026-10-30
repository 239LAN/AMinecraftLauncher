"""构建单文件便携版：输出到 ``dist/<版本号>/A Minecraft Launcher.exe``。

用法：
    python build.py

历史版本目录会保留，不会互相覆盖。
"""

from __future__ import annotations

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
    return subprocess.call(command, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
