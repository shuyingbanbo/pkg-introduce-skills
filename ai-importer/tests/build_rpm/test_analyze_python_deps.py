"""analyze_python_deps.py — Python 包 RPM 依赖分析。

纯逻辑函数全测;网络(urllib)与 dnf 批量查询(run_batch_lookup)用
monkeypatch 单点替换,只测调用方逻辑,不真正发起网络 / dnf。
"""

from __future__ import annotations

import json
import sys
import types
import urllib.error
from pathlib import Path

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sys.path.insert(0, str(SCRIPT_DIRS["build_rpm"]))
# 注意:模块名故意用绝对路径。coverage>=7 把文件形式的 --cov=<file.py> 当作
# source_pkgs 模块名前缀匹配(非目录 → source_pkgs),importlib 加载的模块若用
# 短名注册,框架 __name__ 永远无法命中绝对路径前缀,导致"Module was never
# imported"、0 覆盖。以绝对路径为模块名即可让文件形式 --cov 正常收集。
_FILE = SCRIPT_DIRS["build_rpm"] / "analyze_python_deps.py"
mod = load_module(str(_FILE), _FILE)


# ─────────────────────────────────────────────
# 1. 包名规范化 / PEP508 解析
# ─────────────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("requests", "requests"),
    ("Django", "django"),
    ("python_dateutil", "python-dateutil"),
    ("a.b.c", "a-b-c"),
    ("Foo-Bar", "foo-bar"),
    ("a__b", "a-b"),
    ("a--b", "a-b"),
    ("", ""),
])
def test_normalize_pkg_name(name, expected):
    assert mod.normalize_pkg_name(name) == expected


@pytest.mark.parametrize("spec,expected", [
    ('python-dateutil>=2.7.0; python_version>="3"', "python-dateutil"),
    ("requests[security]>=2.0", "requests"),
    ("click", "click"),
    ("Django>=3.2", "django"),
    ("numpy==1.26.*", "numpy"),
    ("", ""),
    (">=1.0", ""),                      # 无包名
    ("; python_version<'3'", ""),       # marker 前为空
])
def test_extract_pypi_name(spec, expected):
    assert mod.extract_pypi_name(spec) == expected


@pytest.mark.parametrize("spec,expected", [
    ("requests>=2.0,<3", "(python3-requests >= 2.0 with python3-requests < 3)"),
    ("click", "python3-click"),
    ("requests[security]>=2.0", "python3-requests >= 2.0"),
    ("", ""),
])
def test_transform_module_name(spec, expected):
    assert mod.transform_module_name(spec) == expected


@pytest.mark.parametrize("spec,expected", [
    ("requests>=2.0,<3", ">= 2.0, < 3"),
    ("python-dateutil>=2.7.0; python_version>='3'", ">= 2.7.0"),
    ("requests[security]>=2.0", ">= 2.0"),
    ("foo!=1.2,>=2", "!= 1.2, >= 2"),
    ("foo~=3.8", "~= 3.8"),
    ("click", ""),                      # 无约束
    ("", ""),
])
def test_extract_requirement_expr(spec, expected):
    assert mod.extract_requirement_expr(spec) == expected


def test_project_url_for_pypi_name():
    assert mod.project_url_for_pypi_name("requests") == "https://pypi.org/project/requests"


# ─────────────────────────────────────────────
# 2. URL 分类规则表 / 常量
# ─────────────────────────────────────────────

def test_trusted_repo_hosts_constant():
    assert mod.TRUSTED_REPO_HOSTS == {
        "github.com", "gitlab.com", "gitee.com",
        "gitcode.com", "atomgit.com", "bitbucket.org",
    }


def test_blocked_upstream_hosts_constant():
    assert mod.BLOCKED_UPSTREAM_HOSTS == {
        "pypi.org", "test.pypi.org", "pypi.python.org",
        "pythonhosted.org", "readthedocs.io", "readthedocs.org",
    }


def test_preferred_project_url_keys_constant():
    assert mod.PREFERRED_PROJECT_URL_KEYS == [
        "source", "source code", "repository", "code", "homepage", "home",
    ]


def test_suspicious_path_segments_constant():
    assert {"issues", "releases", "pull", "blob", "tree", "docs"} <= mod.SUSPICIOUS_PATH_SEGMENTS


def test_non_repo_project_url_keys_constant():
    assert {"sponsor", "twitter", "changelog", "documentation"} <= mod.NON_REPO_PROJECT_URL_KEYS


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/org/repo", "https://github.com/org/repo"),
    ("https://www.github.com/org/repo.git", "https://github.com/org/repo"),
    ("https://gitlab.com/group/proj", "https://gitlab.com/group/proj"),
    ("https://gitee.com/org/repo/tree/main", "https://gitee.com/org/repo"),
    ("https://github.com/org", ""),                 # 只有 1 段路径
    ("https://github.com/", ""),
    ("https://example.com/org/repo", ""),           # 非可信域名
    ("ftp://github.com/org/repo", ""),              # 非 http(s)
    ("not-a-url", ""),
    (None, ""),
    ("", ""),
])
def test_normalize_repo_root(url, expected):
    assert mod.normalize_repo_root(url) == expected


def test_normalize_repo_root_ignores_depth():
    # normalize_repo_root 只取前两段,不检查深链(实际行为)
    assert mod.normalize_repo_root("https://github.com/org/repo/issues/1") == "https://github.com/org/repo"


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/org/repo", "trusted"),
    ("https://gitee.com/org/repo", "trusted"),
    ("https://gitlab.com/group/proj", "trusted"),
    ("https://bitbucket.org/org/repo", "trusted"),
    ("https://github.com/org/repo/issues/1", "suspicious"),
    ("https://github.com/org/repo/blob/main/x.py", "suspicious"),
    ("https://github.com/org/repo/wiki", "suspicious"),
    ("https://github.com/org/repo/other", "suspicious"),  # 第 3 段非白名单一律 suspicious
    ("https://pypi.org/project/foo", "suspicious"),
    ("https://readthedocs.io/en/latest", "suspicious"),
    ("https://github.com/sponsors/foo", "invalid"),       # 保留 namespace
    ("https://gitlab.com/users/foo", "invalid"),
    ("https://gitee.com/explore/x", "invalid"),
    ("https://github.com/org", "invalid"),                # 只有 1 段
    ("https://example.com/org/repo", "invalid"),          # 非可信域名
    ("github.com/org/repo", "invalid"),                   # 无协议
    ("", "invalid"),
    (None, "invalid"),
])
def test_classify_upstream_url(url, expected):
    assert mod.classify_upstream_url(url) == expected


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/org/repo", "https://github.com/org/repo"),
    ("https://github.com/org/repo/tree/main", "https://github.com/org/repo"),  # suspicious 也归约
    ("https://pypi.org/project/foo", ""),     # blocked 域名 → 归一为不可信 → 空
    ("https://example.com/a/b", ""),
    ("", ""),
])
def test_normalize_candidate_upstream(url, expected):
    assert mod.normalize_candidate_upstream(url) == expected


