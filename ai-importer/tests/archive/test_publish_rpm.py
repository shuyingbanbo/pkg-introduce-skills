"""publish_rpm.py 单元测试 — 配置/辅助函数/报告归档部分。

docker 编排部分(copy_pkg_files / git 重试 / CI 门禁 / init_or_update_repo /
main 主流程)见 test_publish_rpm_docker.py;compat 四函数见
test_publish_rpm_compat.py。

已知生产代码问题(按实际行为断言,不修):
1. archive_introduction_reports 的 success 分支在 report_src 赋值前就引用它
   (约第 1034 行,report_src 定义在约第 1048 行),任何 action 非
   blocked/failed 的包必然抛 UnboundLocalError,success 归档路径实际不可达。
2. _is_review_rpm_report 用子串匹配章节标题,"## 1.5" 这类小节标题会命中 "## 1.";
   反向地,"## 10." 因中间有 "0" 不命中("## 1." 是连续子串才算)。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sys.path.insert(0, str(SCRIPT_DIRS["archive"]))
p = load_module("publish_rpm", SCRIPT_DIRS["archive"] / "publish_rpm.py")

CTR = "ctr"
TODAY = datetime.now().strftime("%Y%m%d")


# ─────────────────────────────────────────────
# load_config
# ─────────────────────────────────────────────

def test_load_config_absolute(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"a": 1}))
    assert p.load_config(str(cfg)) == {"a": 1}


def test_load_config_missing(tmp_path, capsys):
    with pytest.raises(SystemExit) as ei:
        p.load_config(str(tmp_path / "nope.json"))
    assert ei.value.code == 1
    assert "配置文件不存在" in capsys.readouterr().err


def test_load_config_invalid_json(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text("{not json")
    with pytest.raises(json.JSONDecodeError):
        p.load_config(str(cfg))


def test_load_config_script_relative(tmp_path, monkeypatch):
    # __file__ 指向临时目录,验证相对路径先按脚本目录(parent.parent)解析
    monkeypatch.setattr(p, "__file__", str(tmp_path / "scripts" / "publish_rpm.py"))
    (tmp_path / "rel.json").write_text(json.dumps({"ok": True}))
    assert p.load_config("rel.json") == {"ok": True}


# ─────────────────────────────────────────────
# run / normalize_name_token / auth_url
# ─────────────────────────────────────────────

@pytest.mark.parametrize("cwd,check,expected_kw", [
    (None, True, {"cwd": None, "check": True}),
    ("/tmp", False, {"cwd": "/tmp", "check": False}),
])
def test_run(fake_subprocess, cwd, check, expected_kw):
    r = p.run(["git", "pull"], cwd=cwd, check=check)
    assert r.returncode == 0
    assert fake_subprocess.calls[-1] == (["git", "pull"], expected_kw)


@pytest.mark.parametrize("value,expected", [
    ("Python3-Requests", "python3_requests"),
    ("a..b--c", "a_b_c"),
    ("A_B.c-d", "a_b_c_d"),
    ("UPPER", "upper"),
])
def test_normalize_name_token(value, expected):
    assert p.normalize_name_token(value) == expected


@pytest.mark.parametrize("remote,username,token,expected", [
    ("https://github.com/o/r.git", "u", "", "https://github.com/o/r.git"),
    ("https://gitcode.com/o/r.git", "u", "tok", "https://u:tok@gitcode.com/o/r.git"),
    ("git@github.com:o/r.git", "u", "tok", "git@github.com:o/r.git"),  # 无 :// 不注入
    ("http://x/y", "a", "b", "http://a:b@x/y"),
    ("https://h/p", "oauth2", "t", "https://oauth2:t@h/p"),
])
def test_auth_url(remote, username, token, expected):
    assert p.auth_url(remote, username, token) == expected


# ─────────────────────────────────────────────
# ensure_repo_file / ensure_readme
# ─────────────────────────────────────────────

def test_ensure_repo_file_create(tmp_path, capsys):
    dist = tmp_path / "dist"
    dist.mkdir()
    p.ensure_repo_file(dist, "https://raw.githubusercontent.com/o/r/main")
    assert (dist / "repo-aitest.repo").read_text() == (
        "[repo-aitest]\n"
        "name=openEuler RPM Repository\n"
        "baseurl=https://raw.githubusercontent.com/o/r/main/dist\n"
        "enabled=1\n"
        "gpgcheck=0\n"
    )
    assert "更新 .repo 配置" in capsys.readouterr().out


def test_ensure_repo_file_update_and_idempotent(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    f = dist / "repo-aitest.repo"
    f.write_text("old content")
    p.ensure_repo_file(dist, "https://raw.githubusercontent.com/o/r/main")
    assert "baseurl=https://raw.githubusercontent.com/o/r/main/dist" in f.read_text()
    # 幂等:内容一致时不再写入(mtime 不变)
    mtime = f.stat().st_mtime_ns
    p.ensure_repo_file(dist, "https://raw.githubusercontent.com/o/r/main")
    assert f.stat().st_mtime_ns == mtime


@pytest.mark.parametrize("remote,expected_clean,expected_raw", [
    ("https://github.com/o/r.git",
     "https://github.com/o/r.git",
     "https://raw.githubusercontent.com/o/r/main/dist/repo-aitest.repo"),
    # 带 token 的 URL:split("@")[-1] 会连 scheme 一起剥掉(生产代码实际行为)
    ("https://u:secret@github.com/o/r.git",
     "github.com/o/r.git",
     "github.com/o/r/main/dist/repo-aitest.repo"),
])
def test_ensure_readme_created(tmp_path, remote, expected_clean, expected_raw):
    repo = tmp_path / "repo"
    repo.mkdir()
    p.ensure_readme(str(repo), remote)
    content = (repo / "README.md").read_text()
    assert "# openEuler RPM 仓库" in content
    assert expected_raw in content
    assert expected_clean in content
    assert "secret" not in content


def test_ensure_readme_existing_untouched(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("KEEP")
    p.ensure_readme(str(repo), "https://github.com/o/r.git")
    assert (repo / "README.md").read_text() == "KEEP"


# ─────────────────────────────────────────────
# _is_review_rpm_report
# ─────────────────────────────────────────────

@pytest.mark.parametrize("content,expected", [
    ("## 1. a\n## 2. b\n## 3. c\n", True),
    ("## 1. a\n## 2. b\n", False),
    ("plain text without sections", False),
])
def test_is_review_rpm_report(tmp_path, content, expected):
    f = tmp_path / "r.md"
    f.write_text(content)
    assert p._is_review_rpm_report(f) is expected


def test_is_review_rpm_report_substring_quirk(tmp_path):
    # 注意:"## 10." 不含 "## 1." 子串("0" 在中间)→ 伪章节不命中,判 False
    f = tmp_path / "r.md"
    f.write_text("## 10. a\n## 20. b\n## 30. c\n")
    assert p._is_review_rpm_report(f) is False


def test_is_review_rpm_report_real_report(tmp_path):
    """≥3 个标准章节 → True。"""
    f = tmp_path / "r.md"
    f.write_text("## 1. 基本信息\n## 2. 上游合规\n## 3. License\n## 5. RPM 产物\n")
    assert p._is_review_rpm_report(f) is True


def test_is_review_rpm_report_missing(tmp_path):
    assert p._is_review_rpm_report(tmp_path / "nope.md") is False


def test_is_review_rpm_report_read_error(tmp_path):
    # 目标是目录时 read_text 抛异常 → 被吞掉,返回 False
    d = tmp_path / "adir"
    d.mkdir()
    assert p._is_review_rpm_report(d) is False


# ─────────────────────────────────────────────
# archive_introduction_reports
# ─────────────────────────────────────────────

def test_archive_reports_empty_dir(tmp_path):
    assert p.archive_introduction_reports(["foo"], "", str(tmp_path)) == 0


def test_archive_reports_failed_full(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    pkg_root = tmp_path / "pkgs"
    (pkg_root / "foo").mkdir(parents=True)

    (reports / "build_rpm_result_foo.json").write_text(
        json.dumps({"action": "failed", "version": "1.2.3"}))
    (reports / "check_result_foo.json").write_text("{}")
    (reports / "gate_result_foo.json").write_text("{}")
    (reports / "pre_check_foo.json").write_text('"from_reports"')
    (reports / "pkg_introduce_result_foo.json").write_text("{}")
    (reports / "pkg_introduce_result_dep1.json").write_text("{}")
    (reports / "import_issues.log").write_text("issues")
    (reports / "foo_introduction_report.md").write_text("## 1. a\n## 2. b\n## 3. c\n")
    (pkg_root / "foo" / "build.log").write_text("log")
    (pkg_root / "foo" / "foo.spec").write_text("spec")
    (pkg_root / "foo" / "rpmlint.txt").write_text("rpmlint")
    (pkg_root / "foo" / "build_rpm_result.json").write_text("{}")
    (pkg_root / "foo" / "pre_check_foo.json").write_text('"from_pkg"')

    n = p.archive_introduction_reports(
        ["foo"], str(reports), str(repo), pkg_dir=str(pkg_root))
    assert n == 1
    dest = repo / "reports" / "failed" / f"foo-1.2.3-{TODAY}"
    assert sorted(x.name for x in dest.iterdir()) == sorted([
        "foo_introduction_report.md",
        "pkg_introduce_result_foo.json",
        "check_result_foo.json",
        "gate_result_foo.json",
        "build_rpm_result_foo.json",
        "pre_check_foo.json",
        "build.log",
        "foo.spec",
        "rpmlint.txt",
        "build_rpm_result.json",
        "import_issues.log",
        "pkg_introduce_result_dep1.json",
    ])
    # 固定名称文件优先从 reports_dir 复制,pkg_root 的同名文件不覆盖
    assert (dest / "pre_check_foo.json").read_text() == '"from_reports"'


def test_archive_reports_fallback_introduce_result(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    # build_rpm_result 无 action → 回退读 pkg_introduce_result 的 version+action
    (reports / "build_rpm_result_foo.json").write_text(json.dumps({"version": "9.9"}))
    (reports / "pkg_introduce_result_foo.json").write_text(
        json.dumps({"version": "1.0", "action": "blocked"}))
    n = p.archive_introduction_reports(["foo"], str(reports), str(repo))
    assert n == 1
    assert (repo / "reports" / "failed" / f"foo-1.0-{TODAY}").is_dir()


def test_archive_reports_version_from_pkg_root(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    pkg_root = tmp_path / "pkgs"
    (pkg_root / "foo").mkdir(parents=True)
    (pkg_root / "foo" / "build_rpm_result.json").write_text(
        json.dumps({"version": "2.0", "action": "failed"}))
    n = p.archive_introduction_reports(
        ["foo"], str(reports), str(repo), pkg_dir=str(pkg_root))
    assert n == 1
    assert (repo / "reports" / "failed" / f"foo-2.0-{TODAY}").is_dir()


@pytest.mark.parametrize("build_result", [
    {"action": "introduced", "version": "1.0"},  # action 非 failed/blocked → success 分支
    None,                                         # 无任何结果文件 → unknown/unknown
])
def test_archive_reports_success_branch_bug(tmp_path, build_result):
    # 已知生产代码 bug:success 分支在 report_src 赋值前引用它(约第 1034 行,
    # 赋值在约第 1048 行),必然抛 UnboundLocalError,success 归档路径实际不可达。
    reports = tmp_path / "reports"
    reports.mkdir()
    if build_result:
        (reports / "build_rpm_result_foo.json").write_text(json.dumps(build_result))
    with pytest.raises(UnboundLocalError):
        p.archive_introduction_reports(["foo"], str(reports), str(tmp_path / "repo"))


def test_archive_reports_multiple_pkgs(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    for pkg in ("foo", "bar"):
        (reports / f"build_rpm_result_{pkg}.json").write_text(
            json.dumps({"action": "failed", "version": "1.0"}))
    assert p.archive_introduction_reports(["foo", "bar"], str(reports), str(repo)) == 2


# ─────────────────────────────────────────────
# mark_archived_in_result
# ─────────────────────────────────────────────

def test_mark_archived_no_reports_dir(tmp_path):
    f = tmp_path / "pkg_introduce_result_foo.json"
    f.write_text("{}")
    assert p.mark_archived_in_result(["foo"], None) is None
    assert f.read_text() == "{}"


def test_mark_archived_sets_flag(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    f = reports / "pkg_introduce_result_foo.json"
    f.write_text(json.dumps({"action": "failed", "version": "1.0"}, ensure_ascii=False))
    p.mark_archived_in_result(["foo"], str(reports))
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["archived"] is True
    assert data["action"] == "failed"
    assert data["version"] == "1.0"


def test_main_missing_pkgs(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["publish_rpm.py"])
    with pytest.raises(SystemExit) as ei:
        p.main()
    assert ei.value.code == 2


def test_mark_archived_missing_and_invalid(tmp_path, capsys):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "pkg_introduce_result_bad.json").write_text("{not json")
    p.mark_archived_in_result(["nofile", "bad"], str(reports))  # 不抛异常
    assert not (reports / "pkg_introduce_result_nofile.json").exists()
    assert "无法更新" in capsys.readouterr().err
