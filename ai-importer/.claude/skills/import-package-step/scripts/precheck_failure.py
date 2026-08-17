#!/usr/bin/env python3
"""Build failure pre-check: scan build log for high-confidence fixable patterns.

If a pattern matches, writes failure_hint_*.json — a *hint* for pkg-fixer
(pattern name, reason, fix instructions, optional spec_patch suggestion).
It does NOT write failure_analysis (pkg-fixer is the single author of that
file), does NOT append fix_instructions.md, and does NOT modify the spec —
pattern 命中不等于修复正确，修复动作与最终诊断均由 pkg-fixer 完成。

Usage:
  python3 precheck_failure.py --session-dir <dir> --pkgname <pkg>
  # stdout: hint_written | needs_ai
"""

import argparse
import json
import re
import sys
from pathlib import Path


# ── Macro → explicit-command mapping ────────────────────────────────────────

_MACRO_REPLACEMENTS = {
    "%cmake_build": "cmake --build . -j$(nproc)",
    "%cmake_install": "DESTDIR=%{buildroot} cmake --install .",
    "%make_build": "make -j$(nproc)",
    "%make_install": "DESTDIR=%{buildroot} make install",
}


def _detect_broken_macro(spec_lines: list[str]) -> str | None:
    """Return the first macro in *spec_lines* that needs replacing, or None."""
    for line in spec_lines:
        stripped = line.strip()
        if stripped in _MACRO_REPLACEMENTS:
            return stripped
    return None


def _resolve_macro_fix(spec_lines: list[str]) -> tuple[list[str], list[dict]] | None:
    """Check which macro needs replacement.

    Returns (fixed_lines, spec_patch) if a macro was found and can be fixed,
    or None if no known macro is present in the spec.
    """
    macro = _detect_broken_macro(spec_lines)
    if not macro:
        return None
    replacement = _MACRO_REPLACEMENTS[macro]
    fixed = [replacement + "\n" if line.strip() == macro else line
             for line in spec_lines]
    spec_patch = [{
        "description": f"将 {macro} 替换为 {replacement}，避免非交互 shell 中的 job control 错误（fg/bg）",
        "before": macro,
        "after": replacement,
    }]
    return fixed, spec_patch


# ── Pattern definitions ─────────────────────────────────────────────────────

# Each pattern dict:
#   name:      unique identifier (for logging)
#   regex:     compiled regex to match against build log
#   verdict:   always "rebuild"（仅作 hint，最终 verdict 由 fixer 决定）
#   reason:    human-readable reason (static)
#   fix_instructions: human-readable fix description
#   resolve:   function(spec_lines) -> (fixed_lines, spec_patch) or None
#              None means "AI must handle this in rebuild mode"

PATTERNS = [
    {
        "name": "fg_no_job_control",
        "regex": re.compile(r"fg: no job control", re.MULTILINE),
        "verdict": "rebuild",
        "reason": "%build 宏在非交互 shell 中调用 fg 失败",
        "fix_instructions": (
            "将 %cmake_build 替换为 cmake --build . -j$(nproc)，"
            "或将 %make_build 替换为 make -j$(nproc)。"
            "cmake configure 阶段（%cmake 或 %configure）保持不变，只替换 build 步骤。"
        ),
        "resolve": _resolve_macro_fix,
    },
    {
        "name": "bg_no_job_control",
        "regex": re.compile(r"bg: no job control", re.MULTILINE),
        "verdict": "rebuild",
        "reason": "shell job control 错误（bg），%build 宏在非交互 shell 中不兼容",
        "fix_instructions": (
            "将构建宏替换为显式命令。"
            "若使用 %cmake_build → cmake --build . -j$(nproc)，"
            "若使用 %make_build → make -j$(nproc)。"
        ),
        "resolve": _resolve_macro_fix,
    },
    {
        "name": "cd_no_such_file_prep",
        "regex": re.compile(r"cd: (.+?): No such file or directory", re.MULTILINE),
        "verdict": "rebuild",
        "reason": "",  # filled dynamically from match group
        "fix_instructions": (
            "将 %autosetup -n 参数改为 %{name}-%{version}"
            "（build-rpm 的 --transform 已统一目录名）。"
        ),
        "resolve": None,  # pkg-fixer rebuild 模式会根据 fix_instructions 处理
    },
]


