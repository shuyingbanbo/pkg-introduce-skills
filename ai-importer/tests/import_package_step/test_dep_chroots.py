"""dep_chroots.py — dep_registry per-chroot 就绪状态 helper(纯逻辑)。"""

from __future__ import annotations

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

dc = load_module("dep_chroots", SCRIPT_DIRS["step"] / "dep_chroots.py")


# ─────────────────────────────────────────────
# ready_for
# ─────────────────────────────────────────────

def test_ready_for_vendor_only_always_ready():
    assert dc.ready_for({"status": "vendor_only"}, "x86_64") is True
    assert dc.ready_for({"status": "vendor_only", "chroots": {"x86_64": {"status": "failed"}}}, "x86_64") is True


@pytest.mark.parametrize("status,expected", [
    ("build_done", True),
    ("reused", True),
    ("pending", False),
    ("building", False),
    ("failed", False),
    ("skipped", False),
    ("", False),
])
def test_ready_for_old_format(status, expected):
    assert dc.ready_for({"status": status}, "x86_64") is expected


@pytest.mark.parametrize("chroot_status,expected", [
    ("build_done", True),
    ("reused", True),
    ("pending", False),
    ("failed", False),
    ("building", False),
    ("skipped", False),
])
def test_ready_for_per_chroot(chroot_status, expected):
    entry = {"status": "pending", "chroots": {"x86_64": {"status": chroot_status}}}
    assert dc.ready_for(entry, "x86_64") is expected


def test_ready_for_missing_chroot_not_ready():
    entry = {"status": "pending", "chroots": {"x86_64": {"status": "build_done"}}}
    assert dc.ready_for(entry, "aarch64") is False


def test_ready_for_chroot_entry_not_dict():
    entry = {"chroots": {"x86_64": "build_done"}}  # 非法结构
    assert dc.ready_for(entry, "x86_64") is False


def test_ready_for_non_dict_entry():
    assert dc.ready_for(None, "x86_64") is False
    assert dc.ready_for("string", "x86_64") is False


def test_ready_for_pkg_status_takes_precedence_without_chroots_key():
    # 无 chroots 键 → 退化包级 status
    assert dc.ready_for({"status": "build_done"}, "x86_64") is True


# ─────────────────────────────────────────────
# aggregate_status
# ─────────────────────────────────────────────

def test_aggregate_status_any_failed():
    entry = {"chroots": {"a": {"status": "build_done"}, "b": {"status": "failed"}}}
    assert dc.aggregate_status(entry, ["a", "b"]) == "failed"


def test_aggregate_status_all_ready_or_skipped():
    entry = {"chroots": {"a": {"status": "build_done"}, "b": {"status": "reused"}}}
    assert dc.aggregate_status(entry, ["a", "b"]) == "build_done"
    entry = {"chroots": {"a": {"status": "build_done"}, "b": {"status": "skipped"}}}
    assert dc.aggregate_status(entry, ["a", "b"]) == "build_done"


def test_aggregate_status_all_skipped_is_failed():
    entry = {"chroots": {"a": {"status": "skipped"}, "b": {"status": "skipped"}}}
    assert dc.aggregate_status(entry, ["a", "b"]) == "failed"


def test_aggregate_status_building_wins_over_pending():
    entry = {"chroots": {"a": {"status": "building"}, "b": {"status": "pending"}}}
    assert dc.aggregate_status(entry, ["a", "b"]) == "building"


def test_aggregate_status_pending_fallback():
    entry = {"chroots": {"a": {"status": "pending"}}}
    assert dc.aggregate_status(entry, ["a"]) == "pending"
    # chroot 不在映射中按 pending 计
    entry = {"chroots": {"a": {"status": "build_done"}}}
    assert dc.aggregate_status(entry, ["a", "missing"]) == "pending"


def test_aggregate_status_old_format_passthrough():
    assert dc.aggregate_status({"status": "build_done"}, ["a"]) == "build_done"
    assert dc.aggregate_status({"status": "failed"}, ["a"]) == "failed"
    assert dc.aggregate_status({}, ["a"]) == "pending"


def test_aggregate_status_vendor_only():
    assert dc.aggregate_status({"status": "vendor_only"}, ["a"]) == "vendor_only"
    assert dc.aggregate_status({"status": "vendor_only", "chroots": {"a": {"status": "failed"}}}, ["a"]) == "vendor_only"


def test_aggregate_status_non_dict():
    assert dc.aggregate_status(None, ["a"]) == "pending"
    assert dc.aggregate_status("x", ["a"]) == "pending"


def test_aggregate_status_empty_target_chroots():
    # 空列表 → "statuses and" 短路 → 落到 pending
    entry = {"chroots": {"a": {"status": "build_done"}}}
    assert dc.aggregate_status(entry, []) == "pending"


# ─────────────────────────────────────────────
# chroot_status_map
# ─────────────────────────────────────────────

def test_chroot_status_map_basic():
    entry = {"chroots": {"a": {"status": "build_done", "build_id": 42},
                          "b": {"status": "pending"}}}
    assert dc.chroot_status_map(entry) == {
        "a": {"status": "build_done", "build_id": 42},
        "b": {"status": "pending", "build_id": None},
    }


def test_chroot_status_map_no_chroots():
    assert dc.chroot_status_map({}) == {}
    assert dc.chroot_status_map({"status": "build_done"}) == {}
    assert dc.chroot_status_map(None) == {}


def test_chroot_status_map_skips_non_dict_entries():
    entry = {"chroots": {"a": "build_done", "b": {"status": "reused"}}}
    assert dc.chroot_status_map(entry) == {"b": {"status": "reused", "build_id": None}}
