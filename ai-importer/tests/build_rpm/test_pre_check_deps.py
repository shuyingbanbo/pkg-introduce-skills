"""pre_check_deps.py — RPM 编译前依赖预检(核心状态机 classify_dependency + 编排)。

测试策略:
- 纯逻辑函数(常量、级联映射、source index、summary、rust 工具链读取)直接断言。
- 网络(urllib)与 dnf/COPR 查询用 monkeypatch 单点替换:
  resolve_upstream_url / _enrich_via_cascade / EXISTING_CHECKER / CASCADE_CHECKER。
- main() 全链路:fake_subprocess 拦截 subprocess.run + 预写分析结果 JSON,
  不跑真实分析脚本、不发网络、不碰 dnf。
"""

from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sys.path.insert(0, str(SCRIPT_DIRS["build_rpm"]))
mod = load_module("pre_check_deps", SCRIPT_DIRS["build_rpm"] / "pre_check_deps.py")

# 真实 check_existing_package 模块(纯逻辑函数 parse_requirement / evaluate_requirement
# 直接复用;check_existing_package 等 dnf/COPR 方法按需 stub)
_REAL_EXISTING = mod.EXISTING_CHECKER

_CASCADE_CRED_ENV = (
    "COPR_FRONTEND_URL", "COPR_OWNER", "COPR_PROJECT",
    "COPR_API_LOGIN", "COPR_API_TOKEN",
)


@pytest.fixture
def no_cascade_env(monkeypatch):
    """清空 COPR 级联凭据,保证 _enrich_via_cascade 返回 None(不发网络)。"""
    for var in (*_CASCADE_CRED_ENV, "COPR_CHROOT"):
        monkeypatch.delenv(var, raising=False)


def make_existing_stub(decision="reuse_official", official_exists=True,
                       official_version="2.0", reason="stub-reason",
                       requested=None, raise_on_check=False, choose="introduce_new"):
    """构造 EXISTING_CHECKER 替身:parse/evaluate 委托真实实现(纯逻辑),
    check_existing_package / choose_decision / build_reason 按需替换。"""

    class StubExistingChecker:
        def parse_requirement(self, requirement):
            return _REAL_EXISTING.parse_requirement(requirement)

        def evaluate_requirement(self, version, req_info):
            return _REAL_EXISTING.evaluate_requirement(version, req_info)

        def check_existing_package(self, pkgname, version="", requirement="", lang="", **kw):
            if raise_on_check:
                raise RuntimeError("check_existing_package boom")
            return {
                "requested": requested or {"pkgname": pkgname, "version": version,
                                           "requirement": requirement},
                "official": {
                    "exists": official_exists,
                    "highest": {"version": official_version} if official_version else None,
                    "meets_need": official_exists,
                },
                "user_repo": {},
                "decision": decision,
                "reason": reason,
            }

        def choose_decision(self, official, user_repo, requested_version, requirement):
            return choose

        def build_reason(self, decision, official, user_repo, requested_version, requirement):
            return "merged-reason"

    return StubExistingChecker()


class _FakeUrlResponse:
    """urllib.urlopen 的假响应(读 bytes JSON)。"""

    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _dep(name="foo", dep=None, requirement="", constraint=None, **extra):
    """构造 classify_dependency 入参(每次全新 dict,避免原地修改串扰)。"""
    d = {"name": name, "dep": dep or name, "requirement": requirement}
    d["constraint"] = constraint if constraint is not None else requirement
    d.update(extra)
    return d


def _write_files(tmp_path, files):
    """在 tmp_path 下按相对路径创建文件(目录自动创建)。"""
    for rel in files:
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x\n", encoding="utf-8")


# ─────────────────────────────────────────────
# 1. 常量与配置
# ─────────────────────────────────────────────

def test_analyzers_language_table():
    assert set(mod.ANALYZERS) == {"python", "go", "rust", "c", "cpp", "nodejs", "java", "ros"}
    for cfg in mod.ANALYZERS.values():
        assert cfg["script"].endswith(".py")
        assert isinstance(cfg["extra_args"], list)


def test_vendor_langs_and_secondary_probe_constants():
    assert mod.VENDOR_LANGS == {"go", "rust"}
    assert mod.SECONDARY_PROBE == {"Cargo.toml": "rust", "go.mod": "go"}


def test_build_system_whitelist_deprecated_alias():
    # BUILD_SYSTEM_WHITELIST 已废弃置 None,判断收敛到 chroot_toolchain
    assert mod.BUILD_SYSTEM_WHITELIST is None
    assert mod._is_build_system_tool("cmake") is True
    assert mod._is_build_system_tool("hatchling") is True
    assert mod._is_build_system_tool("libssl") is False
    assert mod._is_build_system_tool("") is False


def test_cascade_to_precheck_decision_mapping():
    assert mod._CASCADE_TO_PRECHECK_DECISION == {
        "reuse_copr_project": "reuse_user_repo",
        "reuse_eur_srpm": "introduce_new",
        "reuse_official": "reuse_official",
        "reuse_additional_repo": "reuse_official",
        "evaluate": "block_official_older",
        "introduce_new_with_ref": "introduce_new",
        "introduce_new": "introduce_new",
    }


def test_dep_conflict_mode(monkeypatch):
    monkeypatch.setattr(mod, "_load_pkg_introduce_config", lambda: {})
    assert mod._dep_conflict_mode() == "block"
    monkeypatch.setattr(mod, "_load_pkg_introduce_config",
                        lambda: {"dep_conflict": {"mode": "compat"}})
    assert mod._dep_conflict_mode() == "compat"
    monkeypatch.setattr(mod, "_load_pkg_introduce_config",
                        lambda: {"dep_conflict": {"mode": "force_compat"}})
    assert mod._dep_conflict_mode() == "force_compat"


def test_load_pkg_introduce_config_returns_dict():
    # 仓库内无 config.json(只有 .example)→ 返回空 dict,不抛异常
    assert isinstance(mod._load_pkg_introduce_config(), dict)


# ─────────────────────────────────────────────
# 2. detect_secondary_langs
# ─────────────────────────────────────────────

@pytest.mark.parametrize("lang,files,expected", [
    ("python", [], ([], {})),
    ("python", ["Cargo.toml"], (["rust"], {"rust": "Cargo.toml"})),
    ("python", ["go.mod"], (["go"], {"go": "go.mod"})),
    ("python", ["rust/Cargo.toml"], (["rust"], {"rust": "rust/Cargo.toml"})),
    ("python", ["a/b/go.mod"], (["go"], {"go": "a/b/go.mod"})),
    ("python", ["Cargo.toml", "go.mod"], (["rust", "go"],
                                          {"rust": "Cargo.toml", "go": "go.mod"})),
    ("rust", ["Cargo.toml", "go.mod"], (["go"], {"go": "go.mod"})),   # 主语言自身跳过
    ("c", ["Cargo.toml", "rust/Cargo.toml"], (["rust"], {"rust": "Cargo.toml"})),  # 浅层优先
])
def test_detect_secondary_langs(tmp_path, lang, files, expected):
    _write_files(tmp_path, files)
    assert mod.detect_secondary_langs(lang, str(tmp_path)) == expected


def test_detect_secondary_langs_source_dir_missing():
    assert mod.detect_secondary_langs("python", "/nonexistent-dir-xyz") == ([], {})


# ─────────────────────────────────────────────
# 3. 输出路径构造
# ─────────────────────────────────────────────

@pytest.mark.parametrize("pkgname,requested,expected", [
    ("pkg", "", "/tmp/dep_check_pkg.json"),
    ("pkg", "/x/y.json", "/x/y.json"),
    ("pkg", "relative.json", "relative.json"),
])
def test_make_output_path(pkgname, requested, expected):
    assert mod.make_output_path(pkgname, requested) == expected


@pytest.mark.parametrize("output_path,pkgname,expected", [
    ("/tmp/out.json", "pkg", "/tmp/out_analysis.json"),
    ("/tmp/out", "pkg", "/tmp/out_analysis.json"),       # 无后缀补 .json
    ("/tmp/a.b.json", "pkg", "/tmp/a.b_analysis.json"),  # 多段后缀只剥最后一段
    ("", "pkg", "/tmp/dep_check_pkg_analysis.json"),
    ("/tmp/.hidden.json", "pkg", "/tmp/.hidden_analysis.json"),  # 隐藏文件 stem 保留点号(实际行为)
])
def test_make_analysis_path(output_path, pkgname, expected):
    assert str(mod.make_analysis_path(output_path, pkgname)) == expected


# ─────────────────────────────────────────────
# 4. resolve_python_executable
# ─────────────────────────────────────────────

def test_resolve_python_executable_falls_back_to_current(monkeypatch):
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "exists", lambda self: self.as_posix() == sys.executable)
    assert mod.resolve_python_executable() == sys.executable


def test_resolve_python_executable_prefers_which(monkeypatch):
    monkeypatch.setattr(
        Path, "exists",
        lambda self: self.as_posix() not in ("/usr/bin/python3.11", "/usr/local/bin/python3.11"))
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/opt/py311/bin/python3.11")
    assert mod.resolve_python_executable() == "/opt/py311/bin/python3.11"


def test_resolve_python_executable_never_empty():
    assert mod.resolve_python_executable()  # 最坏回退 "python3",永不为空


def test_resolve_python_executable_nothing_found(monkeypatch):
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "exists", lambda self: False)
    assert mod.resolve_python_executable() == "python3"


def test_loader_spec_failure_raises(monkeypatch):
    import importlib.util as _iu
    monkeypatch.setattr(_iu, "spec_from_file_location", lambda name, path: None)
    with pytest.raises(RuntimeError):
        mod.load_existing_checker()
    with pytest.raises(RuntimeError):
        mod._load_cascade_checker()
    with pytest.raises(RuntimeError):
        mod.load_python_upstream_helpers()


def test_loaders_readd_script_dir(monkeypatch):
    # scripts 目录不在 sys.path 时,loader 自行补回(与 analyze_python_deps 同款)
    script_dir = str(mod.SCRIPT_DIR)
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != script_dir])
    assert script_dir not in sys.path
    assert mod.load_existing_checker() is not None
    assert script_dir in sys.path
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != script_dir])
    assert mod.load_python_upstream_helpers()  # 返回 helpers dict
    assert script_dir in sys.path


