"""step_supervisor.determine_action — 状态机编排主分支(第 3 组)。

策略:构造 session 目录状态,mock _poll_copr_build/_poll_copr_build_chroots
后驱动 determine_action 的主路径。
"""

from __future__ import annotations

import json
import sys

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sys.path.insert(0, str(SCRIPT_DIRS["step"]))
sys.path.insert(0, str(SCRIPT_DIRS["build_rpm"]))

ss = load_module("step_supervisor", SCRIPT_DIRS["step"] / "step_supervisor.py")


def _mk_session(tmp_path, chroots=None):
    """标准 session:可选多 chroot。"""
    data = {"copr_login": "u", "copr_token": "t"}
    if chroots:
        data["copr_chroots"] = chroots
    else:
        data["copr_chroot"] = "openeuler-24.03-x86_64"
    (tmp_path / "session.json").write_text(json.dumps(data))
    (tmp_path / "pkgs").mkdir(exist_ok=True)
    return tmp_path


def _wf(tmp_path, pkgname="main"):
    wf_path = tmp_path / f"workflow_{pkgname}.json"
    wf = {"pkgname": pkgname}
    wf_path.write_text(json.dumps(wf))
    return wf


def _reg(tmp_path, data):
    (tmp_path / "dep_registry.json").write_text(json.dumps(data))
    return data


def _gate(tmp_path, pkg="main", decision="introduce_new"):
    pkg_dir = tmp_path / "pkgs" / pkg
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / f"gate_result_{pkg}.json").write_text(json.dumps({
        "overall_status": "done", "result": {"decision": decision},
    }))


def _main_result(tmp_path, status="copr_running", pkg="main", **extra):
    pkg_dir = tmp_path / "pkgs" / pkg
    pkg_dir.mkdir(parents=True, exist_ok=True)
    data = {"status": status, **extra}
    (pkg_dir / "build_rpm_result.json").write_text(json.dumps(data))
    return data


# ─────────────────────────────────────────────
# 优先级 -1:evaluate_main 失败
# ─────────────────────────────────────────────

def test_evaluate_failed_no_analysis(tmp_path):
    _mk_session(tmp_path)
    wf = _wf(tmp_path)
    wf["evaluate_failed"] = "boom"
    _gate(tmp_path)  # gate 存在但 evaluate_failed 优先
    assert ss.determine_action(tmp_path, wf, {}) == ("analyze_evaluate_main", "main", 0)


def test_evaluate_failed_retry(tmp_path):
    _mk_session(tmp_path)
    wf = _wf(tmp_path)
    wf["evaluate_failed"] = "boom"
    pkg_dir = tmp_path / "pkgs" / "main"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "evaluate_analysis_main.json").write_text(json.dumps(
        {"verdict": "retry", "suggestion": "try version 2.0"}))
    _gate(tmp_path)

    action, target, delay = ss.determine_action(tmp_path, wf, {})
    assert action == "evaluate_main"
    assert delay == 60
    # 分析文件删除 + retry hint 写入 + gate_result 删除 + evaluate_failed 清除
    assert not (pkg_dir / "evaluate_analysis_main.json").exists()
    assert (pkg_dir / "evaluate_retry_hint.txt").read_text() == "try version 2.0"
    assert not (pkg_dir / "gate_result_main.json").exists()
    assert "evaluate_failed" not in wf


def test_evaluate_failed_abort(tmp_path):
    _mk_session(tmp_path)
    wf = _wf(tmp_path)
    wf["evaluate_failed"] = "boom"
    pkg_dir = tmp_path / "pkgs" / "main"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "evaluate_analysis_main.json").write_text(json.dumps(
        {"verdict": "abort", "reason": "license unknown"}))
    action, target, delay = ss.determine_action(tmp_path, wf, {})
    assert action == "fail"
    assert "license unknown" in target
    assert delay is None


# ─────────────────────────────────────────────
# 优先级 0:主包 gate
# ─────────────────────────────────────────────

def test_gate_missing(tmp_path):
    _mk_session(tmp_path)
    wf = _wf(tmp_path)
    action, target, delay = ss.determine_action(tmp_path, wf, {})
    assert (action, target, delay) == ("evaluate_main", "main", 60)


def test_gate_invalid_deleted(tmp_path):
    _mk_session(tmp_path)
    wf = _wf(tmp_path)
    _gate(tmp_path)
    # 覆盖为无效 gate
    (tmp_path / "pkgs" / "main" / "gate_result_main.json").write_text('{bad')
    action, _, _ = ss.determine_action(tmp_path, wf, {})
    assert action == "evaluate_main"
    assert not (tmp_path / "pkgs" / "main" / "gate_result_main.json").exists()


def test_gate_check_failed_retry_long_delay(tmp_path):
    _mk_session(tmp_path)
    wf = _wf(tmp_path)
    pkg_dir = tmp_path / "pkgs" / "main"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "gate_result_main.json").write_text(json.dumps({
        "overall_status": "done", "result": {"decision": "check_failed"},
    }))
    action, _, delay = ss.determine_action(tmp_path, wf, {})
    assert action == "evaluate_main"
    assert delay == 120
    assert not (pkg_dir / "gate_result_main.json").exists()