def test_candidate_urls_from_pypi_info_preferred_first():
    info = {
        "project_urls": {
            "Homepage": "https://github.com/org/repo",
            "Source": "https://gitlab.com/o/r",
            "Twitter": "https://twitter.com/foo",
            "Changelog": "https://github.com/org/repo/blob/main/CHANGES",
        },
    }
    got = mod.candidate_urls_from_pypi_info(info)
    # 第一轮按 PREFERRED key 顺序(source → homepage);
    # 第二轮排除 NON_REPO key(twitter/changelog),但会把 preferred key 的值重复追加(未去重,实际行为)
    assert got == [
        "https://gitlab.com/o/r",
        "https://github.com/org/repo",
        "https://github.com/org/repo",
        "https://gitlab.com/o/r",
    ]


def test_candidate_urls_from_pypi_info_home_page_and_empty():
    assert mod.candidate_urls_from_pypi_info(
        {"home_page": "https://example.com/x"}) == ["https://example.com/x"]
    assert mod.candidate_urls_from_pypi_info({}) == []
    assert mod.candidate_urls_from_pypi_info(
        {"project_urls": None, "home_page": "https://example.com/x"}) == ["https://example.com/x"]


def test_candidate_urls_skips_empty_values():
    info = {"project_urls": {"Homepage": "", "Source": None, "Docs": ""},
            "home_page": "https://example.com/x"}
    assert mod.candidate_urls_from_pypi_info(info) == ["https://example.com/x"]


def test_canonical_upstream_url_trusted():
    pypi_json = {"info": {"project_urls": {"Homepage": "https://github.com/org/repo"}}}
    assert mod.canonical_upstream_url(pypi_json, "foo") == "https://github.com/org/repo"


def test_canonical_upstream_url_deep_link_normalized():
    pypi_json = {"info": {"project_urls": {"Homepage": "https://github.com/org/repo/tree/main"}}}
    assert mod.canonical_upstream_url(pypi_json, "foo") == "https://github.com/org/repo"


def test_canonical_upstream_url_no_trusted():
    pypi_json = {"info": {"home_page": "https://pypi.org/project/foo"}}
    assert mod.canonical_upstream_url(pypi_json, "foo") == ""


def test_canonical_upstream_url_none_or_empty():
    assert mod.canonical_upstream_url(None, "foo") == ""
    assert mod.canonical_upstream_url({}, "foo") == ""


# ─────────────────────────────────────────────
# 3. build_dependency_item / build_dependency_items
# ─────────────────────────────────────────────

def test_build_dependency_item_full():
    item = mod.build_dependency_item("requests>=2.0,<3")
    assert item == {
        "name": "requests",
        "spec": "requests>=2.0,<3",
        "requirement": ">= 2.0, < 3",
        "rpm_requirement": "(python3-requests >= 2.0 with python3-requests < 3)",
        "rpm_pkg_name": "python3-requests",
        "srpm_name": "python-requests",
        "upstream_url": "",
    }


def test_build_dependency_item_simple():
    item = mod.build_dependency_item("click")
    assert item["name"] == "click"
    assert item["rpm_requirement"] == "python3-click"
    assert item["requirement"] == ""
    assert item["srpm_name"] == "python-click"


@pytest.mark.parametrize("spec", ["", ">=2.0", "; python_version<'3'"])
def test_build_dependency_item_invalid(spec):
    assert mod.build_dependency_item(spec) is None


def test_build_dependency_item_with_pypi_json():
    pypi_json = {"info": {"project_urls": {"Homepage": "https://github.com/psf/requests"}}}
    item = mod.build_dependency_item("requests", pypi_json)
    assert item["upstream_url"] == "https://github.com/psf/requests"


def test_build_dependency_items_dedup():
    items = mod.build_dependency_items(["requests>=2", "requests>=2", "click"])
    assert [i["name"] for i in items] == ["requests", "click"]


def test_build_dependency_items_same_name_diff_requirement_kept():
    items = mod.build_dependency_items(["requests", "requests>=2"])
    assert [(i["name"], i["requirement"]) for i in items] == [("requests", ""), ("requests", ">= 2")]


def test_build_dependency_items_with_metadata():
    meta = {"click": {"info": {"project_urls": {"Homepage": "https://github.com/pallets/click"}}}}
    items = mod.build_dependency_items(["click"], meta)
    assert items[0]["upstream_url"] == "https://github.com/pallets/click"


def test_build_dependency_items_empty_and_invalid():
    assert mod.build_dependency_items([]) == []
    assert mod.build_dependency_items([""]) == []
    assert mod.build_dependency_items(["requests"], None)  # pypi_metadata=None 不报错


# ─────────────────────────────────────────────
# 4. PyPI API(单点 mock urllib)
# ─────────────────────────────────────────────

class _FakeUrlResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_pypi_info_latest(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=15):
        calls.append(req.full_url)
        return _FakeUrlResponse({"info": {"name": "requests", "version": "2.31.0"}})

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    result = mod.fetch_pypi_info("requests")
    assert result["info"]["version"] == "2.31.0"
    assert calls == ["https://pypi.org/pypi/requests/json"]


def test_fetch_pypi_info_with_version_hit(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=15):
        calls.append(req.full_url)
        return _FakeUrlResponse({"info": {"version": "1.0"}})

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    result = mod.fetch_pypi_info("requests", version="1.0")
    assert result["info"]["version"] == "1.0"
    assert calls == ["https://pypi.org/pypi/requests/1.0/json"]


def test_fetch_pypi_info_with_version_falls_back_on_404(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=15):
        calls.append(req.full_url)
        if req.full_url.endswith("/1.0/json"):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", None, None)
        return _FakeUrlResponse({"info": {"version": "2.0"}})

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    result = mod.fetch_pypi_info("requests", version="1.0")
    assert result["info"]["version"] == "2.0"
    assert calls == [
        "https://pypi.org/pypi/requests/1.0/json",
        "https://pypi.org/pypi/requests/json",
    ]