# ─────────────────────────────────────────────
# 5. 级联 decision → action 映射
# ─────────────────────────────────────────────

def test_upstream_url_trusted_and_suspicious_helpers():
    assert mod.is_trusted_upstream_url("https://github.com/o/r") is True
    assert mod.is_trusted_upstream_url("https://pypi.org/project/x") is False
    assert mod.is_trusted_upstream_url("") is False
    assert mod.is_suspicious_upstream_url("https://github.com/o/r/issues/1") is True
    assert mod.is_suspicious_upstream_url("https://github.com/o/r") is False
    assert mod.is_suspicious_upstream_url("") is False

@pytest.mark.parametrize("decision,has_url,expected", [
    ("reuse_copr_project", True, ("resolved", "用户 COPR project 已有成功构建，直接复用")),
    ("reuse_copr_project", False, ("resolved", "用户 COPR project 已有成功构建，直接复用")),
    ("reuse_eur_srpm", True, ("recurse", "EUR 已有匹配版本（chroot 一致），以 EUR SRPM 为参考重建到用户 project")),
    ("reuse_official", True, ("resolved", "openEuler 官方源版本满足要求，直接复用")),
    ("reuse_additional_repo", True, ("resolved", "项目 additional_repos（外挂源）已有满足要求的版本，直接复用")),
    ("evaluate", True, ("recurse", "openEuler 官方源版本不满足要求，需引入更高版本")),
    ("introduce_new_with_ref", True, ("recurse", "已有参考源（gitcode/EUR），以参考 spec 为起点构建")),
    ("introduce_new", True, ("recurse", "所有来源均未找到，需全新引入")),
    ("introduce_new", False, ("needs_ai", "所有来源均未找到，需 AI 补全 upstream URL")),
])
def test_cascade_decision_to_action(decision, has_url, expected):
    assert mod._cascade_decision_to_action(decision, has_url) == expected


def test_cascade_decision_to_action_unknown():
    assert mod._cascade_decision_to_action("bogus", True) == ("", "")
    assert mod._cascade_decision_to_action("", False) == ("", "")


# ─────────────────────────────────────────────
# 6. 级联结果 → existing_check 结构
# ─────────────────────────────────────────────

@pytest.mark.parametrize("cascade,decision,exists,meets,version", [
    ({"decision": "reuse_copr_project", "level": 0, "match": {"version": "1.2.3"}},
     "reuse_user_repo", True, True, "1.2.3"),
    ({"decision": "reuse_eur_srpm", "level": 1, "match": {"version": "2.0"}},
     "introduce_new", True, False, "2.0"),
    ({"decision": "reuse_official", "level": 2, "match": {"version": "3.0"}},
     "reuse_official", True, True, "3.0"),
    ({"decision": "reuse_additional_repo", "level": 5, "match": {"version": "4.0"}},
     "reuse_official", True, True, "4.0"),
    ({"decision": "evaluate", "level": 2, "match": {"version": "1.0"}},
     "block_official_older", True, False, "1.0"),
    ({"decision": "introduce_new_with_ref", "level": 3, "match": None},
     "introduce_new", False, False, ""),
    ({"decision": "introduce_new", "level": 4, "match": None},
     "introduce_new", False, False, ""),
    # 已知行为固化:level 0(用户 COPR)/1(EUR) 被推断为 official.exists=True,
    # 与"官方源"语义不完全一致,按实际实现断言
    ({"decision": "weird", "level": 4, "match": None}, "weird", False, False, ""),
])
def test_build_existing_check_from_cascade(cascade, decision, exists, meets, version):
    r = mod._build_existing_check_from_cascade(cascade, {"name": "x"})
    assert r["decision"] == decision
    assert r["official"]["exists"] is exists
    assert r["official"]["meets_need"] is meets
    assert r["official"]["satisfies_requirement"] is meets
    assert r["official"]["comparison_unknown"] is False
    assert r["reason"] == f"cascade L{cascade['level']}: {cascade['decision']}"
    assert r["cascade"] is cascade
    if version:
        assert r["official"]["highest"] == {"version": version}
    else:
        assert r["official"]["highest"] is None


def test_build_existing_check_from_cascade_version_fallback():
    # match 无 version 时回退 cascade["version"]
    r = mod._build_existing_check_from_cascade(
        {"decision": "reuse_official", "level": 2, "match": None, "version": "9.9"},
        {"name": "x"})
    assert r["official"]["highest"] == {"version": "9.9"}


# ─────────────────────────────────────────────
# 7. summarize_source_match
# ─────────────────────────────────────────────

_SRC_ITEM = {"rpm": "python3-requests", "version": "2.31.0", "release": "1"}


@pytest.mark.parametrize("requirement,item,status,satisfies", [
    ("", None, "missing", False),
    (">=2.0", _SRC_ITEM, "satisfied", True),
    (">=3.0", _SRC_ITEM, "older", False),
    ("!=1.5", _SRC_ITEM, "unknown_requirement", False),   # 不可靠解析保守继续
    ("", _SRC_ITEM, "satisfied", True),                   # 无约束视为满足
    (">= 2.0", _SRC_ITEM, "satisfied", True),             # 带空格约束可解析
])
def test_summarize_source_match(requirement, item, status, satisfies):
    r = mod.summarize_source_match({"requirement": requirement}, item)
    assert r["status"] == status
    assert r["satisfies_requirement"] is satisfies
    assert r["reason"]


def test_summarize_source_match_missing_details():
    r = mod.summarize_source_match({"requirement": ">=2.0"}, None)
    assert r["rpm"] is None and r["version"] is None and r["release"] is None
    assert r["reason"] == "openEuler 源中未找到可用包"


def test_summarize_source_match_older_reason_contains_version():
    r = mod.summarize_source_match({"requirement": ">=3.0"}, _SRC_ITEM)
    assert "2.31.0" in r["reason"] and ">=3.0" in r["reason"]


# ─────────────────────────────────────────────
# 8. build_source_index / lookup_source_item
# ─────────────────────────────────────────────

def test_build_source_index_four_keys():
    item = {"dep": "requests>=2.0", "name": "requests", "requirement": ">=2.0",
            "rpm": "python3-requests"}
    idx = mod.build_source_index([item])
    assert ("requests>=2.0", ">=2.0") in idx
    assert ("requests>=2.0", "") in idx
    assert ("requests", ">=2.0") in idx
    assert ("requests", "") in idx
    assert all(v is item for v in idx.values())


def test_build_source_index_skips_empty_names():
    assert mod.build_source_index([{"dep": ""}]) == {}
    assert mod.build_source_index([{}]) == {}
    assert mod.build_source_index([]) == {}


@pytest.mark.parametrize("dep,expected_hit", [
    ({"name": "requests", "dep": "requests>=2.0", "requirement": ">=2.0"}, True),
    ({"name": "requests", "dep": "requests>=3.0", "requirement": ">=3.0"}, True),  # dep 无约束兜底键
    ({"name": "requests", "dep": "requests", "requirement": ""}, True),
    ({"name": "click", "dep": "click", "requirement": ""}, False),
    ({"name": "", "dep": ""}, False),
])
def test_lookup_source_item(dep, expected_hit):
    idx = mod.build_source_index([{"dep": "requests>=2.0", "name": "requests",
                                   "requirement": ">=2.0", "rpm": "python3-requests"}])
    got = mod.lookup_source_item(dep, idx)
    if expected_hit:
        assert got is not None and got["rpm"] == "python3-requests"
    else:
        assert got is None


# ─────────────────────────────────────────────
# 9. merge_official_source_older_result
# ─────────────────────────────────────────────

def test_merge_official_source_older_result(monkeypatch):
    monkeypatch.setattr(mod, "EXISTING_CHECKER",
                        make_existing_stub(choose="introduce_new"))
    dep = _dep("foo", "foo", ">=2.0")
    source_check = {"status": "older", "satisfies_requirement": False,
                    "rpm": "python3-foo", "version": "1.0", "release": "1"}
    existing_check = {"requested": {"version": "2.0", "requirement": ">=2.0"},
                      "official": {"exists": False, "matched_paths": ["x"], "candidates": []},
                      "user_repo": {"exists": False}}
    merged = mod.merge_official_source_older_result(dep, source_check, existing_check)
    assert merged["exists_in_official"] is True
    assert merged["decision"] == "introduce_new"
    assert merged["reason"] == "merged-reason"
    official = merged["official"]
    assert "<openeuler-source>" in official["matched_paths"]
    assert official["highest"]["path"] == "<openeuler-source>"
    assert official["highest"]["match_type"] == "source_repo"
    assert official["highest"]["name"] == "python3-foo"
    assert official["highest"]["version"] == "1.0"
    assert official["satisfies_requested_version"] is False
    assert official["satisfies_requirement"] is False
    assert official["meets_need"] is False
    # 原 existing_check 不被原地修改
    assert existing_check["official"]["exists"] is False
    assert "matched_paths" not in existing_check["official"] or \
        "<openeuler-source>" not in existing_check["official"]["matched_paths"]


def test_merge_official_source_older_no_requested_version(monkeypatch):
    monkeypatch.setattr(mod, "EXISTING_CHECKER", make_existing_stub())
    merged = mod.merge_official_source_older_result(
        _dep("foo", "foo", ""), {"status": "older", "rpm": "python3-foo",
                                 "version": "1.0", "release": None},
        {"official": {"exists": False, "matched_paths": [], "candidates": []},
         "user_repo": {}})
    assert merged["official"]["satisfies_requested_version"] is None


# ─────────────────────────────────────────────
# 10. classify_requirement_constraint / infer_version_source
# ─────────────────────────────────────────────

