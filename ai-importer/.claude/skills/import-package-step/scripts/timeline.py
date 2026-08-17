#!/usr/bin/env python3
"""Timeline 事件写入/读取工具。

提供统一的 timeline 写入接口，供 Python 代码（import）和 Shell 脚本（CLI）共用。

用法：
  # Shell：写入事件
  python3 timeline.py --session-dir /path/to/session \
    --type state.transition --pkg setuptools \
    --data '{"from":"pending_evaluate","to":"evaluate_done","reason":"gate introduce_new"}'

  # 人类调试：读 timeline
  python3 timeline.py --session-dir /path/to/session --format table
  python3 timeline.py --session-dir /path/to/session --pkg setuptools --format table
  python3 timeline.py --session-dir /path/to/session --type error

  # 机器消费
  python3 timeline.py --session-dir /path/to/session --format json
  python3 timeline.py --session-dir /path/to/session --since "2026-07-28T12:00:00Z"
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def write_event(session_dir: str | Path, type_: str, pkg: str,
                data: dict | None = None) -> None:
    """追加一个事件到 session_dir/timeline.jsonl。

    Best-effort：写入失败只打 stderr，不抛异常、不影响主流程。
    """
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        line = json.dumps({
            "ts": ts,
            "type": type_,
            "pkg": pkg,
            "data": data if data is not None else {},
        }, ensure_ascii=False)
        timeline = Path(session_dir) / "timeline.jsonl"
        with open(timeline, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"[timeline] write failed ({type_}, {pkg}): {e}", file=sys.stderr)


def read_events(session_dir: str | Path, pkg: str | None = None,
                type_: str | None = None, since: str | None = None) -> list[dict]:
    """读取并过滤 timeline 事件。返回事件列表。"""
    timeline = Path(session_dir) / "timeline.jsonl"
    if not timeline.exists():
        return []
    events: list[dict] = []
    with open(timeline, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if pkg and evt.get("pkg") != pkg:
                continue
            if type_ and evt.get("type") != type_:
                continue
            if since and evt.get("ts", "") < since:
                continue
            events.append(evt)
    return events


def _format_table(events: list[dict]) -> None:
    """人类可读的表格输出。"""
    if not events:
        print("(no events)")
        return
    for evt in events:
        ts = evt["ts"][:19].replace("T", " ")  # 只保留日期+时分秒
        type_ = evt["type"]
        pkg = evt.get("pkg", "") or "-"
        data = evt.get("data", {})
        # 从 data 提取一行摘要
        match type_:
            case "state.transition":
                summary = (f"{data.get('from', '?')} → {data.get('to', '?')}"
                           f"  ({data.get('reason', '?')})")
            case "action.start":
                summary = f"action={data.get('action', '?')}"
            case "action.end":
                summary = (f"action={data.get('action', '?')}"
                           f"  result={data.get('result', '?')}"
                           f"  duration={data.get('duration_s', '?')}s")
            case "session.created":
                summary = f"pkgname={data.get('pkgname', '?')}"
            case "session.completed":
                summary = (f"status={data.get('status', '?')}"
                           f"  duration={data.get('duration_s', '?')}s"
                           f"  loops={data.get('loop_count', '?')}")
            case "loop.end":
                summary = (f"action={data.get('action', '?')}"
                           f"  target={data.get('target', '?')}")
            case "loop.wait":
                summary = f"delay={data.get('delay_s', '?')}s  targets={data.get('targets', [])}"
            case "loop.skip":
                summary = (f"action={data.get('action', '?')}"
                           f"  target={data.get('target', '?')}"
                           f"  result={data.get('script_result', '?')}")
            case "build.submitted":
                summary = f"build_id={data.get('build_id', '?')}  srpm={data.get('srpm', '?')}"
            case "build.completed":
                summary = (f"build_id={data.get('build_id', '?')}"
                           f"  status={data.get('status', '?')}"
                           f"  duration={data.get('duration_s', '?')}s")
            case "ci_check.end":
                summary = f"status={data.get('status', '?')}"
            case "error":
                summary = data.get("message", "")[:100]
            case _:
                summary = json.dumps(data, ensure_ascii=False)[:80]
        print(f"{ts}  {type_:<24} {pkg:<22} {summary}")


def _snapshot_statuses(session_dir: Path) -> dict[str, str]:
    """快照当前 session_dir 下所有 pkg 的 status（dep_registry + 主包 workflow）。

    返回 {pkgname: status_string} 字典。
    """
    snap: dict[str, str] = {}

    # dep_registry.json
    reg_path = session_dir / "dep_registry.json"
    if reg_path.exists():
        try:
            reg = json.loads(reg_path.read_text(encoding="utf-8"))
            for pkg, entry in reg.items():
                if isinstance(entry, dict) and "status" in entry:
                    snap[pkg] = str(entry["status"])
        except Exception:
            pass

    # 主包 workflow
    wf_files = list(session_dir.glob("workflow_*.json"))
    if wf_files:
        try:
            wf = json.loads(wf_files[0].read_text(encoding="utf-8"))
            pkgname = wf.get("pkgname", "")
            if pkgname:
                # 主包状态从 build_rpm_result 推断
                result_path = session_dir / f"pkgs/{pkgname}/build_rpm_result.json"
                if result_path.exists():
                    try:
                        br = json.loads(result_path.read_text(encoding="utf-8"))
                        status = br.get("status", "")
                        if status:
                            snap[pkgname] = f"main:{status}"
                    except Exception:
                        pass
                else:
                    # 尚未开始构建
                    gate_path = session_dir / f"pkgs/{pkgname}/gate_result_{pkgname}.json"
                    if gate_path.exists():
                        try:
                            g = json.loads(gate_path.read_text(encoding="utf-8"))
                            decision = (g.get("result") or {}).get("decision", "")
                            if decision in ("reuse_official", "reuse_copr_project",
                                            "reuse_additional_repo"):
                                snap[pkgname] = "main:reused"
                            else:
                                snap[pkgname] = "main:evaluated"
                        except Exception:
                            pass
                    else:
                        snap[pkgname] = "main:pending"
        except Exception:
            pass

    return snap


def diff_and_write_transitions(session_dir: str | Path,
                                before: dict[str, str]) -> dict[str, str]:
    """对比快照与当前状态，为每个变化写一条 state.transition 事件。

    返回当前状态快照（调用方可直接用于下一轮快照）。
    """
    sd = Path(session_dir)
    after = _snapshot_statuses(sd)

    for pkg, new_status in after.items():
        old_status = before.get(pkg, "(new)")
        if old_status != new_status:
            write_event(sd, "state.transition", pkg, {
                "from": old_status,
                "to": new_status,
                "reason": "supervisor",
            })

    return after


# ── CLI ──────────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Timeline event writer/reader for AI importer sessions"
    )
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--type", dest="type_", default="")
    parser.add_argument("--pkg", default="")
    parser.add_argument("--data", default=None,
                        help="JSON payload for write mode (omitted = read mode)")
    parser.add_argument("--format", default="", choices=["table", "json"])
    parser.add_argument("--since", default="")
    args = parser.parse_args()

    sd = Path(args.session_dir)

    # 写入模式：--data 显式提供
    if args.data is not None:
        try:
            data = json.loads(args.data)
        except json.JSONDecodeError as e:
            print(f"[timeline] invalid JSON data: {e}", file=sys.stderr)
            return 1
        write_event(sd, args.type_, args.pkg or "", data)
        return 0

    # 读取模式
    events = read_events(
        sd,
        pkg=args.pkg or None,
        type_=args.type_ or None,
        since=args.since or None,
    )

    if args.format == "table":
        _format_table(events)
    elif args.format == "json":
        print(json.dumps(events, ensure_ascii=False, indent=2))
    else:
        # 默认人类可读
        _format_table(events)

    return 0


if __name__ == "__main__":
    sys.exit(main())
