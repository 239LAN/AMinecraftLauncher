"""整合包导入导出离线单元测试（现场构造 zip，用假的网络层）。

    python -m unittest tests.test_modpack -v
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import modpack
from engine.models import GamePaths
from engine.mod_provider import ModFile
from engine.modpack import ModpackError, ModpackInstaller

# 下载替身写入的固定内容（大小与 sha1 都要对得上索引）。
PAYLOAD = b"MOD!"
PAYLOAD_SHA1 = hashlib.sha1(PAYLOAD).hexdigest()


def _write_zip(path: Path, members: dict) -> Path:
    """把 {成员名: str|bytes} 写成 zip。"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data.encode("utf-8") if isinstance(data, str) else data)
    return path


def _mrpack_index() -> dict:
    return {
        "formatVersion": 1,
        "game": "minecraft",
        "versionId": "1.2.3",
        "name": "Test Pack",
        "dependencies": {"minecraft": "1.20.1", "fabric-loader": "0.15.11"},
        "files": [
            {
                "path": "mods/foo.jar",
                "hashes": {"sha1": PAYLOAD_SHA1},
                "env": {"client": "required"},
                "downloads": ["https://cdn/foo.jar"],
                "fileSize": len(PAYLOAD),
            },
            {
                "path": "config/x.json",
                "hashes": {"sha1": PAYLOAD_SHA1},
                "env": {"client": "required"},
                "downloads": ["https://cdn/x.json"],
                "fileSize": len(PAYLOAD),
            },
            {
                # 客户端不支持，应被跳过。
                "path": "mods/server-only.jar",
                "hashes": {"sha1": PAYLOAD_SHA1},
                "env": {"client": "unsupported"},
                "downloads": ["https://cdn/server-only.jar"],
                "fileSize": len(PAYLOAD),
            },
        ],
    }


def _cf_manifest() -> dict:
    return {
        "minecraft": {
            "version": "1.20.1",
            "modLoaders": [{"id": "forge-47.2.0", "primary": True}],
        },
        "manifestType": "minecraftModpack",
        "manifestVersion": 1,
        "name": "CF Pack",
        "version": "2.0",
        "author": "someone",
        "files": [{"projectID": 123, "fileID": 456, "required": True}],
    }


class ModpackInspectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.installer = ModpackInstaller(GamePaths(self.root))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_inspect_mrpack(self) -> None:
        archive = _write_zip(
            self.root / "pack.mrpack",
            {
                "modrinth.index.json": json.dumps(_mrpack_index()),
                "overrides/config/foo.toml": "a = 1",
                "client-overrides/options.txt": "gui = 1",
            },
        )
        info = self.installer.inspect(archive)

        self.assertEqual(info.format, "mrpack")
        self.assertEqual(info.name, "Test Pack")
        self.assertEqual(info.version, "1.2.3")
        self.assertEqual(info.game_version, "1.20.1")
        self.assertEqual(info.loader, "fabric")
        self.assertEqual(info.loader_version, "0.15.11")
        self.assertEqual(info.file_count, 2)  # 排除 client=unsupported
        self.assertEqual(info.override_count, 2)

    def test_inspect_curseforge(self) -> None:
        archive = _write_zip(
            self.root / "pack.zip",
            {
                "manifest.json": json.dumps(_cf_manifest()),
                "overrides/config/foo.cfg": "x=1",
            },
        )
        info = self.installer.inspect(archive)

        self.assertEqual(info.format, "curseforge")
        self.assertEqual(info.name, "CF Pack")
        self.assertEqual(info.version, "2.0")
        self.assertEqual(info.game_version, "1.20.1")
        self.assertEqual(info.loader, "forge")
        self.assertEqual(info.loader_version, "47.2.0")
        self.assertEqual(info.file_count, 1)
        self.assertEqual(info.override_count, 1)

    def test_zip_with_modrinth_index_is_mrpack(self) -> None:
        archive = _write_zip(
            self.root / "renamed.zip",
            {"modrinth.index.json": json.dumps(_mrpack_index())},
        )
        self.assertEqual(self.installer.inspect(archive).format, "mrpack")

    def test_unrecognized_format_raises(self) -> None:
        archive = _write_zip(self.root / "junk.zip", {"readme.txt": "hello"})
        with self.assertRaises(ModpackError):
            self.installer.inspect(archive)


class ModpackInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = GamePaths(self.root)
        self.installer = ModpackInstaller(self.paths)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _fake_download(self, calls: list):
        def fake(url, dest, **kwargs):
            dest = Path(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(PAYLOAD)
            on_chunk = kwargs.get("on_chunk")
            if on_chunk:
                on_chunk(len(PAYLOAD))
            calls.append((url, dest, kwargs.get("headers")))
            return len(PAYLOAD)

        return fake

    def test_install_mrpack(self) -> None:
        archive = _write_zip(
            self.root / "pack.mrpack",
            {
                "modrinth.index.json": json.dumps(_mrpack_index()),
                "overrides/config/foo.toml": "a = 1",
                "client-overrides/options.txt": "gui = 1",
            },
        )
        calls: list = []
        events: list = []

        with mock.patch.object(
            modpack.net, "download_file", side_effect=self._fake_download(calls)
        ):
            info = self.installer.install(
                archive,
                version_id="tp",
                progress=lambda *args: events.append(args),
            )

        game_dir = self.paths.version_dir("tp")
        # overrides 被复制到版本目录
        self.assertTrue((game_dir / "config" / "foo.toml").is_file())
        # client-overrides 也复制到版本目录根
        self.assertTrue((game_dir / "options.txt").is_file())
        # files 下载到正确路径
        self.assertTrue((game_dir / "mods" / "foo.jar").is_file())
        self.assertTrue((game_dir / "config" / "x.json").is_file())
        # client=unsupported 的条目被跳过
        self.assertFalse((game_dir / "mods" / "server-only.jar").exists())

        urls = {url for url, _, _ in calls}
        self.assertEqual(urls, {"https://cdn/foo.jar", "https://cdn/x.json"})
        self.assertEqual(info.file_count, 2)

        # 进度回调最终为 (2, 2)
        self.assertEqual(events[0], ("mods", 0, 2))
        self.assertEqual(events[-1], ("mods", 2, 2))

    def test_install_curseforge(self) -> None:
        archive = _write_zip(
            self.root / "pack.zip",
            {
                "manifest.json": json.dumps(_cf_manifest()),
                "overrides/config/foo.cfg": "x=1",
            },
        )

        class FakeProvider:
            def resolve_files(self, project_id, **kwargs):
                return [
                    ModFile(
                        source="curseforge",
                        project_id=str(project_id),
                        file_id="456",
                        filename="bar.jar",
                        url="https://cf/bar.jar",
                        size=len(PAYLOAD),
                        sha1=PAYLOAD_SHA1,
                    )
                ]

            def download_headers(self):
                return {}

        calls: list = []
        with mock.patch.object(
            modpack, "build_provider", return_value=FakeProvider()
        ), mock.patch.object(
            modpack.net, "download_file", side_effect=self._fake_download(calls)
        ):
            self.installer.install(archive, version_id="cf1")

        game_dir = self.paths.version_dir("cf1")
        self.assertTrue((game_dir / "mods" / "bar.jar").is_file())
        self.assertTrue((game_dir / "config" / "foo.cfg").is_file())
        # 走 mcimirror 后不再需要 x-api-key。
        self.assertEqual(calls[0][2], {})

    def test_cancel_raises(self) -> None:
        archive = _write_zip(
            self.root / "pack.zip",
            {"manifest.json": json.dumps(_cf_manifest())},
        )
        with self.assertRaises(ModpackError) as ctx:
            self.installer.install(archive, version_id="cf", cancel=lambda: True)
        self.assertIn("已取消", str(ctx.exception))


class ModpackExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = GamePaths(self.root)
        self.installer = ModpackInstaller(self.paths)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_export_mrpack(self) -> None:
        mods_dir = self.paths.version_dir("vid") / "mods"
        mods_dir.mkdir(parents=True)
        (mods_dir / "a.jar").write_bytes(b"A")
        (mods_dir / "b.jar.disabled").write_bytes(b"B")  # 禁用状态，不应导出
        (mods_dir / "notes.txt").write_text("x")

        dest = self.root / "out.mrpack"
        result = self.installer.export_mrpack(
            version_id="vid",
            dest_zip=dest,
            name="Exported",
            pack_version="2.0",
            game_version="1.21",
            loader="fabric",
            loader_version="0.16.0",
        )

        self.assertEqual(result, dest)
        with zipfile.ZipFile(dest) as zf:
            names = set(zf.namelist())
            index = json.loads(zf.read("modrinth.index.json").decode("utf-8"))

        self.assertIn("modrinth.index.json", names)
        self.assertIn("overrides/mods/a.jar", names)
        self.assertNotIn("overrides/mods/b.jar.disabled", names)
        self.assertNotIn("overrides/mods/notes.txt", names)

        self.assertEqual(index["files"], [])
        self.assertEqual(index["name"], "Exported")
        self.assertEqual(index["versionId"], "2.0")
        self.assertEqual(
            index["dependencies"],
            {"minecraft": "1.21", "fabric-loader": "0.16.0"},
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)