@pytest.mark.parametrize("requirement,expected_type", [
    ("", "unbounded"),
    (">=1.0", "range"),
    ("==1.0", "exact"),
    ("^1.2.3", "range"),      # npm caret 归一化为 range
    ("~2.0", "range"),        # npm tilde 归一化为 range
    (">=1.0,<2.0", "range"),
    ("!=1.5", "range"),
])
def test_classify_requirement_constraint(requirement, expected_type):
    ctype, info = mod.classify_requirement_constraint(requirement, None)
    assert ctype == expected_type
    if expected_type == "unbounded":
        assert info == {}
    else:
        assert info  # 解析产物非空


def test_classify_requirement_constraint_with_info():
    # requirement_info 传入时保留原字段并补充 specifiers
    ctype, info = mod.classify_requirement_constraint(
        ">=1.0", {"status": "parsed", "clauses": [{"operator": ">=", "version": "1.0"}]})
    assert ctype == "range"
    assert info["status"] == "parsed"
    assert {"operator": ">=", "version": "1.0"} in info["specifiers"]


@pytest.mark.parametrize("item,existing_check,expected", [
    ({"version_source": "pypi"}, None, "pypi"),
    ({"requirement_info": {"source": "requirements.txt"}}, None, "requirements.txt"),
    ({}, {"requested": {"requirement_info": {"source": "pyproject.toml"}}}, "pyproject.toml"),
    ({"requirement": ">=1.0"}, None, "manifest"),
    ({}, None, "unknown"),
    ({"version_source": "  "}, None, "unknown"),  # 空白显式来源视为未提供
])
def test_infer_version_source(item, existing_check, expected):
    assert mod.infer_version_source(item, existing_check) == expected


# ─────────────────────────────────────────────
# 11. normalize_dependency_item
# ─────────────────────────────────────────────

def test_normalize_dependency_item_full(monkeypatch):
    monkeypatch.setattr(mod, "ensure_dependency_upstream",
                        lambda item, lang: ("https://github.com/o/r", "provided"))
    norm = mod.normalize_dependency_item(
        {"name": "foo", "dep": "foo>=1.0", "spec": "foo>=1.0", "requirement": ">=1.0",
         "type": "build"}, "python", "runtime")
    assert norm["name"] == "foo"
    assert norm["dep"] == "foo>=1.0"
    assert norm["spec"] == "foo>=1.0"
    assert norm["type"] == "build"          # 显式 type 优先
    assert norm["category"] == "runtime"
    assert norm["constraint_type"] == "range"
    assert norm["version_source"] == "manifest"
    assert norm["rpm_requirement"] == "foo>=1.0"
    assert norm["rpm_pkg_name"] == "python3-foo"
    assert norm["upstream_url"] == "https://github.com/o/r"
    assert norm["upstream_resolution"] == "provided"


def test_normalize_dependency_item_fallbacks(monkeypatch):
    monkeypatch.setattr(mod, "ensure_dependency_upstream",
                        lambda item, lang: ("", "unresolved"))
    norm = mod.normalize_dependency_item({"dep": "click"}, "python", "runtime")
    assert norm["name"] == "click"
    assert norm["dep"] == "click"
    assert norm["spec"] == "click"
    assert norm["type"] == "python"         # 无 type 回退语言
    assert norm["requirement"] == ""
    assert norm["constraint_type"] == "unbounded"
    assert norm["version_source"] == "unknown"
    assert norm["rpm_requirement"] == "click"
    assert norm["rpm_pkg_name"] == "python3-click"
    assert norm["upstream_url"] == ""
    assert norm["upstream_resolution"] == "unresolved"


def test_normalize_dependency_item_rpm_fields_precedence(monkeypatch):
    monkeypatch.setattr(mod, "ensure_dependency_upstream",
                        lambda item, lang: ("", "unresolved"))
    norm = mod.normalize_dependency_item(
        {"dep": "go-x", "rpm_requirement": "golang-x", "rpm_pkg_name": "golang-x"},
        "go", "runtime")
    assert norm["rpm_requirement"] == "golang-x"
    assert norm["rpm_pkg_name"] == "golang-x"


# ─────────────────────────────────────────────
# 12. dependency_items_from_result
# ─────────────────────────────────────────────

_PY_RESULT = {
    "rpm_check": {
        "missing": [{"name": "click", "dep": "click"}],
        "version_conflict": [{"name": "flask", "found_version": "1.0"}],
    },
    "dependency_items": [
        {"name": "requests", "dep": "requests>=2.0", "requirement": ">=2.0"},
        {"name": "flask", "dep": "flask"},          # 在 version_conflict → 排除
        {"name": "click", "dep": "click"},          # 在 missing → 排除
    ],
    "build_sys_dependency_items": [
        {"name": "hatchling", "dep": "hatchling"},
        {"name": "selfpkg", "dep": "selfpkg"},      # 自引用 → 跳过
        {"name": "flask", "dep": "flask"},          # 已冲突处理 → 跳过
    ],
}


def test_dependency_items_python(monkeypatch):
    monkeypatch.setattr(mod, "resolve_upstream_url", lambda name, lang: "")
    pending, preblocked = mod.dependency_items_from_result("python", _PY_RESULT, "selfpkg")
    assert [(i["name"], i["category"]) for i in pending] == [
        ("requests", "runtime"), ("hatchling", "build_system"), ("click", "runtime")]
    assert [(i["name"], i["found_version"], i["preblocked"]) for i in preblocked] == \
        [("flask", "1.0", True)]


def test_dependency_items_python_no_pkgname_keeps_self_ref(monkeypatch):
    monkeypatch.setattr(mod, "resolve_upstream_url", lambda name, lang: "")
    pending, _ = mod.dependency_items_from_result("python", _PY_RESULT, "")
    assert ("selfpkg", "build_system") in [(i["name"], i["category"]) for i in pending]


def test_dependency_items_python_preblocked_not_in_pending(monkeypatch):
    monkeypatch.setattr(mod, "resolve_upstream_url", lambda name, lang: "")
    pending, preblocked = mod.dependency_items_from_result("python", _PY_RESULT, "selfpkg")
    names = {i["name"] for i in pending}
    for item in preblocked:
        assert item["name"] not in names


def test_dependency_items_cpp_ignores_rpm_check(monkeypatch):
    monkeypatch.setattr(mod, "resolve_upstream_url", lambda name, lang: "")
    result = {"dependency_items": [{"name": "fmt", "dep": "fmt"}],
              "rpm_check": {"missing": [{"name": "zlib"}]}}
    pending, preblocked = mod.dependency_items_from_result("cpp", result)
    assert [i["name"] for i in pending] == ["fmt"]
    assert preblocked == []


def test_dependency_items_nodejs_runtime_deps(monkeypatch):
    monkeypatch.setattr(mod, "resolve_upstream_url", lambda name, lang: "")
    result = {"rpm_check": {"missing": [{"name": "lodash"}]},
              "runtime_deps": {"missing": [{"name": "express"}],
                               "version_conflict": [{"name": "async", "found_version": "1.0"}]}}
    pending, preblocked = mod.dependency_items_from_result("nodejs", result)
    assert [i["name"] for i in pending] == ["lodash", "express"]
    assert [(i["name"], i["found_version"]) for i in preblocked] == [("async", "1.0")]


def test_dependency_items_other_lang_rpm_check_only(monkeypatch):
    monkeypatch.setattr(mod, "resolve_upstream_url", lambda name, lang: "")
    result = {"rpm_check": {"missing": [{"name": "zlib"}],
                            "version_conflict": [{"name": "openssl", "found_version": "1.1"}]},
              "dependency_items": [{"name": "ignored"}]}   # 非 python/cpp 忽略
    pending, preblocked = mod.dependency_items_from_result("c", result)
    assert [i["name"] for i in pending] == ["zlib"]
    assert [(i["name"], i["found_version"]) for i in preblocked] == [("openssl", "1.1")]


def test_dependency_items_empty_result():
    assert mod.dependency_items_from_result("python", {}) == ([], [])
    assert mod.dependency_items_from_result("python", {}, "pkg") == ([], [])


# ─────────────────────────────────────────────
# 13. build_available_index_for_result
# ─────────────────────────────────────────────

def test_build_available_index_python_includes_build_sys():
    idx = mod.build_available_index_for_result("python", {
        "rpm_check": {"available": [{"dep": "a", "name": "a"}]},
        "build_sys_rpm_check": {"available": [{"dep": "b", "name": "b"}]}})
    assert ("a", "") in idx and ("b", "") in idx


def test_build_available_index_nodejs_includes_runtime_deps():
    idx = mod.build_available_index_for_result("nodejs", {
        "rpm_check": {"available": [{"dep": "a", "name": "a"}]},
        "runtime_deps": {"available": [{"dep": "c", "name": "c"}]}})
    assert ("a", "") in idx and ("c", "") in idx


def test_build_available_index_other_lang_ignores_extra():
    # c 语言不并入 build_sys / runtime_deps 的 available
    idx = mod.build_available_index_for_result("c", {
        "rpm_check": {"available": [{"dep": "a", "name": "a"}]},
        "build_sys_rpm_check": {"available": [{"dep": "b", "name": "b"}]}})
    assert ("a", "") in idx and ("b", "") not in idx


def test_build_available_index_empty():
    assert mod.build_available_index_for_result("python", {}) == {}


# ─────────────────────────────────────────────
# 14. classify_preblocked_dependency
# ─────────────────────────────────────────────

def test_preblocked_reuse_same_major_newer(no_cascade_env):
    dep = {"name": "requests", "dep": "requests", "found_version": "2.31.0",
           "requirement": ">=2.0"}
    r = mod.classify_preblocked_dependency(dep, "python")
    assert r["decision"] == "reuse_official"
    assert r["action"] == "resolved"
    assert r["source_check"] == {"status": "ok", "satisfies_requirement": True}
    assert r["existing_check"]["official"]["exists"] is True
    assert "同主版本且更新" in r["reason"]


@pytest.mark.parametrize("extra,expected_action", [
    ({"upstream_url": "https://github.com/org/repo"}, "recurse"),
    ({}, "needs_ai"),
])
def test_preblocked_older_fallback(no_cascade_env, extra, expected_action):
    dep = {"name": "requests", "found_version": "1.0.0", "requirement": ">=2.0", **extra}
    r = mod.classify_preblocked_dependency(dep, "python")
    assert r["decision"] == "block_official_older"
    assert r["action"] == expected_action
    assert r["source_check"]["status"] == "older"
    assert r["existing_check"]["official"]["highest"] == {"version": "1.0.0"}
    assert "引入更高版本到 COPR" in r["existing_check"]["reason"]


