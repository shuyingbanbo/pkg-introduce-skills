#!/usr/bin/env python3
"""spec Requires/BuildRequires provider 预检（提交 COPR 前强制执行）。

背景：无 provider 的 BuildRequires 在 mock builddep 阶段即失败（损失尚小），
但无 provider 的 Requires 要等构建成功后的 CI 可安装性检查才暴露——白烧
一整轮构建（ros2-numpy 事故：构建 2 小时成功后 CI 才报 python3-transforms3d
无 provider）。本脚本在提交前用与 CI 完全相同的 repo 集合（官方
everything/update/EPOL + COPR result repo + 项目 additional_repos）逐一验证
spec 声明的依赖都有 provider。

用法：
  python3 verify_spec_requires.py <spec_path> --session-dir <sd> [--pkg <name>]
  python3 verify_spec_requires.py <spec_path> --session-dir <sd> --register-missing

退出码：
  0  全部有 provider（或环境不可用降级放行，stderr 有 WARN）
  1  存在无 provider 的依赖（输出缺失清单与引入指引）
  2  参数/环境错误
  3  缺口已注册为待引入依赖（--register-missing 模式）——调用方禁止本次
     提交，等依赖构建完成后再提交主包
  4  完整性校验未通过：package.xml 声明的依赖被静默丢弃（未写进 spec、
     未注册、未显式豁免）——仅 ROS 场景（存在 pre_check 分析）生效
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
# run_ci_check 提供与 CI 门禁完全一致的 repo 集合（官方源 + COPR result +
# additional_repos），保证"预检通过 ⇒ CI 可安装性必然通过"
CI_SCRIPTS = SCRIPT_DIR.parents[1] / "pkg-introduce" / "scripts"
REGISTER_DEP = (SCRIPT_DIR.parents[1] / "import-package-step" / "scripts"
                / "register-dep.py")
for _p in (str(CI_SCRIPTS),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from run_ci_check import (  # noqa: E402
    _chroot_arch,
    _chroot_repo_base,
    _copr_repo_accessible,
    _extra_repos,
    _get_copr_result_url,
)

_REQ_RE = re.compile(r"^(Build)?Requires\s*:\s*(.+)$", re.IGNORECASE)
_OPS = {">=", "<=", "=", ">", "<", "!=", "<>"}
# 语言前缀（自引用判定用）：python3-transforms3d 之于 python-transforms3d 主包
_LANG_PREFIXES = ("python3-", "python-", "nodejs-", "rubygem-", "golang-",
                  "perl-", "rust-")


def _spec_name(spec_text: str) -> str:
    m = re.search(r"^Name\s*:\s*(\S+)", spec_text, re.MULTILINE)
    return m.group(1) if m else ""


def _parse_requires(spec_text: str, ros_distro: str, name: str) -> list[str]:
    """提取 Requires/BuildRequires 的能力名（去版本约束、展开常用宏、去自引用）。"""
    caps: list[str] = []
    for line in spec_text.splitlines():
        line = line.split("#", 1)[0].strip()
        m = _REQ_RE.match(line)
        if not m:
            continue
        value = m.group(2)
        value = value.replace("%{ros_distro}", ros_distro or "humble")
        if name:
            value = value.replace("%{name}", name)
        for tok in re.split(r"[\s,]+", value):
            tok = tok.strip()
            if not tok or tok in _OPS or tok[0].isdigit():
                continue
            if "%" in tok or tok.startswith(("rpmlib(", "rtld(", "config(")):
                continue
            caps.append(tok)
    # 去重并保持顺序
    return list(dict.fromkeys(caps))


def _is_self_reference(cap: str, name: str) -> bool:
    """spec 主包/子包自提供的依赖（首次构建前任何源都查不到，须排除）。"""
    if not name:
        return False
    if cap == name or cap.startswith(name + "-"):
        return True
    stem = name
    for pfx in _LANG_PREFIXES:
        if stem.startswith(pfx):
            stem = stem[len(pfx):]
            break
    return bool(stem) and len(stem) > 3 and stem in cap


def _repo_flags(session_dir: Path) -> tuple[list[str], str]:
    """构造与 CI 相同的 repo 集合的 dnf 参数；返回 (flags, chroot)。"""
    chroot, result_url, additional_repos = _get_copr_result_url(session_dir)
    if not chroot:
        return [], ""
    base = _chroot_repo_base(chroot)
    if not base:
        print(f"[verify_spec_requires] WARN: 未知 chroot {chroot}，跳过预检",
              file=sys.stderr)
        return [], ""
    arch = _chroot_arch(chroot)
    # 与 CI 的 install 检查同模式：--disablerepo=* + --enablerepo=<id>
    # （本版 dnf 的 --repo 与 --disablerepo 互斥，用 --enablerepo）；
    # --forcearch 必须带：宿主 pod 与目标 chroot 架构不同（x86_64 pod 查
    # aarch64 源）时 dnf 按宿主架构过滤，全部误报无 provider
    flags = ["--disablerepo=*", f"--forcearch={arch}"]
    for repoid, url in [
            ("pre-oe-official", f"{base}/everything/{arch}/"),
            ("pre-oe-update", f"{base}/update/{arch}/"),
            ("pre-oe-epol", f"{base}/EPOL/main/{arch}/")]:
        flags += ["--repofrompath", f"{repoid},{url}", f"--enablerepo={repoid}"]
    if result_url and _copr_repo_accessible(result_url):
        flags += ["--repofrompath", f"pre-copr-result,{result_url}",
                  "--enablerepo=pre-copr-result"]
    else:
        print("[verify_spec_requires] INFO: COPR result repo 暂不可达"
              "（首次构建？），仅按官方源+外挂源检查", file=sys.stderr)
    for repoid, url in _extra_repos(additional_repos, arch):
        flags += ["--repofrompath", f"{repoid},{url}", f"--enablerepo={repoid}"]
    return flags, chroot


def _has_provider(cap: str, flags: list[str]) -> bool | None:
    """True=有 provider；False=确定没有；None=查询失败（网络等，按有处理）。"""
    cmd = ["dnf", "repoquery", "--quiet", "--whatprovides", cap, *flags]
    try:
        rc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except Exception as exc:
        print(f"[verify_spec_requires] WARN: repoquery {cap} 异常: {exc}",
              file=sys.stderr)
        return None
    if rc.returncode != 0 and not rc.stdout.strip():
        err = rc.stderr.strip()
        # dnf 对"无匹配"返回 0 空输出；非 0 多为网络/元数据问题
        print(f"[verify_spec_requires] WARN: repoquery {cap} 失败: {err[:200]}",
              file=sys.stderr)
        return None
    return bool(rc.stdout.strip())


def _load_analysis(session_dir: Path, pkg: str) -> dict | None:
    """ROS pre_check 依赖分析（reports/pre_check_<pkg>_analysis.json）。

    文件不存在（非 ROS 场景 / 分析未产出）返回 None，调用方跳过完整性校验。
    pkg 命名在连字符/下划线间可能漂移（ros2-numpy vs ros2_numpy），两种形态都试。
    """
    if not pkg:
        return None
    reports = session_dir / "reports"
    for cand in (reports / f"pre_check_{pkg}_analysis.json",
                 reports / f"pre_check_{pkg.replace('-', '_')}_analysis.json",
                 reports / f"pre_check_{pkg.replace('_', '-')}_analysis.json"):
        if cand.exists():
            try:
                return json.loads(cand.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"[verify_spec_requires] WARN: 解析 {cand} 失败: {exc}",
                      file=sys.stderr)
                return None
    return None


def _expected_deps(analysis: dict, ros_distro: str) -> list[str]:
    """package.xml 声明的依赖应出现在 spec 中的名字清单（spec 形态）。

    ros_deps/ros_deps_upstream → ros-<distro>-<name>；build_requires（remap
    已映射为 rpm 名）与 unresolved（名字通常即 RPM 名）原样。test_deps 按
    纪律本就不写 spec，不在分析输出的这些字段里，无需排除。
    """
    distro = ros_distro or "humble"
    expected = [f"ros-{distro}-{d.replace('_', '-')}"
                for d in (analysis.get("ros_deps") or [])
                + (analysis.get("ros_deps_upstream") or [])]
    expected += list(analysis.get("build_requires") or [])
    expected += list(analysis.get("unresolved") or [])
    return list(dict.fromkeys(expected))


def _load_waivers(session_dir: Path, pkg: str) -> set[str]:
    """显式豁免清单 pkgs/<pkg>/waived_deps.txt：每行 `<dep> # <理由>`。

    无理由注释的行不予认可（防空豁免变相绕过门禁），stderr 告警。
    """
    waived: set[str] = set()
    if not pkg:
        return waived
    path = session_dir / "pkgs" / pkg / "waived_deps.txt"
    if not path.exists():
        return waived
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        dep, sep, reason = line.partition("#")
        dep = dep.strip()
        if not dep:
            continue
        if not sep or not reason.strip():
            print(f"[verify_spec_requires] WARN: 豁免条目无理由，不予认可: {line}",
                  file=sys.stderr)
            continue
        waived.add(dep)
    return waived


def _completeness_check(session_dir: Path, pkg: str, ros_distro: str,
                        caps: list[str]) -> list[str]:
    """反向完整性校验：package.xml 声明的依赖是否都进了 spec。

    返回被静默丢弃的依赖清单（空 = 通过）。防的是 ros2-numpy 事故的另一面：
    provider 预检只管"spec 里写了的依赖有没有 provider"，管不到"该写的依赖
    被 agent 静默删掉"——删掉后预检与 CI 全过，但包装上是功能残废品。
    """
    analysis = _load_analysis(session_dir, pkg)
    if analysis is None:
        return []
    waived = _load_waivers(session_dir, pkg)
    present = set(caps)
    dropped = [d for d in _expected_deps(analysis, ros_distro)
               if d not in present and d not in waived]
    return dropped


def _register_missing(missing: list[str], session_dir: Path, pkg: str) -> list[str]:
    """把缺失依赖注册进 dep_registry（上游 URL 留空，由 resolve_upstream 经
    PyPI/npm 等 API 解析）。返回注册成功的名单。"""
    registered: list[str] = []
    for cap in missing:
        cmd = [sys.executable, str(REGISTER_DEP),
               "--session-dir", str(session_dir), "--pkg", cap,
               "--required-by", pkg or "main"]
        try:
            rc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except Exception as exc:
            print(f"[verify_spec_requires] WARN: 注册 {cap} 异常: {exc}",
                  file=sys.stderr)
            continue
        if rc.returncode == 0:
            registered.append(cap)
        else:
            print(f"[verify_spec_requires] WARN: 注册 {cap} 失败: "
                  f"{rc.stderr.strip()[:200]}", file=sys.stderr)
    return registered


def main() -> int:
    parser = argparse.ArgumentParser(description="spec Requires provider 提交前预检")
    parser.add_argument("spec_path")
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--pkg", default="", help="主包名（注册依赖时的 required-by）")
    parser.add_argument("--register-missing", action="store_true",
                        help="缺失依赖自动注册进 dep_registry（递归引入），退出码 3")
    args = parser.parse_args()

    spec_path = Path(args.spec_path)
    if not spec_path.exists():
        print(f"[verify_spec_requires] ERROR: spec 不存在: {spec_path}",
              file=sys.stderr)
        return 2
    session_dir = Path(args.session_dir)
    session = {}
    sj = session_dir / "session.json"
    if sj.exists():
        try:
            session = json.loads(sj.read_text(encoding="utf-8"))
        except Exception:
            pass
    ros_distro = session.get("ros_distro", "")

    spec_text = spec_path.read_text(encoding="utf-8", errors="replace")
    name = _spec_name(spec_text)
    caps = [c for c in _parse_requires(spec_text, ros_distro, name)
            if not _is_self_reference(c, name)]

    # 反向完整性校验（ROS 场景，存在 pre_check 分析时）：package.xml 声明的
    # 依赖必须写进 spec、或已注册递归引入后写进 spec、或显式豁免——禁止静默
    # 丢弃。被丢弃的依赖不出现在 spec 里，provider 预检与 CI 都查不到它，
    # 但装上的包运行时 import 直接失败（ros2-numpy/python3-transforms3d 事故）
    dropped = _completeness_check(session_dir, args.pkg, ros_distro, caps)
    if dropped:
        print(json.dumps({"dropped": dropped}, ensure_ascii=False))
        print(f"[verify_spec_requires] FAIL: package.xml 声明的依赖未写入 spec"
              f"（静默丢弃）: {', '.join(dropped)}\n"
              f"处置三选一：①写入 spec 的 Requires/BuildRequires（无 provider 的"
              f"会由本脚本 --register-missing 自动注册递归引入）；②确属多余依赖，"
              f"在 pkgs/{args.pkg or '<pkg>'}/waived_deps.txt 显式豁免并注明理由"
              f"（格式: <dep> # <理由>）；③依赖名写错的，修正 spec",
              file=sys.stderr)
        return 4

    if not caps:
        print("[verify_spec_requires] OK: spec 无需校验的 Requires")
        return 0

    flags, chroot = _repo_flags(session_dir)
    if not flags:
        # 环境不可用：降级放行（不阻塞构建，CI 仍是最终门禁）
        return 0

    missing: list[str] = []
    for cap in caps:
        ok = _has_provider(cap, flags)
        if ok is False:
            missing.append(cap)
    if not missing:
        print(f"[verify_spec_requires] OK: {len(caps)} 个依赖在 {chroot} 全部有 provider")
        return 0

    guidance = (f"spec 声明的依赖在所有已配置源（官方 everything/update/EPOL"
                f"{'+COPR result' if 'pre-copr-result' in ' '.join(flags) else ''}"
                f"+additional_repos）均无 provider: {', '.join(missing)}")
    if args.register_missing:
        registered = _register_missing(missing, session_dir, args.pkg)
        if registered:
            print(json.dumps({"missing": missing, "registered": registered},
                             ensure_ascii=False))
            print(f"[verify_spec_requires] {guidance}\n"
                  f"已注册为待引入依赖: {', '.join(registered)} —— "
                  f"禁止本次提交，等依赖构建完成后再提交主包", file=sys.stderr)
            return 3
    print(json.dumps({"missing": missing}, ensure_ascii=False))
    print(f"[verify_spec_requires] FAIL: {guidance}\n"
          f"请先提交这些依赖的引包任务（成功后再提交本包），"
          f"或修正 spec 中错误的依赖名", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
