"""analyze_c_deps.py — C 包 RPM 依赖分析(纯逻辑 + 单点 mock run_batch_lookup)。"""

from __future__ import annotations

import json
import sys

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sys.path.insert(0, str(SCRIPT_DIRS["build_rpm"]))
mod = load_module("analyze_c_deps", SCRIPT_DIRS["build_rpm"] / "analyze_c_deps.py")


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


# ─────────────────────────────────────────────
# normalize_requirement
# ─────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("", ""),
    ("   ", ""),
    ('">= 2.0"', ">= 2.0"),      # 引号剥离
    (">=2.0", ">= 2.0"),
    ("=2.0", "== 2.0"),          # 单 = 归一为 ==
    ("2.0", ">= 2.0"),           # 纯版本 → 默认 >=
    ("1.2.3", ">= 1.2.3"),
    ("abc", ""),                 # 非版本、非操作符 → 空
    ("> 1", "> 1"),
    ("<=1.0", "<= 1.0"),
])
def test_normalize_requirement(raw, expected):
    assert mod.normalize_requirement(raw) == expected


def test_normalize_requirement_custom_default():
    assert mod.normalize_requirement("2.0", default_operator="==") == "== 2.0"


# ─────────────────────────────────────────────
# build_dependency_item
# ─────────────────────────────────────────────

@pytest.mark.parametrize("dep_type,name,req,expected_rpm_req", [
    ("find_package", "ZLIB", "", "cmake(ZLIB)"),
    ("find_package", "ZLIB", ">= 3.0", "cmake(ZLIB) >= 3.0"),
    ("pkgconfig", "OpenSSL", "", "pkgconfig(openssl)"),   # pkgconfig 小写化
    ("pkgconfig", "zlib", ">= 1.2", "pkgconfig(zlib) >= 1.2"),
    ("link", "SSL", "", "libssl.so"),
    ("link", "ssl", ">= 1.0", "libssl.so"),               # link 不附加 requirement
])
def test_build_dependency_item(dep_type, name, req, expected_rpm_req):
    item = mod.build_dependency_item(dep_type, name, req)
    assert item["dep"] == name
    assert item["name"] == name
    assert item["type"] == dep_type
    assert item["requirement"] == req
    assert item["rpm_requirement"] == expected_rpm_req
    assert item["upstream_url"] == ""


# ─────────────────────────────────────────────
# merge_dependency_items
# ─────────────────────────────────────────────

def test_merge_dependency_items_dedup():
    items = [
        mod.build_dependency_item("link", "ssl"),
        mod.build_dependency_item("link", "ssl"),
        mod.build_dependency_item("pkgconfig", "zlib"),
    ]
    merged = mod.merge_dependency_items(items)
    assert len(merged) == 2
    assert [(i["type"], i["dep"]) for i in merged] == [("link", "ssl"), ("pkgconfig", "zlib")]


def test_merge_dependency_items_requirement_priority():
    items = [
        mod.build_dependency_item("pkgconfig", "zlib"),
        mod.build_dependency_item("pkgconfig", "zlib", ">= 1.2"),   # 空 requirement 被带版本替换
    ]
    merged = mod.merge_dependency_items(items)
    assert len(merged) == 1
    assert merged[0]["requirement"] == ">= 1.2"


def test_merge_dependency_items_eq_overrides_ge():
    items = [
        mod.build_dependency_item("find_package", "ZLIB", ">= 3.0"),
        mod.build_dependency_item("find_package", "ZLIB", "== 4.0"),  # >= 被 == 替换
    ]
    merged = mod.merge_dependency_items(items)
    assert merged[0]["requirement"] == "== 4.0"


def test_merge_dependency_items_ge_kept_over_ge():
    items = [
        mod.build_dependency_item("find_package", "ZLIB", ">= 3.0"),
        mod.build_dependency_item("find_package", "ZLIB", ">= 4.0"),  # 后续 >= 不覆盖
    ]
    merged = mod.merge_dependency_items(items)
    assert merged[0]["requirement"] == ">= 3.0"


# ─────────────────────────────────────────────
# parse_pkg_module_clause
# ─────────────────────────────────────────────

def test_parse_pkg_module_clause_simple():
    items = mod.parse_pkg_module_clause("zlib openssl")
    assert [(i["dep"], i["type"]) for i in items] == [("zlib", "pkgconfig"), ("openssl", "pkgconfig")]
    assert all(i["requirement"] == "" for i in items)


