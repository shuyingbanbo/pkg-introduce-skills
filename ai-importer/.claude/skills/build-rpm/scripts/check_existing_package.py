#!/usr/bin/env python3
"""检查目标包在 openEuler 官方源和 COPR project 中的复用/引入决策。

COPR 模式（无 Docker）：
  - 官方源：dnf repoquery 直接在本地执行（worker pod = openEuler 24.03）
  - COPR project 源：通过 COPR API 查询
  - 决策简化为两种：reuse_official | introduce_new
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request
import urllib.parse
import urllib.error
import base64
from pathlib import Path
from typing import Any, Optional

# 引入构建工具链名单与判断函数
from chroot_toolchain import is_toolchain

SKILLS_DIR = Path(__file__).resolve().parents[2]

OFFICIAL_REPO_LABEL = "<openeuler-official>"
COPR_PROJECT_LABEL  = "<copr-project>"

KNOWN_PREFIXES = ("python3_", "python_", "ros_humble_", "lib_")

# chroot name → openEuler repo base URL mapping
_CHROOT_REPO_MAP = {
    "openeuler-22.03_LTS-":       "http://repo.openeuler.org/openEuler-22.03-LTS",
    "openeuler-22.03_LTS_SP1-":   "http://repo.openeuler.org/openEuler-22.03-LTS-SP1",
    "openeuler-22.03_LTS_SP2-":   "http://repo.openeuler.org/openEuler-22.03-LTS-SP2",
    "openeuler-22.03_LTS_SP3-":   "http://repo.openeuler.org/openEuler-22.03-LTS-SP3",
    "openeuler-22.03_LTS_SP4-":   "http://repo.openeuler.org/openEuler-22.03-LTS-SP4",
    "openeuler-24.03_LTS-":       "http://repo.openeuler.org/openEuler-24.03-LTS",
    "openeuler-24.03_LTS_SP1-":   "http://repo.openeuler.org/openEuler-24.03-LTS-SP1",
    "openeuler-24.03_LTS_SP2-":   "http://repo.openeuler.org/openEuler-24.03-LTS-SP2",
    "openeuler-24.03_LTS_SP3-":   "http://repo.openeuler.org/openEuler-24.03-LTS-SP3",
    "openeuler-24.03_LTS_SP4-":   "http://repo.openeuler.org/openEuler-24.03-LTS-SP4",
}

_ACTIVE_REPO_FILE: Optional[Path] = None

SIMPLE_REQUIREMENT_RE = re.compile(
    r"^(?:[^<>=!]*\)?\s*)?(>=|==|<=|>|<)\s*([0-9A-Za-z.+:_~\-]+)$"
)
COMPOUND_REQUIREMENT_SPLIT_RE = re.compile(r"\s*(?:,|\bwith\b|\band\b)\s*", re.IGNORECASE)


# ── 版本比较（与原版完全一致）────────────────────────────────────────────────

def normalize_name_token(value: str) -> str:
    return re.sub(r"[-_.]+", "_", value.lower())


def split_version_tokens(version: str) -> list:
    tokens = []
    for part in re.split(r"[^A-Za-z0-9]+", version):
        if not part:
            continue
        for token in re.findall(r"[A-Za-z]+|\d+", part):
            tokens.append(int(token) if token.isdigit() else token.lower())
    return tokens


def compare_versions(left: str, right: str) -> int:
    # 预发布标记：1.0rc1 < 1.0（PEP 440 语义）；token 已 lowercase
    _PRE = ("a", "alpha", "b", "beta", "rc", "pre", "preview", "dev")

    def _cmp_token(l, r):
        if l is None and r is None: return 0
        if l is None:
            if isinstance(r, int): return 0 if r == 0 else -1
            return 1 if r in _PRE else -1
        if r is None:
            if isinstance(l, int): return 0 if l == 0 else 1
            return -1 if l in _PRE else 1
        if isinstance(l, int) and isinstance(r, int): return (l > r) - (l < r)
        if isinstance(l, int): return 1
        if isinstance(r, int): return -1
        return (l > r) - (l < r)

    lt = split_version_tokens(left)
    rt = split_version_tokens(right)
    for i in range(max(len(lt), len(rt))):
        lv = lt[i] if i < len(lt) else None
        rv = rt[i] if i < len(rt) else None
        c = _cmp_token(lv, rv)
        if c != 0:
            return c
    return 0


def parse_requirement(requirement: str) -> dict:
    raw = (requirement or "").strip()
    if not raw:
        return {"raw": "", "status": "none", "operator": None, "version": None, "clauses": []}
    lowered = raw.lower()
    if " or " in lowered or "!=" in raw or "~=" in raw:
        return {"raw": raw, "status": "unknown", "operator": None, "version": None, "clauses": []}
    clauses = []
    for part in COMPOUND_REQUIREMENT_SPLIT_RE.split(raw.strip().strip("()")):
        clause_text = part.strip().strip("()")
        if not clause_text:
            continue
        match = SIMPLE_REQUIREMENT_RE.match(clause_text)
        if not match:
            return {"raw": raw, "status": "unknown", "operator": None, "version": None, "clauses": []}
        clauses.append({"raw": clause_text, "operator": match.group(1), "version": match.group(2)})
    if not clauses:
        return {"raw": raw, "status": "unknown", "operator": None, "version": None, "clauses": []}
    first = clauses[0] if len(clauses) == 1 else {"operator": None, "version": None}
    return {"raw": raw, "status": "parsed", "operator": first["operator"],
            "version": first["version"], "clauses": clauses}


def evaluate_constraint(version: Optional[str], operator: Optional[str], expected: Optional[str]) -> Optional[bool]:
    if not version or not operator or not expected:
        return None
    cmp = compare_versions(version, expected)
    return {">=": cmp >= 0, "==": cmp == 0, "<=": cmp <= 0, ">": cmp > 0, "<": cmp < 0}.get(operator)


def evaluate_requirement(version: Optional[str], req_info: dict) -> Optional[bool]:
    if not version or req_info.get("status") != "parsed":
        return None
    clauses = req_info.get("clauses") or []
    if not clauses and req_info.get("operator") and req_info.get("version"):
        clauses = [{"operator": req_info["operator"], "version": req_info["version"]}]
    if not clauses:
        return None
    results = [evaluate_constraint(version, c.get("operator"), c.get("version")) for c in clauses]
    if any(r is None for r in results):
        return None
    return all(bool(r) for r in results)


# ── 包名候选生成 ──────────────────────────────────────────────────────────────

def build_name_candidates(pkgname: str, lang: str = "") -> set:
    def _stems(name):
        normalized = normalize_name_token(name)
        stems = {normalized}
        changed = True
        while changed:
            changed = False
            for stem in list(stems):
                for prefix in KNOWN_PREFIXES:
                    if stem.startswith(prefix):
                        c = stem[len(prefix):]
                        if c and c not in stems:
                            stems.add(c); changed = True
                if stem.startswith("lib") and len(stem) > 3:
                    c = stem[3:]
                    if c and c not in stems:
                        stems.add(c); changed = True
        return {s for s in stems if s}

    stems = _stems(pkgname)
    candidates = set()
    for stem in stems:
        dash = stem.replace("_", "-")
        under = dash.replace("-", "_")
        for v in {stem, dash, under}:
            if not v:
                continue
            candidates.update([v, f"python3-{v}", f"python3_{v}",
                                f"ros-humble-{v}", f"ros_humble_{v}",
                                f"lib{v}", f"lib-{v}", f"lib_{v}",
                                f"{v}-devel", f"{v}_devel"])
    return {c for c in candidates if c}


# ── repo 动态切换 ─────────────────────────────────────────────────────────────

def _chroot_to_repo_base(chroot: str) -> Optional[str]:
    """从 chroot 名（如 openeuler-22.03_LTS_SP2-x86_64）映射到 repo base URL。"""
    for prefix, base in _CHROOT_REPO_MAP.items():
        if chroot.startswith(prefix):
            return base
    return None


def setup_repo_for_chroot(chroot: str, copr_url: str = "", owner: str = "",
                          project: str = "") -> bool:
    """
    根据 chroot 名字写入对应 openEuler 版本的 repo 文件到 /etc/yum.repos.d/。
    若提供 copr_url/owner/project，同时写入 COPR project repo。
    返回是否需要（True=写了新 repo，False=chroot 已是本地版本，无需切换）。
    """
    global _ACTIVE_REPO_FILE

    base = _chroot_to_repo_base(chroot)
    if not base:
        return False

    # 判断 chroot 架构
    arch = "x86_64"
    if chroot.endswith("-aarch64"):
        arch = "aarch64"

    copr_section = ""
    if copr_url and owner and project:
        # RPM 产物在 backend:5002，不是 frontend
        backend_url = copr_url.rstrip('/').replace(':31211', ':5002').replace('copr-frontend:5000', 'copr-backend:5002')
        # 若 copr_url 是集群外地址（含 IP），替换为集群内 backend 地址
        import re as _re
        if _re.search(r'\d+\.\d+\.\d+\.\d+', backend_url):
            backend_url = "http://copr-backend:5002"
        copr_section = f"""