def test_fetch_pypi_info_network_error_returns_none(monkeypatch):
    def fake_urlopen(req, timeout=15):
        raise OSError("connection refused")

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    assert mod.fetch_pypi_info("requests") is None


def test_fetch_pypi_info_http500_then_latest_none(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=15):
        calls.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 500, "Err", None, None)

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    assert mod.fetch_pypi_info("requests", version="1.0") is None
    assert len(calls) == 2  # 版本 URL + 最新版 URL 都失败


def test_collect_pypi_metadata_dedup(monkeypatch):
    fetched = []

    def fake_fetch(name, version=""):
        fetched.append(name)
        return {"info": {"name": name}} if name != "missing" else None

    monkeypatch.setattr(mod, "fetch_pypi_info", fake_fetch)
    meta = mod.collect_pypi_metadata(["requests>=2", "requests", "click", "", "missing"])
    assert set(meta) == {"requests", "click"}
    assert fetched == ["requests", "click", "missing"]


def test_collect_pypi_metadata_empty():
    assert mod.collect_pypi_metadata([]) == {}


def test_parse_pypi_deps_basic():
    pypi_json = {
        "info": {"version": "2.31.0", "requires_dist": [
            "urllib3>=1.21.1,<3",
            "idna>=2.5,<4",
            'charset-normalizer>=2,<4; extra == "test"',
            'socks; python_version >= "3.8"',
        ]},
        "urls": [],
    }
    requires, has_c_ext, version = mod.parse_pypi_deps(pypi_json)
    assert requires == ["urllib3>=1.21.1,<3", "idna>=2.5,<4", "socks"]
    assert has_c_ext is False
    assert version == "2.31.0"


@pytest.mark.parametrize("url,expected", [
    ("https://files.pythonhosted.org/pkg/foo-1.0-cp311-cp311-manylinux_x86_64.whl", True),
    ("https://files.pythonhosted.org/pkg/foo-1.0-cp39-abi3-win_amd64.whl", True),
    ("https://files.pythonhosted.org/pkg/foo-1.0-py3-none-any.whl", False),
    ("https://files.pythonhosted.org/pkg/foo-1.0.tar.gz", False),
])
def test_parse_pypi_deps_c_ext(url, expected):
    pypi_json = {
        "info": {"version": "1.0", "requires_dist": None},
        "urls": [{"packagetype": "bdist_wheel", "url": url}],
    }
    _, has_c_ext, _ = mod.parse_pypi_deps(pypi_json)
    assert has_c_ext is expected


def test_parse_pypi_deps_releases_keyed():
    pypi_json = {
        "info": {"version": "1.0", "requires_dist": []},
        "releases": {"1.0": [{"packagetype": "bdist_wheel",
                               "url": "https://files.pythonhosted.org/pkg/foo-1.0-cp39-abi3-win_amd64.whl"}]},
        "urls": [],
    }
    _, has_c_ext, _ = mod.parse_pypi_deps(pypi_json)
    assert has_c_ext is True


def test_parse_pypi_deps_empty_info():
    requires, has_c_ext, version = mod.parse_pypi_deps({"info": {}})
    assert requires == []
    assert has_c_ext is False
    assert version == ""


# ─────────────────────────────────────────────
# 5. 本地源码解析(tmp_path 写夹具)
# ─────────────────────────────────────────────

def test_load_toml_valid(tmp_path):
    f = tmp_path / "pyproject.toml"
    f.write_text('[project]\nname = "x"\n')
    assert mod._load_toml(f) == {"project": {"name": "x"}}


def test_load_toml_invalid(tmp_path, capsys):
    f = tmp_path / "pyproject.toml"
    f.write_text("not valid toml [[[\n")
    assert mod._load_toml(f) == {}
    assert "WARN" in capsys.readouterr().err


def test_load_toml_missing_file(tmp_path, capsys):
    assert mod._load_toml(tmp_path / "nope.toml") == {}
    assert "WARN" in capsys.readouterr().err


def test_parse_local_deps_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "1.0"\ndependencies = ["requests>=2.0", "click<9"]\n'
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n')
    deps, backend = mod.parse_local_deps(str(tmp_path))
    assert deps == ["requests>=2.0", "click<9"]
    assert backend == "hatchling"


@pytest.mark.parametrize("backend_full,expected", [
    ("hatchling.build", "hatchling"),
    ("setuptools.build_meta", "setuptools"),
    ("flit_core.buildapi", "flit"),
    ("poetry.core.masonry.api", "poetry"),
    ("pdm.backend", "pdm"),
    ("mesonpy", "meson-python"),
    ("unknown.backend", "setuptools"),
])
def test_parse_local_deps_backend_map(tmp_path, backend_full, expected):
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname="x"\ndependencies = ["requests"]\n'
        f'[build-system]\nbuild-backend = "{backend_full}"\n')
    deps, backend = mod.parse_local_deps(str(tmp_path))
    assert deps == ["requests"]
    assert backend == expected


