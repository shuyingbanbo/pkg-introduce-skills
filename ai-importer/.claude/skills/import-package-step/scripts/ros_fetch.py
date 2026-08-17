#!/usr/bin/env python3
"""
ROS 源码获取：upstream cache → session（替代 download_source）

  - 从 upstream cache（挂载卷，env ROS_UPSTREAM_CACHE，默认 /app/upstream_cache）
    取该包源码（cache/src/<repo>/，已 vcs clone + checkout humble 分支），
    拷入 session 的 sources/<pkg>
  - cache 修正表处理：
    - ros-url-fix：包名在表内 → 用表内 URL 换真实仓库（-release 伪装仓库修正）
    - ros-version-fix：包名在表内 → checkout 指定版本（version_mismatch 修正）
  - cache 缺失 → 直接 git clone 到 sources/<pkg>（--depth 1，按清单 URL 分支），
    不写 cache（增量初始化属部署期任务，避免并发写风险）
  - spec 基线：cache/repo/<pkg>.spec 存在则拷入 pkgs/<pkg>/reference/（ros_spec 的
    起点，LLM 做 diff 审查而非从零写）

用法：
  python3 ros_fetch.py --pkg <pkgname> --session-dir <sd> [--ros-distro humble]
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR.parents[1] / "build-rpm" / "scripts" / "data" / "ros"


def _parse_repo_url(raw_url: str) -> tuple[str, str]:
    """清单 URL → (git_url, branch)。支持 .../tree/<branch> 与裸 URL。"""
    url = raw_url.strip()
    if "/tree/" in url:
        head, branch = url.split("/tree/", 1)
        return head, branch.strip()
    return url, "humble"


def _load_map(path: Path) -> dict:
    if not path.exists():
        return {}
    m = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            m[parts[0]] = parts[1]
    return m


def _reregister_deps(sd: Path, pkg: str) -> None:
    """源码就位后补跑 ros_prep 的依赖分析/注册（幂等）。

    ros_prep 首次运行在 ros_fetch 之前，sources 为空时依赖分析静默跳过，
    upstream 依赖会因此漏注册（smach-ros 残废包事故：构建成功但 spec 无
    smach/smach-msgs 依赖）。fetch 完成后重跑 ros_prep，补齐 manifest 的
    registered_deps 与 dep_registry。
    """
    cmd = [sys.executable, str(SCRIPT_DIR / "ros_prep.py"),
           "--pkg", pkg, "--session-dir", str(sd)]
    try:
        sess = json.loads((sd / "session.json").read_text(encoding="utf-8"))
        deep = sess.get("deep_dependency")
        # 递归默认为开：仅显式关闭（false/"0"）时传 --no-deep
        if deep is False or str(deep).lower() in ("0", "false"):
            cmd.append("--no-deep")
        else:
            cmd.append("--deep")
    except Exception:
        pass
    try:
        rc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if rc.returncode != 0:
            print(f"[ros_fetch] WARN 依赖补注册失败: {rc.stderr.strip()[:200]}",
                  file=sys.stderr)
        else:
            print("[ros_fetch] 依赖补注册完成（ros_prep 重跑）")
    except Exception as exc:
        print(f"[ros_fetch] WARN 依赖补注册异常: {exc}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="ROS 源码获取")
    parser.add_argument("--pkg", required=True)
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--ros-distro", default="humble")
    args = parser.parse_args()

    import os
    sd = Path(args.session_dir)
    full = args.pkg.strip().replace("_", "-")
    cands = [full]
    for pfx in ("ros-humble-", "ros2-"):
        if full.startswith(pfx) and full[len(pfx):]:
            cands.append(full[len(pfx):])
    # 与 ros_prep 选名对齐：优先采用已生成 manifest 的候选名（完整名优先）。
    # ros2-numpy 这类上游名自带 ros2- 前缀，盲剥成 numpy 会找不到
    # pkgs/<pkg>/ros_pkg_manifest.json，且报告把包名显示成 numpy
    pkg = next((n for n in cands
                if (sd / "pkgs" / n / "ros_pkg_manifest.json").exists()), cands[0])

    src_dir = sd / "sources" / pkg
    if src_dir.is_dir() and any(src_dir.iterdir()):
        print(f"[ros_fetch] 源码已存在: {src_dir}")
        _reregister_deps(sd, pkg)
        return 0

    # ── 1. 从 manifest 定位仓库 ─────────────────────────────────────────────
    manifest_path = sd / "pkgs" / pkg / "ros_pkg_manifest.json"
    repo_branch = ""
    target_version = ""
    listed_version = ""
    tier = "sig"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        repo_url = manifest.get("repo_url", "")
        repo_branch = manifest.get("repo_branch", "")
        target_version = manifest.get("target_version", "")
        listed_version = manifest.get("listed_version", "")
        tier = manifest.get("tier", "sig")
    else:
        # 无 manifest（异常路径）：依次查 SIG 清单、rosdistro 全量清单
        repo_url = ""
        for fname in ("ros-projects.list", "ros-upstream.list"):
            lst = DATA_DIR / args.ros_distro / fname
            if not lst.exists():
                continue
            for line in lst.read_text(encoding="utf-8", errors="ignore").splitlines():
                parts = line.strip().split("\t")
                if len(parts) >= 4 and parts[0].strip() == pkg:
                    repo_url = parts[1].strip()
                    if fname == "ros-upstream.list" and len(parts) >= 5:
                        repo_branch = parts[2].strip()
                        target_version = parts[4].strip()
                    break
            if repo_url:
                break
    if not repo_url:
        print(f"[ERROR] 无法定位 {pkg} 的上游仓库", file=sys.stderr)
        return 1

    # ── 2. 修正表 ───────────────────────────────────────────────────────────
    config_dir = DATA_DIR / args.ros_distro / "config"
    url_fix = _load_map(config_dir / "ros-url-fix")
    ver_fix = _load_map(config_dir / "ros-version-fix")
    if pkg in url_fix:
        print(f"[ros_fetch] ros-url-fix 命中: {pkg} → {url_fix[pkg]}")
        repo_url = url_fix[pkg]
    git_url, branch = _parse_repo_url(repo_url)
    # upstream/user  tier 的 URL 是纯仓库地址，分支来自 manifest/清单的独立列
    if repo_branch and "/tree/" not in repo_url:
        branch = repo_branch
    if pkg in ver_fix:
        branch = ver_fix[pkg]
        print(f"[ros_fetch] ros-version-fix 命中: {pkg} → checkout {branch}")
    # 目标版本 tag 优先：清单外包（upstream/user tier）或用户显式指定版本
    # （target != listed，升级场景）时，先尝试 checkout 版本号对应的 tag，
    # 拉不到再回退分支（tag 命名不统一是上游现实，见方案风险点）
    ver_tag = ""
    if target_version and (tier != "sig" or target_version != listed_version):
        ver_tag = target_version.split("-", 1)[0].strip()

    # ── 3. cache 优先，缺则 clone ───────────────────────────────────────────
    cache_base = Path(os.environ.get("ROS_UPSTREAM_CACHE", "/app/upstream_cache"))
    repo_name = git_url.rstrip("/").split("/")[-1].removesuffix(".git")
    cache_src = cache_base / "src" / repo_name

    src_dir.parent.mkdir(parents=True, exist_ok=True)
    if cache_src.is_dir():
        print(f"[ros_fetch] 从 cache 拷贝: {cache_src}")
        shutil.copytree(cache_src, src_dir, dirs_exist_ok=True)
        # cache 分支可能与目标分支不一致（version-fix 场景）：重新 checkout；
        # ver_tag 优先（见上），tag 不存在回退分支
        try:
            if ver_tag:
                rc = subprocess.run(["git", "-C", str(src_dir), "checkout", ver_tag, "--"],
                                    capture_output=True, text=True, timeout=120)
                if rc.returncode != 0:
                    print(f"[ros_fetch] tag {ver_tag} 不存在，回退分支 {branch}",
                          file=sys.stderr)
                    subprocess.run(["git", "-C", str(src_dir), "checkout", branch, "--"],
                                   capture_output=True, text=True, timeout=120)
            else:
                subprocess.run(["git", "-C", str(src_dir), "checkout", branch, "--"],
                               capture_output=True, text=True, timeout=120)
            subprocess.run(["git", "-C", str(src_dir), "submodule", "update",
                            "--init", "--recursive", "--depth", "1"],
                           capture_output=True, text=True, timeout=600)
        except Exception as exc:
            print(f"[ros_fetch] WARN checkout branch failed: {exc}", file=sys.stderr)
    else:
        print(f"[ros_fetch] cache 未命中 {cache_src}，直接 clone {git_url}@{ver_tag or branch}")
        try:
            if ver_tag:
                rc = subprocess.run(
                    ["git", "clone", "--depth", "1", "--branch", ver_tag,
                     "--recursive", git_url, str(src_dir)],
                    capture_output=True, text=True, timeout=900,
                )
                if rc.returncode != 0:
                    print(f"[ros_fetch] tag {ver_tag} clone 失败，回退分支 {branch}",
                          file=sys.stderr)
                    shutil.rmtree(src_dir, ignore_errors=True)
                    rc = subprocess.run(
                        ["git", "clone", "--depth", "1", "--branch", branch,
                         "--recursive", git_url, str(src_dir)],
                        capture_output=True, text=True, timeout=900,
                    )
            else:
                rc = subprocess.run(
                    ["git", "clone", "--depth", "1", "--branch", branch,
                     "--recursive", git_url, str(src_dir)],
                    capture_output=True, text=True, timeout=900,
                )
            if rc.returncode != 0:
                print(f"[ERROR] clone 失败: {rc.stderr.strip()[:300]}", file=sys.stderr)
                return 1
        except FileNotFoundError:
            print("[ERROR] git 不可用", file=sys.stderr)
            return 1

    # ── 4. spec 基线（cache/repo/<pkg>.spec → reference/）──────────────────
    spec_cache = cache_base / "repo" / f"{pkg}.spec"
    if spec_cache.exists():
        ref_dir = sd / "pkgs" / pkg / "reference"
        ref_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(spec_cache, ref_dir / f"{pkg}.spec")
        print(f"[ros_fetch] spec 基线已拷入 reference/: {spec_cache.name}")

    # ── 5. 依赖补注册（源码就位后 ros_prep 幂等重跑，见函数注释）────────────
    _reregister_deps(sd, pkg)

    print(f"[ros_fetch] done: {src_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
