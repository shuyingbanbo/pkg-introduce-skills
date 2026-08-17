"""download_source.py — upstream URL 解析、版本 ref 候选构造、版本选择与下载流程。

纯逻辑重点:
- normalize_version / parse_numeric_version_parts
- build_version_ref_candidates(版本 → 候选 tag/branch refs)
- detect_url_type(URL 分类)
- UNSTABLE_SUFFIXES / _parse_version_tuple / _meets_constraint
- select_best_version(远端 tags 选择,含 pre-release/精确版本/区间约束)
- resolve_git_ref / list_remote_tags(monkeypatch run_git_command)
- download_git_repo / download_tarball / main(fake_subprocess + 模块属性 monkeypatch)
"""

from __future__ import annotations

import json
import subprocess

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

ds = load_module("download_source", SCRIPT_DIRS["pkg_introduce"] / "download_source.py")


def _git_result(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(["git"], returncode, stdout, stderr)


# ─────────────────────────────────────────────
# normalize_version
# ─────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("v1.2.3", "1.2.3"),
    ("V2.0.0", "2.0.0"),
    ("1.2.3", "1.2.3"),
    ("  v1.0.0  ", "1.0.0"),
    ("", ""),
    (None, ""),
    ("v", ""),               # 单个 v 剥完为空
    ("version", "ersion"),   # BUG 注:startswith("v") 无词边界判断,任何 v 开头的词都被剥首字符
])
def test_normalize_version(value, expected):
    assert ds.normalize_version(value) == expected


# ─────────────────────────────────────────────
# parse_numeric_version_parts
# ─────────────────────────────────────────────

@pytest.mark.parametrize("version,expected", [
    ("1.2.3", ["1", "2", "3"]),
    ("v1.2.3", ["1", "2", "3"]),
    ("1.2", ["1", "2"]),
    ("1", ["1"]),
    ("1.2.3.4", ["1", "2", "3", "4"]),
    ("01.02", ["01", "02"]),          # 纯数字即通过,不校验前导零
    ("1.2.3rc1", []),
    ("1.2.3-alpha", []),
    ("", []),
    (None, []),
    ("abc", []),
])
def test_parse_numeric_version_parts(version, expected):
    assert ds.parse_numeric_version_parts(version) == expected


# ─────────────────────────────────────────────
# build_version_ref_candidates
# ─────────────────────────────────────────────

@pytest.mark.parametrize("version,expected", [
    ("1.2", ["refs/tags/v1.2", "refs/tags/1.2", "refs/tags/v1.2.0", "refs/tags/1.2.0",
             "refs/heads/release/1.2", "refs/heads/release-1.2",
             "refs/heads/1.2", "refs/heads/v1.2"]),
    ("1.2.3", ["refs/tags/v1.2.3", "refs/tags/1.2.3",
               "refs/heads/release/1.2", "refs/heads/release-1.2",
               "refs/heads/1.2", "refs/heads/v1.2",
               "refs/heads/1.2.3", "refs/heads/v1.2.3"]),
    ("1.2.3rc1", ["refs/tags/v1.2.3rc1", "refs/tags/1.2.3rc1",
                  "refs/heads/release/1.2.3rc1", "refs/heads/release-1.2.3rc1",
                  "refs/heads/1.2.3rc1", "refs/heads/v1.2.3rc1"]),
    ("1", ["refs/tags/v1", "refs/tags/1",
           "refs/heads/release/1", "refs/heads/release-1",
           "refs/heads/1", "refs/heads/v1"]),
    ("1.2.3.4", ["refs/tags/v1.2.3.4", "refs/tags/1.2.3.4",
                 "refs/heads/release/1.2", "refs/heads/release-1.2",
                 "refs/heads/1.2", "refs/heads/v1.2",
                 "refs/heads/1.2.3.4", "refs/heads/v1.2.3.4"]),
])
def test_build_version_ref_candidates(version, expected):
    assert ds.build_version_ref_candidates(version) == expected


def test_build_version_ref_candidates_v_prefix_equivalent():
    assert ds.build_version_ref_candidates("v1.2.3") == ds.build_version_ref_candidates("1.2.3")


def test_build_version_ref_candidates_empty():
    assert ds.build_version_ref_candidates("") == []
    assert ds.build_version_ref_candidates(None) == []


def test_build_version_ref_candidates_repo_name():
    # Maven Release Plugin 默认 <name>-<version> tag 格式
    got = ds.build_version_ref_candidates("1.2.3", repo_name="jfiglet")
    assert got[:2] == ["refs/tags/jfiglet-1.2.3", "refs/tags/jfiglet-v1.2.3"]
    assert got[2:] == ds.build_version_ref_candidates("1.2.3")


