"""step_supervisor.main / print_progress — CLI 胶水与进展输出。"""

from __future__ import annotations

import json
import sys

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sys.path.insert(0, str(SCRIPT_DIRS["step"]))
sys.path.insert(0, str(SCRIPT_DIRS["build_rpm"]))

ss = load_module("step_supervisor", SCRIPT_DIRS["step"] / "step_supervisor.py")


def _session(tmp_path):
    (tmp_path / "session.json").write_text(json.dumps({"copr_chroot": "c"}))
    (tmp_path / "pkgs").mkdir(exist_ok=True)
    return tmp_path


def _wf(tmp_path, pkgname="main"):
    wf_path = tmp_path / f"workflow_{pkgname}.json"
    wf_path.write_text(json.dumps({"pkgname": pkgname}))
    return wf_path


def _main(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["step_supervisor.py"] + argv)
    return ss.main()


# ─────────────────────────────────────────────
# main:更新模式
# ─────────────────────────────────────────────

def test_main_no_workflow(tmp_path, monkeypatch, capsys):
    _session(tmp_path)
    rc = _main(monkeypatch, ["--session-dir", str(tmp_path)])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "no workflow file found"


def test_main_update_evaluate_main_reuse(tmp_path, monkeypatch, capsys):
    _session(tmp_path)
    wf_path = _wf(tmp_path)
    (tmp_path / "dep_registry.json").write_text(json.dumps({}))
    rc = _main(monkeypatch, ["--session-dir", str(tmp_path),
                             "--update-action", "evaluate_main",
                             "--gate-decision", "reuse_official"])
    assert rc == 0
    wf = json.loads(wf_path.read_text())
    assert wf["goal_achieved"] is True
    assert "main" in wf["reused_pkgs"]
    assert wf["loop_count"] == 1
    # 快照无状态变化 → 不产生 transition 事件(timeline.jsonl 可不创建)
    assert json.loads(capsys.readouterr().out) == {"updated": True}


def test_main_update_evaluate_dep(tmp_path, monkeypatch, capsys):
    _session(tmp_path)
    wf_path = _wf(tmp_path)
    reg_path = tmp_path / "dep_registry.json"
    reg_path.write_text(json.dumps({"dep1": {"status": "pending_evaluate"}}))
    rc = _main(monkeypatch, ["--session-dir", str(tmp_path),
                             "--update-action", "evaluate",
                             "--update-target", "dep1",
                             "--gate-decision", "introduce_new"])
    assert rc == 0
    reg = json.loads(reg_path.read_text())
    assert reg["dep1"]["status"] == "evaluate_done"


def test_main_update_build_dep(tmp_path, monkeypatch, capsys):
    _session(tmp_path)
    wf_path = _wf(tmp_path)
    reg_path = tmp_path / "dep_registry.json"
    reg_path.write_text(json.dumps({"dep1": {"status": "copr_running"}}))
    rc = _main(monkeypatch, ["--session-dir", str(tmp_path),
                             "--update-action", "build_dep",
                             "--update-target", "dep1",
                             "--build-result", "success"])
    assert rc == 0
    reg = json.loads(reg_path.read_text())
    assert reg["dep1"]["status"] == "build_done"


def test_main_update_done(tmp_path, monkeypatch, capsys):
    _session(tmp_path)
    wf_path = _wf(tmp_path)
    rc = _main(monkeypatch, ["--session-dir", str(tmp_path), "--update-action", "done"])
    assert rc == 0
    wf = json.loads(wf_path.read_text())
    assert wf["goal_achieved"] is True


def test_main_update_fail(tmp_path, monkeypatch, capsys):
    _session(tmp_path)
    wf_path = _wf(tmp_path)
    rc = _main(monkeypatch, ["--session-dir", str(tmp_path),
                             "--update-action", "fail", "--update-target", "compile error"])
    assert rc == 0
    wf = json.loads(wf_path.read_text())
    assert wf["goal_achieved"] is False
    assert wf["error"] == "compile error"


# ─────────────────────────────────────────────
# main:读状态模式
# ─────────────────────────────────────────────

def test_main_read_mode_returns_action(tmp_path, monkeypatch, capsys):
    _session(tmp_path)
    wf_path = _wf(tmp_path)
    (tmp_path / "dep_registry.json").write_text(json.dumps({}))
    rc = _main(monkeypatch, ["--session-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ACTION=evaluate_main" in out  # 无 gate → evaluate_main
    assert "TARGET=main" in out


def test_main_read_mode_with_gate(tmp_path, monkeypatch, capsys):
    _session(tmp_path)
    wf_path = _wf(tmp_path)
    (tmp_path / "dep_registry.json").write_text(json.dumps({}))
    pkg_dir = tmp_path / "pkgs" / "main"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "gate_result_main.json").write_text(json.dumps({
        "overall_status": "done", "result": {"decision": "introduce_new"},
    }))
    rc = _main(monkeypatch, ["--session-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ACTION=build_main" in out


# ─────────────────────────────────────────────
# print_progress
# ─────────────────────────────────────────────

def test_print_progress_basic(tmp_path, capsys):
    _session(tmp_path)
    wf = {"pkgname": "main", "loop_count": 3}
    reg = {
        "dep1": {"status": "build_done"},
        "dep2": {"status": "build_failed"},
        "dep3": {"status": "pending_evaluate", "required_by": "main"},
    }
    ss.print_progress(tmp_path, wf, reg, "build_main", "main")
    out = capsys.readouterr().out
    assert "主包  main" in out
    assert "第 4 步" in out  # loop_count + 1
    assert "已就绪 1 个" in out
    assert "dep2" in out
    assert "build_main" in out


def test_print_progress_no_deps(tmp_path, capsys):
    _session(tmp_path)
    wf = {"pkgname": "main"}
    ss.print_progress(tmp_path, wf, {}, "done", "main")
    out = capsys.readouterr().out
    assert "依赖  无" in out


def test_print_progress_with_main_result(tmp_path, capsys):
    _session(tmp_path)
    wf = {"pkgname": "main"}
    pkg_dir = tmp_path / "pkgs" / "main"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "build_rpm_result.json").write_text(json.dumps({"status": "success"}))
    ss.print_progress(tmp_path, wf, {}, "done", "main")
    out = capsys.readouterr().out
    assert "构建成功" in out  # _MAIN_STATUS_LABEL 映射