def test_preblocked_no_found_version(no_cascade_env):
    r = mod.classify_preblocked_dependency(
        {"name": "x", "requirement": ">=1.0", "upstream_url": "https://github.com/o/x"},
        "python")
    assert r["decision"] == "block_official_older"
    assert r["action"] == "recurse"
    assert r["existing_check"]["official"]["highest"] is None


def test_preblocked_no_requirement(no_cascade_env):
    r = mod.classify_preblocked_dependency({"name": "x", "found_version": "1.0"}, "python")
    assert r["decision"] == "block_official_older"
    assert r["action"] == "needs_ai"


def test_preblocked_version_parse_failure_blocks(no_cascade_env):
    # 已知行为固化:found_version 含非数值段(如 "2.0.0rc1")时 int() 比较抛
    # ValueError → 视为不满足,走 block 分支(即使实际可能更新)
    dep = {"name": "x", "found_version": "2.0.0rc1", "requirement": ">=1.0",
           "upstream_url": "https://github.com/o/x"}
    r = mod.classify_preblocked_dependency(dep, "python")
    assert r["decision"] == "block_official_older"
    assert r["action"] == "recurse"


def test_preblocked_cascade_recurse(monkeypatch):
    monkeypatch.setattr(mod, "_enrich_via_cascade", lambda *a: {
        "decision": "reuse_eur_srpm", "level": 1, "match": None})
    dep = {"name": "x", "found_version": "1.0", "requirement": ">=2.0",
           "upstream_url": "https://github.com/o/x"}
    r = mod.classify_preblocked_dependency(dep, "python")
    assert r["action"] == "recurse"
    assert "EUR 已有匹配版本" in r["reason"]


def test_preblocked_cascade_resolved(monkeypatch):
    # 已知行为固化:级联 reuse_official 时 action=resolved 但 decision 仍为
    # block_official_older(两字段不同步)
    monkeypatch.setattr(mod, "_enrich_via_cascade", lambda *a: {
        "decision": "reuse_official", "level": 2, "match": None})
    dep = {"name": "x", "found_version": "1.0", "requirement": ">=2.0"}
    r = mod.classify_preblocked_dependency(dep, "python")
    assert r["action"] == "resolved"
    assert r["decision"] == "block_official_older"
    assert "openEuler 官方源版本满足要求" in r["reason"]


# ─────────────────────────────────────────────
# 15. classify_dependency — 核心状态机
# ─────────────────────────────────────────────

def test_classify_reuse_source():
    idx = mod.build_source_index([{"dep": "requests>=2.0", "name": "requests",
                                   "requirement": ">=2.0", "rpm": "python3-requests",
                                   "version": "2.31.0", "release": "1"}])
    dep = _dep("requests", "requests>=2.0", ">=2.0")
    r = mod.classify_dependency(dep, "python", idx)
    assert r["decision"] == "reuse_source"
    assert r["action"] == "resolved"
    assert r["existing_check"] is None
    assert r["source_check"]["status"] == "satisfied"
    assert r["debug_constraint_flow"]["after"]["decision"] == "reuse_source"
    assert dep["constraint_type"] == "range"    # 入参被原地更新(实际行为)


def test_classify_reuse_source_without_requirement():
    idx = mod.build_source_index([{"dep": "click", "name": "click",
                                   "rpm": "python3-click", "version": "8.1.7"}])
    r = mod.classify_dependency(_dep("click", "click", ""), "python", idx)
    assert r["decision"] == "reuse_source"
    assert r["source_check"]["status"] == "satisfied"


@pytest.mark.parametrize("cascade,expected_decision,expected_action", [
    ({"decision": "reuse_official", "level": 2, "match": {"version": "3.0"}},
     "reuse_official", "resolved"),
    ({"decision": "reuse_copr_project", "level": 0, "match": {"version": "1.0"}},
     "reuse_user_repo", "resolved"),
    ({"decision": "reuse_additional_repo", "level": 5, "match": {"version": "4.0"}},
     "reuse_official", "resolved"),
    ({"decision": "reuse_eur_srpm", "level": 1, "match": {"version": "2.0"}},
     "introduce_new", "needs_ai"),
    ({"decision": "evaluate", "level": 2, "match": {"version": "1.0"}},
     "block_official_older", "blocked"),
    ({"decision": "introduce_new", "level": 4, "match": None},
     "introduce_new", "needs_ai"),
])
def test_classify_cascade_decisions(monkeypatch, cascade, expected_decision, expected_action):
    monkeypatch.setattr(mod, "_dep_conflict_mode", lambda: "block")
    monkeypatch.setattr(mod, "_enrich_via_cascade",
                        lambda pkgname, lang, requirement: cascade)
    r = mod.classify_dependency(_dep("foo", "foo", ">=2.0"), "python", {})
    assert r["decision"] == expected_decision
    assert r["action"] == expected_action
    assert r["existing_check"]["cascade"] is cascade


def test_classify_cascade_introduce_new_with_url(monkeypatch):
    monkeypatch.setattr(mod, "_enrich_via_cascade", lambda *a: {
        "decision": "introduce_new", "level": 4, "match": None})
    r = mod.classify_dependency(
        _dep("bar", "bar", "", upstream_url="https://github.com/o/bar"), "python", {})
    assert (r["decision"], r["action"]) == ("introduce_new", "recurse")


def test_classify_unknown_cascade_decision(monkeypatch):
    monkeypatch.setattr(mod, "_enrich_via_cascade", lambda *a: {
        "decision": "weird", "level": 4, "match": None})
    r = mod.classify_dependency(_dep("x", "x", ""), "python", {})
    assert (r["decision"], r["action"]) == ("weird", "needs_ai")


def test_classify_fallback_reuse_official(monkeypatch):
    monkeypatch.setattr(mod, "_enrich_via_cascade", lambda *a: None)
    stub = make_existing_stub(decision="reuse_official", reason="官方源满足",
                              requested={"pkgname": "foo", "requirement": ">=2.0",
                                         "requirement_info": {"status": "parsed",
                                                              "source": "requirements.txt"}})
    monkeypatch.setattr(mod, "EXISTING_CHECKER", stub)
    r = mod.classify_dependency(_dep("foo", "foo", ">=2.0"), "python", {})
    assert (r["decision"], r["action"]) == ("reuse_official", "resolved")
    assert r["reason"] == "官方源满足"
    assert r["version_source"] == "requirements.txt"
    assert r["constraint_type"] == "range"


def test_classify_fallback_older_merges_official_source(monkeypatch):
    # 源索引有包但版本低 + L2 fallback 官方源不存在 → merge_official_source_older_result
    monkeypatch.setattr(mod, "_enrich_via_cascade", lambda *a: None)
    stub = make_existing_stub(decision="introduce_new", official_exists=False,
                              choose="introduce_new")
    monkeypatch.setattr(mod, "EXISTING_CHECKER", stub)
    idx = mod.build_source_index([{"dep": "foo", "name": "foo", "rpm": "python3-foo",
                                   "version": "1.0", "release": "1"}])
    r = mod.classify_dependency(
        _dep("foo", "foo", ">=2.0", upstream_url="https://github.com/o/foo"),
        "python", idx)
    assert (r["decision"], r["action"]) == ("introduce_new", "recurse")
    assert r["source_check"]["status"] == "older"
    assert r["existing_check"]["exists_in_official"] is True
    assert r["reason"] == "merged-reason"


def test_classify_older_source_with_cascade_skips_merge(monkeypatch):
    monkeypatch.setattr(mod, "_dep_conflict_mode", lambda: "block")
    monkeypatch.setattr(mod, "_enrich_via_cascade", lambda *a: {
        "decision": "evaluate", "level": 2, "match": {"version": "1.0"}})
    idx = mod.build_source_index([{"dep": "foo", "name": "foo", "rpm": "python3-foo",
                                   "version": "1.0", "release": "1"}])
    r = mod.classify_dependency(_dep("foo", "foo", ">=2.0"), "python", idx)
    assert r["source_check"]["status"] == "older"
    assert r["decision"] == "block_official_older"
    assert "exists_in_official" not in r["existing_check"]   # 级联官方源存在 → 不 merge


def test_classify_build_system_tool_reuse(monkeypatch):
    monkeypatch.setattr(mod, "_enrich_via_cascade", lambda *a: {
        "decision": "evaluate", "level": 2, "match": {"version": "0.5"}})
    r = mod.classify_dependency(
        _dep("hatchling", "hatchling", ">=2.0", category="build_system"), "python", {})
    assert (r["decision"], r["action"]) == ("reuse_official", "resolved")
    assert "白名单" in r["reason"]


def test_classify_build_system_tool_missing_needs_ai(monkeypatch):
    monkeypatch.setattr(mod, "_enrich_via_cascade", lambda *a: {
        "decision": "introduce_new", "level": 4, "match": None})
    r = mod.classify_dependency(
        _dep("maturin", "maturin", ">=1.0", category="build_system"), "python", {})
    assert (r["decision"], r["action"]) == ("needs_ai", "needs_ai")


def test_classify_compat_mode_c_recurse(monkeypatch):
    monkeypatch.setattr(mod, "_dep_conflict_mode", lambda: "compat")
    monkeypatch.setattr(mod, "_enrich_via_cascade", lambda *a: {
        "decision": "evaluate", "level": 2, "match": {"version": "1.0"}})
    r = mod.classify_dependency(
        _dep("libfoo", "libfoo", ">=2.0", upstream_url="https://github.com/o/libfoo"),
        "c", {})
    assert (r["decision"], r["action"]) == ("block_official_older", "recurse")
    assert r["compat_introduce"] is True
    assert r["compat_srpm_name"].startswith("libfoo-")
    assert r["compat_rpm_name"].startswith("libfoo-")
    assert "compat 模式" in r["reason"]


