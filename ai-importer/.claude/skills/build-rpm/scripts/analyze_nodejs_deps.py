#!/usr/bin/env python3
"""
Node.js 包 RPM 依赖分析脚本

依赖来源：
  1. package.json — name / engines.node（版本约束）/ dependencies（运行时依赖）
  2. binding.gyp  — libraries 字段中的 -l<lib>（原生扩展）

RPM 查询策略（共享一次性批量查询）：
  Level 1: `pkgconfig(foo)`
  Level 2: `libfoo.so*` / `*-devel` 回退
  nodejs 运行时依赖：npm(<name>) provide 查询 / nodejs-<name> name 查询

固定 BuildRequires：nodejs-devel + npm（+ 原生扩展查到的系统库）

用法：
  python3 analyze_nodejs_deps.py <source_dir>
  python3 analyze_nodejs_deps.py <source_dir> --check-rpm --container oe-build-env
  python3 analyze_nodejs_deps.py <source_dir> --check-rpm --container oe-build-env -o result.json
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set

from rpm_batch_lookup import (
    BatchLookupError,
    fallback_results,
    file_glob_query,
    name_query,
    provides_query,
    run_batch_lookup,
)


GLIBC_BUILTINS = {"pthread", "m", "dl", "c", "rt", "gcc_s", "stdc++", "resolv", "util"}


def _npm_to_rpm_name(npm_name: str) -> str:
    """Convert npm package name to openEuler nodejs-* RPM name.

    Handles scoped packages (@scope/pkg -> nodejs-scope-pkg) and normalises
    non-alphanumeric characters to dashes.
    """
    name = npm_name
    if name.startswith("@"):
        # @scope/pkg -> scope-pkg
        name = name.lstrip("@").replace("/", "-", 1)
    # Replace remaining slashes or underscores with dashes
    name = re.sub(r"[/_]", "-", name)
    return f"nodejs-{name}"



# ── 1. 源码解析 ───────────────────────────────────────────────────────────────

def parse_package_json(source_dir: str) -> Dict:
    """解析 package.json，提取 name、engines.node 和 dependencies"""
    pkg_json = Path(source_dir) / "package.json"
    if not pkg_json.exists():
        return {"found": False, "name": "", "node_version": "", "has_native": False, "dependencies": {}}

    try:
        data = json.loads(pkg_json.read_text(errors="ignore"))
    except json.JSONDecodeError:
        return {"found": True, "name": "", "node_version": "", "has_native": False, "dependencies": {}}

    name = data.get("name", "")
    node_version = ""
    engines = data.get("engines", {})
    if isinstance(engines, dict):
        node_version = engines.get("node", "")

    has_native = (Path(source_dir) / "binding.gyp").exists()

    # 收集运行时依赖（dependencies，不含 devDependencies）
    dependencies = {}
    raw_deps = data.get("dependencies", {})
    if isinstance(raw_deps, dict):
        dependencies = raw_deps

    return {
        "found": True,
        "name": name,
        "node_version": node_version,
        "has_native": has_native,
        "dependencies": dependencies,
    }


def parse_binding_gyp(source_dir: str) -> Dict:
    """
    解析 binding.gyp，提取 libraries 字段中的 -l<lib>。
    binding.gyp 是 JSON 超集（允许注释），用正则提取 libraries 数组。
    """
    gyp = Path(source_dir) / "binding.gyp"
    if not gyp.exists():
        return {"found": False, "link_libs": []}

    content = gyp.read_text(errors="ignore")
    link_libs: Set[str] = set()

    for m in re.finditer(r'"libraries"\s*:\s*\[([^\]]*)\]', content, re.DOTALL):
        block = m.group(1)
        for lib in re.findall(r'-l(\w+)', block):
            if lib not in GLIBC_BUILTINS:
                link_libs.add(lib)

    return {"found": True, "link_libs": sorted(link_libs)}


# ── 2. 原生扩展 RPM 查询 ──────────────────────────────────────────────────────

def build_lookup_tasks(link_libs: List[str]) -> List[Dict]:
    tasks: List[Dict] = []
    for lib in link_libs:
        lib_lower = lib.lower()
        tasks.append({
            "dep": lib,
            "type": "link",
            "prefer_devel": True,
            "queries": [
                provides_query(f"pkgconfig({lib_lower})", "pkgconfig()"),
                file_glob_query(f"*/lib{lib_lower}.so*", "libso", prefer_devel=True),
                name_query(f"{lib_lower}-devel", "name", prefer_devel=True),
                name_query(f"lib{lib_lower}-devel", "name", prefer_devel=True),
            ],
        })
    return tasks


def check_rpm_availability(link_libs: List[str] = None) -> Dict:
    if link_libs is None:
        link_libs = []
    tasks = build_lookup_tasks(link_libs)
    print(f"\n[INFO] 本地查询原生扩展 RPM 可用性...")

    try:
        results = run_batch_lookup(tasks, timeout=120)
    except (BatchLookupError, OSError, json.JSONDecodeError) as e:
        print(f"[WARN] 批量 RPM 查询失败（{e}），跳过依赖检查")
        results = fallback_results(tasks)

    available, missing = [], []
    for item in results:
        lib, rpm, level = item["dep"], item.get("rpm"), item.get("level", "")
        if rpm:
            print(f"  ✓ [link] {lib:<38} → {rpm}  ({level})")
            available.append({"dep": lib, "type": "link", "rpm": rpm, "level": level})
        else:
            print(f"  ✗ [link] {lib:<38} → 未找到")
            missing.append({"dep": lib, "type": "link"})
    return {"available": available, "missing": missing}


# ── 3. 运行时 npm 依赖查询 ────────────────────────────────────────────────────

def _parse_npm_constraint(constraint: str):
    """
    将 npm semver 约束解析为 (min_ver, max_ver, min_inclusive, max_inclusive)。
    支持：^X.Y.Z  ~X.Y.Z  >=X.Y.Z  >X.Y.Z  <=X.Y.Z  <X.Y.Z  =X.Y.Z  X.Y.Z
    返回 None 表示无约束或无法解析。
    """
    constraint = constraint.strip()
    if not constraint or constraint == "*" or constraint == "latest":
        return None

    def to_tuple(v: str):
        # 在首个非数字/点字符处截断（1.0rc1 → 1.0），而非删除字母（会得到 1.01 这种错误值）
        parts = re.split(r"[^0-9.]", v, maxsplit=1)[0].split(".")
        try:
            return tuple(int(x) for x in parts[:3] if x != "")
        except ValueError:
            return None

    # ^ caret: ^1.2.3 → >=1.2.3 <2.0.0, ^0.2.3 → >=0.2.3 <0.3.0
    m = re.match(r'^\^(\d+\.\d+\.\d+)', constraint)
    if m:
        base = to_tuple(m.group(1))
        if base:
            major, minor, patch = base
            if major > 0:
                return base, (major + 1, 0, 0), True, False
            elif minor > 0:
                return base, (0, minor + 1, 0), True, False
            else:
                return base, (0, 0, patch + 1), True, False

    # ~ tilde: ~1.2.3 → >=1.2.3 <1.3.0
    m = re.match(r'^~(\d+\.\d+\.\d+)', constraint)
    if m:
        base = to_tuple(m.group(1))
        if base:
            major, minor, _ = base
            return base, (major, minor + 1, 0), True, False

    # >= <= > < = exact
    m = re.match(r'^(>=|<=|>|<|=|==)?\s*(\d+[\d.]*)$', constraint)
    if m:
        op, ver = m.group(1) or "=", m.group(2)
        v = to_tuple(ver)
        if v is None:
            return None
        if op in ("=", "==", ""):
            return v, v, True, True
        if op == ">=":
            return v, None, True, True
        if op == ">":
            return v, None, False, True
        if op == "<=":
            return (0, 0, 0), v, True, True
        if op == "<":
            return (0, 0, 0), v, True, False

    return None


def _version_satisfies(rpm_version: str, constraint: str) -> bool:
    """判断容器内 RPM 的版本是否满足 npm 版本约束。解析失败则保守返回 True（不阻断）。"""
    if not constraint or not rpm_version:
        return True

    def to_tuple(v: str):
        # 在首个非数字/点字符处截断（1.0rc1 → 1.0），而非删除字母（会得到 1.01 这种错误值）
        parts = re.split(r"[^0-9.]", v, maxsplit=1)[0].split(".")
        try:
            return tuple(int(x) for x in parts[:3] if x != "")
        except ValueError:
            return None

    parsed = _parse_npm_constraint(constraint)
    if parsed is None:
        return True  # 无法解析的约束保守放行

    min_ver, max_ver, min_inc, max_inc = parsed
    rv = to_tuple(rpm_version)
    if rv is None:
        return True  # 版本格式异常，保守放行

    # 补齐到三段
    def pad(t):
        return t + (0,) * (3 - len(t)) if t else (0, 0, 0)

    rv = pad(rv)
    if min_ver:
        min_ver = pad(min_ver)
        if min_inc and rv < min_ver:
            return False
        if not min_inc and rv <= min_ver:
            return False
    if max_ver:
        max_ver = pad(max_ver)
        if max_inc and rv > max_ver:
            return False
        if not max_inc and rv >= max_ver:
            return False

    return True


def check_runtime_deps(dependencies: Dict[str, str]) -> Dict:
    """
    检查 package.json dependencies 在容器内是否有对应 RPM，并校验版本约束。
    返回 available / missing / version_conflict 三态列表。
    version_conflict: 社区源有该包但版本不满足约束，携带 found_version。
    """
    if not dependencies:
        return {"available": [], "missing": [], "version_conflict": []}

    print(f"\n[INFO] 检查 {len(dependencies)} 个运行时依赖...")
    available, missing, version_conflict = [], [], []

    for npm_name, version_constraint in dependencies.items():
        tasks = [
            {
                "dep": npm_name,
                "type": "runtime",
                "prefer_devel": False,
                "queries": [
                    provides_query(f"npm({npm_name})", "npm_provides"),
                    name_query(_npm_to_rpm_name(npm_name), "name"),
                ],
            }
        ]
        try:
            results = run_batch_lookup("", tasks, timeout=60)
        except Exception as e:
            print(f"  [WARN] 查询 {npm_name} 失败（{e}），标记为 pending")
            missing.append({
                "dep": npm_name,
                "type": "runtime",
                "constraint": version_constraint,
                "upstream_url": "",
            })
            continue

        found = False
        for item in results:
            rpm = item.get("rpm")
            if not rpm:
                continue
            rpm_version = item.get("version") or ""
            satisfies = _version_satisfies(rpm_version, version_constraint)
            if satisfies:
                print(f"  ✓ [runtime] {npm_name:<35} → {rpm} {rpm_version}  ({item.get('level','')})")
                available.append({
                    "dep": npm_name,
                    "type": "runtime",
                    "rpm": rpm,
                    "rpm_version": rpm_version,
                    "constraint": version_constraint,
                })
                found = True
                break
            else:
                print(f"  ~ [runtime] {npm_name:<35} → {rpm} {rpm_version} 不满足约束 {version_constraint}（版本冲突）")
                version_conflict.append({
                    "dep": npm_name,
                    "name": npm_name,
                    "type": "runtime",
                    "rpm": rpm,
                    "found_version": rpm_version,
                    "constraint": version_constraint,
                    "requirement": version_constraint,
                    "upstream_url": "",
                })
                found = True
                break
        if not found:
            missing.append({
                "dep": npm_name,
                "type": "runtime",
                "constraint": version_constraint,
                "upstream_url": "",
            })

    return {"available": available, "missing": missing, "version_conflict": version_conflict}


# ── 4. BuildRequires 生成 & 报告 ──────────────────────────────────────────────

def build_rpm_requires(pkg_info: Dict, rpm_check: Optional[Dict]) -> List[str]:
    result = ["nodejs-devel", "npm"]
    node_ver = pkg_info.get("node_version", "")
    if node_ver:
        m = re.search(r"(\d+)", node_ver)
        if m:
            result[0] = f"nodejs >= {m.group(1)}"

    if pkg_info.get("has_native"):
        result.append("gcc")
        result.append("gcc-c++")
        result.append("python3")

    if rpm_check:
        seen = set(result)
        for item in rpm_check.get("available", []):
            rpm = item["rpm"]
            if rpm not in seen:
                result.append(rpm)
                seen.add(rpm)
    return result


def print_report(pkg_info: Dict, gyp_info: Dict, rpm_check: Optional[Dict], runtime_deps: Optional[Dict]):
    sep = "=" * 60
    print(f"\n{sep}")
    print("Node.js 包 RPM 依赖分析报告")
    print(sep)
    print(f"  包名      : {pkg_info.get('name', '(未知)')}")
    if pkg_info.get("node_version"):
        print(f"  Node 版本 : {pkg_info['node_version']}")
    print(f"  原生扩展  : {'是' if pkg_info.get('has_native') else '否'}")

    if gyp_info.get("link_libs"):
        print(f"\n[binding.gyp -l 链接库]  {len(gyp_info['link_libs'])} 个")
        for lib in gyp_info["link_libs"]:
            print(f"  - {lib}")

    if rpm_check:
        avail = rpm_check["available"]
        miss  = rpm_check["missing"]
        print(f"\n[原生扩展 RPM 可用性]  已有 {len(avail)} / 缺失 {len(miss)}")
        for item in avail:
            print(f"  ✓ {item['dep']:<30} → {item['rpm']}  [{item['level']}]")
        for item in miss:
            print(f"  ✗ {item['dep']}")

    if runtime_deps:
        avail = runtime_deps["available"]
        miss  = runtime_deps["missing"]
        print(f"\n[运行时依赖（package.json dependencies）]  已有 {len(avail)} / 需引入 {len(miss)}")
        for item in avail:
            print(f"  ✓ {item['dep']:<35} → {item['rpm']}")
        for item in miss:
            print(f"  ✗ {item['dep']:<35}  constraint={item.get('constraint','')}")

    br = build_rpm_requires(pkg_info, rpm_check)
    print(f"\n[BuildRequires 建议]")
    for r in br:
        print(f"  BuildRequires: {r}")
    print(sep)


# ── 5. 主入口 ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Node.js 包 RPM 依赖分析")
    parser.add_argument("source_dir", help="Node.js 项目源码目录")
    parser.add_argument("--check-rpm", action="store_true", help="在容器内查询 RPM 可用性")
    parser.add_argument("-o", "--output", default="", help="结果输出到 JSON 文件")
    args = parser.parse_args()

    source_dir = os.path.abspath(args.source_dir)
    if not os.path.isdir(source_dir):
        print(f"[ERROR] 目录不存在: {source_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] 分析目录: {source_dir}")
    pkg_info = parse_package_json(source_dir)
    gyp_info = parse_binding_gyp(source_dir)

    if not pkg_info["found"]:
        print("[WARN] 未找到 package.json，可能不是 Node.js 项目", file=sys.stderr)

    rpm_check = None
    runtime_deps = None

    if args.check_rpm:
        link_libs = gyp_info.get("link_libs", [])
        if not link_libs:
            print("[INFO] 未检测到原生扩展依赖，跳过原生扩展 RPM 查询")
        else:
            rpm_check = check_rpm_availability(link_libs=link_libs)

        runtime_deps = check_runtime_deps(pkg_info.get("dependencies", {}))

    print_report(pkg_info, gyp_info, rpm_check, runtime_deps)

    if args.output:
        result = {
            "name": pkg_info.get("name", ""),
            "node_version": pkg_info.get("node_version", ""),
            "has_native": pkg_info.get("has_native", False),
            "link_libs": gyp_info.get("link_libs", []),
            "dependencies": pkg_info.get("dependencies", {}),
            "rpm_check": rpm_check,
            "runtime_deps": runtime_deps,
            "build_requires": build_rpm_requires(pkg_info, rpm_check),
        }
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n[INFO] 结果已保存: {args.output}")

    has_missing = (rpm_check and rpm_check["missing"]) or (runtime_deps and runtime_deps["missing"])
    if has_missing:
        sys.exit(2)


if __name__ == "__main__":
    main()

