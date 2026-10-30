"""LoaderInstaller 的离线单元测试（打桩 net 层与 subprocess，不访问网络）。

    python -m unittest tests.test_loader -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import loader_installer
from engine.loader_installer import (
    LoaderError,
    LoaderInstaller,
    LoaderVersion,
    neoforge_version_matches,
    parse_forge_promotions,
    parse_maven_metadata,
)
from engine.models import GamePaths

# fabric meta 的版本列表结构（保留关键字段）。
FABRIC_LOADERS = [
    {
        "loader": {"version": "0.15.11", "stable": True},
        "intermediary": {"version": "1.20.1"},
        "launcherMeta": {"version": 1},
    },
    {
        "loader": {"version": "0.16.0-beta.1", "stable": False},
        "intermediary": {"version": "1.20.1"},
        "launcherMeta": {"version": 1},
    },
]

FABRIC_PROFILE = {
    "id": "fabric-loader-0.15.11-1.20.1",
    "inheritsFrom": "1.20.1",
    "mainClass": "net.fabricmc.loader.impl.launch.knot.KnotClient",
    "libraries": [
        {"name": "net.fabricmc:fabric-loader:0.15.11"},
    ],
}

FORGE_PROMOTIONS = {
    "promos": {
        "1.20.1-latest": "47.2.20",
        "1.20.1-recommended": "47.2.20",  # 与 latest 相同，应去重
        "1.20.1-1.20.2": "47.2.0",        # 非 latest/recommended 也属于该版本
        "1.19.2-latest": "43.2.0",        # 不同游戏版本，应被排除
    }
}

NEOFORGE_METADATA = b"""<?xml version="1.0" encoding="UTF-8"?>
<metadata>
  <groupId>net.neoforged</groupId>
  <artifactId>neoforge</artifactId>
  <versioning>
    <latest>21.1.66</latest>
    <release>21.1.66</release>
    <versions>
      <version>20.4.237</version>
      <version>21.1.66</version>
      <version>21.1.67</version>
      <version>21.0.5</version>
    </versions>
  </versioning>
