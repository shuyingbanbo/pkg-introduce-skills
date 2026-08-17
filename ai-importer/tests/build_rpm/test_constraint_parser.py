"""constraint_parser.py — 统一约束解析模块(纯逻辑)。

覆盖:
- normalize_npm_constraint: ^ / ~ 六条正则分支 + 无匹配原样返回
- parse_constraint: 全部 constraint_type 分类(exact / range / unbounded / unknown)
  及 requirement_info 驱动的 clauses / specifiers / exact_version / status 分支
- to_specifier_set / satisfies: 有 packaging 与无 packaging 两条实现路径
"""

from __future__ import annotations

import sys

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

mod = load_module("constraint_parser", SCRIPT_DIRS["build_rpm"] / "constraint_parser.py")


# ─────────────────────────────────────────────
# normalize_npm_constraint
# ─────────────────────────────────────────────

@pytest.mark.parametrize("requirement,expected", [
    ("^1.2.3", ">=1.2.3,<2.0.0"),
    ("^1.2", ">=1.2.0,<2.0.0"),
    ("^1", ">=1.0.0,<2.0.0"),
    ("^10.20", ">=10.20.0,<11.0.0"),       # 多位数 major/minor
    ("^0.4.2", ">=0.4.2,<1.0.0"),
    ("~1.2.3", ">=1.2.3,<1.3.0"),
    ("~1.2", ">=1.2.0,<1.3.0"),
    ("  ^1.2.3  ", ">=1.2.3,<2.0.0"),       # 首尾空白
])
def test_normalize_npm_constraint_matches(requirement, expected):
    assert mod.normalize_npm_constraint(requirement) == expected


@pytest.mark.parametrize("requirement,expected", [
    ("~1", "~1"),              # 无 ~major 模式 → 原样返回
    (">=1.0", ">=1.0"),        # 非 npm 写法原样
    ("1.2.3", "1.2.3"),
    ("", ""),
])
def test_normalize_npm_constraint_passthrough(requirement, expected):
    assert mod.normalize_npm_constraint(requirement) == expected


# ─────────────────────────────────────────────
# parse_constraint — npm ^/~ 归一
# ─────────────────────────────────────────────

@pytest.mark.parametrize("requirement,expected", [
    ("^1.2.3", ("range", {"specifiers": [
        {"operator": ">=", "version": "1.2.3"}, {"operator": "<", "version": "2.0.0"}]})),
    ("~2.0.0", ("range", {"specifiers": [
        {"operator": ">=", "version": "2.0.0"}, {"operator": "<", "version": "2.1.0"}]})),
])
def test_parse_constraint_npm(requirement, expected):
    assert mod.parse_constraint(requirement) == expected


# ─────────────────────────────────────────────
# parse_constraint — 基础分类(无 requirement_info)
# ─────────────────────────────────────────────

@pytest.mark.parametrize("requirement,expected", [
    ("", ("unbounded", {})),
    ("   ", ("unbounded", {})),
    (None, ("unbounded", {})),
])
def test_parse_constraint_unbounded(requirement, expected):
    assert mod.parse_constraint(requirement) == expected


@pytest.mark.parametrize("requirement,expected_type,expected_info", [
    (">=1.0", "range", {"specifiers": [{"operator": ">=", "version": "1.0"}]}),
    ("<=2.0", "range", {"specifiers": [{"operator": "<=", "version": "2.0"}]}),
    (">1.0", "range", {"specifiers": [{"operator": ">", "version": "1.0"}]}),
    ("<2.0", "range", {"specifiers": [{"operator": "<", "version": "2.0"}]}),
    ("!=1.5", "range", {"specifiers": [{"operator": "!=", "version": "1.5"}]}),
    ("~=1.8", "range", {"specifiers": [{"operator": "~=", "version": "1.8"}]}),
    (">=1.0,<2.0", "range", {"specifiers": [
        {"operator": ">=", "version": "1.0"}, {"operator": "<", "version": "2.0"}]}),
])
def test_parse_constraint_range_ops(requirement, expected_type, expected_info):
    ctype, info = mod.parse_constraint(requirement)
    assert ctype == expected_type
    assert info == expected_info


