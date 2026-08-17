#!/usr/bin/env python3
"""将 copr_build_result.json 的结果同步写回 build_rpm_result.json。

用法：
  python3 sync-copr-result.py --session-dir . --pkg hello-openeuler
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--pkg", required=True)
    args = parser.parse_args()

    sd = Path(args.session_dir)
    copr_path = sd / "pkgs" / args.pkg / "copr_build_result.json"
    br_path   = sd / "pkgs" / args.pkg / "build_rpm_result.json"

    if not copr_path.exists():
        print(f"[sync-copr-result] copr_build_result.json not found, skip")
        return

    copr = json.loads(copr_path.read_text(encoding="utf-8"))
    br   = json.loads(br_path.read_text(encoding="utf-8")) if br_path.exists() else {}

    copr_status = copr.get("status", "failed")
    if copr_status == "success":
        br["status"] = "success"
        br["rpms"]   = copr.get("rpms", [])
    elif copr_status == "copr_running":
        br["status"]        = "copr_running"
        br["copr_build_id"] = copr.get("copr_build_id")
    else:
        br["status"]         = "failed"
        br["failure_reason"] = copr.get("failure_reason", "copr build failed")
        # 保留构建日志末尾供 reviewer 分析
        build_log = copr.get("build_log", "")
        br["build_log_tail"] = build_log[-2000:] if build_log else ""

    br["copr_build_id"] = copr.get("copr_build_id")
    # 多 chroot 字段透传（worker 写入 copr_build_result.json 时才有；缺省保持旧结构）
    for key in ("copr_chroots", "copr_build_ids", "copr_chroot"):
        if copr.get(key):
            br[key] = copr[key]
    br_path.write_text(json.dumps(br, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[sync-copr-result] {args.pkg}: copr_status={copr_status} → build_rpm_result.status={br['status']}")

    # 失败时提取结构化错误报告，供 pkg-fixer 诊断（best-effort，不影响主流程）
    if br["status"] == "failed":
        extractor = Path(__file__).parent / "extract-build-failure.py"
        subprocess.run(
            [sys.executable, str(extractor), "--session-dir", str(sd), "--pkg", args.pkg],
            check=False,
        )


if __name__ == "__main__":
    main()
