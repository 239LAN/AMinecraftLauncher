"""JavaManager 的离线单元测试（打桩网络层，不访问真实 API）。

    python -m unittest tests.test_java -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import java_manager
from engine.java_manager import (
    JavaError,
    JavaInfo,
    JavaManager,
    JavaRuntime,
    adoptium_arch,
    adoptium_mirror_candidates,
    adoptium_os_name,
    parse_java_version_output,
)
from engine.models import GamePaths

# 一段真实的 `-XshowSettings:properties -version` 输出样式。
JAVA17_OUTPUT = """\
Property settings:
    java.home = /usr/lib/jvm/temurin-17-jre
    java.vendor = Eclipse Adoptium
    java.version = 17.0.9
    os.arch = amd64

openjdk version "17.0.9" 2023-10-17
OpenJDK Runtime Environment Temurin-17.0.9+9 (build 17.0.9+9)
OpenJDK 64-Bit Server VM Temurin-17.0.9+9 (build 17.0.9+9, mixed mode, sharing)
"""

JAVA8_OUTPUT = """\
Property settings:
    java.version = 1.8.0_292
    java.vendor = Oracle Corporation
    os.arch = x86_64

java version "1.8.0_292"
"""

# 仅 banner、无 properties 的情况，应回退到引号内的版本。
JAVA21_BANNER_ONLY = """\
openjdk version "21.0.1" 2023-10-17
OpenJDK Runtime Environment Temurin-21.0.1+12 (build 21.0.1+12)
"""

ADOPTIUM_PAYLOAD = [
    {
        "binary": {
            "package": {
                "link": "https://example.invalid/OpenJDK17U-jre_x64_linux.tar.gz",
                "size": 4242,
                "checksum": "a" * 40,
            }
        },
        "version": {"semver": "17.0.9+9", "major": 17},
    }
]


class ParseJavaVersionTest(unittest.TestCase):
    def test_parse_modern(self) -> None:
        info = parse_java_version_output(JAVA17_OUTPUT, "/opt/java/bin/java")
        self.assertIsNotNone(info)
        self.assertEqual(info.path, "/opt/java/bin/java")
        self.assertEqual(info.version, "17.0.9")
        self.assertEqual(info.major, 17)
        self.assertEqual(info.vendor, "Eclipse Adoptium")
        self.assertEqual(info.arch, "x86_64")

    def test_parse_legacy_8(self) -> None:
        info = parse_java_version_output(JAVA8_OUTPUT, "java")
        self.assertIsNotNone(info)
        self.assertEqual(info.version, "1.8.0_292")
        self.assertEqual(info.major, 8)
        self.assertEqual(info.vendor, "Oracle Corporation")
        self.assertEqual(info.arch, "x86_64")

    def test_parse_banner_only_fallback(self) -> None:
        info = parse_java_version_output(JAVA21_BANNER_ONLY, "java")
        self.assertIsNotNone(info)
        self.assertEqual(info.major, 21)
        self.assertEqual(info.version, "21.0.1")
        # 无 java.vendor 时从 banner 猜出厂商。
        self.assertEqual(info.vendor, "Eclipse Adoptium")

    def test_parse_garbage_returns_none(self) -> None:
        self.assertIsNone(parse_java_version_output("", "java"))
        self.assertIsNone(parse_java_version_output("not a java output", "java"))


class AdoptiumMappingTest(unittest.TestCase):
    def test_os_mapping(self) -> None:
        self.assertEqual(adoptium_os_name("windows"), "windows")
        self.assertEqual(adoptium_os_name("linux"), "linux")
        self.assertEqual(adoptium_os_name("osx"), "mac")
        # 未知值原样返回。
        self.assertEqual(adoptium_os_name("plan9"), "plan9")

    def test_arch_mapping(self) -> None:
        self.assertEqual(adoptium_arch("x86_64"), "x64")
        self.assertEqual(adoptium_arch("arm64"), "aarch64")
        self.assertEqual(adoptium_arch("x86"), "x86")
        self.assertEqual(adoptium_arch("arm"), "arm")
        self.assertEqual(adoptium_arch("arm32"), "arm")


class JavaManagerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = GamePaths(self.root)
        self.manager = JavaManager(self.paths)

    def tearDown(self) -> None:
        self.tmp.cleanup()


class ListAvailableTest(JavaManagerTestCase):
    def test_parse_payload(self) -> None:
        with mock.patch.object(
            java_manager.net, "get_json", return_value=ADOPTIUM_PAYLOAD
        ) as called:
            runtimes = self.manager.list_available(17)

        self.assertEqual(len(runtimes), 1)
        runtime = runtimes[0]
        self.assertEqual(runtime.major, 17)
        self.assertEqual(runtime.version, "17.0.9+9")
        self.assertEqual(runtime.size, 4242)
        self.assertEqual(runtime.sha1, "a" * 40)
        self.assertEqual(runtime.vendor, "Eclipse Adoptium")
        self.assertTrue(runtime.url.endswith(".tar.gz"))

        url = called.call_args.args[0]
        self.assertIn("/17/hotspot", url)
        self.assertIn("image_type=jre", url)
        self.assertIn(f"os={adoptium_os_name(java_manager.detect_os_name())}", url)
        self.assertIn(f"arch={adoptium_arch(java_manager.detect_os_arch())}", url)

    def test_empty_payload_raises(self) -> None:
        with mock.patch.object(java_manager.net, "get_json", return_value=[]):
            with self.assertRaises(JavaError):
                self.manager.list_available(17)


class InstallTest(JavaManagerTestCase):
    def _make_existing_runtime(self, major: int) -> Path:
        """在 runtimes_dir 下造一个「已安装」的运行时目录。"""
        bin_dir = self.manager.runtimes_dir / f"temurin-{major}" / "bin"
        bin_dir.mkdir(parents=True)
        # 两种平台的可执行名都造出来，保证 java_bin.exists() 为真。
        (bin_dir / "java").write_text("#!/bin/sh\n")
        (bin_dir / "java.exe").write_text("MZ")
        return bin_dir.parent

    def test_reuse_when_present(self) -> None:
        target = self._make_existing_runtime(17)
        probe_result = JavaInfo(
            path=str(target / "bin" / "java"),
            version="17.0.9",
            major=17,
            vendor="Eclipse Adoptium",
            arch="x86_64",
            source="system",
        )
        with mock.patch.object(
            JavaManager, "probe", return_value=probe_result
        ) as probe, mock.patch.object(java_manager.net, "download_file") as download:
            info = self.manager.install(17)

        self.assertEqual(info.major, 17)
        self.assertEqual(info.source, "downloaded")
        download.assert_not_called()  # 复用时不下载
        self.assertTrue(probe.called)


class FindForTest(JavaManagerTestCase):
    def _scanned(self) -> list[JavaInfo]:
        return [
            JavaInfo(path="/a/bin/java", version="8.0.400", major=8, source="system"),
            JavaInfo(path="/b/bin/java", version="17.0.9", major=17, source="system"),
            JavaInfo(path="/c/bin/java", version="21.0.1", major=21, source="system"),
        ]

    def test_exact_match(self) -> None:
        with mock.patch.object(self.manager, "scan", return_value=self._scanned()):
            info = self.manager.find_for(17)
        self.assertIsNotNone(info)
        self.assertEqual(info.path, "/b/bin/java")

    def test_fallback_picks_largest_major(self) -> None:
        with mock.patch.object(self.manager, "scan", return_value=self._scanned()):
            info = self.manager.find_for(11)
        self.assertIsNotNone(info)
        self.assertEqual(info.major, 21)

    def test_extra_takes_priority(self) -> None:
        extra_info = JavaInfo(path="/extra/bin/java", version="17.0.5", major=17)
        scanned = self._scanned()
        with mock.patch.object(self.manager, "scan", return_value=scanned), mock.patch.object(
            self.manager, "probe", side_effect=lambda p: extra_info if p == extra_info.path else None
        ):
            info = self.manager.find_for(17, extra=[extra_info.path])
        self.assertIsNotNone(info)
        self.assertEqual(info.path, "/extra/bin/java")

    def test_returns_none_when_nothing_found(self) -> None:
        def _none(_path: str):
            return None

        with mock.patch.object(self.manager, "scan", return_value=[]), mock.patch.object(
            self.manager, "probe", side_effect=_none
        ):
            self.assertIsNone(self.manager.find_for(17, extra=["/nope/java"]))


class ScanDedupeTest(JavaManagerTestCase):
    def test_scan_keeps_highest_version_per_major(self) -> None:
        # 造两个同 major 的候选目录，scan 应只保留版本更高的那个。
        for name, version in (("temurin-17a", "17.0.1"), ("temurin-17b", "17.0.9")):
            bin_dir = self.manager.runtimes_dir / name / "bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "java").write_text("x")
            (bin_dir / "java.exe").write_text("x")

        def _probe(path: str):
            if "temurin-17a" in path:
                return JavaInfo(path=path, version="17.0.1", major=17)
            if "temurin-17b" in path:
                return JavaInfo(path=path, version="17.0.9", major=17)
            return None  # 其它系统候选一律视为无效

        with mock.patch.object(self.manager, "probe", side_effect=_probe):
            infos = self.manager.scan()

        majors = [info.major for info in infos]
        self.assertEqual(majors, [17])
        self.assertEqual(infos[0].version, "17.0.9")
        self.assertEqual(infos[0].source, "downloaded")


class AdoptiumMirrorTest(unittest.TestCase):
    def test_mirror_candidate_from_release_url(self) -> None:
        runtime = JavaRuntime(
            major=25,
            version="25.0.5",
            os="windows",
            arch="x64",
            url=(
                "https://github.com/adoptium/temurin25-binaries/releases/download/"
                "jdk-25.0.5%2B8/OpenJDK25U-jre_x64_windows_hotspot_25.0.5_8.zip"
            ),
            size=58589419,
        )
        self.assertEqual(
            adoptium_mirror_candidates(runtime),
            [
                "https://mirrors.tuna.tsinghua.edu.cn/Adoptium/25/jre/x64/windows/"
                "OpenJDK25U-jre_x64_windows_hotspot_25.0.5_8.zip"
            ],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)