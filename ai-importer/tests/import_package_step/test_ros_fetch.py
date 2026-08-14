"""ros_fetch.py — ROS 源码获取:URL 解析 + map 加载 + 依赖补注册(纯逻辑)。"""

from __future__ import annotations

import json

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

rf = load_module("ros_fetch", SCRIPT_DIRS["step"] / "ros_fetch.py")


# ─────────────────────────────────────────────
# _parse_repo_url
# ─────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://github.com/ros2/rclcpp", ("https://github.com/ros2/rclcpp", "humble")),
    ("https://github.com/ros2/rclcpp/tree/rolling",
     ("https://github.com/ros2/rclcpp", "rolling")),
    ("  https://github.com/ros2/rclcpp/tree/humble  ",
     ("https://github.com/ros2/rclcpp", "humble")),   # 首尾空白
    ("https://gitee.com/x/y/tree/", ("https://gitee.com/x/y", "")),  # 空 branch
])
def test_parse_repo_url(url, expected):
    assert rf._parse_repo_url(url) == expected


# ─────────────────────────────────────────────
# _load_map
# ─────────────────────────────────────────────

def test_load_map_missing(tmp_path):
    assert rf._load_map(tmp_path / "nope") == {}


def test_load_map_basic(tmp_path):
    f = tmp_path / "map.txt"
    f.write_text("# comment\n\npkg1 url1\npkg2\turl2\ninvalid-line\n")
    assert rf._load_map(f) == {"pkg1": "url1", "pkg2": "url2"}


# ─────────────────────────────────────────────
# _reregister_deps
# ─────────────────────────────────────────────

def test_reregister_deps_runs_ros_prep(tmp_path, fake_subprocess, capsys):
    (tmp_path / "session.json").write_text(json.dumps({"deep_dependency": True}))
    fake_subprocess.when(lambda s: "ros_prep.py" in s, returncode=0)
    rf._reregister_deps(tmp_path, "rclcpp")
    assert fake_subprocess.called_with("ros_prep.py")
    assert "依赖补注册完成" in capsys.readouterr().out


def test_reregister_deps_appends_deep_flag(tmp_path, fake_subprocess):
    (tmp_path / "session.json").write_text(json.dumps({"deep_dependency": "true"}))
    fake_subprocess.when(lambda s: "ros_prep.py" in s, returncode=0)
    rf._reregister_deps(tmp_path, "rclcpp")
    cmd = next(c for c, _ in fake_subprocess.calls if "ros_prep.py" in " ".join(c))
    assert "--deep" in cmd


def test_reregister_deps_no_deep_flag(tmp_path, fake_subprocess):
    (tmp_path / "session.json").write_text(json.dumps({"deep_dependency": False}))
    fake_subprocess.when(lambda s: "ros_prep.py" in s, returncode=0)
    rf._reregister_deps(tmp_path, "rclcpp")
    cmd = next(c for c, _ in fake_subprocess.calls if "ros_prep.py" in " ".join(c))
    assert "--deep" not in cmd


def test_reregister_deps_bad_session_ok(tmp_path, fake_subprocess, capsys):
    """session.json 损坏不影响补注册(异常吞掉)。"""
    (tmp_path / "session.json").write_text("{bad")
    fake_subprocess.when(lambda s: "ros_prep.py" in s, returncode=0)
    rf._reregister_deps(tmp_path, "rclcpp")
    assert fake_subprocess.called_with("ros_prep.py")


def test_reregister_deps_failure_warns(tmp_path, fake_subprocess, capsys):
    fake_subprocess.when(lambda s: "ros_prep.py" in s, returncode=1, stderr="boom")
    rf._reregister_deps(tmp_path, "rclcpp")
    assert "补注册失败" in capsys.readouterr().err