def test_parse_local_deps_pyproject_no_project_falls_back_to_setup_py(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n')
    (tmp_path / "setup.py").write_text(
        "from setuptools import setup\nsetup(install_requires=['requests>=2.0'])\n")
    deps, backend = mod.parse_local_deps(str(tmp_path))
    assert deps == ["requests>=2.0"]
    assert backend == "setuptools"


def test_parse_local_deps_pyproject_project_without_deps_no_fallback(tmp_path):
    # [project] 存在但无 dependencies → 直接返回空,不再回退 setup.py
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "1.0"\n')
    (tmp_path / "setup.py").write_text("setup(install_requires=['requests'])\n")
    deps, backend = mod.parse_local_deps(str(tmp_path))
    assert deps == []
    assert backend == "setuptools"


def test_parse_local_deps_dynamic_dependencies_warn(tmp_path, capsys):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndynamic = ["dependencies"]\n')
    deps, backend = mod.parse_local_deps(str(tmp_path))
    assert deps == []
    assert backend == "setuptools"
    assert "dynamic" in capsys.readouterr().err


def test_parse_local_deps_invalid_toml_falls_back(tmp_path):
    (tmp_path / "pyproject.toml").write_text("not valid toml [[[\n")
    (tmp_path / "requirements.txt").write_text("click\n")
    deps, backend = mod.parse_local_deps(str(tmp_path))
    assert deps == ["click"]
    assert backend == "setuptools"


def test_parse_local_deps_setup_py_extras(tmp_path):
    (tmp_path / "setup.py").write_text(
        "setup(name='demo',\n"
        "    install_requires=[\n"
        "        'requests>=2.0',\n"
        "        'ray[default]>=1.0',\n"
        "        'flask',\n"
        "    ],\n"
        ")\n")
    deps, backend = mod.parse_local_deps(str(tmp_path))
    assert deps == ["requests>=2.0", "ray[default]>=1.0", "flask"]
    assert backend == "setuptools"


def test_parse_local_deps_setup_py_backend_poetry(tmp_path):
    (tmp_path / "setup.py").write_text("import poetry\nsetup(install_requires=['x'])\n")
    _, backend = mod.parse_local_deps(str(tmp_path))
    assert backend == "poetry"


def test_parse_local_deps_setup_py_backend_flit(tmp_path):
    (tmp_path / "setup.py").write_text("from flit import x\nsetup(install_requires=['x'])\n")
    _, backend = mod.parse_local_deps(str(tmp_path))
    assert backend == "flit"


def test_parse_local_deps_setup_py_placeholder_warn(tmp_path, capsys):
    (tmp_path / "setup.py").write_text(
        "setup(install_requires=['numpy >= {}', 'click'])\n")
    deps, backend = mod.parse_local_deps(str(tmp_path))
    assert deps == ["numpy >= {}", "click"]
    assert "占位符" in capsys.readouterr().err


def test_parse_local_deps_setup_py_empty_falls_to_requirements(tmp_path):
    (tmp_path / "setup.py").write_text("setup(install_requires=[])\n")
    (tmp_path / "requirements.txt").write_text("click\n")
    deps, backend = mod.parse_local_deps(str(tmp_path))
    assert deps == ["click"]
    assert backend == "setuptools"


def test_parse_local_deps_setup_py_parentheses_not_supported(tmp_path):
    # 已知局限:元组写法 install_requires=('a',) 不解析(只找 '['),回退 requirements.txt
    (tmp_path / "setup.py").write_text("setup(install_requires=('requests',))\n")
    (tmp_path / "requirements.txt").write_text("click\n")
    deps, backend = mod.parse_local_deps(str(tmp_path))
    assert deps == ["click"]


def test_parse_local_deps_requirements_txt(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "# comment\nrequests>=2.0\n\nclick==8.1.7  # inline comment\n"
        "-r other.txt\n--index-url https://pypi.org/simple\n")
    deps, backend = mod.parse_local_deps(str(tmp_path))
    assert deps == ["requests>=2.0", "click==8.1.7"]
    assert backend == "setuptools"


def test_parse_local_deps_empty_dir(tmp_path):
    deps, backend = mod.parse_local_deps(str(tmp_path))
    assert deps == []
    assert backend == "setuptools"


def test_parse_build_system_deps(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["hatchling", "hatch-vcs>=0.3"]\nbuild-backend = "hatchling.build"\n')
    assert mod.parse_build_system_deps(str(tmp_path)) == ["hatchling", "hatch-vcs>=0.3"]


def test_parse_build_system_deps_missing(tmp_path):
    assert mod.parse_build_system_deps(str(tmp_path)) == []


def test_parse_build_system_deps_invalid_toml(tmp_path):
    (tmp_path / "pyproject.toml").write_text("###bad\n")
    assert mod.parse_build_system_deps(str(tmp_path)) == []


def test_scan_c_extensions_local_pyx_and_c(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "module.pyx").write_text("cdef int x")
    (pkg / "core.c").write_text("int main(){}")
    (pkg / "util.cpp").write_text("void f(){}")
    result = mod.scan_c_extensions_local(str(tmp_path))
    assert result["has_c_ext"] is True
    assert result["pyx_files"] == ["pkg/module.pyx"]
    assert result["c_files"] == ["pkg/core.c", "pkg/util.cpp"]


def test_scan_c_extensions_local_setup_extension(tmp_path):
    (tmp_path / "setup.py").write_text(
        "from setuptools import Extension\nsetup(ext_modules=[Extension('foo', sources=['a.c'])])\n")
    result = mod.scan_c_extensions_local(str(tmp_path))
    assert result["has_c_ext"] is True
    assert any("Extension()" in r for r in result["reasons"])


def test_scan_c_extensions_local_none(tmp_path):
    (tmp_path / "setup.py").write_text("setup(name='x')\n")
    assert mod.scan_c_extensions_local(str(tmp_path)) == {
        "has_c_ext": False, "reasons": [], "pyx_files": [], "c_files": []}


def test_parse_extension_libraries_setup_and_pyx(tmp_path):
    (tmp_path / "setup.py").write_text(
        "ext = Extension('x', libraries=['pq', 'ssl', 'm'])\n"
        "ext2 = Extension('y', libraries=[\"z\", 'pthread'])\n")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mod.pyx").write_text(
        "# distutils: libraries = pq zstd bz2\n# cython: language_level=3\ncdef foo()\n")
    libs = mod.parse_extension_libraries(str(tmp_path))
    # 内置库 m / pthread 被过滤,结果排序
    assert libs == ["bz2", "pq", "ssl", "z", "zstd"]


def test_parse_extension_libraries_empty(tmp_path):
    assert mod.parse_extension_libraries(str(tmp_path)) == []


def test_parse_extension_libraries_all_builtins(tmp_path):
    (tmp_path / "setup.py").write_text("Extension('x', libraries=['m', 'c', 'pthread'])\n")
    assert mod.parse_extension_libraries(str(tmp_path)) == []


def test_parse_extension_libraries_pyx_read_error(tmp_path):
    # .pyx 路径是目录时 read_text 抛 OSError → 跳过该文件(实际行为)
    (tmp_path / "broken.pyx").mkdir()
    (tmp_path / "setup.py").write_text("Extension('x', libraries=['pq'])\n")
    assert mod.parse_extension_libraries(str(tmp_path)) == ["pq"]


# ─────────────────────────────────────────────
# 6. 依赖并集合并
# ─────────────────────────────────────────────

@pytest.mark.parametrize("dep,expected", [
    ("requests>=2.0", "requests"),
    ("requests<=2.0", "requests"),
    ("requests!=1.5", "requests"),
    ("requests==2.0", "requests"),
    ("requests>2", "requests"),
    ("requests<3", "requests"),
    ("ray[default]>=1.0", "ray"),
    ("Flask>=1.0; python_version<'3.8'", "flask"),
    ("  click  ", "click"),
])
def test_extract_pkg_key(dep, expected):
    assert mod._extract_pkg_key(dep) == expected


def test_extract_local_version_pyproject_static(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\nversion = "1.2.3"\n')
    assert mod._extract_local_version(str(tmp_path)) == "1.2.3"


def test_extract_local_version_poetry(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\nversion = "attr: x.__version__"\n'
        '[tool.poetry]\nversion = "2.0.0"\n')
    assert mod._extract_local_version(str(tmp_path)) == "2.0.0"


def test_extract_local_version_setuptools_dynamic_attr_init(tmp_path):
    pkg = tmp_path / "demo"
    pkg.mkdir()
    (pkg / "__init__.py").write_text('__version__ = "3.4.5"\n')
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\nversion = "attr: x.__version__"\n'
        '[tool.setuptools.dynamic]\nversion = {attr = "demo.__version__"}\n')
    assert mod._extract_local_version(str(tmp_path)) == "3.4.5"


def test_extract_local_version_setuptools_dynamic_attr_version_py(tmp_path):
    pkg = tmp_path / "demo"
    pkg.mkdir()
    (pkg / "__version__.py").write_text('__version__ = "1.1.1"\n')
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\nversion = "attr: x.__version__"\n'
        '[tool.setuptools.dynamic]\nversion = {attr = "demo.__version__"}\n')
    assert mod._extract_local_version(str(tmp_path)) == "1.1.1"


