"""哈希文件库单元测试（内容寻址 + 文件树 + 原子迁移）。

    python -m unittest tests.test_hash_store -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.hash_store import HashStore, HashStoreError


class HashStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = HashStore(self.root / "mcfile")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_capture_and_materialize(self) -> None:
        work = self.root / "work"
        (work / "versions" / "1.20.1").mkdir(parents=True)
        (work / "versions" / "1.20.1" / "1.20.1.json").write_text("{}", encoding="utf-8")
        (work / "libraries" / "a").mkdir(parents=True)
        (work / "libraries" / "a" / "x.jar").write_bytes(b"jar")
        (work / "saves" / "world").mkdir(parents=True)
        (work / "saves" / "world" / "level.dat").write_bytes(b"save")

        result = self.store.capture(work)
        self.assertEqual(result["added"], 3)
        self.assertEqual(len(self.store.load()), 3)

        target = self.root / "restore"
        self.store.materialize(target)
        self.assertEqual((target / "versions/1.20.1/1.20.1.json").read_text(), "{}")
        self.assertEqual((target / "libraries/a/x.jar").read_bytes(), b"jar")
        self.assertEqual((target / "saves/world/level.dat").read_bytes(), b"save")
        # 只读 jar 走硬链接，其余为真实复制。
        self.assertEqual(
            (target / "libraries/a/x.jar").stat().st_ino,
            self.store.blob_path(self.store.entry("libraries/a/x.jar")["sha1"]).stat().st_ino,
        )
        self.assertNotEqual(
            (target / "saves/world/level.dat").stat().st_ino,
            self.store.blob_path(self.store.entry("saves/world/level.dat")["sha1"]).stat().st_ino,
        )

    def test_dedup_and_prune(self) -> None:
        work = self.root / "work"
        work.mkdir()
        (work / "a.bin").write_bytes(b"same")
        (work / "b.bin").write_bytes(b"same")
        self.store.capture(work)
        self.assertEqual(len(self.store.load()), 2)
        self.assertEqual(self.store.stats()["blobs"], 1)
        self.assertEqual(self.store.stats()["saved_bytes"], 4)

        (work / "b.bin").unlink()
        result = self.store.capture(work)
        self.assertEqual(result["removed"], 1)
        self.assertEqual(set(self.store.load()), {"a.bin"})
        self.assertEqual(self.store.stats()["blobs"], 1)

        (work / "a.bin").unlink()
        self.store.capture(work)
        self.assertEqual(self.store.load(), {})
        self.assertEqual(self.store.stats()["blobs"], 0)

    def test_migrate_in_and_out(self) -> None:
        game_root = self.root / ".minecraft"
        work = self.root / ".work"
        (game_root / "mods").mkdir(parents=True)
        (game_root / "mods" / "a.jar").write_bytes(b"jar")
        (game_root / "options.txt").write_text("fov:90", encoding="utf-8")

        self.store.migrate_in(game_root, work)
        self.assertFalse(game_root.exists())
        self.assertEqual((work / "mods" / "a.jar").read_bytes(), b"jar")

        self.store.migrate_out(work, game_root)
        self.assertFalse(work.exists())
        self.assertEqual((game_root / "mods" / "a.jar").read_bytes(), b"jar")
        self.assertEqual((game_root / "options.txt").read_text(encoding="utf-8"), "fov:90")

    def test_missing_blob_raises(self) -> None:
        work = self.root / "work"
        work.mkdir()
        (work / "a.txt").write_text("x", encoding="utf-8")
        self.store.capture(work)
        sha1 = self.store.entry("a.txt")["sha1"]
        self.store.blob_path(sha1).unlink()
        with self.assertRaises(HashStoreError):
            self.store.materialize(self.root / "out")


if __name__ == "__main__":
    unittest.main()
