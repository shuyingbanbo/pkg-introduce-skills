"""step_supervisor.py — 状态机引擎(第 1 组纯判定 + 第 2 组文件状态迁移)。

determine_action 编排组见 test_step_supervisor_determine_action.py。
"""

from __future__ import annotations

import json
import sys

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sys.path.insert(0, str(SCRIPT_DIRS["step"]))
sys.path.insert(0, str(SCRIPT_DIRS["build_rpm"]))

ss = load_module("step_supervisor", SCRIPT_DIRS["step"] / "step_supervisor.py")


# ─────────────────────────────────────────────
# session 读取 / chroot 目标
# ─────────────────────────────────────────────

def test_read_session_missing(tmp_path):
    assert ss._read_session(tmp_path) == {}


def test_read_session_bad_json(tmp_path):
    (tmp_path / "session.json").write_text("{bad")
    assert ss._read_session(tmp_path) == {}


def test_target_chroots_list_priority(tmp_path):
    (tmp_path / "session.json").write_text(json.dumps(
        {"copr_chroots": ["a", "b"], "copr_chroot": "old"}))
    assert ss._target_chroots(tmp_path) == ["a", "b"]


def test_target_chroots_old_single(tmp_path):
    (tmp_path / "session.json").write_text(json.dumps({"copr_chroot": "single"}))
    assert ss._target_chroots(tmp_path) == ["single"]
    assert ss._target_chroots(tmp_path) == ["single"]


def test_target_chroots_empty(tmp_path):
    (tmp_path / "session.json").write_text(json.dumps({}))
    assert ss._target_chroots(tmp_path) == []


def test_chroot_tracking(tmp_path):
    (tmp_path / "session.json").write_text(json.dumps({"copr_chroots": ["a"]}))
    assert ss._chroot_tracking(tmp_path) is True
    (tmp_path / "session.json").write_text(json.dumps({"copr_chroot": "a"}))
    assert ss._chroot_tracking(tmp_path) is False


# ─────────────────────────────────────────────
# _refresh_pkg_status / _blockers_of / 提交门控
# ─────────────────────────────────────────────

@pytest.mark.parametrize("agg,expected", [
    ("build_done", "build_done"),
    ("failed", "build_failed"),
    ("building", "copr_running"),
    ("pending", "evaluate_done"),  # pending 聚合映射为 evaluate_done(补交门控)
])
def test_refresh_pkg_status(agg, expected, monkeypatch):
    monkeypatch.setattr(ss, "_aggregate_status", lambda e, t: agg)
    assert ss._refresh_pkg_status({"status": "x"}, ["a"]) == expected


def test_refresh_pkg_status_vendor_only():
    assert ss._refresh_pkg_status({"status": "vendor_only"}, ["a"]) == "vendor_only"


def test_blockers_of():
    reg = {"dep1": {"required_by": "main"}, "dep2": {"required_by": "dep1"},
           "dep3": {"required_by": "main"}, "dep4": {"required_by": ""}}
    assert ss._blockers_of(reg, "main") == ["dep1", "dep3"]
    assert ss._blockers_of(reg, "dep1") == ["dep2"]


def test_submittable_chroots_skips_closed():
    reg = {"dep": {"chroots": {"a": {"status": "build_done"}, "b": {"status": "pending"}}}}
    assert ss._submittable_chroots(reg, "dep", ["a", "b"], blockers=[]) == ["b"]


def test_submittable_chroots_blockers_not_ready():
    reg = {
        "dep": {"status": "pending"},
        "blk": {"chroots": {"a": {"status": "pending"}}},
    }
    # dep 的 blocker=blk,blk 在 chroot a 未就绪 → dep 在 a 不可提交
    assert ss._submittable_chroots(reg, "dep", ["a"], blockers=["blk"]) == []


def test_submittable_chroots_blockers_ready():
    reg = {
        "dep": {"status": "pending"},
        "blk": {"chroots": {"a": {"status": "build_done"}}},
    }
    assert ss._submittable_chroots(reg, "dep", ["a"], blockers=["blk"]) == ["a"]


