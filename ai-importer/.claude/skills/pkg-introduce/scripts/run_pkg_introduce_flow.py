#!/usr/bin/env python3
"""Stage-based pkg-introduce orchestrator helpers.

This file intentionally exposes subcommands for skill-driven orchestration.
The skill remains the top-level phase controller; this script closes each phase.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _load_config() -> dict:
    config_path = Path(__file__).resolve().parent.parent / "config.json"
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
from typing import Any

CLAUDE_SKILLS_DIR = Path(__file__).resolve().parents[2]
PKG_SCRIPTS_DIR = Path(__file__).resolve().parent
RESULT_SCRIPT = PKG_SCRIPTS_DIR / "pkg_introduce_result.py"
CHECK_REPO_SCRIPT = PKG_SCRIPTS_DIR / "check_repo.py"
DOWNLOAD_SCRIPT = PKG_SCRIPTS_DIR / "download_source.py"
LICENSE_SCRIPT = PKG_SCRIPTS_DIR / "check_license.py"
EXTRACT_VERSION_SCRIPT = PKG_SCRIPTS_DIR / "extract_version.py"
CHECK_EXISTING_SCRIPT = PKG_SCRIPTS_DIR / "check_existing_package.py"
INIT_SESSION_SCRIPT = PKG_SCRIPTS_DIR / "init_session_state.py"


class FlowError(RuntimeError):
    def __init__(self, reason: str, failure_type: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.failure_type = failure_type


# ---------- Common helpers ----------

def run_command(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, check=False)


# 网络类瞬时错误特征（git clone / wget 的 stderr/stdout 文本，小写匹配）。
# 这类错误重试有意义；404 / 认证失败 / 仓库不存在等不在此列，直接判死。
TRANSIENT_NET_PATTERNS = (
    "failure when receiving data",
    "recv failure",
    "send failure",
    "connection reset",
    "connection refused",
    "connection timed out",
    "operation timed out",
    "could not resolve host",
    "temporary failure in name resolution",
    "empty reply from server",
    "returned error: 50",      # 502/503/504 网关类错误
    "early eof",
    "rpc failed",
    "the remote end hung up",
    "gnutls",
)

DOWNLOAD_MAX_ATTEMPTS = 3  # 1 次原始 + 2 次重试


def _is_transient_network_error(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in TRANSIENT_NET_PATTERNS)


# ---------- Step tracking ----------

# 所有阶段按顺序定义，归档时以此为权威清单
FLOW_STEPS = [
    "repo_check",
    "download",
    "license_check",
    "detect",
    "existing_check",
    "build",
    "ci_gate",
    "review_summary",
]


def steps_path(pkgname: str, reports_dir: Path) -> Path:
    return reports_dir / f"steps_{pkgname}.json"


def load_steps(pkgname: str, reports_dir: Path) -> dict[str, str]:
    p = steps_path(pkgname, reports_dir)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {step: "pending" for step in FLOW_STEPS}


def mark_step(pkgname: str, reports_dir: Path, step: str, status: str = "done") -> None:
    """Write step status to steps_<pkgname>.json. status: done | skipped | failed | needs_ai"""
    steps = load_steps(pkgname, reports_dir)
    steps[step] = status
    steps_path(pkgname, reports_dir).write_text(
        json.dumps(steps, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def normalize_version_text(version: str) -> str:
    return (version or "").strip().removeprefix("v")


def split_version_input(version: str) -> tuple[str, str]:
    """拆分 version 输入为 (裸版本号, 完整 tag)。

    用户可填完整 tag（如 workers-sdk@0.18.8、jfiglet-0.0.8、@scope/pkg@1.0.0）：
    完整 tag 用于 ref 解析，裸版本号（数字尾）用于 detect 比对和 spec Version。
    普通版本号输入时两者相同。

    >>> split_version_input("0.18.8")
    ('0.18.8', '0.18.8')
    >>> split_version_input("v0.18.8")
    ('0.18.8', 'v0.18.8')
    >>> split_version_input("workers-sdk@0.18.8")
    ('0.18.8', 'workers-sdk@0.18.8')
    >>> split_version_input("jfiglet-0.0.8")
    ('0.0.8', 'jfiglet-0.0.8')
    >>> split_version_input("@cloudflare/pkg@1.0.0")
    ('1.0.0', '@cloudflare/pkg@1.0.0')
    """
    version = (version or "").strip()
    # 匹配结尾的数字版本段：以 \d+\.\d 开头（至少包含 major.minor），
    # 后跟可选的点分数字段。预发布后缀（如 -rc.1、a6）无法单靠此正则匹配，
    # 会 fall through 到默认路径，此时整个输入即为裸版本号（无前缀可剥离）。
    m = re.search(r"(\d+\.\d[.\d]*)$", version)
    if m and m.start() > 0:
        return m.group(1), version      # (0.18.8, workers-sdk@0.18.8)
    return version.removeprefix("v"), version


def result_path(pkgname: str, reports_dir: Path) -> Path:
    return reports_dir / f"pkg_introduce_result_{pkgname}.json"


def run_result_command(subcommand: str, pkgname: str, reports_dir: Path, extra_args: list[str]) -> subprocess.CompletedProcess:
    command = [sys.executable, str(RESULT_SCRIPT), subcommand, pkgname, "--reports-dir", str(reports_dir), *extra_args]
    return run_command(command)


def update_result(
    pkgname: str,
    reports_dir: Path,
    *,
    action: str | None = None,
    reason: str | None = None,
    status: str | None = None,
    failure_type: str | None = None,
    failure_reason: str | None = None,
    version: str | None = None,
    requested_version: str | None = None,
    decision: str | None = None,
    lang: str | None = None,
    analysis_file: str | None = None,
    archived: bool | None = None,
) -> None:
    extra: list[str] = []
    mapping = {
        "--action": action,
        "--reason": reason,
        "--status": status,
        "--failure-type": failure_type,
        "--failure-reason": failure_reason,
        "--version": version,
        "--requested-version": requested_version,
        "--decision": decision,
        "--lang": lang,
        "--analysis-file": analysis_file,
    }
    for flag, value in mapping.items():
        if value is not None:
            extra += [flag, value]
    if archived is not None:
        extra += ["--archived", "true" if archived else "false"]
    proc = run_result_command("update", pkgname, reports_dir, extra)
    if proc.returncode != 0:
        raise FlowError(f"failed to update result: {proc.stderr.strip() or proc.stdout.strip()}")


# ---------- Phase implementations ----------

def initialize_top_level(build_state_dir: Path, reports_dir: Path, sources_dir: Path) -> dict[str, Any]:
    proc = run_command([
        sys.executable,
        str(INIT_SESSION_SCRIPT),
        "--build-state-dir",
        str(build_state_dir),
        "--reports-dir",
        str(reports_dir),
        "--sources-dir",
        str(sources_dir),
    ])
    if proc.returncode != 0:
        raise FlowError(proc.stderr.strip() or proc.stdout.strip() or "failed to initialize session state")
    return {
        "status": "done",
        "build_state_dir": str(build_state_dir),
        "reports_dir": str(reports_dir),
        "sources_dir": str(sources_dir),
    }


def run_repo_check(pkgname: str, upstream_url: str, reports_dir: Path) -> dict[str, Any]:
    path = reports_dir / f"repo_check_{pkgname}.json"
    proc = run_command([sys.executable, str(CHECK_REPO_SCRIPT), upstream_url, "-o", str(path)])
    if proc.returncode != 0:
        mark_step(pkgname, reports_dir, "repo_check", "failed")
        raise FlowError(proc.stderr.strip() or proc.stdout.strip() or "repo check failed", "non_retryable_repo_blocked")
    mark_step(pkgname, reports_dir, "repo_check")
    return {"status": "done", "repo_check": str(path)}


def _split_github_tree_url(upstream_url: str) -> tuple[str, str]:
    """Split GitHub /tree/<ref> or /commit/<ref> URLs into (repo_url, ref).

    Returns (upstream_url, "") if no /tree/ or /commit/ segment is found.
    """
    m = re.search(r"^(https?://github\.com/[^/]+/[^/]+)(?:/(?:tree|commit)/([0-9a-f]{7,40}|[^/]+)).*$", upstream_url)
    if m:
        return m.group(1), m.group(2)
    return upstream_url, ""


def run_download(pkgname: str, upstream_url: str, expected_version: str, sources_dir: Path,
                 reports_dir: Path, constraint: str = "") -> dict[str, Any]:
    source_dir = sources_dir / pkgname
    shutil.rmtree(source_dir, ignore_errors=True)
    path = reports_dir / f"download_result_{pkgname}.json"

    # Normalise GitHub /tree/<ref> or /commit/<ref> URLs
    repo_url, extracted_ref = _split_github_tree_url(upstream_url)

    command = [
        sys.executable,
        str(DOWNLOAD_SCRIPT),
        "--upstream-url",
        repo_url,
        "--output-dir",
        str(sources_dir),
        "--pkgname", pkgname,
        "-o",
        str(path),
    ]
    if expected_version:
        command[4:4] = ["--version", expected_version]
    elif extracted_ref:
        command.extend(["--ref", extracted_ref])
    elif constraint:
        # dependency mode：无精确版本，用 constraint 选稳定版
        command.extend(["--constraint", constraint])
    proc = run_command(command)
    # P0-2: version 解析失败且 URL 里有 ref 时，用 ref 重试一次再放弃
    if proc.returncode != 0 and expected_version and extracted_ref:
        print(
            f"[WARN] 按版本 {expected_version} 解析失败，回退到 URL 中的 ref: {extracted_ref}",
            file=sys.stderr,
        )
        command = [
            sys.executable, str(DOWNLOAD_SCRIPT), "--upstream-url", repo_url,
            "--output-dir", str(sources_dir), "--pkgname", pkgname,
            "-o", str(path),
            "--ref", extracted_ref,
        ]
        proc = run_command(command)
    # 网络类瞬时错误：脚本内重试吸收（确定性行为），重试无效才上报 failed
    attempt = 1
    while (
        proc.returncode != 0
        and attempt < DOWNLOAD_MAX_ATTEMPTS
        and _is_transient_network_error(proc.stderr + "\n" + proc.stdout)
    ):
        wait_s = 10 * attempt
        print(
            f"[WARN] download 遇到网络错误，{wait_s}s 后重试（第 {attempt + 1}/{DOWNLOAD_MAX_ATTEMPTS} 次）",
            file=sys.stderr,
        )
        time.sleep(wait_s)
        proc = run_command(command)
        attempt += 1
    if proc.returncode != 0:
        err_text = proc.stderr.strip() or proc.stdout.strip() or "download failed"
        failure_type = (
            "retryable_network"
            if _is_transient_network_error(proc.stderr + "\n" + proc.stdout)
            else "non_retryable_source_missing"
        )
        mark_step(pkgname, reports_dir, "download", "failed")
        raise FlowError(err_text, failure_type)
    mark_step(pkgname, reports_dir, "download")
    return {"status": "done", "source_dir": str(source_dir), "download_result": str(path)}


def run_license_check(pkgname: str, source_dir: Path, reports_dir: Path) -> dict[str, Any]:
    cfg = _load_config().get("license_check", {})
    if not cfg.get("enabled", True):
        path = reports_dir / f"license_check_{pkgname}.json"
        skipped = {
            "category": "skipped",
            "blocking": False,
            "needs_ai_fallback": False,
            "license_ids": [],
            "message": "license_check.enabled=false，已跳过 License 检查",
        }
        path.write_text(json.dumps(skipped, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[INFO] License 检查已跳过（config: license_check.enabled=false）")
        mark_step(pkgname, reports_dir, "license_check", "skipped")
        return {"status": "skipped", "license_check": str(path)}

    path = reports_dir / f"license_check_{pkgname}.json"
    proc = run_command([sys.executable, str(LICENSE_SCRIPT), str(source_dir), "--pkg", pkgname, "-o", str(path)])
    if proc.returncode != 0:
        mark_step(pkgname, reports_dir, "license_check", "failed")
        raise FlowError(proc.stderr.strip() or proc.stdout.strip() or "license check failed", "non_retryable_license_blocked")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("needs_ai_fallback"):
        mark_step(pkgname, reports_dir, "license_check", "needs_ai")
        return {"status": "needs_ai", "license_check": str(path), "category": data.get("category"), "reason": data.get("message", "")}
    mark_step(pkgname, reports_dir, "license_check")
    return {"status": "done", "license_check": str(path)}


def detect_lang(source_dir: Path) -> str:
    """Detect primary language by checking characteristic files in priority order.

    Priority: go.mod > Cargo.toml > package.json > pyproject.toml/setup.py >
              pom.xml/build.gradle > *.gemspec/Gemfile > CMakeLists.txt/meson.build/configure.ac
    """
    checks = [
        ("go.mod",                                          "go"),
        ("Cargo.toml",                                      "rust"),
        ("package.json",                                    "nodejs"),
        ("pyproject.toml",                                  "python"),
        ("setup.py",                                        "python"),
        ("pom.xml",                                         "java"),
        ("build.gradle",                                    "java"),
        ("build.gradle.kts",                                "java"),
    ]
    for filename, lang in checks:
        if (source_dir / filename).exists():
            return lang
    # gemspec (glob)
    if list(source_dir.glob("*.gemspec")) or (source_dir / "Gemfile").exists():
        return "ruby"
    # C/C++ build systems
    for f in ("CMakeLists.txt", "meson.build", "configure.ac"):
        if (source_dir / f).exists():
            return "c"
    return "python"  # fallback


def detect_lang_and_version(source_dir: Path, expected_version: str) -> dict[str, Any]:
    lang = detect_lang(source_dir)
    proc = run_command([sys.executable, str(EXTRACT_VERSION_SCRIPT), lang, str(source_dir)])
    version = proc.stdout.strip()
    if proc.returncode != 0:
        raise FlowError(proc.stderr.strip() or proc.stdout.strip() or "failed to detect version")
    if not version:
        # Static extraction exhausted — signal skill to invoke agent fallback
        return {
            "status": "needs_agent",
            "lang": lang,
            "version": "",
            "reason": "static version extraction returned empty; dynamic version or non-standard layout",
            "source_dir": str(source_dir),
            "expected_version": expected_version,
        }
    if expected_version and normalize_version_text(expected_version) != normalize_version_text(version):
        return {
            "status": "needs_ai",
            "lang": lang,
            "version": version,
            "expected_version": expected_version,
            "reason": f"detected version {version} does not match expected {expected_version}; may be a normalisation difference",
            "source_dir": str(source_dir),
        }
    return {"status": "done", "lang": lang, "version": version}


def run_existing_check(pkgname: str, version: str, lang: str, reports_dir: Path, container: str, constraint: str = "") -> dict[str, Any]:
    path = reports_dir / f"existing_check_{pkgname}.json"
    cmd = [
        sys.executable,
        str(CHECK_EXISTING_SCRIPT),
        pkgname,
        "--version", version,
        "--lang", lang,
        "--container", container,
        "-o", str(path),
    ]
    if constraint:
        cmd += ["--requirement", constraint]
    proc = run_command(cmd)
    if proc.returncode != 0:
        mark_step(pkgname, reports_dir, "existing_check", "failed")
        raise FlowError(proc.stderr.strip() or proc.stdout.strip() or "existing check failed")
    data = json.loads(path.read_text(encoding="utf-8"))
    decision = data.get("decision", "")
    reason = data.get("reason", "")

    # Honour dep_conflict.mode=compat/force_compat: when official has an older version and the
    # language supports co-installation via a compat package name, treat it as introduce_new.
    # force_compat applies to all languages; compat only applies to nodejs/c/cpp/java/ruby.
    _COMPAT_SUPPORTED_LANGS = {"nodejs", "c", "cpp", "java", "ruby"}
    if decision == "block_official_older":
        conflict_mode = _load_config().get("dep_conflict", {}).get("mode", "block")
        can_compat = (
            (conflict_mode == "compat" and lang in _COMPAT_SUPPORTED_LANGS)
            or conflict_mode == "force_compat"
        )
        if can_compat:
            decision = "introduce_new"
            reason = reason + f"（dep_conflict.mode={conflict_mode}：以 compat 包名引入新版本，不阻断）"
            data["decision"] = decision
            data["reason"] = reason
            data["compat_introduce"] = True
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    mark_step(pkgname, reports_dir, "existing_check")
    return {"status": "done", "existing_check": str(path), "decision": decision, "reason": reason}


def finalize_result(args: argparse.Namespace) -> dict[str, Any]:
    reports_dir = Path(args.reports_dir)
    update_result(
        args.pkg,
        reports_dir,
        action=args.action,
        reason=args.reason,
        status=args.status,
        failure_type=args.failure_type,
        failure_reason=args.failure_reason,
        version=args.version,
        requested_version=args.requested_version,
        decision=args.decision,
        lang=args.lang,
        analysis_file=args.analysis_file,
        archived=args.archived,
    )
    return {"status": "done", "result_file": str(result_path(args.pkg, reports_dir))}


# ---------- CLI ----------

def print_payload(payload: dict[str, Any]) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage-based pkg-introduce orchestrator helpers")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--pkg", required=True)
    init_parser.add_argument("--mode", choices=["top-level", "dependency"], required=True)
    init_parser.add_argument("--build-state-dir", default="./build_state")
    init_parser.add_argument("--reports-dir", default="./reports")
    init_parser.add_argument("--sources-dir", default="./sources")

    repo_parser = subparsers.add_parser("repo-check")
    repo_parser.add_argument("--pkg", required=True)
    repo_parser.add_argument("--upstream-url", required=True)
    repo_parser.add_argument("--reports-dir", default="./reports")

    download_parser = subparsers.add_parser("download")
    download_parser.add_argument("--pkg", required=True)
    download_parser.add_argument("--upstream-url", required=True)
    download_parser.add_argument("--version", default="")
    download_parser.add_argument("--sources-dir", default="./sources")
    download_parser.add_argument("--reports-dir", default="./reports")

    license_parser = subparsers.add_parser("license-check")
    license_parser.add_argument("--pkg", required=True)
    license_parser.add_argument("--source-dir", required=True)
    license_parser.add_argument("--reports-dir", default="./reports")

    detect_parser = subparsers.add_parser("detect")
    detect_parser.add_argument("--pkg", required=True)
    detect_parser.add_argument("--source-dir", required=True)
    detect_parser.add_argument("--expected-version", default="")
    detect_parser.add_argument("--reports-dir", default="./reports")

    existing_parser = subparsers.add_parser("existing-check")
    existing_parser.add_argument("--pkg", required=True)
    existing_parser.add_argument("--lang", required=True)
    existing_parser.add_argument("--version", required=True)
    existing_parser.add_argument("--container", default="oe-build-env")
    existing_parser.add_argument("--reports-dir", default="./reports")

    finalize_parser = subparsers.add_parser("finalize-result")
    finalize_parser.add_argument("--pkg", required=True)
    finalize_parser.add_argument("--reports-dir", default="./reports")
    finalize_parser.add_argument("--action", default=None)
    finalize_parser.add_argument("--reason", default=None)
    finalize_parser.add_argument("--status", default=None)
    finalize_parser.add_argument("--failure-type", dest="failure_type", default=None)
    finalize_parser.add_argument("--failure-reason", dest="failure_reason", default=None)
    finalize_parser.add_argument("--version", default=None)
    finalize_parser.add_argument("--requested-version", dest="requested_version", default=None)
    finalize_parser.add_argument("--decision", default=None)
    finalize_parser.add_argument("--lang", default=None)
    finalize_parser.add_argument("--analysis-file", dest="analysis_file", default=None)
    finalize_parser.add_argument("--archived", choices=["true", "false"], default=None)

    mark_parser = subparsers.add_parser("mark-step")
    mark_parser.add_argument("--pkg", required=True)
    mark_parser.add_argument("--step", required=True, choices=FLOW_STEPS)
    mark_parser.add_argument("--status", default="done", choices=["done", "skipped", "failed"])
    mark_parser.add_argument("--reports-dir", default="./reports")

    args = parser.parse_args()

    try:
        if args.command == "init":
            payload = initialize_top_level(Path(args.build_state_dir), Path(args.reports_dir), Path(args.sources_dir))
            return print_payload(payload)
        if args.command == "repo-check":
            payload = run_repo_check(args.pkg, args.upstream_url, Path(args.reports_dir))
            return print_payload(payload)
        if args.command == "download":
            payload = run_download(args.pkg, args.upstream_url, args.version, Path(args.sources_dir), Path(args.reports_dir))
            return print_payload(payload)
        if args.command == "license-check":
            payload = run_license_check(args.pkg, Path(args.source_dir), Path(args.reports_dir))
            return print_payload(payload)
        if args.command == "detect":
            reports_dir = Path(args.reports_dir)
            try:
                # P0-1: 用户可能填了完整 tag（如 workers-sdk@0.18.8）
                # detect 比对和后续 spec Version 应使用裸版本号
                bare_version, _ = split_version_input(args.expected_version)
                payload = detect_lang_and_version(Path(args.source_dir), bare_version)
                if payload.get("status") == "done":
                    mark_step(args.pkg, reports_dir, "detect")
                return print_payload(payload)
            except FlowError:
                mark_step(args.pkg, reports_dir, "detect", "failed")
                raise
        if args.command == "existing-check":
            payload = run_existing_check(args.pkg, args.version, args.lang, Path(args.reports_dir), args.container)
            return print_payload(payload)
        if args.command == "finalize-result":
            archived = None if args.archived is None else args.archived == "true"
            args.archived = archived
            # P0-1: spec Version 应为裸版本号，非法 RPM 字符（如 @）
            if args.version is not None:
                args.version, _ = split_version_input(args.version)
            if args.requested_version is not None:
                args.requested_version, _ = split_version_input(args.requested_version)
            payload = finalize_result(args)
            return print_payload(payload)
        if args.command == "mark-step":
            mark_step(args.pkg, Path(args.reports_dir), args.step, args.status)
            return print_payload({"status": "done", "step": args.step, "step_status": args.status})
        raise FlowError(f"unknown command: {args.command}")
    except FlowError as exc:
        print(json.dumps({"status": "failed", "reason": exc.reason, "failure_type": exc.failure_type}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
