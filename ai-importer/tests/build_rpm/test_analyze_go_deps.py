"""analyze_go_deps.py — Go 包 RPM 依赖分析(go.mod/CGO 解析 + 单点 mock run_batch_lookup)。"""

from __future__ import annotations

import json
import sys

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sys.path.insert(0, str(SCRIPT_DIRS["build_rpm"]))
mod = load_module("analyze_go_deps", SCRIPT_DIRS["build_rpm"] / "analyze_go_deps.py")


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


# ─────────────────────────────────────────────
# parse_go_mod
# ─────────────────────────────────────────────

GO_MOD = """module github.com/example/demo

go 1.21

require (
    github.com/spf13/cobra v1.8.0
    github.com/stretchr/testify v1.9.0 // indirect
)

require github.com/google/uuid v1.6.0

require golang.org/x/sys v0.18.0 // indirect
"""


def test_parse_go_mod_full(tmp_path):
    _write(tmp_path, "go.mod", GO_MOD)
    result = mod.parse_go_mod(str(tmp_path))
    assert result["found"] is True
    assert result["module_path"] == "github.com/example/demo"
    assert result["go_version"] == "1.21"
    names = [d["name"] for d in result["module_deps"]]
    assert names == [
        "github.com/spf13/cobra",
        "github.com/stretchr/testify",
        "github.com/google/uuid",
        "golang.org/x/sys",
    ]
    assert all(d["vendor_only"] is True for d in result["module_deps"])


def test_parse_go_mod_missing(tmp_path):
    result = mod.parse_go_mod(str(tmp_path))
    assert result == {"found": False}


def test_parse_go_mod_minimal(tmp_path):
    _write(tmp_path, "go.mod", "module example.com/m\n\ngo 1.20\n")
    result = mod.parse_go_mod(str(tmp_path))
    assert result["module_path"] == "example.com/m"
    assert result["go_version"] == "1.20"
    assert result["module_deps"] == []


def test_parse_go_mod_dedup_across_blocks(tmp_path):
    # 同一模块同时出现在 require 块与单行 require → 去重
    _write(tmp_path, "go.mod", (
        "module m\n\nrequire (\n    example.com/a v1.0.0\n)\n\n"
        "require example.com/a v1.2.0\n"
    ))
    result = mod.parse_go_mod(str(tmp_path))
    assert [d["name"] for d in result["module_deps"]] == ["example.com/a"]


def test_parse_go_mod_single_line_require_only(tmp_path):
    _write(tmp_path, "go.mod", "module m\n\nrequire example.com/a v1.0.0\nrequire example.com/b v2.0.0\n")
    result = mod.parse_go_mod(str(tmp_path))
    assert [d["name"] for d in result["module_deps"]] == ["example.com/a", "example.com/b"]


# ─────────────────────────────────────────────
# scan_cgo
# ─────────────────────────────────────────────

def test_scan_cgo_full(tmp_path):
    _write(tmp_path, "cgo.go", '''
package demo

/*
#cgo LDFLAGS: -lssl -lcrypto -lpthread
#cgo pkg-config: openssl
#include <stdlib.h>
*/
import "C"

func F() { C.CString("x") }
''')
    _write(tmp_path, "plain.go", "package demo\n\nfunc G() {}\n")
    _write(tmp_path, "skip_test.go", 'package demo\n\nimport "C"\n')
    _write(tmp_path, "sub/wrap.c", "int wrap(void) { return 0; }\n")
    _write(tmp_path, "sub/wrap.h", "int wrap(void);\n")
    result = mod.scan_cgo(str(tmp_path))
    assert result["has_cgo"] is True
    assert result["cgo_files"] == ["cgo.go"]     # _test.go 跳过
    assert result["ldflags_libs"] == ["crypto", "ssl"]   # pthread 过滤,排序
    assert result["pkg_config"] == ["openssl"]
    assert sorted(result["c_source_files"]) == ["sub/wrap.c", "sub/wrap.h"]


def test_scan_cgo_no_cgo(tmp_path):
    _write(tmp_path, "main.go", "package main\n\nfunc main() {}\n")
    result = mod.scan_cgo(str(tmp_path))
    assert result["has_cgo"] is False
    assert result["cgo_files"] == []
    assert result["ldflags_libs"] == []
    assert result["pkg_config"] == []


def test_scan_cgo_backtick_import(tmp_path):
    _write(tmp_path, "cgo.go", 'package demo\n\nimport `C`\n')
    result = mod.scan_cgo(str(tmp_path))
    assert result["has_cgo"] is True


def test_scan_cgo_pkg_config_multiple(tmp_path):
    _write(tmp_path, "cgo.go", '''
package demo
// #cgo pkg-config: openssl zlib
import "C"
''')
    result = mod.scan_cgo(str(tmp_path))
    assert result["pkg_config"] == ["openssl", "zlib"]


# ─────────────────────────────────────────────
# build_lookup_tasks
# ─────────────────────────────────────────────

def test_build_lookup_tasks():
    cgo_info = {"ldflags_libs": ["ssl"], "pkg_config": ["openssl"]}
    tasks = mod.build_lookup_tasks(cgo_info)
    assert len(tasks) == 2

    ssl = next(t for t in tasks if t["dep"] == "ssl")
    assert ssl["type"] == "ldflags"
    assert ssl["prefer_devel"] is True
    values = [q["value"] for q in ssl["queries"]]
    assert values == ["*/libssl.so*", "ssl-devel", "libssl-devel", "pkgconfig(ssl)"]

    openssl = next(t for t in tasks if t["dep"] == "openssl")
    assert openssl["type"] == "pkg-config"
    values = [q["value"] for q in openssl["queries"]]
    assert values == ["pkgconfig(openssl)", "*/libopenssl.so*", "openssl-devel"]


