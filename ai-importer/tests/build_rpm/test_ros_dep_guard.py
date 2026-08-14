"""ros_dep_guard.py — ROS 依赖名防幻觉共享校验(两级地面真值)。"""

from __future__ import annotations

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

rg = load_module("ros_dep_guard", SCRIPT_DIRS["build_rpm"] / "ros_dep_guard.py")

# ros_dep_guard 顶层已 import analyze_ros_deps,这里复用其真实清单加载器
from analyze_ros_deps import load_projects, load_upstream  # noqa: E402

FAKE_PROJECTS = {"ament-cmake": ("url", "maintained", "1.3.3-1"), "foo": ("url2", "developed", "0.1-2")}
FAKE_UPSTREAM = {"upstream-only": ("url", "humble", "maintained", "0.0.1-1")}


# ─────────────────────────────────────────────
# split_ros_name
# ─────────────────────────────────────────────

@pytest.mark.parametrize("pkg,expected", [
    ("ros-humble-ament-cmake", ("humble", "ament-cmake")),
    ("ros-humble-ament_cmake", ("humble", "ament_cmake")),
    ("ros-jazzy-foo", ("jazzy", "foo")),
    ("ros-123-foo", ("123", "foo")),       # distro 允许数字
    ("ros-humble-a-b", ("humble", "a-b")),
    ("ros-humble-foo.bar", ("humble", "foo.bar")),
    (" ros-humble-foo ", ("humble", "foo")),  # 首尾空白
    ("ament-cmake", None),                 # 非 ros-* 前缀
    ("ros-Humble-foo", None),              # distro 必须小写
    ("ros-humble-", None),                 # 缺 name 段
    ("", None),
    ("ros-humble-foo!", None),             # 非法字符
    ("ros--foo", None),
])
def test_split_ros_name(pkg, expected):
    assert rg.split_ros_name(pkg) == expected


# ─────────────────────────────────────────────
# norm_ros_name / lookup
# ─────────────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("ament_cmake", "ament-cmake"),
    ("a_b_c", "a-b-c"),
    ("already-hyphen", "already-hyphen"),
    ("", ""),
    ("_x", "-x"),
])
def test_norm_ros_name(name, expected):
    assert rg.norm_ros_name(name) == expected


@pytest.mark.parametrize("name,expected", [
    ("ament-cmake", "ament-cmake"),
    ("ament_cmake", "ament-cmake"),  # 下划线归一化后命中
    ("foo", "foo"),
    ("missing", None),
    ("", None),
])
def test_lookup_ros_dep(name, expected):
    assert rg.lookup_ros_dep(name, FAKE_PROJECTS) == expected


@pytest.mark.parametrize("name,expected", [
    ("upstream-only", "upstream-only"),
    ("upstream_only", "upstream-only"),
    ("ament-cmake", None),  # 只在 SIG 清单,不在 upstream 清单
    ("", None),
])
def test_lookup_upstream_dep(name, expected):
    assert rg.lookup_upstream_dep(name, FAKE_UPSTREAM) == expected


# ─────────────────────────────────────────────
# suggest_ros_names
# ─────────────────────────────────────────────

def test_suggest_ros_names_exact():
    projects = {"ament-cmake-python": 1, "ament-cmake": 1, "ament-lint": 1}
    assert rg.suggest_ros_names("ament-python", projects) == ["ament-cmake-python", "ament-lint", "ament-cmake"]


def test_suggest_ros_names_limit():
    projects = {"ament-a": 1, "ament-b": 1, "ament-c": 1, "ament-d": 1, "ament-e": 1}
    assert rg.suggest_ros_names("ament", projects, n=4) == ["ament-e", "ament-d", "ament-c", "ament-b"]


def test_suggest_ros_names_none_close():
    assert rg.suggest_ros_names("zzz-qqq", {"aaa": 1, "bbb": 1}) == []


def test_suggest_ros_names_underscore_normalized():
    assert rg.suggest_ros_names("ament_python", {"ament-cmake-python": 1}) == ["ament-cmake-python"]


# ─────────────────────────────────────────────
# scan_spec_ros_deps
# ─────────────────────────────────────────────

SPEC_TEXT = """Name: testpkg
BuildRequires: ros-humble-ament-cmake
BuildRequires:  ros-humble-rclcpp , ros-humble-std-msgs
Requires: ros-humble-rosidl-default-runtime
BuildRequires: ros-humble-rclcpp
BuildRequires: ros-rolling-something
BuildRequires: ros-humble-foo>=1.0
BuildRequires: gcc
"""


