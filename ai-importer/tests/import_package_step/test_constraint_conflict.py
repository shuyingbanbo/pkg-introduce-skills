"""constraint_conflict.py — 版本约束冲突检测与合并(100% 纯逻辑)。"""

from __future__ import annotations

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

cc = load_module("constraint_conflict", SCRIPT_DIRS["step"] / "constraint_conflict.py")


# ─────────────────────────────────────────────
# _parse_clauses / _version_tuple / _cmp
# ─────────────────────────────────────────────

@pytest.mark.parametrize("constraint,expected", [
    ("", []),
    (">=1.2", [(">=", "1.2")]),
    (">=1.2, !=1.5", [(">=", "1.2"), ("!=", "1.5")]),
    (">= 1.0, < 2.0", [(">=", "1.0"), ("<", "2.0")]),
    (">=1.0.0rc1", [(">=", "1.0.0rc1")]),
    ("^1.2.3", []),            # npm 写法解析不出,保守跳过
    ("~2.0.0", []),
    ("no-op-here", []),
])
def test_parse_clauses(constraint, expected):
    got = [(c.op, c.version) for c in cc._parse_clauses(constraint)]
    assert got == expected


@pytest.mark.parametrize("version,expected", [
    ("1.2.3", (1, 2, 3)),
    ("1.0.0rc1", (1, 0, 0, "rc", 1)),
    ("v2.3", ("v", 2, 3)),  # v 前缀保留为 token
    ("2.0.0.alpha.20260317", (2, 0, 0, "alpha", 20260317)),
    ("", ()),
])
def test_version_tuple(version, expected):
    assert cc._version_tuple(version) == expected


@pytest.mark.parametrize("a,b,expected", [
    ("1.2.3", "1.2.4", -1),
    ("1.2.4", "1.2.3", 1),
    ("1.2.3", "1.2.3", 0),
    ("1.0", "1.0.1", -1),          # 段数少 → 小
    ("2.0", "10.0", -1),           # 数字按数值比较
    ("1.0.0rc1", "1.0.0", -1),     # 预发布语义(修复后):1.0.0rc1 < 1.0.0
    ("abc", "abd", -1),
])
def test_cmp(a, b, expected):
    assert cc._cmp(a, b) == expected


# ─────────────────────────────────────────────
# has_conflict
# ─────────────────────────────────────────────

@pytest.mark.parametrize("old,new", [
    ("", ">=1.0"),        # 空约束
    (">=1.0", ""),
    (">=1.0", ">=1.0"),   # 相同约束
    ("^1.2.3", "~2.0.0"), # 都解析不出 → 保守放过
    ("not-a-constraint", ">=1.0"),
])
def test_has_conflict_conservative_pass(old, new):
    is_conflict, reason = cc.has_conflict(old, new)
    assert is_conflict is False
    assert reason == ""


def test_conflict_no_overlap():
    assert cc.has_conflict(">=2.0", "<=1.0")[0] is True


def test_conflict_boundary_exclusive():
    assert cc.has_conflict(">=1.0", "<1.0")[0] is True
    assert cc.has_conflict(">1.0", "<=1.0")[0] is True


def test_boundary_inclusive_overlap_ok():
    assert cc.has_conflict(">=1.0", "<=1.0")[0] is False
    assert cc.has_conflict(">1.0", "<2.0")[0] is False


def test_conflict_lower_overrides_upper():
    # 新旧约束各自都带区间,取更严格的下/上界后无重叠
    assert cc.has_conflict(">=1.0", ">=2.0, <=1.5")[0] is True
    assert cc.has_conflict("<=2.0", ">=1.5, <=1.0")[0] is True


@pytest.mark.parametrize("old,new", [
    ("==1.2", "==1.3"),
    ("==1.2", ">=2.0"),       # exact 低于下界
    ("==1.2", "<=1.0"),       # exact 高于上界
    ("==1.2", "==1.2, !=1.2"),
])
def test_conflict_exact_clause(old, new):
    assert cc.has_conflict(old, new)[0] is True


@pytest.mark.parametrize("old,new", [
    ("==1.2", ">=1.0"),
    ("==1.2", "<=1.5"),
    ("==1.2", "!=1.3"),
    (">=1.0", "!=1.5"),
    ("!=1.2", "!=1.3"),
])
def test_no_conflict_exact_clause(old, new):
    assert cc.has_conflict(old, new)[0] is False


def test_conflict_reason_is_nonempty():
    _, reason = cc.has_conflict(">=2.0", "<=1.0")
    assert reason


def test_has_conflict_does_not_raise_on_junk():
    # 解析异常路径(如版本含无法比较的类型)保守返回 False
    is_conflict, reason = cc.has_conflict(">=a.a", "<=b.b")
    assert is_conflict is False
    assert reason == ""


# ─────────────────────────────────────────────
# merge_constraints
# ─────────────────────────────────────────────

def test_merge_empty_sides():
    assert cc.merge_constraints("", ">=1.0") == ">=1.0"
    assert cc.merge_constraints(">=1.0", "") == ">=1.0"
    assert cc.merge_constraints("", "") == ""


def test_merge_same():
    assert cc.merge_constraints(">=1.0", ">=1.0") == ">=1.0"


def test_merge_dedup_and_order():
    merged = cc.merge_constraints(">=1.0, <2.0", "<2.0, !=1.5")
    assert merged == ">=1.0, <2.0, !=1.5"


def test_merge_commutative_clause_set():
    # 注意:docstring 声称"与调用顺序无关",实际拼接顺序保留 old→new,字符串不等;
    # 但子句集合相同(语义等价),按集合断言
    old, new = ">=1.0, !=1.2", "<=2.0"
    a = cc.merge_constraints(old, new)
    b = cc.merge_constraints(new, old)
    assert a != b
    assert set(a.split(", ")) == set(b.split(", "))


def test_merge_unparseable_falls_back_to_new():
    assert cc.merge_constraints("^1.2.3", "~2.0.0") == "~2.0.0"
