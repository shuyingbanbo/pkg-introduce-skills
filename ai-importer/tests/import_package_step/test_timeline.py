"""timeline.py — timeline 事件写入/读取/状态快照(纯逻辑 + CLI 模式)。"""

from __future__ import annotations

import json

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

tl = load_module("timeline", SCRIPT_DIRS["step"] / "timeline.py")


# ─────────────────────────────────────────────
# write_event
# ─────────────────────────────────────────────

def test_write_event_creates_file_and_appends(tmp_path):
    tl.write_event(tmp_path, "state.transition", "setuptools", {"from": "a", "to": "b"})
    tl.write_event(tmp_path, "error", "setuptools", {"message": "boom"})

    lines = (tmp_path / "timeline.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    evt = json.loads(lines[0])
    assert evt["type"] == "state.transition"
    assert evt["pkg"] == "setuptools"
    assert evt["data"] == {"from": "a", "to": "b"}
    assert evt["ts"].startswith("20")  # ISO 时间戳


def test_write_event_data_none_becomes_empty_dict(tmp_path):
    tl.write_event(tmp_path, "session.completed", "pkg")
    evt = json.loads((tmp_path / "timeline.jsonl").read_text())
    assert evt["data"] == {}


def test_write_event_keeps_non_ascii(tmp_path):
    tl.write_event(tmp_path, "error", "pkg", {"message": "中文错误"})
    evt = json.loads((tmp_path / "timeline.jsonl").read_text())
    assert evt["data"]["message"] == "中文错误"


def test_write_event_failure_is_best_effort(tmp_path, capsys):
    # session_dir 是文件 → open 抛 OSError → 只打 stderr 不抛异常
    f = tmp_path / "not_a_dir"
    f.write_text("x")
    tl.write_event(f, "error", "pkg")
    err = capsys.readouterr().err
    assert "write failed" in err


# ─────────────────────────────────────────────
# read_events
# ─────────────────────────────────────────────

def test_read_events_missing_file_returns_empty(tmp_path):
    assert tl.read_events(tmp_path) == []


def test_read_events_filters(tmp_path):
    tl.write_event(tmp_path, "state.transition", "pkgA", {"to": "b"})
    tl.write_event(tmp_path, "error", "pkgA", {"message": "x"})
    tl.write_event(tmp_path, "state.transition", "pkgB", {"to": "c"})

    assert len(tl.read_events(tmp_path)) == 3
    assert len(tl.read_events(tmp_path, pkg="pkgA")) == 2
    assert len(tl.read_events(tmp_path, type_="error")) == 1
    assert len(tl.read_events(tmp_path, pkg="pkgB", type_="state.transition")) == 1
    # since 过滤:按 ts 字符串比较
    since = "2099-01-01T00:00:00Z"
    assert tl.read_events(tmp_path, since=since) == []
    assert tl.read_events(tmp_path, since="2000-01-01T00:00:00Z") != []


def test_read_events_skips_bad_lines(tmp_path):
    (tmp_path / "timeline.jsonl").write_text(
        '{"ts":"1","type":"a","pkg":"p","data":{}}\n'
        "not-json\n"
        "\n"
    )
    events = tl.read_events(tmp_path)
    assert len(events) == 1
    assert events[0]["type"] == "a"


# ─────────────────────────────────────────────
# _snapshot_statuses
# ─────────────────────────────────────────────

def test_snapshot_from_dep_registry(tmp_path):
    (tmp_path / "dep_registry.json").write_text(json.dumps({
        "dep1": {"status": "build_done"},
        "dep2": {"status": "failed"},
        "dep3": {"no_status": True},   # 无 status 字段 → 跳过
        "dep4": "not-a-dict",          # 非 dict → 跳过
    }))
    assert tl._snapshot_statuses(tmp_path) == {"dep1": "build_done", "dep2": "failed"}


def test_snapshot_from_workflow_build_result(tmp_path):
    (tmp_path / "dep_registry.json").write_text(json.dumps({}))
    (tmp_path / "workflow_main.json").write_text(json.dumps({"pkgname": "mainpkg"}))
    pkg_dir = tmp_path / "pkgs" / "mainpkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "build_rpm_result.json").write_text(json.dumps({"status": "succeeded"}))
    assert tl._snapshot_statuses(tmp_path) == {"mainpkg": "main:succeeded"}


@pytest.mark.parametrize("decision,expected", [
    ("reuse_official", "main:reused"),
    ("reuse_copr_project", "main:reused"),
    ("reuse_additional_repo", "main:reused"),
    ("introduce_new", "main:evaluated"),
    ("evaluate", "main:evaluated"),
])
def test_snapshot_gate_decision(tmp_path, decision, expected):
    (tmp_path / "dep_registry.json").write_text(json.dumps({}))
    (tmp_path / "workflow_main.json").write_text(json.dumps({"pkgname": "mainpkg"}))
    pkg_dir = tmp_path / "pkgs" / "mainpkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "gate_result_mainpkg.json").write_text(json.dumps({
        "result": {"decision": decision},
    }))
    assert tl._snapshot_statuses(tmp_path) == {"mainpkg": expected}


