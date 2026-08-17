r"""review_spec.py — RPM spec 静态审查脚本测试(纯字符串/文件系统逻辑,无真实 rpmbuild/rpmlint)。

覆盖:
- check_spec:19 个 rule_id(F-01..C-02)逐条参数化,每条规则 ≥1 命中 + ≥1 不命中,
  命中时断言完整 issue dict(rule_id/severity/location/message/suggestion)。
- check_rpmlint:行解析、8 条 rule_map 映射、4 类已知误报降级为 I、非 [EW] 行丢弃。
- check_final:AR-01..AR-05 归档完整性(tmp_path,纯 Path 操作)。
- determine_verdict / generate_report:PASS/WARN/BLOCK 与 fixed/still/new 对比输出。
- main():CLI 三阶段(spec/lint/final)与退出码(monkeypatch sys.argv)。

生产代码 quirks(测试按实际行为断言,不做修改):
1. F-02 `^Version:\s*\S+` 的 `\s*` 会跨行吞换行:"Version:" 后无值但后续还有行时
   不报 F-02(只有 Version 为末行才命中)。
2. B-01 只匹配 "BuildRequires: cmake" 单/双空格,制表符缩进的声明不被识别。
3. check_rpmlint 的正则只收 [EW] 行,rpmlint 自身 I 级输出被整体丢弃。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sys.path.insert(0, str(SCRIPT_DIRS["review"]))
mod = load_module("review_spec", SCRIPT_DIRS["review"] / "review_spec.py")

# ─────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────

DEBUG_NIL = "%global debug_package %{nil}"
SO_FILES = "%files\n%{_libdir}/libfoo.so.1"
CMAKEREQS = "BuildRequires: cmake\nBuildRequires: gcc-c++"
MESONREQS = "BuildRequires: meson\nBuildRequires: gcc-c++\nBuildRequires: ninja-build"


def mk(pkgname="foo", **over):
    """构造一份通过全部检查的基线 spec,按需覆写任一字段。

    各槽位对应 spec 的章节;置空字符串即删除该章节。"""
    parts = {
        "name": f"Name: {pkgname}",
        "version": "Version: 1.0.0",
        "release": "Release: 1%{?dist}",
        "license": "License: MIT",
        "source0": "Source0: %{name}-%{version}.tar.gz",
        "buildreqs": "",
        "build": "",
        "install": "",
        "buildarch": "",
        "debug": "",
        "devel": "",
        "files": "%files\n%license LICENSE",
        "post": "",
        "postun": "",
        "changelog": "%changelog\n* Mon Jan 06 2025 tester <t@example.com> - 1.0.0-1\n- init",
    }
    parts.update(over)
    return "\n".join(v for v in parts.values() if v)


def by_id(issues, rid):
    return [i for i in issues if i["rule_id"] == rid]


def issue(rid="LINT", sev="E", loc="loc", msg="msg", sug=""):
    return {"rule_id": rid, "severity": sev, "location": loc,
            "message": msg, "suggestion": sug}


# ─────────────────────────────────────────────
# check_spec — 基线 spec 零问题
# ─────────────────────────────────────────────

def test_check_spec_valid_spec_is_clean():
    assert mod.check_spec(mk(), "foo") == []
    assert mod.check_spec(mk(pkgname="bar"), "bar") == []


# ─────────────────────────────────────────────
# check_spec — §1 基础字段 F-01..F-06(命中:断言完整 dict)
# ─────────────────────────────────────────────

@pytest.mark.parametrize("over,pkgname,expected", [
    pytest.param(
        {"name": ""}, "foo",
        issue("F-01", "E", "Name", "缺少 Name 字段", "添加 Name 字段"),
        id="F-01-name-missing"),
    pytest.param(
        {"name": "Name: bar"}, "foo",
        issue("F-01", "E", "Name", "Name 字段 'bar' 与包名 'foo' 不一致",
              "修改 Name 字段与包目录名一致"),
        id="F-01-name-mismatch"),
    pytest.param(
        {"version": ""}, "foo",
        issue("F-02", "E", "Version", "缺少 Version 字段", "添加 Version 字段"),
        id="F-02-version-missing"),
    pytest.param(
        {"release": ""}, "foo",
        issue("F-03", "E", "Release", "缺少 Release 字段", "添加 Release: 1%{?dist}"),
        id="F-03-release-missing"),
    pytest.param(
        {"release": "Release: 2%{?dist}"}, "foo",
        issue("F-03", "E", "Release",
              "Release 字段应为 '1%{?dist}'，当前为 '2%{?dist}'", "改为 1%{?dist}"),
        id="F-03-release-wrong"),
    pytest.param(
        {"license": ""}, "foo",
        issue("F-04", "E", "License", "缺少 License 字段", "添加 SPDX 标识符"),
        id="F-04-license-missing"),
    pytest.param(
        {"source0": "Source0: https://example.com/foo.tar.xz"}, "foo",
        issue("F-06", "E", "Source0",
              "Source0 应为 '%{name}-%{version}.tar.gz'，当前为 'https://example.com/foo.tar.xz'",
              "改为 %{name}-%{version}.tar.gz"),
        id="F-06-source0-wrong"),
])
def test_check_spec_basic_fields_hit(over, pkgname, expected):
    issues = mod.check_spec(mk(pkgname=pkgname, **over), pkgname)
    assert issues == [expected]


# ─────────────────────────────────────────────
# check_spec — §2 BuildRequires B-01 / B-02
# ─────────────────────────────────────────────

def test_b01_cmake_missing_both():
    assert mod.check_spec(mk(build="%build\n%cmake"), "foo") == [
        issue("B-01", "E", "BuildRequires", "CMake 项目缺少 BuildRequires: cmake",
              "添加 BuildRequires: cmake"),
        issue("B-01", "E", "BuildRequires", "CMake 项目缺少 BuildRequires: gcc-c++",
              "添加 BuildRequires: gcc-c++"),
    ]


def test_b01_cmake_missing_gcc_only():
    spec = mk(build="%build\n%cmake", buildreqs="BuildRequires: cmake")
    assert mod.check_spec(spec, "foo") == [
        issue("B-01", "E", "BuildRequires", "CMake 项目缺少 BuildRequires: gcc-c++",
              "添加 BuildRequires: gcc-c++"),
    ]


def test_b01_double_space_variant_accepted():
    # 两空格变体 "BuildRequires:  cmake" 也是合法声明
    spec = mk(build="%build\n%cmake",
              buildreqs="BuildRequires:  cmake\nBuildRequires:  gcc-c++")
    assert by_id(mod.check_spec(spec, "foo"), "B-01") == []


def test_b02_meson_missing_all():
    expected = [
        issue("B-02", "E", "BuildRequires", f"Meson 项目缺少 BuildRequires: {req}",
              f"添加 BuildRequires: {req}")
        for req in ("meson", "gcc-c++", "ninja-build")
    ]
    assert mod.check_spec(mk(build="%build\n%meson"), "foo") == expected


def test_b02_meson_missing_ninja_only():
    spec = mk(build="%build\n%meson",
              buildreqs="BuildRequires: meson\nBuildRequires: gcc-c++")
    assert mod.check_spec(spec, "foo") == [
        issue("B-02", "E", "BuildRequires", "Meson 项目缺少 BuildRequires: ninja-build",
              "添加 BuildRequires: ninja-build"),
    ]


def test_b01_tab_indent_not_recognized():
    # quirk: B-01 只匹配单/双空格,制表符缩进的 "BuildRequires:\tcmake" 不被识别,
    # 仍会报缺少 cmake(实际行为,不做修改)
    spec = mk(build="%build\n%cmake",
              buildreqs="BuildRequires:\tcmake\nBuildRequires: gcc-c++")
    assert mod.check_spec(spec, "foo") == [
        issue("B-01", "E", "BuildRequires", "CMake 项目缺少 BuildRequires: cmake",
              "添加 BuildRequires: cmake"),
    ]


# ─────────────────────────────────────────────
# check_spec — §3 分包规则 H-02 / H-03 / P-05 / P-06
# ─────────────────────────────────────────────

def test_h02_header_only_empty_main_package():
    spec = mk(debug=DEBUG_NIL, files="%files\n%files devel\n%{_includedir}/foo.h")
    assert mod.check_spec(spec, "foo") == [
        issue("H-02", "E", "%files",
              "header-only 库存在空主包，会触发 no-binary E 错误",
              "删除空的 %files 主包段落"),
    ]


def test_h03_header_only_devel_requires_name():
    spec = mk(debug=DEBUG_NIL,
              devel="%package devel\nSummary: dev\nRequires: %{name}")
    assert mod.check_spec(spec, "foo") == [
        issue("H-03", "E", "%package devel",
              "header-only 库的 -devel 包不应声明 Requires: %{name}（没有主包）",
              "删除该 Requires 行"),
    ]


def test_p06_shared_lib_missing_both_ldconfig():
    assert mod.check_spec(mk(files=SO_FILES), "foo") == [
        issue("P-06", "E", "%post", "共享库缺少 %post -p /sbin/ldconfig",
              "添加 %post -p /sbin/ldconfig"),
        issue("P-06", "E", "%postun", "共享库缺少 %postun -p /sbin/ldconfig",
              "添加 %postun -p /sbin/ldconfig"),
    ]


def test_p06_shared_lib_missing_postun_only():
    spec = mk(files=SO_FILES, post="%post -p /sbin/ldconfig")
    assert mod.check_spec(spec, "foo") == [
        issue("P-06", "E", "%postun", "共享库缺少 %postun -p /sbin/ldconfig",
              "添加 %postun -p /sbin/ldconfig"),
    ]


def test_p05_devel_missing_requires_name():
    spec = mk(files=SO_FILES,
              post="%post -p /sbin/ldconfig",
              postun="%postun -p /sbin/ldconfig",
              devel="%package devel\nSummary: dev\nRequires: libbar")
    assert mod.check_spec(spec, "foo") == [
        issue("P-05", "E", "%package devel",
              "-devel 包缺少 Requires: %{name}（或 %{name}%{?_isa}）= %{version}-%{release}",
              "添加 Requires: %{name}%{?_isa} = %{version}-%{release}"),
    ]


# ─────────────────────────────────────────────
# check_spec — §3.3 noarch vs arch A-01 / A-02 / A-03
# ─────────────────────────────────────────────

def test_a01_noarch_with_libdir_cmake():
    spec = mk(buildarch="BuildArch: noarch", files="%files\n%{_libdir}/cmake/foo.cmake")
    assert mod.check_spec(spec, "foo") == [
        issue("A-01", "E", "BuildArch / %files",
              "cmake 文件在 %{_libdir}/cmake/ 但声明了 BuildArch: noarch，会触发 noarch-with-lib64",
              "去掉 BuildArch: noarch"),
    ]


def test_a01_buildarch_six_space_variant():
    spec = mk(buildarch="BuildArch:      noarch",
              files="%files\n%{_libdir}/cmake/foo.cmake")
    assert len(by_id(mod.check_spec(spec, "foo"), "A-01")) == 1


def test_a03_noarch_with_libdir_pkgconfig():
    spec = mk(buildarch="BuildArch: noarch",
              files="%files\n%{_libdir}/pkgconfig/foo.pc")
    assert mod.check_spec(spec, "foo") == [
        issue("A-03", "E", "BuildArch / %files",
              "pkgconfig 文件在 %{_libdir}/pkgconfig/ 但声明了 BuildArch: noarch，会触发 noarch-with-lib64",
              "去掉 BuildArch: noarch"),
    ]


def test_a02_header_only_datadir_cmake_suggests_noarch():
    spec = mk(debug=DEBUG_NIL, files="%files\n%{_datadir}/cmake/foo")
    assert mod.check_spec(spec, "foo") == [
        issue("A-02", "W", "BuildArch",
              "cmake 文件在 %{_datadir}/cmake/，header-only 库建议声明 BuildArch: noarch",
              "添加 BuildArch: noarch"),
    ]


# ─────────────────────────────────────────────
# check_spec — §4/§5 %build/%install BD-01 / BD-02 / I-01
# ─────────────────────────────────────────────

def test_bd01_handwritten_cmake_dotdot():
    spec = mk(build="%build\n%cmake\ncmake ..", buildreqs=CMAKEREQS)
    assert mod.check_spec(spec, "foo") == [
        issue("BD-01", "E", "%build", "不得手写 cmake ..，应使用 %cmake 宏",
              "改用 %cmake 宏"),
    ]


def test_bd02_handwritten_meson_setup():
    spec = mk(build="%build\n%meson\nmeson setup _build", buildreqs=MESONREQS)
    assert mod.check_spec(spec, "foo") == [
        issue("BD-02", "E", "%build", "不得手写 meson setup，应使用 %meson 宏",
              "改用 %meson 宏"),
    ]


def test_i01_handwritten_make_install():
    spec = mk(build="%build\n%cmake", install="%install\nmake install",
              buildreqs=CMAKEREQS)
    assert mod.check_spec(spec, "foo") == [
        issue("I-01", "E", "%install",
              "CMake 项目不得手写 make install，应使用 %cmake_install",
              "改用 %cmake_install"),
    ]


# ─────────────────────────────────────────────
# check_spec — §7 %changelog C-01 / C-02
# ─────────────────────────────────────────────

def test_c01_changelog_missing():
    assert mod.check_spec(mk(changelog=""), "foo") == [
        issue("C-01", "E", "%changelog", "缺少 %changelog 段落", "添加 %changelog 段落"),
    ]


def test_c01_changelog_empty():
    assert mod.check_spec(mk(changelog="%changelog"), "foo") == [
        issue("C-01", "E", "%changelog", "%changelog 段落为空，至少需要一条条目",
              "添加初始 changelog 条目"),
    ]


def test_c02_changelog_bad_date_format():
    spec = mk(changelog="%changelog\n* 2025-01-06 tester <t@example.com> - 1.0.0-1\n- init")
    assert mod.check_spec(spec, "foo") == [
        issue("C-02", "E", "%changelog",
              "changelog 日期格式不正确：'* 2025-01-06 tester <t@example.com> - 1.0.0-1'",
              "格式应为 '* Www Mon DD YYYY'"),
    ]


# ─────────────────────────────────────────────
# check_spec — 各规则不命中(每规则至少 1 个负例)
# ─────────────────────────────────────────────

@pytest.mark.parametrize("over,rid", [
    pytest.param({}, "F-01", id="F-01-name-ok"),
    pytest.param({"name": "Name: foo"}, "F-01", id="F-01-name-matches"),
    pytest.param({}, "F-02", id="F-02-version-ok"),
    pytest.param({}, "F-03", id="F-03-release-ok"),
    pytest.param({}, "F-04", id="F-04-license-ok"),
    pytest.param({}, "F-06", id="F-06-source0-ok"),
    pytest.param({"source0": ""}, "F-06", id="F-06-no-source0-no-complaint"),
    pytest.param({"build": "%build\n%cmake", "buildreqs": CMAKEREQS}, "B-01",
                 id="B-01-cmake-reqs-ok"),
    pytest.param({"build": "%build\n%meson", "buildreqs": MESONREQS}, "B-02",
                 id="B-02-meson-reqs-ok"),
    pytest.param({"debug": DEBUG_NIL, "files": "%files\n%{_includedir}/foo.h"}, "H-02",
                 id="H-02-main-has-content"),
    pytest.param({"debug": DEBUG_NIL, "devel": "%package devel\nSummary: dev"}, "H-03",
                 id="H-03-devel-no-requires"),
    pytest.param({"files": SO_FILES,
                  "post": "%post -p /sbin/ldconfig",
                  "postun": "%postun -p /sbin/ldconfig"}, "P-06",
                 id="P-06-ldconfig-present"),
    pytest.param({"files": SO_FILES,
                  "post": "%post -p /sbin/ldconfig",
                  "postun": "%postun -p /sbin/ldconfig",
                  "devel": "%package devel\nRequires: %{name}%{?_isa} = %{version}-%{release}"},
                 "P-05", id="P-05-devel-requires-ok"),
    pytest.param({"files": "%files\n%{_libdir}/cmake/foo.cmake"}, "A-01",
                 id="A-01-not-noarch"),
    pytest.param({"files": "%files\n%{_libdir}/pkgconfig/foo.pc"}, "A-03",
                 id="A-03-not-noarch"),
    pytest.param({"debug": DEBUG_NIL, "buildarch": "BuildArch: noarch",
                  "files": "%files\n%{_datadir}/cmake/foo"}, "A-02",
                 id="A-02-already-noarch"),
    pytest.param({"files": "%files\n%{_datadir}/cmake/foo"}, "A-02",
                 id="A-02-not-header-only"),
    pytest.param({"debug": DEBUG_NIL,
                  "files": "%files\n%{_libdir}/cmake/foo\n%{_datadir}/cmake/foo"}, "A-02",
                 id="A-02-has-libdir-cmake"),
    pytest.param({"build": "%build\n%cmake", "buildreqs": CMAKEREQS}, "BD-01",
                 id="BD-01-cmake-macro-only"),
    pytest.param({"build": "%build\n%meson", "buildreqs": MESONREQS}, "BD-02",
                 id="BD-02-meson-macro-only"),
    pytest.param({"build": "%build\n%cmake", "install": "%install\n%cmake_install",
                  "buildreqs": CMAKEREQS}, "I-01", id="I-01-cmake-install-macro"),
    pytest.param({}, "C-01", id="C-01-changelog-ok"),
    pytest.param({}, "C-02", id="C-02-date-format-ok"),
])
def test_check_spec_no_issue(over, rid):
    spec = mk(**over)
    assert by_id(mod.check_spec(spec, "foo"), rid) == []


# ─────────────────────────────────────────────
# check_spec — 生产代码 quirk:F-02 跨行吞换行
# ─────────────────────────────────────────────

def test_f02_version_value_missing_quirk():
    # quirk: `^Version:\s*\S+` 的 \s* 可跨行,"Version:" 无值但后续还有行时不报 F-02
    # (实际行为,不做修改);仅当 Version 为末行时才命中。
    followed = "Name: foo\nVersion:\nRelease: 1%{?dist}\n"
    assert by_id(mod.check_spec(followed, "foo"), "F-02") == []
    last_line = "Name: foo\nVersion:"
    assert by_id(mod.check_spec(last_line, "foo"), "F-02")


# ─────────────────────────────────────────────
# check_rpmlint — rule_map 解析
# ─────────────────────────────────────────────

@pytest.mark.parametrize("line,expected", [
    pytest.param("foo.x86_64: E: no-binary",
                 issue("H-02", "E", "foo.x86_64", "no-binary",
                       "加 BuildArch: noarch 或删除空主包"),
                 id="no-binary"),
    pytest.param("foo.x86_64: E: no-binary This package is empty",
                 issue("H-02", "E", "foo.x86_64", "no-binary This package is empty",
                       "加 BuildArch: noarch 或删除空主包"),
                 id="no-binary-with-detail"),
    pytest.param("foo: E: noarch-with-lib64",
                 issue("A-01", "E", "foo", "noarch-with-lib64", "去掉 BuildArch: noarch"),
                 id="noarch-with-lib64"),
    pytest.param("foo: E: devel-file-in-non-devel-package /usr/include/foo.h",
                 issue("P-03", "E", "foo",
                       "devel-file-in-non-devel-package /usr/include/foo.h",
                       "将头文件/pkgconfig 移到 -devel 包"),
                 id="devel-file-in-non-devel-package"),
    pytest.param("foo: E: non-versioned-file-in-library-package",
                 issue("P-07", "E", "foo", "non-versioned-file-in-library-package",
                       "将 doc/license 移出主包"),
                 id="non-versioned-file-in-library-package"),
    pytest.param("foo: E: library-without-ldconfig",
                 issue("P-06", "E", "foo", "library-without-ldconfig",
                       "添加 %post/%postun -p /sbin/ldconfig"),
                 id="library-without-ldconfig"),
    pytest.param("foo: E: static-library-without-debuginfo",
                 issue("BD-04", "E", "foo", "static-library-without-debuginfo",
                       "禁用静态库或加 %global debug_package %{nil}"),
                 id="static-library-without-debuginfo"),
    pytest.param("foo: W: spelling-error Summary foobar",
                 issue("F-08", "W", "foo", "spelling-error Summary foobar",
                       "修改描述中的拼写"),
                 id="spelling-error-w"),
    pytest.param("foo: E: non-standard-dir-in-usr /usr/foo",
                 issue("AR-05", "E", "foo", "non-standard-dir-in-usr /usr/foo",
                       "上游安装行为，记录但不阻断"),
                 id="non-standard-dir-in-usr"),
    pytest.param("foo: E: unknown-error-xyz whatever",
                 issue("LINT", "E", "foo", "unknown-error-xyz whatever", ""),
                 id="unknown-error"),
])
def test_check_rpmlint_rule_map(line, expected):
    assert mod.check_rpmlint(line) == [expected]


# ─────────────────────────────────────────────
# check_rpmlint — 已知误报降级为 I
# ─────────────────────────────────────────────

@pytest.mark.parametrize("line", [
    "foo: E: no-signature",
    "foo: E: invalid-license MIT",
    "foo: E: missing-hash-section",
    "foo: E: no-library-dependency-for /usr/lib/libfoo.so",
    "foo: W: no-signature",
])
def test_check_rpmlint_false_positive_downgraded_to_i(line):
    issues = mod.check_rpmlint(line)
    assert len(issues) == 1
    assert issues[0]["severity"] == "I"      # E/W 降级为 I
    assert issues[0]["rule_id"] == "LINT"    # 误报不映射具体规则
    assert issues[0]["suggestion"] == ""


# ─────────────────────────────────────────────
# check_rpmlint — 跳过无法解析的行
# ─────────────────────────────────────────────

@pytest.mark.parametrize("line", [
    "",
    "   ",
    "some random text",
    "foo: I: no-signature",   # quirk: 正则只收 [EW],rpmlint 自身 I 级输出被丢弃
    "E: no-binary",           # 无包名前缀,解析不出
])
def test_check_rpmlint_skips_unparseable(line):
    assert mod.check_rpmlint(line) == []


def test_check_rpmlint_multiline_and_blank_lines():
    out = "\nfoo: E: no-binary\n\nbar: W: spelling-error x y\n"
    issues = mod.check_rpmlint(out)
    assert [(i["rule_id"], i["severity"]) for i in issues] == [
        ("H-02", "E"), ("F-08", "W"),
    ]


# ─────────────────────────────────────────────
# check_final — 归档完整性 AR-01..AR-05
# ─────────────────────────────────────────────

def _make_dist(tmp_path, rpms=(), srpms=(), repodata=False):
    dist = tmp_path / "dist"
    dist.mkdir()
    for name in rpms:
        (dist / name).write_text("")
    for name in srpms:
        (dist / name).write_text("")
    if repodata:
        (dist / "repodata").mkdir()
    return dist


def _make_spec(tmp_path, text="Name: foo\nVersion: 1.0.0\n"):
    spec = tmp_path / "foo" / "foo.spec"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text(text)
    return spec


def test_check_final_empty_dist(tmp_path):
    dist = _make_dist(tmp_path)
    issues = mod.check_final("foo", str(dist), str(tmp_path / "foo" / "foo.spec"))
    assert len(issues) == 4
    assert by_id(issues, "AR-01") == [
        issue("AR-01", "E", "dist/foo*.rpm",
              "dist/ 目录中找不到 foo 的 binary RPM",
              "确认 rpmbuild 成功并已复制 RPM 到 dist/")]
    assert by_id(issues, "AR-02") == [
        issue("AR-02", "E", "dist/foo*.src.rpm",
              "dist/ 目录中找不到 foo 的 source RPM", "复制 SRPM 到 dist/")]
    assert by_id(issues, "AR-03") == [
        issue("AR-03", "E", str(tmp_path / "foo" / "foo.spec"),
              f"spec 文件不存在：{tmp_path / 'foo' / 'foo.spec'}",
              "确认 spec 已写入包目录")]
    assert by_id(issues, "AR-05") == [
        issue("AR-05", "W", "dist/repodata/",
              "repodata/ 目录不存在，可能未运行 createrepo", "运行 createrepo dist/")]


def test_check_final_complete_no_issues(tmp_path):
    dist = _make_dist(tmp_path,
                      rpms=["foo-1.0.0-1.x86_64.rpm"],
                      srpms=["foo-1.0.0-1.src.rpm"],
                      repodata=True)
    spec = _make_spec(tmp_path)
    assert mod.check_final("foo", str(dist), str(spec)) == []


def test_check_final_version_mismatch(tmp_path):
    dist = _make_dist(tmp_path,
                      rpms=["foo-9.9.9-1.x86_64.rpm"],
                      srpms=["foo-1.0.0-1.src.rpm"],
                      repodata=True)
    spec = _make_spec(tmp_path)
    issues = mod.check_final("foo", str(dist), str(spec))
    assert issues == [
        issue("AR-04", "W", "foo-9.9.9-1.x86_64.rpm",
              "RPM 文件名中的版本与 spec Version (1.0.0) 不一致",
              "检查版本号是否正确"),
    ]


def test_check_final_spec_without_version_line(tmp_path):
    # spec 存在但无 Version 行 → 不报 AR-04(无版本可比)
    dist = _make_dist(tmp_path,
                      rpms=["foo-1.0.0-1.x86_64.rpm"],
                      srpms=["foo-1.0.0-1.src.rpm"],
                      repodata=True)
    spec = _make_spec(tmp_path, text="Name: foo\n")
    assert by_id(mod.check_final("foo", str(dist), str(spec)), "AR-04") == []


def test_check_final_falsy_spec_path_skips_ar03(tmp_path):
    dist = _make_dist(tmp_path)
    issues = mod.check_final("foo", str(dist), "")
    assert len(issues) == 3
    assert by_id(issues, "AR-03") == []


# ─────────────────────────────────────────────
# determine_verdict
# ─────────────────────────────────────────────

@pytest.mark.parametrize("severities,expected", [
    ([], "PASS"),
    (["I"], "PASS"),
    (["W"], "WARN"),
    (["E"], "BLOCK"),
    (["W", "E"], "BLOCK"),
    (["I", "W"], "WARN"),
])
def test_determine_verdict(severities, expected):
    issues = [{"severity": s} for s in severities]
    assert mod.determine_verdict(issues) == expected


# ─────────────────────────────────────────────
# generate_report
# ─────────────────────────────────────────────

def test_generate_report_pass_empty():
    report = mod.generate_report("foo", "1.0", "spec", 1, "foo.spec", [])
    assert "**裁决：✅ `PASS`**" in report
    assert "无 E 级问题，可继续。" in report
    assert report.count("_无_") == 3          # E/W/I 三张表均为空
    assert "- 无规则违反" in report
    assert "| 包名 | foo |" in report
    assert "| 版本 | 1.0 |" in report
    assert "| 审查阶段 | spec |" in report
    assert "| 审查轮次 | 1 |" in report
    assert "| 输入文件 | `foo.spec` |" in report
    assert "## 与上轮对比" not in report


def test_generate_report_block_with_e_issue():
    issues = [issue("F-01", "E", "Name", "缺少 Name 字段", "添加 Name 字段")]
    report = mod.generate_report("foo", "1.0", "spec", 1, "foo.spec", issues)
    assert "**裁决：🚫 `BLOCK`**" in report
    assert "发现 1 个 E 级问题，必须修复后重新审查。" in report
    assert "| 1 | `Name` | 缺少 Name 字段 | 添加 Name 字段 |" in report
    assert "| # | 位置 | 问题描述 | 修复建议 |" in report
    assert "- 依据 `spec-review-rules.md § F-01`" in report


def test_generate_report_warn_with_w_issue():
    issues = [issue("A-02", "W", "BuildArch", "建议 noarch", "添加 BuildArch: noarch")]
    report = mod.generate_report("foo", "1.0", "spec", 1, "foo.spec", issues)
    assert "**裁决：⚠️ `WARN`**" in report
    assert "发现 1 个 W 级问题，建议修复，不阻断流程。" in report
    assert "- 依据 `spec-review-rules.md § A-02`" in report


def test_generate_report_i_issues_pass_but_listed():
    issues = [issue("LINT", "I", "pkg", "info msg", "")]
    report = mod.generate_report("foo", "1.0", "lint", 1, "rl.txt", issues)
    assert "**裁决：✅ `PASS`**" in report
    assert "| # | 位置 | 说明 |" in report           # I 表无建议列
    assert "| 1 | `pkg` | info msg |" in report


def test_generate_report_basis_sorted_and_excludes_lint():
    issues = [
        issue("F-01", "E", "Name", "m1", "s1"),
        issue("LINT", "E", "pkg", "m2", ""),
        issue("A-02", "W", "BuildArch", "m3", "s3"),
    ]
    report = mod.generate_report("foo", "1.0", "spec", 1, "foo.spec", issues)
    assert "spec-review-rules.md § LINT" not in report
    pos_a = report.index("- 依据 `spec-review-rules.md § A-02`")
    pos_f = report.index("- 依据 `spec-review-rules.md § F-01`")
    assert pos_a < pos_f                          # rule_id 排序


def test_generate_report_comparison_fixed_still_new():
    prev = [issue("F-01", "E", "Name", "old issue", "")]
    curr = [issue("F-01", "E", "Name", "old issue", ""),
            issue("C-01", "E", "%changelog", "new issue", "")]
    report = mod.generate_report("foo", "1.0", "spec", 2, "foo.spec", curr,
                                 prev_issues=prev)
    assert "## 与上轮对比" in report
    assert "| old issue | 存在 | E（仍存在） |" in report
    assert "| new issue | 不存在 | E（新增） |" in report


def test_generate_report_comparison_all_three_categories():
    prev = [issue("F-01", "E", "a", "fixed issue", ""),
            issue("F-02", "E", "b", "still issue", "")]
    curr = [issue("F-02", "E", "b", "still issue", ""),
            issue("F-03", "W", "c", "new issue", "")]
    report = mod.generate_report("foo", "1.0", "spec", 2, "foo.spec", curr,
                                 prev_issues=prev)
    assert "| fixed issue | 已修复 | ✓ 已修复 |" in report
    assert "| still issue | 存在 | E（仍存在） |" in report
    assert "| new issue | 不存在 | W（新增） |" in report


def test_generate_report_no_comparison_when_round_one():
    prev = [issue("F-01", "E", "a", "old issue", "")]
    report = mod.generate_report("foo", "1.0", "spec", 1, "foo.spec", [],
                                 prev_issues=prev)
    assert "## 与上轮对比" not in report


def test_generate_report_no_comparison_without_prev():
    report = mod.generate_report("foo", "1.0", "spec", 2, "foo.spec", [])
    assert "## 与上轮对比" not in report


# ─────────────────────────────────────────────
# main() — CLI 入口(monkeypatch argv)
# ─────────────────────────────────────────────

def _run_main(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exc:
        mod.main()
    return exc.value.code


def test_main_spec_pass(tmp_path, monkeypatch):
    spec = tmp_path / "foo.spec"
    spec.write_text(mk())
    out = tmp_path / "out.json"
    report_dir = tmp_path / "reports"
    code = _run_main(monkeypatch, [
        "review_spec.py", "foo", "spec", "--spec", str(spec),
        "-o", str(out), "--report-dir", str(report_dir)])
    assert code == 0
    data = json.loads(out.read_text())
    assert data["pkgname"] == "foo"
    assert data["version"] == "1.0.0"
    assert data["stage"] == "spec"
    assert data["round"] == 1
    assert data["verdict"] == "PASS"
    assert data["issues"] == []
    assert (data["e_count"], data["w_count"], data["i_count"]) == (0, 0, 0)
    assert data["report_path"] == str(report_dir / "review_foo_spec.md")
    assert (report_dir / "review_foo_spec.md").exists()
    assert "**裁决：✅ `PASS`**" in (report_dir / "review_foo_spec.md").read_text()


def test_main_spec_block_exit_1(tmp_path, monkeypatch):
    spec = tmp_path / "foo.spec"
    spec.write_text(mk(license=""))       # 缺 License → E → BLOCK
    out = tmp_path / "out.json"
    code = _run_main(monkeypatch, [
        "review_spec.py", "foo", "spec", "--spec", str(spec),
        "-o", str(out), "--report-dir", str(tmp_path / "reports")])
    assert code == 1
    data = json.loads(out.read_text())
    assert data["verdict"] == "BLOCK"
    assert data["e_count"] == 1


def test_main_spec_missing_file_exit_1(tmp_path, monkeypatch):
    out = tmp_path / "out.json"
    code = _run_main(monkeypatch, [
        "review_spec.py", "foo", "spec", "--spec", str(tmp_path / "nope.spec"),
        "-o", str(out), "--report-dir", str(tmp_path / "reports")])
    assert code == 1
    assert not out.exists()


def test_main_lint_with_rpmlint_and_spec(tmp_path, monkeypatch):
    spec = tmp_path / "foo.spec"
    spec.write_text(mk())
    rl = tmp_path / "rpmlint.txt"
    rl.write_text("foo.x86_64: E: no-binary\n")
    out = tmp_path / "out.json"
    code = _run_main(monkeypatch, [
        "review_spec.py", "foo", "lint", "--spec", str(spec),
        "--rpmlint", str(rl), "-o", str(out),
        "--report-dir", str(tmp_path / "reports")])
    assert code == 1
    data = json.loads(out.read_text())
    assert data["verdict"] == "BLOCK"
    assert [i["rule_id"] for i in data["issues"]] == ["H-02"]


def test_main_lint_rpmlint_file_missing_warns(tmp_path, monkeypatch):
    spec = tmp_path / "foo.spec"
    spec.write_text(mk())
    out = tmp_path / "out.json"
    code = _run_main(monkeypatch, [
        "review_spec.py", "foo", "lint", "--spec", str(spec),
        "--rpmlint", str(tmp_path / "nope.txt"), "-o", str(out),
        "--report-dir", str(tmp_path / "reports")])
    assert code == 0                          # 仅警告,spec 干净 → PASS
    assert json.loads(out.read_text())["verdict"] == "PASS"


def test_main_final_complete(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)               # final 阶段按 CWD 相对路径找 ./foo/foo.spec
    _make_dist(tmp_path,
               rpms=["foo-1.0.0-1.x86_64.rpm"],
               srpms=["foo-1.0.0-1.src.rpm"],
               repodata=True)
    _make_spec(tmp_path)
    out = tmp_path / "out.json"
    code = _run_main(monkeypatch, [
        "review_spec.py", "foo", "final", "--dist-dir", str(tmp_path / "dist"),
        "-o", str(out), "--report-dir", str(tmp_path / "reports")])
    assert code == 0
    data = json.loads(out.read_text())
    assert data["verdict"] == "PASS"
    assert data["issues"] == []


def test_main_final_missing_repodata_warns(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_dist(tmp_path,
               rpms=["foo-1.0.0-1.x86_64.rpm"],
               srpms=["foo-1.0.0-1.src.rpm"])
    _make_spec(tmp_path)
    out = tmp_path / "out.json"
    code = _run_main(monkeypatch, [
        "review_spec.py", "foo", "final", "--dist-dir", str(tmp_path / "dist"),
        "-o", str(out), "--report-dir", str(tmp_path / "reports")])
    assert code == 0                          # W 不阻断
    data = json.loads(out.read_text())
    assert data["verdict"] == "WARN"
    assert data["w_count"] == 1


def test_main_final_empty_dist_blocks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_dist(tmp_path)
    _make_spec(tmp_path)
    out = tmp_path / "out.json"
    code = _run_main(monkeypatch, [
        "review_spec.py", "foo", "final", "--dist-dir", str(tmp_path / "dist"),
        "-o", str(out), "--report-dir", str(tmp_path / "reports")])
    assert code == 1
    data = json.loads(out.read_text())
    assert data["verdict"] == "BLOCK"
    assert {i["rule_id"] for i in data["issues"]} == {"AR-01", "AR-02", "AR-05"}


def test_main_prev_report_comparison(tmp_path, monkeypatch):
    spec = tmp_path / "foo.spec"
    spec.write_text(mk())                     # 本轮干净 → 上轮问题全部 "已修复"
    prev = tmp_path / "prev.json"
    prev.write_text(json.dumps({"issues": [
        issue("F-01", "E", "Name", "old issue", "s")]}))
    out = tmp_path / "out.json"
    report_dir = tmp_path / "reports"
    code = _run_main(monkeypatch, [
        "review_spec.py", "foo", "spec", "--spec", str(spec),
        "--round", "2", "--prev-report", str(prev),
        "-o", str(out), "--report-dir", str(report_dir)])
    assert code == 0
    report = (report_dir / "review_foo_spec.md").read_text()
    assert "## 与上轮对比" in report
    assert "| old issue | 已修复 | ✓ 已修复 |" in report


def test_main_prev_report_invalid_json_ignored(tmp_path, monkeypatch):
    spec = tmp_path / "foo.spec"
    spec.write_text(mk())
    prev = tmp_path / "prev.json"
    prev.write_text("not valid json{{{")       # 解析失败 → 静默忽略
    out = tmp_path / "out.json"
    report_dir = tmp_path / "reports"
    code = _run_main(monkeypatch, [
        "review_spec.py", "foo", "spec", "--spec", str(spec),
        "--round", "2", "--prev-report", str(prev),
        "-o", str(out), "--report-dir", str(report_dir)])
    assert code == 0
    report = (report_dir / "review_foo_spec.md").read_text()
    assert "## 与上轮对比" not in report


def test_main_final_with_spec_also_runs_check_spec(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_dist(tmp_path,
               rpms=["foo-1.0.0-1.x86_64.rpm"],
               srpms=["foo-1.0.0-1.src.rpm"],
               repodata=True)
    _make_spec(tmp_path)
    spec_arg = tmp_path / "arg.spec"
    spec_arg.write_text(mk())                 # final 阶段带 --spec 时同样跑 check_spec
    out = tmp_path / "out.json"
    code = _run_main(monkeypatch, [
        "review_spec.py", "foo", "final", "--spec", str(spec_arg),
        "--dist-dir", str(tmp_path / "dist"),
        "-o", str(out), "--report-dir", str(tmp_path / "reports")])
    assert code == 0
    assert json.loads(out.read_text())["verdict"] == "PASS"