def test_build_version_ref_candidates_no_duplicates():
    for v in ("1.2", "1.2.3", "1", "1.2.3.4"):
        got = ds.build_version_ref_candidates(v)
        assert len(got) == len(set(got))
    got = ds.build_version_ref_candidates("1.2.3", "jfiglet")
    assert len(got) == len(set(got))


# ─────────────────────────────────────────────
# detect_url_type
# ─────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://github.com/foo/bar", "git_repo"),
    ("https://gitlab.com/foo/bar.git", "git_repo"),
    ("https://gitee.com/foo/bar", "git_repo"),
    ("https://atomgit.com/foo/bar", "git_repo"),
    ("https://gitcode.com/foo/bar", "git_repo"),
    ("git@github.com:foo/bar.git", "git_repo"),
    ("https://example.com/foo", "git_repo"),          # 其他 http 地址按 git 仓库处理
    ("HTTPS://GITHUB.COM/FOO/BAR", "git_repo"),       # 大小写不敏感
    ("www.github.com/foo/bar", "git_repo"),           # 无 scheme 但含 git host
    ("https://example.com/foo.tar.gz", "tarball"),
    ("https://example.com/foo.tar.xz", "tarball"),
    ("https://example.com/foo.tar.bz2", "tarball"),
    ("https://example.com/foo.tgz", "tarball"),
    ("https://example.com/foo.zip", "tarball"),
    ("https://github.com/a/b/releases/download/1.0/a.tar.gz", "tarball"),  # 压缩包优先于 git host
    ("ftp://example.com/foo", "unknown"),
    ("", "unknown"),
    ("just-some-text", "unknown"),
])
def test_detect_url_type(url, expected):
    assert ds.detect_url_type(url) == expected


# ─────────────────────────────────────────────
# UNSTABLE_SUFFIXES
# ─────────────────────────────────────────────

@pytest.mark.parametrize("version,is_unstable", [
    ("1.0.0-rc1", True),
    ("1.0.0-rc.1", True),
    ("1.0.0RC1", True),            # IGNORECASE
    ("3.0a6", True),               # 无分隔符后缀
    ("1.0.0rc1", True),
    ("2.0.0-alpha.1", True),
    ("1.0.0-beta", True),
    ("1.0.0.dev1", True),
    ("1.0.0post1", True),
    ("1.0.0.post1", False),        # 注:带分隔符的 post 不在第一组后缀词里,lookbehind 又要求紧邻数字 → 判定稳定
    ("1.0.0snapshot1", False),     # 注:snapshot 不在无分隔符后缀组 → 判定稳定
    ("1.4.0", False),
    ("1.10.2", False),
    ("2024.01", False),
])
def test_unstable_suffixes(version, is_unstable):
    assert bool(ds.UNSTABLE_SUFFIXES.search(version)) is is_unstable


# ─────────────────────────────────────────────
# _parse_version_tuple
# ─────────────────────────────────────────────

@pytest.mark.parametrize("version,expected", [
    ("v1.4.0", (1, 4, 0)),
    ("V2.1", (2, 1)),
    ("1.10.0", (1, 10, 0)),          # 数字按数值比较
    ("3.0a6", (3, 0)),               # a6 的 6 不计入
    ("1.0.0-rc.1", (1, 0, 0)),
    ("1.0.0rc1", (1, 0, 0)),
    ("2.0.0-alpha.1", (2, 0, 0)),
    ("release-1.2", (1, 2)),
    ("2024.01", (2024, 1)),
    ("foo", (0,)),
    ("", (0,)),
])
def test_parse_version_tuple(version, expected):
    assert ds._parse_version_tuple(version) == expected


# ─────────────────────────────────────────────
# _meets_constraint
# ─────────────────────────────────────────────

@pytest.mark.parametrize("tag,constraint,expected", [
    ("1.4.0", "", True),
    ("1.4.0", ">= 1.4.0", True),
    ("1.3.9", ">= 1.4.0", False),
    ("1.4.0", "== 1.4.0", True),
    ("1.4.1", "== 1.4.0", False),
    ("1.4.1", "!= 1.4.0", True),
    ("1.4.0", "!= 1.4.0", False),
    ("1.4.0", "<= 1.4.0", True),
    ("1.4.1", "<= 1.4.0", False),
    ("1.5.0", "> 1.4.0", True),
    ("1.4.0", "> 1.4.0", False),
    ("1.4.0", "< 1.5.0", True),
    ("1.5.0", "< 1.5.0", False),
    ("2.0.0", ">= 1.4.0, < 3.0.0", True),
    ("3.0.0", ">= 1.4.0, < 3.0.0", False),
    ("v1.4.0", ">= 1.4.0", True),       # v 前缀 tag 正常比较
    ("1.5.0rc1", ">= 1.4.0", True),     # pre-release 后缀剥除后比较
    ("1.4.0", "^1.2.3", True),          # 解析不出的约束保守放过
    ("1.4.0", ">= 1.4.0, junk-here", True),  # 无法解析的部分跳过
    ("1.4.0", "no-op-here", True),
])
def test_meets_constraint(tag, constraint, expected):
    assert ds._meets_constraint(tag, constraint) is expected


