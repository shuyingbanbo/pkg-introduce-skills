"""analyze_cpp_deps.py — C/C++ 包 RPM 依赖分析(纯逻辑 + 单点 mock run_batch_lookup)。"""

from __future__ import annotations

import json
import sys

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sys.path.insert(0, str(SCRIPT_DIRS["build_rpm"]))
mod = load_module("analyze_cpp_deps", SCRIPT_DIRS["build_rpm"] / "analyze_cpp_deps.py")


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


# ─────────────────────────────────────────────
# normalize_requirement / build_dependency_item / merge
# ─────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("", ""),
    (">=2.0", ">= 2.0"),
    ("=2.0", "== 2.0"),
    ("2.0", ">= 2.0"),
    ("abc", ""),
])
def test_normalize_requirement(raw, expected):
    assert mod.normalize_requirement(raw) == expected


@pytest.mark.parametrize("dep_type,name,req,expected_rpm_req", [
    ("cmake", "Boost", "", "cmake(Boost)"),
    ("cmake", "Boost", ">= 1.70", "cmake(Boost) >= 1.70"),
    ("pkgconfig", "OpenSSL", "", "pkgconfig(openssl)"),
    ("link", "Curl", "", "libcurl.so"),
    ("link", "curl", ">= 1.0", "libcurl.so"),   # link 不附加 requirement
])
def test_build_dependency_item(dep_type, name, req, expected_rpm_req):
    item = mod.build_dependency_item(dep_type, name, req)
    assert item["type"] == dep_type
    assert item["rpm_requirement"] == expected_rpm_req
    assert item["requirement"] == req


def test_merge_dependency_items():
    items = [
        mod.build_dependency_item("link", "curl"),
        mod.build_dependency_item("link", "curl"),
        mod.build_dependency_item("pkgconfig", "zlib", ">= 1.2"),
    ]
    merged = mod.merge_dependency_items(items)
    assert len(merged) == 2
    assert [(i["type"], i["dep"]) for i in merged] == [("link", "curl"), ("pkgconfig", "zlib")]


def test_parse_pkg_module_clause():
    items = mod.parse_pkg_module_clause("REQUIRED zlib >= 1.2.3 openssl")
    assert [(i["dep"], i["requirement"]) for i in items] == [("zlib", ">= 1.2.3"), ("openssl", "")]
    assert items[0]["type"] == "pkgconfig"
    assert items[0]["rpm_requirement"] == "pkgconfig(zlib) >= 1.2.3"


# ─────────────────────────────────────────────
# parse_cmake
# ─────────────────────────────────────────────

CPP_CMAKE_CONTENT = """
cmake_minimum_required(VERSION 3.20)
project(demo CXX)

find_package(Boost 1.70 REQUIRED COMPONENTS system)
find_package(Threads)
find_package(PkgConfig)
find_package(OpenSSL)

pkg_check_modules(FOO REQUIRED zlib >= 1.2 openssl)

target_link_libraries(demo PRIVATE -lcurl -lpthread -lstdc++)
"""


def test_parse_cmake(tmp_path):
    _write(tmp_path, "CMakeLists.txt", CPP_CMAKE_CONTENT)
    parsed = mod.parse_cmake(str(tmp_path))
    assert parsed["build_system"] == "cmake"
    assert parsed["cmake_min_version"] == "3.20"
    # C++ 版 CMAKE_SKIP 不含 Threads/PkgConfig → 两者保留
    assert parsed["find_packages"] == ["Boost", "OpenSSL", "PkgConfig", "Threads"]
    assert parsed["pkg_modules"] == ["openssl", "zlib"]
    # 生产代码 quirk:-l(\w+) 匹配不到 stdc++ 的 "+",截成 "stdc";
    # GLIBC_BUILTINS 里的 "stdc++" 因此永远命中不了 -lstdc++。按实际行为断言。
    assert parsed["link_libs"] == ["curl", "stdc"]

    boost = next(i for i in parsed["find_package_items"] if i["dep"] == "Boost")
    assert boost["type"] == "cmake"               # C++ 用 cmake(X) 而非 find_package
    assert boost["requirement"] == ">= 1.70"
    assert boost["rpm_requirement"] == "cmake(Boost) >= 1.70"


def test_parse_cmake_exact(tmp_path):
    _write(tmp_path, "CMakeLists.txt", "find_package(Qt5 5.15 EXACT)\n")
    parsed = mod.parse_cmake(str(tmp_path))
    assert parsed["find_package_items"][0]["requirement"] == "== 5.15"


