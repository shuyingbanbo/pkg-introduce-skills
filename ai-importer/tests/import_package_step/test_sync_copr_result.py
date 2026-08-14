"""sync-copr-result.py — COPR 结果回写 build_rpm_result(三状态分支)。"""

from __future__ import annotations

import json

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sc = load_module("sync-copr-result", SCRIPT_DIRS["step"] / "sync-copr-result.py")


def _setup(tmp_path, copr=None, br=None):
    pkg_dir = tmp_path / "pkgs" / "pkg"
    pkg_dir.mkdir(parents=True)
    if copr is not None:
        (pkg_dir / "copr_build_result.json").write_text(json.dumps(copr))
    if br is not None:
        (pkg_dir / "build_rpm_result.json").write_text(json.dumps(br))
    return pkg_dir


def _run(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.argv", ["sync-copr-result.py",
                                     "--session-dir", str(tmp_path), "--pkg", "pkg"])
    return sc.main()


def _br(tmp_path):
    return json.loads((tmp_path / "pkgs" / "pkg" / "build_rpm_result.json").read_text())


def test_missing_copr_result(tmp_path, monkeypatch, capsys):
    _setup(tmp_path)
    rc = _run(monkeypatch, tmp_path)
    assert rc is None
    assert "not found" in capsys.readouterr().out


def test_success_sync(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, copr={"status": "success", "rpms": ["a.rpm", "b.rpm"], "copr_build_id": 5})
    _run(monkeypatch, tmp_path)
    br = _br(tmp_path)
    assert br["status"] == "success"
    assert br["rpms"] == ["a.rpm", "b.rpm"]
    assert br["copr_build_id"] == 5


def test_copr_running_sync(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, copr={"status": "copr_running", "copr_build_id": 9})
    _run(monkeypatch, tmp_path)
    br = _br(tmp_path)
    assert br["status"] == "copr_running"
    assert br["copr_build_id"] == 9


def test_failed_sync_with_log_tail(tmp_path, monkeypatch, capsys, fake_subprocess):
    long_log = "x" * 5000
    _setup(tmp_path, copr={"status": "failed", "failure_reason": "compile error",
                           "build_log": long_log, "copr_build_id": 3})
    fake_subprocess.when(lambda s: "extract-build-failure.py" in s, returncode=0)
    _run(monkeypatch, tmp_path)
    br = _br(tmp_path)
    assert br["status"] == "failed"
    assert br["failure_reason"] == "compile error"
    # build_log_tail 截断 2000
    assert len(br["build_log_tail"]) == 2000
    assert br["build_log_tail"] == long_log[-2000:]
    # 失败时触发 extract-build-failure
    assert fake_subprocess.called_with("extract-build-failure.py")


def test_failed_default_reason(tmp_path, monkeypatch, capsys, fake_subprocess):
    _setup(tmp_path, copr={"status": "failed"})
    fake_subprocess.when(lambda s: "extract-build-failure.py" in s, returncode=0)
    _run(monkeypatch, tmp_path)
    assert _br(tmp_path)["failure_reason"] == "copr build failed"


def test_chroot_fields_passthrough(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, copr={"status": "success",
                           "copr_chroots": ["a"], "copr_build_ids": {"a": 1},
                           "copr_chroot": "a"})
    _run(monkeypatch, tmp_path)
    br = _br(tmp_path)
    assert br["copr_chroots"] == ["a"]
    assert br["copr_build_ids"] == {"a": 1}
    assert br["copr_chroot"] == "a"