def test_classify_compat_mode_unsupported_lang_blocked(monkeypatch):
    monkeypatch.setattr(mod, "_dep_conflict_mode", lambda: "compat")
    monkeypatch.setattr(mod, "_enrich_via_cascade", lambda *a: {
        "decision": "evaluate", "level": 2, "match": {"version": "1.0"}})
    r = mod.classify_dependency(
        _dep("foo", "foo", ">=2.0", upstream_url="https://github.com/o/foo"),
        "python", {})
    assert (r["decision"], r["action"]) == ("block_official_older", "blocked")
    assert "不支持 compat 共存" in r["reason"]
    assert "compat_introduce" not in r


def test_classify_compat_mode_without_upstream_needs_ai(monkeypatch):
    monkeypatch.setattr(mod, "_dep_conflict_mode", lambda: "compat")
    monkeypatch.setattr(mod, "_enrich_via_cascade", lambda *a: {
        "decision": "evaluate", "level": 2, "match": {"version": "1.0"}})
    r = mod.classify_dependency(_dep("libfoo", "libfoo", ">=2.0"), "c", {})
    assert (r["decision"], r["action"]) == ("block_official_older", "needs_ai")
    assert r["compat_introduce"] is True
    assert "需 AI web search" in r["reason"]


def test_classify_force_compat_any_lang(monkeypatch):
    monkeypatch.setattr(mod, "_dep_conflict_mode", lambda: "force_compat")
    monkeypatch.setattr(mod, "_enrich_via_cascade", lambda *a: {
        "decision": "evaluate", "level": 2, "match": {"version": "1.0"}})
    r = mod.classify_dependency(
        _dep("foo", "foo", ">=2.0", upstream_url="https://github.com/o/foo"),
        "python", {})
    assert (r["decision"], r["action"]) == ("block_official_older", "recurse")
    assert r["compat_introduce"] is True
    assert r["compat_rpm_name"].startswith("python3-foo-")


def test_classify_block_official_older_blocked(monkeypatch):
    monkeypatch.setattr(mod, "_dep_conflict_mode", lambda: "block")
    monkeypatch.setattr(mod, "_enrich_via_cascade", lambda *a: {
        "decision": "evaluate", "level": 2, "match": {"version": "1.0"}})
    r = mod.classify_dependency(
        _dep("foo", "foo", ">=2.0", upstream_url="https://github.com/o/foo"),
        "python", {})
    assert (r["decision"], r["action"]) == ("block_official_older", "blocked")
    assert "compat_introduce" not in r


# ─────────────────────────────────────────────
# 16. build_summary / print_pending_to_stdout
# ─────────────────────────────────────────────

def test_build_summary_buckets():
    decisions = [
        {"name": "a", "action": "resolved"},
        {"name": "b", "action": "recurse"},
        {"name": "c", "action": "needs_ai"},
        {"name": "d", "action": "blocked"},
    ]
    s = mod.build_summary("pkg", "python", "/src", "/analysis.json", decisions)
    assert s["pkgname"] == "pkg" and s["lang"] == "python"
    assert s["source_dir"] == "/src" and s["analysis_file"] == "/analysis.json"
    assert [i["name"] for i in s["resolved"]] == ["a"]
    assert [i["name"] for i in s["pending"]] == ["b"]
    assert [i["name"] for i in s["needs_ai"]] == ["c"]
    assert [i["name"] for i in s["blocked"]] == ["d"]
    assert s["dependency_decisions"] is decisions


def test_build_summary_empty():
    s = mod.build_summary("pkg", "python", "/src", "", [])
    assert s["resolved"] == [] and s["pending"] == [] and s["needs_ai"] == []
    assert s["blocked"] == []


def test_print_pending_to_stdout_dedup(capsys):
    mod.print_pending_to_stdout([
        {"name": "a", "upstream_url": "https://github.com/o/a"},
        {"name": "a", "upstream_url": "https://github.com/o/a"},   # 重复 → 跳过
        {"name": "b", "upstream_url": ""},
    ])
    assert capsys.readouterr().out == "a https://github.com/o/a\nb\n"


def test_print_pending_to_stdout_empty(capsys):
    mod.print_pending_to_stdout([])
    assert capsys.readouterr().out == ""


# ─────────────────────────────────────────────
# 17. Rust MSRV / toolchain 预检
# ─────────────────────────────────────────────

def test_read_rust_toolchain_channel_toml(tmp_path):
    (tmp_path / "rust-toolchain.toml").write_text(
        '[toolchain]\nchannel = "1.92.0"\n', encoding="utf-8")
    assert mod._read_rust_toolchain_channel(tmp_path) == "1.92.0"


def test_read_rust_toolchain_channel_single_quotes(tmp_path):
    (tmp_path / "rust-toolchain.toml").write_text(
        "[toolchain]\nchannel = 'nightly'\n", encoding="utf-8")
    assert mod._read_rust_toolchain_channel(tmp_path) == "nightly"


def test_read_rust_toolchain_channel_legacy_file(tmp_path):
    (tmp_path / "rust-toolchain").write_text("stable\n", encoding="utf-8")
    assert mod._read_rust_toolchain_channel(tmp_path) == "stable"


def test_read_rust_toolchain_channel_missing_or_empty(tmp_path):
    assert mod._read_rust_toolchain_channel(tmp_path) == ""
    (tmp_path / "rust-toolchain.toml").write_text('[toolchain]\n# no channel\n',
                                                  encoding="utf-8")
    assert mod._read_rust_toolchain_channel(tmp_path) == ""


def test_read_rust_toolchain_channel_oserror(tmp_path):
    # 文件是目录 → read_text 抛 OSError → 跳过(实际行为)
    (tmp_path / "rust-toolchain.toml").mkdir()
    assert mod._read_rust_toolchain_channel(tmp_path) == ""


def test_read_cargo_rust_version_oserror(tmp_path):
    (tmp_path / "Cargo.toml").mkdir()
    assert mod._read_cargo_rust_version(tmp_path) == ""


@pytest.mark.parametrize("content,expected", [
    ('[package]\nname = "x"\nrust-version = "1.80.0"\n', "1.80.0"),
    ("[package]\nname = 'x'\nrust-version = '1.75'\n", "1.75"),
    ("[package]\nname = 'x'\n", ""),                 # 无 rust-version
    ("", ""),
])
def test_read_cargo_rust_version(tmp_path, content, expected):
    (tmp_path / "Cargo.toml").write_text(content, encoding="utf-8")
    assert mod._read_cargo_rust_version(tmp_path) == expected


def test_read_cargo_rust_version_missing(tmp_path):
    assert mod._read_cargo_rust_version(tmp_path) == ""


class _RustQueryStub:
    """EXISTING_CHECKER 的 rust repoquery 替身。"""

    def __init__(self, version="1.75.0", switched=True, raise_repo=False):
        self.version = version
        self.switched = switched
        self.raise_repo = raise_repo
        self.tore_down = False

    def setup_repo_for_chroot(self, chroot, **kw):
        if self.raise_repo:
            raise RuntimeError("repo setup failed")
        return self.switched

    def teardown_repo(self):
        self.tore_down = True

    def _dnf_repoquery(self, pkgname, lang):
        return {"version": self.version} if self.version else None


def test_query_chroot_rustc_version(monkeypatch):
    stub = _RustQueryStub()
    monkeypatch.setattr(mod, "EXISTING_CHECKER", stub)
    assert mod._query_chroot_rustc_version("openeuler-24.03-x86_64") == "1.75.0"
    assert stub.tore_down is True     # 切换过 repo → 必 teardown


def test_query_chroot_rustc_version_no_switch_no_teardown(monkeypatch):
    stub = _RustQueryStub(switched=False)
    monkeypatch.setattr(mod, "EXISTING_CHECKER", stub)
    assert mod._query_chroot_rustc_version("openeuler-24.03-x86_64") == "1.75.0"
    assert stub.tore_down is False


def test_query_chroot_rustc_version_empty_chroot():
    assert mod._query_chroot_rustc_version("") == ""


def test_query_chroot_rustc_version_exception(monkeypatch):
    monkeypatch.setattr(mod, "EXISTING_CHECKER", _RustQueryStub(raise_repo=True))
    assert mod._query_chroot_rustc_version("openeuler-24.03-x86_64") == ""


def _write_rust_manifest(tmp_path, channel=None, rust_version=None):
    if channel is not None:
        (tmp_path / "rust-toolchain.toml").write_text(
            f'[toolchain]\nchannel = "{channel}"\n', encoding="utf-8")
    if rust_version is not None:
        (tmp_path / "Cargo.toml").write_text(
            f'[package]\nname = "x"\nversion = "1.0"\nrust-version = "{rust_version}"\n',
            encoding="utf-8")


def test_check_rust_toolchain_nightly_blocked(tmp_path, no_cascade_env):
    _write_rust_manifest(tmp_path, channel="nightly")
    conflict = mod.check_rust_toolchain("pkg", str(tmp_path))
    assert conflict is not None
    assert conflict["name"] == "pkg"
    assert "nightly" in conflict["reason"]


def test_check_rust_toolchain_msrv_too_new(tmp_path, monkeypatch, no_cascade_env):
    monkeypatch.setenv("COPR_CHROOT", "openeuler-24.03-x86_64")
    monkeypatch.setattr(mod, "_query_chroot_rustc_version", lambda chroot: "1.75.0")
    _write_rust_manifest(tmp_path, rust_version="1.80.0")
    conflict = mod.check_rust_toolchain("pkg", str(tmp_path))
    assert conflict is not None
    assert "要求 rustc >= 1.80.0" in conflict["reason"]
    assert "openeuler-24.03-x86_64" in conflict["reason"]


def test_check_rust_toolchain_chroot_newer_passes(tmp_path, monkeypatch, no_cascade_env):
    monkeypatch.setenv("COPR_CHROOT", "openeuler-24.03-x86_64")
    monkeypatch.setattr(mod, "_query_chroot_rustc_version", lambda chroot: "1.90.0")
    _write_rust_manifest(tmp_path, rust_version="1.80.0")
    assert mod.check_rust_toolchain("pkg", str(tmp_path)) is None


