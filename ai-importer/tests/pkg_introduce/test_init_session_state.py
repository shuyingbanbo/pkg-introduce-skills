"""init_session_state.py — 会话状态初始化(目录重置 + 残留警告 + 状态脚本调用)。"""

from __future__ import annotations

import sys

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

iss = load_module("init_session_state", SCRIPT_DIRS["pkg_introduce"] / "init_session_state.py")


def test_reset_directory_recreates(tmp_path):
    d = tmp_path / "x"
    d.mkdir()
    (d / "f.txt").write_text("x")
    iss.reset_directory(d)
    assert d.is_dir()
    assert list(d.iterdir()) == []


def test_initialize_creates_files(tmp_path, fake_subprocess):
    fake_subprocess.when(lambda s: "dependency_resolution_state.py" in s, returncode=0)
    rc = iss.initialize_session_state(tmp_path / "bs", tmp_path / "rp", tmp_path / "src")
    assert rc == 0
    assert (tmp_path / "bs" / "building.txt").exists()
    assert (tmp_path / "bs" / "introduced.txt").exists()
    assert (tmp_path / "rp").is_dir()
    assert (tmp_path / "src").is_dir()
    # 状态脚本被调用(init 子命令)
    cmd = next(c for c, _ in fake_subprocess.calls if "dependency_resolution_state.py" in " ".join(c))
    assert "init" in cmd


def test_initialize_warns_on_building_residual(tmp_path, fake_subprocess, capsys):
    fake_subprocess.when(lambda s: "dependency_resolution_state.py" in s, returncode=0)
    bs = tmp_path / "bs"
    bs.mkdir()
    (bs / "building.txt").write_text("pkg-a\npkg-b\n")
    iss.initialize_session_state(bs, tmp_path / "rp", tmp_path / "src")
    err = capsys.readouterr().out
    assert "building.txt 有残留" in err
    assert "pkg-a" in err


def test_initialize_subprocess_failure_rc(tmp_path, fake_subprocess):
    fake_subprocess.when(lambda s: "dependency_resolution_state.py" in s, returncode=7)
    rc = iss.initialize_session_state(tmp_path / "bs", tmp_path / "rp", tmp_path / "src")
    assert rc == 7


def test_main(tmp_path, monkeypatch, fake_subprocess):
    fake_subprocess.when(lambda s: "dependency_resolution_state.py" in s, returncode=0)
    monkeypatch.setattr("sys.argv", ["init_session_state.py",
                                     "--build-state-dir", str(tmp_path / "bs")])
    rc = iss.main()
    assert rc == 0
    assert (tmp_path / "bs" / "building.txt").exists()
