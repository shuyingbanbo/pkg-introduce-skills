"""run_pkg_introduce_flow.py — 分阶段编排助手测试。

覆盖：
- _load_config / run_command / _is_transient_network_error / FLOW_STEPS
- steps 跟踪（load_steps / mark_step / write_json / steps_path / result_path）
- normalize_version_text / split_version_input
- run_result_command / update_result（flag 映射 / archived / 失败 FlowError）
- initialize_top_level / run_repo_check / run_download
  （github tree/commit 拆分、ref 回退重试、网络瞬时错误重试、failure_type 分类）
- run_license_check（config 禁用 / needs_ai / 失败）
- detect_lang 优先级 / detect_lang_and_version（needs_agent / needs_ai / 失败）
- run_existing_check（dep_conflict compat 决策改写）
- finalize_result / print_payload / main（各 subcommand + 失败出口 1）
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

flow = load_module("run_pkg_introduce_flow",
                   SCRIPT_DIRS["pkg_introduce"] / "run_pkg_introduce_flow.py")

P = SCRIPT_DIRS["pkg_introduce"]


def _cp(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(["cmd"], returncode, stdout, stderr)


def _argv(args: list[str], monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run_pkg_introduce_flow.py", *args])


# ─────────────────────────────────────────────
# _load_config / run_command / 瞬时错误识别
# ─────────────────────────────────────────────

def test_load_config_default_empty():
    # 仓库内只有 config.json.example,无 config.json → 返回 {}
    assert flow._load_config() == {}


def test_load_config_invalid_json_returns_empty(monkeypatch):
    def boom(*a, **k):
        raise ValueError("bad json")

    monkeypatch.setattr(flow.json, "loads", boom)
    assert flow._load_config() == {}


def test_run_command_calls_subprocess(fake_subprocess):
    fake_subprocess.when("echo hi", stdout="hi\n")
    proc = flow.run_command(["echo", "hi"])
    assert proc.returncode == 0
    assert proc.stdout == "hi\n"
    cmd, kwargs = fake_subprocess.calls[0]
    assert cmd == ["echo", "hi"]
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["check"] is False


@pytest.mark.parametrize("text,expected", [
    ("Failure when receiving data", True),
    ("recv failure", True),
    ("SEND FAILURE", True),  # 小写匹配,大小写不敏感
    ("Connection reset by peer", True),
    ("connection refused", True),
    ("Connection timed out", True),
    ("Operation timed out", True),
    ("could not resolve host", True),
    ("Temporary failure in name resolution", True),
    ("Empty reply from server", True),
    ("returned error: 502", True),
    ("early EOF", True),
    ("RPC failed", True),
    ("The remote end hung up unexpectedly", True),
    ("gnutls_handshake", True),
    ("error: 404 Not Found", False),
    ("Authentication failed", False),
    ("repository not found", False),
    ("", False),
    ("compile error in source", False),
])
def test_is_transient_network_error(text, expected):
    assert flow._is_transient_network_error(text) is expected


def test_flow_steps_definition():
    assert flow.FLOW_STEPS == ["repo_check", "download", "license_check", "detect",
                               "existing_check", "build", "ci_gate", "review_summary"]


# ─────────────────────────────────────────────
# steps 跟踪与文件写
# ─────────────────────────────────────────────

def test_load_steps_missing_all_pending(tmp_path):
    steps = flow.load_steps("pkg", tmp_path)
    assert steps == {s: "pending" for s in flow.FLOW_STEPS}


def test_load_steps_existing(tmp_path):
    (tmp_path / "steps_pkg.json").write_text(
        json.dumps({"download": "done", "build": "failed"}))
    # 生产代码现状:已存在文件时原样返回,缺失键不补齐默认 pending
    assert flow.load_steps("pkg", tmp_path) == \
        {"download": "done", "build": "failed"}


def test_load_steps_corrupt_file(tmp_path):
    (tmp_path / "steps_pkg.json").write_text("{not json")
    assert flow.load_steps("pkg", tmp_path) == \
        {s: "pending" for s in flow.FLOW_STEPS}


def test_mark_step(tmp_path):
    flow.mark_step("pkg", tmp_path, "download", "failed")
    data = json.loads((tmp_path / "steps_pkg.json").read_text(encoding="utf-8"))
    assert data["download"] == "failed"
    assert data["repo_check"] == "pending"
    flow.mark_step("pkg", tmp_path, "download")
    data = json.loads((tmp_path / "steps_pkg.json").read_text(encoding="utf-8"))
    assert data["download"] == "done"


def test_write_json_creates_parents(tmp_path):
    target = tmp_path / "a" / "b" / "c.json"
    flow.write_json(target, {"x": [1, 2]})
    assert json.loads(target.read_text(encoding="utf-8")) == {"x": [1, 2]}


def test_result_path(tmp_path):
    assert flow.result_path("foo", tmp_path) == \
        tmp_path / "pkg_introduce_result_foo.json"


# ─────────────────────────────────────────────
# 版本文本处理
# ─────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("1.2.3", "1.2.3"),
    ("v1.2.3", "1.2.3"),
    ("  v1.2.3  ", "1.2.3"),
    ("", ""),
    (None, ""),
    ("V", "V"),  # 仅小写 v 剥离
])
def test_normalize_version_text(value, expected):
    assert flow.normalize_version_text(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("0.18.8", ("0.18.8", "0.18.8")),
    ("v0.18.8", ("0.18.8", "v0.18.8")),
    ("workers-sdk@0.18.8", ("0.18.8", "workers-sdk@0.18.8")),
    ("jfiglet-0.0.8", ("0.0.8", "jfiglet-0.0.8")),
    ("@cloudflare/pkg@1.0.0", ("1.0.0", "@cloudflare/pkg@1.0.0")),
    ("1.2", ("1.2", "1.2")),  # 数字段在串首 → 走默认路径
    ("1.2.3-rc.1", ("1.2.3-rc.1", "1.2.3-rc.1")),  # 预发布后缀无法匹配
    ("", ("", "")),
    ("v", ("", "v")),  # 仅 v → 剥成空
    ("  v1.0.0  ", ("1.0.0", "v1.0.0")),  # 先 strip
])
def test_split_version_input(value, expected):
    assert flow.split_version_input(value) == expected


# ─────────────────────────────────────────────
# run_result_command / update_result
# ─────────────────────────────────────────────

def test_run_result_command_composition(fake_subprocess):
    proc = flow.run_result_command("update", "pkg", P.parent, ["--action", "x"])
    assert proc.returncode == 0
    cmd = fake_subprocess.calls[0][0]
    assert cmd[0] == sys.executable
    assert cmd[1].endswith("pkg_introduce_result.py")
    assert cmd[2] == "update"
    assert cmd[3] == "pkg"
    assert "--reports-dir" in cmd
    assert cmd[-2:] == ["--action", "x"]


def test_update_result_flag_mapping(fake_subprocess, tmp_path):
    flow.update_result(
        "pkg", tmp_path, action="built_new", reason="r", status="done",
        failure_type="t", failure_reason="fr", version="1.0",
        requested_version="2.0", decision="introduce_new", lang="python",
        analysis_file="af.json", archived=True)
    cmd = fake_subprocess.calls[0][0]
    assert cmd[2] == "update"
    for flag, value in [("--action", "built_new"), ("--reason", "r"),
                        ("--status", "done"), ("--failure-type", "t"),
                        ("--failure-reason", "fr"), ("--version", "1.0"),
                        ("--requested-version", "2.0"),
                        ("--decision", "introduce_new"), ("--lang", "python"),
                        ("--analysis-file", "af.json"),
                        ("--archived", "true")]:
        idx = cmd.index(flag)
        assert cmd[idx + 1] == value


def test_update_result_none_omitted(fake_subprocess, tmp_path):
    flow.update_result("pkg", tmp_path)
    cmd = fake_subprocess.calls[0][0]
    assert "--archived" not in cmd
    assert "--version" not in cmd


def test_update_result_archived_false(fake_subprocess, tmp_path):
    flow.update_result("pkg", tmp_path, archived=False)
    cmd = fake_subprocess.calls[0][0]
    assert cmd[cmd.index("--archived") + 1] == "false"


def test_update_result_failure_raises(fake_subprocess, tmp_path):
    fake_subprocess.when(lambda s: "pkg_introduce_result.py" in s,
                         returncode=1, stderr="bad field\n")
    with pytest.raises(flow.FlowError) as ei:
        flow.update_result("pkg", tmp_path, action="x")
    assert ei.value.reason == "failed to update result: bad field"


# ─────────────────────────────────────────────
# initialize_top_level / run_repo_check
# ─────────────────────────────────────────────

def test_initialize_top_level_success(fake_subprocess, tmp_path):
    result = flow.initialize_top_level(tmp_path / "bs", tmp_path / "r",
                                       tmp_path / "s")
    assert result["status"] == "done"
    assert result["build_state_dir"] == str(tmp_path / "bs")
    assert result["reports_dir"] == str(tmp_path / "r")
    assert result["sources_dir"] == str(tmp_path / "s")
    cmd = fake_subprocess.calls[0][0]
    assert cmd[1].endswith("init_session_state.py")
    assert "--build-state-dir" in cmd and "--reports-dir" in cmd


def test_initialize_top_level_failure(fake_subprocess, tmp_path):
    fake_subprocess.when(lambda s: "init_session_state.py" in s,
                         returncode=1, stderr="init boom\n")
    with pytest.raises(flow.FlowError) as ei:
        flow.initialize_top_level(tmp_path / "bs", tmp_path / "r", tmp_path / "s")
    assert ei.value.reason == "init boom"


def test_initialize_top_level_failure_no_output(fake_subprocess, tmp_path):
    fake_subprocess.when(lambda s: "init_session_state.py" in s, returncode=1)
    with pytest.raises(flow.FlowError) as ei:
        flow.initialize_top_level(tmp_path / "bs", tmp_path / "r", tmp_path / "s")
    assert ei.value.reason == "failed to initialize session state"


def test_run_repo_check_success(fake_subprocess, tmp_path):
    result = flow.run_repo_check("pkg", "https://github.com/x/y", tmp_path)
    assert result["status"] == "done"
    assert result["repo_check"] == str(tmp_path / "repo_check_pkg.json")
    cmd = fake_subprocess.calls[0][0]
    assert cmd[1].endswith("check_repo.py")
    assert "https://github.com/x/y" in cmd
    assert "-o" in cmd
    steps = json.loads((tmp_path / "steps_pkg.json").read_text())
    assert steps["repo_check"] == "done"


def test_run_repo_check_failure(fake_subprocess, tmp_path):
    fake_subprocess.when(lambda s: "check_repo.py" in s,
                         returncode=1, stderr="blocked: inactive\n")
    with pytest.raises(flow.FlowError) as ei:
        flow.run_repo_check("pkg", "https://u", tmp_path)
    assert ei.value.reason == "blocked: inactive"
    assert ei.value.failure_type == "non_retryable_repo_blocked"
    steps = json.loads((tmp_path / "steps_pkg.json").read_text())
    assert steps["repo_check"] == "failed"


# ─────────────────────────────────────────────
# _split_github_tree_url / run_download
# ─────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://github.com/o/r/tree/main", ("https://github.com/o/r", "main")),
    ("https://github.com/o/r/tree/v1.2.3", ("https://github.com/o/r", "v1.2.3")),
    ("https://github.com/o/r/commit/abc1234",
     ("https://github.com/o/r", "abc1234")),
    ("http://github.com/o/r/tree/x", ("http://github.com/o/r", "x")),
    ("https://github.com/o/r", ("https://github.com/o/r", "")),
    ("https://github.com/o/r/blob/main/README.md",
     ("https://github.com/o/r/blob/main/README.md", "")),
    ("https://gitlab.com/o/r/tree/main",
     ("https://gitlab.com/o/r/tree/main", "")),  # 非 github.com
    ("https://github.com/o/r/tree/main/sub",
     ("https://github.com/o/r", "main")),  # 多余路径段被 .* 吞掉
])
def test_split_github_tree_url(url, expected):
    assert flow._split_github_tree_url(url) == expected


def test_run_download_success_with_version(fake_subprocess, tmp_path):
    result = flow.run_download("pkg", "https://github.com/o/r/tree/main", "1.0.0",
                               tmp_path / "sources", tmp_path)
    assert result["status"] == "done"
    assert result["source_dir"] == str(tmp_path / "sources" / "pkg")
    cmd = fake_subprocess.calls[0][0]
    assert cmd[1].endswith("download_source.py")
    assert "--upstream-url" in cmd
    assert cmd[cmd.index("--upstream-url") + 1] == "https://github.com/o/r"  # tree 段已剥离
    assert "--version" in cmd and "1.0.0" in cmd
    assert "--ref" not in cmd


def test_run_download_extracted_ref_no_version(fake_subprocess, tmp_path):
    flow.run_download("pkg", "https://github.com/o/r/tree/v2.0.0", "",
                      tmp_path / "sources", tmp_path)
    cmd = fake_subprocess.calls[0][0]
    assert "--ref" in cmd and cmd[cmd.index("--ref") + 1] == "v2.0.0"
    assert "--version" not in cmd


def test_run_download_constraint_when_no_version_or_ref(fake_subprocess, tmp_path):
    flow.run_download("pkg", "https://github.com/o/r", "", tmp_path / "sources",
                      tmp_path, constraint=">= 1.4.0")
    cmd = fake_subprocess.calls[0][0]
    assert "--constraint" in cmd and cmd[cmd.index("--constraint") + 1] == ">= 1.4.0"


def test_run_download_clears_source_dir(tmp_path, fake_subprocess):
    src = tmp_path / "sources" / "pkg"
    src.mkdir(parents=True)
    (src / "old.txt").write_text("x")
    flow.run_download("pkg", "https://github.com/o/r", "", tmp_path / "sources",
                      tmp_path)
    assert not src.exists()  # shutil.rmtree 清掉旧源码目录


def test_run_download_version_failure_falls_back_to_ref(fake_subprocess, tmp_path,
                                                        capsys):
    fake_subprocess.when(lambda s: "--version" in s,
                         returncode=1, stderr="tag not found\n")
    fake_subprocess.when(lambda s: "--ref" in s, returncode=0)
    result = flow.run_download("pkg", "https://github.com/o/r/tree/v9.9.9",
                               "1.0.0", tmp_path / "sources", tmp_path)
    assert result["status"] == "done"
    # 第二次调用以 --ref 重试
    second = fake_subprocess.calls[1][0]
    assert "--ref" in second and second[second.index("--ref") + 1] == "v9.9.9"
    assert "回退到 URL 中的 ref" in capsys.readouterr().err


def test_run_download_transient_retries_then_fails(fake_subprocess, tmp_path,
                                                   monkeypatch):
    sleeps = []
    monkeypatch.setattr(flow.time, "sleep", lambda s: sleeps.append(s))
    fake_subprocess.when(lambda s: "download_source.py" in s,
                         returncode=1, stderr="Connection reset by peer\n")
    with pytest.raises(flow.FlowError) as ei:
        flow.run_download("pkg", "https://github.com/o/r", "", tmp_path / "s",
                          tmp_path)
    assert ei.value.failure_type == "retryable_network"
    assert sleeps == [10, 20]  # 1 次原始 + 2 次重试
    assert len(fake_subprocess.calls) == 3
    steps = json.loads((tmp_path / "steps_pkg.json").read_text())
    assert steps["download"] == "failed"


def test_run_download_transient_then_success(fake_subprocess, tmp_path,
                                             monkeypatch):
    monkeypatch.setattr(flow.time, "sleep", lambda s: None)
    attempts = {"n": 0}

    def first_only(cmd_str):
        attempts["n"] += 1
        return attempts["n"] == 1

    fake_subprocess.when(first_only, returncode=1,
                         stderr="The remote end hung up unexpectedly\n")
    result = flow.run_download("pkg", "https://github.com/o/r", "",
                               tmp_path / "s", tmp_path)
    assert result["status"] == "done"
    assert attempts["n"] == 2
    steps = json.loads((tmp_path / "steps_pkg.json").read_text())
    assert steps["download"] == "done"


def test_run_download_non_transient_failure(fake_subprocess, tmp_path,
                                            monkeypatch):
    sleeps = []
    monkeypatch.setattr(flow.time, "sleep", lambda s: sleeps.append(s))
    fake_subprocess.when(lambda s: "download_source.py" in s,
                         returncode=1, stderr="404 Not Found\n")
    with pytest.raises(flow.FlowError) as ei:
        flow.run_download("pkg", "https://github.com/o/r", "", tmp_path / "s",
                          tmp_path)
    assert ei.value.failure_type == "non_retryable_source_missing"
    assert ei.value.reason == "404 Not Found"
    assert sleeps == []  # 非瞬时错误不重试
    assert len(fake_subprocess.calls) == 1


# ─────────────────────────────────────────────
# run_license_check
# ─────────────────────────────────────────────

def test_run_license_check_disabled_by_config(fake_subprocess, tmp_path,
                                              monkeypatch, capsys):
    monkeypatch.setattr(flow, "_load_config",
                        lambda: {"license_check": {"enabled": False}})
    result = flow.run_license_check("pkg", tmp_path / "src", tmp_path)
    assert result["status"] == "skipped"
    saved = json.loads((tmp_path / "license_check_pkg.json").read_text())
    assert saved["category"] == "skipped"
    assert "license_check.enabled=false" in saved["message"]
    assert fake_subprocess.calls == []  # 未调 check_license.py
    steps = json.loads((tmp_path / "steps_pkg.json").read_text())
    assert steps["license_check"] == "skipped"
    assert "License 检查已跳过" in capsys.readouterr().out


def test_run_license_check_done(fake_subprocess, tmp_path):
    (tmp_path / "license_check_pkg.json").write_text(
        json.dumps({"needs_ai_fallback": False, "category": "permissive"}))
    result = flow.run_license_check("pkg", tmp_path / "src", tmp_path)
    assert result["status"] == "done"
    assert result["license_check"] == str(tmp_path / "license_check_pkg.json")
    cmd = fake_subprocess.calls[0][0]
    assert cmd[1].endswith("check_license.py")
    assert cmd[2] == str(tmp_path / "src")
    assert "--pkg" in cmd and cmd[cmd.index("--pkg") + 1] == "pkg"


def test_run_license_check_needs_ai(fake_subprocess, tmp_path):
    (tmp_path / "license_check_pkg.json").write_text(json.dumps({
        "needs_ai_fallback": True, "category": "unknown", "message": "m"}))
    result = flow.run_license_check("pkg", tmp_path / "src", tmp_path)
    assert result["status"] == "needs_ai"
    assert result["category"] == "unknown"
    assert result["reason"] == "m"
    steps = json.loads((tmp_path / "steps_pkg.json").read_text())
    assert steps["license_check"] == "needs_ai"


def test_run_license_check_failure(fake_subprocess, tmp_path):
    fake_subprocess.when(lambda s: "check_license.py" in s,
                         returncode=1, stderr="blocked: BUSL\n")
    with pytest.raises(flow.FlowError) as ei:
        flow.run_license_check("pkg", tmp_path / "src", tmp_path)
    assert ei.value.failure_type == "non_retryable_license_blocked"
    steps = json.loads((tmp_path / "steps_pkg.json").read_text())
    assert steps["license_check"] == "failed"


# ─────────────────────────────────────────────
# detect_lang / detect_lang_and_version
# ─────────────────────────────────────────────

@pytest.mark.parametrize("files,expected", [
    (["go.mod"], "go"),
    (["Cargo.toml"], "rust"),
    (["package.json"], "nodejs"),
    (["pyproject.toml"], "python"),
    (["setup.py"], "python"),
    (["pom.xml"], "java"),
    (["build.gradle"], "java"),
    (["build.gradle.kts"], "java"),
    (["demo.gemspec"], "ruby"),
    (["Gemfile"], "ruby"),
    (["CMakeLists.txt"], "c"),
    (["meson.build"], "c"),
    (["configure.ac"], "c"),
    ([], "python"),  # 兜底
])
def test_detect_lang(tmp_path, files, expected):
    for f in files:
        (tmp_path / f).write_text("x")
    assert flow.detect_lang(tmp_path) == expected


def test_detect_lang_priority(tmp_path):
    (tmp_path / "Cargo.toml").write_text("x")
    (tmp_path / "package.json").write_text("x")
    (tmp_path / "go.mod").write_text("x")
    assert flow.detect_lang(tmp_path) == "go"  # go.mod 优先级最高


def test_detect_lang_and_version_done(fake_subprocess, tmp_path):
    fake_subprocess.when(lambda s: "extract_version.py" in s, stdout="1.2.3\n")
    result = flow.detect_lang_and_version(tmp_path, "1.2.3")
    assert result == {"status": "done", "lang": "python", "version": "1.2.3"}


def test_detect_lang_and_version_failure(fake_subprocess, tmp_path):
    fake_subprocess.when(lambda s: "extract_version.py" in s,
                         returncode=1, stderr="cannot detect\n")
    with pytest.raises(flow.FlowError) as ei:
        flow.detect_lang_and_version(tmp_path, "")
    assert ei.value.reason == "cannot detect"


def test_detect_lang_and_version_empty_needs_agent(fake_subprocess, tmp_path):
    fake_subprocess.when(lambda s: "extract_version.py" in s, stdout="\n")
    result = flow.detect_lang_and_version(tmp_path, "1.0")
    assert result["status"] == "needs_agent"
    assert result["lang"] == "python"
    assert result["version"] == ""
    assert result["expected_version"] == "1.0"
    assert "static version extraction returned empty" in result["reason"]


def test_detect_lang_and_version_mismatch_needs_ai(fake_subprocess, tmp_path):
    fake_subprocess.when(lambda s: "extract_version.py" in s, stdout="0.9\n")
    result = flow.detect_lang_and_version(tmp_path, "1.0")
    assert result["status"] == "needs_ai"
    assert result["version"] == "0.9"
    assert result["expected_version"] == "1.0"
    assert "does not match expected" in result["reason"]


def test_detect_lang_and_version_v_prefix_normalized(fake_subprocess, tmp_path):
    # expected 带 v 前缀时归一化后视为一致
    fake_subprocess.when(lambda s: "extract_version.py" in s, stdout="1.0.0\n")
    result = flow.detect_lang_and_version(tmp_path, "v1.0.0")
    assert result["status"] == "done"
    assert result["version"] == "1.0.0"


# ─────────────────────────────────────────────
# run_existing_check
# ─────────────────────────────────────────────

def _write_existing(tmp_path, decision="introduce_new", reason="r"):
    p = tmp_path / "existing_check_pkg.json"
    p.write_text(json.dumps({"decision": decision, "reason": reason}))
    return p


def test_run_existing_check_success(fake_subprocess, tmp_path):
    _write_existing(tmp_path, decision="reuse_official", reason="found")
    result = flow.run_existing_check("pkg", "1.0", "python", tmp_path, "oe")
    assert result["status"] == "done"
    assert result["decision"] == "reuse_official"
    assert result["reason"] == "found"
    cmd = fake_subprocess.calls[0][0]
    assert cmd[1].endswith("check_existing_package.py")
    assert "--version" in cmd and "1.0" in cmd
    assert "--lang" in cmd and "python" in cmd
    assert "--container" in cmd and "oe" in cmd
    assert "--requirement" not in cmd


def test_run_existing_check_constraint(fake_subprocess, tmp_path):
    _write_existing(tmp_path)
    flow.run_existing_check("pkg", "1.0", "python", tmp_path, "oe",
                            constraint=">= 1.4.0")
    cmd = fake_subprocess.calls[0][0]
    assert "--requirement" in cmd and cmd[cmd.index("--requirement") + 1] == ">= 1.4.0"


def test_run_existing_check_failure(fake_subprocess, tmp_path):
    fake_subprocess.when(lambda s: "check_existing_package.py" in s,
                         returncode=1, stderr="query failed\n")
    with pytest.raises(flow.FlowError) as ei:
        flow.run_existing_check("pkg", "1.0", "python", tmp_path, "oe")
    assert ei.value.reason == "query failed"
    steps = json.loads((tmp_path / "steps_pkg.json").read_text())
    assert steps["existing_check"] == "failed"


def test_run_existing_check_compat_nodejs(fake_subprocess, tmp_path, monkeypatch):
    monkeypatch.setattr(flow, "_load_config",
                        lambda: {"dep_conflict": {"mode": "compat"}})
    _write_existing(tmp_path, decision="block_official_older",
                    reason="official has 1.0")
    result = flow.run_existing_check("pkg", "2.0", "nodejs", tmp_path, "oe")
    assert result["decision"] == "introduce_new"
    assert "dep_conflict.mode=compat" in result["reason"]
    data = json.loads((tmp_path / "existing_check_pkg.json").read_text())
    assert data["compat_introduce"] is True
    assert data["decision"] == "introduce_new"


def test_run_existing_check_compat_unsupported_lang(fake_subprocess, tmp_path,
                                                    monkeypatch):
    monkeypatch.setattr(flow, "_load_config",
                        lambda: {"dep_conflict": {"mode": "compat"}})
    _write_existing(tmp_path, decision="block_official_older", reason="r")
    result = flow.run_existing_check("pkg", "2.0", "go", tmp_path, "oe")
    assert result["decision"] == "block_official_older"  # go 不在 compat 列表


def test_run_existing_check_force_compat_any_lang(fake_subprocess, tmp_path,
                                                  monkeypatch):
    monkeypatch.setattr(flow, "_load_config",
                        lambda: {"dep_conflict": {"mode": "force_compat"}})
    _write_existing(tmp_path, decision="block_official_older", reason="r")
    result = flow.run_existing_check("pkg", "2.0", "go", tmp_path, "oe")
    assert result["decision"] == "introduce_new"


def test_run_existing_check_block_mode(fake_subprocess, tmp_path, monkeypatch):
    monkeypatch.setattr(flow, "_load_config", lambda: {})
    _write_existing(tmp_path, decision="block_official_older", reason="r")
    result = flow.run_existing_check("pkg", "2.0", "nodejs", tmp_path, "oe")
    assert result["decision"] == "block_official_older"
    data = json.loads((tmp_path / "existing_check_pkg.json").read_text())
    assert "compat_introduce" not in data


# ─────────────────────────────────────────────
# finalize_result / print_payload / main
# ─────────────────────────────────────────────

def test_finalize_result(tmp_path, monkeypatch):
    recorded = {}

    def fake_update(pkg, reports_dir, **kw):
        recorded["pkg"] = pkg
        recorded["reports_dir"] = reports_dir
        recorded.update(kw)

    monkeypatch.setattr(flow, "update_result", fake_update)
    import argparse
    args = argparse.Namespace(
        pkg="pkg", reports_dir=str(tmp_path), action="built_new", reason="r",
        status="done", failure_type=None, failure_reason=None, version="1.0",
        requested_version=None, decision="introduce_new", lang="python",
        analysis_file=None, archived=False)
    result = flow.finalize_result(args)
    assert result == {"status": "done",
                      "result_file": str(tmp_path / "pkg_introduce_result_pkg.json")}
    assert recorded["action"] == "built_new"
    assert recorded["archived"] is False


def test_print_payload(capsys):
    assert flow.print_payload({"a": 1}) == 0
    out = capsys.readouterr().out
    assert json.loads(out) == {"a": 1}


def test_main_init_success(fake_subprocess, tmp_path, monkeypatch, capsys):
    _argv(["init", "--pkg", "pkg", "--mode", "top-level",
           "--build-state-dir", str(tmp_path / "bs"),
           "--reports-dir", str(tmp_path / "r"),
           "--sources-dir", str(tmp_path / "s")], monkeypatch)
    assert flow.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "done"


def test_main_init_failure(fake_subprocess, tmp_path, monkeypatch, capsys):
    fake_subprocess.when(lambda s: "init_session_state.py" in s,
                         returncode=1, stderr="boom\n")
    _argv(["init", "--pkg", "pkg", "--mode", "top-level"], monkeypatch)
    assert flow.main() == 1
    err = capsys.readouterr().err
    assert '"status": "failed"' in err
    assert "boom" in err


def test_main_repo_check(fake_subprocess, tmp_path, monkeypatch):
    _argv(["repo-check", "--pkg", "pkg", "--upstream-url", "https://u",
           "--reports-dir", str(tmp_path)], monkeypatch)
    assert flow.main() == 0


def test_main_download(fake_subprocess, tmp_path, monkeypatch):
    _argv(["download", "--pkg", "pkg", "--upstream-url", "https://u",
           "--sources-dir", str(tmp_path / "s"),
           "--reports-dir", str(tmp_path)], monkeypatch)
    assert flow.main() == 0


def test_main_license_check_skipped(fake_subprocess, tmp_path, monkeypatch):
    monkeypatch.setattr(flow, "_load_config",
                        lambda: {"license_check": {"enabled": False}})
    _argv(["license-check", "--pkg", "pkg",
           "--source-dir", str(tmp_path / "src"),
           "--reports-dir", str(tmp_path)], monkeypatch)
    assert flow.main() == 0


def test_main_license_check_done(fake_subprocess, tmp_path, monkeypatch):
    (tmp_path / "license_check_pkg.json").write_text(
        json.dumps({"needs_ai_fallback": False}))
    _argv(["license-check", "--pkg", "pkg",
           "--source-dir", str(tmp_path / "src"),
           "--reports-dir", str(tmp_path)], monkeypatch)
    assert flow.main() == 0


def test_main_detect_success(fake_subprocess, tmp_path, monkeypatch):
    fake_subprocess.when(lambda s: "extract_version.py" in s, stdout="1.2.3\n")
    _argv(["detect", "--pkg", "pkg", "--source-dir", str(tmp_path),
           "--reports-dir", str(tmp_path)], monkeypatch)
    assert flow.main() == 0
    steps = json.loads((tmp_path / "steps_pkg.json").read_text())
    assert steps["detect"] == "done"


def test_main_detect_splits_full_tag(fake_subprocess, tmp_path, monkeypatch):
    # P0-1:expected-version 为完整 tag 时按裸版本号比对
    fake_subprocess.when(lambda s: "extract_version.py" in s, stdout="0.18.8\n")
    _argv(["detect", "--pkg", "pkg", "--source-dir", str(tmp_path),
           "--expected-version", "workers-sdk@0.18.8",
           "--reports-dir", str(tmp_path)], monkeypatch)
    assert flow.main() == 0


def test_main_detect_failure(fake_subprocess, tmp_path, monkeypatch):
    fake_subprocess.when(lambda s: "extract_version.py" in s,
                         returncode=1, stderr="boom\n")
    _argv(["detect", "--pkg", "pkg", "--source-dir", str(tmp_path),
           "--reports-dir", str(tmp_path)], monkeypatch)
    assert flow.main() == 1
    steps = json.loads((tmp_path / "steps_pkg.json").read_text())
    assert steps["detect"] == "failed"


def test_main_existing_check(fake_subprocess, tmp_path, monkeypatch):
    _write_existing(tmp_path)
    _argv(["existing-check", "--pkg", "pkg", "--version", "1.0", "--lang", "go",
           "--reports-dir", str(tmp_path)], monkeypatch)
    assert flow.main() == 0


def test_main_finalize_result_splits_version(fake_subprocess, tmp_path,
                                             monkeypatch):
    _argv(["finalize-result", "--pkg", "pkg",
           "--reports-dir", str(tmp_path),
           "--version", "v1.2.3", "--archived", "true"], monkeypatch)
    assert flow.main() == 0
    cmd = fake_subprocess.calls[0][0]
    assert cmd[2] == "update"
    assert cmd[cmd.index("--version") + 1] == "1.2.3"
    assert cmd[cmd.index("--archived") + 1] == "true"


def test_main_finalize_result_archived_false(fake_subprocess, tmp_path,
                                             monkeypatch):
    _argv(["finalize-result", "--pkg", "pkg",
           "--reports-dir", str(tmp_path), "--archived", "false"], monkeypatch)
    assert flow.main() == 0
    cmd = fake_subprocess.calls[0][0]
    assert cmd[cmd.index("--archived") + 1] == "false"


def test_main_mark_step(fake_subprocess, tmp_path, monkeypatch):
    _argv(["mark-step", "--pkg", "pkg", "--step", "build",
           "--status", "failed", "--reports-dir", str(tmp_path)], monkeypatch)
    assert flow.main() == 0
    steps = json.loads((tmp_path / "steps_pkg.json").read_text())
    assert steps["build"] == "failed"


def test_main_mark_step_invalid_choice(monkeypatch):
    _argv(["mark-step", "--pkg", "pkg", "--step", "nope"], monkeypatch)
    with pytest.raises(SystemExit) as ei:
        flow.main()
    assert ei.value.code == 2
