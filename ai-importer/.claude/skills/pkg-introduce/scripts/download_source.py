#!/usr/bin/env python3
"""
从 PR 信息中提取上游地址并下载源码

流程：
  1. 读取 extract_pr_info.py 生成的 pr_N_info.json
  2. 从 YAML diff 中解析 upstream 字段
  3. 根据 URL 类型选择下载方式：
     - GitHub/GitLab/AtomGit → git clone --depth=1
     - .tar.gz/.tar.xz/.zip  → wget + 解压

用法：
  python3 download_source.py --pr-json pr_1_info.json --output-dir ./sources
  python3 download_source.py --owner shuyingbanbao --repo community --pr 1 --output-dir ./sources
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional


def _load_config() -> dict:
    config_path = Path(__file__).resolve().parent.parent / "config.json"
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ── 1. 从 PR JSON 提取 upstream URL ──────────────────────────────────────────

def extract_upstream_url(pr_json_path: str) -> Optional[str]:
    """从 pr_N_info.json 的 diff 内容中解析 upstream 字段"""
    with open(pr_json_path, encoding="utf-8") as f:
        data = json.load(f)

    files = data.get("files") or []
    for file_info in files:
        filename = file_info.get("filename", "")
        # 只处理 .yaml/.yml 文件
        if not (filename.endswith(".yaml") or filename.endswith(".yml")):
            continue

        patch = file_info.get("patch", "")
        diff_text = patch.get("diff", "") if isinstance(patch, dict) else patch

        url = _parse_upstream_from_diff(diff_text)
        if url:
            print(f"[INFO] 在文件 {filename} 中找到 upstream: {url}")
            return url

    return None


def _parse_upstream_from_diff(diff_text: str) -> Optional[str]:
    """从 diff 文本中提取 upstream: <url> 行"""
    for line in diff_text.splitlines():
        # diff 新增行以 + 开头
        content = line.lstrip("+").strip()
        m = re.match(r"upstream\s*:\s*(\S+)", content, re.IGNORECASE)
        if m:
            url = m.group(1).rstrip("/")
            return url
    return None


# ── 2. 判断 URL 类型并下载 ────────────────────────────────────────────────────

def detect_url_type(url: str) -> str:
    """
    判断 URL 类型：
      git_repo  — GitHub/GitLab/Gitee/AtomGit 仓库地址
      tarball   — .tar.gz / .tar.xz / .tar.bz2 / .zip 压缩包
      unknown
    """
    lower = url.lower()
    if any(lower.endswith(ext) for ext in (".tar.gz", ".tar.xz", ".tar.bz2", ".tgz", ".zip")):
        return "tarball"
    git_hosts = ("github.com", "gitlab.com", "gitee.com", "atomgit.com", "gitcode.com")
    if any(host in lower for host in git_hosts):
        return "git_repo"
    # 其他 http/https 地址，尝试当 git 仓库处理
    if lower.startswith("http") or lower.startswith("git@"):
        return "git_repo"
    return "unknown"


def normalize_version(value: str) -> str:
    """标准化版本字符串，去掉前缀 v。"""
    normalized = (value or "").strip()
    return normalized[1:] if normalized.lower().startswith("v") else normalized


def parse_numeric_version_parts(version: str) -> list[str]:
    """仅解析纯数字点分版本，其他格式返回空。"""
    normalized = normalize_version(version)
    if not normalized or not re.fullmatch(r"\d+(?:\.\d+)*", normalized):
        return []
    return normalized.split(".")


def build_version_ref_candidates(version: str, repo_name: str = "") -> list[str]:
    """根据版本号生成保守、可解释的候选 tag/branch refs。

    repo_name 用于补充 Maven Release Plugin 默认生成的 <name>-<version> 格式 tag，
    在 Java 生态中很常见（如 jfiglet-0.0.8）。
    """
    normalized = normalize_version(version)
    if not normalized:
        return []

    candidates: list[str] = []
    numeric_parts = parse_numeric_version_parts(normalized)

    def add_candidate(ref: str) -> None:
        if ref not in candidates:
            candidates.append(ref)

    # Maven Release Plugin 默认 tag 格式：<reponame>-<version>
    if repo_name:
        add_candidate(f"refs/tags/{repo_name}-{normalized}")
        add_candidate(f"refs/tags/{repo_name}-v{normalized}")

    if len(numeric_parts) == 2:
        major_minor = ".".join(numeric_parts)
        patch_zero = f"{major_minor}.0"
        for ref in (
            f"refs/tags/v{major_minor}",
            f"refs/tags/{major_minor}",
            f"refs/tags/v{patch_zero}",
            f"refs/tags/{patch_zero}",
            f"refs/heads/release/{major_minor}",
            f"refs/heads/release-{major_minor}",
            f"refs/heads/{major_minor}",
            f"refs/heads/v{major_minor}",
        ):
            add_candidate(ref)
        return candidates

    if len(numeric_parts) >= 3:
        exact_version = ".".join(numeric_parts)
        major_minor = ".".join(numeric_parts[:2])
        for ref in (
            f"refs/tags/v{exact_version}",
            f"refs/tags/{exact_version}",
            f"refs/heads/release/{major_minor}",
            f"refs/heads/release-{major_minor}",
            f"refs/heads/{major_minor}",
            f"refs/heads/v{major_minor}",
            f"refs/heads/{exact_version}",
            f"refs/heads/v{exact_version}",
        ):
            add_candidate(ref)
        return candidates

    for ref in (
        f"refs/tags/v{normalized}",
        f"refs/tags/{normalized}",
        f"refs/heads/release/{normalized}",
        f"refs/heads/release-{normalized}",
        f"refs/heads/{normalized}",
        f"refs/heads/v{normalized}",
    ):
        add_candidate(ref)
    return candidates


def run_git_command(args: list[str], *, timeout: int = 300, cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
    )


def resolve_git_ref(url: str, version: str, *, silent: bool = False) -> str:
    """按预定义顺序解析与版本匹配的远端 ref。

    silent=True 时找不到 ref 返回空字符串而非 sys.exit。
    """
    repo_name = url.rstrip("/").split("/")[-1].removesuffix(".git")
    candidates = build_version_ref_candidates(version, repo_name=repo_name)
    if not candidates:
        if silent:
            return ""
        print("[ERROR] 指定了空版本号，无法解析 ref", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] 开始解析版本 {version} 对应的远端 ref")
    result = run_git_command(["git", "ls-remote", "--refs", url, *candidates], timeout=120)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        message = stderr or "git ls-remote 执行失败"
        if silent:
            print(f"[WARN] 远端 ref 查询失败（静默）: {message}", file=sys.stderr)
            return ""
        print(f"[ERROR] 远端 ref 查询失败: {message}", file=sys.stderr)
        sys.exit(1)

    matched_refs: set[str] = set()
    for line in result.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2:
            matched_refs.add(parts[1])

    for candidate in candidates:
        if candidate in matched_refs:
            print(f"[INFO] 命中远端 ref: {candidate}")
            return candidate

    if silent:
        return ""
    print(
        f"[ERROR] 未找到与版本 {version} 对应的 tag/branch，已尝试: {', '.join(candidates)}",
        file=sys.stderr,
    )
    sys.exit(1)


def clone_repo_at_ref(url: str, dest: Path, ref: str) -> None:
    """使用指定 ref 做浅克隆/检出。"""
    output_dir = dest.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    # Commit hash: cannot use --branch; do a full clone then checkout
    if re.fullmatch(r"[0-9a-f]{7,40}", ref, re.IGNORECASE):
        print(f"[INFO] git clone (full) + checkout {ref[:12]}...")
        result = subprocess.run(["git", "clone", url, str(dest)], capture_output=False, timeout=600)
        if result.returncode != 0:
            print(f"[ERROR] git clone 失败，退出码: {result.returncode}", file=sys.stderr)
            sys.exit(1)
        result = subprocess.run(["git", "-C", str(dest), "checkout", ref], capture_output=False, timeout=60)
        if result.returncode != 0:
            print(f"[ERROR] git checkout {ref} 失败，退出码: {result.returncode}", file=sys.stderr)
            sys.exit(1)
        return

    if ref.startswith("refs/tags/"):
        tag = ref.removeprefix("refs/tags/")
        command = ["git", "clone", "--depth=1", "--recurse-submodules",
                   "--branch", tag, url, str(dest)]
    else:
        branch = ref.removeprefix("refs/heads/")
        command = ["git", "clone", "--depth=1", "--recurse-submodules",
                   "--branch", branch, "--single-branch", url, str(dest)]

    print(f"[INFO] git clone --depth=1 定向检出 {ref}")
    result = subprocess.run(command, capture_output=False, timeout=300)
    if result.returncode != 0:
        print(f"[ERROR] 基于 ref {ref} 的 git clone 失败，退出码: {result.returncode}", file=sys.stderr)
        sys.exit(1)


UNSTABLE_SUFFIXES = re.compile(
    r"(?:"
    r"[-.](?:alpha|beta|rc|preview|dev|snapshot|canary|nightly|dirty|unstable|next|milestone)\d*"
    r"|"
    r"(?<=[0-9])(?:a|b|c|rc|alpha|beta|pre|dev|post)\d*"  # 无分隔符后缀：3.0a6, 3.1.0rc1
    r")(?:\b|$)",
    re.IGNORECASE,
)


def list_remote_tags(url: str) -> list[str]:
    """Return sorted list of tag names from the remote (newest-looking first)."""
    result = run_git_command(["git", "ls-remote", "--tags", "--refs", url], timeout=60)
    if result.returncode != 0:
        return []
    tags = []
    for line in result.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2:
            ref = parts[1]
            tag = ref.removeprefix("refs/tags/")
            tags.append(tag)
    # Sort: tags that look like version numbers last-to-first (newest first)
    def sort_key(t: str) -> tuple:
        return _parse_version_tuple(t)
    return sorted(tags, key=sort_key, reverse=True)


def _parse_version_tuple(version_str: str) -> tuple:
    """Parse a version string like 'v1.4.0' or '3.0a6' into a comparable tuple.

    先从 version_str 中剥离预发布后缀标识及紧随数字，再提取剩余的数字序列。
    > "3.0a6"  → (3, 0)    # a6 的 6 不计入版本号
    > "1.0.0-rc.1" → (1, 0, 0)  # -rc.1 整体剥离
    """
    normalized = version_str.lstrip("vV")
    cleaned = re.sub(
        r"(?:alpha|beta|rc|preview|dev|a|b|c|post|milestone)[.\d]*",
        "", normalized, flags=re.IGNORECASE,
    )
    parts = re.findall(r"\d+", cleaned)
    return tuple(int(p) for p in parts) if parts else (0,)


def _meets_constraint(tag: str, constraint: str) -> bool:
    """Check if a tag satisfies a constraint like '>= 1.4.0' or '== 1.4.0'."""
    if not constraint:
        return True
    tag_ver = _parse_version_tuple(tag)
    for part in re.split(r"\s*,\s*", constraint.strip()):
        part = part.strip()
        m = re.match(r"(>=|<=|==|!=|>|<)\s*(.+)", part)
        if not m:
            if part.strip():
                print(f"[WARN] 无法解析的约束部分: '{part}'，跳过", file=sys.stderr)
            continue
        op, ver_str = m.group(1), m.group(2).strip()
        req_ver = _parse_version_tuple(ver_str)
        if op == ">=" and not (tag_ver >= req_ver):
            return False
        elif op == "<=" and not (tag_ver <= req_ver):
            return False
        elif op == "==" and not (tag_ver == req_ver):
            return False
        elif op == "!=" and not (tag_ver != req_ver):
            return False
        elif op == ">" and not (tag_ver > req_ver):
            return False
        elif op == "<" and not (tag_ver < req_ver):
            return False
    return True


def select_best_version(url: str, constraint: str = "") -> Optional[dict]:
    """从远端 tags 中选出最佳版本（含稳定性信息）。

    返回格式：{"version": "3.0a6", "is_stable": False, "reason": "..."}
    稳定版优先，无稳定版时回退到满足约束的最新预发布版。
    返回 None 表示真·没有满足约束的版本。

    - constraint 为精确版本（如 '1.4.0' 或 '== 1.4.0'）时直接返回该版本
    - constraint 为区间（如 '>= 1.4.0'）时选满足条件的最小稳定版
    - constraint 为空时选最新稳定版
    """
    # 精确版本快路径：直接返回，不查 tags（保持兼容）
    if constraint:
        exact = re.match(r"^(?:==\s*)?([0-9][^\s,]*)$", constraint.strip())
        if exact:
            ver = exact.group(1).strip()
            is_stable = not UNSTABLE_SUFFIXES.search(ver)
            return {
                "version": ver,
                "is_stable": is_stable,
                "reason": "精确版本约束，直接采用",
            }

    tags = list_remote_tags(url)
    if not tags:
        return None

    stable = [t for t in tags if not UNSTABLE_SUFFIXES.search(t)]
    unstable = [t for t in tags if UNSTABLE_SUFFIXES.search(t)]

    if constraint:
        stable = [t for t in stable if _meets_constraint(t, constraint)]
        unstable = [t for t in unstable if _meets_constraint(t, constraint)]

    # stable 优先，取最小的满足约束的稳定版（降序列表，最后一个是最小满足版）
    if stable:
        return {
            "version": stable[-1],
            "is_stable": True,
            "reason": "满足约束的最新稳定版",
        }

    # 回退到 unstable（AI 可接受或拒绝）
    if unstable:
        return {
            "version": unstable[0],   # 降序列表，第一个是最新预发布版
            "is_stable": False,
            "reason": f"无满足约束的稳定版，回退到最新预发布版 {unstable[0]}",
        }

    return None  # 真·没有版本


def detect_project_version(dest: Path) -> Optional[str]:
    """Detect declared version from common manifest files."""
    # Cargo.toml (Rust)
    cargo_toml = dest / "Cargo.toml"
    if cargo_toml.exists():
        content = cargo_toml.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r'^\s*version\s*=\s*["\']([^"\']+)["\']', content, re.MULTILINE)
        if m:
            return m.group(1).strip()
    # pyproject.toml (Python)
    pyproject = dest / "pyproject.toml"
    if pyproject.exists():
        content = pyproject.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r'^\s*version\s*=\s*["\']([^"\']+)["\']', content, re.MULTILINE)
        if m:
            return m.group(1).strip()
    # pom.xml (Java)
    pom = dest / "pom.xml"
    if pom.exists():
        content = pom.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"<version>\s*([^<\s]+)\s*</version>", content)
        if m:
            return m.group(1).strip()
    # setup.py (Python legacy)
    setup_py = dest / "setup.py"
    if setup_py.exists():
        content = setup_py.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"""version\s*=\s*['"]([^'"]+)['"]""", content)
        if m:
            return m.group(1).strip()
    # package.json (Node.js)
    pkg_json = dest / "package.json"
    if pkg_json.exists():
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
            return data.get("version", "")
        except Exception:
            pass
    return None


def download_git_repo(url: str, output_dir: Path, version: Optional[str] = None,
                      ref: Optional[str] = None, constraint: Optional[str] = None,
                      pkgname: Optional[str] = None) -> Path:
    """下载 git 仓库，支持按版本解析 branch/tag。

    version 优先级：
    1. 精确版本（已指定）→ 直接用
    2. constraint 区间 → select_best_version 选最佳版本（稳定版优先，不稳定版回退）
    3. 都没有 → clone main，检测到开发版时自动切换稳定 tag

    pkgname 不为空时，输出目录使用 pkgname（而非从 URL 提取的 repo 名），
    保证 download 和后续 check/gate 步骤的 source_dir 一致。
    """
    repo_name = url.rstrip("/").split("/")[-1]
    if repo_name.endswith(".git"):
        repo_name = repo_name[:-4]
    dest = output_dir / (pkgname if pkgname else repo_name)

    if dest.exists():
        print(f"[INFO] 目录已存在，跳过克隆: {dest}")
        return dest

    output_dir.mkdir(parents=True, exist_ok=True)

    resolved_ref = ref

    # 若没有明确版本但有 constraint，先从远端 tags 选版本
    if not version and not resolved_ref and constraint:
        print(f"[INFO] dependency mode：根据 constraint '{constraint}' 从远端 tags 选稳定版本...",
              file=sys.stderr)
        selected = select_best_version(url, constraint)
        if selected:
            version_str = selected["version"]
            print(f"[INFO] 选定版本: {version_str} (stable={selected['is_stable']})", file=sys.stderr)
            version = version_str
        else:
            print(f"[WARN] 未找到满足 '{constraint}' 的版本，回退到默认分支", file=sys.stderr)

    if version and not resolved_ref:
        resolved_ref = resolve_git_ref(url, version)

    if resolved_ref:
        clone_repo_at_ref(url, dest, resolved_ref)
    else:
        print(f"[INFO] git clone --depth=1 {url}")
        result = subprocess.run(
            ["git", "clone", "--depth=1", "--recurse-submodules", url, str(dest)],
            capture_output=False,
            timeout=300,
        )
        if result.returncode != 0:
            print(f"[ERROR] git clone 失败，退出码: {result.returncode}", file=sys.stderr)
            sys.exit(1)

        # Detect version in default branch — if it has a matching tag, switch to it for reproducibility
        detected_ver = detect_project_version(dest)
        allow_unstable = _load_config().get("version_check", {}).get("allow_unstable", False)
        if detected_ver and UNSTABLE_SUFFIXES.search(detected_ver) and not allow_unstable:
            print(
                f"[WARN] 默认分支版本 '{detected_ver}' 为不稳定版本，自动查找最新稳定 tag...",
                file=sys.stderr,
            )
            tags = list_remote_tags(url)
            stable_tag = next(
                (t for t in tags if not UNSTABLE_SUFFIXES.search(t)),
                None,
            )
            if not stable_tag:
                tag_list = "\n".join(f"  - {t}" for t in tags[:20]) if tags else "  （无可用 tag）"
                print(
                    f"\n[BLOCK] 默认分支版本 '{detected_ver}' 为不稳定版本，且上游无可用稳定 tag。\n"
                    f"请通过 --version 指定正式 tag。\n"
                    f"\n上游可用 tag（最多显示 20 个）：\n{tag_list}\n",
                    file=sys.stderr,
                )
                shutil.rmtree(dest, ignore_errors=True)
                sys.exit(1)
            print(f"[INFO] 自动切换到最新稳定 tag: {stable_tag}", file=sys.stderr)
            shutil.rmtree(dest, ignore_errors=True)
            clone_repo_at_ref(url, dest, stable_tag)
        elif detected_ver and UNSTABLE_SUFFIXES.search(detected_ver) and allow_unstable:
            print(
                f"[WARN] 默认分支版本 '{detected_ver}' 为不稳定版本，"
                f"version_check.allow_unstable=true，继续引入",
                file=sys.stderr,
            )
        elif detected_ver:
            # Stable version declared in default branch — try to switch to its matching tag
            # so builds are pinned to an immutable ref and fully reproducible.
            matched_ref = None
            try:
                matched_ref = resolve_git_ref(url, detected_ver, silent=True) or None
            except SystemExit:
                pass  # should not happen with silent=True, but guard anyway
            if matched_ref and matched_ref.startswith("refs/tags/"):
                print(
                    f"[INFO] 默认分支声明版本 '{detected_ver}'，自动切换到对应 tag {matched_ref} 以保证可复现",
                    file=sys.stderr,
                )
                shutil.rmtree(dest, ignore_errors=True)
                clone_repo_at_ref(url, dest, matched_ref)
            else:
                print(
                    f"[WARN] 未找到版本 '{detected_ver}' 对应的 tag，保留默认分支（可复现性较弱）",
                    file=sys.stderr,
                )

    print(f"[INFO] 克隆完成: {dest}")
    return dest


def download_tarball(url: str, output_dir: Path, pkgname: Optional[str] = None) -> Path:
    """下载压缩包并解压，返回解压后的目录路径。

    pkgname 不为空时，将解压结果重命名为 pkgname，保证调用方按 pkgname 能找到目录。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = url.split("/")[-1].split("?")[0]
    dest_file = output_dir / filename

    if not dest_file.exists():
        print(f"[INFO] 下载: {url}")
        result = subprocess.run(
            ["wget", "-q", "--show-progress", "-O", str(dest_file), url],
            timeout=300,
        )
        if result.returncode != 0:
            print(f"[ERROR] 下载失败", file=sys.stderr)
            sys.exit(1)
    else:
        print(f"[INFO] 文件已存在，跳过下载: {dest_file}")

    print(f"[INFO] 解压: {dest_file}")
    if filename.endswith(".zip"):
        subprocess.run(["unzip", "-q", str(dest_file), "-d", str(output_dir)], check=True)
    else:
        subprocess.run(["tar", "-xf", str(dest_file), "-C", str(output_dir)], check=True)

    entries = [e for e in output_dir.iterdir() if e.is_dir()]
    if len(entries) == 1:
        extracted = entries[0]
    else:
        stem = re.sub(r"\.(tar\.\w+|tgz|zip)$", "", filename)
        for e in entries:
            if stem in e.name:
                extracted = e
                break
        else:
            extracted = entries[0] if entries else output_dir

    if pkgname and extracted.name != pkgname:
        target = output_dir / pkgname
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        shutil.move(str(extracted), str(target))
        print(f"[INFO] tarball 解压目录已重命名: {extracted.name} -> {pkgname}")
        return target
    return extracted


