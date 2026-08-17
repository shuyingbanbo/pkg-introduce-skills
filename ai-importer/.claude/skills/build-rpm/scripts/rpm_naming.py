#!/usr/bin/env python3
"""
RPM 包命名统一模块。

各语言的 RPM 包名和 Requires 表达式由此模块统一生成，
所有 analyze_*_deps.py、pre_check_deps.py、spec 生成均应通过此模块取值，
不得在各处自行拼接包名前缀。

openEuler 双包模式（Python）：
  SRPM Name:      python-<name>        （源码包，用于仓库管理）
  二进制包名:      python3-<name>       （实际安装的包，通过 %package -n 声明）
  Requires: 写法: python3-<name>       （引用二进制包名）

其他语言命名惯例：
- Node.js: nodejs-<name>
- Java   : <groupId>:<artifact>（通过 mvn() Provides 机制）
- C/C++  : 通过 pkgconfig() / cmake() / lib*.so Provides 机制
- Go     : 通常无运行时 RPM 依赖（vendor 构建）
- Rust   : 通常无运行时 RPM 依赖（静态链接）
"""

import re


def _normalize(name: str) -> str:
    """规范化包名：小写 + 连字符替换下划线/点（PEP 503 / npm 惯例）。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def get_srpm_name(lang: str, upstream_name: str) -> str:
    """
    返回 SRPM 名，即 spec 文件 Name: 字段的值（源码包名）。

    Python 双包模式下 SRPM 名用 python- 前缀：
        get_srpm_name("python", "requests")         → "python-requests"
        get_srpm_name("python", "python-multipart") → "python-python-multipart"
        get_srpm_name("python", "Django")            → "python-django"

    其他语言 SRPM 名与二进制包名相同（无双包模式）：
        get_srpm_name("nodejs", "lodash")            → "nodejs-lodash"
    """
    lang = lang.lower()
    if lang == "python":
        return f"python-{_normalize(upstream_name)}"
    else:
        return get_rpm_pkg_name(lang, upstream_name)


def get_rpm_pkg_name(lang: str, upstream_name: str) -> str:
    """
    返回二进制 RPM 包名，用于：
    - Python 双包模式下的 %package -n 字段
    - Requires: / BuildRequires: 中的包名
    - rpm -qa 查到的实际包名

    Python 示例：
        get_rpm_pkg_name("python", "requests")         → "python3-requests"
        get_rpm_pkg_name("python", "python-multipart") → "python3-python-multipart"
        get_rpm_pkg_name("python", "Django")            → "python3-django"

    Node.js 示例：
        get_rpm_pkg_name("nodejs", "lodash")            → "nodejs-lodash"
        get_rpm_pkg_name("nodejs", "@scope/pkg")        → "nodejs-scope-pkg"
    """
    lang = lang.lower()
    if lang == "python":
        return f"python3-{_normalize(upstream_name)}"
    elif lang == "nodejs":
        name = upstream_name
        if name.startswith("@"):
            name = name.lstrip("@").replace("/", "-")
        return f"nodejs-{_normalize(name)}"
    else:
        # java: 用 mvn(groupId:artifactId) Provides 机制，不加前缀
        # c/cpp: 用 pkgconfig() / cmake() / lib*.so，不加前缀
        # go/rust: 通常无运行时 RPM 依赖
        return upstream_name


def get_rpm_requirement(lang: str, upstream_name: str, constraint: str = "") -> str:
    """
    返回可直接写入 spec Requires: 的完整表达式（使用二进制包名）。

    Python 示例：
        get_rpm_requirement("python", "requests", ">= 2.0")      → "python3-requests >= 2.0"
        get_rpm_requirement("python", "requests", ">= 2.0, < 3") → "(python3-requests >= 2.0 with python3-requests < 3)"
        get_rpm_requirement("python", "requests")                 → "python3-requests"

    Node.js 示例：
        get_rpm_requirement("nodejs", "lodash", ">= 4.0")         → "nodejs-lodash >= 4.0"

    Java 示例（保持 mvn() 表达式，由 analyze_java_deps 处理）：
        get_rpm_requirement("java", "org.apache:commons-lang3")    → "org.apache:commons-lang3"
    """
    pkg = get_rpm_pkg_name(lang, upstream_name)
    if not constraint:
        return pkg

    parts = [c.strip() for c in constraint.split(",") if c.strip()]
    parts = [re.sub(r"([><=!~]+)\s*", r"\1 ", p).strip() for p in parts]

    if len(parts) == 1:
        return f"{pkg} {parts[0]}"
    else:
        expr = " with ".join(f"{pkg} {p}" for p in parts)
        return f"({expr})"


def get_compat_srpm_name(lang: str, upstream_name: str, major_version: str) -> str:
    """
    返回 compat 包的 SRPM 名（spec Name: 字段），格式：<原SRPM名>-<主版本号>

    Python 示例：
        get_compat_srpm_name("python", "beautifulsoup4", "4.12") → "python-beautifulsoup4-4.12"
        get_compat_srpm_name("python", "protobuf", "5")          → "python-protobuf-5"
    """
    base = get_srpm_name(lang, upstream_name)
    return f"{base}-{major_version}"


def get_compat_rpm_pkg_name(lang: str, upstream_name: str, major_version: str) -> str:
    """
    返回 compat 包的二进制 RPM 包名，格式：<原包名>-<主版本号>

    Python 示例：
        get_compat_rpm_pkg_name("python", "beautifulsoup4", "4.12") → "python3-beautifulsoup4-4.12"
        get_compat_rpm_pkg_name("python", "protobuf", "5")          → "python3-protobuf-5"
    """
    base = get_rpm_pkg_name(lang, upstream_name)
    return f"{base}-{major_version}"


def extract_compat_major_version(version: str) -> str:
    """
    从完整版本号提取 compat 主版本标识（major.minor 或仅 major）。

    规则：
    - 版本号 >= 1.0：取 major.minor（如 4.12.3 → 4.12）
    - 大版本跳跃（major 变化大）：仅取 major（如 5.27.3 → 5）

    示例：
        extract_compat_major_version("4.12.3")  → "4.12"
        extract_compat_major_version("5.27.3")  → "5"
        extract_compat_major_version("1.5.0")   → "1.5"
        extract_compat_major_version("2024.1")  → "2024"
    """
    parts = version.lstrip("v").split(".")
    if not parts:
        return version
    try:
        major = int(parts[0])
    except ValueError:
        return parts[0]
    # major >= 100（如日期版本 2024.1）或 major >= 10：只取 major
    if major >= 10:
        return str(major)
    # 否则取 major.minor
    if len(parts) >= 2:
        return f"{parts[0]}.{parts[1]}"
    return parts[0]


def rpm_name_from_gav(name: str) -> str:
    """Maven GAV / mvn() provides 名 → RPM 名（artifactId）。

    'com.google.j2objc:j2objc-annotations'   → 'j2objc-annotations'
    'mvn(org.jspecify:jspecify)'             → 'jspecify'
    'j2objc-annotations'（非 GAV）            → 原样返回

    用于 dep_registry 注册 key 归一化与包名比对，防止同一依赖以
    GAV 名和简单名重复注册（Guava session 曾因此出现 6 条目 / 3 真依赖）。
    """
    n = name.strip()
    # 剥 mvn(...) provides 包裹
    if n.startswith("mvn(") and n.endswith(")"):
        n = n[4:-1]
    if ":" in n:
        n = n.split(":")[-1]
    return n


def upstream_from_srpm_name(srpm_name: str, lang: str = "python") -> str:
    """
    从 SRPM/RPM 包名还原上游 PyPI/npm 等注册表中的原始名称。
    是 get_srpm_name() 的逆操作。

    Python 示例：
        upstream_from_srpm_name("python3-setuptools")  → "setuptools"
        upstream_from_srpm_name("python-setuptools")   → "setuptools"
        upstream_from_srpm_name("python3-Django")       → "Django"

    Node.js 示例：
        upstream_from_srpm_name("nodejs-lodash", "nodejs") → "lodash"

    未匹配任何已知前缀时原样返回，兼容已无前缀的名称和未知语言。
    """
    lang = lang.lower()
    if lang == "python":
        # python3- 必须在 python- 之前，避免 python3-xxx 被 python- 抢先匹配成 3-xxx
        for prefix in ["python3-", "python-"]:
            if srpm_name.startswith(prefix):
                return srpm_name[len(prefix):]
    elif lang == "nodejs":
        if srpm_name.startswith("nodejs-"):
            return srpm_name[len("nodejs-"):]
    return srpm_name


def rpm_name_from_pep508(pkg_spec: str) -> str:
    """
    从 PEP 508 依赖规范直接生成 Python RPM Requires 表达式。

    'requests>=2.0,<3'       → '(python3-requests >= 2.0 with python3-requests < 3)'
    'python-dateutil>=2.7.0' → 'python3-python-dateutil >= 2.7.0'
    'click'                  → 'python3-click'
    """
    spec = pkg_spec.split(";")[0].strip()
    spec = re.sub(r"\[.*?\]", "", spec)
    m = re.match(r"([a-zA-Z0-9_\-.]+)", spec.strip())
    if not m:
        return ""
    upstream_name = m.group(1)
    version_part = spec[len(m.group(1)):].strip().strip("()")
    return get_rpm_requirement("python", upstream_name, version_part)