def test_extract_local_version_setup_cfg(tmp_path):
    (tmp_path / "setup.cfg").write_text("[metadata]\nname = x\nversion = 2.5.0\n")
    assert mod._extract_local_version(str(tmp_path)) == "2.5.0"


def test_extract_local_version_setup_cfg_attr(tmp_path):
    pkg = tmp_path / "demo"
    pkg.mkdir()
    (pkg / "__init__.py").write_text('__version__ = "4.0.0"\n')
    (tmp_path / "setup.cfg").write_text("[metadata]\nversion = attr: demo.__version__\n")
    assert mod._extract_local_version(str(tmp_path)) == "4.0.0"


def test_extract_local_version_setup_cfg_attr_version_py(tmp_path):
    pkg = tmp_path / "demo"
    pkg.mkdir()
    (pkg / "__version__.py").write_text('__version__ = "0.9.9"\n')
    (tmp_path / "setup.cfg").write_text("[metadata]\nversion = attr: demo.__version__\n")
    assert mod._extract_local_version(str(tmp_path)) == "0.9.9"


def test_extract_local_version_setup_cfg_attr_module_py(tmp_path):
    (tmp_path / "demo.py").write_text('__version__ = "0.8.8"\n')
    (tmp_path / "setup.cfg").write_text("[metadata]\nversion = attr: demo.__version__\n")
    assert mod._extract_local_version(str(tmp_path)) == "0.8.8"


def test_extract_local_version_files(tmp_path):
    (tmp_path / "VERSION").write_text("7.8.9\n")
    assert mod._extract_local_version(str(tmp_path)) == "7.8.9"
    (tmp_path / "VERSION").unlink()
    (tmp_path / "version.txt").write_text("0.1.0\n")
    assert mod._extract_local_version(str(tmp_path)) == "0.1.0"


def test_extract_local_version_init_scan(tmp_path):
    pkg = tmp_path / "celery"
    pkg.mkdir()
    (pkg / "__init__.py").write_text('__version__ = "5.3.0"\n')
    assert mod._extract_local_version(str(tmp_path)) == "5.3.0"


def test_extract_local_version_skips_hidden_dir(tmp_path):
    h = tmp_path / ".hidden"
    h.mkdir()
    (h / "__init__.py").write_text('__version__ = "9.9.9"\n')
    assert mod._extract_local_version(str(tmp_path)) == ""


def test_extract_local_version_none(tmp_path):
    assert mod._extract_local_version(str(tmp_path)) == ""


@pytest.mark.parametrize("dep,expected", [
    ("requests>=2.0,<3", ">=2.0,<3"),
    ("foo<=1.0", "<=1.0"),
    ("foo!=1.2", "!=1.2"),
    ("foo==2.0", "==2.0"),
    ("foo>1.0", ">1.0"),
    ("foo<1.0", "<1.0"),
    ("foo~=3.8", "~=3.8"),
    ("click", ""),
    # 已知 bug 固化:带 extras 时约束被 .split("[")[0] 截断丢失,
    # 生产修复应为 name_part 取 split("[")[0] 前先按第一个操作符切分
    ("ray[default]>=1.0", ""),
])
def test_extract_version_constraint(dep, expected):
    assert mod._extract_version_constraint(dep) == expected


@pytest.mark.parametrize("spec,expected", [
    (">= {}", True),
    ("numpy >={}", True),
    ("foo < {}", True),
    ("==  {}", True),
    ("~={}", True),
    (">=1.0", False),
    ("", False),
    ("{}", False),          # 无操作符
])
def test_constraint_is_broken(spec, expected):
    assert mod._constraint_is_broken(spec) == expected


def test_merge_requires_local_priority():
    pypi = ["requests>=2.0", "click>=8.0"]
    local = ["requests>=2.31", "flask"]
    assert mod.merge_requires(pypi, local) == ["requests>=2.31", "flask", "click>=8.0"]


def test_merge_requires_empty_sides():
    assert mod.merge_requires([], ["requests"]) == ["requests"]
    assert mod.merge_requires(["requests>=2"], []) == ["requests>=2"]
    assert mod.merge_requires([], []) == []


def test_merge_requires_broken_local_replaced_by_pypi():
    assert mod.merge_requires(["numpy>=1.21"], ["numpy >={}"]) == ["numpy>=1.21"]


def test_merge_requires_broken_local_without_pypi_kept():
    assert mod.merge_requires([], ["numpy >={}"]) == ["numpy >={}"]


def test_merge_requires_name_normalization_dedup():
    # 大小写/分隔符不同的同一包名视为同一 key,本地优先
    assert mod.merge_requires(["django>=4.0"], ["Django>=3.2"]) == ["Django>=3.2"]


def test_merge_requires_skips_empty_pypi_key():
    # PyPI 侧解析不出包名的条目直接跳过
    assert mod.merge_requires(["", ">=1.0", "click"], ["requests"]) == ["requests", "click"]


# ─────────────────────────────────────────────
# 7. build_lookup_tasks
# ─────────────────────────────────────────────

def test_build_lookup_tasks():
    tasks = mod.build_lookup_tasks(["requests>=2.0", "click"])
    assert len(tasks) == 2
    t0 = tasks[0]
    assert t0["dep"] == "requests>=2.0"
    assert t0["name"] == "requests"
    assert t0["requirement"] == ">= 2.0"
    assert t0["rpm_requirement"] == "python3-requests >= 2.0"
    assert t0["rpm_name"] == "python3dist(requests)"
    assert t0["queries"] == [
        {"kind": "provides", "value": "python3dist(requests)", "level": "python3dist()"}]
    assert tasks[1]["rpm_name"] == "python3dist(click)"
    assert tasks[1]["requirement"] == ""