def test_submittable_chroots_old_format():
    """旧条目(无 chroots)按包级 status 就绪判断。"""
    reg = {"dep": {"status": "build_done"}, "blk": {"status": "build_done"}}
    assert ss._submittable_chroots(reg, "dep", ["a"], blockers=["blk"]) == ["a"]


def test_apply_doomed_chroots_cascade():
    reg = {
        "main": {"status": "pending", "chroots": {}},
        "dep": {"required_by": "main", "status": "pending",
                "chroots": {"a": {"status": "skipped"}, "b": {"status": "build_done"}}},
    }
    changed = ss._apply_doomed_chroots(reg, "main", ["a", "b"])
    assert changed is True
    assert reg["main"]["chroots"]["a"]["status"] == "skipped"
    assert reg["main"]["chroots"].get("b", {}).get("status") != "skipped"


def test_apply_doomed_chroots_no_blockers():
    reg = {"main": {"status": "pending"}}
    assert ss._apply_doomed_chroots(reg, "main", ["a"]) is False


def test_apply_doomed_chroots_already_closed():
    reg = {
        "main": {"status": "pending", "chroots": {"a": {"status": "build_done"}}},
        "dep": {"required_by": "main", "chroots": {"a": {"status": "skipped"}}},
    }
    assert ss._apply_doomed_chroots(reg, "main", ["a"]) is False


# ─────────────────────────────────────────────
# COPR 状态归一 / build_delay
# ─────────────────────────────────────────────

@pytest.mark.parametrize("state,expected", [
    ("succeeded", "succeeded"),
    ("failed", "failed"),
    ("canceled", "failed"),
    ("skipped", "failed"),
    ("running", "running"),
    ("pending", "running"),
    ("", "running"),  # 空串非终态 → running
])
def test_normalize_copr_state(state, expected):
    assert ss._normalize_copr_state(state) == expected


def test_build_delay_slow_langs():
    assert ss.build_delay("rust") > ss.build_delay("python")
    assert ss.build_delay("go") == ss.build_delay("c")
    assert ss.build_delay("nodejs") == ss.build_delay("python")


# ─────────────────────────────────────────────
# compute_depth / vendor 判定
# ─────────────────────────────────────────────

def test_compute_depth():
    reg = {
        "main": {"required_by": ""},
        "dep1": {"required_by": "main"},
        "dep2": {"required_by": "dep1"},
        "dep3": {"required_by": "dep2"},
    }
    assert ss.compute_depth("dep1", reg, "main") == 1
    assert ss.compute_depth("dep3", reg, "main") == 3


def test_compute_depth_unknown_parent():
    reg = {"dep": {"required_by": "ghost"}}
    assert ss.compute_depth("dep", reg, "main") == 1


def test_compute_depth_cycle_protection():
    reg = {"a": {"required_by": "b"}, "b": {"required_by": "a"}}
    assert ss.compute_depth("a", reg, "main") == 100  # 1 + 99(环检测)


def test_compute_depth_parent_is_main():
    reg = {"dep": {"required_by": "main"}}
    assert ss.compute_depth("dep", reg, "main") == 1


# ─────────────────────────────────────────────
# fix_state 计数器
# ─────────────────────────────────────────────

def test_fix_rounds_bump_clear(tmp_path):
    assert ss._fix_rounds(tmp_path, "pkg") == 0
    assert ss._bump_fix_round(tmp_path, "pkg") == 1
    assert ss._bump_fix_round(tmp_path, "pkg") == 2
    assert ss._fix_rounds(tmp_path, "pkg") == 2
    ss._clear_fix_counters(tmp_path, "pkg", "fix_round")
    assert ss._fix_rounds(tmp_path, "pkg") == 0


def test_fix_rounds_per_chroot(tmp_path):
    assert ss._bump_fix_round(tmp_path, "pkg", chroot="a") == 1
    assert ss._bump_fix_round(tmp_path, "pkg", chroot="a") == 2
    assert ss._bump_fix_round(tmp_path, "pkg", chroot="b") == 1
    # 包级不受影响
    assert ss._fix_rounds(tmp_path, "pkg") == 0
    assert ss._fix_rounds(tmp_path, "pkg", chroot="a") == 2


