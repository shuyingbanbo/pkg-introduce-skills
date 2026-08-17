"""run_check.py — Phase 1 基础检查测试。

覆盖：
- _default_report / _save / _already_done / _compute_overall（优先级）
- 5 个 step runner：init（dependency 跳过）、repo_check、download、
  license_check（skipped / needs_ai 证据收集）、detect
  （LLM 已解 / needs_agent / needs_ai / java-gradle 中止）
- run_check 编排：happy path、FlowError 各阶段、needs_ai 出口码 2、
  幂等续跑、constraint 透传、license 非阻断 failed
- main（--pkg-dir 重定向、argparse 缺参）
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sys.path.insert(0, str(SCRIPT_DIRS["pkg_introduce"]))
flow = load_module("run_pkg_introduce_flow",
                   SCRIPT_DIRS["pkg_introduce"] / "run_pkg_introduce_flow.py")
check = load_module("run_check", SCRIPT_DIRS["pkg_introduce"] / "run_check.py")


def make_args(**overrides):
    defaults = dict(
        pkg="testpkg", upstream_url="https://github.com/x/testpkg",
        version="", constraint="", mode="top-level", pkg_dir=None,
        reports_dir="./reports", sources_dir="./sources",
        build_state_dir="./build_state",
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _report_path(reports_dir, pkg="testpkg"):
    return reports_dir / f"check_result_{pkg}.json"


def _load_report(reports_dir, pkg="testpkg"):
    return json.loads(_report_path(reports_dir, pkg).read_text(encoding="utf-8"))


def _cp(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(["cmd"], returncode, stdout, stderr)


# ─────────────────────────────────────────────
# _default_report / _save / _already_done / _compute_overall
# ─────────────────────────────────────────────

def test_default_report():
    r = check._default_report("foo", "https://up")
    assert r["pkgname"] == "foo"
    assert r["upstream_url"] == "https://up"
    assert r["overall_status"] == "pending"
    assert r["config_summary"] == {
        "license_check_enabled": True,
        "allow_unstable": False,
        "repo_check_blocking": True,
        "dep_conflict_mode": "block",
    }
    assert list(r["steps"]) == check.CHECK_STEPS
    assert all(s == {"status": "pending"} for s in r["steps"].values())
    assert r["result"] is None


def test_save_creates_parents(tmp_path):
    target = tmp_path / "a" / "b" / "r.json"
    check._save({"k": 1}, target)
    assert json.loads(target.read_text(encoding="utf-8")) == {"k": 1}


@pytest.mark.parametrize("step,expected", [
    ({"status": "done"}, True),
    ({"status": "skipped"}, True),
    ({"status": "pending"}, False),
    ({"status": "failed"}, False),
    ({"status": "needs_ai"}, False),
    ({}, False),
])
def test_already_done(step, expected):
    assert check._already_done(step) is expected


@pytest.mark.parametrize("steps,expected", [
    ({"a": {"status": "done"}, "b": {"status": "done"}}, "done"),
    ({"a": {"status": "done"}, "b": {"status": "skipped"}}, "done"),
    ({"a": {"status": "done"}, "b": {"status": "pending"}}, "pending"),
    ({"a": {"status": "done"}, "b": {"status": "needs_ai"}}, "needs_ai"),
    ({"a": {"status": "failed"}, "b": {"status": "needs_ai"}}, "failed"),  # failed 优先
    ({"a": {"status": "done"}, "b": {"status": "failed"}}, "failed"),
    ({"a": {}}, "pending"),  # 缺 status 视为 pending
    ({}, "done"),  # 空 dict 真空洞满足 all()
])
def test_compute_overall(steps, expected):
    assert check._compute_overall(steps) == expected


# ─────────────────────────────────────────────
# _run_init
# ─────────────────────────────────────────────

def _report():
    return {"steps": {step: {"status": "pending"} for step in check.CHECK_STEPS}}


def test_run_init_dependency_mode_skips(tmp_path, monkeypatch):
    report = _report()
    called = {"n": 0}
    monkeypatch.setattr(check, "initialize_top_level",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    check._run_init(report, "dependency", tmp_path / "bs", tmp_path / "r",
                    tmp_path / "s")
    assert report["steps"]["init"] == \
        {"status": "skipped", "reason": "dependency mode: reuse top-level state"}
    assert called["n"] == 0


def test_run_init_top_level_done(tmp_path, monkeypatch):
    report = _report()
    recorded = {}
    monkeypatch.setattr(check, "initialize_top_level",
                        lambda bs, r, s: recorded.update(bs=bs, r=r, s=s))
    check._run_init(report, "top-level", tmp_path / "bs", tmp_path / "r",
                    tmp_path / "s")
    assert report["steps"]["init"] == {"status": "done"}
    assert recorded["bs"] == tmp_path / "bs"


def test_run_init_flow_error(tmp_path, monkeypatch):
    report = _report()
    monkeypatch.setattr(check, "initialize_top_level",
                        lambda *a, **k: (_ for _ in ()).throw(
                            flow.FlowError("init boom", "t")))
    with pytest.raises(flow.FlowError):
        check._run_init(report, "top-level", tmp_path / "bs", tmp_path / "r",
                        tmp_path / "s")
    assert report["steps"]["init"] == {"status": "failed", "reason": "init boom"}


# ─────────────────────────────────────────────
# _run_repo_check
# ─────────────────────────────────────────────

def test_run_repo_check_done(tmp_path, monkeypatch):
    report = _report()
    monkeypatch.setattr(check, "run_repo_check",
                        lambda pkg, url, rd: {
                            "status": "done", "platform": "github",
                            "days_inactive": 3, "warning": "w",
                        })
    check._run_repo_check(report, "p", "https://u", tmp_path)
    assert report["steps"]["repo_check"] == {
        "status": "done", "platform": "github", "days_inactive": 3, "warning": "w"}


def test_run_repo_check_flow_error(tmp_path, monkeypatch):
    report = _report()
    monkeypatch.setattr(check, "run_repo_check",
                        lambda *a: (_ for _ in ()).throw(
                            flow.FlowError("blocked", "non_retryable_repo_blocked")))
    with pytest.raises(flow.FlowError):
        check._run_repo_check(report, "p", "u", tmp_path)
    assert report["steps"]["repo_check"] == {"status": "failed", "reason": "blocked"}


# ─────────────────────────────────────────────
# _run_download
# ─────────────────────────────────────────────

def test_run_download_done(tmp_path, monkeypatch):
    report = _report()
    recorded = {}
    monkeypatch.setattr(check, "run_download",
                        lambda pkg, url, version, sd, rd, constraint="":
                        recorded.update(version=version, constraint=constraint) or
                        {"status": "done", "version": "1.2.3", "source_dir": "/src"})
    check._run_download(report, "p", "u", "1.2.3", tmp_path / "s", tmp_path / "r",
                        constraint=">= 1.0")
    assert report["steps"]["download"] == {
        "status": "done", "version": "1.2.3", "source_dir": "/src"}
    assert recorded == {"version": "1.2.3", "constraint": ">= 1.0"}


def test_run_download_flow_error(tmp_path, monkeypatch):
    report = _report()
    monkeypatch.setattr(check, "run_download",
                        lambda *a, **k: (_ for _ in ()).throw(flow.FlowError("dl failed")))
    with pytest.raises(flow.FlowError):
        check._run_download(report, "p", "u", "", tmp_path / "s", tmp_path / "r")
    assert report["steps"]["download"] == {"status": "failed", "reason": "dl failed"}


# ─────────────────────────────────────────────
# _run_license_check
# ─────────────────────────────────────────────

def test_run_license_check_done(tmp_path, monkeypatch):
    report = _report()
    monkeypatch.setattr(check, "run_license_check",
                        lambda *a: {"status": "done", "license_check": "/x"})
    check._run_license_check(report, "p", tmp_path, tmp_path)
    assert report["steps"]["license_check"] == {"status": "done"}


def test_run_license_check_skipped(tmp_path, monkeypatch):
    report = _report()
    monkeypatch.setattr(check, "run_license_check", lambda *a: {"status": "skipped"})
    check._run_license_check(report, "p", tmp_path, tmp_path)
    assert report["steps"]["license_check"] == \
        {"status": "skipped", "reason": "license_check.enabled=false"}


def test_run_license_check_needs_ai_with_evidence(tmp_path, monkeypatch):
    report = _report()
    lic_json = tmp_path / "lic.json"
    lic_json.write_text(json.dumps({"license_ids": ["MIT"], "source": "LICENSE",
                                    "message": "需要 AI 兜底判断"}))
    (tmp_path / "LICENSE").write_text("MIT License text " * 100)  # 500+ 字符
    monkeypatch.setattr(check, "run_license_check",
                        lambda *a: {"status": "needs_ai", "license_check": str(lic_json),
                                    "category": "unknown", "reason": "r"})
    check._run_license_check(report, "p", tmp_path, tmp_path)
    step = report["steps"]["license_check"]
    assert step["status"] == "needs_ai"
    assert step["reason"] == "r"
    assert step["category"] == "unknown"
    assert step["ai_inputs"]["raw_license_ids"] == ["MIT"]
    assert step["ai_inputs"]["source"] == "LICENSE"
    assert step["ai_inputs"]["message"] == "需要 AI 兜底判断"
    snippet = step["ai_inputs"]["license_file_snippet"]
    assert snippet.startswith("MIT License text") and len(snippet) == 500
    assert "判断该许可证" in step["ai_instructions"]


def test_run_license_check_needs_ai_no_files(tmp_path, monkeypatch):
    # license_check 指向不存在的文件 + 无 LICENSE 文件 → ai_inputs 无证据
    report = _report()
    monkeypatch.setattr(check, "run_license_check",
                        lambda *a: {"status": "needs_ai",
                                    "license_check": str(tmp_path / "nope.json"),
                                    "category": "unknown", "reason": "r"})
    check._run_license_check(report, "p", tmp_path, tmp_path)
    step = report["steps"]["license_check"]
    assert step["status"] == "needs_ai"
    assert step["ai_inputs"] == {}
    assert "ai_instructions" in step


def test_run_license_check_needs_ai_empty_path_crashes(tmp_path, monkeypatch):
    # BUG 注(生产代码现状):license_check 为空串时 Path("") 解析为 "."，
    # exists() 为 True → read_text 抛 IsADirectoryError,未捕获。
    report = _report()
    monkeypatch.setattr(check, "run_license_check",
                        lambda *a: {"status": "needs_ai", "license_check": "",
                                    "category": "unknown", "reason": "r"})
    with pytest.raises(IsADirectoryError):
        check._run_license_check(report, "p", tmp_path, tmp_path)


def test_run_license_check_flow_error(tmp_path, monkeypatch):
    report = _report()
    monkeypatch.setattr(check, "run_license_check",
                        lambda *a: (_ for _ in ()).throw(flow.FlowError("lic blocked")))
    with pytest.raises(flow.FlowError):
        check._run_license_check(report, "p", tmp_path, tmp_path)
    assert report["steps"]["license_check"] == {"status": "failed", "reason": "lic blocked"}


def test_run_license_check_license_file_unreadable(tmp_path, monkeypatch):
    # LICENSE 是目录 → read_text 抛 OSError → 跳过 snippet(生产代码现状)
    report = _report()
    (tmp_path / "LICENSE").mkdir()
    monkeypatch.setattr(check, "run_license_check",
                        lambda *a: {"status": "needs_ai",
                                    "license_check": str(tmp_path / "nope.json"),
                                    "category": "unknown", "reason": "r"})
    check._run_license_check(report, "p", tmp_path, tmp_path)
    assert "license_file_snippet" not in report["steps"]["license_check"]["ai_inputs"]


# ─────────────────────────────────────────────
# _run_detect
# ─────────────────────────────────────────────

def test_run_detect_llm_resolved_reuses(tmp_path, monkeypatch):
    report = {"steps": {"detect": {"status": "done", "ai_resolved": True,
                                   "lang": "ruby", "version": "2.0"}}}
    monkeypatch.setattr(check, "detect_lang_and_version",
                        lambda *a: (_ for _ in ()).throw(AssertionError("not called")))
    assert check._run_detect(report, "p", tmp_path, "") == ("ruby", "2.0")


def test_run_detect_needs_agent_with_git_tags(tmp_path, monkeypatch):
    report = _report()
    monkeypatch.setattr(check, "detect_lang_and_version",
                        lambda sd, ev: {"status": "needs_agent", "lang": "go",
                                        "version": "", "reason": "empty",
                                        "expected_version": "1.0"})
    monkeypatch.setattr(check, "run_command",
                        lambda cmd: _cp(stdout="v1.0.0\nv0.9.0\n"))
    lang, version = check._run_detect(report, "p", tmp_path, "1.0")
    assert (lang, version) == ("go", "")
    step = report["steps"]["detect"]
    assert step["status"] == "needs_ai"
    assert step["lang"] == "go"
    assert step["version"] == ""  # 生产代码现状:needs_ai 时 version 恒为空
    assert step["expected_version"] == "1.0"
    assert step["ai_inputs"]["git_tags"] == ["v1.0.0", "v0.9.0"]
    assert "期望版本: 1.0" in step["ai_instructions"]


def test_run_detect_needs_agent_git_fails_manifest_evidence(tmp_path, monkeypatch):
    report = _report()
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "x"\nversion = "0.1.0"\n')
    monkeypatch.setattr(check, "detect_lang_and_version",
                        lambda sd, ev: {"status": "needs_agent", "lang": "rust",
                                        "version": "", "reason": "empty"})
    monkeypatch.setattr(check, "run_command", lambda cmd: _cp(returncode=1))
    check._run_detect(report, "p", tmp_path, "")
    step = report["steps"]["detect"]
    assert "git_tags" not in step["ai_inputs"]
    assert "Cargo.toml" in step["ai_inputs"]
    assert step["ai_inputs"]["Cargo.toml"].startswith("[package]")


def test_run_detect_needs_ai_status(tmp_path, monkeypatch):
    report = _report()
    monkeypatch.setattr(check, "detect_lang_and_version",
                        lambda sd, ev: {"status": "needs_ai", "lang": "python",
                                        "version": "0.9", "expected_version": "1.0",
                                        "reason": "mismatch"})
    monkeypatch.setattr(check, "run_command", lambda cmd: _cp(returncode=1))
    lang, version = check._run_detect(report, "p", tmp_path, "1.0")
    assert (lang, version) == ("python", "")
    assert report["steps"]["detect"]["reason"] == "mismatch"


def test_run_detect_manifest_unreadable_skipped(tmp_path, monkeypatch):
    # manifest 是目录(不可读)→ OSError 被吞,证据中不收录
    report = _report()
    (tmp_path / "Cargo.toml").mkdir()
    monkeypatch.setattr(check, "detect_lang_and_version",
                        lambda sd, ev: {"status": "needs_agent", "lang": "rust",
                                        "version": "", "reason": "empty"})
    monkeypatch.setattr(check, "run_command", lambda cmd: _cp(returncode=1))
    check._run_detect(report, "p", tmp_path, "")
    assert "Cargo.toml" not in report["steps"]["detect"]["ai_inputs"]


def test_run_detect_done(tmp_path, monkeypatch):
    report = _report()
    monkeypatch.setattr(check, "detect_lang_and_version",
                        lambda sd, ev: {"status": "done", "lang": "python",
                                        "version": "1.2.3"})
    assert check._run_detect(report, "p", tmp_path, "1.2.3") == ("python", "1.2.3")
    assert report["steps"]["detect"] == \
        {"status": "done", "lang": "python", "version": "1.2.3"}


def test_run_detect_java_maven_build_system(tmp_path, monkeypatch):
    report = _report()
    monkeypatch.setattr(check, "detect_lang_and_version",
                        lambda sd, ev: {"status": "done", "lang": "java",
                                        "version": "1.0"})
    monkeypatch.setattr(check, "detect_java_build_system", lambda sd: "maven")
    assert check._run_detect(report, "p", tmp_path, "1.0") == ("java", "1.0")
    assert report["steps"]["detect"]["build_system"] == "maven"


def test_run_detect_java_gradle_aborts(tmp_path, monkeypatch):
    report = _report()
    monkeypatch.setattr(check, "detect_lang_and_version",
                        lambda sd, ev: {"status": "done", "lang": "java",
                                        "version": "1.0"})
    monkeypatch.setattr(check, "detect_java_build_system", lambda sd: "gradle")
    with pytest.raises(flow.FlowError) as ei:
        check._run_detect(report, "p", tmp_path, "1.0")
    assert ei.value.failure_type == "non_retryable_gradle_build_system"
    step = report["steps"]["detect"]
    assert step["status"] == "failed"
    assert step["build_system"] == "gradle"
    assert "Gradle build system is not supported" in step["reason"]


def test_run_detect_failed_status(tmp_path, monkeypatch):
    report = _report()
    monkeypatch.setattr(check, "detect_lang_and_version",
                        lambda sd, ev: {"status": "failed", "reason": "no files"})
    with pytest.raises(flow.FlowError) as ei:
        check._run_detect(report, "p", tmp_path, "")
    assert ei.value.reason == "no files"
    assert report["steps"]["detect"] == {"status": "failed", "reason": "no files"}


# ─────────────────────────────────────────────
# run_check 编排
# ─────────────────────────────────────────────

def _patch_steps(monkeypatch, **step_results):
    """monkeypatch 5 个被 import 的编排函数;缺省返回 done。"""
    defaults = {
        "initialize_top_level": None,  # 返回 None 即可
        "run_repo_check": {"status": "done"},
        "run_download": {"status": "done", "version": "1.2.3", "source_dir": "/src"},
        "run_license_check": {"status": "done"},
        "detect_lang_and_version": {"status": "done", "lang": "python", "version": "1.2.3"},
    }
    defaults.update(step_results)
    monkeypatch.setattr(check, "initialize_top_level",
                        defaults["initialize_top_level"] or (lambda *a: None))
    monkeypatch.setattr(check, "run_repo_check",
                        defaults["run_repo_check"] if callable(defaults["run_repo_check"])
                        else (lambda *a: defaults["run_repo_check"]))
    monkeypatch.setattr(check, "run_download",
                        defaults["run_download"] if callable(defaults["run_download"])
                        else (lambda *a, **k: defaults["run_download"]))
    monkeypatch.setattr(check, "run_license_check",
                        defaults["run_license_check"] if callable(defaults["run_license_check"])
                        else (lambda *a: defaults["run_license_check"]))
    monkeypatch.setattr(check, "detect_lang_and_version",
                        defaults["detect_lang_and_version"]
                        if callable(defaults["detect_lang_and_version"])
                        else (lambda *a: defaults["detect_lang_and_version"]))
    monkeypatch.setattr(check, "detect_java_build_system", lambda sd: "maven")


def test_run_check_happy_path(tmp_path, monkeypatch, capsys):
    _patch_steps(monkeypatch)
    reports = tmp_path / "reports"
    args = make_args(reports_dir=str(reports), sources_dir=str(tmp_path / "sources"),
                     build_state_dir=str(tmp_path / "bs"))
    assert check.run_check(args) == 0
    rep = _load_report(reports)
    assert rep["overall_status"] == "done"
    assert [rep["steps"][s]["status"] for s in check.CHECK_STEPS] == \
        ["done"] * 5
    assert rep["result"] == {"lang": "python", "version": "1.2.3",
                             "source_dir": "/src"}
    assert '"status": "done"' in capsys.readouterr().out


def test_run_check_download_gets_version_and_detect_expected(tmp_path, monkeypatch):
    # args.version 优先传给 run_download,detect 的 expected 用 args.version 或下载版本
    recorded = {}
    monkeypatch.setattr(check, "initialize_top_level", lambda *a: None)
    monkeypatch.setattr(check, "run_repo_check", lambda *a: {"status": "done"})
    monkeypatch.setattr(check, "run_license_check", lambda *a: {"status": "done"})

    def fake_download(pkg, url, version, sd, rd, constraint=""):
        recorded["download_version"] = version
        return {"status": "done", "version": "9.9.9", "source_dir": "/s"}

    def fake_detect(sd, expected):
        recorded["detect_expected"] = expected
        return {"status": "done", "lang": "python", "version": "9.9.9"}

    monkeypatch.setattr(check, "run_download", fake_download)
    monkeypatch.setattr(check, "detect_lang_and_version", fake_detect)
    args = make_args(reports_dir=str(tmp_path / "reports"),
                     sources_dir=str(tmp_path / "sources"),
                     build_state_dir=str(tmp_path / "bs"))
    assert check.run_check(args) == 0
    assert recorded == {"download_version": "", "detect_expected": "9.9.9"}

    # 第二次跑需换新 reports 目录,否则幂等逻辑会跳过全部步骤
    recorded.clear()
    args = make_args(reports_dir=str(tmp_path / "reports2"),
                     sources_dir=str(tmp_path / "sources"),
                     build_state_dir=str(tmp_path / "bs"),
                     version="2.0.0")
    assert check.run_check(args) == 0
    assert recorded == {"download_version": "2.0.0", "detect_expected": "2.0.0"}


def test_run_check_dependency_mode_passes_constraint(tmp_path, monkeypatch):
    recorded = {}
    monkeypatch.setattr(check, "initialize_top_level", lambda *a: None)
    monkeypatch.setattr(check, "run_repo_check", lambda *a: {"status": "done"})
    monkeypatch.setattr(check, "run_license_check", lambda *a: {"status": "done"})
    monkeypatch.setattr(check, "detect_lang_and_version",
                        lambda sd, ev: {"status": "done", "lang": "c", "version": "1"})

    def fake_download(pkg, url, version, sd, rd, constraint=""):
        recorded["constraint"] = constraint
        return {"status": "done", "version": "1", "source_dir": "/s"}

    monkeypatch.setattr(check, "run_download", fake_download)
    args = make_args(reports_dir=str(tmp_path / "reports"),
                     sources_dir=str(tmp_path / "sources"),
                     build_state_dir=str(tmp_path / "bs"),
                     mode="dependency", constraint=">= 1.4.0")
    assert check.run_check(args) == 0
    assert recorded["constraint"] == ">= 1.4.0"
    rep = _load_report(tmp_path / "reports")
    assert rep["steps"]["init"]["status"] == "skipped"


def test_run_check_top_level_constraint_empty(tmp_path, monkeypatch):
    recorded = {}
    monkeypatch.setattr(check, "initialize_top_level", lambda *a: None)
    monkeypatch.setattr(check, "run_repo_check", lambda *a: {"status": "done"})
    monkeypatch.setattr(check, "run_license_check", lambda *a: {"status": "done"})
    monkeypatch.setattr(check, "detect_lang_and_version",
                        lambda sd, ev: {"status": "done", "lang": "c", "version": "1"})
    monkeypatch.setattr(check, "run_download",
                        lambda pkg, url, version, sd, rd, constraint="":
                        recorded.update(constraint=constraint) or
                        {"status": "done", "version": "1", "source_dir": "/s"})
    args = make_args(reports_dir=str(tmp_path / "reports"),
                     sources_dir=str(tmp_path / "sources"),
                     build_state_dir=str(tmp_path / "bs"),
                     constraint=">= 1.4.0")  # top-level 模式忽略 constraint
    assert check.run_check(args) == 0
    assert recorded["constraint"] == ""


def test_run_check_repo_check_flow_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(check, "initialize_top_level", lambda *a: None)

    def boom(*a):
        raise flow.FlowError("repo blocked")

    monkeypatch.setattr(check, "run_repo_check", boom)
    args = make_args(reports_dir=str(tmp_path / "reports"),
                     sources_dir=str(tmp_path / "sources"),
                     build_state_dir=str(tmp_path / "bs"))
    assert check.run_check(args) == 1
    rep = _load_report(tmp_path / "reports")
    assert rep["overall_status"] == "failed"
    assert rep["steps"]["repo_check"]["status"] == "failed"
    assert rep["steps"]["download"]["status"] == "pending"  # 后续步骤未执行
    assert '"status": "failed"' in capsys.readouterr().out


def test_run_check_init_flow_error(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "initialize_top_level",
                        lambda *a: (_ for _ in ()).throw(flow.FlowError("init boom")))
    args = make_args(reports_dir=str(tmp_path / "reports"),
                     sources_dir=str(tmp_path / "sources"),
                     build_state_dir=str(tmp_path / "bs"))
    assert check.run_check(args) == 1
    rep = _load_report(tmp_path / "reports")
    assert rep["steps"]["init"]["status"] == "failed"
    assert rep["steps"]["repo_check"]["status"] == "pending"


def test_run_check_license_needs_ai_exit_2(tmp_path, monkeypatch):
    # license_check 指向不存在的文件 → 证据为空(Path("") 会解析为 "." 导致读目录)
    _patch_steps(monkeypatch,
                 run_license_check={"status": "needs_ai",
                                    "license_check": str(tmp_path / "nope.json"),
                                    "category": "unknown", "reason": "r"})
    args = make_args(reports_dir=str(tmp_path / "reports"),
                     sources_dir=str(tmp_path / "sources"),
                     build_state_dir=str(tmp_path / "bs"))
    assert check.run_check(args) == 2
    rep = _load_report(tmp_path / "reports")
    assert rep["overall_status"] == "needs_ai"
    assert rep["steps"]["license_check"]["status"] == "needs_ai"


def test_run_check_detect_needs_ai_exit_2(tmp_path, monkeypatch):
    _patch_steps(monkeypatch,
                 detect_lang_and_version={"status": "needs_ai", "lang": "python",
                                          "version": "0.9", "expected_version": "1.2.3",
                                          "reason": "mismatch"})
    monkeypatch.setattr(check, "run_command", lambda cmd: _cp(returncode=1))
    args = make_args(reports_dir=str(tmp_path / "reports"),
                     sources_dir=str(tmp_path / "sources"),
                     build_state_dir=str(tmp_path / "bs"))
    assert check.run_check(args) == 2
    rep = _load_report(tmp_path / "reports")
    assert rep["overall_status"] == "needs_ai"
    assert rep["steps"]["detect"]["status"] == "needs_ai"


def test_run_check_license_failed_status_exit_1(tmp_path, monkeypatch):
    # license_check 返回 status=failed 不抛 FlowError → 末尾 overall=failed 返回 1
    _patch_steps(monkeypatch,
                 run_license_check={"status": "failed"})
    args = make_args(reports_dir=str(tmp_path / "reports"),
                     sources_dir=str(tmp_path / "sources"),
                     build_state_dir=str(tmp_path / "bs"))
    assert check.run_check(args) == 1
    rep = _load_report(tmp_path / "reports")
    assert rep["overall_status"] == "failed"
    # 非异常路径:result 块仍会写入
    assert rep["result"]["lang"] == "python"


def test_run_check_gradle_abort_flow(tmp_path, monkeypatch):
    _patch_steps(monkeypatch,
                 detect_lang_and_version={"status": "done", "lang": "java",
                                          "version": "1.0"})
    monkeypatch.setattr(check, "detect_java_build_system", lambda sd: "gradle")
    args = make_args(reports_dir=str(tmp_path / "reports"),
                     sources_dir=str(tmp_path / "sources"),
                     build_state_dir=str(tmp_path / "bs"))
    assert check.run_check(args) == 1
    rep = _load_report(tmp_path / "reports")
    assert rep["steps"]["detect"]["status"] == "failed"
    assert rep["steps"]["detect"]["build_system"] == "gradle"


def test_run_check_idempotent_resume(tmp_path, monkeypatch):
    # 已有完整报告时全部步骤跳过,不调用任何编排函数
    reports = tmp_path / "reports"
    reports.mkdir()
    _report_path(reports).write_text(json.dumps({
        "pkgname": "testpkg", "upstream_url": "u",
        "steps": {
            "init": {"status": "done"},
            "repo_check": {"status": "done", "platform": "github"},
            "download": {"status": "done", "version": "1.0", "source_dir": "/s"},
            "license_check": {"status": "skipped", "reason": "x"},
            "detect": {"status": "done", "lang": "rust", "version": "1.0",
                       "ai_resolved": True},
        },
    }))
    called = {"n": 0}

    def fail(*a, **k):
        called["n"] += 1

    monkeypatch.setattr(check, "initialize_top_level", fail)
    monkeypatch.setattr(check, "run_repo_check", fail)
    monkeypatch.setattr(check, "run_download", fail)
    monkeypatch.setattr(check, "run_license_check", fail)
    monkeypatch.setattr(check, "detect_lang_and_version", fail)
    args = make_args(reports_dir=str(reports), sources_dir=str(tmp_path / "sources"),
                     build_state_dir=str(tmp_path / "bs"))
    assert check.run_check(args) == 0
    assert called["n"] == 0
    rep = _load_report(reports)
    assert rep["result"] == {"lang": "rust", "version": "1.0", "source_dir": "/s"}


# ─────────────────────────────────────────────
# main
# ─────────────────────────────────────────────

def test_main_missing_url_exits(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run_check.py", "--pkg", "foo"])
    with pytest.raises(SystemExit) as ei:
        check.main()
    assert ei.value.code == 2


def test_main_happy(tmp_path, monkeypatch):
    _patch_steps(monkeypatch)
    reports = tmp_path / "reports"
    monkeypatch.setattr(sys, "argv", ["run_check.py", "--pkg", "testpkg",
                                      "--url", "https://u",
                                      "--reports-dir", str(reports),
                                      "--sources-dir", str(tmp_path / "sources"),
                                      "--build-state-dir", str(tmp_path / "bs")])
    assert check.main() == 0
    assert _report_path(reports).exists()


def test_main_pkg_dir_overrides_reports_dir(tmp_path, monkeypatch):
    _patch_steps(monkeypatch)
    pkg_dir = tmp_path / "pkgdir"
    monkeypatch.setattr(sys, "argv", ["run_check.py", "--pkg", "testpkg",
                                      "--url", "https://u",
                                      "--pkg-dir", str(pkg_dir),
                                      "--sources-dir", str(tmp_path / "sources"),
                                      "--build-state-dir", str(tmp_path / "bs")])
    assert check.main() == 0
    assert _report_path(pkg_dir).exists()
    assert not _report_path(tmp_path / "pkgdir" / "reports").exists()


def test_main_repo_check_failure_returns_1(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "initialize_top_level", lambda *a: None)
    monkeypatch.setattr(check, "run_repo_check",
                        lambda *a: (_ for _ in ()).throw(flow.FlowError("boom")))
    monkeypatch.setattr(sys, "argv", ["run_check.py", "--pkg", "testpkg",
                                      "--url", "https://u",
                                      "--reports-dir", str(tmp_path / "reports"),
                                      "--sources-dir", str(tmp_path / "sources"),
                                      "--build-state-dir", str(tmp_path / "bs")])
    assert check.main() == 1
