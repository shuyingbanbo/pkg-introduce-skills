#!/usr/bin/env python3
"""Shared one-shot RPM availability lookup via a single container-side DNF sack load."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

_INTERNAL_KEYS = {"queries", "prefer_devel", "enabled_repos", "repofrompath"}

# chroot 名称前缀 → openEuler 官方源 base URL
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


def chroot_to_repofrompath(chroot: str) -> list[tuple[str, str]]:
    """根据 chroot 名称（如 openeuler-22.03_LTS_SP2-x86_64）返回 repofrompath 列表。
    每个元素为 (repo_id, url) 元组，传给 dnf base.repos.add_new_repo。
    arch 从 chroot 名称末尾提取（x86_64 / aarch64）。
    """
    arch = chroot.rsplit("-", 1)[-1] if "-" in chroot else "x86_64"
    for prefix, base_url in _CHROOT_REPO_MAP.items():
        if chroot.startswith(prefix):
            # repo_id 带 chroot 后缀：dnf 缓存按 <repoid>-<hash> 建目录，
            # warm_repo_cache / cacheonly 判断按 repoid 前缀 glob——
            # id 不含 chroot 时多 chroot 会互相误判"已缓存"
            return [
                (f"oe-official-{chroot}", f"{base_url}/everything/{arch}/"),
                (f"oe-update-{chroot}",   f"{base_url}/update/{arch}/"),
                (f"oe-epol-{chroot}",     f"{base_url}/EPOL/main/{arch}/"),
            ]
    return []

# 宿主机缓存文件路径，优先用 SESSION_TMP_DIR 环境变量，否则用 /tmp
_CACHE_DIR = Path(os.environ.get("SESSION_TMP_DIR", "/tmp")) / "rpm_lookup_cache"
_CACHE_FILE = _CACHE_DIR / "batch_lookup_cache.json"

def _load_cache() -> dict:
    try:
        if _CACHE_FILE.exists():
            return json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}

def _save_cache(cache: dict) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

def _task_key(task: Dict[str, Any], enabled_repos: Optional[List[str]]) -> str:
    payload = {k: v for k, v in task.items() if k not in _INTERNAL_KEYS}
    payload["_repos"] = sorted(enabled_repos) if enabled_repos else []
    return hashlib.md5(json.dumps(payload, sort_keys=True).encode()).hexdigest()

_CONTAINER_SCRIPT = r'''
import json
import sys
import warnings

warnings.filterwarnings("ignore")

try:
    import dnf
except ImportError:
    print(json.dumps({"error": "dnf not available"}))
    sys.exit(0)

INTERNAL_KEYS = {"queries", "prefer_devel", "enabled_repos", "repofrompath"}


def unique_packages(packages):
    seen = set()
    result = []
    for pkg in packages:
        name = getattr(pkg, "name", None)
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(pkg)
    return result


def first_package(sack, name):
    packages = unique_packages(sack.query().filter(name=name))
    return packages[0] if packages else None


def resolve_devel_candidate(sack, pkg):
    name = getattr(pkg, "name", "")
    if not name:
        return None
    if name.endswith("-devel"):
        return pkg
    return first_package(sack, f"{name}-devel")


def pick_package(sack, packages, prefer_devel):
    packages = unique_packages(packages)
    if not packages:
        return None
    if prefer_devel:
        for pkg in packages:
            name = getattr(pkg, "name", "")
            if name.endswith("-devel"):
                return pkg
        for pkg in packages:
            candidate = resolve_devel_candidate(sack, pkg)
            if candidate is not None:
                return candidate
    return packages[0]


def query_packages(sack, query):
    kind = query["kind"]
    value = query["value"]
    if kind == "provides":
        return sack.query().filter(provides=value)
    if kind == "name":
        return sack.query().filter(name=value)
    if kind == "name_glob":
        return sack.query().filter(name__glob=value)
    if kind == "file":
        return sack.query().filter(file=value)
    if kind == "file_glob":
        return sack.query().filter(file__glob=value)
    raise ValueError(f"unsupported query kind: {kind}")


def sanitize_task(task):
    result = {k: v for k, v in task.items() if k not in INTERNAL_KEYS}
    result["rpm"] = None
    result["version"] = None
    result["release"] = None
    result["level"] = ""
    return result


tasks = json.load(sys.stdin)
enabled_repos = tasks[0].get("enabled_repos") if tasks else None
repofrompath = tasks[0].get("repofrompath") if tasks else None  # list of [repo_id, url]
base = dnf.Base()
base.conf.cachedir = "/var/cache/dnf"
# 有缓存时用 cacheonly 加速，无缓存时允许下载
import os as _os, pathlib as _pl
_cache_exists = any(
    _pl.Path("/var/cache/dnf").glob("*/repomd.xml")
) if _pl.Path("/var/cache/dnf").exists() else False
base.conf.cacheonly = _cache_exists
base.read_all_repos()
if repofrompath:
    # 使用目标 chroot 源：禁用所有本地 repo，只用传入的临时 repo
    for repo in base.repos.iter_enabled():
        repo.disable()
    repo_ids = []
    for repo_id, url in repofrompath:
        base.repos.add_new_repo(repo_id, base.conf, baseurl=[url])
        repo_ids.append(repo_id)
    # 只有在该 chroot 的 repodata 尚未缓存时才强制下载；有缓存则复用
    _chroot_cached = all(
        any(_pl.Path("/var/cache/dnf").glob(f"{rid}-*/repodata/repomd.xml"))
        for rid in repo_ids
    )
    if not _chroot_cached:
        base.conf.cacheonly = False  # 首次需要下载 repodata
elif enabled_repos is not None:
    allowed = set(enabled_repos)
    for repo in base.repos.iter_enabled():
        repo.disable()
    for repo in base.repos.all():
        if repo.id in allowed:
            repo.enable()
base.fill_sack(load_system_repo=False, load_available_repos=True)
sack = base.sack

results = []
for task in tasks:
    result = sanitize_task(task)
    for query in task.get("queries", []):
        packages = query_packages(sack, query)
        pkg = pick_package(sack, packages, query.get("prefer_devel", task.get("prefer_devel", False)))
        if pkg is not None:
            result["rpm"] = getattr(pkg, "name", None)
            result["version"] = getattr(pkg, "version", None)
            result["release"] = getattr(pkg, "release", None)
            result["level"] = query.get("level", query["kind"])
            break
    results.append(result)

print(json.dumps(results))
'''


class BatchLookupError(RuntimeError):
    """Raised when the shared RPM batch lookup cannot complete."""


def provides_query(value: str, level: str) -> Dict[str, Any]:
    return {"kind": "provides", "value": value, "level": level}


def name_query(value: str, level: str = "name", prefer_devel: bool = False) -> Dict[str, Any]:
    return {"kind": "name", "value": value, "level": level, "prefer_devel": prefer_devel}


def name_glob_query(value: str, level: str = "name-glob", prefer_devel: bool = False) -> Dict[str, Any]:
    return {"kind": "name_glob", "value": value, "level": level, "prefer_devel": prefer_devel}


def file_query(value: str, level: str = "file", prefer_devel: bool = False) -> Dict[str, Any]:
    return {"kind": "file", "value": value, "level": level, "prefer_devel": prefer_devel}


def file_glob_query(value: str, level: str = "file-glob", prefer_devel: bool = False) -> Dict[str, Any]:
    return {"kind": "file_glob", "value": value, "level": level, "prefer_devel": prefer_devel}


def fallback_results(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            **{k: v for k, v in task.items() if k not in _INTERNAL_KEYS},
            "rpm": None,
            "version": None,
            "release": None,
            "level": "",
        }
        for task in tasks
    ]


def run_batch_lookup(
    tasks: List[Dict[str, Any]],
    timeout: int = 120,
    enabled_repos: Optional[List[str]] = None,
    chroot: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if not tasks:
        return []

    # 如果指定了 chroot，解析出对应的 repofrompath
    repofrompath: Optional[List[List[str]]] = None
    if chroot:
        rfp = chroot_to_repofrompath(chroot)
        if rfp:
            repofrompath = [[rid, url] for rid, url in rfp]

    # 宿主机缓存：命中的直接返回，未命中的发往容器查询
    # 注意：chroot 不同时缓存键也不同，避免跨 chroot 缓存污染
    effective_repos = repofrompath[0] if repofrompath else enabled_repos
    cache = _load_cache()
    results: List[Any] = [None] * len(tasks)
    miss_indices: List[int] = []
    miss_tasks: List[Dict[str, Any]] = []

    for i, task in enumerate(tasks):
        key = _task_key(task, [f"chroot:{chroot}"] if chroot else (enabled_repos or []))
        if key in cache:
            results[i] = cache[key]
        else:
            miss_indices.append(i)
            miss_tasks.append(task)

    if miss_tasks:
        payload_tasks = []
        for task in miss_tasks:
            payload_task = dict(task)
            if repofrompath is not None:
                payload_task["repofrompath"] = repofrompath
            elif enabled_repos is not None:
                payload_task["enabled_repos"] = enabled_repos
            payload_tasks.append(payload_task)

        proc = subprocess.run(
            ["python3", "-c", _CONTAINER_SCRIPT],
            input=json.dumps(payload_tasks),
            capture_output=True,
            text=True,
            timeout=300,
        )

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[:400]
            raise BatchLookupError(detail or f"local dnf lookup failed with code {proc.returncode}")

        raw = proc.stdout.strip()
        if not raw:
            raise BatchLookupError((proc.stderr or "no output").strip()[:400])

        try:
            fresh = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BatchLookupError(f"JSON parse error: {exc}") from exc

        # 写回缓存并填充结果
        for idx, result in zip(miss_indices, fresh):
            key = _task_key(tasks[idx], [f"chroot:{chroot}"] if chroot else (enabled_repos or []))
            cache[key] = result
            results[idx] = result
        _save_cache(cache)

    return results  # type: ignore[return-value]
