"""sync_rpms_to_repo.py 单元测试 — 容器 RPM 同步到归档仓 dist/ + repodata 更新。

docker cp(容器→本地)通过 docker_cp_real fixture 真实落盘,
使 synced 集合差值逻辑可被断言;其余 subprocess 走 fake。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import SCRIPT_DIRS, load_module
from tests.archive.helpers import docker_cp_real_run

sys.path.insert(0, str(SCRIPT_DIRS["archive"]))
s = load_module("sync_rpms_to_repo",
                SCRIPT_DIRS["archive"] / "sync_rpms_to_repo.py")

CTR = "ctr"


@pytest.fixture
def docker_cp_real(fake_subprocess, monkeypatch):
    """docker cp(容器→本地)真实落盘,其余 subprocess 走 fake_subprocess。"""
    monkeypatch.setattr(subprocess, "run", docker_cp_real_run(fake_subprocess.run))
    return fake_subprocess


# ─────────────────────────────────────────────
# normalize_name_token
# ─────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("Python3-Requests", "python3_requests"),
    ("a..b--c", "a_b_c"),
    ("A_B.c-d", "a_b_c_d"),
])
def test_normalize_name_token(value, expected):
    assert s.normalize_name_token(value) == expected


# ─────────────────────────────────────────────
# sync_rpms
# ─────────────────────────────────────────────

def test_sync_rpms_happy(tmp_path, docker_cp_real):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake = docker_cp_real
    fake.when("docker exec ctr cat /root/rpmbuild/SPECS/python3-foo.spec", returncode=1)
    fake.when(lambda c: "find /root/rpmbuild/RPMS" in c, stdout=(
        "/root/rpmbuild/RPMS/python3-foo-1.0-1.noarch.rpm\n"
        "/root/rpmbuild/RPMS/libbar-1.0-1.x86_64.rpm\n"))
    fake.when(lambda c: "stat -c" in c, stdout="100")

    synced = s.sync_rpms("python3-foo", CTR, repo)
    # python3- 前缀剥掉后按 foo 匹配;libbar 不误匹配
    assert synced == [repo / "dist" / "python3-foo-1.0-1.noarch.rpm"]
    assert (repo / "dist" / "python3-foo-1.0-1.noarch.rpm").exists()
    assert not (repo / "dist" / "libbar-1.0-1.x86_64.rpm").exists()


def test_sync_rpms_subpackage_matched(tmp_path, docker_cp_real):
    # %package -n 子包名与主包名无关,只有扩展名列表能匹配
    repo = tmp_path / "repo"
    repo.mkdir()
    fake = docker_cp_real
    fake.when("docker exec ctr cat /root/rpmbuild/SPECS/foo.spec",
              stdout="%package -n bar-devel\n")
    fake.when(lambda c: "find /root/rpmbuild/RPMS" in c,
              stdout="/root/rpmbuild/RPMS/bar-devel-1.0-1.x86_64.rpm\n")
    fake.when(lambda c: "stat -c" in c, stdout="100")
    assert s.sync_rpms("foo", CTR, repo) == [repo / "dist" / "bar-devel-1.0-1.x86_64.rpm"]


def test_sync_rpms_oversize_skipped(tmp_path, docker_cp_real, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake = docker_cp_real
    fake.when("docker exec ctr cat /root/rpmbuild/SPECS/foo.spec", returncode=1)
    fake.when(lambda c: "find /root/rpmbuild/RPMS" in c,
              stdout="/root/rpmbuild/RPMS/foo-1.0-1.x86_64.rpm\n")
    fake.when(lambda c: "stat -c" in c, stdout=str(101 * 1024 * 1024))
    assert s.sync_rpms("foo", CTR, repo) == []
    assert not (repo / "dist" / "foo-1.0-1.x86_64.rpm").exists()
    assert ">100MB" in capsys.readouterr().out


def test_sync_rpms_stat_fail_still_copied(tmp_path, docker_cp_real):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake = docker_cp_real
    fake.when("docker exec ctr cat /root/rpmbuild/SPECS/foo.spec", returncode=1)
    fake.when(lambda c: "find /root/rpmbuild/RPMS" in c,
              stdout="/root/rpmbuild/RPMS/foo-1.0-1.x86_64.rpm\n")
    fake.when(lambda c: "stat -c" in c, returncode=1)  # stat 失败 → 不跳过
    assert s.sync_rpms("foo", CTR, repo) == [repo / "dist" / "foo-1.0-1.x86_64.rpm"]


def test_sync_rpms_empty_find(tmp_path, docker_cp_real):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake = docker_cp_real
    fake.when("docker exec ctr cat /root/rpmbuild/SPECS/foo.spec", returncode=1)
    fake.when(lambda c: "find /root/rpmbuild/RPMS" in c, stdout="")
    assert s.sync_rpms("foo", CTR, repo) == []


def test_sync_rpms_preexisting_not_in_synced(tmp_path, docker_cp_real):
    repo = tmp_path / "repo"
    (repo / "dist").mkdir(parents=True)
    (repo / "dist" / "foo-1.0-1.x86_64.rpm").touch()
    fake = docker_cp_real
    fake.when("docker exec ctr cat /root/rpmbuild/SPECS/foo.spec", returncode=1)
    fake.when(lambda c: "find /root/rpmbuild/RPMS" in c,
              stdout="/root/rpmbuild/RPMS/foo-1.0-1.x86_64.rpm\n")
    fake.when(lambda c: "stat -c" in c, stdout="100")
    assert s.sync_rpms("foo", CTR, repo) == []  # 集合差值:同名旧文件不算新增


# ─────────────────────────────────────────────
# update_repodata
# ─────────────────────────────────────────────

def test_update_repodata_missing_tool(tmp_path, fake_subprocess, capsys):
    fake_subprocess.when("which createrepo_c", returncode=1)
    assert s.update_repodata(tmp_path) is None  # 警告后跳过,不退出
    assert "createrepo_c 未安装" in capsys.readouterr().err


def test_update_repodata_failure_exits(tmp_path, fake_subprocess):
    fake_subprocess.when("which createrepo_c", returncode=0)
    fake_subprocess.when("createrepo_c", returncode=1, stderr="boom")
    with pytest.raises(SystemExit) as ei:
        s.update_repodata(tmp_path)
    assert ei.value.code == 1


def test_update_repodata_ok(tmp_path, fake_subprocess):
    fake_subprocess.when("which createrepo_c", returncode=0)
    fake_subprocess.when("createrepo_c", returncode=0, stdout="Done")
    s.update_repodata(tmp_path)
    assert fake_subprocess.called_with("createrepo_c --update")


# ─────────────────────────────────────────────
# main
# ─────────────────────────────────────────────

def test_main_happy(tmp_path, docker_cp_real, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake = docker_cp_real
    fake.when("docker exec ctr cat /root/rpmbuild/SPECS/python3-foo.spec", returncode=1)
    fake.when(lambda c: "find /root/rpmbuild/RPMS" in c,
              stdout="/root/rpmbuild/RPMS/python3-foo-1.0-1.noarch.rpm\n")
    fake.when(lambda c: "stat -c" in c, stdout="100")
    fake.when("which createrepo_c", returncode=0)
    fake.when("createrepo_c", returncode=0)
    monkeypatch.setattr(sys, "argv", ["sync_rpms_to_repo.py", "--pkg", "python3-foo",
                                      "--container", CTR, "--repo-local", str(repo)])
    assert s.main() == 0
    assert "新增 RPM 数量: 1" in capsys.readouterr().out


def test_main_missing_args(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["sync_rpms_to_repo.py"])
    with pytest.raises(SystemExit) as ei:
        s.main()
    assert ei.value.code == 2
