#!/usr/bin/env python3
"""Phase 2：引入门禁（COPR 模式，无 Docker）

前提：run_check.py 已成功跑完（overall_status=done），
      check_result_<pkgname>.json 中有确认的 lang/version。

步骤：existing_check（4 级级联：EUR → 官方源 → gitcode → 全新）

gate 阶段完成决策后动作：
  - reuse_eur_srpm：下载 EUR SRPM，提取 spec 到 reference
  - introduce_new_with_ref：拉取 gitcode 参考源到 reference

输出 gate_result_<pkgname>.json，格式：
  overall_status: "done" | "failed"
  result.decision: "reuse_eur_srpm" | "reuse_official" | "introduce_new_with_ref" | "introduce_new"

exit codes:
  0  overall_status=done
  1  overall_status=failed
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

_PKG_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_PKG_SCRIPTS))

from cascade_package_check import check_package_existence  # noqa: E402

GATE_STEPS = ["existing_check"]


def _get_project_chroots(copr_url: str, owner: str, project: str,
                          login: str, token: str) -> list:
    """查询 COPR project 的 chroot 列表，返回 x86_64 chroot 名（优先）。"""
    import base64, urllib.request, urllib.error
    creds = base64.b64encode(f"{login}:{token}".encode()).decode()
    url = f"{copr_url.rstrip('/')}/api_3/project?ownername={owner}&projectname={project}"
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {creds}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
        chroots = list(d.get("chroot_repos", {}).keys())
        x86 = [c for c in chroots if c.endswith("-x86_64")]
        return x86 if x86 else chroots
    except Exception:
        return []


def _save(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _already_done(step_data: dict) -> bool:
    return step_data.get("status") in ("done", "skipped")


def _download_eur_srpm(match_info: dict, pkgname: str, pkgs_dir: Path, srpms_dir: Path) -> None:
    """下载 EUR SRPM 并提取 spec 到 reference 目录。"""
    srpm_url = match_info["srpm_url"]
    srpm_file = match_info.get("srpm_file", f"{pkgname}.src.rpm")
    srpms_dir.mkdir(parents=True, exist_ok=True)
    srpm_path = srpms_dir / srpm_file
    if srpm_path.exists():
        return
    try:
        print(f"[gate] 下载 EUR SRPM: {srpm_url}", file=sys.stderr)
        subprocess.run(["curl", "-sL", "-o", str(srpm_path), srpm_url], check=True, timeout=120)
        print(f"[gate] SRPM 已保存: {srpm_path}", file=sys.stderr)
        ref_dir = pkgs_dir / "reference"
        ref_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            f"cd {shlex.quote(str(ref_dir))} && rpm2cpio {shlex.quote(str(srpm_path))} | cpio -idmv '*.spec' 2>/dev/null",
            shell=True, timeout=30,
        )
        print(f"[gate] spec 已提取到: {ref_dir}", file=sys.stderr)
    except Exception as exc:
        print(f"[gate] WARN: 下载/提取 SRPM 失败: {exc}", file=sys.stderr)


def _fetch_reference(match_info: dict, pkgname: str, pkgs_dir: Path) -> None:
    """从 gitcode src-openeuler 拉取参考 spec/yaml/patches。"""
    repo_name = match_info["repo_name"]
    target_branch = match_info.get("target_branch", "")
    ref_dir = pkgs_dir / "reference"
    if (ref_dir / f"{repo_name}.spec").exists() or (ref_dir / f"{pkgname}.spec").exists():
        return
    ref_result = ref_dir.parent / "reference_result.json"
    try:
        fetch_script = Path(__file__).resolve().parent / "../../build-rpm/scripts/fetch_reference_spec.py"
        cmd = ["python3", str(fetch_script), "--pkgname", repo_name,
               "--output-dir", str(ref_dir), "--output-json", str(ref_result)]
        if target_branch:
            cmd += ["--target-branch", target_branch]
            print(f"[gate] 拉取参考源: gitcode.com/src-openeuler/{repo_name} (branch={target_branch})",
                  file=sys.stderr)
        else:
            print(f"[gate] 拉取参考源: gitcode.com/src-openeuler/{repo_name}", file=sys.stderr)
        subprocess.run(cmd, check=True, timeout=60)
        print(f"[gate] 参考源已拉取到: {ref_dir}", file=sys.stderr)
    except Exception as exc:
        print(f"[gate] WARN: 拉取参考源失败: {exc}", file=sys.stderr)


def run_gate(args: argparse.Namespace) -> int:
    reports_dir      = Path(args.reports_dir)
    check_report_path = reports_dir / f"check_result_{args.pkg}.json"
    gate_report_path  = reports_dir / f"gate_result_{args.pkg}.json"

    if not check_report_path.exists():
        print(f"[ERROR] check_result_{args.pkg}.json not found; run run_check.py first",
              file=sys.stderr)
        return 1

    check = json.loads(check_report_path.read_text(encoding="utf-8"))
    if check.get("overall_status") != "done":
        print(f"[ERROR] check phase not done (status={check.get('overall_status')})",
              file=sys.stderr)
        return 1

    cr      = check.get("result") or {}
    lang    = args.lang    or cr.get("lang", "")
    version = args.version or cr.get("version", "")

    if not lang or not version:
        print(f"[ERROR] lang or version missing (lang={lang!r}, version={version!r})",
              file=sys.stderr)
        return 1

    # Load or init gate report
    if gate_report_path.exists():
        report = json.loads(gate_report_path.read_text(encoding="utf-8"))
    else:
        report = {
            "pkgname":        args.pkg,
            "lang":           lang,
            "version":        version,
            "overall_status": "pending",
            "steps":          {step: {"status": "pending"} for step in GATE_STEPS},
            "result":         None,
        }

    steps = report["steps"]

    # COPR 凭据（优先参数，其次环境变量）
    copr_url = args.copr_url or os.environ.get("COPR_FRONTEND_URL", "http://copr-frontend:5000")
    owner    = args.copr_owner  or os.environ.get("COPR_OWNER", "")
    project  = args.copr_project or os.environ.get("COPR_PROJECT", "")
    login    = args.copr_login  or os.environ.get("COPR_API_LOGIN", "")
    token    = args.copr_token  or os.environ.get("COPR_API_TOKEN", "")

    # chroot 优先从 session.json 读
    chroot = args.copr_chroot or os.environ.get("COPR_CHROOT", "")
    if not chroot:
        session_json = Path(args.reports_dir).parents[1] / "session.json"
        if not session_json.exists():
            for p in Path(args.reports_dir).parents:
                candidate = p / "session.json"
                if candidate.exists():
                    session_json = candidate
                    break
        if session_json.exists():
            try:
                chroot = json.loads(session_json.read_text(encoding="utf-8")).get("copr_chroot", "")
            except Exception:
                pass
    if not chroot:
        chroots = _get_project_chroots(copr_url, owner, project, login, token)
        chroot  = chroots[0] if chroots else ""

    try:
        if not _already_done(steps["existing_check"]):
            # 4 级级联查找（Level 1-4）：EUR → 官方源 → gitcode → 全新
            cascade_result = check_package_existence(
                args.pkg,
                lang=lang,
                version=version,
                requirement=args.constraint,
                target=chroot,
                copr_url=copr_url,
                copr_owner=owner,
                copr_project=project,
                copr_login=login,
                copr_token=token,
            )
            decision = cascade_result["decision"]
            level = cascade_result["level"]

            # 生成 reason 文本
            match_info = cascade_result.get("match") or {}
            if decision == "reuse_copr_project":
                reason = (
                    f"用户 COPR project 已有构建：{match_info.get('source', '')} "
                    f"version={match_info.get('version', '')}，直接复用"
                )
            elif decision == "reuse_eur_srpm":
                reason = (
                    f"EUR 找到 {match_info.get('eur_owner', '')}/{match_info.get('eur_project', '')} "
                    f"chroot={match_info.get('chroot', '')} "
                    f"version={match_info.get('version', '')}，下载 SRPM 重建"
                )
            elif decision == "reuse_official":
                reason = (
                    f"openEuler 目标版本已有满足要求的包："
                    f"{match_info.get('rpm_name', '')} {match_info.get('version', '')}"
                )
            elif decision == "reuse_additional_repo":
                reason = (
                    f"项目 additional_repos（外挂源）已有满足要求的包："
                    f"{cascade_result.get('rpm_name', '')} "
                    f"{cascade_result.get('version', '')}"
                    f"（{cascade_result.get('source', '')}），直接复用"
                )
            elif decision == "evaluate":
                reason = (
                    f"openEuler 目标版本有包但版本不满足要求："
                    f"{match_info.get('rpm_name', '')} {match_info.get('version', '')}"
                )
            elif decision == "introduce_new_with_ref":
                ref_info = cascade_result.get("reference") or {}
                if ref_info.get("source") == "eur":
                    reason = (
                        f"EUR 有 {ref_info.get('eur_owner', '')}/{ref_info.get('eur_project', '')} "
                        f"version={ref_info.get('version', '')} 但 chroot 不匹配"
                        f"（{ref_info.get('chroot', '')} vs {chroot}），以其 SRPM 为参考重建"
                    )
                else:
                    reason = (
                        f"src-openeuler 仓库存在：{match_info.get('gitcode_repo', '')}，"
                        f"以参考 spec 为起点构建"
                    )
            elif decision == "introduce_new":
                reason = "所有来源均未找到，从头构建"
            else:
                reason = f"decision={decision}"

            steps["existing_check"] = {
                "status":   "done",
                "decision": decision,
                "level":    level,
                "reason":   reason,
                "chroot":   chroot,
                "match":    cascade_result.get("match"),
                "reference": cascade_result.get("reference"),
            }
            _save(report, gate_report_path)

            # ── 决策后动作：下载 SRPM 或拉取参考源 ────────────────────
            session_dir = reports_dir.parent
            pkgs_dir = session_dir / "pkgs" / args.pkg
            srpms_dir = session_dir / "srpms"
            ref_info = cascade_result.get("reference") or {}
            if decision == "reuse_eur_srpm" and match_info.get("srpm_url"):
                _download_eur_srpm(match_info, args.pkg, pkgs_dir, srpms_dir)
            elif decision == "introduce_new_with_ref" and ref_info.get("source") == "eur" \
                    and ref_info.get("srpm_url"):
                # EUR 参考源（chroot 不匹配降级）：下载 SRPM 提取 spec 作参考，
                # 后续走完整构建流程（§3 自动检测 reference/ 下的参考 spec）
                _download_eur_srpm(ref_info, args.pkg, pkgs_dir, srpms_dir)
            elif decision == "introduce_new_with_ref" and match_info.get("repo_name"):
                _fetch_reference(match_info, args.pkg, pkgs_dir)

    except Exception as exc:
        report["overall_status"] = "failed"
        steps["existing_check"] = {"status": "failed", "reason": str(exc)}
        _save(report, gate_report_path)
        print(json.dumps({"status": "failed", "report": str(gate_report_path)},
                         ensure_ascii=False))
        return 1

    all_done = all(s.get("status") in ("done", "skipped") for s in steps.values())
    report["overall_status"] = "done" if all_done else "failed"
    report["result"] = {
        "lang":       lang,
        "version":    version,
        "decision":   steps["existing_check"].get("decision", ""),
        "reason":     steps["existing_check"].get("reason", ""),
        "copr_owner": owner,
        "copr_project": project,
    }
    _save(report, gate_report_path)

    print(json.dumps({"status": report["overall_status"], "report": str(gate_report_path)},
                     ensure_ascii=False))
    return 0 if report["overall_status"] == "done" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="pkg-introduce Phase 2: 引入门禁（COPR 模式）")
    parser.add_argument("--pkg",          required=True)
    parser.add_argument("--url",          default="", dest="upstream_url")
    parser.add_argument("--lang",         default="")
    parser.add_argument("--version",      default="")
    parser.add_argument("--constraint",   default="")
    parser.add_argument("--mode",         default="top-level", choices=["top-level", "dependency"])
    parser.add_argument("--reports-dir",  default="./reports", dest="reports_dir")
    parser.add_argument("--pkg-dir",      default=None, dest="pkg_dir")
    # COPR 凭据（可通过环境变量替代）
    parser.add_argument("--copr-url",     default="", dest="copr_url")
    parser.add_argument("--copr-owner",   default="", dest="copr_owner")
    parser.add_argument("--copr-project", default="", dest="copr_project")
    parser.add_argument("--copr-login",   default="", dest="copr_login")
    parser.add_argument("--copr-token",   default="", dest="copr_token")
    parser.add_argument("--copr-chroot",  default="", dest="copr_chroot",
                        help="目标 chroot（优先级：参数 > session.json > COPR API 查询）")
    args = parser.parse_args()
    if args.pkg_dir:
        args.reports_dir = args.pkg_dir
    return run_gate(args)


if __name__ == "__main__":
    sys.exit(main())
