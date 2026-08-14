"""run_evaluate_dep.py — 无 AI 依赖评估(run_check → run_gate → registry,全 subprocess mock)。"""

from __future__ import annotations

import json
import sys

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sys.path.insert(0, str(SCRIPT_DIRS["step"]))
rd_ = load_module("run_evaluate_dep", SCRIPT_DIRS["step"] / "run_evaluate_dep.py")


def _session(tmp_path, registry=None):
    (tmp_path / "session.json").write_text(json.dumps({
        "copr_url": "http://copr-frontend:5000", "copr_login": "u", "copr_token": "t",
        "copr_chroot": "openeuler-24.03-x86_64",
    }))
    if registry is not None:
        (tmp_path / "dep_registry.json").write_text(json.dumps(registry))
    (tmp_path / "pkgs" / "dep1").mkdir(parents=True)
    return tmp_path


def _gate_result(tmp_path, pkg="dep1", decision="introduce_new", lang="python", version="1.0"):
    (tmp_path / "pkgs" / pkg / f"gate_result_{pkg}.json").write_text(json.dumps({
        "result": {"decision": decision, "lang": lang, "version": version},
    }))


def _run(fake_subprocess, tmp_path, **kwargs):
    """构造 run() 调用;subprocess 按脚本名分发。"""
    opts = dict(session_dir=tmp_path, pkgname="dep1", mode="dependency",
                url="https://github.com/x/dep1")
    opts.update(kwargs)
    return rd_.run(**opts)


def test_check_hard_failure(fake_subprocess, tmp_path):
    fake_subprocess.when(lambda s: "run_check.py" in s, returncode=1, stderr="boom")
    result = _run(fake_subprocess, tmp_path)
    assert result["status"] == "failed"
    assert result["stage"] == "check"
    assert "boom" in result["reason"]


def test_check_needs_ai(fake_subprocess, tmp_path):
    fake_subprocess.when(lambda s: "run_check.py" in s, returncode=2)
    result = _run(fake_subprocess, tmp_path)
    assert result["status"] == "needs_ai"
    assert result["stage"] == "check"
    assert result["check_result"].endswith("check_result_dep1.json")


def test_gate_failure(fake_subprocess, tmp_path):
    _session(tmp_path)
    fake_subprocess.when(lambda s: "run_check.py" in s, returncode=0)
    fake_subprocess.when(lambda s: "run_gate.py" in s, returncode=1, stderr="gate boom")
    result = _run(fake_subprocess, tmp_path)
    assert result["status"] == "failed"
    assert result["stage"] == "gate"
    assert "gate boom" in result["reason"]


def test_gate_success_updates_registry(fake_subprocess, tmp_path):
    _session(tmp_path, registry={"dep1": {"url": "u", "constraint": "", "status": "pending_evaluate"}})
    _gate_result(tmp_path)
    fake_subprocess.when(lambda s: "run_check.py" in s, returncode=0)
    fake_subprocess.when(lambda s: "run_gate.py" in s, returncode=0)

    result = _run(fake_subprocess, tmp_path)
    assert result["status"] == "done"
    assert result["decision"] == "introduce_new"
    assert result["lang"] == "python"
    reg = json.loads((tmp_path / "dep_registry.json").read_text())
    assert reg["dep1"]["status"] == "evaluate_done"
    assert reg["dep1"]["lang"] == "python"


def test_no_update_registry_flag(fake_subprocess, tmp_path):
    _session(tmp_path, registry={"dep1": {"url": "u", "constraint": "", "status": "pending_evaluate"}})
    _gate_result(tmp_path)
    fake_subprocess.when(lambda s: "run_check.py" in s, returncode=0)
    fake_subprocess.when(lambda s: "run_gate.py" in s, returncode=0)

    result = _run(fake_subprocess, tmp_path, no_update_registry=True)
    assert result["status"] == "done"
    reg = json.loads((tmp_path / "dep_registry.json").read_text())
    assert reg["dep1"]["status"] == "pending_evaluate"  # 未更新


def test_top_level_runs_evaluate_deps(fake_subprocess, tmp_path):
    _session(tmp_path)
    _gate_result(tmp_path, decision="introduce_new", lang="python")
    (tmp_path / "sources" / "dep1").mkdir(parents=True)
    fake_subprocess.when(lambda s: "run_check.py" in s, returncode=0)
    fake_subprocess.when(lambda s: "run_gate.py" in s, returncode=0)
    fake_subprocess.when(lambda s: "evaluate-deps.py" in s, returncode=0)

    result = _run(fake_subprocess, tmp_path, mode="top-level")
    assert result["status"] == "done"
    assert fake_subprocess.called_with("evaluate-deps.py")
    # timeline 事件写入
    events = (tmp_path / "timeline.jsonl").read_text().strip().splitlines()
    assert any("evaluate_deps.end" in l for l in events)


def test_top_level_no_evaluate_deps_for_other_decisions(fake_subprocess, tmp_path):
    """reuse 决策不触发依赖评估。"""
    _session(tmp_path)
    _gate_result(tmp_path, decision="reuse_official", lang="python")
    (tmp_path / "sources" / "dep1").mkdir(parents=True)
    fake_subprocess.when(lambda s: "run_check.py" in s, returncode=0)
    fake_subprocess.when(lambda s: "run_gate.py" in s, returncode=0)

    result = _run(fake_subprocess, tmp_path, mode="top-level")
    assert result["status"] == "done"
    assert not fake_subprocess.called_with("evaluate-deps.py")


def test_gate_result_missing(fake_subprocess, tmp_path):
    _session(tmp_path)
    fake_subprocess.when(lambda s: "run_check.py" in s, returncode=0)
    fake_subprocess.when(lambda s: "run_gate.py" in s, returncode=0)
    result = _run(fake_subprocess, tmp_path)
    assert result["status"] == "failed"
    assert "gate_result not found" in result["reason"]


# ─────────────────────────────────────────────
# main(CLI)
# ─────────────────────────────────────────────

def test_main_needs_ai_returns_1(fake_subprocess, tmp_path, monkeypatch, capsys):
    _session(tmp_path)
    fake_subprocess.when(lambda s: "run_check.py" in s, returncode=2)
    monkeypatch.setattr("sys.argv", ["run_evaluate_dep.py", "--pkg", "dep1",
                                     "--url", "u", "--session-dir", str(tmp_path)])
    rc = rd_.main()
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "needs_ai"


def test_main_done_returns_0(fake_subprocess, tmp_path, monkeypatch, capsys):
    _session(tmp_path)
    _gate_result(tmp_path)
    fake_subprocess.when(lambda s: "run_check.py" in s, returncode=0)
    fake_subprocess.when(lambda s: "run_gate.py" in s, returncode=0)
    monkeypatch.setattr("sys.argv", ["run_evaluate_dep.py", "--pkg", "dep1",
                                     "--url", "u", "--session-dir", str(tmp_path)])
    rc = rd_.main()
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "done"