def test_parse_constraint_bare_version_is_unbounded():
    assert mod.parse_constraint("requests") == ("unbounded", {})


@pytest.mark.parametrize("requirement,expected", [
    ("==1.2.3", ("exact", {"specifiers": [{"operator": "==", "version": "1.2.3"}],
                           "exact_version": "1.2.3"})),
    ("===1.2.3", ("exact", {"specifiers": [{"operator": "===", "version": "1.2.3"}],
                            "exact_version": "1.2.3"})),
    ("requests==2.0", ("exact", {"exact_version": "2.0"})),
    ("requests===2.0", ("exact", {"exact_version": "2.0"})),
])
def test_parse_constraint_exact(requirement, expected):
    assert mod.parse_constraint(requirement) == expected


@pytest.mark.parametrize("requirement,expected_type", [
    ("requests>=2.0,<3.0", "range"),
    ("requests[security]>=2.0", "range"),                      # extras 剥离后仍 range
    ("requests>=2.0; python_version<'3.8'", "range"),          # marker 剥离后仍 range
])
def test_parse_constraint_pep508_range(requirement, expected_type):
    ctype, info = mod.parse_constraint(requirement)
    assert ctype == expected_type
    assert "specifiers" not in info  # Requirement 解析路径不落 specifiers


def test_parse_constraint_conflicting_exact_is_unknown():
    # SpecifierSet 路径:两个不同 == 值 → 既非单值 exact 也无 range 算子 → unknown
    ctype, info = mod.parse_constraint("==1.0,==2.0")
    assert ctype == "unknown"
    assert info == {"specifiers": [{"operator": "==", "version": "1.0"},
                                   {"operator": "==", "version": "2.0"}]}


def test_parse_constraint_junk_is_unknown():
    assert mod.parse_constraint("!!!") == ("unknown", {})


@pytest.mark.parametrize("requirement", [",", ",,"])
def test_parse_constraint_specifierset_empty_is_unbounded(requirement):
    # Requirement 解析失败 → SpecifierSet 解析成功但无子句 → unbounded
    assert mod.parse_constraint(requirement) == ("unbounded", {})


# ─────────────────────────────────────────────
# parse_constraint — status == "unknown" 分支
# ─────────────────────────────────────────────

@pytest.mark.parametrize("requirement,info,expected", [
    ("==1.0", {"status": "unknown"}, ("exact", {
        "status": "unknown",
        "specifiers": [{"operator": "==", "version": "1.0"}],
        "exact_version": "1.0"})),
    (">=1.0", {"status": "unknown"}, ("range", {
        "status": "unknown",
        "specifiers": [{"operator": ">=", "version": "1.0"}]})),
    ("==1.0,==2.0", {"status": "unknown"}, ("range", {
        "status": "unknown",
        "specifiers": [{"operator": "==", "version": "1.0"},
                       {"operator": "==", "version": "2.0"}]})),
    ("###", {"status": "unknown"}, ("unknown", {"status": "unknown"})),
    ("foo", {"status": "unknown"}, ("unbounded", {"status": "unknown"})),
    ("1.2.3", {"status": "unknown"}, ("unbounded", {"status": "unknown"})),
])
def test_parse_constraint_status_unknown(requirement, info, expected):
    assert mod.parse_constraint(requirement, info) == expected


def test_parse_constraint_status_unknown_falls_back_to_bare_requirement(monkeypatch):
    """placeholder 前缀拼接失败、裸字符串可解析时走 Requirement(normalized) 兜底。"""
    real_requirement = mod.Requirement

    class _FakeRequirement:
        def __init__(self, spec):
            if spec.startswith("placeholder"):
                raise ValueError("placeholder prefix rejected")
            self._real = real_requirement(spec)

        @property
        def specifier(self):
            return self._real.specifier

    monkeypatch.setattr(mod, "Requirement", _FakeRequirement)
    ctype, info = mod.parse_constraint("x>=1.0", {"status": "unknown"})
    assert ctype == "range"
    assert info == {"status": "unknown",
                    "specifiers": [{"operator": ">=", "version": "1.0"}]}


