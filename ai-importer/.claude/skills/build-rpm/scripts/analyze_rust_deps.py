#!/usr/bin/env python3
"""
Rust 包 RPM 依赖分析脚本

核心逻辑：
  Rust 的 [dependencies] 由 cargo 在构建时自动处理，不需要 RPM。
  RPM 打包真正需要关注的只有系统 C 库依赖，来源：
    1. build.rs — println!("cargo:rustc-link-lib=ssl") / pkg_config crate
    2. Cargo.toml — links = "foo" 字段（声明链接的系统库名）
    3. *.c / *.cpp 文件（cc crate 编译的内嵌 C 代码）

RPM 查询策略（共享一次性批量查询）：
  Level 1: `pkgconfig(foo)`
  Level 2: `libfoo.so*` / `*-devel` 回退

用法：
  python3 analyze_rust_deps.py <source_dir>
  python3 analyze_rust_deps.py <source_dir> --check-rpm --container oe-build-env
  python3 analyze_rust_deps.py <source_dir> --check-rpm --container oe-build-env -o result.json
"""

import argparse
import json
import os
import re
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


# ── 1. 源码解析 ───────────────────────────────────────────────────────────────

def parse_cargo_toml(source_dir: str, manifest_path: str = "") -> Dict:
    """
    解析 Cargo.toml，提取：
    - rust-version / edition
    - links 字段（声明链接的系统库）
    - build-dependencies（build.rs 用到的 crate，如 pkg-config、cc）
    - crate_deps（[dependencies] 等段的 crate 名清单，vendor 解决，标记 vendor_only）

    manifest_path 非空时直接解析该文件（混合包场景：Cargo.toml 在子目录，
    如 pendulum 的 rust/Cargo.toml）；否则取 <source_dir>/Cargo.toml。
    """
    cargo_toml = Path(manifest_path) if manifest_path else Path(source_dir) / "Cargo.toml"
    if not cargo_toml.exists():
        return {"found": False, "rust_version": "", "links": [], "build_deps": [], "crate_deps": []}

    content = cargo_toml.read_text(errors="ignore")
    result: Dict = {"found": True, "rust_version": "", "links": [], "build_deps": [], "crate_deps": []}

    # rust-version = "1.65"
    m = re.search(r'^rust-version\s*=\s*"([^"]+)"', content, re.MULTILINE)
    if m:
        result["rust_version"] = m.group(1)

    # edition = "2021"
    m = re.search(r'^edition\s*=\s*"([^"]+)"', content, re.MULTILINE)
    if m:
        result["edition"] = m.group(1)

    # links = "foo"  — 声明链接的系统库名
    m = re.search(r'^links\s*=\s*"([^"]+)"', content, re.MULTILINE)
    if m:
        lib = m.group(1)
        if lib not in GLIBC_BUILTINS:
            result["links"].append(lib)

    # 依赖段 crate 名提取：[dependencies] / [build-dependencies] / [dev-dependencies]
    # / [target.'cfg(...)'.dependencies] 统一按 *dependencies 段处理。
    # crate 由父包 cargo vendor 整体解决，标记 vendor_only（不进 dep_registry、
    # 不写 BuildRequires），清单供 precheck/supervisor 做 crate 身份判定。
    in_build_deps = False
    in_deps = False
    seen_crates: Set[str] = set()
    for line in content.splitlines():
        line = line.strip()
        if re.match(r'^\[.*\]\s*$', line):
            in_build_deps = bool(re.match(r'^\[.*build-dependencies.*\]', line))
            in_deps = bool(re.match(r'^\[.*dependencies.*\]', line))
            continue
        if in_deps:
            m = re.match(r'^"?([\w-]+)"?\s*=', line)
            if m:
                crate = m.group(1)
                if crate not in seen_crates:
                    seen_crates.add(crate)
                    result["crate_deps"].append({"name": crate, "vendor_only": True})
        if in_build_deps:
            m = re.match(r'^"?([\w-]+)"?\s*=', line)
            if m:
                result["build_deps"].append(m.group(1))

    return result