def test_build_lookup_tasks_empty():
    assert mod.build_lookup_tasks({}) == []


# ─────────────────────────────────────────────
# check_rpm_availability(mock run_batch_lookup)
# ─────────────────────────────────────────────

def test_check_rpm_availability(monkeypatch):
    def fake(tasks, timeout=300, **kw):
        assert timeout == 300
        out = []
        for t in tasks:
            base = {k: v for k, v in t.items() if k not in {"queries", "prefer_devel"}}
            if t["dep"] == "ssl":
                out.append({**base, "rpm": "openssl-devel", "version": None, "release": None, "level": "libso"})
            else:
                out.append({**base, "rpm": None, "version": None, "release": None, "level": ""})
        return out
    monkeypatch.setattr(mod, "run_batch_lookup", fake)
    result = mod.check_rpm_availability(cgo_info={"ldflags_libs": ["ssl", "foo"]})
    assert result["available"] == [{"dep": "ssl", "type": "ldflags", "rpm": "openssl-devel"}]
    assert result["missing"] == [{"dep": "foo", "type": "ldflags"}]


def test_check_rpm_availability_fallback_on_error(monkeypatch):
    def boom(tasks, timeout=300, **kw):
        raise OSError("dnf gone")
    monkeypatch.setattr(mod, "run_batch_lookup", boom)
    result = mod.check_rpm_availability(cgo_info={"pkg_config": ["zlib"]})
    assert result["available"] == []
    assert result["missing"] == [{"dep": "zlib", "type": "pkg-config"}]


# ─────────────────────────────────────────────
# build_rpm_requires
# ─────────────────────────────────────────────

def test_build_rpm_requires_with_go_version():
    go_mod = {"go_version": "1.21"}
    cgo_info = {"has_cgo": False}
    assert mod.build_rpm_requires(go_mod, cgo_info, None) == ["golang >= 1.21"]


def test_build_rpm_requires_no_go_version():
    assert mod.build_rpm_requires({}, {"has_cgo": False}, None) == ["golang"]


def test_build_rpm_requires_cgo():
    assert mod.build_rpm_requires({}, {"has_cgo": True}, None) == ["golang", "gcc", "glibc-devel"]


def test_build_rpm_requires_with_rpm_check():
    go_mod = {"go_version": "1.20"}
    cgo_info = {"has_cgo": True}
    rpm_check = {"available": [
        {"rpm": "openssl-devel"},
        {"rpm": "openssl-devel"},    # 去重
        {"rpm": "zlib-devel"},
    ], "missing": []}
    result = mod.build_rpm_requires(go_mod, cgo_info, rpm_check)
    assert result == ["golang >= 1.20", "gcc", "glibc-devel", "openssl-devel", "zlib-devel"]


# ─────────────────────────────────────────────
# print_report / main
# ─────────────────────────────────────────────

def test_print_report(capsys):
    go_mod = {"found": True, "module_path": "example.com/demo", "go_version": "1.21"}
    cgo_info = {"has_cgo": True, "cgo_files": ["cgo.go"], "ldflags_libs": ["ssl"],
                "pkg_config": ["openssl"], "c_source_files": ["a.c"]}
    rpm_check = {"available": [{"dep": "ssl", "type": "ldflags", "rpm": "openssl-devel"}],
                 "missing": [{"dep": "foo", "type": "ldflags"}]}
    mod.print_report(go_mod, cgo_info, rpm_check)
    out = capsys.readouterr().out
    assert "Go 包 RPM 依赖分析报告" in out
    assert "模块路径 : example.com/demo" in out
    assert "openssl-devel" in out
    assert "BuildRequires: golang >= 1.21" in out


def test_print_report_no_gomod(capsys):
    mod.print_report({"found": False}, {"has_cgo": False}, None)
    out = capsys.readouterr().out
    assert "未找到 go.mod 文件" in out
    assert "未使用 CGO" in out


def test_main_output_json(tmp_path, capsys, monkeypatch):
    _write(tmp_path, "go.mod", GO_MOD)
    _write(tmp_path, "cgo.go", 'package demo\nimport "C"\n')
    out_json = tmp_path / "result.json"
    monkeypatch.setattr(sys, "argv", ["analyze_go_deps.py", str(tmp_path), "-o", str(out_json)])
    mod.main()
    result = json.loads(out_json.read_text())
    assert result["go_mod"]["go_version"] == "1.21"
    assert result["cgo"]["has_cgo"] is True
    assert result["build_requires"] == ["golang >= 1.21", "gcc", "glibc-devel"]
    assert result["rpm_check"] is None


def test_main_missing_dir(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["analyze_go_deps.py", str(tmp_path / "nope")])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1


def test_main_check_rpm_no_cgo_skips(tmp_path, capsys, monkeypatch):
    _write(tmp_path, "go.mod", "module m\n\ngo 1.20\n")
    monkeypatch.setattr(sys, "argv", ["analyze_go_deps.py", str(tmp_path), "--check-rpm"])
    mod.main()
    assert "跳过 RPM 查询" in capsys.readouterr().out
