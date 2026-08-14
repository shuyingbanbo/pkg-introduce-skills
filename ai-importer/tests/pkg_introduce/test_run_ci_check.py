"""run_ci_check.py — CI 门禁（COPR 模式）测试。

覆盖：
- _chroot_repo_base / _chroot_arch / _extra_repos（$basearch 替换 / 跳过非 http）
- _get_copr_result_url（session chroot 匹配 / x86_64 兜底 / 异常）、
  _write_repo_file（官方三源 + COPR 渲染 / 无内容返回 False / PermissionError）、
  _copr_repo_accessible
- run_repoclosure（工具缺失 / 命令组装 / 失败重试 / 超时 [INFRA]）
- run_install_check（SKIP / 命令组装 / 跨架构 forcearch / 失败 / 超时）
- run_builddep（spec 缺失 SKIP / 探测失败 / Error 判定规则）
- main（全通过 / 真实失败 / infra 失败 / 警告 / 异常兜底 / spec 选择 / 默认输出目录）
不测真实 dnf / 网络。
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

ci = load_module("run_ci_check", SCRIPT_DIRS["pkg_introduce"] / "run_ci_check.py")


def _argv(args: list[str], monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run_ci_check.py", *args])


class _FakeResponse:
    def __init__(self, payload=None, status=200):
        self.status = status
        self._payload = (json.dumps(payload).encode() if payload is not None
                         else b"")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _write_session(session_dir, **overrides):
    data = {
        "copr_url": "http://copr-frontend:5000",
        "copr_owner": "owner",
        "copr_project": "proj",
        "copr_login": "login",
        "copr_token": "token",
    }
    data.update(overrides)
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "session.json").write_text(json.dumps(data))


# ─────────────────────────────────────────────
# _chroot_repo_base / _chroot_arch
# ─────────────────────────────────────────────

@pytest.mark.parametrize("chroot,expected", [
    ("openeuler-22.03_LTS-x86_64",
     "https://repo.huaweicloud.com/openeuler/openEuler-22.03-LTS"),
    ("openeuler-22.03_LTS_SP4-aarch64",
     "https://repo.huaweicloud.com/openeuler/openEuler-22.03-LTS-SP4"),
    ("openeuler-24.03_LTS-x86_64",
     "https://repo.huaweicloud.com/openeuler/openEuler-24.03-LTS"),
    ("openeuler-24.03_LTS_SP3-aarch64",
     "https://repo.huaweicloud.com/openeuler/openEuler-24.03-LTS-SP3"),
    ("openeuler-20.03-x86_64", None),  # 未收录版本
    ("", None),
    ("centos-9-x86_64", None),
])
def test_chroot_repo_base(chroot, expected):
    assert ci._chroot_repo_base(chroot) == expected


@pytest.mark.parametrize("chroot,expected", [
    ("openeuler-24.03_LTS-aarch64", "aarch64"),
    ("openeuler-24.03_LTS-x86_64", "x86_64"),
    ("openeuler-24.03_LTS", "x86_64"),  # 无后缀缺省 x86_64
    ("", "x86_64"),
])
def test_chroot_arch(chroot, expected):
    assert ci._chroot_arch(chroot) == expected


# ─────────────────────────────────────────────
# _extra_repos
# ─────────────────────────────────────────────

def test_extra_repos_basearch_replaced(capsys):
    entries = ci._extra_repos(["https://ros.example.com/$basearch/repo"], "aarch64")
    assert entries == [("ci-extra-0", "https://ros.example.com/aarch64/repo")]


def test_extra_repos_multiple_and_http(capsys):
    entries = ci._extra_repos(["http://a/x", "https://b/$basearch"], "x86_64")
    assert entries == [("ci-extra-0", "http://a/x"),
                       ("ci-extra-1", "https://b/x86_64")]


def test_extra_repos_skips_copr_scheme(capsys):
    entries = ci._extra_repos(["copr://group/proj"], "x86_64")
    assert entries == []
    assert "跳过暂不支持的 additional repo" in capsys.readouterr().err


def test_extra_repos_skips_non_str_and_blank(capsys):
    entries = ci._extra_repos([123, None, "   ", "https://ok/x"], "x86_64")
    assert entries == [("ci-extra-3", "https://ok/x")]


def test_extra_repos_none(capsys):
    assert ci._extra_repos(None, "x86_64") == []


# ─────────────────────────────────────────────
# _get_copr_result_url
# ─────────────────────────────────────────────

def test_get_copr_result_url_session_chroot(monkeypatch, tmp_path):
    _write_session(tmp_path, copr_chroot="openeuler-24.03-x86_64")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return _FakeResponse({
            "chroot_repos": {
                "openeuler-24.03-x86_64": "http://res/x86",
                "openeuler-24.03-aarch64": "http://res/aarch64",
            },
            "additional_repos": ["https://extra/$basearch"],
        })

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    chroot, url, extra = ci._get_copr_result_url(tmp_path)
    assert chroot == "openeuler-24.03-x86_64"
    assert url == "http://res/x86"
    assert extra == ["https://extra/$basearch"]
    assert captured["req"].full_url.endswith("?ownername=owner&projectname=proj")
    assert captured["req"].headers["Authorization"].startswith("Basic ")


def test_get_copr_result_url_fallback_first_x86(monkeypatch, tmp_path):
    _write_session(tmp_path)
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None:
                        _FakeResponse({
                            "chroot_repos": {
                                "openeuler-24.03-aarch64": "http://res/a",
                                "openeuler-24.03-x86_64": "http://res/x",
                            }}))
    chroot, url, _ = ci._get_copr_result_url(tmp_path)
    assert (chroot, url) == ("openeuler-24.03-x86_64", "http://res/x")


def test_get_copr_result_url_no_x86_first_item(monkeypatch, tmp_path):
    _write_session(tmp_path)
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None:
                        _FakeResponse({
                            "chroot_repos": {
                                "openeuler-24.03-aarch64": "http://res/a"}}))
    chroot, url, _ = ci._get_copr_result_url(tmp_path)
    assert (chroot, url) == ("openeuler-24.03-aarch64", "http://res/a")


def test_get_copr_result_url_exception(monkeypatch, tmp_path, capsys):
    _write_session(tmp_path)

    def boom(req, timeout=None):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    chroot, url, extra = ci._get_copr_result_url(tmp_path)
    assert (chroot, url, extra) == ("", "", [])
    assert "无法获取 COPR chroot 信息" in capsys.readouterr().err


# ─────────────────────────────────────────────
# _write_repo_file
# ─────────────────────────────────────────────

def test_write_repo_file_known_chroot_with_copr(tmp_path):
    repo = tmp_path / "ci.repo"
    ok = ci._write_repo_file(repo, "openeuler-24.03_LTS-x86_64",
                             "http://copr/results/")
    assert ok is True
    content = repo.read_text(encoding="utf-8")
    base = "https://repo.huaweicloud.com/openeuler/openEuler-24.03-LTS"
    assert "[ci-oe-official]" in content
    assert f"baseurl={base}/everything/x86_64/" in content
    assert "[ci-oe-update]" in content
    assert f"baseurl={base}/update/x86_64/" in content
    assert "[ci-oe-epol]" in content
    assert f"baseurl={base}/EPOL/main/x86_64/" in content
    assert "[ci-copr-result]" in content
    assert "baseurl=http://copr/results/" in content
    assert "gpgcheck=0" in content


def test_write_repo_file_unknown_chroot_no_copr(tmp_path):
    repo = tmp_path / "ci.repo"
    ok = ci._write_repo_file(repo, "unknown-chroot-x86_64", "")
    assert ok is False
    assert not repo.exists()


def test_write_repo_file_unknown_chroot_with_copr(tmp_path):
    repo = tmp_path / "ci.repo"
    ok = ci._write_repo_file(repo, "unknown-chroot-x86_64",
                             "http://copr/results/")
    assert ok is True
    content = repo.read_text(encoding="utf-8")
    assert "[ci-copr-result]" in content
    assert "[ci-oe-official]" not in content  # 无官方源段


def test_write_repo_file_permission_error(tmp_path, monkeypatch):
    def deny(self, *a, **k):
        raise PermissionError("readonly")

    monkeypatch.setattr(Path, "write_text", deny)
    assert ci._write_repo_file(tmp_path / "x.repo",
                               "openeuler-24.03_LTS-x86_64", "http://c/") is False


# ─────────────────────────────────────────────
# _copr_repo_accessible
# ─────────────────────────────────────────────

def test_copr_repo_accessible_ok(monkeypatch):
    captured = {}
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None:
                        captured.update(url=req.full_url, timeout=timeout) or
                        _FakeResponse(status=200))
    assert ci._copr_repo_accessible("http://copr/results/") is True
    assert captured["url"] == "http://copr/results/repodata/repomd.xml"
    assert captured["timeout"] == 5


def test_copr_repo_accessible_not_200(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResponse(status=404))
    assert ci._copr_repo_accessible("http://copr/results/") is False


def test_copr_repo_accessible_exception(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: (_ for _ in ()).throw(
                            urllib.error.URLError("nope")))
    assert ci._copr_repo_accessible("http://copr/results/") is False


# ─────────────────────────────────────────────
# run_repoclosure
# ─────────────────────────────────────────────

def _repoclosure_cmd(fake_subprocess):
    for cmd, _ in fake_subprocess.calls:
        if cmd and cmd[0] == "repoclosure":
            return cmd
    raise AssertionError("repoclosure 未被调用")


def test_repoclosure_tool_missing(fake_subprocess):
    fake_subprocess.when("which", returncode=1)
    ok, msg = ci.run_repoclosure(["pkgA"], "openeuler-24.03_LTS-x86_64",
                                 "http://copr/", [])
    assert ok is False
    assert msg.startswith("[INFRA] repoclosure 不可用")


def test_repoclosure_success_cmd_shape(fake_subprocess, monkeypatch):
    monkeypatch.setattr(ci, "_copr_repo_accessible", lambda u: True)
    ok, msg = ci.run_repoclosure(["pkgA", "pkgB"], "openeuler-24.03_LTS-x86_64",
                                 "http://copr/results/",
                                 ["https://extra/$basearch/ros"])
    assert (ok, msg) == (True, "")
    cmd = _repoclosure_cmd(fake_subprocess)
    base = "https://repo.huaweicloud.com/openeuler/openEuler-24.03-LTS"
    assert "--newest" in cmd
    assert "--arch" in cmd and cmd[cmd.index("--arch") + 1] == "x86_64"
    assert f"ci-oe-official,{base}/everything/x86_64/" in cmd
    assert f"ci-oe-update,{base}/update/x86_64/" in cmd
    assert f"ci-oe-epol,{base}/EPOL/main/x86_64/" in cmd
    assert "ci-copr-result,http://copr/results/" in cmd
    assert "ci-extra-0,https://extra/x86_64/ros" in cmd
    assert cmd[-4:] == ["--check", "pkgA", "--check", "pkgB"]


def test_repoclosure_unknown_chroot_uses_existing_repo(fake_subprocess,
                                                       monkeypatch):
    monkeypatch.setattr(ci, "_copr_repo_accessible", lambda u: True)
    ok, _ = ci.run_repoclosure(["pkgA"], "", "http://copr/", [])
    assert ok is True
    cmd = _repoclosure_cmd(fake_subprocess)
    joined = " ".join(cmd)
    assert "--arch" not in cmd
    assert "ci-oe-official" not in joined  # 无官方源注入
    assert "--repofrompath" in cmd  # copr 源仍注入
    assert "ci-copr-result,http://copr/" in joined


def test_repoclosure_copr_inaccessible_skipped(fake_subprocess, monkeypatch,
                                               capsys):
    monkeypatch.setattr(ci, "_copr_repo_accessible", lambda u: False)
    ok, _ = ci.run_repoclosure(["pkgA"], "openeuler-24.03_LTS-x86_64",
                               "http://copr/", [])
    assert ok is True
    cmd = _repoclosure_cmd(fake_subprocess)
    assert "ci-copr-result" not in " ".join(cmd)
    assert "COPR result repo 暂不可访问" in capsys.readouterr().out


def test_repoclosure_failure_retried_once(fake_subprocess, monkeypatch):
    monkeypatch.setattr(ci, "_copr_repo_accessible", lambda u: True)
    sleeps = []
    monkeypatch.setattr(ci.time, "sleep", lambda s: sleeps.append(s))
    fake_subprocess.when(lambda s: s.startswith("repoclosure"),
                         returncode=1, stdout="broken dep\n", stderr="e\n")
    ok, msg = ci.run_repoclosure(["pkgA"], "openeuler-24.03_LTS-x86_64",
                                 "http://copr/", [])
    assert ok is False
    assert msg == "broken dep\ne"  # stdout+stderr 合并 strip
    assert sleeps == [2]  # 失败后等 2s 重试一次


def test_repoclosure_timeout_then_success(fake_subprocess, monkeypatch):
    monkeypatch.setattr(ci, "_copr_repo_accessible", lambda u: True)
    monkeypatch.setattr(ci.time, "sleep", lambda s: None)
    attempts = {"n": 0}

    def first_only(cmd_str):
        if cmd_str.startswith("repoclosure"):
            attempts["n"] += 1
            return attempts["n"] == 1
        return False

    fake_subprocess.when(first_only,
                         exc=subprocess.TimeoutExpired("repoclosure", 600))
    ok, msg = ci.run_repoclosure(["pkgA"], "openeuler-24.03_LTS-x86_64",
                                 "http://copr/", [])
    assert (ok, msg) == (True, "")


def test_repoclosure_double_timeout_infra(fake_subprocess, monkeypatch):
    monkeypatch.setattr(ci, "_copr_repo_accessible", lambda u: True)
    monkeypatch.setattr(ci.time, "sleep", lambda s: None)
    fake_subprocess.when(lambda s: s.startswith("repoclosure"),
                         exc=subprocess.TimeoutExpired("repoclosure", 600))
    ok, msg = ci.run_repoclosure(["pkgA"], "openeuler-24.03_LTS-x86_64",
                                 "http://copr/", [])
    assert ok is False
    assert msg.startswith("[INFRA] repoclosure 超时")
    assert "600s" in msg


# ─────────────────────────────────────────────
# run_install_check
# ─────────────────────────────────────────────

def _install_cmd(fake_subprocess):
    for cmd, _ in fake_subprocess.calls:
        if cmd and cmd[0] == "dnf" and cmd[1] == "install":
            return cmd
    raise AssertionError("dnf install 未被调用")


def test_install_check_skip_when_copr_inaccessible(monkeypatch, fake_subprocess):
    monkeypatch.setattr(ci, "_copr_repo_accessible", lambda u: False)
    ok, msg = ci.run_install_check(["pkgA"], "openeuler-24.03_LTS-x86_64",
                                   "http://copr/", [])
    assert ok is True
    assert msg.startswith("[SKIP] COPR result repo 不可访问")
    assert fake_subprocess.calls == []


def test_install_check_success_cmd_shape(fake_subprocess, monkeypatch,
                                         tmp_path):
    monkeypatch.setattr(ci, "_copr_repo_accessible", lambda u: True)
    monkeypatch.setattr(ci.platform, "machine", lambda: "x86_64")
    installroot = tmp_path / "ci-install-xyz"
    monkeypatch.setattr(ci.tempfile, "mkdtemp", lambda prefix: str(installroot))
    removed = []
    monkeypatch.setattr(ci.shutil, "rmtree",
                        lambda p, ignore_errors=False: removed.append(str(p)))
    ok, msg = ci.run_install_check(["pkgA"], "openeuler-24.03_LTS-x86_64",
                                   "http://copr/results/",
                                   ["https://extra/$basearch"])
    assert (ok, msg) == (True, "")
    cmd = _install_cmd(fake_subprocess)
    base = "https://repo.huaweicloud.com/openeuler/openEuler-24.03-LTS"
    assert "--nogpgcheck" in cmd
    assert f"--installroot={installroot}" in cmd
    assert "--releasever=/" in cmd
    assert "--disablerepo=*" in cmd
    assert "--enablerepo=ci-oe-official" in cmd
    assert f"ci-oe-official,{base}/everything/x86_64/" in cmd
    assert "ci-copr-result,http://copr/results/" in cmd
    assert "ci-extra-0,https://extra/x86_64" in cmd
    assert cmd[-1] == "pkgA"
    assert removed == [str(installroot)]  # 安装后清理 installroot
    # 空 installroot 的 usrmerge 骨架已预建(bin → usr/bin 软链)
    assert (installroot / "bin").is_symlink()
    assert (installroot / "usr" / "lib64").is_dir()


def test_install_check_cross_arch_flags(fake_subprocess, monkeypatch,
                                        tmp_path):
    monkeypatch.setattr(ci, "_copr_repo_accessible", lambda u: True)
    monkeypatch.setattr(ci.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(ci.tempfile, "mkdtemp",
                        lambda prefix: str(tmp_path / "ci-install"))
    monkeypatch.setattr(ci.shutil, "rmtree", lambda p, ignore_errors=False: None)
    ci.run_install_check(["pkgA"], "openeuler-24.03_LTS-aarch64",
                         "http://copr/", [])
    cmd = _install_cmd(fake_subprocess)
    assert "--forcearch=aarch64" in cmd
    assert "--setopt=tsflags=noscripts" in cmd


def test_install_check_same_arch_no_forcearch(fake_subprocess, monkeypatch,
                                              tmp_path):
    monkeypatch.setattr(ci, "_copr_repo_accessible", lambda u: True)
    monkeypatch.setattr(ci.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(ci.tempfile, "mkdtemp",
                        lambda prefix: str(tmp_path / "ci-install"))
    monkeypatch.setattr(ci.shutil, "rmtree", lambda p, ignore_errors=False: None)
    ci.run_install_check(["pkgA"], "openeuler-24.03_LTS-x86_64",
                         "http://copr/", [])
    cmd = _install_cmd(fake_subprocess)
    assert "--forcearch=x86_64" not in cmd


def test_install_check_failure(fake_subprocess, monkeypatch, tmp_path):
    monkeypatch.setattr(ci, "_copr_repo_accessible", lambda u: True)
    monkeypatch.setattr(ci.tempfile, "mkdtemp",
                        lambda prefix: str(tmp_path / "ci-install"))
    monkeypatch.setattr(ci.shutil, "rmtree", lambda p, ignore_errors=False: None)
    fake_subprocess.when(lambda s: s.startswith("dnf install"),
                         returncode=1, stdout="Error: dep\n", stderr="conflict\n")
    ok, msg = ci.run_install_check(["pkgA"], "openeuler-24.03_LTS-x86_64",
                                   "http://copr/", [])
    assert ok is False
    assert msg == "Error: dep\nconflict"


def test_install_check_timeout_infra(fake_subprocess, monkeypatch, tmp_path):
    monkeypatch.setattr(ci, "_copr_repo_accessible", lambda u: True)
    monkeypatch.setattr(ci.tempfile, "mkdtemp",
                        lambda prefix: str(tmp_path / "ci-install"))
    monkeypatch.setattr(ci.shutil, "rmtree", lambda p, ignore_errors=False: None)
    fake_subprocess.when(lambda s: s.startswith("dnf install"),
                         exc=subprocess.TimeoutExpired("dnf", 600))
    ok, msg = ci.run_install_check(["pkgA"], "openeuler-24.03_LTS-x86_64",
                                   "http://copr/", [])
    assert ok is False
    assert msg.startswith("[INFRA] dnf install 超时")


# ─────────────────────────────────────────────
# run_builddep
# ─────────────────────────────────────────────

def _builddep_cmd(fake_subprocess):
    for cmd, _ in fake_subprocess.calls:
        if cmd and cmd[0] == "dnf" and "builddep" in cmd and "--assumeno" in cmd:
            return cmd
    raise AssertionError("dnf builddep --assumeno 未被调用")


def test_builddep_missing_spec_skip(fake_subprocess, tmp_path):
    ok, msg = ci.run_builddep("pkg", tmp_path / "nope.spec",
                              "openeuler-24.03_LTS-x86_64", "http://copr/", [])
    assert ok is True
    assert msg.startswith("[SKIP] spec 文件不存在")
    assert fake_subprocess.calls == []


def test_builddep_probe_fails(fake_subprocess, tmp_path):
    fake_subprocess.when(lambda s: s == "dnf builddep --help", returncode=1)
    spec = tmp_path / "p.spec"
    spec.write_text("x")
    ok, msg = ci.run_builddep("pkg", spec,
                              "openeuler-24.03_LTS-x86_64", "http://copr/", [])
    assert ok is False
    assert msg.startswith("[INFRA] dnf builddep 不可用")


def test_builddep_probe_no_such_command(fake_subprocess, tmp_path):
    fake_subprocess.when(lambda s: s == "dnf builddep --help",
                         stdout="No such command: builddep")
    spec = tmp_path / "p.spec"
    spec.write_text("x")
    ok, msg = ci.run_builddep("pkg", spec,
                              "openeuler-24.03_LTS-x86_64", "http://copr/", [])
    assert ok is False
    assert "[INFRA] dnf builddep 不可用" in msg


def test_builddep_probe_filenotfound(fake_subprocess, tmp_path):
    fake_subprocess.when(lambda s: s == "dnf builddep --help",
                         exc=FileNotFoundError())
    spec = tmp_path / "p.spec"
    spec.write_text("x")
    ok, msg = ci.run_builddep("pkg", spec,
                              "openeuler-24.03_LTS-x86_64", "http://copr/", [])
    assert ok is False
    assert "[INFRA] dnf builddep 不可用" in msg


def test_builddep_success_cmd_shape(fake_subprocess, tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "_copr_repo_accessible", lambda u: True)
    monkeypatch.setattr(ci.tempfile, "mkdtemp",
                        lambda prefix: str(tmp_path / "ci-builddep"))
    monkeypatch.setattr(ci.shutil, "rmtree", lambda p, ignore_errors=False: None)
    spec = tmp_path / "p.spec"
    spec.write_text("BuildRequires: gcc\n")
    ok, msg = ci.run_builddep("pkg", spec, "openeuler-24.03_LTS-x86_64",
                              "http://copr/results/", ["https://extra/$basearch"])
    assert (ok, msg) == (True, "")
    cmd = _builddep_cmd(fake_subprocess)
    base = "https://repo.huaweicloud.com/openeuler/openEuler-24.03-LTS"
    assert "--assumeno" in cmd
    installroot = tmp_path / "ci-builddep"
    assert f"--installroot={installroot}" in cmd
    assert "--releasever=/" in cmd
    assert f"ci-oe-official,{base}/everything/x86_64/" in cmd
    assert "--disablerepo=*" in cmd
    assert "ci-copr-result,http://copr/results/" in cmd
    assert "ci-extra-0,https://extra/x86_64" in cmd
    assert cmd[-1] == str(spec)


def test_builddep_unknown_chroot_no_official_repos(fake_subprocess, tmp_path,
                                                   monkeypatch):
    monkeypatch.setattr(ci, "_copr_repo_accessible", lambda u: True)
    monkeypatch.setattr(ci.tempfile, "mkdtemp",
                        lambda prefix: str(tmp_path / "ci-builddep"))
    monkeypatch.setattr(ci.shutil, "rmtree", lambda p, ignore_errors=False: None)
    spec = tmp_path / "p.spec"
    spec.write_text("x")
    ci.run_builddep("pkg", spec, "", "http://copr/", [])
    cmd = _builddep_cmd(fake_subprocess)
    assert "--disablerepo=*" not in cmd


def test_builddep_cross_arch(fake_subprocess, tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "_copr_repo_accessible", lambda u: True)
    monkeypatch.setattr(ci.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(ci.tempfile, "mkdtemp",
                        lambda prefix: str(tmp_path / "ci-builddep"))
    monkeypatch.setattr(ci.shutil, "rmtree", lambda p, ignore_errors=False: None)
    spec = tmp_path / "p.spec"
    spec.write_text("x")
    ci.run_builddep("pkg", spec, "openeuler-24.03_LTS-aarch64", "http://copr/", [])
    assert "--forcearch=aarch64" in _builddep_cmd(fake_subprocess)


def test_builddep_error_could_not_be_found(fake_subprocess, tmp_path,
                                           monkeypatch):
    monkeypatch.setattr(ci, "_copr_repo_accessible", lambda u: True)
    monkeypatch.setattr(ci.tempfile, "mkdtemp",
                        lambda prefix: str(tmp_path / "ci-builddep"))
    monkeypatch.setattr(ci.shutil, "rmtree", lambda p, ignore_errors=False: None)
    spec = tmp_path / "p.spec"
    spec.write_text("x")
    fake_subprocess.when(lambda s: s.startswith("dnf builddep --assumeno"),
                         returncode=1,
                         stdout="Error: package 'libfoo-devel' could not be found\n")
    ok, msg = ci.run_builddep("pkg", spec, "openeuler-24.03_LTS-x86_64",
                              "http://copr/", [])
    assert ok is False
    assert "could not be found" in msg


def test_builddep_error_no_match(fake_subprocess, tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "_copr_repo_accessible", lambda u: True)
    monkeypatch.setattr(ci.tempfile, "mkdtemp",
                        lambda prefix: str(tmp_path / "ci-builddep"))
    monkeypatch.setattr(ci.shutil, "rmtree", lambda p, ignore_errors=False: None)
    spec = tmp_path / "p.spec"
    spec.write_text("x")
    fake_subprocess.when(lambda s: s.startswith("dnf builddep --assumeno"),
                         returncode=1,
                         stdout="Error: No match for argument: libfoo-devel\n")
    ok, msg = ci.run_builddep("pkg", spec, "openeuler-24.03_LTS-x86_64",
                              "http://copr/", [])
    assert ok is False
    assert "No match" in msg


def test_builddep_other_error_passes(fake_subprocess, tmp_path, monkeypatch):
    # 生产行为:--assumeno 成功时返回非零;仅 "could not be found"/"No match"
    # 计入失败,其他 Error 文本(如事务检查)不判定失败
    monkeypatch.setattr(ci, "_copr_repo_accessible", lambda u: True)
    monkeypatch.setattr(ci.tempfile, "mkdtemp",
                        lambda prefix: str(tmp_path / "ci-builddep"))
    monkeypatch.setattr(ci.shutil, "rmtree", lambda p, ignore_errors=False: None)
    spec = tmp_path / "p.spec"
    spec.write_text("x")
    fake_subprocess.when(lambda s: s.startswith("dnf builddep --assumeno"),
                         returncode=1, stdout="Error: Transaction test error\n")
    ok, msg = ci.run_builddep("pkg", spec, "openeuler-24.03_LTS-x86_64",
                              "http://copr/", [])
    assert (ok, msg) == (True, "")


def test_builddep_assumeno_rejected_passes(fake_subprocess, tmp_path,
                                           monkeypatch):
    # --assumeno 拒绝安装(rc=1)但无 Error 文本 → 通过
    monkeypatch.setattr(ci, "_copr_repo_accessible", lambda u: True)
    monkeypatch.setattr(ci.tempfile, "mkdtemp",
                        lambda prefix: str(tmp_path / "ci-builddep"))
    monkeypatch.setattr(ci.shutil, "rmtree", lambda p, ignore_errors=False: None)
    spec = tmp_path / "p.spec"
    spec.write_text("x")
    fake_subprocess.when(lambda s: s.startswith("dnf builddep --assumeno"),
                         returncode=1,
                         stdout="Operation aborted.\n")
    ok, _ = ci.run_builddep("pkg", spec, "openeuler-24.03_LTS-x86_64",
                            "http://copr/", [])
    assert ok is True


def test_builddep_timeout_infra(fake_subprocess, tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "_copr_repo_accessible", lambda u: True)
    monkeypatch.setattr(ci.tempfile, "mkdtemp",
                        lambda prefix: str(tmp_path / "ci-builddep"))
    monkeypatch.setattr(ci.shutil, "rmtree", lambda p, ignore_errors=False: None)
    spec = tmp_path / "p.spec"
    spec.write_text("x")
    fake_subprocess.when(lambda s: s.startswith("dnf builddep --assumeno"),
                         exc=subprocess.TimeoutExpired("dnf", 300))
    ok, msg = ci.run_builddep("pkg", spec, "openeuler-24.03_LTS-x86_64",
                              "http://copr/", [])
    assert ok is False
    assert msg.startswith("[INFRA] dnf builddep 超时")


# ─────────────────────────────────────────────
# main
# ─────────────────────────────────────────────

def _patch_checks(monkeypatch, *, copr=("openeuler-24.03_LTS-x86_64",
                                         "http://copr/results/", []),
                  repoclosure=(True, ""), install=(True, ""),
                  builddep=(True, ""), copr_access=None):
    monkeypatch.setattr(ci, "_get_copr_result_url", lambda sd: copr)
    monkeypatch.setattr(ci, "run_repoclosure",
                        lambda *a: repoclosure)
    monkeypatch.setattr(ci, "run_install_check", lambda *a: install)
    monkeypatch.setattr(ci, "run_builddep", lambda *a: builddep)
    if copr_access is not None:
        monkeypatch.setattr(ci, "_copr_repo_accessible", lambda u: copr_access)


def _result(reports_dir):
    return json.loads((reports_dir / "ci_check_result.json").read_text())


def test_main_all_pass(monkeypatch, tmp_path, capsys):
    _patch_checks(monkeypatch)
    _write_session(tmp_path)
    reports = tmp_path / "pkgs" / "pkgA"
    _argv(["--pkgs", "pkgA", "pkgB", "--session-dir", str(tmp_path),
           "--reports-dir", str(reports)], monkeypatch)
    assert ci.main() == 0
    r = _result(reports)
    assert r["status"] == "pass"
    assert r["errors"] == []
    assert r["warnings"] == []
    assert r["chroot"] == "openeuler-24.03_LTS-x86_64"
    assert r["copr_result_url"] == "http://copr/results/"
    assert "门禁全部通过" in capsys.readouterr().out


def test_main_check_failure(monkeypatch, tmp_path):
    _patch_checks(monkeypatch,
                  repoclosure=(False, "deps missing"))
    _write_session(tmp_path)
    reports = tmp_path / "pkgs" / "pkgA"
    _argv(["--pkgs", "pkgA", "--session-dir", str(tmp_path),
           "--reports-dir", str(reports)], monkeypatch)
    assert ci.main() == 1
    r = _result(reports)
    assert r["status"] == "fail"
    assert r["errors"] == ["repoclosure 失败:\ndeps missing"]


def test_main_install_failure(monkeypatch, tmp_path, capsys):
    _patch_checks(monkeypatch,
                  install=(False, "Error: transaction failed"))
    _write_session(tmp_path)
    reports = tmp_path / "pkgs" / "pkgA"
    _argv(["--pkgs", "pkgA", "--session-dir", str(tmp_path),
           "--reports-dir", str(reports)], monkeypatch)
    assert ci.main() == 1
    r = _result(reports)
    assert r["status"] == "fail"
    assert r["errors"] == ["可安装性检查失败:\nError: transaction failed"]
    assert "可安装性检查失败" in capsys.readouterr().err


def test_main_infra_error(monkeypatch, tmp_path):
    _patch_checks(monkeypatch,
                  builddep=(False, "[INFRA] dnf builddep 不可用：镜像缺少 dnf-plugins-core 包，需更新 worker 镜像"))
    _write_session(tmp_path)
    reports = tmp_path / "pkgs" / "pkgA"
    _argv(["--pkgs", "pkgA", "--session-dir", str(tmp_path),
           "--reports-dir", str(reports)], monkeypatch)
    assert ci.main() == 1
    r = _result(reports)
    assert r["status"] == "error"
    assert len(r["errors"]) == 1
    assert "[INFRA]" in r["errors"][0]


def test_main_errors_beat_infra(monkeypatch, tmp_path):
    _patch_checks(monkeypatch,
                  repoclosure=(False, "deps missing"),
                  builddep=(False, "[INFRA] timeout"))
    _write_session(tmp_path)
    reports = tmp_path / "pkgs" / "pkgA"
    _argv(["--pkgs", "pkgA", "--session-dir", str(tmp_path),
           "--reports-dir", str(reports)], monkeypatch)
    assert ci.main() == 1
    r = _result(reports)
    assert r["status"] == "fail"  # 真实失败优先于 infra
    assert len(r["errors"]) == 2


def test_main_install_skip_warning(monkeypatch, tmp_path):
    _patch_checks(monkeypatch,
                  install=(True, "[SKIP] COPR result repo 不可访问，跳过可安装性检查"))
    _write_session(tmp_path)
    reports = tmp_path / "pkgs" / "pkgA"
    _argv(["--pkgs", "pkgA", "--session-dir", str(tmp_path),
           "--reports-dir", str(reports)], monkeypatch)
    assert ci.main() == 0
    r = _result(reports)
    assert r["status"] == "pass"
    assert r["warnings"] == ["[SKIP] COPR result repo 不可访问，跳过可安装性检查"]


def test_main_unexpected_exception_infra(monkeypatch, tmp_path):
    def boom(*a):
        raise RuntimeError("kaboom")

    _patch_checks(monkeypatch, repoclosure=None)
    monkeypatch.setattr(ci, "run_repoclosure", boom)
    _write_session(tmp_path)
    reports = tmp_path / "pkgs" / "pkgA"
    _argv(["--pkgs", "pkgA", "--session-dir", str(tmp_path),
           "--reports-dir", str(reports)], monkeypatch)
    assert ci.main() == 1
    r = _result(reports)
    assert r["status"] == "error"
    assert any("[INFRA] CI 脚本异常" in e for e in r["errors"])


def test_main_builddep_uses_reports_dir_spec(monkeypatch, tmp_path):
    recorded = {}
    _patch_checks(monkeypatch)
    monkeypatch.setattr(ci, "run_builddep",
                        lambda pkg, spec, *a: recorded.update(pkg=pkg, spec=spec) or
                        (True, ""))
    _write_session(tmp_path)
    reports = tmp_path / "pkgs" / "pkgA"
    reports.mkdir(parents=True)
    spec = reports / "some-pkg.spec"
    spec.write_text("x")
    _argv(["--pkgs", "pkgA", "pkgB", "--session-dir", str(tmp_path),
           "--reports-dir", str(reports)], monkeypatch)
    assert ci.main() == 0
    assert recorded == {"pkg": "pkgB", "spec": spec}  # 两个包都用同一 spec


def test_main_builddep_fallback_spec_name(monkeypatch, tmp_path):
    recorded = {}
    _patch_checks(monkeypatch)
    monkeypatch.setattr(ci, "run_builddep",
                        lambda pkg, spec, *a: recorded.update(spec=spec) or (True, ""))
    _write_session(tmp_path)
    reports = tmp_path / "pkgs" / "pkgA"
    _argv(["--pkgs", "pkgA", "--session-dir", str(tmp_path),
           "--reports-dir", str(reports)], monkeypatch)
    assert ci.main() == 0
    assert recorded["spec"] == reports / "pkgA.spec"


def test_main_default_reports_dir(monkeypatch, tmp_path):
    _patch_checks(monkeypatch)
    _write_session(tmp_path)
    _argv(["--pkgs", "pkgA", "--session-dir", str(tmp_path)], monkeypatch)
    assert ci.main() == 0
    default = tmp_path / "pkgs" / "pkgA" / "ci_check_result.json"
    assert default.exists()


def test_main_no_copr_result_url(monkeypatch, tmp_path, capsys):
    _patch_checks(monkeypatch, copr=("", "", []))
    _write_session(tmp_path)
    reports = tmp_path / "pkgs" / "pkgA"
    _argv(["--pkgs", "pkgA", "--session-dir", str(tmp_path),
           "--reports-dir", str(reports)], monkeypatch)
    assert ci.main() == 0
    assert "未找到 COPR result URL" in capsys.readouterr().err


def test_main_additional_repos_printed(monkeypatch, tmp_path, capsys):
    _patch_checks(monkeypatch,
                  copr=("ch", "http://u", ["https://extra/$basearch"]))
    _write_session(tmp_path)
    reports = tmp_path / "pkgs" / "pkgA"
    _argv(["--pkgs", "pkgA", "--session-dir", str(tmp_path),
           "--reports-dir", str(reports)], monkeypatch)
    assert ci.main() == 0
    assert "additional_repos" in capsys.readouterr().out


def test_main_repoclosure_skip_warning(monkeypatch, tmp_path):
    _patch_checks(monkeypatch,
                  repoclosure=(True, "[SKIP] 无 repo 可查"))
    _write_session(tmp_path)
    reports = tmp_path / "pkgs" / "pkgA"
    _argv(["--pkgs", "pkgA", "--session-dir", str(tmp_path),
           "--reports-dir", str(reports)], monkeypatch)
    assert ci.main() == 0
    r = _result(reports)
    assert r["status"] == "pass"
    assert r["warnings"] == ["[SKIP] 无 repo 可查"]


def test_main_builddep_skip_warning(monkeypatch, tmp_path):
    _patch_checks(monkeypatch,
                  builddep=(True, "[SKIP] spec 文件不存在: /x/y.spec"))
    _write_session(tmp_path)
    reports = tmp_path / "pkgs" / "pkgA"
    _argv(["--pkgs", "pkgA", "--session-dir", str(tmp_path),
           "--reports-dir", str(reports)], monkeypatch)
    assert ci.main() == 0
    r = _result(reports)
    assert r["status"] == "pass"
    assert r["warnings"] == ["[SKIP] spec 文件不存在: /x/y.spec"]


def test_main_missing_pkgs_arg(monkeypatch):
    _argv(["--session-dir", "/tmp/x"], monkeypatch)
    with pytest.raises(SystemExit) as ei:
        ci.main()
    assert ei.value.code == 2
