"""resolve_dependency_versions.py + providers/{npm,pypi}.py 测试。

providers 网络调用用 monkeypatch 替换 urllib.request.urlopen;
resolve_candidates 的策略状态机用 monkeypatch 替换模块内
list_pypi_stable_versions / list_npm_stable_versions。
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

# 预注册依赖模块,保证 resolve_dependency_versions 顶层 import 命中
drs = load_module("dependency_resolution_state", SCRIPT_DIRS["build_rpm"] / "dependency_resolution_state.py")
npm = load_module("providers.npm", SCRIPT_DIRS["build_rpm"] / "providers" / "npm.py")
pypi = load_module("providers.pypi", SCRIPT_DIRS["build_rpm"] / "providers" / "pypi.py")
cp = load_module("constraint_parser", SCRIPT_DIRS["build_rpm"] / "constraint_parser.py")
rdv = load_module("resolve_dependency_versions", SCRIPT_DIRS["build_rpm"] / "resolve_dependency_versions.py")


class _FakeResponse:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data


@pytest.fixture
def build_state(tmp_path):
    drs.ensure_state_files(str(tmp_path))
    return str(tmp_path)


def _python_dep(**overrides):
    dep = {"name": "requests", "type": "python", "constraint": ">=2.0,<3",
           "constraint_type": "range", "requirement_info": {}}
    dep.update(overrides)
    return dep


# ═════════════════════════════════════════════
# providers 纯函数:normalize_version
# ═════════════════════════════════════════════

@pytest.mark.parametrize("value,expected", [
    ("v1.2.3", "1.2.3"),
    ("V2.0.0", "2.0.0"),
    ("1.2.3", "1.2.3"),
    (" v1.0 ", "1.0"),
    ("", ""),
    (None, ""),
])
def test_pypi_normalize_version(value, expected):
    assert pypi.normalize_version(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("v1.2.3", "1.2.3"),
    ("V2.0.0", "2.0.0"),
    ("1.2.3", "1.2.3"),
    (" v1.0 ", "1.0"),
    ("", ""),
    (None, ""),
])
def test_npm_normalize_version(value, expected):
    assert npm.normalize_version(value) == expected


# ═════════════════════════════════════════════
# providers/npm.py:list_stable_versions
# ═════════════════════════════════════════════

NPM_PAYLOAD = {"versions": {
    "1.0.0": {},
    "2.0.0": {},
    "2.1.0-beta.1": {},
    "v2.1.1": {},
    "1.5": {},
    "0.9.9": {},
    "3.0.0-rc1": {},
    "1.2.x": {},
    "2.0.0-alpha": {},
    "10.0.0": {},
}}


def test_npm_list_stable_versions(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=15: _FakeResponse(NPM_PAYLOAD))
    # 预发布(beta/rc/alpha)与非法版本(1.2.x)剔除;v 前缀剥除;按数值段降序
    assert npm.list_stable_versions("somepkg") == ["10.0.0", "2.1.1", "2.0.0", "1.5", "1.0.0", "0.9.9"]


def test_npm_list_stable_versions_network_error_returns_empty(monkeypatch):
    def boom(req, timeout=15):
        raise OSError("network down")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert npm.list_stable_versions("somepkg") == []


def test_npm_list_stable_versions_invalid_json_returns_empty(monkeypatch):
    class _Bad:
        def read(self):
            return b"not json"
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=15: _Bad())
    assert npm.list_stable_versions("somepkg") == []


def test_npm_list_stable_versions_no_versions_key(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=15: _FakeResponse({}))
    assert npm.list_stable_versions("somepkg") == []


def test_npm_list_stable_versions_empty_normalized_key_skipped(monkeypatch):
    # 键归一化后为空串 → 跳过
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=15: _FakeResponse({"versions": {"": {}, "1.0.0": {}}}))
    assert npm.list_stable_versions("somepkg") == ["1.0.0"]


def test_npm_list_stable_versions_non_dict_payload_raises(monkeypatch):
    # 注:生产代码网络层 try 只包住 urlopen/json.loads,payload 非 dict 时
    # payload.get 的 AttributeError 直接抛出,按实际行为断言
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=15: _FakeResponse(["1.0.0"]))
    with pytest.raises(AttributeError):
        npm.list_stable_versions("somepkg")


# ═════════════════════════════════════════════
# providers/pypi.py:list_stable_versions
# ═════════════════════════════════════════════

PYPI_PAYLOAD = {"releases": {
    "1.0.0": [{"filename": "pkg-1.0.0.tar.gz"}],
    "2.0.0": [{}],
    "2.1.0rc1": [{"filename": "x"}],
    "3.0.0.dev1": [{"filename": "x"}],
    "v4.0.0": [{"filename": "x"}],
    "5.0.0": [],
    "invalid!": [{"filename": "x"}],
}}


def test_pypi_list_stable_versions(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=15: _FakeResponse(PYPI_PAYLOAD))
    # rc/dev 预发布剔除;files 为空剔除;非法版本剔除;v 前缀剥除;降序
    assert pypi.list_stable_versions("somepkg") == ["4.0.0", "2.0.0", "1.0.0"]


def test_pypi_list_stable_versions_network_error_propagates(monkeypatch):
    # 注:与 npm provider 不同,pypi 不吞网络异常,按实际行为断言
    def boom(req, timeout=15):
        raise OSError("down")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(OSError):
        pypi.list_stable_versions("somepkg")


def test_pypi_list_stable_versions_empty_releases(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=15: _FakeResponse({"releases": {}}))
    assert pypi.list_stable_versions("somepkg") == []


def test_pypi_list_stable_versions_missing_releases_key(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=15: _FakeResponse({}))
    assert pypi.list_stable_versions("somepkg") == []


def test_pypi_list_stable_versions_packaging_missing(monkeypatch):
    # packaging 不可用时的降级分支(pypi.py 顶层 try/except 的 Version=None 路径)
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=15: _FakeResponse({"releases": {"1.0.0": [{}]}}))
    monkeypatch.setattr(pypi, "Version", None)
    assert pypi.list_stable_versions("somepkg") == []


# ═════════════════════════════════════════════
# satisfies_constraint
# ═════════════════════════════════════════════

@pytest.mark.parametrize("version,constraint,constraint_type,requirement_info,expected", [
    ("1.0.0", "", "unbounded", {}, True),
    ("", "", "unbounded", {}, False),                     # 空版本归一化为空 → False
    ("1.0.0", ">=1", "unknown", {}, False),
    ("2.5.0", ">=2.0,<3", "range", {}, True),
    ("3.0.0", ">=2.0,<3", "range", {}, False),
    ("1.5.0", "==1.5.0", "exact", {"exact_version": "1.5.0"}, True),
    ("v1.5.0", "==1.5.0", "exact", {"exact_version": "1.5.0"}, True),
    ("1.6.0", "==1.5.0", "exact", {"exact_version": "1.5.0"}, False),
    ("1.5.0", "==1.5.0", "exact", {}, False),             # 无 exact_version → False
    ("not-a-version", ">=2.0", "range", {}, False),       # 非法版本
    ("1.9.0", "^1.2.3", "range", {}, True),               # npm ^ → >=1.2.3,<2.0.0
    ("2.1.0", "^1.2.3", "range", {}, False),
    ("0.9.0", "~0.9.5", "range", {}, False),              # ~0.9.5 → >=0.9.5,<0.10.0
    ("1.0.0", "??", "range", {}, False),                  # 无法解析 → False
])
def test_satisfies_constraint(version, constraint, constraint_type, requirement_info, expected):
    assert rdv.satisfies_constraint(version, constraint, constraint_type, requirement_info) == expected


# ═════════════════════════════════════════════
# attempted_versions_for / build_constraint_record
# ═════════════════════════════════════════════

@pytest.mark.parametrize("state,dep,expected", [
    ({}, "foo", set()),
    ({"foo": None}, "foo", set()),
    ({"foo": {"attempted_versions": []}}, "foo", set()),
    ({"foo": {"attempted_versions": [{"version": "1.0"}, {"version": "2.0", "result": "x"}]}}, "foo", {"1.0", "2.0"}),
    ({"foo": {"attempted_versions": ["1.0", {"version": "2.0"}, None]}}, "foo", {"2.0"}),  # 非 dict 跳过
    ({"other": {"attempted_versions": [{"version": "1.0"}]}}, "foo", set()),
])
def test_attempted_versions_for(state, dep, expected):
    assert rdv.attempted_versions_for(dep, state) == expected


@pytest.mark.parametrize("dep_item,requested_by,expected", [
    ({"type": "python", "constraint": ">=2.0"}, "pkg-a", {"from": "pkg-a", "lang": "python", "constraint": ">=2.0"}),
    ({"type": "go", "requirement": "v1.2.3", "constraint": ""}, "pkg-b", {"from": "pkg-b", "lang": "go", "constraint": "v1.2.3"}),
    ({}, "pkg-c", {"from": "pkg-c", "lang": "", "constraint": ""}),
    ({"type": "nodejs"}, "", {"from": "", "lang": "nodejs", "constraint": ""}),
])
def test_build_constraint_record(dep_item, requested_by, expected):
    assert rdv.build_constraint_record(dep_item, requested_by) == expected


# ═════════════════════════════════════════════
# resolve_candidates:locked 分支
# ═════════════════════════════════════════════

def test_resolve_candidates_locked_reuse(build_state):
    drs.record_resolution(build_state, "requests", "2.31.0", "", "pypi", "range", "resolved", ["old-parent"], [{"from": "old-parent"}])
    result = rdv.resolve_candidates(_python_dep(), build_state, "parent")
    assert result["selected_strategy"] == "reuse_locked_version"
    assert result["locked_version"] == "2.31.0"
    assert result["conflict"] is False
    assert result["candidates"] == ["2.31.0"]
    assert result["reason"] == "locked version satisfies current constraint"
    assert result["name"] == "requests"
    assert result["constraint_record"] == {"from": "parent", "lang": "python", "constraint": ">=2.0,<3"}


def test_resolve_candidates_locked_conflict(build_state):
    drs.record_resolution(build_state, "requests", "3.5.0", "", "pypi", "range", "resolved", [], [])
    result = rdv.resolve_candidates(_python_dep(), build_state, "parent")
    assert result["selected_strategy"] == "locked_version_conflict"
    assert result["conflict"] is True
    assert result["candidates"] == []
    assert result["reason"] == "locked version 3.5.0 does not satisfy constraint >=2.0,<3"


def test_resolve_candidates_locked_unbounded_satisfies(build_state):
    drs.record_resolution(build_state, "requests", "2.31.0", "", "pypi", "range", "resolved", [], [])
    dep = {"name": "requests", "type": "python", "constraint": "", "constraint_type": "unbounded", "requirement_info": {}}
    assert rdv.resolve_candidates(dep, build_state, "parent")["selected_strategy"] == "reuse_locked_version"


def test_resolve_candidates_locked_empty_constraint_conflict(build_state):
    drs.record_resolution(build_state, "requests", "2.31.0", "", "pypi", "range", "resolved", [], [])
    dep = {"name": "requests", "type": "python", "constraint": "", "constraint_type": "unknown", "requirement_info": {}}
    result = rdv.resolve_candidates(dep, build_state, "parent")
    assert result["selected_strategy"] == "locked_version_conflict"
    assert result["reason"] == "locked version 2.31.0 does not satisfy constraint <none>"


# ═════════════════════════════════════════════
# resolve_candidates:exact 分支
# ═════════════════════════════════════════════

@pytest.mark.parametrize("attempted,expected_candidates", [
    (False, ["1.5.0"]),
    (True, []),   # 已尝试过的 exact 版本不再给出
])
def test_resolve_candidates_exact(build_state, attempted, expected_candidates):
    if attempted:
        drs.record_attempt(build_state, "requests", "1.5.0", "failed", "boom")
    dep = {"name": "requests", "type": "python", "constraint": "==1.5.0",
           "constraint_type": "exact", "requirement_info": {"exact_version": "1.5.0"}}
    result = rdv.resolve_candidates(dep, build_state, "parent")
    assert result["selected_strategy"] == "exact_version"
    assert result["candidates"] == expected_candidates
    assert result["conflict"] is False
    assert result["locked_version"] == ""
    assert result["reason"] == "exact version derived from dependency constraint"


def test_resolve_candidates_exact_v_prefix_normalized(build_state):
    dep = {"name": "requests", "type": "python", "constraint": "==1.5.0",
           "constraint_type": "exact", "requirement_info": {"exact_version": "v1.5.0"}}
    assert rdv.resolve_candidates(dep, build_state, "parent")["candidates"] == ["1.5.0"]


def test_resolve_candidates_exact_missing_version(build_state):
    dep = {"name": "requests", "type": "python", "constraint": "==1.5.0", "constraint_type": "exact"}
    assert rdv.resolve_candidates(dep, build_state, "parent")["candidates"] == []


# ═════════════════════════════════════════════
# resolve_candidates:python/nodejs/runtime provider 分支
# ═════════════════════════════════════════════

def test_resolve_candidates_python_range(build_state, monkeypatch):
    monkeypatch.setattr(rdv, "list_pypi_stable_versions", lambda name: ["3.0.0", "2.32.3", "2.31.0", "2.28.1"])
    result = rdv.resolve_candidates(_python_dep(), build_state, "parent")
    assert result["selected_strategy"] == "range_latest_compatible"
    assert result["candidates"] == ["2.32.3", "2.31.0", "2.28.1"]  # 3.0.0 超上界;最多 3 个
    assert result["conflict"] is False
    assert result["reason"] == "generated from compatible stable releases"
    assert result["requested_by"] == "parent"


def test_resolve_candidates_python_range_filters_attempted(build_state, monkeypatch):
    drs.record_attempt(build_state, "requests", "2.31.0", "failed", "x")
    monkeypatch.setattr(rdv, "list_pypi_stable_versions", lambda name: ["2.32.3", "2.31.0", "2.28.1"])
    result = rdv.resolve_candidates(_python_dep(), build_state, "parent")
    assert result["candidates"] == ["2.32.3", "2.28.1"]


def test_resolve_candidates_python_range_max_candidates(build_state, monkeypatch):
    monkeypatch.setattr(rdv, "list_pypi_stable_versions", lambda name: ["2.9.9", "2.8.0", "2.7.0", "2.6.0", "2.5.0"])
    result = rdv.resolve_candidates(_python_dep(), build_state, "parent")
    assert len(result["candidates"]) == 3
    assert result["candidates"] == ["2.9.9", "2.8.0", "2.7.0"]


def test_resolve_candidates_python_range_all_incompatible(build_state, monkeypatch):
    monkeypatch.setattr(rdv, "list_pypi_stable_versions", lambda name: ["5.0.0", "4.0.0"])
    result = rdv.resolve_candidates(_python_dep(), build_state, "parent")
    assert result["candidates"] == []
    assert result["conflict"] is False  # 无候选但不是 conflict


def test_resolve_candidates_python_unbounded(build_state, monkeypatch):
    # provider 按 newest-first 排序,resolve_candidates 保持 provider 顺序
    monkeypatch.setattr(rdv, "list_pypi_stable_versions", lambda name: ["4.0", "3.0", "2.0", "1.1"])
    dep = {"name": "requests", "type": "python", "constraint": "", "constraint_type": "unbounded", "requirement_info": {}}
    result = rdv.resolve_candidates(dep, build_state, "parent")
    assert result["selected_strategy"] == "stable_candidates"
    assert result["candidates"] == ["4.0", "3.0", "2.0"]


def test_resolve_candidates_nodejs_range(build_state, monkeypatch):
    monkeypatch.setattr(rdv, "list_npm_stable_versions", lambda name: ["2.1.0", "1.9.0", "1.8.0", "1.5.0"])
    dep = {"name": "lodash", "type": "nodejs", "constraint": "^1.2.3", "constraint_type": "range", "requirement_info": {}}
    result = rdv.resolve_candidates(dep, build_state, "parent")
    assert result["selected_strategy"] == "range_latest_compatible"
    assert result["candidates"] == ["1.9.0", "1.8.0", "1.5.0"]
    assert result["reason"] == "generated from compatible npm stable releases"


def test_resolve_candidates_runtime_unbounded(build_state, monkeypatch):
    monkeypatch.setattr(rdv, "list_npm_stable_versions", lambda name: ["3.0.0", "2.0.0", "1.0.0"])
    dep = {"name": "dep", "type": "runtime", "constraint": "", "constraint_type": "unbounded", "requirement_info": {}}
    result = rdv.resolve_candidates(dep, build_state, "parent")
    assert result["selected_strategy"] == "stable_candidates"
    assert result["candidates"] == ["3.0.0", "2.0.0", "1.0.0"]


# ═════════════════════════════════════════════
# resolve_candidates:unsupported 分支
# ═════════════════════════════════════════════

@pytest.mark.parametrize("dep_item,expected_conflict", [
    ({"name": "z", "type": "c", "constraint": ">=1", "constraint_type": "range"}, True),
    ({"name": "z", "type": "c", "constraint": "", "constraint_type": "unbounded"}, False),
    ({"name": "z", "type": "java", "constraint": "", "constraint_type": "unbounded"}, False),
    ({"name": "z", "type": "python", "constraint": "abc", "constraint_type": "unknown"}, True),
])
def test_resolve_candidates_unsupported(build_state, dep_item, expected_conflict):
    result = rdv.resolve_candidates(dep_item, build_state, "parent")
    assert result["selected_strategy"] == "unsupported_candidate_generation"
    assert result["conflict"] is expected_conflict
    assert result["candidates"] == []
    assert result["reason"] == "no reliable candidate generation strategy for this language/constraint yet"


def test_resolve_candidates_name_fallback(build_state):
    dep = {"dep": "foo", "type": "c", "constraint": "", "constraint_type": "unbounded"}
    assert rdv.resolve_candidates(dep, build_state, "parent")["name"] == "foo"


# ═════════════════════════════════════════════
# append_planning_snapshot / read_json
# ═════════════════════════════════════════════

def test_append_planning_snapshot_creates(tmp_path):
    path = rdv.append_planning_snapshot(str(tmp_path), {"n": 1})
    assert path == Path(tmp_path) / "dependency_planning_history.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"history": [{"n": 1}]}


def test_append_planning_snapshot_accumulation_bug(tmp_path):
    # 注:生产代码疑似 bug——第二次调用读回的是 {"history": [...]} 包装结构,
    # isinstance(history, list) 为 False 导致历史被重置,实际只保留最新一条(按实际行为断言)
    rdv.append_planning_snapshot(str(tmp_path), {"n": 1})
    rdv.append_planning_snapshot(str(tmp_path), {"n": 2})
    data = json.loads((tmp_path / "dependency_planning_history.json").read_text(encoding="utf-8"))
    assert data == {"history": [{"n": 2}]}


def test_append_planning_snapshot_corrupt_file(tmp_path):
    p = tmp_path / "dependency_planning_history.json"
    p.write_text("{not json", encoding="utf-8")
    rdv.append_planning_snapshot(str(tmp_path), {"n": 1})
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data == {"history": [{"n": 1}]}


def test_append_planning_snapshot_non_list_content(tmp_path):
    p = tmp_path / "dependency_planning_history.json"
    p.write_text('{"other": 1}', encoding="utf-8")
    rdv.append_planning_snapshot(str(tmp_path), {"n": 1})
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data == {"history": [{"n": 1}]}


def test_read_json(tmp_path):
    p = tmp_path / "x.json"
    p.write_text('{"a": 1}', encoding="utf-8")
    assert rdv.read_json(p) == {"a": 1}


def test_max_candidates_constant():
    assert rdv.MAX_CANDIDATES == 3


def test_constraint_parser_pre_registered():
    # 验证 resolve_dependency_versions 顶层 import 命中了预注册的 constraint_parser
    assert rdv.to_specifier_set is cp.to_specifier_set
    assert rdv.normalize_npm_constraint is cp.normalize_npm_constraint


# ═════════════════════════════════════════════
# build_layer_plan / resolve_layer_candidates
# ═════════════════════════════════════════════

def test_build_layer_plan_prechecked_conflict(build_state):
    requests = [{"name": "dup1", "constraint": ">=1", "constraint_type": "range",
                 "conflict": True, "conflict_reason": "duplicate request"}]
    plan = rdv.build_layer_plan(requests, build_state, "parent", pkgname="mypkg", lang="python")
    assert plan["pkgname"] == "mypkg"
    assert plan["lang"] == "python"
    assert plan["requested_by"] == "parent"
    assert plan["summary"] == {"request_count": 1, "planned_count": 0, "blocked_count": 1}
    assert plan["planned"] == []
    node = plan["blocked"][0]
    assert node["selected_strategy"] == "prechecked_conflict"
    assert node["node_state"] == "blocked"
    assert node["reason"] == "duplicate request"
    assert node["candidates"] == []
    assert node["locked_version"] == ""
    assert node["conflict"] is True  # **dep_item 原样透传
    assert plan["planning_log"][0] == {
        "name": "dup1", "identity": "dup1",
        "input_constraint": ">=1", "input_constraint_type": "range",
        "selected_strategy": "prechecked_conflict", "locked_version": "",
        "candidates": [], "node_state": "blocked", "reason": "duplicate request",
    }


def test_build_layer_plan_mixed(build_state, monkeypatch):
    monkeypatch.setattr(rdv, "list_pypi_stable_versions", lambda name: ["2.31.0", "2.28.1"])
    requests = [
        {"name": "conflict-dep", "constraint": ">=9", "constraint_type": "range",
         "conflict": True, "conflict_reason": "dup"},
        {"name": "requests", "type": "python", "constraint": ">=2.0,<3",
         "constraint_type": "range", "requirement_info": {}},
    ]
    plan = rdv.build_layer_plan(requests, build_state, "parent", pkgname="mypkg", lang="python")
    assert plan["summary"] == {"request_count": 2, "planned_count": 1, "blocked_count": 1}
    planned = plan["planned"][0]
    assert planned["name"] == "requests"
    assert planned["node_state"] == "planned"
    assert planned["selected_strategy"] == "range_latest_compatible"
    assert planned["candidates"] == ["2.31.0", "2.28.1"]
    assert len(plan["planning_log"]) == 2
    assert plan["planning_log"][1]["node_state"] == "planned"
    hist = json.loads(Path(build_state, "dependency_planning_history.json").read_text(encoding="utf-8"))
    assert hist["history"][0]["summary"] == plan["summary"]
    assert hist["history"][0]["pkgname"] == "mypkg"


def test_build_layer_plan_locked_conflict_blocked(build_state):
    drs.record_resolution(build_state, "requests", "3.5.0", "", "pypi", "range", "resolved", [], [])
    requests = [{"name": "requests", "type": "python", "constraint": ">=2.0,<3", "constraint_type": "range"}]
    plan = rdv.build_layer_plan(requests, build_state, "parent")
    assert plan["summary"] == {"request_count": 1, "planned_count": 0, "blocked_count": 1}
    assert plan["blocked"][0]["selected_strategy"] == "locked_version_conflict"
    assert plan["blocked"][0]["node_state"] == "blocked"


def test_build_layer_plan_empty_requests(build_state):
    plan = rdv.build_layer_plan([], build_state, "parent", pkgname="p", lang="c")
    assert plan["summary"] == {"request_count": 0, "planned_count": 0, "blocked_count": 0}
    assert plan["planned"] == []
    assert plan["blocked"] == []
    assert plan["planning_log"] == []


def test_resolve_layer_candidates_delegates(build_state):
    plan = rdv.resolve_layer_candidates([], build_state, "parent", pkgname="p", lang="python")
    assert plan["summary"] == {"request_count": 0, "planned_count": 0, "blocked_count": 0}
    assert plan["pkgname"] == "p"
    assert plan["lang"] == "python"
