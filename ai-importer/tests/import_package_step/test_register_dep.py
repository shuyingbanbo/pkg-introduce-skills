"""register-dep.py — dep_registry 依赖注册(URL 校验 + 注册/更新/冲突语义)。"""

from __future__ import annotations

import json

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

rd = load_module("register-dep", SCRIPT_DIRS["step"] / "register-dep.py")


# ─────────────────────────────────────────────
# is_git_repo_url(纯逻辑)
# ─────────────────────────────────────────────

@pytest.mark.parametrize("url,ok", [
    ("https://github.com/mesonbuild/meson", True),
    ("https://gitee.com/openeuler/hello", True),
    ("https://github.com/org/repo.git", True),
    ("http://gitlab.com/org/repo", True),          # http 也允许
    ("https://unknown.host.com/owner/repo", True),  # 未知主机 + owner/repo → 警告放行
    ("", False),
    ("https://pypi.org/project/foo/", False),       # 包注册表
    ("https://npmjs.com/package/foo", False),
    ("https://crates.io/crates/foo", False),
    ("https://docs.python.org/3/", False),
    ("https://github.com/foo/releases/download/v1.0/x.tar.gz", False),
    ("https://github.com/single", False),           # 可信主机缺 owner/repo
    ("ftp://github.com/a/b", False),                # 协议不允许
    ("https://unknown.host.com/single", False),     # 未知主机 + 单段路径
])
def test_is_git_repo_url(url, ok):
    result, reason = rd.is_git_repo_url(url)
    assert result is ok, f"{url}: {reason}"


def test_is_git_repo_url_reason_text():
    ok, reason = rd.is_git_repo_url("")
    assert reason == "URL 为空"
    ok, reason = rd.is_git_repo_url("https://github.com/a")
    assert "owner/repo" in reason
    ok, reason = rd.is_git_repo_url("https://pypi.org/project/x")
    assert "不是 git 仓库" in reason


# ─────────────────────────────────────────────
# main:注册/更新/冲突
# ─────────────────────────────────────────────

def _reg_path(session_dir):
    return session_dir / "dep_registry.json"


def _run(monkeypatch, session_dir, *args):
    monkeypatch.setattr("sys.argv", ["register-dep.py", "--session-dir", str(session_dir)] + list(args))
    return rd.main()


def test_register_new_dep(tmp_path, monkeypatch, capsys):
    _run(monkeypatch, tmp_path, "--pkg", "requests", "--url", "https://github.com/psf/requests",
         "--constraint", ">= 2.0", "--required-by", "python-numpy")
    reg = json.loads(_reg_path(tmp_path).read_text())
    assert reg["requests"] == {
        "url": "https://github.com/psf/requests",
        "constraint": ">= 2.0",
        "status": "pending_evaluate",
        "required_by": "python-numpy",
    }
    assert "registered requests" in capsys.readouterr().out


def test_register_new_dep_with_lang_vendor(tmp_path, monkeypatch, capsys):
    _run(monkeypatch, tmp_path, "--pkg", "serde", "--url", "https://github.com/serde-rs/serde",
         "--lang", "Rust")  # lang 归一为小写
    reg = json.loads(_reg_path(tmp_path).read_text())
    assert reg["serde"]["lang"] == "rust"


def test_register_gav_normalized(tmp_path, monkeypatch, capsys):
    _run(monkeypatch, tmp_path, "--pkg", "com.google.guava:guava", "--skip-url-check")
    reg = json.loads(_reg_path(tmp_path).read_text())
    assert "guava" in reg
    assert "com.google.guava:guava" not in reg


def test_register_existing_no_change(tmp_path, monkeypatch, capsys):
    (tmp_path / "dep_registry.json").write_text(json.dumps({
        "requests": {"url": "https://github.com/psf/requests", "constraint": ">= 2.0",
                  "status": "pending_evaluate", "required_by": ""},
    }))
    _run(monkeypatch, tmp_path, "--pkg", "requests", "--constraint", ">= 2.0")
    assert "already registered, no change" in capsys.readouterr().out


