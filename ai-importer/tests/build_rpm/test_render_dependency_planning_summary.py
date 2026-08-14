"""render_dependency_planning_summary.py — 依赖规划摘要渲染(纯字符串拼接)。"""

from __future__ import annotations

import json
import sys

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sys.path.insert(0, str(SCRIPT_DIRS["build_rpm"]))
mod = load_module(
    "render_dependency_planning_summary",
    SCRIPT_DIRS["build_rpm"] / "render_dependency_planning_summary.py",
)


# ─────────────────────────────────────────────
# read_json
# ─────────────────────────────────────────────

def test_read_json(tmp_path):
    p = tmp_path / "plan.json"
    p.write_text(json.dumps({"pkgname": "demo", "executed": []}))
    assert mod.read_json(p) == {"pkgname": "demo", "executed": []}


def test_read_json_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        mod.read_json(tmp_path / "nope.json")


# ─────────────────────────────────────────────
# summarize_execution
# ─────────────────────────────────────────────

def test_summarize_execution_dep_name_priority():
    executed = [
        {"dep_name": "foo", "name": "ignored", "status": "done"},
        {"name": "bar", "status": "failed"},           # 无 dep_name → 用 name
    ]
    index = mod.summarize_execution(executed)
    assert set(index.keys()) == {"foo", "bar"}
    assert index["foo"]["status"] == "done"


def test_summarize_execution_empty_and_blank_skipped():
    executed = [
        {"dep_name": "  ", "name": None},
        {"name": ""},
        {},
    ]
    assert mod.summarize_execution(executed) == {}


def test_summarize_execution_strip_and_overwrite():
    executed = [
        {"dep_name": " foo "},
        {"dep_name": "foo", "status": "later-wins"},
    ]
    index = mod.summarize_execution(executed)
    assert list(index.keys()) == ["foo"]        # strip 后归一,后写覆盖
    assert index["foo"]["status"] == "later-wins"


# ─────────────────────────────────────────────
# render_summary
# ─────────────────────────────────────────────

FULL_PAYLOAD = {
    "pkgname": "demo-pkg",
    "requested_by": "parent-pkg",
    "summary": {"request_count": 3, "planned_count": 2, "blocked_count": 1},
    "planning_log": [
        {
            "name": "dep-a",
            "node_state": "planned",
            "input_constraint": ">= 1.0",
            "input_constraint_type": "version",
            "selected_strategy": "newest",
            "locked_version": "1.2.3",
            "candidates": ["1.2.3", "1.1.0"],
            "reason": "resolved by dnf",
        },
    ],
    "executed": [
        {
            "dep_name": "dep-a",
            "status": "built",
            "action": "introduce",
            "selected_candidate": "1.2.3",
            "version": "1.2.3-1",
            "candidate_trace": [
                {"candidate": "1.2.3", "status": "ok", "action": "build", "failure_reason": ""},
                {"candidate": "1.1.0", "status": "skipped", "action": "", "failure_reason": "too old"},
            ],
            "attempts": [
                {"candidate": "1.2.3", "status": "ok", "action": "build", "failure_reason": ""},
            ],
        },
    ],
}


def test_render_summary_full():
    text = mod.render_summary(FULL_PAYLOAD)
    lines = text.splitlines()
    assert lines[0] == "Dependency planning summary for demo-pkg"
    assert lines[1] == "Requested by: parent-pkg"
    assert "Requests: 3 | Planned: 2 | Blocked: 1" in text
    assert "- dep-a [planned]" in text
    assert "  constraint      : >= 1.0" in text
    assert "  constraint_type : version" in text
    assert "  strategy        : newest" in text
    assert "  locked_version  : 1.2.3" in text
    assert "  candidates      : 1.2.3, 1.1.0" in text
    assert "  reason          : resolved by dnf" in text
    assert "  execution       : built" in text
    assert "  action          : introduce" in text
    assert "  selected        : 1.2.3" in text
    assert "  locked_result   : 1.2.3-1" in text
    assert "  candidate_trace :" in text
    assert "- candidate=1.2.3; status=ok; action=build; failure=<none>" in text
    assert "- candidate=1.1.0; status=skipped; action=<none>; failure=too old" in text
    assert "  attempts        :" in text
    assert "- candidate=1.2.3; status=ok; action=build; failure=<none>" in text
    assert text.endswith("\n")