def test_build_lookup_tasks_invalid_skipped():
    assert mod.build_lookup_tasks(["", ">=2.0"]) == []


def test_build_lookup_tasks_with_metadata():
    meta = {"click": {"info": {"project_urls": {"Homepage": "https://github.com/pallets/click"}}}}
    tasks = mod.build_lookup_tasks(["click"], meta)
    assert tasks[0]["upstream_url"] == "https://github.com/pallets/click"


# ─────────────────────────────────────────────
# 8. check_rpm_availability(单点 mock run_batch_lookup + check_existing_package)
# ─────────────────────────────────────────────

def _stub_check_existing_package(monkeypatch, evaluate_result=True, raises=False):
    """向 sys.modules 注入假 check_existing_package(懒加载 import_module 命中)。"""
    stub = types.ModuleType("check_existing_package")
    if raises:
        def _boom(req):
            raise RuntimeError("boom")
        stub.parse_requirement = _boom
    else:
        stub.parse_requirement = lambda req: {"status": "parsed", "clauses": []}
    stub.evaluate_requirement = lambda version, req_info: evaluate_result
    monkeypatch.setitem(sys.modules, "check_existing_package", stub)
    return stub


def _found_item(dep, requirement, version="2.31.0"):
    name = mod.extract_pypi_name(dep)
    return {
        "dep": dep, "name": name, "requirement": requirement,
        "rpm_requirement": f"python3-{name}",
        "rpm_name": f"python3dist({name})",
        "rpm": f"python3-{name}", "version": version, "release": "1",
        "upstream_url": "",
    }


def test_check_rpm_availability_available(monkeypatch):
    _stub_check_existing_package(monkeypatch, evaluate_result=True)
    results = [_found_item("requests>=2.0", ">= 2.0")]
    monkeypatch.setattr(mod, "run_batch_lookup",
                        lambda tasks, timeout=120, chroot=None: results)
    out = mod.check_rpm_availability(["requests>=2.0"])
    assert out["available"][0]["rpm"] == "python3-requests"
    assert out["available"][0]["version"] == "2.31.0"
    assert out["available"][0]["release"] == "1"
    assert out["missing"] == []
    assert out["version_conflict"] == []


def test_check_rpm_availability_missing(monkeypatch):
    _stub_check_existing_package(monkeypatch)
    results = [{
        "dep": "click", "name": "click", "requirement": "",
        "rpm_requirement": "python3-click", "rpm_name": "python3dist(click)",
        "rpm": None, "version": None, "release": None, "upstream_url": "",
    }]
    monkeypatch.setattr(mod, "run_batch_lookup",
                        lambda tasks, timeout=120, chroot=None: results)
    out = mod.check_rpm_availability(["click"])
    assert out["available"] == []
    assert out["missing"][0]["dep"] == "click"
    assert out["missing"][0]["rpm_name"] == "python3dist(click)"
    assert out["version_conflict"] == []


def test_check_rpm_availability_version_conflict(monkeypatch):
    _stub_check_existing_package(monkeypatch, evaluate_result=False)
    results = [_found_item("requests>=3.0", ">= 3.0")]
    monkeypatch.setattr(mod, "run_batch_lookup",
                        lambda tasks, timeout=120, chroot=None: results)
    out = mod.check_rpm_availability(["requests>=3.0"])
    assert out["available"] == []
    assert out["version_conflict"][0]["rpm"] == "python3-requests"
    assert out["version_conflict"][0]["found_version"] == "2.31.0"
    assert out["missing"] == []


def test_check_rpm_availability_evaluate_error_treated_ok(monkeypatch):
    # 版本比较抛异常 → except 吞掉,按满足处理
    _stub_check_existing_package(monkeypatch, raises=True)
    results = [_found_item("requests>=2.0", ">= 2.0")]
    monkeypatch.setattr(mod, "run_batch_lookup",
                        lambda tasks, timeout=120, chroot=None: results)
    out = mod.check_rpm_availability(["requests>=2.0"])
    assert out["available"][0]["rpm"] == "python3-requests"
    assert out["version_conflict"] == []


def test_check_rpm_availability_lookup_failure_fallback(monkeypatch):
    def _raise(tasks, timeout=120, chroot=None):
        raise mod.BatchLookupError("dnf failed")
    monkeypatch.setattr(mod, "run_batch_lookup", _raise)
    out = mod.check_rpm_availability(["requests"])
    assert out["available"] == []
    assert [m["dep"] for m in out["missing"]] == ["requests"]
    assert out["version_conflict"] == []


def test_check_rpm_availability_passes_chroot(monkeypatch):
    _stub_check_existing_package(monkeypatch)
    captured = {}

    def fake_lookup(tasks, timeout=120, chroot=None):
        captured["chroot"] = chroot
        return mod.fallback_results(tasks)

    monkeypatch.setattr(mod, "run_batch_lookup", fake_lookup)
    mod.check_rpm_availability(["click"], chroot="openeuler-24.03_LTS-x86_64")
    assert captured["chroot"] == "openeuler-24.03_LTS-x86_64"


def test_check_rpm_availability_no_requires():
    assert mod.check_rpm_availability(None) == {"available": [], "missing": [], "version_conflict": []}


def test_check_rpm_availability_readds_script_dir(monkeypatch):
    # scripts 目录不在 sys.path 时,函数内自行补回(check_existing_package 懒加载路径)
    _stub_check_existing_package(monkeypatch)
    monkeypatch.setattr(mod, "run_batch_lookup", lambda tasks, timeout=120, chroot=None: [])
    script_dir = str(SCRIPT_DIRS["build_rpm"])
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != script_dir])
    out = mod.check_rpm_availability(["click"])
    assert script_dir in sys.path  # 被重新插入
    assert out == {"available": [], "missing": [], "version_conflict": []}


# ─────────────────────────────────────────────
# 9. check_c_library_rpms
# ─────────────────────────────────────────────

def test_check_c_library_rpms_empty():
    assert mod.check_c_library_rpms([]) == {"available": [], "missing": []}
    assert mod.check_c_library_rpms(None) == {"available": [], "missing": []}


