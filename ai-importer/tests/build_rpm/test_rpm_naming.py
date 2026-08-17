"""rpm_naming.py — RPM 包命名统一模块(纯逻辑,10 个函数全测)。"""

from __future__ import annotations

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

rn = load_module("rpm_naming", SCRIPT_DIRS["build_rpm"] / "rpm_naming.py")


# ─────────────────────────────────────────────
# _normalize
# ─────────────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("requests", "requests"),
    ("Django", "django"),
    ("python_foo", "python-foo"),
    ("a.b.c", "a-b-c"),
    ("Foo-Bar", "foo-bar"),
    ("a--b", "a-b"),
])
def test_normalize(name, expected):
    assert rn._normalize(name) == expected


# ─────────────────────────────────────────────
# get_srpm_name
# ─────────────────────────────────────────────

@pytest.mark.parametrize("lang,name,expected", [
    ("python", "requests", "python-requests"),
    ("python", "python-multipart", "python-python-multipart"),
    ("python", "Django", "python-django"),
    ("PYTHON", "requests", "python-requests"),   # 语言大小写归一
    ("nodejs", "lodash", "nodejs-lodash"),
    ("go", "github.com/foo/bar", "github.com/foo/bar"),  # 无前缀
    ("rust", "serde", "serde"),
    ("java", "org.apache:commons-lang3", "org.apache:commons-lang3"),
])
def test_get_srpm_name(lang, name, expected):
    assert rn.get_srpm_name(lang, name) == expected


# ─────────────────────────────────────────────
# get_rpm_pkg_name
# ─────────────────────────────────────────────

@pytest.mark.parametrize("lang,name,expected", [
    ("python", "requests", "python3-requests"),
    ("python", "python-multipart", "python3-python-multipart"),
    ("python", "Django", "python3-django"),
    ("nodejs", "lodash", "nodejs-lodash"),
    ("nodejs", "@scope/pkg", "nodejs-scope-pkg"),
    ("nodejs", "@babel/core", "nodejs-babel-core"),
    ("c", "libssl", "libssl"),
    ("cpp", "fmt", "fmt"),
    ("go", "cobra", "cobra"),
    ("java", "org.apache:commons-lang3", "org.apache:commons-lang3"),
])
def test_get_rpm_pkg_name(lang, name, expected):
    assert rn.get_rpm_pkg_name(lang, name) == expected


# ─────────────────────────────────────────────
# get_rpm_requirement
# ─────────────────────────────────────────────

@pytest.mark.parametrize("lang,name,constraint,expected", [
    ("python", "requests", "", "python3-requests"),
    ("python", "requests", ">= 2.0", "python3-requests >= 2.0"),
    ("python", "requests", ">=2.0,<3", "(python3-requests >= 2.0 with python3-requests < 3)"),
    ("python", "requests", ">=2.0, !=1.5", "(python3-requests >= 2.0 with python3-requests != 1.5)"),
    ("nodejs", "lodash", ">= 4.0", "nodejs-lodash >= 4.0"),
    ("python", "requests", "~=3.8", "python3-requests ~= 3.8"),
    ("java", "org.apache:commons-lang3", "", "org.apache:commons-lang3"),
])
def test_get_rpm_requirement(lang, name, constraint, expected):
    assert rn.get_rpm_requirement(lang, name, constraint) == expected


# ─────────────────────────────────────────────
# compat 命名
# ─────────────────────────────────────────────

def test_get_compat_srpm_name():
    assert rn.get_compat_srpm_name("python", "beautifulsoup4", "4.12") == "python-beautifulsoup4-4.12"
    assert rn.get_compat_srpm_name("python", "protobuf", "5") == "python-protobuf-5"
    assert rn.get_compat_srpm_name("nodejs", "lodash", "4") == "nodejs-lodash-4"


def test_get_compat_rpm_pkg_name():
    assert rn.get_compat_rpm_pkg_name("python", "beautifulsoup4", "4.12") == "python3-beautifulsoup4-4.12"
    assert rn.get_compat_rpm_pkg_name("python", "protobuf", "5") == "python3-protobuf-5"


# ─────────────────────────────────────────────
# extract_compat_major_version
# ─────────────────────────────────────────────

@pytest.mark.parametrize("version,expected", [
    ("4.12.3", "4.12"),     # 普通版本取 major.minor
    ("5.27.3", "5.27"),     # 注意:docstring 示例写 "5",但代码 major>=10 才截断,5<10 取 major.minor
    ("1.5.0", "1.5"),
    ("2024.1", "2024"),     # 日期版本只取 major
    ("10.1", "10"),
    ("9.1", "9.1"),
    ("v2.3", "2.3"),        # 剥 v 前缀
    ("v11.0", "11"),
    ("abc", "abc"),         # 非数字开头原样
    ("1", "1"),
])
def test_extract_compat_major_version(version, expected):
    assert rn.extract_compat_major_version(version) == expected


# ─────────────────────────────────────────────
# rpm_name_from_gav
# ─────────────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("com.google.j2objc:j2objc-annotations", "j2objc-annotations"),
    ("mvn(org.jspecify:jspecify)", "jspecify"),
    ("j2objc-annotations", "j2objc-annotations"),   # 非 GAV 原样
    ("mvn(com.fasterxml.jackson.core:jackson-databind)", "jackson-databind"),
    ("  org.slf4j:slf4j-api  ", "slf4j-api"),       # 首尾空白
])
def test_rpm_name_from_gav(name, expected):
    assert rn.rpm_name_from_gav(name) == expected


# ─────────────────────────────────────────────
# upstream_from_srpm_name
# ─────────────────────────────────────────────

@pytest.mark.parametrize("name,lang,expected", [
    ("python3-setuptools", "python", "setuptools"),
    ("python-setuptools", "python", "setuptools"),
    ("python3-Django", "python", "Django"),
    ("python3-python-multipart", "python", "python-multipart"),
    ("python-foo", "python", "foo"),
    ("nodejs-lodash", "nodejs", "lodash"),
    ("no-prefix", "python", "no-prefix"),           # 未知前缀原样
    ("python3-foo", "nodejs", "python3-foo"),       # 语言不匹配原样
])
def test_upstream_from_srpm_name(name, lang, expected):
    assert rn.upstream_from_srpm_name(name, lang) == expected


# ─────────────────────────────────────────────
# rpm_name_from_pep508
# ─────────────────────────────────────────────

@pytest.mark.parametrize("spec,expected", [
    ("requests>=2.0,<3", "(python3-requests >= 2.0 with python3-requests < 3)"),
    ("python-dateutil>=2.7.0", "python3-python-dateutil >= 2.7.0"),
    ("click", "python3-click"),
    ("requests[security]>=2.0", "python3-requests >= 2.0"),   # extras 剥离
    ("requests>=2.0; python_version<'3.8'", "python3-requests >= 2.0"),  # marker 剥离
    ("  flask  >=1.0 ", "python3-flask >= 1.0"),
    ("", ""),           # 解析不出 → 空
])
def test_rpm_name_from_pep508(spec, expected):
    assert rn.rpm_name_from_pep508(spec) == expected
