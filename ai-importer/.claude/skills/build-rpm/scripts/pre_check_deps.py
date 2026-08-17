#!/usr/bin/env python3
"""
RPM 编译前依赖预检脚本

在 rpmbuild 循环前调用，分析运行时依赖在 openEuler 源、官方归档仓库、用户 RPM 仓库中的可用性，
输出需要递归引入的包列表（格式：<pkgname> <upstream_url>），供调用方继续处理。

用法：
  python3 pre_check_deps.py <pkgname> <lang> <source_dir> [--container oe-build-env]

退出码：
  0 — 所有依赖均已满足或可复用
  2 — 存在需要递归引入/升级的依赖（stdout 输出 <name> <url> 列表）
  1 — 存在阻断项或脚本执行出错
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from rpm_naming import get_rpm_pkg_name, get_srpm_name, get_compat_srpm_name, get_compat_rpm_pkg_name, extract_compat_major_version  # noqa: E402
from constraint_parser import parse_constraint as _parse_constraint  # noqa: E402
from chroot_toolchain import is_build_system_tool  # noqa: E402

CHECK_EXISTING_SCRIPT = SCRIPT_DIR / "check_existing_package.py"
ANALYZE_PYTHON_SCRIPT = SCRIPT_DIR / "analyze_python_deps.py"
CASCADE_CHECK_SCRIPT = SCRIPT_DIR / "cascade_package_check.py"


def _load_pkg_introduce_config() -> dict:
    config_path = SCRIPT_DIR.parent.parent / "pkg-introduce" / "config.json"
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _dep_conflict_mode() -> str:
    """返回依赖冲突处理模式: 'block'（默认阻断）、'compat'（兼容包名引入）或 'force_compat'（所有语言强制 compat）。"""
    cfg = _load_pkg_introduce_config()
    return cfg.get("dep_conflict", {}).get("mode", "block")

# ── 语言 → 分析脚本映射 ───────────────────────────────────────────────────────

ANALYZERS = {
    "python": {"script": "analyze_python_deps.py", "extra_args": []},
    "go":     {"script": "analyze_go_deps.py",     "extra_args": []},
    "rust":   {"script": "analyze_rust_deps.py",   "extra_args": []},
    "c":      {"script": "analyze_c_deps.py",      "extra_args": []},
    "cpp":    {"script": "analyze_cpp_deps.py",    "extra_args": []},
    "nodejs": {"script": "analyze_nodejs_deps.py", "extra_args": []},
    "java":   {"script": "analyze_java_deps.py",   "extra_args": []},
    "ros":    {"script": "analyze_ros_deps.py",    "extra_args": []},
}

# vendor 语言闭集：这些语言的语言级依赖（crate/module）由 vendor 解决，
# 不进 dep_registry、不打独立 RPM。
VENDOR_LANGS = {"go", "rust"}

# ── 混合包副语言探测（与主 lang 正交，语言无关）─────────────────────────────
# 主包是 python/c 等语言时，源码里可能混入 Cargo.toml / go.mod（如 pendulum
# 的 rust/ 子目录、setuptools-rust 的 src/rust/）。只探测 vendor 语言，
# 浅层 glob：根目录 + 至多两层子目录。

SECONDARY_PROBE = {"Cargo.toml": "rust", "go.mod": "go"}


def detect_secondary_langs(lang: str, source_dir: str) -> tuple[list[str], dict[str, str]]:
    """返回 (secondary_langs, secondary_manifests)。

    secondary_manifests: lang → manifest 相对路径，如 {"rust": "rust/Cargo.toml"}，
    供 builder 定位 vendor / analyzer 的工作目录。
    """
    src = Path(source_dir)
    secondary: list[str] = []
    manifests: dict[str, str] = {}
    for fname, probe_lang in SECONDARY_PROBE.items():
        if probe_lang == lang:
            continue
        candidates = (
            list(src.glob(fname))
            + list(src.glob(f"*/{fname}"))
            + list(src.glob(f"*/*/{fname}"))
        )
        if candidates:
            secondary.append(probe_lang)
            manifests[probe_lang] = str(min(candidates, key=lambda p: len(p.parts)).relative_to(src))
    return secondary, manifests


def load_existing_checker() -> Any:
    script_dir = str(SCRIPT_DIR)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    spec = importlib.util.spec_from_file_location("check_existing_package", CHECK_EXISTING_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载脚本: {CHECK_EXISTING_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EXISTING_CHECKER = load_existing_checker()


def _load_cascade_checker() -> Any:
    spec = importlib.util.spec_from_file_location("cascade_package_check", CASCADE_CHECK_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载脚本: {CASCADE_CHECK_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CASCADE_CHECKER = _load_cascade_checker()


def load_python_upstream_helpers() -> dict[str, Any]:
    script_dir = str(SCRIPT_DIR)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    spec = importlib.util.spec_from_file_location("analyze_python_deps", ANALYZE_PYTHON_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载脚本: {ANALYZE_PYTHON_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {
        "fetch_pypi_info": module.fetch_pypi_info,
        "canonical_upstream_url": module.canonical_upstream_url,
        "classify_upstream_url": module.classify_upstream_url,
        "normalize_candidate_upstream": module.normalize_candidate_upstream,
        "candidate_urls_from_pypi_info": module.candidate_urls_from_pypi_info,
    }


PYTHON_UPSTREAM_HELPERS = load_python_upstream_helpers()


# ── PyPI 上游地址查询 ──────────────────────────────────────────────────────────

def _github_search_repo(pkg_name: str) -> str:
    """通过 GitHub Search API 查找包名对应的仓库，返回 html_url 或空串。"""
    # 尝试常见 org 直接命中（避免 Search API 速率限制）
    normalized = pkg_name.replace("-", "_")
    for candidate in [pkg_name, normalized]:
        for org in ["", "BeanieODM", "roman-right"]:
            path = f"{org}/{candidate}" if org else candidate
            try:
                req = urllib.request.Request(
                    f"https://api.github.com/repos/{path}",
                    headers={"User-Agent": "pre_check_deps/1.0", "Accept": "application/vnd.github+json"},
                )
                data = json.loads(urllib.request.urlopen(req, timeout=8).read())
                if data.get("html_url"):
                    normalized_url = normalize_upstream_candidate(data["html_url"])
                    if normalized_url:
                        return normalized_url
            except Exception:
                pass
    # fallback: GitHub Search API
    try:
        query = urllib.parse.quote(f"{pkg_name} language:python")
        req = urllib.request.Request(
            f"https://api.github.com/search/repositories?q={query}&per_page=3",
            headers={"User-Agent": "pre_check_deps/1.0", "Accept": "application/vnd.github+json"},
        )
        results = json.loads(urllib.request.urlopen(req, timeout=10).read())
        for item in results.get("items", []):
            name_lower = item.get("name", "").lower().replace("-", "_")
            pkg_lower = pkg_name.lower().replace("-", "_")
            if name_lower == pkg_lower and item.get("html_url"):
                normalized_url = normalize_upstream_candidate(item["html_url"])
                if normalized_url:
                    return normalized_url
    except Exception:
        pass
    return ""


def classify_upstream_candidate(url: str) -> str:
    return PYTHON_UPSTREAM_HELPERS["classify_upstream_url"](url)


def normalize_upstream_candidate(url: str) -> str:
    return PYTHON_UPSTREAM_HELPERS["normalize_candidate_upstream"](url)


def is_trusted_upstream_url(url: str) -> bool:
    return classify_upstream_candidate(url) == "trusted"


def is_suspicious_upstream_url(url: str) -> bool:
    return classify_upstream_candidate(url) == "suspicious"


def get_pypi_upstream(pypi_name: str) -> str:
    """从 PyPI JSON API 提取可信源码仓地址，必要时回退到 GitHub 搜索。"""
    try:
        pypi_json = PYTHON_UPSTREAM_HELPERS["fetch_pypi_info"](pypi_name)
        if pypi_json:
            canonical = PYTHON_UPSTREAM_HELPERS["canonical_upstream_url"](pypi_json, pypi_name)
            if canonical and is_trusted_upstream_url(canonical):
                return canonical
            info = pypi_json.get("info", {})
            for url in PYTHON_UPSTREAM_HELPERS["candidate_urls_from_pypi_info"](info):
                normalized = normalize_upstream_candidate(url)
                if normalized and is_trusted_upstream_url(normalized):
                    return normalized
    except Exception:
        pass
    github_url = _github_search_repo(pypi_name)
    if github_url and is_trusted_upstream_url(github_url):
        return github_url
    return ""


# ── 构建系统工具白名单 ──────────────────────────────────────────────────────────
# 名单定义已收敛到 chroot_toolchain.py 的 BUILD_SYSTEM_TOOLS，此处仅保留导入别名以兼容
# 可能的外部引用。这些工具不是运行时依赖：为应用包递归构建发行版基础设施是本末倒置，
# 且可能引入循环依赖。spec 中 BuildRequires 不带版本，mock 会自动装源里的版本。
# 原集合见 chroot_toolchain.BUILD_SYSTEM_TOOLS
BUILD_SYSTEM_WHITELIST = None  # deprecated; use is_build_system_tool()

# 保留兼容函数签名，但实现复用统一名单
def _is_build_system_tool(name: str) -> bool:
    """判断包名是否为已知的构建系统工具。"""
    return is_build_system_tool(name)


# ── 通用辅助 ──────────────────────────────────────────────────────────────────

def resolve_python_executable() -> str:
    """优先使用 python3.11，不存在时回退到当前 python3。"""
    candidates = [
        "/usr/bin/python3.11",
        "/usr/local/bin/python3.11",
        shutil.which("python3.11"),
        sys.executable,
        shutil.which("python3"),
    ]
    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if candidate.startswith("/") and not Path(candidate).exists():
            continue
        return candidate
    return "python3"


def make_output_path(pkgname: str, requested: str) -> str:
    return requested or f"/tmp/dep_check_{pkgname}.json"


def make_analysis_path(output_path: str, pkgname: str) -> Path:
    out_path = Path(output_path)
    suffix = out_path.suffix or ".json"
    stem = out_path.name[:-len(suffix)] if out_path.name.endswith(suffix) else out_path.name
    return out_path.with_name(f"{stem}_analysis{suffix}") if stem else Path(f"/tmp/dep_check_{pkgname}_analysis.json")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def summarize_source_match(dep: dict[str, Any], source_item: dict[str, Any] | None) -> dict[str, Any]:
    requirement = dep.get("requirement", "")
    if not source_item:
        return {
            "status": "missing",
            "rpm": None,
            "version": None,
            "release": None,
            "satisfies_requirement": False,
            "reason": "openEuler 源中未找到可用包",
        }

    requirement_info = EXISTING_CHECKER.parse_requirement(requirement)
    version = source_item.get("version")
    if requirement_info["status"] == "parsed":
        satisfies = EXISTING_CHECKER.evaluate_requirement(version, requirement_info)
        if satisfies:
            reason = f"openEuler 源中已有满足约束 {requirement} 的包"
            status = "satisfied"
        else:
            reason = f"openEuler 源中已有包，但版本 {version or '未知'} 不满足约束 {requirement}"
            status = "older"
    elif requirement_info["status"] == "unknown":
        satisfies = False
        reason = f"openEuler 源中已有包，但版本约束 {requirement} 无法可靠解析，保守继续"
        status = "unknown_requirement"
    else:
        satisfies = True
        reason = "openEuler 源中已有可用包"
        status = "satisfied"

    return {
        "status": status,
        "rpm": source_item.get("rpm"),
        "version": version,
        "release": source_item.get("release"),
        "satisfies_requirement": bool(satisfies),
        "reason": reason,
    }


def build_source_index(items: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        keys = {
            (item.get("dep", ""), item.get("requirement", "")),
            (item.get("dep", ""), ""),
            (item.get("name", ""), item.get("requirement", "")),
            (item.get("name", ""), ""),
        }
        for key in keys:
            if key[0]:
                index.setdefault(key, item)
    return index


def lookup_source_item(dep: dict[str, Any], source_index: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any] | None:
    keys = [
        (dep.get("dep", ""), dep.get("requirement", "")),
        (dep.get("dep", ""), ""),
        (dep.get("name", ""), dep.get("requirement", "")),
        (dep.get("name", ""), ""),
    ]
    for key in keys:
        if key[0] and key in source_index:
            return source_index[key]
    return None


def merge_official_source_older_result(
    dep: dict[str, Any],
    source_check: dict[str, Any],
    existing_check: dict[str, Any],
) -> dict[str, Any]:
    requested = dict(existing_check.get("requested") or {})
    requested_version = (requested.get("version") or "").strip()
    requirement = (requested.get("requirement") or dep.get("requirement") or "").strip()

    official = dict(existing_check.get("official") or {})
    user_repo = dict(existing_check.get("user_repo") or {})

    highest = {
        "path": "<openeuler-source>",
        "match_type": "source_repo",
        "name": source_check.get("rpm") or dep.get("name") or dep.get("dep") or "",
        "version": source_check.get("version"),
        "release": source_check.get("release"),
        "arch": None,
    }

    matched_paths = list(official.get("matched_paths") or [])
    if "<openeuler-source>" not in matched_paths:
        matched_paths.append("<openeuler-source>")

    candidates = list(official.get("candidates") or [])
    candidates.append(highest)

    official.update(
        {
            "exists": True,
            "matched_paths": matched_paths,
            "candidates": candidates,
            "highest": highest,
            "satisfies_requested_version": False if requested_version else None,
            "satisfies_requirement": False,
            "meets_need": False,
            "comparison_unknown": False,
        }
    )

    decision = EXISTING_CHECKER.choose_decision(official, user_repo, requested_version, requirement)
    patched = dict(existing_check)
    patched["official"] = official
    patched["exists_in_official"] = True
    patched["decision"] = decision
    patched["reason"] = EXISTING_CHECKER.build_reason(
        decision,
        official,
        user_repo,
        requested_version,
        requirement,
    )
    return patched


def resolve_upstream_url(name: str, lang: str) -> str:
    """尝试为任意语言的依赖包解析可信上游仓库根 URL。"""
    if not name:
        return ""
    if lang == "go":
        if name.startswith("github.com/") or name.startswith("gitlab.com/") or name.startswith("golang.org/"):
            candidate = normalize_upstream_candidate("https://" + name)
            return candidate if is_trusted_upstream_url(candidate) else ""
        return _github_search_repo(name.split("/")[-1])
    if lang == "python":
        return get_pypi_upstream(name)
    if lang == "rust":
        try:
            req = urllib.request.Request(
                f"https://crates.io/api/v1/crates/{name}",
                headers={"User-Agent": "pre_check_deps/1.0"},
            )
            data = json.loads(urllib.request.urlopen(req, timeout=10).read())
            repo = data.get("crate", {}).get("repository") or data.get("crate", {}).get("homepage")
            normalized = normalize_upstream_candidate(repo) if repo else ""
            if normalized and is_trusted_upstream_url(normalized):
                return normalized
        except Exception:
            pass
    if lang == "nodejs":
        try:
            req = urllib.request.Request(
                f"https://registry.npmjs.org/{name}/latest",
                headers={"User-Agent": "pre_check_deps/1.0"},
            )
            data = json.loads(urllib.request.urlopen(req, timeout=10).read())
            repo = data.get("repository", {})
            if isinstance(repo, dict):
                url = repo.get("url", "")
            else:
                url = str(repo)
            url = url.replace("git+", "").replace("git://", "https://")
            if url.startswith("github:"):
                url = "https://github.com/" + url[7:]
            normalized = normalize_upstream_candidate(url)
            if normalized and is_trusted_upstream_url(normalized):
                return normalized
        except Exception:
            pass
    return _github_search_repo(name)


def ensure_dependency_upstream(item: dict[str, Any], lang: str) -> tuple[str, str]:
    name = item.get("name") or item.get("dep") or ""
    existing_url = item.get("upstream_url", "") or ""
    suspicious_urls: list[str] = []

    normalized_existing = normalize_upstream_candidate(existing_url)
    if normalized_existing and is_trusted_upstream_url(normalized_existing):
        return normalized_existing, "provided"
    if existing_url:
        suspicious_urls.append(existing_url)
        if normalized_existing and not is_trusted_upstream_url(normalized_existing):
            suspicious_urls.append(normalized_existing)

    resolved = resolve_upstream_url(name, lang)
    if resolved and is_trusted_upstream_url(resolved):
        return resolved, "registry"
    if resolved:
        suspicious_urls.append(resolved)

    if lang == "python" and name:
        metadata_url = f"https://pypi.org/project/{name}"  # noqa: F841 — kept for future use

    return "", "unresolved"


def normalize_dependency_item(item: dict[str, Any], lang: str, category: str) -> dict[str, Any]:
    name = item.get("name") or item.get("dep") or ""
    upstream_url, upstream_resolution = ensure_dependency_upstream(item, lang)
    requirement = item.get("requirement", "") or item.get("constraint", "")
    raw_requirement_info = item.get("requirement_info")
    if not isinstance(raw_requirement_info, dict):
        raw_requirement_info = None
    constraint_type, requirement_info = classify_requirement_constraint(requirement, raw_requirement_info)
    version_source = infer_version_source({**item, "requirement_info": requirement_info})
    return {
        "name": name,
        "dep": item.get("dep") or name,
        "spec": item.get("spec") or item.get("dep") or name,
        "type": item.get("type") or lang,
        "category": category,
        "requirement": requirement,
        "constraint": requirement,
        "constraint_type": constraint_type,
        "version_source": version_source,
        "requirement_info": requirement_info,
        "rpm_requirement": item.get("rpm_requirement") or item.get("rpm_name") or item.get("dep") or name,
        "rpm_pkg_name": item.get("rpm_pkg_name") or get_rpm_pkg_name(lang, name),
        "upstream_url": upstream_url,
        "upstream_resolution": upstream_resolution,
    }


def classify_requirement_constraint(requirement: str, requirement_info: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    """委托给 constraint_parser.parse_constraint，保留函数签名向后兼容。"""
    return _parse_constraint(requirement, requirement_info)


def infer_version_source(item: dict[str, Any], existing_check: dict[str, Any] | None = None) -> str:
    explicit_source = (item.get("version_source") or "").strip()
    if explicit_source:
        return explicit_source

    requirement_info = (item.get("requirement_info") or {}) if isinstance(item.get("requirement_info"), dict) else {}
    if requirement_info.get("source"):
        return str(requirement_info["source"]).strip() or "unknown"

    requested = dict((existing_check or {}).get("requested") or {})
    requested_requirement_info = requested.get("requirement_info")
    if isinstance(requested_requirement_info, dict) and requested_requirement_info.get("source"):
        return str(requested_requirement_info["source"]).strip() or "unknown"

    return "manifest" if (item.get("requirement") or "").strip() else "unknown"


def dependency_items_from_result(lang: str, result: dict[str, Any], pkgname: str = "") -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """返回 (pending_items, preblocked_items)。

    preblocked_items: analyze 阶段已确认版本冲突（社区源有但版本低）的依赖，
                      携带 found_version，供 classify_dependency 直接决策，
                      无需再调用 check_existing_package。
    """
    pending: list[dict[str, Any]] = []
    preblocked: list[dict[str, Any]] = []

    if lang == "python":
        rpm_check = result.get("rpm_check") or {}
        # version_conflict 的包名集合，从 dependency_items 中排除，避免重复处理
        conflict_names = {item.get("name", "") for item in rpm_check.get("version_conflict", [])}
        # missing 的包名集合，也从 dependency_items 中排除
        missing_names = {item.get("name", "") for item in rpm_check.get("missing", [])}
        already_seen = conflict_names | missing_names
        for item in result.get("dependency_items", []):
            if item.get("name", "") not in already_seen:
                pending.append(normalize_dependency_item(item, lang, "runtime"))
        for item in result.get("build_sys_dependency_items", []):
            # 跳过自引用：bootstrap 包（如 flit-core）build-backend 指向自身
            if pkgname and item.get("name", "") == pkgname:
                continue
            # 跳过已在 version_conflict 或 missing 中处理的包，避免重复分类
            if item.get("name", "") in already_seen:
                continue
            pending.append(normalize_dependency_item(item, lang, "build_system"))
        # rpm_check.missing：openEuler 源中不存在的包，必须注册为待引入依赖
        for item in rpm_check.get("missing", []):
            pending.append(normalize_dependency_item(item, lang, "runtime"))
        for item in rpm_check.get("version_conflict", []):
            norm = normalize_dependency_item(item, lang, "runtime")
            norm["found_version"] = item.get("found_version", "")
            norm["preblocked"] = True
            preblocked.append(norm)
        return pending, preblocked

    if lang == "cpp":
        for item in result.get("dependency_items", []):
            pending.append(normalize_dependency_item(item, lang, "runtime"))
        return pending, preblocked

    rpm_check = result.get("rpm_check") or {}
    for item in rpm_check.get("missing", []):
        pending.append(normalize_dependency_item(item, lang, "runtime"))
    for item in rpm_check.get("version_conflict", []):
        norm = normalize_dependency_item(item, lang, "runtime")
        norm["found_version"] = item.get("found_version", "")
        norm["preblocked"] = True
        preblocked.append(norm)

    # nodejs: 同时处理运行时 npm 依赖中未在社区源找到的包
    if lang == "nodejs":
        runtime_deps = result.get("runtime_deps") or {}
        for item in runtime_deps.get("missing", []):
            pending.append(normalize_dependency_item(item, lang, "runtime"))
        for item in runtime_deps.get("version_conflict", []):
            norm = normalize_dependency_item(item, lang, "runtime")
            norm["found_version"] = item.get("found_version", "")
            norm["preblocked"] = True
            preblocked.append(norm)

    return pending, preblocked


def build_available_index_for_result(lang: str, result: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    available_items: list[dict[str, Any]] = []
    rpm_check = result.get("rpm_check") or {}
    available_items.extend(rpm_check.get("available", []))
    if lang == "python":
        build_sys_check = result.get("build_sys_rpm_check") or {}
        available_items.extend(build_sys_check.get("available", []))
    if lang == "nodejs":
        runtime_deps = result.get("runtime_deps") or {}
        available_items.extend(runtime_deps.get("available", []))
    return build_source_index(available_items)


def classify_preblocked_dependency(dep: dict[str, Any], lang: str) -> dict[str, Any]:
    """处理 analyze 阶段已确认版本冲突的依赖（社区源有但版本低）。

    COPR 场景下官方源版本不满足要求时，直接引入更高版本到 AiRepo，
    不需要 compat 包机制（COPR 仓库与官方源叠加，不存在覆盖冲突）。
    """
    found_version = dep.get("found_version", "")
    requirement = dep.get("requirement", "")

    # 社区版本比要求版本更新且同主版本时，直接 reuse（requirements.txt == 精确锁版的误判修正）
    import re as _re
    req_ver_m = _re.search(r"[\d][0-9A-Za-z.+_~\-]*", requirement or "")
    req_ver_only = req_ver_m.group(0) if req_ver_m else ""
    if found_version and req_ver_only:
        off_major = found_version.split(".")[0]
        req_major = req_ver_only.split(".")[0]
        try:
            _cmp = (list(map(int, found_version.split("."))) > list(map(int, req_ver_only.split("."))))
        except ValueError:
            _cmp = False
        if _cmp and off_major == req_major:
            official_info = {
                "exists": True,
                "highest": {"version": found_version},
                "satisfies_requirement": True,
                "meets_need": True,
                "comparison_unknown": False,
            }
            return {
                **dep,
                "source_check": {"status": "ok", "satisfies_requirement": True},
                "existing_check": {
                    "official": official_info,
                    "decision": "reuse_official",
                    "reason": f"社区源版本 {found_version} 与要求版本 {req_ver_only} 同主版本且更新，直接复用",
                },
                "decision": "reuse_official",
                "action": "resolved",
                "reason": f"社区源版本 {found_version} 与要求版本 {req_ver_only} 同主版本且更新，直接复用",
            }

    reason_base = (
        f"社区仓库已存在同名包，但最高版本 {found_version or '未知版本'} "
        f"不满足要求（{requirement or '无版本约束'}），引入更高版本到 COPR"
    )

    # ── 级联检查（L0-L4）───────────────────────────────────────────
    # 官方源版本不满足要求时，用完整级联查 L0（用户 COPR）/ L1（EUR）/
    # L3（gitcode 参考源）是否有更好的处置方案。
    cascade = _enrich_via_cascade(dep.get("name", ""), lang, requirement)
    if cascade:
        cascade_decision = cascade.get("decision", "")
        cascade_action, cascade_reason = _cascade_decision_to_action(
            cascade_decision, bool(dep.get("upstream_url"))
        )
        action = cascade_action or ("recurse" if dep.get("upstream_url") else "needs_ai")
        if cascade_action == "resolved":
            reason = reason_base + f"（{cascade_reason}）"
        else:
            reason = reason_base + f"（{cascade_reason}）" if cascade_reason else reason_base + "（官方源版本不满足要求，将引入更高版本到 COPR）"
    else:
        # 级联不可用（缺 COPR 凭据等），回退到原判定
        if dep.get("upstream_url"):
            action = "recurse"
            reason = reason_base + "（官方源版本不满足要求，将引入更高版本到 COPR）"
        else:
            action = "needs_ai"
            reason = reason_base + "（需 AI web search 补全 upstream URL 后引入）"

    official_info = {
        "exists": True,
        "highest": {"version": found_version} if found_version else None,
        "satisfies_requirement": False,
        "meets_need": False,
        "comparison_unknown": False,
    }
    existing_check = {
        "official": official_info,
        "decision": "block_official_older",
        "reason": reason_base,
    }

    return {
        **dep,
        "source_check": {"status": "older", "satisfies_requirement": False},
        "existing_check": existing_check,
        "decision": "block_official_older",
        "action": action,
        "reason": reason,
    }


def _enrich_via_cascade(pkgname: str, lang: str, requirement: str) -> dict | None:
    """级联检查（L0-L4）：对依赖做完整的 4 级级联查找。

    当 L2（check_existing_package）判定包不存在或版本不满足时调用，
    用 L0（用户 COPR）/ L1（EUR）/ L3（gitcode 参考源）补充信息。

    返回 cascade_result dict（含 decision/level/match 等字段）或 None（查询失败/不适用）。
    """
    copr_url = os.environ.get("COPR_FRONTEND_URL", "")
    copr_owner = os.environ.get("COPR_OWNER", "")
    copr_project = os.environ.get("COPR_PROJECT", "")
    copr_login = os.environ.get("COPR_API_LOGIN", "")
    copr_token = os.environ.get("COPR_API_TOKEN", "")
    if not (copr_url and copr_owner and copr_project and copr_login and copr_token):
        return None
    try:
        # version 留空：cascade 内部会从 requirement 推导下界版本（集中实现，
        # 与 evaluate-deps / run_gate 等其他调用方共用同一份防线）
        return CASCADE_CHECKER.check_package_existence(
            pkgname,
            lang=lang,
            version="",
            requirement=requirement,
            target=os.environ.get("COPR_CHROOT", ""),
            copr_url=copr_url,
            copr_owner=copr_owner,
            copr_project=copr_project,
            copr_login=copr_login,
            copr_token=copr_token,
        )
    except Exception:
        return None


def _cascade_decision_to_action(decision: str, has_upstream_url: bool) -> tuple[str, str]:
    """将级联 decision 映射为 pre_check (action, reason)。"""
    if decision in ("reuse_copr_project",):
        return ("resolved", "用户 COPR project 已有成功构建，直接复用")
    elif decision in ("reuse_eur_srpm",):
        # 依赖路径没有"下载 EUR SRPM 重建"的执行通道（那是主包 run_gate 的能力），
        # 判 resolved 会变成"假 resolved"：spec 照写 Requires 但没人把包建出来。
        # 统一走递归引入，以 EUR SRPM 为参考重建。
        return ("recurse", "EUR 已有匹配版本（chroot 一致），以 EUR SRPM 为参考重建到用户 project")
    elif decision in ("reuse_official",):
        return ("resolved", "openEuler 官方源版本满足要求，直接复用")
    elif decision in ("reuse_additional_repo",):
        return ("resolved", "项目 additional_repos（外挂源）已有满足要求的版本，直接复用")
    elif decision in ("evaluate",):
        return ("recurse", "openEuler 官方源版本不满足要求，需引入更高版本")
    elif decision in ("introduce_new_with_ref",):
        return ("recurse", "已有参考源（gitcode/EUR），以参考 spec 为起点构建")
    elif decision in ("introduce_new",):
        if has_upstream_url:
            return ("recurse", "所有来源均未找到，需全新引入")
        else:
            return ("needs_ai", "所有来源均未找到，需 AI 补全 upstream URL")
    return ("", "")


# 级联 decision → pre_check 内部 decision 映射
_CASCADE_TO_PRECHECK_DECISION = {
    "reuse_copr_project": "reuse_user_repo",
    "reuse_eur_srpm": "introduce_new",
    "reuse_official": "reuse_official",
    "reuse_additional_repo": "reuse_official",
    "evaluate": "block_official_older",
    "introduce_new_with_ref": "introduce_new",
    "introduce_new": "introduce_new",
}


def _build_existing_check_from_cascade(cascade: dict, dep: dict) -> dict:
    """从级联结果构建 pre_check 内部用的 existing_check 结构。

    级联 L0-L4 的 decision 空间比 L2-only 的 check_existing_package 更丰富，
    通过此函数映射为 pre_check 兼容的 decision + official_info。
    """
    cascade_decision = cascade.get("decision", "introduce_new")
    cascade_level = cascade.get("level", 4)
    cascade_match = cascade.get("match") or {}

    decision = _CASCADE_TO_PRECHECK_DECISION.get(cascade_decision, cascade_decision)

    # 从级联层级推断包在官方源中的存在性（参考源/全新引入显然不在官方源）
    official_exists = (
        cascade_level <= 2 or cascade_decision in ("evaluate", "reuse_additional_repo")
    ) and cascade_decision not in ("introduce_new_with_ref", "introduce_new")
    official_version = ""
    if cascade_level == 0:
        official_version = cascade_match.get("version", "")
    elif cascade_level == 1:
        official_version = cascade_match.get("version", "")
    elif cascade_level <= 2 or cascade_decision == "reuse_additional_repo":
        official_version = cascade_match.get("version", "") or cascade.get("version", "")

    # reuse_eur_srpm 不算 meets_need：依赖路径上它被映射为 introduce_new
    # 走递归重建，EUR 里的包并不能直接满足需求
    meets_need = cascade_decision in ("reuse_official", "reuse_copr_project",
                                      "reuse_additional_repo")

    return {
        "official": {
            "exists": official_exists,
            "highest": {"version": official_version} if official_version else None,
            "satisfies_requirement": meets_need,
            "meets_need": meets_need,
            "comparison_unknown": False,
        },
        "decision": decision,
        "reason": f"cascade L{cascade_level}: {cascade_decision}",
        "cascade": cascade,  # 保留原始级联结果供参考
    }


def classify_dependency(dep: dict[str, Any], lang: str, source_index: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    source_item = lookup_source_item(dep, source_index)
    source_check = summarize_source_match(dep, source_item)

    original_requirement_info = dep.get("requirement_info") if isinstance(dep.get("requirement_info"), dict) else None
    dep["constraint_type"], dep["requirement_info"] = classify_requirement_constraint(
        dep.get("constraint") or dep.get("requirement", ""),
        original_requirement_info,
    )

    debug_flow = {
        "name": dep.get("name") or dep.get("dep") or "",
        "before": {
            "constraint": dep.get("constraint") or dep.get("requirement", ""),
            "constraint_type": dep.get("constraint_type", "unknown"),
            "requirement_info": dep.get("requirement_info", {}),
        },
    }

    if source_check["satisfies_requirement"]:
        debug_flow["after"] = {
            "constraint_type": dep.get("constraint_type", "unknown"),
            "requirement_info": dep.get("requirement_info", {}),
            "decision": "reuse_source",
        }
        return {
            **dep,
            "source_check": source_check,
            "existing_check": None,
            "decision": "reuse_source",
            "action": "resolved",
            "reason": source_check["reason"],
            "debug_constraint_flow": debug_flow,
        }

    # ── 级联检查（L0-L4）：统一使用 cascade_package_check，与主包同一套判定逻辑 ──
    # 级联内置了 L2（dnf repoquery）+ L0（COPR）/ L1（EUR）/ L3（gitcode），
    # 不再单独调 check_existing_package。
    cascade = _enrich_via_cascade(dep.get("name", "") or dep.get("dep", ""), lang, dep.get("requirement", ""))
    if cascade:
        # 将级联结果映射为 pre_check 内部的 existing_check + decision 结构，
        # 保持后续 compat / build_system / constraint 等处理逻辑不变。
        existing_check = _build_existing_check_from_cascade(cascade, dep)
        decision = existing_check["decision"]
        # requirement_info 从 dep 自身获取（级联不提供此字段）
        if dep.get("requirement"):
            dep["constraint_type"], dep["requirement_info"] = classify_requirement_constraint(
                dep.get("constraint") or dep.get("requirement", ""),
                dep.get("requirement_info") if isinstance(dep.get("requirement_info"), dict) else None,
            )
    else:
        # 级联不可用（无 COPR 凭据等）→ 回退到 L2-only
        existing_check = EXISTING_CHECKER.check_existing_package(
            dep["name"],
            requirement=dep.get("requirement", ""),
            lang=lang,
        )
        requested = dict(existing_check.get("requested") or {})
        requested_requirement_info = requested.get("requirement_info")
        if isinstance(requested_requirement_info, dict):
            dep["requirement_info"] = requested_requirement_info
            dep["constraint_type"], dep["requirement_info"] = classify_requirement_constraint(
                dep.get("constraint") or dep.get("requirement", ""),
                requested_requirement_info,
            )
            dep["version_source"] = infer_version_source(dep, existing_check)
        elif dep.get("requirement"):
            dep["constraint_type"], dep["requirement_info"] = classify_requirement_constraint(
                dep.get("constraint") or dep.get("requirement", ""),
                dep.get("requirement_info") if isinstance(dep.get("requirement_info"), dict) else None,
            )
        decision = existing_check["decision"]
    if source_check["status"] == "older" and not existing_check.get("official", {}).get("exists"):
        existing_check = merge_official_source_older_result(dep, source_check, existing_check)

    # 构建系统工具白名单：官方源存在任意版本时强制 reuse_official，忽略版本约束。
    # 构建工具不是运行时依赖，spec 中 BuildRequires 不带版本即可。
    category = dep.get("category", "")
    dep_name = dep.get("name") or dep.get("dep") or ""
    if category == "build_system" and _is_build_system_tool(dep_name):
        if existing_check.get("official", {}).get("exists"):
            existing_check["decision"] = "reuse_official"
            existing_check["reason"] = (
                f"{dep_name} 是构建系统工具（白名单），"
                f"官方源已有版本，直接复用"
            )
        else:
            # 白名单内的构建工具但官方源不存在 → 降级为 needs_ai，不直接进构建队列
            existing_check["decision"] = "needs_ai"

    decision = existing_check["decision"]
    if decision == "needs_ai":
        action = "needs_ai"
        reason = existing_check.get("reason", "构建系统工具需 AI 判断")
    elif decision in {"reuse_official", "reuse_user_repo"}:
        action = "resolved"
        reason = existing_check["reason"]
    elif decision == "block_official_older":
        # 级联已确认版本不满足（L2 evaluate → block_official_older）。
        # cascade 结果在 existing_check["cascade"] 中，此处只需处理 compat 逻辑。
        conflict_mode = _dep_conflict_mode()
        _COMPAT_SUPPORTED_LANGS = {"c", "cpp", "java"}
        can_compat = (
            (conflict_mode == "compat" and lang in _COMPAT_SUPPORTED_LANGS)
            or conflict_mode == "force_compat"
        )
        if can_compat:
            found_ver = existing_check.get("official", {}).get("highest", {}).get("version", "") or ""
            major = extract_compat_major_version(found_ver)
            compat_srpm = get_compat_srpm_name(lang, dep.get("name", ""), major)
            compat_rpm = get_compat_rpm_pkg_name(lang, dep.get("name", ""), major)
            if dep.get("upstream_url"):
                action = "recurse"
                reason = existing_check["reason"] + f"（compat 模式：将以 compat 包名 {compat_rpm} 引入新版本）"
                dep = {**dep, "compat_introduce": True, "compat_srpm_name": compat_srpm, "compat_rpm_name": compat_rpm}
            else:
                action = "needs_ai"
                reason = existing_check["reason"] + f"（compat 模式：需 AI web search 补全 upstream URL 后以 {compat_rpm} 引入）"
                dep = {**dep, "compat_introduce": True, "compat_srpm_name": compat_srpm, "compat_rpm_name": compat_rpm}
        else:
            action = "blocked"
            if conflict_mode in ("compat", "force_compat") and lang not in _COMPAT_SUPPORTED_LANGS and conflict_mode != "force_compat":
                reason = existing_check["reason"] + f"（{lang} 包文件路径不含版本号，不支持 compat 共存）"
            else:
                reason = existing_check["reason"]
    else:
        # introduce_new / 未命中任何源：检查 upstream_url 决定 recurse 或 needs_ai
        if not dep.get("upstream_url"):
            action = "needs_ai"
            reason = "无法确定依赖上游源码仓库地址，需 AI web search 补全"
        else:
            action = "recurse"
            reason = existing_check["reason"]

    debug_flow["after"] = {
        "constraint_type": dep.get("constraint_type", "unknown"),
        "requirement_info": dep.get("requirement_info", {}),
        "decision": decision,
        "action": action,
    }

    return {
        **dep,
        "source_check": source_check,
        "existing_check": existing_check,
        "decision": decision,
        "action": action,
        "reason": reason,
        "debug_constraint_flow": debug_flow,
    }


def build_summary(pkgname: str, lang: str, source_dir: str, analysis_file: str, decisions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "pkgname": pkgname,
        "lang": lang,
        "source_dir": source_dir,
        "analysis_file": analysis_file,
        "dependency_decisions": decisions,
        "resolved": [item for item in decisions if item["action"] == "resolved"],
        "pending": [item for item in decisions if item["action"] == "recurse"],
        "needs_ai": [item for item in decisions if item["action"] == "needs_ai"],
        "blocked": [item for item in decisions if item["action"] == "blocked"],
    }


def print_pending_to_stdout(pending: list[dict[str, Any]]) -> None:
    seen: set[tuple[str, str]] = set()
    for item in pending:
        key = (item["name"], item.get("upstream_url", ""))
        if key in seen:
            continue
        seen.add(key)
        print(f"{item['name']} {item.get('upstream_url', '')}".rstrip())


# ── Rust MSRV / toolchain 预检 ────────────────────────────────────────────────
# 背景：Rust 项目常通过 Cargo.toml 的 rust-version 字段或 rust-toolchain.toml 的
# channel 字段声明编译器版本要求。这类冲突是结构性的（改 spec 无法解决，只能
# 换编译器版本或换包版本），若不提前检查，只能等 cargo build 失败后走一轮完整
# 的"失败诊断 → 判定 abort"循环才能确认，白白浪费一次编译+诊断的时间。
#
# 历史上 spec-rules-rust.md 文档里有一段用 `docker exec ${SESSION_CONTAINER}
# rustc --version` 查编译器版本的检查脚本，但当前架构已不再使用容器构建
# （pkg-builder.md 明确标注"COPR 模式下无 SESSION_CONTAINER"），这段检查从未
# 被接入过实际的预检流程。真正决定构建时用哪个 rustc 版本的，是 COPR 目标
# chroot 仓库里 rust 包的版本，不是本地环境的 rustc（本地 worker 容器里也
# 根本没装 rustc），所以这里改用 dnf repoquery 查 chroot 仓库里的 rust 包版本，
# 复用 check_existing_package.py 里已有的 repo 切换机制。
def _read_rust_toolchain_channel(source_dir: Path) -> str:
    """读取 rust-toolchain.toml 的 channel 字段，取不到返回空串。"""
    for fname in ("rust-toolchain.toml", "rust-toolchain"):
        f = source_dir / fname
        if not f.exists():
            continue
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        m = re.search(r'channel\s*=\s*["\']([^"\']+)["\']', content)
        if m:
            return m.group(1).strip()
        # rust-toolchain（无 .toml 后缀）历史格式：文件内容直接是版本号/channel 名
        if fname == "rust-toolchain" and content.strip():
            return content.strip().splitlines()[0].strip()
    return ""


def _read_cargo_rust_version(source_dir: Path) -> str:
    """读取 Cargo.toml 的 rust-version 字段（MSRV），取不到返回空串。"""
    f = source_dir / "Cargo.toml"
    if not f.exists():
        return ""
    try:
        content = f.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    m = re.search(r'^\s*rust-version\s*=\s*["\']([^"\']+)["\']', content, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _query_chroot_rustc_version(copr_chroot: str) -> str:
    """查 COPR 目标 chroot 仓库里 rust 包的版本，查不到返回空串。"""
    if not copr_chroot:
        return ""
    repo_switched = False
    try:
        repo_switched = EXISTING_CHECKER.setup_repo_for_chroot(copr_chroot)
        found = EXISTING_CHECKER._dnf_repoquery("rust", "rust")
        return (found or {}).get("version", "")
    except Exception:
        return ""
    finally:
        if repo_switched:
            EXISTING_CHECKER.teardown_repo()


def check_rust_toolchain(pkgname: str, source_dir: str) -> dict[str, Any] | None:
    """Rust MSRV / toolchain channel 预检。返回 None 表示无冲突或无法判断（放行）。

    返回非 None 时表示确认存在结构性冲突，调用方应直接判定 blocked，
    不进入 vendor/构建流程。
    """
    src = Path(source_dir)

    channel = _read_rust_toolchain_channel(src)
    if channel and channel.lower() in ("nightly", "beta"):
        return {
            "name": pkgname,
            "reason": f"rust-toolchain 要求 channel={channel!r}，标准 COPR chroot 只提供 stable 版 rustc，"
                       f"无法满足 nightly/beta 工具链要求",
        }

    msrv = _read_cargo_rust_version(src)
    # channel 为具体版本号（如 "1.92.0"）时，与 Cargo.toml 的 rust-version 是两条独立的
    # MSRV 声明路径，须分别与 chroot rustc 版本比较（参考 lessons/rust.json 中 servo 案例）。
    required_versions = [v for v in (msrv, channel) if v and re.match(r'^\d+\.\d+', v)]
    if not required_versions:
        return None  # 未声明版本要求，先尝试构建，失败再判断（既有行为不变）

    copr_chroot = os.environ.get("COPR_CHROOT", "")
    chroot_rustc = _query_chroot_rustc_version(copr_chroot)
    if not chroot_rustc:
        return None  # 查不到 chroot rustc 版本时保守放行，不误判

    for required in required_versions:
        try:
            from packaging.version import Version
            if Version(chroot_rustc) < Version(required):
                return {
                    "name": pkgname,
                    "reason": f"要求 rustc >= {required}，但目标 chroot（{copr_chroot}）"
                               f"提供的 rustc 版本为 {chroot_rustc}，无法通过修改 spec 解决",
                }
        except Exception:
            continue  # 版本号解析失败时跳过这一条，不误判

    return None


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="RPM 编译前依赖预检")
    parser.add_argument("pkgname", help="包名")
    parser.add_argument("lang", help="语言：python/go/rust/c/cpp/nodejs/java/ruby")
    parser.add_argument("source_dir", help="源码目录（绝对路径）")
    parser.add_argument("-o", "--output", default="", help="JSON 结果输出路径")
    args = parser.parse_args()

    lang = args.lang.lower()
    if lang not in ANALYZERS:
        print(f"[WARN] 不支持的语言 {lang}，跳过预检", file=sys.stderr)
        sys.exit(0)

    cfg = ANALYZERS[lang]
    script = SCRIPT_DIR / cfg["script"]
    if not script.exists():
        print(f"[WARN] 分析脚本不存在: {script}，跳过预检", file=sys.stderr)
        sys.exit(0)

    # ── 混合包副语言探测（ANALYZERS 分发之前，与主 lang 正交）────────────────
    secondary, secondary_manifests = detect_secondary_langs(lang, args.source_dir)
    if secondary:
        print(f"[pre_check] 检测到副语言 {secondary}，manifests: {secondary_manifests}", file=sys.stderr)
    # vendor_langs：本包参与 vendor 的语言（主语言或副语言中的 go/rust），
    # 替代原全局 vendor_mode 布尔——混合包只有副语言进 vendor_langs，
    # 主语言的依赖检查照常进行。
    vendor_langs = [lang] if lang in VENDOR_LANGS else []
    vendor_langs += [l for l in secondary if l in VENDOR_LANGS]

    def attach_hybrid_fields(summary: dict) -> dict:
        summary["secondary_langs"] = secondary
        summary["secondary_manifests"] = secondary_manifests
        summary["vendor_langs"] = vendor_langs
        return summary

    out_file = make_output_path(args.pkgname, args.output)
    analysis_path = make_analysis_path(out_file, args.pkgname)

    # ── Rust MSRV / toolchain 结构性冲突检查（vendor 早退之前，见函数注释）──────
    # 这类冲突无法通过修改 spec 解决，提前拦截可以省掉一整轮"编译失败→AI诊断"。
    # 混合包（如 pendulum：主 lang=python + rust/Cargo.toml）同样需要做此检查：
    # Cargo.toml 在子目录（rust-version 在此），rust-toolchain.toml 通常在仓库根，两处都查。
    rust_check_dirs: list[Path] = []
    if lang == "rust":
        rust_check_dirs.append(Path(args.source_dir))
    elif "rust" in secondary_manifests:
        rust_check_dirs.append(Path(args.source_dir))
        rust_check_dirs.append(Path(args.source_dir) / Path(secondary_manifests["rust"]).parent)
    for check_dir in rust_check_dirs:
        conflict = check_rust_toolchain(args.pkgname, str(check_dir))
        if conflict:
            output_path = Path(out_file)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            summary = attach_hybrid_fields(build_summary(args.pkgname, lang, args.source_dir, "", []))
            summary["blocked"] = [conflict]
            output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[BLOCK] {conflict['name']}: {conflict['reason']}", file=sys.stderr)
            sys.exit(1)

    # ── vendor 语言早退：主语言为 Go/Rust 时永远 vendor，跳过语言级依赖存在性检查 ──
    # 构建环境离线，这两种语言没有"不 vendor"的场景。
    # 系统库依赖（CGO、-sys crate）由 rpmbuild 循环兜底（报 missing header → 补 BuildRequires）。
    # 注意：主语言不是 vendor 语言的混合包（lang=python + Cargo.toml）不命中本分支，
    # 主语言依赖照查，副语言的 crate/module 由 vendor_langs 标记交给 builder vendor。
    if lang in VENDOR_LANGS:
        output_path = Path(out_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        summary = attach_hybrid_fields(build_summary(args.pkgname, lang, args.source_dir, "", []))
        summary["vendor_mode"] = True
        output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[pre_check] {lang} vendor 模式，跳过语言级依赖检查", file=sys.stderr)
        sys.exit(0)

    # ── Node.js vendor 阈值判断：依赖多时自动切换 vendor 模式 ────────────────────
    # 先做纯静态分析（不查 RPM 源），用 package.json dependencies 总数作为上界。
    # 大部分 npm 包不在 openEuler 社区源，依赖多意味着 missing 也多，vendor 更经济。
    NODEJS_VENDOR_THRESHOLD = 10
    if lang == "nodejs":
        static_cmd = [resolve_python_executable(), str(script), args.source_dir, "-o", str(analysis_path)]
        subprocess.run(static_cmd, capture_output=False)
        try:
            static_result = load_json(analysis_path)
        except Exception:
            static_result = {}

        # dependencies 总数是 missing 的保守上界（不需要查 RPM 源）
        deps_count = len(static_result.get("dependencies", {}))
        source_path = Path(args.source_dir)
        has_lockfile = (source_path / "package-lock.json").exists() or (source_path / "yarn.lock").exists()

        if deps_count > NODEJS_VENDOR_THRESHOLD:
            output_path = Path(out_file)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if not has_lockfile:
                print(f"[pre_check] nodejs: {deps_count} 个依赖 > 阈值 {NODEJS_VENDOR_THRESHOLD} 但无 lockfile，无法确定性 vendor", file=sys.stderr)
                summary = attach_hybrid_fields(build_summary(args.pkgname, lang, args.source_dir, str(analysis_path), []))
                summary["blocked"] = [{"name": args.pkgname, "reason": f"{deps_count} npm deps declared but no lockfile (package-lock.json/yarn.lock), cannot vendor deterministically"}]
                output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
                sys.exit(1)
            print(f"[pre_check] nodejs: {deps_count} 个依赖 > 阈值 {NODEJS_VENDOR_THRESHOLD}，切换 vendor 模式", file=sys.stderr)
            summary = attach_hybrid_fields(build_summary(args.pkgname, lang, args.source_dir, str(analysis_path), []))
            summary["vendor_mode"] = True
            summary["vendor_langs"] = ["nodejs"] + vendor_langs
            output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            sys.exit(0)
        # deps <= THRESHOLD → 走原有 --check-rpm 路径（查实际 missing 数量）
        print(f"[pre_check] nodejs: {deps_count} 个依赖 <= 阈值 {NODEJS_VENDOR_THRESHOLD}，走 RPM-native 路径", file=sys.stderr)

    cmd = [resolve_python_executable(), str(script), args.source_dir, "--check-rpm", "-o", str(analysis_path)]
    if lang == "python":
        cmd += ["--pkg", args.pkgname]
        copr_chroot = os.environ.get("COPR_CHROOT", "")
        if copr_chroot:
            cmd += ["--chroot", copr_chroot]

    print(f"[pre_check] 运行: {' '.join(cmd)}", file=sys.stderr)
    proc = subprocess.run(cmd, capture_output=False)
    if proc.returncode not in (0, 2):
        print(f"[ERROR] 依赖分析脚本执行失败，退出码: {proc.returncode}", file=sys.stderr)
        sys.exit(1)

    try:
        result = load_json(analysis_path)
    except Exception as e:
        print(f"[ERROR] 无法读取分析结果: {e}", file=sys.stderr)
        sys.exit(1)

    dependency_items, preblocked_items = dependency_items_from_result(lang, result, args.pkgname)
    source_index = build_available_index_for_result(lang, result)

    # preblocked_items: analyze 阶段已确认版本冲突，直接决策，不再调用 check_existing_package
    # 整个分类阶段用 try/except 保护：classify_dependency 内部会调 dnf repoquery
    # 和 COPR API，任一步失败都应有明确错误信息而非静默退出留下孤儿中间产物。
    try:
        preblocked_decisions = [classify_preblocked_dependency(dep, lang) for dep in preblocked_items]
        # 其余依赖走完整的 classify_dependency 流程
        decisions = preblocked_decisions + [classify_dependency(dep, lang, source_index) for dep in dependency_items]
        summary = build_summary(args.pkgname, lang, args.source_dir, str(analysis_path), decisions)
    except Exception as e:
        print(f"[ERROR] 依赖分类失败: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)

    # 缺口3：把 analyze 阶段查到的、在目标 chroot 源中确实存在的 C 扩展链接库
    # -devel 包，作为一个独立字段带进 summary，供 spec 生成时直接加入 BuildRequires。
    # 只带 available（已验证存在）的，不写未经验证的包名；查不到的交给构建失败循环。
    c_lib_check = result.get("c_library_rpm_check") or {}
    c_lib_brs = []
    seen_brs: set[str] = set()
    for item in c_lib_check.get("available", []):
        rpm = item.get("rpm", "")
        if rpm and rpm not in seen_brs:
            seen_brs.add(rpm)
            c_lib_brs.append(rpm)

    # ── 副语言 analyzer：提取混合包中 vendor 语言部分的系统 C 库依赖 + crate 清单 ──
    # 混合包（如 python + rust/Cargo.toml）同样可能链接 openssl 等系统库；
    # crate/module 清单供 supervisor 识别 vendor_only 依赖、供 builder 做 vendor。
    vendor_crates: dict[str, list[str]] = {}
    for sec_lang in secondary:
        sec_cfg = ANALYZERS.get(sec_lang)
        if not sec_cfg:
            continue
        sec_script = SCRIPT_DIR / sec_cfg["script"]
        if not sec_script.exists():
            continue
        manifest_rel = secondary_manifests.get(sec_lang, "")
        if not manifest_rel:
            continue
        # analyzer 在 manifest 所在目录工作（Cargo.toml / go.mod 的父目录）
        sec_dir = str(Path(args.source_dir) / Path(manifest_rel).parent)
        sec_out = analysis_path.with_name(f"{analysis_path.stem}_{sec_lang}{analysis_path.suffix}")
        sec_cmd = [resolve_python_executable(), str(sec_script), sec_dir, "--check-rpm", "-o", str(sec_out)]
        print(f"[pre_check] 副语言 {sec_lang} 分析: {' '.join(sec_cmd)}", file=sys.stderr)
        sec_proc = subprocess.run(sec_cmd, capture_output=False)
        if sec_proc.returncode not in (0, 2):
            print(f"[WARN] 副语言 {sec_lang} 分析失败（忽略，由构建失败循环兜底），退出码 {sec_proc.returncode}", file=sys.stderr)
            continue
        try:
            sec_result = load_json(sec_out)
        except Exception as e:
            print(f"[WARN] 无法读取副语言 {sec_lang} 分析结果: {e}", file=sys.stderr)
            continue
        # 系统 C 库（已验证存在）并入 c_library_build_requires；missing 的不阻断，
        # 与纯 rust 路径一致，由 rpmbuild 循环兜底。
        for item in (sec_result.get("rpm_check") or {}).get("available", []):
            rpm = item.get("rpm", "")
            if rpm and rpm not in seen_brs:
                seen_brs.add(rpm)
                c_lib_brs.append(rpm)
        # crate/module 依赖：vendor 解决，不进 pending、不写 BuildRequires
        crates = [c.get("name", "") for c in sec_result.get("crate_deps", []) if isinstance(c, dict) and c.get("name")]
        go_mod_info = sec_result.get("go_mod") or {}
        crates += [m.get("name", "") for m in go_mod_info.get("module_deps", []) if isinstance(m, dict) and m.get("name")]
        if crates:
            vendor_crates[sec_lang] = sorted(set(crates))
            print(f"[pre_check] 副语言 {sec_lang} crate/module 依赖（vendor 解决）: {vendor_crates[sec_lang]}", file=sys.stderr)

    attach_hybrid_fields(summary)
    if vendor_crates:
        summary["vendor_crates"] = vendor_crates
    summary["c_library_build_requires"] = c_lib_brs
    if c_lib_brs:
        print(f"[pre_check] C 扩展链接库 BuildRequires: {c_lib_brs}", file=sys.stderr)

    output_path = Path(out_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    blocked = summary["blocked"]
    pending = summary["pending"]
    resolved = summary["resolved"]

    print(f"[pre_check] 已解决 {len(resolved)} 个依赖，待递归 {len(pending)} 个，阻断 {len(blocked)} 个", file=sys.stderr)

    if blocked:
        for item in blocked:
            print(f"[BLOCK] {item['name']}: {item['reason']}", file=sys.stderr)
        sys.exit(1)

    if not pending:
        print("[pre_check] 所有依赖均已满足或可复用", file=sys.stderr)
        sys.exit(0)

    print(f"[pre_check] 发现 {len(pending)} 个需递归处理的依赖：", file=sys.stderr)
    for item in pending:
        print(f"  - {item['name']}  {item.get('upstream_url', '')}  [{item['decision']}]", file=sys.stderr)
    print_pending_to_stdout(pending)
    sys.exit(2)


if __name__ == "__main__":
    main()
