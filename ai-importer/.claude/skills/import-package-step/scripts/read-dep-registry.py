#!/usr/bin/env python3
"""从 dep_registry.json 读取指定包的字段。

用法：
  python3 read-dep-registry.py --session-dir . --pkg setuptools --field url
  python3 read-dep-registry.py --session-dir . --pkg setuptools --field constraint
  python3 read-dep-registry.py --session-dir . --pkg setuptools --field chroots  # JSON
  python3 read-dep-registry.py --session-dir . --pkg setuptools  # 输出所有字段的 export

多 chroot（§8.1）：条目带 chroots 键时，--field chroots 输出该映射的 JSON；
不带 --field 时除包级字段外，逐 chroot 输出
  export DEP_CHROOT_<CHROOT>_STATUS=... / DEP_CHROOT_<CHROOT>_BUILD_ID=...
（<CHROOT> 非字母数字字符替换为 '_'）。无 chroots 键的旧条目输出与旧版完全一致。
"""
import argparse
import json
import re
import shlex
import sys
from pathlib import Path

# 引入 GAV 名归一化（读侧与注册侧对齐）
BUILD_RPM_SCRIPTS = Path(__file__).resolve().parents[2] / "build-rpm" / "scripts"
sys.path.insert(0, str(BUILD_RPM_SCRIPTS))
from rpm_naming import rpm_name_from_gav  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--pkg", required=True)
    parser.add_argument("--field", default="", help="只输出指定字段的值")
    args = parser.parse_args()

    reg_file = Path(args.session_dir) / "dep_registry.json"
    if not reg_file.exists():
        if args.field:
            print("")
        sys.exit(0)

    reg = json.loads(reg_file.read_text(encoding="utf-8"))
    # GAV 名归一化：调用方传 'com.google.guava:guava' 也能命中 'guava' 条目
    entry = reg.get(rpm_name_from_gav(args.pkg), {})
    if isinstance(entry, str):
        entry = {"url": entry}

    if args.field:
        value = entry.get(args.field, "")
        # chroots 等结构化字段输出 JSON，避免 python repr 不可解析
        if isinstance(value, (dict, list)):
            print(json.dumps(value, ensure_ascii=False))
        else:
            print(value)
        return

    for k, v in entry.items():
        if k == "chroots":
            continue  # per-chroot 状态单独逐条输出（见下）
        print(f"export DEP_{k.upper()}={shlex.quote(str(v))}")

    # per-chroot 状态（§8.1）：逐 chroot 列出 status/build_id
    chroots = entry.get("chroots")
    if isinstance(chroots, dict):
        for chroot, cinfo in chroots.items():
            if not isinstance(cinfo, dict):
                continue
            key = re.sub(r"[^A-Za-z0-9]", "_", chroot).upper()
            print(f"export DEP_CHROOT_{key}_STATUS={shlex.quote(str(cinfo.get('status', 'pending')))}")
            build_id = cinfo.get("build_id")
            if build_id is not None:
                print(f"export DEP_CHROOT_{key}_BUILD_ID={shlex.quote(str(build_id))}")


if __name__ == "__main__":
    main()
