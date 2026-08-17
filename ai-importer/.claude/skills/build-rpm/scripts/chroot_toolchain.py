#!/usr/bin/env python3
"""构建工具链清单（Toolchain Manifest）生成与查询。

为每个目标 chroot 生成一份清单，回答："该 chroot 的官方源里有哪些构建工具、
什么版本"。这份清单作为全局约束：AI 不得升级/引入构建工具，只能用清单里
的版本；若上游要求更高版本，应由 AI 修改 spec/源码适应当前 chroot 的版本。

白名单（名单）与清单（manifest）的关系：
- 名单（BUILD_SYSTEM_TOOLS / TOOLCHAIN_PACKAGES）回答身份问题："是不是构建工具"。
- 清单回答能力问题："目标 chroot 里这个构建工具是什么版本"。
- 清单 = 名单 × chroot 官方源查询；名单是清单的输入定义，清单是名单的运行时实例化。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from rpm_batch_lookup import chroot_to_repofrompath


# ── 构建工具名单（唯一事实来源）───────────────────────────────────────────────

# Python build-system 后端 / 构建辅助工具。对应 upstream/PyPI 名，归一化后会自动
# 匹配 python3-/python- 前缀的 RPM 名。
BUILD_SYSTEM_TOOLS = {
    # 原 pre_check_deps.py BUILD_SYSTEM_WHITELIST
    "setuptools",
    "setuptools-scm",
    "wheel",
    "pip",
    "flit",
    "flit-core",
    "hatchling",
    "poetry",
    "poetry-core",
    "pdm",
    "pdm-backend",
    "meson-python",
    "scikit-build",
    "ninja",
    "cmake",
    "pbr",
    # 原失败分析 agent 白名单中额外出现的工具
    "build",
    "scikit-build-core",
    "cython",
    "maturin",
    "versioneer",
    "jupyter-packaging",
}

# 通用编译器 / 构建工具链（RPM 名形式，也包含常见别名）。
TOOLCHAIN_PACKAGES = [
    # 通用编译器/构建工具
    "golang",
    "go",  # 别名，有时被称为 go
    "rust",
    "cargo",
    "rustc",  # rust 别名
    "gcc",
    "gcc-c++",
    "g++",  # gcc-c++ 别名
    "make",
    "cmake",
    "meson",
    "ninja-build",
    "ninja",  # ninja-build 别名
    # Java/Node/其他语言工具链
    "nodejs",
    "node",  # nodejs 别名
    "npm",
    "java-latest-openjdk-devel",
    "maven",
    "mvn",  # maven 别名
    "perl",
    # Python 运行时/构建相关（作为构建工具本身）
    "python3",
    "python3-devel",
    "python3-setuptools",
    "python3-pip",
    "python3-wheel",
    # Python build-system 后端对应的 RPM 名（显式列出便于 dnf 查询）
    "python3-flit-core",
    "python3-hatchling",
    "python3-poetry-core",
    "python3-pdm-backend",
    "python3-meson-python",
    "python3-scikit-build",
    "python3-build",
    "python3-cython",
    "python3-maturin",
]


def _normalize_toolchain_name(name: str) -> str:
    """统一工具链名格式，用于跨命名空间比对。

    处理：小写、下划线改连字符、剥离 python3-/python-/py3- 前缀。
    这样 PyPI 名（setuptools-scm）、RPM 名（python3-setuptools_scm）、
    上游名（setuptools_scm）都能归一到同一 key。
    """
    n = name.lower().replace("_", "-")
    for prefix in ("python3-", "python-", "py3-"):
        if n.startswith(prefix):
            n = n[len(prefix):]
    return n


# 归一化后的总名单（身份判断用）
_NORMALIZED_TOOLCHAIN_NAMES: set[str] = {
    _normalize_toolchain_name(n)
    for n in (BUILD_SYSTEM_TOOLS | set(TOOLCHAIN_PACKAGES))
}


def is_toolchain(name: str) -> bool:
    """判断给定名字（PyPI/RPM/上游名均可）是否为已知的构建工具/工具链。"""
    return _normalize_toolchain_name(name) in _NORMALIZED_TOOLCHAIN_NAMES


def is_build_system_tool(name: str) -> bool:
    """判断是否为 Python build-system 后端（保留 pre_check_deps.py 原语义）。"""
    return _normalize_toolchain_name(name) in {
        _normalize_toolchain_name(n) for n in BUILD_SYSTEM_TOOLS
    }


# ── Manifest 生成 ────────────────────────────────────────────────────────────

def _query_toolchain_versions(chroot: str) -> Dict[str, Dict[str, Any]]:
    """用 dnf 直接查询 TOOLCHAIN_PACKAGES 在目标 chroot 官方源中的版本。

    不经过 run_batch_lookup，避免其 cacheonly 策略在缓存"存在但无效"时返回空结果。
    """
    import warnings

    warnings.filterwarnings("ignore")

    try:
        import dnf
    except ImportError as exc:
        raise RuntimeError("dnf Python module not available") from exc

    repofrompath = chroot_to_repofrompath(chroot)
    if not repofrompath:
        raise RuntimeError(f"unknown chroot: {chroot}")

    base = dnf.Base()
    base.conf.cachedir = "/var/cache/dnf"
    # 禁用系统默认 repo，只使用目标 chroot 的 openEuler 官方源
    base.read_all_repos()
    for repo in base.repos.iter_enabled():
        repo.disable()
    for repo_id, url in repofrompath:
        base.repos.add_new_repo(repo_id, base.conf, baseurl=[url])
    # 强制允许下载/刷新 metadata，不因旧缓存存在而跳过
    base.conf.cacheonly = False
    base.fill_sack(load_system_repo=False, load_available_repos=True)

    sack = base.sack
    result: Dict[str, Dict[str, Any]] = {}
    for pkg_name in TOOLCHAIN_PACKAGES:
        pkgs = list(sack.query().filter(name=pkg_name))
        if pkgs:
            pkg = pkgs[0]
            result[pkg_name] = {
                "version": pkg.version,
                "release": pkg.release,
                "available": True,
            }
        else:
            result[pkg_name] = {"version": None, "release": None, "available": False}

    return result


def generate_manifest(chroot: str, output_path: Optional[str | Path] = None) -> Dict[str, Any]:
    """查询目标 chroot 官方源中 TOOLCHAIN_PACKAGES 的版本，生成 manifest。

    若 output_path 给定，将 manifest 写入该文件；同时返回 manifest 字典。
    """
    toolchain = _query_toolchain_versions(chroot)

    manifest = {
        "chroot": chroot,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "toolchain": toolchain,
    }

    if output_path is not None:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    return manifest


def load_manifest(session_dir: str | Path) -> Dict[str, Any]:
    """从 session 目录加载已有的 toolchain manifest（支持多 chroot 合并）。

    若 manifest 尚未生成，返回空 dict；调用方应兼容此情况（向前兼容）。
    """
    sd = Path(session_dir)
    manifests = sorted(sd.glob("toolchain_*.json"))
    if not manifests:
        return {}

    merged_toolchain: Dict[str, Dict[str, Any]] = {}
    for m in manifests:
        try:
            data = json.loads(m.read_text(encoding="utf-8"))
            merged_toolchain.update(data.get("toolchain", {}))
        except Exception:
            continue

    return {"toolchain": merged_toolchain}


def get_tool_version(session_dir: str | Path, tool_name: str) -> Optional[str]:
    """快捷读取 session manifest 中某工具的版本（未生成或不存在返回 None）。"""
    manifest = load_manifest(session_dir)
    info = manifest.get("toolchain", {}).get(tool_name)
    if info and info.get("available"):
        return info.get("version")
    return None


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="生成目标 chroot 的构建工具链清单")
    parser.add_argument("chroot", help="chroot 名称，如 openeuler-24.03_LTS_SP3-x86_64")
    parser.add_argument("--session-dir", required=True, help="session 目录，清单写在这里")
    args = parser.parse_args()

    out = Path(args.session_dir) / f"toolchain_{args.chroot}.json"
    manifest = generate_manifest(args.chroot, out)
    print(f"[chroot_toolchain] generated {out}: {sum(1 for v in manifest['toolchain'].values() if v['available'])} tools available")