def test_goal_achieved_done(tmp_path):
    _mk_session(tmp_path)
    wf = _wf(tmp_path)
    wf["goal_achieved"] = True
    _gate(tmp_path, decision="reuse_official")
    assert ss.determine_action(tmp_path, wf, {}) == ("done", "main", None)


# ─────────────────────────────────────────────
# 优先级 1:dep evaluate
# ─────────────────────────────────────────────

def test_dep_evaluate_failed_no_analysis(tmp_path):
    _mk_session(tmp_path)
    wf = _wf(tmp_path)
    _gate(tmp_path)
    reg = _reg(tmp_path, {"dep1": {"status": "evaluate_failed", "error": "x"}})
    action, target, delay = ss.determine_action(tmp_path, wf, reg)
    assert (action, target) == ("analyze_evaluate", "dep1")


def test_dep_evaluate_failed_retry(tmp_path):
    _mk_session(tmp_path)
    wf = _wf(tmp_path)
    _gate(tmp_path)
    reg = _reg(tmp_path, {"dep1": {"status": "evaluate_failed", "error": "x"}})
    pkg_dir = tmp_path / "pkgs" / "dep1"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "evaluate_analysis_dep1.json").write_text(json.dumps(
        {"verdict": "retry", "suggestion": "fix url"}))
    action, target, delay = ss.determine_action(tmp_path, wf, reg)
    assert (action, target) == ("evaluate", "dep1")
    assert reg["dep1"]["status"] == "pending_evaluate"
    assert (pkg_dir / "evaluate_retry_hint.txt").read_text() == "fix url"


def test_dep_vendor_lang_intercept(tmp_path):
    """lang=go/rust 的 dep → 直接 vendor_only,不 evaluate。"""
    _mk_session(tmp_path)
    wf = _wf(tmp_path)
    _gate(tmp_path)
    reg = _reg(tmp_path, {"crate1": {"status": "pending_evaluate", "lang": "rust"}})
    action, target, delay = ss.determine_action(tmp_path, wf, reg)
    assert reg["crate1"]["status"] == "vendor_only"
    # 递归后无 dep 工作 → 主包尚未构建 → build_main
    assert (action, target) == ("build_main", "main")


def test_dep_ros_intercept(tmp_path):
    """lang=ros 的 dep → evaluate_done + 伪 gate_result。"""
    _mk_session(tmp_path)
    wf = _wf(tmp_path)
    _gate(tmp_path)
    reg = _reg(tmp_path, {"rosdep": {"status": "pending_evaluate", "lang": "ros"}})
    action, target, delay = ss.determine_action(tmp_path, wf, reg)
    assert reg["rosdep"]["status"] == "evaluate_done"
    gate_path = tmp_path / "pkgs" / "rosdep" / "gate_result_rosdep.json"
    assert gate_path.exists()
    assert json.loads(gate_path.read_text())["result"]["decision"] == "introduce_new"


def test_dep_evaluate_no_url_resolve(tmp_path, fake_subprocess):
    """dep 无 url → resolve_upstream(subprocess 失败 → AI 兜底)。"""
    _mk_session(tmp_path)
    wf = _wf(tmp_path)
    _gate(tmp_path)
    fake_subprocess.when(lambda s: "resolve_upstream.py" in s, returncode=1)
    reg = _reg(tmp_path, {"dep1": {"status": "pending_evaluate"}})
    action, target, _ = ss.determine_action(tmp_path, wf, reg)
    assert (action, target) == ("resolve_upstream", "dep1")


def test_dep_evaluate_with_url(tmp_path):
    _mk_session(tmp_path)
    wf = _wf(tmp_path)
    _gate(tmp_path)
    reg = _reg(tmp_path, {"dep1": {"status": "pending_evaluate", "url": "https://github.com/x/y"}})
    action, target, delay = ss.determine_action(tmp_path, wf, reg)
    assert (action, target) == ("evaluate", "dep1")
    assert delay == 60


def test_dep_depth_exceeded_fail(tmp_path):
    _mk_session(tmp_path)
    wf = _wf(tmp_path)
    _gate(tmp_path)
    # 深链:dep4 → dep3 → dep2 → dep1 → main,超过 MAX_DEP_DEPTH(5)
    reg = {
        "dep1": {"status": "pending_evaluate", "url": "u", "required_by": "main"},
        "dep2": {"status": "pending_evaluate", "url": "u", "required_by": "dep1"},
        "dep3": {"status": "pending_evaluate", "url": "u", "required_by": "dep2"},
        "dep4": {"status": "pending_evaluate", "url": "u", "required_by": "dep3"},
        "dep5": {"status": "pending_evaluate", "url": "u", "required_by": "dep4"},
        "dep6": {"status": "pending_evaluate", "url": "u", "required_by": "dep5"},
    }
    _reg(tmp_path, reg)
    action, target, delay = ss.determine_action(tmp_path, wf, reg)
    assert action == "fail"
    assert "depth exceeded" in target