def test_render_summary_unknown_names():
    payload = {"planning_log": []}
    text = mod.render_summary(payload)
    assert "Dependency planning summary for <unknown>" in text
    assert "Requested by: <unknown>" in text


def test_render_summary_no_planning_log():
    text = mod.render_summary({})
    assert "Requests: 0 | Planned: 0 | Blocked: 0" in text
    assert "- No dependency planning records." in text
    assert text.endswith("\n")


def test_render_summary_counts_fallback_from_log():
    # summary 缺省时按 planning_log 的 node_state 统计
    payload = {
        "pkgname": "p",
        "planning_log": [
            {"name": "a", "node_state": "planned"},
            {"name": "b", "node_state": "planned"},
            {"name": "c", "node_state": "blocked"},
            {"name": "d", "node_state": "done"},
        ],
    }
    text = mod.render_summary(payload)
    assert "Requests: 4 | Planned: 2 | Blocked: 1" in text


def test_render_summary_minimal_item_defaults():
    payload = {
        "pkgname": "p",
        "planning_log": [{"name": "dep-x"}],
    }
    text = mod.render_summary(payload)
    assert "- dep-x [unknown]" in text
    assert "  constraint      : <none>" in text
    assert "  constraint_type : unknown" in text
    assert "  strategy        : <none>" in text
    assert "  locked_version  : <none>" in text
    assert "  candidates      : <none>" in text
    assert "  reason          : <none>" in text
    assert "execution" not in text        # 无执行记录不输出 execution 行


def test_render_summary_execution_no_match_by_name():
    payload = {
        "planning_log": [{"name": "dep-a"}],
        "executed": [{"dep_name": "dep-b", "status": "built"}],
    }
    text = mod.render_summary(payload)
    assert "execution" not in text


def test_render_summary_execution_requested_version_fallback():
    # locked_result:无 version 时回退 requested_version
    payload = {
        "planning_log": [{"name": "a"}],
        "executed": [{"dep_name": "a", "status": "built", "requested_version": "9.9"}],
    }
    text = mod.render_summary(payload)
    assert "  locked_result   : 9.9" in text


def test_render_summary_execution_defaults():
    payload = {
        "planning_log": [{"name": "a"}],
        "executed": [{"dep_name": "a"}],
    }
    text = mod.render_summary(payload)
    assert "  execution       : <unknown>" in text
    assert "  action          : <none>" in text
    assert "  selected        : <none>" in text
    assert "  locked_result   : <none>" in text


# ─────────────────────────────────────────────
# main
# ─────────────────────────────────────────────

def test_main_prints_rendered(tmp_path, capsys, monkeypatch):
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(FULL_PAYLOAD))
    monkeypatch.setattr(sys, "argv", ["render_dependency_planning_summary.py", "--input-json", str(p)])
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "Dependency planning summary for demo-pkg" in out
    assert out.endswith("\n")


def test_main_output_file(tmp_path, capsys, monkeypatch):
    p = tmp_path / "plan.json"
    p.write_text(json.dumps({"pkgname": "p", "planning_log": []}))
    out_txt = tmp_path / "sub" / "summary.txt"
    monkeypatch.setattr(sys, "argv", [
        "render_dependency_planning_summary.py", "--input-json", str(p), "-o", str(out_txt),
    ])
    assert mod.main() == 0
    assert out_txt.exists()
    assert "Dependency planning summary for p" in out_txt.read_text()


def test_main_missing_input(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "render_dependency_planning_summary.py", "--input-json", str(tmp_path / "nope.json"),
    ])
    assert mod.main() == 1
    assert "错误" in capsys.readouterr().err


def test_main_invalid_json(tmp_path, capsys, monkeypatch):
    p = tmp_path / "bad.json"
    p.write_text("{ not json")
    monkeypatch.setattr(sys, "argv", ["render_dependency_planning_summary.py", "--input-json", str(p)])
    assert mod.main() == 1
    assert "错误" in capsys.readouterr().err
