"""LauncherService：把引擎能力与任务/配置/密钥串起来的应用层编排。

这里只做"编排 + 事件上报"，MC 业务逻辑仍在 engine 内；
API 层与界面层都调用本类，避免逻辑重复。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Optional

from engine import mirror as mirror_module
from engine import mod_info, net, relay as relay_engine, server_list, updater, version_meta
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
from engine.hash_store import HashStore, RUN_MARKER
from engine.java_manager import JavaManager
from engine.launch_builder import LaunchBuilder
from engine.loader_installer import SUPPORTED_LOADERS, LoaderError, LoaderInstaller
from engine.models import Account, FileEntry, GamePaths
from engine.modpack import ModpackInstaller
from engine.mod_provider import (
    CURSEFORGE_API,
    MODRINTH_API,
    ModProvider,
    build_provider,
    murmur2_fingerprint,
)
from engine.version import APP_VERSION
from engine.version_resolver import VersionResolver

from .config import ConfigStore, LauncherConfig
from .secrets import SecretStore
from .tasks import Task, TaskManager, TaskStatus


def pick_updated_file(files: list, current_sha1: str, current_published: str = ""):
    """从候选文件里挑出真正更新的那个。

    只用哈希差异 + 发布时间判断，不比较版本号：不少 Mod 的 jar 内版本号与
    Modrinth 上的 version_number 并不一致。没有发布时间时退回旧行为。
    """
    candidates = [
        item for item in files if item.sha1 and item.sha1.lower() != current_sha1.lower()
    ]
    if not candidates:
        return None
    if not current_published:
        return candidates[0]
    later = [
        item for item in candidates if item.published_at and item.published_at > current_published
    ]
    return max(later, key=lambda item: item.published_at) if later else None


def launcher_update_available(
    version: str, current: str, identity: str, seen: str
) -> bool:
    """判断是否有启动器更新。

    新版本算更新；同一版本 Release 的附件被重新上传（标识变化）也算一次更新。
    新安装的用户没有历史标识时，不会把自己当前版本误判成更新。
    """
    seen_version = seen.split("|", 1)[0] if seen else ""
    return bool(
        updater.is_newer(version, current)
        or (version == current and seen_version == current and identity != seen)
    )


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
        # 日志同时落盘，重启后仍可查看；界面只读取最近若干行。
        self.tasks.set_log_file(self.store.data_dir / "logs" / "launcher.log")
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
        self._modpack_lock = threading.Lock()
        # 不同版本的安装会同时写同一批 libraries / assets，串行化避免写坏共享文件。
        self._files_lock = threading.Lock()
        self._game_lock = threading.RLock()
        # 启动流程互斥：双击或两个页面同时点启动时只允许一个真正拉起游戏。
        self._launch_lock = threading.Lock()
        self._game_process: Optional[subprocess.Popen] = None
        self._game_version = ""
        self._game_run_dir: Optional[Path] = None
        # 联机：frpc 隧道与当前房间状态（房主或访客）。
        self._relay_runner = relay_engine.TunnelRunner(
            self.store.data_dir / "tools", self.store.data_dir / "relay"
        )
        self._relay_sessions: dict[str, dict] = {}
        self._relay_room: Optional[dict] = None
        self._relay_guest: Optional[dict] = None
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
            task.emit({"type": "phase", "phase": "storage", "done": 0, "total": 0})
            with self._task_lock(self._files_lock, task):
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
                    timeout=10,
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
        self._relay_runner.stop()
        self._relay_room = None
        self._relay_guest = None
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

    def launcher_logs(self, *, limit: int = 500, keyword: str = "") -> list[str]:
        """读取启动器日志；优先读落盘文件（含上次运行），否则回退内存日志。"""
        path = self.tasks.log_file
        lines: list[str] = []
        if path is not None and path.exists():
            try:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    lines = handle.readlines()
            except OSError:
                lines = []
        if not lines:
            lines, _ = self.tasks.global_logs()
        lines = [line.rstrip("\r\n") for line in lines]
        if keyword.strip():
            key = keyword.strip().lower()
            lines = [line for line in lines if key in line.lower()]
        return lines[-max(1, int(limit)):]

    def export_launcher_logs(self, lines: Optional[list[str]] = None) -> dict:
        """把日志导出到 data/logs，返回文件路径。"""
        directory = self.store.data_dir / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = directory / f"launcher-{stamp}.log"
        content = lines if lines is not None else self.launcher_logs(limit=100000)
        target.write_text("\n".join(content), encoding="utf-8")
        self.log(f"日志已导出：{target}")
        return {"path": str(target)}

    def logs_dir(self) -> str:
        directory = self.store.data_dir / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        return str(directory)

    def _loader_java(self, task: Task, game_version: str) -> str:
        """Forge / NeoForge 安装器需要能运行对应版本的 Java，这里按原版要求准备。"""
        try:
            resolved = self.resolver().resolve(
                game_version,
                include_assets=False,
                prefer_local=True,
                cancel=task.check_cancel,
            )
            required = resolved.java_major
        except DownloadCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - 拿不到版本信息时退回当前配置
            task.log(f"无法确定 {game_version} 所需的 Java 版本：{exc}")
            return self.config.java

        def on_progress(event: dict) -> None:
            task.emit(
                {
                    "type": "phase",
                    "phase": "java",
                    "done": int(event.get("percent") or 0),
                    "total": 100,
                }
            )

        try:
            return self._ensure_java(
                required, self.config.java, progress=on_progress, cancel=task.check_cancel
            )
        except DownloadCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - Java 准备失败时仍尝试现有配置
            task.log(f"Java {required}+ 准备失败（将尝试当前配置）：{exc}")
            return self.config.java

    def _game_files_busy(self) -> bool:
        """是否有任务正在改写游戏本体文件（安装 / 补全 / 加载器 / 整合包）。"""
        active = {TaskStatus.PENDING.value, TaskStatus.RUNNING.value}
        content_prefixes = (
            "安装 mod ",
            "安装 resourcepack ",
            "安装 shader ",
            "安装 datapack ",
            "安装 Java ",
        )
        for snapshot in self.tasks.list_snapshots(limit=50):
            if snapshot.get("status") not in active:
                continue
            name = str(snapshot.get("name") or "")
            if name.startswith(content_prefixes):
                continue
            if name.startswith(("安装 ", "补全 ", "导入整合包 ", "安装整合包 ")):
                return True
        return False

    def _find_active_task(self, name: str) -> Optional[Task]:
        """同名任务还在跑时直接复用它，避免两个线程写同一批文件。"""
        active = {TaskStatus.PENDING.value, TaskStatus.RUNNING.value}
        for snapshot in self.tasks.list_snapshots(limit=50):
            if snapshot.get("status") in active and snapshot.get("name") == name:
                return self.tasks.get(snapshot["id"])
        return None

    @contextmanager
    def _task_lock(self, lock: threading.Lock, task: Task):
        """等待锁期间也响应取消，避免用户在排队时「点了取消没反应」。"""
        while not lock.acquire(timeout=0.5):
            if task.check_cancel():
                raise DownloadCancelled("已取消")
        try:
            if task.check_cancel():
                raise DownloadCancelled("已取消")
            yield
        finally:
            lock.release()

    def _serialized_modpack(self, job: Callable[[Task], None]) -> Callable[[Task], None]:
        """整合包会向同一加载器版本写入内容，串行执行避免两个包互相覆盖。"""

        def run(task: Task) -> None:
            if task.check_cancel():
                raise DownloadCancelled("已取消")
            if self._modpack_lock.locked():
                task.emit({"type": "phase", "phase": "wait", "done": 0, "total": 0})
                task.log("等待其它整合包安装完成…")
            with self._task_lock(self._modpack_lock, task):
                if self._files_lock.locked():
                    task.emit({"type": "phase", "phase": "waitfiles", "done": 0, "total": 0})
                    task.log("等待其它游戏文件安装任务完成…")
                with self._task_lock(self._files_lock, task):
                    job(task)

        return run

    def _serialized_files(self, job: Callable[[Task], None]) -> Callable[[Task], None]:
        """游戏文件安装任务串行执行，避免并发下载同一个 libraries / assets 文件。"""

        def run(task: Task) -> None:
            if task.check_cancel():
                raise DownloadCancelled("已取消")
            if self._files_lock.locked():
                task.emit({"type": "phase", "phase": "waitfiles", "done": 0, "total": 0})
                task.log("等待其它游戏文件安装任务完成…")
            with self._task_lock(self._files_lock, task):
                job(task)

        return run

    def _create_task(self, name: str, job: Callable[[Task], None]) -> Task:
        """创建会改写游戏目录的任务；结束后自动同步哈希存储。"""

        def wrapped(task: Task) -> None:
            try:
                job(task)
            finally:
                if self.config.hash_store and not self._storage_closed:
                    # 下载完成后的哈希入库可能较久，给界面一个明确阶段。
                    task.emit({"type": "phase", "phase": "hashsync", "done": 0, "total": 0})
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
            meta = version_meta.detect_from_meta(self.paths(), version_id) or {}
            result.append(
                {
                    "id": version_id,
                    "isolated": bool(settings["isolated"]),
                    "loader": meta.get("loader") or _detect_loader(version_id),
                    "game_version": meta.get("game_version") or _game_version_of(version_id),
                    "pack_name": meta.get("pack_name") or "",
                }
            )
        return result

    def version_profile(self, version_id: Optional[str]) -> dict:
        """版本的「游戏版本 + 加载器」画像，供下载页自动筛选资源。"""
        if not version_id:
            return {"id": "", "game_version": "", "loader": "vanilla"}
        meta = version_meta.detect_from_meta(self.paths(), version_id) or {}
        return {
            "id": version_id,
            "game_version": meta.get("game_version") or _game_version_of(version_id),
            "loader": meta.get("loader") or _detect_loader(version_id),
            "loader_version": meta.get("loader_version") or "",
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
    def _download_version(
        self, task: Task, version: str, include_assets: bool
    ) -> None:
        """下载某个原版版本的客户端、库与资源。"""
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
        task.log(f"原版文件就绪：下载 {stats.files_downloaded} 个，跳过 {stats.files_skipped} 个")

    def start_install(
        self,
        version: str,
        *,
        include_assets: bool = True,
        name: str = "",
        loader: str = "",
        loader_version: str = "",
    ) -> Task:
        """安装原版，可选装模组加载器，并用自定义名称创建独立版本目录。"""
        target_name = (name or "").strip() or version
        if Path(target_name).name != target_name or not target_name:
            raise RuntimeError(f"版本名不合法：{target_name}")
        loader = (loader or "").strip().lower()
        loader_version = (loader_version or "").strip()
        if loader not in SUPPORTED_LOADERS:
            loader = ""
        if not loader:
            loader_version = ""

        def job(task: Task) -> None:
            self._download_version(task, version, include_assets)
            target = version
            meta = {
                "game_version": version,
                "loader": "vanilla",
                "loader_version": "",
            }
            if loader:
                task.log(f"安装加载器 {loader} {loader_version}")
                java = self.config.java
                if loader in ("forge", "neoforge"):
                    java = self._loader_java(task, version)

                def on_phase(stage: str, done: int, total: int) -> None:
                    task.emit({"type": "phase", "phase": stage, "done": done, "total": total})

                result = LoaderInstaller(self.paths(), java=java).install(
                    loader,
                    version,
                    loader_version,
                    progress=on_phase,
                    log=task.log,
                    cancel=task.check_cancel,
                )
                self._install_version_files(task, result.version_id)
                target = result.version_id
                meta.update(loader=loader, loader_version=loader_version)

            version_meta.write_meta(self.paths(), target, **meta)
            if target_name != target:
                version_meta.clone_version(self.paths(), target, target_name, meta)
                task.log(f"已创建独立版本：{target_name}")
                target = target_name

            task.result = {
                "version": version,
                "version_id": target,
                "name": target_name,
                "loader": loader or "vanilla",
                "loader_version": loader_version,
            }

        label = f"安装 {version}" + (f" + {loader}" if loader else "")
        existing = self._find_active_task(label)
        return existing or self._create_task(label, self._serialized_files(job))

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

        name = f"补全 {version} 文件"
        existing = self._find_active_task(name)
        return existing or self._create_task(name, self._serialized_files(job))

    def delete_version(self, version_id: str) -> dict:
        """删除已安装版本目录（含隔离的 mods / 存档 / 配置），并清理相关设置。"""
        if not version_id or Path(version_id).name != version_id:
            raise RuntimeError(f"版本名不合法：{version_id}")
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

    def rename_version(self, version_id: str, new_id: str) -> dict:
        """重命名版本目录，并同步配置里的版本级设置。"""
        new_id = (new_id or "").strip()
        if self.has_running_game():
            raise RuntimeError("游戏正在运行，请先结束游戏再重命名版本")
        version_meta.rename_version(self.paths(), version_id, new_id)
        override = self.config.version_settings.pop(version_id, None)
        if override is not None:
            self.config.version_settings[new_id] = override
        if self.config.last_version == version_id:
            self.config.last_version = new_id
        self.store.save(self.config)
        self._capture_storage()
        self.log(f"版本已重命名：{version_id} → {new_id}")
        return {"old": version_id, "new": new_id}

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
        self, loader: str, game_version: str, loader_version: str, *, name: str = ""
    ) -> Task:
        label = f"{loader} {loader_version}"
        target_name = (name or "").strip()

        def job(task: Task) -> None:
            paths = self.paths()
            java = self.config.java

            def on_progress(stage: str, done: int, total: int) -> None:
                task.emit({"type": "phase", "phase": stage, "done": done, "total": total})

            task.log(f"开始安装 {label}（基于 {game_version}）")
            if game_version not in self.installed_versions():
                # 原版还没装完整时先补全基础文件，否则安装完加载器也无法启动。
                task.log(f"原版 {game_version} 尚未安装，先补全基础文件")
                resolved = self.resolver().resolve(
                    game_version,
                    include_assets=True,
                    progress=on_progress,
                    cancel=task.check_cancel,
                )
                stats = Downloader(
                    workers=self.config.download_workers, progress=task.emit
                ).download(resolved.files, cancel=task.check_cancel)
                if stats.failed:
                    raise RuntimeError(
                        f"原版文件下载失败，首个错误：{stats.failed[0][1]}"
                    )
                task.log(
                    f"原版文件就绪：下载 {stats.files_downloaded} 个，"
                    f"跳过 {stats.files_skipped} 个"
                )
            if loader in ("forge", "neoforge"):
                # Forge / NeoForge 安装器需要用对应版本的 Java 运行。
                java = self._loader_java(task, game_version)
            installer = LoaderInstaller(paths, java=java)
            result = installer.install(
                loader,
                game_version,
                loader_version,
                progress=on_progress,
                log=task.log,
                cancel=task.check_cancel,
            )
            # 继承版本的客户端 jar / 库补齐，否则启动时 classpath 会缺文件。
            self._install_version_files(task, result.version_id)
            meta = {
                "game_version": game_version,
                "loader": loader,
                "loader_version": loader_version,
            }
            version_meta.write_meta(self.paths(), result.version_id, **meta)
            target = result.version_id
            if target_name and target_name != target:
                version_meta.clone_version(self.paths(), target, target_name, meta)
                target = target_name
            task.result = {
                "version_id": target,
                "loader": result.loader,
                "loader_version": result.loader_version,
                "game_version": result.game_version,
            }
            task.log(f"加载器安装完成：{target}")

        name = f"安装 {label}（{game_version}）"
        existing = self._find_active_task(name)
        return existing or self._create_task(name, self._serialized_files(job))

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
                    percent = min(100.0, done / total * 100) if total else 0.0
                    task.emit(
                        {
                            "type": "progress",
                            "bytes_done": done,
                            "bytes_total": total,
                            "percent": round(percent, 2),
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

        name = f"安装 Java {major}"
        existing = self._find_active_task(name)
        return existing or self._create_task(name, job)

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
        extra_game: Optional[list[str]] = None,
    ) -> dict:
        """启动游戏；同一时间只允许一个启动流程，避免拉起多个实例。"""
        if not self._launch_lock.acquire(blocking=False):
            raise RuntimeError("游戏正在启动中，请稍候")
        try:
            return self._launch_locked(
                version,
                offline=offline,
                username=username,
                account_id=account_id,
                memory_mb=memory_mb,
                width=width,
                height=height,
                dry_run=dry_run,
                progress=progress,
                cancel=cancel,
                extra_game=extra_game,
            )
        finally:
            self._launch_lock.release()

    def _launch_locked(
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
        extra_game: Optional[list[str]] = None,
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
        if not dry_run and self._game_files_busy():
            raise RuntimeError("游戏文件正在安装或补全，请等待任务完成后再启动")

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

        def cleanup_run_dir() -> None:
            """启动准备失败时回收已物化的临时游戏目录，避免占用磁盘。"""
            if run_dir is not None:
                self.hash_store.clear(run_dir)

        try:
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
                extra_game=[*(settings.get("extra_game") or []), *(extra_game or [])],
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
        except DownloadCancelled:
            cleanup_run_dir()
            raise
        except Exception:
            cleanup_run_dir()
            raise

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
        log_path = self._game_log_path(version)
        popen_kwargs: dict = {}
        if os.name == "nt":
            # 避免 java.exe 在桌面上弹出黑色控制台窗口。
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(
                    f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 {version} =====\n"
                )
                handle.flush()
                # 把游戏输出落到日志，启动失败时界面和日志里都能看到原因。
                popen_kwargs["stdout"] = handle
                popen_kwargs["stderr"] = subprocess.STDOUT
                process = subprocess.Popen(command, cwd=str(game_dir), **popen_kwargs)
        except Exception:
            if run_dir is not None:
                self.hash_store.clear(run_dir)
            raise
        self.log(f"游戏输出日志：{log_path}")
        self._register_game(process, version, run_dir)
        # 有的失败（缺依赖、Java 参数错误）会让进程几秒内就退出，先确认真的起来了。
        emit(99, "等待游戏窗口…")
        time.sleep(1.5)
        code = process.poll()
        if code is not None:
            tail = self._tail_text(log_path)
            detail = f"游戏启动后立即退出（退出码 {code}）"
            self.log(f"{detail}；最近输出：\n{tail}" if tail else detail)
            raise RuntimeError(f"{detail}。{tail}" if tail else detail)
        emit(100, f"游戏已启动（PID {process.pid}）")
        return {
            "pid": process.pid,
            "command": command,
            "account": account.username,
            "game_directory": str(game_dir),
            "log": str(log_path),
        }

    def _game_log_path(self, version: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in version)
        return self.store.data_dir / "logs" / f"game-{safe}.log"

    @staticmethod
    def _tail_text(path: Path, *, lines: int = 6, limit: int = 600) -> str:
        """取日志末尾若干行，用于把崩溃原因直接展示在启动失败提示里。"""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        tail = "\n".join(text.strip().splitlines()[-lines:]) if text.strip() else ""
        return tail[-limit:]

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
        if fresh.expired:
            # ensure_fresh 刷新失败时会原样返回旧令牌，过期令牌无法通过正版校验。
            return (
                Account.offline_account(username),
                "微软登录令牌已过期且自动刷新失败，已回退为离线模式启动",
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
            # 文件名来自第三方 API，取 basename 防止写出目标目录。
            filename = Path(str(chosen.filename or "")).name
            if not filename:
                raise RuntimeError("下载源返回的文件名无效")
            # 同名文件处于「已禁用」状态时先清掉，避免新旧两个文件同时出现在列表里。
            (target_dir / f"{filename}.disabled").unlink(missing_ok=True)
            entry = FileEntry(
                url=chosen.url,
                target_path=target_dir / filename,
                sha1=chosen.sha1,
                size=chosen.size,
                kind=kind,
                # CurseForge 的 CDN 下载必须带 Key。
                headers=provider.download_headers() or None,
            )
            # 单个 Mod / 资源包也可能很大，沿用全局并发数让文件内部分片也能并行下载。
            stats = Downloader(
                workers=self.config.download_workers, progress=task.emit
            ).download([entry], cancel=task.check_cancel)
            if stats.failed:
                raise RuntimeError(stats.failed[0][1])
            task.result = {
                "filename": filename,
                "path": str(entry.target_path),
                "kind": kind,
                "version": chosen.version,
                "skipped": stats.files_skipped > 0,
            }

        name = f"安装 {kind} {label}" + (f" → {version_id}" if version_id else "")
        existing = self._find_active_task(name)
        return existing or self._create_task(name, job)

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

    def mod_dependencies(
        self,
        source: str,
        project_id: str,
        *,
        game_version: Optional[str] = None,
        loader: Optional[str] = None,
    ) -> list[dict]:
        """解析目标 Mod 在所选版本下的必装前置，供界面弹窗一键安装。"""
        source = (source or "modrinth").lower()
        provider = build_provider(source, timeout=20)
        files = provider.resolve_files(
            project_id,
            game_version=game_version,
            loader=loader,
        )
        if not files:
            return []
        dependencies = files[0].dependencies or []
        result: list[dict] = []
        seen: set[str] = set()
        for dep in dependencies:
            if not isinstance(dep, dict):
                continue
            if source == "modrinth":
                if str(dep.get("dependency_type") or "") != "required":
                    continue
                dep_project = str(dep.get("project_id") or "")
                if not dep_project or dep_project == project_id or dep_project in seen:
                    continue
                seen.add(dep_project)
                name, icon, filename = dep_project, "", str(dep.get("file_name") or "")
                try:
                    status, payload = net.get_json_auth(
                        f"{MODRINTH_API}/project/{dep_project}",
                        headers={"Accept": "application/json"},
                        timeout=15,
                    )
                    if status == 200 and isinstance(payload, dict):
                        name = str(payload.get("title") or name)
                        icon = str(payload.get("icon_url") or "")
                except Exception:  # noqa: BLE001 - 名称查询失败不影响安装
                    pass
                if not filename:
                    try:
                        dep_files = provider.resolve_files(
                            dep_project, game_version=game_version, loader=loader
                        )
                        if dep_files:
                            filename = dep_files[0].filename
                    except Exception:  # noqa: BLE001
                        pass
                result.append(
                    {
                        "source": "modrinth",
                        "project_id": dep_project,
                        "name": name,
                        "icon_url": icon,
                        "filename": filename,
                        "version_id": str(dep.get("version_id") or ""),
                    }
                )
            else:
                if int(dep.get("relationType") or 0) != 3:
                    continue
                dep_project = str(dep.get("modId") or "")
                if not dep_project or dep_project == project_id or dep_project in seen:
                    continue
                seen.add(dep_project)
                name, icon, filename = dep_project, "", ""
                try:
                    status, payload = net.get_json_auth(
                        f"{CURSEFORGE_API}/mods/{dep_project}",
                        headers={"Accept": "application/json"},
                        timeout=15,
                    )
                    if status == 200 and isinstance(payload.get("data"), dict):
                        data = payload["data"]
                        name = str(data.get("name") or name)
                        icon = str((data.get("logo") or {}).get("thumbnailUrl") or "")
                except Exception:  # noqa: BLE001
                    pass
                try:
                    dep_files = provider.resolve_files(
                        dep_project, game_version=game_version, loader=loader
                    )
                    if dep_files:
                        filename = dep_files[0].filename
                except Exception:  # noqa: BLE001
                    pass
                result.append(
                    {
                        "source": "curseforge",
                        "project_id": dep_project,
                        "name": name,
                        "icon_url": icon,
                        "filename": filename,
                        "version_id": "",
                    }
                )
        return result

    def _is_isolated(self, version_id: Optional[str]) -> bool:
        if not version_id:
            return False
        return bool(self.config.merged_settings(version_id)["isolated"])

    # ------------------------------------------------------------ 本地内容管理
    def content_list(self, kind: str, version_id: Optional[str] = None) -> list[dict]:
        manager = self.content_manager()
        isolated = self._is_isolated(version_id)
        result = []
        for item in manager.list_local(kind, version_id=version_id, isolated=isolated):
            data = {
                "kind": item.kind,
                "filename": item.filename,
                "path": str(item.path),
                "enabled": item.enabled,
                "size": item.size,
                "modified": item.modified,
            }
            if kind == CONTENT_MOD:
                info = mod_info.read_mod_info(item.path)
                data.update(
                    {
                        "display_name": info.name,
                        "mod_version": info.version,
                        "mod_id": info.mod_id,
                        "mod_loader": info.loader,
                        "sha1": mod_info.cached_sha1(item.path),
                    }
                )
                if info.icon:
                    encoded = base64.b64encode(info.icon).decode("ascii")
                    data["icon"] = f"data:image/{info.icon_ext};base64,{encoded}"
            result.append(data)
        return result

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
        filename: Optional[str] = None,
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
            filename=filename,
        )
        self.log(f"导入 {kind}：{target.name}")
        self._capture_storage()
        return {"filename": target.name, "path": str(target)}

    # ------------------------------------------------------------ Mod 更新
    def check_mod_updates(self, version_id: str) -> dict[str, dict]:
        """先按 SHA1 查 Modrinth，查不到再按指纹查 CurseForge。"""
        profile = self.version_profile(version_id)
        game_version = profile.get("game_version") or None
        loader = profile.get("loader")
        loader = loader if loader not in (None, "", "vanilla", "optifine") else None
        modrinth = build_provider("modrinth", timeout=10)
        curseforge = build_provider("curseforge", timeout=15)

        updates: dict[str, dict] = {}
        pending: list[tuple[dict, str, int]] = []
        for item in self.content_list(CONTENT_MOD, version_id):
            sha1 = item.get("sha1")
            if not item.get("enabled") or not sha1:
                continue
            found = self._modrinth_update(modrinth, sha1, game_version, loader)
            if found:
                updates[item["filename"]] = found
                continue
            fingerprint = self._jar_fingerprint(str(item.get("path") or ""))
            if fingerprint:
                pending.append((item, sha1, fingerprint))

        if pending:
            try:
                matches = curseforge.fingerprint_lookup([fp for _, _, fp in pending])
            except Exception:  # noqa: BLE001 - CurseForge 不可用时只保留 Modrinth 结果
                matches = {}
            for item, sha1, fingerprint in pending:
                match = matches.get(fingerprint)
                if not match:
                    continue
                found = self._curseforge_update(
                    curseforge, match, sha1, game_version, loader
                )
                if found:
                    updates[item["filename"]] = found
        return updates

    @staticmethod
    def _jar_fingerprint(path: str) -> int:
        if not path:
            return 0
        try:
            return murmur2_fingerprint(path)
        except OSError:
            return 0

    @staticmethod
    def _modrinth_update(
        provider: ModProvider,
        sha1: str,
        game_version: str | None,
        loader: str | None,
    ) -> dict | None:
        try:
            status, payload = net.get_json_auth(
                f"{MODRINTH_API}/version_file/{sha1}?algorithm=sha1",
                headers={"Accept": "application/json"},
                timeout=10,
            )
        except Exception:  # noqa: BLE001 - 单个 Mod 查询失败不影响其它
            return None
        if status != 200 or not isinstance(payload, dict):
            return None
        project_id = str(payload.get("project_id") or "")
        if not project_id:
            return None
        current = str(payload.get("version_number") or "")
        # jar 内版本号常与平台版本号不一致，只用发布时间判断新旧。
        current_published = str(payload.get("date_published") or "")
        try:
            files = provider.resolve_files(
                project_id, game_version=game_version, loader=loader
            )
        except Exception:  # noqa: BLE001
            return None
        newer = pick_updated_file(files, sha1, current_published)
        if newer is None:
            return None
        return {
            "project_id": project_id,
            "current_version": current,
            "new_version": newer.version,
            "filename": newer.filename,
            "url": newer.url,
            "size": newer.size,
            "sha1": newer.sha1,
            "game_versions": newer.game_versions,
            "source": "modrinth",
        }

    @staticmethod
    def _curseforge_update(
        provider: ModProvider,
        match: dict,
        sha1: str,
        game_version: str | None,
        loader: str | None,
    ) -> dict | None:
        file = match.get("file") or {}
        project_id = str(file.get("modId") or "")
        if not project_id:
            return None
        current = str(file.get("displayName") or "")
        current_published = str(file.get("fileDate") or "")
        try:
            files = provider.resolve_files(
                project_id, game_version=game_version, loader=loader
            )
        except Exception:  # noqa: BLE001
            return None
        newer = pick_updated_file(files, sha1, current_published)
        if newer is None:
            return None
        return {
            "project_id": project_id,
            "current_version": current,
            "new_version": newer.version,
            "filename": newer.filename,
            "url": newer.url,
            "size": newer.size,
            "sha1": newer.sha1,
            "game_versions": newer.game_versions,
            "source": "curseforge",
        }

    def update_mod(
        self,
        version_id: str,
        filename: str,
        url: str,
        new_filename: str,
        *,
        expected_sha1: str = "",
    ) -> dict:
        """下载新版本 Mod 并替换旧文件。"""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jar") as handle:
            temp_path = Path(handle.name)
        try:
            net.download_file(url, temp_path, timeout=60)
            if expected_sha1:
                actual = mod_info.sha1_of(temp_path)
                if actual.lower() != expected_sha1.lower():
                    raise RuntimeError(f"SHA1 校验失败：{new_filename}")
            self.content_import(
                CONTENT_MOD,
                str(temp_path),
                version_id=version_id,
                overwrite=True,
                filename=new_filename,
            )
        finally:
            temp_path.unlink(missing_ok=True)
        if new_filename != filename:
            self.content_delete(CONTENT_MOD, filename, version_id)
        self.log(f"已更新 Mod：{filename} → {new_filename}")
        return {"filename": new_filename}

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

    def _saves_dir(self, version_id: Optional[str]) -> Path:
        if version_id and self._is_isolated(version_id):
            return self.paths().saves_dir_for(version_id, isolated=True)
        return self.paths().saves_dir

    def backup_save(self, name: str, version_id: Optional[str] = None) -> dict:
        """把存档打包到 data/backups，便于手动备份。"""
        source = self._saves_dir(version_id) / str(name)
        if not source.is_dir():
            raise ContentError(f"存档不存在：{name}")
        dest_dir = self.store.data_dir / "backups"
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = dest_dir / f"{name}-{stamp}.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for item in source.rglob("*"):
                if item.is_file():
                    archive.write(item, item.relative_to(source))
        self.log(f"已备份存档：{name} → {target}")
        return {"name": name, "path": str(target)}

    def import_save(self, archive_path: str, version_id: Optional[str] = None) -> dict:
        """从 zip 导入存档到 saves/<zip 名>。"""
        path = Path(archive_path)
        if not path.is_file():
            raise ContentError(f"文件不存在：{archive_path}")
        name = path.stem
        target = self._saves_dir(version_id) / name
        if target.exists():
            raise ContentError(f"同名存档已存在：{name}")
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                rel = Path(member.filename)
                if rel.is_absolute() or ".." in rel.parts:
                    continue
                archive.extract(member, target)
        self._capture_storage()
        self.log(f"已导入存档：{name}")
        return {"name": name, "path": str(target)}

    # ------------------------------------------------------------ 服务器列表
    def _servers_path(self, version_id: Optional[str]) -> Path:
        if version_id:
            base = self.paths().version_game_dir(
                version_id, isolated=self._is_isolated(version_id)
            )
        else:
            base = self.paths().root
        return base / "servers.dat"

    def list_servers(self, version_id: Optional[str] = None) -> list[dict]:
        return server_list.load_servers(self._servers_path(version_id))

    def add_server(
        self, name: str, ip: str, version_id: Optional[str] = None
    ) -> list[dict]:
        address = (ip or "").strip()
        if not address:
            raise RuntimeError("服务器地址不能为空")
        path = self._servers_path(version_id)
        servers = server_list.load_servers(path)
        servers.append({"name": (name or "").strip() or address, "ip": address})
        server_list.save_servers(path, servers)
        self._capture_storage()
        self.log(f"已添加服务器：{servers[-1]['name']}（{address}）")
        return servers

    def delete_server(
        self, index: int, version_id: Optional[str] = None
    ) -> list[dict]:
        path = self._servers_path(version_id)
        servers = server_list.load_servers(path)
        if not 0 <= int(index) < len(servers):
            raise RuntimeError("服务器不存在")
        removed = servers.pop(int(index))
        server_list.save_servers(path, servers)
        self._capture_storage()
        self.log(f"已删除服务器：{removed.get('name') or removed.get('ip')}")
        return servers

    def open_path(self, path: str) -> None:
        """在系统文件管理器中打开目录。"""
        target = Path(path)
        if not target.exists():
            raise RuntimeError(f"路径不存在：{path}")
        if os.name == "nt":
            os.startfile(str(target))  # noqa: S606 - 仅本机界面操作
        else:
            subprocess.Popen(["xdg-open", str(target)])

    def list_screenshots(self, version_id: Optional[str] = None) -> list[dict]:
        isolated = self._is_isolated(version_id)
        return [
            {"name": s.name, "path": str(s.path), "size": s.size, "modified": s.modified}
            for s in self.content_manager().list_screenshots(
                version_id=version_id, isolated=isolated
            )
        ]

    def screenshots_dir(self, version_id: Optional[str] = None) -> str:
        return str(
            self.content_manager().screenshot_dir(
                version_id=version_id, isolated=self._is_isolated(version_id)
            )
        )

    def delete_screenshot(self, name: str, version_id: Optional[str] = None) -> dict:
        self.content_manager().delete_screenshot(
            name, version_id=version_id, isolated=self._is_isolated(version_id)
        )
        self._capture_storage()
        self.log(f"删除截图：{name}")
        return {"name": name, "deleted": True}

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

    def _install_version_files(self, task: Task, version_id: str) -> None:
        """下载某个版本启动所需的 jar / libraries / 资源索引。

        加载器版本继承了原版文件，Forge / NeoForge 安装器生成的目录通常没有
        自己的客户端 jar；这里按版本解析结果补全，避免启动时 classpath 缺文件。
        """
        resolved = self.resolver().resolve(
            version_id,
            include_assets=False,
            prefer_local=True,
            cancel=task.check_cancel,
        )
        task.log(f"补全 {version_id} 的启动文件：{len(resolved.files)} 个")
        stats = Downloader(
            workers=self.config.download_workers, progress=task.emit
        ).download(resolved.files, cancel=task.check_cancel)
        if stats.failed:
            raise RuntimeError(
                f"{version_id} 文件下载失败，首个错误：{stats.failed[0][1]}"
            )
        task.log(
            f"{version_id} 文件就绪：下载 {stats.files_downloaded} 个，"
            f"跳过 {stats.files_skipped} 个"
        )

    def _prepare_modpack_version(self, task: Task, info, fallback: str = "") -> str:
        """确保整合包所需原版文件与加载器就绪，返回整合包应装入的版本 ID。

        整合包不能直接倒进一个空的版本目录：启动器安装时会清理没有版本
        JSON 的目录，而且缺少加载器时游戏无法启动。这里先补全原版文件，
        再按包内清单安装对应加载器，最后返回该加载器版本的目录名。
        """
        base = info.game_version or fallback
        if not base:
            raise RuntimeError("无法确定整合包的目标游戏版本")
        loader = (
            info.loader
            if info.loader in SUPPORTED_LOADERS and info.loader_version
            else ""
        )
        loader_version = info.loader_version if loader else ""
        if info.loader and not loader:
            task.log(f"加载器 {info.loader} 信息不完整或暂不支持，将按原版版本安装")
        target = f"Minecraft {base}" + (
            f" + {loader} {loader_version}" if loader else "（原版）"
        )
        task.log(f"整合包目标：{target}")

        def on_phase(stage: str, done: int, total: int) -> None:
            task.emit({"type": "phase", "phase": stage, "done": done, "total": total})

        if base in self.installed_versions():
            task.log(f"原版 {base} 已安装，跳过基础文件下载")
        else:
            resolved = self.resolver().resolve(
                base,
                include_assets=True,
                progress=on_phase,
                cancel=task.check_cancel,
            )
            task.log(f"校验原版文件：{len(resolved.files)} 个")
            stats = Downloader(
                workers=self.config.download_workers, progress=task.emit
            ).download(resolved.files, cancel=task.check_cancel)
            if stats.failed:
                raise RuntimeError(
                    f"原版文件下载失败，首个错误：{stats.failed[0][1]}"
                )
            task.log(
                f"原版文件就绪：下载 {stats.files_downloaded} 个，"
                f"跳过 {stats.files_skipped} 个"
            )

        if not loader:
            version_meta.write_meta(
                self.paths(), base, game_version=base, loader="vanilla"
            )
            return base

        task.log(f"安装加载器 {loader} {loader_version}")
        java = self.config.java
        if loader in ("forge", "neoforge"):
            java = self._loader_java(task, base)
        loader_installer = LoaderInstaller(self.paths(), java=java)
        result = loader_installer.install(
            loader,
            base,
            loader_version,
            progress=on_phase,
            log=task.log,
            cancel=task.check_cancel,
        )
        self._install_version_files(task, result.version_id)
        version_meta.write_meta(
            self.paths(),
            result.version_id,
            game_version=base,
            loader=loader,
            loader_version=loader_version,
        )
        task.log(f"加载器就绪：{result.version_id}")
        return result.version_id

    def _mark_modpack_isolated(self, version_id: str) -> None:
        """整合包安装到版本目录，必须开启版本隔离才能被游戏读取。"""
        if not version_id:
            return
        self.config.set_version_override(version_id, {"isolated": True})
        self.store.save(self.config)

    def start_modpack_install(
        self, path: str, *, version_id: str, name: str = ""
    ) -> Task:
        archive = Path(path)
        target_name = (name or "").strip()

        def job(task: Task) -> None:
            installer = self._modpack()

            def on_progress(stage: str, done: int, total: int) -> None:
                task.emit({"type": "phase", "phase": stage, "done": done, "total": total})

            info = installer.inspect(archive)
            target = self._prepare_modpack_version(task, info, version_id)
            meta = {
                "game_version": info.game_version or version_id,
                "loader": info.loader if info.loader in SUPPORTED_LOADERS else "vanilla",
                "loader_version": info.loader_version or "",
                "pack_name": info.name,
            }
            version_meta.write_meta(self.paths(), target, **meta)
            if target_name and target_name != target:
                version_meta.clone_version(self.paths(), target, target_name, meta)
                task.log(f"整合包版本命名：{target_name}")
                target = target_name
            self._mark_modpack_isolated(target)
            info = installer.install(
                archive,
                version_id=target,
                progress=on_progress,
                log=task.log,
                cancel=task.check_cancel,
            )
            task.result = {
                "version_id": target,
                "name": info.name,
                "format": info.format,
                "file_count": info.file_count,
            }
            task.log(f"整合包导入完成：{info.name} → {target}")

        label = f"导入整合包 {archive.name}"
        existing = self._find_active_task(label)
        return existing or self._create_task(label, self._serialized_modpack(job))

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
            isolated=self._is_isolated(version_id),
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
        version_name: str = "",
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
            filename = Path(str(chosen.filename or "")).name
            if not filename:
                raise RuntimeError("下载源返回的文件名无效")
            entry = FileEntry(
                url=chosen.url,
                target_path=archive_dir / filename,
                sha1=chosen.sha1,
                size=chosen.size,
                kind=CONTENT_MODPACK,
                headers=provider.download_headers() or None,
            )
            stats = Downloader(
                workers=self.config.download_workers, progress=task.emit
            ).download([entry], cancel=task.check_cancel)
            if stats.failed:
                raise RuntimeError(stats.failed[0][1])

            installer = self._modpack()
            info = installer.inspect(entry.target_path)
            target = self._prepare_modpack_version(task, info, version_id or "")
            meta = {
                "game_version": info.game_version or (version_id or ""),
                "loader": info.loader if info.loader in SUPPORTED_LOADERS else "vanilla",
                "loader_version": info.loader_version or "",
                "pack_name": info.name,
            }
            version_meta.write_meta(self.paths(), target, **meta)
            wanted = (version_name or "").strip()
            if wanted and wanted != target:
                version_meta.clone_version(self.paths(), target, wanted, meta)
                target = wanted
            self._mark_modpack_isolated(target)

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
                "filename": filename,
                "version_id": target,
                "name": info.name,
                "game_version": info.game_version,
                "loader": info.loader,
            }
            task.log(f"整合包安装完成：{info.name} → {target}")

        name = f"安装整合包 {label}"
        existing = self._find_active_task(name)
        return existing or self._create_task(name, self._serialized_modpack(job))

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
        try:
            result = self.auth.poll_auth(device_code)
            if result is None:
                return {"status": "pending"}
            account = self._register_msa(result)
        except Exception as exc:  # noqa: BLE001 - 记录完整错误，便于排查登录链路
            self.log(f"微软登录失败：{exc!r}")
            raise
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
        self.auth.refresh_auth(stored.refresh_token)
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
        # 先做跨字段校验再落配置，避免校验失败时把其它字段留在内存里造成状态不一致。
        if "root" in changes and changes["root"] and self.config.hash_store:
            raise RuntimeError("哈希存储开启时不能切换游戏目录，请先关闭哈希存储")

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
            "login_mode",
            "launcher_update_mode",
        }
        for key, value in changes.items():
            if key in allowed:
                setattr(self.config, key, value)

        # 下载线程限制在 1-1024；配置文件被手改时也不会起几千个线程。
        self.config.download_workers = max(
            1, min(1024, int(self.config.download_workers or 256))
        )

        if "root" in changes and changes["root"]:
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

    # ------------------------------------------------------------------ 自更新
    def check_launcher_update(self) -> dict:
        info = updater.fetch_latest()
        identity = updater.asset_identity(info)
        version = str(info.get("version") or "")
        info["identity"] = identity
        info["current"] = APP_VERSION
        info["available"] = launcher_update_available(
            version,
            APP_VERSION,
            identity,
            str(self.config.launcher_update_seen or ""),
        )
        info["frozen"] = bool(getattr(sys, "frozen", False))
        return info

    def mark_launcher_update_seen(self, identity: str) -> None:
        """记录已提醒 / 已安装的更新，保证同一次更新只提醒一次。"""
        if identity and self.config.launcher_update_seen != identity:
            self.config.launcher_update_seen = identity
            self.store.save(self.config)

    def launcher_changelog(self, limit: int = 8) -> list[dict]:
        """主页更新日志：直接展示 GitHub Release 的描述。"""
        return updater.fetch_releases(limit=limit)

    def _update_target(self) -> Path:
        if getattr(sys, "frozen", False):
            return updater.update_target()
        directory = self.store.data_dir / "updates"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / "A Minecraft Launcher-update.exe"

    def download_launcher_update(
        self,
        info: dict,
        *,
        on_chunk: Optional[Callable[[int], None]] = None,
        cancel: Optional[Callable[[], bool]] = None,
    ) -> dict:
        dest = self._update_target()
        dest.unlink(missing_ok=True)
        dest.with_name(dest.name + ".part").unlink(missing_ok=True)
        self.log(f"开始下载更新 {info.get('version')}（公共 GH 代理优先）")
        updater.download(info, dest, on_chunk=on_chunk, cancel=cancel)
        self.log(f"更新已下载：{dest}")
        return {"path": str(dest), "version": str(info.get("version") or "")}

    def apply_launcher_update(self, path: str) -> dict:
        script = updater.apply_update(Path(path))
        self.log(f"已生成更新脚本，退出后将替换并重启：{script}")
        return {"script": str(script)}

    # ------------------------------------------------------------------ 联机
    def relay_master(self) -> str:
        """主节点地址随安装包内置，不提供配置项。"""
        return relay_engine.master_endpoint()

    def relay_nodes(self) -> list[dict]:
        nodes = relay_engine.fetch_nodes(self.relay_master())
        if not nodes:
            raise relay_engine.RelayError("联机服务暂无可用节点")
        return nodes

    def _relay_display_name(self) -> str:
        """联机房间显示的名字：优先用当前登录账号，离线也能开房。"""
        account = self._accounts.active()
        return str(getattr(account, "name", "") or "")[:32]

    def relay_session(self, api: str) -> dict:
        """换取某个节点的会话令牌，过期前复用。"""
        cached = self._relay_sessions.get(api)
        if cached and time.time() < float(cached.get("expires_at") or 0) - 60:
            return cached
        payload = relay_engine.RelayClient(api).session(self._relay_display_name())
        payload["api"] = api
        payload["expires_at"] = time.time() + int(payload.get("expires_in") or 3600)
        self._relay_sessions[api] = payload
        return payload

    def _relay_client(self, api: str) -> relay_engine.RelayClient:
        session = self.relay_session(api)
        return relay_engine.RelayClient(api, token=str(session.get("session_token") or ""))

    def relay_pick_node(self) -> dict:
        """按在线节点负载选择节点，供自动开房使用。"""
        nodes = self.relay_nodes()
        online = [node for node in nodes if node.get("online", True) and node.get("api")]
        if not online:
            raise relay_engine.RelayError("联机服务暂无在线节点")
        return min(
            online,
            key=lambda node: (
                (int(node.get("rooms") or 0) / max(1, int(node.get("max_rooms") or 1))),
                int(node.get("rooms") or 0),
            ),
        )

    def relay_rooms(self, limit: int = 60) -> list[dict]:
        """汇总各节点的公开房间（隐藏房间只由服务端排除）。"""
        nodes = self.relay_nodes()
        errors: list[str] = []

        def fetch(node: dict) -> tuple[str, list[dict]]:
            api = str(node.get("api") or "")
            if not api:
                return api, []
            client = self._relay_client(api)
            return api, client.list_rooms()[:limit]

        results: list[tuple[str, list[dict]]] = []
        with ThreadPoolExecutor(max_workers=max(1, min(8, len(nodes)))) as pool:
            futures = [pool.submit(fetch, node) for node in nodes if node.get("api")]
            for future in futures:
                try:
                    results.append(future.result())
                except relay_engine.RelayError as exc:
                    errors.append(str(exc))
        room_list: list[dict] = []
        node_names = {str(node.get("api")): node.get("name") or node.get("id") for node in nodes}
        for api, items in results:
            for room in items:
                if not isinstance(room, dict):
                    continue
                room["api"] = api
                room["node_name"] = node_names.get(api, "")
                room_list.append(room)
        if not results and errors:
            raise relay_engine.RelayError(errors[0])
        return room_list

    def relay_room_info(self, api: str, room_id: str) -> dict:
        return self._relay_client(api).room_info(room_id)

    def relay_create_room(
        self,
        api: str = "",
        *,
        name: str,
        version: str,
        loader: str,
        max_players: int,
        local_port: int,
        provider: str = "aml",
        hidden: bool = False,
        mods: Optional[list[dict]] = None,
        mods_declared: bool = False,
    ) -> dict:
        if not api:
            api = str(self.relay_pick_node().get("api") or "")
        if not (1024 <= int(local_port) <= 65535):
            raise relay_engine.RelayError("本地端口不合法")
        # 只允许把真正的 Minecraft 服务打进隧道，防止隧道被用于其他 TCP 服务。
        if not relay_engine.probe_minecraft(int(local_port)):
            raise relay_engine.RelayError(
                "所选端口不是 Minecraft 服务，请先在游戏内对局域网开放后重试"
            )
        client = self._relay_client(api)
        room = client.create_room(
            {
                "name": name,
                "version": version,
                "loader": loader,
                "max_players": int(max_players),
                "local_port": int(local_port),
                "mode": "stcp",
                "provider": provider,
                "hidden": bool(hidden),
                "mods": list(mods or []),
                "mods_declared": bool(mods_declared or mods),
            }
        )
        room["api"] = api
        self._relay_runner.start(
            "host", f"room-{room['room_id']}", room["host_frpc"]
        )
        self._relay_room = room
        self._relay_guest = None
        self.log(
            f"联机：已创建房间 {room.get('join_code')}"
            f"（{'隐藏' if hidden else '公开'}，上限 {max_players} 人）"
        )
        return room

    def relay_find_room(self, code: str) -> tuple[str, dict]:
        """按 4 位房间号在各节点中查找房间。"""
        raw = "".join(ch for ch in str(code or "") if ch.isdigit())
        if len(raw) != 4:
            raise relay_engine.RelayError("请输入 4 位数字房间号")
        errors: list[str] = []
        for node in self.relay_nodes():
            api = str(node.get("api") or "")
            if not api:
                continue
            try:
                return api, self._relay_client(api).lookup(raw)
            except relay_engine.RelayError as exc:
                if exc.code in ("404", "410"):
                    continue
                errors.append(str(exc))
        if errors:
            raise relay_engine.RelayError(errors[0])
        raise relay_engine.RelayError("房间不存在或已过期", code="404")

    def relay_join_room(self, api: str, room_id: str, *, bind_port: int = 0) -> dict:
        port = int(bind_port) or relay_engine.pick_free_port()
        client = self._relay_client(api)
        result = client.join(room_id, bind_port=port)
        self._relay_runner.start(
            "guest", f"guest-{room_id}", result["visitor_frpc"], bind_port=port
        )
        entry = {
            "api": api,
            "room_id": room_id,
            "room": result.get("room") or {},
            "connect": f"127.0.0.1:{port}",
            "bind_port": port,
        }
        self._relay_guest = entry
        self._relay_room = None
        self.log(f"联机：已加入房间 {entry['room'].get('join_code') or room_id}")
        return entry

    def relay_heartbeat(self, api: str, room_id: str) -> dict:
        return self._relay_client(api).heartbeat(room_id)

    def relay_room_detail(self) -> dict:
        if not self._relay_room:
            return {}
        return self._relay_client(self._relay_room["api"]).detail(self._relay_room["room_id"])

    def relay_leave_room(self) -> None:
        guest = self._relay_guest
        self._relay_runner.stop()
        self._relay_guest = None
        if not guest:
            return
        try:
            relay_engine.RelayClient(
                guest["api"], token=str(self.relay_session(guest["api"]).get("session_token") or "")
            ).leave(guest["room_id"])
        except relay_engine.RelayError as exc:
            self.log(f"联机：退出房间时服务端返回异常（{exc}）")
        self.log("联机：已退出房间")

    def relay_close_room(self) -> None:
        room = self._relay_room
        self._relay_runner.stop()
        self._relay_room = None
        if not room:
            return
        try:
            relay_engine.RelayClient(
                room["api"], token=str(self.relay_session(room["api"]).get("session_token") or "")
            ).close(room["room_id"])
        except relay_engine.RelayError as exc:
            self.log(f"联机：关闭房间时服务端返回异常（{exc}）")
        self.log("联机：已关闭房间")

    def relay_status(self) -> dict:
        return {
            "tunnel": self._relay_runner.status(),
            "room": self._relay_room,
            "guest": self._relay_guest,
            "master": self.relay_master(),
        }

    # ---- 局域网端口 / 模组清单 / 启动参数 ----

    def relay_lan_port(self, version: str, *, default: int = 25565) -> int:
        """读取当前版本 latest.log 中“对局域网开放”的端口。"""
        if not version:
            return 0
        paths = self.paths()
        isolated = bool(self.config.merged_settings(version).get("isolated"))
        log_path = paths.version_game_dir(version, isolated=isolated) / "logs" / "latest.log"
        return relay_engine.detect_lan_port(log_path, default=default)

    def relay_scan_ports(self) -> dict:
        """扫描正在监听的 Minecraft 端口（不依赖是否由本启动器启动）。"""
        info = self.running_game_info()
        version = str(info.get("version") or "")
        log_port = self.relay_lan_port(version, default=0) if version else 0
        ports = relay_engine.scan_minecraft_ports(log_port or None)
        return {
            "running": bool(info),
            "version": version,
            "pid": info.get("pid"),
            "log_port": log_port,
            "ports": ports,
        }

    def relay_local_mods(self, version_id: str) -> list[dict]:
        """当前客户端已启用的 Mod 清单（用于开房时上传）。"""
        result = []
        for item in self.content_list(CONTENT_MOD, version_id):
            if not item.get("enabled"):
                continue
            filename = str(item.get("filename") or "")
            if not filename.lower().endswith(".jar"):
                continue
            result.append(
                {
                    "filename": filename,
                    "name": item.get("display_name") or filename,
                    "version": item.get("mod_version") or "",
                    "sha1": item.get("sha1") or "",
                    "mod_id": item.get("mod_id") or "",
                    "enabled": True,
                }
            )
        return result

    def relay_resolve_mods(self, mods: list[dict]) -> dict:
        """按 SHA1 反查 Modrinth（可选 CurseForge），补齐链接与下载地址。"""
        items = [dict(item) for item in mods if isinstance(item, dict)]

        def lookup(item: dict) -> dict:
            sha1 = str(item.get("sha1") or "").strip().lower()
            if sha1:
                try:
                    status, payload = net.get_json_auth(
                        f"{MODRINTH_API}/version_file/{sha1}", timeout=8
                    )
                    if status == 200 and isinstance(payload, dict):
                        project_id = str(payload.get("project_id") or "")
                        if project_id:
                            item["modrinth_url"] = f"https://modrinth.com/project/{project_id}"
                        files = payload.get("files") or []
                        for file in files:
                            if not isinstance(file, dict):
                                continue
                            if not item.get("download_url") or file.get("primary"):
                                item["download_url"] = str(file.get("url") or "")
                                item["filename"] = str(
                                    file.get("filename") or item.get("filename") or ""
                                )
                                if file.get("primary"):
                                    break
                except Exception:  # noqa: BLE001 - 查询失败不影响开房
                    pass
            return item

        with ThreadPoolExecutor(max_workers=max(1, min(8, len(items)))) as pool:
            resolved = list(pool.map(lookup, items)) if items else []
        found = sum(1 for item in resolved if item.get("modrinth_url") or item.get("curseforge_url"))
        return {"mods": resolved, "found": found, "missing": len(resolved) - found}

    def relay_join_options(self, host_mods: list[dict]) -> list[dict]:
        """把房主 Mod 清单与本机各客户端比对，供加入房间时选择。"""
        host_mods = [item for item in (host_mods or []) if isinstance(item, dict)]
        options = []
        for version in self.local_versions():
            version_id = str(version.get("id") or "")
            local = self.relay_local_mods(version_id) if version_id else []
            host_sha = {str(item.get("sha1") or "").lower() for item in host_mods if item.get("sha1")}
            host_files = {
                Path(str(item.get("filename") or "")).name.lower()
                for item in host_mods
                if item.get("filename")
            }
            local_sha = {str(item.get("sha1") or "").lower() for item in local if item.get("sha1")}
            local_files = {Path(str(item.get("filename") or "")).name.lower() for item in local}
            missing = [
                item
                for item in host_mods
                if not (
                    (item.get("sha1") and str(item.get("sha1")).lower() in local_sha)
                    or (
                        item.get("filename")
                        and Path(str(item.get("filename"))).name.lower() in local_files
                    )
                )
            ]
            extra = [
                item
                for item in local
                if not (
                    (item.get("sha1") and str(item.get("sha1")).lower() in host_sha)
                    or Path(str(item.get("filename") or "")).name.lower() in host_files
                )
            ]
            options.append(
                {
                    "version": version_id,
                    "game_version": version.get("game_version") or "",
                    "loader": version.get("loader") or "vanilla",
                    "match": not missing and not extra,
                    "missing": missing,
                    "extra": extra,
                    "host_count": len(host_mods),
                    "local_count": len(local),
                }
            )
        return options

    def relay_download_missing(self, version_id: str, mods: list[dict]) -> dict:
        """下载缺失 Mod 到指定客户端（仅处理带下载地址的条目）。"""
        installed: list[str] = []
        skipped: list[dict] = []
        temp_dir = Path(tempfile.mkdtemp(prefix="aml-relay-mod-"))
        try:
            for item in mods:
                url = str(item.get("download_url") or "").strip()
                filename = str(item.get("filename") or "").strip()
                if not url or not filename:
                    skipped.append(
                        {
                            "filename": filename or item.get("name") or "未知 Mod",
                            "reason": "未找到下载地址",
                        }
                    )
                    continue
                dest = temp_dir / Path(filename).name
                try:
                    net.download_file(url, dest, timeout=30, retries=2)
                    sha1 = str(item.get("sha1") or "").lower()
                    if sha1 and mod_info.cached_sha1(dest).lower() != sha1:
                        skipped.append({"filename": filename, "reason": "SHA1 校验失败"})
                        continue
                    self.content_import(
                        CONTENT_MOD, str(dest), version_id=version_id, filename=dest.name
                    )
                    installed.append(dest.name)
                except Exception as exc:  # noqa: BLE001 - 单个 Mod 失败继续处理其他
                    skipped.append({"filename": filename, "reason": str(exc)})
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
        return {"installed": installed, "skipped": skipped}

    def relay_launch_args(self, version_id: str, port: int) -> dict:
        """返回启动参数与是否支持自动进服。"""
        profile = self.version_profile(version_id)
        game_version = str(profile.get("game_version") or "")
        return {
            "args": relay_engine.launch_connect_args(game_version, int(port)),
            "auto": relay_engine.supports_auto_connect(game_version),
            "game_version": game_version,
        }

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
    """从版本目录名里提取原版版本号。

    - ``1.20.1`` / ``1.20.1-forge-47.2.0`` / ``fabric-loader-0.15.0-1.20.1``
      都返回 ``1.20.1``；
    - NeoForge 只写加载器版本（``neoforge-21.1.66``）时按编号规则推导为
      ``1.21.1``，不再把加载器版本误当成游戏版本。
    """
    import re

    text = version_id or ""
    matches = re.findall(r"\d+\.\d+(?:\.\d+)?", text)
    vanilla = [item for item in matches if item.startswith("1.")]
    if vanilla:
        return vanilla[0]
    lowered = text.lower()
    if "neoforge" in lowered and matches:
        head = matches[0].split(".")
        if len(head) >= 2:
            return f"1.{head[0]}.{head[1]}"
    return matches[0] if matches else text


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