def test_parse_cmake_skip_builtin(tmp_path):
    _write(tmp_path, "CMakeLists.txt",
           "find_package(GNUInstallDirs)\nfind_package(CheckCXXCompilerFlag)\nfind_package(FetchContent)\n")
    parsed = mod.parse_cmake(str(tmp_path))
    assert parsed["find_packages"] == []


def test_parse_cmake_recursive(tmp_path):
    _write(tmp_path, "CMakeLists.txt", "find_package(Boost)\n")
    _write(tmp_path, "src/CMakeLists.txt", "pkg_check_modules(QT5 REQUIRED Qt5Core)\ntarget_link_libraries(x -lcurl)\n")
    parsed = mod.parse_cmake(str(tmp_path))
    assert parsed["find_packages"] == ["Boost"]
    assert parsed["pkg_modules"] == ["Qt5Core"]
    assert parsed["link_libs"] == ["curl"]
    assert len(parsed["dependency_items"]) == 3


# ─────────────────────────────────────────────
# parse_autoconf / parse_makefile
# ─────────────────────────────────────────────

def test_parse_autoconf(tmp_path):
    _write(tmp_path, "configure.ac", """
AC_CHECK_LIB([curl], [curl_easy_init])
AC_CHECK_LIB(m, [cos])
PKG_CHECK_MODULES(DEPS, zlib >= 1.2 openssl)
""")
    parsed = mod.parse_autoconf(str(tmp_path))
    assert parsed["build_system"] == "autoconf"
    assert parsed["link_libs"] == ["curl"]        # m 过滤
    assert parsed["pkg_modules"] == ["openssl", "zlib"]


def test_parse_makefile(tmp_path):
    _write(tmp_path, "Makefile", "LDLIBS = -lcurl -lz -lpthread\n")
    parsed = mod.parse_makefile(str(tmp_path))
    assert parsed["build_system"] == "make"
    assert parsed["link_libs"] == ["curl", "z"]
    assert parsed["pkg_modules"] == []


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


# ─────────────────────────────────────────────
# build_lookup_tasks
# ─────────────────────────────────────────────

def test_build_lookup_tasks(tmp_path):
    _write(tmp_path, "CMakeLists.txt", CPP_CMAKE_CONTENT)
    parsed = mod.parse_cmake(str(tmp_path))
    tasks = mod.build_lookup_tasks(parsed)

    boost = next(t for t in tasks if t["dep"] == "Boost")
    assert boost["prefer_devel"] is True
    assert [q["kind"] for q in boost["queries"]] == ["provides", "provides", "file_glob", "name"]
    assert boost["queries"][0]["value"] == "cmake(Boost)"       # Level 1 cmake()
    assert boost["queries"][1]["value"] == "pkgconfig(boost)"   # Level 2 pkgconfig()
    assert boost["queries"][2]["value"] == "*/libboost.so*"
    assert boost["queries"][3]["value"] == "boost-devel"
    assert boost["queries"][3]["level"] == "name"

    curl = next(t for t in tasks if t["dep"] == "curl")
    values = [q["value"] for q in curl["queries"]]
    assert values == ["*/libcurl.so*", "curl-devel", "libcurl-devel", "pkgconfig(curl)"]


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


def test_check_rpm_availability_available_and_missing(monkeypatch):
    parsed = {
        "find_package_items": [mod.build_dependency_item("cmake", "Boost")],
        "link_lib_items": [mod.build_dependency_item("link", "curl")],
    }
    monkeypatch.setattr(mod, "run_batch_lookup", _fake_lookup_by_dep({
        "Boost": {"rpm": "boost-devel", "version": "1.74.0", "release": "1", "level": "cmake()"},
    }))
    result = mod.check_rpm_availability(parsed=parsed)
    assert len(result["available"]) == 1
    assert result["available"][0]["rpm"] == "boost-devel"
    assert result["available"][0]["level"] == "cmake()"
    assert result["missing"][0]["dep"] == "curl"


def test_check_rpm_availability_fallback_on_error(monkeypatch):
    parsed = {"link_lib_items": [mod.build_dependency_item("link", "curl")]}
    def boom(tasks, timeout=120, **kw):
        raise mod.BatchLookupError("boom")
    monkeypatch.setattr(mod, "run_batch_lookup", boom)
    result = mod.check_rpm_availability(parsed=parsed)
    assert result["available"] == []
    assert [m["dep"] for m in result["missing"]] == ["curl"]


def test_check_rpm_availability_version_conflict(monkeypatch):
    parsed = {"find_package_items": [mod.build_dependency_item("cmake", "Boost", ">= 2.0")]}
    monkeypatch.setattr(mod, "run_batch_lookup", _fake_lookup_by_dep({
        "Boost": {"rpm": "boost-devel", "version": "1.74.0", "release": "1", "level": "cmake()"},
    }))
    result = mod.check_rpm_availability(parsed=parsed)
    assert result["available"] == []
    assert [m["dep"] for m in result["missing"]] == ["Boost"]


