#!/usr/bin/env python3
"""
CI 门禁（COPR 模式）：本地执行 repoclosure + dnf install + dnf builddep，无 Docker。

三级检查（强度递进）：
  - repoclosure：依赖闭合（每个 Requires 在 repo 集合里有提供者）
  - dnf install：可安装性（干净 installroot 真跑安装事务，装完即删）
  - dnf builddep：编译期依赖（BuildRequires 可满足）

repo 集合：
  - 官方源：对应 chroot 版本的 openEuler repo
  - COPR project 源：本次构建的包
  - COPR project additional_repos：项目级外部源（如 ROS SIG 源）

用法：
  python3 run_ci_check.py \
    --pkgs python3-foo python3-bar \
    --session-dir /tmp/claude-ws/foo-abc123 \
    --reports-dir ./pkgs/foo

exit codes:
  0  全部通过
  1  检查失败
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# openEuler chroot name → repo base URL
_CHROOT_REPO_MAP = {
    "openeuler-22.03_LTS-":      "http://repo.openeuler.org/openEuler-22.03-LTS",
    "openeuler-22.03_LTS_SP1-":  "http://repo.openeuler.org/openEuler-22.03-LTS-SP1",
    "openeuler-22.03_LTS_SP2-":  "http://repo.openeuler.org/openEuler-22.03-LTS-SP2",
    "openeuler-22.03_LTS_SP3-":  "http://repo.openeuler.org/openEuler-22.03-LTS-SP3",
    "openeuler-22.03_LTS_SP4-":  "http://repo.openeuler.org/openEuler-22.03-LTS-SP4",
    "openeuler-24.03_LTS-":      "http://repo.openeuler.org/openEuler-24.03-LTS",
    "openeuler-24.03_LTS_SP1-":  "http://repo.openeuler.org/openEuler-24.03-LTS-SP1",
    "openeuler-24.03_LTS_SP2-":  "http://repo.openeuler.org/openEuler-24.03-LTS-SP2",
    "openeuler-24.03_LTS_SP3-":  "http://repo.openeuler.org/openEuler-24.03-LTS-SP3",
    "openeuler-24.03_LTS_SP4-":  "http://repo.openeuler.org/openEuler-24.03-LTS-SP4",
}


def _chroot_repo_base(chroot: str) -> str | None:
    for prefix, base in _CHROOT_REPO_MAP.items():
        if chroot.startswith(prefix):
            return base
    return None


def _chroot_arch(chroot: str) -> str:
    return "aarch64" if chroot.endswith("-aarch64") else "x86_64"


def _extra_repos(additional_repos: list[str], arch: str) -> list[tuple[str, str]]:
    """把 COPR project 的 additional_repos 归一化为 (repoid, baseurl) 列表。

    仅支持 http(s) baseurl（copr:// 前缀首期不处理，跳过并告警）；
    URL 中的 $basearch 替换为目标 chroot 架构。
    """
    entries: list[tuple[str, str]] = []
    for i, url in enumerate(additional_repos or []):
        if not isinstance(url, str):
            continue
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            print(f"[CI] 跳过暂不支持的 additional repo: {url}", file=sys.stderr)
            continue
        entries.append((f"ci-extra-{i}", url.replace("$basearch", arch)))
    return entries


def _get_copr_result_url(session_dir: Path) -> tuple[str, str, list[str]]:
    """从 session.json + gate_result 读取 COPR result repo URL、chroot 和 additional_repos。"""
    session = json.loads((session_dir / "session.json").read_text())
    copr_url     = session.get("copr_url", "http://copr-frontend:5000")
    copr_owner   = session.get("copr_owner", "")
    copr_project = session.get("copr_project", "")
    login        = session.get("copr_login", "")
    token        = session.get("copr_token", "")

    # 找 COPR project chroot
    import base64, urllib.request, urllib.parse
    creds = base64.b64encode(f"{login}:{token}".encode()).decode()
    url   = (f"{copr_url.rstrip('/')}/api_3/project"
             f"?ownername={copr_owner}&projectname={copr_project}")
    req   = urllib.request.Request(url, headers={"Authorization": f"Basic {creds}"})
    chroot = ""
    result_url = ""
    additional_repos: list[str] = []
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        chroot_repos = data.get("chroot_repos", {})
        # 项目级外部源（如 ROS SIG 源），构建时会挂进 chroot，
        # CI 检查也必须挂，否则基座包依赖被误报缺失
        additional_repos = data.get("additional_repos", []) or []
        target_chroot = session.get("copr_chroot", "")
        # 优先匹配 session.json 里的 copr_chroot
        if target_chroot and target_chroot in chroot_repos:
            chroot = target_chroot
            result_url = chroot_repos[target_chroot]
        else:
            # 兜底：挑第一个 x86_64
            for c, repo in chroot_repos.items():
                if c.endswith("-x86_64"):
                    chroot = c
                    result_url = repo
                    break
        if not chroot and chroot_repos:
            chroot, result_url = next(iter(chroot_repos.items()))
    except Exception as e:
        print(f"[CI] WARNING: 无法获取 COPR chroot 信息: {e}", file=sys.stderr)

    return chroot, result_url, additional_repos


def _write_repo_file(repo_path: Path, chroot: str, copr_result_url: str) -> bool:
    """写入临时 repo 文件。返回是否成功。"""
    base = _chroot_repo_base(chroot)
    arch = _chroot_arch(chroot)

    content = ""
    if base:
        content += f"""[ci-oe-official]
