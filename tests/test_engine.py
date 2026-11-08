"""引擎层单元测试（离线，不访问网络）。

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import Account, Downloader, GamePaths, LaunchBuilder, VersionResolver
from engine.models import FileEntry
from engine.rules import rules_allow

VERSION_JSON = {
    "id": "1.20.1",
    "type": "release",
    "mainClass": "net.minecraft.client.main.Main",
    "javaVersion": {"majorVersion": 17},
    "downloads": {
        "client": {
            "url": "https://example.invalid/client.jar",
            "sha1": "c" * 40,
            "size": 100,
        }
    },
    "assetIndex": {
        "id": "5",
        "url": "https://example.invalid/5.json",
        "sha1": "a" * 40,
        "size": 10,
    },
    "logging": {
        "client": {
            "argument": "-Dlog4j.configurationFile=${path}",
            "file": {
                "id": "client-1.12.xml",
                "url": "https://example.invalid/log.xml",
                "sha1": "d" * 40,
                "size": 5,
            },
            "type": "log4j2-xml",
        }
    },
    "libraries": [
        {
            "name": "com.example:plain:1.0",
            "downloads": {
                "artifact": {
                    "path": "com/example/plain/1.0/plain-1.0.jar",
                    "url": "https://example.invalid/plain.jar",
                    "sha1": "e" * 40,
                    "size": 20,
                }
            },
        },
        {
            "name": "com.example:old-native:1.0",
            "downloads": {
                "artifact": {
                    "path": "com/example/old-native/1.0/old-native-1.0.jar",
                    "url": "https://example.invalid/old.jar",
                    "sha1": "f" * 40,
                    "size": 30,
                },
                "classifiers": {
                    "natives-linux": {
                        "path": "com/example/old-native/1.0/old-native-1.0-natives-linux.jar",
                        "url": "https://example.invalid/old-natives.jar",
                        "sha1": "1" * 40,
                        "size": 40,
                    }
                },
            },
            "natives": {"linux": "natives-linux", "windows": "natives-windows"},
        },
        {
            "name": "org.lwjgl:lwjgl:3.3.1:natives-linux",
            "downloads": {
                "artifact": {
                    "path": "org/lwjgl/lwjgl/3.3.1/lwjgl-3.3.1-natives-linux.jar",
                    "url": "https://example.invalid/lwjgl-natives.jar",
                    "sha1": "2" * 40,
                    "size": 50,
                }
            },
            "rules": [{"action": "allow", "os": {"name": "linux"}}],
        },
        {
            "name": "com.example:windows-only:1.0",
            "downloads": {
                "artifact": {
                    "path": "com/example/windows-only/1.0/windows-only-1.0.jar",
                    "url": "https://example.invalid/win.jar",
                    "sha1": "3" * 40,
                    "size": 60,
                }
            },
            "rules": [{"action": "allow", "os": {"name": "windows"}}],
        },
    ],
    "arguments": {
        "jvm": [
            {"rules": [{"action": "allow", "os": {"name": "osx"}}], "value": "-XstartOnFirstThread"},
            "-Djava.library.path=${natives_directory}",
            "-cp",
            "${classpath}",
        ],
        "game": [
            "--username",
            "${auth_player_name}",
            "--uuid",
            "${auth_uuid}",
            "--accessToken",
            "${auth_access_token}",
            "--gameDir",
            "${game_directory}",
            {"rules": [{"action": "allow", "features": {"has_custom_resolution": True}}],
             "value": ["--width", "${resolution_width}"]},
            {"rules": [{"action": "allow", "features": {"is_demo_user": True}}], "value": "--demo"},
        ],
    },
}


class RulesTest(unittest.TestCase):
    def test_empty_rules_allow(self):
        self.assertTrue(rules_allow(None, "linux", "x64"))

    def test_os_filter(self):
        rules = [{"action": "allow", "os": {"name": "windows"}}]
        self.assertTrue(rules_allow(rules, "windows", "x64"))
        self.assertFalse(rules_allow(rules, "linux", "x64"))

    def test_feature_filter(self):
        rules = [{"action": "allow", "features": {"has_custom_resolution": True}}]
        self.assertFalse(rules_allow(rules, "linux", "x64", {}))
        self.assertTrue(rules_allow(rules, "linux", "x64", {"has_custom_resolution": True}))

    def test_disallow_wins_last(self):
        rules = [
            {"action": "allow"},
            {"action": "disallow", "os": {"name": "linux"}},
        ]
        self.assertFalse(rules_allow(rules, "linux", "x64"))
        self.assertTrue(rules_allow(rules, "windows", "x64"))


class ResolverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = GamePaths(Path(self.tmp.name))
        self.resolver = VersionResolver(self.paths, os_name="linux", os_arch="x64")

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_produces_flat_manifest(self):
        resolved = self.resolver._parse("1.20.1", VERSION_JSON, False, None)

        kinds = [f.kind for f in resolved.files]
        self.assertIn("client", kinds)
        self.assertIn("library", kinds)
        self.assertIn("native", kinds)
        self.assertIn("asset_index", kinds)
        self.assertIn("log_config", kinds)

        # windows-only 库应被剔除，lwjgl natives 保留为原生库
        names = [lib.name for lib in resolved.libraries]
        self.assertNotIn("com.example:windows-only:1.0", names)
        self.assertIn("org.lwjgl:lwjgl:3.3.1:natives-linux", names)

    def test_classpath_excludes_natives_and_ends_with_client(self):
        resolved = self.resolver._parse("1.20.1", VERSION_JSON, False, None)
        classpath = [str(p) for p in resolved.classpath]

        self.assertTrue(classpath[-1].endswith("1.20.1.jar"))
        self.assertTrue(any(p.endswith("plain-1.0.jar") for p in classpath))
        # 老式原生库的基础 jar 进 classpath，classifier jar 不进
        self.assertTrue(any(p.endswith("old-native-1.0.jar") for p in classpath))
        self.assertFalse(any("natives" in p for p in classpath))

        # 原生库标记：老式 + 现代各 1 个
        natives = [lib for lib in resolved.libraries if lib.native_jar]
        self.assertEqual(len(natives), 2)

    def test_os_specific_jvm_arg(self):
        builder = LaunchBuilder(self.paths)
        account = Account.offline_account("Steve")

        linux = self.resolver._parse("1.20.1", VERSION_JSON, False, None)
        linux_cmd = builder.build(linux, account, prepare_natives=False)
        self.assertNotIn("-XstartOnFirstThread", linux_cmd)

        osx = VersionResolver(self.paths, os_name="osx", os_arch="arm64")
        mac = osx._parse("1.20.1", VERSION_JSON, False, None)
        mac_cmd = builder.build(mac, account, prepare_natives=False)
        self.assertIn("-XstartOnFirstThread", mac_cmd)

    def test_java_major_parsed(self):
        resolved = self.resolver._parse("1.20.1", VERSION_JSON, False, None)
        self.assertEqual(resolved.java_major, 17)


class LaunchBuilderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = GamePaths(Path(self.tmp.name))
        self.resolver = VersionResolver(self.paths, os_name="linux", os_arch="x64")
        self.resolved = self.resolver._parse("1.20.1", VERSION_JSON, False, None)
        self.builder = LaunchBuilder(self.paths)

    def tearDown(self):
        self.tmp.cleanup()

    def test_offline_uuid_is_stable_v3(self):
        from engine import offline_uuid

        first = offline_uuid("Steve")
        self.assertEqual(first, offline_uuid("Steve"))
        self.assertEqual(first[14], "3")  # version nibble
        self.assertIn(first[19], "89ab")  # variant

    def test_command_layout(self):
        account = Account.offline_account("Steve")
        command = self.builder.build(self.resolved, account, prepare_natives=False)

        self.assertEqual(command[0], "java")
        self.assertIn("-Djava.library.path=" + str(self.resolved.natives_dir), command)

        main_index = command.index("net.minecraft.client.main.Main")
        cp_index = command.index("-cp")
        self.assertLess(cp_index, main_index)  # -cp 在主类前
        self.assertLess(cp_index, main_index)

        classpath = command[cp_index + 1]
        self.assertIn(os.pathsep, classpath)
        self.assertTrue(classpath.endswith("1.20.1.jar"))

        self.assertIn("--username", command)
        self.assertEqual(command[command.index("--username") + 1], "Steve")

    def test_placeholders_substituted(self):
        account = Account.offline_account("Alex")
        command = self.builder.build(self.resolved, account, prepare_natives=False)
        joined = " ".join(command)

        self.assertNotIn("${", joined)
        self.assertIn(account.uuid, joined)
        self.assertIn(str(self.paths.root), joined)
        self.assertTrue(account.access_token)  # 离线 token 非空占位

    def test_custom_resolution_feature_gate(self):
        account = Account.offline_account("Steve")
        without = self.builder.build(self.resolved, account, prepare_natives=False)
        self.assertNotIn("--width", without)

        with_res = self.builder.build(
            self.resolved, account, width=1280, height=720, prepare_natives=False
        )
        self.assertIn("--width", with_res)
        self.assertEqual(with_res[with_res.index("--width") + 1], "1280")

    def test_memory_flag(self):
        account = Account.offline_account("Steve")
        command = self.builder.build(
            self.resolved, account, memory_mb=4096, prepare_natives=False
        )
        self.assertIn("-Xmx4096M", command)

    def test_log_config_arg(self):
        account = Account.offline_account("Steve")
        command = self.builder.build(self.resolved, account, prepare_natives=False)
        log_arg = [a for a in command if a.startswith("-Dlog4j.configurationFile=")]
        self.assertEqual(len(log_arg), 1)
        self.assertTrue(log_arg[0].endswith("client-1.12.xml"))

    def test_legacy_minecraft_arguments(self):
        legacy_json = {
            "id": "1.7.10",
            "type": "release",
            "mainClass": "net.minecraft.client.Main",
            "downloads": {"client": {"url": "https://example.invalid/c.jar", "sha1": "c" * 40, "size": 1}},
            "libraries": [],
            "minecraftArguments": (
                "--username ${auth_player_name} --uuid ${auth_uuid} "
                "--accessToken ${auth_access_token} --gameDir ${game_directory}"
            ),
        }
        resolved = self.resolver._parse("1.7.10", legacy_json, False, None)
        command = self.builder.build(
            resolved, Account.offline_account("Steve"), prepare_natives=False
        )

        self.assertIn("-Djava.library.path=" + str(resolved.natives_dir), command)
        cp_index = command.index("-cp")
        main_index = command.index("net.minecraft.client.Main")
        self.assertLess(cp_index, main_index)  # 手工补的 -cp 也在主类前
        self.assertEqual(command[command.index("--username") + 1], "Steve")


class DownloaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "source.bin"
        self.payload = b"minecraft" * 5000
        self.source.write_bytes(self.payload)
        self.sha1 = hashlib.sha1(self.payload).hexdigest()

    def tearDown(self):
        self.tmp.cleanup()

    def _entry(self, dest: Path, sha1: str | None = None) -> FileEntry:
        return FileEntry(
            url=self.source.as_uri(),
            target_path=dest,
            sha1=sha1 or self.sha1,
            size=len(self.payload),
            kind="asset",
        )

    def test_download_and_verify(self):
        dest = self.root / "out" / "file.bin"
        stats = Downloader(workers=2).download([self._entry(dest)])

        self.assertTrue(stats.ok, stats.failed)
        self.assertEqual(dest.read_bytes(), self.payload)
        self.assertEqual(stats.files_done, 1)
        self.assertEqual(stats.files_skipped, 0)

    def test_second_run_skips(self):
        dest = self.root / "out" / "file.bin"
        Downloader(workers=2).download([self._entry(dest)])
        stats = Downloader(workers=2).download([self._entry(dest)])

        self.assertEqual(stats.files_skipped, 1)
        self.assertEqual(stats.files_downloaded, 0)
        self.assertEqual(stats.bytes_done, len(self.payload))

    def test_duplicate_entries_counted_once(self):
        """同一目标文件重复声明时，文件总数与实际下载数都不应重复计算。"""
        dest = self.root / "out" / "file.bin"
        entry = self._entry(dest)

        stats = Downloader(workers=2).download([entry, entry])

        self.assertTrue(stats.ok, stats.failed)
        self.assertEqual(stats.files_total, 1)
        self.assertEqual(stats.files_done, 1)
        self.assertEqual(stats.files_downloaded, 1)
        self.assertEqual(stats.files_skipped, 0)

    def test_corrupt_file_is_redownloaded(self):
        dest = self.root / "out" / "file.bin"
        Downloader(workers=2).download([self._entry(dest)])
        dest.write_bytes(b"corrupted")

        stats = Downloader(workers=2).download([self._entry(dest)])
        self.assertEqual(stats.files_skipped, 0)
        self.assertEqual(dest.read_bytes(), self.payload)

    def test_sha1_mismatch_reports_failure(self):
        dest = self.root / "out" / "file.bin"
        stats = Downloader(workers=2).download([self._entry(dest, sha1="0" * 40)])

        self.assertFalse(stats.ok)
        self.assertFalse(dest.exists())
        self.assertEqual(len(stats.failed), 1)

    def test_progress_events_emitted(self):
        events = []
        dest = self.root / "out" / "file.bin"
        source = self.root / "big.bin"
        source.write_bytes(b"x" * (4 << 20))
        entry = FileEntry(
            url=source.as_uri(),
            target_path=dest,
            sha1=hashlib.sha1(source.read_bytes()).hexdigest(),
            size=source.stat().st_size,
            kind="asset",
        )
        Downloader(workers=1, progress=events.append).download([entry])

        self.assertTrue(events)
        self.assertEqual(events[-1]["current"], "done")
        self.assertEqual(events[-1]["percent"], 100.0)

    def test_cancel_stops_download(self):
        from engine.downloader import DownloadCancelled

        dest = self.root / "out" / "file.bin"
        with self.assertRaises(DownloadCancelled):
            Downloader(workers=1).download([self._entry(dest)], cancel=lambda: True)


class MirrorCandidateTest(unittest.TestCase):
    """GitHub Release 资源优先走公共代理（Adoptium 等直连经常卡住）。"""

    def test_github_url_gets_proxy_first(self) -> None:
        from engine.mirror import MirrorManager

        manager = MirrorManager()
        url = "https://github.com/adoptium/temurin25-binaries/releases/download/x/y.zip"
        candidates = manager.candidates(url)
        self.assertEqual(candidates[0], f"https://v6.gh-proxy.org/{url}")
        self.assertIn(url, candidates)

    def test_non_github_url_untouched(self) -> None:
        from engine.mirror import MirrorManager

        manager = MirrorManager()
        self.assertEqual(manager.candidates("https://example.com/a.zip"), ["https://example.com/a.zip"])


class NativesExtractionTest(unittest.TestCase):
    """26.x 原生包按 <平台>/<架构>/ 分目录，需要扁平化进 natives/java。"""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.natives = self.root / "natives"

    def _make_jar(self, name: str, entries: dict[str, bytes]) -> Path:
        import zipfile

        path = self.root / name
        with zipfile.ZipFile(path, "w") as archive:
            for member, data in entries.items():
                archive.writestr(member, data)
        return path

    def _resolved(self, jars: list[Path], jvm_args: list[str]):
        from types import SimpleNamespace

        libraries = [
            SimpleNamespace(
                native_jar=SimpleNamespace(target_path=str(path)),
                exclude=("META-INF/",),
            )
            for path in jars
        ]
        return SimpleNamespace(
            natives_dir=self.natives, jvm_args=jvm_args, libraries=libraries
        )

    def test_legacy_flat_natives(self) -> None:
        from engine.launch_builder import LaunchBuilder

        jar = self._make_jar("legacy.jar", {"lwjgl.dll": b"x"})
        resolved = self._resolved([jar], ["-Djava.library.path=${natives_directory}"])
        LaunchBuilder(GamePaths(self.root)).extract_natives(resolved)
        self.assertTrue((self.natives / "lwjgl.dll").is_file())

    def test_new_layout_flattens_and_skips_other_arch(self) -> None:
        from unittest import mock

        from engine import launch_builder
        from engine.launch_builder import LaunchBuilder

        x64 = self._make_jar("x64.jar", {"windows/x64/org/lwjgl/lwjgl.dll": b"x64"})
        arm = self._make_jar("arm.jar", {"windows/arm64/org/lwjgl/lwjgl.dll": b"arm"})
        resolved = self._resolved(
            [x64, arm], ["-Djava.library.path=${natives_directory}/java"]
        )
        with mock.patch.object(
            launch_builder, "detect_os_name", return_value="windows"
        ), mock.patch.object(launch_builder, "detect_os_arch", return_value="x64"):
            LaunchBuilder(GamePaths(self.root)).extract_natives(resolved)

        target = self.natives / "java" / "lwjgl.dll"
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_bytes(), b"x64")
        self.assertFalse((self.natives / "windows").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)