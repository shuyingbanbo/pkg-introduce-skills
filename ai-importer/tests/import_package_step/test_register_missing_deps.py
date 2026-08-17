"""register-missing-deps.py — 从构建日志提取缺失依赖并注册。

注意(实现行为,非文档):missing 依赖由日志正则提取——
- "No matching package to install: 'xxx >= 1.0'" 捕获整串(含约束),注册 key 即整串,
  constraint 提取不出(不匹配第二条 nothing provides 形式);
- "nothing provides xxx >= 1.0 needed by" 捕获纯包名,constraint 可提取。
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

rm = load_module("register-missing-deps", SCRIPT_DIRS["step"] / "register-missing-deps.py")


def _run(monkeypatch, session_dir, *args):
    monkeypatch.setattr("sys.argv", ["register-missing-deps.py",
                                     "--session-dir", str(session_dir)] + list(args))
    return rm.main()


# ─────────────────────────────────────────────
# _extract_constraint
# ─────────────────────────────────────────────

@pytest.mark.parametrize("log,rpm_pkg,expected", [
    ("nothing provides libfoo >= 1.0 needed by main", "libfoo", ">= 1.0"),
    ("nothing provides libbar >= 2.0 needed by main", "libbar", ">= 2.0"),
    # No matching 形式的整串(含约束)提取不出约束
    ("No matching package to install: 'libfoo >= 1.4.0'", "libfoo >= 1.4.0", ""),
    ("nothing provides libbaz needed by main", "libbaz", ""),
    ("nothing relevant", "libfoo", ""),
    ("", "libfoo", ""),
])
def test_extract_constraint(log, rpm_pkg, expected):
    assert rm._extract_constraint(log, rpm_pkg) == expected


# ─────────────────────────────────────────────
# main
# ─────────────────────────────────────────────

def _setup(tmp_path, log="", registry=None):
    pkg_dir = tmp_path / "pkgs" / "pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "build_rpm_result.json").write_text(json.dumps({"build_log": log}))
    if registry is not None:
        (tmp_path / "dep_registry.json").write_text(json.dumps(registry))
    return tmp_path


def test_main_no_missing(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, log="build succeeded")
    _run(monkeypatch, tmp_path, "--pkg", "pkg")
    assert "no missing packages found" in capsys.readouterr().out


def test_main_registers_from_nothing_provides(tmp_path, monkeypatch, capsys):
    # 注意:nothing provides 提取正则 [^\s]+ 后须紧跟 " needed by",
    # 带约束的 "xxx >= 1.0 needed by" 匹配不上 → constraint 恒为空
    _setup(tmp_path, log="nothing provides libfoo needed by main")
    _run(monkeypatch, tmp_path, "--pkg", "pkg")
    reg = json.loads((tmp_path / "dep_registry.json").read_text())
    assert reg["libfoo"]["constraint"] == ""
    assert reg["libfoo"]["status"] == "pending_evaluate"
    assert reg["libfoo"]["required_by"] == "pkg"


def test_main_no_matching_form_registers_full_string(tmp_path, monkeypatch, capsys):
    """No matching 形式:注册 key 是整串(含约束),constraint 为空。"""
    _setup(tmp_path, log="No matching package to install: 'libfoo >= 1.4.0'")
    _run(monkeypatch, tmp_path, "--pkg", "pkg")
    reg = json.loads((tmp_path / "dep_registry.json").read_text())
    assert reg["libfoo >= 1.4.0"]["constraint"] == ""


def test_main_python_prefix_stripped(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, log="nothing provides python3-requests needed by x")
    _run(monkeypatch, tmp_path, "--pkg", "pkg")
    reg = json.loads((tmp_path / "dep_registry.json").read_text())
    assert "requests" in reg
    assert "python3-requests" not in reg


def test_main_skip_toolchain(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, log="nothing provides gcc needed by x")
    _run(monkeypatch, tmp_path, "--pkg", "pkg")
    # main 无条件写 registry(即使为空);toolchain 未注册
    assert json.loads((tmp_path / "dep_registry.json").read_text()) == {}
    assert "skip toolchain: gcc" in capsys.readouterr().out


def test_main_existing_entry_unchanged(tmp_path, monkeypatch, capsys):
    """已登记条目无 constraint 补充来源(提取恒为空)→ 原样保留。"""
    _setup(tmp_path, log="nothing provides libfoo needed by x",
           registry={"libfoo": {"url": "", "constraint": ">= 1.0", "status": "pending_evaluate"}})
    _run(monkeypatch, tmp_path, "--pkg", "pkg")
    reg = json.loads((tmp_path / "dep_registry.json").read_text())
    assert reg["libfoo"]["constraint"] == ">= 1.0"


def test_main_ros_upstream_tier2(tmp_path, monkeypatch, capsys):
    """ros 缺失依赖:projects 查无但 upstream 查有 → 注册带 url + lang=ros。"""
    _setup(tmp_path, log="nothing provides ros-humble-ament-cmake needed by x")
    import analyze_ros_deps
    monkeypatch.setattr(analyze_ros_deps, "load_projects",
                        lambda d: {"rclcpp": ("url", "active", "1.0")})
    monkeypatch.setattr(analyze_ros_deps, "load_upstream", lambda d: {
        "ament-cmake": ("https://github.com/ros2/ament_cmake", "humble", "active", "1.3.0"),
    })

    _run(monkeypatch, tmp_path, "--pkg", "pkg")
    reg = json.loads((tmp_path / "dep_registry.json").read_text())
    entry = reg["ros-humble-ament-cmake"]
    assert entry["lang"] == "ros"
    assert entry["url"] == "https://github.com/ros2/ament_cmake"


def test_main_ros_unknown_exits_3(tmp_path, monkeypatch, capsys):
    """ros 幻觉依赖名 → 拒绝全部注册,退出码 3。"""
    _setup(tmp_path, log=("nothing provides ros-humble-ghost-pkg needed by x\n"
                          "nothing provides libfoo needed by x"))
    import analyze_ros_deps
    monkeypatch.setattr(analyze_ros_deps, "load_projects",
                        lambda d: {"rclcpp": ("url", "active", "1.0")})
    monkeypatch.setattr(analyze_ros_deps, "load_upstream", lambda d: {})

    with pytest.raises(SystemExit) as e:
        _run(monkeypatch, tmp_path, "--pkg", "pkg")
    assert e.value.code == 3
    assert "拒绝注册" in capsys.readouterr().err
    # 部分注册被整体拒绝
    assert not (tmp_path / "dep_registry.json").exists()
