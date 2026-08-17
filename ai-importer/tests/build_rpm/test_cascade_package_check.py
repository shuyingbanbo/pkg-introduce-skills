"""cascade_package_check.py — L0/L5/L2/L1/L3/L4 级联存在性检查。

纯函数(chroot 拆分/版本防线/候选构造)直接断言;
urllib/git/dnf 经 mock 后逐层构造命中场景。
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sys.path.insert(0, str(SCRIPT_DIRS["build_rpm"]))
import fetch_reference_spec  # noqa: E402  预载,确保 sys.modules 中存在
cascade = load_module("cascade_package_check",
                      SCRIPT_DIRS["build_rpm"] / "cascade_package_check.py")


def _patch_find_best_branch(monkeypatch, fake):
    """patch fetch_reference_spec._find_best_branch。

    cascade._check_src_openeuler 在调用时执行 `from fetch_reference_spec
    import _find_best_branch`,从 sys.modules 解析;其他测试文件若用
    load_module 重载了该模块(替换 sys.modules 条目),patch 需落在当前
    sys.modules 里的对象上。
    """
    frs = sys.modules["fetch_reference_spec"]
    monkeypatch.setattr(frs, "_find_best_branch", fake)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install_urlopen(monkeypatch, handler):
    def fake(req, timeout=None):
        return handler(req, timeout)
    monkeypatch.setattr(cascade.urllib.request, "urlopen", fake)
    return fake


# ─────────────────────────────────────────────
# chroot 拆分与匹配
# ─────────────────────────────────────────────

@pytest.mark.parametrize("chroot,expected", [
    ("openeuler-24.03_LTS_SP3-x86_64", ("openeuler-24.03-lts-sp3", "x86_64")),
    ("openeuler-22.03_LTS-aarch64", ("openeuler-22.03-lts", "aarch64")),
    ("openeuler-24.03_LTS", ("openeuler-24.03-lts", "")),
    ("", ("", "")),
    ("openEuler-24.03-LTS-SP3-X86_64/", ("openeuler-24.03-lts-sp3", "x86_64")),
    ("openeuler-24.03_LTS-noarch", ("openeuler-24.03-lts", "noarch")),
])
def test_split_chroot(chroot, expected):
    assert cascade._split_chroot(chroot) == expected


@pytest.mark.parametrize("build_chroot,target_base,target_arch,expected", [
    ("openeuler-24.03-lts-sp3-x86_64", "openeuler-24.03-lts-sp3", "x86_64", True),
    ("openeuler-24.03-lts-sp3-x86_64", "openeuler-24.03-lts-sp3", "aarch64", False),
    ("openeuler-24.03-lts-sp3-aarch64", "openeuler-24.03", "aarch64", True),
    ("openeuler-22.03-lts-x86_64", "openeuler-24.03", "", False),   # OS 前缀不匹配
    ("openeuler-24.03-lts-sp3", "openeuler-24.03-lts-sp3", "x86_64", True),  # build 无架构时放行
    ("openeuler-24.03-lts-sp3-x86_64", "openeuler-24.03-lts-sp3", "", True),  # target 无架构时不校验
])
def test_chroot_matches(build_chroot, target_base, target_arch, expected):
    assert cascade._chroot_matches(build_chroot, target_base, target_arch) is expected


# ─────────────────────────────────────────────
# 版本防线
# ─────────────────────────────────────────────

@pytest.mark.parametrize("requirement,expected", [
    (">=2.0,<3", "2.0"),
    (">1.0,>=2.0", "2.0"),      # 取下界中最高者
    ("==1.5", "1.5"),
    (">0.8", "0.8"),
    ("<=2.0", ""),              # 只有上界 → 无下界
    ("<2.0", ""),
    ("~=1.0", ""),              # 无法解析 → 空
    ("", ""),
    (">=0.8,>=0.6", "0.8"),
])
def test_requirement_min_version(requirement, expected):
    assert cascade._requirement_min_version(requirement) == expected


@pytest.mark.parametrize("found,requested,requirement,expected", [
    ("1.5", "", "", True),        # 无约束 → 存在即满足
    ("", "1.0", "", False),       # 无版本号 → 不满足
    ("1.5", "1.0", "", True),
    ("1.0", "1.5", "", False),
    ("2.0", "", ">=1.5", True),
    ("1.0", "", ">=1.5", False),
    ("1.5", "1.0", ">=1.2,<2", True),
    ("1.9", "1.0", ">=1.2,<1.5", False),   # requested 满足但 requirement 不满足
    ("0.9", "1.0", ">=0.5", False),        # requirement 满足但 requested 不满足
    ("1.0", "", "~=1.0", False),           # 无法解析 → 保守不满足
    ("1.5", "1.0", "!=1.5", False),        # unknown → False
])
def test_version_satisfies(found, requested, requirement, expected):
    assert cascade._version_satisfies(found, requested, requirement) is expected


# ─────────────────────────────────────────────
# Level 1: EUR fulltext 搜索
# ─────────────────────────────────────────────

EUR_HTML = """
<html><body>
<a href="/coprs/owner1/proj1/">proj1</a>
<a href="/coprs/owner1/proj1/">dup</a>
<a href="/coprs/owner2/proj2/">proj2</a>
<a href="/other/">x</a>
</body></html>
"""


def test_eur_fulltext_search_parses_links(monkeypatch):
    def handler(req, timeout):
        assert "fulltext=requests" in req.full_url
        assert "packagename=requests" in req.full_url
        return _FakeResponse(EUR_HTML.encode())
    _install_urlopen(monkeypatch, handler)
    projects = cascade._eur_fulltext_search("requests")
    assert projects == [
        {"owner": "owner1", "project": "proj1"},
        {"owner": "owner2", "project": "proj2"},
    ]


def test_eur_fulltext_search_network_error(monkeypatch):
    def handler(req, timeout):
        raise OSError("boom")
    _install_urlopen(monkeypatch, handler)
    assert cascade._eur_fulltext_search("requests") == []


@pytest.mark.parametrize("build_dir,pkgname,expected", [
    ("python3-requests", "requests", True),
    ("python-requests", "requests", True),
    ("requests", "requests", True),
    ("nodejs-lodash", "lodash", True),
    ("golang-x", "x", True),
    ("Python3-Requests", "REQUESTS", True),       # 大小写不敏感
    ("python_requests", "requests", True),        # 下划线归一
    ("foo", "bar", False),
    ("python-requests", "python3-requests", False),
    ("requests", "python3-requests", False),
])
def test_eur_pkgname_matches(build_dir, pkgname, expected):
    assert cascade._eur_pkgname_matches(build_dir, pkgname) is expected


def _eur_scan_handler(version="2.28.1", srpm=None, binary_files=True,
                      build_pkgname="python-requests"):
    """构造 results → chroot → build 三级目录的假 HTML。

    srpm=None 表示默认 SRPM 名;srpm="" 表示无 SRPM(仅二进制)。
    """
    if srpm is None:
        srpm = f"python-requests-{version}-1.src.rpm"
    srpm_link = f'<a href="{srpm}">s</a>' if srpm else ""
    binary = "python3-requests-2.28.1-1.noarch.rpm" if binary_files else ""

    def handler(req, timeout):
        url = req.full_url
        if url.endswith("/results/owner1/proj1/"):
            return _FakeResponse(
                '<a href="openeuler-24.03-lts-sp3-x86_64/">a</a>'
                '<a href="fedora-39-x86_64/">b</a>'
                '<a href="../">up</a>'.encode())
        if url.endswith("/openeuler-24.03-lts-sp3-x86_64/"):
            return _FakeResponse(
                f'<a href="012345-{build_pkgname}/">b</a>'
                '<a href="098765-other-pkg/">o</a>'.encode())
        if url.endswith("/012345-python-requests/"):
            return _FakeResponse(
                f'{srpm_link}<a href="{binary}">b</a>'.encode())
        if url.endswith("/098765-other-pkg/"):
            return _FakeResponse('<a href="other-1.0-1.src.rpm">x</a>'.encode())
        raise AssertionError(f"unexpected url: {url}")
    return handler


def test_scan_eur_results_hit(monkeypatch):
    _install_urlopen(monkeypatch, _eur_scan_handler())
    match = cascade._scan_eur_results(
        [{"owner": "owner1", "project": "proj1"}], "requests",
        target_chroot="openeuler-24.03_LTS_SP3-x86_64", target_version="2.0.0",
    )
    assert match is not None
    assert match["level"] == 1
    assert match["decision"] == "reuse_eur_srpm"
    assert match["version"] == "2.28.1"
    assert match["chroot"] == "openeuler-24.03-lts-sp3-x86_64"
    assert match["chroot_matched"] is True
    assert match["srpm_url"].endswith("012345-python-requests/python-requests-2.28.1-1.src.rpm")
    assert match["srpm_file"] == "python-requests-2.28.1-1.src.rpm"
    assert len(match["binary_rpm_urls"]) == 1
    assert match["binary_rpm_files"] == ["python3-requests-2.28.1-1.noarch.rpm"]
    assert match["eur_owner"] == "owner1"
    assert match["eur_project"] == "proj1"


def test_scan_eur_results_version_too_low(monkeypatch):
    _install_urlopen(monkeypatch, _eur_scan_handler())
    # EUR 版本 2.28.1 不满足目标 3.0.0 → 保守跳过 → None
    assert cascade._scan_eur_results(
        [{"owner": "owner1", "project": "proj1"}], "requests",
        target_chroot="openeuler-24.03_LTS_SP3-x86_64", target_version="3.0.0",
    ) is None


def test_scan_eur_results_no_target_chroot(monkeypatch):
    _install_urlopen(monkeypatch, _eur_scan_handler())
    match = cascade._scan_eur_results([{"owner": "owner1", "project": "proj1"}], "requests")
    assert match is not None
    assert match["chroot_matched"] is False      # 未给 target → 不算精确匹配
    assert match["decision"] == "reuse_eur_srpm"


def test_scan_eur_results_unparseable_version_skipped(monkeypatch):
    _install_urlopen(monkeypatch, _eur_scan_handler(srpm="weirdname.src.rpm"))
    # SRPM 名解析不出版本,且要求 target_version → 跳过
    assert cascade._scan_eur_results(
        [{"owner": "owner1", "project": "proj1"}], "requests",
        target_version="2.0.0",
    ) is None


def test_scan_eur_results_binary_only(monkeypatch):
    _install_urlopen(monkeypatch, _eur_scan_handler(srpm=""))
    match = cascade._scan_eur_results([{"owner": "owner1", "project": "proj1"}], "requests")
    assert match is not None
    assert match["srpm_url"] is None
    assert match["version"] is None
    assert len(match["binary_rpm_urls"]) == 1


def test_scan_eur_results_pkgname_mismatch(monkeypatch):
    _install_urlopen(monkeypatch, _eur_scan_handler(build_pkgname="nothing"))
    assert cascade._scan_eur_results(
        [{"owner": "owner1", "project": "proj1"}], "requests",
    ) is None


def test_scan_eur_results_results_page_error(monkeypatch):
    def handler(req, timeout):
        raise OSError("boom")
    _install_urlopen(monkeypatch, handler)
    assert cascade._scan_eur_results(
        [{"owner": "owner1", "project": "proj1"}], "requests",
    ) is None


# ─────────────────────────────────────────────
# Level 2: openEuler 目标版本
# ─────────────────────────────────────────────

def test_check_target_version_reuse(monkeypatch):
    monkeypatch.setattr(cascade._checker, "check_existing_package", lambda *a, **kw: {
        "official": {"exists": True, "meets_need": True,
                     "highest": {"name": "python3-requests", "version": "2.28.1"}},
        "reason": "官方源已有满足要求的版本",
    })
    result = cascade._check_target_version("requests", "python", "openeuler-24.03_LTS_SP3-x86_64", "2.0", "")
    assert result["level"] == 2
    assert result["decision"] == "reuse_official"
    assert result["rpm_name"] == "python3-requests"
    assert result["version"] == "2.28.1"
    assert result["source"] == "openEuler openeuler-24.03_LTS_SP3-x86_64"


def test_check_target_version_exists_but_not_meet(monkeypatch):
    monkeypatch.setattr(cascade._checker, "check_existing_package", lambda *a, **kw: {
        "official": {"exists": True, "meets_need": False,
                     "highest": {"name": "foo", "version": "1.0"}},
        "reason": "r",
    })
    result = cascade._check_target_version("foo", "", "openeuler-24.03_LTS_SP3-x86_64", "2.0", "")
    assert result["decision"] == "evaluate"
    assert result["level"] == 2


def test_check_target_version_not_found(monkeypatch):
    captured = {}
    def fake(pkgname, **kw):
        captured.update(kw)
        return {"official": {"exists": False}, "reason": "r"}
    monkeypatch.setattr(cascade._checker, "check_existing_package", fake)
    assert cascade._check_target_version("foo", "python", "openeuler-24.03_LTS_SP3-x86_64",
                                         "1.0", ">=0.5") is None
    assert captured["chroot"] == "openeuler-24.03_LTS_SP3-x86_64"
    assert captured["version"] == "1.0"
    assert captured["requirement"] == ">=0.5"


# ─────────────────────────────────────────────
# Level 3: gitcode src-openeuler
# ─────────────────────────────────────────────

@pytest.mark.parametrize("pkgname,lang,expected", [
    ("requests", "python", ["requests", "python-requests", "python3-requests"]),
    ("lodash", "nodejs", ["lodash", "nodejs-lodash"]),
    ("snappy", "c", ["snappy"]),
    ("snappy", "cpp", ["snappy"]),
    ("github.com/foo/bar", "go", ["github.com/foo/bar", "golang-github.com/foo/bar"]),
    ("golang-foo", "go", ["golang-foo"]),
    ("serde", "rust", ["serde", "rust-serde"]),
    ("rust-foo", "rust", ["rust-foo"]),
    ("lib", "", ["lib"]),
])
def test_build_gitcode_candidates(pkgname, lang, expected):
    assert cascade._build_gitcode_candidates(pkgname, lang) == expected


def test_git_ls_remote_exists(fake_subprocess):
    fake_subprocess.when("git", stdout="abc\trefs/heads/master\n", returncode=0)
    assert cascade._git_ls_remote("foo") is True
    assert fake_subprocess.called_with("git ls-remote --heads https://gitcode.com/src-openeuler/foo.git")


def test_git_ls_remote_not_found(fake_subprocess):
    fake_subprocess.when("git", returncode=1, stderr="fatal: repository not found")
    assert cascade._git_ls_remote("foo") is False


def test_git_ls_remote_other_failure(fake_subprocess):
    fake_subprocess.when("git", returncode=1, stderr="connection reset by peer")
    assert cascade._git_ls_remote("foo") is None


def test_git_ls_remote_timeout(fake_subprocess):
    fake_subprocess.when("git", exc=subprocess.TimeoutExpired("git", 10))
    assert cascade._git_ls_remote("foo") is None


def test_git_ls_remote_unexpected_exception(fake_subprocess):
    fake_subprocess.when("git", exc=OSError("no git"))
    assert cascade._git_ls_remote("foo") is None


def test_check_src_openeuler_found(monkeypatch):
    monkeypatch.setattr(cascade, "_git_ls_remote", lambda c: True)
    result = cascade._check_src_openeuler("requests", "python")
    assert result["level"] == 3
    assert result["decision"] == "introduce_new_with_ref"
    assert result["repo_name"] == "requests"
    assert result["gitcode_repo"] == "https://gitcode.com/src-openeuler/requests.git"
    assert "target_branch" not in result


def test_check_src_openeuler_with_target_branch(monkeypatch):
    monkeypatch.setattr(cascade, "_git_ls_remote", lambda c: True)
    _patch_find_best_branch(monkeypatch, lambda pkg, target: "openEuler-24.03-LTS-SP3")
    result = cascade._check_src_openeuler("requests", "python", target="openeuler-24.03_LTS_SP3-x86_64")
    assert result["target_branch"] == "openEuler-24.03-LTS-SP3"


def test_check_src_openeuler_candidate_fallthrough(monkeypatch):
    results = {"requests": False, "python-requests": True}
    monkeypatch.setattr(cascade, "_git_ls_remote", lambda c: results[c])
    result = cascade._check_src_openeuler("requests", "python")
    assert result["repo_name"] == "python-requests"


def test_check_src_openeuler_network_error_then_found(monkeypatch):
    results = {"requests": None, "python-requests": True}
    monkeypatch.setattr(cascade, "_git_ls_remote", lambda c: results[c])
    result = cascade._check_src_openeuler("requests", "python")
    assert result["repo_name"] == "python-requests"


def test_check_src_openeuler_all_miss(monkeypatch):
    monkeypatch.setattr(cascade, "_git_ls_remote", lambda c: False)
    assert cascade._check_src_openeuler("requests", "python") is None


# ─────────────────────────────────────────────
# Level 5: additional_repos(外挂源)
# ─────────────────────────────────────────────

def test_get_project_additional_repos_no_creds():
    assert cascade._get_project_additional_repos("", "", "", "", "") == []
    assert cascade._get_project_additional_repos("http://x", "o", "p", "", "") == []


def test_get_project_additional_repos_ok(monkeypatch):
    monkeypatch.setattr(cascade, "_ADDITIONAL_REPOS_CACHE", {})
    def handler(req, timeout):
        return _FakeResponse(json.dumps(
            {"additional_repos": [" https://a/b ", "", "https://c/$basearch/", 42, None]}
        ).encode())
    _install_urlopen(monkeypatch, handler)
    repos = cascade._get_project_additional_repos("http://copr:5000", "o", "p", "l", "t")
    assert repos == ["https://a/b", "https://c/$basearch/"]


def test_get_project_additional_repos_cached(monkeypatch):
    monkeypatch.setattr(cascade, "_ADDITIONAL_REPOS_CACHE", {})
    calls = []
    def handler(req, timeout):
        calls.append(1)
        return _FakeResponse(json.dumps({"additional_repos": ["https://a"]}).encode())
    _install_urlopen(monkeypatch, handler)
    assert cascade._get_project_additional_repos("http://copr:5000", "o", "p", "l", "t") == ["https://a"]
    assert cascade._get_project_additional_repos("http://copr:5000", "o", "p", "l", "t") == ["https://a"]
    assert len(calls) == 1     # 进程内缓存


def test_get_project_additional_repos_api_error(monkeypatch, capsys):
    monkeypatch.setattr(cascade, "_ADDITIONAL_REPOS_CACHE", {})
    def handler(req, timeout):
        raise OSError("boom")
    _install_urlopen(monkeypatch, handler)
    assert cascade._get_project_additional_repos("http://copr:5000", "o", "p", "l", "t") == []
    assert "additional_repos" in capsys.readouterr().err


def test_check_additional_repos_hit(fake_subprocess):
    fake_subprocess.when("dnf repoquery", stdout="1.5.0\n2.0.0\n")
    result = cascade._check_additional_repos(
        "requests", "python", ["https://mirror.example/ros/$basearch/"],
        "openeuler-24.03_LTS_SP3-x86_64", "2.0.0", "",
    )
    assert result["level"] == 5
    assert result["decision"] == "reuse_additional_repo"
    assert result["rpm_name"] == "python-requests"
    assert result["version"] == "2.0.0"          # 取最高版本
    assert result["source"] == "https://mirror.example/ros/x86_64/"   # $basearch 已替换
    assert result["match"]["repo"] == "cascade-extra-0"


def test_check_additional_repos_version_too_low(fake_subprocess):
    fake_subprocess.when("dnf repoquery", stdout="1.5.0\n")
    assert cascade._check_additional_repos(
        "foo", "", ["https://mirror.example/"],
        "openeuler-24.03_LTS_SP3-x86_64", "2.0.0", "",
    ) is None


def test_check_additional_repos_requirement_guard(fake_subprocess):
    fake_subprocess.when("dnf repoquery", stdout="2.0.0\n")
    assert cascade._check_additional_repos(
        "foo", "", ["https://mirror.example/"],
        "openeuler-24.03_LTS_SP3-x86_64", "", ">=2.5.0",
    ) is None


def test_check_additional_repos_no_constraints_hit(fake_subprocess):
    fake_subprocess.when("dnf repoquery", stdout="0.1.0\n")
    result = cascade._check_additional_repos(
        "foo", "", ["https://mirror.example/"],
        "openeuler-24.03_LTS_SP3-x86_64", "", "",
    )
    assert result["version"] == "0.1.0"


def test_check_additional_repos_skips_non_http(fake_subprocess):
    assert cascade._check_additional_repos(
        "foo", "", ["copr://someone/proj"],
        "openeuler-24.03_LTS_SP3-x86_64", "", "",
    ) is None
    assert fake_subprocess.calls == []


def test_check_additional_repos_query_failure(fake_subprocess, capsys):
    fake_subprocess.when("dnf repoquery", returncode=1, stderr="repodata missing")
    assert cascade._check_additional_repos(
        "foo", "", ["https://mirror.example/"],
        "openeuler-24.03_LTS_SP3-x86_64", "", "",
    ) is None
    assert "cascade" in capsys.readouterr().err


def test_check_additional_repos_query_exception(fake_subprocess, capsys):
    fake_subprocess.when("dnf repoquery", exc=FileNotFoundError("no dnf"))
    assert cascade._check_additional_repos(
        "foo", "", ["https://mirror.example/"],
        "openeuler-24.03_LTS_SP3-x86_64", "", "",
    ) is None
    assert "cascade" in capsys.readouterr().err


def test_check_additional_repos_forcearch(monkeypatch, fake_subprocess):
    monkeypatch.setattr(cascade.platform, "machine", lambda: "aarch64")
    fake_subprocess.when("dnf repoquery", stdout="1.0\n")
    cascade._check_additional_repos(
        "foo", "", ["https://mirror.example/$basearch/"],
        "openeuler-24.03_LTS_SP3-x86_64", "", "",
    )
    assert fake_subprocess.called_with("--forcearch=x86_64")


def test_check_additional_repos_same_arch_no_forcearch(monkeypatch, fake_subprocess):
    monkeypatch.setattr(cascade.platform, "machine", lambda: "x86_64")
    fake_subprocess.when("dnf repoquery", stdout="1.0\n")
    cascade._check_additional_repos(
        "foo", "", ["https://mirror.example/"],
        "openeuler-24.03_LTS_SP3-x86_64", "", "",
    )
    assert not fake_subprocess.called_with("--forcearch")


# ─────────────────────────────────────────────
# Level 0: 用户 COPR project
# ─────────────────────────────────────────────

def _copr_items(items):
    return _FakeResponse(json.dumps({"items": items}).encode())


def test_check_user_copr_project_no_creds():
    assert cascade._check_user_copr_project(
        "foo", "", "o", "p", "l", "", target="openeuler-24.03_LTS_SP3-x86_64",
    ) is None


def test_check_user_copr_project_empty_target():
    assert cascade._check_user_copr_project(
        "foo", "http://c", "o", "p", "l", "t", target="",
    ) is None


def test_check_user_copr_project_hit(monkeypatch):
    captured = {}
    def handler(req, timeout):
        captured["url"] = req.full_url
        return _copr_items([
            {"state": "succeeded", "chroots": ["openeuler-24.03-lts-sp3-x86_64"],
             "source_package": {"version": "2.28.1"}},
            {"state": "failed", "chroots": ["openeuler-24.03-lts-sp3-x86_64"],
             "source_package": {"version": "9.9.9"}},
        ])
    _install_urlopen(monkeypatch, handler)
    result = cascade._check_user_copr_project(
        "requests", "http://copr:5000", "o", "p", "l", "t",
        target="openeuler-24.03_LTS_SP3-x86_64", lang="python",
        version="2.0.0", requirement="",
    )
    assert result["level"] == 0
    assert result["decision"] == "reuse_copr_project"
    assert result["version"] == "2.28.1"
    assert result["source"] == "o/p"
    assert result["match"]["chroot"] == "openeuler-24.03-lts-sp3-x86_64"
    # python 语言按 SRPM 名查询
    assert "packagename=python-requests" in captured["url"]


def test_check_user_copr_project_version_too_low(monkeypatch):
    def handler(req, timeout):
        return _copr_items([
            {"state": "succeeded", "chroots": ["openeuler-24.03-lts-sp3-x86_64"],
             "source_package": {"version": "1.5.0"}},
        ])
    _install_urlopen(monkeypatch, handler)
    assert cascade._check_user_copr_project(
        "requests", "http://c", "o", "p", "l", "t",
        target="openeuler-24.03_LTS_SP3-x86_64", version="2.0.0",
    ) is None


def test_check_user_copr_project_chroot_mismatch(monkeypatch):
    def handler(req, timeout):
        return _copr_items([
            {"state": "succeeded", "chroots": ["openeuler-22.03-lts-x86_64"],
             "source_package": {"version": "2.28.1"}},
        ])
    _install_urlopen(monkeypatch, handler)
    assert cascade._check_user_copr_project(
        "requests", "http://c", "o", "p", "l", "t",
        target="openeuler-24.03_LTS_SP3-x86_64", version="2.0.0",
    ) is None


def test_check_user_copr_project_arch_mismatch(monkeypatch):
    def handler(req, timeout):
        return _copr_items([
            {"state": "succeeded", "chroots": ["openeuler-24.03-lts-sp3-aarch64"],
             "source_package": {"version": "2.28.1"}},
        ])
    _install_urlopen(monkeypatch, handler)
    assert cascade._check_user_copr_project(
        "requests", "http://c", "o", "p", "l", "t",
        target="openeuler-24.03_LTS_SP3-x86_64", version="2.0.0",
    ) is None


def test_check_user_copr_project_requirement_guard(monkeypatch):
    def handler(req, timeout):
        return _copr_items([
            {"state": "succeeded", "chroots": ["openeuler-24.03-lts-sp3-x86_64"],
             "source_package": {"version": "2.0.1"}},
        ])
    _install_urlopen(monkeypatch, handler)
    result = cascade._check_user_copr_project(
        "foo", "http://c", "o", "p", "l", "t",
        target="openeuler-24.03_LTS_SP3-x86_64", requirement=">=2.0,<2.1",
    )
    assert result is not None and result["version"] == "2.0.1"


def test_check_user_copr_project_api_error(monkeypatch):
    def handler(req, timeout):
        raise OSError("boom")
    _install_urlopen(monkeypatch, handler)
    assert cascade._check_user_copr_project(
        "foo", "http://c", "o", "p", "l", "t",
        target="openeuler-24.03_LTS_SP3-x86_64",
    ) is None


# ─────────────────────────────────────────────
# 主入口 check_package_existence
# ─────────────────────────────────────────────

@pytest.fixture
def level_mocks(monkeypatch):
    """逐层 mock,默认全部未命中;测试按需覆盖。"""
    monkeypatch.setattr(cascade, "_check_user_copr_project", lambda *a, **kw: None)
    monkeypatch.setattr(cascade, "_get_project_additional_repos", lambda *a, **kw: [])
    monkeypatch.setattr(cascade, "_check_additional_repos", lambda *a, **kw: None)
    monkeypatch.setattr(cascade, "_check_target_version", lambda *a, **kw: None)
    monkeypatch.setattr(cascade, "_eur_fulltext_search", lambda p: [])
    monkeypatch.setattr(cascade, "_scan_eur_results", lambda *a, **kw: None)
    monkeypatch.setattr(cascade, "_check_src_openeuler", lambda *a, **kw: None)


def test_check_package_existence_level0(level_mocks, monkeypatch):
    monkeypatch.setattr(cascade, "_check_user_copr_project",
                        lambda *a, **kw: {"level": 0, "decision": "reuse_copr_project",
                                          "rpm_name": "foo", "version": "1.0",
                                          "source": "o/p", "match": {"source": "o/p"}})
    result = cascade.check_package_existence("foo")
    assert result["level"] == 0
    assert result["decision"] == "reuse_copr_project"
    assert result["match"] == {"source": "o/p"}


def test_check_package_existence_level5(level_mocks, monkeypatch):
    extra = {"level": 5, "decision": "reuse_additional_repo",
             "rpm_name": "foo", "version": "1.0", "source": "https://x",
             "match": {"source": "https://x", "version": "1.0", "repo": "cascade-extra-0"}}
    monkeypatch.setattr(cascade, "_get_project_additional_repos", lambda *a, **kw: ["https://x"])
    monkeypatch.setattr(cascade, "_check_additional_repos", lambda *a, **kw: extra)
    result = cascade.check_package_existence(
        "foo", copr_url="http://c", copr_owner="o", copr_project="p",
        copr_login="l", copr_token="t",
    )
    assert result["level"] == 5
    assert result["decision"] == "reuse_additional_repo"
    assert result["match"]["repo"] == "cascade-extra-0"


def test_check_package_existence_level2(level_mocks, monkeypatch):
    monkeypatch.setattr(cascade, "_check_target_version",
                        lambda *a, **kw: {"level": 2, "decision": "reuse_official",
                                          "rpm_name": "foo", "version": "1.0",
                                          "source": "openEuler x", "reason": "r"})
    result = cascade.check_package_existence("foo", target="openeuler-24.03_LTS_SP3-x86_64")
    assert result["level"] == 2
    assert result["decision"] == "reuse_official"


def test_check_package_existence_level1_matched(level_mocks, monkeypatch):
    eur = {"level": 1, "eur_owner": "o", "eur_project": "p",
           "srpm_url": "https://x/a.src.rpm", "srpm_file": "a.src.rpm",
           "binary_rpm_urls": [], "binary_rpm_files": [],
           "version": "1.0", "chroot": "openeuler-24.03-lts-sp3-x86_64",
           "chroot_matched": True}
    monkeypatch.setattr(cascade, "_eur_fulltext_search", lambda p: [{"owner": "o", "project": "p"}])
    monkeypatch.setattr(cascade, "_scan_eur_results", lambda *a, **kw: eur)
    result = cascade.check_package_existence("foo", target="openeuler-24.03_LTS_SP3-x86_64")
    assert result["level"] == 1
    assert result["decision"] == "reuse_eur_srpm"
    assert result["match"] == eur
    assert result["reference"] is None


def test_check_package_existence_level1_unmatched_chroot(level_mocks, monkeypatch):
    eur = {"level": 1, "eur_owner": "o", "eur_project": "p",
           "srpm_url": "https://x/a.src.rpm", "srpm_file": "a.src.rpm",
           "binary_rpm_urls": [], "binary_rpm_files": [],
           "version": "1.0", "chroot": "fedora-39-x86_64",
           "chroot_matched": False}
    monkeypatch.setattr(cascade, "_eur_fulltext_search", lambda p: [{"owner": "o", "project": "p"}])
    monkeypatch.setattr(cascade, "_scan_eur_results", lambda *a, **kw: eur)
    result = cascade.check_package_existence("foo", target="openeuler-24.03_LTS_SP3-x86_64")
    # chroot 不匹配 → 降级为参考源
    assert result["level"] == 1
    assert result["decision"] == "introduce_new_with_ref"
    assert result["match"] is None
    assert result["reference"]["source"] == "eur"
    assert result["reference"]["srpm_url"] == "https://x/a.src.rpm"
    assert result["reference"]["chroot"] == "fedora-39-x86_64"


def test_check_package_existence_level3(level_mocks, monkeypatch):
    monkeypatch.setattr(cascade, "_check_src_openeuler",
                        lambda *a, **kw: {"level": 3, "decision": "introduce_new_with_ref",
                                          "repo_name": "foo",
                                          "gitcode_repo": "https://gitcode.com/src-openeuler/foo.git"})
    result = cascade.check_package_existence("foo")
    assert result["level"] == 3
    assert result["decision"] == "introduce_new_with_ref"
    assert result["match"] is None


def test_check_package_existence_level4(level_mocks):
    result = cascade.check_package_existence("foo")
    assert result["level"] == 4
    assert result["decision"] == "introduce_new"
    assert result["match"] is None
    assert result["reference"] is None
    assert result["pkgname"] == "foo"


def test_check_package_existence_derives_version_from_requirement(level_mocks, monkeypatch):
    captured = {}
    def fake_l0(pkgname, copr_url, owner, project, login, token, target, lang,
                version="", requirement=""):
        captured["version"] = version
        return None
    monkeypatch.setattr(cascade, "_check_user_copr_project", fake_l0)
    cascade.check_package_existence("foo", requirement=">=1.2.3")
    assert captured["version"] == "1.2.3"     # 从 requirement 推导下界


def test_check_package_existence_no_version_no_requirement(level_mocks, monkeypatch):
    captured = {}
    def fake_l0(*a, **kw):
        captured["version"] = kw.get("version")
        return None
    monkeypatch.setattr(cascade, "_check_user_copr_project", fake_l0)
    cascade.check_package_existence("foo")
    assert captured["version"] == ""


# ─────────────────────────────────────────────
# main()
# ─────────────────────────────────────────────

@pytest.mark.parametrize("decision,expected_code", [
    ("reuse_eur_srpm", 0),
    ("reuse_official", 0),
    ("reuse_copr_project", 0),
    ("reuse_additional_repo", 0),
    ("introduce_new_with_ref", 3),
    ("introduce_new", 4),
])
def test_main_exit_codes(monkeypatch, capsys, decision, expected_code):
    monkeypatch.setattr(cascade, "check_package_existence",
                        lambda *a, **kw: {"pkgname": "foo", "level": 0,
                                          "decision": decision, "match": None,
                                          "reference": None})
    monkeypatch.setattr(sys, "argv", ["cascade_package_check.py", "foo"])
    assert cascade.main() == expected_code


def test_main_output_file(tmp_path, monkeypatch, capsys):
    canned = {"pkgname": "foo", "level": 4, "decision": "introduce_new",
              "match": None, "reference": None}
    monkeypatch.setattr(cascade, "check_package_existence", lambda *a, **kw: canned)
    out = tmp_path / "sub" / "check_result.json"
    monkeypatch.setattr(sys, "argv", [
        "cascade_package_check.py", "Foo", "--lang", "PYTHON", "--target", "t",
        "--version", " 1.2 ", "-o", str(out),
    ])
    code = cascade.main()
    assert code == 4
    assert out.exists()
    assert json.loads(out.read_text()) == canned
    assert capsys.readouterr().out == ""      # 有 -o 且无 --json → 不打印


def test_main_json_stdout(monkeypatch, capsys):
    canned = {"pkgname": "foo", "level": 2, "decision": "reuse_official",
              "match": None, "reference": None}
    captured = {}
    def fake_check(pkgname, **kw):
        captured.update(kw)
        return canned
    monkeypatch.setattr(cascade, "check_package_existence", fake_check)
    monkeypatch.setattr(sys, "argv", [
        "cascade_package_check.py", "Foo", "--lang", "PYTHON", "--version", " 1.2 ", "--json",
    ])
    assert cascade.main() == 0
    assert "reuse_official" in capsys.readouterr().out
    assert captured["lang"] == "python"       # strip + lower
    assert captured["version"] == "1.2"
