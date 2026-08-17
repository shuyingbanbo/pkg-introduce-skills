#!/usr/bin/env python3
"""
ROS 引包预检：定位 + gate 判定 + manifest + 伪 gate_result（ROS 链第一步）

替代 run_check+run_gate 在 ROS 语境的角色，一个脚本完成：

  1. 定位（三级）：ros-projects.list（SIG 源已有）→ ros-upstream.list
     （rosdistro 真实存在、SIG 未移植）→ 用户显式 URL（自研包）；
     用户显式 url/version 参数优先级最高
  2. sibling 展开（同仓库其他包，EUR 按仓库整体构建）
  3. gate 判定：cascade 查询 ros-humble-<pkg>（EUR → 官方源 → gitcode）
     → 官方已有且版本满足 = reuse；目标版本更新 = upgrade；没有 = introduce_new
  4. 依赖分拣（复用 analyze_ros_deps.classify_deps，SIG 清单 + rosdistro 全量）：
     - upstream 清单命中的依赖（SIG 未移植）→ 自动 register-dep 注册递归构建
     - SIG 清单内的缺口：explicit → missing_deps_<pkg>.txt（终止+重提闭环）；
       deep → register-dep 注册（lang=ros，走现有 build 链）
     - 官方已有的依赖 → manifest 的 official_deps（BuildRequires 候选）
  5. 产出三样：
     - ros_pkg_manifest.json（含 tier / target_version / repo_branch）
     - 伪 gate_result_<pkg>.json（decision=introduce_new, lang=ros, version=目标版本）
     - missing_deps_<pkg>.txt（explicit 缺口）

伪 gate_result 是融入方案的支点：supervisor 的 gate_valid 检查只认
decision ∈ (introduce_new, reuse_*, ...) 且 overall_status=done，伪 gate_result
让 build/fix 整条链零改动复用。

用法：
  python3 ros_prep.py --pkg <pkgname> --session-dir <sd>
  python3 ros_prep.py --pkg <pkgname> --session-dir <sd> --deep
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
# 复用 analyze_ros_deps（解析/分拣）与 cascade_package_check（EUR/官方源查询）
BUILD_RPM_SCRIPTS = SCRIPT_DIR.parents[1] / "build-rpm" / "scripts"
for _p in (str(BUILD_RPM_SCRIPTS), str(SCRIPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from analyze_ros_deps import (  # noqa: E402
    classify_deps, load_projects, load_remap, load_upstream,
)
from cascade_package_check import check_package_existence  # noqa: E402

# 官方 ROS 基座包（ROS SIG repo 提供，永不注册/报缺口，直接 BuildRequires）
ROS_BASE_PKGS = {
    "ament_cmake", "ament_cmake_core", "ament_cmake_export_dependencies",
    "ament_cmake_python", "ament_lint_auto", "ament_lint_common",
    "ament_package", "ament_index_python", "ros_workspace", "rcutils",
    "rclpy", "rosidl_cmake", "rosidl_default_generators", "rosidl_default_runtime",
    "rosidl_runtime_c", "rosidl_typesupport_interface", "python3_rosdistro",
    "urdfdom", "console_bridge", "tinyxml_vendor", "eigen3_cmake_module",
}


def _norm_candidates(pkg: str) -> list[str]:
    """包名候选（优先级从高到低）：完整名归一化在前，剥 ros-humble-/ros2- 前缀在后。

    `-`/`_` 归一化查表：用户可能把 RPM 名（ros-humble-x）或变体输进来。
    注意 ros2-numpy 这类上游包名本身就带 ros2 前缀，盲剥会变成 numpy，
    导致清单误配、gate 对不存在的 "numpy" 做判定、报告显示错乱——
    必须先按完整名查两级清单，查不到再尝试剥前缀。
    """
    full = pkg.strip().replace("_", "-")
    cands = [full]
    for pfx in ("ros-humble-", "ros2-"):
        if full.startswith(pfx) and full[len(pfx):]:
            cands.append(full[len(pfx):])
    return cands


def _read_session(sd: Path) -> dict:
    path = sd / "session.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _cmp_version(listed: str, official: str) -> int:
    """简单版本比较：≥ 返回 1，< 返回 -1，无法比较返回 0。"""
    try:
        def _k(v):
            return [int(x) if x.isdigit() else x for x in v.replace("-", ".").split(".")]
        a, b = _k(listed), _k(official)
        return (a > b) - (a < b)
    except Exception:
        return 0


def _cascade_query(pkg_rpm: str, chroot: str, session: dict,
                   version: str = "", lang: str = "ros") -> dict:
    """复用普通链的 4 级级联查询判定包的存在状态。

    lang="ros" 用于 ros-humble-<pkg> 命名归一；unresolved 非 ROS 依赖
    （python3-transforms3d 等已是 RPM 名）传 lang="" 按原名直查。
    """
    import os
    copr_url = session.get("copr_url", os.environ.get("COPR_FRONTEND_URL",
                                                      "http://copr-frontend:5000"))
    try:
        return check_package_existence(
            pkg_rpm, lang=lang, version=version, requirement="",
            target=chroot, copr_url=copr_url,
            copr_owner=session.get("copr_owner", ""),
            copr_project=session.get("copr_project", ""),
            copr_login=session.get("copr_login", ""),
            copr_token=session.get("copr_token", ""),
        )
    except Exception as exc:
        # 查询失败保守处理：按"未找到"走引入链（构建失败由修复循环兜底）
        print(f"[ros_prep] WARN cascade query failed for {pkg_rpm}: {exc}", file=sys.stderr)
        return {"decision": "introduce_new", "level": 0}


def _is_official(decision: str) -> bool:
    return decision in ("reuse_official", "reuse_eur_srpm", "reuse_copr_project",
                        "reuse_additional_repo")


def main() -> int:
    parser = argparse.ArgumentParser(description="ROS 引包预检")
    parser.add_argument("--pkg", required=True, help="ROS 包名")
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--ros-distro", default="humble", help="ROS 发行版（默认 humble）")
    parser.add_argument("--deep", action="store_true", default=True,
                        help="（默认开启，兼容保留）deep 模式：缺口自动注册依赖")
    parser.add_argument("--no-deep", dest="deep", action="store_false",
                        help="关闭递归：缺口依赖仅记入报告，不注册引入")
    args = parser.parse_args()

    sd = Path(args.session_dir)
    session = _read_session(sd)
    ros_distro = session.get("ros_distro") or args.ros_distro
    chroot = session.get("copr_chroot", "")

    pkg = _norm_candidates(args.pkg)[0]
    pkg_dir = sd / "pkgs" / pkg
    pkg_dir.mkdir(parents=True, exist_ok=True)
    gate_path = pkg_dir / f"gate_result_{pkg}.json"
    manifest_path = pkg_dir / "ros_pkg_manifest.json"
    missing_path = pkg_dir / f"missing_deps_{pkg}.txt"

    def _fail(reason: str) -> int:
        _write_json(gate_path, {
            "pkgname": pkg, "lang": "ros", "version": "",
            "overall_status": "failed", "error": reason, "result": None,
        })
        print(json.dumps({"status": "failed", "reason": reason}, ensure_ascii=False))
        return 1

    # ── 1. 定位（两级清单 + 用户参数）───────────────────────────────────────
    #   Tier 1: ros-projects.list（SIG 源已有）
    #   Tier 2: ros-upstream.list（rosdistro 真实存在、SIG 未移植 → 可构建）
    #   Tier 3: 都不在 → 用户必须显式提供 URL（自研/私有包）
    # 用户显式参数（session.json 的 upstream_url/version）优先级最高。
    projects = load_projects(ros_distro)
    if not projects:
        return _fail(f"ros-projects.list 为空或不存在（distro={ros_distro}）")
    upstream = load_upstream(ros_distro)
    # 完整名优先、剥前缀兜底：两级清单命中谁就用谁（ros2-numpy 这类上游名
    # 自带 ros2- 前缀，盲剥会变成 numpy，导致清单误配与报告显示错乱）
    hit = next((n for n in _norm_candidates(args.pkg)
                if n in projects or n in upstream), None)
    if hit and hit != pkg:
        pkg = hit
        pkg_dir = sd / "pkgs" / pkg
        pkg_dir.mkdir(parents=True, exist_ok=True)
        gate_path = pkg_dir / f"gate_result_{pkg}.json"
        manifest_path = pkg_dir / "ros_pkg_manifest.json"
        missing_path = pkg_dir / f"missing_deps_{pkg}.txt"
    user_url = (session.get("upstream_url") or "").strip()
    user_ver = (session.get("version") or "").strip()

    repo_branch = ""
    if pkg in projects:
        tier = "sig"
        repo_url, status, listed_ver = projects[pkg]
        siblings = sorted(k for k, v in projects.items() if v[0] == repo_url and k != pkg)
    elif pkg in upstream:
        tier = "upstream"
        repo_url, repo_branch, status, listed_ver = upstream[pkg]
        siblings = sorted(k for k, v in upstream.items() if v[0] == repo_url and k != pkg)
    elif user_url:
        tier = "user"
        repo_url, status, listed_ver = user_url, "user_provided", ""
        siblings = []
    else:
        return _fail(
            f"包在 SIG 清单和 rosdistro 全量清单中都不存在: {pkg}"
            f"（请核对包名/ros_distro；若是自研 ROS 包，请同时提供上游 git URL 重新提交）")

    # 用户显式 URL 覆盖清单定位（fork/自研仓库）；版本优先级：用户参数 > 清单版本
    if user_url:
        repo_url = user_url
    target_ver = user_ver or listed_ver

    # ── 2. gate 判定（主包）────────────────────────────────────────────────
    pkg_rpm = f"ros-{ros_distro}-{pkg}"
    cascade = _cascade_query(pkg_rpm, chroot, session, version=target_ver)
    decision = cascade["decision"]
    match = cascade.get("match") or {}
    official_ver = match.get("version", "")

    # decision → 伪 gate_result 映射
    if _is_official(decision):
        # 官方已有：目标版本更新 → upgrade（以官方 spec 为参考升级构建）；
        # 官方版本 ≥ 目标版本（含目标版本为空）→ reuse（官方已满足，写
        # goal_achieved 直接 done）
        if target_ver and official_ver and _cmp_version(target_ver, official_ver) > 0:
            gate_decision = "introduce_new_with_ref"
            disposition = "upgrade"
            gate_reason = (f"官方已有 ros-{ros_distro}-{pkg} {official_ver}，"
                           f"目标版本 {target_ver} 更新，以官方 spec 为参考升级构建")
        else:
            gate_decision = "reuse_official"
            disposition = "reuse"
            gate_reason = f"官方已有 ros-{ros_distro}-{pkg} {official_ver or target_ver}，直接复用"
    elif decision == "introduce_new_with_ref":
        gate_decision = "introduce_new_with_ref"
        disposition = "introduce_new"
        ref = cascade.get("reference") or {}
        gate_reason = (f"以参考 spec 为起点构建："
                       f"{ref.get('source', 'unknown')} {ref.get('gitcode_repo', '')}")
    else:
        gate_decision = "introduce_new"
        disposition = "introduce_new"
        gate_reason = "官方源未找到，从头构建"

    # ── 3. 依赖分拣（package.xml 可能尚缺：deep 模式由递归 evaluate 补全）────
    # sources/ 就绪后由 ros_fetch 前的 analyze 补充；此处依赖信息来自
    # 已有 manifest 或解析（若源码已在 session 中）。
    manifest = {
        "pkgname": pkg,
        "ros_distro": ros_distro,
        "tier": tier,
        "repo_url": repo_url,
        "repo_branch": repo_branch,
        "repo_status": status,
        "listed_version": listed_ver,
        "target_version": target_ver,
        "siblings": siblings,
        "gate_decision": gate_decision,
        "official_deps": [],
        "missing_deps": [],
        "registered_deps": [],
        "official_deps_rpm": [],
    }

    # 源码已在 session（重跑/断点续跑）→ 现场解析依赖
    src_candidates = [sd / "sources" / pkg, sd / "sources" / repo_url.split("/")[-1]]
    src_dir = next((s for s in src_candidates if s.is_dir()), None)
    if src_dir is not None:
        try:
            from analyze_ros_deps import parse_package_xml
            parsed = parse_package_xml(str(src_dir), pkg)
            if parsed["found"]:
                remap = load_remap()
                cls = classify_deps(parsed["deps"] + parsed["buildtool_deps"],
                                    projects, remap, upstream=upstream)
                # SIG 清单内依赖 + rosdistro 全量清单内依赖（SIG 未移植）统一处理；
                # 区别在于注册时 URL 来源与是否要求 --deep
                for d in sorted(set(cls["ros_deps"]) | set(cls["ros_deps_upstream"])):
                    # ros_deps 已归一化为连字符（清单规范名），ROS_BASE_PKGS 是
                    # 下划线命名，比较前统一
                    if d.replace("-", "_") in ROS_BASE_PKGS:
                        manifest["official_deps_rpm"].append(f"ros-{ros_distro}-{d}")
                        continue
                    c = _cascade_query(f"ros-{ros_distro}-{d}", chroot, session)
                    if _is_official(c["decision"]):
                        manifest["official_deps"].append(d)
                        manifest["official_deps_rpm"].append(f"ros-{ros_distro}-{d}")
                        continue
                    # upstream 命中的依赖（SIG 未移植）自动注册递归构建，无论
                    # 是否 --deep；SIG 清单内的缺口维持原语义（--deep 才注册）
                    is_upstream_dep = d in cls["ros_deps_upstream"]
                    if is_upstream_dep or args.deep:
                        dep_url = (upstream[d][0] if is_upstream_dep
                                   else projects[d][0])
                        rc = subprocess.run(
                            [sys.executable, str(SCRIPT_DIR / "register-dep.py"),
                             "--session-dir", str(sd), "--pkg", f"ros-{ros_distro}-{d}",
                             "--url", dep_url, "--lang", "ros",
                             "--required-by", pkg],
                            capture_output=True, text=True, timeout=30,
                        )
                        if rc.returncode == 0:
                            manifest["registered_deps"].append(d)
                        else:
                            manifest["missing_deps"].append(d)
                            print(f"[ros_prep] WARN register {d} failed: "
                                  f"{rc.stderr.strip()[:200]}", file=sys.stderr)
                    else:
                        manifest["missing_deps"].append(d)
                # 非 ROS 依赖（unresolved：两级清单与 remap 都未命中，多为
                # python3-* 等系统/Python 包，名字通常即 RPM 名）：实证 provider，
                # 所有源都没有的缺口在 deep 模式注册为待引入依赖走普通链
                # （resolve_upstream 经 PyPI 等 API 定位上游），非 deep 记入
                # missing_deps 报告提示。旧实现直接丢弃 unresolved，导致
                # spec 带出的 Requires 到 CI 阶段才暴露（白烧一轮构建）。
                for d in cls["unresolved"]:
                    c = _cascade_query(d, chroot, session, lang="")
                    if _is_official(c["decision"]):
                        continue
                    if args.deep:
                        rc = subprocess.run(
                            [sys.executable, str(SCRIPT_DIR / "register-dep.py"),
                             "--session-dir", str(sd), "--pkg", d,
                             "--required-by", pkg],
                            capture_output=True, text=True, timeout=30,
                        )
                        if rc.returncode == 0:
                            manifest["registered_deps"].append(d)
                        else:
                            manifest["missing_deps"].append(d)
                            print(f"[ros_prep] WARN register {d} failed: "
                                  f"{rc.stderr.strip()[:200]}", file=sys.stderr)
                    else:
                        manifest["missing_deps"].append(d)
        except Exception as exc:
            print(f"[ros_prep] WARN dep analysis skipped: {exc}", file=sys.stderr)

    _write_json(manifest_path, manifest)

    # explicit 缺口清单（随任务结束写入报告，前端渲染可点击 tag）。
    # missing_deps 里 ROS 依赖是清单短名（补 ros-<distro>- 前缀），
    # unresolved 非 ROS 依赖已是完整 RPM 名（原样写入）
    if manifest["missing_deps"]:
        ros_names = set(projects) | set(upstream)
        missing_path.write_text("\n".join(
            f"ros-{ros_distro}-{d}" if d in ros_names else d
            for d in manifest["missing_deps"]
        ) + "\n", encoding="utf-8")

    # ── 4. 伪 gate_result ───────────────────────────────────────────────────
    # reuse 主包：直接置 goal_achieved（现有链 reuse 语义由 pkg-evaluator 写，
    # ROS 链没有该 agent，这里代写）
    if gate_decision == "reuse_official":
        wf_files = list(sd.glob("workflow_*.json"))
        if wf_files:
            try:
                wf = json.loads(wf_files[0].read_text(encoding="utf-8"))
                wf["goal_achieved"] = True
                wf.setdefault("reused_pkgs", []).append(pkg)
                wf_files[0].write_text(json.dumps(wf, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
            except Exception:
                pass

    _write_json(gate_path, {
        "pkgname": pkg,
        "lang": "ros",
        "version": target_ver,
        "overall_status": "done",
        # 结构化处置：reuse（官方已满足）/ upgrade（官方有旧版，构建新版）/
        # introduce_new（官方没有，新构建）——job_runner 回写 Redis 供前端展示
        "disposition": disposition,
        "result": {
            "lang": "ros",
            "version": target_ver,
            "decision": gate_decision,
            "reason": gate_reason,
            "repo_url": repo_url,
        },
        "reference": cascade.get("reference") or {},
        "steps": {"existing_check": {"status": "done", "decision": gate_decision,
                                     "reason": gate_reason}},
    })

    print(json.dumps({
        "status": "done",
        "pkgname": pkg,
        "tier": tier,
        "gate_decision": gate_decision,
        "target_version": target_ver,
        "official_deps": manifest["official_deps"],
        "missing_deps": manifest["missing_deps"],
        "registered_deps": manifest["registered_deps"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
