"""init_archive_repo.py 单元测试 — 归档仓初始化(clone/pull)、session.json 回写。"""

from __future__ import annotations

import json
import sys

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sys.path.insert(0, str(SCRIPT_DIRS["archive"]))
m = load_module("init_archive_repo",
                SCRIPT_DIRS["archive"] / "init_archive_repo.py")


def _write_cfg(tmp_path, local_dir, with_token=True):
    git_cfg = {"token": "tok", "username": "u"} if with_token else {}
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "gitcode": git_cfg,
        "repo": {"remote_url": "https://github.com/o/r.git",
                 "branch": "main", "local_dir": str(local_dir)},
    }))
    return cfg


# ─────────────────────────────────────────────
# load_config
# ─────────────────────────────────────────────

def test_load_config_valid(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"a": 1}))
    assert m.load_config(cfg) == {"a": 1}


def test_load_config_missing(tmp_path, capsys):
    with pytest.raises(SystemExit) as ei:
        m.load_config(tmp_path / "nope.json")
    assert ei.value.code == 1
    assert "配置文件不存在" in capsys.readouterr().err


def test_load_config_invalid_json(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text("{bad")
    with pytest.raises(json.JSONDecodeError):
        m.load_config(cfg)


# ─────────────────────────────────────────────
# auth_url
# ─────────────────────────────────────────────

@pytest.mark.parametrize("remote,username,token,expected", [
    ("https://github.com/o/r.git", "u", "tok", "https://u:tok@github.com/o/r.git"),
    ("https://github.com/o/r.git", "oauth2", "", "https://github.com/o/r.git"),
    ("git@github.com:o/r.git", "u", "tok", "git@github.com:o/r.git"),  # 无 :// 不注入
    ("http://x/y.git", "a", "b", "http://a:b@x/y.git"),
    ("https://h/p", "u", "t", "https://u:t@h/p"),
])
def test_auth_url(remote, username, token, expected):
    assert m.auth_url(remote, username, token) == expected


# ─────────────────────────────────────────────
# init_or_update_repo
# ─────────────────────────────────────────────

def test_init_or_update_repo_pull(tmp_path, fake_subprocess):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    m.init_or_update_repo(repo, "https://o/r.git", "main")
    assert fake_subprocess.called_with("git pull origin main")
    assert not fake_subprocess.called_with("git clone")


def test_init_or_update_repo_clone_ok(tmp_path, fake_subprocess):
    repo = tmp_path / "repo"
    fake_subprocess.when("git clone", returncode=0)
    m.init_or_update_repo(repo, "https://o/r.git", "main")
    assert fake_subprocess.called_with("git clone --branch main https://o/r.git")
    assert not fake_subprocess.called_with("git init")


def test_init_or_update_repo_empty_remote(tmp_path, fake_subprocess):
    repo = tmp_path / "repo"
    fake_subprocess.when("git clone", returncode=1, stderr="empty repository")
    m.init_or_update_repo(repo, "https://u:tok@github.com/o/r.git", "main")
    assert repo.is_dir()
    assert fake_subprocess.called_with("git init")
    assert fake_subprocess.called_with("git checkout -b main")
    # remote add 使用带认证的 URL
    assert fake_subprocess.called_with(
        "git remote add origin https://u:tok@github.com/o/r.git")


# ─────────────────────────────────────────────
# main
# ─────────────────────────────────────────────

def test_main_pull_path_writes_session(tmp_path, fake_subprocess, monkeypatch, capsys):
    local = tmp_path / "repo"
    local.mkdir()
    (local / ".git").mkdir()
    session = tmp_path / "session.json"
    session.write_text(json.dumps({"pkgs": ["foo"]}, ensure_ascii=False))
    cfg = _write_cfg(tmp_path, local)
    monkeypatch.setattr(sys, "argv", ["init_archive_repo.py",
                                      "--session-json", str(session),
                                      "--config", str(cfg)])
    assert m.main() == 0
    data = json.loads(session.read_text(encoding="utf-8"))
    assert data["repo_local"] == str(local)
    assert data["pkgs"] == ["foo"]  # 原有字段保留
    assert (local / "dist").is_dir()
    assert f"REPO_LOCAL={local}" in capsys.readouterr().out


def test_main_default_config_path(tmp_path, fake_subprocess, monkeypatch):
    # 默认 config 为脚本目录的上级目录(archive-rpm-sources/)下的 config.json
    skill_root = tmp_path / "archive-rpm-sources"
    skill_root.mkdir()
    monkeypatch.setattr(m, "__file__", str(skill_root / "scripts" / "init_archive_repo.py"))
    local = tmp_path / "repo"
    cfg = skill_root / "config.json"
    cfg.write_text(json.dumps({"repo": {"remote_url": "https://o/r.git",
                                        "branch": "main", "local_dir": str(local)}}))
    session = tmp_path / "session.json"
    session.write_text("{}")
    fake_subprocess.when("git clone", returncode=0)
    monkeypatch.setattr(sys, "argv", ["init_archive_repo.py",
                                      "--session-json", str(session)])
    assert m.main() == 0
    assert json.loads(session.read_text(encoding="utf-8"))["repo_local"] == str(local)
    assert (local / "dist").is_dir()


def test_main_missing_config_exits(tmp_path, monkeypatch, capsys):
    session = tmp_path / "session.json"
    session.write_text("{}")
    monkeypatch.setattr(sys, "argv", ["init_archive_repo.py",
                                      "--session-json", str(session),
                                      "--config", str(tmp_path / "nope.json")])
    with pytest.raises(SystemExit) as ei:
        m.main()
    assert ei.value.code == 1
    assert "配置文件不存在" in capsys.readouterr().err


def test_main_missing_session_arg(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["init_archive_repo.py"])
    with pytest.raises(SystemExit) as ei:
        m.main()
    assert ei.value.code == 2
