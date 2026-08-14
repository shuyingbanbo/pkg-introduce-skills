"""aggregate_dependency_requests.py — 同层 pending 依赖聚合为 DependencyRequest(纯逻辑)。

覆盖:
- normalize_requested_by / append_unique 辅助函数
- classify_constraint_conflict:exact 冲突、range 内嵌 == 冲突、解析失败保守放过、无 packaging 路径
- merge_constraints:空/单条/多条合并去重、exact 短路、不可解析回退、无 packaging 路径
- choose_constraint_type:precedence 打分与 fallback
- aggregate_pending_requests:分组聚合、字段传递、requested_by 归一、member 统计、conflict → blocked
- main():正常落盘、文件缺失错误、缺参 SystemExit
"""

from __future__ import annotations

import json
import sys

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

agg = load_module("aggregate_dependency_requests", SCRIPT_DIRS["build_rpm"] / "aggregate_dependency_requests.py")


# ─────────────────────────────────────────────
# normalize_requested_by / append_unique
# ─────────────────────────────────────────────

@pytest.mark.parametrize("value,default_pkg,expected", [
    (["a", "b", " "], "dflt", ["a", "b"]),        # 空白元素过滤
    (["a", "a", "b"], "dflt", ["a", "a", "b"]),   # 本函数不去重(去重在 append_unique)
    ("pkgA", "dflt", ["pkgA"]),
    ("  ", "dflt", ["dflt"]),                     # 空白字符串走默认
    ("", "dflt", ["dflt"]),
    (None, "parent", ["parent"]),
    (None, "", []),                               # 无默认且无值 → 空
    ("", "", []),
    (123, "dflt", ["dflt"]),                      # 非 list/str → 默认
])
def test_normalize_requested_by(value, default_pkg, expected):
    assert agg.normalize_requested_by(value, default_pkg) == expected


def test_append_unique():
    items: list = []
    agg.append_unique(items, "a")
    agg.append_unique(items, "a")
    agg.append_unique(items, "b")
    assert items == ["a", "b"]


# ─────────────────────────────────────────────
# classify_constraint_conflict
# ─────────────────────────────────────────────

@pytest.mark.parametrize("constraints,constraint_type,expected", [
    ([], "exact", (False, "")),
    (["==1.0"], "exact", (False, "")),
    (["==1.0", "==1.0"], "exact", (False, "")),
    ([" ==1.0 ", "==1.0"], "exact", (False, "")),   # strip 后相同
    ([">=1.0", "<2.0"], "range", (False, "")),
    (["==1.0", "==1.0"], "range", (False, "")),
    (["", ">=1.0"], "range", (False, "")),          # 过滤空后仅一条
    (["!!!", "###"], "range", (False, "")),         # 解析异常 → 保守放过
    (["x", "y"], "unbounded", (False, "")),         # 非 exact 类型也走 Requirement 解析
])
def test_classify_constraint_conflict_no_conflict(constraints, constraint_type, expected):
    assert agg.classify_constraint_conflict(constraints, constraint_type) == expected


def test_classify_constraint_conflict_exact():
    conflict, reason = agg.classify_constraint_conflict(["==1.0", "==2.0"], "exact")
    assert conflict is True
    assert reason == "multiple exact constraints are incompatible: ==1.0, ==2.0"


def test_classify_constraint_conflict_exact_sorted_reason():
    _, reason = agg.classify_constraint_conflict(["==2.0", "==1.0"], "exact")
    assert reason == "multiple exact constraints are incompatible: ==1.0, ==2.0"


def test_classify_constraint_conflict_range_with_embedded_exacts():
    conflict, reason = agg.classify_constraint_conflict(["==1.0", "==2.0"], "range")
    assert conflict is True
    assert reason == "multiple exact range members are incompatible: 1.0, 2.0"


# ─────────────────────────────────────────────
# merge_constraints
# ─────────────────────────────────────────────

@pytest.mark.parametrize("constraints,constraint_type,expected", [
    ([], "unknown", ("", "unknown")),
    ([], "", ("", "unknown")),                      # 空类型兜底 unknown
    ([">=1.0"], "", (">=1.0", "unknown")),
    ([" >=1.0 "], "range", (">=1.0", "range")),     # strip
    (["", ">=1.0"], "unknown", (">=1.0", "unknown")),  # 空元素过滤后单条
    (["==1.0", "==2.0"], "exact", ("==1.0", "exact")), # exact 取首条不合并
    ([">=1.0", "<2.0"], "range", (">=1.0,<2.0", "range")),
    ([">=1.0", ">=1.0, <2.0"], "range", (">=1.0,<2.0", "range")),  # 子句去重
    (["==1.0", ">=1.0"], "range", ("==1.0,>=1.0", "range")),
    (["^1.2.3", "~2.0.0"], "range", ("^1.2.3", "range")),  # 全部不可解析 → 回退首条
    ([">=1.0", "!!!", "<2.0"], "range", (">=1.0,<2.0", "range")),  # 部分可解析
])
def test_merge_constraints(constraints, constraint_type, expected):
    assert agg.merge_constraints(constraints, constraint_type) == expected


