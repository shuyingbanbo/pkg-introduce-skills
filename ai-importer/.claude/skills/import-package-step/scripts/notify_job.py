#!/usr/bin/env python3
"""
通知 job_runner job 完成，写 redis job hash 和 done log。

用法：
  python3 notify_job.py --session-dir <dir> --status success|failed
"""
import argparse
import json
import os
import pathlib
import sys

import redis

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from dep_chroots import chroot_status_map  # noqa: E402


def _collect_chroot_status(sd: pathlib.Path) -> dict:
    """聚合所有包的 per-chroot 状态，输出与 job_runner 一致的扁平结构
    {chroot: {"status": "succeeded"|"failed"|"skipped", "build_id": <int|null>}}。

    跨包收敛规则（与 job_runner._collect_chroot_status 对齐）：任一 failed→failed，
    否则任一非终态→skipped，全 succeeded→succeeded；build_id 优先取主包
    （session.json 的 pkgname）条目，取不到再取任意包。
    无任何 per-chroot 数据时返回 {}，调用方跳过 chroot_status 字段
    （不影响现有字段、旧格式行为不变）。
    """
    reg_path = sd / "dep_registry.json"
    if not reg_path.exists():
        return {}
    try:
        reg = json.loads(reg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    pkgname = ""
    sess_path = sd / "session.json"
    if sess_path.exists():
        try:
            pkgname = json.loads(sess_path.read_text(encoding="utf-8")).get("pkgname", "")
        except (OSError, json.JSONDecodeError):
            pkgname = ""
    per_chroot = {}
    for pkg, entry in reg.items():
        for c, info in chroot_status_map(entry).items():
            slot = per_chroot.setdefault(
                c, {"statuses": [], "build_id": None, "main_build_id": None})
            slot["statuses"].append(info.get("status", ""))
            bid = info.get("build_id")
            if bid and pkg == pkgname:
                slot["main_build_id"] = bid
            elif bid and slot["build_id"] is None:
                slot["build_id"] = bid
    out = {}
    for c, slot in per_chroot.items():
        mapped = []
        for st in slot["statuses"]:
            if st == "failed":
                mapped.append("failed")
            elif st in ("build_done", "reused"):
                mapped.append("succeeded")
            else:
                # 未到终态（pending/building 等）或显式 skipped 均按 skipped 记
                mapped.append("skipped")
        if "failed" in mapped:
            status = "failed"
        elif all(s == "succeeded" for s in mapped):
            status = "succeeded"
        else:
            status = "skipped"
        build_id = slot["main_build_id"]
        if build_id is None:
            build_id = slot["build_id"]
        out[c] = {"status": status, "build_id": build_id}
    return out


def notify(session_dir: str, status: str) -> None:
    sd = pathlib.Path(session_dir)
    job_id = sd.name  # session 目录名 = job_id

    wf_files = list(sd.glob("workflow_*.json"))
    wf = json.loads(wf_files[0].read_text()) if wf_files else {}

    fields = {
        "status":      status,
        "built_pkgs":  " ".join(wf.get("built_pkgs", [])),
        "reused_pkgs": " ".join(wf.get("reused_pkgs", [])),
        "loop_count":  str(wf.get("loop_count", "")),
        "error":       (wf.get("error") or wf.get("failure_reason") or "")
                       if status == "failed" else "",
    }

    # 多 chroot（§8.1）：有 per-chroot 数据才写 chroot_status，旧格式 job 不写该字段
    chroot_status = _collect_chroot_status(sd)
    if chroot_status:
        fields["chroot_status"] = json.dumps(chroot_status, ensure_ascii=False)

    host = os.environ.get("REDIS_HOST", "redis")
    r = redis.Redis(host=host, port=6379, decode_responses=True)
    r.hset(f"job:ai:{job_id}", mapping=fields)
    r.rpush(f"logs:ai:{job_id}", json.dumps({"done": True, "status": status}))

    built  = fields["built_pkgs"]
    err    = fields["error"]
    suffix = f"  built={built}" if built else ""
    suffix += f"  reason={err}" if err else ""
    print(f"[引包] 完成  status={status}{suffix}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--session-dir", required=True)
    p.add_argument("--status", required=True, choices=["success", "failed"])
    args = p.parse_args()
    try:
        notify(args.session_dir, args.status)
    except Exception as e:
        print(f"[notify_job] warning: {e}", file=sys.stderr)
        sys.exit(1)
