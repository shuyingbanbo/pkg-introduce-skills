"""resolve_upstream.py — 无 AI 解析上游 URL(纯函数 + 单点 mock _http_get + main)。"""

from __future__ import annotations

import json

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

ru = load_module("resolve_upstream", SCRIPT_DIRS["step"] / "resolve_upstream.py")


# ─────────────────────────────────────────────
# _clean_git_url
# ─────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("", ""),
    ("https://github.com/user/repo.git", "https://github.com/user/repo"),
    ("git://github.com/user/repo", "https://github.com/user/repo"),
    ("git+https://github.com/user/repo", "https://github.com/user/repo"),
    ("git+ssh://git@github.com/user/repo.git", "https://github.com/user/repo"),  # git+ 剥后 ssh://git@ 也转 https
    ("ssh://git@github.com/user/repo", "https://github.com/user/repo"),
    ("git@github.com:user/repo.git", "https://github.com/user/repo"),  # 无协议
    ("https://github.com/user/repo/", "https://github.com/user/repo"),  # 去尾部 /
])
def test_clean_git_url(url, expected):
    assert ru._clean_git_url(url) == expected


# ─────────────────────────────────────────────
# _is_trusted
# ─────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://github.com/x/y", True),
    ("https://gitee.com/x/y", True),
    ("https://gitcode.com/x/y", True),
    ("https://evil.example.com/x", False),
    ("", False),
])
def test_is_trusted(url, expected):
    assert ru._is_trusted(url) is expected


# ─────────────────────────────────────────────
# detect_lang
# ─────────────────────────────────────────────

@pytest.mark.parametrize("pkg,hint,expected", [
    ("foo", "python", "python"),
    ("nodejs-lodash", "", "nodejs"),
    ("python3-requests", "", "python"),
    ("python-requests", "", "python"),
    ("rust-serde", "", "rust"),
    ("unknown-pkg", "", ""),
])
def test_detect_lang(pkg, hint, expected):
    assert ru.detect_lang(pkg, hint) == expected


# ─────────────────────────────────────────────
# resolve_*:单点 mock _http_get
# ─────────────────────────────────────────────

def test_resolve_nodejs_repository(monkeypatch):
    monkeypatch.setattr(ru, "_http_get", lambda url: {
        "repository": {"url": "git+https://github.com/lodash/lodash.git"},
        "homepage": "https://evil.example.com/",
    })
    assert ru.resolve_nodejs("nodejs-lodash") == "https://github.com/lodash/lodash"


def test_resolve_nodejs_fallback_homepage(monkeypatch):
    monkeypatch.setattr(ru, "_http_get", lambda url: {
        "repository": {"url": "https://evil.example.com/x"},
        "homepage": "https://github.com/x/y",
    })
    assert ru.resolve_nodejs("nodejs-foo") == "https://github.com/x/y"


def test_resolve_nodejs_no_trusted(monkeypatch):
    monkeypatch.setattr(ru, "_http_get", lambda url: {
        "repository": {"url": "https://evil.example.com/x"},
        "homepage": "",
    })
    assert ru.resolve_nodejs("nodejs-foo") is None


def test_resolve_nodejs_http_failure(monkeypatch):
    monkeypatch.setattr(ru, "_http_get", lambda url: None)
    assert ru.resolve_nodejs("nodejs-foo") is None


def test_resolve_python_project_urls(monkeypatch):
    monkeypatch.setattr(ru, "_http_get", lambda url: {
        "info": {"project_urls": {"Homepage": "https://gitee.com/psf/requests"}},
    })
    assert ru.resolve_python("python3-requests") == "https://gitee.com/psf/requests"


def test_resolve_python_fallback_fields(monkeypatch):
    monkeypatch.setattr(ru, "_http_get", lambda url: {
        "info": {"project_url": "https://github.com/psf/requests/"},
    })
    assert ru.resolve_python("requests") == "https://github.com/psf/requests"


