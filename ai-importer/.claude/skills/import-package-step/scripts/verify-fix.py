#!/usr/bin/env python3
"""pkg-fixer 提交前验证关口：确认 spec 修改真实落地且基本合法。

校验项（按序，任一不过即退出）：
  退出码 1：spec 与上一轮 submitted_specs/ 快照无 diff（空 patch，拒绝提交）
  退出码 2：fix_report.json 自报的改动点（after 文本）未体现在 spec 中
  退出码 3：rpmlint 报错（rpmlint 不存在时跳过并告警）
  退出码 4：rpmbuild --nobuild 验证 %prep 失败（需 --build-dir，可选）

全部通过 → 退出码 0。调用方（pkg-fixer）随后自行完成
快照存档（提交拿到 build_id 后）+ rpmbuild -bs + COPR 提交。

用法：
  python3 verify-fix.py --session-dir . --pkg git \
      [--report ./pkgs/git/fix_report.json] [--build-dir ./build]
"""

import argparse
import glob
import json
import shutil
import subprocess
import sys
from pathlib import Path


def _latest_snapshot(pkg_dir: Path) -> Path | None:
    files = sorted(glob.glob(str(pkg_dir / "submitted_specs" / "spec_*.spec")))
    return Path(files[-1]) if files else None


def main() -> int:
    parser = argparse.ArgumentParser(description="fixer 提交前验证")
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--pkg", required=True)
    parser.add_argument("--report", default="", help="fix_report.json 路径（可选）")
    parser.add_argument("--build-dir", default="", help="rpmbuild topdir（可选，提供则校验 %prep）")
    args = parser.parse_args()

    pkg_dir = Path(args.session_dir) / "pkgs" / args.pkg
    spec_path = pkg_dir / f"{args.pkg}.spec"
    if not spec_path.exists():
        print("[verify-fix] spec 不存在，无法验证", file=sys.stderr)
        return 1
    spec_text = spec_path.read_text(encoding="utf-8")

    # 1. 与上一轮 submitted 快照必须有 diff（无快照 = 首次，跳过）
    snapshot = _latest_snapshot(pkg_dir)
    if snapshot is not None:
        if snapshot.read_text(encoding="utf-8") == spec_text:
            print(f"[verify-fix] FAIL: spec 与上一轮提交快照 {snapshot.name} 无 diff，拒绝空转提交", file=sys.stderr)
            return 1

    # 2. fix_report 自报改动必须落地
    if args.report:
        report_path = Path(args.report)
        if report_path.exists():
            entries = json.loads(report_path.read_text(encoding="utf-8"))
            if not isinstance(entries, list):
                entries = entries.get("changes", [])
            missing = []
            for entry in entries:
                after = entry.get("after", "") if isinstance(entry, dict) else ""
                if after and after not in spec_text:
                    missing.append(entry.get("description", after[:60]))
            if missing:
                print(f"[verify-fix] FAIL: 自报改动未落地: {missing}", file=sys.stderr)
                return 2

    # 3. rpmlint
    if shutil.which("rpmlint"):
        proc = subprocess.run(
            ["rpmlint", str(spec_path)], capture_output=True, text=True
        )
        errors = [l for l in proc.stdout.splitlines() if " E: " in l or l.endswith("(Badness: exceeded)")]
        if errors:
            print(f"[verify-fix] FAIL: rpmlint 报错:\n" + "\n".join(errors[:20]), file=sys.stderr)
            return 3
    else:
        print("[verify-fix] WARN: rpmlint 不存在，跳过静态检查", file=sys.stderr)

    # 4. %prep 验证（可选）
    if args.build_dir:
        build_dir = Path(args.build_dir)
        spec_copy = build_dir / "SPECS" / f"{args.pkg}.spec"
        if spec_copy.exists():
            proc = subprocess.run(
                ["rpmbuild", "--nobuild", "--nodeps",
                 "--define", f"_topdir {build_dir.resolve()}",
                 str(spec_copy)],
                capture_output=True, text=True,
            )
            if proc.returncode != 0:
                tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-30:])
                print(f"[verify-fix] FAIL: rpmbuild --nobuild 未通过:\n{tail}", file=sys.stderr)
                return 4

    print("[verify-fix] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