# ─────────────────────────────────────────────
# list_remote_tags
# ─────────────────────────────────────────────

def test_list_remote_tags_sorted_numeric_desc(monkeypatch):
    stdout = "\n".join([
        "h1\trefs/tags/v1.2.0",
        "h2\trefs/tags/v1.10.0",
        "h3\trefs/tags/v1.2.1",
    ])
    monkeypatch.setattr(ds, "run_git_command", lambda *a, **kw: _git_result(stdout=stdout))
    assert ds.list_remote_tags("https://github.com/foo/bar") == [
        "v1.10.0", "v1.2.1", "v1.2.0",
    ]


def test_list_remote_tags_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(ds, "run_git_command", lambda *a, **kw: _git_result(returncode=1, stderr="boom"))
    assert ds.list_remote_tags("https://github.com/foo/bar") == []


def test_list_remote_tags_empty_and_malformed_lines(monkeypatch):
    monkeypatch.setattr(ds, "run_git_command", lambda *a, **kw: _git_result(stdout="just-one-token\n"))
    assert ds.list_remote_tags("u") == []
    monkeypatch.setattr(ds, "run_git_command", lambda *a, **kw: _git_result(stdout=""))
    assert ds.list_remote_tags("u") == []


# ─────────────────────────────────────────────
# select_best_version
# ─────────────────────────────────────────────

def test_select_exact_version_fast_path(monkeypatch):
    def fail(url):
        raise AssertionError("精确版本快路径不应查询远端 tags")
    monkeypatch.setattr(ds, "list_remote_tags", fail)
    assert ds.select_best_version("https://github.com/foo/bar", "1.4.0") == {
        "version": "1.4.0", "is_stable": True, "reason": "精确版本约束，直接采用",
    }


def test_select_exact_version_with_eq_prefix(monkeypatch):
    monkeypatch.setattr(ds, "list_remote_tags", lambda url: None)  # 不被调用
    got = ds.select_best_version("u", "== 1.4.0")
    assert got["version"] == "1.4.0" and got["is_stable"] is True


def test_select_exact_version_unstable(monkeypatch):
    monkeypatch.setattr(ds, "list_remote_tags", lambda url: None)
    got = ds.select_best_version("u", "3.0a6")
    assert got["version"] == "3.0a6" and got["is_stable"] is False


def test_select_range_picks_smallest_stable(monkeypatch):
    monkeypatch.setattr(ds, "list_remote_tags",
                        lambda url: ["v1.5.0", "v1.4.0", "v1.3.9"])
    got = ds.select_best_version("u", ">= 1.4.0")
    assert got["version"] == "v1.4.0"
    assert got["is_stable"] is True


def test_select_no_constraint_picks_oldest_stable(monkeypatch):
    # 注:docstring 声称"选最新稳定版",实际代码取降序列表最后一个(最旧稳定版)
    monkeypatch.setattr(ds, "list_remote_tags",
                        lambda url: ["v1.5.0", "v1.4.0", "v1.3.9"])
    got = ds.select_best_version("u", "")
    assert got["version"] == "v1.3.9"
    assert got["is_stable"] is True


def test_select_falls_back_to_newest_prerelease(monkeypatch):
    monkeypatch.setattr(ds, "list_remote_tags", lambda url: ["2.1.0rc1", "1.0.0"])
    got = ds.select_best_version("u", ">= 2.0.0")
    assert got["version"] == "2.1.0rc1"
    assert got["is_stable"] is False
    assert "回退" in got["reason"]


def test_select_none_when_no_meeting_version(monkeypatch):
    monkeypatch.setattr(ds, "list_remote_tags", lambda url: ["1.0.0"])
    assert ds.select_best_version("u", ">= 9.0.0") is None


def test_select_none_when_no_tags(monkeypatch):
    monkeypatch.setattr(ds, "list_remote_tags", lambda url: [])
    assert ds.select_best_version("u", ">= 1.0.0") is None
    assert ds.select_best_version("u", "") is None


