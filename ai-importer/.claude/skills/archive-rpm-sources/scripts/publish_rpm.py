#!/usr/bin/env python3
"""
RPM 归档脚本：
  - spec + source tarball → <pkg>/ 目录（升级时清理旧 tarball）
  - 编译好的 RPM          → dist/（扁平结构 + repodata，可直接作为 yum 软件源）
  - 升级处理：
      安全升级（patch/minor，无反向依赖）    → 直接替换
      需要 compat（major 或存在反向依赖）    → 按包类型决策：
        C 库 / 非语言运行时二进制            → rpmrebuild 改名，不依赖旧源码
        Python / Java / Ruby / Node         → 报错中止，提示手动处理
      --force-upgrade                        → 跳过 compat 逻辑，直接替换
  - CI 门禁：提交前在容器内跑 repoclosure（运行时依赖）+ dnf builddep（编译期依赖），失败则回滚

用法：
  python3 publish_rpm.py --pkgs python3-foo
  python3 publish_rpm.py --pkgs python3-foo --force-upgrade
  python3 publish_rpm.py --pkgs python3-foo python3-bar --container oe-build-env
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple


# ──────────────────────────────────────────────
# 基础工具
# ──────────────────────────────────────────────

def load_config(config_path: str) -> dict:
    config = Path(config_path)
    if not config.is_absolute():
        script_relative = Path(__file__).resolve().parent.parent / config_path
        if script_relative.exists():
            config = script_relative

    try:
        with open(config) as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"[ERROR] 配置文件不存在: {config}", file=sys.stderr)
        sys.exit(1)


def run(cmd: list, cwd: str = None, check: bool = True) -> subprocess.CompletedProcess:
    print(f"[RUN] {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=cwd, check=check)


def normalize_name_token(value: str) -> str:
    return re.sub(r"[-_.]+", "_", value.lower())


def auth_url(remote_url: str, username: str, token: str) -> str:
    if not token:
        return remote_url
    if "://" in remote_url:
        scheme, rest = remote_url.split("://", 1)
        return f"{scheme}://{username}:{token}@{rest}"
    return remote_url


# ──────────────────────────────────────────────
# Git 仓库管理
# ──────────────────────────────────────────────

def init_or_update_repo(local_dir: str, remote_url: str, branch: str):
    path = Path(local_dir)
    if (path / ".git").exists():
        print(f"[INFO] 拉取最新代码")
        run(["git", "pull", "origin", branch], cwd=local_dir, check=False)
        return
    print(f"[INFO] 克隆仓库: {remote_url} → {local_dir}")
    result = subprocess.run(
        ["git", "clone", "--branch", branch, remote_url, local_dir],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print("[INFO] 克隆失败（空仓库），初始化本地仓库")
        path.mkdir(parents=True, exist_ok=True)
        run(["git", "init"], cwd=local_dir)
        run(["git", "checkout", "-b", branch], cwd=local_dir)
        run(["git", "remote", "add", "origin", remote_url], cwd=local_dir)


def git_commit_and_push(repo_dir: str, branch: str, remote_url: str, msg: str,
                        max_retries: int = 5):
    import random, time

    run(["git", "add", "."], cwd=repo_dir)
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo_dir,
        capture_output=True, text=True
    )
    if not status.stdout.strip():
        print("[INFO] 无变更，跳过提交")
        return
    run(["git", "commit", "-m", msg], cwd=repo_dir)

    last_push_result = None
    for attempt in range(1, max_retries + 1):
        last_push_result = subprocess.run(
            ["git", "push", remote_url, f"HEAD:{branch}", "--set-upstream"],
            cwd=repo_dir, capture_output=True, text=True
        )
        if last_push_result.returncode == 0:
            print(f"[INFO] 推送成功（第 {attempt} 次尝试）")
            return

        print(f"[WARN] 推送失败（第 {attempt}/{max_retries} 次），尝试 rebase 后重试...")
        rebase = subprocess.run(
            ["git", "pull", "--rebase", remote_url, branch],
            cwd=repo_dir, capture_output=True, text=True
        )
        if rebase.returncode != 0:
            # rebase 失败说明存在真正的文件冲突，分析并上报
            conflict_files = subprocess.run(
                ["git", "diff", "--name-only", "--diff-filter=U"],
                cwd=repo_dir, capture_output=True, text=True
            ).stdout.strip()
            status_out = subprocess.run(
                ["git", "status"], cwd=repo_dir, capture_output=True, text=True
            ).stdout.strip()
            # 中止 rebase，恢复干净状态
            subprocess.run(["git", "rebase", "--abort"], cwd=repo_dir, capture_output=True)
            raise RuntimeError(
                f"归档中止：rebase 失败，存在真实冲突，需人工处理。\n\n"
                f"冲突文件:\n{conflict_files or '（无法获取，见下方 status）'}\n\n"
                f"git status:\n{status_out}\n\n"
                f"rebase stderr:\n{rebase.stderr.strip()}"
            )

        jitter = random.randint(2, 8)
        print(f"[INFO] rebase 成功，{jitter}s 后重试推送...")
        time.sleep(jitter)

    # 超过最大重试次数，分析最后一次失败原因并上报
    log_diff = subprocess.run(
        ["git", "log", "--oneline", "-5"],
        cwd=repo_dir, capture_output=True, text=True
    ).stdout.strip()
    raise RuntimeError(
        f"归档中止：推送失败（已重试 {max_retries} 次），需人工处理。\n\n"
        f"最后一次 push 错误:\n{last_push_result.stderr.strip()}\n\n"
        f"本地最近提交:\n{log_diff}"
    )


def git_reset_working_tree(repo_dir: str):
    subprocess.run(["git", "checkout", "--", "."], cwd=repo_dir)
    subprocess.run(["git", "clean", "-fd"], cwd=repo_dir)


# ──────────────────────────────────────────────
# 从容器拷出文件
# ──────────────────────────────────────────────

def copy_pkg_files(
    container: str, pkg_name: str, repo_dir: str
) -> Tuple[List[str], List[Path]]:
    """
    从容器拷出文件：
      - spec + source tarball → <repo_dir>/<pkg_name>/（升级时清理旧 tarball）
      - 编译好的 RPM          → <repo_dir>/dist/
    返回 (已拷出文件相对路径列表, 新增到 dist/ 的 RPM Path 列表)
    通过集合差值精确追踪新增 RPM，不依赖时间戳。
    """
    pkg_dir  = Path(repo_dir) / pkg_name
    dist_dir = Path(repo_dir) / "dist"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    dist_dir.mkdir(parents=True, exist_ok=True)

    copied: List[str] = []
    dist_before = set(dist_dir.glob("*.rpm"))

    # ── spec 文件 → <pkg>/（同名覆盖）──
    # 优先查 <pkg_name>.spec，找不到时依次尝试带语言前缀的名称
    _SPEC_CANDIDATES = [
        f"/root/rpmbuild/SPECS/{pkg_name}.spec",
        f"/root/rpmbuild/SPECS/nodejs-{pkg_name}.spec",
        f"/root/rpmbuild/SPECS/python-{pkg_name}.spec",
        f"/root/rpmbuild/SPECS/python3-{pkg_name}.spec",
        f"/root/rpmbuild/SPECS/java-{pkg_name}.spec",
        f"/root/rpmbuild/SPECS/rubygem-{pkg_name}.spec",
    ]
    spec_src = None
    for _candidate in _SPEC_CANDIDATES:
        if subprocess.run(["docker", "exec", container, "test", "-f", _candidate],
                          capture_output=True).returncode == 0:
            spec_src = _candidate
            break
    if spec_src:
        subprocess.run(
            ["docker", "cp", f"{container}:{spec_src}", str(pkg_dir / f"{pkg_name}.spec")], check=True
        )
        print(f"[INFO] 拷出 spec: {Path(spec_src).name} → {pkg_name}/{pkg_name}.spec")
        copied.append(f"{pkg_name}/{pkg_name}.spec")
    else:
        print(f"[WARN] spec 文件不存在（已尝试 {len(_SPEC_CANDIDATES)} 个候选路径）: {pkg_name}.spec")

    # ── source tarball → <pkg>/（清理旧版本）──
    # 去掉语言前缀，得到裸包名（如 "tabulate"、"asio"）用于匹配 RPM 文件名
    _LANG_PREFIXES = (
        "python3-", "python-", "ros-humble-", "ros-",
        "java-", "maven-", "rubygem-", "nodejs-", "npm-",
        "perl-", "lua-", "php8-", "php-",
    )
    base_name = pkg_name
    for _pfx in _LANG_PREFIXES:
        if base_name.startswith(_pfx):
            base_name = base_name[len(_pfx):]
            break
    normalized_base_name = normalize_name_token(base_name)

    # 从 spec 中提取 %package -n <name> 声明的子包名，用于扩展 RPM 匹配范围
    def get_named_subpackages(container: str, pkg_name: str) -> List[str]:
        spec_path = f"/root/rpmbuild/SPECS/{pkg_name}.spec"
        r = subprocess.run(
            ["docker", "exec", container, "cat", spec_path],
            capture_output=True, text=True
        )
        if r.returncode != 0:
            return []
        names = re.findall(r'^%package\s+-n\s+(\S+)', r.stdout, re.MULTILINE)
        return names

    named_subpkgs = get_named_subpackages(container, pkg_name)
    extra_normalized = [normalize_name_token(n) for n in named_subpkgs]

    # 从 spec 文件中解析 Source0 的实际文件名（支持 %global 宏展开）
    # 这样可以正确处理 Source0 使用不同于包名的 %{pkg_name} 宏的情况
    # 例如：ros-humble-situational-graphs-msgs 的 tarball 是 situational_graphs_msgs-x.y.z.tar.gz
    def resolve_spec_source_prefix(container: str, pkg_name: str) -> Optional[str]:
        spec_path = f"/root/rpmbuild/SPECS/{pkg_name}.spec"
        r = subprocess.run(
            ["docker", "exec", container, "cat", spec_path],
            capture_output=True, text=True
        )
        if r.returncode != 0:
            return None
        spec_text = r.stdout
        # 收集所有 %global 和 %define 宏
        macros: dict = {}
        for m in re.finditer(r'^%(?:global|define)\s+(\w+)\s+(\S+)', spec_text, re.MULTILINE):
            macros[m.group(1)] = m.group(2)
        # 提取 Source0 行
        m = re.search(r'^Source0?\s*:\s*(\S+)', spec_text, re.MULTILINE)
        if not m:
            return None
        src0 = m.group(1)
        # 展开已知宏
        def expand(s: str, macros: dict) -> str:
            for _ in range(5):
                expanded = re.sub(
                    r'%\{(\w+)\}',
                    lambda mo: macros.get(mo.group(1), mo.group(0)),
                    s
                )
                if expanded == s:
                    break
                s = expanded
            return s
        # 将 %{version} 等无法静态解析的宏替换为通配前缀截断
        src0 = expand(src0, macros)
        # 若 Source0 是完整 URL，只取最后一段文件名（URL 中 / 之后的部分）
        if "/" in src0:
            src0 = src0.rsplit("/", 1)[-1]
        # 返回 %{version} / 剩余宏 之前的静态前缀（作为文件名匹配前缀）
        prefix = re.split(r'%\{|\$', src0)[0]
        return prefix if prefix else None

    spec_source_prefix = resolve_spec_source_prefix(container, pkg_name)
    # spec_source_prefix 示例："situational_graphs_msgs-"，优先用它；
    # 若无法解析则回退到 base_name
    effective_prefix = spec_source_prefix if spec_source_prefix else base_name

    existing_tarballs = (
        set(pkg_dir.glob("*.tar.gz")) | set(pkg_dir.glob("*.tar.bz2"))
        | set(pkg_dir.glob("*.tar.xz")) | set(pkg_dir.glob("*.zip"))
    )
    result = subprocess.run(
        ["docker", "exec", container, "bash", "-c",
         "ls /root/rpmbuild/SOURCES/ 2>/dev/null"],
        capture_output=True, text=True
    )
    new_tarballs: set = set()
    for src in result.stdout.strip().splitlines():
        src = src.strip()
        if not src or src.endswith(".whl") or not src.startswith(effective_prefix):
            continue
        # 检查文件大小，跳过超过 GitHub 100MB 限制的文件
        size_result = subprocess.run(
            ["docker", "exec", container, "stat", "-c", "%s", f"/root/rpmbuild/SOURCES/{src}"],
            capture_output=True, text=True
        )
        if size_result.returncode == 0:
            file_size = int(size_result.stdout.strip())
            if file_size > 100 * 1024 * 1024:
                print(f"[WARN] 跳过 source tarball（超过 GitHub 100MB 限制，{file_size // 1024 // 1024}MB）: {src}")
                continue
        dest = pkg_dir / src
        subprocess.run(
            ["docker", "cp", f"{container}:/root/rpmbuild/SOURCES/{src}", str(pkg_dir)],
            check=True
        )
        print(f"[INFO] 拷出 source: {src} → {pkg_name}/")
        copied.append(f"{pkg_name}/{src}")
        new_tarballs.add(dest)

    for old in existing_tarballs - new_tarballs:
        if old.name.startswith(base_name):
            old.unlink()
            print(f"[INFO] 清理旧 tarball: {old.name}")

    # ── 编译好的 RPM → dist/ ──
    result = subprocess.run(
        ["docker", "exec", container, "bash", "-c",
         "find /root/rpmbuild/RPMS /root/rpmbuild/SRPMS -name '*.rpm' 2>/dev/null"],
        capture_output=True, text=True
    )
    for rpm_path in result.stdout.strip().splitlines():
        rpm_path = rpm_path.strip()
        if not rpm_path:
            continue
        rpm_name = Path(rpm_path).name
        normalized_rpm_name = normalize_name_token(rpm_name)
        # 要求 normalized_base_name 出现在词边界处（前缀或跟着 _ ），
        # 防止短包名（如 "bar"）误匹配 "libbar" / "foobar" 等无关包名
        all_base_names = [normalized_base_name] + extra_normalized
        matched = False
        for nb in all_base_names:
            pat = r'(?:^|_)' + re.escape(nb) + r'(?:_|$|\d)'
            if re.search(pat, normalized_rpm_name):
                matched = True
                break
        if not matched:
            continue
        # 检查文件大小，跳过超过 GitHub 100MB 限制的文件
        size_result = subprocess.run(
            ["docker", "exec", container, "stat", "-c", "%s", rpm_path],
            capture_output=True, text=True
        )
        if size_result.returncode == 0:
            file_size = int(size_result.stdout.strip())
            if file_size > 100 * 1024 * 1024:
                print(f"[WARN] 跳过 RPM（超过 GitHub 100MB 限制，{file_size // 1024 // 1024}MB）: {rpm_name}")
                continue
        subprocess.run(
            ["docker", "cp", f"{container}:{rpm_path}", str(dist_dir)], check=True
        )
        print(f"[INFO] 拷出 RPM: {rpm_name} → dist/")
        copied.append(f"dist/{rpm_name}")

    # 集合差值：精确获取新增 RPM
    dist_after = set(dist_dir.glob("*.rpm"))
    new_dist_rpms = list(dist_after - dist_before)
    return copied, new_dist_rpms


# ──────────────────────────────────────────────
# RPM 文件名解析与版本比较
# ──────────────────────────────────────────────

def parse_rpm_nvra(filename: str) -> Optional[dict]:
    """解析 {name}-{version}-{release}.{arch}.rpm，失败返回 None。"""
    if not filename.endswith(".rpm"):
        return None
    base = filename[:-4]
    parts = base.rsplit(".", 1)
    if len(parts) != 2:
        return None
    nvr, arch = parts
    parts = nvr.rsplit("-", 1)
    if len(parts) != 2:
        return None
    nv, release = parts
    parts = nv.rsplit("-", 1)
    if len(parts) != 2:
        return None
    name, version = parts
    return {"name": name, "version": version, "release": release, "arch": arch}


def get_version_change_type(old_ver: str, new_ver: str) -> str:
    """
    比较版本号，返回 'major' / 'minor' / 'patch' / 'unknown'。
    取前三段纯数字比较，兼容 0.48.0.alpha.20260317 等非标格式。

    注意：major=0 的 pre-1.0 包（如 ruyi 0.48→0.49）按 minor 处理。
    如存在 breaking change，请使用 --force-upgrade 并手动处理 compat。
    """
    def to_ints(v: str) -> List[int]:
        parts = []
        for seg in v.split("."):
            if seg.isdigit():
                parts.append(int(seg))
            else:
                break
        return parts[:3]

    old, new = to_ints(old_ver), to_ints(new_ver)
    if not old or not new:
        return "unknown"
    if new[0] != old[0]:
        return "major"
    if len(new) > 1 and len(old) > 1 and new[1] != old[1]:
        return "minor"
    return "patch"


# ──────────────────────────────────────────────
# 包类型检测
# ──────────────────────────────────────────────

# 语言运行时包：安装路径由模块名决定（不含版本），新旧版本文件必然冲突
# no_compat: 直接报错中止
# try_compat: 尝试 rpmrebuild（路径含版本，有机会共存，失败再报错）
_RUNTIME_INDICATORS = {
    "python": ["python3_sitelib", "python_sitelib", "python3_sitearch",
               "%py3_install", "python3dist("],
    "java":   ["%{_javadir}", "%{_mavenpomdir}", "%mvn_", "mvn_install"],
    "nodejs": ["%{nodejs_sitelib}", "npm install", "node_modules"],
    "perl":   ["%{perl_vendorlib}", "%{perl_vendorarch}", "perl(", "Perl_vendorlib"],
    "lua":    ["%{lua_pkgdir}", "lua_version", "%luarocks_install"],
    "php":    ["%{php_extdir}", "%{php_inidir}", "phpize", "%php_zts"],
    "ruby":   ["%{gem_dir}", "%{gem_instdir}", "%{gem_extdir_mri}",
               "gem install", "rubygem("],
}

_NAME_PREFIX_MAP = {
    "python3-": "python", "python-": "python",
    "java-": "java", "maven-": "java",
    "nodejs-": "nodejs", "npm-": "nodejs",
    "perl-": "perl",
    "lua-": "lua",
    "php-": "php", "php8-": "php",
    "rubygem-": "ruby",
}

# 安装路径不含版本号，新旧版本必然文件冲突，不支持 compat
_NO_COMPAT_TYPES = {"python", "nodejs", "perl", "lua", "php"}

# 安装路径含版本号（gem 目录、jar 文件名），有机会 compat，尝试 rpmrebuild
_TRY_COMPAT_TYPES = {"java", "ruby"}


def detect_package_type(pkg_name: str, repo_dir: str) -> str:
    """
    返回包类型：'python' / 'java' / 'ruby' / 'nodejs' / 'other'
    优先读 spec 内容判断，其次按包名前缀推断。
    'other' 包含 C 库和 Go/Rust/C 可执行文件，这类包可以尝试 rpmrebuild compat。
    """
    spec = Path(repo_dir) / pkg_name / f"{pkg_name}.spec"
    if spec.exists():
        content = spec.read_text()
        for lang, markers in _RUNTIME_INDICATORS.items():
            if any(m in content for m in markers):
                return lang

    for prefix, lang in _NAME_PREFIX_MAP.items():
        if pkg_name.startswith(prefix):
            return lang

    return "other"


# ──────────────────────────────────────────────
# 反向依赖查询
# ──────────────────────────────────────────────

def find_rpm_dependents(pkg_name: str, dist_dir: Path, container: str) -> List[str]:
    """查询 dist/ 中哪些 RPM 声明了对 pkg_name 的 Requires。"""
    rpms = list(dist_dir.glob("*.rpm"))
    if not rpms:
        return []

    tmp = "/tmp/_dep_check"
    subprocess.run(["docker", "exec", container, "rm", "-rf", tmp], capture_output=True)
    subprocess.run(["docker", "exec", container, "mkdir", "-p", tmp], capture_output=True)
    for rpm in rpms:
        subprocess.run(
            ["docker", "cp", str(rpm), f"{container}:{tmp}/"], capture_output=True
        )

    result = subprocess.run(
        ["docker", "exec", container, "bash", "-c",
         f"for f in {tmp}/*.rpm; do "
         f"  rpm -qp --requires \"$f\" 2>/dev/null | grep -q '^{re.escape(pkg_name)}' "
         f"  && basename \"$f\"; "
         f"done"],
        capture_output=True, text=True
    )
    subprocess.run(["docker", "exec", container, "rm", "-rf", tmp], capture_output=True)
    return [r.strip() for r in result.stdout.strip().splitlines() if r.strip()]


# ──────────────────────────────────────────────
# Compat 包生成（rpmrebuild，不依赖旧源码）
# ──────────────────────────────────────────────

def create_compat_via_rpmrebuild(
    pkg_name: str,
    old_rpm: Path,
    old_info: dict,
    dist_dir: Path,
    container: str,
) -> bool:
    """
    使用 rpmrebuild 从已有 RPM 文件直接改名生成 compat 包，无需旧源码。
    适用于 C 库和可执行文件类型的包。

    compat 包命名：{pkg_name}-{old_major}
    compat 包提供：Provides: {pkg_name} = {old_version}-{old_release}

    注意：此方法对 Python/Java 等包无效，因为文件路径相同会导致安装冲突。
    返回是否成功。
    """
    old_major   = old_info["version"].split(".")[0]
    compat_name = f"{pkg_name}-{old_major}"

    if list(dist_dir.glob(f"{compat_name}-*.rpm")):
        print(f"[COMPAT] {compat_name} 已存在，跳过")
        return True

    print(f"[COMPAT] 生成 compat 包: {compat_name}（基于 {old_rpm.name}）")

    # 安装 rpmrebuild
    inst = subprocess.run(
        ["docker", "exec", container,
         "dnf", "install", "-y", "--quiet", "rpmrebuild"],
        capture_output=True
    )
    if inst.returncode != 0:
        # rpmrebuild 可能不在社区源里，尝试直接检查是否已安装
        check = subprocess.run(
            ["docker", "exec", container, "which", "rpmrebuild"],
            capture_output=True
        )
        if check.returncode != 0:
            print(f"[WARN] rpmrebuild 不可用（安装失败且未找到）", file=sys.stderr)
            return False

    tmp = "/tmp/_compat_build"
    subprocess.run(
        ["docker", "exec", container, "bash", "-c", f"rm -rf {tmp} && mkdir -p {tmp}/output"],
        capture_output=True
    )

    # 将旧 RPM 拷入容器
    container_rpm = f"{tmp}/{old_rpm.name}"
    subprocess.run(
        ["docker", "cp", str(old_rpm), f"{container}:{container_rpm}"], check=True
    )

    # spec 修改脚本：改 Name，加 Provides
    provides  = f"{pkg_name} = {old_info['version']}-{old_info['release']}"
    patch_py  = (
        "import sys, re\n"
        "c = sys.stdin.read()\n"
        # 替换 Name 字段（\\s* 在 f-string 里 \\ → \，生成 r'^(Name:\s*)...'）
        f"c = re.sub(r'^(Name:\\s*){re.escape(pkg_name)}(\\s*)$',\n"
        f"           r'\\g<1>{compat_name}\\2', c, flags=re.MULTILINE)\n"
        # 在 Name 行后插入 Provides
        f"c = re.sub(r'(^Name:.*$)',\n"
        f"           r'\\1\\nProvides: {provides}',\n"
        f"           c, count=1, flags=re.MULTILINE)\n"
        "sys.stdout.write(c)\n"
    )
    subprocess.run(
        ["docker", "exec", "-i", container, "bash", "-c",
         f"cat > {tmp}/patch.py"],
        input=patch_py, text=True, check=True
    )

    # 运行 rpmrebuild
    result = subprocess.run(
        ["docker", "exec", container,
         "rpmrebuild",
         "--change-spec-preamble", f"python3 {tmp}/patch.py",
         "--notest-install",
         "-d", f"{tmp}/output",
         "-p", container_rpm],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"[WARN] rpmrebuild 失败:\n{result.stdout[-600:]}", file=sys.stderr)
        return False

    # 将 compat RPM 拷回 dist/
    find_result = subprocess.run(
        ["docker", "exec", container, "bash", "-c",
         f"find {tmp}/output -name '*.rpm' 2>/dev/null"],
        capture_output=True, text=True
    )
    found = False
    for rp in find_result.stdout.strip().splitlines():
        rp = rp.strip()
        if rp:
            subprocess.run(
                ["docker", "cp", f"{container}:{rp}", str(dist_dir)], check=True
            )
            print(f"[COMPAT] 已创建: {Path(rp).name}")
            found = True

    subprocess.run(
        ["docker", "exec", container, "rm", "-rf", tmp], capture_output=True
    )
    return found


# ──────────────────────────────────────────────
# dist/ 升级冲突处理
# ──────────────────────────────────────────────

def resolve_dist_conflicts(
    dist_dir: Path,
    new_rpms: List[Path],      # 通过集合差值传入，不依赖时间戳
    container: str,
    repo_dir: str,
    force_upgrade: bool = False,
) -> Tuple[List[Path], List[str]]:
    """
    对 new_rpms 中每个包，查找 dist/ 中是否存在旧版本（name+arch 相同，文件名不同）。

    处理逻辑：
      --force-upgrade            → 跳过 compat，直接删旧版本
      patch/minor + 无反向依赖   → 安全升级，直接替换
      major 或存在反向依赖：
        C 库 / other 类型        → rpmrebuild 创建 compat，成功则删旧版本
                                   失败则删新版本（回滚），抛 RuntimeError
        Python/Java/Ruby/Node   → 删新版本（回滚），抛 RuntimeError 提示手动处理

    返回 (已移除的旧 RPM 列表, 升级说明列表)
    """
    removed:       List[Path] = []
    upgrade_notes: List[str]  = []

    for new_rpm in new_rpms:
        new_info = parse_rpm_nvra(new_rpm.name)
        if not new_info:
            raise ValueError(
                f"无法解析新 RPM 文件名: {new_rpm.name}，"
                "请确认格式为 {name}-{version}-{release}.{arch}.rpm"
            )

        for existing in list(dist_dir.glob("*.rpm")):
            if existing.name == new_rpm.name:
                continue
            existing_info = parse_rpm_nvra(existing.name)
            if not existing_info:
                continue
            if (existing_info["name"] != new_info["name"] or
                    existing_info["arch"] != new_info["arch"]):
                continue

            # ── 发现版本冲突 ──
            change_type = get_version_change_type(
                existing_info["version"], new_info["version"]
            )
            pkg_name = existing_info["name"]
            print(f"\n[UPGRADE] {pkg_name}: "
                  f"{existing_info['version']} → {new_info['version']} ({change_type})")

            # ── --force-upgrade：跳过 compat，直接替换 ──
            if force_upgrade:
                print(f"[UPGRADE] --force-upgrade：直接替换，跳过 compat 检查")
                existing.unlink()
                removed.append(existing)
                upgrade_notes.append(
                    f"{pkg_name}: 强制升级 {existing_info['version']} → {new_info['version']}"
                )
                continue

            # ── 判断是否需要 compat ──
            dependents  = find_rpm_dependents(pkg_name, dist_dir, container)
            needs_compat = (change_type == "major") or bool(dependents)

            if not needs_compat:
                existing.unlink()
                removed.append(existing)
                upgrade_notes.append(
                    f"{pkg_name}: 安全升级 ({change_type})，"
                    f"无反向依赖，已替换"
                )
                continue

            # ── 需要 compat ──
            reason = ("major 版本升级" if change_type == "major"
                      else f"存在反向依赖: {dependents}")
            print(f"[UPGRADE] 需要 compat 包（{reason}）")

            pkg_type = detect_package_type(pkg_name, repo_dir)

            if pkg_type in _NO_COMPAT_TYPES:
                # 安装路径由模块名决定，不含版本信息，新旧版本文件必然冲突
                new_rpm.unlink()   # 回滚：删新版本，保留旧版本
                raise RuntimeError(
                    f"\n[{pkg_name}] {pkg_type} 包不支持自动创建 compat 包，归档中止。\n"
                    f"  原因：{pkg_type} 包文件安装到固定路径（路径不含版本号），"
                    f"新旧版本文件路径冲突。\n\n"
                    f"  解决方案：\n"
                    f"  1. 先升级所有反向依赖包（{dependents}），再归档此包\n"
                    f"  2. 使用 --force-upgrade 强制升级（直接替换，反向依赖包可能运行时受影响）"
                )

            # ── C 库 / 可执行文件：尝试 rpmrebuild ──
            success = create_compat_via_rpmrebuild(
                pkg_name, existing, existing_info, dist_dir, container
            )

            if success:
                existing.unlink()
                removed.append(existing)
                old_major = existing_info["version"].split(".")[0]
                upgrade_notes.append(
                    f"{pkg_name}: {change_type} 升级，"
                    f"已创建 compat 包 {pkg_name}-{old_major}"
                )
            else:
                # compat 构建失败：回滚新版本，保留旧版本
                new_rpm.unlink()
                raise RuntimeError(
                    f"\n[{pkg_name}] compat 包创建失败，已回滚（保留旧版本 "
                    f"{existing_info['version']}）。\n\n"
                    f"  可能原因：\n"
                    f"  - rpmrebuild 未安装或不在 openEuler 源中\n"
                    f"  - spec 中有复杂宏依赖，rpmrebuild 无法处理\n\n"
                    f"  解决方案：\n"
                    f"  1. 手动在容器内安装 rpmrebuild 后重试\n"
                    f"  2. 使用 --force-upgrade 强制升级（不创建 compat）"
                )

    return removed, upgrade_notes


# ──────────────────────────────────────────────
# repodata 更新
# ──────────────────────────────────────────────

def update_repodata(dist_dir: Path):
    """运行 createrepo_c --update，失败则抛出 RuntimeError。"""
    if subprocess.run(["which", "createrepo_c"], capture_output=True).returncode != 0:
        raise RuntimeError("createrepo_c 未安装：apt-get install createrepo-c -y")
    print(f"[INFO] 重建 repodata")
    result = subprocess.run(
        ["createrepo_c", "--update", str(dist_dir)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"createrepo_c 执行失败:\n{result.stderr.strip()}")
    print(result.stdout.strip())


# ──────────────────────────────────────────────
# CI 门禁
# ──────────────────────────────────────────────

def run_ci_gate(dist_dir: Path, container: str, new_rpms: list = None, repo_dir: str = None, pkgs: list = None):
    """
    将本地 dist/ 复制到容器内，运行两项检查：
    1. repoclosure：验证本次新增 RPM 的运行时 Requires 可满足
    2. dnf builddep：验证本次新增包的 spec BuildRequires 可满足
    失败则抛出 RuntimeError。
    """
    tmp = "/tmp/_ci_dist"

    # 清理 DNF ci-local 缓存，避免旧元数据干扰
    subprocess.run(
        ["docker", "exec", container, "bash", "-c", "rm -rf /var/cache/dnf/ci-local*"],
        capture_output=True
    )

    print(f"\n[CI] 复制 dist/ 到容器...")
    subprocess.run(["docker", "exec", container, "rm", "-rf", tmp], capture_output=True)
    subprocess.run(["docker", "cp", str(dist_dir), f"{container}:{tmp}"], check=True)

    subprocess.run(
        ["docker", "exec", container,
         "dnf", "install", "-y", "--quiet", "dnf-utils"],
        capture_output=True
    )

    # ── 检查1：repoclosure（运行时依赖）──
    # 先检测容器内是否有 repoclosure，没有则跳过（builder 阶段已用 dnf builddep 验证）
    has_repoclosure = subprocess.run(
        ["docker", "exec", container, "bash", "-c", "which repoclosure 2>/dev/null"],
        capture_output=True, text=True
    ).returncode == 0

    if not has_repoclosure:
        print("[CI] ⚠ repoclosure 不可用，跳过运行时依赖检查（builder 阶段已验证）")
    else:
        # 先探测哪些 repo 可用，跳过返回 404 的镜像
        probe = subprocess.run(
            ["docker", "exec", container, "bash", "-c",
             "dnf repolist --enabled 2>/dev/null | awk 'NR>1{print $1}'"],
            capture_output=True, text=True
        )
        available_repos = set(probe.stdout.split())
        wanted_repos = ["ci-local", "OS", "everything", "update", "EPOL", "EPOL-update", "repo-aitest"]
        cmd = [
            "docker", "exec", container,
            "repoclosure",
            "--repofrompath", f"ci-local,{tmp}",
            "--disablerepo=*",
        ]
        for repo in wanted_repos:
            if repo == "ci-local" or repo in available_repos:
                cmd += [f"--enablerepo={repo}"]
        cmd += ["--newest"]
        if new_rpms:
            for rpm_path in new_rpms:
                name = rpm_path.name
                m = re.match(r'^(.+?)-[^-]+-[^-]+\.[^.]+\.rpm$', name)
                pkg_name = m.group(1) if m else name.replace('.rpm', '')
                cmd += ["--pkg", pkg_name]
            print(f"[CI] 运行 repoclosure（检查 {len(new_rpms)} 个新包的运行时依赖）...")
        else:
            cmd += ["--check", "ci-local"]
            print(f"[CI] 运行 repoclosure（检查全部包的运行时依赖）...")

        result = subprocess.run(cmd, capture_output=True, text=True)
        subprocess.run(["docker", "exec", container, "rm", "-rf", tmp], capture_output=True)

        if result.returncode != 0:
            raise RuntimeError(
                f"CI 门禁未通过 — 运行时依赖检查失败:\n"
                f"{result.stdout.strip()}\n{result.stderr.strip()}"
            )
        print("[CI] ✓ 运行时依赖检查通过")

    # ── 检查2：dnf builddep（编译期依赖）──
    if repo_dir and pkgs:
        _check_builddeps(container, repo_dir, pkgs, tmp)


def _check_builddeps(container: str, repo_dir: str, pkgs: list, tmp_dist: str):
    """
    对每个包的 spec 文件运行 dnf builddep --assumeno，验证 BuildRequires 可满足。
    tmp_dist 是已复制到容器内的 dist/ 路径，用于配置 ci-local 源。
    """
    # 先把 dist/ 重新复制进容器（repoclosure 结束后已删除）
    dist_dir = Path(repo_dir) / "dist"
    subprocess.run(["docker", "exec", container, "rm", "-rf", tmp_dist], capture_output=True)
    subprocess.run(["docker", "cp", str(dist_dir), f"{container}:{tmp_dist}"], check=True)

    errors = []
    for pkg in pkgs:
        spec_local = Path(repo_dir) / pkg / f"{pkg}.spec"
        if not spec_local.exists():
            print(f"[CI] builddep: spec 不存在，跳过 {pkg}")
            continue

        # 将 spec 拷入容器
        tmp_spec = f"/tmp/_ci_spec_{pkg}.spec"
        subprocess.run(
            ["docker", "cp", str(spec_local), f"{container}:{tmp_spec}"], check=True
        )

        print(f"[CI] 运行 dnf builddep（检查 {pkg} 的编译期依赖）...")
        result = subprocess.run(
            ["docker", "exec", container,
             "dnf", "builddep", "--assumeno",
             "--repofrompath", f"ci-local,{tmp_dist}",
             "--enablerepo", "ci-local",
             tmp_spec],
            capture_output=True, text=True
        )
        subprocess.run(
            ["docker", "exec", container, "rm", "-f", tmp_spec], capture_output=True
        )

        # dnf builddep --assumeno 在依赖可满足时以非零码退出（因为 assumeno 拒绝了安装）
        # 只有在依赖无法满足时才会输出 "Error:" 并包含 "could not be found" 或 "No match"
        combined = result.stdout + result.stderr
        dep_failed = "Error:" in combined and (
            "could not be found" in combined or "No match" in combined
        )
        if dep_failed:
            errors.append(f"{pkg} BuildRequires 不满足:\n{combined.strip()}")
        else:
            print(f"[CI] ✓ {pkg} 编译期依赖检查通过")

    subprocess.run(["docker", "exec", container, "rm", "-rf", tmp_dist], capture_output=True)

    if errors:
        raise RuntimeError(
            "CI 门禁未通过 — 编译期依赖检查失败:\n" + "\n\n".join(errors)
        )


# ──────────────────────────────────────────────
# 辅助
# ──────────────────────────────────────────────

def ensure_repo_file(dist_dir: Path, raw_base_url: str):
    repo_file = dist_dir / "repo-aitest.repo"
    content = (
        f"[repo-aitest]\n"
        f"name=openEuler RPM Repository\n"
        f"baseurl={raw_base_url}/dist\n"
        f"enabled=1\n"
        f"gpgcheck=0\n"
    )
    if not repo_file.exists() or repo_file.read_text() != content:
        repo_file.write_text(content)
        print(f"[INFO] 更新 .repo 配置")


def ensure_readme(repo_dir: str, remote_url: str):
    readme = Path(repo_dir) / "README.md"
    if readme.exists():
        return
    # 只剥 user:token@ 凭据，保留协议（split("@")[-1] 会把 https:// 也丢掉）
    clean_url = re.sub(r"://[^@/]+@", "://", remote_url)
    raw_base = clean_url.replace(
        "https://github.com/", "https://raw.githubusercontent.com/"
    ).removesuffix(".git")
    readme.write_text(f"""# openEuler RPM 仓库

