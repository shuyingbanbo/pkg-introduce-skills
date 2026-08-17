#!/usr/bin/env python3
"""
Run evaluate (check + gate) for a single dep without AI.

Replaces the pkg-evaluator agent for deterministic cases:
  - run_check.py exit 0 → run_gate.py → update dep_registry → done
  - run_check.py exit 2 → needs_ai (fall back to Claude)
  - run_check.py exit 1 → hard failure

Exit codes:
  0 — evaluate completed (status in JSON output is "done" or "failed")
  1 — script error (bad args, file not found)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_PKG_INTRODUCE_SCRIPTS = (
    Path(__file__).resolve().parents[2] / "pkg-introduce" / "scripts"
)
_RUN_CHECK = _PKG_INTRODUCE_SCRIPTS / "run_check.py"
_RUN_GATE = _PKG_INTRODUCE_SCRIPTS / "run_gate.py"
_SCRIPTS_DIR = Path(__file__).resolve().parent
_EVALUATE_DEPS = _SCRIPTS_DIR / "evaluate-deps.py"

from timeline import write_event  # noqa: E402  同目录脚本，直接运行时已入 sys.path


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(session_dir: Path, pkgname: str, mode: str, url: str,
        constraint: str = "", version: str = "",
        no_update_registry: bool = False) -> dict:
    """Run check + gate. Returns {"status": "done"|"needs_ai"|"failed", ...}."""
    reports_dir = session_dir / "pkgs" / pkgname
    sources_dir = session_dir / "sources"
    build_state_dir = session_dir / "build_state"

    # ── run_check.py ──────────────────────────────────────────────────
    check_cmd = [
        sys.executable, str(_RUN_CHECK),
        "--pkg", pkgname,
        "--url", url,
        "--mode", mode,
        "--pkg-dir", str(reports_dir),
        "--sources-dir", str(sources_dir),
        "--build-state-dir", str(build_state_dir),
    ]
    if version:
        check_cmd += ["--version", version]
    if constraint:
        check_cmd += ["--constraint", constraint]

    check_proc = subprocess.run(check_cmd, capture_output=True, text=True, timeout=300)
    check_rc = check_proc.returncode

    if check_rc == 1:
        # Hard failure
        return {
            "status": "failed",
            "stage": "check",
            "reason": (check_proc.stderr.strip() or check_proc.stdout.strip()
                       or "run_check.py failed"),
        }

    if check_rc == 2:
        # needs_ai — return signal for Claude to handle
        check_result_path = reports_dir / f"check_result_{pkgname}.json"
        return {
            "status": "needs_ai",
            "stage": "check",
            "check_result": str(check_result_path),
            "reason": "run_check.py returned needs_ai, requires LLM to resolve "
                      "license or version",
        }

    # check_rc == 0 — all steps passed, proceed to gate
    # ── run_gate.py ───────────────────────────────────────────────────
    session = _read_json(session_dir / "session.json")

    gate_cmd = [
        sys.executable, str(_RUN_GATE),
        "--pkg", pkgname,
        "--url", url,
        "--mode", mode,
        "--pkg-dir", str(reports_dir),
        "--copr-url", session.get("copr_url", ""),
        "--copr-owner", session.get("copr_owner", ""),
        "--copr-project", session.get("copr_project", ""),
        "--copr-login", session.get("copr_login", ""),
        "--copr-token", session.get("copr_token", ""),
        "--copr-chroot", session.get("copr_chroot", ""),
    ]
    if constraint:
        gate_cmd += ["--constraint", constraint]

    gate_proc = subprocess.run(gate_cmd, capture_output=True, text=True, timeout=120)

    if gate_proc.returncode != 0:
        return {
            "status": "failed",
            "stage": "gate",
            "reason": gate_proc.stderr.strip() or gate_proc.stdout.strip()
                      or "run_gate.py failed",
        }

    # Gate succeeded — read result and update dep_registry
    gate_result_path = reports_dir / f"gate_result_{pkgname}.json"
    if gate_result_path.exists():
        gate = _read_json(gate_result_path)
        decision = (gate.get("result") or {}).get("decision", "")
        lang = (gate.get("result") or {}).get("lang", "")
        version_detected = (gate.get("result") or {}).get("version", "")

        # Update dep_registry（并行模式下由 job_runner 主线程统一写入）
        if not no_update_registry:
            reg_path = session_dir / "dep_registry.json"
            if reg_path.exists() and mode == "dependency":
                reg = _read_json(reg_path)
                if pkgname in reg:
                    reg[pkgname]["status"] = "evaluate_done"
                    if lang:
                        reg[pkgname]["lang"] = lang
                    _write_json(reg_path, reg)

        # 依赖提取与注册（等价于 pkg-evaluator Phase 3）
        # 脚本直调路径跳过了 agent，需在此补上 evaluate-deps.py
        if mode == "top-level" and decision in ("introduce_new", "introduce_new_with_ref") and lang:
            source_dir = session_dir / "sources" / pkgname
            if source_dir.exists() and _EVALUATE_DEPS.exists():
                print(f"[run_evaluate_dep] 依赖评估: evaluate-deps.py --pkg {pkgname} --lang {lang}", file=sys.stderr)
                env = os.environ.copy()
                for k_map, k_session in (
                    ("COPR_FRONTEND_URL", "copr_url"),
                    ("COPR_OWNER", "copr_owner"),
                    ("COPR_PROJECT", "copr_project"),
                    ("COPR_API_LOGIN", "copr_login"),
                    ("COPR_API_TOKEN", "copr_token"),
                    ("COPR_CHROOT", "copr_chroot"),
                ):
                    v = session.get(k_session, "")
                    if v:
                        env[k_map] = v
                try:
                    proc = subprocess.run(
                        [sys.executable, str(_EVALUATE_DEPS),
                         "--session-dir", str(session_dir),
                         "--pkg", pkgname,
                         "--lang", lang,
                         "--source-dir", str(source_dir)],
                        capture_output=True, text=True, timeout=300, env=env,
                    )
                    # rc 与 stderr 摘要落日志 + timeline，避免静默失败不可见
                    stderr_lines = (proc.stderr or "").strip().splitlines()
                    detail = " | ".join(stderr_lines[-3:])[:300]
                    print(f"[run_evaluate_dep] evaluate-deps rc={proc.returncode}: {detail}",
                          file=sys.stderr)
                    write_event(session_dir, "evaluate_deps.end", pkgname, {
                        "rc": proc.returncode,
                        "detail": detail,
                    })
                except Exception as exc:
                    print(f"[run_evaluate_dep] evaluate-deps 失败（不阻塞主流程）: {exc}", file=sys.stderr)
                    write_event(session_dir, "evaluate_deps.end", pkgname, {
                        "rc": -1,
                        "detail": f"exception: {exc}",
                    })

        return {
            "status": "done",
            "decision": decision,
            "lang": lang,
            "version": version_detected,
            "gate_result": str(gate_result_path),
        }

    return {"status": "failed", "stage": "gate", "reason": "gate_result not found"}


# ── CLI ────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run evaluate (check + gate) for a dep without AI"
    )
    parser.add_argument("--pkg", required=True)
    parser.add_argument("--mode", default="dependency", choices=["top-level", "dependency"])
    parser.add_argument("--url", required=True)
    parser.add_argument("--constraint", default="")
    parser.add_argument("--version", default="")
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--no-update-registry", action="store_true",
                        help="不写 dep_registry（并行模式下由 job_runner 主线程统一写入）")
    args = parser.parse_args()

    session_dir = Path(args.session_dir)
    result = run(
        session_dir=session_dir,
        pkgname=args.pkg,
        mode=args.mode,
        url=args.url,
        constraint=args.constraint,
        version=args.version,
        no_update_registry=args.no_update_registry,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") in ("done", "failed") else 1


if __name__ == "__main__":
    sys.exit(main())
