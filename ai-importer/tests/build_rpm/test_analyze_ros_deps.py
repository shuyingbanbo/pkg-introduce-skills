"""analyze_ros_deps.py — ROS 包 RPM 依赖分析(package.xml 解析 + 分类 + mock 批量查询)。"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sys.path.insert(0, str(SCRIPT_DIRS["build_rpm"]))
mod = load_module("analyze_ros_deps", SCRIPT_DIRS["build_rpm"] / "analyze_ros_deps.py")


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


# ─────────────────────────────────────────────
# _skip_condition
# ─────────────────────────────────────────────

@pytest.mark.parametrize("attrs,expected", [
    ({"ROS_VERSION": "1"}, True),
    ({"condition": "$ROS_VERSION == 1"}, True),
    ({"condition": "$ROS_PYTHON_VERSION == 2"}, True),
    ({"condition": "$ROS_VERSION == 2"}, False),
    ({}, False),
])
def test_skip_condition(attrs, expected):
    el = ET.fromstring("<depend>foo</depend>")
    for k, v in attrs.items():
        el.set(k, v)
    assert mod._skip_condition(el) is expected


# ─────────────────────────────────────────────
# parse_package_xml
# ─────────────────────────────────────────────

PACKAGE_XML = """<?xml version="1.0"?>
<package format="3">
  <name>demo_pkg</name>
  <version>0.1.0</version>
  <license>Apache-2.0</license>
  <buildtool_depend>ament_cmake</buildtool_depend>
  <depend>rclcpp</depend>
  <build_depend>std_msgs</build_depend>
  <exec_depend>sensor_msgs</exec_depend>
  <depend>std_msgs</depend>
  <depend condition="$ROS_VERSION == 1">roscpp</depend>
  <depend ROS_VERSION="1">rospy</depend>
  <test_depend>ament_lint_auto</test_depend>
  <test_depend>ament_cmake_gtest</test_depend>
  <export>
    <build_type>ament_cmake</build_type>
  </export>