def test_set_and_clear_counter_chroot(tmp_path):
    ss._set_fix_counter(tmp_path, "pkg", "no_output_rounds", 3, chroot="a")
    assert ss._fix_rounds(tmp_path, "pkg") == 0  # 不是 fix_round
    state = ss._read_fix_state(tmp_path, "pkg")
    assert state["chroots"]["a"]["no_output_rounds"] == 3
    ss._clear_fix_counters(tmp_path, "pkg", "no_output_rounds", chroot="a")
    assert "no_output_rounds" not in ss._read_fix_state(tmp_path, "pkg")["chroots"]["a"]


def test_read_fix_state_legacy_fallback(tmp_path):
    """旧位置 build_rpm_result.no_output_rounds 作 fallback 读取。"""
    pkg_dir = tmp_path / "pkgs" / "pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "build_rpm_result.json").write_text(json.dumps({"no_output_rounds": 2}))
    assert ss._read_fix_state(tmp_path, "pkg").get("no_output_rounds") == 2


def test_current_build_id_priority(tmp_path):
    pkg_dir = tmp_path / "pkgs" / "pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "build_rpm_result.json").write_text(json.dumps({"copr_build_id": 11}))
    reg = {"pkg": {"chroots": {"a": {"build_id": 22}}}}
    # per-chroot 优先
    assert ss._current_build_id(tmp_path, "pkg", reg, chroot="a") == 22
    # 无 chroot 参数 → build_rpm_result 优先
    assert ss._current_build_id(tmp_path, "pkg", reg) == 11


# ─────────────────────────────────────────────
# _is_ros_session / _satisfies_constraint
# ─────────────────────────────────────────────

def test_is_ros_session(tmp_path):
    (tmp_path / "session.json").write_text(json.dumps({"import_type": "ros"}))
    assert ss._is_ros_session(tmp_path) is True
    (tmp_path / "session.json").write_text(json.dumps({"import_type": "pypi"}))
    assert ss._is_ros_session(tmp_path) is False


@pytest.mark.parametrize("version,constraint,expected", [
    ("1.5.0", ">=1.0", True),
    ("0.9.0", ">=1.0", False),
    ("1.5.0", ">=1.0,<2.0", True),
    ("2.5.0", ">=1.0,<2.0", False),
    ("1.5.0", "", True),
    ("1.5.0", "==1.5.0", True),
    ("1.5.0", "==1.6.0", False),
])
def test_satisfies_constraint(version, constraint, expected):
    assert ss._satisfies_constraint(version, constraint) is expected


# ─────────────────────────────────────────────
# 第 2 组:文件状态迁移
# ─────────────────────────────────────────────

def _wf(tmp_path, pkgname="main"):
    wf_path = tmp_path / f"workflow_{pkgname}.json"
    wf = {"pkgname": pkgname}
    wf_path.write_text(json.dumps(wf))
    return wf, wf_path


def test_record_built_pkg(tmp_path):
    wf, wf_path = _wf(tmp_path)
    ss._record_built_pkg(tmp_path, wf, "dep1")
    ss._record_built_pkg(tmp_path, wf, "dep1")  # 幂等
    ss._record_built_pkg(tmp_path, wf, "dep2")
    assert wf["built_pkgs"] == ["dep1", "dep2"]
    # 回写 workflow 文件
    assert json.loads(wf_path.read_text())["built_pkgs"] == ["dep1", "dep2"]


def test_update_after_evaluate_main_reuse(tmp_path):
    wf, wf_path = _wf(tmp_path)
    ss.update_after_evaluate_main(tmp_path, wf, wf_path, "reuse_official")
    assert wf["goal_achieved"] is True
    assert "main" in wf["reused_pkgs"]
    assert wf["loop_count"] == 1


def test_update_after_evaluate_main_introduce(tmp_path):
    wf, wf_path = _wf(tmp_path)
    ss.update_after_evaluate_main(tmp_path, wf, wf_path, "introduce_new")
    assert "goal_achieved" not in wf
    assert wf["loop_count"] == 1


def test_update_after_evaluate_main_failed(tmp_path):
    wf, wf_path = _wf(tmp_path)
    ss.update_after_evaluate_main(tmp_path, wf, wf_path, "")
    assert "evaluate_failed" in wf


