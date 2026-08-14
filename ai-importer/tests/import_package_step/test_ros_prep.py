"""ros_prep.py — ROS 引包预检(纯函数 + main 编排,mock load_projects/_cascade_query)。"""

from __future__ import annotations

import json

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

rp = load_module("ros_prep", SCRIPT_DIRS["step"] / "ros_prep.py")


# ─────────────────────────────────────────────
# _norm_name
# ─────────────────────────────────────────────

@pytest.mark.parametrize("pkg,expected", [
    ("rclcpp", "rclcpp"),
    ("ros-humble-rclcpp", "rclcpp"),
    ("ros2-rclcpp", "rclcpp"),
    ("ament_cmake", "ament-cmake"),       # _ → -
    ("ros-humble-ament_cmake", "ament-cmake"),
    ("  rclcpp  ", "rclcpp"),             # 首尾空白
])
def test_norm_name(pkg, expected):
    assert rp._norm_name(pkg) == expected


# ─────────────────────────────────────────────
# _cmp_version
# ─────────────────────────────────────────────

@pytest.mark.parametrize("listed,official,expected", [
    ("1.5.0", "1.0.0", 1),
    ("1.0.0", "1.5.0", -1),
    ("1.0.0", "1.0.0", 0),
    ("1.10", "1.9", 1),                   # 数值比较
    ("2.0", "10.0", -1),
    ("1.0-1", "1.0", 1),                  # - 当 . 处理 → 1.0.1 > 1.0
    ("abc", "1.0", 0),                    # 无法比较 → 0
])
def test_cmp_version(listed, official, expected):
    assert rp._cmp_version(listed, official) == expected


# ─────────────────────────────────────────────
# _is_official
# ─────────────────────────────────────────────

@pytest.mark.parametrize("decision,expected", [
    ("reuse_official", True),
    ("reuse_eur_srpm", True),
    ("reuse_copr_project", True),
    ("reuse_additional_repo", True),
    ("introduce_new", False),
    ("introduce_new_with_ref", False),
    ("evaluate", False),
    ("", False),
])
def test_is_official(decision, expected):
    assert rp._is_official(decision) is expected


# ─────────────────────────────────────────────
# _read_session / _write_json
# ─────────────────────────────────────────────

def test_read_session_missing(tmp_path):
    assert rp._read_session(tmp_path) == {}


def test_read_session_bad_json(tmp_path):
    (tmp_path / "session.json").write_text("{bad json")
    assert rp._read_session(tmp_path) == {}


def test_read_session_ok(tmp_path):
    (tmp_path / "session.json").write_text(json.dumps({"copr_url": "x"}))
    assert rp._read_session(tmp_path) == {"copr_url": "x"}


def test_write_json_creates_parents(tmp_path):
    p = tmp_path / "a" / "b" / "f.json"
    rp._write_json(p, {"k": "v"})
    assert json.loads(p.read_text()) == {"k": "v"}


# ─────────────────────────────────────────────
# main:定位与 gate 判定
# ─────────────────────────────────────────────

def _run(monkeypatch, session_dir, *args):
    monkeypatch.setattr("sys.argv", ["ros_prep.py", "--session-dir", str(session_dir)] + list(args))
    return rp.main()


@pytest.fixture
def no_net(monkeypatch):
    """默认让 cascade 保守返回 introduce_new,projects 非空(空 projects 直接 fail)。"""
    monkeypatch.setattr(rp, "load_projects", lambda d: {"dummy": ("url", "active", "1.0")})
    monkeypatch.setattr(rp, "load_upstream", lambda d: {})
    monkeypatch.setattr(rp, "_cascade_query",
                        lambda *a, **k: {"decision": "introduce_new", "match": {}})


def test_main_projects_empty_fails(tmp_path, monkeypatch, capsys, no_net):
    monkeypatch.setattr(rp, "load_projects", lambda d: {})
    rc = _run(monkeypatch, tmp_path, "--pkg", "rclcpp")
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "failed"
    assert "ros-projects.list 为空" in out["reason"]
    gate = json.loads((tmp_path / "pkgs" / "rclcpp" / "gate_result_rclcpp.json").read_text())
    assert gate["overall_status"] == "failed"


def test_main_sig_tier_introduce_new(tmp_path, monkeypatch, capsys, no_net):
    monkeypatch.setattr(rp, "load_projects", lambda d: {
        "rclcpp": ("https://github.com/ros2/rclcpp", "active", "20.0.0"),
    })
    rc = _run(monkeypatch, tmp_path, "--pkg", "rclcpp")
    assert rc == 0

    pkg_dir = tmp_path / "pkgs" / "rclcpp"
    manifest = json.loads((pkg_dir / "ros_pkg_manifest.json").read_text())
    assert manifest["tier"] == "sig"
    assert manifest["repo_url"] == "https://github.com/ros2/rclcpp"
    assert manifest["target_version"] == "20.0.0"
    assert manifest["gate_decision"] == "introduce_new"

    gate = json.loads((pkg_dir / "gate_result_rclcpp.json").read_text())
    assert gate["overall_status"] == "done"
    assert gate["result"]["decision"] == "introduce_new"
    assert gate["disposition"] == "introduce_new"


def test_main_upstream_tier_with_branch(tmp_path, monkeypatch, capsys, no_net):
    monkeypatch.setattr(rp, "load_projects", lambda d: {"rclcpp": ("url", "active", "1.0")})
    monkeypatch.setattr(rp, "load_upstream", lambda d: {
        "ament-cmake": ("https://github.com/ros2/ament_cmake", "humble", "active", "1.3.0"),
    })
    rc = _run(monkeypatch, tmp_path, "--pkg", "ament-cmake")
    assert rc == 0
    manifest = json.loads((tmp_path / "pkgs" / "ament-cmake" / "ros_pkg_manifest.json").read_text())
    assert manifest["tier"] == "upstream"
    assert manifest["repo_branch"] == "humble"