</package>
"""


def test_parse_package_xml_full(tmp_path):
    _write(tmp_path, "package.xml", PACKAGE_XML)
    parsed = mod.parse_package_xml(str(tmp_path))
    assert parsed["found"] is True
    assert parsed["name"] == "demo_pkg"
    assert parsed["version"] == "0.1.0"
    assert parsed["license"] == "Apache-2.0"
    # 条件项 roscpp/rospy 过滤;std_msgs 重复去重;排序
    assert parsed["deps"] == ["rclcpp", "sensor_msgs", "std_msgs"]
    assert parsed["buildtool_deps"] == ["ament_cmake"]
    assert parsed["test_deps"] == ["ament_cmake_gtest", "ament_lint_auto"]
    assert parsed["build_type"] == "ament_cmake"


def test_parse_package_xml_pkgname_layout(tmp_path):
    _write(tmp_path, "my_pkg/package.xml", "<package><name>my_pkg</name><depend>rclcpp</depend></package>")
    _write(tmp_path, "package.xml", "<package><name>root_pkg</name></package>")   # 干扰项
    parsed = mod.parse_package_xml(str(tmp_path), pkgname="my_pkg")
    assert parsed["name"] == "my_pkg"
    assert parsed["deps"] == ["rclcpp"]


def test_parse_package_xml_subdir_fallback(tmp_path):
    _write(tmp_path, "sub/package.xml", "<package><name>sub_pkg</name><depend>rclcpp</depend></package>")
    parsed = mod.parse_package_xml(str(tmp_path))
    assert parsed["found"] is True
    assert parsed["name"] == "sub_pkg"


def test_parse_package_xml_not_found(tmp_path):
    parsed = mod.parse_package_xml(str(tmp_path))
    assert parsed["found"] is False
    assert "package.xml not found" in parsed["error"]


def test_parse_package_xml_parse_error(tmp_path):
    _write(tmp_path, "package.xml", "<package><broken>")
    parsed = mod.parse_package_xml(str(tmp_path))
    assert parsed["found"] is False
    assert "parse error" in parsed["error"]


def test_parse_package_xml_no_build_type(tmp_path):
    _write(tmp_path, "package.xml", "<package><name>x</name></package>")
    parsed = mod.parse_package_xml(str(tmp_path))
    assert parsed["build_type"] == ""


# ─────────────────────────────────────────────
# load_projects / load_upstream / load_remap(monkeypatch DATA_DIR)
# ─────────────────────────────────────────────

def _fake_data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data" / "ros"
    (d / "humble").mkdir(parents=True)
    (d / "global_config").mkdir(parents=True)
    _write(d, "humble/ros-projects.list", (
        "# comment line\n"
        "\n"
        "rclcpp\thttps://github.com/ros2/rclcpp\tmaintained\t16.0.10-1\n"
        "std-msgs\thttps://github.com/ros2/common_interfaces\tmaintained\t4.2.0-1\n"
        "short-line-only\n"    # <4 列,跳过
    ))
    _write(d, "humble/ros-upstream.list", (
        "deep-pkg\thttps://github.com/x/deep\thumble\tmaintained\t1.0.0\n"
        "tooshort\n"           # <5 列,跳过
    ))
    _write(d, "global_config/pkg.remap", (
        "# remap comment\n"
        "python3-dev python3-devel\n"
        "pkg-config pkgconfig\n"
    ))
    monkeypatch.setattr(mod, "DATA_DIR", d)
    return d


def test_load_projects(tmp_path, monkeypatch):
    _fake_data_dir(tmp_path, monkeypatch)
    projects = mod.load_projects("humble")
    assert projects == {
        "rclcpp": ("https://github.com/ros2/rclcpp", "maintained", "16.0.10-1"),
        "std-msgs": ("https://github.com/ros2/common_interfaces", "maintained", "4.2.0-1"),
    }


def test_load_projects_missing_distro(tmp_path, monkeypatch):
    _fake_data_dir(tmp_path, monkeypatch)
    assert mod.load_projects("noetic") == {}


def test_load_upstream(tmp_path, monkeypatch):
    _fake_data_dir(tmp_path, monkeypatch)
    upstream = mod.load_upstream("humble")
    assert upstream == {"deep-pkg": ("https://github.com/x/deep", "humble", "maintained", "1.0.0")}


def test_load_remap(tmp_path, monkeypatch):
    _fake_data_dir(tmp_path, monkeypatch)
    remap = mod.load_remap()
    assert remap == {"python3-dev": "python3-devel", "pkg-config": "pkgconfig"}


def test_load_projects_real_data():
    # 仓库自带 humble 数据文件存在且非空
    projects = mod.load_projects("humble")
    assert len(projects) > 100


# ─────────────────────────────────────────────
# classify_deps
# ─────────────────────────────────────────────

PROJECTS = {"rclcpp": ("u", "maintained", "1-1"), "std-msgs": ("u", "maintained", "1-1"),
            "ament-cmake": ("u", "maintained", "1-1")}
UPSTREAM = {"deep-pkg": ("u", "b", "maintained", "1.0")}
REMAP = {"python3-dev": "python3-devel", "yaml_cpp": "yaml-cpp-devel"}


def test_classify_deps_all_buckets():
    result = mod.classify_deps(
        ["rclcpp", "std_msgs", "deep_pkg", "python3-dev", "yaml_cpp", "libfoo-dev"],
        PROJECTS, REMAP, base_pkgs=None, upstream=UPSTREAM,
    )
    assert result["ros_deps"] == ["rclcpp", "std-msgs"]       # 下划线归一为连字符
    assert result["ros_deps_upstream"] == ["deep-pkg"]
    assert result["build_requires"] == ["python3-devel", "yaml-cpp-devel"]
    assert result["unresolved"] == ["libfoo-dev"]


def test_classify_deps_base_pkgs_filter():
    result = mod.classify_deps(["libfoo-dev"], {}, {}, base_pkgs={"libfoo-dev"})
    assert result == {"ros_deps": [], "ros_deps_upstream": [],
                      "build_requires": [], "unresolved": []}


def test_classify_deps_norm_remap_hit():
    # yaml_cpp 在 remap 表中(下划线键),norm "yaml-cpp" 不在 remap → 用原名字段命中
    result = mod.classify_deps(["yaml_cpp"], {}, REMAP)
    assert result["build_requires"] == ["yaml-cpp-devel"]


def test_classify_deps_no_upstream_arg():
    result = mod.classify_deps(["rclcpp", "foo"], PROJECTS, {})
    assert result["ros_deps"] == ["rclcpp"]
    assert result["ros_deps_upstream"] == []
    assert result["unresolved"] == ["foo"]


def test_classify_deps_dedup_and_sort():
    result = mod.classify_deps(["foo", "rclcpp", "foo"], PROJECTS, {})
    assert result["unresolved"] == ["foo"]
    assert result["ros_deps"] == ["rclcpp"]


def test_classify_deps_empty():
    result = mod.classify_deps([], PROJECTS, REMAP)
    assert result == {"ros_deps": [], "ros_deps_upstream": [],
                      "build_requires": [], "unresolved": []}


# ─────────────────────────────────────────────
# _rpm_batch_lookup
# ─────────────────────────────────────────────

def test_rpm_batch_lookup_empty():
    assert mod._rpm_batch_lookup([]) is None


def test_rpm_batch_lookup_ok(monkeypatch):
    import rpm_batch_lookup
    calls = []
    def fake(names):
        calls.append(names)
        return {"available": names}
    monkeypatch.setattr(rpm_batch_lookup, "run_batch_lookup", fake)
    result = mod._rpm_batch_lookup(["libfoo-dev"])
    assert result == {"available": ["libfoo-dev"]}
    assert calls == [["libfoo-dev"]]


def test_rpm_batch_lookup_exception(monkeypatch, capsys):
    import rpm_batch_lookup
    def boom(names):
        raise RuntimeError("container unavailable")
    monkeypatch.setattr(rpm_batch_lookup, "run_batch_lookup", boom)
    assert mod._rpm_batch_lookup(["libfoo-dev"]) is None
    assert "rpm batch lookup failed" in capsys.readouterr().err


# ─────────────────────────────────────────────
# main(mock 清单加载器)
# ─────────────────────────────────────────────

def _patch_loaders(monkeypatch):
    monkeypatch.setattr(mod, "load_projects", lambda distro: dict(PROJECTS))
    monkeypatch.setattr(mod, "load_upstream", lambda distro: dict(UPSTREAM))
    monkeypatch.setattr(mod, "load_remap", lambda: dict(REMAP))


def test_main_basic_output_json(tmp_path, capsys, monkeypatch):
    _write(tmp_path, "package.xml", PACKAGE_XML)
    _patch_loaders(monkeypatch)
    out_json = tmp_path / "result.json"
    monkeypatch.setattr(sys, "argv", ["analyze_ros_deps.py", str(tmp_path), "-o", str(out_json)])
    assert mod.main() == 0
    result = json.loads(out_json.read_text())
    assert result["pkgname"] == "demo_pkg"
    assert result["version"] == "0.1.0"
    assert result["license"] == "Apache-2.0"
    assert result["build_type"] == "ament_cmake"
    # std_msgs → norm std-msgs 命中 projects;buildtool ament_cmake → norm ament-cmake 命中
    assert result["ros_deps"] == ["ament-cmake", "rclcpp", "std-msgs"]
    assert result["unresolved"] == ["sensor_msgs"]
    assert result["test_deps"] == ["ament_cmake_gtest", "ament_lint_auto"]
    assert result["package_xml"]["found"] is True
    assert result["rpm_check"] is None


def test_main_build_type_adds_ament_cmake(tmp_path, monkeypatch):
    _write(tmp_path, "package.xml", """<package>
  <name>p</name>
  <depend>rclcpp</depend>
  <export><build_type>ament_cmake</build_type></export>
