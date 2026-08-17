#!/usr/bin/env python3
"""向 dep_registry.json 注册一个需要引入的依赖。

用法：
  python3 register-dep.py --session-dir . --pkg meson \
    --url https://github.com/mesonbuild/meson \
    --constraint ">= 1.4.0" \
    --required-by python-numpy

若该依赖已登记过且新旧 --constraint 不同：两者不冲突时自动合并为同时满足
两者的约束；冲突时（如已登记 ">=2.0"，新约束要求 "<1.5"）报错退出（exit 1），
不静默覆盖旧约束，调用方（pkg-fixer 等）需要处理这种不可能同时
满足的依赖版本要求。
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from constraint_conflict import has_conflict, merge_constraints  # noqa: E402

# 引入构建工具链约束（manifest 生成的脚本目录）
BUILD_RPM_SCRIPTS = Path(__file__).resolve().parents[2] / "build-rpm" / "scripts"
sys.path.insert(0, str(BUILD_RPM_SCRIPTS))
from chroot_toolchain import is_toolchain, get_tool_version  # noqa: E402
from rpm_naming import rpm_name_from_gav  # noqa: E402

# 可信的 git 仓库主机
_TRUSTED_GIT_HOSTS = (
    "github.com",
    "gitlab.com",
    "gitee.com",
    "codeberg.org",
    "bitbucket.org",
    "salsa.debian.org",
    "pagure.io",
    "git.sr.ht",
)

# 明确不是 git 仓库的 URL 特征
_NON_REPO_PATTERNS = re.compile(
    r"(pypi\.org|npmjs\.com|crates\.io|rubygems\.org"
    r"|mesonbuild\.com|cmake\.org|gnu\.org/software"
    r"|readthedocs|docs\.|wiki\.|/releases/download/)"
)


def is_git_repo_url(url: str) -> tuple[bool, str]:
    """检查 URL 是否是可 git clone 的仓库地址。

    返回 (ok, reason)。
    """
    if not url:
        return False, "URL 为空"

    # 拒绝明显不是 git 仓库的地址
    if _NON_REPO_PATTERNS.search(url):
        return False, f"URL 看起来是官网/文档/包注册表，不是 git 仓库: {url}"

    # 必须是 https:// 或 http://
    if not (url.startswith("https://") or url.startswith("http://")):
        return False, f"URL 必须以 https:// 或 http:// 开头: {url}"

    # 提取主机名
    try:
        host = url.split("//", 1)[1].split("/")[0].lower()
        path = "/".join(url.split("//", 1)[1].split("/")[1:])
    except IndexError:
        return False, f"URL 格式无效: {url}"

    # 检查主机是否可信
    trusted = any(host == h or host.endswith("." + h) for h in _TRUSTED_GIT_HOSTS)
    if not trusted:
        # 未知主机时，要求路径至少有 owner/repo 两段
        parts = [p for p in path.split("/") if p]
        if len(parts) < 2:
            return False, f"未知主机且路径不像 owner/repo 格式: {url}"
        # 未知主机但路径格式合理，警告但允许
        return True, f"警告：未知主机 {host}，请确认是 git 仓库"

    # 可信主机下，路径需要有 owner/repo 两段
    parts = [p for p in path.rstrip("/").split("/") if p]
    if len(parts) < 2:
        return False, f"路径缺少 owner/repo，不像 git 仓库: {url}"

    return True, "ok"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--pkg", required=True, help="要引入的包名")
    parser.add_argument("--url", default="", help="upstream git 仓库 URL")
    parser.add_argument("--constraint", default="", help="版本约束，如 '>= 1.4.0'")
    parser.add_argument("--required-by", default="", help="哪个包需要它")
    parser.add_argument("--lang", default="",
                        help="依赖语言（仅注册方确知是 crate/module 时携带，如 rust/go；"
                             "包级依赖即使知道是 rust 写的也不要带）")
    parser.add_argument("--skip-url-check", action="store_true", help="跳过 URL 格式校验（慎用）")
    args = parser.parse_args()

    # URL 校验
    if args.url and not args.skip_url_check:
        ok, reason = is_git_repo_url(args.url)
        if not ok:
            print(f"[register-dep] ERROR: URL 校验失败 — {reason}", file=sys.stderr)
            print(f"[register-dep] 请提供可 git clone 的仓库地址（如 https://github.com/owner/repo）", file=sys.stderr)
            sys.exit(1)
        if reason != "ok":
            print(f"[register-dep] {reason}", file=sys.stderr)

    # constraint 为空时警告
    if not args.constraint:
        print(f"[register-dep] WARNING: --constraint 未指定，evaluator 将选最新稳定版而非最小满足版本。"
              f"建议明确指定版本约束（如 '>= 1.4.0'）。", file=sys.stderr)

    # GAV 名归一化：'com.google.guava:guava' → 'guava'，防止同一依赖双名注册
    args.pkg = rpm_name_from_gav(args.pkg)

    # 构建工具链硬过滤：禁止引入/升级构建工具
    if is_toolchain(args.pkg):
        tool_version = get_tool_version(args.session_dir, args.pkg)
        version_hint = f" (chroot has {tool_version})" if tool_version else ""
        print(f"[register-dep] ERROR: {args.pkg} is a toolchain package{version_hint}. "
              f"Do not introduce build tools into dep_registry. "
              f"Adapt spec/source to the chroot toolchain version instead.", file=sys.stderr)
        sys.exit(2)

    # ROS 依赖名两级校验：ros-<distro>-<name> 必须真实存在——
    #   Tier 1: ros-projects.list（SIG 源已有）→ 正常注册
    #   Tier 2: 仅 ros-upstream.list（rosdistro 真实存在、SIG 未移植）→ 放行注册，
    #           URL 缺省时自动从 upstream 清单补（递归构建 SIG 未覆盖的包）
    #   两级都查不到 = spec 里的幻觉依赖名——拒绝注册（历史教训：
    #   ros-humble-ament-python 被"构建"成 README-only 空壳包）。
    from ros_dep_guard import (  # noqa: E402
        format_invalid_report, lookup_ros_dep, lookup_upstream_dep,
        split_ros_name, suggest_ros_names,
    )
    from analyze_ros_deps import load_projects, load_upstream  # noqa: E402
    ros_parts = split_ros_name(args.pkg)
    if ros_parts:
        ros_distro, ros_name = ros_parts
        projects = load_projects(ros_distro)
        if projects and lookup_ros_dep(ros_name, projects) is None:
            upstream = load_upstream(ros_distro)
            if lookup_upstream_dep(ros_name, upstream) is not None:
                if not args.url:
                    args.url = upstream[lookup_upstream_dep(ros_name, upstream)][0]
                if not args.lang:
                    # 对齐 ros_prep deep 模式：supervisor 对 lang=ros 的 dep
                    # 跳过普通 evaluate，注册即 evaluate_done
                    args.lang = "ros"
                print(f"[register-dep] INFO: {args.pkg} SIG 源未移植（rosdistro 真实存在），"
                      f"注册递归构建，url={args.url!r}", file=sys.stderr)
            else:
                bad = {ros_name: suggest_ros_names(ros_name, projects)}
                print(f"[register-dep] ERROR: 拒绝注册。\n{format_invalid_report(bad, ros_distro)}",
                      file=sys.stderr)
                sys.exit(3)

    reg_path = Path(args.session_dir) / "dep_registry.json"
    reg = json.loads(reg_path.read_text(encoding="utf-8")) if reg_path.exists() else {}

    if args.pkg in reg:
        old = reg[args.pkg]
        changed = []
        if args.lang and not old.get("lang"):
            old["lang"] = args.lang.strip().lower()
            changed.append("lang")
        if args.url and not old.get("url"):
            # 已有条目补充 URL 时也校验
            if not args.skip_url_check:
                ok, reason = is_git_repo_url(args.url)
                if not ok:
                    print(f"[register-dep] ERROR: URL 校验失败 — {reason}", file=sys.stderr)
                    sys.exit(1)
            old["url"] = args.url
            changed.append("url")
        if args.constraint and args.constraint != old.get("constraint", ""):
            old_constraint = old.get("constraint", "")
            if old_constraint:
                conflict, reason = has_conflict(old_constraint, args.constraint)
                if conflict:
                    print(f"[register-dep] ERROR: {args.pkg} 版本约束冲突 — "
                          f"已登记 {old_constraint!r}，新约束 {args.constraint!r}：{reason}",
                          file=sys.stderr)
                    sys.exit(1)
                merged = merge_constraints(old_constraint, args.constraint)
                if merged != old_constraint:
                    old["constraint"] = merged
                    changed.append("constraint")
            else:
                old["constraint"] = args.constraint
                changed.append("constraint")
        if changed:
            reg_path.write_text(json.dumps(reg, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"[register-dep] updated {args.pkg}: {changed}")
        else:
            print(f"[register-dep] {args.pkg} already registered, no change")
        return

    # 新条目不带 chroots 键：evaluate 阶段 chroot 无关（§8.1），
    # per-chroot 状态由构建阶段的 step_supervisor 按需建立；
    # 已有条目走整字典读-改-写，chroots 等未知键天然保留。
    reg[args.pkg] = {
        "url": args.url,
        "constraint": args.constraint,
        "status": "pending_evaluate",
        "required_by": args.required_by,
    }
    if args.lang:
        # 仅 crate/module 类依赖携带 lang（见 --lang 帮助）；supervisor 据此
        # 在 pending_evaluate 路由前置 vendor_only 终态，跳过 evaluate/build。
        reg[args.pkg]["lang"] = args.lang.strip().lower()
    reg_path.write_text(json.dumps(reg, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[register-dep] registered {args.pkg} (url={args.url!r}, constraint={args.constraint!r})")


if __name__ == "__main__":
    main()
