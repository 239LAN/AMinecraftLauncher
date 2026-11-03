"""1.3.0 新能力的离线单元测试：服务器 NBT、Mod 元数据、版本命名。

    python -m unittest tests.test_meta -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import mod_info, server_list, version_meta
from engine.models import GamePaths

PNG_ICON = b"\x89PNG\r\n\x1a\n" + b"0" * 16


class MetaTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.paths = GamePaths(root=self.root)


class ServerListTests(MetaTestCase):
    def test_roundtrip(self) -> None:
        path = self.root / "servers.dat"
        servers = [
            {"name": "本地", "ip": "localhost:25565", "accept_textures": True},
            {"name": "群服", "ip": "mc.example.com", "icon": "aGVsbG8="},
        ]
        server_list.save_servers(path, servers)
        loaded = server_list.load_servers(path)
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["name"], "本地")
        self.assertEqual(loaded[0]["ip"], "localhost:25565")
        self.assertTrue(loaded[0]["accept_textures"])
        self.assertEqual(loaded[1]["icon"], "aGVsbG8=")
        self.assertFalse(loaded[1]["accept_textures"])

    def test_missing_file_and_empty_entry(self) -> None:
        self.assertEqual(server_list.load_servers(self.root / "nope.dat"), [])
        path = self.root / "servers.dat"
        server_list.save_servers(path, [{"name": "空地址", "ip": ""}])
        self.assertEqual(server_list.load_servers(path), [])

    def test_accept_textures_is_byte_tag(self) -> None:
        path = self.root / "servers.dat"
        server_list.save_servers(path, [{"name": "x", "ip": "host", "accept_textures": True}])
        with __import__("gzip").open(path, "rb") as handle:
            data = handle.read()
        name_at = data.index(b"acceptTextures")
        tag_at = name_at - 3
        # Vanilla 要求这里是 TAG_BYTE(1)，值 1 表示接受服务器材质包。
        self.assertEqual(data[tag_at], 1)
        self.assertEqual(data[name_at + len(b"acceptTextures")], 1)


class ModInfoTests(MetaTestCase):
    def _jar(self, name: str, member: str, payload: str, extra: dict[str, bytes] | None = None) -> Path:
        path = self.root / name
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(member, payload)
            for key, value in (extra or {}).items():
                archive.writestr(key, value)
        return path

    def test_fabric_metadata_and_icon(self) -> None:
        payload = json.dumps(
            {
                "id": "coolmod",
                "name": "Cool Mod",
                "version": "2.3.4",
                "icon": "assets/coolmod/icon.png",
            }
        )
        path = self._jar(
            "coolmod.jar",
            "fabric.mod.json",
            payload,
            {"assets/coolmod/icon.png": PNG_ICON},
        )
        info = mod_info.read_mod_info(path)
        self.assertEqual(info.name, "Cool Mod")
        self.assertEqual(info.version, "2.3.4")
        self.assertEqual(info.mod_id, "coolmod")
        self.assertEqual(info.loader, "fabric")
        self.assertEqual(info.icon, PNG_ICON)
        self.assertEqual(info.icon_ext, "png")

    def test_forge_toml(self) -> None:
        payload = """
[[mods]]
modId = "examplemod"
displayName = "Example Mod"
version = "1.0.0"
logoFile = "logo.png"
"""
        path = self._jar("example.jar", "META-INF/mods.toml", payload, {"logo.png": PNG_ICON})
        info = mod_info.read_mod_info(path)
        self.assertEqual(info.name, "Example Mod")
        self.assertEqual(info.version, "1.0.0")
        self.assertEqual(info.loader, "forge")
        self.assertEqual(info.icon, PNG_ICON)

    def test_manifest_fallback_and_bad_jar(self) -> None:
        manifest = "Implementation-Title: Plain Mod\nImplementation-Version: 9.9\n"
        path = self._jar("plain.jar", "META-INF/MANIFEST.MF", manifest)
        info = mod_info.read_mod_info(path)
        self.assertEqual(info.name, "Plain Mod")
        self.assertEqual(info.version, "9.9")

        bad = self.root / "broken.jar"
        bad.write_bytes(b"not a zip")
        self.assertEqual(mod_info.read_mod_info(bad).name, "broken")

    def test_sha1_cache(self) -> None:
        path = self.root / "a.jar"
        path.write_bytes(b"abc")
        digest = mod_info.cached_sha1(path)
        self.assertEqual(len(digest), 40)
        self.assertEqual(digest, mod_info.cached_sha1(path))
        path.write_bytes(b"abcd")
        self.assertNotEqual(digest, mod_info.cached_sha1(path))


class VersionMetaTests(MetaTestCase):
    def _make_version(self, version_id: str) -> Path:
        directory = self.paths.version_dir(version_id)
        directory.mkdir(parents=True)
        (directory / f"{version_id}.json").write_text(
            json.dumps({"id": version_id, "type": "release"}), encoding="utf-8"
        )
        return directory

    def test_write_and_detect(self) -> None:
        self._make_version("1.20.1")
        version_meta.write_meta(
            self.paths, "1.20.1", game_version="1.20.1", loader="fabric", loader_version="0.15.0"
        )
        meta = version_meta.detect_from_meta(self.paths, "1.20.1")
        self.assertEqual(meta["game_version"], "1.20.1")
        self.assertEqual(meta["loader"], "fabric")
        self.assertEqual(meta["loader_version"], "0.15.0")
        self.assertIsNone(version_meta.detect_from_meta(self.paths, "缺失"))

    def test_clone_version(self) -> None:
        self._make_version("1.20.1")
        (self.paths.version_dir("1.20.1") / "mods").mkdir()
        version_meta.clone_version(
            self.paths,
            "1.20.1",
            "我的整合包",
            {"game_version": "1.20.1", "loader": "fabric", "loader_version": "0.15.0"},
        )
        target = self.paths.version_dir("我的整合包")
        self.assertTrue((target / "mods").is_dir())
        self.assertFalse((target / "1.20.1.json").exists())
        data = json.loads((target / "我的整合包.json").read_text(encoding="utf-8"))
        self.assertEqual(data["id"], "我的整合包")
        self.assertEqual(version_meta.read_meta(self.paths, "我的整合包")["loader"], "fabric")

        with self.assertRaises(version_meta.VersionMetaError):
            version_meta.clone_version(self.paths, "1.20.1", "我的整合包", {})

    def test_rename_version(self) -> None:
        self._make_version("1.20.1")
        version_meta.write_meta(self.paths, "1.20.1", game_version="1.20.1", loader="vanilla")
        version_meta.rename_version(self.paths, "1.20.1", "原版备份")
        self.assertFalse(self.paths.version_dir("1.20.1").exists())
        target = self.paths.version_dir("原版备份")
        self.assertTrue((target / "原版备份.json").is_file())
        data = json.loads((target / "原版备份.json").read_text(encoding="utf-8"))
        self.assertEqual(data["id"], "原版备份")
        self.assertEqual(version_meta.read_meta(self.paths, "原版备份")["game_version"], "1.20.1")


if __name__ == "__main__":
    unittest.main()