def scan_build_rs(source_dir: str) -> Dict:
    """
    扫描 build.rs，提取系统库依赖：
    - println!("cargo:rustc-link-lib=foo")        → 链接 libfoo
    - println!("cargo:rustc-link-lib=static=foo") → 静态链接
    - pkg_config::probe_library("foo")             → pkg-config 查询
    - pkg_config::Config::new().probe("foo")       → pkg-config 查询
    """
    build_rs = Path(source_dir) / "build.rs"
    if not build_rs.exists():
        return {"found": False, "link_libs": [], "pkg_configs": []}

    content = build_rs.read_text(errors="ignore")
    link_libs: Set[str] = set()
    pkg_configs: Set[str] = set()

    # cargo:rustc-link-lib=[static=|dylib=]foo
    for m in re.finditer(r'cargo:rustc-link-lib=(?:static=|dylib=)?(\w+)', content):
        lib = m.group(1)
        if lib not in GLIBC_BUILTINS:
            link_libs.add(lib)

    # pkg_config::probe_library("foo") 或 .probe("foo")
    for m in re.finditer(r'(?:probe_library|\.probe)\s*\(\s*"([^"]+)"', content):
        pkg_configs.add(m.group(1))

    # pkg_config::Config::new().arg("--libs").probe("foo")
    for m in re.finditer(r'"([a-z][a-z0-9_-]+)"', content):
        # 只收集看起来像库名的短字符串（在 pkg_config 上下文中）
        pass

    return {
        "found": True,
        "link_libs": sorted(link_libs),
        "pkg_configs": sorted(pkg_configs),
    }


def scan_c_sources(source_dir: str) -> List[str]:
    """检测内嵌 C/C++ 源文件（cc crate 编译）"""
    src = Path(source_dir)
    c_files = []
    for ext in ("*.c", "*.cpp", "*.cc"):
        c_files.extend(str(f.relative_to(src)) for f in src.rglob(ext))
    return c_files[:10]


# ── 2. 两级 RPM 查询 ──────────────────────────────────────────────────────────

def build_lookup_tasks(parsed: Dict) -> List[Dict]:
    tasks: List[Dict] = []
    for pc in parsed.get("pkg_configs", []):
        pc_lower = pc.lower()
        tasks.append({
            "dep": pc,
            "type": "pkgconfig",
            "prefer_devel": True,
            "queries": [
                provides_query(f"pkgconfig({pc_lower})", "pkgconfig()"),
                file_glob_query(f"*/lib{pc_lower}.so*", "libso", prefer_devel=True),
                name_query(f"{pc_lower}-devel", "name", prefer_devel=True),
            ],
        })
    for lib in parsed.get("link_libs", []):
        lib_lower = lib.lower()
        tasks.append({
            "dep": lib,
            "type": "link",
            "prefer_devel": True,
            "queries": [
                file_glob_query(f"*/lib{lib_lower}.so*", "libso", prefer_devel=True),
                name_query(f"{lib_lower}-devel", "name", prefer_devel=True),
                name_query(f"lib{lib_lower}-devel", "name", prefer_devel=True),
                provides_query(f"pkgconfig({lib_lower})", "pkgconfig()"),
            ],
        })
    return tasks


# ── 3. RPM 可用性检查 ─────────────────────────────────────────────────────────

def check_rpm_availability(parsed: Dict = None) -> Dict:
    if parsed is None:
        parsed = {}
    tasks = build_lookup_tasks(parsed)
    print(f"\n[INFO] 本地查询 RPM 可用性（单次批量查询）...")

    try:
        results = run_batch_lookup(tasks, timeout=120)
    except (BatchLookupError, OSError, json.JSONDecodeError) as e:
        print(f"[WARN] 批量 RPM 查询失败（{e}），跳过依赖检查")
        results = fallback_results(tasks)

    available, missing = [], []
    for item in results:
        dep_type, dep, rpm, level = item["type"], item["dep"], item.get("rpm"), item.get("level", "")
        label = f"[{dep_type}] {dep}"
        if rpm:
            print(f"  ✓ {label:<38} → {rpm}  ({level})")
            available.append({"dep": dep, "type": dep_type, "rpm": rpm, "level": level})
        else:
            print(f"  ✗ {label:<38} → 未找到")
            missing.append({"dep": dep, "type": dep_type})

    return {"available": available, "missing": missing}


# ── 4. BuildRequires 生成 & 报告 ──────────────────────────────────────────────

def build_rpm_requires(rust_version: str, rpm_check: Optional[Dict]) -> List[str]:
    result = ["rust", "cargo"]
    if rust_version:
        result[0] = f"rust >= {rust_version}"

    if rpm_check:
        seen = set(["rust", "cargo", result[0]])
        for item in rpm_check.get("available", []):
            rpm = item["rpm"]
            if rpm not in seen:
                result.append(rpm)
                seen.add(rpm)
    return result