def test_check_c_library_rpms_available_and_missing(monkeypatch):
    results = [
        {"dep": "pq", "rpm": "libpq-devel", "level": "pkgconfig()"},
        {"dep": "zstd", "rpm": None},
    ]
    monkeypatch.setattr(mod, "run_batch_lookup",
                        lambda tasks, timeout=120, chroot=None: results)
    out = mod.check_c_library_rpms(["pq", "zstd"])
    assert out["available"] == [{"lib": "pq", "rpm": "libpq-devel", "level": "pkgconfig()"}]
    assert out["missing"] == [{"lib": "zstd"}]


def test_check_c_library_rpms_lookup_failure(monkeypatch):
    def _raise(tasks, timeout=120, chroot=None):
        raise mod.BatchLookupError("boom")
    monkeypatch.setattr(mod, "run_batch_lookup", _raise)
    out = mod.check_c_library_rpms(["pq", "ssl"])
    assert out["available"] == []
    assert [m["lib"] for m in out["missing"]] == ["pq", "ssl"]


# ─────────────────────────────────────────────
# 10. build_rpm_requires / print_report
# ─────────────────────────────────────────────

def test_build_rpm_requires_base():
    c_ext = {"has_c_ext": False}
    assert mod.build_rpm_requires(c_ext, None) == [
        "python3-devel", "python3-setuptools", "python3-pip", "python3-wheel"]


def test_build_rpm_requires_c_ext_with_pyx():
    c_ext = {"has_c_ext": True, "pyx_files": ["a.pyx"]}
    br = mod.build_rpm_requires(c_ext, None)
    assert "gcc" in br
    assert "python3-Cython" in br


def test_build_rpm_requires_c_ext_no_pyx():
    c_ext = {"has_c_ext": True, "pyx_files": []}
    br = mod.build_rpm_requires(c_ext, None)
    assert "gcc" in br
    assert "python3-Cython" not in br


def test_build_rpm_requires_build_sys_and_available():
    c_ext = {"has_c_ext": False}
    rpm_check = {"available": [{"rpm": "python3-requests"}, {"rpm": "python3-devel"}]}
    br = mod.build_rpm_requires(c_ext, rpm_check, build_sys_rpms=["python3-hatchling"])
    assert br[:4] == ["python3-devel", "python3-setuptools", "python3-pip", "python3-wheel"]
    assert "python3-hatchling" in br
    assert "python3-requests" in br
    assert br.count("python3-devel") == 1   # 去重


def test_print_report_basic(capsys):
    mod.print_report(
        source_dir="/src", pkg_name="demo", version="1.0",
        pypi_requires=["requests>=2.0"], local_requires=["requests>=2.0", "click"],
        merged_requires=["requests>=2.0", "click"], build_backend="setuptools",
        c_ext_pypi=False, c_ext_local={"has_c_ext": False, "reasons": [], "pyx_files": [], "c_files": []},
        rpm_check=None, build_sys_requires=[], build_sys_rpm_check=None)
    out = capsys.readouterr().out
    assert "Python 包 RPM 依赖分析报告" in out
    assert "demo" in out
    assert "[PyPI] requests>=2.0" in out
    assert "[本地] click" in out
    assert "纯 Python 包，无 C 扩展" in out
    assert "BuildRequires: python3-devel" in out


def test_print_report_with_rpm_check_and_build_sys(capsys):
    rpm_check = {
        "available": [{"dep": "requests>=2.0", "rpm": "python3-requests"}],
        "missing": [{"dep": "click", "rpm_name": "python3dist(click)"}],
    }
    bs_check = {
        "available": [{"dep": "hatchling", "rpm": "python3-hatchling"}],
        "missing": [{"dep": "hatch-vcs", "rpm": "python3dist(hatch-vcs)"}],
    }
    mod.print_report(
        source_dir="/src", pkg_name="demo", version="1.0",
        pypi_requires=[], local_requires=[], merged_requires=[],
        build_backend="hatchling",
        c_ext_pypi=True,
        c_ext_local={"has_c_ext": True,
                     "reasons": ["发现 1 个 .pyx 文件（Cython）"],
                     "pyx_files": ["a.pyx"], "c_files": []},
        rpm_check=rpm_check,
        build_sys_requires=["hatchling", "hatch-vcs"],
        build_sys_rpm_check=bs_check)
    out = capsys.readouterr().out
    assert "已有 1 个 / 缺失 1 个" in out
    assert "✓ requests>=2.0" in out
    assert "✗ 缺失" in out
    assert "✓ hatchling" in out
    assert "✗ hatch-vcs" in out and "未找到" in out
    assert "BuildRequires: python3-hatchling" in out
    assert "BuildRequires: gcc" in out
    assert "BuildRequires: python3-Cython" in out
    assert "PyPI wheel 含架构标记" in out


def test_print_report_build_sys_unqueried(capsys):
    # build_sys_rpm_check 为 None → 构建系统依赖显示 "?" 状态
    mod.print_report(
        source_dir="/src", pkg_name="demo", version="1.0",
        pypi_requires=[], local_requires=[], merged_requires=[],
        build_backend="setuptools",
        c_ext_pypi=False, c_ext_local={"has_c_ext": False, "reasons": [], "pyx_files": [], "c_files": []},
        rpm_check=None, build_sys_requires=["hatchling"], build_sys_rpm_check=None)
    out = capsys.readouterr().out
    assert "? hatchling" in out
    assert "BuildRequires: python3dist(hatchling)" in out


# ─────────────────────────────────────────────
# 11. main(全链路 mock,不发网络)
# ─────────────────────────────────────────────

def test_main_pypi_fails_local_only(tmp_path, monkeypatch, capsys):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "1.0"\ndependencies = ["click"]\n')
    monkeypatch.setattr(mod, "fetch_pypi_info", lambda pkg_name, version="": None)
    monkeypatch.setattr(sys, "argv",
                        ["analyze_python_deps.py", str(tmp_path), "--pkg", "demo"])
    mod.main()
    out = capsys.readouterr().out
    assert "PyPI 查询失败，仅使用本地解析" in out
    assert "[本地] click" in out