# ─────────────────────────────────────────────
# choose_constraint_type
# ─────────────────────────────────────────────

@pytest.mark.parametrize("items,fallback,expected", [
    ([], "", "unknown"),
    ([], "range", "range"),
    ([{}, {"constraint_type": ""}], "unknown", "unknown"),   # 缺字段按 unknown 打分
    ([{"constraint_type": "range"}, {"constraint_type": "exact"}], "unknown", "exact"),
    ([{"constraint_type": "unbounded"}], "range", "range"),  # fallback 更高
    ([{"constraint_type": "unknown"}], "unbounded", "unbounded"),
    ([{"constraint_type": "bogus"}], "unknown", "unknown"),  # 未知类型 0 分
    ([{"constraint_type": "exact"}, {"constraint_type": "exact"}], "range", "exact"),
])
def test_choose_constraint_type(items, fallback, expected):
    assert agg.choose_constraint_type(items, fallback) == expected


# ─────────────────────────────────────────────
# aggregate_pending_requests
# ─────────────────────────────────────────────

def test_aggregate_empty():
    assert agg.aggregate_pending_requests({}, "parent") == []
    assert agg.aggregate_pending_requests({"pending": None}, "parent") == []
    assert agg.aggregate_pending_requests({"pending": [{"constraint": ">=1.0"}]}, "parent") == []  # 无 name/dep 跳过


def test_aggregate_single_item_full_fields():
    requests = agg.aggregate_pending_requests({"pending": [
        {"name": "requests", "dep": "python3-requests", "type": "python",
         "constraint": ">=2.0", "upstream_url": "https://pypi.org/p/requests",
         "upstream_resolution": "official", "version_source": "pypi",
         "category": "runtime", "requested_by": ["flask"]},
    ]}, "parent")
    assert len(requests) == 1
    req = requests[0]
    assert req["name"] == "requests"
    assert req["dep"] == "python3-requests"          # 显式 dep 保留
    assert req["type"] == "python"
    assert req["identity"] == "python:requests"
    assert req["upstream_url"] == "https://pypi.org/p/requests"
    assert req["upstream_resolution"] == "official"
    assert req["version_source"] == "pypi"
    assert req["constraint"] == ">=2.0"
    assert req["requirement"] == ">=2.0"
    assert req["all_constraints"] == [">=2.0"]
    assert req["constraint_type"] == "unknown"       # 单条约束 + 无显式类型 → unknown(实际行为)
    assert req["requested_by"] == ["flask"]          # 显式 requested_by 优先
    assert req["categories"] == ["runtime"]
    assert req["requirement_info"] == {}
    assert req["member_count"] == 1
    assert req["node_state"] == "discovered"
    assert req["conflict"] is False
    assert req["conflict_reason"] == ""


def test_aggregate_dep_field_and_lang_fallback():
    requests = agg.aggregate_pending_requests({"lang": "python", "pending": [
        {"dep": "numpy", "requirement": ">=1.20"},   # 无 name 用 dep;约束取 requirement
        {"name": "six", "constraint": ""},
    ]}, "parent")
    by_name = {r["name"]: r for r in requests}
    assert set(by_name) == {"numpy", "six"}
    numpy_req = by_name["numpy"]
    assert numpy_req["dep"] == "numpy"               # 无显式 dep → 回退 name
    assert numpy_req["type"] == "python"             # summary.lang 兜底
    assert numpy_req["identity"] == "python:numpy"
    assert numpy_req["constraint"] == ">=1.20"       # requirement 字段兜底
    # summary.lang 对同层所有缺 type 的 item 生效
    assert by_name["six"]["identity"] == "python:six"


def test_aggregate_identity_without_type_or_lang():
    requests = agg.aggregate_pending_requests({"pending": [
        {"name": "six", "constraint": ""},
    ]}, "parent")
    assert requests[0]["type"] == ""
    assert requests[0]["identity"] == "six"          # 无 type 无 lang → identity 仅名字