def test_parse_pkg_module_clause_with_operators():
    items = mod.parse_pkg_module_clause("zlib >= 1.2.3 openssl")
    assert items[0]["dep"] == "zlib"
    assert items[0]["requirement"] == ">= 1.2.3"
    assert items[1]["dep"] == "openssl"
    assert items[1]["requirement"] == ""


def test_parse_pkg_module_clause_combined_token():
    items = mod.parse_pkg_module_clause("zlib>=1.2.3")
    assert items[0]["dep"] == "zlib"
    assert items[0]["requirement"] == ">= 1.2.3"
    assert items[0]["rpm_requirement"] == "pkgconfig(zlib) >= 1.2.3"


def test_parse_pkg_module_clause_ignored_tokens():
    items = mod.parse_pkg_module_clause("REQUIRED QUIET zlib [imported] openssl")
    assert [(i["dep"], i["type"]) for i in items] == [("zlib", "pkgconfig"), ("openssl", "pkgconfig")]


# ─────────────────────────────────────────────
# parse_cmake
# ─────────────────────────────────────────────

CMAKE_CONTENT = """
cmake_minimum_required(VERSION 3.16)
project(demo C)

find_package(ZLIB 3.0 REQUIRED)
find_package(Threads REQUIRED)
find_package(OpenSSL)

pkg_check_modules(FOO REQUIRED zlib >= 1.2 openssl)
pkg_search_module(BAR libxml-2.0)

target_link_libraries(demo PRIVATE -lssl -lpthread -lz -lm)
"""


def test_parse_cmake(tmp_path):
    _write(tmp_path, "CMakeLists.txt", CMAKE_CONTENT)
    parsed = mod.parse_cmake(str(tmp_path))
    assert parsed["build_system"] == "cmake"
    assert parsed["cmake_min_version"] == "3.16"
    assert parsed["find_packages"] == ["OpenSSL", "ZLIB"]   # Threads 在 CMAKE_SKIP
    assert parsed["pkg_modules"] == ["libxml-2.0", "openssl", "zlib"]
    assert parsed["link_libs"] == ["ssl", "z"]              # pthread/m 过滤

    # find_package item 细节
    zlib_item = next(i for i in parsed["find_package_items"] if i["dep"] == "ZLIB")
    assert zlib_item["type"] == "find_package"
    assert zlib_item["requirement"] == ">= 3.0"
    assert zlib_item["rpm_requirement"] == "cmake(ZLIB) >= 3.0"
    openssl_item = next(i for i in parsed["find_package_items"] if i["dep"] == "OpenSSL")
    assert openssl_item["requirement"] == ""

    zlib_pc = next(i for i in parsed["pkg_module_items"] if i["dep"] == "zlib")
    assert zlib_pc["requirement"] == ">= 1.2"

    link = next(i for i in parsed["link_lib_items"] if i["dep"] == "ssl")
    assert link["rpm_requirement"] == "libssl.so"

    # dependency_items 合并后数量(2 find + 3 pkg + 2 link,无跨类冲突)
    assert len(parsed["dependency_items"]) == 7


def test_parse_cmake_exact_version(tmp_path):
    _write(tmp_path, "CMakeLists.txt", "find_package(ZLIB 3.0 EXACT REQUIRED)\n")
    parsed = mod.parse_cmake(str(tmp_path))
    item = parsed["find_package_items"][0]
    assert item["requirement"] == "== 3.0"


def test_parse_cmake_recursive_and_cmake_files(tmp_path):
    _write(tmp_path, "CMakeLists.txt", "find_package(BZip2)\n")
    _write(tmp_path, "cmake/FindExtra.cmake", "find_package(LibXml2 2.0)\n")
    _write(tmp_path, "sub/CMakeLists.txt", "pkg_check_modules(QT5 REQUIRED Qt5Core)\n")
    parsed = mod.parse_cmake(str(tmp_path))
    assert parsed["find_packages"] == ["BZip2", "LibXml2"]
    assert parsed["pkg_modules"] == ["Qt5Core"]


def test_parse_cmake_no_version_requirement_when_not_numeric(tmp_path):
    _write(tmp_path, "CMakeLists.txt", "find_package(ZLIB REQUIRED)\n")
    parsed = mod.parse_cmake(str(tmp_path))
    assert parsed["find_package_items"][0]["requirement"] == ""


# ─────────────────────────────────────────────
# parse_autoconf
# ─────────────────────────────────────────────

