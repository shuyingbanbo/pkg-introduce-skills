#!/usr/bin/env python3
"""
ROS 包 RPM 依赖分析脚本

解析源码目录里的 package.xml（移植 ros-porting-tools get-package-xml.py 的过滤逻辑），
把依赖三类分拣：

  | 类别          | 判定                                 | 处理                              |
  |---------------|--------------------------------------|-----------------------------------|
  | ROS 包依赖    | 依赖名在 ros-projects.list 中        | ros_deps（explicit 缺口 / deep 注册）|
  | 外部依赖      | pkg.remap 查表（deb→rpm）            | 直接进 BuildRequires             |
  | 系统依赖      | openEuler 基础源已有（--check-rpm）  | rpm_batch_lookup 查询             |

输出契约对齐现有 analyze_<lang>_deps.py：JSON 含 build_requires 数组等字段，
供 pre_check_deps.py 以标准方式汇总成 pre_check.json。本脚本的 parse_package_xml()
与 classify_deps() 为公共函数，ros_prep.py 复用（sys.path 导入）。

用法：
  python3 analyze_ros_deps.py <source_dir>
  python3 analyze_ros_deps.py <source_dir> --ros-distro humble --check-rpm -o result.json
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from xml.etree import ElementTree

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data" / "ros"

# package.xml 依赖标签 → 类别（test_depend 构建时禁用测试，只记录不产 BuildRequires）
ROS_DEP_TAGS = (
    "depend", "build_depend", "build_export_depend",
    "exec_depend", "run_depend",
    "buildtool_depend", "buildtool_export_depend",
)
TEST_DEP_TAGS = ("test_depend",)

# 移植 get-package-xml.py 的条件过滤：ROS 1 条件项 / 过时 python2 条件
SKIP_CONDITIONS = {
    "$ROS_VERSION == 1",
    "$ROS_PYTHON_VERSION == 2",
}


def _skip_condition(dep_elem) -> bool:
    """判断依赖元素是否应跳过（ROS 1 / python2 条件项）。"""
    if dep_elem.get("ROS_VERSION") == "1":
        return True
    cond = dep_elem.get("condition", "")
    if cond in SKIP_CONDITIONS or cond in ("$ROS_VERSION == 1", "$ROS_PYTHON_VERSION == 2"):
        return True
    return False


def parse_package_xml(source_dir: str, pkgname: str = "") -> dict:
    """
    解析 <source_dir> 下主包的 package.xml。

    定位策略：优先 <source_dir>/<pkgname>/package.xml（ament 布局，仓库含多包），
    其次 <source_dir>/package.xml（单包仓库）；都找不到时扫描一级子目录。

    返回：
      found / name / version / license / deps（依赖名列表，已过滤）/ buildtool_deps
    """
    src = Path(source_dir)
    candidates = []
    if pkgname:
        candidates.append(src / pkgname / "package.xml")
    candidates.append(src / "package.xml")
    if src.is_dir():
        candidates += sorted(
            p / "package.xml" for p in src.iterdir() if p.is_dir()
        )
    xml_path = None
    for c in candidates:
        if c.exists():
            xml_path = c
            break
    if xml_path is None:
        return {"found": False, "error": f"package.xml not found under {source_dir}"}

    try:
        root = ElementTree.parse(str(xml_path)).getroot()
    except ElementTree.ParseError as exc:
        return {"found": False, "error": f"package.xml parse error: {exc}"}

    def _tag_text(tag: str) -> str:
        el = root.find(tag)
        if el is not None and el.text:
            return el.text.strip()
        return ""

    deps: list[str] = []
    buildtool_deps: list[str] = []
    for tag in ROS_DEP_TAGS:
        for el in root.findall(tag):
            if _skip_condition(el):
                continue
            if not el.text:
                continue
            name = el.text.strip()
            if not name:
                continue
            if "buildtool" in tag:
                buildtool_deps.append(name)
            else:
                deps.append(name)
    test_deps = []
    for tag in TEST_DEP_TAGS:
        for el in root.findall(tag):
            if _skip_condition(el):
                continue
            if el.text and el.text.strip():
                test_deps.append(el.text.strip())

    # <export><build_type>ament_cmake|ament_python</build_type></export>
    # build_type 不是依赖标签（ROS_DEP_TAGS 不含它），单独提取：
    # ament_python 常被误当成依赖名脑补出 ros-<distro>-ament-python（清单中不存在）
    build_type = ""
    bt = root.find("export/build_type")
    if bt is not None and bt.text:
        build_type = bt.text.strip()

    return {
        "found": True,
        "pkg_xml_path": str(xml_path),
        "name": _tag_text("name"),
        "version": _tag_text("version"),
        "license": _tag_text("license"),
        "deps": sorted(set(deps)),
        "buildtool_deps": sorted(set(buildtool_deps)),
        "test_deps": sorted(set(test_deps)),
        "build_type": build_type,
    }


def load_projects(ros_distro: str = "humble") -> dict:
    """加载 ros-projects.list → {包名: (仓库URL, 维护状态, 版本-发布号)}。"""
    lst = DATA_DIR / ros_distro / "ros-projects.list"
    projects: dict[str, tuple[str, str, str]] = {}
    if not lst.exists():
        return projects
    for line in lst.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        projects[parts[0].strip()] = (parts[1].strip(), parts[2].strip(), parts[3].strip())
    return projects


def load_upstream(ros_distro: str = "humble") -> dict:
    """加载 ros-upstream.list（rosdistro 全量清单）→ {包名: (仓库URL, 分支, 状态, 版本)}。

    第二级地面真值：SIG 清单（load_projects）查不到但本清单查得到的包，
    是"真实存在但 SIG 未移植"的包，允许注册递归构建。
    """
    lst = DATA_DIR / ros_distro / "ros-upstream.list"
    upstream: dict[str, tuple[str, str, str, str]] = {}
    if not lst.exists():
        return upstream
    for line in lst.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        upstream[parts[0].strip()] = (parts[1].strip(), parts[2].strip(),
                                      parts[3].strip(), parts[4].strip())
    return upstream


def load_remap() -> dict:
    """加载 pkg.remap → {deb名: rpm名}。"""
    remap = {}
    p = DATA_DIR / "global_config" / "pkg.remap"
    if not p.exists():
        return remap
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            remap[parts[0]] = parts[1]
    return remap


def classify_deps(dep_names: list[str], projects: dict, remap: dict,
                  base_pkgs: set[str] | None = None,
                  upstream: dict | None = None) -> dict:
    """
    四类分拣：
      ros_deps          — 依赖名在 ros-projects.list（SIG 源已有）→ 待注册/缺口
      ros_deps_upstream — 仅在 ros-upstream.list（SIG 未移植、rosdistro 真实存在）
                          → 可注册递归构建
      build_requires    — pkg.remap 查表命中 → 直接 BuildRequires
      unresolved        — 都没命中 → 视为系统依赖，留给 --check-rpm 实证
    base_pkgs：openEuler 基础源已有包名集合（--check-rpm 查询结果），命中的过滤掉。
    """
    upstream = upstream or {}
    ros_deps: list[str] = []
    ros_deps_upstream: list[str] = []
    build_requires: list[str] = []
    unresolved: list[str] = []
    for name in sorted(set(dep_names)):
        # package.xml 用下划线命名、清单用连字符：归一化后查清单，
        # 命中时以清单规范名（连字符）输出（否则 rosidl_default_generators 这类
        # 名字会全部漏进 unresolved）
        norm = name.replace("_", "-")
        if norm in projects:
            ros_deps.append(norm)
        elif norm in upstream:
            ros_deps_upstream.append(norm)
        elif name in remap:
            build_requires.append(remap[name])
        elif norm in remap:
            build_requires.append(remap[norm])
        elif base_pkgs and name in base_pkgs:
            pass  # 系统依赖，基础源已有，不需要 BuildRequires
        else:
            unresolved.append(name)
    return {
        "ros_deps": sorted(set(ros_deps)),
        "ros_deps_upstream": sorted(set(ros_deps_upstream)),
        "build_requires": sorted(set(build_requires)),
        "unresolved": sorted(set(unresolved)),
    }


def _rpm_batch_lookup(names: list[str]):
    """对 unresolved 依赖做批量 RPM 查询（对齐 analyze_* 的 --check-rpm 契约）。"""
    if not names:
        return None
    try:
        from rpm_batch_lookup import run_batch_lookup
        return run_batch_lookup(names)
    except Exception as exc:  # 容器不可用等，容错返回 None
        print(f"[WARN] rpm batch lookup failed: {exc}", file=sys.stderr)
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="ROS 包 RPM 依赖分析")
    parser.add_argument("source_dir", help="ROS 包源码目录（含 package.xml）")
    parser.add_argument("--pkgname", default="", help="包名（定位 <src>/<pkgname>/package.xml）")
    parser.add_argument("--ros-distro", default=os.environ.get("ROS_DISTRO", "humble"),
                        help="ROS 发行版（默认 env ROS_DISTRO / humble）")
    parser.add_argument("--check-rpm", action="store_true", help="查询 unresolved 依赖的 RPM 可用性")
    parser.add_argument("-o", "--output", default="", help="结果输出到 JSON 文件")
    args = parser.parse_args()

    source_dir = os.path.abspath(args.source_dir)
    if not os.path.isdir(source_dir):
        print(f"[ERROR] 目录不存在: {source_dir}", file=sys.stderr)
        return 1

    parsed = parse_package_xml(source_dir, args.pkgname)
    if not parsed["found"]:
        print(f"[WARN] {parsed.get('error', 'package.xml not found')}", file=sys.stderr)
        parsed["deps"], parsed["buildtool_deps"] = [], []

    projects = load_projects(args.ros_distro)
    upstream = load_upstream(args.ros_distro)
    remap = load_remap()

    # buildtool 依赖（ament_cmake 等）官方源已有（ROS SIG repo），直接进 BuildRequires 候选
    all_dep_names = parsed["deps"] + parsed["buildtool_deps"]
    classified = classify_deps(all_dep_names, projects, remap, upstream=upstream)

    # <build_type> 不是依赖标签，但决定构建工具链：
    #   ament_cmake  → 构建必须依赖 ament-cmake（清单内），补入 ros_deps
    #   ament_python → 纯 setuptools 构建，不产生任何 ros-<distro>-* 依赖
    #     （历史教训：agent 曾凭 build_type 脑补出不存在的 ros-humble-ament-python）
    build_type = parsed.get("build_type", "")
    if build_type == "ament_cmake" and "ament-cmake" in projects \
            and "ament-cmake" not in classified["ros_deps"]:
        classified["ros_deps"] = sorted(classified["ros_deps"] + ["ament-cmake"])

    # 外部/系统依赖的 RPM 实证
    rpm_check = None
    if args.check_rpm and classified["unresolved"]:
        rpm_check = _rpm_batch_lookup(classified["unresolved"])

    result = {
        "pkgname": parsed.get("name") or args.pkgname,
        "version": parsed.get("version", ""),
        "license": parsed.get("license", ""),
        "ros_distro": args.ros_distro,
        "build_type": build_type,
        "ros_deps": classified["ros_deps"],
        "ros_deps_upstream": classified["ros_deps_upstream"],
        "build_requires": classified["build_requires"],
        "unresolved": classified["unresolved"],
        "test_deps": parsed["test_deps"],
        "rpm_check": rpm_check,
        "package_xml": {
            "found": parsed["found"],
            "path": parsed.get("pkg_xml_path", ""),
        },
    }

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[INFO] 结果已保存: {args.output}")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
