#!/usr/bin/env python3
"""从构建日志提取结构化错误报告，供 pkg-fixer 诊断使用。

读取 pkgs/<pkg>/build_rpm_result.json 的 build_log，产出
pkgs/<pkg>/build_failure_<build_id>.json：
  - failed_phase：失败发生的 rpmbuild 阶段（%prep/%build/%install/%check/%files/srpm/unknown）
  - failing_command：失败命令（rpmbuild 回显的 '+ ' 命令行，启发式）
  - error_lines：关键词命中行 ±2 行上下文，去重，cap 50 行
  - spec_hash：当前 spec 的 sha256（供 fixer 对照 submitted 快照）
  - same_as_previous：与上一轮 build_failure 的 error signature 是否相同（仅提示，不做闸门）
  - log_tail：日志末尾兜底

用法：
  python3 extract-build-failure.py --session-dir . --pkg git
"""

import argparse
import glob
import hashlib
import json
import re
import sys
from pathlib import Path

# 错误关键词（大小写不敏感）
_ERROR_KEYWORDS = re.compile(
    r"error:|fatal|FAILED|No such file|not found|cannot find|Can't locate|"
    r"undefined reference|nothing provides|No matching package|"
    r"ModuleNotFoundError|ImportError|command not found|Bad exit status|"
    r"unpackaged file",
    re.IGNORECASE,
)

_PHASE_BAD_EXIT = re.compile(r"Bad exit status from .+?\(%(\w+)\)")
_PHASE_EXECUTING = re.compile(r"Executing\(%(\w+)\)")
_UNPACKAGED = re.compile(r"Installed \(but unpackaged\) file\(s\) found")

_CAP_ERROR_LINES = 50
_CAP_LOG_TAIL = 100


def _detect_phase(lines: list[str]) -> str:
    """从日志行推断失败阶段。"""
    if any(_UNPACKAGED.search(l) for l in lines):
        return "%files"
    for line in reversed(lines):
        m = _PHASE_BAD_EXIT.search(line)
        if m:
            return f"%{m.group(1)}"
    # 兜底：最后一个 Executing(%x) 标记
    phase = ""
    for line in lines:
        m = _PHASE_EXECUTING.search(line)
        if m:
            phase = f"%{m.group(1)}"
    return phase or "unknown"


def _extract_error_lines(lines: list[str]) -> list[str]:
    """关键词命中行 ±2 行上下文，去重，cap。"""
    hit_idx = [i for i, l in enumerate(lines) if _ERROR_KEYWORDS.search(l)]
    if not hit_idx:
        return []
    picked: list[str] = []
    seen: set[str] = set()
    for i in hit_idx:
        for j in range(max(0, i - 2), min(len(lines), i + 3)):
            text = lines[j].rstrip()
            if text and text not in seen:
                seen.add(text)
                picked.append(text)
        if len(picked) >= _CAP_ERROR_LINES:
            break
    return picked[:_CAP_ERROR_LINES]


def _detect_failing_command(lines: list[str]) -> str:
    """启发式：rpmbuild 以 '+ ' 回显执行的命令，取首个错误命中行之前最近的回显命令。"""
    first_err = next((i for i, l in enumerate(lines) if _ERROR_KEYWORDS.search(l)), None)
    if first_err is None:
        return ""
    for i in range(first_err, -1, -1):
        stripped = lines[i].strip()
        if stripped.startswith("+ ") and len(stripped) > 2:
            return stripped[2:][:300]
    return ""