def test_snapshot_pending_when_no_gate(tmp_path):
    (tmp_path / "dep_registry.json").write_text(json.dumps({}))
    (tmp_path / "workflow_main.json").write_text(json.dumps({"pkgname": "mainpkg"}))
    (tmp_path / "pkgs" / "mainpkg").mkdir(parents=True)
    assert tl._snapshot_statuses(tmp_path) == {"mainpkg": "main:pending"}


def test_snapshot_empty_session(tmp_path):
    assert tl._snapshot_statuses(tmp_path) == {}


# ─────────────────────────────────────────────
# diff_and_write_transitions
# ─────────────────────────────────────────────

def test_diff_writes_transitions_for_changes(tmp_path):
    (tmp_path / "dep_registry.json").write_text(json.dumps({"dep1": {"status": "build_done"}}))
    before = {"dep1": "pending", "dep2": "build_done"}

    after = tl.diff_and_write_transitions(tmp_path, before)

    assert after == {"dep1": "build_done"}
    events = tl.read_events(tmp_path, type_="state.transition")
    assert len(events) == 1
    assert events[0]["pkg"] == "dep1"
    assert events[0]["data"]["from"] == "pending"
    assert events[0]["data"]["to"] == "build_done"
    assert events[0]["data"]["reason"] == "supervisor"


def test_diff_new_pkg_from_empty_before(tmp_path):
    (tmp_path / "dep_registry.json").write_text(json.dumps({"dep1": {"status": "reused"}}))
    after = tl.diff_and_write_transitions(tmp_path, {})
    assert after == {"dep1": "reused"}
    events = tl.read_events(tmp_path, type_="state.transition")
    assert events[0]["data"]["from"] == "(new)"


def test_diff_no_change_no_events(tmp_path):
    (tmp_path / "dep_registry.json").write_text(json.dumps({"dep1": {"status": "build_done"}}))
    tl.diff_and_write_transitions(tmp_path, {"dep1": "build_done"})
    assert tl.read_events(tmp_path) == []


# ─────────────────────────────────────────────
# main(CLI 胶水)
# ─────────────────────────────────────────────

def _main_with_argv(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["timeline.py"] + argv)
    return tl.main()


def test_main_write_mode(tmp_path, capsys, monkeypatch):
    rc = _main_with_argv(monkeypatch, ["--session-dir", str(tmp_path), "--type", "error",
                                       "--pkg", "p", "--data", '{"message": "x"}'])
    assert rc == 0
    evt = json.loads((tmp_path / "timeline.jsonl").read_text())
    assert evt["data"]["message"] == "x"


def test_main_write_invalid_json(tmp_path, capsys, monkeypatch):
    rc = _main_with_argv(monkeypatch, ["--session-dir", str(tmp_path), "--type", "error",
                                       "--data", "{bad"])
    assert rc == 1
    assert "invalid JSON" in capsys.readouterr().err


def test_main_read_json_mode(tmp_path, capsys, monkeypatch):
    tl.write_event(tmp_path, "error", "pkgA", {"message": "x"})
    tl.write_event(tmp_path, "state.transition", "pkgB", {"to": "c"})
    rc = _main_with_argv(monkeypatch, ["--session-dir", str(tmp_path), "--format", "json",
                                       "--pkg", "pkgB"])
    assert rc == 0
    events = json.loads(capsys.readouterr().out)
    assert len(events) == 1
    assert events[0]["type"] == "state.transition"


def test_main_read_table_mode_empty(tmp_path, capsys, monkeypatch):
    rc = _main_with_argv(monkeypatch, ["--session-dir", str(tmp_path), "--format", "table"])
    assert rc == 0
    assert "(no events)" in capsys.readouterr().out