def test_select_stable_preferred_even_without_constraint(monkeypatch):
    monkeypatch.setattr(ds, "list_remote_tags", lambda url: ["2.1.0rc1", "2.0.0"])
    got = ds.select_best_version("u", "")
    assert got["version"] == "2.0.0" and got["is_stable"] is True


# ─────────────────────────────────────────────
# _parse_upstream_from_diff / extract_upstream_url
# ─────────────────────────────────────────────

@pytest.mark.parametrize("diff_text,expected", [
    ("+upstream: https://github.com/foo/bar", "https://github.com/foo/bar"),
    ("+upstream:  https://github.com/foo/bar/", "https://github.com/foo/bar"),  # 尾斜杠剥除
    ("+UPSTREAM: https://x/y", "https://x/y"),               # IGNORECASE
    (" upstream: https://x/y", "https://x/y"),               # 上下文行也会被匹配(实际行为)
    ("-upstream: https://github.com/foo/bar", None),         # 删除行不匹配
    ("+upstreams: https://x/y", None),                       # upstream 后必须紧跟冒号
    ("+upstream:", None),                                    # 无值不匹配
    ("", None),
])
def test_parse_upstream_from_diff(diff_text, expected):
    assert ds._parse_upstream_from_diff(diff_text) == expected


def test_extract_upstream_url_from_yaml_dict_patch(tmp_path):
    pr = tmp_path / "pr_1_info.json"
    pr.write_text(json.dumps({"files": [
        {"filename": "openeuler.yaml",
         "patch": {"diff": "+upstream: https://github.com/foo/bar"}},
    ]}))
    assert ds.extract_upstream_url(str(pr)) == "https://github.com/foo/bar"


def test_extract_upstream_url_from_yml_str_patch(tmp_path):
    pr = tmp_path / "pr_2_info.json"
    pr.write_text(json.dumps({"files": [
        {"filename": "other.md", "patch": "+upstream: https://example.com/a.md"},
        {"filename": "pkg.yml", "patch": "+upstream: https://gitlab.com/a/b"},
    ]}))
    assert ds.extract_upstream_url(str(pr)) == "https://gitlab.com/a/b"


def test_extract_upstream_url_first_yaml_wins(tmp_path):
    pr = tmp_path / "pr_3_info.json"
    pr.write_text(json.dumps({"files": [
        {"filename": "a.yaml", "patch": "+upstream: https://github.com/first"},
        {"filename": "b.yaml", "patch": "+upstream: https://github.com/second"},
    ]}))
    assert ds.extract_upstream_url(str(pr)) == "https://github.com/first"


def test_extract_upstream_url_not_found(tmp_path):
    pr = tmp_path / "pr_4_info.json"
    pr.write_text(json.dumps({"files": [
        {"filename": "readme.md", "patch": "+upstream: https://github.com/foo/bar"},
    ]}))
    assert ds.extract_upstream_url(str(pr)) is None
    pr.write_text(json.dumps({}))       # 无 files 字段
    assert ds.extract_upstream_url(str(pr)) is None


# ─────────────────────────────────────────────
# detect_project_version
# ─────────────────────────────────────────────

def _write(tmp_path, name, content):
    (tmp_path / name).write_text(content)


def test_detect_version_cargo_toml(tmp_path):
    _write(tmp_path, "Cargo.toml", '[package]\nname = "x"\nversion = "0.8.2"\n')
    assert ds.detect_project_version(tmp_path) == "0.8.2"


def test_detect_version_pyproject_toml(tmp_path):
    _write(tmp_path, "pyproject.toml", '[project]\nname = "x"\nversion = "1.2.3"\n')
    assert ds.detect_project_version(tmp_path) == "1.2.3"


def test_detect_version_pom_xml(tmp_path):
    _write(tmp_path, "pom.xml", "<project><version>2.0</version></project>")
    assert ds.detect_project_version(tmp_path) == "2.0"


def test_detect_version_setup_py(tmp_path):
    _write(tmp_path, "setup.py", 'setup(\n    version = "3.1",\n)\n')
    assert ds.detect_project_version(tmp_path) == "3.1"
    # 注:download_source 的 setup.py 正则只匹配 version = "...",
    # __version__ 写法不识别(与 python_metadata 不同)
    _write(tmp_path, "setup.py", '__version__ = "3.2"\n')
    assert ds.detect_project_version(tmp_path) is None


def test_detect_version_package_json(tmp_path):
    _write(tmp_path, "package.json", json.dumps({"name": "x", "version": "4.0.0"}))
    assert ds.detect_project_version(tmp_path) == "4.0.0"


