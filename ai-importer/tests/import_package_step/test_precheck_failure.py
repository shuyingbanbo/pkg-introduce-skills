"""precheck_failure.py — 构建失败预检:宏修复检测 + hint 写入(纯逻辑)。"""

from __future__ import annotations

import json

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

pf = load_module("precheck_failure", SCRIPT_DIRS["step"] / "precheck_failure.py")


# ─────────────────────────────────────────────
# _detect_broken_macro / _resolve_macro_fix
# ─────────────────────────────────────────────

@pytest.mark.parametrize("lines,expected", [
    (["Name: foo\n", "%cmake_build\n", "%install\n"], "%cmake_build"),
    (["  %make_install  \n"], "%make_install"),          # 首尾空白也命中
    (["%cmake\n", "%install\n"], None),                   # 无坏宏
    ([], None),
])
def test_detect_broken_macro(lines, expected):
    assert pf._detect_broken_macro(lines) == expected


def test_resolve_macro_fix_found():
    lines = ["Name: foo\n", "%make_build\n", "%install\n"]
    fixed, patch = pf._resolve_macro_fix(lines)
    assert fixed == ["Name: foo\n", "make -j$(nproc)\n", "%install\n"]
    assert patch[0]["before"] == "%make_build"
    assert patch[0]["after"] == "make -j$(nproc)"
    assert "description" in patch[0]


def test_resolve_macro_fix_none():
    assert pf._resolve_macro_fix(["Name: foo\n", "%install\n"]) is None
    assert pf._resolve_macro_fix([]) is None


# ─────────────────────────────────────────────
# find_pattern
# ─────────────────────────────────────────────

@pytest.mark.parametrize("log,name", [
    ("some output\nfg: no job control\nmore", "fg_no_job_control"),
    ("bg: no job control", "bg_no_job_control"),
    ("cd: build: No such file or directory", "cd_no_such_file_prep"),
])
def test_find_pattern_matches(log, name):
    pat = pf.find_pattern(log)
    assert pat is not None
    assert pat["name"] == name
    assert pat["verdict"] == "rebuild"


def test_find_pattern_no_match():
    assert pf.find_pattern("everything fine") is None
    assert pf.find_pattern("") is None


# ─────────────────────────────────────────────
# write_hint
# ─────────────────────────────────────────────

def _make_pkg(tmp_path, pkgname="pkg", spec_lines=None):
    pkg_dir = tmp_path / "pkgs" / pkgname
    pkg_dir.mkdir(parents=True)
    if spec_lines is not None:
        (pkg_dir / f"{pkgname}.spec").write_text("".join(spec_lines))
    return pkg_dir


def test_write_hint_basic(tmp_path, capsys):
    pkg_dir = _make_pkg(tmp_path, spec_lines=["%cmake_build\n"])
    pat = pf.find_pattern("fg: no job control")
    pf.write_hint(tmp_path, "pkg", "123", pat)

    hint_path = pkg_dir / "failure_hint_pkg_123.json"
    assert hint_path.exists()
    hint = json.loads(hint_path.read_text())
    assert hint["type"] == "hint"
    assert hint["confidence"] == "high"
    assert hint["pattern"] == "fg_no_job_control"
    assert hint["verdict_hint"] == "rebuild"
    assert hint["spec_patch"] == [{
        "description": f"将 %cmake_build 替换为 cmake --build . -j$(nproc)，避免非交互 shell 中的 job control 错误（fg/bg）",
        "before": "%cmake_build",
        "after": "cmake --build . -j$(nproc)",
    }]
    assert "fix_instructions" in hint
    assert "note" in hint


def test_write_hint_without_build_id(tmp_path, capsys):
    pkg_dir = _make_pkg(tmp_path)
    pat = pf.find_pattern("bg: no job control")
    pf.write_hint(tmp_path, "pkg", "", pat)
    assert (pkg_dir / "failure_hint_pkg.json").exists()


def test_write_hint_cd_pattern_reason_substitution(tmp_path, capsys):
    """cd_no_such_file 的 reason 用匹配组填充。"""
    pkg_dir = _make_pkg(tmp_path)
    pat = pf.find_pattern("cd: build: No such file or directory")
    pf.write_hint(tmp_path, "pkg", "", pat)
    hint = json.loads((pkg_dir / "failure_hint_pkg.json").read_text())
    assert "build" in hint["reason"]
    assert hint["spec_patch"] == []   # 无 resolver → 无 patch


def test_write_hint_resolver_but_spec_without_macro(tmp_path, capsys):
    """pattern 命中但 spec 里没有坏宏 → 空 spec_patch + stderr 提示。"""
    pkg_dir = _make_pkg(tmp_path, spec_lines=["%cmake\n"])
    pat = pf.find_pattern("fg: no job control")
    pf.write_hint(tmp_path, "pkg", "", pat)
    hint = json.loads((pkg_dir / "failure_hint_pkg.json").read_text())
    assert hint["spec_patch"] == []
    assert "macro not found" in capsys.readouterr().err


def test_write_hint_no_spec_no_resolver(tmp_path, capsys):
    pkg_dir = _make_pkg(tmp_path)
    pat = pf.find_pattern("cd: build: No such file or directory")
    pf.write_hint(tmp_path, "pkg", "", pat)
    assert "requires AI-driven spec fix" in capsys.readouterr().err


# ─────────────────────────────────────────────
# get_build_log
# ─────────────────────────────────────────────

def test_get_build_log_missing_result(tmp_path):
    assert pf.get_build_log(tmp_path, "pkg") == ("", "")


def test_get_build_log_prefers_tail(tmp_path):
    pkg_dir = _make_pkg(tmp_path)
    (pkg_dir / "build_rpm_result.json").write_text(json.dumps({
        "build_log": "full log",
        "build_log_tail": "tail log",
        "copr_build_id": 42,
    }))
    log, bid = pf.get_build_log(tmp_path, "pkg")
    assert log == "tail log"
    assert bid == "42"


def test_get_build_log_fallback_to_full(tmp_path):
    pkg_dir = _make_pkg(tmp_path)
    (pkg_dir / "build_rpm_result.json").write_text(json.dumps({"build_log": "only full"}))
    log, bid = pf.get_build_log(tmp_path, "pkg")
    assert log == "only full"
    assert bid == ""


# ─────────────────────────────────────────────
# main(CLI)
# ─────────────────────────────────────────────

def _main(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["precheck_failure.py"] + argv)
    return pf.main()


def test_main_no_log_needs_ai(tmp_path, capsys, monkeypatch):
    _make_pkg(tmp_path)
    rc = _main(monkeypatch, ["--session-dir", str(tmp_path), "--pkgname", "pkg"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "needs_ai"


def test_main_no_pattern_needs_ai(tmp_path, capsys, monkeypatch):
    pkg_dir = _make_pkg(tmp_path)
    (pkg_dir / "build_rpm_result.json").write_text(json.dumps({"build_log": "clean log"}))
    rc = _main(monkeypatch, ["--session-dir", str(tmp_path), "--pkgname", "pkg"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "needs_ai"


def test_main_matched_writes_hint(tmp_path, capsys, monkeypatch):
    pkg_dir = _make_pkg(tmp_path, spec_lines=["%cmake_build\n"])
    (pkg_dir / "build_rpm_result.json").write_text(json.dumps({
        "build_log": "fg: no job control", "copr_build_id": "7",
    }))
    rc = _main(monkeypatch, ["--session-dir", str(tmp_path), "--pkgname", "pkg"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "hint_written"
    assert (pkg_dir / "failure_hint_pkg_7.json").exists()
