"""dependency_resolution_state.py — 依赖版本解析会话状态管理 + resolution_runtime.py。

覆盖:状态文件读写/去重、快照聚合、finalize payload 构造、runtime 侧
apply_finalize_runtime 成功/失败分支。main() 的 9 个子命令按约定不测。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

drs = load_module("dependency_resolution_state", SCRIPT_DIRS["build_rpm"] / "dependency_resolution_state.py")
rr = load_module("resolution_runtime", SCRIPT_DIRS["build_rpm"] / "resolution_runtime.py")


# ─────────────────────────────────────────────
# state_file_path / read_json / write_json
# ─────────────────────────────────────────────

@pytest.mark.parametrize("state_name,filename", [
    ("resolved_versions", "resolved_versions.json"),
    ("dependency_attempts", "dependency_attempts.json"),
    ("dependency_outcomes", "dependency_outcomes.json"),
    ("session_snapshot", "session_snapshot.json"),
])
def test_state_file_path(state_name, filename):
    assert drs.state_file_path("/tmp/bs", state_name) == Path("/tmp/bs") / filename


@pytest.mark.parametrize("bad", ["", "nope", "resolved", "building"])
def test_state_file_path_unknown_raises(bad):
    with pytest.raises(ValueError, match="未知状态文件类型"):
        drs.state_file_path("/tmp/bs", bad)


def test_read_json_missing_returns_empty(tmp_path):
    assert drs.read_json(tmp_path / "nope.json") == {}


def test_read_json_roundtrip(tmp_path):
    p = tmp_path / "x.json"
    drs.write_json(p, {"b": 1, "a": "值"})
    assert drs.read_json(p) == {"b": 1, "a": "值"}


def test_write_json_creates_parents_and_format(tmp_path):
    path = tmp_path / "a" / "b" / "x.json"
    drs.write_json(path, {"b": 1, "a": "中文"})
    text = path.read_text(encoding="utf-8")
    assert "中文" in text                        # ensure_ascii=False
    assert text.index('"a"') < text.index('"b"')  # sort_keys=True
    assert text.startswith('{\n  "a"')           # indent=2
    assert text.endswith("}\n")
    assert drs.read_json(path) == {"b": 1, "a": "中文"}


# ─────────────────────────────────────────────
# ensure_state_files
# ─────────────────────────────────────────────

def test_ensure_state_files_creates_all(tmp_path):
    created = drs.ensure_state_files(str(tmp_path))
    assert set(created) == set(drs.STATE_FILE_NAMES)
    for state_name in drs.STATE_FILE_NAMES:
        assert Path(created[state_name]) == drs.state_file_path(str(tmp_path), state_name)
        assert Path(created[state_name]).exists()
        assert drs.read_json(Path(created[state_name])) == {}


def test_ensure_state_files_idempotent(tmp_path):
    first = drs.ensure_state_files(str(tmp_path))
    (tmp_path / "resolved_versions.json").write_text(json.dumps({"a": 1}), encoding="utf-8")
    second = drs.ensure_state_files(str(tmp_path))
    assert first == second  # 已存在不重写
    assert drs.read_json(tmp_path / "resolved_versions.json") == {"a": 1}


# ─────────────────────────────────────────────
# append_unique_list_item / text 状态
# ─────────────────────────────────────────────

@pytest.mark.parametrize("start,value,expected", [
    ([], "a", ["a"]),
    (["a"], "a", ["a"]),
    ([1, 2], 2, [1, 2]),
    ([1, 2], 3, [1, 2, 3]),
    (["x"], None, ["x", None]),
])
def test_append_unique_list_item(start, value, expected):
    assert drs.append_unique_list_item(start, value) == expected


def test_load_text_state_missing_returns_empty(tmp_path):
    assert drs.load_text_state(tmp_path / "nope.txt") == []


def test_load_text_state_strips_lines(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text(" a \n\nb\n#comment\n", encoding="utf-8")
    assert drs.load_text_state(p) == ["a", "b", "#comment"]


def test_write_text_state_dedup_and_strip(tmp_path):
    p = tmp_path / "x.txt"
    drs.write_text_state(p, ["a", " a ", "a", "b", "", "  ", "c"])
    assert drs.load_text_state(p) == ["a", "b", "c"]


def test_write_text_state_all_empty_writes_empty_file(tmp_path):
    p = tmp_path / "x.txt"
    drs.write_text_state(p, ["  ", "", "\t"])
    assert p.read_text(encoding="utf-8") == ""


def test_write_text_state_creates_parent(tmp_path):
    p = tmp_path / "sub" / "dir" / "x.txt"
    drs.write_text_state(p, ["a"])
    assert drs.load_text_state(p) == ["a"]


# ─────────────────────────────────────────────
# resolution_payload_from_finalize
# ─────────────────────────────────────────────

@pytest.mark.parametrize("finalize,expected_version,expected_requested", [
    ({"version": "2.0.0", "requested_version": ">=2.0"}, "2.0.0", ">=2.0"),
    ({"requested_version": "1.5.0"}, "1.5.0", "1.5.0"),   # 无 version 回退 requested_version
    ({"version": "1.5.0"}, "1.5.0", "1.5.0"),             # 无 requested_version 回退 version
    ({}, "", ""),
    ({"version": None}, "", ""),
    ({"version": 2.1}, "2.1", "2.1"),                     # 数字转字符串
    ({"version": "", "requested_version": ""}, "", ""),
])
def test_resolution_payload_from_finalize(finalize, expected_version, expected_requested):
    payload = drs.resolution_payload_from_finalize(finalize, "src", "rtype", ["by"], [{"c": 1}])
    assert payload["version"] == expected_version
    assert payload["requested_version"] == expected_requested
    assert payload["source"] == "src"
    assert payload["resolution_type"] == "rtype"
    assert payload["status"] == "resolved"
    assert payload["requested_by"] == ["by"]
    assert payload["constraints"] == [{"c": 1}]


# ─────────────────────────────────────────────
# record_resolution
# ─────────────────────────────────────────────

def test_record_resolution_writes_entry(tmp_path):
    path = drs.record_resolution(str(tmp_path), "requests", "2.31.0", ">=2.0", "pypi", "range", "resolved", ["parent"], [{"from": "parent"}])
    assert path == drs.state_file_path(str(tmp_path), "resolved_versions")
    entry = drs.read_json(path)["requests"]
    assert entry == {
        "version": "2.31.0",
        "requested_version": ">=2.0",
        "source": "pypi",
        "resolution_type": "range",
        "status": "resolved",
        "requested_by": ["parent"],
        "constraints": [{"from": "parent"}],
    }


def test_record_resolution_requested_version_fallback(tmp_path):
    drs.record_resolution(str(tmp_path), "foo", "1.0", "", "manual", "exact", "resolved", [], [])
    entry = drs.read_json(drs.state_file_path(str(tmp_path), "resolved_versions"))["foo"]
    assert entry["requested_version"] == "1.0"


def test_record_resolution_merges_requested_by(tmp_path):
    drs.record_resolution(str(tmp_path), "foo", "1.0", "", "s", "t", "resolved", ["a", "b", "a", ""], [])
    drs.record_resolution(str(tmp_path), "foo", "1.1", "", "s", "t", "resolved", ["b", "c", None], [])
    entry = drs.read_json(drs.state_file_path(str(tmp_path), "resolved_versions"))["foo"]
    assert entry["requested_by"] == ["a", "b", "c"]


def test_record_resolution_merges_constraints(tmp_path):
    drs.record_resolution(str(tmp_path), "foo", "1.0", "", "s", "t", "resolved", [], [{"a": 1}])
    drs.record_resolution(str(tmp_path), "foo", "1.1", "", "s", "t", "resolved", [], [{"a": 1}, {"b": 2}, None, {}])
    entry = drs.read_json(drs.state_file_path(str(tmp_path), "resolved_versions"))["foo"]
    # None 与 {} 均为 falsy,被 if item 过滤
    assert entry["constraints"] == [{"a": 1}, {"b": 2}]


# ─────────────────────────────────────────────
# record_attempt / record_dependency_outcome / append_constraint
# ─────────────────────────────────────────────

def test_record_attempt_dedup_by_version(tmp_path):
    drs.record_attempt(str(tmp_path), "foo", "1.0", "failed", "boom")
    drs.record_attempt(str(tmp_path), "foo", "1.0", "success", "ok")  # 同版本不再追加(不覆盖旧结果)
    drs.record_attempt(str(tmp_path), "foo", "1.1", "failed", "boom2")
    entry = drs.read_json(drs.state_file_path(str(tmp_path), "dependency_attempts"))["foo"]
    assert entry["attempted_versions"] == [
        {"version": "1.0", "result": "failed", "reason": "boom"},
        {"version": "1.1", "result": "failed", "reason": "boom2"},
    ]


def test_record_attempt_multi_dep(tmp_path):
    drs.record_attempt(str(tmp_path), "a", "1.0", "ok", "")
    drs.record_attempt(str(tmp_path), "b", "2.0", "fail", "x")
    data = drs.read_json(drs.state_file_path(str(tmp_path), "dependency_attempts"))
    assert set(data) == {"a", "b"}


def test_record_dependency_outcome(tmp_path):
    path = drs.record_dependency_outcome(str(tmp_path), "foo", {"status": "introduced", "srpm": "x.src.rpm"})
    assert drs.read_json(path) == {"foo": {"status": "introduced", "srpm": "x.src.rpm"}}


def test_append_constraint_dedup(tmp_path):
    drs.append_constraint(str(tmp_path), "foo", {"op": ">=", "v": "1"})
    drs.append_constraint(str(tmp_path), "foo", {"op": ">=", "v": "1"})  # 重复跳过
    drs.append_constraint(str(tmp_path), "foo", {})                       # falsy 跳过
    drs.append_constraint(str(tmp_path), "foo", None)                     # None 跳过
    drs.append_constraint(str(tmp_path), "bar", {"op": "==", "v": "2"})
    data = drs.read_json(drs.state_file_path(str(tmp_path), "resolved_versions"))
    assert data["foo"]["constraints"] == [{"op": ">=", "v": "1"}]
    assert data["bar"]["constraints"] == [{"op": "==", "v": "2"}]


# ─────────────────────────────────────────────
# mark_building / clear_building / mark_introduced
# ─────────────────────────────────────────────

def test_mark_and_clear_building(tmp_path):
    drs.mark_building(str(tmp_path), "a")
    drs.mark_building(str(tmp_path), "b")
    drs.mark_building(str(tmp_path), "a")  # 去重
    assert drs.load_text_state(tmp_path / "building.txt") == ["a", "b"]
    drs.clear_building(str(tmp_path), "a")
    assert drs.load_text_state(tmp_path / "building.txt") == ["b"]
    drs.clear_building(str(tmp_path), "a")  # 已清除再清 no-op
    assert drs.load_text_state(tmp_path / "building.txt") == ["b"]
    drs.clear_building(str(tmp_path), "b")
    assert drs.load_text_state(tmp_path / "building.txt") == []


@pytest.mark.parametrize("dep_name", ["", None])
def test_mark_building_skips_empty(tmp_path, dep_name):
    drs.mark_building(str(tmp_path), dep_name)
    assert drs.load_text_state(tmp_path / "building.txt") == []


def test_mark_introduced(tmp_path):
    drs.mark_introduced(str(tmp_path), "p1")
    drs.mark_introduced(str(tmp_path), "p2")
    drs.mark_introduced(str(tmp_path), "p1")
    assert drs.load_text_state(tmp_path / "introduced.txt") == ["p1", "p2"]


# ─────────────────────────────────────────────
# session snapshot
# ─────────────────────────────────────────────

def test_build_session_snapshot(tmp_path):
    drs.record_resolution(str(tmp_path), "foo", "1.0", "", "s", "t", "resolved", [], [])
    drs.mark_building(str(tmp_path), "b1")
    drs.mark_introduced(str(tmp_path), "i1")
    snap = drs.build_session_snapshot(str(tmp_path))
    assert snap["resolved_versions"]["foo"]["version"] == "1.0"
    assert snap["dependency_attempts"] == {}
    assert snap["dependency_outcomes"] == {}
    assert snap["building"] == ["b1"]
    assert snap["introduced"] == ["i1"]
    assert snap["_meta"] == {"source": "live-state-files"}


def test_load_session_state_equals_snapshot(tmp_path):
    assert drs.load_session_state(str(tmp_path)) == drs.build_session_snapshot(str(tmp_path))


def test_write_session_snapshot(tmp_path):
    path = drs.write_session_snapshot(str(tmp_path))
    assert path == drs.state_file_path(str(tmp_path), "session_snapshot")
    data = drs.read_json(path)
    assert data["_meta"]["source"] == "live-state-files"
    assert data["_meta"]["written_to"] == str(path)


def test_dump_session_snapshot_returns_path(tmp_path):
    path = drs.dump_session_snapshot(str(tmp_path))
    assert path == drs.state_file_path(str(tmp_path), "session_snapshot")
    assert path.exists()


@pytest.mark.parametrize("state_name", [
    "resolved_versions", "dependency_attempts", "dependency_outcomes", "session_snapshot",
])
def test_load_state_missing_returns_empty(tmp_path, state_name):
    assert drs.load_state(str(tmp_path), state_name) == {}


# ═════════════════════════════════════════════
# resolution_runtime.py
# ═════════════════════════════════════════════

def test_success_actions_constant():
    assert rr.SUCCESS_ACTIONS == {"built_new", "upgraded_user_repo", "reused_official", "reused_user_repo"}


@pytest.mark.parametrize("dep_item,requested_by,expected", [
    ({"type": "python", "constraint": ">=2.0"}, "pkg-a", {"from": "pkg-a", "lang": "python", "constraint": ">=2.0"}),
    ({"type": "", "constraint": "", "requirement": "req-x"}, "pkg-b", {"from": "pkg-b", "lang": "", "constraint": "req-x"}),
    ({"type": "go", "requirement": "v1.2.3"}, "pkg-c", {"from": "pkg-c", "lang": "go", "constraint": "v1.2.3"}),
    ({}, "pkg-d", {"from": "pkg-d", "lang": "", "constraint": ""}),
    ({"type": "nodejs"}, "", {"from": "", "lang": "nodejs", "constraint": ""}),
])
def test_rr_build_constraint_record(dep_item, requested_by, expected):
    assert rr.build_constraint_record(dep_item, requested_by) == expected


@pytest.mark.parametrize("action", sorted(rr.SUCCESS_ACTIONS))
def test_apply_finalize_runtime_success(tmp_path, action):
    dep_item = {"name": "requests", "type": "python", "constraint": ">=2.0"}
    resolution_result = {"name": "requests", "requested_by": "parent", "constraint_type": "range", "selected_strategy": "range_latest_compatible"}
    finalize_result = {"action": action, "version": "2.31.0", "requested_version": ">=2.0"}
    result = rr.apply_finalize_runtime(dep_item, resolution_result, finalize_result, str(tmp_path))
    assert result == {
        "status": "accepted", "dep_name": "requests", "version": "2.31.0",
        "requested_version": ">=2.0", "action": action,
    }
    entry = drs.read_json(drs.state_file_path(str(tmp_path), "resolved_versions"))["requests"]
    assert entry["source"] == "range_latest_compatible"
    assert entry["resolution_type"] == "range"
    assert entry["status"] == "resolved"
    assert entry["requested_by"] == ["parent"]
    assert entry["constraints"] == [{"from": "parent", "lang": "python", "constraint": ">=2.0"}]


def test_apply_finalize_runtime_name_and_type_fallback(tmp_path):
    dep_item = {"dep": "foo", "constraint_type": "exact", "type": "python"}
    resolution_result = {"requested_by": "p"}  # 无 name / constraint_type / selected_strategy
    finalize_result = {"action": "built_new", "version": "1.0"}
    result = rr.apply_finalize_runtime(dep_item, resolution_result, finalize_result, str(tmp_path))
    assert result["status"] == "accepted"
    assert result["dep_name"] == "foo"
    assert result["requested_version"] == "1.0"  # requested_version 缺失回退 version
    entry = drs.read_json(drs.state_file_path(str(tmp_path), "resolved_versions"))["foo"]
    assert entry["resolution_type"] == "exact"  # resolution_result 无 → dep_item
    assert entry["source"] == "manual"          # selected_strategy 缺失默认 manual
    assert entry["constraints"] == [{"from": "p", "lang": "python", "constraint": ""}]


def test_apply_finalize_runtime_success_no_requested_by(tmp_path):
    dep_item = {"name": "foo"}
    resolution_result = {"name": "foo"}
    finalize_result = {"action": "reused_official", "version": "1.0"}
    result = rr.apply_finalize_runtime(dep_item, resolution_result, finalize_result, str(tmp_path))
    assert result["status"] == "accepted"
    entry = drs.read_json(drs.state_file_path(str(tmp_path), "resolved_versions"))["foo"]
    assert entry["requested_by"] == []  # 空串被 record_resolution 过滤
    assert entry["constraints"] == [{"from": "", "lang": "", "constraint": ""}]


def test_apply_finalize_runtime_constraint_record_from_resolution_result(tmp_path):
    dep_item = {"name": "foo", "type": "python"}
    resolution_result = {"name": "foo", "requested_by": "p",
                         "constraint_record": {"from": "p", "lang": "python", "constraint": ">=1"}}
    finalize_result = {"action": "upgraded_user_repo", "version": "1.5"}
    rr.apply_finalize_runtime(dep_item, resolution_result, finalize_result, str(tmp_path))
    entry = drs.read_json(drs.state_file_path(str(tmp_path), "resolved_versions"))["foo"]
    assert entry["constraints"] == [{"from": "p", "lang": "python", "constraint": ">=1"}]


@pytest.mark.parametrize("retryable,expected_status", [
    (True, "retry"),
    (False, "blocked"),
    (None, "blocked"),
])
def test_apply_finalize_runtime_failure_records_attempt(tmp_path, retryable, expected_status):
    dep_item = {"name": "foo"}
    resolution_result = {"name": "foo", "requested_by": "p"}
    finalize_result = {"action": "build_failed", "version": "1.0", "failure_type": "copr",
                       "failure_reason": "boom", "retryable": retryable}
    result = rr.apply_finalize_runtime(dep_item, resolution_result, finalize_result, str(tmp_path))
    assert result["status"] == expected_status
    assert result["dep_name"] == "foo"
    assert result["version"] == "1.0"
    assert result["action"] == "build_failed"
    assert result["failure_type"] == "copr"
    assert result["failure_reason"] == "boom"
    attempts = drs.read_json(drs.state_file_path(str(tmp_path), "dependency_attempts"))
    assert attempts["foo"]["attempted_versions"] == [{"version": "1.0", "result": "copr", "reason": "boom"}]


def test_apply_finalize_runtime_failure_no_version_no_attempt(tmp_path):
    dep_item = {"name": "foo"}
    resolution_result = {"name": "foo"}
    finalize_result = {"action": "failed", "retryable": True}
    result = rr.apply_finalize_runtime(dep_item, resolution_result, finalize_result, str(tmp_path))
    assert result["status"] == "retry"
    assert result["version"] == ""
    assert drs.read_json(drs.state_file_path(str(tmp_path), "dependency_attempts")) == {}


def test_apply_finalize_runtime_failure_reason_fallback(tmp_path):
    dep_item = {"name": "foo"}
    resolution_result = {"name": "foo"}
    finalize_result = {"action": "build_failed", "version": "1.0", "reason": "alt-reason", "retryable": False}
    result = rr.apply_finalize_runtime(dep_item, resolution_result, finalize_result, str(tmp_path))
    assert result["failure_reason"] == "alt-reason"
    attempts = drs.read_json(drs.state_file_path(str(tmp_path), "dependency_attempts"))
    assert attempts["foo"]["attempted_versions"][0]["reason"] == "alt-reason"


def test_apply_finalize_runtime_attempt_result_fallback(tmp_path):
    # failure_type 与 action 都为空 → attempt result 回退 "failed"
    dep_item = {"name": "foo"}
    resolution_result = {"name": "foo"}
    finalize_result = {"action": "", "version": "1.0", "retryable": True}
    result = rr.apply_finalize_runtime(dep_item, resolution_result, finalize_result, str(tmp_path))
    assert result["action"] == ""
    attempts = drs.read_json(drs.state_file_path(str(tmp_path), "dependency_attempts"))
    assert attempts["foo"]["attempted_versions"][0]["result"] == "failed"


def test_record_execution_outcome(tmp_path):
    rr.record_execution_outcome(str(tmp_path), "foo", {"status": "done", "srpm": "x"})
    assert drs.read_json(drs.state_file_path(str(tmp_path), "dependency_outcomes")) == {"foo": {"status": "done", "srpm": "x"}}