def test_parse_constraint_status_unknown_spec_without_operator(monkeypatch):
    """覆盖 specifier 循环中无已知算子前缀的 else 分支。"""
    class _FakeRequirement:
        def __init__(self, spec):
            if spec.startswith("placeholder"):
                raise ValueError("placeholder prefix rejected")
            self.specifier = ["==2.0", "1.0"]  # 第二个无算子前缀

    monkeypatch.setattr(mod, "Requirement", _FakeRequirement)
    ctype, info = mod.parse_constraint("x", {"status": "unknown"})
    assert ctype == "exact"
    assert info == {"status": "unknown",
                    "specifiers": [{"operator": "==", "version": "2.0"},
                                   {"operator": "", "version": "1.0"}],
                    "exact_version": "2.0"}


# ─────────────────────────────────────────────
# parse_constraint — requirement_info 驱动分支
# ─────────────────────────────────────────────

def test_parse_constraint_exact_version_shortcut():
    assert mod.parse_constraint("x", {"exact_version": "1.0"}) == ("exact", {"exact_version": "1.0"})
    assert mod.parse_constraint("", {"exact_version": "1.0"}) == ("unbounded", {"exact_version": "1.0"})


def test_parse_constraint_clauses_all_exact_unique():
    ctype, info = mod.parse_constraint("x", {"clauses": [
        {"operator": "==", "version": "1.2.3"},
        {"operator": "===", "version": "1.2.3"},
    ]})
    assert ctype == "exact"
    assert info["exact_version"] == "1.2.3"
    assert "specifiers" not in info


def test_parse_constraint_clauses_all_exact_conflicting():
    # 实际行为:全部 == 但版本不一致 → 不满足 exact,又无 range 算子,
    # 落到 Requirement("x") → 无 spec → unbounded(生产代码缺陷,按实际行为断言)
    ctype, info = mod.parse_constraint("x", {"clauses": [
        {"operator": "==", "version": "1.0"},
        {"operator": "==", "version": "2.0"},
    ]})
    assert ctype == "unbounded"
    assert "exact_version" not in info


@pytest.mark.parametrize("clauses,expected_type", [
    ([{"operator": ">=", "version": "1.0"}, {"operator": "==", "version": "1.5"}], "range"),
    ([{"operator": "!=", "version": "1.0"}], "range"),
    ([{"operator": ">", "version": "1.0"}, {"operator": "<", "version": "2.0"}], "range"),
    ([{"operator": "<=", "version": "2.0"}], "range"),
    ([{"operator": "~=", "version": "1.8"}], "range"),
])
def test_parse_constraint_clauses_with_range_op(clauses, expected_type):
    ctype, info = mod.parse_constraint("x", {"clauses": clauses})
    assert ctype == expected_type
    assert info["specifiers"] == [
        {"operator": str(c["operator"]), "version": str(c["version"])} for c in clauses
    ]


def test_parse_constraint_clauses_keeps_existing_specifiers():
    ctype, info = mod.parse_constraint("x", {
        "clauses": [{"operator": ">=", "version": "1.0"}],
        "specifiers": [{"operator": "==", "version": "9.9"}],
    })
    assert ctype == "range"
    assert info["specifiers"] == [{"operator": "==", "version": "9.9"}]  # 不覆盖已有值


def test_parse_constraint_clauses_empty_operator_falls_through():
    assert mod.parse_constraint("x", {"clauses": [{"operator": "", "version": "1.0"}]}) == \
        ("unbounded", {"clauses": [{"operator": "", "version": "1.0"}]})


@pytest.mark.parametrize("specifiers,expected_type", [
    ([{"operator": "==", "version": "1.0"}], "exact"),
    ([{"operator": "===", "version": "1.0"}], "exact"),
    # 多条 exact 子句(即使版本相同)不判 exact:代码要求 len(candidates)==1(生产代码实际行为)
    ([{"operator": "==", "version": "1.0"}, {"operator": "===", "version": "1.0"}], "unknown"),
    ([{"operator": "==", "version": "1.0"}, {"operator": "==", "version": "2.0"}], "unknown"),
    ([{"operator": "==", "version": "1.0"}, {"operator": ">=", "version": "1.0"}], "range"),
    ([{"operator": ">=", "version": "1.0"}], "range"),
    ([{"operator": "", "version": "1.0"}], "unknown"),
    (["junk", 1], "unknown"),          # 非 dict 成员:vacuous all() → 非 exact,非 range
])
def test_parse_constraint_specifiers(specifiers, expected_type):
    ctype, info = mod.parse_constraint("x", {"specifiers": specifiers})
    assert ctype == expected_type


