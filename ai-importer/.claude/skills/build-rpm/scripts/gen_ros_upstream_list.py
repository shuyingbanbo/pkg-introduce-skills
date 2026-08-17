#!/usr/bin/env python3
"""从 rosdistro 官方 distribution.yaml 生成 ROS 全量上游清单 ros-upstream.list。

用途：ros-projects.list（SIG 清单）只覆盖 SIG 源已移植的包；本清单覆盖 rosdistro
release 全量（humble 约 2300+ 包），作为"ROS 世界真实存在"的第二级地面真值——
SIG 清单查不到但本清单查得到的包，视为"真实存在但 SIG 未移植"，允许注册递归构建。

输出格式（tab 分隔，5 列）：
  包名(连字符) \t source仓库URL(纯地址) \t source分支 \t 维护状态 \t release版本(含发布号)

与 ros-projects.list 的差异：
  - URL 是纯仓库地址，不带 /tree/<branch> 后缀（分支独立成列，避免 git clone 失败）
  - 无 release 段的包版本留空（未 release，只能按 source 分支构建）

用法：
  python3 gen_ros_upstream_list.py                      # humble，联网生成
  python3 gen_ros_upstream_list.py --ros-distro jazzy
  python3 gen_ros_upstream_list.py --input distribution.yaml  # 用本地文件
"""

import argparse
import sys
import urllib.request
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data" / "ros"

DISTRO_YAML_URL = "https://raw.githubusercontent.com/ros/rosdistro/master/{distro}/distribution.yaml"


def load_distribution(distro: str, input_path: str = "") -> dict:
    if input_path:
        with open(input_path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    url = DISTRO_YAML_URL.format(distro=distro)
    with urllib.request.urlopen(url, timeout=60) as resp:
        return yaml.safe_load(resp.read().decode("utf-8"))


def gen_rows(dist: dict) -> list[tuple[str, str, str, str, str]]:
    """distribution.yaml → [(name, url, branch, status, version), ...]（按包名排序）。"""
    rows = []
    for _repo_name, repo in (dist.get("repositories") or {}).items():
        release = repo.get("release") or {}
        source = repo.get("source") or {}
        doc = repo.get("doc") or {}
        url = source.get("url") or doc.get("url") or ""
        branch = source.get("version") or doc.get("version") or ""
        status = repo.get("status") or ""
        version = release.get("version") or ""
        packages = release.get("packages")
        if not packages:
            # 无 release 段（或 release 未列 packages）：仓库即单包，用仓库名
            packages = [_repo_name]
            if not release:
                version = ""
        for pkg in packages:
            # 与 ros-projects.list / ros_dep_guard 的归一化约定一致：连字符
            rows.append((pkg.replace("_", "-"), url, branch, status, version))
    rows.sort()
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 ros-upstream.list（rosdistro 全量清单）")
    parser.add_argument("--ros-distro", default="humble")
    parser.add_argument("--input", default="", help="本地 distribution.yaml（默认联网拉取）")
    parser.add_argument("-o", "--output", default="",
                        help="输出路径（默认 data/ros/<distro>/ros-upstream.list）")
    args = parser.parse_args()

    dist = load_distribution(args.ros_distro, args.input)
    rows = gen_rows(dist)

    out = Path(args.output) if args.output else DATA_DIR / args.ros_distro / "ros-upstream.list"
    out.parent.mkdir(parents=True, exist_ok=True)
    header = ("# ROS upstream 全量清单（由 gen_ros_upstream_list.py 从 rosdistro "
              f"{args.ros_distro}/distribution.yaml 生成，请勿手改；重新生成跑脚本即可）\n"
              "# 包名\tsource仓库URL\tsource分支\t维护状态\trelease版本\n")
    with open(out, "w", encoding="utf-8") as f:
        f.write(header)
        for name, url, branch, status, version in rows:
            f.write(f"{name}\t{url}\t{branch}\t{status}\t{version}\n")

    n_ver = sum(1 for r in rows if r[4])
    print(f"[INFO] {args.ros_distro}: {len(rows)} 个包（{n_ver} 个有 release 版本）→ {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