# ─────────────────────────────────────────────
# 优先级 2.5:dep copr_running 轮询(旧路径)
# ─────────────────────────────────────────────

def test_dep_copr_running_poll_succeeded(tmp_path, monkeypatch):
    _mk_session(tmp_path)
    wf = _wf(tmp_path)
    _gate(tmp_path)
    reg = _reg(tmp_path, {"dep1": {"status": "copr_running", "copr_build_id": 42}})
    monkeypatch.setattr(ss, "_poll_copr_build", lambda bid, sd: "succeeded")
    action, target, delay = ss.determine_action(tmp_path, wf, reg)
    assert reg["dep1"]["status"] == "build_done"
    assert "dep1" in wf["built_pkgs"]


def test_dep_copr_running_poll_failed(tmp_path, monkeypatch):
    _mk_session(tmp_path)
    wf = _wf(tmp_path)
    _gate(tmp_path)
    reg = _reg(tmp_path, {"dep1": {"status": "copr_running", "copr_build_id": 42}})
    monkeypatch.setattr(ss, "_poll_copr_build", lambda bid, sd: "failed")
    action, target, delay = ss.determine_action(tmp_path, wf, reg)
    assert reg["dep1"]["status"] == "build_failed"
    assert "failed" in reg["dep1"]["error"]


def test_dep_copr_running_poll_still_running(tmp_path, monkeypatch):
    _mk_session(tmp_path)
    wf = _wf(tmp_path)
    _gate(tmp_path)
    reg = _reg(tmp_path, {"dep1": {"status": "copr_running", "copr_build_id": 42}})
    monkeypatch.setattr(ss, "_poll_copr_build", lambda bid, sd: "running")
    action, target, delay = ss.determine_action(tmp_path, wf, reg)
    assert (action, delay) == ("wait", 60)
    assert "copr_running" in target


# ─────────────────────────────────────────────
# 优先级 3:dep build_failed
# ─────────────────────────────────────────────

def test_dep_build_failed_no_analysis_fix(tmp_path):
    _mk_session(tmp_path)
    wf = _wf(tmp_path)
    _gate(tmp_path)
    reg = _reg(tmp_path, {"dep1": {"status": "build_failed", "copr_build_id": 42}})
    pkg_dir = tmp_path / "pkgs" / "dep1"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "build_rpm_result.json").write_text(json.dumps(
        {"status": "failed", "copr_build_id": 42, "failure_reason": "compile error"}))
    action, target, _ = ss.determine_action(tmp_path, wf, reg)
    assert action == "fix_failure_dep"
    assert target == "dep1"


def test_dep_build_failed_builder_selfcheck_retry(tmp_path):
    """builder 自检失败(无 build_id)→ 回 builder 重建,超限后 fail。"""
    _mk_session(tmp_path)
    wf = _wf(tmp_path)
    _gate(tmp_path)
    reg = _reg(tmp_path, {"dep1": {"status": "build_failed"}})
    action, target, _ = ss.determine_action(tmp_path, wf, reg)
    assert action == "build_dep"
    assert reg["dep1"]["status"] == "evaluate_done"
    # 超限:预置 fix_rounds
    ss._bump_fix_round(tmp_path, "dep1")
    ss._bump_fix_round(tmp_path, "dep1")
    reg2 = _reg(tmp_path, {"dep1": {"status": "build_failed"}})
    action, target, delay = ss.determine_action(tmp_path, wf, reg2)
    assert action == "fail"
    assert delay is None


def test_dep_build_failed_mismatch_abort(tmp_path):
    """Package name mismatch 二次 → 强制 abort。"""
    _mk_session(tmp_path)
    wf = _wf(tmp_path)
    _gate(tmp_path)
    reg = _reg(tmp_path, {"dep1": {"status": "build_failed", "copr_build_id": 42}})
    pkg_dir = tmp_path / "pkgs" / "dep1"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "build_rpm_result.json").write_text(json.dumps(
        {"status": "failed", "copr_build_id": 42,
         "failure_reason": "Package name mismatch"}))
    ss._set_fix_counter(tmp_path, "dep1", "mismatch_count", 2)
    action, target, delay = ss.determine_action(tmp_path, wf, reg)
    assert action == "fail"
    assert "mismatch" in target


# ─────────────────────────────────────────────
# 主包分支
# ─────────────────────────────────────────────

def test_main_copr_running_wait(tmp_path, monkeypatch):
    _mk_session(tmp_path)
    wf = _wf(tmp_path)
    _gate(tmp_path)
    _main_result(tmp_path, status="copr_running", copr_build_id=7)
    monkeypatch.setattr(ss, "_poll_copr_build", lambda bid, sd: "running")
    action, target, delay = ss.determine_action(tmp_path, wf, {})
    assert action == "wait"
    assert delay == 60


def test_main_no_result_build_main(tmp_path):
    """主包无 build_rpm_result(尚未构建)→ build_main。"""
    _mk_session(tmp_path)
    wf = _wf(tmp_path)
    _gate(tmp_path)
    action, target, delay = ss.determine_action(tmp_path, wf, {})
    assert action == "build_main"
    assert target == "main"
    assert delay is not None