def test_parse_autoconf(tmp_path):
    _write(tmp_path, "configure.ac", """
AC_INIT([demo], [1.0])
AC_CHECK_LIB([ssl], [SSL_new])
AC_CHECK_LIB(pthread, [pthread_create])
AC_CHECK_LIB(z, [compress])
PKG_CHECK_MODULES(DEPS, zlib >= 1.2 openssl)
""")
    parsed = mod.parse_autoconf(str(tmp_path))
    assert parsed["build_system"] == "autoconf"
    assert parsed["link_libs"] == ["ssl", "z"]     # pthread 过滤
    assert parsed["pkg_modules"] == ["openssl", "zlib"]
    assert parsed["find_packages"] == []
    zlib_item = next(i for i in parsed["pkg_module_items"] if i["dep"] == "zlib")
    assert zlib_item["requirement"] == ">= 1.2"


def test_parse_autoconf_configure_in(tmp_path):
    _write(tmp_path, "configure.in", "AC_CHECK_LIB([curl], [curl_easy_init])\n")
    parsed = mod.parse_autoconf(str(tmp_path))
    assert parsed["link_libs"] == ["curl"]


def test_parse_autoconf_missing(tmp_path):
    parsed = mod.parse_autoconf(str(tmp_path))
    assert parsed["build_system"] == "autoconf"
    assert parsed["dependency_items"] == []


# ─────────────────────────────────────────────
# parse_meson
# ─────────────────────────────────────────────

def test_parse_meson(tmp_path):
    _write(tmp_path, "meson.build", """
project('demo', 'c')
zlib_dep = dependency('zlib', version : '>=1.2.3')
m_dep = dependency('m')
ssl_dep = cc.find_library('ssl', required: true)
crypto = cc.find_library('crypto')
""")
    parsed = mod.parse_meson(str(tmp_path))
    assert parsed["build_system"] == "meson"
    assert parsed["pkg_modules"] == ["zlib"]       # m 过滤
    assert parsed["link_libs"] == ["crypto", "ssl"]
    zlib_item = parsed["pkg_module_items"][0]
    assert zlib_item["requirement"] == ">= 1.2.3"


# ─────────────────────────────────────────────
# parse_makefile
# ─────────────────────────────────────────────

def test_parse_makefile(tmp_path):
    _write(tmp_path, "Makefile", "LDLIBS = -lssl -lz -lpthread\nLIBS = -lcurl\n")
    parsed = mod.parse_makefile(str(tmp_path))
    assert parsed["build_system"] == "make"
    assert parsed["link_libs"] == ["curl", "ssl", "z"]


def test_parse_makefile_gnumakefile_fallback(tmp_path):
    _write(tmp_path, "GNUmakefile", "LIBS = -lncurses\n")
    parsed = mod.parse_makefile(str(tmp_path))
    assert parsed["link_libs"] == ["ncurses"]


# ─────────────────────────────────────────────
# detect_build_system
# ─────────────────────────────────────────────

@pytest.mark.parametrize("files,expected", [
    (["CMakeLists.txt"], "cmake"),
    (["configure.ac"], "autoconf"),
    (["configure.in"], "autoconf"),
    (["meson.build"], "meson"),
    (["Makefile"], "make"),
    ([], "unknown"),
])
def test_detect_build_system(tmp_path, files, expected):
    for f in files:
        _write(tmp_path, f, "")
    assert mod.detect_build_system(str(tmp_path)) == expected


def test_detect_build_system_priority(tmp_path):
    _write(tmp_path, "Makefile", "")
    _write(tmp_path, "meson.build", "")
    _write(tmp_path, "configure.ac", "")
    _write(tmp_path, "CMakeLists.txt", "")
    assert mod.detect_build_system(str(tmp_path)) == "cmake"


# ─────────────────────────────────────────────
# build_lookup_tasks
# ─────────────────────────────────────────────

def test_build_lookup_tasks(tmp_path):
    _write(tmp_path, "CMakeLists.txt", CMAKE_CONTENT)
    parsed = mod.parse_cmake(str(tmp_path))
    tasks = mod.build_lookup_tasks(parsed)
    assert len(tasks) == len(parsed["dependency_items"])

    zlib_task = next(t for t in tasks if t["dep"] == "ZLIB" and t["type"] == "find_package")
    assert "prefer_devel" not in zlib_task
    assert [q["kind"] for q in zlib_task["queries"]] == ["provides", "provides"]
    assert zlib_task["queries"][0]["value"] == "pkgconfig(zlib)"
    assert zlib_task["queries"][1]["value"] == "cmake(ZLIB)"
    assert zlib_task["queries"][1]["level"] == "cmake()"

    ssl_task = next(t for t in tasks if t["dep"] == "ssl" and t["type"] == "link")
    assert ssl_task["prefer_devel"] is True
    values = [q["value"] for q in ssl_task["queries"]]
    assert values == ["pkgconfig(ssl)", "cmake(ssl)", "libssl*-devel", "ssl*-devel"]
    assert all(q["kind"] in ("provides", "name_glob") for q in ssl_task["queries"])