def test_update_after_evaluate_reuse(tmp_path):
    reg_path = tmp_path / "dep_registry.json"
    reg = {"dep": {"status": "pending_evaluate"}}
    reg_path.write_text(json.dumps(reg))
    ss.update_after_evaluate(tmp_path, reg, reg_path, "dep", "reuse_official")
    assert reg["dep"]["status"] == "reused"


def test_update_after_evaluate_introduce(tmp_path):
    reg_path = tmp_path / "dep_registry.json"
    reg = {"dep": {"status": "pending_evaluate"}}
    reg_path.write_text(json.dumps(reg))
    ss.update_after_evaluate(tmp_path, reg, reg_path, "dep", "introduce_new")
    assert reg["dep"]["status"] == "evaluate_done"


def test_update_after_evaluate_failed(tmp_path):
    reg_path = tmp_path / "dep_registry.json"
    reg = {"dep": {"status": "pending_evaluate"}}
    reg_path.write_text(json.dumps(reg))
    ss.update_after_evaluate(tmp_path, reg, reg_path, "dep", "")
    assert reg["dep"]["status"] == "evaluate_failed"
    assert reg["dep"]["error"]


def test_promote_pending_deps_old_format(tmp_path):
    reg = {
        "dep": {"status": "pending_deps"},
        "blk": {"required_by": "dep", "status": "build_done"},
    }
    changed = ss._promote_pending_deps(reg, tracking=False, targets=[])
    assert changed is True
    assert reg["dep"]["status"] == "evaluate_done"


def test_promote_pending_deps_blocker_not_ready(tmp_path):
    reg = {
        "dep": {"status": "pending_deps"},
        "blk": {"required_by": "dep", "status": "pending"},
    }
    assert ss._promote_pending_deps(reg, tracking=False, targets=[]) is False
    assert reg["dep"]["status"] == "pending_deps"


def test_promote_pending_deps_tracking(tmp_path):
    reg = {
        "dep": {"status": "pending_deps"},
        "blk": {"required_by": "dep", "status": "pending",
                "chroots": {"a": {"status": "build_done"}}},
    }
    changed = ss._promote_pending_deps(reg, tracking=True, targets=["a"])
    assert changed is True
    assert reg["dep"]["status"] == "evaluate_done"


def test_sync_dep_result_failed(tmp_path):
    pkg_dir = tmp_path / "pkgs" / "dep"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "build_rpm_result.json").write_text(json.dumps({"status": "copr_running"}))
    ss._sync_dep_result_failed(tmp_path, "dep", "build failed")
    br = json.loads((pkg_dir / "build_rpm_result.json").read_text())
    assert br["status"] == "failed"
    assert br["failure_reason"] == "build failed"


def test_sync_dep_result_not_running_untouched(tmp_path):
    pkg_dir = tmp_path / "pkgs" / "dep"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "build_rpm_result.json").write_text(json.dumps({"status": "success"}))
    ss._sync_dep_result_failed(tmp_path, "dep", "x")
    br = json.loads((pkg_dir / "build_rpm_result.json").read_text())
    assert br["status"] == "success"


def test_update_after_build_dep_success(tmp_path):
    """dep 构建成功(旧 session)→ build_done。"""
    reg_path = tmp_path / "dep_registry.json"
    reg = {"dep": {"status": "copr_running"}}
    reg_path.write_text(json.dumps(reg))
    wf, wf_path = _wf(tmp_path)
    ss.update_after_build(tmp_path, wf, wf_path, reg, reg_path,
                          "dep", "success", is_dep=True)
    assert reg["dep"]["status"] == "build_done"
    assert "dep" in wf["built_pkgs"]


def test_update_after_build_copr_running_dep(tmp_path):
    reg_path = tmp_path / "dep_registry.json"
    reg = {"dep": {"status": "pending_evaluate"}}
    reg_path.write_text(json.dumps(reg))
    wf, wf_path = _wf(tmp_path)
    pkg_dir = tmp_path / "pkgs" / "dep"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "build_rpm_result.json").write_text(json.dumps({"copr_build_id": 77}))
    ss.update_after_build(tmp_path, wf, wf_path, reg, reg_path,
                          "dep", "copr_running", is_dep=True)
    assert reg["dep"]["status"] == "copr_running"
    assert reg["dep"]["copr_build_id"] == 77