def print_report(parsed: Dict, rpm_check: Optional[Dict]):
    sep = "=" * 60
    print(f"\n{sep}")
    print("Rust 包 RPM 依赖分析报告")
    print(sep)

    cargo = parsed.get("cargo_toml", {})
    if cargo.get("rust_version"):
        print(f"  rust-version : >= {cargo['rust_version']}")
    if cargo.get("edition"):
        print(f"  edition      : {cargo['edition']}")
    if cargo.get("links"):
        print(f"\n[Cargo.toml links]  {len(cargo['links'])} 个")
        for l in cargo["links"]:
            print(f"  - {l}")

    build = parsed.get("build_rs", {})
    if build.get("pkg_configs"):
        print(f"\n[build.rs pkg_config]  {len(build['pkg_configs'])} 个")
        for p in build["pkg_configs"]:
            print(f"  - {p}")
    if build.get("link_libs"):
        print(f"\n[build.rs -l 链接库]  {len(build['link_libs'])} 个")
        for l in build["link_libs"]:
            print(f"  - {l}")
    if parsed.get("c_sources"):
        print(f"\n[内嵌 C/C++ 源文件]  {len(parsed['c_sources'])} 个（前10）")
        for f in parsed["c_sources"]:
            print(f"  - {f}")

    if rpm_check:
        avail = rpm_check["available"]
        miss  = rpm_check["missing"]
        print(f"\n[RPM 可用性]  已有 {len(avail)} / 缺失 {len(miss)}")
        for item in avail:
            print(f"  ✓ {item['dep']:<30} → {item['rpm']}  [{item['level']}]")
        for item in miss:
            print(f"  ✗ {item['dep']}")

    rust_ver = cargo.get("rust_version", "")
    br = build_rpm_requires(rust_ver, rpm_check)
    print(f"\n[BuildRequires 建议]")
    for r in br:
        print(f"  BuildRequires: {r}")
    print(sep)


# ── 5. 主入口 ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Rust 包 RPM 依赖分析")
    parser.add_argument("source_dir", help="Rust 项目源码目录")
    parser.add_argument("--check-rpm", action="store_true", help="在容器内查询 RPM 可用性")
    parser.add_argument("--manifest-path", default="",
                        help="Cargo.toml 路径（混合包场景，默认 <source_dir>/Cargo.toml）")
    parser.add_argument("-o", "--output", default="", help="结果输出到 JSON 文件")
    args = parser.parse_args()

    source_dir = os.path.abspath(args.source_dir)
    if not os.path.isdir(source_dir):
        print(f"[ERROR] 目录不存在: {source_dir}", file=sys.stderr)
        sys.exit(1)

    # build.rs / 内嵌 C 源码在 Cargo.toml 所在 crate 目录内扫描
    crate_dir = str(Path(args.manifest_path).parent) if args.manifest_path else source_dir

    print(f"[INFO] 分析目录: {source_dir}")

    cargo_toml = parse_cargo_toml(source_dir, args.manifest_path)
    build_rs   = scan_build_rs(crate_dir)
    c_sources  = scan_c_sources(crate_dir)

    if not cargo_toml["found"]:
        print("[WARN] 未找到 Cargo.toml，可能不是 Rust 项目", file=sys.stderr)

    # 合并所有系统库依赖
    all_pkg_configs: List[str] = sorted(set(build_rs.get("pkg_configs", [])))
    all_link_libs: List[str] = sorted(set(
        build_rs.get("link_libs", []) + cargo_toml.get("links", [])
    ))

    parsed = {
        "cargo_toml": cargo_toml,
        "build_rs": build_rs,
        "c_sources": c_sources,
        "pkg_configs": all_pkg_configs,
        "link_libs": all_link_libs,
    }

    rpm_check = None
    if args.check_rpm:
        total = len(all_pkg_configs) + len(all_link_libs)
        if total == 0:
            print("[INFO] 未检测到系统库依赖，跳过 RPM 查询")
        else:
            rpm_check = check_rpm_availability(parsed=parsed)

    print_report(parsed, rpm_check)

    if args.output:
        result = {
            "rust_version": cargo_toml.get("rust_version", ""),
            "edition": cargo_toml.get("edition", ""),
            "links": cargo_toml.get("links", []),
            "crate_deps": cargo_toml.get("crate_deps", []),
            "pkg_configs": all_pkg_configs,
            "link_libs": all_link_libs,
            "c_sources": c_sources,
            "rpm_check": rpm_check,
            "build_requires": build_rpm_requires(cargo_toml.get("rust_version", ""), rpm_check),
        }
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n[INFO] 结果已保存: {args.output}")

    if rpm_check and rpm_check["missing"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