def test_build_lookup_tasks_empty():
    assert mod.build_lookup_tasks({}) == []


# ─────────────────────────────────────────────
# check_rpm_availability(mock run_batch_lookup)
# ─────────────────────────────────────────────

def _fake_lookup_by_dep(results_map):
    def fake(tasks, timeout=120, **kw):
        out = []
        for t in tasks:
            base = {k: v for k, v in t.items() if k not in {"queries", "prefer_devel"}}
            res = results_map.get(t["dep"])
            if res is None:
                out.append({**base, "rpm": None, "version": None, "release": None, "level": ""})
            else:
                out.append({**base, **res})
        return out
    return fake


def test_check_rpm_availability_available(monkeypatch):
    parsed = {
        "find_package_items": [mod.build_dependency_item("find_package", "ZLIB")],
        "pkg_module_items": [mod.build_dependency_item("pkgconfig", "zlib")],
        "link_lib_items": [mod.build_dependency_item("link", "ssl")],
    }
    monkeypatch.setattr(mod, "run_batch_lookup", _fake_lookup_by_dep({
        "ZLIB": {"rpm": "zlib-devel", "version": "1.2.11", "release": "1", "level": "pkgconfig()"},
        "zlib": {"rpm": "zlib-devel", "version": None, "release": None, "level": "pkgconfig()"},
        "ssl": {"rpm": "openssl-devel", "version": None, "release": None, "level": "name-glob"},
    }))
    result = mod.check_rpm_availability(parsed=parsed)
    assert len(result["available"]) == 3
    assert result["missing"] == []
    zlib_avail = next(i for i in result["available"] if i["dep"] == "ZLIB")
    assert zlib_avail["rpm"] == "zlib-devel"
    assert zlib_avail["version"] == "1.2.11"
    assert zlib_avail["level"] == "pkgconfig()"
    assert zlib_avail["type"] == "find_package"


def test_check_rpm_availability_missing(monkeypatch):
    parsed = {"link_lib_items": [mod.build_dependency_item("link", "foo")]}
    monkeypatch.setattr(mod, "run_batch_lookup", _fake_lookup_by_dep({}))
    result = mod.check_rpm_availability(parsed=parsed)
    assert result["available"] == []
    assert len(result["missing"]) == 1
    miss = result["missing"][0]
    assert miss["dep"] == "foo"
    assert miss["type"] == "link"
    assert miss["rpm_requirement"] == "libfoo.so"


def test_check_rpm_availability_fallback_on_error(monkeypatch):
    parsed = {"link_lib_items": [mod.build_dependency_item("link", "ssl")]}
    def boom(tasks, timeout=120, **kw):
        raise mod.BatchLookupError("boom")
    monkeypatch.setattr(mod, "run_batch_lookup", boom)
    result = mod.check_rpm_availability(parsed=parsed)
    assert result["available"] == []
    assert [m["dep"] for m in result["missing"]] == ["ssl"]


def test_check_rpm_availability_no_tasks(monkeypatch, capsys):
    called = []
    def fake(tasks, timeout=120, **kw):
        called.append(tasks)
        return []
    monkeypatch.setattr(mod, "run_batch_lookup", fake)
    result = mod.check_rpm_availability(parsed={})
    assert result == {"available": [], "missing": []}
    assert called == []      # 无任务时不触发查询


def test_check_rpm_availability_version_ok(monkeypatch):
    parsed = {"find_package_items": [mod.build_dependency_item("find_package", "ZLIB", ">= 3.0")]}
    monkeypatch.setattr(mod, "run_batch_lookup", _fake_lookup_by_dep({
        "ZLIB": {"rpm": "zlib-devel", "version": "3.0.0", "release": "1", "level": "pkgconfig()"},
    }))
    result = mod.check_rpm_availability(parsed=parsed)
    assert [i["dep"] for i in result["available"]] == ["ZLIB"]
    assert result["missing"] == []


