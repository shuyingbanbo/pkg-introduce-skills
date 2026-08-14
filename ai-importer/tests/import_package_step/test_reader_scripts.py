"""小 reader 脚本组:read-session / read-gate-result / read-gate-fields /
read-build-result / read-dep-registry / print-summary / mark-interrupted
(喂 JSON 断言 stdout,shell eval 格式)。"""

from __future__ import annotations

import json

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

read_session = load_module("read-session", SCRIPT_DIRS["step"] / "read-session.py")
read_gate_result = load_module("read-gate-result", SCRIPT_DIRS["step"] / "read-gate-result.py")
read_gate_fields = load_module("read-gate-fields", SCRIPT_DIRS["step"] / "read-gate-fields.py")
read_build_result = load_module("read-build-result", SCRIPT_DIRS["step"] / "read-build-result.py")
read_dep_registry = load_module("read-dep-registry", SCRIPT_DIRS["step"] / "read-dep-registry.py")
print_summary = load_module("print-summary", SCRIPT_DIRS["step"] / "print-summary.py")
mark_interrupted = load_module("mark-interrupted", SCRIPT_DIRS["step"] / "mark-interrupted.py")


def _run(monkeypatch, mod, argv):
    monkeypatch.setattr("sys.argv", [mod.__name__.replace("_", "-") + ".py"] + argv)
    return mod.main()


# ─────────────────────────────────────────────
# read-session
# ─────────────────────────────────────────────

def test_read_session_exports(tmp_path, monkeypatch, capsys):
    (tmp_path / "session.json").write_text(json.dumps({
        "copr_url": "http://x", "copr_login": "u", "copr_token": "t",
        "copr_chroot": "openeuler-24.03-x86_64",
    }))
    rc = _run(monkeypatch, read_session, ["--session-dir", str(tmp_path)])
    assert rc is None
    out = capsys.readouterr().out
    assert "export COPR_FRONTEND_URL=http://x" in out  # 安全字符不加引号
    assert "export COPR_API_LOGIN=u" in out  # shlex.quote 简单值不加引号
    assert "export COPR_CHROOT=openeuler-24.03-x86_64" in out


def test_read_session_multi_chroot_primary(tmp_path, monkeypatch, capsys):
    """copr_chroots 列表 → COPR_CHROOTS 逗号分隔,COPR_CHROOT 取排序后 x86_64。"""
    (tmp_path / "session.json").write_text(json.dumps({
        "copr_chroots": ["openeuler-24.03-aarch64", "openeuler-24.03-x86_64"],
    }))
    _run(monkeypatch, read_session, ["--session-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert "export COPR_CHROOTS=openeuler-24.03-aarch64,openeuler-24.03-x86_64" in out  # 逗号安全字符不加引号
    assert "export COPR_CHROOT=openeuler-24.03-x86_64" in out


def test_read_session_field(tmp_path, monkeypatch, capsys):
    (tmp_path / "session.json").write_text(json.dumps({"pkgname": "git"}))
    _run(monkeypatch, read_session, ["--session-dir", str(tmp_path), "--field", "pkgname"])
    assert capsys.readouterr().out.strip() == "git"


def test_read_session_missing_exits_1(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit) as e:
        _run(monkeypatch, read_session, ["--session-dir", str(tmp_path)])
    assert e.value.code == 1


# ─────────────────────────────────────────────
# read-gate-result
# ─────────────────────────────────────────────

def _gate(tmp_path, overall="done", decision="introduce_new", lang="python", version="1.0"):
    pkg_dir = tmp_path / "pkgs" / "pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "gate_result_pkg.json").write_text(json.dumps({
        "overall_status": overall, "result": {"decision": decision, "lang": lang, "version": version},
    }))