def test_scan_spec_ros_deps_main():
    names = rg.scan_spec_ros_deps(SPEC_TEXT, "humble")
    # 去重 + 排序;其他 distro 与无前缀行不报;版本约束截断
    assert names == ["ament-cmake", "foo", "rclcpp", "rosidl-default-runtime", "std-msgs"]


def test_scan_spec_ros_deps_macros():
    spec = "BuildRequires: ros-%{ros_distro}-rclcpp\nBuildRequires: ros-%{?ros_distro}-std-msgs\n"
    assert rg.scan_spec_ros_deps(spec, "humble") == ["rclcpp", "std-msgs"]


@pytest.mark.parametrize("spec_text,expected", [
    ("", []),
    ("BuildRequires: gcc\n", []),
    ("Requires: ros-humble-foo >= 1.0\n", ["foo"]),   # 空格分隔的约束
    ("BuildRequires: ros-humble-foo>=1.0\n", ["foo"]),
    ("BuildRequires: ros-humble-foo<=2.0\n", ["foo"]),
    ("BuildRequires: ros-humble-foo!=2.0\n", ["foo"]),
    ("BuildRequires: ros-rolling-other\n", []),       # 其他 distro 不误报
    ("buildrequires: ros-humble-lowercase\n", ["lowercase"]),  # 大小写不敏感
    ("BuildRequires: ros-humble-foo\nBuildRequires: ros-humble-FOO\n", ["FOO", "foo"]),
    ("BuildRequires: ros-humble-foo\nRequires: ros-humble-foo\n", ["foo"]),  # 去重
])
def test_scan_spec_ros_deps_edge(spec_text, expected):
    assert rg.scan_spec_ros_deps(spec_text, "humble") == expected


# ─────────────────────────────────────────────
# invalid_ros_deps
# ─────────────────────────────────────────────

def test_invalid_ros_deps_all_valid():
    bad = rg.invalid_ros_deps(["ament-cmake", "ament_cmake", "foo"], FAKE_PROJECTS, FAKE_UPSTREAM)
    assert bad == {}


def test_invalid_ros_deps_upstream_only_allowed():
    # 上游清单命中 = 真实存在但 SIG 未移植,不算幻觉
    bad = rg.invalid_ros_deps(["upstream-only", "upstream_only"], FAKE_PROJECTS, FAKE_UPSTREAM)
    assert bad == {}


def test_invalid_ros_deps_hallucinated_with_suggestions():
    bad = rg.invalid_ros_deps(["ament-cmake", "ament-python", "no-such-thing"], FAKE_PROJECTS, FAKE_UPSTREAM)
    assert set(bad) == {"ament-python", "no-such-thing"}
    assert bad["ament-python"] == ["ament-cmake"]  # difflib 阈值 0.5 恰好命中
    assert bad["no-such-thing"] == []


def test_invalid_ros_deps_upstream_none():
    # 未提供 upstream 清单时,仅 SIG 清单未命中的都算幻觉
    bad = rg.invalid_ros_deps(["upstream-only"], FAKE_PROJECTS, None)
    assert bad == {"upstream-only": []}


def test_invalid_ros_deps_empty():
    assert rg.invalid_ros_deps([], FAKE_PROJECTS, FAKE_UPSTREAM) == {}


def test_invalid_ros_deps_real_data():
    projects = load_projects("humble")
    upstream = load_upstream("humble")
    bad = rg.invalid_ros_deps(["ament-cmake", "totally-made-up-name-xyz"], projects, upstream)
    assert list(bad) == ["totally-made-up-name-xyz"]


def test_lookup_with_real_projects():
    projects = load_projects("humble")
    assert projects  # 真实清单非空
    assert rg.lookup_ros_dep("ament-cmake", projects) == "ament-cmake"
    assert rg.lookup_ros_dep("ament_cmake", projects) == "ament-cmake"
    assert rg.lookup_ros_dep("definitely-not-a-ros-pkg-xyz", projects) is None


# ─────────────────────────────────────────────
# format_invalid_report
# ─────────────────────────────────────────────

def test_format_invalid_report():
    bad = {"fake-name": ["real-name", "other"], "totally_fake": []}
    report = rg.format_invalid_report(bad, "humble")
    assert report.startswith("以下 ros-humble-* 依赖名")
    assert "幻觉依赖名" in report
    assert "ros-humble-fake-name（最近匹配: real-name, other）" in report
    assert "ros-humble-totally-fake" in report
    assert "不得注册递归构建" in report


def test_format_invalid_report_underscore_normalized():
    report = rg.format_invalid_report({"fake_name": []}, "humble")
    assert "ros-humble-fake-name" in report
    assert "（最近匹配" not in report  # 无建议时无提示段