def test_aggregate_groups_by_name_and_sorts():
    requests = agg.aggregate_pending_requests({"pending": [
        {"name": "zeta", "constraint": ">=1.0", "constraint_type": "range"},
        {"name": "alpha", "constraint": ">=1.0", "constraint_type": "range"},
        {"name": "zeta", "constraint": "<2.0", "constraint_type": "range"},
    ]}, "parent")
    assert [r["name"] for r in requests] == ["alpha", "zeta"]
    zeta = requests[1]
    assert zeta["member_count"] == 2
    assert zeta["all_constraints"] == [">=1.0", "<2.0"]
    assert zeta["constraint"] == ">=1.0,<2.0"
    assert zeta["constraint_type"] == "range"
    assert len(zeta["decision_trace"]) == 2
    assert zeta["node_state"] == "discovered"
    assert zeta["conflict"] is False


def test_aggregate_exact_conflict_blocks():
    requests = agg.aggregate_pending_requests({"pending": [
        {"name": "requests", "constraint": "==1.0", "constraint_type": "exact"},
        {"name": "requests", "constraint": "==2.0", "constraint_type": "exact"},
    ]}, "parent")
    req = requests[0]
    assert req["constraint"] == "==1.0"              # merge 取首条
    assert req["constraint_type"] == "exact"
    assert req["conflict"] is True
    assert req["conflict_reason"] == "multiple exact constraints are incompatible: ==1.0, ==2.0"
    assert req["node_state"] == "blocked"


def test_aggregate_range_conflict_with_embedded_exacts():
    requests = agg.aggregate_pending_requests({"pending": [
        {"name": "pkg", "constraint": "==1.0", "constraint_type": "range"},
        {"name": "pkg", "constraint": "==2.0", "constraint_type": "range"},
    ]}, "parent")
    req = requests[0]
    assert req["constraint"] == "==1.0,==2.0"
    assert req["conflict"] is True
    assert req["conflict_reason"] == "multiple exact range members are incompatible: 1.0, 2.0"
    assert req["node_state"] == "blocked"


def test_aggregate_requested_by_variants():
    requests = agg.aggregate_pending_requests({"pending": [
        {"name": "a", "constraint": ">=1.0", "requested_by": ["x", "x", " ", "y"]},
        {"name": "b", "constraint": ">=1.0", "requested_by": "strOwner"},
        {"name": "c", "constraint": ">=1.0"},
        {"name": "d", "constraint": ">=1.0", "requested_by": ""},
    ]}, "parent")
    by_name = {r["name"]: r for r in requests}
    assert by_name["a"]["requested_by"] == ["x", "y"]    # 去重 + 过滤空白
    assert by_name["b"]["requested_by"] == ["strOwner"]
    assert by_name["c"]["requested_by"] == ["parent"]    # 默认 requested_by 参数
    assert by_name["d"]["requested_by"] == ["parent"]    # 空白字符串同样回退默认


def test_aggregate_requested_by_empty_when_no_source():
    requests = agg.aggregate_pending_requests({"pending": [
        {"name": "solo", "constraint": ">=1.0"},
    ]}, "")
    assert requests[0]["requested_by"] == []


def test_aggregate_requirement_info_dedupe_by_equality():
    # dict == 按内容比较,内容相同的两个 dict 只保留第一个候选(实际行为)
    info = {"clauses": [{"operator": ">=", "version": "1.0"}]}
    requests = agg.aggregate_pending_requests({"pending": [
        {"name": "p", "constraint": ">=1.0", "requirement_info": info},
        {"name": "p", "constraint": ">=1.0", "requirement_info": dict(info)},
    ]}, "parent")
    assert requests[0]["requirement_info"] == info
    requests2 = agg.aggregate_pending_requests({"pending": [
        {"name": "p", "constraint": ">=1.0", "requirement_info": {"v": "1"}},
        {"name": "p", "constraint": ">=1.0", "requirement_info": {"v": "2"}},
    ]}, "parent")
    assert requests2[0]["requirement_info"] == {"v": "1"}  # 取第一个候选


def test_aggregate_categories_dedup():
    requests = agg.aggregate_pending_requests({"pending": [
        {"name": "p", "constraint": ">=1.0", "category": "runtime"},
        {"name": "p", "constraint": ">=1.0", "category": "runtime"},
        {"name": "p", "constraint": ">=1.0", "category": "build"},
    ]}, "parent")
    assert requests[0]["categories"] == ["runtime", "build"]


def test_aggregate_member_preview_caps_at_five():
    same = [
        {"name": "dup", "constraint": f">={i}.0", "constraint_type": "range"}
        for i in range(6)
    ]
    requests = agg.aggregate_pending_requests({"pending": same}, "parent")
    assert requests[0]["member_count"] == 6
    assert len(requests[0]["member_preview"]) == 5
    assert len(requests[0]["decision_trace"]) == 6


