#!/usr/bin/env python3
"""
Python 包 RPM 依赖分析脚本

依赖来源取并集：
  1. PyPI JSON API（https://pypi.org/pypi/<pkg>/json）— 最权威
  2. 本地源码解析（setup.py / pyproject.toml / requirements.txt）— 兜底

C 扩展检测：
  - PyPI：wheel URL 含架构（amd64/arm64）→ 有 C 扩展
  - 本地：.pyx 文件 / Extension() 调用 / .c 文件

最终用容器内一次性批量查询 `python3dist(...)` 的 RPM Provides 可用性，
完全依赖 RPM 官方 Provides 机制，不猜包名前缀。

用法：
  python3 analyze_python_deps.py <source_dir> [--pkg <pypi_name>]
  python3 analyze_python_deps.py <source_dir> --pkg requests --check-rpm --container oe-build-env
  python3 analyze_python_deps.py <source_dir> --check-rpm -o result.json
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from rpm_batch_lookup import BatchLookupError, fallback_results, name_glob_query, provides_query, run_batch_lookup
from rpm_naming import get_rpm_pkg_name, get_srpm_name, rpm_name_from_pep508


# ── 1. 包名规范化 ─────────────────────────────────────────────────────────────

def normalize_pkg_name(name: str) -> str:
    """规范化 Python 包名：小写 + 连字符（PEP 503）"""
    return re.sub(r"[-_.]+", "-", name).lower()


def extract_pypi_name(pkg_spec: str) -> str:
    """
    从 PEP 508 依赖规范提取规范化的 PyPI 包名，用于 python3dist() 查询。
    'python-dateutil>=2.7.0; python_version>="3"' → 'python-dateutil'
    'requests[security]>=2.0'                     → 'requests'
    """
    pkg_spec = pkg_spec.split(";")[0].strip()
    pkg_spec = re.sub(r"\[.*?\]", "", pkg_spec)
    m = re.match(r"([a-zA-Z0-9_\-\.]+)", pkg_spec.strip())
    if not m:
        return ""
    return normalize_pkg_name(m.group(1))


def transform_module_name(pkg_spec: str) -> str:
    """
    将 PEP 508 依赖转为 RPM Requires 表达式。
    'requests>=2.0,<3'       → '(python3-requests >= 2.0 with python3-requests < 3)'
    'python-dateutil>=2.7.0' → 'python3-python-dateutil >= 2.7.0'
    'click'                  → 'python3-click'
    委托给 rpm_naming.rpm_name_from_pep508()，保持命名与 get_rpm_pkg_name("python") 一致。
    """
    return rpm_name_from_pep508(pkg_spec)


def extract_requirement_expr(pkg_spec: str) -> str:
    """提取依赖中的原始版本约束表达式，无法提取时返回空串。"""
    pkg_spec_clean = pkg_spec.split(";")[0].strip()
    pkg_spec_clean = re.sub(r"\[.*?\]", "", pkg_spec_clean)
    m = re.match(r"([a-zA-Z0-9_\-\.]+)", pkg_spec_clean.strip())
    if not m:
        return ""
    version_part = pkg_spec_clean[len(m.group(1)):].strip().strip("()")
    if not version_part:
        return ""
    constraints = []
    for c in version_part.split(","):
        c = c.strip()
        if c:
            constraints.append(re.sub(r"([><=!]+)\s*", r"\1 ", c).strip())
    return ", ".join(constraints)


def project_url_for_pypi_name(pypi_name: str) -> str:
    return f"https://pypi.org/project/{pypi_name}"


TRUSTED_REPO_HOSTS = {
    "github.com",
    "gitlab.com",
    "gitee.com",
    "gitcode.com",
    "atomgit.com",
    "bitbucket.org",
}

PREFERRED_PROJECT_URL_KEYS = [
    "source",
    "source code",
    "repository",
    "code",
    "homepage",
    "home",
]

SUSPICIOUS_PATH_SEGMENTS = {
    "issues",
    "releases",
    "pull",
    "pulls",
    "actions",
    "wiki",
    "blob",
    "tree",
    "docs",
    "discussions",
    "milestones",
    "projects",
    "security",
    "commit",
    "commits",
    "compare",
    "raw",
}

BLOCKED_UPSTREAM_HOSTS = {
    "pypi.org",
    "test.pypi.org",
    "pypi.python.org",
    "pythonhosted.org",
    "readthedocs.io",
    "readthedocs.org",
}

# GitHub/Gitee/GitLab 保留路径前缀（不是用户/组织名）
_RESERVED_BY_HOST = {
    "github.com": {
        "sponsors", "orgs", "apps", "topics", "collections",
        "marketplace", "trending", "explore", "features",
        "enterprise", "settings", "notifications", "login",
        "join", "about", "pricing", "site", "readme",
        "account", "dashboard", "codespaces", "gists",
    },
    "gitlab.com": {
        "help", "explore", "users", "groups", "-",
        "dashboard", "search",
    },
    "gitee.com": {
        "organizations", "explore", "enterprises", "gists",
    },
    # bitbucket.org / atomgit.com / gitcode.com：暂无已知保留 namespace，保守留空
}


def normalize_repo_root(url: str) -> str:
    if not isinstance(url, str):
        return ""
    raw = url.strip()
    if not raw.startswith(("http://", "https://")):
        return ""

    parsed = urlparse(raw)
    host = parsed.netloc.lower().removeprefix("www.")
    if host not in TRUSTED_REPO_HOSTS:
        return ""

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return ""

    owner = parts[0]
    repo = parts[1].removesuffix(".git")
    if not owner or not repo:
        return ""

    return f"https://{host}/{owner}/{repo}"


def classify_upstream_url(url: str) -> str:
    if not isinstance(url, str):
        return "invalid"
    raw = url.strip()
    if not raw.startswith(("http://", "https://")):
        return "invalid"

    parsed = urlparse(raw)
    host = parsed.netloc.lower().removeprefix("www.")
    if host in BLOCKED_UPSTREAM_HOSTS:
        return "suspicious"
    if host not in TRUSTED_REPO_HOSTS:
        return "invalid"

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return "invalid"
    # 检查第一段是否是 Git 平台保留 namespace（如 github.com/sponsors/xxx）
    reserved = _RESERVED_BY_HOST.get(host, set())
    if parts[0].lower() in reserved:
        return "invalid"
    if len(parts) == 2:
        return "trusted"
    if parts[2].lower() in SUSPICIOUS_PATH_SEGMENTS:
        return "suspicious"
    return "suspicious"


def normalize_candidate_upstream(url: str) -> str:
    kind = classify_upstream_url(url)
    if kind == "trusted":
        return normalize_repo_root(url)
    if kind == "suspicious":
        return normalize_repo_root(url)
    return ""


NON_REPO_PROJECT_URL_KEYS = {
    "sponsor", "funding", "donation", "donate",
    "twitter", "chat", "discord", "slack", "gitter",
    "say thanks", "say thanks!",
    "changelog", "release notes", "history", "documentation",
}

def candidate_urls_from_pypi_info(info: Dict) -> List[str]:
    candidates: List[str] = []
    project_urls = info.get("project_urls") or {}
    if isinstance(project_urls, dict):
        # 第一轮：优先 key 匹配
        for preferred_key in PREFERRED_PROJECT_URL_KEYS:
            for key, value in project_urls.items():
                if not value:
                    continue
                if key.strip().lower() == preferred_key:
                    candidates.append(value)
        # 第二轮：所有 project_urls（排除已知非仓库 key）
        for key, value in project_urls.items():
            if not value:
                continue
            if key.strip().lower() in NON_REPO_PROJECT_URL_KEYS:
                continue
            candidates.append(value)
    if info.get("home_page"):
        candidates.append(info["home_page"])
    return candidates


def canonical_upstream_url(pypi_json: Optional[Dict], pypi_name: str) -> str:
    """优先返回可信源码仓根地址；无法确认时返回空串。

    候选 URL 的 norm（归约根）若被 classify 为 trusted，则接受。
    这样深度 URL（如 tree/blob/issues 子路径）也能通过归约到 root 后胜出，
    同时 Sponsor/Funding 等保留 namespace 被阻挡。
    """
    if pypi_json:
        info = pypi_json.get("info", {})
        for url in candidate_urls_from_pypi_info(info):
            norm = normalize_candidate_upstream(url)
            if norm and classify_upstream_url(norm) == "trusted":
                return norm
    return ""


def build_dependency_item(pkg_spec: str, pypi_json: Optional[Dict] = None) -> Optional[Dict[str, str]]:
    pypi_name = extract_pypi_name(pkg_spec)
    if not pypi_name:
        return None
    rpm_requirement = transform_module_name(pkg_spec)
    return {
        "name": pypi_name,
        "spec": pkg_spec,
        "requirement": extract_requirement_expr(pkg_spec),
        "rpm_requirement": rpm_requirement or get_rpm_pkg_name("python", pypi_name),
        "rpm_pkg_name": get_rpm_pkg_name("python", pypi_name),
        "srpm_name": get_srpm_name("python", pypi_name),
        "upstream_url": canonical_upstream_url(pypi_json, pypi_name),
    }


def build_dependency_items(requires: List[str], pypi_metadata: Optional[Dict[str, Dict]] = None) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    for dep_spec in requires:
        pypi_name = extract_pypi_name(dep_spec)
        item = build_dependency_item(dep_spec, (pypi_metadata or {}).get(pypi_name, {}))
        if not item:
            continue
        key = (item["name"], item["requirement"])
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
    return items


# ── 2. PyPI API 查询 ──────────────────────────────────────────────────────────

def fetch_pypi_info(pkg_name: str, version: str = "") -> Optional[Dict]:
    """
    从 PyPI JSON API 获取包元数据。
    若指定 version，优先查 /pypi/<name>/<version>/json；404 时回退到最新版。
    返回 None 表示包不存在或网络不通。
    """
    def _fetch(url: str) -> Optional[Dict]:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "analyze_python_deps/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code != 404:
                print(f"[WARN] PyPI 请求失败 ({e.code}): {url}", file=sys.stderr)
        except Exception as e:
            print(f"[WARN] PyPI 网络错误: {e}", file=sys.stderr)
        return None

    if version:
        result = _fetch(f"https://pypi.org/pypi/{pkg_name}/{version}/json")
        if result:
            return result
        # 指定版本 404，回退最新版
        print(f"[WARN] PyPI 上未找到 {pkg_name}=={version}，回退到最新版", file=sys.stderr)

    result = _fetch(f"https://pypi.org/pypi/{pkg_name}/json")
    if result is None:
        print(f"[WARN] PyPI 上未找到包: {pkg_name}", file=sys.stderr)
    return result


def collect_pypi_metadata(requires: List[str]) -> Dict[str, Dict]:
    """为依赖项批量收集 PyPI 元数据，用于补全 canonical upstream URL。"""
    metadata: Dict[str, Dict] = {}
    seen: Set[str] = set()
    for dep_spec in requires:
        pypi_name = extract_pypi_name(dep_spec)
        if not pypi_name or pypi_name in seen:
            continue
        seen.add(pypi_name)
        pypi_json = fetch_pypi_info(pypi_name)
        if pypi_json:
            metadata[pypi_name] = pypi_json
    return metadata


def parse_pypi_deps(pypi_json: Dict) -> Tuple[List[str], bool, str]:
    """
    从 PyPI JSON 提取依赖和 C 扩展信息。
    返回 (requires_list, has_c_ext, version)
    """
    info = pypi_json.get("info", {})
    requires_dist = info.get("requires_dist") or []

    requires = []
    for r in requires_dist:
        # 过滤掉 extra 可选依赖（如 ; extra == "test"）
        idx = r.find(";")
        if idx != -1:
            marker = r[idx + 1:].strip()
            if "extra" in marker:
                continue
        clean = r[:idx].strip() if idx != -1 else r.strip()
        if clean:
            requires.append(clean)

    # C 扩展检测：检查是否有架构相关的 wheel（参考 pyporter __get_buildarch）
    version = info.get("version", "")
    has_c_ext = False

    # 检查当前版本的 releases
    releases = pypi_json.get("releases", {})
    urls = releases.get(version, []) or pypi_json.get("urls", [])
    for r in urls:
        pkg_type = r.get("packagetype", "")
        url = r.get("url", "")
        # 有架构相关 wheel 说明有 C 扩展
        if pkg_type == "bdist_wheel" and any(
            arch in url for arch in ("amd64", "x86_64", "arm64", "aarch64", "cp3")
        ):
            has_c_ext = True
            break

    return requires, has_c_ext, version


# ── 3. 本地源码解析 ───────────────────────────────────────────────────────────

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        try:
            from pip._vendor import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            tomllib = None  # type: ignore[assignment]


def _load_toml(toml_file: Path) -> Dict:
    """用 tomllib 解析 TOML 文件，不可用时返回空字典"""
    if tomllib is None:
        print("  [WARN] tomllib 不可用，跳过 pyproject.toml 解析", file=sys.stderr)
        return {}
    try:
        with open(toml_file, "rb") as f:
            return tomllib.load(f)
    except Exception as e:
        print(f"  [WARN] 解析 pyproject.toml 失败: {e}", file=sys.stderr)
        return {}


def parse_local_deps(source_dir: str) -> Tuple[List[str], str]:
    """
    从本地源码解析运行时依赖，优先级：pyproject.toml > setup.py > requirements.txt
    返回 (requires_list, build_backend)
    """
    src = Path(source_dir)

    # pyproject.toml —— 用 tomllib 正确解析，避免正则截断问题
    toml_file = src / "pyproject.toml"
    if toml_file.exists():
        data = _load_toml(toml_file)
        if data:
            project = data.get("project", {})
            deps = list(project.get("dependencies", []))

            if "dependencies" in project.get("dynamic", []):
                print("  [WARN] dependencies 为 dynamic，本地无法静态获取", file=sys.stderr)

            # 从 build-backend 字段提取简短名称
            # 例：hatchling.build → hatchling，setuptools.build_meta → setuptools
            backend_full = data.get("build-system", {}).get("build-backend", "")
            _backend_map = {
                "hatchling": "hatchling", "setuptools": "setuptools",
                "flit_core": "flit",      "poetry":     "poetry",
                "pdm":       "pdm",       "meson":      "meson-python",
            }
            backend = "setuptools"
            for key, name in _backend_map.items():
                if key in backend_full:
                    backend = name
                    break

            # 只有 [project] 节存在且有 dependencies 时才提前返回；
            # 否则继续尝试 setup.py（如 skypilot：pyproject.toml 只含 build-system/tool，
            # 依赖全在 setup.py 里）
            if deps or "project" in data:
                return deps, backend

    # setup.py 兜底
    setup_py = src / "setup.py"
    if setup_py.exists():
        content = setup_py.read_text(errors="ignore")
        requires = []
        # 用括号深度匹配，避免 ray[default] 这类带 extras 的依赖里的 ] 触发非贪婪提前终止
        start = content.find("install_requires")
        if start != -1:
            bracket_start = content.find("[", start)
            if bracket_start != -1:
                depth = 0
                end_pos = bracket_start
                for i, c in enumerate(content[bracket_start:]):
                    if c == "[":
                        depth += 1
                    elif c == "]":
                        depth -= 1
                        if depth == 0:
                            end_pos = bracket_start + i
                            break
                inner = content[bracket_start + 1:end_pos]
                for dep in re.findall(r"""['"]([^'"]+)['"]""", inner):
                    dep = dep.strip()
                    if dep:
                        # 检测未填充的模板占位符（如 numpy >={}），方便排查
                        if re.search(r'[><=!~]+\s*\{\s*\}', dep):
                            print(f"  [WARN] 本地约束含未填充占位符: {dep!r}，"
                                  f"将由 merge_requires 降级到 PyPI 约束", file=sys.stderr)
                        requires.append(dep)
        if requires:
            backend = "flit" if "flit" in content else ("poetry" if "poetry" in content else "setuptools")
            return requires, backend

    # requirements.txt 兜底
    req_file = src / "requirements.txt"
    if req_file.exists():
        requires = []
        for line in req_file.read_text(errors="ignore").splitlines():
            line = line.split("#")[0].strip()
            if line and not line.startswith("-"):
                requires.append(line)
        return requires, "setuptools"

    return [], "setuptools"


def parse_build_system_deps(source_dir: str) -> List[str]:
    """从 pyproject.toml 的 [build-system].requires 提取构建系统依赖"""
    toml_file = Path(source_dir) / "pyproject.toml"
    if not toml_file.exists():
        return []
    data = _load_toml(toml_file)
    return list(data.get("build-system", {}).get("requires", []))


def scan_c_extensions_local(source_dir: str) -> Dict:
    """本地扫描 C 扩展迹象"""
    src = Path(source_dir)
    reasons = []

    setup_py = src / "setup.py"
    if setup_py.exists() and re.search(r"\bExtension\s*\(", setup_py.read_text(errors="ignore")):
        reasons.append("setup.py 包含 Extension() 调用")

    pyx_files = [str(f.relative_to(src)) for f in src.rglob("*.pyx")]
    if pyx_files:
        reasons.append(f"发现 {len(pyx_files)} 个 .pyx 文件（Cython）")

    c_files = []
    for ext in ("*.c", "*.cpp", "*.cc"):
        c_files.extend(str(f.relative_to(src)) for f in src.rglob(ext))
    if c_files:
        reasons.append(f"发现 {len(c_files)} 个 C/C++ 源文件")

    return {
        "has_c_ext": len(reasons) > 0,
        "reasons": reasons,
        "pyx_files": pyx_files[:5],
        "c_files": c_files[:5],
    }


# glibc/编译器自带库，不对应额外的 -devel RPM，从链接库检测中排除
_C_LIB_BUILTINS = {"m", "c", "pthread", "dl", "rt", "util", "resolv", "gcc_s", "stdc++"}


def parse_extension_libraries(source_dir: str) -> List[str]:
    """从 setup.py 的 Extension(libraries=[...]) 和 .pyx 的 `# distutils: libraries`
    声明中静态提取需要链接的系统库名（仅解析显式声明，不执行任何代码）。

    scan_c_extensions_local 函数只判断"有没有 C 扩展"，本函数进一步
    回答"链接了哪些库"，供后续映射到 -devel RPM 包名。解析不出（如库名是变量拼接、
    或用 pkg-config 动态探测）的情况，交给构建失败诊断循环兜底。
    """
    src = Path(source_dir)
    libs: Set[str] = set()

    # 1. setup.py 中的 Extension(..., libraries=['pq', 'ssl'], ...)
    setup_py = src / "setup.py"
    if setup_py.exists():
        text = setup_py.read_text(errors="ignore")
        for m in re.finditer(r"libraries\s*=\s*\[([^\]]*)\]", text):
            for lit in re.findall(r"""['"]([^'"]+)['"]""", m.group(1)):
                libs.add(lit.strip())

    # 2. Cython .pyx 头部的 `# distutils: libraries = pq ssl`
    for pyx in src.rglob("*.pyx"):
        try:
            head = pyx.read_text(errors="ignore")[:2000]
        except OSError:
            continue
        for m in re.finditer(r"#\s*distutils:\s*libraries\s*=\s*(.+)", head):
            for name in m.group(1).split():
                libs.add(name.strip())

    return sorted(n for n in libs if n and n.lower() not in _C_LIB_BUILTINS)


# ── 4. 依赖并集合并 ───────────────────────────────────────────────────────────

def _extract_pkg_key(dep: str) -> str:
    return normalize_pkg_name(dep.split(";")[0].split("[")[0].split(">=")[0]
                               .split("<=")[0].split("!=")[0].split("==")[0]
                               .split(">")[0].split("<")[0].strip())


def _extract_local_version(source_dir: str) -> str:
    """从本地源码提取包版本，用于与 PyPI 版本比对。"""
    src = Path(source_dir)

    toml_file = src / "pyproject.toml"
    if toml_file.exists():
        data = _load_toml(toml_file)
        if data:
            v = data.get("project", {}).get("version", "")
            if v and not str(v).startswith("attr:"):
                return str(v)
            # poetry
            v = data.get("tool", {}).get("poetry", {}).get("version", "")
            if v:
                return str(v)
            # setuptools dynamic: version = {attr = "pkg.__version__"}
            dyn_ver = (data.get("tool", {}).get("setuptools", {})
                       .get("dynamic", {}).get("version", {}))
            if isinstance(dyn_ver, dict):
                attr_ref = dyn_ver.get("attr", "")  # e.g. "aiohttp.__version__"
                if attr_ref:
                    # try to read the attribute from the source file directly
                    parts = attr_ref.rsplit(".", 1)
                    if len(parts) == 2:
                        mod_path = src / parts[0].replace(".", "/") / "__version__.py"
                        if not mod_path.exists():
                            mod_path = src / (parts[0].replace(".", "/") + ".py")
                        if mod_path.exists():
                            content = mod_path.read_text(errors="ignore")
                            m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']',
                                          content, re.MULTILINE)
                            if m:
                                return m.group(1)
                        # fallback: search in __init__.py
                        init_path = src / parts[0].replace(".", "/") / "__init__.py"
                        if init_path.exists():
                            content = init_path.read_text(errors="ignore")
                            m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']',
                                          content, re.MULTILINE)
                            if m:
                                return m.group(1)

    setup_cfg = src / "setup.cfg"
    if setup_cfg.exists():
        content = setup_cfg.read_text(errors="ignore")
        m = re.search(r"^\s*version\s*=\s*(.+)$", content, re.MULTILINE)
        if m:
            raw = m.group(1).strip()
            # handle "attr: pkg.module.__version__" references
            attr_m = re.match(r"attr:\s*(.+)", raw)
            if attr_m:
                attr_ref = attr_m.group(1).strip()  # e.g. "aiohttp.__version__"
                parts = attr_ref.rsplit(".", 1)
                if len(parts) == 2:
                    mod_path = src / parts[0].replace(".", "/") / "__version__.py"
                    if not mod_path.exists():
                        mod_path = src / (parts[0].replace(".", "/") + ".py")
                    if mod_path.exists():
                        vm = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']',
                                       mod_path.read_text(errors="ignore"), re.MULTILINE)
                        if vm:
                            return vm.group(1)
                    init_path = src / parts[0].replace(".", "/") / "__init__.py"
                    if init_path.exists():
                        vm = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']',
                                       init_path.read_text(errors="ignore"), re.MULTILINE)
                        if vm:
                            return vm.group(1)
            elif not raw.startswith("file:"):
                return raw

    for fname in ("VERSION", "version.txt"):
        vfile = src / fname
        if vfile.exists():
            return vfile.read_text(errors="ignore").strip().splitlines()[0].strip()

    # fallback: scan direct subdirectory __init__.py for __version__
    # (e.g. celery stores version in celery/__init__.py, read by setup.py)
    for init_path in sorted(src.glob("*/__init__.py")):
        if init_path.parent.name.startswith("."):
            continue
        content = init_path.read_text(errors="ignore")
        m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', content, re.MULTILINE)
        if m:
            return m.group(1)

    return ""


def _extract_version_constraint(dep: str) -> str:
    """Extract the version constraint part (everything after package name)."""
    name_part = dep.split(";")[0].split("[")[0]
    for op in (">=", "<=", "!=", "==", ">", "<", "~="):
        if op in name_part:
            idx = name_part.index(op)
            return name_part[idx:].strip()
    return ""


# 约束含未替换模板占位符的模式：>= {}、< {}、== {} 等
_TEMPLATE_PLACEHOLDER_RE = re.compile(r'[><=!~]+\s*\{\s*\}')


def _constraint_is_broken(spec: str) -> bool:
    """检测 spec 中的约束是否包含未填充的模板占位符（如 >= {}）。"""
    return bool(_TEMPLATE_PLACEHOLDER_RE.search(spec))


def merge_requires(pypi_requires: List[str], local_requires: List[str]) -> List[str]:
    """
    合并 PyPI 和本地解析的依赖。
    策略：本地 pyproject.toml / setup.py 为主，PyPI 仅补充本地没有的包。
    若本地解析出的约束含未填充模板占位符（如 >= {}），
    则降级使用 PyPI 的真实约束覆盖本地脏数据。
    """
    seen: Dict[str, str] = {}  # normalized_name -> original_spec

    # 本地优先：先用本地所有依赖填充
    for dep in local_requires:
        key = _extract_pkg_key(dep)
        if key:
            seen[key] = dep

    # PyPI 补充本地没有的包；若本地约束是模板占位符，用 PyPI 真实约束覆盖
    for dep in pypi_requires:
        key = _extract_pkg_key(dep)
        if not key:
            continue
        if key not in seen:
            # 本地没有这个包，从 PyPI 补充
            seen[key] = dep
        elif _constraint_is_broken(seen[key]):
            # 本地解析出模板占位符（如 numpy >={}），用 PyPI 真实约束覆盖
            seen[key] = dep

    return list(seen.values())


def build_lookup_tasks(requires: List[str], pypi_metadata: Optional[Dict[str, Dict]] = None) -> List[Dict]:
    tasks: List[Dict] = []
    for dep_spec in requires:
        item = build_dependency_item(dep_spec, (pypi_metadata or {}).get(extract_pypi_name(dep_spec), {}))
        if not item:
            continue
        pypi_name = item["name"]
        tasks.append({
            "dep": dep_spec,
            "name": pypi_name,
            "requirement": item["requirement"],
            "rpm_requirement": item["rpm_requirement"],
            "upstream_url": item["upstream_url"],
            "rpm_name": f"python3dist({pypi_name})",
            "queries": [provides_query(f"python3dist({pypi_name})", "python3dist()")],
        })
    return tasks


# ── 5. 容器内 dnf 查询 ────────────────────────────────────────────────────────

def check_rpm_availability(requires: List[str] = None, pypi_metadata: Optional[Dict[str, Dict]] = None,
                           chroot: Optional[str] = None) -> Dict:
    """
    批量查询依赖的 RPM 可用性（本地 dnf 执行，无需容器）。
    chroot: 目标构建 chroot（如 openeuler-22.03_LTS_SP2-x86_64），
            指定时使用对应的 openEuler 源查询，确保结果与构建环境一致。
    返回 available / missing / version_conflict 列表。
    """
    if requires is None:
        requires = []
    tasks = build_lookup_tasks(requires, pypi_metadata)
    chroot_info = f"，chroot={chroot}" if chroot else ""
    print(f"\n[INFO] 本地查询 RPM 可用性（通过 python3dist，单次批量查询{chroot_info}）...")

    try:
        results = run_batch_lookup(tasks, timeout=120, chroot=chroot)
    except (BatchLookupError, OSError, json.JSONDecodeError) as e:
        print(f"[WARN] python3dist 批量查询失败（{e}），跳过依赖检查")
        results = fallback_results(tasks)

    import importlib as _il
    import sys as _sys
    _script_dir = str(Path(__file__).resolve().parent)
    if _script_dir not in _sys.path:
        _sys.path.insert(0, _script_dir)
    _cep = _il.import_module("check_existing_package")

    available = []
    missing = []
    version_conflict = []
    for item in results:
        dep_spec = item["dep"]
        label = item["rpm_name"]
        found = item.get("rpm")
        requirement = item.get("requirement", "")
        if found:
            rpm_version = item.get("version") or ""
            version_label = f" {rpm_version}" if rpm_version else ""
            version_ok = True
            if rpm_version and requirement:
                try:
                    req_info = _cep.parse_requirement(requirement)
                    eval_result = _cep.evaluate_requirement(rpm_version, req_info)
                    if eval_result is False:
                        version_ok = False
                except Exception:
                    pass
            if version_ok:
                print(f"  ✓ {label:<45} → {found}{version_label}")
                available.append({
                    "dep": dep_spec,
                    "name": item.get("name", ""),
                    "requirement": requirement,
                    "rpm_requirement": item.get("rpm_requirement", label),
                    "rpm": found,
                    "version": item.get("version"),
                    "release": item.get("release"),
                    "upstream_url": item.get("upstream_url", ""),
                })
            else:
                print(f"  ~ {label:<45} → {found}{version_label} 不满足约束 {requirement}（版本冲突）")
                version_conflict.append({
                    "dep": dep_spec,
                    "name": item.get("name", ""),
                    "requirement": requirement,
                    "rpm_requirement": item.get("rpm_requirement", label),
                    "rpm_name": label,
                    "rpm": found,
                    "found_version": rpm_version,
                    "upstream_url": item.get("upstream_url", ""),
                })
        else:
            print(f"  ✗ {label:<45} → 未找到")
            missing.append({
                "dep": dep_spec,
                "name": item.get("name", ""),
                "requirement": requirement,
                "rpm_requirement": item.get("rpm_requirement", label),
                "rpm_name": label,
                "upstream_url": item.get("upstream_url", ""),
            })

    return {"available": available, "missing": missing, "version_conflict": version_conflict}


def check_c_library_rpms(lib_names: List[str], chroot: Optional[str] = None) -> Dict:
    """把 C 扩展链接的系统库名映射到 -devel RPM 包名（三级查询，与 analyze_c_deps
    的 link_lib 路径一致：pkgconfig → cmake → lib*-devel / *-devel）。

    只返回在目标 chroot 源中确实存在的包（available）；查不到的（missing）不做处理，
    交给构建失败诊断循环兜底，避免写入未经验证的 BuildRequires。查询本身失败时
    （无 dnf / 网络问题）也返回空 available，保持保守。
    """
    if not lib_names:
        return {"available": [], "missing": []}

    tasks = []
    for lib in lib_names:
        low = lib.lower()
        tasks.append({
            "dep": lib,
            "name": lib,
            "rpm_name": f"{low}-devel",
            "prefer_devel": True,
            "queries": [
                provides_query(f"pkgconfig({low})", "pkgconfig()"),
                provides_query(f"cmake({lib})", "cmake()"),
                name_glob_query(f"lib{low}*-devel", "name-glob", prefer_devel=True),
                name_glob_query(f"{low}*-devel", "name-glob", prefer_devel=True),
            ],
        })

    chroot_info = f"，chroot={chroot}" if chroot else ""
    print(f"\n[INFO] 查询 C 扩展链接库的 -devel RPM（{len(tasks)} 个{chroot_info}）...")
    try:
        results = run_batch_lookup(tasks, timeout=120, chroot=chroot)
    except (BatchLookupError, OSError, json.JSONDecodeError) as e:
        print(f"[WARN] C 库 RPM 查询失败（{e}），跳过（交由构建失败循环兜底）")
        return {"available": [], "missing": [{"lib": t["dep"]} for t in tasks]}

    available, missing = [], []
    for item in results:
        lib = item["dep"]
        rpm = item.get("rpm")
        if rpm:
            print(f"  ✓ lib {lib:<30} → {rpm}")
            available.append({"lib": lib, "rpm": rpm, "level": item.get("level", "")})
        else:
            print(f"  ✗ lib {lib:<30} → 未找到（交由构建失败循环兜底）")
            missing.append({"lib": lib})
    return {"available": available, "missing": missing}


# ── 6. 报告输出 ───────────────────────────────────────────────────────────────

def build_rpm_requires(c_ext: Dict, rpm_check: Optional[Dict],
                       build_sys_rpms: Optional[List[str]] = None) -> List[str]:
    """生成 spec BuildRequires 列表"""
    result = ["python3-devel", "python3-setuptools", "python3-pip", "python3-wheel"]
    seen = set(result)
    # 构建系统依赖（hatchling 等）优先加入
    for rpm in (build_sys_rpms or []):
        if rpm not in seen:
            result.append(rpm)
            seen.add(rpm)
    if c_ext.get("has_c_ext"):
        result.append("gcc")
        seen.add("gcc")
        if c_ext.get("pyx_files"):
            result.append("python3-Cython")
            seen.add("python3-Cython")
    if rpm_check:
        for item in rpm_check.get("available", []):
            rpm = item["rpm"]
            if rpm not in seen:
                result.append(rpm)
                seen.add(rpm)
    return result


def print_report(source_dir: str, pkg_name: str, version: str,
                 pypi_requires: List[str], local_requires: List[str],
                 merged_requires: List[str], build_backend: str,
                 c_ext_pypi: bool, c_ext_local: Dict,
                 rpm_check: Optional[Dict],
                 build_sys_requires: Optional[List[str]] = None,
                 build_sys_rpm_check: Optional[Dict] = None):
    sep = "=" * 65
    print(f"\n{sep}")
    print("Python 包 RPM 依赖分析报告")
    print(sep)
    print(f"  包名      : {pkg_name or '(未知)'}  {version}")
    print(f"  源码目录  : {source_dir}")
    print(f"  构建后端  : {build_backend}")

    print(f"\n[依赖来源对比]")
    print(f"  PyPI API  : {len(pypi_requires)} 个依赖")
    print(f"  本地解析  : {len(local_requires)} 个依赖")
    print(f"  并集合并  : {len(merged_requires)} 个依赖")

    if merged_requires:
        print(f"\n[合并后依赖列表]")
        pypi_set = {normalize_pkg_name(d.split(";")[0].split("[")[0]
                    .split(">=")[0].split("<=")[0].split("!=")[0]
                    .split("==")[0].split(">")[0].split("<")[0].strip())
                    for d in pypi_requires}
        for dep in merged_requires:
            key = normalize_pkg_name(dep.split(";")[0].split("[")[0]
                  .split(">=")[0].split("<=")[0].split("!=")[0]
                  .split("==")[0].split(">")[0].split("<")[0].strip())
            src_tag = "[PyPI]" if key in pypi_set else "[本地]"
            dist_label = f"python3dist({extract_pypi_name(dep)})"
            print(f"  {src_tag} {dep:<40} → {dist_label}")

    if build_sys_requires:
        print(f"\n[构建系统依赖]  来自 [build-system].requires")
        bs_avail = {item["dep"]: item["rpm"] for item in (build_sys_rpm_check or {}).get("available", [])}
        bs_miss  = {item["dep"] for item in (build_sys_rpm_check or {}).get("missing",  [])}
        for dep in build_sys_requires:
            dist_label = f"python3dist({extract_pypi_name(dep)})"
            if dep in bs_avail:
                print(f"  ✓ {dep:<40} → {bs_avail[dep]}")
            elif dep in bs_miss:
                print(f"  ✗ {dep:<40} → {dist_label}  (未找到)")
            else:
                print(f"  ? {dep:<40} → {dist_label}")

    print(f"\n[C 扩展检测]")
    if c_ext_pypi:
        print("  ✓ PyPI wheel 含架构标记（有 C 扩展）")
    if c_ext_local["has_c_ext"]:
        for r in c_ext_local["reasons"]:
            print(f"  ✓ {r}")
    if not c_ext_pypi and not c_ext_local["has_c_ext"]:
        print("  纯 Python 包，无 C 扩展")

    if rpm_check:
        avail = rpm_check["available"]
        miss = rpm_check["missing"]
        print(f"\n[RPM 可用性]  已有 {len(avail)} 个 / 缺失 {len(miss)} 个")
        if avail:
            for item in avail:
                print(f"  ✓ {item['dep']:<40} → {item['rpm']}")
        if miss:
            print(f"\n  ✗ 缺失（需自行打包或从其他源安装）:")
            for item in miss:
                print(f"    {item['dep']:<40}  ({item['rpm_name']})")

    # 优先用查询到的实际 RPM 包名；无查询结果时用 python3dist() 格式
    if build_sys_rpm_check:
        build_sys_rpms = [item["rpm"] for item in build_sys_rpm_check.get("available", [])]
    else:
        build_sys_rpms = [f"python3dist({extract_pypi_name(d)})"
                          for d in (build_sys_requires or []) if extract_pypi_name(d)]
    br = build_rpm_requires(c_ext_local, rpm_check, build_sys_rpms)
    print(f"\n[BuildRequires 建议]")
    for r in br:
        print(f"  BuildRequires: {r}")
    print(sep)


# ── 7. 主入口 ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Python 包 RPM 依赖分析（PyPI + 本地取并集）")
    parser.add_argument("source_dir", help="Python 项目源码目录")
    parser.add_argument("--pkg", default="",
                        help="PyPI 包名（默认从源码目录名推断）")
    parser.add_argument("--check-rpm", action="store_true",
                        help="在容器内用 dnf 查询 RPM 可用性")
    parser.add_argument("--chroot", default=os.environ.get("COPR_CHROOT", ""),
                        help="目标构建 chroot（如 openeuler-22.03_LTS_SP2-x86_64），用于精确查询对应源")
    parser.add_argument("-o", "--output", default="",
                        help="结果输出到 JSON 文件")
    args = parser.parse_args()

    source_dir = os.path.abspath(args.source_dir)
    if not os.path.isdir(source_dir):
        print(f"[ERROR] 目录不存在: {source_dir}", file=sys.stderr)
        sys.exit(1)

    # 推断包名（剥掉 RPM python-/python3- 前缀，还原 PyPI 真实包名）
    raw_pkg_name = args.pkg or Path(source_dir).name
    pkg_name = raw_pkg_name
    for _pfx in ("python3-", "python-"):
        if pkg_name.startswith(_pfx):
            pkg_name = pkg_name[len(_pfx):]
            break
    print(f"[INFO] 分析目录: {source_dir}")
    print(f"[INFO] PyPI 包名: {pkg_name}")

    # ── 来源1：PyPI API ──
    pypi_requires: List[str] = []
    c_ext_pypi = False
    version = ""
    pypi_metadata: Dict[str, Dict] = {}
    print(f"\n[INFO] 查询 PyPI API...")
    local_version_for_pypi = _extract_local_version(source_dir)
    pypi_json = fetch_pypi_info(pkg_name, version=local_version_for_pypi or "")
    if pypi_json:
        pypi_requires, c_ext_pypi, version = parse_pypi_deps(pypi_json)
        print(f"  ✓ 获取成功，版本 {version}，{len(pypi_requires)} 个依赖")
        if c_ext_pypi:
            print("  ✓ 检测到 C 扩展（wheel 含架构标记）")
    else:
        print("  ✗ PyPI 查询失败，仅使用本地解析")

    # ── 来源2：本地源码 ──
    print(f"\n[INFO] 解析本地源码...")
    local_requires, build_backend = parse_local_deps(source_dir)
    build_sys_requires = parse_build_system_deps(source_dir)
    c_ext_local = scan_c_extensions_local(source_dir)
    print(f"  构建后端: {build_backend}，{len(local_requires)} 个依赖")
    if build_sys_requires:
        print(f"  构建系统依赖 [build-system].requires: {build_sys_requires}")

    # ── 版本一致性检查：若 PyPI 版本与本地版本不一致，放弃 PyPI 依赖 ──
    local_version = _extract_local_version(source_dir)
    if pypi_requires and local_version and version:
        def _norm_ver(v: str) -> str:
            return v.strip().lstrip("v").lower()
        if _norm_ver(version) != _norm_ver(local_version):
            print(f"  [WARN] PyPI 版本 {version} ≠ 本地版本 {local_version}，放弃 PyPI 依赖，仅使用本地解析")
            pypi_requires = []

    # ── 取并集 ──
    merged = merge_requires(pypi_requires, local_requires)
    print(f"\n[INFO] 并集合并: PyPI({len(pypi_requires)}) + 本地({len(local_requires)}) → {len(merged)} 个")

    if merged or build_sys_requires:
        print(f"\n[INFO] 收集依赖 PyPI 元数据以规范化 upstream URL...")
        pypi_metadata = collect_pypi_metadata(merged + build_sys_requires)
        print(f"  ✓ 已获取 {len(pypi_metadata)} 个依赖的 PyPI 元数据")

    # ── C 扩展链接库检测──
    # 仅当检测到本地 C 扩展时才解析链接库并查 -devel RPM，纯 Python 包跳过。
    c_libs = parse_extension_libraries(source_dir) if c_ext_local["has_c_ext"] else []
    if c_libs:
        print(f"\n[INFO] C 扩展声明链接库: {c_libs}")

    # ── dnf 查询 ──
    rpm_check = None
    build_sys_rpm_check = None
    c_library_rpm_check = None
    if args.check_rpm:
        if not merged:
            print("[INFO] 无运行时依赖，跳过 RPM 查询")
        else:
            rpm_check = check_rpm_availability(requires=merged, pypi_metadata=pypi_metadata,
                                               chroot=args.chroot or None)
        if build_sys_requires:
            print(f"\n[INFO] 查询构建系统依赖 RPM 可用性...")
            build_sys_rpm_check = check_rpm_availability(requires=build_sys_requires, pypi_metadata=pypi_metadata,
                                                         chroot=args.chroot or None)
        if c_libs:
            c_library_rpm_check = check_c_library_rpms(c_libs, chroot=args.chroot or None)

    print_report(source_dir, pkg_name, version,
                 pypi_requires, local_requires, merged, build_backend,
                 c_ext_pypi, c_ext_local, rpm_check,
                 build_sys_requires, build_sys_rpm_check)

    if args.output:
        if build_sys_rpm_check:
            build_sys_rpms = [item["rpm"] for item in build_sys_rpm_check.get("available", [])]
        else:
            build_sys_rpms = [f"python3dist({extract_pypi_name(d)})"
                              for d in (build_sys_requires or []) if extract_pypi_name(d)]
        result = {
            "pkg_name": raw_pkg_name,
            "version": version,
            "build_backend": build_backend,
            "pypi_requires": pypi_requires,
            "local_requires": local_requires,
            "merged_requires": merged,
            "dependency_items": build_dependency_items(merged, pypi_metadata),
            "build_sys_requires": build_sys_requires,
            "build_sys_dependency_items": build_dependency_items(build_sys_requires, pypi_metadata),
            "c_ext_pypi": c_ext_pypi,
            "c_ext_local": c_ext_local,
            "c_libraries": c_libs,
            "rpm_check": rpm_check,
            "build_sys_rpm_check": build_sys_rpm_check,
            "c_library_rpm_check": c_library_rpm_check,
            "build_requires": build_rpm_requires(c_ext_local, rpm_check, build_sys_rpms),
        }
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n[INFO] 结果已保存: {args.output}")

    if rpm_check and (rpm_check.get("missing") or rpm_check.get("version_conflict")):
        sys.exit(2)


if __name__ == "__main__":
    main()
