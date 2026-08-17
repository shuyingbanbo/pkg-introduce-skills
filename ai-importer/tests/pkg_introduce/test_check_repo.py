"""check_repo.py — 仓库活跃度检查(纯函数 + 单点 mock _http_get + check_repo 编排)。"""

from __future__ import annotations

import json
import sys
from urllib.parse import urlparse

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sys.path.insert(0, str(SCRIPT_DIRS["pkg_introduce"]))
cr = load_module("check_repo", SCRIPT_DIRS["pkg_introduce"] / "check_repo.py")


# ─────────────────────────────────────────────
# _parse_owner_repo
# ─────────────────────────────────────────────

@pytest.mark.parametrize("path,expected", [
    ("/owner/repo", ("owner", "repo")),
    ("/owner/repo.git", ("owner", "repo")),
    ("owner/repo/extra", ("owner", "repo")),
    ("/single", None),
    ("/", None),
])
def test_parse_owner_repo(path, expected):
    assert cr._parse_owner_repo(path) == expected


# ─────────────────────────────────────────────
# _days_since
# ─────────────────────────────────────────────

def test_days_since_valid():
    import datetime
    days = cr._days_since("2020-01-01T00:00:00Z")
    assert days is not None and days > 0


@pytest.mark.parametrize("iso_str", ["", "not-a-date", None])
def test_days_since_invalid(iso_str):
    assert cr._days_since(iso_str) is None


# ─────────────────────────────────────────────
# 各平台 checker(单点 mock _http_get)
# ─────────────────────────────────────────────

def _mock_get(monkeypatch, data):
    calls = []
    monkeypatch.setattr(cr, "_http_get", lambda url, token=None, timeout=10: (calls.append(url) or data))
    return calls


def test_check_github_ok(monkeypatch):
    calls = _mock_get(monkeypatch, {"pushed_at": "2025-01-01T00:00:00Z"})
    days, detail = cr._check_github(urlparse("https://github.com/psf/requests"))
    assert detail == "2025-01-01"
    assert calls[0].startswith("https://api.github.com/repos/psf/requests")


def test_check_github_bad_path(monkeypatch):
    _mock_get(monkeypatch, {})
    days, detail = cr._check_github(urlparse("https://github.com/single"))
    assert days is None
    assert "无法解析" in detail


def test_check_github_api_failure(monkeypatch):
    _mock_get(monkeypatch, None)
    days, detail = cr._check_github(urlparse("https://github.com/a/b"))
    assert days is None
    assert "API 请求失败" in detail


def test_check_gitlab_ok(monkeypatch):
    calls = _mock_get(monkeypatch, {"last_activity_at": "2025-06-01T00:00:00Z"})
    days, detail = cr._check_gitlab(urlparse("https://gitlab.com/group/sub/repo"))
    assert detail == "2025-06-01"
    assert "gitlab.com/api/v4/projects/" in calls[0]


def test_check_gitee_ok(monkeypatch):
    calls = _mock_get(monkeypatch, {"pushed_at": "2025-03-01T00:00:00Z"})
    days, detail = cr._check_gitee(urlparse("https://gitee.com/openeuler/hello"))
    assert detail == "2025-03-01"
    assert "gitee.com/api/v5/repos/openeuler/hello" in calls[0]


def test_check_gitcode_uses_token(monkeypatch):
    monkeypatch.setattr(cr, "_load_token", lambda: "secret-token")
    calls = []
    monkeypatch.setattr(cr, "_http_get", lambda url, token=None, timeout=10: (calls.append((url, token)) or {"pushed_at": "2025-01-01T00:00:00Z"}))
    days, detail = cr._check_gitcode(urlparse("https://gitcode.com/x/y"))
    assert days is not None
    assert calls[0][1] == "secret-token"
    assert "atomgit.com" not in calls[0][0]  # 用原始 host


def test_check_bitbucket_ok(monkeypatch):
    _mock_get(monkeypatch, {"updated_on": "2025-02-01T00:00:00Z"})
    days, detail = cr._check_bitbucket(urlparse("https://bitbucket.org/ws/repo"))
    assert detail == "2025-02-01"


def test_check_bitbucket_failure(monkeypatch):
    _mock_get(monkeypatch, {"no_key": 1})
    days, detail = cr._check_bitbucket(urlparse("https://bitbucket.org/ws/repo"))
    assert days is None


# ─────────────────────────────────────────────
# check_repo(编排)
# ─────────────────────────────────────────────

def test_check_repo_unknown_platform(tmp_path, monkeypatch):
    result = cr.check_repo("https://example.com/owner/repo")
    assert result["platform_type"] == "unknown"
    assert result["blocking"] is True
    assert "不在支持的平台列表中" in result["message"]


def test_check_repo_query_failure_not_blocking(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "CHECKERS", {"github": lambda p: (None, "API 请求失败")})
    result = cr.check_repo("https://github.com/psf/requests")
    assert result["blocking"] is False
    assert "请人工确认" in result["message"]


def test_check_repo_stale_blocks(tmp_path, monkeypatch):
    # 10 年前的推送 → stale → blocking(默认阈值 5 年)
    monkeypatch.setattr(cr, "_http_get", lambda url, token=None, timeout=10: {"pushed_at": "2010-01-01T00:00:00Z"})
    result = cr.check_repo("https://github.com/psf/requests")
    assert result["days_inactive"] > cr.INACTIVE_DAYS
    assert result["blocking"] is True
    assert "stale" in result["message"] or "超过" in result["message"]


def test_check_repo_fresh_not_blocking(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "_http_get", lambda url, token=None, timeout=10: {"pushed_at": "2025-06-01T00:00:00Z"})
    result = cr.check_repo("https://github.com/psf/requests")
    assert result["blocking"] is False


def test_check_repo_custom_threshold(tmp_path, monkeypatch):
    """config.json 覆盖阈值:30 天即算 stale。"""
    (tmp_path / "config.json").write_text(json.dumps(
        {"repo_check": {"inactive_days_threshold": 30, "blocking": True}}))
    monkeypatch.setattr(cr, "_load_config", lambda: json.loads(
        (tmp_path / "config.json").read_text()))
    monkeypatch.setattr(cr, "_http_get", lambda url, token=None, timeout=10: {"pushed_at": "2025-07-01T00:00:00Z"})
    result = cr.check_repo("https://github.com/psf/requests")
    assert result["blocking"] is True


def test_check_repo_config_blocking_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "_load_config", lambda: {"repo_check": {"blocking": False}})
    monkeypatch.setattr(cr, "_http_get", lambda url, token=None, timeout=10: {"pushed_at": "2010-01-01T00:00:00Z"})
    result = cr.check_repo("https://github.com/psf/requests")
    assert result["blocking"] is False  # stale 但 blocking 关


def test_check_repo_www_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "CHECKERS", {"github": lambda p: (1, "2025-06-01")})
    result = cr.check_repo("https://www.github.com/psf/requests")
    assert result["platform_type"] == "github"