def download_source(url: str, output_dir: Path, version: Optional[str] = None,
                    ref: Optional[str] = None, constraint: Optional[str] = None,
                    pkgname: Optional[str] = None) -> Path:
    """根据 URL 类型自动选择下载方式"""
    url_type = detect_url_type(url)
    print(f"[INFO] URL 类型: {url_type}")

    if url_type == "git_repo":
        return download_git_repo(url, output_dir, version=version, ref=ref, constraint=constraint,
                                 pkgname=pkgname)
    elif url_type == "tarball":
        return download_tarball(url, output_dir, pkgname=pkgname)
    else:
        print(f"[WARN] 未知 URL 类型，尝试 git clone: {url}")
        return download_git_repo(url, output_dir, version=version, ref=ref, constraint=constraint,
                                 pkgname=pkgname)


# ── 3. 主入口 ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="从 PR 提取上游地址并下载源码")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pr-json", help="已有的 pr_N_info.json 文件路径")
    group.add_argument("--upstream-url", help="直接指定上游 URL（跳过 PR 解析）")
    parser.add_argument("--output-dir", default="./sources", help="源码下载目录（默认 ./sources）")
    parser.add_argument("--version", default="", help="期望版本，用于按版本选择 git tag/branch")
    parser.add_argument("--constraint", default="", help="版本约束（dependency mode），如 '>= 1.4.0'")
    parser.add_argument("--ref", default="", help="显式指定 git ref（内部扩展参数）")
    parser.add_argument("--pkgname", default="", help="输出目录名（默认从 URL 提取 repo 名；传此参数则用指定名，保证与后续步骤的 source_dir 一致）")
    parser.add_argument("-o", "--output", default="", help="将结果写入 JSON 文件")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)

    if args.upstream_url:
        url = args.upstream_url
    else:
        if not os.path.exists(args.pr_json):
            print(f"[ERROR] 文件不存在: {args.pr_json}", file=sys.stderr)
            sys.exit(1)
        url = extract_upstream_url(args.pr_json)
        if not url:
            print("[ERROR] 未在 PR 文件中找到 upstream 字段", file=sys.stderr)
            sys.exit(1)

    requested_version = args.version.strip() or None
    requested_ref = args.ref.strip() or None
    requested_constraint = args.constraint.strip() or None
    requested_pkgname = args.pkgname.strip() or None
    source_dir = download_source(url, output_dir, version=requested_version,
                                 ref=requested_ref, constraint=requested_constraint,
                                 pkgname=requested_pkgname)
    print(f"\nSOURCE_DIR={source_dir}")

    if args.output:
        result = {
            "upstream_url": url,
            "source_dir": str(source_dir),
            "requested_version": requested_version or "",
            "requested_ref": requested_ref or "",
        }
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[INFO] 结果已保存: {args.output}")


if __name__ == "__main__":
    main()
