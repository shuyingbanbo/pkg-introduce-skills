#!/usr/bin/env python3
"""ROS 依赖名防幻觉共享校验（spec-rules-ros.md §6 反幻觉铁律的机械实现）。

判定依据只有一个：`data/ros/<distro>/ros-projects.list`（ROS SIG 移植清单，
即"ROS 世界真实存在的包"的地面真值）。任何 `ros-<distro>-<name>` 形式的
依赖名，`<name>` 归一化（`_`→`-`）后必须命中清单，否则视为幻觉名——
正确动作是修正 spec 中的依赖名，而不是注册递归构建。

供三处复用：
  - verify_ros_spec_deps.py（spec 提交前门禁，CLI）
  - register-dep.py / register-missing-deps.py（依赖注册前硬拒）
"""

import difflib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_ros_deps import load_projects  # noqa: E402

# ros-<distro>-<name>：distro 为字母数字（humble/jazzy/...），name 至少一段
ROS_NAME_RE = re.compile(r"^ros-([a-z0-9]+)-([A-Za-z0-9][A-Za-z0-9_.+]*-?[A-Za-z0-9_.+]*)$")

# spec 行内依赖声明
_REQUIRES_RE = re.compile(r"^\s*(?:Build)?Requires\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
# 版本约束截断（ros-humble-foo>=1.0 这类无空格写法）
_CONSTRAINT_CUT_RE = re.compile(r"[><=!]")


def split_ros_name(pkg: str) -> tuple[str, str] | None:
    """'ros-humble-ament-cmake' → ('humble', 'ament-cmake')；非 ros-* 名返回 None。"""
    m = ROS_NAME_RE.match(pkg.strip())
    if not m:
        return None
    return m.group(1), m.group(2)


def norm_ros_name(name: str) -> str:
    """package.xml 用下划线、ros-projects.list 用连字符，统一为连字符。"""
    return name.replace("_", "-")


def lookup_ros_dep(name: str, projects: dict) -> str | None:
    """归一化后命中清单返回规范名（连字符形式），否则 None。"""
    key = norm_ros_name(name)
    return key if key in projects else None


def suggest_ros_names(name: str, projects: dict, n: int = 3) -> list[str]:
    """给幻觉名找清单里的最近匹配，供报错提示（ament-python → ament-cmake-python）。"""
    return difflib.get_close_matches(norm_ros_name(name), list(projects), n=n, cutoff=0.5)


def scan_spec_ros_deps(spec_text: str, ros_distro: str) -> list[str]:
    """从 spec 文本提取所有 ros-<distro>-* 的 BuildRequires/Requires 目标名（去重）。

    只匹配当前 distro 的前缀（`%{ros_distro}` / `%{?ros_distro}` 宏先展开），
    其他 distro 的条件化段落不误报。
    """
    expanded = spec_text.replace("%{?ros_distro}", ros_distro).replace("%{ros_distro}", ros_distro)
    names: list[str] = []
    prefix = f"ros-{ros_distro}-"
    for m in _REQUIRES_RE.finditer(expanded):
        for tok in re.split(r"[\s,]+", m.group(1)):
            if not tok.startswith(prefix):
                continue
            dep = tok[len(prefix):]
            dep = _CONSTRAINT_CUT_RE.split(dep, 1)[0].strip()
            if dep:
                names.append(dep)
    return sorted(set(names))


def invalid_ros_deps(names: list[str], projects: dict) -> dict[str, list[str]]:
    """{幻觉名: [建议...]}；全部合法时为空 dict。"""
    bad: dict[str, list[str]] = {}
    for name in names:
        if lookup_ros_dep(name, projects) is None:
            bad[name] = suggest_ros_names(name, projects)
    return bad


def format_invalid_report(bad: dict[str, list[str]], ros_distro: str) -> str:
    lines = [
        f"以下 ros-{ros_distro}-* 依赖名在 ros-projects.list 中不存在（幻觉依赖名）:",
    ]
    for name, sugg in bad.items():
        hint = f"（最近匹配: {', '.join(sugg)}）" if sugg else ""
        lines.append(f"  - ros-{ros_distro}-{norm_ros_name(name)}{hint}")
    lines.append("正确动作：修正 spec 中的依赖名为清单中真实存在的包名；"
                 "不得注册递归构建（造不出清单外的 ROS 包）。")
    return "\n".join(lines)