def test_check_rust_toolchain_channel_version(tmp_path, monkeypatch, no_cascade_env):
    monkeypatch.setenv("COPR_CHROOT", "openeuler-24.03-x86_64")
    monkeypatch.setattr(mod, "_query_chroot_rustc_version", lambda chroot: "1.75.0")
    _write_rust_manifest(tmp_path, channel="1.92.0")
    assert mod.check_rust_toolchain("pkg", str(tmp_path)) is not None


def test_check_rust_toolchain_no_declared_version(tmp_path, no_cascade_env):
    _write_rust_manifest(tmp_path)
    assert mod.check_rust_toolchain("pkg", str(tmp_path)) is None


def test_check_rust_toolchain_chroot_unknown_passes(tmp_path, monkeypatch, no_cascade_env):
    # 查不到 chroot rustc 版本 → 保守放行,不误判
    monkeypatch.setenv("COPR_CHROOT", "openeuler-24.03-x86_64")
    monkeypatch.setattr(mod, "_query_chroot_rustc_version", lambda chroot: "")
    _write_rust_manifest(tmp_path, rust_version="1.80.0")
    assert mod.check_rust_toolchain("pkg", str(tmp_path)) is None


def test_check_rust_toolchain_version_parse_error_skips(tmp_path, monkeypatch, no_cascade_env):
    # 版本号解析抛异常时跳过该条要求,不误判
    monkeypatch.setenv("COPR_CHROOT", "openeuler-24.03-x86_64")
    monkeypatch.setattr(mod, "_query_chroot_rustc_version", lambda chroot: "1.75.0")
    import packaging.version as _pv
    monkeypatch.setattr(_pv, "Version",
                        lambda v: (_ for _ in ()).throw(ValueError("bad version")))
    _write_rust_manifest(tmp_path, rust_version="1.80.0")
    assert mod.check_rust_toolchain("pkg", str(tmp_path)) is None


# ─────────────────────────────────────────────
# 18. _enrich_via_cascade
# ─────────────────────────────────────────────

def test_enrich_via_cascade_no_creds(no_cascade_env):
    assert mod._enrich_via_cascade("foo", "python", ">=1.0") is None


def test_enrich_via_cascade_passes_creds(monkeypatch):
    for var in _CASCADE_CRED_ENV:
        monkeypatch.setenv(var, f"val-{var}")
    monkeypatch.setenv("COPR_CHROOT", "openeuler-24.03-x86_64")
    captured = {}

    def fake_check(pkgname, lang="", version="", requirement="", target="",
                   copr_url="", copr_owner="", copr_project="",
                   copr_login="", copr_token=""):
        captured.update(pkgname=pkgname, lang=lang, version=version,
                        requirement=requirement, target=target, copr_url=copr_url,
                        copr_owner=copr_owner, copr_project=copr_project,
                        copr_login=copr_login, copr_token=copr_token)
        return {"decision": "reuse_official", "level": 2, "match": None}

    monkeypatch.setattr(mod.CASCADE_CHECKER, "check_package_existence", fake_check)
    result = mod._enrich_via_cascade("foo", "python", ">=1.0")
    assert result == {"decision": "reuse_official", "level": 2, "match": None}
    assert captured["pkgname"] == "foo"
    assert captured["lang"] == "python"
    assert captured["version"] == ""          # version 留空,由级联从 requirement 推导
    assert captured["requirement"] == ">=1.0"
    assert captured["target"] == "openeuler-24.03-x86_64"
    assert captured["copr_url"] == "val-COPR_FRONTEND_URL"
    assert captured["copr_owner"] == "val-COPR_OWNER"
    assert captured["copr_login"] == "val-COPR_API_LOGIN"
    assert captured["copr_token"] == "val-COPR_API_TOKEN"


def test_enrich_via_cascade_exception_returns_none(monkeypatch):
    for var in _CASCADE_CRED_ENV:
        monkeypatch.setenv(var, "val")
    monkeypatch.setattr(
        mod.CASCADE_CHECKER, "check_package_existence",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("cascade boom")))
    assert mod._enrich_via_cascade("foo", "python", ">=1.0") is None


# ─────────────────────────────────────────────
# 19. 上游 URL 解析
# ─────────────────────────────────────────────

def test_github_search_repo_direct_hit(monkeypatch):
    def fake_urlopen(req, timeout=8):
        return _FakeUrlResponse({"html_url": "https://github.com/psf/requests"})

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    assert mod._github_search_repo("requests") == "https://github.com/psf/requests"


def test_github_search_repo_search_fallback(monkeypatch):
    def fake_urlopen(req, timeout=8):
        if "/search/repositories" in req.full_url:
            return _FakeUrlResponse({"items": [
                {"name": "other", "html_url": "https://github.com/o/other"},
                {"name": "requests", "html_url": "https://github.com/psf/requests"}]})
        raise urllib.error.URLError("nope")

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    assert mod._github_search_repo("requests") == "https://github.com/psf/requests"


def test_github_search_repo_search_no_match(monkeypatch):
    def fake_urlopen(req, timeout=8):
        if "/search/repositories" in req.full_url:
            return _FakeUrlResponse({"items": [
                {"name": "other", "html_url": "https://github.com/o/other"}]})
        raise urllib.error.URLError("nope")

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    assert mod._github_search_repo("requests") == ""


def test_github_search_repo_all_fail(monkeypatch):
    monkeypatch.setattr(mod.urllib.request, "urlopen",
                        lambda req, timeout=8: (_ for _ in ()).throw(OSError("net")))
    assert mod._github_search_repo("requests") == ""


def test_get_pypi_upstream_canonical_trusted(monkeypatch):
    monkeypatch.setitem(mod.PYTHON_UPSTREAM_HELPERS, "fetch_pypi_info",
                        lambda name: {"info": {}})
    monkeypatch.setitem(mod.PYTHON_UPSTREAM_HELPERS, "canonical_upstream_url",
                        lambda js, name: "https://github.com/o/r")
    assert mod.get_pypi_upstream("foo") == "https://github.com/o/r"


def test_get_pypi_upstream_candidate_fallback(monkeypatch):
    monkeypatch.setitem(mod.PYTHON_UPSTREAM_HELPERS, "fetch_pypi_info",
                        lambda name: {"info": {"project_urls": {}}})
    monkeypatch.setitem(mod.PYTHON_UPSTREAM_HELPERS, "canonical_upstream_url",
                        lambda js, name: "")
    monkeypatch.setitem(mod.PYTHON_UPSTREAM_HELPERS, "candidate_urls_from_pypi_info",
                        lambda info: ["https://github.com/o/r"])
    assert mod.get_pypi_upstream("foo") == "https://github.com/o/r"


def test_get_pypi_upstream_github_fallback(monkeypatch):
    monkeypatch.setitem(mod.PYTHON_UPSTREAM_HELPERS, "fetch_pypi_info", lambda name: None)
    monkeypatch.setattr(mod, "_github_search_repo", lambda name: "https://github.com/o/r")
    assert mod.get_pypi_upstream("foo") == "https://github.com/o/r"


def test_get_pypi_upstream_unresolved(monkeypatch):
    monkeypatch.setitem(mod.PYTHON_UPSTREAM_HELPERS, "fetch_pypi_info", lambda name: None)
    monkeypatch.setattr(mod, "_github_search_repo", lambda name: "")
    assert mod.get_pypi_upstream("foo") == ""


