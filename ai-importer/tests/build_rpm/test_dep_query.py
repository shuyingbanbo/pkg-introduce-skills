"""dep_query.py — 统一依赖查询入口(三态:ok / too_low / not_exist)。

任务构造与版本求值直接断言;run_batch_lookup 用可编程 fake 替换。
"""

from __future__ import annotations

import sys

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sys.path.insert(0, str(SCRIPT_DIRS["build_rpm"]))
mod = load_module("dep_query", SCRIPT_DIRS["build_rpm"] / "dep_query.py")


# ─────────────────────────────────────────────
# 按语言构造查询任务
# ─────────────────────────────────────────────

def test_build_tasks_for_lang_python():
    tasks = mod._build_tasks_for_lang("python-multipart", "python")
    assert len(tasks) == 1
    q = tasks[0]["queries"]
    assert q[0] == {"kind": "provides", "value": "python3dist(python-multipart)",
                    "level": "python3dist()"}
    assert q[1] == {"kind": "name", "value": "python3-python-multipart",
                    "level": "name", "prefer_devel": False}
    # 注意:前缀 "python3-" 的连字符不参与替换,只有 dep_name 自身的 - 转 _
    assert q[2] == {"kind": "name", "value": "python3-python_multipart",
                    "level": "name", "prefer_devel": False}


def test_build_tasks_for_lang_python_case_insensitive():
    # lang 大写也走 python 分支
    tasks = mod._build_tasks_for_lang("x", "PYTHON")
    assert tasks[0]["queries"][0]["value"] == "python3dist(x)"


def test_build_tasks_for_lang_nodejs():
    tasks = mod._build_tasks_for_lang("Some_Pkg", "nodejs")
    q = tasks[0]["queries"]
    assert q[0]["value"] == "npm(Some_Pkg)"
    assert q[0]["kind"] == "provides"
    # 名称归一化:小写、下划线→连字符
    assert q[1]["value"] == "nodejs-some-pkg"
    assert q[2]["value"] == "nodejs_some_pkg"


def test_build_tasks_for_lang_java():
    tasks = mod._build_tasks_for_lang("org.foo:bar", "java")
    assert tasks[0]["queries"] == [
        {"kind": "provides", "value": "mvn(org.foo:bar)", "level": "mvn()"}]


def test_build_tasks_for_lang_system_lib():
    tasks = mod._build_tasks_for_lang("SSL", "go")
    task = tasks[0]
    assert task["prefer_devel"] is True
    q = task["queries"]
    assert q[0] == {"kind": "provides", "value": "pkgconfig(ssl)", "level": "pkgconfig()"}
    assert q[1] == {"kind": "file_glob", "value": "*/libssl.so*",
                    "level": "libso", "prefer_devel": True}
    assert q[2]["value"] == "ssl-devel"
    assert q[3]["value"] == "libssl-devel"
    assert q[2]["prefer_devel"] is True
    assert q[4] == {"kind": "name", "value": "ssl",
                    "level": "name", "prefer_devel": False}


@pytest.mark.parametrize("lang", ["", "rust", "c", "cpp", "unknown"])
def test_build_tasks_for_lang_fallback_to_system(lang):
    tasks = mod._build_tasks_for_lang("zlib", lang)
    assert len(tasks) == 1
    assert tasks[0]["queries"][0]["value"] == "pkgconfig(zlib)"


# ─────────────────────────────────────────────
# 版本求值
# ─────────────────────────────────────────────

def test_evaluate_version_no_requirement():
    assert mod._evaluate_version("0.0.1", "") is True
    assert mod._evaluate_version("", "") is True


@pytest.mark.parametrize("found,requirement,expected", [
    ("2.6.3", ">= 2.6.3", True),
    ("2.6.2", ">= 2.6.3", False),
    ("2.6.3", "== 2.6.3", True),
    ("2.6.2", "== 2.6.3", False),
    ("2.0", ">=1.5,<3", True),
    ("3.5", ">=1.5,<3", False),
    ("2.6.2", "~=2.6", True),   # 无法解析 → 保守不阻断(None → True)
    ("", ">= 1", True),         # 版本缺失 → 保守不阻断
])
def test_evaluate_version(found, requirement, expected):
    assert mod._evaluate_version(found, requirement) is expected


# ─────────────────────────────────────────────
# RepoQueryResult
# ─────────────────────────────────────────────

def test_repo_query_result_to_dict():
    r = mod.RepoQueryResult("ok", rpm_name="openssl-devel",
                            found_version="3.0.9", required=">= 3")
    assert r.to_dict() == {"status": "ok", "rpm_name": "openssl-devel",
                           "found_version": "3.0.9", "required": ">= 3"}


def test_repo_query_result_repr():
    r = mod.RepoQueryResult("too_low", rpm_name="x", found_version="1.0")
    assert "too_low" in repr(r)
    assert "'x'" in repr(r)


# ─────────────────────────────────────────────
# query_repo_for_dep(mock run_batch_lookup)
# ─────────────────────────────────────────────

def _install_lookup(monkeypatch, results=None, exc=None):
    captured = {}

    def fake(tasks, timeout=120, enabled_repos=None):
        captured["tasks"] = tasks
        captured["timeout"] = timeout
        captured["enabled_repos"] = enabled_repos
        if exc is not None:
            raise exc
        return results or []
    monkeypatch.setattr(mod, "run_batch_lookup", fake)
    return captured


