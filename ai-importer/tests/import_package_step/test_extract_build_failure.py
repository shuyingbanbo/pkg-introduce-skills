"""extract-build-failure.py — 构建日志错误提取(纯逻辑 + main 主路径)。

已知 bug(只记录不修,见 test_main_missing_module_hint_python):
  extract-build-failure.py 中 `dict.fromkeys(mods)[:10]` 对 dict 切片恒抛
  TypeError,被 except 吞掉 → missing_module_hints 功能恒为 []。
  生产修复应为 `list(dict.fromkeys(mods))[:10]`。
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

ebf = load_module("extract-build-failure", SCRIPT_DIRS["step"] / "extract-build-failure.py")


# ─────────────────────────────────────────────
# _detect_phase
# ─────────────────────────────────────────────

def test_detect_phase_unpackaged_wins():
    lines = ["Executing(%build)", "error: Installed (but unpackaged) file(s) found", "x"]
    assert ebf._detect_phase(lines) == "%files"


@pytest.mark.parametrize("bad_exit_line,expected", [
    ("Bad exit status from /var/tmp/rpm-tmp.x (%build)", "%build"),
    ("Bad exit status from /var/tmp/rpm-tmp.y (%install)", "%install"),
])
def test_detect_phase_bad_exit(bad_exit_line, expected):
    lines = ["Executing(%prep)", bad_exit_line, "RPM build errors"]
    assert ebf._detect_phase(lines) == expected


def test_detect_phase_last_executing_fallback():
    lines = ["Executing(%prep)", "Executing(%build)", "something went wrong"]
    assert ebf._detect_phase(lines) == "%build"


def test_detect_phase_unknown():
    assert ebf._detect_phase(["nothing special"]) == "unknown"
    assert ebf._detect_phase([]) == "unknown"


# ─────────────────────────────────────────────
# _extract_error_lines
# ─────────────────────────────────────────────

def test_extract_error_lines_context_and_dedup():
    lines = [
        "line0", "line1", "error: boom1", "line3", "line4",
        "error: boom1",   # 重复行被去重
        "line6", "error: boom2", "line8",
    ]
    picked = ebf._extract_error_lines(lines)
    assert "error: boom1" in picked
    assert "error: boom2" in picked
    assert picked.count("error: boom1") == 1
    # 上下文:boom1 前后各 2 行
    assert "line1" in picked and "line3" in picked


def test_extract_error_lines_no_hit():
    assert ebf._extract_error_lines(["clean line"]) == []
    assert ebf._extract_error_lines([]) == []


def test_extract_error_lines_case_insensitive():
    assert ebf._extract_error_lines(["ERROR: fatal stuff"]) != []


def test_extract_error_lines_cap():
    many = [f"error: err{i}" for i in range(100)]
    picked = ebf._extract_error_lines(many)
    assert len(picked) <= ebf._CAP_ERROR_LINES


# ─────────────────────────────────────────────
# _detect_failing_command
# ─────────────────────────────────────────────

def test_detect_failing_command():
    lines = [
        "+ make -j4",             # 错误之前最近的命令
        "gcc: error: foo.c: No such file",
    ]
    assert ebf._detect_failing_command(lines) == "make -j4"


def test_detect_failing_command_no_error():
    assert ebf._detect_failing_command(["+ make -j4"]) == ""
    assert ebf._detect_failing_command([]) == ""


def test_detect_failing_command_no_leading_plus():
    lines = ["plain line", "error: boom"]
    assert ebf._detect_failing_command(lines) == ""


def test_detect_failing_command_truncates():
    lines = ["+ " + "x" * 500, "error: boom"]
    assert len(ebf._detect_failing_command(lines)) <= 300


# ─────────────────────────────────────────────
# _normalize / _signature
# ─────────────────────────────────────────────

def test_normalize():
    assert ebf._normalize("/builddir/build/BUILD/foo-1.2/x.c:3: error: x") == \
        "<path>:<n>: error: x"
    assert ebf._normalize("Error   Line  1") == "error line <n>"


def test_signature():
    # "/x/1" 整体被路径正则吞掉,数字不再单独归一
    assert ebf._signature("%build", ["/x/1: error: a"]) == \
        "%build|<path>: error: a"
    assert ebf._signature("%build", []) == "%build|"


# ─────────────────────────────────────────────
# main(CLI 主路径)
# ─────────────────────────────────────────────

def _setup_pkg(tmp_path, pkg="git", build_log="", build_id="", lang=""):
    pkg_dir = tmp_path / "pkgs" / pkg
    pkg_dir.mkdir(parents=True)
    result = {"build_log": build_log}
    if build_id:
        result["copr_build_id"] = build_id
    (pkg_dir / "build_rpm_result.json").write_text(json.dumps(result))
    if lang:
        (pkg_dir / f"gate_result_{pkg}.json").write_text(json.dumps(
            {"result": {"lang": lang}}))
    return pkg_dir


def _main(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["extract-build-failure.py"] + argv)
    return ebf.main()


def test_main_missing_result_skips(tmp_path, capsys, monkeypatch):
    rc = _main(monkeypatch, ["--session-dir", str(tmp_path), "--pkg", "git"])
    assert rc == 0
    assert "不存在" in capsys.readouterr().err


def test_main_writes_report(tmp_path, capsys, monkeypatch):
    log = "Executing(%build)\n+ make -j4\ngcc: error: foo.c: No such file or directory\n"
    pkg_dir = _setup_pkg(tmp_path, build_log=log, build_id="42")
    (pkg_dir / "git.spec").write_text("Name: git\n")

    rc = _main(monkeypatch, ["--session-dir", str(tmp_path), "--pkg", "git"])
    assert rc == 0
    report = json.loads((pkg_dir / "build_failure_42.json").read_text())
    assert report["build_id"] == "42"
    assert report["failed_phase"] == "%build"
    assert report["failing_command"] == "make -j4"
    assert report["error_lines"] != []
    assert report["spec_hash"].startswith("sha256:")
    assert report["same_as_previous"] is False
    assert "log_tail" in report


def test_main_same_as_previous(tmp_path, capsys, monkeypatch):
    """已知 bug 防护:生产代码用 `if build_id not in f` 子串匹配过滤 prev 文件,
    session 路径恰好含 build_id 数字时(pytest tmp 目录 pytest-N 含 1/2)会误过滤
    → same_as_previous 恒 False。测试用字母 build_id 规避路径巧合。
    生产修复应为文件名基名匹配(如 Path(f).name)。"""
    log = "error: boom\n"
    pkg_dir = _setup_pkg(tmp_path, build_log=log, build_id="AAA")
    rc = _main(monkeypatch, ["--session-dir", str(tmp_path), "--pkg", "git"])
    assert rc == 0

    pkg_dir2 = tmp_path / "pkgs" / "git"
    (pkg_dir2 / "build_rpm_result.json").write_text(json.dumps(
        {"build_log": log, "copr_build_id": "BBB"}))
    rc = _main(monkeypatch, ["--session-dir", str(tmp_path), "--pkg", "git"])
    assert rc == 0
    report = json.loads((pkg_dir2 / "build_failure_BBB.json").read_text())
    assert report["same_as_previous"] is True
    assert (pkg_dir2 / "build_failure_AAA.json").exists()


def test_main_missing_module_hint_python(tmp_path, capsys, monkeypatch):
    """已知 bug 固化测试:dict.fromkeys(mods)[:10] 对 dict 切片恒抛 TypeError,
    被 except 吞掉 → missing_module_hints 恒为 []。生产修复应为
    list(dict.fromkeys(mods))[:10](只记录,不修)。"""
    log = "ModuleNotFoundError: No module named 'requests'\n"
    pkg_dir = _setup_pkg(tmp_path, build_log=log, lang="python")
    rc = _main(monkeypatch, ["--session-dir", str(tmp_path), "--pkg", "git"])
    assert rc == 0
    report = json.loads((pkg_dir / "build_failure.json").read_text())
    assert report["missing_module_hints"] == []


def test_main_no_build_id_uses_default_name(tmp_path, capsys, monkeypatch):
    pkg_dir = _setup_pkg(tmp_path, build_log="error: boom")
    rc = _main(monkeypatch, ["--session-dir", str(tmp_path), "--pkg", "git"])
    assert rc == 0
    assert (pkg_dir / "build_failure.json").exists()