def test_check_rpm_availability_version_ok(monkeypatch):
    parsed = {"find_package_items": [mod.build_dependency_item("cmake", "Boost", ">= 1.70")]}
    monkeypatch.setattr(mod, "run_batch_lookup", _fake_lookup_by_dep({
        "Boost": {"rpm": "boost-devel", "version": "1.74.0", "release": "1", "level": "cmake()"},
    }))
    result = mod.check_rpm_availability(parsed=parsed)
    assert [i["dep"] for i in result["available"]] == ["Boost"]


# ─────────────────────────────────────────────
# build_rpm_requires
# ─────────────────────────────────────────────

@pytest.mark.parametrize("bs,expected", [
    ("cmake", ["gcc", "gcc-c++", "cmake"]),
    ("autoconf", ["gcc", "gcc-c++", "autoconf", "automake", "libtool", "make"]),
    # 注:detect_build_system 会返回 "meson",但 main() 对 meson 走 parse_makefile
    # 分支(生产代码 quirk);build_rpm_requires 的 meson 分支仍按实际行为断言
    ("meson", ["gcc", "gcc-c++", "meson", "ninja-build"]),
    ("make", ["gcc", "gcc-c++", "make"]),
    ("unknown", ["gcc", "gcc-c++", "make"]),
])
def test_build_rpm_requires(bs, expected):
    assert mod.build_rpm_requires(bs, None) == expected


def test_build_rpm_requires_with_rpm_check():
    rpm_check = {"available": [
        {"rpm": "cmake"},
        {"rpm": "boost-devel"},
        {"rpm": "boost-devel"},
    ], "missing": []}
    assert mod.build_rpm_requires("cmake", rpm_check) == ["gcc", "gcc-c++", "cmake", "boost-devel"]


# ─────────────────────────────────────────────
# print_report / main
# ─────────────────────────────────────────────

def test_print_report(capsys, tmp_path):
    _write(tmp_path, "CMakeLists.txt", CPP_CMAKE_CONTENT)
    parsed = mod.parse_cmake(str(tmp_path))
    rpm_check = {"available": [
        {"dep": "Boost", "type": "cmake", "requirement": ">= 1.70",
         "rpm": "boost-devel", "version": "1.74.0", "level": "cmake()"},
    ], "missing": [{"dep": "curl", "type": "link", "requirement": ""}]}
    mod.print_report(parsed, rpm_check)
    out = capsys.readouterr().out
    assert "C/C++ 包 RPM 依赖分析报告" in out
    assert "构建系统 : cmake" in out
    assert "boost-devel" in out
    assert "BuildRequires: gcc-c++" in out


def test_main_output_json(tmp_path, capsys, monkeypatch):
    _write(tmp_path, "CMakeLists.txt", "find_package(Boost)\n")
    out_json = tmp_path / "result.json"
    monkeypatch.setattr(sys, "argv", ["analyze_cpp_deps.py", str(tmp_path), "-o", str(out_json)])
    mod.main()
    result = json.loads(out_json.read_text())
    assert result["build_system"] == "cmake"
    assert result["find_packages"] == ["Boost"]
    assert result["build_requires"] == ["gcc", "gcc-c++", "cmake"]


def test_main_missing_dir(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["analyze_cpp_deps.py", str(tmp_path / "nope")])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1


def test_main_meson_falls_back_to_makefile(tmp_path, capsys, monkeypatch):
    # 生产代码 quirk:detect 返回 meson,但 main 的 elif 链没有 meson 分支,
    # 落入 parse_makefile。按实际行为断言。
    _write(tmp_path, "meson.build", "dependency('zlib')\n")
    out_json = tmp_path / "r.json"
    monkeypatch.setattr(sys, "argv", ["analyze_cpp_deps.py", str(tmp_path), "-o", str(out_json)])
    mod.main()
    result = json.loads(out_json.read_text())
    assert result["build_system"] == "make"       # parse_makefile 的返回值
    assert result["dependency_items"] == []


def test_main_check_rpm_missing_exit2(tmp_path, monkeypatch):
    _write(tmp_path, "Makefile", "LIBS = -lfoo\n")
    monkeypatch.setattr(sys, "argv", ["analyze_cpp_deps.py", str(tmp_path), "--check-rpm"])
    monkeypatch.setattr(mod, "run_batch_lookup", _fake_lookup_by_dep({}))
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 2