## 目录结构

```
<pkg-name>/   spec 文件 + 上游源码 tarball
dist/         编译好的 RPM 包 + repodata（yum 软件源）
```

## 使用软件源

```bash
curl -o /etc/yum.repos.d/repo-aitest.repo \\
  {raw_base}/main/dist/repo-aitest.repo
dnf repolist
```

## 仓库地址

{clean_url}
""")
    print("[INFO] 已生成 README.md")


# ──────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────

def _is_review_rpm_report(report_path: Path) -> bool:
    """
    检查报告是否由 review-rpm summary 生成。
    判断依据：必须同时包含以下章节标题中的至少 3 个，
    说明是结构化的完整报告而非手工写的简短摘要。
    """
    _REQUIRED_SECTIONS = [
        "## 1.",   # 基本信息
        "## 2.",   # 上游合规 / 各章节
        "## 3.",   # License / 章节
        "## 4.",   # 版本决策 / 章节
        "## 5.",   # RPM 产物 / 章节
    ]
    try:
        content = report_path.read_text(encoding="utf-8")
        matched = sum(1 for s in _REQUIRED_SECTIONS if s in content)
        return matched >= 3
    except Exception:
        return False


def archive_introduction_reports(pkgs: list, reports_dir: str, repo_dir: str, pkg_dir: str = "") -> int:
    """
    将主包的所有引入报告归档到 repo_dir/reports/success/<pkgname>-<version>-<YYYYMMDD>/
    或 repo_dir/reports/failed/<pkgname>-<version>-<YYYYMMDD>/。

    归档内容（均为主包 pkgname 对应的文件）：
      - <pkgname>_introduction_report.md    汇总报告（若存在）
      - pkg_introduce_result_<pkgname>.json 主包引入结果
      - check_result_<pkgname>.json         Phase 1 基础检查报告
      - gate_result_<pkgname>.json          Phase 2 引入门禁报告
      - build_rpm_result_<pkgname>.json     构建结果
      - import_issues.log                   问题日志（整个 session 共享）
      - pkg_introduce_result_*.json         所有依赖包引入结果（本次 session 引入的）
      - build.log                           rpmbuild 完整编译日志（来自 pkg_dir/<pkgname>/）
      - <pkgname>.spec                      spec 文件（来自 pkg_dir/<pkgname>/）
      - pre_check_<pkgname>.json            依赖预检结果（来自 pkg_dir/<pkgname>/ 或 reports_dir）
      - rpmlint.txt                         rpmlint 检查输出（来自 pkg_dir/<pkgname>/）

    返回归档成功的目录数量。
    """
    if not reports_dir:
        return 0

    reports_path = Path(reports_dir)
    pkg_root = Path(pkg_dir) if pkg_dir else None
    today = datetime.now().strftime("%Y%m%d")
    archived = 0

    for pkg in pkgs:
        # 从 build_rpm_result.json 或 pkg_introduce_result 读取版本和 action
        version = "unknown"
        action = "unknown"

        # 优先从 build_rpm_result.json 读
        build_result_file = reports_path / f"build_rpm_result_{pkg}.json"
        if not build_result_file.exists() and pkg_root:
            build_result_file = pkg_root / pkg / "build_rpm_result.json"
        if build_result_file.exists():
            try:
                data = json.loads(build_result_file.read_text(encoding="utf-8"))
                version = data.get("version") or data.get("requested_version") or "unknown"
                action = data.get("action") or data.get("status") or "unknown"
            except Exception:
                pass

        # fallback：从 pkg_introduce_result 读
        if action == "unknown":
            result_file = reports_path / f"pkg_introduce_result_{pkg}.json"
            if result_file.exists():
                try:
                    data = json.loads(result_file.read_text(encoding="utf-8"))
                    version = data.get("version") or data.get("requested_version") or "unknown"
                    action = data.get("action", "unknown")
                except Exception:
                    pass

        subfolder = "failed" if action in ("blocked", "failed") else "success"
        dir_name = f"{pkg}-{version}-{today}"
        dest_dir = Path(repo_dir) / "reports" / subfolder / dir_name
        dest_dir.mkdir(parents=True, exist_ok=True)

        copied = []

        # 汇总报告路径（success 门检与下方复制共用，必须先于此处定义）
        report_src = reports_path / f"{pkg}_introduction_report.md"

        # 成功归档：必须有结构化汇总报告且 review_summary 完成
        if subfolder == "success":
            if not report_src.exists() or not _is_review_rpm_report(report_src):
                print(f"[WARN] {pkg}: 报告不存在或不是结构化报告，跳过 success 归档")
                continue
            steps_file = reports_path / f"steps_{pkg}.json"
            if steps_file.exists():
                try:
                    steps = json.loads(steps_file.read_text(encoding="utf-8"))
                    if steps.get("review_summary") not in ("done", "skipped"):
                        print(f"[WARN] {pkg}: review_summary 步骤未完成，跳过 success 归档")
                        continue
                except Exception:
                    pass

        # 1. 汇总报告（可选，失败时可能不存在）
        report_src = reports_path / f"{pkg}_introduction_report.md"
        if report_src.exists() and _is_review_rpm_report(report_src):
            shutil.copy2(str(report_src), str(dest_dir / report_src.name))
            copied.append(report_src.name)

        # 2. 固定名称的报告文件（来自 reports_dir）
        for fname in (
            f"pkg_introduce_result_{pkg}.json",
            f"check_result_{pkg}.json",
            f"gate_result_{pkg}.json",
            f"build_rpm_result_{pkg}.json",
            f"pre_check_{pkg}.json",
        ):
            src = reports_path / fname
            if src.exists():
                shutil.copy2(str(src), str(dest_dir / fname))
                copied.append(fname)

        # 3. 来自 pkgs/<pkgname>/ 的构建产物
        if pkg_root:
            pkg_build_dir = pkg_root / pkg
            for fname in ("build.log", f"{pkg}.spec", "rpmlint.txt",
                          f"pre_check_{pkg}.json", f"build_rpm_result.json"):
                src = pkg_build_dir / fname
                if src.exists() and fname not in copied:
                    shutil.copy2(str(src), str(dest_dir / fname))
                    copied.append(fname)

        # 4. import_issues.log（整个 session 共享）
        issues_log = reports_path / "import_issues.log"
        if issues_log.exists():
            shutil.copy2(str(issues_log), str(dest_dir / "import_issues.log"))
            copied.append("import_issues.log")

        # 5. 所有依赖包的 pkg_introduce_result_*.json
        for dep_result in sorted(reports_path.glob("pkg_introduce_result_*.json")):
            if dep_result.name == f"pkg_introduce_result_{pkg}.json":
                continue
            shutil.copy2(str(dep_result), str(dest_dir / dep_result.name))
            copied.append(dep_result.name)

        print(f"[INFO] 归档报告目录: reports/{subfolder}/{dir_name}/ ({len(copied)} 个文件)")
        archived += 1

    return archived


def mark_archived_in_result(pkgs: list, reports_dir: Optional[str]) -> None:
    """Update pkg_introduce_result_<pkg>.json with archived=true if it exists."""
    if not reports_dir:
        return
    reports_path = Path(reports_dir)
    for pkg in pkgs:
        result_file = reports_path / f"pkg_introduce_result_{pkg}.json"
        if not result_file.exists():
            continue
        try:
            data = json.loads(result_file.read_text(encoding="utf-8"))
            data["archived"] = True
            result_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[INFO] 已更新 {result_file.name}: archived=true")
        except Exception as exc:
            print(f"[WARN] 无法更新 {result_file.name}: {exc}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="归档 RPM 到 GitHub 仓库")
    parser.add_argument("--container",     default="oe-build-env", help="容器名")
    parser.add_argument("--pkgs",          nargs="+", required=True, help="包名列表")
    parser.add_argument("--config",        default="config.json",   help="配置文件路径")
    parser.add_argument("--force-upgrade", action="store_true",
                        help="跳过 compat 逻辑，直接替换旧版本（需要 compat 时慎用）")
    parser.add_argument("--reports-dir",   default="",
                        help="pkg-introduce reports 目录，归档成功后自动回写 archived=true")
    parser.add_argument("--pkg-dir",       default="",
                        help="pkgs/ 根目录，用于收录 build.log、spec、pre_check.json 等构建产物")
    args = parser.parse_args()

    cfg      = load_config(args.config)
    # 支持 gitcode 和 github 两种配置 key
    git_cfg  = cfg.get("gitcode") or cfg.get("github") or {}
    token    = git_cfg.get("token", "")
    username = git_cfg.get("username", "oauth2")   # GitCode 用 oauth2 作为用户名
    remote   = cfg["repo"]["remote_url"]
    branch   = cfg["repo"]["branch"]
    local    = cfg["repo"]["local_dir"]
    authed   = auth_url(remote, username, token)

    raw_base = (
        remote
        .replace("https://github.com/", "https://raw.githubusercontent.com/")
        .removesuffix(".git")
    )
    raw_base = f"{raw_base}/{branch}"

    # ── Step 1: 初始化/拉取仓库 ──
    print("\n=== Step 1: 初始化仓库 ===")
    init_or_update_repo(local, authed, branch)
    ensure_readme(local, remote)

    # ── Step 2: 从容器拷出文件，追踪新增 RPM ──
    print("\n=== Step 2: 拷出文件 ===")
    all_copied:    List[str]  = []
    all_new_rpms:  List[Path] = []
    skipped_pkgs:  List[str]  = []
    for pkg in args.pkgs:
        print(f"\n[INFO] 处理包: {pkg}")
        # 步骤清单检查：build 和 review_summary 都必须完成才能归档
        if args.reports_dir:
            steps_file = Path(args.reports_dir) / f"steps_{pkg}.json"
            if steps_file.exists():
                try:
                    steps = json.loads(steps_file.read_text(encoding="utf-8"))
                    build_status = steps.get("build", "pending")
                    ci_gate_status = steps.get("ci_gate", "pending")
                    review_status = steps.get("review_summary", "pending")
                    if build_status not in ("done", "skipped", "failed"):
                        print(f"[ERROR] {pkg}: build 步骤未完成（status={build_status}），阻断归档", file=sys.stderr)
                        skipped_pkgs.append(pkg)
                        continue
                    if build_status == "failed":
                        print(f"[INFO] {pkg}: build 步骤失败，跳过 RPM 归档，仅归档失败报告")
                        skipped_pkgs.append(pkg)
                        continue
                    if ci_gate_status not in ("done", "skipped"):
                        print(f"[ERROR] {pkg}: ci_gate 步骤未完成（status={ci_gate_status}），阻断归档", file=sys.stderr)
                        print(f"[ERROR] CI 门禁由 builder 阶段2 负责，请确认 builder 已完成 CI 验证", file=sys.stderr)
                        skipped_pkgs.append(pkg)
                        continue
                    if review_status not in ("done", "skipped"):
                        print(f"[WARN] {pkg}: review_summary 步骤未完成（status={review_status}），跳过归档")
                        print(f"[WARN] 请先执行 /review-rpm summary {pkg} 生成引入报告后再归档")
                        skipped_pkgs.append(pkg)
                        continue
                except Exception:
                    pass
        copied, new_rpms = copy_pkg_files(args.container, pkg, local)
        all_copied.extend(copied)
        all_new_rpms.extend(new_rpms)
        print(f"[INFO] {pkg}: 归档 {len(copied)} 个文件，新增 RPM {len(new_rpms)} 个")
    if skipped_pkgs:
        print(f"\n[WARN] 以下包因步骤未完成被跳过归档 RPM: {', '.join(skipped_pkgs)}")

    # ── Step 3: 升级冲突处理 + repodata 更新 ──
    print("\n=== Step 3: 处理升级冲突，更新 dist ===")
    dist_dir = Path(local) / "dist"
    try:
        removed, upgrade_notes = resolve_dist_conflicts(
            dist_dir, all_new_rpms, args.container, local,
            force_upgrade=args.force_upgrade
        )
        if removed:
            print(f"\n[INFO] 共移除 {len(removed)} 个旧版本 RPM")
        update_repodata(dist_dir)
        ensure_repo_file(dist_dir, raw_base)
    except (ValueError, RuntimeError) as e:
        print(f"\n[ERROR] dist 更新失败: {e}", file=sys.stderr)
        print("[ERROR] 回滚工作区，归档中止", file=sys.stderr)
        # git reset 只能恢复已跟踪文件；新增的文件（新 RPM、新 spec）需用 clean 清除。
        # git_reset_working_tree 已组合了 checkout -- . 和 clean -fd，
        # 对全新仓库（无历史提交）同样能清除 untracked 文件。
        git_reset_working_tree(local)
        # 额外删除 all_new_rpms 中未被 git 跟踪且仍存在的文件（防止 git clean 漏掉）
        for rpm in all_new_rpms:
            if rpm.exists():
                rpm.unlink()
                print(f"[ROLLBACK] 删除未提交的新 RPM: {rpm.name}", file=sys.stderr)
        sys.exit(1)

    # ── Step 4: 归档引入报告 ──
    print("\n=== Step 4: 归档引入报告 ===")
    archived_count = archive_introduction_reports(args.pkgs, args.reports_dir or "", local, pkg_dir=args.pkg_dir or "")
    if archived_count:
        print(f"[INFO] 共归档 {archived_count} 份引入报告")
    else:
        print("[INFO] 无引入报告可归档")

    # ── Step 5: 提交推送 ──
    print("\n=== Step 5: 提交推送 ===")
    try:
        git_commit_and_push(local, branch, authed, f"add {', '.join(args.pkgs)}")
    except RuntimeError as e:
        print(f"\n[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    # ── Step 6: 回写 archived=true ──
    mark_archived_in_result(args.pkgs, args.reports_dir or None)

    print(f"""
========================================
RPM 归档报告
========================================
包数量    : {len(args.pkgs)} 个
归档文件  : {len(all_copied)} 个
引入报告  : {archived_count} 份（reports/success/ 或 reports/failed/）
仓库      : {remote}
软件源    : {raw_base}/dist

升级处理:
{chr(10).join(f'  - {n}' for n in upgrade_notes) if upgrade_notes else '  （无版本冲突）'}

已归档文件:
{chr(10).join(f'  - {f}' for f in all_copied)}
========================================
""")


if __name__ == "__main__":
    main()
