"""引擎闭环 CLI：versions / install / launch。

    python cli.py versions
    python cli.py install 1.20.1 --root ./mc
    python cli.py launch 1.20.1 --username Steve --dry-run
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from engine import Account, Downloader, GamePaths, LaunchBuilder, VersionResolver
from engine.downloader import DownloadStats


def _print_progress(event: dict) -> None:
    if event.get("type") != "progress":
        return
    percent = event.get("percent", 0.0)
    done = event.get("files_done", 0)
    total = event.get("files_total", 0)
    current = event.get("current", "")
    sys.stdout.write(
        f"\r  [{percent:6.2f}%] {done}/{total} 文件  {current[:48]:<48}"
    )
    sys.stdout.flush()
    if current == "done":
        sys.stdout.write("\n")


def _install(root: Path, version: str, workers: int) -> DownloadStats:
    paths = GamePaths(root)
    paths.ensure()

    resolver = VersionResolver(paths)
    print(f"解析版本 {version} ...")
    resolved = resolver.resolve(version, progress=lambda phase, i, n: None)
    print(f"  文件 {len(resolved.files)} 个，库 {len(resolved.libraries)} 个，"
          f"Java {resolved.java_major}+")

    downloader = Downloader(workers=workers, progress=_print_progress)
    stats = downloader.download(resolved.files)
    print(f"完成：下载 {stats.files_downloaded}，"
          f"跳过 {stats.files_skipped}，失败 {len(stats.failed)}")
    for target, error in stats.failed:
        print(f"  ! {target}: {error}")
    return stats


def cmd_versions(args: argparse.Namespace) -> int:
    resolver = VersionResolver(GamePaths(args.root))
    versions = resolver.list_versions(include_snapshots=args.snapshots)
    for info in versions[: args.limit]:
        print(f"{info.id:<20} {info.type:<10} {info.release_time}")
    print(f"共 {len(versions)} 个版本")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    stats = _install(Path(args.root), args.version, args.workers)
    return 0 if stats.ok else 1


def cmd_launch(args: argparse.Namespace) -> int:
    paths = GamePaths(Path(args.root), java=args.java)
    resolver = VersionResolver(paths)
    resolved = resolver.resolve(args.version, include_assets=False)

    account = Account.offline_account(args.username)
    builder = LaunchBuilder(paths)
    command = builder.build(
        resolved,
        account,
        memory_mb=args.memory,
    )

    print(builder.describe(command))
    if args.dry_run:
        print("(dry-run，未真正启动)")
        return 0

    process = subprocess.Popen(
        command,
        cwd=str(paths.root),
        stdout=None,
        stderr=None,
    )
    print(f"已启动，PID={process.pid}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Minecraft 引擎闭环 CLI")
    parser.add_argument("--root", default="./mc", help="游戏根目录")
    sub = parser.add_subparsers(dest="command", required=True)

    p_versions = sub.add_parser("versions", help="列出可用版本")
    p_versions.add_argument("--snapshots", action="store_true", help="包含快照")
    p_versions.add_argument("--limit", type=int, default=20)
    p_versions.set_defaults(func=cmd_versions)

    p_install = sub.add_parser("install", help="解析并下载版本文件")
    p_install.add_argument("version")
    p_install.add_argument("--workers", type=int, default=16)
    p_install.set_defaults(func=cmd_install)

    p_launch = sub.add_parser("launch", help="构建命令行并启动（离线）")
    p_launch.add_argument("version")
    p_launch.add_argument("--username", default="Steve")
    p_launch.add_argument("--memory", type=int, default=2048, help="最大内存 MB")
    p_launch.add_argument("--java", default="java", help="Java 可执行文件")
    p_launch.add_argument("--dry-run", action="store_true", help="只打印命令行")
    p_launch.set_defaults(func=cmd_launch)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())