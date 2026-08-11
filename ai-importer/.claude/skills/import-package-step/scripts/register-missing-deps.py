#!/usr/bin/env python3
"""从 COPR 构建日志中提取缺失 RPM 包，注册到 dep_registry.json。

用法：
  python3 register-missing-deps.py --session-dir . --pkg setuptools
"""
import argparse
import json
import re
import sys
from pathlib import Path

# 引入构建工具链约束
BUILD_RPM_SCRIPTS = Path(__file__).resolve().parents[2] / "build-rpm" / "scripts"
sys.path.insert(0, str(BUILD_RPM_SCRIPTS))
from chroot_toolchain import is_toolchain  # noqa: E402
from rpm_naming import rpm_name_from_gav  # noqa: E402


def _extract_constraint(log_text: str, rpm_pkg: str) -> str:
    """从 log 里提取包名对应的版本约束，如 '>= 1.4.0'。"""
    # 匹配 "No matching package to install: 'xxx >= y.z'"
    m = re.search(
        r"No matching package to install: ['\"]" + re.escape(rpm_pkg) + r"\s*([><=!][^'\"]+)['\"]",
        log_text,
    )
    if m:
        return m.group(1).strip()
    # 匹配 "nothing provides xxx >= y.z needed by"
    m = re.search(
        r"nothing provides " + re.escape(rpm_pkg) + r"\s*([><=!][^\s]+(?:\s*[0-9][^\s]*)?)",
        log_text,
    )
    if m:
        return m.group(1).strip()
    return ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--pkg", required=True)
    args = parser.parse_args()

    sd = Path(args.session_dir)
    build_result_path = sd / "pkgs" / args.pkg / "build_rpm_result.json"
    build_log_path    = sd / "pkgs" / args.pkg / "build.log"

    log_text = ""
    if build_result_path.exists():
        d = json.loads(build_result_path.read_text(encoding="utf-8"))
        log_text = d.get("build_log", "") or d.get("build_log_tail", "")
    if not log_text and build_log_path.exists():
        log_text = build_log_path.read_text(encoding="utf-8", errors="replace")

    missing = re.findall(r"No matching package to install: '([^']+)'", log_text)
    missing += re.findall(r"nothing provides ([^\s]+) needed by", log_text)

    if not missing:
        print("[register-missing-deps] no missing packages found")
        return

    # ROS 依赖名两级校验：log 里的 ros-<distro>-<name> 必须真实存在——
    #   Tier 1: ros-projects.list（SIG 源已有）
    #   Tier 2: 仅 ros-upstream.list（rosdistro 真实存在、SIG 未移植）→ 放行，
    #           注册时自动从 upstream 清单补 URL（递归构建 SIG 未覆盖的包）
    #   两级都查不到 = spec 幻觉依赖名，整体拒绝并提示修 spec。
    #   任一条幻觉名即拒绝全部，避免部分注册掩盖真正的修法。
    from ros_dep_guard import (  # noqa: E402
        format_invalid_report, lookup_ros_dep, lookup_upstream_dep,
        split_ros_name, suggest_ros_names,
    )
    from analyze_ros_deps import load_projects, load_upstream  # noqa: E402
    upstream_urls: dict[str, str] = {}  # rpm 包名 → upstream 清单 URL（注册时补 url 用）
    bad: dict[str, list[str]] = {}
    bad_distro = ""
    for rpm_pkg in missing:
        ros_parts = split_ros_name(rpm_pkg)
        if not ros_parts:
            continue
        ros_distro, ros_name = ros_parts
        projects = load_projects(ros_distro)
        if projects and lookup_ros_dep(ros_name, projects) is None:
            upstream = load_upstream(ros_distro)
            up_key = lookup_upstream_dep(ros_name, upstream)
            if up_key is not None:
                upstream_urls[rpm_pkg] = upstream[up_key][0]
                continue
            bad[ros_name] = suggest_ros_names(ros_name, projects)
            bad_distro = ros_distro
    if bad:
        print(f"[register-missing-deps] ERROR: 拒绝注册。\n"
              f"{format_invalid_report(bad, bad_distro)}", file=sys.stderr)
        sys.exit(3)

    reg_path = sd / "dep_registry.json"
    reg = json.loads(reg_path.read_text(encoding="utf-8")) if reg_path.exists() else {}

    added = []
    for rpm_pkg in missing:
        # 去掉 python3-/python- 前缀还原 pypi/pkg 名
        pkg_name = rpm_pkg.removeprefix("python3-").removeprefix("python-")
        # GAV / mvn(...) 名归一化（mvn(org.jspecify:jspecify) → jspecify）
        pkg_name = rpm_name_from_gav(pkg_name)
        # 构建工具链不得注册为依赖
        if is_toolchain(pkg_name):
            print(f"[register-missing-deps] skip toolchain: {pkg_name}")
            continue
        constraint = _extract_constraint(log_text, rpm_pkg)
        if pkg_name not in reg:
            # 新条目不带 chroots 键：evaluate 阶段 chroot 无关（§8.1），
            # per-chroot 状态由构建阶段的 step_supervisor 按需建立；
            # 已有条目走整字典读-改-写，chroots 等未知键天然保留。
            # url：ROS upstream 清单命中的依赖自动带出源码仓库地址
            reg[pkg_name] = {
                "url": upstream_urls.get(rpm_pkg, ""),
                "constraint": constraint,
                "status": "pending_evaluate",
                "required_by": args.pkg,
            }
            if rpm_pkg in upstream_urls:
                # 对齐 ros_prep deep 模式的注册形态：supervisor 对 lang=ros 的 dep
                # 跳过普通 evaluate，注册即 evaluate_done
                reg[pkg_name]["lang"] = "ros"
            added.append(pkg_name)
        elif constraint and not reg[pkg_name].get("constraint"):
            # 补充已有条目缺失的 constraint
            reg[pkg_name]["constraint"] = constraint

    reg_path.write_text(json.dumps(reg, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[register-missing-deps] added: {added}")


if __name__ == "__main__":
    main()
