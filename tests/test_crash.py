"""崩溃分析器离线单元测试。

    python -m unittest tests.test_crash -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.crash_analyzer import CrashAnalyzer, CrashReport

# 每条规则至少一个正样本
SAMPLES = {
    "out_of_memory": (
        'Exception in thread "main" java.lang.OutOfMemoryError: Java heap space\n'
        "JVM args: -Xmx2048M -Xms512M\n"
        "java.lang.OutOfMemoryError: Java heap space"
    ),
    "java_too_old": (
        "java.lang.UnsupportedClassVersionError: "
        "net/minecraft/client/main/Main has been compiled by a more recent version"
    ),
    "missing_library": (
        "Error: Could not find or load main class net.minecraft.client.main.Main"
    ),
    "opengl": "org.lwjgl.LWJGLException: Pixel format not accelerated",
    "mod_conflict": (
        "ModResolutionException: Duplicate mods: [sodium.jar, sodium-fabric.jar]"
    ),
    "forge_crash": (
        "net.minecraftforge.fml.loading.FMLLoader\n"
        "The game crashed whilst initializing game"
    ),
    "missing_assets": (
        "java.io.FileNotFoundException: .minecraft/assets/objects/ab/abcdef (No such file)"
    ),
    "permission": "java.nio.file.AccessDeniedException: ./saves/new_world",
    "native_crash": "Process crashed with exit code: -1073741819",
    "game_crash": "The game crashed whilst rendering overlay",
}


class RuleCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.analyzer = CrashAnalyzer()

    def test_each_rule_triggers(self) -> None:
        for expected_code, text in SAMPLES.items():
            with self.subTest(code=expected_code):
                report = self.analyzer.analyze(text)
                codes = [cause.code for cause in report.causes]
                self.assertIn(expected_code, codes)

    def test_out_of_memory_evidence_includes_xmx(self) -> None:
        report = self.analyzer.analyze(SAMPLES["out_of_memory"])
        cause = next(c for c in report.causes if c.code == "out_of_memory")
        self.assertTrue(any("-Xmx" in line for line in cause.evidence))

    def test_permission_suggestion_mentions_fix(self) -> None:
        report = self.analyzer.analyze(SAMPLES["permission"])
        cause = next(c for c in report.causes if c.code == "permission")
        self.assertIn("权限", cause.suggestion)
        self.assertEqual(cause.severity, "error")


class AnalyzeBehaviourTests(unittest.TestCase):
    def setUp(self) -> None:
        self.analyzer = CrashAnalyzer()

    def test_clean_log_has_no_cause(self) -> None:
        text = (
            "[12:00:00] [main/INFO]: Loading Minecraft 1.20.1 with Fabric Loader\n"
            "[12:00:01] [main/INFO]: Preparing spawn area\n"
            "[12:00:02] [Render thread/INFO]: Backend library: LWJGL 3.3.1\n"
            "[12:00:03] [main/INFO]: Saving and pausing game"
        )
        report = self.analyzer.analyze(text)
        self.assertEqual(report.causes, [])
        self.assertEqual(report.summary, "未发现已知的崩溃特征")

    def test_same_code_deduplicated_and_evidence_capped(self) -> None:
        text = "\n".join(
            ["java.lang.OutOfMemoryError: Java heap space"] * 5
        )
        report = self.analyzer.analyze(text)
        codes = [cause.code for cause in report.causes]
        self.assertEqual(codes.count("out_of_memory"), 1)
        cause = report.causes[0]
        self.assertLessEqual(len(cause.evidence), 3)

    def test_causes_sorted_by_appearance(self) -> None:
        # mod_conflict 先出现，out_of_memory 后出现，二者同为 error。
        text = (
            "ModResolutionException: Duplicate mods: [a.jar, b.jar]\n"
            "java.lang.OutOfMemoryError: Java heap space"
        )
        report = self.analyzer.analyze(text)
        self.assertEqual(report.causes[0].code, "mod_conflict")
        self.assertEqual(report.causes[1].code, "out_of_memory")

    def test_log_tail_keeps_last_40_lines(self) -> None:
        lines = [f"line {i}" for i in range(100)]
        report = self.analyzer.analyze("\n".join(lines))
        self.assertEqual(len(report.log_tail), 40)
        self.assertEqual(report.log_tail[-1], "line 99")
        self.assertEqual(report.raw, "\n".join(lines))

    def test_analyze_files_reads_and_ignores_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "latest.log"
            second = Path(tmp) / "crash.txt"
            first.write_text("The game crashed whilst rendering overlay", encoding="utf-8")
            second.write_text("java.lang.OutOfMemoryError: Java heap space", encoding="utf-8")
            missing = Path(tmp) / "nope.log"

            report = self.analyzer.analyze_files([first, missing, second])

        codes = {cause.code for cause in report.causes}
        self.assertIn("game_crash", codes)
        self.assertIn("out_of_memory", codes)


if __name__ == "__main__":
    unittest.main(verbosity=2)