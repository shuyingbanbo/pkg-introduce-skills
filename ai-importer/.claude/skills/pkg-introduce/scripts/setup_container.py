#!/usr/bin/env python3
"""
启动 openEuler 构建容器

功能：
  1. 每次调用强制删除旧容器并重新创建（保证环境干净）
  2. 挂载源码目录到容器内 /build/source
  3. 在容器内安装基础构建工具
  4. 提供 exec_in_container() 供其他脚本调用

用法：
  python3 setup_container.py --source-dir ./sources/fzf
  python3 setup_container.py --source-dir ./sources/fzf --name my-build-env
  python3 setup_container.py --stop   # 停止并删除容器
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent

def _load_conf() -> dict:
    # BUILD_ENV_CONF 由 Build Job entrypoint 动态生成并注入，用于多版本/并发场景
    # 未设置时回退到静态配置文件（本地开发 / 单次手动调用）
    if not _CONF_PATH.exists():
        print(f"[ERROR] 配置文件不存在: {_CONF_PATH}", file=sys.stderr)
        sys.exit(1)
    with open(_CONF_PATH) as f:
        return json.load(f)

_env_path = os.environ.get("BUILD_ENV_CONF", "")
_CONF_PATH = Path(_env_path) if _env_path else _SCRIPT_DIR.parent / "build-env.conf.json"

_CONF = _load_conf()
_IMG = _CONF["image"]
_CTR = _CONF["container"]

DEFAULT_IMAGE = _IMG["tag"]
DEFAULT_NAME  = _CTR["name"]
CONTAINER_SOURCE_DIR = _CTR["source_mount"]
PLATFORM = _CTR["platform"]

ARCH = _IMG["arch"]


def _resolve_build_root() -> str:
    """返回稳定镜像源的 repo 根路径（repo.openeuler.org，无需探测 dailybuild 版本）。"""
    base_url = _IMG["base_url"]
    branch   = _IMG["branch"]
    return f"{base_url}/{branch}"


BUILD_ROOT = _resolve_build_root()
REPO_TEMPLATE = """\
[OS]
name=openEuler-OS
baseurl={build_root}/OS/$basearch/
enabled=1
gpgcheck=0

[everything]
name=openEuler-everything
baseurl={build_root}/everything/$basearch/
enabled=1
gpgcheck=0

[update]
name=openEuler-update
baseurl={build_root}/update/$basearch/
enabled=1
gpgcheck=0

[EPOL]
name=openEuler-EPOL
baseurl={build_root}/EPOL/main/$basearch/
enabled=1
gpgcheck=0