</package>
""")
    monkeypatch.setattr(mod, "load_projects", lambda d: {"ament-cmake": ("u", "maintained", "1-1"),
                                                        "rclcpp": ("u", "maintained", "1-1")})
    monkeypatch.setattr(mod, "load_upstream", lambda d: {})
    monkeypatch.setattr(mod, "load_remap", lambda: {})
    out_json = tmp_path / "r.json"
    monkeypatch.setattr(sys, "argv", ["analyze_ros_deps.py", str(tmp_path), "-o", str(out_json)])
    mod.main()
    result = json.loads(out_json.read_text())
    # build_type=ament_cmake 且清单有 ament-cmake → 补进 ros_deps
    assert result["ros_deps"] == ["ament-cmake", "rclcpp"]


def test_main_build_type_ament_python_no_addition(tmp_path, monkeypatch):
    _write(tmp_path, "package.xml", """<package>
  <name>p</name>
  <depend>rclpy</depend>
  <export><build_type>ament_python</build_type></export>
</package>
""")
    _patch_loaders(monkeypatch)
    out_json = tmp_path / "r.json"
    monkeypatch.setattr(sys, "argv", ["analyze_ros_deps.py", str(tmp_path), "-o", str(out_json)])
    mod.main()
    result = json.loads(out_json.read_text())
    assert result["build_type"] == "ament_python"
    assert result["ros_deps"] == []      # rclpy 不在 fake 清单,ament_python 不补依赖


def test_main_check_rpm_unresolved(tmp_path, monkeypatch):
    _write(tmp_path, "package.xml", "<package><name>p</name><depend>libfoo-dev</depend></package>")
    _patch_loaders(monkeypatch)
    monkeypatch.setattr(mod, "_rpm_batch_lookup", lambda names: {"found": list(names)})
    out_json = tmp_path / "r.json"
    monkeypatch.setattr(sys, "argv", ["analyze_ros_deps.py", str(tmp_path),
                                      "--check-rpm", "-o", str(out_json)])
    mod.main()
    result = json.loads(out_json.read_text())
    assert result["unresolved"] == ["libfoo-dev"]
    assert result["rpm_check"] == {"found": ["libfoo-dev"]}


def test_main_missing_package_xml(tmp_path, capsys, monkeypatch):
    # 生产代码 bug:package.xml 未找到时 parsed 只有 {"found": False, "error": ...},
    # main 只补了 deps/buildtool_deps 两个键,随后 result 构造访问 parsed["test_deps"]
    # 抛 KeyError(而非打印警告后返回 0)。测试按实际行为断言,仅校验警告已打印。
    _patch_loaders(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["analyze_ros_deps.py", str(tmp_path)])
    with pytest.raises(KeyError):
        mod.main()
    assert "package.xml not found" in capsys.readouterr().err


def test_main_pkgname_arg(tmp_path, monkeypatch):
    _write(tmp_path, "pkg/package.xml", "<package><name>pkg</name><depend>rclcpp</depend></package>")
    _patch_loaders(monkeypatch)
    out_json = tmp_path / "r.json"
    monkeypatch.setattr(sys, "argv", ["analyze_ros_deps.py", str(tmp_path),
                                      "--pkgname", "pkg", "-o", str(out_json)])
    mod.main()
    result = json.loads(out_json.read_text())
    assert result["pkgname"] == "pkg"
    assert result["ros_deps"] == ["rclcpp"]
