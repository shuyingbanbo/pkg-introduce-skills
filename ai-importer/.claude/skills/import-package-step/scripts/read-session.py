#!/usr/bin/env python3
"""从 session.json 读取所有字段，输出 shell eval 可用的 export 语句。

用法：
  eval "$(python3 read-session.py --session-dir /path/to/session)"

输出示例：
  export COPR_FRONTEND_URL='http://copr-frontend:5000'
  export COPR_OWNER='openeuler-ai'
  ...
"""
import argparse
import json
import shlex
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--field", default="", help="只输出指定字段的值（不带 export）")
    args = parser.parse_args()

    sd = Path(args.session_dir)
    session_file = sd / "session.json"
    if not session_file.exists():
        print(f"ERROR: session.json not found: {session_file}", file=sys.stderr)
        sys.exit(1)

    s = json.loads(session_file.read_text(encoding="utf-8"))

    if args.field:
        print(s.get(args.field, ""))
        return

    # 多 chroot：copr_chroots（list）→ COPR_CHROOTS 逗号分隔；
    # COPR_CHROOT = 主 chroot（排序后第一个 -x86_64 结尾的，否则排序后第一个）。
    # 旧 session 只有 copr_chroot 单值时输出与旧版一致（不导出 COPR_CHROOTS）。
    copr_chroots = s.get("copr_chroots") or []
    if isinstance(copr_chroots, str):
        copr_chroots = [c.strip() for c in copr_chroots.split(",") if c.strip()]
    if copr_chroots:
        ordered = sorted(copr_chroots)
        primary = next((c for c in ordered if c.endswith("-x86_64")), ordered[0])
    else:
        primary = s.get("copr_chroot", "")

    mapping = [
        ("COPR_FRONTEND_URL", s.get("copr_url", "http://copr-frontend:5000")),
        ("COPR_OWNER",        s.get("copr_owner", "")),
        ("COPR_PROJECT",      s.get("copr_project", "")),
        ("COPR_API_LOGIN",    s.get("copr_login", "")),
        ("COPR_API_TOKEN",    s.get("copr_token", "")),
        ("COPR_CHROOT",       primary),
        ("SESSION_UPSTREAM_URL", s.get("upstream_url", "")),
        ("SESSION_PKGNAME",   s.get("pkgname", "")),
        ("SESSION_VERSION",   s.get("version", "")),
    ]
    if copr_chroots:
        mapping.insert(6, ("COPR_CHROOTS", ",".join(copr_chroots)))
    for k, v in mapping:
        print(f"export {k}={shlex.quote(str(v))}")


if __name__ == "__main__":
    main()