name=openEuler {chroot} official
baseurl={base}/everything/{arch}/
enabled=1
gpgcheck=0

[ci-oe-update]
name=openEuler {chroot} update
baseurl={base}/update/{arch}/
enabled=1
gpgcheck=0

[ci-oe-epol]
name=openEuler {chroot} EPOL
baseurl={base}/EPOL/main/{arch}/
enabled=1
gpgcheck=0

"""

    if copr_result_url:
        content += f"""[ci-copr-result]
name=COPR project result
baseurl={copr_result_url}
enabled=1
gpgcheck=0

"""

    if not content:
        return False

    try:
        repo_path.write_text(content, encoding="utf-8")
        return True
    except PermissionError:
        return False


def _copr_repo_accessible(copr_result_url: str) -> bool:
    """检查 COPR result repo 的 repomd.xml 是否可访问。"""
    import urllib.request
    url = copr_result_url.rstrip("/") + "/repodata/repomd.xml"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def run_repoclosure(pkgs: list[str], chroot: str, copr_result_url: str,
                    additional_repos: list[str]) -> tuple[bool, str]:
    """运行 repoclosure 检查运行时依赖。"""
    check = subprocess.run(["which", "repoclosure"], capture_output=True)
    if check.returncode != 0:
        # repoclosure 由镜像内置（dnf-utils）提供；缺失视为环境异常，
        # 显式失败而非静默跳过——跳过会让 CI 门禁形同虚设
        return False, "[INFRA] repoclosure 不可用：镜像缺少 dnf-utils 包，需更新 worker 镜像"

    base = _chroot_repo_base(chroot)
    arch = _chroot_arch(chroot)

    # 检查 COPR result repo 是否可访问（空 project 时 repomd.xml 不存在）
    use_copr = bool(copr_result_url) and _copr_repo_accessible(copr_result_url)
    if copr_result_url and not use_copr:
        print("[CI] COPR result repo 暂不可访问（可能尚无已构建的包），跳过 COPR 源")

    cmd = ["repoclosure", "--newest"]

    if base:
        # 显式按目标 chroot 架构解析（宿主 pod 架构可能与 chroot 不同，
        # 如 x86_64 pod 检查 aarch64 包，缺省会按宿主架构漏报/误报）
        cmd += ["--arch", arch]
        cmd += [
            "--repofrompath", f"ci-oe-official,{base}/everything/{arch}/",
            "--repofrompath", f"ci-oe-update,{base}/update/{arch}/",
            "--repofrompath", f"ci-oe-epol,{base}/EPOL/main/{arch}/",
            "--repo", "ci-oe-official",
            "--repo", "ci-oe-update",
            "--repo", "ci-oe-epol",
        ]
    else:
        # 没有 chroot 信息，使用现有 repo
        pass

    if use_copr:
        cmd += [
            "--repofrompath", f"ci-copr-result,{copr_result_url}",
            "--repo", "ci-copr-result",
        ]

    for repoid, repo_url in _extra_repos(additional_repos, arch):
        cmd += ["--repofrompath", f"{repoid},{repo_url}", "--repo", repoid]

    for pkg in pkgs:
        cmd += ["--check", pkg]

    # 元数据要拉官方源 + EPOL + COPR + additional_repos 多个 repo，网络慢时
    # 180s 不够（真实环境观测到分钟级），放宽到 600s 与 install 检查一致；
    # 超时属环境/网络问题（[INFRA]），与依赖未闭合区分开，supervisor 会重跑 CI
    # 而不是把包送进修复流程误判
    def _run():
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=600), ""
        except subprocess.TimeoutExpired:
            return None, ("[INFRA] repoclosure 超时（600s）：repo 元数据拉取过慢，"
                          "属环境/网络问题，非依赖闭合失败")

    result, err = _run()
    # repodata 可能有秒级更新延迟（createrepo 刚跑完），失败时重试一次
    if result is None or result.returncode != 0:
        time.sleep(2)
        result, err = _run()
    if result is None:
        return False, err
    if result.returncode != 0:
        return False, (result.stdout + result.stderr).strip()
    return True, ""


def run_install_check(pkgs: list[str], chroot: str, copr_result_url: str,
                      additional_repos: list[str]) -> tuple[bool, str]:
    """运行 dnf install 做可安装性检查（干净 installroot 真跑安装事务）。

    repoclosure 只验证依赖闭合（每个 Requires 在 repo 集合里有提供者），
    不验证包本身可安装——conflicts、文件冲突、事务级错误都查不出。
    这里在空 installroot 里真装一遍，装完即删。不能用 --assumeno：
    只解算不执行，证不了 scriptlet 和事务提交。
    """
    base = _chroot_repo_base(chroot)
    arch = _chroot_arch(chroot)
    use_copr = bool(copr_result_url) and _copr_repo_accessible(copr_result_url)
    if not use_copr:
        # 产物源不可访问（尚无已构建的包）时无从安装，跳过而非误报
        return True, "[SKIP] COPR result repo 不可访问，跳过可安装性检查"

    cmd = ["dnf", "install", "-y"]

    # CI 源（含 --repofrompath 注入的）均无签名/无密钥可验，repoclosure 与
    # builddep 的 repo 文件统一 gpgcheck=0；--repofrompath 不支持内联
    # gpgcheck=0，空 installroot 里默认 gpgcheck=1 会整体误报，这里显式关闭
    cmd += ["--nogpgcheck"]

    # 与 builddep 同模式：空 installroot + --releasever=/，与宿主 pod 解耦
    installroot = tempfile.mkdtemp(prefix="ci-install-")
    # 预建 usrmerge 软链：空 installroot 里若 libgcc 先于 filesystem 解包，
    # rpm 会把 /lib64 自动建成真实目录，filesystem 的 /lib64->usr/lib64
    # 软链无法覆盖目录（cpio: already exists as a directory），整事务回滚
    # 误报。预建软链后包文件直接落进 usr/lib64，与实际系统布局一致
    root = Path(installroot)
    for d in ("usr/bin", "usr/lib", "usr/lib64", "usr/sbin"):
        (root / d).mkdir(parents=True, exist_ok=True)
    for link, target in (("bin", "usr/bin"), ("lib", "usr/lib"),
                         ("lib64", "usr/lib64"), ("sbin", "usr/sbin")):
        (root / link).symlink_to(target)
    cmd += [f"--installroot={installroot}", "--releasever=/"]
    if base and arch != platform.machine():
        cmd += [f"--forcearch={arch}"]
        # 跨架构无法执行目标架构的 scriptlet（%post 等），跳过以免误报；
        # 代价是 scriptlet 执行失败查不出（同架构检查不带此参数）
        cmd += ["--setopt=tsflags=noscripts"]

    if base:
        cmd += [
            "--repofrompath", f"ci-oe-official,{base}/everything/{arch}/",
            "--repofrompath", f"ci-oe-update,{base}/update/{arch}/",
            "--repofrompath", f"ci-oe-epol,{base}/EPOL/main/{arch}/",
            "--disablerepo=*",
            "--enablerepo=ci-oe-official",
            "--enablerepo=ci-oe-update",
            "--enablerepo=ci-oe-epol",
        ]

    cmd += [
        "--repofrompath", f"ci-copr-result,{copr_result_url}",
        "--enablerepo=ci-copr-result",
    ]

    for repoid, repo_url in _extra_repos(additional_repos, arch):
        cmd += ["--repofrompath", f"{repoid},{repo_url}", f"--enablerepo={repoid}"]

    cmd += pkgs

    # 真实安装要下载 RPM 包体（不只是元数据），放宽超时
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return False, ("[INFRA] dnf install 超时（600s）：repo 元数据/RPM 下载过慢，"
                       "属环境/网络问题，非包不可安装")
    finally:
        shutil.rmtree(installroot, ignore_errors=True)
    if result.returncode != 0:
        return False, (result.stdout + result.stderr).strip()
    return True, ""


def run_builddep(pkg: str, spec_path: Path, chroot: str, copr_result_url: str,
                 additional_repos: list[str]) -> tuple[bool, str]:
    """运行 dnf builddep 检查编译期依赖。"""
    if not spec_path.exists():
        return True, f"[SKIP] spec 文件不存在: {spec_path}"

    # builddep 由镜像内置（dnf-plugins-core）提供；插件缺失时 dnf 报
    # "No such command"，而失败判定只认 "Error:"，会静默误判为通过，
    # 因此先显式探测，缺失即失败
    try:
        probe = subprocess.run(["dnf", "builddep", "--help"],
                               capture_output=True, text=True, timeout=30)
        unavailable = (probe.returncode != 0 or
                       "No such command" in (probe.stdout + probe.stderr))
    except (FileNotFoundError, subprocess.TimeoutExpired):
        unavailable = True
    if unavailable:
        return False, "[INFRA] dnf builddep 不可用：镜像缺少 dnf-plugins-core 包，需更新 worker 镜像"

    base = _chroot_repo_base(chroot)
    arch = _chroot_arch(chroot)
    use_copr = bool(copr_result_url) and _copr_repo_accessible(copr_result_url)

    cmd = ["dnf", "builddep", "--assumeno"]

    # 始终使用空 installroot + --releasever=/，与宿主 pod 完全解耦：
    # 否则宿主 @System 已装包会被当作已满足的 BuildRequires，
    # worker 镜像里装过的包会掩盖缺失的 BR，检查结果随镜像内容变化。
    # 跨架构（如 x86_64 pod 检查 aarch64 chroot）再加 --forcearch
    # 让 dnf 按目标架构过滤包（否则架构相关包被排除，误报 No matching package）
    installroot = tempfile.mkdtemp(prefix="ci-builddep-")
    cmd += [f"--installroot={installroot}", "--releasever=/"]
    if base and arch != platform.machine():
        cmd += [f"--forcearch={arch}"]

    if base:
        cmd += [
            "--repofrompath", f"ci-oe-official,{base}/everything/{arch}/",
            "--repofrompath", f"ci-oe-update,{base}/update/{arch}/",
            "--repofrompath", f"ci-oe-epol,{base}/EPOL/main/{arch}/",
            "--disablerepo=*",
            "--enablerepo=ci-oe-official",
            "--enablerepo=ci-oe-update",
            "--enablerepo=ci-oe-epol",
        ]

    if use_copr:
        cmd += [
            "--repofrompath", f"ci-copr-result,{copr_result_url}",
            "--enablerepo=ci-copr-result",
        ]

    for repoid, repo_url in _extra_repos(additional_repos, arch):
        cmd += ["--repofrompath", f"{repoid},{repo_url}", f"--enablerepo={repoid}"]

    cmd.append(str(spec_path))

    # 空 installroot 需重新下载仓库元数据，放宽超时
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return False, ("[INFRA] dnf builddep 超时（300s）：repo 元数据拉取过慢，"
                       "属环境/网络问题，非 BuildRequires 不满足")
    finally:
        shutil.rmtree(installroot, ignore_errors=True)
    combined = result.stdout + result.stderr
    # --assumeno 成功时返回非零（拒绝安装），只有含 Error 才是真正失败
    failed = ("Error:" in combined and
              ("could not be found" in combined or "No match" in combined))
    return (not failed), (combined if failed else "")


def main() -> int:
    parser = argparse.ArgumentParser(description="CI 门禁：repoclosure + dnf install + dnf builddep（COPR 模式）")
    parser.add_argument("--pkgs", nargs="+", required=True, help="待检查的包名列表")
    parser.add_argument("--session-dir", required=True, help="session 目录路径")
    parser.add_argument("--reports-dir", default="", help="报告输出目录")
    args = parser.parse_args()

    session_dir  = Path(args.session_dir)
    reports_dir  = Path(args.reports_dir) if args.reports_dir else session_dir / "pkgs" / args.pkgs[0]
    reports_dir.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    infra_errors: list[str] = []
    warnings: list[str] = []

    def _record(prefix: str, msg: str) -> None:
        # [INFRA] 标记环境/网络类失败（超时、工具缺失、脚本异常），
        # 与真实检查失败区分：supervisor 对 infra 失败重跑 CI，不进修复流程
        entry = f"{prefix}:\n{msg}"
        (infra_errors if "[INFRA]" in msg else errors).append(entry)

    # 1. 获取 COPR 信息
    print("[CI] 读取 COPR 信息...")
    chroot, copr_result_url, additional_repos = _get_copr_result_url(session_dir)
    if chroot:
        print(f"[CI] chroot: {chroot}")
    if copr_result_url:
        print(f"[CI] COPR result URL: {copr_result_url}")
    else:
        print("[CI] WARNING: 未找到 COPR result URL，将仅检查官方源", file=sys.stderr)
    if additional_repos:
        print(f"[CI] additional_repos: {additional_repos}")

    try:
        # 2. repoclosure（所有包一起验证运行时依赖闭合）
        print(f"[CI] 运行 repoclosure（{len(args.pkgs)} 个包）...")
        ok, msg = run_repoclosure(args.pkgs, chroot, copr_result_url, additional_repos)
        if ok:
            if msg.startswith("[SKIP]"):
                print(f"[CI] ⚠ {msg}")
                warnings.append(msg)
            else:
                print("[CI] ✓ 运行时依赖闭合检查通过")
        else:
            print("[CI] ✗ 运行时依赖闭合检查失败", file=sys.stderr)
            _record("repoclosure 失败", msg)

        # 3. dnf install（所有包一起验证可安装性，干净 installroot 真装）
        print(f"[CI] 运行可安装性检查（{len(args.pkgs)} 个包）...")
        ok, msg = run_install_check(args.pkgs, chroot, copr_result_url, additional_repos)
        if ok:
            if msg.startswith("[SKIP]"):
                print(f"[CI] ⚠ {msg}")
                warnings.append(msg)
            else:
                print("[CI] ✓ 可安装性检查通过")
        else:
            print("[CI] ✗ 可安装性检查失败", file=sys.stderr)
            _record("可安装性检查失败", msg)

        # 4. dnf builddep（逐包验证编译期依赖）
        # spec 在 reports_dir（= pkgs/$TARGET）下；--pkgs 传的是 SRPM/二进制名，未必等于目录名
        spec_files = sorted(reports_dir.glob("*.spec"))
        for pkg in args.pkgs:
            spec_path = spec_files[0] if spec_files else reports_dir / f"{pkg}.spec"
            print(f"[CI] 运行 dnf builddep（{pkg}）...")
            ok, msg = run_builddep(pkg, spec_path, chroot, copr_result_url, additional_repos)
            if ok:
                if msg.startswith("[SKIP]"):
                    print(f"[CI] ⚠ {msg}")
                    warnings.append(msg)
                else:
                    print(f"[CI] ✓ {pkg} 编译期依赖检查通过")
            else:
                print(f"[CI] ✗ {pkg} 编译期依赖检查失败", file=sys.stderr)
                _record(f"{pkg} BuildRequires 不满足", msg)

    except Exception as e:
        # 兜底：任何未预期异常（如子进程超时泄漏）都要留下真实结果文件，
        # 而不是让上游 agent 面对缺失/陈旧的 ci_check_result.json 自行编造内容
        infra_errors.append(f"[INFRA] CI 脚本异常（未完成全部检查）: {e!r}")

    # 5. 写结果文件
    # status: fail=真实检查失败（进修复流程）；error=纯环境性失败（重跑 CI）
    status = "fail" if errors else ("error" if infra_errors else "pass")
    result = {"status": status, "errors": errors + infra_errors, "warnings": warnings,
              "chroot": chroot, "copr_result_url": copr_result_url,
              "additional_repos": additional_repos}
    out_file = reports_dir / "ci_check_result.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[CI] 结果已写入: {out_file}")

    if errors or infra_errors:
        print(f"\n[CI] 门禁未通过，共 {len(errors)} 项失败 + {len(infra_errors)} 项环境性错误",
              file=sys.stderr)
        return 1

    print("[CI] 门禁全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