def _normalize(text: str) -> str:
    """归一化 error signature：去数字/路径/空白差异。"""
    text = re.sub(r"/[^\s:]+", "<path>", text)
    text = re.sub(r"\d+", "<n>", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def _signature(failed_phase: str, error_lines: list[str]) -> str:
    first = _normalize(error_lines[0]) if error_lines else ""
    return f"{failed_phase}|{first}"


def main() -> int:
    parser = argparse.ArgumentParser(description="提取结构化构建错误报告")
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--pkg", required=True)
    args = parser.parse_args()

    pkg_dir = Path(args.session_dir) / "pkgs" / args.pkg
    br_path = pkg_dir / "build_rpm_result.json"
    if not br_path.exists():
        print(f"[extract-build-failure] {br_path} 不存在，跳过", file=sys.stderr)
        return 0

    br = json.loads(br_path.read_text(encoding="utf-8"))
    build_id = str(br.get("copr_build_id") or "")
    log_text = br.get("build_log", "") or br.get("build_log_tail", "")
    lines = log_text.splitlines()

    failed_phase = _detect_phase(lines)
    error_lines = _extract_error_lines(lines)
    failing_command = _detect_failing_command(lines)

    # 当前 spec 哈希
    spec_path = pkg_dir / f"{args.pkg}.spec"
    spec_hash = ""
    if spec_path.exists():
        spec_hash = "sha256:" + hashlib.sha256(spec_path.read_bytes()).hexdigest()

    # 与上一轮比较（仅提示）
    my_signature = _signature(failed_phase, error_lines)
    same_as_previous = False
    # 精确排除本轮自身的输出文件——子串匹配会误伤（如 build_id=52 滤掉 ..._521.json），
    # build_id 为空时排除 build_failure.json，避免与将被覆盖的自身旧文件比较恒为 True
    own_name = f"build_failure_{build_id}.json" if build_id else "build_failure.json"
    prev_files = sorted(
        f for f in glob.glob(str(pkg_dir / "build_failure_*.json"))
        if Path(f).name != own_name
    )
    if prev_files:
        try:
            prev = json.loads(Path(prev_files[-1]).read_text(encoding="utf-8"))
            prev_signature = _signature(
                prev.get("failed_phase", ""), prev.get("error_lines", [])
            )
            same_as_previous = bool(my_signature) and my_signature == prev_signature
        except Exception:
            pass

    # 语言 import 缺包 → RPM 包名 hint（闭世界查表，低置信，pkg-fixer 可推翻）
    hints: list[dict] = []
    lang = ""
    gate = pkg_dir / f"gate_result_{args.pkg}.json"
    if gate.exists():
        try:
            lang = json.loads(gate.read_text(encoding="utf-8")).get("result", {}).get("lang", "")
        except Exception:
            pass
    if lang in ("python", "nodejs"):
        mods: list[str] = []
        for line in lines:
            m = re.search(r"ModuleNotFoundError: No module named ['\"]([^'\"]+)['\"]", line) or \
                re.search(r"Cannot find module ['\"]([^'\"]+)['\"]", line)
            if m:
                mods.append(m.group(1))
        if mods:
            try:
                _d = str(Path(__file__).resolve().parents[2] / "build-rpm" / "scripts")
                if _d not in sys.path:
                    sys.path.insert(0, _d)
                from rpm_naming import get_rpm_pkg_name
                for mod in list(dict.fromkeys(mods))[:10]:
                    hints.append({"module": mod,
                                  "rpm_hint": get_rpm_pkg_name(lang, mod.split(".")[0]),
                                  "confidence": "low"})
            except Exception:
                hints = []

    report = {
        "build_id": build_id,
        "failed_phase": failed_phase,
        "failing_command": failing_command,
        "error_lines": error_lines,
        "spec_hash": spec_hash,
        "same_as_previous": same_as_previous,
        "failure_reason": br.get("failure_reason", ""),
        "missing_module_hints": hints,
        "log_tail": "\n".join(lines[-_CAP_LOG_TAIL:]),
    }

    out_name = f"build_failure_{build_id}.json" if build_id else "build_failure.json"
    out_path = pkg_dir / out_name
    out_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        f"[extract-build-failure] {args.pkg}: phase={failed_phase} "
        f"errors={len(error_lines)} same_as_previous={same_as_previous} → {out_name}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