def test_query_repo_for_dep_ok_no_requirement(monkeypatch):
    captured = _install_lookup(monkeypatch, results=[
        {"dep": "ssl", "rpm": "openssl-devel", "version": "3.0.9",
         "release": "1", "level": "pkgconfig()"},
    ])
    r = mod.query_repo_for_dep("ssl", "go", "")
    assert r.status == "ok"
    assert r.rpm_name == "openssl-devel"
    assert r.found_version == "3.0.9"
    assert r.required == ""
    assert captured["timeout"] == 600
    # 任务按语言构造并透传
    assert captured["tasks"][0]["queries"][0]["value"] == "pkgconfig(ssl)"


def test_query_repo_for_dep_ok_requirement_satisfied(monkeypatch):
    _install_lookup(monkeypatch, results=[
        {"dep": "x", "rpm": "python3-x", "version": "2.6.3", "level": "name"},
    ])
    r = mod.query_repo_for_dep("x", "python", ">= 2.6.3")
    assert r.status == "ok"
    assert r.required == ">= 2.6.3"


def test_query_repo_for_dep_too_low(monkeypatch):
    _install_lookup(monkeypatch, results=[
        {"dep": "x", "rpm": "python3-x", "version": "2.6.2", "level": "name"},
    ])
    r = mod.query_repo_for_dep("x", "python", ">= 2.6.3")
    assert r.status == "too_low"
    assert r.rpm_name == "python3-x"
    assert r.found_version == "2.6.2"


def test_query_repo_for_dep_too_low_first_result_wins(monkeypatch):
    # 注意:生产实现对首个带 rpm 的结果即下结论(too_low 不会继续看后续结果)
    _install_lookup(monkeypatch, results=[
        {"dep": "x", "rpm": "python3-x", "version": "2.6.2", "level": "name"},
        {"dep": "x", "rpm": "python3-x", "version": "2.6.3", "level": "name"},
    ])
    r = mod.query_repo_for_dep("x", "python", ">= 2.6.3")
    assert r.status == "too_low"


def test_query_repo_for_dep_not_exist_rpm_none(monkeypatch):
    _install_lookup(monkeypatch, results=[
        {"dep": "x", "rpm": None, "version": None, "release": None, "level": ""},
    ])
    r = mod.query_repo_for_dep("x", "go", ">= 1")
    assert r.status == "not_exist"
    assert r.rpm_name is None
    assert r.required == ">= 1"


def test_query_repo_for_dep_not_exist_empty_results(monkeypatch):
    _install_lookup(monkeypatch, results=[])
    assert mod.query_repo_for_dep("x", "go", "").status == "not_exist"


def test_query_repo_for_dep_batch_error(monkeypatch):
    _install_lookup(monkeypatch, exc=mod.BatchLookupError("dnf boom"))
    assert mod.query_repo_for_dep("x", "go", "").status == "not_exist"


def test_query_repo_for_dep_oserror(monkeypatch):
    _install_lookup(monkeypatch, exc=OSError("dnf missing"))
    assert mod.query_repo_for_dep("x", "go", "").status == "not_exist"


def test_query_repo_for_dep_enabled_repos(monkeypatch):
    captured = _install_lookup(monkeypatch, results=[
        {"dep": "x", "rpm": "foo", "version": "1", "level": "name"},
    ])
    r = mod.query_repo_for_dep("x", "go", "", enabled_repos=["oe-official"])
    assert r.status == "ok"
    assert captured["enabled_repos"] == ["oe-official"]
    assert captured["tasks"][0]["enabled_repos"] == ["oe-official"]


def test_query_repo_for_dep_no_enabled_repos_key(monkeypatch):
    captured = _install_lookup(monkeypatch, results=[
        {"dep": "x", "rpm": "foo", "version": "1", "level": "name"},
    ])
    mod.query_repo_for_dep("x", "go", "")
    assert "enabled_repos" not in captured["tasks"][0]


def test_query_repo_for_dep_empty_version_ok(monkeypatch):
    # found_version 为空且无约束 → 直接 ok(生产实现如此)
    _install_lookup(monkeypatch, results=[
        {"dep": "x", "rpm": "foo", "version": "", "level": "name"},
    ])
    r = mod.query_repo_for_dep("x", "go", "")
    assert r.status == "ok"
    assert r.found_version == ""


# ─────────────────────────────────────────────
# query_both_repos
# ─────────────────────────────────────────────

def test_query_both_repos(monkeypatch):
    calls = []
    official_result = mod.RepoQueryResult("ok", rpm_name="foo", found_version="1.0")

    def fake(dep_name, lang, requirement, enabled_repos=None):
        calls.append((dep_name, lang, requirement, enabled_repos))
        return official_result
    monkeypatch.setattr(mod, "query_repo_for_dep", fake)
    official, user = mod.query_both_repos(
        "zlib", "go", ">= 1.2", official_repo_ids=["oe-official", "oe-epol"], user_repo_id="ignored",
    )
    assert official is official_result
    # user_repo 查询已废弃,恒返回 not_exist
    assert user.status == "not_exist"
    assert user.required == ">= 1.2"
    assert calls == [("zlib", "go", ">= 1.2", ["oe-official", "oe-epol"])]


def test_query_both_repos_user_always_not_exist(monkeypatch):
    monkeypatch.setattr(mod, "query_repo_for_dep",
                        lambda *a, **kw: mod.RepoQueryResult("ok", rpm_name="x"))
    _, user = mod.query_both_repos("zlib", "go", "")
    assert user.status == "not_exist"
    assert user.rpm_name is None
