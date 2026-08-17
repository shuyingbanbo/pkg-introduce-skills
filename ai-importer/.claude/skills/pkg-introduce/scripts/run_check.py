#!/usr/bin/env python3
"""Phase 1：基础检查（不需要容器）

步骤：init → repo_check → download → license_check → detect

输出 check_result_<pkgname>.json，格式：
  overall_status: "done" | "failed" | "needs_ai"
  steps.<step>.status: "done" | "skipped" | "failed" | "needs_ai"
  steps.<step> 当 needs_ai 时还包含 ai_inputs（LLM 需要的证据）

LLM 处理 needs_ai 的方式：
  直接修改 check_result_<pkgname>.json 对应步骤的字段：
  - status 改为 "done"
  - 补齐 lang/version 等字段
  - 加上 ai_resolved: true 和 reason
  然后直接调 run_gate.py（不需要重跑 run_check.py）

exit codes:
  0  overall_status=done    — 可以直接进 run_gate.py
  1  overall_status=failed  — 硬失败，写结果终止
  2  overall_status=needs_ai — LLM 补齐报告后直接进 run_gate.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_PKG_SCRIPTS = Path(__file__).resolve().parent
_SETUP_CONTAINER_SCRIPT = Path(__file__).resolve().parent / "setup_container.py"
sys.path.insert(0, str(_PKG_SCRIPTS))

from run_pkg_introduce_flow import (  # noqa: E402
    _load_config,
    FlowError,
    initialize_top_level,
    run_repo_check,
    run_download,
    run_license_check,
    detect_lang_and_version,
    run_command,
)
from java_metadata import detect_java_build_system  # noqa: E402

CHECK_STEPS = ["init", "repo_check", "download", "license_check", "detect"]


# ── Helpers ────────────────────────────────────────────────────────────────

def _default_report(pkgname: str, upstream_url: str) -> dict[str, Any]:
    cfg = _load_config()
    return {
        "pkgname": pkgname,
        "upstream_url": upstream_url,
        "overall_status": "pending",
        "config_summary": {
            "license_check_enabled": cfg.get("license_check", {}).get("enabled", True),
            "allow_unstable": cfg.get("version_check", {}).get("allow_unstable", False),
            "repo_check_blocking": cfg.get("repo_check", {}).get("blocking", True),
            "dep_conflict_mode": cfg.get("dep_conflict", {}).get("mode", "block"),
        },
        "steps": {step: {"status": "pending"} for step in CHECK_STEPS},
        "result": None,
    }


def _save(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _already_done(step_data: dict[str, Any]) -> bool:
    return step_data.get("status") in ("done", "skipped")


def _compute_overall(steps: dict[str, Any]) -> str:
    statuses = [v.get("status", "pending") for v in steps.values()]
    if "failed" in statuses:
        return "failed"
    if "needs_ai" in statuses:
        return "needs_ai"
    if all(s in ("done", "skipped") for s in statuses):
        return "done"
    return "pending"


# ── Step runners ───────────────────────────────────────────────────────────

def _run_init(report: dict, mode: str, build_state_dir: Path, reports_dir: Path, sources_dir: Path) -> None:
    if mode == "dependency":
        report["steps"]["init"] = {"status": "skipped", "reason": "dependency mode: reuse top-level state"}
        return
    try:
        initialize_top_level(build_state_dir, reports_dir, sources_dir)
        report["steps"]["init"] = {"status": "done"}
    except FlowError as exc:
        report["steps"]["init"] = {"status": "failed", "reason": exc.reason}
        raise


def _run_repo_check(report: dict, pkgname: str, upstream_url: str, reports_dir: Path) -> None:
    try:
        result = run_repo_check(pkgname, upstream_url, reports_dir)
        report["steps"]["repo_check"] = {
            "status": "done",
            "platform": result.get("platform"),
            "days_inactive": result.get("days_inactive"),
            "warning": result.get("warning"),
        }
    except FlowError as exc:
        report["steps"]["repo_check"] = {"status": "failed", "reason": exc.reason}
        raise


def _run_download(report: dict, pkgname: str, upstream_url: str, version: str,
                  sources_dir: Path, reports_dir: Path, constraint: str = "") -> None:
    try:
        result = run_download(pkgname, upstream_url, version, sources_dir, reports_dir,
                              constraint=constraint)
        report["steps"]["download"] = {
            "status": "done",
            "version": result.get("version", ""),
            "source_dir": result.get("source_dir", ""),
        }
    except FlowError as exc:
        report["steps"]["download"] = {"status": "failed", "reason": exc.reason}
        raise


def _run_license_check(report: dict, pkgname: str, source_dir: Path, reports_dir: Path) -> None:
    try:
        result = run_license_check(pkgname, source_dir, reports_dir)
        status = result.get("status", "done")
        step: dict[str, Any] = {"status": status}
        if status == "skipped":
            step["reason"] = "license_check.enabled=false"
        elif status == "needs_ai":
            step["reason"] = result.get("reason", "")
            step["category"] = result.get("category", "")
            # Evidence for LLM
            evidence: dict[str, Any] = {}
            lic_path = Path(result.get("license_check", ""))
            if lic_path.exists():
                lic = json.loads(lic_path.read_text(encoding="utf-8"))
                evidence["raw_license_ids"] = lic.get("license_ids", [])
                evidence["source"] = lic.get("source", "")
                evidence["message"] = lic.get("message", "")
            for name in ("LICENSE", "LICENSE.txt", "LICENSE.md", "COPYING", "NOTICE"):
                lf = source_dir / name
                if lf.exists():
                    try:
                        evidence["license_file_snippet"] = lf.read_text(encoding="utf-8", errors="ignore")[:500]
                    except OSError:
                        pass
                    break
            step["ai_inputs"] = evidence
            # Instructions for LLM
            step["ai_instructions"] = (
                "判断该许可证是否为可接受的开源许可证。"
                "直接修改本步骤的 status 为 'done' 并补充 decision('accept'/'reject') 和 reason。"
                "若接受，同时将 license_category 填写为对应 SPDX 分类。"
            )
        report["steps"]["license_check"] = step
    except FlowError as exc:
        report["steps"]["license_check"] = {"status": "failed", "reason": exc.reason}
        raise


def _run_detect(report: dict, pkgname: str, source_dir: Path, expected_version: str) -> tuple[str, str]:
    """Returns (lang, version). On needs_ai, embeds evidence in step and returns ("", "")."""
    # If step was already resolved by LLM (status=done with ai_resolved=true), use it
    existing = report["steps"].get("detect", {})
    if existing.get("status") == "done" and existing.get("ai_resolved"):
        return existing.get("lang", ""), existing.get("version", "")

    result = detect_lang_and_version(source_dir, expected_version)
    status = result.get("status", "done")
    lang = result.get("lang", "")
    version = result.get("version", "")

    if status in ("needs_agent", "needs_ai"):
        step: dict[str, Any] = {
            "status": "needs_ai",
            "lang": lang,
            "version": "",
            "reason": result.get("reason", ""),
        }
        if result.get("expected_version"):
            step["expected_version"] = result["expected_version"]
        # Evidence
        evidence: dict[str, Any] = {}
        git_proc = run_command(["git", "-C", str(source_dir), "tag", "--sort=-version:refname"])
        if git_proc.returncode == 0:
            evidence["git_tags"] = git_proc.stdout.strip().splitlines()[:20]
        for fname in ("pyproject.toml", "Cargo.toml", "package.json", "pom.xml", "setup.py", "VERSION"):
            fpath = source_dir / fname
            if fpath.exists():
                try:
                    evidence[fname] = fpath.read_text(encoding="utf-8", errors="ignore")[:300]
                except OSError:
                    pass
        step["ai_inputs"] = evidence
        step["ai_instructions"] = (
            f"从上述证据中推断软件包的真实版本号和语言类型。"
            f"{'期望版本: ' + result['expected_version'] + '，' if result.get('expected_version') else ''}"
            "直接修改本步骤：将 status 改为 'done'，填写 lang 和 version，加上 ai_resolved: true 和 reason。"
            "version 必须是完整的版本号（如 1.2.3），不含 'v' 前缀。"
        )
        report["steps"]["detect"] = step
        return lang, ""

    if status == "done":
        step_done: dict[str, Any] = {"status": "done", "lang": lang, "version": version}
        if lang == "java":
            build_system = detect_java_build_system(str(source_dir))
            step_done["build_system"] = build_system
            if build_system == "gradle":
                # Gradle 项目在 chroot 的 maven-local 离线设施下无法构建，
                # 在 evaluate 阶段直接标记失败（abort），不浪费构建资源
                report["steps"]["detect"] = {
                    "status": "failed",
                    "lang": lang,
                    "version": version,
                    "build_system": "gradle",
                    "reason": ("Gradle build system is not supported by the chroot Java "
                               "offline build infrastructure (maven-local based); "
                               "abort without attempting build"),
                }
                raise FlowError(report["steps"]["detect"]["reason"],
                                "non_retryable_gradle_build_system")
        report["steps"]["detect"] = step_done
        return lang, version

    report["steps"]["detect"] = {"status": "failed", "reason": result.get("reason", str(result))}
    raise FlowError(result.get("reason", "detect failed"))


# ── Main ───────────────────────────────────────────────────────────────────

def run_check(args: argparse.Namespace) -> int:
    reports_dir = Path(args.reports_dir)
    sources_dir = Path(args.sources_dir)
    build_state_dir = Path(args.build_state_dir)
    report_path = reports_dir / f"check_result_{args.pkg}.json"

    # Load existing report if present (allows LLM to have pre-edited it)
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
    else:
        report = _default_report(args.pkg, args.upstream_url)

    steps = report["steps"]

    try:
        # ── init ──────────────────────────────────────────────────────────
        if not _already_done(steps["init"]):
            _run_init(report, args.mode, build_state_dir, reports_dir, sources_dir)
            _save(report, report_path)

        # ── repo_check ────────────────────────────────────────────────────
        if not _already_done(steps["repo_check"]):
            _run_repo_check(report, args.pkg, args.upstream_url, reports_dir)
            _save(report, report_path)

        # ── download ──────────────────────────────────────────────────────
        if not _already_done(steps["download"]):
            _run_download(report, args.pkg, args.upstream_url, args.version or "",
                          sources_dir, reports_dir,
                          constraint=args.constraint if args.mode == "dependency" else "")
            _save(report, report_path)

        # ── license_check ─────────────────────────────────────────────────
        if not _already_done(steps["license_check"]):
            source_dir = sources_dir / args.pkg
            _run_license_check(report, args.pkg, source_dir, reports_dir)
            _save(report, report_path)

        # ── detect ────────────────────────────────────────────────────────
        source_dir = sources_dir / args.pkg
        if not _already_done(steps["detect"]):
            _run_detect(report, args.pkg, source_dir,
                        args.version or steps["download"].get("version", ""))
            _save(report, report_path)

    except FlowError:
        report["overall_status"] = _compute_overall(steps)
        _save(report, report_path)
        print(json.dumps({"status": "failed", "report": str(report_path)}, ensure_ascii=False))
        return 1

    overall = _compute_overall(steps)
    report["overall_status"] = overall

    # Embed confirmed outputs for run_gate.py to consume
    detect_step = steps.get("detect", {})
    report["result"] = {
        "lang": detect_step.get("lang", ""),
        "version": detect_step.get("version", ""),
        "source_dir": steps.get("download", {}).get("source_dir", str(sources_dir / args.pkg)),
    }
    _save(report, report_path)

    print(json.dumps({"status": overall, "report": str(report_path)}, ensure_ascii=False))
    if overall == "done":
        return 0
    if overall == "needs_ai":
        return 2
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="pkg-introduce Phase 1: 基础检查（不需要容器）")
    parser.add_argument("--pkg", required=True)
    parser.add_argument("--url", required=True, dest="upstream_url")
    parser.add_argument("--version", default="")
    parser.add_argument("--constraint", default="", help="版本约束（dependency mode），如 '>= 1.4.0'")
    parser.add_argument("--mode", default="top-level", choices=["top-level", "dependency"])
    parser.add_argument("--reports-dir", default="./reports", dest="reports_dir")
    parser.add_argument("--pkg-dir", default=None, dest="pkg_dir")
    parser.add_argument("--sources-dir", default="./sources", dest="sources_dir")
    parser.add_argument("--build-state-dir", default="./build_state", dest="build_state_dir")
    args = parser.parse_args()
    # --pkg-dir 优先：将输出写到 ./pkgs/<pkgname>/ 而不是 ./reports/
    if args.pkg_dir:
        args.reports_dir = args.pkg_dir
    return run_check(args)


if __name__ == "__main__":
    sys.exit(main())
