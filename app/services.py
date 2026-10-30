"""LauncherService：把引擎能力与任务/配置/密钥串起来的应用层编排。

这里只做"编排 + 事件上报"，MC 业务逻辑仍在 engine 内；
API 层与界面层都调用本类，避免逻辑重复。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from engine import mirror as mirror_module
from engine.accounts import AccountError, AccountStore, ExternalAuthClient, SkinService, StoredAccount
from engine.auth_client import AuthClient, AuthError, AuthResult, KeyringTokenStore
from engine.content_manager import (
    CONTENT_KINDS,
    CONTENT_MOD,
    CONTENT_MODPACK,
    ContentError,
    ContentManager,
)
from engine.crash_analyzer import CrashAnalyzer
from engine.downloader import DownloadCancelled, Downloader
from engine.hash_store import HashStore, HashStoreError, RUN_MARKER
from engine.java_manager import JavaError, JavaManager
from engine.launch_builder import LaunchBuilder
from engine.loader_installer import SUPPORTED_LOADERS, LoaderError, LoaderInstaller
from engine.models import Account, FileEntry, GamePaths
from engine.modpack import ModpackError, ModpackInstaller
from engine.mod_provider import ModProvider, build_provider
from engine.version_resolver import VersionResolver

from .config import ConfigStore, LauncherConfig
from .secrets import SecretStore
from .tasks import Task, TaskManager, TaskStatus


class LauncherService:
    def __init__(
        self,
        store: ConfigStore,
        secrets: SecretStore,
        tasks: TaskManager,
    ) -> None:
        self.store = store
        self.secrets = secrets
        self.tasks = tasks
        self.config: LauncherConfig = store.load()
        self._auth_client_id = self.config.client_id
        self._auth = AuthClient(client_id=self.config.client_id, store=KeyringTokenStore())
        self._accounts = AccountStore(self.store.data_dir / "accounts.json", secrets)
        # 启动时应用配置里的下载源；探测放后台，网络不通时也不会拖慢启动。
        mirror_module.manager.set_source(self.config.download_source)
        mirror_module.manager.install()
        threading.Thread(target=mirror_module.manager.probe, daemon=True).start()

        # 哈希文件库：mcfile 存内容，.work 是运行期间的工作目录。
        self.hash_store = HashStore(self.store.data_dir / "mcfile")
        self.work_root = self.store.data_dir / ".work"
        self.work_marker = self.store.data_dir / ".work-ready"
        self.run_root = self.store.data_dir / ".run"
        self._storage_lock = threading.RLock()
        self._storage_closed = False
        self._game_lock = threading.RLock()
        self._game_process: Optional[subprocess.Popen] = None
        self._game_version = ""
        self._game_run_dir: Optional[Path] = None
        if self.config.hash_store:
            recovered = self._recover_runs()
            self._ensure_work_tree(recovered)

    def _recover_runs(self) -> bool:
        """启动时回收上次未同步的运行目录；只信任带完成标记的目录。"""
        if not self.run_root.exists():
            return False
        recovered = False
        for path in self.run_root.iterdir():
            if not path.is_dir():
                continue
            if not (path / RUN_MARKER).exists():
                # 半成品目录只是物化过程中的副本，哈希库仍然完整。
                self.hash_store.clear(path)
                continue
            try:
                result = self.hash_store.capture(path)
                self.log(
                    f"已恢复上次未同步的游戏目录 {path.name}：新增 {result['added']}，"
                    f"变更 {result['changed']}"
                )
                self.hash_store.clear(path)
                recovered = True
            except Exception as exc:  # noqa: BLE001
                self.log(f"恢复 {path.name} 失败（目录保留）：{exc}")
        return recovered

    # ------------------------------------------------------------- 哈希文件库
    def _materialize_work(self) -> int:
        count = self.hash_store.materialize(self.work_root)
        self.work_marker.write_text("1", encoding="utf-8")
        return count

    def _ensure_work_tree(self, refresh: bool = False) -> None:
        """确保工作目录存在；已有内容说明上次未同步，先回收再沿用。"""
        if self.work_marker.exists() and not refresh:
            self._capture_storage()
            return
        try:
            count = self._materialize_work()
            self.log(f"哈希存储：已物化 {count} 个文件到工作目录")
        except Exception as exc:  # noqa: BLE001
            self.log(f"哈希存储物化失败：{exc}")

    def _capture_storage(self) -> None:
        if not self.config.hash_store or self._storage_closed:
            return
        try:
            result = self.hash_store.capture(self.work_root)
            if any(result.values()):
                self.log(
                    "哈希存储同步：新增 {added}，变更 {changed}，移除 {removed}，"
                    "写入哈希 {blobs}".format(**result)
                )
        except Exception as exc:  # noqa: BLE001
            self.log(f"哈希存储同步失败：{exc}")

    def storage_status(self) -> dict:
        status = self.hash_store.stats()
        status["enabled"] = bool(self.config.hash_store)
        status["game_root"] = str(Path(self.config.root))
        status["work_dir"] = str(self.work_root)
        status["running_game"] = self.has_running_game()
        return status

    def set_hash_store(self, enabled: bool) -> Task:
        """开启 / 关闭哈希存储（带进度、原子迁移）。"""
        enabled = bool(enabled)
        game_root = Path(self.config.root)

        def job(task: Task) -> None:
            if enabled == self.config.hash_store:
                task.log("哈希存储状态未变化")
                return
            if self.has_running_game():
                raise RuntimeError("游戏正在运行，请先结束游戏再切换存储方式")
            busy = [
                item
                for item in self.tasks.list_snapshots(limit=50)
                if item.get("id") != task.id
                and item.get("status") in {TaskStatus.PENDING.value, TaskStatus.RUNNING.value}
            ]
            if busy:
                raise RuntimeError("还有任务正在进行，请等待完成后再切换存储方式")
            if enabled:
                task.log("开始迁移到哈希存储（需要约 2 倍磁盘空间，请勿关闭启动器）")
                self.hash_store.prune_runs(self.run_root)
                if game_root.exists() and any(game_root.iterdir()):
                    result = self.hash_store.migrate_in(game_root, self.work_root)
                    self.work_marker.write_text("1", encoding="utf-8")
                else:
                    self._materialize_work()
                    result = {}
                self.config.hash_store = True
                self.store.save(self.config)
                task.result = {"hash_store": True, "migration": result}
                task.log("已开启哈希存储；游戏文件无法被其它启动器读取")
            else:
                task.log("正在还原为普通 .minecraft 目录…")
                self.hash_store.migrate_out(self.work_root, game_root)
                self.work_marker.unlink(missing_ok=True)
                self.config.hash_store = False
                self.store.save(self.config)
                task.result = {"hash_store": False}
                task.log("已关闭哈希存储，文件已还原")

        return self.tasks.create("切换哈希存储", job)

    # ---------------------------------------------------------------- 游戏进程
    def has_running_game(self) -> bool:
        with self._game_lock:
            return self._game_process is not None and self._game_process.poll() is None

    def running_game_info(self) -> dict:
        with self._game_lock:
            if self._game_process is None or self._game_process.poll() is not None:
                return {}
            return {"version": self._game_version, "pid": self._game_process.pid}

    def stop_game(self, *, timeout: float = 8.0) -> bool:
        """结束游戏进程树；返回是否确实结束了一个运行中的进程。"""
        with self._game_lock:
            process = self._game_process
        if process is None or process.poll() is not None:
            return False
        self.log(f"正在结束游戏进程 PID {process.pid}…")
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                process.terminate()
            process.wait(timeout=timeout)
        except Exception:  # noqa: BLE001
            try:
                process.kill()
            except Exception:  # noqa: BLE001
                pass
        return True

    def _register_game(self, process: subprocess.Popen, version: str, run_dir: Optional[Path]) -> None:
        with self._game_lock:
            self._game_process = process
            self._game_version = version
            self._game_run_dir = run_dir
        if run_dir is not None:
            threading.Thread(
                target=self._watch_game, args=(process, run_dir), daemon=True
            ).start()

    def _watch_game(self, process: subprocess.Popen, run_dir: Path) -> None:
        try:
            process.wait()
        except Exception:  # noqa: BLE001
            return
        self._finish_run(run_dir)

    def _finish_run(self, run_dir: Path) -> None:
        if self._storage_closed:
            return
        if not (run_dir / RUN_MARKER).exists():
            # 物化未完成就退出的目录不可信，直接丢弃（哈希库仍然完整）。
            self.hash_store.clear(run_dir)
            with self._game_lock:
                self._game_process = None
                self._game_version = ""
                self._game_run_dir = None
            return
        with self._storage_lock:
            synced = False
            try:
                result = self.hash_store.capture(run_dir)
                self._materialize_work()
                self.log(
                    "游戏已退出，文件已同步：新增 {added}，变更 {changed}，移除 {removed}".format(
                        **result
                    )
                )
                synced = True
            except Exception as exc:  # noqa: BLE001
                self.log(f"游戏退出后同步失败（临时目录已保留）：{exc}")
            finally:
                if synced:
                    self.hash_store.clear(run_dir)
                with self._game_lock:
                    self._game_process = None
                    self._game_version = ""
                    self._game_run_dir = None

    def shutdown_storage(self) -> None:
        """启动器退出前调用：结束游戏并回收工作目录。"""
        self.stop_game()
        if not self.config.hash_store or self._storage_closed:
            return
        self._storage_closed = True
        with self._storage_lock:
            try:
                with self._game_lock:
                    run_dir = self._game_run_dir
                if run_dir is not None and Path(run_dir).exists():
                    if (Path(run_dir) / RUN_MARKER).exists():
                        self.hash_store.capture(Path(run_dir))
                    self.hash_store.clear(Path(run_dir))
                # 其它残留的运行目录也先回收，再统一清理。
                self._recover_runs()
                self.hash_store.capture(self.work_root)
                self.hash_store.clear(self.work_root)
                self.work_marker.unlink(missing_ok=True)
                self.hash_store.prune_runs(self.run_root)
                self.log("哈希存储：工作目录已回收")
            except Exception as exc:  # noqa: BLE001
                self.log(f"哈希存储回收失败（工作目录保留）：{exc}")

    # ------------------------------------------------------------------ 基础
    def log(self, line: str) -> None:
        self.tasks.log(line)

    def _create_task(self, name: str, job: Callable[[Task], None]) -> Task:
        """创建会改写游戏目录的任务；结束后自动同步哈希存储。"""

        def wrapped(task: Task) -> None:
            try:
                job(task)
            finally:
                self._capture_storage()

        return self.tasks.create(name, wrapped)

    @property
    def auth(self) -> AuthClient:
        if self._auth_client_id != self.config.client_id:
            self._auth_client_id = self.config.client_id
            self._auth = AuthClient(
                client_id=self.config.client_id, store=KeyringTokenStore()
            )
        return self._auth

    def paths(self) -> GamePaths:
        if self.config.hash_store:
            return GamePaths(self.work_root, java=self.config.java)
        return self.config.to_game_paths()

    def resolver(self) -> VersionResolver:
        return VersionResolver(self.paths())

    def java_manager(self) -> JavaManager:
        return JavaManager(self.paths())

    def content_manager(self) -> ContentManager:
        return ContentManager(self.paths())

    @property
    def accounts_store(self) -> AccountStore:
        return self._accounts

    # ------------------------------------------------------------- 下载源镜像
    def mirror_status(self) -> dict:
        return mirror_module.manager.status().as_dict()

    def probe_mirror(self) -> dict:
        status = mirror_module.manager.probe()
        self.log(
            f"下载源探测：{status.active}（{status.latency_ms} ms）"
            if status.available
            else f"镜像不可用，已回退官方源（{status.active}）"
        )
        return status.as_dict()

    # ------------------------------------------------------------------ 版本
    def list_versions(self, include_snapshots: bool = False) -> list[dict]:
        versions = self.resolver().list_versions(include_snapshots=include_snapshots)
        installed = self.installed_versions()
        return [
            {
                "id": info.id,
                "type": info.type,
                "release_time": info.release_time,
                "installed": info.id in installed,
                "isolated": bool(self.config.merged_settings(info.id)["isolated"]),
            }
            for info in versions
        ]

    def installed_versions(self) -> set[str]:
        versions_dir = self.paths().versions_dir
        if not versions_dir.exists():
            return set()
        # 安装任务进行中时不做清理，避免误删正在写入的版本目录。
        cleanup = not self._has_active_tasks()
        installed: set[str] = set()
        for child in versions_dir.iterdir():
            if not child.is_dir():
                continue
            if not (child / f"{child.name}.json").exists():
                if cleanup:
                    shutil.rmtree(child, ignore_errors=True)
                    self.log(f"已清理中断安装的版本：{child.name}")
                continue
            if self._version_usable(child):
                installed.add(child.name)
            elif cleanup:
                shutil.rmtree(child, ignore_errors=True)
                self.log(f"已清理中断安装的版本：{child.name}")
        return installed

    def _has_active_tasks(self) -> bool:
        active = {TaskStatus.PENDING.value, TaskStatus.RUNNING.value}
        return any(
            snapshot.get("status") in active
            for snapshot in self.tasks.list_snapshots(limit=50)
        )

    def _version_usable(self, version_dir: Path) -> bool:
        """版本目录是否完整：有客户端 jar，或通过 inheritsFrom 链条能找到父版本的 jar。"""
        seen: set[str] = set()
        current = version_dir
        while True:
            name = current.name
            if name in seen:
                return False
            seen.add(name)
            if (current / f"{name}.jar").exists():
                return True
            try:
                data = json.loads((current / f"{name}.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return False
            parent = str(data.get("inheritsFrom") or "").strip()
            if not parent:
                return False
            current = current.parent / parent
            if not (current / f"{current.name}.json").exists():
                return False

    def local_versions(self) -> list[dict]:
        """只列出本机已安装（含加载器）的版本。"""
        result = []
        for version_id in sorted(self.installed_versions()):
            settings = self.config.merged_settings(version_id)
            result.append(
                {
                    "id": version_id,
                    "isolated": bool(settings["isolated"]),
                    "loader": _detect_loader(version_id),
                }
            )
        return result

    def version_profile(self, version_id: Optional[str]) -> dict:
        """版本的「游戏版本 + 加载器」画像，供下载页自动筛选资源。"""
        if not version_id:
            return {"id": "", "game_version": "", "loader": "vanilla"}
        return {
            "id": version_id,
            "game_version": _game_version_of(version_id),
            "loader": _detect_loader(version_id),
        }

    # --------------------------------------------------------- 版本级启动设置
    def version_settings(self, version_id: str) -> dict:
        return {
            "version_id": version_id,
            "global": {
                "isolated": self.config.isolated,
                "java": self.config.java,
                "memory_mb": self.config.memory_mb,
                "min_memory_mb": self.config.min_memory_mb,
                "width": self.config.width,
                "height": self.config.height,
            },
            "override": self.config.override_for(version_id),
            "merged": self.config.merged_settings(version_id),
        }

    def update_version_settings(self, version_id: str, changes: dict) -> dict:
        changes = dict(changes)
        # 额外参数在界面上以空格分隔的字符串提交，这里统一成列表，
        # 否则启动时展开字符串会被逐字符拆开。
        for key in ("extra_jvm", "extra_game"):
            if isinstance(changes.get(key), str):
                changes[key] = changes[key].split()
        self.config.set_version_override(version_id, changes)
        self.store.save(self.config)
        return self.version_settings(version_id)

    # ------------------------------------------------------------------ 安装
    def start_install(self, version: str, *, include_assets: bool = True) -> Task:
        def job(task: Task) -> None:
            resolver = self.resolver()
            task.log(f"解析版本 {version}（包含资源文件：{include_assets}）")

            def on_phase(phase: str, done: int, total: int) -> None:
                task.emit({"type": "phase", "phase": phase, "done": done, "total": total})

            resolved = resolver.resolve(
                version,
                include_assets=include_assets,
                progress=on_phase,
                cancel=task.check_cancel,
            )
            task.log(f"清单解析完成，共 {len(resolved.files)} 个文件")

            downloader = Downloader(
                workers=self.config.download_workers,
                progress=task.emit,
            )
            stats = downloader.download(resolved.files, cancel=task.check_cancel)
            if stats.failed:
                raise RuntimeError(
                    f"{len(stats.failed)} 个文件下载失败，首个错误：{stats.failed[0][1]}"
                )
            task.result = {
                "version": version,
                "files": stats.files_done,
                "downloaded": stats.files_downloaded,
                "skipped": stats.files_skipped,
                "bytes": stats.bytes_done,
            }
            task.log(f"安装完成：下载 {stats.files_downloaded} 个，跳过 {stats.files_skipped} 个")

        return self._create_task(f"安装 {version}", job)

    def start_repair(self, version: str) -> Task:
        """校验并补全某个已安装版本的文件（缺失 / 损坏的 jar、库、资源）。"""

        def job(task: Task) -> None:
            resolver = self.resolver()
            task.log(f"校验 {version} 的本地文件")

            def on_phase(phase: str, done: int, total: int) -> None:
                task.emit({"type": "phase", "phase": phase, "done": done, "total": total})

            resolved = resolver.resolve(
                version,
                include_assets=True,
                prefer_local=True,
                progress=on_phase,
                cancel=task.check_cancel,
            )
            task.log(f"共 {len(resolved.files)} 个文件需要校验")

            downloader = Downloader(workers=self.config.download_workers, progress=task.emit)
            stats = downloader.download(resolved.files, cancel=task.check_cancel)
            repaired = stats.files_downloaded
            task.result = {
                "version": version,
                "files": stats.files_done,
                "downloaded": repaired,
                "skipped": stats.files_skipped,
                "repaired": repaired,
                "failed": len(stats.failed),
            }
            task.log(f"补全完成：修复 {repaired} 个，跳过 {stats.files_skipped} 个")
            if stats.failed:
                raise RuntimeError(
                    f"{len(stats.failed)} 个文件下载失败，首个错误：{stats.failed[0][1]}"
                )

        return self._create_task(f"补全 {version} 文件", job)

    def delete_version(self, version_id: str) -> dict:
        """删除已安装版本目录（含隔离的 mods / 存档 / 配置），并清理相关设置。"""
        for snapshot in self.tasks.list_snapshots(limit=50):
            if snapshot.get("status") in {TaskStatus.PENDING.value, TaskStatus.RUNNING.value} and (
                version_id in snapshot.get("name", "")
            ):
                raise RuntimeError("该版本还有任务在进行，请先取消或等待完成")
        target = self.paths().version_dir(version_id)
        if not target.is_dir():
            raise RuntimeError(f"版本不存在：{version_id}")
        shutil.rmtree(target)
        self._capture_storage()

        changed = False
        if self.config.version_settings.pop(version_id, None) is not None:
            changed = True
        if self.config.last_version == version_id:
            self.config.last_version = ""
            changed = True
        if changed:
            self.store.save(self.config)
        self.log(f"已删除版本：{version_id}")
        return {"version_id": version_id, "deleted": True}

    # --------------------------------------------------------------- 加载器
    def loader_versions(self, loader: str, game_version: str) -> list[dict]:
        if loader not in SUPPORTED_LOADERS:
            raise LoaderError(f"不支持的加载器：{loader}")
        installer = LoaderInstaller(self.paths(), java=self.config.java)
        return [
            {
                "loader": item.loader,
                "version": item.version,
                "game_version": item.game_version,
                "stable": item.stable,
                "installed": installer.installed(item.loader, item.game_version, item.version)
                is not None,
            }
            for item in installer.list_versions(loader, game_version)
        ]

    def start_loader_install(
        self, loader: str, game_version: str, loader_version: str
    ) -> Task:
        label = f"{loader} {loader_version}"

        def job(task: Task) -> None:
            installer = LoaderInstaller(self.paths(), java=self.config.java)

            def on_progress(stage: str, done: int, total: int) -> None:
                task.emit({"type": "phase", "phase": stage, "done": done, "total": total})

            task.log(f"开始安装 {label}（基于 {game_version}）")
            result = installer.install(
                loader,
                game_version,
                loader_version,
                progress=on_progress,
                log=task.log,
                cancel=task.check_cancel,
            )
            task.result = {
                "version_id": result.version_id,
                "loader": result.loader,
                "loader_version": result.loader_version,
                "game_version": result.game_version,
            }
            task.log(f"加载器安装完成：{result.version_id}")

        return self._create_task(f"安装 {label}", job)

    # ------------------------------------------------------------------ Java
    def java_scan(self) -> list[dict]:
        return [
            {
                "path": info.path,
                "version": info.version,
                "major": info.major,
                "vendor": info.vendor,
                "arch": info.arch,
                "source": info.source,
                "current": info.path == self.config.java,
            }
            for info in self.java_manager().scan()
        ]

    def java_available(self, major: int) -> list[dict]:
        return [
            {
                "major": runtime.major,
                "version": runtime.version,
                "os": runtime.os,
                "arch": runtime.arch,
                "url": runtime.url,
                "size": runtime.size,
                "vendor": runtime.vendor,
            }
            for runtime in self.java_manager().list_available(major)
        ]

    def test_java(self, executable: str) -> dict:
        info = self.java_manager().probe(executable)
        if info is None:
            return {"ok": False, "message": f"无法执行或不是有效的 Java：{executable}"}
        return {
            "ok": True,
            "path": info.path,
            "version": info.version,
            "major": info.major,
            "vendor": info.vendor,
            "arch": info.arch,
        }

    def start_java_install(self, major: int) -> Task:
        def job(task: Task) -> None:
            manager = self.java_manager()
            task.log(f"获取 Java {major} 下载信息")
            task.emit(
                {
                    "type": "progress",
                    "bytes_done": 0,
                    "bytes_total": 0,
                    "percent": 0.0,
                    "current": f"获取 Java {major} 下载信息…",
                }
            )
            try:
                runtimes = manager.list_available(major, cancel=task.check_cancel)
            except DownloadCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - 直接失败，避免重复等待一次网络超时
                raise RuntimeError(f"无法获取 Java {major} 的下载信息：{exc}") from exc
            total = int(runtimes[0].size) if runtimes else 0
            counter = {"bytes": 0, "last": 0.0}

            def on_bytes(delta: int) -> None:
                counter["bytes"] += delta
                now = time.monotonic()
                # 节流：最多每 0.2 秒上报一次，避免刷爆事件队列。
                if now - counter["last"] >= 0.2:
                    counter["last"] = now
                    done = counter["bytes"]
                    task.emit(
                        {
                            "type": "progress",
                            "bytes_done": done,
                            "bytes_total": total,
                            "percent": round(done / total * 100, 2) if total else 0.0,
                            "current": f"下载 Java {major}",
                        }
                    )

            task.emit(
                {
                    "type": "progress",
                    "bytes_done": 0,
                    "bytes_total": total,
                    "percent": 0.0,
                    "current": f"开始下载 Java {major}…",
                }
            )
            task.log(f"开始下载 Java {major}（Eclipse Temurin JRE）")
            info = manager.install(
                major,
                progress=on_bytes,
                cancel=task.check_cancel,
                runtime=runtimes[0] if runtimes else None,
            )
            task.result = {
                "path": info.path,
                "version": info.version,
                "major": info.major,
                "vendor": info.vendor,
            }
            task.log(f"Java {info.major} 安装完成：{info.path}")

        return self.tasks.create(f"安装 Java {major}", job)

    # ------------------------------------------------------------------ 启动
    def launch(
        self,
        version: str,
        *,
        offline: bool = True,
        username: str = "Steve",
        account_id: Optional[str] = None,
        memory_mb: Optional[int] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        dry_run: bool = False,
        progress: Optional[Callable[[dict], None]] = None,
        cancel: Optional[Callable[[], bool]] = None,
    ) -> dict:
        """构建命令行并拉起 Java；只触发 Popen，不阻塞调用方。

        ``progress`` 用于上报启动阶段（含 Java 自动安装进度），
        ``cancel`` 可在 Java 准备阶段中断启动。
        """
        def emit(percent: float, detail: str) -> None:
            if progress:
                try:
                    progress({"percent": percent, "detail": detail})
                except Exception:  # noqa: BLE001 - 进度回调不影响启动
                    pass

        if self.has_running_game():
            raise RuntimeError("游戏正在运行，请先结束当前游戏再启动新实例")

        emit(1, "解析版本信息…")
        storage = bool(self.config.hash_store) and not dry_run
        run_dir: Optional[Path] = None
        if storage:
            self._ensure_work_tree()
            self._capture_storage()
            emit(3, "准备临时游戏目录…")
            self.hash_store.prune_runs(self.run_root)
            run_dir = self.hash_store.new_run_dir(self.run_root)
            try:
                self.hash_store.materialize(run_dir)
                (run_dir / RUN_MARKER).write_text("1", encoding="utf-8")
            except Exception:
                self.hash_store.clear(run_dir)
                raise
            paths = GamePaths(run_dir, java=self.config.java)
        else:
            paths = self.paths()
        settings = self.config.merged_settings(version)
        isolated = bool(settings["isolated"])
        game_dir = paths.version_game_dir(version, isolated=isolated)

        resolved = VersionResolver(paths).resolve(
            version, include_assets=False, prefer_local=True, cancel=cancel
        )

        emit(4, "准备账号与启动参数…")
        account, note, extra_jvm = self._resolve_account(
            offline=offline,
            username=username,
            account_id=account_id,
            game_dir=game_dir,
        )
        if note:
            self.log(note)

        java = str(settings.get("java") or self.config.java)
        if not dry_run:
            java = self._ensure_java(resolved.java_major, java, progress=progress, cancel=cancel)
            java = _prefer_javaw(java)  # Windows 下避免启动游戏时弹出黑色控制台窗口
        # Java 就绪后用户仍可能点了取消，这里再检查一次，避免"取消了却还是启动了"。
        if cancel and cancel():
            raise DownloadCancelled("已取消启动")
        emit(95, "构建启动参数…")
        builder = LaunchBuilder(paths)
        command = builder.build(
            resolved,
            account,
            memory_mb=memory_mb or settings.get("memory_mb"),
            min_memory_mb=settings.get("min_memory_mb"),
            width=width or settings.get("width"),
            height=height or settings.get("height"),
            extra_jvm=[*extra_jvm, *(settings.get("extra_jvm") or [])],
            extra_game=list(settings.get("extra_game") or []),
            game_directory=game_dir,
            java=java,
        )

        if self.config.last_version != version or (
            account_id and self.config.last_account != account_id
        ):
            self.config.last_version = version
            if account_id:
                self.config.last_account = account_id
            self.store.save(self.config)

        mode = "离线" if account.offline else "正版"
        self.log(f"启动 {version}（{account.username}，{mode}，隔离：{isolated}）")
        if dry_run:
            return {
                "pid": 0,
                "command": command,
                "dry_run": True,
                "account": account.username,
                "isolated": isolated,
                "game_directory": str(game_dir),
            }

        game_dir.mkdir(parents=True, exist_ok=True)
        popen_kwargs: dict = {}
        if os.name == "nt":
            # 避免 java.exe 在桌面上弹出黑色控制台窗口。
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            process = subprocess.Popen(command, cwd=str(game_dir), **popen_kwargs)
        except Exception:
            if run_dir is not None:
                self.hash_store.clear(run_dir)
            raise
        self._register_game(process, version, run_dir)
        emit(100, f"已启动（PID {process.pid}）")
        return {
            "pid": process.pid,
            "command": command,
            "account": account.username,
            "game_directory": str(game_dir),
        }

    def _ensure_java(
        self,
        required: int,
        configured: str,
        *,
        progress: Optional[Callable[[dict], None]] = None,
        cancel: Optional[Callable[[], bool]] = None,
    ) -> str:
        """确保有满足版本要求的 Java；本地找不到时自动下载安装。

        优先级：显式配置的 Java → PATH / JAVA_HOME / 系统目录 / 已下载运行时
        → Temurin 自动安装（安装是幂等的，已装过会直接复用）。
        """
        def emit(percent: float, detail: str) -> None:
            if progress:
                try:
                    progress({"percent": percent, "detail": detail})
                except Exception:  # noqa: BLE001
                    pass

        def check_cancel() -> None:
            if cancel and cancel():
                raise DownloadCancelled("已取消启动")

        emit(5, "检查 Java 环境…")
        check_cancel()
        manager = self.java_manager()
        candidates: list[str] = []
        name = (configured or "").strip()
        if name and Path(name).name.lower() not in {"java", "javaw", "java.exe", "javaw.exe"}:
            candidates.append(name)

        required = max(int(required or 0), 8)
        info = manager.find_for(required, extra=candidates)
        # 版本相同最佳；1.17+ 允许用更高版本的 Java，老版本（如 1.8）必须精确匹配。
        if info is not None and (info.major == required or (required >= 16 and info.major > required)):
            emit(70, f"使用 Java {info.major}：{info.path}")
            return info.path

        self.log(f"未找到满足要求的 Java {required}+，自动下载安装（Eclipse Temurin）")
        emit(10, f"正在下载 Java {required}（Temurin）…")
        total = 0
        try:
            runtimes = manager.list_available(required, cancel=cancel)
            total = int(runtimes[0].size) if runtimes else 0
        except Exception as exc:  # noqa: BLE001 - 拿不到下载信息就直接失败，避免重复等待
            if cancel and cancel():
                raise
            raise RuntimeError(
                f"无法获取 Java {required} 的下载信息：{exc}"
                "（可手动安装 Java 后在「设置 → 游戏设置」中指定路径）"
            ) from exc
        check_cancel()

        state = {"bytes": 0, "last": 0.0}

        def on_bytes(delta: int) -> None:
            state["bytes"] += delta
            now = time.monotonic()
            if now - state["last"] < 0.2:
                return
            state["last"] = now
            done_mb = state["bytes"] / 1048576
            if total:
                percent = 10 + (state["bytes"] / total) * 80
                detail = f"正在下载 Java {required}：{done_mb:.1f} / {total / 1048576:.1f} MB"
            else:
                percent = 10 + min(80, done_mb)
                detail = f"正在下载 Java {required}：{done_mb:.1f} MB"
            emit(min(percent, 90), detail)

        try:
            installed = manager.install(
                required, progress=on_bytes, cancel=cancel, runtime=runtimes[0] if runtimes else None
            )
        except Exception as exc:  # noqa: BLE001
            if cancel and cancel():
                raise
            raise RuntimeError(
                f"自动安装 Java {required} 失败：{exc}"
                "（可手动安装 Java 后在「设置 → 游戏设置」中指定路径）"
            ) from exc
        emit(92, f"Java {installed.major} 已就绪")
        self.log(f"Java {installed.major} 已就绪：{installed.path}")
        return installed.path

    def _resolve_account(
        self,
        *,
        offline: bool,
        username: str,
        account_id: Optional[str] = None,
        game_dir: Optional[Path] = None,
    ) -> tuple[Account, str, list[str]]:
        """返回 (账号, 提示, 额外 JVM 参数)；认证不可用时始终回退离线。"""
        if account_id:
            stored = self._accounts.get(account_id)
            if stored is None:
                return (
                    Account.offline_account(username),
                    f"未找到账号 {account_id}，已回退为离线模式启动",
                    [],
                )
            if stored.kind == "offline":
                return Account.offline_account(stored.name), "", []
            if stored.kind == "external":
                account, note = self._external_account(stored, username)
                extra = self._injector_args(stored, game_dir) if note == "" else []
                return account, note, extra
            # microsoft
            tokens = self._accounts.get_tokens(stored.id)
            if tokens is None:
                return (
                    Account.offline_account(username),
                    "账号令牌缺失，已回退为离线模式启动",
                    [],
                )
            return self._msa_account(tokens, username, stored.id)

        if offline:
            return Account.offline_account(username), "", []

        # 未指定账号时沿用旧的单账号逻辑（keyring 里的默认令牌）。
        stored_tokens = self.auth.load_tokens()
        if stored_tokens is None:
            return (
                Account.offline_account(username),
                "未登录微软账号，已回退为离线模式启动",
                [],
            )
        return self._msa_account(stored_tokens, username, None)

    def _msa_account(
        self, tokens: AuthResult, username: str, account_id: Optional[str]
    ) -> tuple[Account, str, list[str]]:
        try:
            client = self._accounts_auth_client(account_id)
            fresh = client.ensure_fresh(tokens)
        except AuthError as exc:
            return (
                Account.offline_account(username),
                f"令牌刷新失败（{exc}），已回退为离线模式启动",
                [],
            )
        if account_id:
            self._accounts.set_tokens(account_id, fresh)
        return (
            Account(
                username=fresh.name or username,
                uuid=fresh.uuid,
                access_token=fresh.access_token,
                user_type="msa",
                offline=False,
            ),
            "",
            [],
        )

    def _accounts_auth_client(self, account_id: Optional[str]) -> AuthClient:
        """多账号场景下每个账号独占 keyring 条目，这里用内存 store 兜住 token。"""
        if account_id is None:
            return self.auth
        from engine.auth_client import MemoryTokenStore

        store = MemoryTokenStore()
        stored = self._accounts.get_tokens(account_id)
        if stored is not None:
            store.set(stored)
        return AuthClient(client_id=self.config.client_id, store=store)

    def _external_account(self, stored: StoredAccount, username: str) -> tuple[Account, str]:
        tokens = self._accounts.get_tokens(stored.id)
        if tokens is None:
            return (
                Account.offline_account(username),
                "外置登录令牌缺失，已回退为离线模式启动",
            )
        return (
            Account(
                username=tokens.name or stored.name or username,
                uuid=tokens.uuid or stored.uuid,
                access_token=tokens.access_token,
                user_type="mojang",
                offline=False,
            ),
            "",
        )

    def _injector_args(self, stored: StoredAccount, game_dir: Optional[Path]) -> list[str]:
        """外置登录需要 authlib-injector 的 javaagent 参数。"""
        jar = self.paths().root / "runtime" / "authlib-injector.jar"
        if not jar.exists():
            self.log("缺少 authlib-injector.jar，外置登录可能无法生效（可在账户页下载）")
            return []
        return [ExternalAuthClient(stored.server).jvm_argument(jar)]

    # ------------------------------------------------------------- Mod 与内容
    def mod_provider(self, source: Optional[str] = None) -> ModProvider:
        return build_provider(source or self.config.mod_source)

    def search_content(
        self,
        kind: str,
        query: str,
        *,
        source: Optional[str] = None,
        game_version: Optional[str] = None,
        loader: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict]:
        results = self.content_manager().search(
            kind,
            query,
            limit=limit,
            offset=offset,
            game_version=game_version,
            loader=loader if kind == CONTENT_MOD else None,
            source=source or self.config.mod_source,
        )
        return [_content_result(r) for r in results]

    def search_mods(
        self,
        query: str,
        *,
        source: Optional[str] = None,
        game_version: Optional[str] = None,
        loader: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict]:
        return self.search_content(
            CONTENT_MOD,
            query,
            source=source,
            game_version=game_version,
            loader=loader or self.config.mod_loader,
            limit=limit,
        )

    def start_content_install(
        self,
        kind: str,
        source: str,
        project_id: str,
        *,
        name: str = "",
        game_version: Optional[str] = None,
        loader: Optional[str] = None,
        version_id: Optional[str] = None,
    ) -> Task:
        label = name or project_id

        def job(task: Task) -> None:
            manager = self.content_manager()
            provider = self.mod_provider(source)
            task.log(f"解析 {provider.display_name} 项目 {label}（{kind}）")
            files = manager.resolve_files(
                kind,
                project_id,
                game_version=game_version,
                loader=loader if kind == CONTENT_MOD else None,
                source=source,
            )
            if not files:
                raise RuntimeError("未找到匹配当前版本/加载器的文件")
            chosen = files[0]
            task.log(f"选中 {chosen.filename}（{chosen.version}）")

            target_dir = manager.dir_for(
                kind, version_id=version_id, isolated=self._is_isolated(version_id)
            )
            target_dir.mkdir(parents=True, exist_ok=True)
            entry = FileEntry(
                url=chosen.url,
                target_path=target_dir / chosen.filename,
                sha1=chosen.sha1,
                size=chosen.size,
                kind=kind,
                # CurseForge 的 CDN 下载必须带 Key。
                headers=provider.download_headers() or None,
            )
            stats = Downloader(workers=1, progress=task.emit).download(
                [entry], cancel=task.check_cancel
            )
            if stats.failed:
                raise RuntimeError(stats.failed[0][1])
            task.result = {
                "filename": chosen.filename,
                "path": str(entry.target_path),
                "kind": kind,
                "version": chosen.version,
                "skipped": stats.files_skipped > 0,
            }

        return self._create_task(f"安装 {kind} {label}", job)

    def start_mod_install(
        self,
        source: str,
        project_id: str,
        *,
        name: str = "",
        game_version: Optional[str] = None,
        loader: Optional[str] = None,
        version_id: Optional[str] = None,
    ) -> Task:
        return self.start_content_install(
            CONTENT_MOD,
            source,
            project_id,
            name=name,
            game_version=game_version,
            loader=loader or self.config.mod_loader,
            version_id=version_id,
        )

    def _is_isolated(self, version_id: Optional[str]) -> bool:
        if not version_id:
            return False
        return bool(self.config.merged_settings(version_id)["isolated"])

    # ------------------------------------------------------------ 本地内容管理
    def content_list(self, kind: str, version_id: Optional[str] = None) -> list[dict]:
        manager = self.content_manager()
        isolated = self._is_isolated(version_id)
        return [
            {
                "kind": item.kind,
                "filename": item.filename,
                "path": str(item.path),
                "enabled": item.enabled,
                "size": item.size,
                "modified": item.modified,
            }
            for item in manager.list_local(kind, version_id=version_id, isolated=isolated)
        ]

    def content_toggle(
        self, kind: str, filename: str, enabled: bool, version_id: Optional[str] = None
    ) -> dict:
        item = self.content_manager().set_enabled(
            kind,
            filename,
            enabled,
            version_id=version_id,
            isolated=self._is_isolated(version_id),
        )
        self.log(f"{'启用' if enabled else '禁用'} {kind}：{filename}")
        self._capture_storage()
        return {"filename": item.filename, "enabled": item.enabled, "path": str(item.path)}

    def content_delete(self, kind: str, filename: str, version_id: Optional[str] = None) -> dict:
        self.content_manager().delete(
            kind, filename, version_id=version_id, isolated=self._is_isolated(version_id)
        )
        self.log(f"删除 {kind}：{filename}")
        self._capture_storage()
        return {"filename": filename, "deleted": True}

    def content_import(
        self,
        kind: str,
        source_path: str,
        *,
        version_id: Optional[str] = None,
        overwrite: bool = False,
    ) -> dict:
        path = Path(source_path)
        if not path.exists():
            raise ContentError(f"文件不存在：{source_path}")
        target = self.content_manager().import_file(
            kind,
            path,
            version_id=version_id,
            isolated=self._is_isolated(version_id),
            overwrite=overwrite,
        )
        self.log(f"导入 {kind}：{target.name}")
        self._capture_storage()
        return {"filename": target.name, "path": str(target)}

    def list_saves(self, version_id: Optional[str] = None) -> list[dict]:
        isolated = self._is_isolated(version_id)
        return [
            {"name": s.name, "path": str(s.path), "size": s.size, "modified": s.modified}
            for s in self.content_manager().list_saves(
                version_id=version_id, isolated=isolated
            )
        ]

    def delete_save(self, name: str, version_id: Optional[str] = None) -> dict:
        self.content_manager().delete_save(
            name, version_id=version_id, isolated=self._is_isolated(version_id)
        )
        self.log(f"删除存档：{name}")
        self._capture_storage()
        return {"name": name, "deleted": True}

    def list_screenshots(self, version_id: Optional[str] = None) -> list[dict]:
        isolated = self._is_isolated(version_id)
        return [
            {"name": s.name, "path": str(s.path), "size": s.size, "modified": s.modified}
            for s in self.content_manager().list_screenshots(
                version_id=version_id, isolated=isolated
            )
        ]

    # ------------------------------------------------------------- 整合包
    def inspect_modpack(self, path: str) -> dict:
        info = self._modpack().inspect(Path(path))
        return {
            "path": str(info.path),
            "format": info.format,
            "name": info.name,
            "version": info.version,
            "game_version": info.game_version,
            "loader": info.loader,
            "loader_version": info.loader_version,
            "file_count": info.file_count,
            "override_count": info.override_count,
        }

    def start_modpack_install(self, path: str, *, version_id: str) -> Task:
        archive = Path(path)

        def job(task: Task) -> None:
            installer = self._modpack()

            def on_progress(stage: str, done: int, total: int) -> None:
                task.emit({"type": "phase", "phase": stage, "done": done, "total": total})

            info = installer.install(
                archive,
                version_id=version_id,
                progress=on_progress,
                log=task.log,
                cancel=task.check_cancel,
            )
            task.result = {
                "version_id": version_id,
                "name": info.name,
                "format": info.format,
                "file_count": info.file_count,
            }
            task.log(f"整合包导入完成：{info.name} → {version_id}")

        return self._create_task(f"导入整合包 {archive.name}", job)

    def export_modpack(
        self,
        version_id: str,
        dest: str,
        *,
        name: str = "",
        pack_version: str = "1.0",
    ) -> dict:
        settings = self.config.merged_settings(version_id)
        target = Path(dest)
        result = self._modpack().export_mrpack(
            version_id=version_id,
            dest_zip=target,
            name=name or version_id,
            pack_version=pack_version,
            game_version=_game_version_of(version_id),
            loader=_detect_loader(version_id),
            loader_version=_loader_version_of(version_id, _detect_loader(version_id)),
        )
        self.log(f"整合包已导出：{result}")
        return {"path": str(result), "version_id": version_id, "isolated": settings["isolated"]}

    def _modpack(self) -> ModpackInstaller:
        return ModpackInstaller(self.paths())

    def modpack_dir(self) -> Path:
        """整合包归档目录（data/modpacks）。"""
        return self.content_manager().dir_for(CONTENT_MODPACK)

    def start_online_modpack_install(
        self,
        source: str,
        project_id: str,
        *,
        name: str = "",
        version_id: Optional[str] = None,
    ) -> Task:
        """下载在线整合包并按包内游戏版本直接安装（不做版本/加载器筛选）。"""
        label = name or project_id

        def job(task: Task) -> None:
            manager = self.content_manager()
            provider = self.mod_provider(source)
            task.log(f"解析整合包 {label}（{provider.display_name}）")
            files = manager.resolve_files(
                CONTENT_MODPACK,
                project_id,
                source=source,
            )
            if not files:
                raise RuntimeError("未找到可下载的整合包文件")
            chosen = files[0]
            task.log(f"选中 {chosen.filename}")

            archive_dir = self.modpack_dir()
            archive_dir.mkdir(parents=True, exist_ok=True)
            entry = FileEntry(
                url=chosen.url,
                target_path=archive_dir / chosen.filename,
                sha1=chosen.sha1,
                size=chosen.size,
                kind=CONTENT_MODPACK,
                headers=provider.download_headers() or None,
            )
            stats = Downloader(workers=1, progress=task.emit).download(
                [entry], cancel=task.check_cancel
            )
            if stats.failed:
                raise RuntimeError(stats.failed[0][1])

            installer = self._modpack()
            info = installer.inspect(entry.target_path)
            target = version_id or info.game_version
            if not target:
                raise RuntimeError("无法确定整合包的目标游戏版本")

            def on_progress(stage: str, done: int, total: int) -> None:
                task.emit({"type": "phase", "phase": stage, "done": done, "total": total})

            installer.install(
                entry.target_path,
                version_id=target,
                progress=on_progress,
                log=task.log,
                cancel=task.check_cancel,
            )
            task.result = {
                "filename": chosen.filename,
                "version_id": target,
                "name": info.name,
                "game_version": info.game_version,
                "loader": info.loader,
            }
            task.log(f"整合包安装完成：{info.name} → {target}")

        return self._create_task(f"安装整合包 {label}", job)

    # ------------------------------------------------------------- 崩溃分析
    def analyze_crash(self, version_id: Optional[str] = None) -> dict:
        paths = self.paths()
        if version_id:
            game_dir = paths.version_game_dir(
                version_id, isolated=self._is_isolated(version_id)
            )
        else:
            game_dir = paths.root

        candidates: list[Path] = []
        crash_dir = game_dir / "crash-reports"
        if crash_dir.exists():
            candidates.extend(
                sorted(crash_dir.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
            )
        latest = game_dir / "logs" / "latest.log"
        candidates.append(latest)

        report = CrashAnalyzer().analyze_files([p for p in candidates if p.exists()])
        return {
            "sources": [str(p) for p in candidates if p.exists()],
            "summary": report.summary,
            "causes": [
                {
                    "code": cause.code,
                    "title": cause.title,
                    "suggestion": cause.suggestion,
                    "severity": cause.severity,
                    "evidence": cause.evidence,
                }
                for cause in report.causes
            ],
            "log_tail": report.log_tail,
        }

    # ------------------------------------------------------------------ 认证
    def auth_start(self) -> dict:
        device = self.auth.start_auth()
        self.log("已申请微软设备码，请在浏览器完成授权")
        return {
            "device_code": device.device_code,
            "user_code": device.user_code,
            "verification_uri": device.verification_uri,
            "expires_in": device.expires_in,
            "interval": device.interval,
            "message": device.message,
        }

    def _register_msa(self, result: AuthResult) -> StoredAccount:
        account = StoredAccount(
            id=result.uuid or f"msa:{result.name}",
            kind="microsoft",
            name=result.name,
            uuid=result.uuid,
            avatar=SkinService().avatar_url(result.uuid) if result.uuid else "",
            last_used=time.time(),
        )
        self._accounts.add(account)
        self._accounts.set_tokens(account.id, result)
        self._accounts.set_active(account.id)
        self.config.last_account = account.id
        self.store.save(self.config)
        return account

    def auth_poll(self, device_code: str) -> dict:
        result = self.auth.poll_auth(device_code)
        if result is None:
            return {"status": "pending"}
        account = self._register_msa(result)
        self.log(f"微软账号登录成功：{result.name}")
        return {"status": "ok", "account": self.account_info(), "stored": account.as_dict()}

    def auth_complete(self, device_code: str, *, timeout: float = 300.0) -> dict:
        result = self.auth.finish_auth(device_code, timeout=timeout)
        account = self._register_msa(result)
        self.log(f"微软账号登录成功：{result.name}")
        return {
            "status": "ok",
            "account": self.account_info(),
            "stored": account.as_dict(),
            "refreshed_at": result.expires_at,
        }

    def auth_refresh(self) -> dict:
        stored = self.auth.load_tokens()
        if stored is None:
            raise AuthError("没有可刷新的登录状态")
        result = self.auth.refresh_auth(stored.refresh_token)
        self.log("令牌已刷新")
        return {"status": "ok", "account": self.account_info()}

    def auth_logout(self) -> dict:
        self.auth.logout()
        self.log("已退出微软账号")
        return {"status": "ok"}

    def account_info(self) -> Optional[dict]:
        stored = self.auth.load_tokens()
        if stored is None:
            return None
        return {
            "name": stored.name,
            "uuid": stored.uuid,
            "user_type": stored.user_type,
            "expires_at": stored.expires_at,
            "expired": stored.expired,
        }

    # -------------------------------------------------------------- 多账户
    def accounts(self) -> list[dict]:
        active = self._accounts.active()
        return [
            {**account.as_dict(), "active": bool(active and active.id == account.id)}
            for account in self._accounts.load()
        ]

    def account_add_offline(self, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            raise AccountError("玩家名不能为空")
        account = StoredAccount(
            id=f"offline:{name}",
            kind="offline",
            name=name,
            last_used=time.time(),
        )
        self._accounts.add(account)
        self._accounts.set_active(account.id)
        self.config.last_account = account.id
        self.store.save(self.config)
        self.log(f"已添加离线账号：{name}")
        return account.as_dict()

    def account_set_active(self, account_id: str) -> dict:
        if self._accounts.get(account_id) is None:
            raise AccountError(f"账号不存在：{account_id}")
        self._accounts.set_active(account_id)
        self.config.last_account = account_id
        self.store.save(self.config)
        return {"active": account_id}

    def account_remove(self, account_id: str) -> dict:
        self._accounts.remove(account_id)
        if self.config.last_account == account_id:
            self.config.last_account = ""
            self.store.save(self.config)
        self.log(f"已移除账号：{account_id}")
        return {"removed": account_id}

    def account_skin(self, account_id: str) -> dict:
        account = self._accounts.get(account_id)
        if account is None:
            raise AccountError(f"账号不存在：{account_id}")
        skin = SkinService()
        uuid = account.uuid
        if account.kind == "offline" and not uuid:
            uuid = Account.offline_account(account.name).uuid
        return {
            "account_id": account.id,
            "name": account.name,
            "uuid": uuid,
            "avatar": account.avatar or (skin.avatar_url(uuid) if uuid else ""),
            "body": skin.body_url(uuid) if uuid else "",
            "skin": skin.skin_url(uuid) if uuid else None,
        }

    def external_login(self, server: str, username: str, password: str) -> dict:
        client = ExternalAuthClient(server)
        result = client.authenticate(username, password)
        account = StoredAccount(
            id=result.uuid or f"external:{username}",
            kind="external",
            name=result.name or username,
            uuid=result.uuid,
            server=server,
            avatar=SkinService().avatar_url(result.uuid) if result.uuid else "",
            last_used=time.time(),
        )
        self._accounts.add(account)
        self._accounts.set_tokens(account.id, result)
        self._accounts.set_active(account.id)
        self.config.last_account = account.id
        self.store.save(self.config)
        self.log(f"外置登录成功：{result.name}（{server}）")
        return account.as_dict()

    def download_injector(self) -> dict:
        jar = ExternalAuthClient("").download_injector(self.paths().root / "runtime")
        return {"path": str(jar)}

    # ------------------------------------------------------------------ 配置
    def update_config(self, changes: dict) -> dict:
        allowed = {
            "java",
            "memory_mb",
            "min_memory_mb",
            "width",
            "height",
            "client_id",
            "last_version",
            "include_snapshots",
            "download_workers",
            "mod_source",
            "mod_loader",
            "isolated",
            "last_account",
        }
        for key, value in changes.items():
            if key in allowed:
                setattr(self.config, key, value)

        if "root" in changes and changes["root"]:
            if self.config.hash_store:
                raise RuntimeError("哈希存储开启时不能切换游戏目录，请先关闭哈希存储")
            new_root = str(Path(str(changes["root"])).expanduser().resolve())
            self.config.root = new_root
            # 新游戏目录立即创建标准结构；账户清单固定在启动器数据目录，不随游戏目录移动。
            self.paths().ensure()
            self.log(f"游戏目录已切换：{new_root}")

        if "download_source" in changes and changes["download_source"]:
            self.config.download_source = str(changes["download_source"])
            mirror_module.install(self.config.download_source)

        self.store.save(self.config)
        return self.config_view()

    def config_view(self) -> dict:
        view = self.config.as_dict()
        view["data_dir"] = str(self.store.data_dir)
        view["keyring_available"] = self.secrets.available
        view["keyring_backend"] = self.secrets.backend_name
        view["installed_versions"] = sorted(self.installed_versions())
        view["mirror"] = self.mirror_status()
        view["storage"] = self.storage_status()
        return view

    def supports(self) -> dict:
        """供界面判断哪些能力可用（避免前端硬编码）。"""
        return {
            "loaders": list(SUPPORTED_LOADERS),
            "content_kinds": list(CONTENT_KINDS),
            "sources": ["modrinth", "curseforge"],
            "download_sources": ["auto", "bmclapi", "official"],
        }


# --------------------------------------------------------------------- helpers
def _prefer_javaw(java: str) -> str:
    """Windows 下优先使用同目录的 javaw.exe，启动游戏时不弹出控制台。"""
    if os.name != "nt":
        return java
    path = Path(java)
    if path.name.lower() != "java.exe":
        return java
    javaw = path.with_name("javaw.exe")
    return str(javaw) if javaw.exists() else java


def _content_result(result) -> dict:
    return {
        "source": result.source,
        "project_id": result.project_id,
        "name": result.name,
        "author": result.author,
        "description": result.description,
        "downloads": result.downloads,
        "icon_url": result.icon_url,
        "page_url": result.page_url,
    }


def _detect_loader(version_id: str) -> str:
    lowered = version_id.lower()
    for loader in ("neoforge", "forge", "fabric", "quilt", "optifine"):
        if loader in lowered:
            return loader
    return "vanilla"


def _game_version_of(version_id: str) -> str:
    """从加载器版本目录名里猜出原版版本号，如 fabric-loader-0.15.0-1.20.1 → 1.20.1。"""
    import re

    matches = re.findall(r"\d+\.\d+(?:\.\d+)?", version_id)
    return matches[-1] if matches else version_id


def _loader_version_of(version_id: str, loader: str) -> str:
    import re

    if loader == "vanilla":
        return ""
    pattern = re.escape(loader) + r"-loader-([^-]+)-"
    match = re.search(pattern, version_id)
    if match:
        return match.group(1)
    match = re.search(re.escape(loader) + r"-([0-9][^-]*)", version_id)
    return match.group(1) if match else ""