# ── Main logic ───────────────────────────────────────────────────────────────

def find_pattern(log_text: str) -> dict | None:
    """Return the first matching pattern dict, or None."""
    for pat in PATTERNS:
        m = pat["regex"].search(log_text)
        if m:
            pat["_match"] = m
            return pat
    return None


def write_hint(session_dir: Path, pkgname: str, copr_build_id: str,
               pattern: dict) -> None:
    """Write failure_hint_*.json — pkg-fixer 的可推翻线索，不是诊断产物。

    Does NOT modify the spec, does NOT write failure_analysis, does NOT
    append fix_instructions.md（后两者分别是 fixer 的诊断产物与历史档案，
    由 fixer 单一作者维护）。
    """
    pkg_dir = session_dir / "pkgs" / pkgname
    pkg_dir.mkdir(parents=True, exist_ok=True)

    # Build reason (may use match groups for patterns like cd_no_such_file)
    reason = pattern["reason"]
    m = pattern.get("_match")
    if m and len(m.groups()) > 0:
        if "%s" in reason:
            reason = reason % m.groups()
        else:
            reason = f"{reason}：{m.group(1)}"

    # Generate spec_patch suggestion: try resolve() to detect the exact fix needed
    spec_patch: list[dict] = []
    spec_path = pkg_dir / f"{pkgname}.spec"

    resolver = pattern.get("resolve")
    if resolver and spec_path.exists():
        original = spec_path.read_text(encoding="utf-8").splitlines(keepends=True)
        resolved = resolver(original)
        if resolved is not None:
            _fixed_lines, spec_patch = resolved
            print(f"[precheck] diagnosed fix for pattern: {pattern['name']}, "
                  f"spec_patch={len(spec_patch)} entries", file=sys.stderr)
        else:
            print(f"[precheck] pattern {pattern['name']} matched but macro not found in spec, "
                  f"AI will diagnose from fix_instructions", file=sys.stderr)
    else:
        # No resolver — AI will figure out the fix from fix_instructions
        print(f"[precheck] pattern {pattern['name']} requires AI-driven spec fix", file=sys.stderr)

    if copr_build_id:
        hint_path = pkg_dir / f"failure_hint_{pkgname}_{copr_build_id}.json"
    else:
        hint_path = pkg_dir / f"failure_hint_{pkgname}.json"

    hint = {
        "type": "hint",
        "confidence": "high",
        "pattern": pattern["name"],
        "verdict_hint": pattern["verdict"],
        "reason": reason,
        "fix_instructions": pattern["fix_instructions"],
        "spec_patch": spec_patch,
        "note": "precheck 脚本的高置信线索，pkg-fixer 验证后可推翻；不替代 failure_analysis",
    }
    hint_path.write_text(
        json.dumps(hint, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def get_build_log(session_dir: Path, pkgname: str) -> tuple[str, str]:
    """Read build log from build_rpm_result.json. Returns (log_text, copr_build_id)."""
    result_path = session_dir / "pkgs" / pkgname / "build_rpm_result.json"
    if not result_path.exists():
        return "", ""

    data = json.loads(result_path.read_text(encoding="utf-8"))
    log_text = data.get("build_log_tail", "") or data.get("build_log", "")
    copr_build_id = str(data.get("copr_build_id", "") or "")
    return log_text, copr_build_id


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pre-check build failure for known fixable patterns (hint only)"
    )
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--pkgname", required=True)
    args = parser.parse_args()

    session_dir = Path(args.session_dir)
    pkgname = args.pkgname

    log_text, copr_build_id = get_build_log(session_dir, pkgname)
    if not log_text:
        print("[precheck] no build log found, falling back to AI", file=sys.stderr)
        print("needs_ai")
        return 0

    pattern = find_pattern(log_text)
    if not pattern:
        print("[precheck] no known pattern matched, falling back to AI", file=sys.stderr)
        print("needs_ai")
        return 0

    print(f"[precheck] matched pattern: {pattern['name']}", file=sys.stderr)
    write_hint(session_dir, pkgname, copr_build_id, pattern)
    print("hint_written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
