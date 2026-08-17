#!/usr/bin/env python3
"""Phase 2.5：依赖评估与注册（在 evaluate 阶段、run_gate 之后调用）。

对主包的每个依赖做完整 4 级级联检查，将需要引入的依赖注册到 dep_registry.json。
与 build 阶段的 pre_check_deps.py 互补：本脚本负责"发现和注册"，pre_check_deps.py
负责"生成 BuildRequires 列表"。

用法：
  python3 evaluate-deps.py --session-dir . --pkg <pkgname> --lang <lang> [--source-dir <path>]

退出码：
  0 — 所有依赖均已满足或已注册
  1 — 脚本执行出错
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
BUILD_RPM_SCRIPTS = SCRIPT_DIR.parents[1] / "build-rpm" / "scripts"
sys.path.insert(0, str(BUILD_RPM_SCRIPTS))

# 语言 → analyzer 映射（与 pre_check_deps.py 保持一致）
ANALYZERS = {
    "python": "analyze_python_deps.py",
    "go":     "analyze_go_deps.py",
    "rust":   "analyze_rust_deps.py",
    "c":      "analyze_c_deps.py",
    "cpp":    "analyze_cpp_deps.py",
    "nodejs": "analyze_nodejs_deps.py",
    "java":   "analyze_java_deps.py",
}

# 需要触发引入的级联 decision
_INTRODUCE_DECISIONS = {"evaluate", "introduce_new_with_ref", "introduce_new"}


def _load_cascade() -> Any:
    cascade_path = BUILD_RPM_SCRIPTS / "cascade_package_check.py"
    spec = importlib.util.spec_from_file_location("cascade_package_check", cascade_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载: {cascade_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _extract_deps(lang: str, source_dir: str, pkgname: str, output_dir: Path) -> list[dict]:
    """运行语言专用 analyzer，提取依赖列表。"""
    analyzer_script = ANALYZERS.get(lang)
    if not analyzer_script:
        print(f"[evaluate-deps] 不支持的语言: {lang}", file=sys.stderr)
        return []

    script_path = BUILD_RPM_SCRIPTS / analyzer_script
    if not script_path.exists():
        print(f"[evaluate-deps] analyzer 不存在: {script_path}", file=sys.stderr)
        return []

    analysis_path = output_dir / f"evaluate_deps_{pkgname}_analysis.json"
    cmd = [sys.executable, str(script_path), source_dir, "--check-rpm", "-o", str(analysis_path)]
    if lang == "python":
        cmd += ["--pkg", pkgname]
        copr_chroot = os.environ.get("COPR_CHROOT", "")
        if copr_chroot:
            cmd += ["--chroot", copr_chroot]

    print(f"[evaluate-deps] 运行: {' '.join(cmd)}", file=sys.stderr)
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode not in (0, 2):
        print(f"[evaluate-deps] analyzer 失败 (rc={proc.returncode}): {proc.stderr.decode()[:500]}",
              file=sys.stderr)
        return []

    try:
        result = json.loads(analysis_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[evaluate-deps] 无法读取分析结果: {e}", file=sys.stderr)
        return []

    # 收集所有需要检查的依赖（去重）
    seen: set[str] = set()
    deps: list[dict] = []
    rpm_check = result.get("rpm_check") or {}

    # dependency_items：运行时依赖
    for item in result.get("dependency_items", []):
        name = item.get("name", "")
        if name and name not in seen:
            seen.add(name)
            deps.append({
                "name": name,
                "requirement": item.get("requirement", ""),
                "upstream_url": item.get("upstream_url", ""),
                "rpm_pkg_name": item.get("rpm_pkg_name", ""),
            })

    # build_sys_dependency_items：构建系统依赖
    for item in result.get("build_sys_dependency_items", []):
        name = item.get("name", "")
        if name and name not in seen and name != pkgname:
            seen.add(name)
            deps.append({
                "name": name,
                "requirement": item.get("requirement", ""),
                "upstream_url": item.get("upstream_url", ""),
                "rpm_pkg_name": item.get("rpm_pkg_name", ""),
            })

    # rpm_check.missing：仓库中不存在的包
    for item in rpm_check.get("missing", []):
        name = item.get("name", "")
        if name and name not in seen:
            seen.add(name)
            deps.append({
                "name": name,
                "requirement": item.get("requirement", ""),
                "upstream_url": item.get("upstream_url", ""),
                "rpm_pkg_name": item.get("rpm_name", ""),
            })

    # rpm_check.version_conflict：版本冲突的包
    for item in rpm_check.get("version_conflict", []):
        name = item.get("name", "")
        if name and name not in seen:
            seen.add(name)
            deps.append({
                "name": name,
                "requirement": item.get("requirement", ""),
                "upstream_url": item.get("upstream_url", ""),
                "rpm_pkg_name": item.get("rpm", ""),
            })

    return deps


def _cascade_check_dep(dep: dict, lang: str, cascade_module: Any) -> dict | None:
    """对单个依赖做级联检查，返回 cascade_result 或 None。"""
    copr_url = os.environ.get("COPR_FRONTEND_URL", "")
    copr_owner = os.environ.get("COPR_OWNER", "")
    copr_project = os.environ.get("COPR_PROJECT", "")
    copr_login = os.environ.get("COPR_API_LOGIN", "")
    copr_token = os.environ.get("COPR_API_TOKEN", "")

    if not (copr_url and copr_owner and copr_project and copr_login and copr_token):
        return None

    try:
        return cascade_module.check_package_existence(
            dep["name"],
            lang=lang,
            version="",
            requirement=dep.get("requirement", ""),
            target=os.environ.get("COPR_CHROOT", ""),
            copr_url=copr_url,
            copr_owner=copr_owner,
            copr_project=copr_project,
            copr_login=copr_login,
            copr_token=copr_token,
        )
    except Exception as e:
        print(f"[evaluate-deps] 级联检查 {dep['name']} 失败: {e}", file=sys.stderr)
        return None


def _register_dep(dep: dict, required_by: str, session_dir: str) -> bool:
    """调用 register-dep.py 将依赖注册到 dep_registry.json。"""
    register_script = SCRIPT_DIR / "register-dep.py"
    cmd = [
        sys.executable, str(register_script),
        "--session-dir", session_dir,
        "--pkg", dep["name"],
        "--constraint", dep.get("requirement", ""),
        "--required-by", required_by,
    ]
    if dep.get("upstream_url"):
        cmd += ["--url", dep["upstream_url"]]
    if dep.get("lang"):
        cmd += ["--lang", dep["lang"]]

    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        stderr = proc.stderr.decode()
        # exit 2 = 工具链过滤，跳过而非报错
        if proc.returncode == 2 and "toolchain" in stderr.lower():
            print(f"[evaluate-deps] 跳过工具链包: {dep['name']}", file=sys.stderr)
            return True
        print(f"[evaluate-deps] register-dep 失败 ({dep['name']}): {stderr[:300]}",
              file=sys.stderr)
        return False
    print(f"[evaluate-deps] registered: {dep['name']}", file=sys.stderr)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="evaluate 阶段依赖评估与注册")
    parser.add_argument("--session-dir", required=True, help="session 根目录")
    parser.add_argument("--pkg", required=True, help="主包名")
    parser.add_argument("--lang", required=True, help="语言")
    parser.add_argument("--source-dir", default="", help="源码目录（默认 session_dir/sources/<pkg>）")
    args = parser.parse_args()

    session_dir = Path(args.session_dir).resolve()
    source_dir = args.source_dir or str(session_dir / "sources" / args.pkg)
    pkg_dir = session_dir / "pkgs" / args.pkg
    reports_dir = session_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    if not Path(source_dir).exists():
        print(f"[evaluate-deps] 源码目录不存在: {source_dir}，跳过依赖评估", file=sys.stderr)
        return 0

    # 1. 提取依赖
    deps = _extract_deps(args.lang, source_dir, args.pkg, reports_dir)
    if not deps:
        print(f"[evaluate-deps] 未发现依赖", file=sys.stderr)
        return 0

    print(f"[evaluate-deps] 发现 {len(deps)} 个依赖: {[d['name'] for d in deps]}", file=sys.stderr)

    # 2. 级联检查 + 注册
    cascade_module = _load_cascade()
    resolved = []
    introduced = []
    failed = []

    for dep in deps:
        cascade_result = _cascade_check_dep(dep, args.lang, cascade_module)
        if cascade_result is None:
            # 级联不可用，跳过（pre_check_deps.py 在 build 阶段兜底）
            print(f"[evaluate-deps] 跳过 {dep['name']}（级联不可用）", file=sys.stderr)
            continue

        decision = cascade_result.get("decision", "")
        level = cascade_result.get("level", 4)
        print(f"[evaluate-deps] {dep['name']}: L{level} {decision}", file=sys.stderr)

        if decision in _INTRODUCE_DECISIONS:
            if _register_dep(dep, args.pkg, str(session_dir)):
                introduced.append({"name": dep["name"], "decision": decision, "level": level})
            else:
                failed.append(dep["name"])
        else:
            resolved.append({"name": dep["name"], "decision": decision, "level": level})

    # 3. 输出摘要
    summary = {
        "pkgname": args.pkg,
        "lang": args.lang,
        "total_deps": len(deps),
        "resolved": resolved,
        "introduced": introduced,
        "failed": failed,
    }
    summary_path = reports_dir / f"evaluate_deps_{args.pkg}.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[evaluate-deps] 结果: {len(resolved)} resolved, {len(introduced)} introduced, "
          f"{len(failed)} failed", file=sys.stderr)

    if introduced:
        dep_names = [d["name"] for d in introduced]
        print(f"[evaluate-deps] 已注册依赖到 dep_registry: {dep_names}", file=sys.stderr)

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
