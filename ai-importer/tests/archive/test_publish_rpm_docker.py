"""publish_rpm.py 单元测试 — git 重试状态机 / docker 编排 / CI 门禁 / main 主流程。

copy_pkg_files 的 docker cp(容器→本地)通过 docker_cp_real fixture 真实落盘,
使集合差值(new_dist_rpms)等文件系统逻辑可被断言;其余 subprocess 走 fake。
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.conftest import SCRIPT_DIRS, load_module
from tests.archive.helpers import docker_cp_real_run

p = load_module("publish_rpm", SCRIPT_DIRS["archive"] / "publish_rpm.py")

CTR = "ctr"


@pytest.fixture
def docker_cp_real(fake_subprocess, monkeypatch):
    """docker cp(容器→本地)真实落盘,其余 subprocess 走 fake_subprocess。"""
    monkeypatch.setattr(subprocess, "run", docker_cp_real_run(fake_subprocess.run))
    return fake_subprocess


@pytest.fixture
def no_sleep(monkeypatch):
    """git_commit_and_push 内部的 time.sleep / random.randint 打桩,避免真实等待。"""
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(random, "randint", lambda a, b: 1)


def _join(cmd) -> str:
    return " ".join(c if isinstance(c, str) else str(c) for c in cmd)


# ─────────────────────────────────────────────
# git_commit_and_push 重试状态机
# ─────────────────────────────────────────────

def test_git_push_no_changes(tmp_path, fake_subprocess):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_subprocess.when("git status --porcelain", stdout="")
    assert p.git_commit_and_push(str(repo), "main", "https://o/r.git", "msg") is None
    assert not fake_subprocess.called_with("git commit")


def test_git_push_first_try(tmp_path, fake_subprocess, no_sleep, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_subprocess.when("git status --porcelain", stdout=" M x.spec")
    fake_subprocess.when("git push", returncode=0)
    p.git_commit_and_push(str(repo), "main", "https://o/r.git", "msg")
    assert fake_subprocess.called_with("git commit -m msg")
    assert fake_subprocess.called_with("HEAD:main --set-upstream")
    assert not fake_subprocess.called_with("git pull --rebase")
    assert "推送成功（第 1 次尝试）" in capsys.readouterr().out


def test_git_push_retry_succeeds(tmp_path, fake_subprocess, no_sleep, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_subprocess.when("git status --porcelain", stdout=" M x.spec")
    state = {"n": 0}

    def first_push_fails(s):
        if not s.startswith("git push"):
            return False
        state["n"] += 1
        return state["n"] == 1

    fake_subprocess.when(first_push_fails, returncode=1, stderr="rejected")
    fake_subprocess.when("git push", returncode=0)
    fake_subprocess.when("git pull --rebase", returncode=0)
    p.git_commit_and_push(str(repo), "main", "https://o/r.git", "msg")
    assert state["n"] == 2
    assert "推送成功（第 2 次尝试）" in capsys.readouterr().out


def test_git_push_rebase_conflict(tmp_path, fake_subprocess, no_sleep):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_subprocess.when("git status --porcelain", stdout=" M x.spec")
    fake_subprocess.when("git push", returncode=1, stderr="non-fast-forward")
    fake_subprocess.when("git pull --rebase", returncode=1, stderr="CONFLICT")
    fake_subprocess.when("git diff --name-only", stdout="foo.spec")
    fake_subprocess.when("git status", stdout="UU foo.spec")
    with pytest.raises(RuntimeError, match="rebase 失败") as ei:
        p.git_commit_and_push(str(repo), "main", "https://o/r.git", "msg")
    assert "foo.spec" in str(ei.value)
    assert "CONFLICT" in str(ei.value)
    assert fake_subprocess.called_with("git rebase --abort")


def test_git_push_exhausted_retries(tmp_path, fake_subprocess, no_sleep):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_subprocess.when("git status --porcelain", stdout=" M x.spec")
    fake_subprocess.when("git push", returncode=1, stderr="non-fast-forward")
    fake_subprocess.when("git pull --rebase", returncode=0)
    fake_subprocess.when("git log --oneline", stdout="abc123 add foo")
    with pytest.raises(RuntimeError, match="已重试 5 次") as ei:
        p.git_commit_and_push(str(repo), "main", "https://o/r.git", "msg")
    assert "non-fast-forward" in str(ei.value)
    assert "abc123" in str(ei.value)


def test_git_reset_working_tree(tmp_path, fake_subprocess):
    p.git_reset_working_tree(str(tmp_path))
    assert fake_subprocess.called_with("git checkout -- .")
    assert fake_subprocess.called_with("git clean -fd")


# ─────────────────────────────────────────────
# copy_pkg_files
# ─────────────────────────────────────────────

def test_copy_pkg_files_happy_path(tmp_path, docker_cp_real):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake = docker_cp_real
    fake.when(lambda s: "ls /root/rpmbuild/SOURCES" in s,
              stdout="foo-2.0.tar.gz\nfoo-2.0.whl\nother-1.0.tar.gz\n")
    fake.when(lambda s: "find /root/rpmbuild/RPMS" in s,
              stdout="/root/rpmbuild/RPMS/foo-1.0-1.noarch.rpm\n"
                     "/root/rpmbuild/RPMS/libbar-1.0-1.x86_64.rpm\n")
    fake.when(lambda s: "stat -c" in s, stdout="12345")

    copied, new_rpms = p.copy_pkg_files(CTR, "foo", str(repo))

    assert copied == ["foo/foo.spec", "foo/foo-2.0.tar.gz",
                      "dist/foo-1.0-1.noarch.rpm"]
    assert new_rpms == [repo / "dist" / "foo-1.0-1.noarch.rpm"]
    assert (repo / "foo" / "foo.spec").exists()
    assert (repo / "foo" / "foo-2.0.tar.gz").exists()
    assert not (repo / "foo" / "foo-2.0.whl").exists()          # .whl 跳过
    assert not (repo / "foo" / "other-1.0.tar.gz").exists()     # 前缀不匹配跳过
    assert not (repo / "dist" / "libbar-1.0-1.x86_64.rpm").exists()  # 短包名不误匹配


def test_copy_pkg_files_spec_fallback_candidates(tmp_path, docker_cp_real):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake = docker_cp_real
    fake.when("docker exec ctr test -f /root/rpmbuild/SPECS/tabulate.spec", returncode=1)
    fake.when("docker exec ctr test -f /root/rpmbuild/SPECS/nodejs-tabulate.spec",
              returncode=1)
    fake.when("docker exec ctr test -f /root/rpmbuild/SPECS/python-tabulate.spec",
              returncode=1)
    # 第 4 个候选 python3-tabulate.spec 未注册规则 → 默认 rc=0,视为存在
    copied, _ = p.copy_pkg_files(CTR, "tabulate", str(repo))
    assert copied[0] == "tabulate/tabulate.spec"
    assert (repo / "tabulate" / "tabulate.spec").exists()
    assert fake.called_with("docker cp ctr:/root/rpmbuild/SPECS/python3-tabulate.spec")


def test_copy_pkg_files_spec_missing(tmp_path, docker_cp_real, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake = docker_cp_real
    fake.when(lambda s: "test -f" in s, returncode=1)
    copied, new_rpms = p.copy_pkg_files(CTR, "foo", str(repo))
    assert copied == []
    assert new_rpms == []
    assert "spec 文件不存在" in capsys.readouterr().out


def test_copy_pkg_files_old_tarball_cleanup(tmp_path, docker_cp_real):
    repo = tmp_path / "repo"
    repo.mkdir()
    pkg_dir = repo / "foo"
    pkg_dir.mkdir()
    (pkg_dir / "foo-1.0.tar.gz").touch()    # 旧版本,将被清理
    (pkg_dir / "keep-1.0.tar.gz").touch()   # 前缀不匹配,保留
    fake = docker_cp_real
    fake.when(lambda s: "test -f" in s, returncode=1)
    fake.when(lambda s: "ls /root/rpmbuild/SOURCES" in s, stdout="foo-2.0.tar.gz\n")
    fake.when(lambda s: "stat -c" in s, stdout="100")
    copied, _ = p.copy_pkg_files(CTR, "foo", str(repo))
    assert not (pkg_dir / "foo-1.0.tar.gz").exists()
    assert (pkg_dir / "keep-1.0.tar.gz").exists()
    assert (pkg_dir / "foo-2.0.tar.gz").exists()
    assert "foo/foo-2.0.tar.gz" in copied


def test_copy_pkg_files_oversize_skipped(tmp_path, docker_cp_real, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake = docker_cp_real
    fake.when(lambda s: "test -f" in s, returncode=1)
    fake.when(lambda s: "ls /root/rpmbuild/SOURCES" in s, stdout="foo-big.tar.gz\n")
    fake.when(lambda s: "find /root/rpmbuild/RPMS" in s,
              stdout="/root/rpmbuild/RPMS/foo-big-1.0-1.x86_64.rpm\n")
    fake.when(lambda s: "stat -c" in s, stdout=str(101 * 1024 * 1024))
    copied, new_rpms = p.copy_pkg_files(CTR, "foo", str(repo))
    assert copied == []
    assert new_rpms == []
    assert "100MB" in capsys.readouterr().out


def test_copy_pkg_files_subpackages_matched(tmp_path, docker_cp_real):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake = docker_cp_real
    fake.when(lambda s: "test -f" in s, returncode=1)
    fake.when("docker exec ctr cat /root/rpmbuild/SPECS/foo.spec",
              stdout="Name: foo\n%package -n foo-devel\n%package -n foo-utils\n")
    fake.when(lambda s: "find /root/rpmbuild/RPMS" in s,
              stdout="/root/rpmbuild/RPMS/foo-devel-1.0-1.x86_64.rpm\n")
    fake.when(lambda s: "stat -c" in s, stdout="10")
    copied, new_rpms = p.copy_pkg_files(CTR, "foo", str(repo))
    assert copied == ["dist/foo-devel-1.0-1.x86_64.rpm"]
    assert new_rpms == [repo / "dist" / "foo-devel-1.0-1.x86_64.rpm"]


def test_copy_pkg_files_source0_prefix(tmp_path, docker_cp_real):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake = docker_cp_real
    fake.when(lambda s: "test -f" in s, returncode=1)
    fake.when("docker exec ctr cat /root/rpmbuild/SPECS/foo.spec",
              stdout="%global srcname foo_src\nSource0: %{srcname}-%{version}.tar.gz\n")
    fake.when(lambda s: "ls /root/rpmbuild/SOURCES" in s,
              stdout="foo_src-1.0.tar.gz\nfoo-1.0.tar.gz\n")
    fake.when(lambda s: "stat -c" in s, stdout="10")
    copied, _ = p.copy_pkg_files(CTR, "foo", str(repo))
    assert "foo/foo_src-1.0.tar.gz" in copied
    assert "foo/foo-1.0.tar.gz" not in copied


# ─────────────────────────────────────────────
# find_rpm_dependents
# ─────────────────────────────────────────────

@pytest.mark.parametrize("rpms,stdout,expected", [
    ([], "", []),  # dist 无 RPM → 直接返回,不发 docker 命令
    (["a-1.0-1.noarch.rpm"], "a-1.0-1.noarch.rpm\n\nother\n",
     ["a-1.0-1.noarch.rpm", "other"]),
])
def test_find_rpm_dependents(tmp_path, fake_subprocess, rpms, stdout, expected):
    dist = tmp_path / "dist"
    dist.mkdir()
    for r in rpms:
        (dist / r).touch()
    if stdout:
        fake_subprocess.when(lambda s: "for f in /tmp/_dep_check" in s, stdout=stdout)
    assert p.find_rpm_dependents("foo", dist, CTR) == expected
    if not rpms:
        assert fake_subprocess.calls == []


# ─────────────────────────────────────────────
# create_compat_via_rpmrebuild
# ─────────────────────────────────────────────

def test_create_compat_already_exists(tmp_path, fake_subprocess):
    dist = tmp_path / "dist"
    dist.mkdir()
    old = dist / "foo-1.0-1.x86_64.rpm"
    old.touch()
    (dist / "foo-1-1.0-1.x86_64.rpm").touch()  # compat 包已存在
    assert p.create_compat_via_rpmrebuild(
        "foo", old, {"version": "1.0", "release": "1"}, dist, CTR) is True
    assert fake_subprocess.calls == []  # 直接跳过,无 docker 调用


def test_create_compat_rpmrebuild_unavailable(tmp_path, fake_subprocess):
    dist = tmp_path / "dist"
    dist.mkdir()
    old = dist / "foo-1.0-1.x86_64.rpm"
    old.touch()
    fake_subprocess.when("docker exec ctr dnf install", returncode=1)
    fake_subprocess.when("docker exec ctr which rpmrebuild", returncode=1)
    assert p.create_compat_via_rpmrebuild(
        "foo", old, {"version": "1.0", "release": "1"}, dist, CTR) is False


def test_create_compat_rpmrebuild_success(tmp_path, docker_cp_real):
    dist = tmp_path / "dist"
    dist.mkdir()
    old = dist / "foo-1.0-1.x86_64.rpm"
    old.touch()
    fake = docker_cp_real
    fake.when("docker exec ctr dnf install", returncode=1)          # 安装失败...
    fake.when("docker exec ctr which rpmrebuild", returncode=0)     # ...但已存在
    fake.when("docker exec ctr rpmrebuild", returncode=0)
    fake.when(lambda s: "find /tmp/_compat_build/output" in s,
              stdout="/tmp/_compat_build/output/foo-1-1.0-1.x86_64.rpm\n")
    assert p.create_compat_via_rpmrebuild(
        "foo", old, {"version": "1.0", "release": "1"}, dist, CTR) is True
    assert (dist / "foo-1-1.0-1.x86_64.rpm").exists()
    # patch.py 内容包含改名 + Provides 注入
    for cmd, kw in fake.calls:
        if "cat > /tmp/_compat_build/patch.py" in _join(cmd):
            assert "Name:" in kw.get("input", "")
            assert "Provides: foo = 1.0-1" in kw["input"]


def test_create_compat_rpmrebuild_fails(tmp_path, fake_subprocess):
    dist = tmp_path / "dist"
    dist.mkdir()
    old = dist / "foo-1.0-1.x86_64.rpm"
    old.touch()
    fake_subprocess.when("docker exec ctr rpmrebuild", returncode=1, stdout="traceback")
    assert p.create_compat_via_rpmrebuild(
        "foo", old, {"version": "1.0", "release": "1"}, dist, CTR) is False
    assert not (dist / "foo-1-1.0-1.x86_64.rpm").exists()


# ─────────────────────────────────────────────
# update_repodata
# ─────────────────────────────────────────────

@pytest.mark.parametrize("which_rc,createrepo_rc,stderr,match", [
    (1, None, "", "createrepo_c 未安装"),
    (0, 1, "boom", "createrepo_c 执行失败"),
])
def test_update_repodata_failures(tmp_path, fake_subprocess, which_rc,
                                  createrepo_rc, stderr, match):
    fake_subprocess.when("which createrepo_c", returncode=which_rc)
    if createrepo_rc is not None:
        fake_subprocess.when("createrepo_c", returncode=createrepo_rc, stderr=stderr)
    with pytest.raises(RuntimeError, match=match):
        p.update_repodata(tmp_path)


def test_update_repodata_ok(tmp_path, fake_subprocess, capsys):
    fake_subprocess.when("which createrepo_c", returncode=0)
    fake_subprocess.when("createrepo_c", returncode=0, stdout="Done")
    p.update_repodata(tmp_path)
    assert fake_subprocess.called_with("createrepo_c --update")
    assert "Done" in capsys.readouterr().out


# ─────────────────────────────────────────────
# run_ci_gate(repoclosure 部分)
# ─────────────────────────────────────────────

def test_ci_gate_repoclosure_unavailable(tmp_path, fake_subprocess, capsys):
    fake_subprocess.when("docker exec ctr bash -c which repoclosure", returncode=1)
    dist = tmp_path / "dist"
    dist.mkdir()
    p.run_ci_gate(dist, CTR)
    assert "跳过运行时依赖检查" in capsys.readouterr().out
    assert fake_subprocess.called_with("dnf-utils")
    assert not fake_subprocess.called_with("repoclosure --repofrompath")


def test_ci_gate_repoclosure_pass_and_pkg_extraction(tmp_path, fake_subprocess, capsys):
    fake_subprocess.when("docker exec ctr bash -c which repoclosure", returncode=0)
    fake_subprocess.when(lambda s: "dnf repolist" in s, stdout="OS\neverything\n")
    fake_subprocess.when(lambda s: "repoclosure" in s, returncode=0)
    new_rpms = [Path("python3-foo-1.0-1.noarch.rpm"), Path("weird.rpm")]
    dist = tmp_path / "dist"
    dist.mkdir()
    p.run_ci_gate(dist, CTR, new_rpms=new_rpms)

    repocall = None
    for cmd, _ in fake_subprocess.calls:
        joined = _join(cmd)
        if "repoclosure" in joined and "--repofrompath" in joined:
            repocall = joined
    assert repocall, "未找到 repoclosure 调用"
    assert "--pkg python3-foo" in repocall   # 从文件名正则提取包名
    assert "--pkg weird" in repocall         # 无法解析时回退为去 .rpm 后缀名
    assert "--enablerepo=ci-local" in repocall
    assert "--enablerepo=OS" in repocall
    assert "--enablerepo=everything" in repocall
    assert "--enablerepo=EPOL" not in repocall       # 不可用 repo 不启用
    assert "--enablerepo=EPOL-update" not in repocall
    assert "--newest" in repocall
    assert "运行时依赖检查通过" in capsys.readouterr().out


def test_ci_gate_repoclosure_fail(tmp_path, fake_subprocess):
    fake_subprocess.when("docker exec ctr bash -c which repoclosure", returncode=0)
    fake_subprocess.when(lambda s: "repoclosure" in s, returncode=1,
                         stdout="out", stderr="Missing requires: libfoo")
    dist = tmp_path / "dist"
    dist.mkdir()
    with pytest.raises(RuntimeError, match="运行时依赖检查失败") as ei:
        p.run_ci_gate(dist, CTR)
    assert "Missing requires: libfoo" in str(ei.value)


# ─────────────────────────────────────────────
# run_ci_gate(dnf builddep 部分)
# ─────────────────────────────────────────────

def _repo_with_spec(tmp_path, pkg):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / pkg).mkdir()
    (repo / pkg / f"{pkg}.spec").write_text("BuildRequires: libfoo-devel\n")
    return repo


def test_ci_builddep_pass(tmp_path, fake_subprocess, capsys):
    repo = _repo_with_spec(tmp_path, "foo")
    fake_subprocess.when("docker exec ctr bash -c which repoclosure", returncode=1)
    p.run_ci_gate(tmp_path / "dist", CTR, repo_dir=str(repo), pkgs=["foo"])
    assert fake_subprocess.called_with("dnf builddep --assumeno")
    assert "编译期依赖检查通过" in capsys.readouterr().out


def test_ci_builddep_skip_missing_spec(tmp_path, fake_subprocess, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_subprocess.when("docker exec ctr bash -c which repoclosure", returncode=1)
    p.run_ci_gate(tmp_path / "dist", CTR, repo_dir=str(repo), pkgs=["nospec"])
    assert not fake_subprocess.called_with("dnf builddep")
    assert "spec 不存在" in capsys.readouterr().out


def test_ci_builddep_dep_failure(tmp_path, fake_subprocess):
    repo = _repo_with_spec(tmp_path, "foo")
    fake_subprocess.when("docker exec ctr bash -c which repoclosure", returncode=1)
    fake_subprocess.when(lambda s: "dnf builddep" in s,
                         stdout="Error: No match for argument: libfoo-devel")
    with pytest.raises(RuntimeError, match="编译期依赖检查失败") as ei:
        p.run_ci_gate(tmp_path / "dist", CTR, repo_dir=str(repo), pkgs=["foo"])
    assert "No match" in str(ei.value)


def test_ci_builddep_error_but_no_match(tmp_path, fake_subprocess):
    # 生产代码语义:dnf builddep --assumeno 依赖可满足时也非零退出,
    # 只有 "Error:" + "could not be found"/"No match" 才算依赖失败。
    repo = _repo_with_spec(tmp_path, "foo")
    fake_subprocess.when("docker exec ctr bash -c which repoclosure", returncode=1)
    fake_subprocess.when(lambda s: "dnf builddep" in s,
                         stdout="Error: Operation aborted.", returncode=1)
    p.run_ci_gate(tmp_path / "dist", CTR, repo_dir=str(repo), pkgs=["foo"])  # 不抛异常


# ─────────────────────────────────────────────
# init_or_update_repo
# ─────────────────────────────────────────────

def test_init_or_update_repo_pull(tmp_path, fake_subprocess):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    p.init_or_update_repo(str(repo), "https://o/r.git", "main")
    assert fake_subprocess.called_with("git pull origin main")
    assert not fake_subprocess.called_with("git clone")


def test_init_or_update_repo_clone_ok(tmp_path, fake_subprocess):
    repo = tmp_path / "repo"
    fake_subprocess.when("git clone", returncode=0)
    p.init_or_update_repo(str(repo), "https://o/r.git", "main")
    assert fake_subprocess.called_with("git clone --branch main https://o/r.git")
    assert not fake_subprocess.called_with("git init")


def test_init_or_update_repo_empty_remote(tmp_path, fake_subprocess):
    repo = tmp_path / "repo"
    fake_subprocess.when("git clone", returncode=1, stderr="repository empty")
    p.init_or_update_repo(str(repo), "https://o/r.git", "main")
    assert repo.is_dir()
    assert fake_subprocess.called_with("git init")
    assert fake_subprocess.called_with("git checkout -b main")
    assert fake_subprocess.called_with("git remote add origin https://o/r.git")


# ─────────────────────────────────────────────
# main 主流程
# ─────────────────────────────────────────────

def _cfg_for_repo(tmp_path, repo):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "repo": {"remote_url": "https://github.com/o/r.git",
                 "branch": "main", "local_dir": str(repo)},
    }))
    return cfg


def _run_main_with_steps(tmp_path, docker_cp_real, monkeypatch, steps):
    """构造 reports-dir 场景并执行 main,返回 (repo, fake)。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "dist").mkdir()  # 正常流程由 init_archive_repo 创建
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "steps_foo.json").write_text(json.dumps(steps))
    # 预先放置 failed 结果,使 Step 4 归档走 failed 分支
    # (success 分支因生产 bug 会抛 UnboundLocalError,见 test_archive_reports_success_branch_bug)
    (reports / "build_rpm_result_foo.json").write_text(
        json.dumps({"action": "failed", "version": "1.0"}))
    fake = docker_cp_real
    fake.when("git clone", returncode=0)
    monkeypatch.setattr(sys, "argv", ["publish_rpm.py", "--pkgs", "foo",
                                      "--config", str(_cfg_for_repo(tmp_path, repo)),
                                      "--container", CTR,
                                      "--reports-dir", str(reports)])
    p.main()
    return repo, fake