def test_detect_version_priority_and_empty(tmp_path):
    _write(tmp_path, "Cargo.toml", '[package]\nversion = "0.1.0"\n')
    _write(tmp_path, "pyproject.toml", '[project]\nversion = "9.9.9"\n')
    assert ds.detect_project_version(tmp_path) == "0.1.0"   # Cargo.toml 优先
    assert ds.detect_project_version(tmp_path / "nope") is None


def test_detect_version_invalid_package_json(tmp_path):
    _write(tmp_path, "package.json", "{not json")
    assert ds.detect_project_version(tmp_path) is None


# ─────────────────────────────────────────────
# resolve_git_ref
# ─────────────────────────────────────────────

def test_resolve_git_ref_matches_candidate(monkeypatch):
    calls = []

    def fake_run(args, *, timeout=300, cwd=None):
        calls.append(args)
        return _git_result(stdout="abc\trefs/tags/v1.2.3\n")

    monkeypatch.setattr(ds, "run_git_command", fake_run)
    assert ds.resolve_git_ref("https://github.com/foo/bar.git", "1.2.3") == "refs/tags/v1.2.3"
    # args = ["git", "ls-remote", "--refs", url, *candidates]
    # repo_name 由 URL 提取,候选列表含 <repo>-<version> Maven 格式
    candidate_list = calls[0][4:]
    assert "refs/tags/bar-1.2.3" in candidate_list
    assert "refs/tags/v1.2.3" in candidate_list


def test_resolve_git_ref_repo_named_tag(monkeypatch):
    monkeypatch.setattr(ds, "run_git_command",
                        lambda *a, **kw: _git_result(stdout="abc\trefs/tags/jfiglet-1.2.3\n"))
    assert ds.resolve_git_ref("https://github.com/foo/jfiglet", "1.2.3") == "refs/tags/jfiglet-1.2.3"


def test_resolve_git_ref_empty_version_silent(monkeypatch):
    def fail(*a, **kw):
        raise AssertionError("空版本不应发起 git 查询")
    monkeypatch.setattr(ds, "run_git_command", fail)
    assert ds.resolve_git_ref("https://github.com/foo/bar", "", silent=True) == ""


def test_resolve_git_ref_empty_version_exits(monkeypatch):
    monkeypatch.setattr(ds, "run_git_command", lambda *a, **kw: None)
    with pytest.raises(SystemExit) as ei:
        ds.resolve_git_ref("https://github.com/foo/bar", "", silent=False)
    assert ei.value.code == 1


def test_resolve_git_ref_remote_failure(monkeypatch):
    monkeypatch.setattr(ds, "run_git_command",
                        lambda *a, **kw: _git_result(returncode=1, stderr="denied"))
    assert ds.resolve_git_ref("https://github.com/foo/bar", "1.2.3", silent=True) == ""
    with pytest.raises(SystemExit) as ei:
        ds.resolve_git_ref("https://github.com/foo/bar", "1.2.3", silent=False)
    assert ei.value.code == 1


def test_resolve_git_ref_no_match(monkeypatch):
    monkeypatch.setattr(ds, "run_git_command",
                        lambda *a, **kw: _git_result(stdout="abc\trefs/tags/v9.9.9\n"))
    assert ds.resolve_git_ref("https://github.com/foo/bar", "1.2.3", silent=True) == ""
    with pytest.raises(SystemExit) as ei:
        ds.resolve_git_ref("https://github.com/foo/bar", "1.2.3", silent=False)
    assert ei.value.code == 1


# ─────────────────────────────────────────────
# clone_repo_at_ref
# ─────────────────────────────────────────────

def test_clone_commit_hash_full_clone_then_checkout(fake_subprocess, tmp_path):
    ds.clone_repo_at_ref("https://github.com/foo/bar", tmp_path / "bar", "a1b2c3d4e5f6789")
    assert fake_subprocess.called_with("git clone")
    assert fake_subprocess.called_with("checkout a1b2c3d4e5f6789")
    assert not fake_subprocess.called_with("--depth=1")


def test_clone_tag_ref(fake_subprocess, tmp_path):
    ds.clone_repo_at_ref("https://github.com/foo/bar", tmp_path / "bar", "refs/tags/v1.0.0")
    assert fake_subprocess.called_with("--branch v1.0.0")
    assert fake_subprocess.called_with("--depth=1")
    assert not fake_subprocess.called_with("--single-branch")


def test_clone_branch_ref(fake_subprocess, tmp_path):
    ds.clone_repo_at_ref("https://github.com/foo/bar", tmp_path / "bar", "refs/heads/main")
    assert fake_subprocess.called_with("--branch main")
    assert fake_subprocess.called_with("--single-branch")