[EPOL-update]
name=openEuler-EPOL-update
baseurl={build_root}/EPOL/update/main/$basearch/
enabled=1
gpgcheck=0
"""

# 容器内基础工具（所有语言都需要）
BASE_PACKAGES = [
    "gcc", "gcc-c++", "make", "rpm-build", "dnf-plugins-core",
    "git", "wget", "tar", "which", "findutils",
]


# 各语言专项工具链
LANG_PACKAGES = {
    "go":     ["golang"],
    "python": ["python3", "python3-pip", "python3-devel"],
    "java":   ["java-latest-openjdk-devel", "maven"],
    "rust":   ["rust", "cargo"],
    "nodejs": ["nodejs", "npm"],
    # c/c++ 已由 BASE_PACKAGES 覆盖，无需额外包
    "c":      [],
    "cpp":    [],
}


def container_exists(name: str) -> bool:
    result = subprocess.run(["docker", "inspect", name], capture_output=True)
    return result.returncode == 0


def fix_repo(name: str) -> bool:
    """替换容器内 repo，自动探测最新 build 版本"""
    print("[INFO] 修复 repo 配置...")
    print(f"[INFO] 配置文件: {_CONF_PATH}")

    build_root = _resolve_build_root()
    repo = REPO_TEMPLATE.format(build_root=build_root)
    print(f"[INFO] build root: {build_root}")

    # 通过 docker cp 写入，避免 shell 转义问题
    import tempfile, os
    with tempfile.NamedTemporaryFile(mode="w", suffix=".repo", delete=False) as f:
        f.write(repo)
        tmp = f.name
    subprocess.run(["docker", "cp", tmp, f"{name}:/etc/yum.repos.d/openEuler.repo"], check=True)
    os.unlink(tmp)

    rc = exec_in_container(name, "rm -f /etc/yum.repos.d/*.repo~ && dnf clean all -q", workdir="/")
    if rc != 0:
        print("[ERROR] dnf clean 失败", file=sys.stderr)
        return False
    print("[INFO] repo 已更新")
    return True


def start_container(source_dir: str, name: str, image: str) -> bool:
    """强制删除旧容器并重新创建，确保每次环境干净"""
    src = Path(source_dir).resolve()
    if not src.exists():
        print(f"[ERROR] 源码目录不存在: {src}", file=sys.stderr)
        return False

    if container_exists(name):
        print(f"[INFO] 删除旧容器以确保环境干净: {name}")
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    print(f"[INFO] 创建并启动容器: {name}")
    print(f"[INFO] 镜像: {image}")
    print(f"[INFO] 挂载: {src} → {CONTAINER_SOURCE_DIR}")

    r = subprocess.run([
        "docker", "run", "-d",
        "--platform", PLATFORM,
        "--name", name,
        "-v", f"{src}:{CONTAINER_SOURCE_DIR}",
        image,
        "tail", "-f", "/dev/null",   # 保持容器运行
    ], capture_output=True, text=True)

    if r.returncode != 0:
        print(f"[ERROR] 启动容器失败:\n{r.stderr}", file=sys.stderr)
        return False

    print(f"[INFO] 容器已启动: {r.stdout.strip()[:12]}")
    if not fix_repo(name):
        print("[ERROR] repo 修复失败，停止继续初始化", file=sys.stderr)
        return False
    return True


def exec_in_container(name: str, cmd: str, workdir: str = CONTAINER_SOURCE_DIR) -> int:
    """在容器内执行命令，实时输出，返回退出码"""
    r = subprocess.run([
        "docker", "exec", "-w", workdir, name,
        "bash", "-c", cmd
    ])
    return r.returncode


def install_base_packages(name: str, lang: str = "") -> bool:
    """安装基础构建工具，以及语言专项工具链"""
    print("[INFO] 安装基础构建工具...")
    pkgs = list(BASE_PACKAGES)

    lang_key = lang.lower().replace("-", "").replace(".", "")
    extra = LANG_PACKAGES.get(lang_key, [])
    if extra:
        print(f"[INFO] 追加语言工具链 ({lang}): {' '.join(extra)}")
        pkgs.extend(extra)
    elif lang and lang_key not in LANG_PACKAGES:
        print(f"[WARN] 未知语言类型 '{lang}'，仅安装基础包", file=sys.stderr)

    commands = [f"dnf install -y --allowerasing {' '.join(pkgs)}"]
    if lang_key == "python":
        commands.append(
            "dnf install -y --allowerasing python3 python3-pip && "
            "dnf install -y --allowerasing --nobest python3-devel"
        )
        commands.append(
            "dnf distro-sync -y --allowerasing python3 python3-devel python3-pip"
        )

    for command in commands:
        rc = exec_in_container(name, command)
        if rc == 0:
            print("[INFO] 基础工具安装完成")
            return True
        print(f"[WARN] 安装命令失败，尝试下一种策略: {command}", file=sys.stderr)

    print("[ERROR] 基础包安装失败", file=sys.stderr)
    return False


def stop_container(name: str):
    """停止并删除容器"""
    if container_exists(name):
        print(f"[INFO] 停止容器: {name}")
        subprocess.run(["docker", "stop", name], capture_output=True)
        subprocess.run(["docker", "rm", name], capture_output=True)
        print(f"[INFO] 容器已删除: {name}")
    else:
        print(f"[INFO] 容器不存在: {name}")


def main():
    parser = argparse.ArgumentParser(description="启动 openEuler 构建容器（配置见 build-env.conf.json）")
    parser.add_argument("--source-dir", default="", help="源码目录（挂载到容器内）")
    parser.add_argument("--name", default=DEFAULT_NAME, help=f"容器名（默认 {DEFAULT_NAME}）")
    parser.add_argument("--image", default=DEFAULT_IMAGE, help=f"镜像名（默认 {DEFAULT_IMAGE}）")
    parser.add_argument("--install-base", action="store_true", help="安装基础构建工具")
    parser.add_argument("--lang", default="", help="语言类型（go/python/java/rust/nodejs/c/cpp），用于安装专项工具链")
    parser.add_argument("--stop", action="store_true", help="停止并删除容器")
    args = parser.parse_args()

    if args.stop:
        stop_container(args.name)
        return

    if not args.source_dir:
        parser.print_help()
        sys.exit(1)

    ok = start_container(args.source_dir, args.name, args.image)
    if not ok:
        sys.exit(1)

    if args.install_base:
        ok = install_base_packages(args.name, args.lang)
        if not ok:
            sys.exit(1)

    print(f"\nCONTAINER_NAME={args.name}")
    print(f"SOURCE_DIR_IN_CONTAINER={CONTAINER_SOURCE_DIR}")


if __name__ == "__main__":
    main()