def test_register_existing_merge_constraint(tmp_path, monkeypatch, capsys):
    (tmp_path / "dep_registry.json").write_text(json.dumps({
        "requests": {"url": "", "constraint": ">= 2.0", "status": "pending_evaluate"},
    }))
    _run(monkeypatch, tmp_path, "--pkg", "requests", "--constraint", "< 3.0")
    reg = json.loads(_reg_path(tmp_path).read_text())
    # 注意:merge_constraints 按 f"{op}{ver}" 重建,不带空格
    assert reg["requests"]["constraint"] == ">=2.0, <3.0"
    assert "updated requests" in capsys.readouterr().out


def test_register_existing_fill_url(tmp_path, monkeypatch, capsys):
    (tmp_path / "dep_registry.json").write_text(json.dumps({
        "requests": {"url": "", "constraint": "", "status": "pending_evaluate"},
    }))
    _run(monkeypatch, tmp_path, "--pkg", "requests", "--url", "https://github.com/psf/requests")
    reg = json.loads(_reg_path(tmp_path).read_text())
    assert reg["requests"]["url"] == "https://github.com/psf/requests"


def test_register_existing_conflict_exits_1(tmp_path, monkeypatch, capsys):
    (tmp_path / "dep_registry.json").write_text(json.dumps({
        "requests": {"url": "", "constraint": ">= 2.0", "status": "pending_evaluate"},
    }))
    with pytest.raises(SystemExit) as e:
        _run(monkeypatch, tmp_path, "--pkg", "requests", "--constraint", "< 1.5")
    assert e.value.code == 1
    # 旧约束不被覆盖
    reg = json.loads(_reg_path(tmp_path).read_text())
    assert reg["requests"]["constraint"] == ">= 2.0"


def test_register_bad_url_exits_1(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit) as e:
        _run(monkeypatch, tmp_path, "--pkg", "foo", "--url", "https://pypi.org/project/foo/")
    assert e.value.code == 1
    assert "URL 校验失败" in capsys.readouterr().err


def test_register_toolchain_exits_2(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit) as e:
        _run(monkeypatch, tmp_path, "--pkg", "gcc", "--skip-url-check")
    assert e.value.code == 2
    assert "toolchain" in capsys.readouterr().err


# ─────────────────────────────────────────────
# main:ROS 两级校验
# ─────────────────────────────────────────────

def test_register_ros_tier2_upstream(tmp_path, monkeypatch, capsys):
    """SIG 未移植但 rosdistro 真实存在 → 放行注册,lang=ros,自动补 url。"""
    # 先触发一次 load_projects 使 analyze_ros_deps 进入 sys.modules
    _run(monkeypatch, tmp_path, "--pkg", "foo", "--skip-url-check")
    import analyze_ros_deps
    monkeypatch.setattr(analyze_ros_deps, "load_projects", lambda d: {"rclcpp": ("url", "active", "1.0")})
    monkeypatch.setattr(analyze_ros_deps, "load_upstream", lambda d: {
        "ament-cmake": ("https://github.com/ros2/ament_cmake", "humble", "active", "1.3.0"),
    })

    _run(monkeypatch, tmp_path, "--pkg", "ros-humble-ament-cmake")
    reg = json.loads(_reg_path(tmp_path).read_text())
    entry = reg["ros-humble-ament-cmake"]
    assert entry["lang"] == "ros"
    assert entry["url"] == "https://github.com/ros2/ament_cmake"
    assert "SIG 源未移植" in capsys.readouterr().err


def test_register_ros_unknown_exits_3(tmp_path, monkeypatch, capsys):
    _run(monkeypatch, tmp_path, "--pkg", "foo", "--skip-url-check")
    import analyze_ros_deps
    monkeypatch.setattr(analyze_ros_deps, "load_projects", lambda d: {"rclcpp": ("url", "active", "1.0")})
    monkeypatch.setattr(analyze_ros_deps, "load_upstream", lambda d: {})

    with pytest.raises(SystemExit) as e:
        _run(monkeypatch, tmp_path, "--pkg", "ros-humble-fakepkg")
    assert e.value.code == 3
    assert "拒绝注册" in capsys.readouterr().err
