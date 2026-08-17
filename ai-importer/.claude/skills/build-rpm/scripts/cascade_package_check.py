#!/usr/bin/env python3
"""级联包存在性检查。

在 evaluate 阶段调用，统一替换独立的 check_existing_package.py（Level 2）
和 fetch_reference_spec.py（Level 3）查询。

查找级别：
  Level 0 — 用户 COPR project（自己的已成功构建）
  Level 5 — 项目 additional_repos（项目级外挂源，如 ROS SIG 源）
     dnf repoquery 直查外挂源 → 有满足版本的包 → 直接复用
     （执行顺序在 L0 之后、L2 之前；编号 5 不打乱既有 level 语义）
  Level 1 — EUR Repo (https://eur.openeuler.openatom.cn)
     fulltext search → 扫描 results 目录 → 下载 SRPM 重建
  Level 2 — openEuler 目标版本 (dnf repoquery)
     目标版本有匹配包 → 直接复用
  Level 3 — src-openeuler 源仓库 (gitcode.com)
     git ls-remote → clone 提取 spec/yaml/patches → 作为参考源
  Level 4 — 全新包
     所有来源都没有 → 从头构建

输出 check_result.json：
  {
    "pkgname": "snappy",
    "level": 2,
    "decision": "reuse_official",
    "match": { ... },
    "reference": null
  }

用法：
  python3 cascade_package_check.py <pkgname> --lang <lang> --target <version>
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import urllib.parse
import urllib.request
import shutil
from pathlib import Path
from typing import Optional

# ── 常量 ────────────────────────────────────────────────────────────────────────
EUR_BASE = "https://eur.openeuler.openatom.cn"
EUR_RESULTS = f"{EUR_BASE}/results"
EUR_FULLTEXT = f"{EUR_BASE}/coprs/fulltext/"
GITCODE_HOST = "gitcode.com"
PKG_NAMESPACE = "src-openeuler"
LS_REMOTE_TIMEOUT = 10
RESULTS_SCAN_TIMEOUT = 15

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from rpm_naming import get_rpm_pkg_name, get_srpm_name  # noqa: E402
import check_existing_package as _checker  # noqa: E402


# ── chroot / 版本匹配辅助 ─────────────────────────────────────────────────────

_ARCH_SUFFIX_RE = re.compile(r"-(x86_64|aarch64|noarch|i686|i386)$", re.IGNORECASE)


def _split_chroot(chroot: str) -> tuple[str, str]:
    """归一化 chroot → (os_base, arch)。os_base 小写、下划线转横线。

    例: "openeuler-24.03_LTS_SP3-x86_64" → ("openeuler-24.03-lts-sp3", "x86_64")
    """
    c = (chroot or "").strip().rstrip("/")
    arch = ""
    m = _ARCH_SUFFIX_RE.search(c)
    if m:
        arch = m.group(1).lower()
        c = c[: m.start()]
    return c.replace("_", "-").lower(), arch


def _chroot_matches(build_chroot: str, target_base: str, target_arch: str) -> bool:
    """build chroot 是否匹配 target：OS 版本前缀一致且架构精确相等。

    架构必须精确相等（target 指明架构时），避免 x86_64 target 复用 aarch64 构建。
    """
    b_base, b_arch = _split_chroot(build_chroot)
    if not b_base.startswith(target_base):
        return False
    if target_arch and b_arch and b_arch != target_arch:
        return False
    return True


def _requirement_min_version(requirement: str) -> str:
    """从版本约束提取下界版本（>=/==/> 子句中的最高下界），作为级联检查的版本防线。

    依赖路径没有显式 version，但 requirement（如 ">=2.0,<3"）隐含下界；
    不推导的话 L0/L1 的版本检查会整体失效，任意老版本都会被误判为可复用。
    """
    info = _checker.parse_requirement(requirement)
    if info.get("status") != "parsed":
        return ""
    lowers = [c["version"] for c in info.get("clauses", [])
              if c.get("operator") in (">=", "==", ">") and c.get("version")]
    if not lowers:
        return ""
    best = lowers[0]
    for v in lowers[1:]:
        if _checker.compare_versions(v, best) > 0:
            best = v
    return best


def _version_satisfies(found_version: str, requested_version: str, requirement: str) -> bool:
    """found 版本是否满足请求：>= requested_version 且满足 requirement 全部子句。

    无版本号判不出来（空）→ 不满足；requirement 无法解析（含 or/!=/~=）→ 保守不满足；
    两者都没有（无约束）→ 存在即满足。
    """
    if not found_version:
        return False
    checks = []
    if requested_version:
        checks.append(_checker.compare_versions(found_version, requested_version) >= 0)
    req_info = _checker.parse_requirement(requirement)
    if req_info.get("status") == "parsed":
        sat = _checker.evaluate_requirement(found_version, req_info)
        if sat is not None:
            checks.append(sat)
    elif req_info.get("status") == "unknown":
        return False  # 约束解析不了，保守不复用（继续级联/构建是安全方向）
    if not checks:
        return True
    return all(checks)


# ── Level 1: EUR fulltext search ────────────────────────────────────────────────

def _eur_fulltext_search(pkgname: str) -> list[dict[str, str]]:
    """用 EUR fulltext search 查找包含目标包的 project 列表。

    返回列表格式：[{"owner": "lynlon", "project": "nginx"}, ...]
    """
    params = urllib.parse.urlencode({"fulltext": pkgname, "packagename": pkgname})
    url = f"{EUR_FULLTEXT}?{params}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "check_package_existence/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return []

    # 解析 HTML 中的 project 链接
    projects = []
    seen = set()
    for m in re.finditer(r'href="/coprs/([^/"]+)/([^/"]+)/"', html):
        owner = m.group(1)
        project_name = m.group(2)
        key = (owner, project_name)
        if key not in seen:
            seen.add(key)
            projects.append({"owner": owner, "project": project_name})

    return projects


KNOWN_LANG_PREFIXES = [
    "python", "python3", "nodejs", "golang",
    "rust", "ruby", "perl", "lua", "php",
]

def _eur_pkgname_matches(build_dir_name: str, pkgname: str) -> bool:
    """检查 EUR build 目录名是否与目标包匹配。

    策略：先整词相等；再剥离已知语言前缀后整词相等。
    """
    bd = build_dir_name.lower().replace("_", "-")
    pn = pkgname.lower().replace("_", "-")
    if bd == pn:
        return True
    # 剥离语言前缀后比较（Python 包常见 python-xxx → xxx）
    for prefix in KNOWN_LANG_PREFIXES:
        if bd.startswith(prefix + "-"):
            if bd[len(prefix) + 1:] == pn:
                return True
            break  # 只匹配一个前缀
    # 反向：加前缀后比较
    for prefix in ["python", "python3", "nodejs", "rust", "golang"]:
        if (prefix + "-" + pn) == bd:
            return True
    return False


def _scan_eur_results(projects: list[dict[str, str]], pkgname: str,
                      target_chroot: str = "", target_version: str = "") -> Optional[dict]:
    """扫描 EUR project 的 results 目录，匹配包名。

    返回命中的第一个匹配结果，含 srpm_url / binary_rpm_url / version / chroot。
    若 target_version 指定，EUR 版本必须 >= 目标版本才返回命中。
    若 target_chroot 指定，仅扫描匹配的目标版本 chroot（如 openeuler-24.03-lts-sp3）。
    """
    # Normalize target for chroot matching（OS 版本 + 架构拆分，匹配时架构精确相等）
    target_base, target_arch = _split_chroot(target_chroot)

    for proj in projects:
        owner = proj["owner"]
        project_name = proj["project"]
        results_url = f"{EUR_RESULTS}/{owner}/{project_name}/"

        # 遍历 results 下的 chroot 目录
        try:
            req = urllib.request.Request(results_url, headers={"User-Agent": "check_package_existence/1.0"})
            with urllib.request.urlopen(req, timeout=RESULTS_SCAN_TIMEOUT) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
        except Exception:
            continue

        chroot_dirs: list[str] = re.findall(r'href="([^"]+/)"', html)
        # If target_base is specified, prioritize matching chroots first
        if target_base:
            matching = [c for c in chroot_dirs if _chroot_matches(c, target_base, target_arch)]
            non_matching = [c for c in chroot_dirs if c not in matching]
            chroot_dirs = matching + non_matching

        for chroot_dir in chroot_dirs:
            chroot = chroot_dir.rstrip("/")
            if not chroot or chroot.startswith(".."):
                continue
            # 命中的 chroot 是否与目标精确匹配（OS 前缀 + 架构）：
            # 不匹配时 EUR 二进制无法直接复用，只能降级为参考源
            chroot_matched = bool(target_base) and _chroot_matches(chroot_dir, target_base, target_arch)

            chroot_url = f"{results_url}{chroot}/"
            try:
                req = urllib.request.Request(chroot_url, headers={"User-Agent": "check_package_existence/1.0"})
                with urllib.request.urlopen(req, timeout=RESULTS_SCAN_TIMEOUT) as resp:
                    chroot_html = resp.read().decode("utf-8", errors="ignore")
            except Exception:
                continue

            # 解析构建目录：<build_id>-<pkgname>/
            build_dirs = re.findall(r'href="(\d+-[^/"]+/)"', chroot_html)
            for build_dir in build_dirs:
                build_dir_clean = build_dir.rstrip("/")
                # 提取 build 目录中的包名（去掉 build_id 前缀）
                parts = build_dir_clean.split("-", 1)
                if len(parts) < 2:
                    continue
                build_pkgname = parts[1]

                if not _eur_pkgname_matches(build_pkgname, pkgname):
                    continue

                # 进入 build 目录，列出文件
                build_url = f"{chroot_url}{build_dir}"
                try:
                    req = urllib.request.Request(build_url, headers={"User-Agent": "check_package_existence/1.0"})
                    with urllib.request.urlopen(req, timeout=RESULTS_SCAN_TIMEOUT) as resp:
                        build_html = resp.read().decode("utf-8", errors="ignore")
                except Exception:
                    continue

                # 找 SRPM 和二进制 RPM
                rpm_files = re.findall(r'href="([^"]+\.rpm)"', build_html)
                srpm_files = [f for f in rpm_files if f.endswith(".src.rpm")]
                binary_files = [f for f in rpm_files if not f.endswith(".src.rpm")]

                # 从 SRPM 文件名提取版本（<name>-<version>-<release>.src.rpm）
                version = None
                srpm_url = None
                if srpm_files:
                    srpm = srpm_files[0]
                    srpm_url = f"{build_url}{srpm}"
                    # 正则从末尾匹配：<version>-<release>.src.rpm
                    ver_match = re.match(
                        r'.+-(\d[\d\w.]*)-(\d[\d\w.]*)\.src\.rpm$', srpm
                    )
                    if ver_match:
                        version = ver_match.group(1)

                binary_urls = [f"{build_url}{f}" for f in binary_files] if binary_files else []

                match_info = {
                    "level": 1,
                    "eur_owner": owner,
                    "eur_project": project_name,
                    "srpm_url": srpm_url,
                    "srpm_file": srpm_files[0] if srpm_files else None,
                    "binary_rpm_urls": binary_urls,
                    "binary_rpm_files": binary_files,
                    "version": version,
                    "chroot": chroot,
                    "chroot_matched": chroot_matched,
                }

                # 版本防线：EUR 版本必须满足目标版本。
                # 版本号解析不出来 / 比较失败 → 保守跳过（复用错版本代价远高于重建）
                if target_version:
                    try:
                        if not _version_satisfies(version or "", target_version, ""):
                            continue  # EUR 版本不满足，继续搜下一个
                    except Exception:
                        continue

                if binary_files or srpm_files:
                    match_info["decision"] = "reuse_eur_srpm"
                else:
                    continue

                return match_info

    return None


# ── Level 2: openEuler 目标版本 ─────────────────────────────────────────────────

def _check_target_version(pkgname: str, lang: str, target: str, version: str,
                          requirement: str) -> Optional[dict]:
    """用 dnf repoquery 查目标版本是否有包。复用 check_existing_package.py 逻辑。"""
    result = _checker.check_existing_package(
        pkgname,
        version=version,
        requirement=requirement,
        lang=lang,
        chroot=target,
    )
    official = result.get("official", {})
    if official.get("exists"):
        highest = official.get("highest", {})
        return {
            "level": 2,
            "decision": "reuse_official" if official.get("meets_need") else "evaluate",
            "rpm_name": highest.get("name", ""),
            "version": highest.get("version", ""),
            "source": f"openEuler {target}",
            "reason": result.get("reason", ""),
        }
    return None


# ── Level 3: gitcode src-openeuler ──────────────────────────────────────────────

def _build_gitcode_candidates(pkgname: str, lang: str) -> list[str]:
    """生成 gitcode src-openeuler 仓库名候选列表。

    命名规则与 RPM 包名一致，复用 rpm_naming.py 映射。
    """
    candidates = [pkgname]

    lang_lower = (lang or "").lower()
    if lang_lower == "python":
        candidates.extend([f"python-{pkgname}", f"python3-{pkgname}"])
    elif lang_lower == "nodejs":
        candidates.append(f"nodejs-{pkgname}")
    elif lang_lower in ("c", "cpp"):
        # C/C++ 通常用上游名
        pass
    elif lang_lower == "go":
        # Go 通常用 golang-<full-module-path>，但 pkgname 可能已经是完整路径
        if not pkgname.startswith("golang-"):
            candidates.append(f"golang-{pkgname}")
    elif lang_lower == "rust":
        if not pkgname.startswith("rust-"):
            candidates.append(f"rust-{pkgname}")

    # 去重保持顺序
    seen = set()
    result = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            result.append(c)
    return result


def _git_ls_remote(pkgname: str) -> Optional[bool]:
    """检查 gitcode repo 是否存在。返回 True/False/None(network_error)。"""
    url = f"https://{GITCODE_HOST}/{PKG_NAMESPACE}/{pkgname}.git"
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", url],
            capture_output=True, text=True, timeout=LS_REMOTE_TIMEOUT,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if result.returncode == 0:
            return True
        stderr = result.stderr.lower()
        if any(kw in stderr for kw in ("not found", "could not read",
                                        "repository not found", "403", "404")):
            return False
        return None
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None


def _clone_and_extract(pkgname: str, output_dir: Path) -> bool:
    """Shallow clone src-openeuler 仓库并提取 spec/yaml/patches。"""
    # 复用 fetch_reference_spec.py 的提取逻辑
    from fetch_reference_spec import _clone_and_extract as _clone_extract
    # fetch_reference_spec 里的 _clone_and_extract 接受的是 gitcode 上的 repo 名
    return _clone_extract(pkgname, output_dir)


def _check_src_openeuler(pkgname: str, lang: str, target: str = "") -> Optional[dict]:
    """查 gitcode.com/src-openeuler 仓库是否存在，并匹配目标版本分支。

    当 target 指定时（如 openEuler-24.03-LTS-SP3），优先使用对应版本分支
    的 spec 作为参考源，避免默认分支版本不匹配的问题。
    """
    from fetch_reference_spec import _find_best_branch

    candidates = _build_gitcode_candidates(pkgname, lang)
    for candidate in candidates:
        exists = _git_ls_remote(candidate)
        if exists is True:
            result = {
                "level": 3,
                "decision": "introduce_new_with_ref",
                "gitcode_repo": f"https://{GITCODE_HOST}/{PKG_NAMESPACE}/{candidate}.git",
                "repo_name": candidate,
            }
            # 查找匹配目标版本的分支
            if target:
                best_branch = _find_best_branch(candidate, target)
                if best_branch:
                    result["target_branch"] = best_branch
            return result
        elif exists is None:
            # 网络错误，继续尝试下一个候选
            continue
        # exists is False → 尝试下一个候选
    return None


# ── Level 5: 项目 additional_repos（外挂源）───────────────────────────────────
# 项目级外挂源（如 ROS SIG 源）由 COPR 后端在构建时挂进 chroot（report806 方案 B），
# CI 门禁也已挂同一组源；存在性判定不查它们，会把外挂源里现成的包误判为缺失而
# 从源头重建（实测 ouster-ros 会话因此重建了半个 ROS 核心栈，4h 超时）。

_ADDITIONAL_REPOS_CACHE: dict = {}


def _get_project_additional_repos(copr_url: str, owner: str, project: str,
                                  login: str, token: str) -> list[str]:
    """GET api_3/project 拿项目级 additional_repos，进程内缓存（每个依赖都会走级联，
    不缓存会对同一项目重复发 API 请求）。"""
    if not (copr_url and owner and project and login and token):
        return []
    key = (copr_url, owner, project)
    if key in _ADDITIONAL_REPOS_CACHE:
        return _ADDITIONAL_REPOS_CACHE[key]
    repos: list[str] = []
    try:
        import base64
        creds = base64.b64encode(f"{login}:{token}".encode()).decode()
        url = (f"{copr_url.rstrip('/')}/api_3/project"
               f"?ownername={urllib.parse.quote(owner)}"
               f"&projectname={urllib.parse.quote(project)}")
        req = urllib.request.Request(url, headers={"Authorization": f"Basic {creds}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        repos = [u.strip() for u in (data.get("additional_repos") or [])
                 if isinstance(u, str) and u.strip()]
    except Exception as e:
        print(f"[cascade] WARN 获取项目 additional_repos 失败: {e}", file=sys.stderr)
    _ADDITIONAL_REPOS_CACHE[key] = repos
    return repos


def _check_additional_repos(pkgname: str, lang: str, repos: list[str],
                            target: str, version: str,
                            requirement: str) -> Optional[dict]:
    """用 dnf repoquery 查项目 additional_repos 中是否有满足版本要求的包。

    命中且版本满足 → reuse_additional_repo；查询失败/超时按未命中继续级联
    （保守方向是构建，不会把不存在的包误判为可复用）。
    """
    query_name = get_srpm_name(lang, pkgname) if lang else pkgname
    _, arch = _split_chroot(target)
    for i, raw_url in enumerate(repos):
        url = raw_url
        if not url.startswith(("http://", "https://")):
            continue  # copr:// 等形式暂不支持
        if arch:
            url = url.replace("$basearch", arch)
        repoid = f"cascade-extra-{i}"
        cmd = ["dnf", "repoquery", "--quiet",
               "--repofrompath", f"{repoid},{url}",
               "--disablerepo=*", f"--enablerepo={repoid}",
               "--qf", "%{version}"]
        # 跨架构（x86_64 pod 查 aarch64 chroot）按目标架构过滤，与 run_ci_check 一致
        if arch and arch != platform.machine():
            cmd.append(f"--forcearch={arch}")
        cmd.append(query_name)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            print(f"[cascade] WARN additional repo 查询异常({repoid} {url}): {e}",
                  file=sys.stderr)
            continue
        if proc.returncode != 0:
            print(f"[cascade] WARN additional repo 查询失败({repoid} {url}): "
                  f"{proc.stderr.strip()[:200]}", file=sys.stderr)
            continue
        versions = [v.strip() for v in proc.stdout.splitlines() if v.strip()]
        best = None
        for v in versions:
            if best is None or _checker.compare_versions(v, best) > 0:
                best = v
        # 版本防线：与 L0/L2 同一套 _version_satisfies，老版本不误判可复用
        if best and _version_satisfies(best, version, requirement):
            return {
                "level": 5,
                "decision": "reuse_additional_repo",
                "rpm_name": query_name,
                "version": best,
                "source": url,
                "match": {"source": url, "version": best, "repo": repoid},
            }
    return None


# ── Level 0: 用户 COPR project ─────────────────────────────────────────────────

def _check_user_copr_project(pkgname: str, copr_url: str, owner: str,
                              project: str, login: str, token: str,
                              target: str = "", lang: str = "",
                              version: str = "", requirement: str = "") -> Optional[dict]:
    """检查用户自己的 COPR project 是否已有此包（避免重复构建）。

    仅当已有构建的 chroot 与 target 匹配（OS 版本前缀 + 架构精确相等）、
    且构建版本满足请求版本/约束（version / requirement）时才返回复用结果，
    避免将不同目标版本、不同架构或版本过低的构建误判为可复用。
    target 为空时无法保证 chroot 精确匹配，直接放弃 L0 复用。
    """
    if not (copr_url and owner and project and login and token):
        return None

    target_base, target_arch = _split_chroot(target)
    if not target_base:
        return None

    import base64
    creds = base64.b64encode(f"{login}:{token}".encode()).decode()
    headers = {"Authorization": f"Basic {creds}"}

    # COPR 里存的是 RPM 包名（如 python-xxx），pkgname 是上游名
    # 需用 rpm_naming 转换后再查询
    query_name = pkgname
    if lang:
        query_name = get_srpm_name(lang, pkgname)

    params = urllib.parse.urlencode({
        "ownername": owner,
        "projectname": project,
        "packagename": query_name,
        "limit": "10",
    })
    url = f"{copr_url.rstrip('/')}/api_3/build/list?{params}"

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        items = data.get("items", [])

        best = None
        best_chroot = ""
        for build in items:
            if build.get("state") != "succeeded":
                continue
            # chroot 必须匹配（OS 版本前缀 + 架构精确相等）
            matched = [c for c in build.get("chroots", [])
                       if _chroot_matches(c, target_base, target_arch)]
            if not matched:
                continue
            ver = build.get("source_package", {}).get("version", "")
            if ver and (best is None or _checker.compare_versions(ver, best["version"]) > 0):
                best = {"name": pkgname, "version": ver}
                best_chroot = matched[0]
        if not best:
            return None
        # 版本防线：project 里最高版本必须满足请求版本/约束，否则继续级联
        if not _version_satisfies(best["version"], version, requirement):
            return None
        return {
            "level": 0,
            "decision": "reuse_copr_project",
            "rpm_name": pkgname,
            "version": best["version"],
            "source": f"{owner}/{project}",
            "match": {
                "source": f"{owner}/{project}",
                "version": best["version"],
                "chroot": best_chroot,
            },
        }
    except Exception:
        pass
    return None


# ── 主入口 ──────────────────────────────────────────────────────────────────────

def check_package_existence(
    pkgname: str,
    lang: str = "",
    version: str = "",
    requirement: str = "",
    target: str = "",
    copr_url: str = "",
    copr_owner: str = "",
    copr_project: str = "",
    copr_login: str = "",
    copr_token: str = "",
) -> dict:
    """4 级级联查找包的处置策略。

    Args:
        pkgname: 上游包名
        lang: 语言（python/go/rust/c/cpp/nodejs/java）
        version: 目标版本号
        requirement: 版本约束（如 >= 1.0）
        target: 目标 openEuler 版本（如 openEuler-24.03-LTS-SP3）
        copr_url / copr_owner / copr_project / copr_login / copr_token:
            用户 COPR 凭据，用于 L0 检查自己的 project 是否已有此包。

    Returns:
        {
            "pkgname": str,
            "level": int (0-4),
            "decision": str,
            "match": { ... } | None,
            "reference": { ... } | None,
        }
    """
    result: dict = {
        "pkgname": pkgname,
        "level": 4,
        "decision": "introduce_new",
        "match": None,
        "reference": None,
    }

    # 依赖路径常只给 requirement 不给 version：推导下界版本，
    # 否则 L0/L1 的版本检查整体失效（任意老版本被误判可复用）
    if not version and requirement:
        version = _requirement_min_version(requirement)

    # ── Level 0: 用户 COPR project ──────────────────────────────────────────
    user_result = _check_user_copr_project(
        pkgname, copr_url, copr_owner, copr_project, copr_login, copr_token, target, lang,
        version=version, requirement=requirement,
    )
    if user_result:
        result.update(user_result)
        return result

    # ── Level 5: 项目 additional_repos（外挂源，如 ROS SIG 源）────────────────
    # 项目主动挂的外部源，构建/CI 两侧都已注入，可信度等同项目自身，
    # 先于官方源/EUR 判定（L2 本来也先于 L1 执行，编号不代表顺序）
    extra_repos = _get_project_additional_repos(copr_url, copr_owner, copr_project,
                                                copr_login, copr_token)
    if extra_repos:
        extra_match = _check_additional_repos(pkgname, lang, extra_repos, target,
                                              version, requirement)
        if extra_match:
            result.update(extra_match)
            return result

    # ── Level 2: openEuler 目标版本 ─────────────────────────────────────────
    # 官方源复用零成本，优先于 EUR SRPM 重建（原顺序 L1 在 L2 前，
    # 官方源已有的包会被 EUR 命中抢走，白白重建一次）
    target_match = _check_target_version(
        pkgname, lang, target, version, requirement
    )
    if target_match:
        result.update(target_match)
        return result

    # ── Level 1: EUR fulltext search ─────────────────────────────────────────
    eur_projects = _eur_fulltext_search(pkgname)
    if eur_projects:
        eur_match = _scan_eur_results(eur_projects, pkgname, target_chroot=target, target_version=version)
        if eur_match:
            result["level"] = 1
            if eur_match.get("chroot_matched"):
                # chroot 精确匹配：EUR 二进制/SRPM 可直接复用
                result["decision"] = "reuse_eur_srpm"
                result["match"] = eur_match
            else:
                # chroot 不匹配：EUR 产物无法直接复用，降级为参考源，
                # 以其 SRPM/spec 为起点重建（与 L3 gitcode 参考源同语义）。
                # 依赖路径上没有"下载 EUR SRPM 重建"的执行通道，
                # 判 reuse 会变成"假 resolved"（没人执行重建动作）。
                result["decision"] = "introduce_new_with_ref"
                result["match"] = None
                result["reference"] = {
                    "source": "eur",
                    "eur_owner": eur_match.get("eur_owner", ""),
                    "eur_project": eur_match.get("eur_project", ""),
                    "srpm_url": eur_match.get("srpm_url"),
                    "srpm_file": eur_match.get("srpm_file"),
                    "version": eur_match.get("version"),
                    "chroot": eur_match.get("chroot"),
                }
            return result

    # ── Level 3: gitcode src-openeuler ──────────────────────────────────────
    gitcode_match = _check_src_openeuler(pkgname, lang, target)
    if gitcode_match:
        result.update(gitcode_match)
        return result

    # ── Level 4: 全新包 ─────────────────────────────────────────────────────
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="级联包存在性检查"
    )
    parser.add_argument("pkgname", help="包名")
    parser.add_argument("--lang", default="", help="语言：python/go/rust/c/cpp/nodejs/java")
    parser.add_argument("--version", default="", help="目标版本号")
    parser.add_argument("--requirement", default="", help="版本约束，如 >= 1.0")
    parser.add_argument("--target", default="",
                        help="目标 openEuler 版本，如 openEuler-24.03-LTS-SP3")
    parser.add_argument("-o", "--output", default="", help="输出 JSON 文件路径")
    parser.add_argument("--json", action="store_true", help="输出 JSON 到 stdout")
    args = parser.parse_args()

    result = check_package_existence(
        args.pkgname,
        lang=args.lang.strip().lower(),
        version=args.version.strip(),
        requirement=args.requirement.strip(),
        target=args.target.strip(),
    )

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    if args.json or not args.output:
        print(json.dumps(result, ensure_ascii=False, indent=2))

    # 退出码：0=命中（L0/L1/L2/L5），3=有参考源（L3），4=全新（L4）
    if result["decision"] in ("reuse_eur_srpm", "reuse_official",
                              "reuse_copr_project", "reuse_additional_repo"):
        return 0
    elif result["decision"] == "introduce_new_with_ref":
        return 3
    else:
        return 4


if __name__ == "__main__":
    sys.exit(main())
