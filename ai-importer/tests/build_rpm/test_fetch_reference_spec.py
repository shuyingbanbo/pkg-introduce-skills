"""fetch_reference_spec.py — gitcode src-openeuler 参考 spec 抓取。

纯函数(分支匹配/目标归一化)直接断言;
git 调用用 fake_subprocess,clone 提取用 mkdtemp 重定向。
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sys.path.insert(0, str(SCRIPT_DIRS["build_rpm"]))
mod = load_module("fetch_reference_spec", SCRIPT_DIRS["build_rpm"] / "fetch_reference_spec.py")


# ─────────────────────────────────────────────
# git 环境与命令封装
# ─────────────────────────────────────────────

def test_git_env_strips_proxies(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy:8080")
    monkeypatch.setenv("https_proxy", "http://proxy:8080")
    monkeypatch.setenv("KEEP_ME", "yes")
    env = mod._git_env()
    assert "HTTP_PROXY" not in env
    assert "HTTPS_PROXY" not in env
    assert "http_proxy" not in env
    assert "https_proxy" not in env
    assert env["KEEP_ME"] == "yes"
    assert env["PATH"] == __import__("os").environ["PATH"]   # 其余环境保留


def test_run_git_success(fake_subprocess):
    fake_subprocess.when("git ls-remote", stdout="refs\n", returncode=0)
    result = mod._run_git(["git", "ls-remote", "https://x"], 10, desc="ls-remote x")
    assert result.returncode == 0
    assert result.stdout == "refs\n"


def test_run_git_failure_logs(capsys, fake_subprocess):
    fake_subprocess.when("git ls-remote", returncode=1, stderr="fatal: boom")
    result = mod._run_git(["git", "ls-remote", "https://x"], 10, desc="ls-remote x")
    assert result.returncode == 1
    err = capsys.readouterr().err
    assert "[fetch_ref]" in err
    assert "ls-remote x" in err
    assert "boom" in err


def test_run_git_failure_default_label(capsys, fake_subprocess):
    fake_subprocess.when("git clone", returncode=1, stderr="nope")
    mod._run_git(["git", "clone", "https://x"], 10)
    assert "git clone" in capsys.readouterr().err


# ─────────────────────────────────────────────
# ls-remote 存在性探测
# ─────────────────────────────────────────────

def test_try_git_ls_remote_exists(fake_subprocess):
    fake_subprocess.when("git ls-remote", stdout="abc\trefs/heads/master\n", returncode=0)
    assert mod._try_git_ls_remote("snappy") is True


def test_try_git_ls_remote_not_found(fake_subprocess):
    fake_subprocess.when("git ls-remote", returncode=1, stderr="fatal: repository not found")
    assert mod._try_git_ls_remote("snappy") is False


@pytest.mark.parametrize("stderr", [
    "could not read Username", "fatal: repository not found",
    "403 forbidden", "404 not found",
])
def test_try_git_ls_remote_not_found_keywords(fake_subprocess, stderr):
    fake_subprocess.when("git ls-remote", returncode=1, stderr=stderr)
    assert mod._try_git_ls_remote("snappy") is False


def test_try_git_ls_remote_does_not_exist_message_not_matched(fake_subprocess):
    # 注意:gitcode 常见报错 "The requested repository does not exist" 不含
    # 关键词表("not found"/"repository not found"/403/404)中的任何一项,
    # 生产代码会返回 None(网络错误方向),按实际行为断言。
    fake_subprocess.when("git ls-remote", returncode=1,
                         stderr="The requested repository does not exist")
    assert mod._try_git_ls_remote("snappy") is None


def test_try_git_ls_remote_ambiguous_failure(fake_subprocess):
    fake_subprocess.when("git ls-remote", returncode=1, stderr="connection reset")
    assert mod._try_git_ls_remote("snappy") is None


def test_try_git_ls_remote_timeout(fake_subprocess):
    fake_subprocess.when("git ls-remote", exc=subprocess.TimeoutExpired("git", 10))
    assert mod._try_git_ls_remote("snappy") is None


def test_try_git_ls_remote_generic_exception(fake_subprocess):
    fake_subprocess.when("git ls-remote", exc=OSError("no network"))
    assert mod._try_git_ls_remote("snappy") is None


def test_try_git_ls_remote_url(fake_subprocess):
    fake_subprocess.when("git ls-remote", returncode=0)
    mod._try_git_ls_remote("snappy")
    assert fake_subprocess.called_with(
        "git ls-remote --heads https://gitcode.com/src-openeuler/snappy.git")


# ─────────────────────────────────────────────
# clone
# ─────────────────────────────────────────────

def test_try_git_clone_success(fake_subprocess):
    fake_subprocess.when("git clone", returncode=0)
    assert mod._try_git_clone("snappy", __import__("pathlib").Path("/tmp/x")) is True


def test_try_git_clone_failure(fake_subprocess):
    fake_subprocess.when("git clone", returncode=128, stderr="denied")
    assert mod._try_git_clone("snappy", __import__("pathlib").Path("/tmp/x")) is False


def test_try_git_clone_timeout(fake_subprocess):
    fake_subprocess.when("git clone", exc=subprocess.TimeoutExpired("git", 30))
    assert mod._try_git_clone("snappy", __import__("pathlib").Path("/tmp/x")) is False


# ─────────────────────────────────────────────
# _repo_exists(重试)
# ─────────────────────────────────────────────

def test_repo_exists_git_unavailable(monkeypatch, capsys):
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    assert mod._repo_exists("snappy") is None
    assert "git not available" in capsys.readouterr().err


def test_repo_exists_immediate_yes(monkeypatch, fake_subprocess):
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/git")
    monkeypatch.setattr(mod, "_try_git_ls_remote", lambda p: True)
    assert mod._repo_exists("snappy") is True


def test_repo_exists_immediate_no(monkeypatch, fake_subprocess):
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/git")
    monkeypatch.setattr(mod, "_try_git_ls_remote", lambda p: False)
    assert mod._repo_exists("snappy") is False


def test_repo_exists_retries_then_success(monkeypatch):
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/git")
    outcomes = iter([None, True])
    monkeypatch.setattr(mod, "_try_git_ls_remote", lambda p: next(outcomes))
    sleeps = []
    monkeypatch.setattr(mod.time, "sleep", sleeps.append)
    assert mod._repo_exists("snappy") is True
    assert sleeps == [2]      # 第一次失败后按 RETRY_BASE_DELAY * attempt 退避


def test_repo_exists_exhausts_retries(monkeypatch):
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/git")
    monkeypatch.setattr(mod, "_try_git_ls_remote", lambda p: None)
    sleeps = []
    monkeypatch.setattr(mod.time, "sleep", sleeps.append)
    assert mod._repo_exists("snappy") is None
    assert sleeps == [2, 4]   # 3 次尝试,2 次退避


# ─────────────────────────────────────────────
# 分支列表
# ─────────────────────────────────────────────

def test_list_remote_branches(fake_subprocess):
    fake_subprocess.when(
        "git ls-remote",
        stdout=("abc\trefs/heads/master\n"
                "def\trefs/heads/openEuler-24.03-LTS-SP3\n"
                "ghi\trefs/tags/v1.0\n"
                "\n"
                "jkl\n"),
        returncode=0,
    )
    branches = mod._list_remote_branches("snappy")
    # 注意:生产实现只剥离 "refs/heads/" 前缀,其他 ref(refs/tags/)原样保留;
    # 单列无 tab 的行被跳过。按实际行为断言。
    assert branches == ["master", "openEuler-24.03-LTS-SP3", "refs/tags/v1.0"]


def test_list_remote_branches_failure(fake_subprocess):
    fake_subprocess.when("git ls-remote", returncode=1, stderr="boom")
    assert mod._list_remote_branches("snappy") is None


def test_list_remote_branches_timeout(fake_subprocess):
    fake_subprocess.when("git ls-remote", exc=subprocess.TimeoutExpired("git", 10))
    assert mod._list_remote_branches("snappy") is None


def test_list_remote_branches_exception(fake_subprocess):
    fake_subprocess.when("git ls-remote", exc=OSError("boom"))
    assert mod._list_remote_branches("snappy") is None


# ─────────────────────────────────────────────
# 目标归一化与分支匹配
# ─────────────────────────────────────────────

@pytest.mark.parametrize("target,expected", [
    ("openeuler-24.03_LTS_SP3-x86_64", "openEuler-24.03-LTS-SP3"),
    ("openeuler-24.03_LTS-x86_64", "openEuler-24.03-LTS"),
    ("openEuler-24.03-LTS-SP3", "openEuler-24.03-LTS-SP3"),
    ("openeuler-24.03_LTS-aarch64", "openEuler-24.03-LTS"),
    ("openeuler-24.03_LTS-noarch", "openEuler-24.03-LTS"),
    ("openeuler-24.03_LTS-i686", "openEuler-24.03-LTS"),
    ("foo-bar-x86_64", "foo-bar"),
    ("", ""),
])
def test_normalize_target(target, expected):
    assert mod._normalize_target(target) == expected


def test_find_best_branch_exact_match(monkeypatch):
    monkeypatch.setattr(mod, "_list_remote_branches",
                        lambda p: ["master", "openEuler-24.03-LTS-SP3"])
    assert mod._find_best_branch("snappy", "openeuler-24.03_LTS_SP3-x86_64") \
        == "openEuler-24.03-LTS-SP3"


def test_find_best_branch_underscore_variant(monkeypatch):
    # 下划线命名风格的分支也能命中
    monkeypatch.setattr(mod, "_list_remote_branches",
                        lambda p: ["openEuler_24.03_LTS_SP3"])
    assert mod._find_best_branch("snappy", "openEuler-24.03-LTS-SP3") \
        == "openEuler_24.03_LTS_SP3"


def test_find_best_branch_prefix_picks_highest(monkeypatch):
    monkeypatch.setattr(mod, "_list_remote_branches",
                        lambda p: ["openEuler-24.03-LTS-SP1", "openEuler-24.03-LTS-SP3",
                                   "openEuler-22.03-LTS-SP2"])
    # 目标 SP2 无精确分支 → 前缀 openEuler-24.03 匹配,反向排序取最高 SP3
    assert mod._find_best_branch("snappy", "openeuler-24.03_LTS_SP2-x86_64") \
        == "openEuler-24.03-LTS-SP3"


def test_find_best_branch_prefix_underscore_base(monkeypatch):
    # 注意:base_underscore 只把 "-" 换 "_"(点保留),
    # 即前缀是 "openEuler_24.03" 而非 "openEuler_24_03"
    monkeypatch.setattr(mod, "_list_remote_branches",
                        lambda p: ["openEuler_24.03-LTS-SP1", "master"])
    assert mod._find_best_branch("snappy", "openeuler-24.03_LTS-x86_64") \
        == "openEuler_24.03-LTS-SP1"


def test_find_best_branch_no_target():
    assert mod._find_best_branch("snappy", "") is None


def test_find_best_branch_no_branches(monkeypatch):
    monkeypatch.setattr(mod, "_list_remote_branches", lambda p: None)
    assert mod._find_best_branch("snappy", "openeuler-24.03_LTS_SP3-x86_64") is None


def test_find_best_branch_no_match(monkeypatch):
    monkeypatch.setattr(mod, "_list_remote_branches", lambda p: ["master"])
    assert mod._find_best_branch("snappy", "openeuler-24.03_LTS_SP3-x86_64") is None


# ─────────────────────────────────────────────
# clone + 提取
# ─────────────────────────────────────────────

@pytest.fixture
def fake_clone_tmp(monkeypatch, tmp_path):
    """mkdtemp 重定向到 tmp_path/refspec_tmp,并预置仓库文件。"""
    import tempfile
    dest = tmp_path / "refspec_tmp"
    dest.mkdir()

    def fake_mkdtemp(prefix=None, dir=None):
        assert prefix and prefix.startswith("refspec_")
        return str(dest)
    monkeypatch.setattr(tempfile, "mkdtemp", fake_mkdtemp)
    return dest


def test_clone_and_extract_success(fake_clone_tmp, fake_subprocess, tmp_path):
    (fake_clone_tmp / "snappy.spec").write_text("Name: snappy\n")
    (fake_clone_tmp / "other.spec").write_text("Name: other\n")
    (fake_clone_tmp / "snappy.yaml").write_text("yaml: 1\n")
    (fake_clone_tmp / "fix.patch").write_text("--- patch\n")
    (fake_clone_tmp / "conf.inc").write_text("%include\n")
    (fake_clone_tmp / "macros.macros").write_text("%macros\n")
    (fake_clone_tmp / "README.md").write_text("not copied\n")
    fake_subprocess.when("git clone", returncode=0)
    out = tmp_path / "out"
    assert mod._clone_and_extract("snappy", out) is True
    names = sorted(f.name for f in out.iterdir())
    assert names == ["conf.inc", "fix.patch", "macros.macros", "snappy.spec", "snappy.yaml"]
    assert (out / "snappy.spec").read_text() == "Name: snappy\n"


def test_clone_and_extract_fallback_spec_name(fake_clone_tmp, fake_subprocess, tmp_path):
    # 无 <pkgname>.spec 时接受任意 .spec
    (fake_clone_tmp / "weird.spec").write_text("Name: weird\n")
    fake_subprocess.when("git clone", returncode=0)
    out = tmp_path / "out"
    assert mod._clone_and_extract("snappy", out) is True
    assert (out / "weird.spec").exists()


def test_clone_and_extract_no_spec(fake_clone_tmp, fake_subprocess, tmp_path):
    (fake_clone_tmp / "README.md").write_text("nothing\n")
    fake_subprocess.when("git clone", returncode=0)
    assert mod._clone_and_extract("snappy", tmp_path / "out") is False


def test_clone_and_extract_branch_fallback_to_default(fake_clone_tmp, fake_subprocess, tmp_path):
    (fake_clone_tmp / "snappy.spec").write_text("Name: snappy\n")
    # 指定分支 clone 失败 → 回退默认分支 clone 成功
    fake_subprocess.when(lambda c: c.startswith("git clone --depth=1 --branch"),
                         returncode=128, stderr="branch not found")
    fake_subprocess.when(lambda c: "git clone --depth=1 https" in c, returncode=0)
    out = tmp_path / "out"
    assert mod._clone_and_extract("snappy", out, target_branch="openEuler-24.03-LTS-SP3") is True
    assert fake_subprocess.called_with("--branch openEuler-24.03-LTS-SP3")


def test_clone_and_extract_all_attempts_fail(fake_clone_tmp, fake_subprocess, tmp_path, monkeypatch):
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)   # 跳过退避等待
    fake_subprocess.when("git clone", returncode=128, stderr="denied")
    assert mod._clone_and_extract("snappy", tmp_path / "out") is False


def test_clone_and_extract_mkdtemp_failure(monkeypatch, tmp_path):
    import tempfile
    monkeypatch.setattr(tempfile, "mkdtemp",
                        lambda **kw: (_ for _ in ()).throw(OSError("no space")))
    assert mod._clone_and_extract("snappy", tmp_path / "out") is False


# ─────────────────────────────────────────────
# 主流程 fetch_reference_spec
# ─────────────────────────────────────────────

def test_fetch_reference_spec_cached(tmp_path, capsys):
    out = tmp_path / "ref"
    out.mkdir()
    (out / "snappy.spec").write_text("Name: snappy\n")
    (out / "snappy.yaml").write_text("y\n")
    result = mod.fetch_reference_spec("snappy", out)
    assert result == {"found": True, "cached": True, "files": ["snappy.spec", "snappy.yaml"]}
    assert "already cached" in capsys.readouterr().err


def test_fetch_reference_spec_network_error(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "_repo_exists", lambda p: None)
    result = mod.fetch_reference_spec("snappy", tmp_path / "ref")
    assert result["found"] is False
    assert result["reason"] == "network_error"
    assert "Cannot reach gitcode.com" in result["detail"]


def test_fetch_reference_spec_repo_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "_repo_exists", lambda p: False)
    result = mod.fetch_reference_spec("snappy", tmp_path / "ref")
    assert result["found"] is False
    assert result["reason"] == "repo_not_found"
    assert "src-openeuler/snappy" in result["detail"]


def test_fetch_reference_spec_success(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "_repo_exists", lambda p: True)
    def fake_clone(pkgname, output_dir, target_branch=""):
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "snappy.spec").write_text("Name: snappy\n")
        (output_dir / "a.yaml").write_text("y\n")
        return True
    monkeypatch.setattr(mod, "_clone_and_extract", fake_clone)
    out = tmp_path / "ref"
    result = mod.fetch_reference_spec("snappy", out, target_branch="openEuler-24.03-LTS-SP3")
    assert result["found"] is True
    assert result["cached"] is False
    assert result["files"] == ["a.yaml", "snappy.spec"]
    assert result["source"] == "gitcode.com/src-openeuler"


def test_fetch_reference_spec_no_spec_file(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "_repo_exists", lambda p: True)
    monkeypatch.setattr(mod, "_clone_and_extract", lambda p, d, b="": False)
    result = mod.fetch_reference_spec("snappy", tmp_path / "ref")
    assert result["found"] is False
    assert result["reason"] == "no_spec_file"


# ─────────────────────────────────────────────
# main()
# ─────────────────────────────────────────────

def test_main(tmp_path, monkeypatch, capsys):
    canned = {"found": True, "cached": True, "files": ["x.spec"]}
    captured = {}
    def fake_fetch(pkgname, output_dir, target_branch=""):
        captured.update(pkgname=pkgname, target_branch=target_branch)
        return canned
    monkeypatch.setattr(mod, "fetch_reference_spec", fake_fetch)
    out_json = tmp_path / "sub" / "result.json"
    monkeypatch.setattr(sys, "argv", [
        "fetch_reference_spec.py", "--pkgname", "snappy",
        "--output-dir", str(tmp_path / "ref"),
        "--target-branch", "openEuler-24.03-LTS-SP3",
        "--output-json", str(out_json),
    ])
    assert mod.main() == 0
    assert captured == {"pkgname": "snappy", "target_branch": "openEuler-24.03-LTS-SP3"}
    assert json.loads(out_json.read_text()) == canned
    assert "cached" in capsys.readouterr().out
