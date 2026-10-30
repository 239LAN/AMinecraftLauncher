"""崩溃与日志诊断：从日志文本里匹配已知崩溃特征并给出中文修复建议。

设计为纯文本匹配，不依赖网络与游戏目录：给定日志（或崩溃报告）文本，
返回命中的 :class:`Cause` 列表与最后若干行原始文本（``log_tail``）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# log_tail 保留的原始行数
TAIL_LINES = 40
# 单条 cause 最多保留的原始日志行数
MAX_EVIDENCE = 3


@dataclass
class Cause:
    """一条崩溃原因（机器可读 code + 人类可读结论与建议）。"""

    code: str
    title: str
    suggestion: str
    severity: str = "error"  # "error" / "warning"
    evidence: list[str] = field(default_factory=list)


@dataclass
class CrashReport:
    """崩溃分析结果。"""

    causes: list[Cause]
    log_tail: list[str]
    raw: str = ""

    @property
    def summary(self) -> str:
        """一句话结论；未命中任何规则时返回固定文案。"""
        if not self.causes:
            return "未发现已知的崩溃特征"
        return "；".join(cause.title for cause in self.causes)


@dataclass
class _Rule:
    """内部规则：一组正则 + 可选的文本级 / 行级附加条件。"""

    code: str
    title: str
    suggestion: str
    patterns: tuple[str, ...]
    severity: str = "error"
    # 文本级附加条件：整段日志必须包含该子串规则才生效。
    guard: str = ""
    # 行级附加条件：命中行还必须包含其中之一（如 libraries / assets）。
    line_extra: tuple[str, ...] = ()
    # 是否尝试从日志中提取 -Xmx 值补充到 evidence。
    extract_xmx: bool = False
    compiled: tuple[re.Pattern[str], ...] = field(init=False, repr=False, default=())

    def __post_init__(self) -> None:
        self.compiled = tuple(re.compile(p, re.IGNORECASE) for p in self.patterns)


# 规则按文档顺序声明；最终输出排序由 severity 与出现顺序决定。
_RULES: tuple[_Rule, ...] = (
    _Rule(
        code="out_of_memory",
        title="Java 堆内存不足（OutOfMemoryError）",
        suggestion=(
            "调大最大堆内存（如把 -Xmx 提高到 4G 或更高），"
            "确认启动参数中的 -Xmx 没有设置过小；同时可关闭部分模组或降低渲染距离。"
        ),
        patterns=(r"java\.lang\.OutOfMemoryError",),
        extract_xmx=True,
    ),
    _Rule(
        code="java_too_old",
        title="Java 版本过低（UnsupportedClassVersionError）",
        suggestion=(
            "当前 Java 版本低于该游戏版本所需，请安装并切换到更高版本的 Java"
            "（1.17+ 需 Java 17，1.20.5+ 需 Java 21）。"
        ),
        patterns=(r"UnsupportedClassVersionError", r"class file version"),
    ),
    _Rule(
        code="missing_library",
        title="缺少依赖库或主类（NoClassDefFoundError 等）",
        suggestion=(
            "重新下载完整的版本文件，确认 libraries 目录完整且未被删除；"
            "若是整合包或模组环境，请检查前置依赖是否齐全。"
        ),
        patterns=(r"Could not find or load main class", r"NoClassDefFoundError"),
    ),
    _Rule(
        code="opengl",
        title="OpenGL / 显卡驱动问题",
        suggestion=(
            "更新显卡驱动到最新版本；关闭光影与高级画质特效；"
            "确认未强制使用软件渲染（如 SwiftShader）。"
        ),
        patterns=(
            r"org\.lwjgl\.LWJGLException",
            r"Failed to create window",
            r"GLFW error",
            r"Pixel format not accelerated",
        ),
    ),
    _Rule(
        code="mod_conflict",
        title="模组冲突或前置依赖缺失",
        suggestion=(
            "检查 mods 目录删除重复模组；按日志补齐缺失的前置依赖；"
            "把冲突或加载失败（Mixin）的模组更新到兼容版本。"
        ),
        patterns=(
            r"Duplicate mods:",
            r"ModResolutionException",
            r"Missing or unsupported mandatory dependencies",
            r"Mixin apply failed",
            r"MixinTransformerError",
        ),
    ),
    _Rule(
        code="forge_crash",
        title="Forge 加载阶段崩溃",
        suggestion=(
            "查看完整崩溃报告，通常是某个模组与当前 Forge 版本不兼容；"
            "尝试更新 Forge 与相关模组，或用二分法逐个排查模组。"
        ),
        patterns=(r"The game crashed whilst",),
        guard="net.minecraftforge",
    ),
    _Rule(
        code="missing_assets",
        title="游戏资源或库文件缺失",
        suggestion=(
            "重新下载对应的 assets / libraries 文件；"
            "确认磁盘空间充足，且杀毒软件没有误删游戏文件。"
        ),
        patterns=(r"Failed to download", r"Could not resolve", r"FileNotFoundException"),
        line_extra=("libraries", "assets"),
    ),
    _Rule(
        code="permission",
        title="文件权限不足",
        suggestion=(
            "以管理员或具备相应权限的账号运行启动器，"
            "或手动为游戏目录授予读写权限（Linux 下检查 chmod / chown）。"
        ),
        patterns=(r"AccessDeniedException", r"Permission denied"),
    ),
    _Rule(
        code="native_crash",
        title="原生代码崩溃（JVM 层）",
        suggestion=(
            "多为显卡驱动或本地库导致，请更新显卡驱动；"
            "若使用了性能优化模组（如 Sodium / Embeddium），确认其与当前版本匹配。"
        ),
        patterns=(r"exit code: -1073741819", r"SIGSEGV"),
    ),
    _Rule(
        code="game_crash",
        title="游戏崩溃（The game crashed whilst）",
        suggestion=(
            "查看崩溃报告顶部的描述与涉及的模组，逐项排查；"
            "必要时以最小模组集启动以定位问题。"
        ),
        patterns=(r"The game crashed whilst",),
    ),
)


class CrashAnalyzer:
    """崩溃日志分析器：正则匹配 + 去重 + 排序。"""

    def analyze(self, text: str) -> CrashReport:
        """分析日志文本，返回命中的原因列表。"""
        text = text or ""
        lines = text.splitlines()
        tail = lines[-TAIL_LINES:] if len(lines) > TAIL_LINES else list(lines)
        lower_text = text.lower()

        hits: list[tuple[str, int, int, Cause]] = []
        for order, rule in enumerate(_RULES):
            # 文本级附加条件不满足则整条规则跳过。
            if rule.guard and rule.guard.lower() not in lower_text:
                continue

            matched: list[str] = []
            first_pos: Optional[int] = None
            for index, line in enumerate(lines):
                if not _line_matches(rule, line):
                    continue
                if first_pos is None:
                    first_pos = index
                if len(matched) < MAX_EVIDENCE:
                    matched.append(line.strip())
            if first_pos is None:
                continue

            evidence = matched
            if rule.extract_xmx:
                xmx_lines = [line.strip() for line in lines if "-Xmx" in line]
                evidence = _dedupe([*matched, *xmx_lines])[:MAX_EVIDENCE]

            hits.append(
                (
                    rule.severity,
                    first_pos,
                    order,
                    Cause(
                        code=rule.code,
                        title=rule.title,
                        suggestion=rule.suggestion,
                        severity=rule.severity,
                        evidence=evidence,
                    ),
                )
            )

        # error 优先，其次按日志中出现顺序。
        hits.sort(key=lambda item: (0 if item[0] == "error" else 1, item[1], item[2]))
        causes = [item[3] for item in hits]
        return CrashReport(causes=causes, log_tail=tail, raw=text)

    def analyze_files(self, paths: list[Path]) -> CrashReport:
        """读取多个文件拼接后分析；不存在的文件忽略。"""
        chunks: list[str] = []
        for candidate in paths:
            path = Path(candidate)
            if not path.is_file():
                continue
            try:
                chunks.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
        return self.analyze("\n".join(chunks))


def _line_matches(rule: _Rule, line: str) -> bool:
    """判断单行是否命中规则（含行级附加条件）。"""
    if not any(pattern.search(line) for pattern in rule.compiled):
        return False
    if rule.line_extra:
        low = line.lower()
        return any(extra in low for extra in rule.line_extra)
    return True


def _dedupe(items: list[str]) -> list[str]:
    """去重并保持原有顺序。"""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result