def test_check_rpm_availability_version_conflict(monkeypatch):
    parsed = {"find_package_items": [mod.build_dependency_item("find_package", "ZLIB", ">= 4.0")]}
    monkeypatch.setattr(mod, "run_batch_lookup", _fake_lookup_by_dep({
        "ZLIB": {"rpm": "zlib-devel", "version": "3.0.0", "release": "1", "level": "pkgconfig()"},
    }))
    result = mod.check_rpm_availability(parsed=parsed)
    assert result["available"] == []
    assert [i["dep"] for i in result["missing"]] == ["ZLIB"]


# ─────────────────────────────────────────────
# build_rpm_requires
# ─────────────────────────────────────────────

@pytest.mark.parametrize("bs,expected", [
    ("cmake", ["gcc", "make", "cmake"]),
    ("autoconf", ["gcc", "make", "autoconf", "automake", "libtool"]),
    ("meson", ["gcc", "make", "meson", "ninja-build"]),
    ("make", ["gcc", "make"]),
    ("unknown", ["gcc", "make"]),
])
def test_build_rpm_requires(bs, expected):
    assert mod.build_rpm_requires(bs, None) == expected


def test_build_rpm_requires_with_rpm_check():
    rpm_check = {"available": [
        {"rpm": "cmake"},          # 与 cmake 分支重复,应去重
        {"rpm": "zlib-devel"},
        {"rpm": "zlib-devel"},     # 自身重复
    ], "missing": []}
    result = mod.build_rpm_requires("cmake", rpm_check)
    assert result == ["gcc", "make", "cmake", "zlib-devel"]


def test_build_rpm_requires_missing_ignored():
    rpm_check = {"available": [], "missing": [{"dep": "foo", "type": "link"}]}
    assert mod.build_rpm_requires("make", rpm_check) == ["gcc", "make"]


# ─────────────────────────────────────────────
# print_report / main
# ─────────────────────────────────────────────

def test_print_report(capsys, tmp_path):
    _write(tmp_path, "CMakeLists.txt", CMAKE_CONTENT)
    parsed = mod.parse_cmake(str(tmp_path))
    rpm_check = {"available": [
        {"dep": "ZLIB", "type": "find_package", "requirement": ">= 3.0",
         "rpm": "zlib-devel", "version": "1.2.11", "level": "pkgconfig()"},
    ], "missing": [{"dep": "ssl", "type": "link", "requirement": ""}]}
    mod.print_report(parsed, rpm_check)
    out = capsys.readouterr().out
    assert "C 包 RPM 依赖分析报告" in out
    assert "构建系统 : cmake" in out
    assert "zlib-devel" in out
    assert "BuildRequires: cmake" in out


def test_print_items(capsys):
    mod.print_items("title", [{"dep": "zlib", "requirement": ">= 1.2"},
                              {"dep": "ssl", "requirement": ""}])
    out = capsys.readouterr().out
    assert "[title]  2 个" in out
    assert "- zlib >= 1.2" in out
    assert "- ssl" in out


def test_print_items_empty(capsys):
    mod.print_items("title", [])
    assert capsys.readouterr().out == ""


def test_main_output_json(tmp_path, capsys, monkeypatch):
    _write(tmp_path, "CMakeLists.txt", "find_package(ZLIB)\n")
    out_json = tmp_path / "sub" / "result.json"
    monkeypatch.setattr(sys, "argv", ["analyze_c_deps.py", str(tmp_path), "-o", str(out_json)])
    mod.main()
    assert out_json.exists()
    result = json.loads(out_json.read_text())
    assert result["build_system"] == "cmake"
    assert result["find_packages"] == ["ZLIB"]
    assert result["build_requires"] == ["gcc", "make", "cmake"]
    assert result["rpm_check"] is None
    assert "[INFO] 结果已保存" in capsys.readouterr().out


def test_main_missing_dir(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["analyze_c_deps.py", str(tmp_path / "nope")])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1
    assert "[ERROR] 目录不存在" in capsys.readouterr().err


def test_main_check_rpm_no_deps(tmp_path, capsys, monkeypatch):
    _write(tmp_path, "Makefile", "")     # 无 -l 依赖
    monkeypatch.setattr(sys, "argv", ["analyze_c_deps.py", str(tmp_path), "--check-rpm"])
    mod.main()   # 依赖为空 → 跳过查询,不退出
    assert "跳过 RPM 查询" in capsys.readouterr().out


def test_main_check_rpm_missing_exit2(tmp_path, monkeypatch, capsys):
    _write(tmp_path, "Makefile", "LIBS = -lfoo\n")
    monkeypatch.setattr(sys, "argv", ["analyze_c_deps.py", str(tmp_path), "--check-rpm"])
    monkeypatch.setattr(mod, "run_batch_lookup", _fake_lookup_by_dep({}))
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 2