def test_main_with_pypi_and_output_json(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "1.0"\ndependencies = ["requests>=2.0"]\n')
    pypi_json = {
        "info": {
            "name": "demo", "version": "1.0",
            "requires_dist": ["requests>=2.0", "urllib3<2"],
            "project_urls": {"Homepage": "https://github.com/org/demo"},
        },
        "urls": [],
    }
    monkeypatch.setattr(mod, "fetch_pypi_info", lambda pkg_name, version="": pypi_json)
    out_file = tmp_path / "result.json"
    monkeypatch.setattr(sys, "argv",
                        ["analyze_python_deps.py", str(tmp_path), "--pkg", "demo",
                         "-o", str(out_file)])
    mod.main()
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["pkg_name"] == "demo"
    assert data["version"] == "1.0"
    assert data["build_backend"] == "setuptools"
    assert "urllib3<2" in data["pypi_requires"]
    assert data["merged_requires"] == ["requests>=2.0", "urllib3<2"]
    assert data["dependency_items"][0]["name"] == "requests"
    assert data["rpm_check"] is None
    assert "python3-devel" in data["build_requires"]


def test_main_version_mismatch_drops_pypi(tmp_path, monkeypatch, capsys):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "2.0.0"\ndependencies = ["click"]\n')
    pypi_json = {"info": {"name": "demo", "version": "1.0.0",
                           "requires_dist": ["requests"]}, "urls": []}
    monkeypatch.setattr(mod, "fetch_pypi_info", lambda pkg_name, version="": pypi_json)
    monkeypatch.setattr(sys, "argv",
                        ["analyze_python_deps.py", str(tmp_path), "--pkg", "demo"])
    mod.main()
    out = capsys.readouterr().out
    assert "放弃 PyPI 依赖" in out
    # PyPI 依赖被丢弃后只剩本地 click,requests 不再出现在合并列表
    assert "并集合并: PyPI(0) + 本地(1) → 1" in out
    assert "[本地] click" in out


def test_main_pkg_prefix_strip(tmp_path, monkeypatch, capsys):
    (tmp_path / "pyproject.toml").write_text('[project]\nname="demo"\nversion="1.0"\n')
    monkeypatch.setattr(mod, "fetch_pypi_info", lambda pkg_name, version="": None)
    monkeypatch.setattr(sys, "argv",
                        ["analyze_python_deps.py", str(tmp_path), "--pkg", "python3-demo"])
    mod.main()
    assert "PyPI 包名: demo" in capsys.readouterr().out


def test_main_check_rpm_available_exits_0(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "1.0"\ndependencies = ["click"]\n')
    monkeypatch.setattr(mod, "fetch_pypi_info", lambda pkg_name, version="": None)

    def fake_lookup(tasks, timeout=120, chroot=None):
        return [{**{k: v for k, v in t.items() if k != "queries"},
                 "rpm": "python3-click", "version": "8.1.7",
                 "release": "1", "level": "provides"} for t in tasks]

    monkeypatch.setattr(mod, "run_batch_lookup", fake_lookup)
    monkeypatch.setattr(sys, "argv",
                        ["analyze_python_deps.py", str(tmp_path), "--pkg", "demo",
                         "--check-rpm"])
    mod.main()  # 无 SystemExit → 退出码 0


def test_main_check_rpm_missing_exits_2(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "1.0"\ndependencies = ["click"]\n')
    monkeypatch.setattr(mod, "fetch_pypi_info", lambda pkg_name, version="": None)
    monkeypatch.setattr(mod, "run_batch_lookup",
                        lambda tasks, timeout=120, chroot=None: mod.fallback_results(tasks))
    monkeypatch.setattr(sys, "argv",
                        ["analyze_python_deps.py", str(tmp_path), "--pkg", "demo",
                         "--check-rpm"])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 2


def test_main_missing_dir_exits_1(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv",
                        ["analyze_python_deps.py", str(tmp_path / "nope"), "--pkg", "demo"])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1


def test_main_c_ext_pypi_and_build_sys(tmp_path, monkeypatch, capsys):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "1.0"\ndependencies = []\n'
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n')
    pypi_json = {
        "info": {"name": "demo", "version": "1.0", "requires_dist": [], "project_urls": {}},
        "urls": [{"packagetype": "bdist_wheel",
                  "url": "https://files.pythonhosted.org/pkg/demo-1.0-cp311-cp311-manylinux_x86_64.whl"}],
    }
    monkeypatch.setattr(mod, "fetch_pypi_info", lambda pkg_name, version="": pypi_json)
    monkeypatch.setattr(sys, "argv", ["analyze_python_deps.py", str(tmp_path), "--pkg", "demo"])
    mod.main()
    out = capsys.readouterr().out
    assert "检测到 C 扩展（wheel 含架构标记）" in out
    assert "构建系统依赖 [build-system].requires" in out


def test_main_check_rpm_no_deps_skips(tmp_path, monkeypatch, capsys):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "1.0"\n')
    monkeypatch.setattr(mod, "fetch_pypi_info", lambda pkg_name, version="": None)
    monkeypatch.setattr(sys, "argv",
                        ["analyze_python_deps.py", str(tmp_path), "--pkg", "demo", "--check-rpm"])
    mod.main()
    assert "无运行时依赖，跳过 RPM 查询" in capsys.readouterr().out


def test_main_check_rpm_build_sys_and_c_libs(tmp_path, monkeypatch, capsys):
    # --check-rpm + [build-system].requires + 本地 C 扩展链接库,三类查询全走
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "1.0"\ndependencies = ["click"]\n'
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n')
    (tmp_path / "setup.py").write_text("Extension('x', libraries=['pq'])\n")
    monkeypatch.setattr(mod, "fetch_pypi_info", lambda pkg_name, version="": None)

    def fake_lookup(tasks, timeout=120, chroot=None):
        return [{**{k: v for k, v in t.items() if k != "queries"},
                 "rpm": f"{t['name']}-devel", "version": "1.0",
                 "release": "1", "level": "pkgconfig()"} for t in tasks]

    monkeypatch.setattr(mod, "run_batch_lookup", fake_lookup)
    out_file = tmp_path / "result.json"
    monkeypatch.setattr(sys, "argv",
                        ["analyze_python_deps.py", str(tmp_path), "--pkg", "demo",
                         "--check-rpm", "-o", str(out_file)])
    mod.main()  # 全部 available → 不抛 SystemExit
    out = capsys.readouterr().out
    assert "C 扩展声明链接库: ['pq']" in out
    assert "查询构建系统依赖 RPM 可用性" in out
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["build_sys_dependency_items"][0]["name"] == "hatchling"
    assert data["build_sys_rpm_check"]["available"][0]["rpm"] == "hatchling-devel"
    assert data["c_libraries"] == ["pq"]
    assert data["c_library_rpm_check"]["available"] == [
        {"lib": "pq", "rpm": "pq-devel", "level": "pkgconfig()"}]
    assert "click-devel" in data["build_requires"]
    assert "hatchling-devel" in data["build_requires"]
    assert "gcc" in data["build_requires"]
