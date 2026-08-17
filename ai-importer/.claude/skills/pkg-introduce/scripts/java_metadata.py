#!/usr/bin/env python3
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path


def _strip_namespace(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _find_first_text(root: ET.Element, name: str) -> str:
    for element in root.iter():
        if _strip_namespace(element.tag) == name and element.text and element.text.strip():
            return element.text.strip()
    return ""


def _extract_project_version(root: ET.Element) -> str:
    """从 pom.xml 的 <project> 直接子节点 <version> 取版本，避免误取插件配置里的版本号。"""
    for child in root:
        if _strip_namespace(child.tag) == "version" and child.text and child.text.strip():
            v = child.text.strip()
            if not v.startswith("${"):
                return v
    return ""


def _extract_parent_version(root: ET.Element) -> str:
    """从 pom.xml 的 <parent>/<version> 取版本（聚合 pom 无直接 <version> 时的兜底）。"""
    for child in root:
        if _strip_namespace(child.tag) == "parent":
            for sub in child:
                if _strip_namespace(sub.tag) == "version" and sub.text and sub.text.strip():
                    v = sub.text.strip()
                    if not v.startswith("${"):
                        return v
    return ""


def _try_pom(pom_path: Path) -> str:
    try:
        root = ET.fromstring(pom_path.read_text(encoding="utf-8", errors="ignore"))
        return _extract_project_version(root) or _extract_parent_version(root)
    except Exception:
        return ""


def extract_java_version(source_dir: str) -> str:
    src = Path(source_dir)

    pom_xml = src / "pom.xml"
    if pom_xml.exists():
        version = _try_pom(pom_xml)
        if version:
            return version
        # Aggregator pom may delegate version to a submodule pom (e.g. parent/pom.xml)
        for candidate in sorted(src.glob("*/pom.xml")):
            version = _try_pom(candidate)
            if version:
                return version

    gradle_properties = src / "gradle.properties"
    if gradle_properties.exists():
        content = gradle_properties.read_text(encoding="utf-8", errors="ignore")
        match = re.search(r"(?m)^\s*version\s*=\s*(.+?)\s*$", content)
        if match:
            return match.group(1).strip()

    for filename in ("build.gradle", "build.gradle.kts"):
        build_file = src / filename
        if not build_file.exists():
            continue
        content = build_file.read_text(encoding="utf-8", errors="ignore")
        patterns = [
            r"(?m)^\s*version\s*=\s*['\"]([^'\"]+)['\"]",
            r"(?m)^\s*version\s+['\"]([^'\"]+)['\"]",
        ]
        for pattern in patterns:
            match = re.search(pattern, content)
            if match:
                return match.group(1).strip()

    # gradle/libs.versions.toml — look for a key matching the project name
    libs_toml = src / "gradle" / "libs.versions.toml"
    if libs_toml.exists():
        try:
            content = libs_toml.read_text(encoding="utf-8", errors="ignore")
            pkg_name = src.name.lower().replace("-", "_").replace(".", "_")
            patterns = [
                rf'(?m)^{re.escape(pkg_name)}\s*=\s*"([^"]+)"',
                r'(?m)^[a-zA-Z0-9_-]+\s*=\s*"(\d+\.\d+[\w.\-]*)"',
            ]
            for pattern in patterns:
                match = re.search(pattern, content)
                if match:
                    return match.group(1).strip()
        except OSError:
            pass

    return ""


def detect_java_build_system(source_dir: str) -> str:
    """探测 Java 项目的构建系统："maven" | "gradle" | "unknown"。

    pom.xml 存在 → maven（优先；存在 pom+gradle 混合项目，maven 设施可用）。
    否则存在 build.gradle(.kts)/settings.gradle(.kts) → gradle。
    都没有 → unknown。
    """
    src = Path(source_dir)
    if (src / "pom.xml").exists():
        return "maven"
    for filename in ("build.gradle", "build.gradle.kts",
                     "settings.gradle", "settings.gradle.kts"):
        if (src / filename).exists():
            return "gradle"
    return "unknown"


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3 or sys.argv[1] not in ("version", "build-system"):
        print("usage: java_metadata.py version|build-system <source-dir>", file=sys.stderr)
        sys.exit(2)
    if sys.argv[1] == "version":
        print(extract_java_version(sys.argv[2]))
    else:
        print(detect_java_build_system(sys.argv[2]))