def test_main_happy_path(tmp_path, docker_cp_real, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "gitcode": {"token": "tok", "username": "u"},
        "repo": {"remote_url": "https://github.com/o/r.git",
                 "branch": "main", "local_dir": str(repo)},
    }))
    fake = docker_cp_real
    fake.when("git clone", returncode=0)
    monkeypatch.setattr(sys, "argv", ["publish_rpm.py", "--pkgs", "foo",
                                      "--config", str(cfg), "--container", CTR])
    p.main()
    assert fake.called_with("git clone --branch main https://u:tok@github.com/o/r.git")
    assert (repo / "foo" / "foo.spec").exists()
    assert (repo / "dist" / "repo-aitest.repo").exists()
    assert (repo / "README.md").exists()
    assert "RPM 归档报告" in capsys.readouterr().out


@pytest.mark.parametrize("steps,expected_msg,to_stderr", [
    ({"build": "running"}, "build 步骤未完成", True),
    ({"build": "failed"}, "仅归档失败报告", False),
    ({"build": "done", "ci_gate": "pending"}, "CI 门禁由 builder 阶段2 负责", True),
    ({"build": "done", "ci_gate": "done", "review_summary": "pending"},
     "请先执行 /review-rpm summary", False),
])
def test_main_steps_gate_blocks(tmp_path, docker_cp_real, monkeypatch, capsys,
                                steps, expected_msg, to_stderr):
    _run_main_with_steps(tmp_path, docker_cp_real, monkeypatch, steps)
    captured = capsys.readouterr()
    text = captured.err if to_stderr else captured.out
    assert expected_msg in text
    assert "以下包因步骤未完成被跳过归档 RPM: foo" in captured.out