def test_get_pypi_upstream_fetch_exception_falls_back(monkeypatch):
    # fetch 抛异常 → except 吞掉 → 走 GitHub 搜索兜底
    monkeypatch.setitem(mod.PYTHON_UPSTREAM_HELPERS, "fetch_pypi_info",
                        lambda name: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(mod, "_github_search_repo", lambda name: "https://github.com/o/r")
    assert mod.get_pypi_upstream("foo") == "https://github.com/o/r"


def test_resolve_upstream_url_empty():
    assert mod.resolve_upstream_url("", "python") == ""


@pytest.mark.parametrize("name,expected", [
    ("github.com/org/repo", "https://github.com/org/repo"),
    ("gitlab.com/o/r", "https://gitlab.com/o/r"),
    ("github.com/org/repo.git", "https://github.com/org/repo"),  # .git 后缀归一剥除
])
def test_resolve_upstream_url_go_hosted(name, expected):
    assert mod.resolve_upstream_url(name, "go") == expected


def test_resolve_upstream_url_go_untrusted_host():
    # golang.org 不在 TRUSTED_REPO_HOSTS 名单 → 归一为 invalid → 返回空
    assert mod.resolve_upstream_url("golang.org/x/mod", "go") == ""


def test_resolve_upstream_url_go_unhosted(monkeypatch):
    monkeypatch.setattr(mod, "_github_search_repo", lambda name: "https://github.com/o/mod")
    assert mod.resolve_upstream_url("codeberg.org/foo/bar", "go") == "https://github.com/o/mod"


def test_resolve_upstream_url_python(monkeypatch):
    monkeypatch.setattr(mod, "get_pypi_upstream", lambda name: "https://github.com/o/r")
    assert mod.resolve_upstream_url("foo", "python") == "https://github.com/o/r"


def test_resolve_upstream_url_rust(monkeypatch):
    def fake_urlopen(req, timeout=10):
        return _FakeUrlResponse({"crate": {
            "repository": "https://github.com/rust-lang/cargo", "homepage": None}})

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    assert mod.resolve_upstream_url("serde", "rust") == "https://github.com/rust-lang/cargo"


def test_resolve_upstream_url_rust_falls_back_to_github(monkeypatch):
    def fake_urlopen(req, timeout=10):
        return _FakeUrlResponse({"crate": {"repository": None, "homepage": None}})

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(mod, "_github_search_repo", lambda name: "https://github.com/o/r")
    assert mod.resolve_upstream_url("serde", "rust") == "https://github.com/o/r"


def test_resolve_upstream_url_rust_network_error(monkeypatch):
    monkeypatch.setattr(mod.urllib.request, "urlopen",
                        lambda req, timeout=10: (_ for _ in ()).throw(OSError("net")))
    monkeypatch.setattr(mod, "_github_search_repo", lambda name: "https://github.com/o/r")
    assert mod.resolve_upstream_url("serde", "rust") == "https://github.com/o/r"


def test_resolve_upstream_url_nodejs_network_error(monkeypatch):
    monkeypatch.setattr(mod.urllib.request, "urlopen",
                        lambda req, timeout=10: (_ for _ in ()).throw(OSError("net")))
    monkeypatch.setattr(mod, "_github_search_repo", lambda name: "https://github.com/o/r")
    assert mod.resolve_upstream_url("lodash", "nodejs") == "https://github.com/o/r"


def test_resolve_upstream_url_nodejs(monkeypatch):
    def fake_urlopen(req, timeout=10):
        return _FakeUrlResponse({"repository": {
            "url": "git+https://github.com/lodash/lodash.git"}})

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    assert mod.resolve_upstream_url("lodash", "nodejs") == "https://github.com/lodash/lodash"


def test_resolve_upstream_url_nodejs_github_shortcut(monkeypatch):
    def fake_urlopen(req, timeout=10):
        return _FakeUrlResponse({"repository": {"url": "github:lodash/lodash"}})

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    assert mod.resolve_upstream_url("lodash", "nodejs") == "https://github.com/lodash/lodash"


def test_resolve_upstream_url_nodejs_repo_string(monkeypatch):
    def fake_urlopen(req, timeout=10):
        return _FakeUrlResponse({"repository": "git://github.com/o/r.git"})

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    assert mod.resolve_upstream_url("r", "nodejs") == "https://github.com/o/r"


def test_resolve_upstream_url_nodejs_suspicious_falls_back(monkeypatch):
    def fake_urlopen(req, timeout=10):
        return _FakeUrlResponse({"repository": {"url": "https://example.com/o/r"}})

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(mod, "_github_search_repo", lambda name: "")
    assert mod.resolve_upstream_url("r", "nodejs") == ""


def test_ensure_dependency_upstream_provided():
    item = {"name": "x", "upstream_url": "https://github.com/o/r"}
    url, resolution = mod.ensure_dependency_upstream(item, "python")
    assert (url, resolution) == ("https://github.com/o/r", "provided")


def test_ensure_dependency_upstream_registry(monkeypatch):
    monkeypatch.setattr(mod, "resolve_upstream_url", lambda name, lang: "https://github.com/o/r")
    item = {"name": "x", "upstream_url": "https://pypi.org/project/x"}   # 可疑已有 URL
    assert mod.ensure_dependency_upstream(item, "python") == ("https://github.com/o/r", "registry")


def test_ensure_dependency_upstream_unresolved(monkeypatch):
    monkeypatch.setattr(mod, "resolve_upstream_url", lambda name, lang: "")
    assert mod.ensure_dependency_upstream({"name": "x"}, "python") == ("", "unresolved")
    monkeypatch.setattr(mod, "resolve_upstream_url", lambda name, lang: "https://evil.com/o/r")
    assert mod.ensure_dependency_upstream({"name": "x"}, "python") == ("", "unresolved")


# ─────────────────────────────────────────────
# 20. main() — 编排与早退路径
# ─────────────────────────────────────────────

def run_main(monkeypatch, *args):
    """以给定 argv 跑 main,返回 SystemExit code。"""
    monkeypatch.setattr(sys, "argv", ["pre_check_deps.py", *args])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    return exc.value.code


def _write_analysis(analysis_path: Path, payload: dict):
    analysis_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_main_missing_args_exits_2(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["pre_check_deps.py"])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 2


def test_main_unsupported_lang(monkeypatch, capsys, tmp_path, no_cascade_env):
    assert run_main(monkeypatch, "demo", "ruby", str(tmp_path)) == 0
    assert "不支持的语言" in capsys.readouterr().err


def test_main_analyzer_script_missing(monkeypatch, capsys, tmp_path, no_cascade_env):
    monkeypatch.setitem(mod.ANALYZERS, "python",
                        {"script": "nope_analyzer.py", "extra_args": []})
    assert run_main(monkeypatch, "demo", "python", str(tmp_path)) == 0
    assert "分析脚本不存在" in capsys.readouterr().err


@pytest.mark.parametrize("lang,expected_vendor_langs", [
    ("rust", ["rust"]),
    ("go", ["go"]),
])
def test_main_vendor_lang_early_exit(monkeypatch, capsys, tmp_path, no_cascade_env, lang,
                                     expected_vendor_langs):
    src = tmp_path / "src"
    src.mkdir()
    out = tmp_path / "out.json"
    assert run_main(monkeypatch, "demo", lang, str(src), "-o", str(out)) == 0
    assert "vendor 模式" in capsys.readouterr().err
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["vendor_mode"] is True
    assert data["vendor_langs"] == expected_vendor_langs
    assert data["secondary_langs"] == []
    assert data["resolved"] == [] and data["pending"] == [] and data["blocked"] == []


def test_main_rust_nightly_blocked(monkeypatch, capsys, tmp_path, no_cascade_env):
    src = tmp_path / "src"
    src.mkdir()
    (src / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "nightly"\n',
                                             encoding="utf-8")
    out = tmp_path / "out.json"
    assert run_main(monkeypatch, "demo", "rust", str(src), "-o", str(out)) == 1
    err = capsys.readouterr().err
    assert "[BLOCK]" in err
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data["blocked"]) == 1
    assert "nightly" in data["blocked"][0]["reason"]


def test_main_python_all_resolved(monkeypatch, capsys, tmp_path, no_cascade_env,
                                  fake_subprocess):
    monkeypatch.setattr(mod, "resolve_upstream_url", lambda name, lang: "")
    src = tmp_path / "src"
    src.mkdir()
    out = tmp_path / "out.json"
    analysis = tmp_path / "out_analysis.json"
    _write_analysis(analysis, {
        "dependency_items": [{"name": "requests", "dep": "requests>=2.0",
                              "requirement": ">=2.0"}],
        "rpm_check": {"available": [{"dep": "requests>=2.0", "name": "requests",
                                     "rpm": "python3-requests", "version": "2.31.0",
                                     "release": "1"}],
                      "missing": [], "version_conflict": []},
    })
    assert run_main(monkeypatch, "demo", "python", str(src), "-o", str(out)) == 0
    assert fake_subprocess.called_with("--check-rpm")
    assert fake_subprocess.called_with("--pkg demo")
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data["resolved"]) == 1
    assert data["resolved"][0]["name"] == "requests"
    assert data["resolved"][0]["decision"] == "reuse_source"
    assert data["pending"] == [] and data["blocked"] == [] and data["needs_ai"] == []
    assert data["secondary_langs"] == [] and data["vendor_langs"] == []
    assert data["secondary_manifests"] == {}
    assert data["c_library_build_requires"] == []
    assert "已解决 1 个依赖" in capsys.readouterr().err


def test_main_python_pending_exits_2(monkeypatch, capsys, tmp_path, no_cascade_env,
                                     fake_subprocess):
    monkeypatch.setattr(mod, "resolve_upstream_url",
                        lambda name, lang: "https://github.com/MagicStack/uvloop")
    stub = make_existing_stub(decision="introduce_new", official_exists=False)
    monkeypatch.setattr(mod, "EXISTING_CHECKER", stub)
    src = tmp_path / "src"
    src.mkdir()
    out = tmp_path / "out.json"
    _write_analysis(tmp_path / "out_analysis.json", {
        "dependency_items": [],
        "rpm_check": {"available": [], "missing": [{"name": "uvloop", "dep": "uvloop"}],
                      "version_conflict": []},
    })
    assert run_main(monkeypatch, "demo", "python", str(src), "-o", str(out)) == 2
    assert capsys.readouterr().out.strip() == "uvloop https://github.com/MagicStack/uvloop"
    data = json.loads(out.read_text(encoding="utf-8"))
    assert [i["name"] for i in data["pending"]] == ["uvloop"]
    assert [i["decision"] for i in data["pending"]] == ["introduce_new"]


def test_main_python_blocked_exits_1(monkeypatch, capsys, tmp_path, no_cascade_env,
                                     fake_subprocess):
    monkeypatch.setattr(mod, "_dep_conflict_mode", lambda: "block")
    monkeypatch.setattr(mod, "resolve_upstream_url",
                        lambda name, lang: "https://github.com/o/requests")
    stub = make_existing_stub(decision="block_official_older", reason="社区源版本不满足")
    monkeypatch.setattr(mod, "EXISTING_CHECKER", stub)
    src = tmp_path / "src"
    src.mkdir()
    out = tmp_path / "out.json"
    _write_analysis(tmp_path / "out_analysis.json", {
        "dependency_items": [{"name": "requests", "dep": "requests",
                              "requirement": ">=3.0"}],
        "rpm_check": {"available": [], "missing": [], "version_conflict": []},
    })
    assert run_main(monkeypatch, "demo", "python", str(src), "-o", str(out)) == 1
    err = capsys.readouterr().err
    assert "[BLOCK] requests" in err
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data["blocked"]) == 1
    assert data["blocked"][0]["decision"] == "block_official_older"


def test_main_passes_chroot(monkeypatch, tmp_path, no_cascade_env, fake_subprocess):
    monkeypatch.setenv("COPR_CHROOT", "openeuler-24.03-x86_64")
    monkeypatch.setattr(mod, "resolve_upstream_url", lambda name, lang: "")
    src = tmp_path / "src"
    src.mkdir()
    out = tmp_path / "out.json"
    _write_analysis(tmp_path / "out_analysis.json",
                    {"dependency_items": [], "rpm_check": {"available": [], "missing": [],
                                                           "version_conflict": []}})
    assert run_main(monkeypatch, "demo", "python", str(src), "-o", str(out)) == 0
    assert fake_subprocess.called_with("--chroot openeuler-24.03-x86_64")


def test_main_analyze_failure_exits_1(monkeypatch, capsys, tmp_path, no_cascade_env,
                                      fake_subprocess):
    fake_subprocess.when(lambda s: "--check-rpm" in s, returncode=1)
    src = tmp_path / "src"
    src.mkdir()
    assert run_main(monkeypatch, "demo", "python", str(src),
                    "-o", str(tmp_path / "out.json")) == 1
    assert "分析脚本执行失败" in capsys.readouterr().err


