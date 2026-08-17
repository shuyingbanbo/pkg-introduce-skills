#!/usr/bin/env python3
"""build-rpm flow orchestrator（COPR 模式）。

只负责两件事：
  --phase precheck  : 跑 pre_check_deps.py，输出依赖预检结果
  CI 验证           : COPR build 成功后调 run_ci_check.py
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from copr_client import parse_chroots, primary_chroot

SCRIPTS_DIR = Path(__file__).resolve().parent
PRE_CHECK_SCRIPT = SCRIPTS_DIR / "pre_check_deps.py"

PKG_INTRODUCE_SCRIPTS = SCRIPTS_DIR.parents[2] / "pkg-introduce" / "scripts"
CI_CHECK_SCRIPT = PKG_INTRODUCE_SCRIPTS / "run_ci_check.py"


class FlowError(RuntimeError):
    def __init__(self, reason: str, failure_type: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.failure_type = failure_type


def run_command(command: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, check=False, cwd=cwd)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def result_path(pkgname: str, reports_dir: Path) -> Path:
    return reports_dir / "build_rpm_result.json"


def run_precheck(pkgname: str, lang: str, source_dir: str, reports_dir: Path) -> tuple[int, Path, str, str]:
    precheck_json = reports_dir / f"pre_check_{pkgname}.json"
    reports_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(PRE_CHECK_SCRIPT), pkgname, lang, source_dir,
           "-o", str(precheck_json)]
    proc = run_command(cmd)
    return proc.returncode, precheck_json, proc.stdout.strip(), proc.stderr.strip()


def reconcile_pending_with_registry(pending: list[dict], session_dir: Path) -> list[dict]:
    """与 dep_registry 对账：已 build_done 的依赖（本项目仓已构建成功）从 pending 剔除。

    precheck 的 pending 基于官方源状态（官方源不会因本次引入而变化），
    不与 registry 对账会导致 dep_needed 与 supervisor"依赖已就绪"结论矛盾，
    build_main 空转死循环。
    """
    if not pending:
        return pending
    dep_reg_path = session_dir / "dep_registry.json"
    if not dep_reg_path.exists():
        return pending
    try:
        dep_reg = json.loads(dep_reg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return pending
    done = {k for k, v in dep_reg.items()
            if isinstance(v, dict) and v.get("status") == "build_done"}
    if not done:
        return pending
    skipped = [d.get("name") or d.get("dep", "") for d in pending
               if (d.get("name") or d.get("dep", "")) in done]
    if skipped:
        print(f"[build-rpm] precheck 对账: {skipped} 已 build_done，从 pending 剔除",
              file=sys.stderr)
    return [d for d in pending if (d.get("name") or d.get("dep", "")) not in done]


def build_result_payload(
    *,
    pkgname: str,
    lang: str,
    version: str,
    requested_version: str,
    depth: int,
    status: str,
    action: str,
    reason: str,
    precheck_summary: dict[str, Any],
    dependency_resolution: dict[str, Any],
    artifacts: dict[str, str],
    failure_type: str = "",
    failure_reason: str = "",
    build: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "pkgname": pkgname,
        "lang": lang,
        "version": version,
        "requested_version": requested_version,
        "depth": depth,
        "status": status,
        "action": action,
        "reason": reason,
        "build": build or {},
        "dependency_resolution": dependency_resolution,
        "precheck": precheck_summary,
        "artifacts": artifacts,
        "failure": {
            "failure_type": failure_type,
            "failure_reason": failure_reason,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="build-rpm flow orchestrator（COPR 模式）")
    parser.add_argument("pkgname")
    parser.add_argument("lang")
    parser.add_argument("upstream_url")
    parser.add_argument("version")
    parser.add_argument("--depth", type=int, default=0)
    parser.add_argument("--session-dir", default="", help="session 根目录，用于路径解析和 CI 验证")
    parser.add_argument("--source-dir", default="")
    parser.add_argument("--build-state-dir", default="./build_state")
    parser.add_argument("--reports-dir", default="./reports")
    parser.add_argument("-o", "--output", default="")
    parser.add_argument(
        "--phase",
        default="precheck",
        choices=["precheck", "ci"],
        help="precheck: 跑依赖预检；ci: 只跑 CI 门禁验证",
    )
    parser.add_argument(
        "--precheck-json",
        default="",
        help="已有的 pre_check 结果文件路径（跳过内部 pre_check 调用）",
    )
    args = parser.parse_args()

    session_dir = Path(args.session_dir).resolve() if args.session_dir else Path.cwd()

    # 从 session.json 读取 COPR 配置，设置环境变量供 pre_check_deps.py 使用
    session_json_path = session_dir / "session.json"
    if session_json_path.exists():
        import json as _json
        try:
            session = _json.loads(session_json_path.read_text(encoding="utf-8"))
            for key, env_var in [
                ("copr_chroot",  "COPR_CHROOT"),
                ("copr_chroots", "COPR_CHROOTS"),
                ("copr_url",     "COPR_FRONTEND_URL"),
                ("copr_owner",   "COPR_OWNER"),
                ("copr_project", "COPR_PROJECT"),
                ("copr_login",   "COPR_API_LOGIN"),
                ("copr_token",   "COPR_API_TOKEN"),
            ]:
                val = session.get(key, "")
                if isinstance(val, list):
                    val = ",".join(str(v) for v in val)
                if val and not os.environ.get(env_var):
                    os.environ[env_var] = val
        except Exception:
            pass

    # 多 chroot：本轮可提交集合优先级 COPR_BUILD_CHROOTS > COPR_CHROOTS > COPR_CHROOT，
    # 归一化后回写环境（COPR_CHROOT=主 chroot），供 precheck / CI / 提交流程消费。
    # 单 chroot 时解析结果即原单值，行为不变。
    build_chroots: list[str] = []
    for _var in ("COPR_BUILD_CHROOTS", "COPR_CHROOTS", "COPR_CHROOT"):
        build_chroots = parse_chroots(os.environ.get(_var, ""))
        if build_chroots:
            break
    if build_chroots:
        os.environ["COPR_CHROOT"] = primary_chroot(build_chroots)
        if not os.environ.get("COPR_CHROOTS"):
            os.environ["COPR_CHROOTS"] = ",".join(build_chroots)

    def _abs(p: str, default: str = "") -> Path:
        s = p or default
        return (session_dir / s).resolve() if s and not Path(s).is_absolute() else Path(s).resolve()

    reports_dir   = _abs(args.reports_dir, "reports")
    output_path   = _abs(args.output) if args.output else result_path(args.pkgname, reports_dir)
    source_dir    = str(_abs(args.source_dir) if args.source_dir else session_dir / "sources" / args.pkgname)
    artifacts: dict[str, str] = {}

    # ── phase=ci：只跑 CI 门禁验证 ───────────────────────────────────────────
    if args.phase == "ci":
        if not CI_CHECK_SCRIPT.exists():
            print(f"[WARN] CI check script not found: {CI_CHECK_SCRIPT}")
            return 0
        ci_proc = subprocess.run(
            [sys.executable, str(CI_CHECK_SCRIPT),
             "--pkgs", args.pkgname,
             "--session-dir", str(session_dir),
             "--reports-dir", str(reports_dir.parent / "pkgs" / args.pkgname)],
            capture_output=True, text=True,
        )
        if ci_proc.returncode != 0:
            payload = build_result_payload(
                pkgname=args.pkgname, lang=args.lang, version=args.version,
                requested_version=args.version, depth=args.depth,
                status="ci_failed", action="blocked", reason="CI check failed",
                precheck_summary={}, dependency_resolution={}, artifacts=artifacts,
                failure_type="non_retryable_build_failure",
                failure_reason=ci_proc.stdout + ci_proc.stderr,
            )
            write_json(output_path, payload)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 1
        return 0

    # ── phase=precheck：跑 pre_check_deps.py ─────────────────────────────────
    try:
        precheck_rc, precheck_json_path, precheck_stdout, precheck_stderr = run_precheck(
            args.pkgname, args.lang, source_dir, reports_dir,
        )
        artifacts["precheck_json"] = str(precheck_json_path)

        if precheck_rc not in (0, 2, 3):
            payload = build_result_payload(
                pkgname=args.pkgname, lang=args.lang, version=args.version,
                requested_version=args.version, depth=args.depth,
                status="failed", action="blocked", reason="pre_check_deps failed",
                precheck_summary={}, dependency_resolution={}, artifacts=artifacts,
                failure_type="retryable_dependency_resolution_failure",
                failure_reason=precheck_stderr or precheck_stdout or "pre_check_deps failed",
            )
            write_json(output_path, payload)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 1

        precheck = read_json(precheck_json_path)
        precheck_summary = {
            "resolved_count": len(precheck.get("resolved") or []),
            "pending_count":  len(precheck.get("pending") or []),
            "needs_ai_count": len(precheck.get("needs_ai") or []),
            "blocked_count":  len(precheck.get("blocked") or []),
        }
        blocked  = list(precheck.get("blocked") or [])
        pending  = list(precheck.get("pending") or [])
        needs_ai = list(precheck.get("needs_ai") or [])

        # 已 build_done 的依赖不算 pending（详见 reconcile_pending_with_registry docstring）
        pending = reconcile_pending_with_registry(pending, session_dir)
        precheck_summary["pending_count"] = len(pending)

        if needs_ai:
            payload = build_result_payload(
                pkgname=args.pkgname, lang=args.lang, version=args.version,
                requested_version=args.version, depth=args.depth,
                status="dep_needed", action="needs_ai",
                reason=f"{len(needs_ai)} dependencies need AI web search to resolve upstream URL",
                precheck_summary=precheck_summary,
                dependency_resolution={
                    "precheck_status": "needs_ai",
                    "needs_ai_deps": [d.get("name") or d.get("dep", "") for d in needs_ai],
                },
                artifacts=artifacts,
            )
            write_json(output_path, payload)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 3

        if blocked:
            payload = build_result_payload(
                pkgname=args.pkgname, lang=args.lang, version=args.version,
                requested_version=args.version, depth=args.depth,
                status="failed", action="blocked", reason="precheck blocked dependencies",
                precheck_summary=precheck_summary,
                dependency_resolution={"precheck_status": "blocked", "recursion_status": "blocked"},
                artifacts=artifacts,
                failure_type="retryable_dependency_resolution_failure",
                failure_reason="precheck blocked dependencies",
            )
            write_json(output_path, payload)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 1

        # vendor_mode（旧字段，纯 go/rust/nodejs 超阈值）或主语言在 vendor_langs 中
        # （混合包：主语言自身被 vendor 时其 pending 才豁免；python 主包不豁免）
        vendor_mode = precheck.get("vendor_mode", False) \
            or args.lang.lower() in (precheck.get("vendor_langs") or [])
        if pending and not vendor_mode:
            payload = build_result_payload(
                pkgname=args.pkgname, lang=args.lang, version=args.version,
                requested_version=args.version, depth=args.depth,
                status="dep_needed", action="dep_needed",
                reason=f"{len(pending)} pending dependencies require recursive introduction",
                precheck_summary=precheck_summary,
                dependency_resolution={
                    "precheck_status": "pending_found",
                    "recursion_status": "deferred_to_skill",
                    "resolved_count": precheck_summary["resolved_count"],
                    "pending_count":  precheck_summary["pending_count"],
                    "blocked_count":  precheck_summary["blocked_count"],
                    "pending_deps":   [d.get("name") or d.get("dep", "") for d in pending],
                },
                artifacts=artifacts,
            )
            write_json(output_path, payload)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 2

        payload = build_result_payload(
            pkgname=args.pkgname, lang=args.lang, version=args.version,
            requested_version=args.version, depth=args.depth,
            status="precheck_done", action="precheck_passed",
            reason="all dependencies resolved, ready for spec generation",
            precheck_summary=precheck_summary,
            dependency_resolution={"precheck_status": "resolved_only", "recursion_status": "not_needed"},
            artifacts=artifacts,
        )
        write_json(output_path, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    except FlowError as exc:
        payload = build_result_payload(
            pkgname=args.pkgname, lang=args.lang, version=args.version,
            requested_version=args.version, depth=args.depth,
            status="failed", action="blocked", reason=exc.reason,
            precheck_summary={}, dependency_resolution={}, artifacts=artifacts,
            failure_type=exc.failure_type, failure_reason=exc.reason,
        )
        write_json(output_path, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