def test_parse_constraint_specifiers_empty_list_falls_to_requirement():
    assert mod.parse_constraint("x", {"specifiers": []}) == ("unbounded", {"specifiers": []})


def test_parse_constraint_specifiers_exact_sets_exact_version():
    ctype, info = mod.parse_constraint("x", {"specifiers": [
        {"operator": "==", "version": "1.0"}]})
    assert ctype == "exact"
    assert info["exact_version"] == "1.0"


# ─────────────────────────────────────────────
# to_specifier_set
# ─────────────────────────────────────────────

def test_to_specifier_set_empty_or_junk():
    assert mod.to_specifier_set("") is None
    assert mod.to_specifier_set(None) is None
    assert mod.to_specifier_set("garbage constraint!") is None


@pytest.mark.parametrize("constraint,expected_str", [
    (">=1.0,<2.0", "<2.0,>=1.0"),        # SpecifierSet 内部排序
    ("^1.2.3", "<2.0.0,>=1.2.3"),        # npm 归一后再转
    ("~1.2", "<1.3.0,>=1.2.0"),
    ("==1.0", "==1.0"),
])
def test_to_specifier_set(constraint, expected_str):
    spec = mod.to_specifier_set(constraint)
    assert spec is not None
    assert str(spec) == expected_str


def test_to_specifier_set_falls_back_to_bare_requirement(monkeypatch):
    real_requirement = mod.Requirement

    class _FakeRequirement:
        def __init__(self, spec):
            if spec.startswith("placeholder"):
                raise ValueError("placeholder prefix rejected")
            self._real = real_requirement(spec)

        @property
        def specifier(self):
            return self._real.specifier

    monkeypatch.setattr(mod, "Requirement", _FakeRequirement)
    spec = mod.to_specifier_set("x>=1.0")
    assert spec is not None
    assert "1.5" in spec
    assert "0.5" not in spec


# ─────────────────────────────────────────────
# satisfies
# ─────────────────────────────────────────────

@pytest.mark.parametrize("constraint_type", ["unbounded", "exact", "range", "unknown"])
def test_satisfies_empty_version_is_false(constraint_type):
    assert mod.satisfies("", ">=1.0", constraint_type, {}) is False


def test_satisfies_unbounded():
    assert mod.satisfies("9.9.9", "anything", "unbounded", {}) is True
    assert mod.satisfies("0.0.1", "", "unbounded", {}) is True


def test_satisfies_unknown():
    assert mod.satisfies("1.0", ">=1.0", "unknown", {}) is False


@pytest.mark.parametrize("version,constraint,info,expected", [
    ("1.2.3", "==1.2.3", {"exact_version": "1.2.3"}, True),
    ("1.2.4", "==1.2.3", {"exact_version": "1.2.3"}, False),
    ("v1.2.3", "==1.2.3", {"exact_version": "1.2.3"}, True),      # v 前缀归一后相等
    ("1.2.3", "==v1.2.3", {"exact_version": "v1.2.3"}, True),
    ("1.2.3", "==1.2.3", {}, False),                              # 无 exact_version → False
    ("2.0", "==2.0.0", {"exact_version": "2.0.0"}, False),        # 字符串比较,不做版本语义归一
])
def test_satisfies_exact(version, constraint, info, expected, build_rpm_scripts):
    assert mod.satisfies(version, constraint, "exact", info) is expected


