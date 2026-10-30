"""Mojang 版本 JSON 中的 rules 规则求值。

规则按顺序应用，最后一条匹配的规则决定结果；没有任何 rules 时默认允许。
"""

from __future__ import annotations

import platform
import re
from typing import Any, Optional


def detect_os_name() -> str:
    """返回 Mojang 使用的 OS 名称：windows / osx / linux。"""
    system = platform.system().lower()
    if system.startswith("win"):
        return "windows"
    if system == "darwin":
        return "osx"
    return "linux"


def detect_os_arch() -> str:
    """返回 Mojang 使用的架构名称：x86 / x64 / arm64 / arm32。"""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    if machine in ("i386", "i486", "i586", "i686", "x86"):
        return "x86"
    if machine.startswith("arm"):
        return "arm32"
    return machine or "x64"


def rules_allow(
    rules: Optional[list[dict[str, Any]]],
    os_name: str,
    os_arch: str,
    features: Optional[dict[str, bool]] = None,
) -> bool:
    """判断一组 rules 是否允许当前环境。"""
    if not rules:
        return True

    features = features or {}
    allowed = False
    for rule in rules:
        if _rule_matches(rule, os_name, os_arch, features):
            allowed = rule.get("action") == "allow"
    return allowed


def _rule_matches(
    rule: dict[str, Any],
    os_name: str,
    os_arch: str,
    features: dict[str, bool],
) -> bool:
    os_rule = rule.get("os")
    if os_rule:
        if "name" in os_rule and os_rule["name"] != os_name:
            return False
        if "arch" in os_rule and os_rule["arch"] != os_arch:
            return False
        if "version" in os_rule:
            os_version = platform.release()
            try:
                if not re.search(os_rule["version"], os_version):
                    return False
            except re.error:
                return False

    feature_rule = rule.get("features")
    if feature_rule:
        for key, expected in feature_rule.items():
            if bool(features.get(key)) != bool(expected):
                return False

    return True