def test_resolve_python_no_trusted(monkeypatch):
    monkeypatch.setattr(ru, "_http_get", lambda url: {"info": {"home_page": "https://x.com"}})
    assert ru.resolve_python("python3-foo") is None


def test_resolve_rust_repository(monkeypatch):
    monkeypatch.setattr(ru, "_http_get", lambda url: {
        "crate": {"repository": "https://github.com/serde-rs/serde"},
    })
    assert ru.resolve_rust("serde") == "https://github.com/serde-rs/serde"


def test_resolve_rust_untrusted_or_missing(monkeypatch):
    monkeypatch.setattr(ru, "_http_get", lambda url: {"crate": {"repository": "https://x.com"}})
    assert ru.resolve_rust("serde") is None
    monkeypatch.setattr(ru, "_http_get", lambda url: None)
    assert ru.resolve_rust("serde") is None


# ─────────────────────────────────────────────
# resolve_upstream(语言调度)
# ─────────────────────────────────────────────

def test_resolve_upstream_lang_hint(monkeypatch):
    calls = []
    monkeypatch.setattr(ru, "_http_get", lambda url: (calls.append(url) or None))
    url, source = ru.resolve_upstream("foo", "python")
    assert url is None
    # 语言提示 python → 先 pypi,再 fallback npm
    assert any("pypi.org" in c for c in calls)
    assert any("registry.npmjs.org" in c for c in calls)


def test_resolve_upstream_first_hit_wins(monkeypatch):
    monkeypatch.setattr(ru, "_http_get", lambda url: {
        "info": {"project_url": "https://github.com/psf/requests"},
    } if "pypi.org" in url else None)
    url, source = ru.resolve_upstream("python3-requests")
    assert url == "https://github.com/psf/requests"
    assert source == "pypi"


def test_resolve_upstream_returns_none(monkeypatch):
    monkeypatch.setattr(ru, "_http_get", lambda url: None)
    assert ru.resolve_upstream("whatever") == (None, None)


# ─────────────────────────────────────────────
# main(CLI)
# ─────────────────────────────────────────────

def _main(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["resolve_upstream.py"] + argv)
    return ru.main()


def test_main_writes_registry_and_prints(tmp_path, monkeypatch, capsys):
    (tmp_path / "dep_registry.json").write_text(json.dumps({
        "requests": {"url": "", "constraint": "", "status": "pending_evaluate"},
    }))
    monkeypatch.setattr(ru, "resolve_upstream",
                        lambda *a, **k: ("https://github.com/psf/requests", "pypi"))
    rc = _main(monkeypatch, ["--pkg", "requests", "--session-dir", str(tmp_path)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "https://github.com/psf/requests"
    reg = json.loads((tmp_path / "dep_registry.json").read_text())
    assert reg["requests"]["url"] == "https://github.com/psf/requests"
    assert reg["requests"]["url_resolution"] == "pypi"


def test_main_does_not_overwrite_existing_url(tmp_path, monkeypatch, capsys):
    (tmp_path / "dep_registry.json").write_text(json.dumps({
        "requests": {"url": "https://old.example.com", "status": "pending_evaluate"},
    }))
    monkeypatch.setattr(ru, "resolve_upstream",
                        lambda *a, **k: ("https://github.com/psf/requests", "pypi"))
    rc = _main(monkeypatch, ["--pkg", "requests", "--session-dir", str(tmp_path)])
    assert rc == 0
    reg = json.loads((tmp_path / "dep_registry.json").read_text())
    assert reg["requests"]["url"] == "https://old.example.com"  # 已有 url 不覆盖


def test_main_json_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ru, "resolve_upstream",
                        lambda *a, **k: ("https://github.com/psf/requests", "pypi"))
    rc = _main(monkeypatch, ["--pkg", "requests", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"status": "resolved", "url": "https://github.com/psf/requests", "source": "pypi"}


def test_main_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ru, "resolve_upstream", lambda *a, **k: (None, None))
    rc = _main(monkeypatch, ["--pkg", "ghost", "--json"])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "failed"