@pytest.mark.parametrize("version,constraint,expected", [
    ("1.5.0", ">=1.0,<2.0", True),
    ("2.5.0", ">=1.0,<2.0", False),
    ("1.0", "==1.0", True),
    ("1.5.0", "==1.0", False),
    ("1.2.3", "^1.2.0", True),
    ("2.0.0", "^1.2.0", False),
    ("1.2.3", "~1.2.0", True),
    ("1.3.0", "~1.2.0", False),
    ("1.0.0rc1", ">=1.0", False),        # 预发布版本默认不满足
])
def test_satisfies_range(version, constraint, expected, build_rpm_scripts):
    assert mod.satisfies(version, constraint, "range", {}) is expected


@pytest.mark.parametrize("version,constraint", [
    ("not.a.version", ">=1.0"),    # 非法版本字符串 → False
    ("1.0", ">=abc"),              # 非法约束 → to_specifier_set None → False
    ("1.0", "garbage constraint!"),
])
def test_satisfies_range_invalid_inputs(version, constraint, build_rpm_scripts):
    assert mod.satisfies(version, constraint, "range", {}) is False


def test_satisfies_range_ignores_exact_version_in_info(build_rpm_scripts):
    # range 类型走 specifier 比较,不看 requirement_info.exact_version
    assert mod.satisfies("1.5.0", ">=1.0", "range", {"exact_version": "9.9"}) is True


# ─────────────────────────────────────────────
# 无 packaging 库路径
# ─────────────────────────────────────────────

@pytest.fixture
def no_packaging_mod(monkeypatch, loaded_modules):
    """加载 packaging 不可用时的独立副本(Requirement/SpecifierSet/Version 均为 None)。

    注意:仅置 sys.modules["packaging"] = None 不够——一旦 packaging.requirements 等
    子模块已被缓存(pytest 自身就会 import),from packaging.requirements import ... 会
    直接命中缓存绕过父模块检查;必须把三个子模块键一并置 None 触发 import halt。
    """
    for key in ("packaging", "packaging.requirements", "packaging.specifiers",
                "packaging.version"):
        monkeypatch.setitem(sys.modules, key, None)
    return loaded_modules("constraint_parser_no_pkg", SCRIPT_DIRS["build_rpm"] / "constraint_parser.py")


def test_no_packaging_module_attrs(no_packaging_mod):
    assert no_packaging_mod.Requirement is None
    assert no_packaging_mod.SpecifierSet is None
    assert no_packaging_mod.Version is None
    assert no_packaging_mod.InvalidVersion is Exception


def test_no_packaging_parse_constraint(no_packaging_mod):
    assert no_packaging_mod.parse_constraint(">=1.0") == ("unknown", {})
    assert no_packaging_mod.parse_constraint("") == ("unbounded", {})
    assert no_packaging_mod.parse_constraint("x", {"status": "unknown"}) == \
        ("unknown", {"status": "unknown"})


def test_no_packaging_info_driven_branches_still_work(no_packaging_mod):
    assert no_packaging_mod.parse_constraint("x", {"exact_version": "1.0"}) == \
        ("exact", {"exact_version": "1.0"})
    ctype, info = no_packaging_mod.parse_constraint("x", {"clauses": [
        {"operator": ">=", "version": "1.0"}]})
    assert ctype == "range"
    assert info["specifiers"] == [{"operator": ">=", "version": "1.0"}]
    assert no_packaging_mod.parse_constraint("x", {"specifiers": [
        {"operator": "==", "version": "1.0"}]})[0] == "exact"


def test_no_packaging_to_specifier_set(no_packaging_mod):
    assert no_packaging_mod.to_specifier_set(">=1.0") is None
    assert no_packaging_mod.to_specifier_set("") is None


def test_no_packaging_satisfies(no_packaging_mod, build_rpm_scripts):
    assert no_packaging_mod.satisfies("1.0", ">=1.0", "range", {}) is False   # 无解析能力 → False
    assert no_packaging_mod.satisfies("1.0", "", "unbounded", {}) is True
    assert no_packaging_mod.satisfies("1.0", "x", "unknown", {}) is False
    # exact 路径只依赖 normalize_version,不依赖 packaging
    assert no_packaging_mod.satisfies("1.0", "==1.0", "exact", {"exact_version": "1.0"}) is True
    assert no_packaging_mod.satisfies("1.1", "==1.0", "exact", {"exact_version": "1.0"}) is False
