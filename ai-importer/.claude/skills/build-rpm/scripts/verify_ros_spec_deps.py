#!/usr/bin/env python3
"""ROS spec 依赖名门禁（spec-rules-ros.md §6 反幻觉铁律的机械校验）。

spec 中所有 `ros-<distro>-*` 的 BuildRequires/Requires 必须能在
ros-projects.list 中找到（`_`/`-` 归一化后），查不到即幻觉依赖名，
打回修 spec——不得让这种 spec 进入 COPR 提交流程。

用法：
  python3 verify_ros_spec_deps.py <spec_path> [--session-dir .] [--ros-distro humble]

退出码：
  0  通过（或非 ROS 场景无法校验，降级放行并告警）
  1  存在幻觉依赖名（输出违规清单与最近匹配建议）
  2  参数/环境错误
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_ros_deps import load_projects  # noqa: E402
from ros_dep_guard import (  # noqa: E402
    format_invalid_report,
    invalid_ros_deps,
    scan_spec_ros_deps,
)


def _session_distro(session_dir: str) -> str:
    if not session_dir:
        return ""
    p = Path(session_dir) / "session.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("ros_distro", "") or ""
        except Exception:
            pass
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="ROS spec 依赖名门禁（反幻觉机械校验）")
    parser.add_argument("spec_path", help="待校验的 spec 文件")
    parser.add_argument("--session-dir", default="", help="session 目录（取 ros_distro）")
    parser.add_argument("--ros-distro", default="", help="ROS 发行版（默认 session.json / humble）")
    args = parser.parse_args()

    spec_path = Path(args.spec_path)
    if not spec_path.exists():
        print(f"[verify_ros_spec_deps] ERROR: spec 不存在: {spec_path}", file=sys.stderr)
        return 2

    ros_distro = args.ros_distro or _session_distro(args.session_dir) or "humble"
    projects = load_projects(ros_distro)
    if not projects:
        # 清单缺失时无法校验，降级放行（不阻塞非 ROS/异常环境）
        print(f"[verify_ros_spec_deps] WARN: ros-projects.list 缺失（distro={ros_distro}），跳过校验",
              file=sys.stderr)
        return 0

    names = scan_spec_ros_deps(spec_path.read_text(encoding="utf-8", errors="replace"),
                               ros_distro)
    if not names:
        print(f"[verify_ros_spec_deps] OK: spec 无 ros-{ros_distro}-* 依赖声明")
        return 0

    bad = invalid_ros_deps(names, projects)
    if bad:
        print(f"[verify_ros_spec_deps] FAIL: {format_invalid_report(bad, ros_distro)}",
              file=sys.stderr)
        return 1

    print(f"[verify_ros_spec_deps] OK: {len(names)} 个 ros-{ros_distro}-* 依赖名均在清单中")
    return 0


if __name__ == "__main__":
    sys.exit(main())