def test_main_steps_all_done_copies(tmp_path, docker_cp_real, monkeypatch, capsys):
    repo, fake = _run_main_with_steps(
        tmp_path, docker_cp_real, monkeypatch,
        {"build": "done", "ci_gate": "done", "review_summary": "done"})
    # 步骤齐备 → copy_pkg_files 正常执行
    assert (repo / "foo" / "foo.spec").exists()
    assert fake.called_with("docker cp ctr:/root/rpmbuild/SPECS/foo.spec")
    out = capsys.readouterr().out
    assert "处理包: foo" in out


def test_main_rollback_on_dist_error(tmp_path, docker_cp_real, monkeypatch, capsys):
    # 新 RPM 文件名无法解析(如 foo-bad.rpm)→ resolve_dist_conflicts 抛 ValueError
    # → 主流程回滚工作区并退出 1
    repo = tmp_path / "repo"
    repo.mkdir()
    fake = docker_cp_real
    fake.when("git clone", returncode=0)
    fake.when(lambda s: "test -f" in s, returncode=1)
    fake.when(lambda s: "find /root/rpmbuild/RPMS" in s,
              stdout="/root/rpmbuild/RPMS/foo-bad.rpm\n")
    fake.when(lambda s: "stat -c" in s, stdout="10")
    monkeypatch.setattr(sys, "argv", ["publish_rpm.py", "--pkgs", "foo",
                                      "--config", str(_cfg_for_repo(tmp_path, repo)),
                                      "--container", CTR])
    with pytest.raises(SystemExit) as ei:
        p.main()
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "回滚工作区" in err
    assert not (repo / "dist" / "foo-bad.rpm").exists()
    assert fake.called_with("git clean -fd")


def test_main_push_failure_exits(tmp_path, docker_cp_real, monkeypatch, capsys):
    # Step 5 推送失败(rebase 冲突)→ RuntimeError → 退出 1
    repo = tmp_path / "repo"
    repo.mkdir()
    fake = docker_cp_real
    fake.when("git clone", returncode=0)
    fake.when(lambda s: "test -f" in s, returncode=1)
    fake.when("git status --porcelain", stdout=" M x.spec")
    fake.when("git push", returncode=1, stderr="non-fast-forward")
    fake.when("git pull --rebase", returncode=1, stderr="CONFLICT")
    fake.when("git diff --name-only", stdout="x.spec")
    fake.when("git status", stdout="UU x.spec")
    monkeypatch.setattr(sys, "argv", ["publish_rpm.py", "--pkgs", "foo",
                                      "--config", str(_cfg_for_repo(tmp_path, repo)),
                                      "--container", CTR])
    with pytest.raises(SystemExit) as ei:
        p.main()
    assert ei.value.code == 1
    assert "归档中止" in capsys.readouterr().err
