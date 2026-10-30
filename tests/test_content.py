"""内容管理的离线单元测试（tempfile + mock，不访问网络）。

    python -m unittest tests.test_content -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.content_manager import (
    CONTENT_DATAPACK,
    CONTENT_MOD,
    CONTENT_MODPACK,
    CONTENT_RESOURCEPACK,
    CONTENT_SHADER,
    ContentError,
    ContentManager,
)
from engine.models import GamePaths


class ContentManagerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.paths = GamePaths(root=self.root)
        self.manager = ContentManager(self.paths)

    def _write(self, path: Path, data: bytes = b"x") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path


class DirForTests(ContentManagerTestCase):
    def test_non_isolated_dirs(self) -> None:
        self.assertEqual(self.manager.dir_for(CONTENT_MOD), self.root / "mods")
        self.assertEqual(
            self.manager.dir_for(CONTENT_RESOURCEPACK), self.root / "resourcepacks"
        )
        self.assertEqual(self.manager.dir_for(CONTENT_SHADER), self.root / "shaderpacks")
        self.assertEqual(self.manager.dir_for(CONTENT_DATAPACK), self.root / "datapacks")
        self.assertEqual(self.manager.dir_for(CONTENT_MODPACK), self.root / "modpacks")

    def test_isolated_dirs(self) -> None:
        version = "1.20.1"
        base = self.root / "versions" / version
        self.assertEqual(
            self.manager.dir_for(CONTENT_MOD, version_id=version, isolated=True),
            base / "mods",
        )
        self.assertEqual(
            self.manager.dir_for(CONTENT_SHADER, version_id=version, isolated=True),
            base / "shaderpacks",
        )
        # 未提供 version_id 时回落到全局目录。
        self.assertEqual(
            self.manager.dir_for(CONTENT_MOD, isolated=True), self.root / "mods"
        )

    def test_unknown_kind_raises(self) -> None:
        with self.assertRaises(ContentError):
            self.manager.dir_for("bogus")


class LocalContentTests(ContentManagerTestCase):
    def test_import_list_toggle_roundtrip(self) -> None:
        source = self.root / "src" / "coolmod.jar"
        self._write(source, b"jar-bytes")

        target = self.manager.import_file(CONTENT_MOD, source)
        self.assertEqual(target, self.root / "mods" / "coolmod.jar")
        self.assertTrue(target.exists())

        listed = self.manager.list_local(CONTENT_MOD)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].filename, "coolmod.jar")
        self.assertTrue(listed[0].enabled)
        self.assertEqual(listed[0].size, len(b"jar-bytes"))

        # 禁用：应重命名为 .disabled，列表里 enabled=False 且文件名还原。
        disabled = self.manager.set_enabled(CONTENT_MOD, "coolmod.jar", False)
        self.assertFalse(disabled.enabled)
        self.assertEqual(disabled.path.name, "coolmod.jar.disabled")
        self.assertFalse((self.root / "mods" / "coolmod.jar").exists())
        self.assertTrue((self.root / "mods" / "coolmod.jar.disabled").exists())

        listed = self.manager.list_local(CONTENT_MOD)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].filename, "coolmod.jar")
        self.assertFalse(listed[0].enabled)

        # 启用：还原回原始文件名。
        enabled = self.manager.set_enabled(CONTENT_MOD, "coolmod.jar", True)
        self.assertTrue(enabled.enabled)
        self.assertEqual(enabled.path.name, "coolmod.jar")
        self.assertTrue((self.root / "mods" / "coolmod.jar").exists())
        self.assertFalse((self.root / "mods" / "coolmod.jar.disabled").exists())

    def test_import_conflict_and_overwrite(self) -> None:
        source = self.root / "a.jar"
        self._write(source, b"one")
        self.manager.import_file(CONTENT_MOD, source)

        with self.assertRaises(ContentError):
            self.manager.import_file(CONTENT_MOD, source)

        self._write(source, b"one-two")
        self.manager.import_file(CONTENT_MOD, source, overwrite=True)
        self.assertEqual((self.root / "mods" / "a.jar").read_bytes(), b"one-two")

    def test_delete(self) -> None:
        source = self.root / "gone.zip"
        self._write(source, b"z")
        self.manager.import_file(CONTENT_RESOURCEPACK, source)
        self.assertEqual(len(self.manager.list_local(CONTENT_RESOURCEPACK)), 1)

        self.manager.delete(CONTENT_RESOURCEPACK, "gone.zip")
        self.assertEqual(self.manager.list_local(CONTENT_RESOURCEPACK), [])
        self.assertFalse((self.root / "resourcepacks" / "gone.zip").exists())

        with self.assertRaises(ContentError):
            self.manager.delete(CONTENT_RESOURCEPACK, "gone.zip")

    def test_delete_disabled_file(self) -> None:
        source = self.root / "off.jar"
        self._write(source, b"d")
        self.manager.import_file(CONTENT_MOD, source)
        self.manager.set_enabled(CONTENT_MOD, "off.jar", False)

        self.assertIn("off.jar", [c.filename for c in self.manager.list_local(CONTENT_MOD)])
        self.manager.delete(CONTENT_MOD, "off.jar")
        self.assertFalse((self.root / "mods" / "off.jar.disabled").exists())

    def test_ignores_part_and_tmp_and_dirs(self) -> None:
        self._write(self.root / "mods" / "ok.jar")
        self._write(self.root / "mods" / "half.jar.part")
        self._write(self.root / "mods" / "temp.jar.tmp")
        self._write(self.root / "mods" / "notes.txt")
        (self.root / "mods" / "subdir").mkdir(parents=True, exist_ok=True)

        filenames = [c.filename for c in self.manager.list_local(CONTENT_MOD)]
        self.assertEqual(filenames, ["ok.jar"])

    def test_list_local_missing_dir(self) -> None:
        self.assertEqual(self.manager.list_local(CONTENT_MOD), [])


class SaveAndScreenshotTests(ContentManagerTestCase):
    def test_list_saves(self) -> None:
        self._write(self.paths.saves_dir / "World1" / "level.dat", b"12345")
        self._write(self.paths.saves_dir / "World1" / "region" / "r.0.0.mca", b"abc")
        self._write(self.paths.saves_dir / "World2" / "level.dat", b"z")
        # 存档根目录下的散落文件不算存档。
        self._write(self.paths.saves_dir / "stray.txt", b"no")

        saves = self.manager.list_saves()
        self.assertEqual([s.name for s in saves], ["World1", "World2"])
        world1 = saves[0]
        self.assertEqual(world1.size, 5 + 3)
        self.assertGreater(world1.modified, 0)

    def test_list_saves_isolated(self) -> None:
        version = "1.19.4"
        self._write(
            self.paths.saves_dir_for(version, isolated=True) / "W" / "level.dat", b"hi"
        )
        self.assertEqual([s.name for s in self.manager.list_saves()], [])
        saves = self.manager.list_saves(version_id=version, isolated=True)
        self.assertEqual([s.name for s in saves], ["W"])

    def test_delete_save(self) -> None:
        self._write(self.paths.saves_dir / "World1" / "level.dat", b"12345")
        self.manager.delete_save("World1")
        self.assertFalse((self.paths.saves_dir / "World1").exists())
        with self.assertRaises(ContentError):
            self.manager.delete_save("World1")

    def test_list_screenshots(self) -> None:
        self._write(self.paths.screenshots_dir / "a.png", b"1")
        self._write(self.paths.screenshots_dir / "b.jpg", b"22")
        self._write(self.paths.screenshots_dir / "c.JPEG", b"333")
        self._write(self.paths.screenshots_dir / "note.txt", b"ignore")

        names = [s.name for s in self.manager.list_screenshots()]
        self.assertEqual(names, ["a.png", "b.jpg", "c.JPEG"])
        self.assertEqual(self.manager.list_screenshots()[1].size, 2)


class RemoteSearchTests(ContentManagerTestCase):
    def test_search_passes_project_type(self) -> None:
        with mock.patch(
            "engine.net.get_json_auth", return_value=(200, {"hits": []})
        ) as called:
            self.manager.search(CONTENT_RESOURCEPACK, "shiny", game_version="1.20.1")

        url = called.call_args.args[0]
        # facets 为 JSON 再 urlencode，冒号会被编码成 %3A。
        self.assertIn("project_type%3Aresourcepack", url)
        self.assertNotIn("project_type%3Amod", url)

    def test_search_default_mod_project_type(self) -> None:
        with mock.patch(
            "engine.net.get_json_auth", return_value=(200, {"hits": []})
        ) as called:
            self.manager.search(CONTENT_MOD, "sodium")

        url = called.call_args.args[0]
        self.assertIn("project_type%3Amod", url)

    def test_search_parses_hits_and_page_url(self) -> None:
        payload = {
            "hits": [
                {
                    "project_id": "abc",
                    "title": "Shader Pack",
                    "slug": "shader-pack",
                    "description": "pretty",
                    "downloads": 10,
                }
            ]
        }
        with mock.patch("engine.net.get_json_auth", return_value=(200, payload)):
            results = self.manager.search(CONTENT_SHADER, "pack")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].project_id, "abc")
        self.assertEqual(results[0].page_url, "https://modrinth.com/shader/shader-pack")

    def test_search_unknown_kind_raises(self) -> None:
        with self.assertRaises(ContentError):
            self.manager.search("bogus", "x")

    def test_resolve_files_unknown_kind_raises(self) -> None:
        with self.assertRaises(ContentError):
            self.manager.resolve_files("bogus", "proj-id")

    def test_resolve_files_modrinth(self) -> None:
        payload = [
            {
                "id": "ver1",
                "version_number": "1.0",
                "game_versions": ["1.20.1"],
                "files": [
                    {
                        "filename": "pack.zip",
                        "url": "https://cdn/pack.zip",
                        "size": 7,
                        "hashes": {"sha1": "a" * 40},
                    }
                ],
            }
        ]
        with mock.patch("engine.net.get_json_auth", return_value=(200, payload)):
            files = self.manager.resolve_files(
                CONTENT_RESOURCEPACK, "proj-id", game_version="1.20.1"
            )

        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].filename, "pack.zip")
        self.assertEqual(files[0].size, 7)


if __name__ == "__main__":
    unittest.main()