def test_aggregate_member_preview_fields():
    requests = agg.aggregate_pending_requests({"pending": [
        {"name": "p", "constraint": ">=1.0", "requested_by": ["owner"],
         "decision": "use", "found_version": "1.2.3"},
        {"name": "p", "constraint": ">=1.0", "decision": "skip",
         "existing_check": {"official": {"highest": {"version": "9.9.9"}}}},
        {"name": "p", "constraint": ">=1.0"},
    ]}, "parent")
    preview = requests[0]["member_preview"]
    assert preview[0] == {"name": "p", "constraint": ">=1.0", "requested_by": ["owner"],
                          "decision": "use", "found_version": "1.2.3"}
    # found_version 为空时回退 existing_check.official.highest.version
    assert preview[1]["found_version"] == "9.9.9"
    assert preview[1]["requested_by"] == "parent"
    assert preview[2]["found_version"] == ""


def test_aggregate_decision_trace_fields():
    requests = agg.aggregate_pending_requests({"pending": [
        {"name": "p", "constraint": ">=1.0", "decision": "reuse", "action": "install",
         "reason": "already present", "found_version": "2.0.0"},
    ]}, "parent")
    trace = requests[0]["decision_trace"]
    assert trace == [{"name": "p", "decision": "reuse", "action": "install",
                      "reason": "already present", "found_version": "2.0.0"}]


# ─────────────────────────────────────────────
# 无 packaging 库路径
# ─────────────────────────────────────────────

@pytest.fixture
def no_packaging_agg(monkeypatch, loaded_modules):
    # 子模块键必须一并置 None:已缓存的 packaging.requirements 会绕过父模块检查
    for key in ("packaging", "packaging.requirements", "packaging.specifiers"):
        monkeypatch.setitem(sys.modules, key, None)
    return loaded_modules("aggregate_dependency_requests_no_pkg",
                          SCRIPT_DIRS["build_rpm"] / "aggregate_dependency_requests.py")


def test_no_packaging_merge_and_classify(no_packaging_agg):
    assert no_packaging_agg.Requirement is None
    assert no_packaging_agg.SpecifierSet is None
    # 多约束无法合并 → 回退首条 + range
    assert no_packaging_agg.merge_constraints(["a", "b"], "range") == ("a", "range")
    # range 冲突检测无解析能力 → 保守放过;exact 的集合比较仍有效
    assert no_packaging_agg.classify_constraint_conflict(["==1.0", "==2.0"], "range") == (False, "")
    assert no_packaging_agg.classify_constraint_conflict(["==1.0", "==2.0"], "exact")[0] is True


# ─────────────────────────────────────────────
# read_json / main
# ─────────────────────────────────────────────

def test_read_json(tmp_path):
    path = tmp_path / "summary.json"
    path.write_text('{"pkgname": "foo", "lang": "python"}', encoding="utf-8")
    assert agg.read_json(path) == {"pkgname": "foo", "lang": "python"}


def test_main_writes_output(tmp_path, monkeypatch, capsys):
    summary_path = tmp_path / "pre_check_demo.json"
    summary_path.write_text(json.dumps({
        "pkgname": "demo", "lang": "python",
        "pending": [{"name": "requests", "constraint": ">=2.0", "type": "python"}],
    }))
    out_path = tmp_path / "sub" / "requests.json"
    monkeypatch.setattr(sys, "argv", [
        "aggregate_dependency_requests.py",
        "--summary-json", str(summary_path),
        "--requested-by", "demo",
        "-o", str(out_path),
    ])
    assert agg.main() == 0
    stdout = capsys.readouterr().out
    payload = json.loads(stdout)
    assert payload["pkgname"] == "demo"
    assert payload["lang"] == "python"
    assert payload["requested_by"] == "demo"
    assert payload["requests"][0]["name"] == "requests"
    # 输出文件落盘(父目录自动创建)
    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert written["requests"][0]["constraint"] == ">=2.0"


def test_main_missing_file_returns_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "aggregate_dependency_requests.py",
        "--summary-json", str(tmp_path / "nope.json"),
        "--requested-by", "demo",
    ])
    assert agg.main() == 1
    assert "错误" in capsys.readouterr().err


def test_main_missing_args_exits_2(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["aggregate_dependency_requests.py"])
    with pytest.raises(SystemExit) as excinfo:
        agg.main()
    assert excinfo.value.code == 2


def test_main_entrypoint_guard(tmp_path, monkeypatch):
    """以 __main__ 方式执行脚本,覆盖 if __name__ == "__main__" 入口。"""
    import runpy

    summary_path = tmp_path / "pre_check_x.json"
    summary_path.write_text(json.dumps({"pkgname": "x", "pending": []}))
    monkeypatch.setattr(sys, "argv", [
        "aggregate_dependency_requests.py",
        "--summary-json", str(summary_path),
        "--requested-by", "x",
    ])
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(str(SCRIPT_DIRS["build_rpm"] / "aggregate_dependency_requests.py"),
                       run_name="__main__")
    assert excinfo.value.code == 0