def test_main_analysis_unreadable_exits_1(monkeypatch, capsys, tmp_path, no_cascade_env,
                                          fake_subprocess):
    src = tmp_path / "src"
    src.mkdir()
    # 分析脚本"成功"(fake rc=0)但结果文件不存在
    assert run_main(monkeypatch, "demo", "python", str(src),
                    "-o", str(tmp_path / "out.json")) == 1
    assert "无法读取分析结果" in capsys.readouterr().err


def test_main_classification_error_exits_1(monkeypatch, capsys, tmp_path, no_cascade_env,
                                           fake_subprocess):
    monkeypatch.setattr(mod, "resolve_upstream_url", lambda name, lang: "")
    monkeypatch.setattr(mod, "EXISTING_CHECKER",
                        make_existing_stub(raise_on_check=True))
    src = tmp_path / "src"
    src.mkdir()
    _write_analysis(tmp_path / "out_analysis.json", {
        "dependency_items": [],
        "rpm_check": {"available": [], "missing": [{"name": "click", "dep": "click"}],
                      "version_conflict": []},
    })
    assert run_main(monkeypatch, "demo", "python", str(src),
                    "-o", str(tmp_path / "out.json")) == 1
    assert "依赖分类失败" in capsys.readouterr().err


def test_main_nodejs_many_deps_no_lockfile_blocked(monkeypatch, capsys, tmp_path,
                                                   no_cascade_env, fake_subprocess):
    src = tmp_path / "src"
    src.mkdir()
    out = tmp_path / "out.json"
    deps = {f"dep{i}": "1.0.0" for i in range(11)}    # > 阈值 10
    _write_analysis(tmp_path / "out_analysis.json", {"dependencies": deps})
    assert run_main(monkeypatch, "demo", "nodejs", str(src), "-o", str(out)) == 1
    assert "无 lockfile" in capsys.readouterr().err
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data["blocked"]) == 1
    assert "lockfile" in data["blocked"][0]["reason"]


def test_main_nodejs_many_deps_lockfile_vendor(monkeypatch, capsys, tmp_path,
                                               no_cascade_env, fake_subprocess):
    src = tmp_path / "src"
    src.mkdir()
    (src / "package-lock.json").write_text("{}", encoding="utf-8")
    out = tmp_path / "out.json"
    deps = {f"dep{i}": "1.0.0" for i in range(11)}
    _write_analysis(tmp_path / "out_analysis.json", {"dependencies": deps})
    assert run_main(monkeypatch, "demo", "nodejs", str(src), "-o", str(out)) == 0
    assert "切换 vendor 模式" in capsys.readouterr().err
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["vendor_mode"] is True
    assert data["vendor_langs"] == ["nodejs"]


def test_main_nodejs_few_deps_rpm_path(monkeypatch, capsys, tmp_path, no_cascade_env,
                                       fake_subprocess):
    monkeypatch.setattr(mod, "resolve_upstream_url", lambda name, lang: "")
    src = tmp_path / "src"
    src.mkdir()
    out = tmp_path / "out.json"
    _write_analysis(tmp_path / "out_analysis.json",
                    {"dependencies": {"a": "1.0", "b": "2.0", "c": "3.0"}})
    assert run_main(monkeypatch, "demo", "nodejs", str(src), "-o", str(out)) == 0
    assert "走 RPM-native 路径" in capsys.readouterr().err
    assert fake_subprocess.called_with("--check-rpm")


def test_main_mixed_python_rust_secondary(monkeypatch, capsys, tmp_path,
                                          no_cascade_env, fake_subprocess):
    monkeypatch.setattr(mod, "resolve_upstream_url", lambda name, lang: "")
    src = tmp_path / "src"
    src.mkdir()
    (src / "Cargo.toml").write_text('[package]\nname = "demo"\nversion = "1.0"\n',
                                    encoding="utf-8")
    out = tmp_path / "out.json"
    # 主语言 python 分析结果:全部可用 + 主语言侧已验证的 C 库(带重复 rpm)
    _write_analysis(tmp_path / "out_analysis.json", {
        "dependency_items": [{"name": "requests", "dep": "requests>=2.0",
                              "requirement": ">=2.0"}],
        "rpm_check": {"available": [{"dep": "requests>=2.0", "name": "requests",
                                     "rpm": "python3-requests", "version": "2.31.0",
                                     "release": "1"}],
                      "missing": [], "version_conflict": []},
        "c_library_rpm_check": {"available": [{"rpm": "libpq-devel"},
                                              {"rpm": "libpq-devel"}]},
    })
    # 副语言 rust 分析结果:crate 清单 + 已验证的 C 库
    _write_analysis(tmp_path / "out_analysis_rust.json", {
        "rpm_check": {"available": [{"rpm": "libssl-devel"}]},
        "crate_deps": [{"name": "tokio"}, {"name": "serde"}],
    })
    assert run_main(monkeypatch, "demo", "python", str(src), "-o", str(out)) == 0
    err = capsys.readouterr().err
    assert "检测到副语言 ['rust']" in err
    assert "副语言 rust 分析" in err
    assert fake_subprocess.called_with("analyze_rust_deps.py")
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["secondary_langs"] == ["rust"]
    assert data["secondary_manifests"] == {"rust": "Cargo.toml"}
    assert data["vendor_langs"] == ["rust"]
    assert data["vendor_crates"] == {"rust": ["serde", "tokio"]}
    # 主语言 c_lib 先去重,再并入副语言 c_lib
    assert data["c_library_build_requires"] == ["libpq-devel", "libssl-devel"]
    assert data["resolved"][0]["name"] == "requests"


def _write_empty_python_analysis(tmp_path):
    _write_analysis(tmp_path / "out_analysis.json", {
        "dependency_items": [],
        "rpm_check": {"available": [], "missing": [], "version_conflict": []},
    })


def test_main_nodejs_static_unreadable_exits_1(monkeypatch, capsys, tmp_path,
                                               no_cascade_env, fake_subprocess):
    # nodejs 静态分析结果文件不存在 → static_result {} → deps_count 0 →
    # 走 check-rpm 路径,结果仍不存在 → 无法读取分析结果
    src = tmp_path / "src"
    src.mkdir()
    assert run_main(monkeypatch, "demo", "nodejs", str(src),
                    "-o", str(tmp_path / "out.json")) == 1
    assert "无法读取分析结果" in capsys.readouterr().err


def test_main_secondary_unknown_lang_skipped(monkeypatch, tmp_path, no_cascade_env,
                                             fake_subprocess):
    # 副语言不在 ANALYZERS 表 → 跳过副语言分析(不报错)
    monkeypatch.setattr(mod, "resolve_upstream_url", lambda name, lang: "")
    monkeypatch.setitem(mod.SECONDARY_PROBE, "baz.manifest", "bazlang")
    src = tmp_path / "src"
    src.mkdir()
    (src / "baz.manifest").write_text("x", encoding="utf-8")
    out = tmp_path / "out.json"
    _write_empty_python_analysis(tmp_path)
    assert run_main(monkeypatch, "demo", "python", str(src), "-o", str(out)) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["secondary_langs"] == ["bazlang"]
    assert "vendor_crates" not in data   # 无 crate 清单 → 字段不写入


def test_main_secondary_script_missing_skipped(monkeypatch, tmp_path, no_cascade_env,
                                               fake_subprocess):
    monkeypatch.setattr(mod, "resolve_upstream_url", lambda name, lang: "")
    monkeypatch.setitem(mod.ANALYZERS, "rust",
                        {"script": "nope_analyzer.py", "extra_args": []})
    src = tmp_path / "src"
    src.mkdir()
    (src / "Cargo.toml").write_text('[package]\nname = "demo"\n', encoding="utf-8")
    out = tmp_path / "out.json"
    _write_empty_python_analysis(tmp_path)
    assert run_main(monkeypatch, "demo", "python", str(src), "-o", str(out)) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["secondary_langs"] == ["rust"]


def test_main_secondary_manifest_missing_skipped(monkeypatch, tmp_path, no_cascade_env,
                                                 fake_subprocess):
    # 副语言存在但 manifest 路径缺失(探测结果被替换)→ 跳过
    monkeypatch.setattr(mod, "resolve_upstream_url", lambda name, lang: "")
    monkeypatch.setattr(mod, "detect_secondary_langs",
                        lambda lang, source_dir: (["rust"], {}))
    src = tmp_path / "src"
    src.mkdir()
    out = tmp_path / "out.json"
    _write_empty_python_analysis(tmp_path)
    assert run_main(monkeypatch, "demo", "python", str(src), "-o", str(out)) == 0


def test_main_secondary_analyzer_fails_warns(monkeypatch, capsys, tmp_path,
                                             no_cascade_env, fake_subprocess):
    # 副语言 analyzer 退出码非 0/2 → WARN 忽略,由构建失败循环兜底
    monkeypatch.setattr(mod, "resolve_upstream_url", lambda name, lang: "")
    fake_subprocess.when(lambda s: "analyze_rust_deps.py" in s, returncode=1)
    src = tmp_path / "src"
    src.mkdir()
    (src / "Cargo.toml").write_text('[package]\nname = "demo"\n', encoding="utf-8")
    out = tmp_path / "out.json"
    _write_empty_python_analysis(tmp_path)
    assert run_main(monkeypatch, "demo", "python", str(src), "-o", str(out)) == 0
    assert "副语言 rust 分析失败" in capsys.readouterr().err


def test_main_secondary_analysis_unreadable_warns(monkeypatch, capsys, tmp_path,
                                                  no_cascade_env, fake_subprocess):
    # 副语言 analyzer "成功"(fake rc=0)但结果文件不存在 → WARN 忽略
    monkeypatch.setattr(mod, "resolve_upstream_url", lambda name, lang: "")
    src = tmp_path / "src"
    src.mkdir()
    (src / "Cargo.toml").write_text('[package]\nname = "demo"\n', encoding="utf-8")
    out = tmp_path / "out.json"
    _write_empty_python_analysis(tmp_path)
    assert run_main(monkeypatch, "demo", "python", str(src), "-o", str(out)) == 0
    assert "无法读取副语言 rust 分析结果" in capsys.readouterr().err