def test_read_gate_result_ok(tmp_path, monkeypatch, capsys):
    _gate(tmp_path)
    rc = _run(monkeypatch, read_gate_result, ["--session-dir", str(tmp_path), "--pkgname", "pkg"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "GATE_DECISION=introduce_new" in out
    assert "GATE_LANG=python" in out
    assert "GATE_VERSION=1.0" in out


def test_read_gate_result_missing(tmp_path, monkeypatch, capsys):
    rc = _run(monkeypatch, read_gate_result, ["--session-dir", str(tmp_path), "--pkgname", "pkg"])
    assert rc == 1


def test_read_gate_result_not_done(tmp_path, monkeypatch, capsys):
    _gate(tmp_path, overall="failed")
    rc = _run(monkeypatch, read_gate_result, ["--session-dir", str(tmp_path), "--pkgname", "pkg"])
    assert rc == 1


# ─────────────────────────────────────────────
# read-gate-fields
# ─────────────────────────────────────────────

def test_read_gate_fields(tmp_path, monkeypatch, capsys):
    _gate(tmp_path)
    _run(monkeypatch, read_gate_fields, ["--session-dir", str(tmp_path), "--pkg", "pkg"])
    out = capsys.readouterr().out
    assert "LANG=python" in out
    assert "VERSION=1.0" in out
    assert "GATE_DECISION=introduce_new" in out


def test_read_gate_fields_single(tmp_path, monkeypatch, capsys):
    _gate(tmp_path)
    _run(monkeypatch, read_gate_fields, ["--session-dir", str(tmp_path), "--pkg", "pkg",
                                         "--field", "lang"])
    assert capsys.readouterr().out.strip() == "python"


def test_read_gate_fields_missing_exits_1(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        _run(monkeypatch, read_gate_fields, ["--session-dir", str(tmp_path), "--pkg", "pkg"])


# ─────────────────────────────────────────────
# read-build-result
# ─────────────────────────────────────────────

def test_read_build_result_ok(tmp_path, monkeypatch, capsys):
    pkg_dir = tmp_path / "pkgs" / "pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "build_rpm_result.json").write_text(json.dumps({"status": "success"}))
    rc = _run(monkeypatch, read_build_result, ["--session-dir", str(tmp_path), "--pkgname", "pkg"])
    assert rc == 0
    assert "BUILD_STATUS=success" in capsys.readouterr().out


def test_read_build_result_missing(tmp_path, monkeypatch, capsys):
    rc = _run(monkeypatch, read_build_result, ["--session-dir", str(tmp_path), "--pkgname", "pkg"])
    assert rc == 1
    assert "BUILD_STATUS=''" in capsys.readouterr().out


# ─────────────────────────────────────────────
# read-dep-registry
# ─────────────────────────────────────────────

def test_read_dep_registry_export(tmp_path, monkeypatch, capsys):
    (tmp_path / "dep_registry.json").write_text(json.dumps({
        "dep1": {"url": "u", "constraint": ">= 1.0", "status": "evaluate_done"},
    }))
    _run(monkeypatch, read_dep_registry, ["--session-dir", str(tmp_path), "--pkg", "dep1"])
    out = capsys.readouterr().out
    assert "export DEP_URL=u" in out
    assert "export DEP_STATUS=evaluate_done" in out


def test_read_dep_registry_field(tmp_path, monkeypatch, capsys):
    (tmp_path / "dep_registry.json").write_text(json.dumps({
        "dep1": {"status": "build_done"},
    }))
    _run(monkeypatch, read_dep_registry, ["--session-dir", str(tmp_path), "--pkg", "dep1",
                                          "--field", "status"])
    assert capsys.readouterr().out.strip() == "build_done"


def test_read_dep_registry_gav_normalized(tmp_path, monkeypatch, capsys):
    (tmp_path / "dep_registry.json").write_text(json.dumps({"guava": {"status": "build_done"}}))
    _run(monkeypatch, read_dep_registry, ["--session-dir", str(tmp_path),
                                          "--pkg", "com.google.guava:guava", "--field", "status"])
    assert capsys.readouterr().out.strip() == "build_done"


def test_read_dep_registry_chroot_fields(tmp_path, monkeypatch, capsys):
    (tmp_path / "dep_registry.json").write_text(json.dumps({
        "dep1": {"status": "copr_running",
                 "chroots": {"openeuler-24.03-x86_64": {"status": "building", "build_id": 7}}},
    }))
    _run(monkeypatch, read_dep_registry, ["--session-dir", str(tmp_path), "--pkg", "dep1"])
    out = capsys.readouterr().out
    assert "DEP_CHROOT_OPENEULER_24_03_X86_64_STATUS=building" in out
    assert "DEP_CHROOT_OPENEULER_24_03_X86_64_BUILD_ID=7" in out


def test_read_dep_registry_missing_file(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit) as e:
        _run(monkeypatch, read_dep_registry, ["--session-dir", str(tmp_path), "--pkg", "x"])
    assert e.value.code == 0


# ─────────────────────────────────────────────
# print-summary
# ─────────────────────────────────────────────

def test_print_summary_success(tmp_path, monkeypatch, capsys):
    (tmp_path / "workflow_main.json").write_text(json.dumps({
        "pkgname": "main", "goal_achieved": True,
        "built_pkgs": ["a", "b"], "reused_pkgs": ["c"], "loop_count": 5,
    }))
    _run(monkeypatch, print_summary, ["--session-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert "[main] SUCCESS" in out
    assert "built: a b" in out
    assert "reused: c" in out
    assert "loops: 5" in out


def test_print_summary_in_progress(tmp_path, monkeypatch, capsys):
    (tmp_path / "workflow_main.json").write_text(json.dumps({"pkgname": "main"}))
    _run(monkeypatch, print_summary, ["--session-dir", str(tmp_path)])
    assert "IN_PROGRESS" in capsys.readouterr().out


def test_print_summary_no_workflow(tmp_path, monkeypatch, capsys):
    _run(monkeypatch, print_summary, ["--session-dir", str(tmp_path)])
    assert "no workflow file found" in capsys.readouterr().out


# ─────────────────────────────────────────────
# mark-interrupted
# ─────────────────────────────────────────────

def test_mark_interrupted_bad_status(tmp_path, monkeypatch, capsys):
    pkg_dir = tmp_path / "pkgs" / "pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "build_rpm_result.json").write_text(json.dumps({"status": "weird"}))
    _run(monkeypatch, mark_interrupted, ["--session-dir", str(tmp_path), "--pkg", "pkg"])
    r = json.loads((pkg_dir / "build_rpm_result.json").read_text())
    assert r["status"] == "interrupted"
    assert "weird" in r["failure"]["failure_reason"]
    assert "→ interrupted" in capsys.readouterr().out


def test_mark_interrupted_valid_status_unchanged(tmp_path, monkeypatch, capsys):
    pkg_dir = tmp_path / "pkgs" / "pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "build_rpm_result.json").write_text(json.dumps({"status": "success"}))
    _run(monkeypatch, mark_interrupted, ["--session-dir", str(tmp_path), "--pkg", "pkg"])
    r = json.loads((pkg_dir / "build_rpm_result.json").read_text())
    assert r["status"] == "success"
    assert "no change" in capsys.readouterr().out
