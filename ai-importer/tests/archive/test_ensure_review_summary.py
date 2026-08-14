"""ensure_review_summary.py 单元测试 — review_summary 步骤状态检查与标记。

main() 输出 JSON 到 stdout,状态信息到 stderr;退出码:
0 全部完成 / 2 存在需补齐 / 1 出错(argparse 错误为 2)。
"""

from __future__ import annotations

import json
import sys

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sys.path.insert(0, str(SCRIPT_DIRS["archive"]))
e = load_module("ensure_review_summary",
                SCRIPT_DIRS["archive"] / "ensure_review_summary.py")


def _run_main(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["ensure_review_summary.py", *argv])
    return e.main()


def _join(cmd) -> str:
    return " ".join(c if isinstance(c, str) else str(c) for c in cmd)


# ─────────────────────────────────────────────
# load_steps
# ─────────────────────────────────────────────

@pytest.mark.parametrize("kind", ["missing", "valid", "invalid_json", "is_dir"])
def test_load_steps(tmp_path, kind):
    reports = tmp_path / "reports"
    reports.mkdir()
    if kind == "valid":
        (reports / "steps_foo.json").write_text(
            json.dumps({"review_summary": "done", "extra": 1}))
    elif kind == "invalid_json":
        (reports / "steps_foo.json").write_text("{oops")
    elif kind == "is_dir":
        (reports / "steps_foo.json").mkdir()  # read_text 抛异常 → 返回 {}
    expected = {"review_summary": "done", "extra": 1} if kind == "valid" else {}
    assert e.load_steps(reports, "foo") == expected


# ─────────────────────────────────────────────
# mark_done
# ─────────────────────────────────────────────

def test_mark_done_calls_flow_script(tmp_path, fake_subprocess):
    e.mark_done("foo", tmp_path)
    assert len(fake_subprocess.calls) == 1
    cmd, kw = fake_subprocess.calls[0]
    joined = _join(cmd)
    assert cmd[0] == sys.executable
    assert "run_pkg_introduce_flow.py" in joined
    assert "mark-step" in joined
    assert "--step review_summary" in joined
    assert "--status done" in joined
    assert str(tmp_path) in joined
    assert kw == {"check": False}


# ─────────────────────────────────────────────
# main
# ─────────────────────────────────────────────

def test_main_all_done(tmp_path, monkeypatch, capsys):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "steps_a.json").write_text(json.dumps({"review_summary": "done"}))
    (reports / "steps_b.json").write_text(json.dumps({"review_summary": "skipped"}))
    rc = _run_main(monkeypatch, "--pkgs", "a", "b", "--reports-dir", str(reports))
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"status": "ok", "needs_summary": []}


def test_main_needs_summary(tmp_path, monkeypatch, capsys):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "steps_b.json").write_text(json.dumps({"review_summary": "done"}))
    # a 无 steps 文件 → 默认 pending → 需要补齐
    rc = _run_main(monkeypatch, "--pkgs", "a", "b", "--reports-dir", str(reports))
    assert rc == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "needs_summary", "needs_summary": ["a"]}


def test_main_mixed_status_order(tmp_path, monkeypatch, capsys):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "steps_a.json").write_text(json.dumps({"review_summary": "running"}))
    (reports / "steps_b.json").write_text(json.dumps({"review_summary": "done"}))
    (reports / "steps_c.json").write_text(json.dumps({"review_summary": "skipped"}))
    rc = _run_main(monkeypatch, "--pkgs", "a", "b", "c",
                   "--reports-dir", str(reports))
    assert rc == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "needs_summary", "needs_summary": ["a"]}  # 按 --pkgs 顺序


def test_main_skipped_only(tmp_path, monkeypatch, capsys):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "steps_a.json").write_text(json.dumps({"review_summary": "skipped"}))
    rc = _run_main(monkeypatch, "--pkgs", "a", "--reports-dir", str(reports))
    assert rc == 0  # skipped 视为完成


def test_main_mark_done(tmp_path, monkeypatch, capsys, fake_subprocess):
    rc = _run_main(monkeypatch, "--pkgs", "a", "b",
                   "--reports-dir", str(tmp_path), "--mark-done")
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "ok", "marked": ["a", "b"]}
    n = sum(1 for cmd, _ in fake_subprocess.calls if "mark-step" in _join(cmd))
    assert n == 2


def test_main_missing_pkgs(monkeypatch):
    with pytest.raises(SystemExit) as ei:
        _run_main(monkeypatch)
    assert ei.value.code == 2
