"""应用层（配置 / 密钥 / 任务调度）的离线单元测试。

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config as app_config
from app.config import ConfigStore, LauncherConfig
from app.secrets import SecretStore
from app.tasks import TaskManager, TaskStatus

# SecretStore 测试用任意键名（不依赖具体业务常量）。
TEST_KEY = "test_api_key"


class ConfigStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_default_config_created(self) -> None:
        store = ConfigStore(data_dir=self.root)
        cfg = store.load()
        self.assertEqual(cfg.root, str(app_config.DEFAULT_ROOT))
        self.assertTrue((self.root / app_config.CONFIG_FILENAME).exists())

    def test_roundtrip(self) -> None:
        store = ConfigStore(data_dir=self.root)
        cfg = store.load()
        cfg.memory_mb = 8192
        cfg.last_version = "1.20.1"
        cfg.root = str(self.root / "game")
        store.save(cfg)

        reloaded = ConfigStore(data_dir=self.root).load()
        self.assertEqual(reloaded.memory_mb, 8192)
        self.assertEqual(reloaded.last_version, "1.20.1")
        self.assertEqual(reloaded.root, str(self.root / "game"))

    def test_custom_root_kept(self) -> None:
        store = ConfigStore(data_dir=self.root)
        cfg = store.load()
        cfg.root = "D:/mc"
        store.save(cfg)
        self.assertEqual(ConfigStore(data_dir=self.root).load().root, "D:/mc")

    def test_legacy_data_root_migrated(self) -> None:
        store = ConfigStore(data_dir=self.root)
        store.config_path.write_text(
            json.dumps({"root": str(self.root)}), encoding="utf-8"
        )
        self.assertEqual(store.load().root, str(app_config.DEFAULT_ROOT))

    def test_unknown_keys_ignored(self) -> None:
        store = ConfigStore(data_dir=self.root)
        store.config_path.write_text(
            json.dumps({"memory_mb": 4096, "bogus_key": 1}), encoding="utf-8"
        )
        cfg = store.load()
        self.assertEqual(cfg.memory_mb, 4096)
        self.assertFalse(hasattr(cfg, "bogus_key"))

    def test_to_game_paths(self) -> None:
        cfg = LauncherConfig(root=str(self.root), java="/opt/java/bin/java")
        paths = cfg.to_game_paths()
        self.assertEqual(paths.root, self.root)
        self.assertEqual(paths.java, "/opt/java/bin/java")


class SecretStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SecretStore(service="minicraftlauncher-test")
        self.addCleanup(self.store.delete, TEST_KEY)

    def test_set_get_delete(self) -> None:
        self.store.set(TEST_KEY, "abc123")
        self.assertEqual(self.store.get(TEST_KEY), "abc123")
        self.store.delete(TEST_KEY)
        self.assertIsNone(self.store.get(TEST_KEY))

    def test_empty_value_clears(self) -> None:
        self.store.set(TEST_KEY, "abc123")
        self.store.set(TEST_KEY, "")
        self.assertIsNone(self.store.get(TEST_KEY))

    def test_masked(self) -> None:
        self.store.set(TEST_KEY, "abcdefghijklmnop")
        masked = self.store.masked(TEST_KEY)
        self.assertEqual(masked, "abcd" + "*" * 8 + "mnop")
        # 短值全部打码。
        self.store.set(TEST_KEY, "short")
        self.assertEqual(self.store.masked(TEST_KEY), "*" * 5)

    def test_memory_fallback_when_unavailable(self) -> None:
        fallback = SecretStore(service="minicraftlauncher-test")
        fallback._available = False  # 模拟无 keyring 后端
        fallback.set(TEST_KEY, "v")
        self.assertEqual(fallback.get(TEST_KEY), "v")
        fallback.delete(TEST_KEY)
        self.assertIsNone(fallback.get(TEST_KEY))
        self.assertFalse(fallback.available)


class TaskManagerTests(unittest.TestCase):
    def _wait_done(self, manager: TaskManager, task_id: str, timeout: float = 5.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            snap = manager.snapshot(task_id)
            if snap and snap["status"] in {
                TaskStatus.DONE.value,
                TaskStatus.FAILED.value,
                TaskStatus.CANCELLED.value,
            }:
                return snap
            time.sleep(0.02)
        self.fail("任务未在超时内结束")

    def test_success_task(self) -> None:
        manager = TaskManager()
        captured = {}

        def job(task):
            task.log("干活中")
            task.emit({"type": "progress", "percent": 50.0, "files_done": 1, "files_total": 2})
            task.emit({"type": "progress", "percent": 100.0, "files_done": 2, "files_total": 2})
            task.result = {"files": 2}
            captured["task"] = task

        task = manager.create("测试任务", job)
        snap = self._wait_done(manager, task.id)
        self.assertEqual(snap["status"], TaskStatus.DONE.value)
        self.assertEqual(snap["percent"], 100.0)
        self.assertEqual(snap["result"], {"files": 2})
        self.assertTrue(any("干活中" in line for line in snap["logs"]))

    def test_failed_task(self) -> None:
        manager = TaskManager()

        def job(task):
            raise RuntimeError("boom")

        task = manager.create("会失败", job)
        snap = self._wait_done(manager, task.id)
        self.assertEqual(snap["status"], TaskStatus.FAILED.value)
        self.assertIn("boom", snap["error"])

    def test_cancel_task(self) -> None:
        manager = TaskManager()
        started = time.time()

        def job(task):
            while not task.check_cancel() and time.time() - started < 3:
                time.sleep(0.01)

        task = manager.create("可取消", job)
        time.sleep(0.05)
        self.assertTrue(manager.cancel(task.id))
        snap = self._wait_done(manager, task.id)
        self.assertEqual(snap["status"], TaskStatus.CANCELLED.value)

    def test_stream_emits_sse_and_end(self) -> None:
        manager = TaskManager()

        def job(task):
            task.emit({"type": "progress", "percent": 100.0})

        task = manager.create("流式任务", job)
        self._wait_done(manager, task.id)

        chunks = list(manager.stream(task.id))
        self.assertTrue(chunks[0].startswith("data: "))
        self.assertIn('"type": "open"', chunks[0])
        joined = "".join(chunks)
        self.assertIn('"type": "progress"', joined)
        self.assertIn('"type": "end"', joined)

    def test_stream_unknown_task(self) -> None:
        manager = TaskManager()
        chunks = list(manager.stream("nope"))
        self.assertIn("task not found", "".join(chunks))

    def test_global_logs_revision(self) -> None:
        manager = TaskManager()
        _, rev0 = manager.global_logs()
        manager.log("hello")
        logs, rev1 = manager.global_logs()
        self.assertGreater(rev1, rev0)
        self.assertTrue(any("hello" in line for line in logs))

    def test_list_snapshots_sorted(self) -> None:
        manager = TaskManager()

        def job(task):
            time.sleep(0.01)

        first = manager.create("一", job)
        time.sleep(0.02)
        manager.create("二", job)
        self._wait_done(manager, first.id)

        names = [s["name"] for s in manager.list_snapshots()]
        self.assertEqual(names[0], "二")


if __name__ == "__main__":
    unittest.main()