def test_clone_failure_exits(fake_subprocess, tmp_path):
    fake_subprocess.when("git clone", returncode=1, stderr="boom")
    with pytest.raises(SystemExit) as ei:
        ds.clone_repo_at_ref("https://github.com/foo/bar", tmp_path / "bar", "refs/tags/v1.0.0")
    assert ei.value.code == 1


def test_clone_commit_hash_clone_failure_exits(fake_subprocess, tmp_path):
    # commit hash 路径的 full clone 失败分支
    fake_subprocess.when("git clone", returncode=1, stderr="boom")
    with pytest.raises(SystemExit) as ei:
        ds.clone_repo_at_ref("https://github.com/foo/bar", tmp_path / "bar", "a1b2c3d4e5f6789")
    assert ei.value.code == 1


def test_clone_checkout_failure_exits(fake_subprocess, tmp_path):
    # 注意:不能用 lambda "checkout" in s 做谓词,测试目录名本身含 "checkout" 会误匹配 clone 命令
    fake_subprocess.when("git -C", returncode=1, stderr="boom")
    with pytest.raises(SystemExit) as ei:
        ds.clone_repo_at_ref("https://github.com/foo/bar", tmp_path / "bar", "a1b2c3d4e5f6789")
    assert ei.value.code == 1


def test_run_git_command(fake_subprocess):
    fake_subprocess.when("git --version", stdout="git version 2.39\n")
    result = ds.run_git_command(["git", "--version"])
    assert result.returncode == 0
    assert result.stdout == "git version 2.39\n"


# ─────────────────────────────────────────────
# download_git_repo
# ─────────────────────────────────────────────

URL = "https://github.com/foo/bar.git"


def test_download_git_repo_dir_exists_skips(fake_subprocess, tmp_path):
    (tmp_path / "bar").mkdir()
    dest = ds.download_git_repo(URL, tmp_path)
    assert dest == tmp_path / "bar"
    assert fake_subprocess.calls == []


def test_download_git_repo_clone_failure_exits(fake_subprocess, tmp_path):
    fake_subprocess.when("git clone", returncode=1, stderr="boom")
    with pytest.raises(SystemExit) as ei:
        ds.download_git_repo(URL, tmp_path)
    assert ei.value.code == 1


def test_download_git_repo_resolve_systemexit_is_guarded(fake_subprocess, tmp_path, monkeypatch):
    # 稳定版自动切换 tag 时 resolve_git_ref 若意外抛 SystemExit 被守卫捕获,保留默认分支
    monkeypatch.setattr(ds, "detect_project_version", lambda dest: "1.4.0")

    def boom(*a, **kw):
        raise SystemExit(1)

    monkeypatch.setattr(ds, "resolve_git_ref", boom)
    dest = ds.download_git_repo(URL, tmp_path)
    assert dest == tmp_path / "bar"
    assert len([c for c, _ in fake_subprocess.calls if "git clone" in " ".join(c)]) == 1


def test_download_git_repo_default_clone(fake_subprocess, tmp_path):
    dest = ds.download_git_repo(URL, tmp_path)
    assert dest == tmp_path / "bar"
    assert fake_subprocess.called_with("git clone --depth=1")


def test_download_git_repo_pkgname(fake_subprocess, tmp_path):
    dest = ds.download_git_repo(URL, tmp_path, pkgname="mypkg")
    assert dest == tmp_path / "mypkg"


def test_download_git_repo_unstable_without_stable_tag(fake_subprocess, tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "detect_project_version", lambda dest: "3.0a6")
    monkeypatch.setattr(ds, "list_remote_tags", lambda url: [])
    with pytest.raises(SystemExit) as ei:
        ds.download_git_repo(URL, tmp_path)
    assert ei.value.code == 1


def test_download_git_repo_unstable_switches_to_stable_tag(fake_subprocess, tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "detect_project_version", lambda dest: "3.0a6")
    monkeypatch.setattr(ds, "list_remote_tags", lambda url: ["1.4.0", "1.3.9"])
    dest = ds.download_git_repo(URL, tmp_path)
    assert dest == tmp_path / "bar"
    assert fake_subprocess.called_with("--branch 1.4.0")


def test_download_git_repo_unstable_allowed_by_config(fake_subprocess, tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "detect_project_version", lambda dest: "3.0a6")
    monkeypatch.setattr(ds, "_load_config",
                        lambda: {"version_check": {"allow_unstable": True}})
    dest = ds.download_git_repo(URL, tmp_path)
    assert dest == tmp_path / "bar"
    assert len([c for c, _ in fake_subprocess.calls if "git clone" in " ".join(c)]) == 1