def test_main_user_url_tier(tmp_path, monkeypatch, capsys, no_net):
    monkeypatch.setattr(rp, "load_projects", lambda d: {"rclcpp": ("url", "active", "1.0")})
    (tmp_path / "session.json").write_text(json.dumps({
        "upstream_url": "https://github.com/user/self-pkg", "version": "2.0.0",
    }))
    rc = _run(monkeypatch, tmp_path, "--pkg", "self-pkg")
    assert rc == 0
    manifest = json.loads((tmp_path / "pkgs" / "self-pkg" / "ros_pkg_manifest.json").read_text())
    assert manifest["tier"] == "user"
    assert manifest["repo_url"] == "https://github.com/user/self-pkg"
    assert manifest["target_version"] == "2.0.0"


def test_main_not_found_fails(tmp_path, monkeypatch, capsys, no_net):
    rc = _run(monkeypatch, tmp_path, "--pkg", "ghost-pkg")
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert "都不存在" in out["reason"]


def test_main_reuse_official_sets_goal(tmp_path, monkeypatch, capsys, no_net):
    monkeypatch.setattr(rp, "load_projects", lambda d: {
        "rclcpp": ("https://github.com/ros2/rclcpp", "active", "20.0.0"),
    })
    monkeypatch.setattr(rp, "_cascade_query",
                        lambda *a, **k: {"decision": "reuse_official",
                                         "match": {"version": "25.0.0"}})
    (tmp_path / "workflow_main.json").write_text(json.dumps({"pkgname": "mainpkg"}))

    rc = _run(monkeypatch, tmp_path, "--pkg", "rclcpp")
    assert rc == 0

    gate = json.loads((tmp_path / "pkgs" / "rclcpp" / "gate_result_rclcpp.json").read_text())
    assert gate["result"]["decision"] == "reuse_official"
    assert gate["disposition"] == "reuse"

    wf = json.loads((tmp_path / "workflow_main.json").read_text())
    assert wf["goal_achieved"] is True
    assert "rclcpp" in wf["reused_pkgs"]


def test_main_official_older_than_target_upgrade(tmp_path, monkeypatch, capsys, no_net):
    monkeypatch.setattr(rp, "load_projects", lambda d: {
        "rclcpp": ("https://github.com/ros2/rclcpp", "active", "30.0.0"),
    })
    monkeypatch.setattr(rp, "_cascade_query",
                        lambda *a, **k: {"decision": "reuse_official",
                                         "match": {"version": "25.0.0"}})
    rc = _run(monkeypatch, tmp_path, "--pkg", "rclcpp")
    assert rc == 0
    gate = json.loads((tmp_path / "pkgs" / "rclcpp" / "gate_result_rclcpp.json").read_text())
    assert gate["result"]["decision"] == "introduce_new_with_ref"
    assert gate["disposition"] == "upgrade"


def test_main_cascade_exception_conservative(tmp_path, monkeypatch, capsys, no_net):
    """cascade 查询抛异常 → 保守按 introduce_new 走引入链。"""
    monkeypatch.setattr(rp, "load_projects", lambda d: {
        "rclcpp": ("https://github.com/ros2/rclcpp", "active", "20.0.0"),
    })
    monkeypatch.setattr(rp, "check_package_existence",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network down")))
    rc = _run(monkeypatch, tmp_path, "--pkg", "rclcpp")
    assert rc == 0
    gate = json.loads((tmp_path / "pkgs" / "rclcpp" / "gate_result_rclcpp.json").read_text())
    assert gate["result"]["decision"] == "introduce_new"


def test_main_writes_missing_deps_file(tmp_path, monkeypatch, capsys, fake_subprocess):
    """deep 模式 + SIG 缺口 → missing_deps_<pkg>.txt 写入。"""
    fake_subprocess.when(lambda s: "register-dep.py" in s, returncode=1, stderr="boom")
    monkeypatch.setattr(rp, "load_projects", lambda d: {
        "rclcpp": ("https://github.com/ros2/rclcpp", "active", "20.0.0"),
        "my-ros-lib": ("https://github.com/ros2/my-ros-lib", "active", "1.0.0"),
    })
    monkeypatch.setattr(rp, "load_upstream", lambda d: {})
    monkeypatch.setattr(rp, "_cascade_query",
                        lambda *a, **k: {"decision": "introduce_new", "match": {}})

    # 提供 sources/ 触发依赖解析:fake parse_package_xml + classify_deps
    src = tmp_path / "sources" / "rclcpp"
    src.mkdir(parents=True)
    (src / "package.xml").write_text("<package/>")
    import analyze_ros_deps
    monkeypatch.setattr(analyze_ros_deps, "parse_package_xml", lambda *a, **k: {
        "found": True, "deps": ["my-ros-lib"], "buildtool_deps": [],
    })
    monkeypatch.setattr(analyze_ros_deps, "classify_deps",
                        lambda *a, **k: {"ros_deps": ["my-ros-lib"], "ros_deps_upstream": []})
    monkeypatch.setattr(analyze_ros_deps, "load_remap", lambda: {})

    rc = _run(monkeypatch, tmp_path, "--pkg", "rclcpp", "--deep")
    assert rc == 0
    missing = tmp_path / "pkgs" / "rclcpp" / "missing_deps_rclcpp.txt"
    assert missing.exists()
    assert "ros-humble-my-ros-lib" in missing.read_text()