</metadata>
"""


class NeoForgeMatchTest(unittest.TestCase):
    """NeoForge 版本前缀匹配（纯函数）。"""

    def test_matches_same_minor_patch(self) -> None:
        self.assertTrue(neoforge_version_matches("1.20.4", "20.4.237"))
        self.assertTrue(neoforge_version_matches("1.20.4", "20.4.100"))
        self.assertTrue(neoforge_version_matches("1.21.1", "21.1.66"))

    def test_rejects_other_versions(self) -> None:
        self.assertFalse(neoforge_version_matches("1.20.4", "20.5.1"))
        self.assertFalse(neoforge_version_matches("1.20.4", "21.1.66"))
        self.assertFalse(neoforge_version_matches("1.21.1", "21.10.5"))
        # 无法确定前缀时一律不匹配。
        self.assertFalse(neoforge_version_matches("1.20", "20.4.237"))
        self.assertFalse(neoforge_version_matches("", "20.4.237"))


class MavenMetadataTest(unittest.TestCase):
    """maven-metadata.xml 解析。"""

    def test_parse_versions(self) -> None:
        versions = parse_maven_metadata(NEOFORGE_METADATA)
        self.assertEqual(versions, ["20.4.237", "21.1.66", "21.1.67", "21.0.5"])

    def test_parse_accepts_str(self) -> None:
        versions = parse_maven_metadata(NEOFORGE_METADATA.decode("utf-8"))
        self.assertIn("21.1.66", versions)

    def test_garbage_returns_empty(self) -> None:
        self.assertEqual(parse_maven_metadata("<not-xml"), [])
        self.assertEqual(parse_maven_metadata("<metadata></metadata>"), [])


class ForgePromotionsTest(unittest.TestCase):
    """Forge promotions_slim.json 解析（含 latest / recommended 去重）。"""

    def test_parse_dedupes_and_filters(self) -> None:
        versions = parse_forge_promotions(FORGE_PROMOTIONS, "1.20.1")
        self.assertEqual(versions, ["47.2.20", "47.2.0"])

    def test_empty_returns_empty(self) -> None:
        self.assertEqual(parse_forge_promotions({}, "1.20.1"), [])


class LoaderInstallerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = GamePaths(self.root)
        self.installer = LoaderInstaller(self.paths)

    def tearDown(self) -> None:
        self.tmp.cleanup()


class ListVersionsTest(LoaderInstallerTestCase):
    def test_fabric_parses_mock_json(self) -> None:
        url = f"{loader_installer.FABRIC_META}/v2/versions/loader/1.20.1"
        with mock.patch.object(
            loader_installer.net, "get_json", return_value=FABRIC_LOADERS
        ) as called:
            versions = self.installer.list_versions("fabric", "1.20.1")

        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[0], LoaderVersion("fabric", "0.15.11", "1.20.1", True))
        self.assertEqual(versions[1].version, "0.16.0-beta.1")
        self.assertFalse(versions[1].stable)
        self.assertEqual(called.call_args.args[0], url)

    def test_quilt_parses_mock_json(self) -> None:
        payload = [{"loader": {"version": "0.24.0", "stable": True}}]
        with mock.patch.object(loader_installer.net, "get_json", return_value=payload):
            versions = self.installer.list_versions("quilt", "1.20.1")
        self.assertEqual(versions, [LoaderVersion("quilt", "0.24.0", "1.20.1", True)])

    def test_forge_from_promotions(self) -> None:
        with mock.patch.object(
            loader_installer.net, "get_json", return_value=FORGE_PROMOTIONS
        ):
            versions = self.installer.list_versions("forge", "1.20.1")
        self.assertEqual([v.version for v in versions], ["47.2.20", "47.2.0"])
        self.assertTrue(all(v.stable for v in versions))

    def test_neoforge_filters_by_game_version(self) -> None:
        with mock.patch.object(
            loader_installer.net, "fetch_bytes", return_value=NEOFORGE_METADATA
        ):
            versions = self.installer.list_versions("neoforge", "1.21.1")
        self.assertEqual([v.version for v in versions], ["21.1.66", "21.1.67"])

    def test_neoforge_unknown_game_version_empty(self) -> None:
        with mock.patch.object(
            loader_installer.net, "fetch_bytes", return_value=NEOFORGE_METADATA
        ):
            versions = self.installer.list_versions("neoforge", "1.20")
        self.assertEqual(versions, [])

    def test_unsupported_loader_raises(self) -> None:
        with self.assertRaises(LoaderError):
            self.installer.list_versions("rift", "1.20.1")


class FabricInstallTest(LoaderInstallerTestCase):
    def _seed_parent_jar(self, game_version: str = "1.20.1") -> Path:
        version_dir = self.paths.version_dir(game_version)
        version_dir.mkdir(parents=True, exist_ok=True)
        jar = self.paths.version_jar(game_version)
        jar.write_bytes(b"fake-client-jar")
        return jar

    def test_install_writes_profile_and_hardlinks_jar(self) -> None:
        parent_jar = self._seed_parent_jar()
        with mock.patch.object(
            loader_installer.net, "get_json", return_value=FABRIC_PROFILE
        ):
            result = self.installer.install("fabric", "1.20.1", "0.15.11")

        self.assertEqual(result.version_id, "fabric-loader-0.15.11-1.20.1")
        self.assertEqual(result.loader, "fabric")
        self.assertEqual(result.loader_version, "0.15.11")
        self.assertEqual(result.game_version, "1.20.1")

        profile_path = self.paths.version_json("fabric-loader-0.15.11-1.20.1")
        self.assertEqual(result.profile_path, profile_path)
        self.assertTrue(profile_path.exists())

        written = json.loads(profile_path.read_text(encoding="utf-8"))
        self.assertEqual(written["inheritsFrom"], "1.20.1")
        self.assertEqual(written["id"], "fabric-loader-0.15.11-1.20.1")

        # 父 jar 被硬链接到加载器版本目录（同一 inode）。
        target_jar = self.paths.version_jar("fabric-loader-0.15.11-1.20.1")
        self.assertTrue(target_jar.exists())
        self.assertTrue(os.path.samefile(str(parent_jar), str(target_jar)))

    def test_install_falls_back_to_constructed_id(self) -> None:
        profile = {k: v for k, v in FABRIC_PROFILE.items() if k != "id"}
        profile["id"] = ""
        with mock.patch.object(loader_installer.net, "get_json", return_value=profile):
            result = self.installer.install("fabric", "1.20.1", "0.15.11")
        self.assertEqual(result.version_id, "fabric-loader-0.15.11-1.20.1")
        self.assertTrue(result.profile_path.exists())

    def test_install_without_parent_jar(self) -> None:
        with mock.patch.object(
            loader_installer.net, "get_json", return_value=FABRIC_PROFILE
        ):
            result = self.installer.install("fabric", "1.20.1", "0.15.11")
        self.assertTrue(result.profile_path.exists())
        # 父 jar 不存在时不硬链接，交给解析阶段下载。
        self.assertFalse(self.paths.version_jar(result.version_id).exists())

    def test_progress_and_log_callbacks(self) -> None:
        stages: list[str] = []
        lines: list[str] = []

        def progress(stage: str, done: int, total: int) -> None:
            stages.append(stage)

        with mock.patch.object(
            loader_installer.net, "get_json", return_value=FABRIC_PROFILE
        ):
            self.installer.install(
                "fabric", "1.20.1", "0.15.11", progress=progress, log=lines.append
            )
        self.assertIn("profile", stages)
        self.assertTrue(lines)

    def test_cancel_raises(self) -> None:
        with self.assertRaises(LoaderError):
            self.installer.install("fabric", "1.20.1", "0.15.11", cancel=lambda: True)


class InstalledTest(LoaderInstallerTestCase):
    def test_none_when_absent(self) -> None:
        self.assertIsNone(self.installer.installed("fabric", "1.20.1", "0.15.11"))

    def test_detects_existing_install(self) -> None:
        version_id = "fabric-loader-0.15.11-1.20.1"
        version_dir = self.paths.version_dir(version_id)
        version_dir.mkdir(parents=True, exist_ok=True)
        self.paths.version_json(version_id).write_text("{}", encoding="utf-8")

        result = self.installer.installed("fabric", "1.20.1", "0.15.11")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.version_id, version_id)
        self.assertEqual(result.profile_path, self.paths.version_json(version_id))

    def test_install_is_idempotent(self) -> None:
        version_id = "fabric-loader-0.15.11-1.20.1"
        version_dir = self.paths.version_dir(version_id)
        version_dir.mkdir(parents=True, exist_ok=True)
        self.paths.version_json(version_id).write_text(
            json.dumps(FABRIC_PROFILE), encoding="utf-8"
        )

        with mock.patch.object(loader_installer.net, "get_json") as get_json:
            result = self.installer.install("fabric", "1.20.1", "0.15.11")

        get_json.assert_not_called()  # 已安装则不再联网
        self.assertEqual(result.version_id, version_id)


class ForgeInstallTest(LoaderInstallerTestCase):
    def test_install_runs_installer_and_detects_dir(self) -> None:
        version_id = "1.20.1-forge-47.2.0"

        def fake_run(args, **kwargs):
            # 模拟安装器在 versions/ 下生成版本目录。
            target = self.paths.version_dir(version_id)
            target.mkdir(parents=True, exist_ok=True)
            self.paths.version_json(version_id).write_text("{}", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, stdout="install ok\n", stderr="")

        lines: list[str] = []
        with mock.patch.object(loader_installer.net, "download_file") as download, \
                mock.patch.object(loader_installer.subprocess, "run", side_effect=fake_run):
            result = self.installer.install(
                "forge", "1.20.1", "47.2.0", log=lines.append
            )

        download.assert_called_once()
        self.assertEqual(result.version_id, version_id)
        self.assertEqual(result.loader, "forge")
        self.assertTrue(result.profile_path.exists())
        self.assertIn("install ok", lines)

    def test_install_falls_back_to_scan(self) -> None:
        # 安装器生成的目录名与精确约定名不同时，应扫描出最匹配者。
        odd_name = "1.20.1-forge-47.2.0-custom"

        def fake_run(args, **kwargs):
            target = self.paths.version_dir(odd_name)
            target.mkdir(parents=True, exist_ok=True)
            self.paths.version_json(odd_name).write_text("{}", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with mock.patch.object(loader_installer.net, "download_file"), \
                mock.patch.object(loader_installer.subprocess, "run", side_effect=fake_run):
            result = self.installer.install("forge", "1.20.1", "47.2.0")

        self.assertEqual(result.version_id, odd_name)

    def test_installer_missing_dir_raises(self) -> None:
        def fake_run(args, **kwargs):
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="boom")

        with mock.patch.object(loader_installer.net, "download_file"), \
                mock.patch.object(loader_installer.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(LoaderError):
                self.installer.install("forge", "1.20.1", "47.2.0")


class NeoForgeInstallTest(LoaderInstallerTestCase):
    def test_install_detects_neoforge_dir(self) -> None:
        version_id = "neoforge-21.1.66"

        def fake_run(args, **kwargs):
            target = self.paths.version_dir(version_id)
            target.mkdir(parents=True, exist_ok=True)
            self.paths.version_json(version_id).write_text("{}", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with mock.patch.object(loader_installer.net, "download_file") as download, \
                mock.patch.object(loader_installer.subprocess, "run", side_effect=fake_run):
            result = self.installer.install("neoforge", "1.21.1", "21.1.66")

        download.assert_called_once()
        self.assertEqual(result.version_id, version_id)
        self.assertEqual(result.loader, "neoforge")


if __name__ == "__main__":
    unittest.main(verbosity=2)