def test_download_git_repo_stable_switch_to_matching_tag(fake_subprocess, tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "detect_project_version", lambda dest: "1.4.0")
    monkeypatch.setattr(ds, "resolve_git_ref",
                        lambda url, version, *, silent=False: "refs/tags/1.4.0")
    dest = ds.download_git_repo(URL, tmp_path)
    assert dest == tmp_path / "bar"
    assert fake_subprocess.called_with("--branch 1.4.0")


def test_download_git_repo_stable_no_matching_tag_keeps_default(fake_subprocess, tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "detect_project_version", lambda dest: "1.4.0")
    monkeypatch.setattr(ds, "resolve_git_ref", lambda *a, **kw: "")
    dest = ds.download_git_repo(URL, tmp_path)
    assert dest == tmp_path / "bar"
    assert len([c for c, _ in fake_subprocess.calls if "git clone" in " ".join(c)]) == 1


def test_download_git_repo_constraint_selects_version(fake_subprocess, tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "select_best_version",
                        lambda url, constraint: {"version": "1.4.0", "is_stable": True})
    seen = {}

    def fake_resolve(url, version, *, silent=False):
        seen["version"] = version
        return "refs/tags/1.4.0"

    monkeypatch.setattr(ds, "resolve_git_ref", fake_resolve)
    dest = ds.download_git_repo(URL, tmp_path, constraint=">= 1.4.0")
    assert dest == tmp_path / "bar"
    assert seen["version"] == "1.4.0"
    assert fake_subprocess.called_with("--branch 1.4.0")


def test_download_git_repo_constraint_no_match_falls_back(fake_subprocess, tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "select_best_version", lambda url, constraint: None)
    monkeypatch.setattr(ds, "resolve_git_ref",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("不应被调用")))
    dest = ds.download_git_repo(URL, tmp_path, constraint=">= 9.0.0")
    assert dest == tmp_path / "bar"
    assert fake_subprocess.called_with("git clone --depth=1")


def test_download_git_repo_explicit_ref(fake_subprocess, tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "resolve_git_ref",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("ref 已给不应解析")))
    dest = ds.download_git_repo(URL, tmp_path, ref="refs/heads/feature")
    assert dest == tmp_path / "bar"
    assert fake_subprocess.called_with("--branch feature")


def test_download_git_repo_version_resolved(fake_subprocess, tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "resolve_git_ref",
                        lambda url, version, *, silent=False: "refs/tags/v1.2.3")
    dest = ds.download_git_repo(URL, tmp_path, version="1.2.3")
    assert dest == tmp_path / "bar"
    assert fake_subprocess.called_with("--branch v1.2.3")


# ─────────────────────────────────────────────
# download_tarball
# ─────────────────────────────────────────────

def test_download_tarball_single_entry_rename(fake_subprocess, tmp_path):
    (tmp_path / "foo-1.2.3").mkdir()   # 模拟解压产物
    dest = ds.download_tarball("https://x.com/foo-1.2.3.tar.gz", tmp_path, pkgname="bar")
    assert dest == tmp_path / "bar"
    assert (tmp_path / "bar").is_dir()
    assert fake_subprocess.called_with("wget")
    assert fake_subprocess.called_with("tar -xf")


def test_download_tarball_multi_entry_stem_match(fake_subprocess, tmp_path):
    (tmp_path / "foo-1.2.3").mkdir()
    (tmp_path / "other").mkdir()
    dest = ds.download_tarball("https://x.com/foo-1.2.3.tar.gz", tmp_path)
    assert dest == tmp_path / "foo-1.2.3"


def test_download_tarball_multi_entry_no_stem_match_takes_first(fake_subprocess, tmp_path):
    (tmp_path / "aaa").mkdir()
    (tmp_path / "bbb").mkdir()
    dest = ds.download_tarball("https://x.com/pkg.tar.gz", tmp_path)
    assert dest == tmp_path / "aaa"


def test_download_tarball_zip_uses_unzip(fake_subprocess, tmp_path):
    dest = ds.download_tarball("https://x.com/pkg.zip", tmp_path)
    assert dest == tmp_path
    assert fake_subprocess.called_with("unzip -q")


def test_download_tarball_wget_failure_exits(fake_subprocess, tmp_path):
    fake_subprocess.when("wget", returncode=1, stderr="boom")
    with pytest.raises(SystemExit) as ei:
        ds.download_tarball("https://x.com/pkg.tar.gz", tmp_path)
    assert ei.value.code == 1


def test_download_tarball_existing_file_skips_wget(fake_subprocess, tmp_path):
    (tmp_path / "pkg.tar.gz").write_text("")   # 已存在,跳过下载
    (tmp_path / "pkg").mkdir()
    dest = ds.download_tarball("https://x.com/pkg.tar.gz", tmp_path)
    assert dest == tmp_path / "pkg"
    assert not fake_subprocess.called_with("wget")
    assert fake_subprocess.called_with("tar -xf")


