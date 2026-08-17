"""verify-fix.py — fixer 提交前验证关口(四道校验,全 subprocess mock)。"""

from __future__ import annotations

import json

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

vf = load_module("verify-fix", SCRIPT_DIRS["step"] / "verify-fix.py")


def _pkg(tmp_path, spec_text="Name: git\nVersion: 1.0\n"):
    pkg_dir = tmp_path / "pkgs" / "git"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "git.spec").write_text(spec_text)
    return pkg_dir


def _main(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["verify-fix.py"] + argv)
    return vf.main()


def test_missing_spec(tmp_path, monkeypatch, capsys):
    rc = _main(monkeypatch, ["--session-dir", str(tmp_path), "--pkg", "git"])
    assert rc == 1
    assert "spec 不存在" in capsys.readouterr().err


def test_no_snapshot_first_fix_passes(tmp_path, monkeypatch, capsys):
    _pkg(tmp_path)
    rc = _main(monkeypatch, ["--session-dir", str(tmp_path), "--pkg", "git"])
    assert rc == 0
    assert "PASS" in capsys.readouterr().out


def test_no_diff_with_snapshot_rejected(tmp_path, monkeypatch, capsys):
    pkg_dir = _pkg(tmp_path, "Name: git\nVersion: 1.0\n")
    snap_dir = pkg_dir / "submitted_specs"
    snap_dir.mkdir()
    (snap_dir / "spec_001.spec").write_text("Name: git\nVersion: 1.0\n")  # 与当前相同
    rc = _main(monkeypatch, ["--session-dir", str(tmp_path), "--pkg", "git"])
    assert rc == 1
    assert "无 diff" in capsys.readouterr().err


def test_diff_with_snapshot_passes(tmp_path, monkeypatch, capsys):
    pkg_dir = _pkg(tmp_path, "Name: git\nVersion: 2.0\n")  # 快照是 1.0
    snap_dir = pkg_dir / "submitted_specs"
    snap_dir.mkdir()
    (snap_dir / "spec_001.spec").write_text("Name: git\nVersion: 1.0\n")
    rc = _main(monkeypatch, ["--session-dir", str(tmp_path), "--pkg", "git"])
    assert rc == 0


def test_fix_report_changes_not_applied(tmp_path, monkeypatch, capsys):
    pkg_dir = _pkg(tmp_path)
    report = tmp_path / "fix_report.json"
    report.write_text(json.dumps([{"description": "改 License", "after": "License: MIT"}]))
    rc = _main(monkeypatch, ["--session-dir", str(tmp_path), "--pkg", "git",
                             "--report", str(report)])
    assert rc == 2
    assert "自报改动未落地" in capsys.readouterr().err


def test_fix_report_changes_applied(tmp_path, monkeypatch, capsys):
    _pkg(tmp_path, "Name: git\nLicense: MIT\n")
    report = tmp_path / "fix_report.json"
    report.write_text(json.dumps([{"description": "改 License", "after": "License: MIT"}]))
    rc = _main(monkeypatch, ["--session-dir", str(tmp_path), "--pkg", "git",
                             "--report", str(report)])
    assert rc == 0


def test_fix_report_dict_format(tmp_path, monkeypatch, capsys):
    """report 是 {"changes": [...]} dict 形式也支持。"""
    pkg_dir = _pkg(tmp_path)
    report = tmp_path / "fix_report.json"
    report.write_text(json.dumps({"changes": [{"after": "License: MIT"}]}))
    rc = _main(monkeypatch, ["--session-dir", str(tmp_path), "--pkg", "git",
                             "--report", str(report)])
    assert rc == 2


def test_rpmlint_errors_block(tmp_path, monkeypatch, capsys, fake_subprocess):
    """rpmlint 存在且报 E: 错误 → 退出 3。"""
    _pkg(tmp_path)
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/rpmlint" if name == "rpmlint" else None)
    fake_subprocess.when("rpmlint", stdout="git.spec:7: E: hardcoded-library-path\n")
    rc = _main(monkeypatch, ["--session-dir", str(tmp_path), "--pkg", "git"])
    assert rc == 3
    assert "rpmlint 报错" in capsys.readouterr().err


def test_rpmlint_missing_skipped(tmp_path, monkeypatch, capsys):
    _pkg(tmp_path)
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)
    rc = _main(monkeypatch, ["--session-dir", str(tmp_path), "--pkg", "git"])
    assert rc == 0
    assert "rpmlint 不存在" in capsys.readouterr().err


def test_prep_verification_failure(tmp_path, monkeypatch, capsys, fake_subprocess):
    """--build-dir 且 rpmbuild --nobuild 失败 → 退出 4。"""
    _pkg(tmp_path)
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)  # 跳过 rpmlint
    build_dir = tmp_path / "build"
    (build_dir / "SPECS").mkdir(parents=True)
    (build_dir / "SPECS" / "git.spec").write_text("Name: git\n")
    fake_subprocess.when("rpmbuild", returncode=1, stderr="error: %prep failed")
    rc = _main(monkeypatch, ["--session-dir", str(tmp_path), "--pkg", "git",
                             "--build-dir", str(build_dir)])
    assert rc == 4
    assert "rpmbuild --nobuild 未通过" in capsys.readouterr().err
