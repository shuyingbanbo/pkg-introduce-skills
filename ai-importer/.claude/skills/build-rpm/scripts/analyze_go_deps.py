#!/usr/bin/env python3
"""
Go 包 RPM 依赖分析脚本

核心逻辑：
  Go 的 go.mod 依赖由 Go 工具链在构建时自动处理，不需要 RPM 包。
  RPM 打包真正需要关注的只有：
    1. go.mod 中声明的 Go 版本 → BuildRequires: golang >= x.y
    2. CGO 使用情况 → 需要 gcc + 具体 C 库的 -devel RPM

  C 库依赖通过在 openEuler 容器内执行一次共享批量查询，
  不依赖静态映射表，覆盖率更高。

用法：
  # 仅静态分析（不需要容器）
  python3 analyze_go_deps.py <source_dir>

  # 在容器内查询哪些依赖已有 RPM、哪些缺失（需要 docker）
  python3 analyze_go_deps.py <source_dir> --check-rpm --container oe-build-env

  # 输出 JSON
  python3 analyze_go_deps.py <source_dir> --check-rpm --container oe-build-env -o result.json
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

from rpm_batch_lookup import (
    BatchLookupError,
    fallback_results,
    file_glob_query,
    name_query,
    provides_query,
    run_batch_lookup,
)

# 永远存在于 glibc 中，不需要单独 RPM 的内置库
GLIBC_BUILTINS = {"pthread", "m", "dl", "c", "rt", "gcc_s", "stdc++", "resolv"}


# ── 1. go.mod 解析 ────────────────────────────────────────────────────────────

def parse_go_mod(source_dir: str) -> Dict:
    """
    解析 go.mod，提取 Go 版本和模块路径。
    返回：{"go_version": "1.21", "module_path": "github.com/foo/bar", "found": True,
           "module_deps": [{"name": "github.com/x/y", "vendor_only": True}, ...]}

    module_deps 是 require 声明的模块清单——由 go mod vendor 整体解决，
    标记 vendor_only（不进 dep_registry、不写 BuildRequires），
    供 precheck/supervisor 做 vendor 语言依赖的身份判定。
    """
    go_mod = Path(source_dir) / "go.mod"
    if not go_mod.exists():
        return {"found": False}

    content = go_mod.read_text(errors="ignore")
    result = {"found": True}

    m = re.search(r"^module\s+(\S+)", content, re.MULTILINE)
    if m:
        result["module_path"] = m.group(1)

    m = re.search(r"^go\s+([\d.]+)", content, re.MULTILINE)
    if m:
        result["go_version"] = m.group(1)

    # require 块与单行 require 的模块名（跳过 // indirect 注释标记，模块名照收）
    module_deps: List[Dict] = []
    seen: set = set()
    for m in re.finditer(r"^require\s+\((.*?)\)", content, re.MULTILINE | re.DOTALL):
        for line in m.group(1).splitlines():
            mm = re.match(r"\s*(\S+)\s+v\S+", line)
            if mm and mm.group(1) not in seen:
                seen.add(mm.group(1))
                module_deps.append({"name": mm.group(1), "vendor_only": True})
    for m in re.finditer(r"^require\s+(\S+)\s+v\S+", content, re.MULTILINE):
        if m.group(1) not in seen:
            seen.add(m.group(1))
            module_deps.append({"name": m.group(1), "vendor_only": True})
    result["module_deps"] = module_deps

    return result


# ── 2. CGO 静态扫描 ───────────────────────────────────────────────────────────

def scan_cgo(source_dir: str) -> Dict:
    """
    静态扫描源码目录，提取所有 CGO 依赖信息。

    返回：
    {
        "has_cgo": True,
        "cgo_files": ["foo/bar.go"],
        "ldflags_libs": ["ssl", "z", "sqlite3"],   # 来自 #cgo LDFLAGS: -l<name>
        "pkg_config": ["openssl", "zlib"],          # 来自 #cgo pkg-config: <name>
        "c_source_files": ["foo/bar.c"],            # 目录中存在的 .c 文件
    }
    """
    result = {
        "has_cgo": False,
        "cgo_files": [],
        "ldflags_libs": [],
        "pkg_config": [],
        "c_source_files": [],
    }

    ldflags_libs: set = set()
    pkg_configs: set = set()
    cgo_files: list = []
    c_files: list = []

    src = Path(source_dir)

    for go_file in src.rglob("*.go"):
        # 跳过测试文件，打包时不编译
        if go_file.name.endswith("_test.go"):
            continue

        try:
            content = go_file.read_text(errors="ignore")
        except OSError:
            continue

        if 'import "C"' not in content and "import `C`" not in content:
            continue

        rel = str(go_file.relative_to(src))
        cgo_files.append(rel)

        # 提取 #cgo LDFLAGS: -lfoo -lbar
        for m in re.finditer(r"#cgo(?:\s+\w+)?\s+LDFLAGS:\s*(.+)", content):
            flags = m.group(1)
            for lib in re.findall(r"-l(\S+)", flags):
                if lib not in GLIBC_BUILTINS:
                    ldflags_libs.add(lib)

        # 提取 #cgo pkg-config: foo bar
        for m in re.finditer(r"#cgo(?:\s+\w+)?\s+pkg-config:\s*(.+)", content):
            for pkg in m.group(1).split():
                pkg_configs.add(pkg.strip())

    # 扫描 .c / .cpp / .h 文件（说明有内嵌 C 代码）
    for ext in ("*.c", "*.cpp", "*.cc", "*.h"):
        for f in src.rglob(ext):
            c_files.append(str(f.relative_to(src)))

    result["has_cgo"] = len(cgo_files) > 0
    result["cgo_files"] = sorted(cgo_files)
    result["ldflags_libs"] = sorted(ldflags_libs)
    result["pkg_config"] = sorted(pkg_configs)
    result["c_source_files"] = sorted(c_files)[:20]  # 最多展示 20 个

    return result


# ── 3. 容器内批量 RPM 查询 ───────────────────────────────────────────────

def build_lookup_tasks(cgo_info: Dict) -> List[Dict]:
    tasks: List[Dict] = []
    for lib in cgo_info.get("ldflags_libs", []):
        lib_lower = lib.lower()
        tasks.append({
            "dep": lib,
            "type": "ldflags",
            "prefer_devel": True,
            "queries": [
                file_glob_query(f"*/lib{lib_lower}.so*", "libso", prefer_devel=True),
                name_query(f"{lib_lower}-devel", "name", prefer_devel=True),
                name_query(f"lib{lib_lower}-devel", "name", prefer_devel=True),
                provides_query(f"pkgconfig({lib_lower})", "pkgconfig()"),
            ],
        })
    for pc in cgo_info.get("pkg_config", []):
        pc_lower = pc.lower()
        tasks.append({
            "dep": pc,
            "type": "pkg-config",
            "prefer_devel": True,
            "queries": [
                provides_query(f"pkgconfig({pc_lower})", "pkgconfig()"),
                file_glob_query(f"*/lib{pc_lower}.so*", "libso", prefer_devel=True),
                name_query(f"{pc_lower}-devel", "name", prefer_devel=True),
            ],
        })
    return tasks


def check_rpm_availability(cgo_info: Dict = None) -> Dict:
    """本地查询 CGO 依赖的 RPM 可用性（无需容器）。"""
    if cgo_info is None:
        cgo_info = {}
    print(f"\n[INFO] 本地查询 RPM 可用性（单次批量查询）...")
    tasks = build_lookup_tasks(cgo_info)

    try:
        results = run_batch_lookup(tasks, timeout=300)
    except (BatchLookupError, OSError, json.JSONDecodeError) as e:
        print(f"[WARN] 批量 RPM 查询失败（{e}），跳过依赖检查")
        results = fallback_results(tasks)

    available = []
    missing = []
    for item in results:
        dep_type, dep, rpm = item["type"], item["dep"], item.get("rpm")
        if rpm:
            print(f"  ✓ [{dep_type}] {dep:<30} → {rpm}")
            available.append({"dep": dep, "type": dep_type, "rpm": rpm})
        else:
            print(f"  ✗ [{dep_type}] {dep:<30} → 未找到 (缺失)")
            missing.append({"dep": dep, "type": dep_type})

    return {"available": available, "missing": missing}


# ── 4. 汇总输出 ───────────────────────────────────────────────────────────────

def build_rpm_requires(go_mod: Dict, cgo_info: Dict, rpm_check: Optional[Dict]) -> List[str]:
    """生成 spec 文件的 BuildRequires 列表"""
    requires = []

    # Go 版本
    go_ver = go_mod.get("go_version")
    if go_ver:
        requires.append(f"golang >= {go_ver}")
    else:
        requires.append("golang")

    # CGO 基础依赖
    if cgo_info.get("has_cgo"):
        requires.append("gcc")
        requires.append("glibc-devel")

    # 已找到的 RPM 包
    if rpm_check:
        seen = set()
        for item in rpm_check.get("available", []):
            rpm = item["rpm"]
            if rpm not in seen:
                requires.append(rpm)
                seen.add(rpm)

    return requires


def print_report(go_mod: Dict, cgo_info: Dict, rpm_check: Optional[Dict]):
    """打印人类可读的分析报告"""
    print("\n" + "=" * 60)
    print("Go 包 RPM 依赖分析报告")
    print("=" * 60)

    # go.mod 信息
    if go_mod.get("found"):
        print(f"\n[go.mod]")
        print(f"  模块路径 : {go_mod.get('module_path', 'unknown')}")
        print(f"  Go 版本  : {go_mod.get('go_version', 'unknown')}")
    else:
        print("\n[警告] 未找到 go.mod 文件")

    # CGO 信息
    print(f"\n[CGO 分析]")
    if not cgo_info["has_cgo"]:
        print("  未使用 CGO，无 C 库依赖")
    else:
        print(f"  CGO 文件数 : {len(cgo_info['cgo_files'])}")
        for f in cgo_info["cgo_files"]:
            print(f"    - {f}")
        if cgo_info["ldflags_libs"]:
            print(f"  LDFLAGS 库 : {', '.join('-l' + l for l in cgo_info['ldflags_libs'])}")
        if cgo_info["pkg_config"]:
            print(f"  pkg-config : {', '.join(cgo_info['pkg_config'])}")
        if cgo_info["c_source_files"]:
            print(f"  内嵌 C 文件: {len(cgo_info['c_source_files'])} 个")

    # RPM 查询结果
    if rpm_check:
        print(f"\n[RPM 可用性]")
        if rpm_check["available"]:
            print(f"  已有 ({len(rpm_check['available'])} 个):")
            for item in rpm_check["available"]:
                print(f"    ✓ {item['dep']:<20} → {item['rpm']}")
        if rpm_check["missing"]:
            print(f"  缺失 ({len(rpm_check['missing'])} 个) — 需要自行打包:")
            for item in rpm_check["missing"]:
                print(f"    ✗ {item['dep']}")

    # BuildRequires 汇总
    requires = build_rpm_requires(go_mod, cgo_info, rpm_check)
    print(f"\n[BuildRequires 建议]")
    for r in requires:
        print(f"  BuildRequires: {r}")

    print("=" * 60)


# ── 5. 主入口 ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Go 包 RPM 依赖分析")
    parser.add_argument("source_dir", help="Go 项目源码目录")
    parser.add_argument("--check-rpm", action="store_true",
                        help="在容器内查询 RPM 可用性（需要 Docker）")
    parser.add_argument("-o", "--output", default="",
                        help="结果输出到 JSON 文件")
    args = parser.parse_args()

    source_dir = os.path.abspath(args.source_dir)
    if not os.path.isdir(source_dir):
        print(f"[ERROR] 目录不存在: {source_dir}", file=sys.stderr)
        sys.exit(1)

    # 执行分析
    print(f"[INFO] 分析目录: {source_dir}")
    go_mod = parse_go_mod(source_dir)
    cgo_info = scan_cgo(source_dir)

    rpm_check = None
    if args.check_rpm:
        if not cgo_info["has_cgo"]:
            print("[INFO] 未检测到 CGO，跳过 RPM 查询")
        else:
            rpm_check = check_rpm_availability(cgo_info=cgo_info)

    print_report(go_mod, cgo_info, rpm_check)

    # 输出 JSON
    if args.output:
        result = {
            "go_mod": go_mod,
            "cgo": cgo_info,
            "rpm_check": rpm_check,
            "build_requires": build_rpm_requires(go_mod, cgo_info, rpm_check),
        }
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n[INFO] 结果已保存: {args.output}")

    # 有缺失依赖时以非零退出码提示
    if rpm_check and rpm_check["missing"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