[oe-check-copr]
name=COPR {owner}/{project} ({chroot})
baseurl={backend_url}/results/{owner}/{project}/{chroot}/
enabled=1
gpgcheck=0
skip_if_unavailable=1
"""

    repo_content = f"""[oe-check-official]
name=openEuler check repo ({chroot})
baseurl={base}/everything/{arch}/
enabled=1
gpgcheck=0

[oe-check-update]
name=openEuler check update ({chroot})
baseurl={base}/update/{arch}/
enabled=1
gpgcheck=0

[oe-check-epol]
name=openEuler check EPOL ({chroot})
baseurl={base}/EPOL/main/{arch}/
enabled=1
gpgcheck=0
{copr_section}"""
    repo_file = Path("/etc/yum.repos.d/oe-check-tmp.repo")
    try:
        repo_file.write_text(repo_content, encoding="utf-8")
        # 只刷新 COPR 源缓存：官方源大且引包过程中不变，保留缓存；COPR 小且频繁更新，每次必须刷
        if copr_section:
            # 直接删除 oe-check-copr 的 cache 目录，绕过 DNF lock（避免与 warm_repo_cache 后台线程争锁超时）
            import shutil as _shutil
            for _d in Path("/var/cache/dnf").glob("oe-check-copr-*"):
                _shutil.rmtree(_d, ignore_errors=True)
        _ACTIVE_REPO_FILE = repo_file
        return True
    except PermissionError:
        # 没有写权限，跳过（不影响查询，只是用本地 repo）
        return False


def teardown_repo():
    """删除临时 repo 文件。"""
    global _ACTIVE_REPO_FILE
    if _ACTIVE_REPO_FILE and _ACTIVE_REPO_FILE.exists():
        try:
            _ACTIVE_REPO_FILE.unlink()
        except Exception:
            pass
        _ACTIVE_REPO_FILE = None


# ── 官方源查询（dnf repoquery 本地执行）──────────────────────────────────────

def _python_query_stem(pkgname: str) -> str:
    """剥离可能已有的 python3-/python- 前缀，避免二次拼接（如 python3-python3-xxx）。"""
    stem = pkgname
    for prefix in ("python3-", "python-"):
        if stem.startswith(prefix):
            stem = stem[len(prefix):]
            break
    return stem


def _dnf_repoquery(pkgname: str, lang: str) -> Optional[dict]:
    """
    在本地 dnf 中查找包。如果设置了临时 repo（_ACTIVE_REPO_FILE），
    则限制查询到该 repo，否则查全部。
    """
    candidates = build_name_candidates(pkgname, lang)
    query_args = []
    lang_lower = (lang or "").lower()

    # 如果有临时 repo，只查临时 repo
    repo_args = []
    if _ACTIVE_REPO_FILE and _ACTIVE_REPO_FILE.exists():
        repo_args = [
            "--disablerepo=*",
            "--enablerepo=oe-check-official",
            "--enablerepo=oe-check-update",
            "--enablerepo=oe-check-epol",
        ]
        # 读取 repo 文件判断是否有 COPR repo section
        try:
            if "[oe-check-copr]" in _ACTIVE_REPO_FILE.read_text():
                repo_args.append("--enablerepo=oe-check-copr")
        except Exception:
            pass

    if lang_lower == "python":
        stem = _python_query_stem(pkgname)
        query_args.append(f"python3dist({stem.lower()})")
        query_args.append(f"python3-{stem}")
        query_args.append(f"python-{stem}")
    elif lang_lower == "nodejs":
        query_args.append(f"npm({pkgname})")
        query_args.append(f"nodejs-{pkgname}")
    elif lang_lower == "java":
        query_args.append(f"mvn({pkgname})")
    else:
        query_args.extend(sorted(candidates)[:8])

    fmt = "%{NAME}\\t%{VERSION}"
    for query in query_args:
        try:
            ret = subprocess.run(
                ["dnf", "repoquery", "--quiet", "--queryformat", fmt]
                + repo_args + [query],
                capture_output=True, text=True, timeout=60,            )
            for line in ret.stdout.splitlines():
                parts = line.strip().split("\t")
                if len(parts) >= 2 and parts[0]:
                    return {"name": parts[0], "version": parts[1]}
        except Exception:
            pass
    return None


def _dnf_repoquery_copr(pkgname: str, lang: str) -> Optional[dict]:
    """专门查 oe-check-copr repo（COPR project 软件源）。"""
    if not (_ACTIVE_REPO_FILE and _ACTIVE_REPO_FILE.exists()):
        return None
    try:
        if "[oe-check-copr]" not in _ACTIVE_REPO_FILE.read_text():
            return None
    except Exception:
        return None

    candidates = build_name_candidates(pkgname, lang)
    lang_lower = (lang or "").lower()
    query_args = []
    if lang_lower == "python":
        stem = _python_query_stem(pkgname)
        query_args = [f"python3-{stem}", f"python3dist({stem.lower()})"]
    elif lang_lower == "nodejs":
        query_args = [f"nodejs-{pkgname}", f"npm({pkgname})"]
    else:
        query_args = sorted(candidates)[:8]

    repo_args = ["--disablerepo=*", "--enablerepo=oe-check-copr"]
    fmt = "%{NAME}\\t%{VERSION}"
    for query in query_args:
        try:
            ret = subprocess.run(
                ["dnf", "repoquery", "--quiet", "--queryformat", fmt]
                + repo_args + [query],
                capture_output=True, text=True, timeout=60,            )
            for line in ret.stdout.splitlines():
                parts = line.strip().split("\t")
                if len(parts) >= 2 and parts[0]:
                    return {"name": parts[0], "version": parts[1]}
        except Exception:
            pass
    return None


def summarize_official_repo(pkgname: str, lang: str, requested_version: str,
                             requirement: str) -> dict:
    found = _dnf_repoquery(pkgname, lang)
    req_info = parse_requirement(requirement)

    if not found:
        return {"exists": False, "highest": None, "meets_need": False,
                "matched_paths": [], "comparison_unknown": False}

    version = found.get("version", "")
    highest = {"path": OFFICIAL_REPO_LABEL, "match_type": "dnf_repo",
               "name": found["name"], "version": version}

    sat_req  = evaluate_requirement(version, req_info)
    sat_ver  = evaluate_constraint(version, ">=", requested_version) if requested_version else None
    unknown  = req_info["status"] == "unknown"

    if unknown:
        meets_need = False
    elif not requested_version and req_info["status"] == "none":
        meets_need = True  # 包存在且无版本约束
    else:
        comparisons = [v for v in (sat_ver, sat_req) if v is not None]
        meets_need = bool(comparisons) and all(comparisons)

    return {
        "exists": True,
        "highest": highest,
        "meets_need": meets_need,
        "satisfies_requested_version": sat_ver,
        "satisfies_requirement": sat_req,
        "matched_paths": [OFFICIAL_REPO_LABEL],
        "comparison_unknown": unknown,
    }


# ── COPR project 查询 ─────────────────────────────────────────────────────────

def _copr_query_package(pkgname: str, copr_url: str, owner: str, project: str,
                        login: str, token: str) -> Optional[dict]:
    """查询 COPR project 中是否有此包，返回最新成功构建的 {name, version} 或 None。

    使用 /api_3/build/list 接口（按包名过滤 succeeded 构建），比 /api_3/packages/ 更可靠：
    - /api_3/packages/ 在当前 COPR 版本不返回数据
    - /api_3/package 的 latest_succeeded 在有更新构建时会被置 null
    """
    creds = base64.b64encode(f"{login}:{token}".encode()).decode()
    headers = {"Authorization": f"Basic {creds}"}

    params = urllib.parse.urlencode({
        "ownername":   owner,
        "projectname": project,
        "packagename": pkgname,
        "limit":       "10",
    })
    url = f"{copr_url.rstrip('/')}/api_3/build/list?{params}"

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        items = data.get("items", [])
        if not items:
            return None
        # 取所有 succeeded 构建中版本最高的
        best = None
        for build in items:
            if build.get("state") != "succeeded":
                continue
            version = build.get("source_package", {}).get("version", "")
            name = build.get("source_package", {}).get("name", pkgname)
            if version:
                if best is None or compare_versions(version, best["version"]) > 0:
                    best = {"name": name, "version": version}
        return best
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        return None
    except Exception:
        return None


def summarize_copr_project(pkgname: str, lang: str, requested_version: str,
                           requirement: str, copr_url: str = "", owner: str = "",
                           project: str = "", login: str = "", token: str = "") -> dict:
    """查询 COPR project 中是否有此包。

    优先通过 dnf repoquery 查询（需要 setup_repo_for_chroot 已写入 oe-check-copr）。
    若 dnf 路径失败，回退到 COPR API（/api_3/build/list）。
    """
    found = _dnf_repoquery_copr(pkgname, lang)
    req_info = parse_requirement(requirement)

    if not found:
        return {"exists": False, "highest": None, "meets_need": False,
                "matched_paths": [], "comparison_unknown": False}

    version = found.get("version", "")
    highest = {"path": COPR_PROJECT_LABEL, "match_type": "dnf_repo",
               "name": found["name"], "version": version}

    sat_req = evaluate_requirement(version, req_info)
    sat_ver = evaluate_constraint(version, ">=", requested_version) if requested_version else None
    unknown = req_info["status"] == "unknown"

    if unknown:
        meets_need = False
    elif not requested_version and req_info["status"] == "none":
        meets_need = True
    else:
        comparisons = [v for v in (sat_ver, sat_req) if v is not None]
        meets_need = bool(comparisons) and all(comparisons)

    return {
        "exists": True,
        "highest": highest,
        "meets_need": meets_need,
        "satisfies_requested_version": sat_ver,
        "satisfies_requirement": sat_req,
        "matched_paths": [COPR_PROJECT_LABEL],
        "comparison_unknown": unknown,
    }


# ── 决策逻辑 ──────────────────────────────────────────────────────────────────

def choose_decision(official: dict, copr: dict, requested_version: str,
                    requirement: str) -> str:
    # 构建工具链：只要官方源存在任意版本，就复用（禁止引入/升级构建工具）
    if official.get("exists"):
        rpm_name = (official.get("highest") or {}).get("name", "")
        if rpm_name and is_toolchain(rpm_name):
            return "reuse_official"

    # 官方源满足 → 直接复用
    if official["meets_need"]:
        return "reuse_official"

    # 官方源版本存在但更新（同主版本）→ 也复用
    if official["exists"] and not official.get("comparison_unknown"):
        off_ver = (official.get("highest") or {}).get("version", "")
        req_ver = requested_version or re.search(
            r"[\d][0-9A-Za-z.+_~\-]*", requirement or ""
        )
        req_ver = req_ver.group(0) if hasattr(req_ver, "group") else (req_ver or "")
        if off_ver and req_ver and compare_versions(off_ver, req_ver) > 0:
            if off_ver.split(".")[0] == req_ver.split(".")[0]:
                return "reuse_official"

    # COPR project 已有满足版本 → 复用（避免重复构建）
    if copr["meets_need"]:
        return "reuse_copr_project"

    # 其他情况：引入
    return "introduce_new"


def build_reason(decision: str, official: dict, copr: dict,
                 requested_version: str, requirement: str) -> str:
    requested_desc = requirement or requested_version or "无版本约束"
    if decision == "reuse_official":
        v = (official.get("highest") or {}).get("version") or "已存在"
        return f"官方源已有满足要求（{requested_desc}）的版本：{v}"
    if decision == "reuse_copr_project":
        v = (copr.get("highest") or {}).get("version") or "已存在"
        return f"COPR project 已有满足要求（{requested_desc}）的版本：{v}"
    return f"官方源和 COPR project 均无满足要求（{requested_desc}）的包，进入引入流程"


# ── 主函数 ────────────────────────────────────────────────────────────────────

def check_existing_package(pkgname: str, version: str = "", requirement: str = "",
                           lang: str = "", copr_url: str = "", owner: str = "",
                           project: str = "", login: str = "", token: str = "",
                           chroot: str = "") -> dict:
    """
    chroot: 目标构建 chroot（如 openeuler-22.03_LTS_SP2-x86_64）。
            传入后自动切换到对应 openEuler 版本的 repo 查询。
    """
    if chroot and _chroot_to_repo_base(chroot) is None:
        # 未识别的 chroot：禁止静默回退到 worker 本地 repo（那可能是错误的
        # OS 版本，查询结果会被误记为目标 OS 的结论）。显式标记查询不可用，
        # 按"官方源未确认"处理（decision=introduce_new，构建是安全方向）。
        print(f"[WARN] unknown chroot {chroot!r}: 无 repo 映射，跳过官方源查询",
              file=sys.stderr)
        not_found = {"exists": False, "highest": None, "meets_need": False,
                     "matched_paths": [], "comparison_unknown": True}
        return {
            "requested": {
                "pkgname":          pkgname,
                "version":          version,
                "requirement":      requirement,
                "lang":             lang,
                "chroot":           chroot,
                "requirement_info": parse_requirement(requirement),
            },
            "official":              not_found,
            "copr_project":          dict(not_found),
            "exists_in_official":    False,
            "exists_in_copr_project": False,
            "decision":              "introduce_new",
            "reason":                f"chroot {chroot} 无 repo 映射，官方源查询不可用",
            "should_skip":           False,
            "error":                 f"unknown chroot: {chroot}",
        }

    repo_switched = False
    try:
        if chroot:
            repo_switched = setup_repo_for_chroot(chroot, copr_url=copr_url,
                                                   owner=owner, project=project)

        official = summarize_official_repo(pkgname, lang, version, requirement)
        copr     = summarize_copr_project(pkgname, lang, version, requirement,
                                          copr_url=copr_url, owner=owner, project=project,
                                          login=login, token=token)
    finally:
        if repo_switched:
            teardown_repo()

    decision = choose_decision(official, copr, version, requirement)

    return {
        "requested": {
            "pkgname":          pkgname,
            "version":          version,
            "requirement":      requirement,
            "lang":             lang,
            "chroot":           chroot,
            "requirement_info": parse_requirement(requirement),
        },
        "official":              official,
        "copr_project":          copr,
        "exists_in_official":    official["exists"],
        "exists_in_copr_project": copr["exists"],
        "decision":              decision,
        "reason":                build_reason(decision, official, copr, version, requirement),
        "should_skip":           decision in {"reuse_official", "reuse_copr_project"},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="检查包在官方源和 COPR project 中的决策（无 Docker）")
    parser.add_argument("pkgname")
    parser.add_argument("--version",     default="")
    parser.add_argument("--requirement", default="")
    parser.add_argument("--lang",        default="")
    parser.add_argument("--copr-url",    default=os.environ.get("COPR_FRONTEND_URL", "http://copr-frontend:5000"))
    parser.add_argument("--owner",       default=os.environ.get("COPR_OWNER", ""))
    parser.add_argument("--project",     default=os.environ.get("COPR_PROJECT", ""))
    parser.add_argument("--login",       default=os.environ.get("COPR_API_LOGIN", ""))
    parser.add_argument("--token",       default=os.environ.get("COPR_API_TOKEN", ""))
    parser.add_argument("--chroot",       default="", help="目标 chroot（如 openeuler-22.03_LTS_SP2-x86_64），用于动态切换 repo")
    parser.add_argument("--json",        action="store_true")
    parser.add_argument("-o", "--output", default="")
    args = parser.parse_args()

    result = check_existing_package(
        args.pkgname,
        version=args.version.strip(),
        requirement=args.requirement.strip(),
        lang=args.lang.strip().lower(),
        copr_url=args.copr_url,
        owner=args.owner,
        project=args.project,
        login=args.login,
        token=args.token,
        chroot=args.chroot.strip(),
    )

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json or not args.output:
        print(json.dumps(result, ensure_ascii=False, indent=2))


import os
if __name__ == "__main__":
    main()
