"""check_existing_package.py — 官方源/COPR project 复用决策。

纯函数(版本比较/约束求值/候选名构造)直接断言;I/O 层(dnf repoquery、
COPR API、repo 文件写入)经 mock 后测主流程与决策逻辑。
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
from pathlib import PosixPath

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sys.path.insert(0, str(SCRIPT_DIRS["build_rpm"]))
mod = load_module("check_existing_package", SCRIPT_DIRS["build_rpm"] / "check_existing_package.py")


# ─────────────────────────────────────────────
# 版本比较
# ─────────────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("Requests", "requests"),
    ("Foo-Bar.Baz", "foo_bar_baz"),
    ("", ""),
    ("A__B", "a_b"),
    ("python3.x", "python3_x"),
])
def test_normalize_name_token(name, expected):
    assert mod.normalize_name_token(name) == expected


@pytest.mark.parametrize("version,expected", [
    ("1.2.3", [1, 2, 3]),
    ("1.2rc1", [1, 2, "rc", 1]),
    ("", []),
    ("2024.1", [2024, 1]),
    ("v2.3", ["v", 2, 3]),
    ("1.0-2.fc40", [1, 0, 2, "fc", 40]),
])
def test_split_version_tokens(version, expected):
    assert mod.split_version_tokens(version) == expected


@pytest.mark.parametrize("left,right,expected", [
    ("1.0", "1.0", 0),
    ("1.2", "1.10", -1),      # 数字段按数值比较
    ("1.10", "1.2", 1),
    ("2.0", "1.9", 1),
    ("1.0", "1.0.0", 0),      # 尾部补零相等
    ("1.0", "1.0.1", -1),
    ("1.0rc1", "1.0", 1),     # 字符串段 > 缺失段(缺失按 0 数字处理)
    ("1.0", "1.0rc1", -1),
    ("abc", "abd", -1),
    ("ABC", "abc", 0),        # 字母统一小写
])
def test_compare_versions(left, right, expected):
    assert mod.compare_versions(left, right) == expected


# ─────────────────────────────────────────────
# 约束解析与求值
# ─────────────────────────────────────────────

@pytest.mark.parametrize("req,status,operator,version,nclauses", [
    ("", "none", None, None, 0),
    ("   ", "none", None, None, 0),
    (">= 2.0", "parsed", ">=", "2.0", 1),
    ("==1.2.3", "parsed", "==", "1.2.3", 1),
    (">=2.0,<3", "parsed", None, None, 2),
    (">= 2.0 with < 3", "parsed", None, None, 2),
    (">=2.0 and <3", "parsed", None, None, 2),
    ("(>=2.0)", "parsed", ">=", "2.0", 1),
    ("~=3.8", "unknown", None, None, 0),
    ("!=1.5", "unknown", None, None, 0),
    (">=1.0 or <2.0", "unknown", None, None, 0),
    ("2.0", "unknown", None, None, 0),   # 裸版本号无法解析 → unknown
    ("abc", "unknown", None, None, 0),
])
def test_parse_requirement(req, status, operator, version, nclauses):
    info = mod.parse_requirement(req)
    assert info["status"] == status
    assert info["operator"] == operator
    assert info["version"] == version
    assert len(info["clauses"]) == nclauses


@pytest.mark.parametrize("version,op,expected,result", [
    (None, ">=", "1.0", None),
    ("", ">=", "1.0", None),
    ("1.0", None, "1.0", None),
    ("1.0", ">=", "0.9", True),
    ("1.0", ">=", "1.1", False),
    ("1.0", "==", "1.0", True),
    ("1.0", ">", "1.0", False),
    ("0.9", "<", "1.0", True),
    ("1.0", "<=", "1.0", True),
    ("1.0", "!=", "1.0", None),   # 未支持的操作符 → None
])
def test_evaluate_constraint(version, op, expected, result):
    assert mod.evaluate_constraint(version, op, expected) is result


def test_evaluate_requirement_multi_clause():
    assert mod.evaluate_requirement("2.5", mod.parse_requirement(">=2.0,<3")) is True
    assert mod.evaluate_requirement("3.5", mod.parse_requirement(">=2.0,<3")) is False
    assert mod.evaluate_requirement("1.5", mod.parse_requirement(">=2.0,<3")) is False


def test_evaluate_requirement_edge_cases():
    assert mod.evaluate_requirement("", mod.parse_requirement(">=1")) is None
    assert mod.evaluate_requirement(None, mod.parse_requirement(">=1")) is None
    assert mod.evaluate_requirement("1.0", mod.parse_requirement("")) is None
    assert mod.evaluate_requirement("1.0", mod.parse_requirement("~=1.0")) is None
    # clauses 缺失时回退到顶层 operator/version
    info = {"status": "parsed", "operator": ">=", "version": "1.0", "clauses": []}
    assert mod.evaluate_requirement("1.5", info) is True
    assert mod.evaluate_requirement("0.5", info) is False


# ─────────────────────────────────────────────
# 包名候选生成
# ─────────────────────────────────────────────

def test_build_name_candidates_basic():
    cands = mod.build_name_candidates("requests")
    assert "requests" in cands
    assert "python3-requests" in cands
    assert "python3_requests" in cands
    assert "ros-humble-requests" in cands
    assert "ros_humble_requests" in cands
    assert "librequests" in cands
    assert "requests-devel" in cands
    assert "requests_devel" in cands
    assert "lib-requests" in cands


def test_build_name_candidates_strips_prefixes():
    # python3_ 前缀剥出裸名
    cands = mod.build_name_candidates("python3_requests")
    assert "requests" in cands
    assert "python3-requests" in cands
    # lib 前缀剥出裸名
    assert "ssl" in mod.build_name_candidates("libssl")
    assert "liblibssl" in mod.build_name_candidates("libssl")


def test_build_name_candidates_case_normalized():
    cands = mod.build_name_candidates("Django")
    assert "django" in cands
    assert "python3-django" in cands


def test_build_name_candidates_empty():
    assert mod.build_name_candidates("") == set()


# ─────────────────────────────────────────────
# chroot → repo 映射
# ─────────────────────────────────────────────

@pytest.mark.parametrize("chroot,expected", [
    ("openeuler-22.03_LTS_SP2-x86_64", "http://repo.openeuler.org/openEuler-22.03-LTS-SP2"),
    ("openeuler-22.03_LTS_SP3-aarch64", "http://repo.openeuler.org/openEuler-22.03-LTS-SP3"),
    ("openeuler-24.03_LTS-x86_64", "http://repo.openeuler.org/openEuler-24.03-LTS"),
    ("openeuler-24.03_LTS_SP4-x86_64", "http://repo.openeuler.org/openEuler-24.03-LTS-SP4"),
    ("openeuler-20.03-x86_64", None),
    ("fedora-39-x86_64", None),
    ("", None),
])
def test_chroot_to_repo_base(chroot, expected):
    assert mod._chroot_to_repo_base(chroot) == expected


# ─────────────────────────────────────────────
# repo 文件写入(重定向 /etc/yum.repos.d 到 tmp)
# ─────────────────────────────────────────────

class _RedirectedPath(PosixPath):
    """把 /etc/yum.repos.d 与 /var/cache/dnf 重定向到测试临时目录。"""
    redirect = {}

    def __new__(cls, *args):
        s = str(args[0]) if args else ""
        for prefix, dest in cls.redirect.items():
            if s.startswith(prefix):
                args = (str(dest / s[len(prefix):]),)
                break
        return super().__new__(cls, *args)


@pytest.fixture
def redirected_paths(tmp_path, monkeypatch):
    # 生产环境 /etc/yum.repos.d 必然存在(生产代码不做 mkdir),这里手动建目录
    (tmp_path / "yum.repos.d").mkdir()
    _RedirectedPath.redirect = {
        "/etc/yum.repos.d/": tmp_path / "yum.repos.d",
        "/var/cache/dnf": tmp_path / "dnfcache",
    }
    monkeypatch.setattr(mod, "Path", _RedirectedPath)
    monkeypatch.setattr(mod, "_ACTIVE_REPO_FILE", None)
    return tmp_path


def _read_repo(tmp_path):
    return (tmp_path / "yum.repos.d" / "oe-check-tmp.repo").read_text()


def test_setup_repo_for_chroot_official_only(redirected_paths, tmp_path):
    ok = mod.setup_repo_for_chroot("openeuler-22.03_LTS_SP2-x86_64")
    assert ok is True
    content = _read_repo(tmp_path)
    assert "[oe-check-official]" in content
    assert "[oe-check-update]" in content
    assert "[oe-check-epol]" in content
    assert "[oe-check-copr]" not in content
    assert "baseurl=http://repo.openeuler.org/openEuler-22.03-LTS-SP2/everything/x86_64/" in content
    assert mod._ACTIVE_REPO_FILE is not None


def test_setup_repo_for_chroot_aarch64(redirected_paths, tmp_path):
    mod.setup_repo_for_chroot("openeuler-24.03_LTS-aarch64")
    content = _read_repo(tmp_path)
    assert "/everything/aarch64/" in content
    assert "/update/aarch64/" in content


def test_setup_repo_for_chroot_with_copr(redirected_paths, tmp_path):
    ok = mod.setup_repo_for_chroot(
        "openeuler-22.03_LTS_SP2-x86_64",
        copr_url="http://copr-frontend:5000", owner="owner1", project="proj1",
    )
    assert ok is True
    content = _read_repo(tmp_path)
    assert "[oe-check-copr]" in content
    # frontend:5000 → backend:5002
    assert "baseurl=http://copr-backend:5002/results/owner1/proj1/openeuler-22.03_LTS_SP2-x86_64/" in content
    assert "gpgcheck=0" in content


def test_setup_repo_for_chroot_copr_ip_replaced(redirected_paths, tmp_path):
    mod.setup_repo_for_chroot(
        "openeuler-24.03_LTS-x86_64",
        copr_url="http://192.168.1.10:31211", owner="o", project="p",
    )
    content = _read_repo(tmp_path)
    # 集群外地址统一替换为集群内 backend
    assert "baseurl=http://copr-backend:5002/results/o/p/" in content


def test_setup_repo_for_chroot_unknown_chroot(redirected_paths, tmp_path):
    assert mod.setup_repo_for_chroot("fedora-39-x86_64") is False
    assert mod._ACTIVE_REPO_FILE is None


class _PermDeniedPath(_RedirectedPath):
    def write_text(self, *a, **kw):
        raise PermissionError("denied")


def test_setup_repo_for_chroot_permission_error(tmp_path, monkeypatch):
    _PermDeniedPath.redirect = _RedirectedPath.redirect
    monkeypatch.setattr(mod, "Path", _PermDeniedPath)
    monkeypatch.setattr(mod, "_ACTIVE_REPO_FILE", None)
    assert mod.setup_repo_for_chroot("openeuler-24.03_LTS-x86_64") is False
    assert mod._ACTIVE_REPO_FILE is None


def test_teardown_repo(redirected_paths, tmp_path):
    mod.setup_repo_for_chroot("openeuler-24.03_LTS-x86_64")
    repo_file = tmp_path / "yum.repos.d" / "oe-check-tmp.repo"
    assert repo_file.exists()
    mod.teardown_repo()
    assert not repo_file.exists()
    assert mod._ACTIVE_REPO_FILE is None


def test_teardown_repo_noop_when_not_set(monkeypatch):
    monkeypatch.setattr(mod, "_ACTIVE_REPO_FILE", None)
    mod.teardown_repo()   # 无异常即通过


def test_teardown_repo_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_ACTIVE_REPO_FILE", tmp_path / "gone.repo")
    mod.teardown_repo()
    # 注意:生产代码的 `_ACTIVE_REPO_FILE = None` 在 `if exists()` 块内,
    # 文件不存在时全局引用不会被清空(疑似遗漏,测试按实际行为断言)。
    assert mod._ACTIVE_REPO_FILE == tmp_path / "gone.repo"


# ─────────────────────────────────────────────
# dnf repoquery(mock subprocess)
# ─────────────────────────────────────────────

@pytest.mark.parametrize("pkgname,stem", [
    ("python3-requests", "requests"),
    ("python-requests", "requests"),
    ("requests", "requests"),
    ("python3-python3-x", "python3-x"),   # 只剥一层
])
def test_python_query_stem(pkgname, stem):
    assert mod._python_query_stem(pkgname) == stem


def test_dnf_repoquery_python_hit(fake_subprocess):
    fake_subprocess.when(lambda c: "python3dist(requests)" in c, stdout="")
    fake_subprocess.when(lambda c: "python3-requests" in c and "python3dist" not in c,
                         stdout="python3-requests\t2.28.1\n")
    found = mod._dnf_repoquery("python3-requests", "python")
    assert found == {"name": "python3-requests", "version": "2.28.1"}


def test_dnf_repoquery_no_hit(fake_subprocess):
    fake_subprocess.when("dnf", stdout="\n")
    assert mod._dnf_repoquery("nonexistent", "") is None


def test_dnf_repoquery_lang_specific_queries(fake_subprocess):
    fake_subprocess.when(lambda c: "npm(lodash)" in c, stdout="nodejs-lodash\t4.17.21\n")
    assert mod._dnf_repoquery("lodash", "nodejs") == {"name": "nodejs-lodash", "version": "4.17.21"}
    fake_subprocess.when(lambda c: "mvn(org.foo:bar)" in c, stdout="foo-bar\t1.0\n")
    assert mod._dnf_repoquery("org.foo:bar", "java") == {"name": "foo-bar", "version": "1.0"}


def test_dnf_repoquery_other_lang_uses_candidates(fake_subprocess):
    # 非 python/nodejs/java:候选名(排序前 8)逐个查询
    fake_subprocess.when(lambda c: "libssl" in c, stdout="openssl\t3.0.0\n")
    found = mod._dnf_repoquery("libssl", "c")
    assert found == {"name": "openssl", "version": "3.0.0"}


def test_dnf_repoquery_exception_returns_none(fake_subprocess):
    fake_subprocess.when("dnf", exc=subprocess.TimeoutExpired("dnf", 60))
    assert mod._dnf_repoquery("requests", "python") is None


def test_dnf_repoquery_repo_args_with_copr(fake_subprocess, tmp_path, monkeypatch):
    repo_file = tmp_path / "active.repo"
    repo_file.write_text("[oe-check-official]\n[oe-check-copr]\n")
    monkeypatch.setattr(mod, "_ACTIVE_REPO_FILE", repo_file)
    fake_subprocess.when("dnf", stdout="foo\t1.0\n")
    mod._dnf_repoquery("foo", "")
    assert fake_subprocess.called_with("--enablerepo=oe-check-copr")
    assert fake_subprocess.called_with("--disablerepo=*")


def test_dnf_repoquery_repo_args_without_copr_section(fake_subprocess, tmp_path, monkeypatch):
    repo_file = tmp_path / "active.repo"
    repo_file.write_text("[oe-check-official]\n")
    monkeypatch.setattr(mod, "_ACTIVE_REPO_FILE", repo_file)
    fake_subprocess.when("dnf", stdout="foo\t1.0\n")
    mod._dnf_repoquery("foo", "")
    assert not fake_subprocess.called_with("--enablerepo=oe-check-copr")


def test_dnf_repoquery_copr_requires_active_repo(monkeypatch, fake_subprocess):
    monkeypatch.setattr(mod, "_ACTIVE_REPO_FILE", None)
    assert mod._dnf_repoquery_copr("requests", "python") is None
    assert fake_subprocess.calls == []


def test_dnf_repoquery_copr_no_copr_section(tmp_path, monkeypatch, fake_subprocess):
    repo_file = tmp_path / "active.repo"
    repo_file.write_text("[oe-check-official]\n")
    monkeypatch.setattr(mod, "_ACTIVE_REPO_FILE", repo_file)
    assert mod._dnf_repoquery_copr("requests", "python") is None


def test_dnf_repoquery_copr_hit(tmp_path, monkeypatch, fake_subprocess):
    repo_file = tmp_path / "active.repo"
    repo_file.write_text("[oe-check-copr]\n")
    monkeypatch.setattr(mod, "_ACTIVE_REPO_FILE", repo_file)
    fake_subprocess.when(lambda c: "python3-requests" in c and "python3dist" not in c,
                         stdout="python3-requests\t2.28.1\n")
    found = mod._dnf_repoquery_copr("python3-requests", "python")
    assert found == {"name": "python3-requests", "version": "2.28.1"}
    assert fake_subprocess.called_with("--enablerepo=oe-check-copr")


# ─────────────────────────────────────────────
# summarize_official_repo / summarize_copr_project
# ─────────────────────────────────────────────

def test_summarize_official_repo_not_found(monkeypatch):
    monkeypatch.setattr(mod, "_dnf_repoquery", lambda p, l: None)
    result = mod.summarize_official_repo("foo", "", "1.0", "")
    assert result["exists"] is False
    assert result["highest"] is None
    assert result["meets_need"] is False
    assert result["matched_paths"] == []


def test_summarize_official_repo_found_no_constraints(monkeypatch):
    monkeypatch.setattr(mod, "_dnf_repoquery", lambda p, l: {"name": "foo", "version": "1.2.3"})
    result = mod.summarize_official_repo("foo", "", "", "")
    assert result["exists"] is True
    assert result["meets_need"] is True
    assert result["highest"]["path"] == "<openeuler-official>"
    assert result["satisfies_requested_version"] is None
    assert result["satisfies_requirement"] is None


@pytest.mark.parametrize("found_ver,requested,requirement,meets,sat_ver,sat_req", [
    ("2.0.1", "2.0", "", True, True, None),
    ("1.9", "2.0", "", False, False, None),
    ("2.0.1", "", ">=2.0", True, None, True),
    ("1.9", "", ">=2.0", False, None, False),
    ("2.0.1", "2.0", ">=1.9,<3", True, True, True),
    ("1.9", "2.0", ">=1.9", False, False, True),   # requirement 满足但版本不满足
])
def test_summarize_official_repo_version_checks(monkeypatch, found_ver, requested,
                                                requirement, meets, sat_ver, sat_req):
    monkeypatch.setattr(mod, "_dnf_repoquery", lambda p, l: {"name": "foo", "version": found_ver})
    result = mod.summarize_official_repo("foo", "", requested, requirement)
    assert result["meets_need"] is meets
    assert result["satisfies_requested_version"] is sat_ver
    assert result["satisfies_requirement"] is sat_req
    assert result["comparison_unknown"] is False


def test_summarize_official_repo_unknown_requirement(monkeypatch):
    monkeypatch.setattr(mod, "_dnf_repoquery", lambda p, l: {"name": "foo", "version": "1.9"})
    result = mod.summarize_official_repo("foo", "", "", "~=1.0")
    assert result["meets_need"] is False
    assert result["comparison_unknown"] is True


def test_summarize_copr_project_hit(monkeypatch):
    monkeypatch.setattr(mod, "_dnf_repoquery_copr", lambda p, l: {"name": "bar", "version": "1.5.0"})
    result = mod.summarize_copr_project("foo", "", "1.4", "")
    assert result["exists"] is True
    assert result["meets_need"] is True
    assert result["highest"]["path"] == "<copr-project>"
    assert result["matched_paths"] == ["<copr-project>"]


def test_summarize_copr_project_miss(monkeypatch):
    monkeypatch.setattr(mod, "_dnf_repoquery_copr", lambda p, l: None)
    result = mod.summarize_copr_project("foo", "", "1.0", "")
    assert result["exists"] is False
    assert result["meets_need"] is False


def test_summarize_copr_project_too_low(monkeypatch):
    monkeypatch.setattr(mod, "_dnf_repoquery_copr", lambda p, l: {"name": "bar", "version": "1.0"})
    result = mod.summarize_copr_project("foo", "", "2.0", "")
    assert result["exists"] is True
    assert result["meets_need"] is False
    assert result["satisfies_requested_version"] is False


# ─────────────────────────────────────────────
# COPR API 查询(mock urllib)
# ─────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install_urlopen(monkeypatch, handler):
    def fake(req, timeout=None):
        return handler(req, timeout)
    monkeypatch.setattr(mod.urllib.request, "urlopen", fake)
    return fake


def test_copr_query_package_picks_highest_succeeded(monkeypatch):
    def handler(req, timeout):
        return _FakeResponse(json.dumps({"items": [
            {"state": "succeeded", "source_package": {"name": "foo", "version": "1.0.0"}},
            {"state": "succeeded", "source_package": {"name": "foo", "version": "2.0.0"}},
            {"state": "failed", "source_package": {"name": "foo", "version": "9.9.9"}},
            {"state": "succeeded", "source_package": {"name": "foo", "version": ""}},
        ]}).encode())
    _install_urlopen(monkeypatch, handler)
    result = mod._copr_query_package("foo", "http://copr:5000", "o", "p", "l", "t")
    assert result == {"name": "foo", "version": "2.0.0"}


def test_copr_query_package_empty_items(monkeypatch):
    _install_urlopen(monkeypatch, lambda req, t: _FakeResponse(json.dumps({"items": []}).encode()))
    assert mod._copr_query_package("foo", "http://copr:5000", "o", "p", "l", "t") is None


def test_copr_query_package_http_404(monkeypatch):
    def handler(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
    _install_urlopen(monkeypatch, handler)
    assert mod._copr_query_package("foo", "http://copr:5000", "o", "p", "l", "t") is None


def test_copr_query_package_http_500(monkeypatch):
    def handler(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 500, "Server Error", {}, None)
    _install_urlopen(monkeypatch, handler)
    assert mod._copr_query_package("foo", "http://copr:5000", "o", "p", "l", "t") is None


def test_copr_query_package_network_error(monkeypatch):
    def handler(req, timeout):
        raise urllib.error.URLError("connection refused")
    _install_urlopen(monkeypatch, handler)
    assert mod._copr_query_package("foo", "http://copr:5000", "o", "p", "l", "t") is None


def test_copr_query_package_uses_basic_auth(monkeypatch):
    import base64
    captured = {}

    def handler(req, timeout):
        captured["auth"] = req.headers.get("Authorization")
        captured["url"] = req.full_url
        return _FakeResponse(json.dumps({"items": []}).encode())
    _install_urlopen(monkeypatch, handler)
    mod._copr_query_package("foo", "http://copr:5000", "o", "p", "l", "t")
    assert captured["auth"] == "Basic " + base64.b64encode(b"l:t").decode()
    assert "ownername=o" in captured["url"]
    assert "packagename=foo" in captured["url"]


# ─────────────────────────────────────────────
# 决策逻辑
# ─────────────────────────────────────────────

def _official(version="1.0.0", meets=True, name="foo", unknown=False):
    return {
        "exists": True, "meets_need": meets, "comparison_unknown": unknown,
        "highest": {"path": "<openeuler-official>", "name": name, "version": version},
    }


def _copr(meets=False):
    return {"exists": False, "meets_need": meets, "comparison_unknown": False,
            "highest": None, "matched_paths": []}


def test_choose_decision_toolchain_wins():
    # 构建工具链:只要官方源存在(即使版本不满足)就复用
    official = _official(version="10.0.0", meets=False, name="gcc")
    assert mod.choose_decision(official, _copr(), "99.0", "") == "reuse_official"


def test_choose_decision_official_meets():
    assert mod.choose_decision(_official(meets=True), _copr(), "", "") == "reuse_official"


def test_choose_decision_official_newer_same_major():
    # 官方源版本更新且同主版本 → 也复用
    official = _official(version="1.9.0", meets=False)
    assert mod.choose_decision(official, _copr(), "1.8.0", "") == "reuse_official"


def test_choose_decision_official_newer_from_requirement():
    official = _official(version="1.9.0", meets=False)
    assert mod.choose_decision(official, _copr(), "", ">=1.8.0") == "reuse_official"


def test_choose_decision_official_newer_different_major():
    official = _official(version="2.0.0", meets=False)
    assert mod.choose_decision(official, _copr(), "1.8.0", "") == "introduce_new"


def test_choose_decision_official_older():
    official = _official(version="1.5.0", meets=False)
    assert mod.choose_decision(official, _copr(), "1.8.0", "") == "introduce_new"


def test_choose_decision_unknown_requirement_skips_newer_check():
    # 约束解析不了(comparison_unknown)→ 不因"同主版本更新"复用
    official = _official(version="1.9.0", meets=False, unknown=True)
    assert mod.choose_decision(official, _copr(), "", "~=1.0") == "introduce_new"


def test_choose_decision_copr_meets():
    official = _official(version="1.5.0", meets=False)
    copr = {"exists": True, "meets_need": True, "comparison_unknown": False,
            "highest": {"path": "<copr-project>", "name": "foo", "version": "1.8.0"}}
    assert mod.choose_decision(official, copr, "1.8.0", "") == "reuse_copr_project"


def test_choose_decision_introduce_new():
    assert mod.choose_decision({"exists": False, "meets_need": False},
                               _copr(), "1.0", "") == "introduce_new"


def test_build_reason_reuse_variants():
    official = {"highest": {"version": "1.2.3"}}
    copr = {"highest": {"version": "1.2.3"}}
    reason = mod.build_reason("reuse_official", official, copr, "", "")
    assert "官方源已有满足要求" in reason
    assert "1.2.3" in reason
    reason = mod.build_reason("reuse_copr_project", official, copr, "", "")
    assert "COPR project 已有满足要求" in reason
    assert "1.2.3" in reason


def test_build_reason_introduce_new():
    # introduce_new 的 reason 不含具体版本号(生产实现如此)
    reason = mod.build_reason("introduce_new", {"highest": None}, _copr(), "", "")
    assert "官方源和 COPR project 均无满足要求" in reason


def test_build_reason_no_version_desc():
    reason = mod.build_reason("reuse_official", {"highest": None}, _copr(), "", "")
    assert "无版本约束" in reason
    assert "已存在" in reason


# ─────────────────────────────────────────────
# 主流程 check_existing_package
# ─────────────────────────────────────────────

def test_check_existing_package_unknown_chroot(capsys):
    result = mod.check_existing_package("foo", chroot="fedora-39-x86_64")
    assert result["decision"] == "introduce_new"
    assert result["should_skip"] is False
    assert result["error"] == "unknown chroot: fedora-39-x86_64"
    assert result["official"]["exists"] is False
    assert result["exists_in_official"] is False
    assert result["exists_in_copr_project"] is False
    assert "unknown chroot" in capsys.readouterr().err


def test_check_existing_package_full_flow(monkeypatch):
    official = _official(meets=True, version="2.0.0")
    copr = _copr()
    monkeypatch.setattr(mod, "setup_repo_for_chroot", lambda *a, **kw: True)
    monkeypatch.setattr(mod, "summarize_official_repo",
                        lambda p, l, v, r: official)
    monkeypatch.setattr(mod, "summarize_copr_project",
                        lambda *a, **kw: copr)
    teardown_calls = []
    monkeypatch.setattr(mod, "teardown_repo", lambda: teardown_calls.append(1))

    result = mod.check_existing_package(
        "foo", version="2.0.0", requirement="", lang="python",
        chroot="openeuler-24.03_LTS-x86_64",
    )
    assert result["decision"] == "reuse_official"
    assert result["should_skip"] is True
    assert result["exists_in_official"] is True
    assert result["exists_in_copr_project"] is False
    assert result["requested"]["pkgname"] == "foo"
    assert result["requested"]["lang"] == "python"
    assert result["requested"]["chroot"] == "openeuler-24.03_LTS-x86_64"
    assert result["requested"]["requirement_info"]["status"] == "none"
    assert teardown_calls == [1]   # repo 切换成功后必须清理


def test_check_existing_package_no_teardown_when_not_switched(monkeypatch):
    monkeypatch.setattr(mod, "setup_repo_for_chroot", lambda *a, **kw: False)
    monkeypatch.setattr(mod, "summarize_official_repo",
                        lambda p, l, v, r: {"exists": False, "meets_need": False,
                                            "highest": None, "comparison_unknown": False})
    monkeypatch.setattr(mod, "summarize_copr_project",
                        lambda *a, **kw: _copr())
    teardown_calls = []
    monkeypatch.setattr(mod, "teardown_repo", lambda: teardown_calls.append(1))
    result = mod.check_existing_package("foo", chroot="openeuler-24.03_LTS-x86_64")
    assert result["decision"] == "introduce_new"
    assert teardown_calls == []


def test_check_existing_package_copr_reuse(monkeypatch):
    official = _official(version="1.0", meets=False)
    copr = {"exists": True, "meets_need": True, "comparison_unknown": False,
            "highest": {"path": "<copr-project>", "name": "foo", "version": "1.5.0"}}
    monkeypatch.setattr(mod, "setup_repo_for_chroot", lambda *a, **kw: False)
    monkeypatch.setattr(mod, "summarize_official_repo", lambda p, l, v, r: official)
    monkeypatch.setattr(mod, "summarize_copr_project", lambda *a, **kw: copr)
    result = mod.check_existing_package("foo", version="1.5.0")
    assert result["decision"] == "reuse_copr_project"
    assert result["should_skip"] is True
    assert result["exists_in_copr_project"] is True


# ─────────────────────────────────────────────
# main()
# ─────────────────────────────────────────────

def test_main_output_file(tmp_path, capsys, monkeypatch):
    canned = {"decision": "introduce_new", "reason": "mock"}
    monkeypatch.setattr(mod, "check_existing_package", lambda *a, **kw: canned)
    out = tmp_path / "sub" / "result.json"
    monkeypatch.setattr(sys, "argv", ["check_existing_package.py", "foo", "-o", str(out)])
    mod.main()
    assert out.exists()
    assert json.loads(out.read_text()) == canned
    # 有 -o 且无 --json → 不打印 stdout
    assert capsys.readouterr().out == ""


def test_main_json_stdout(tmp_path, capsys, monkeypatch):
    canned = {"decision": "reuse_official"}
    monkeypatch.setattr(mod, "check_existing_package", lambda *a, **kw: canned)
    monkeypatch.setattr(sys, "argv", ["check_existing_package.py", "foo", "--json"])
    mod.main()
    assert "reuse_official" in capsys.readouterr().out


def test_main_passes_args(monkeypatch, capsys):
    captured = {}

    def fake_check(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"decision": "introduce_new"}

    monkeypatch.setattr(mod, "check_existing_package", fake_check)
    monkeypatch.setattr(sys, "argv", [
        "check_existing_package.py", "Foo", "--version", "1.2 ", "--lang", "PYTHON",
        "--requirement", ">=1.0", "--chroot", "openeuler-24.03_LTS-x86_64", "--json",
    ])
    mod.main()
    assert captured["args"][0] == "Foo"
    assert captured["kwargs"]["version"] == "1.2"       # strip 空白
    assert captured["kwargs"]["lang"] == "python"       # lower
    assert captured["kwargs"]["chroot"] == "openeuler-24.03_LTS-x86_64"