def test_download_tarball_rename_overwrites_existing_target(fake_subprocess, tmp_path):
    (tmp_path / "foo-1.2.3").mkdir()
    (tmp_path / "bar").mkdir()     # 目标名已存在 → 先删再移
    dest = ds.download_tarball("https://x.com/foo-1.2.3.tar.gz", tmp_path, pkgname="bar")
    assert dest == tmp_path / "bar"
    assert (tmp_path / "bar").is_dir()


# ─────────────────────────────────────────────
# download_source 分派
# ─────────────────────────────────────────────

def test_download_source_dispatches_git(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(ds, "download_git_repo",
                        lambda url, out, **kw: calls.append((url, kw)) or (out / "git"))
    dest = ds.download_source("https://github.com/foo/bar", tmp_path, version="1.0.0")
    assert dest == tmp_path / "git"
    assert calls[0][0] == "https://github.com/foo/bar"
    assert calls[0][1]["version"] == "1.0.0"


def test_download_source_dispatches_tarball(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "download_tarball",
                        lambda url, out, **kw: out / "tarball")
    assert ds.download_source("https://x.com/pkg.tar.gz", tmp_path) == tmp_path / "tarball"


def test_download_source_unknown_falls_back_to_git(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "download_git_repo",
                        lambda url, out, **kw: out / "git")
    assert ds.download_source("ftp://weird/url", tmp_path) == tmp_path / "git"


# ─────────────────────────────────────────────
# _load_config
# ─────────────────────────────────────────────

def test_load_config_missing_returns_empty():
    # 仓库内只有 config.json.example,真实 config.json 不存在 → {}
    assert ds._load_config() == {}


# ─────────────────────────────────────────────
# main
# ─────────────────────────────────────────────

def test_main_upstream_url_writes_result_json(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "download_source",
                        lambda url, output_dir, **kw: output_dir / "src")
    monkeypatch.setattr("sys.argv", [
        "download_source.py", "--upstream-url", "https://github.com/foo/bar",
        "--output-dir", str(tmp_path), "-o", str(tmp_path / "result.json"),
    ])
    ds.main()
    data = json.loads((tmp_path / "result.json").read_text())
    assert data["upstream_url"] == "https://github.com/foo/bar"
    assert data["source_dir"] == str(tmp_path / "src")
    assert data["requested_version"] == ""


def test_main_pr_json_missing_exits(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.argv", [
        "download_source.py", "--pr-json", str(tmp_path / "nope.json"),
        "--output-dir", str(tmp_path),
    ])
    with pytest.raises(SystemExit) as ei:
        ds.main()
    assert ei.value.code == 1


def test_main_pr_json_no_upstream_exits(monkeypatch, tmp_path):
    pr = tmp_path / "pr.json"
    pr.write_text(json.dumps({"files": []}))
    monkeypatch.setattr("sys.argv", [
        "download_source.py", "--pr-json", str(pr), "--output-dir", str(tmp_path),
    ])
    with pytest.raises(SystemExit) as ei:
        ds.main()
    assert ei.value.code == 1


def test_main_pr_json_extracts_and_downloads(monkeypatch, tmp_path):
    pr = tmp_path / "pr.json"
    pr.write_text(json.dumps({"files": []}))
    seen = {}
    monkeypatch.setattr(ds, "extract_upstream_url",
                        lambda path: "https://gitlab.com/a/b")
    monkeypatch.setattr(ds, "download_source",
                        lambda url, output_dir, **kw: seen.update(url=url) or (output_dir / "src"))
    monkeypatch.setattr("sys.argv", [
        "download_source.py", "--pr-json", str(pr), "--output-dir", str(tmp_path),
    ])
    ds.main()
    assert seen["url"] == "https://gitlab.com/a/b"


def test_main_entry_point(fake_subprocess, monkeypatch, tmp_path):
    # 以 __name__="__main__" 重新执行模块源码,覆盖 `if __name__ == "__main__": main()` 分支
    (tmp_path / "pkg-1.0").mkdir()   # 模拟解压产物
    monkeypatch.setattr("sys.argv", [
        "download_source.py", "--upstream-url", "https://x.com/pkg-1.0.tar.gz",
        "--output-dir", str(tmp_path),
    ])
    path = SCRIPT_DIRS["pkg_introduce"] / "download_source.py"
    src = path.read_text(encoding="utf-8")
    ns = {"__name__": "__main__", "__file__": str(path)}
    exec(compile(src, str(path), "exec"), ns)
    assert (tmp_path / "pkg-1.0").is_dir()
