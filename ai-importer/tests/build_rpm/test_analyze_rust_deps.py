"""analyze_rust_deps.py — Rust 包 RPM 依赖分析(Cargo.toml/build.rs 解析 + mock run_batch_lookup)。"""

from __future__ import annotations

import json
import sys

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sys.path.insert(0, str(SCRIPT_DIRS["build_rpm"]))
mod = load_module("analyze_rust_deps", SCRIPT_DIRS["build_rpm"] / "analyze_rust_deps.py")


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


# ─────────────────────────────────────────────
# parse_cargo_toml
# ─────────────────────────────────────────────

CARGO_TOML = """
[package]
name = "demo"
version = "0.1.0"
edition = "2021"
rust-version = "1.65"
links = "ssl"

[dependencies]
serde = { version = "1.0", features = ["derive"] }
"quote" = "1.0"
libc = "0.2"

[build-dependencies]
pkg-config = "0.3"
cc = "1.0"

[dev-dependencies]
tempfile = "3.0"
"""


def test_parse_cargo_toml_full(tmp_path):
    _write(tmp_path, "Cargo.toml", CARGO_TOML)
    result = mod.parse_cargo_toml(str(tmp_path))
    assert result["found"] is True
    assert result["rust_version"] == "1.65"
    assert result["edition"] == "2021"
    assert result["links"] == ["ssl"]
    assert result["build_deps"] == ["pkg-config", "cc"]
    # 生产代码 quirk:in_deps 用 `[.*dependencies.*]` 匹配,[build-dependencies] 与
    # [dev-dependencies] 段都会进入 crate_deps(build-dependencies 同时进 build_deps)。
    crate_names = [d["name"] for d in result["crate_deps"]]
    assert crate_names == ["serde", "quote", "libc", "pkg-config", "cc", "tempfile"]
    assert all(d["vendor_only"] is True for d in result["crate_deps"])


def test_parse_cargo_toml_missing(tmp_path):
    result = mod.parse_cargo_toml(str(tmp_path))
    assert result == {"found": False, "rust_version": "", "links": [], "build_deps": [], "crate_deps": []}


def test_parse_cargo_toml_manifest_path(tmp_path):
    _write(tmp_path, "rust/Cargo.toml", 'rust-version = "1.70"\nlinks = "z"\n')
    result = mod.parse_cargo_toml(str(tmp_path), manifest_path=str(tmp_path / "rust" / "Cargo.toml"))
    assert result["rust_version"] == "1.70"
    assert result["links"] == ["z"]


def test_parse_cargo_toml_glibc_links_filtered(tmp_path):
    _write(tmp_path, "Cargo.toml", 'links = "m"\n')
    result = mod.parse_cargo_toml(str(tmp_path))
    assert result["links"] == []         # "m" 是 glibc 内置,过滤


def test_parse_cargo_toml_duplicate_crate_dedup(tmp_path):
    _write(tmp_path, "Cargo.toml", '[dependencies]\nserde = "1.0"\nserde = "1.1"\n')
    result = mod.parse_cargo_toml(str(tmp_path))
    assert [d["name"] for d in result["crate_deps"]] == ["serde"]


def test_parse_cargo_toml_section_switch(tmp_path):
    # 非 dependencies 段的行不进入 crate_deps
    _write(tmp_path, "Cargo.toml", '[package]\nname = "x"\n\n[dependencies]\nlibc = "0.2"\n')
    result = mod.parse_cargo_toml(str(tmp_path))
    assert [d["name"] for d in result["crate_deps"]] == ["libc"]


# ─────────────────────────────────────────────
# scan_build_rs
# ─────────────────────────────────────────────

def test_scan_build_rs_full(tmp_path):
    _write(tmp_path, "build.rs", '''
fn main() {
    println!("cargo:rustc-link-lib=ssl");
    println!("cargo:rustc-link-lib=static=crypto");
    println!("cargo:rustc-link-lib=dylib=z");
    println!("cargo:rustc-link-lib=m");
    let _ = pkg_config::probe_library("openssl").unwrap();
    let _ = pkg_config::Config::new().probe("zlib").unwrap();
}
''')
    result = mod.scan_build_rs(str(tmp_path))
    assert result["found"] is True
    assert result["link_libs"] == ["crypto", "ssl", "z"]   # m 过滤,static=/dylib= 前缀剥离
    assert result["pkg_configs"] == ["openssl", "zlib"]


def test_scan_build_rs_missing(tmp_path):
    result = mod.scan_build_rs(str(tmp_path))
    assert result == {"found": False, "link_libs": [], "pkg_configs": []}


def test_scan_build_rs_dedup(tmp_path):
    _write(tmp_path, "build.rs", 'fn main() {\n    println!("cargo:rustc-link-lib=ssl");\n    println!("cargo:rustc-link-lib=ssl");\n}\n')
    result = mod.scan_build_rs(str(tmp_path))
    assert result["link_libs"] == ["ssl"]


# ─────────────────────────────────────────────
# scan_c_sources
# ─────────────────────────────────────────────

def test_scan_c_sources(tmp_path):
    _write(tmp_path, "a.c", "")
    _write(tmp_path, "sub/b.cpp", "")
    _write(tmp_path, "sub/c.cc", "")
    _write(tmp_path, "d.h", "")       # .h 不扫描
    result = mod.scan_c_sources(str(tmp_path))
    assert len(result) == 3
    assert set(result) == {"a.c", "sub/b.cpp", "sub/c.cc"}


def test_scan_c_sources_empty(tmp_path):
    assert mod.scan_c_sources(str(tmp_path)) == []


# ─────────────────────────────────────────────
# build_lookup_tasks
# ─────────────────────────────────────────────

def test_build_lookup_tasks():
    parsed = {"pkg_configs": ["openssl"], "link_libs": ["ssl"]}
    tasks = mod.build_lookup_tasks(parsed)
    assert len(tasks) == 2

    openssl = next(t for t in tasks if t["dep"] == "openssl")
    assert openssl["type"] == "pkgconfig"
    assert openssl["prefer_devel"] is True
    values = [q["value"] for q in openssl["queries"]]
    assert values == ["pkgconfig(openssl)", "*/libopenssl.so*", "openssl-devel"]

    ssl = next(t for t in tasks if t["dep"] == "ssl")
    assert ssl["type"] == "link"
    values = [q["value"] for q in ssl["queries"]]
    assert values == ["*/libssl.so*", "ssl-devel", "libssl-devel", "pkgconfig(ssl)"]


def test_build_lookup_tasks_empty():
    assert mod.build_lookup_tasks({}) == []


# ─────────────────────────────────────────────
# check_rpm_availability(mock run_batch_lookup)
# ─────────────────────────────────────────────

def test_check_rpm_availability(monkeypatch):
    def fake(tasks, timeout=120, **kw):
        out = []
        for t in tasks:
            base = {k: v for k, v in t.items() if k not in {"queries", "prefer_devel"}}
            if t["dep"] == "openssl":
                out.append({**base, "rpm": "openssl-devel", "version": None, "release": None, "level": "pkgconfig()"})
            else:
                out.append({**base, "rpm": None, "version": None, "release": None, "level": ""})
        return out
    monkeypatch.setattr(mod, "run_batch_lookup", fake)
    result = mod.check_rpm_availability(parsed={"pkg_configs": ["openssl"], "link_libs": ["ssl"]})
    assert result["available"] == [
        {"dep": "openssl", "type": "pkgconfig", "rpm": "openssl-devel", "level": "pkgconfig()"},
    ]
    assert result["missing"] == [{"dep": "ssl", "type": "link"}]


def test_check_rpm_availability_fallback_on_error(monkeypatch):
    def boom(tasks, timeout=120, **kw):
        raise mod.BatchLookupError("boom")
    monkeypatch.setattr(mod, "run_batch_lookup", boom)
    result = mod.check_rpm_availability(parsed={"link_libs": ["ssl"]})
    assert result["available"] == []
    assert result["missing"] == [{"dep": "ssl", "type": "link"}]


# ─────────────────────────────────────────────
# build_rpm_requires
# ─────────────────────────────────────────────

def test_build_rpm_requires_no_version():
    assert mod.build_rpm_requires("", None) == ["rust", "cargo"]


def test_build_rpm_requires_with_version():
    assert mod.build_rpm_requires("1.65", None) == ["rust >= 1.65", "cargo"]


def test_build_rpm_requires_with_rpm_check():
    rpm_check = {"available": [
        {"rpm": "openssl-devel"},
        {"rpm": "openssl-devel"},    # 去重
        {"rpm": "zlib-devel"},
    ], "missing": []}
    result = mod.build_rpm_requires("1.65", rpm_check)
    assert result == ["rust >= 1.65", "cargo", "openssl-devel", "zlib-devel"]


def test_build_rpm_requires_rpm_dup_with_base(monkeypatch):
    # available 中混入 "rust" / "cargo" 也不重复
    rpm_check = {"available": [{"rpm": "rust"}, {"rpm": "cargo"}, {"rpm": "zlib-devel"}], "missing": []}
    assert mod.build_rpm_requires("", rpm_check) == ["rust", "cargo", "zlib-devel"]


# ─────────────────────────────────────────────
# print_report / main
# ─────────────────────────────────────────────

def test_print_report(capsys):
    parsed = {
        "cargo_toml": {"rust_version": "1.65", "edition": "2021", "links": ["ssl"]},
        "build_rs": {"pkg_configs": ["openssl"], "link_libs": ["crypto"]},
        "c_sources": ["a.c"],
    }
    rpm_check = {"available": [{"dep": "openssl", "type": "pkgconfig", "rpm": "openssl-devel", "level": "pkgconfig()"}],
                 "missing": [{"dep": "ssl", "type": "link"}]}
    mod.print_report(parsed, rpm_check)
    out = capsys.readouterr().out
    assert "Rust 包 RPM 依赖分析报告" in out
    assert "rust-version : >= 1.65" in out
    assert "openssl-devel" in out
    assert "BuildRequires: rust >= 1.65" in out


def test_main_output_json(tmp_path, capsys, monkeypatch):
    _write(tmp_path, "Cargo.toml", CARGO_TOML)
    _write(tmp_path, "build.rs", 'fn main() { println!("cargo:rustc-link-lib=ssl"); }\n')
    _write(tmp_path, "src/a.c", "")
    out_json = tmp_path / "result.json"
    monkeypatch.setattr(sys, "argv", ["analyze_rust_deps.py", str(tmp_path), "-o", str(out_json)])
    mod.main()
    result = json.loads(out_json.read_text())
    assert result["rust_version"] == "1.65"
    assert result["links"] == ["ssl"]
    assert result["pkg_configs"] == []
    assert result["link_libs"] == ["ssl"]
    assert result["c_sources"] == ["src/a.c"]
    assert result["build_requires"] == ["rust >= 1.65", "cargo"]
    assert result["rpm_check"] is None


def test_main_manifest_path(tmp_path, monkeypatch):
    # 混合包场景:--manifest-path 指向子目录 Cargo.toml
    _write(tmp_path, "rust/Cargo.toml", '[package]\nname = "core"\nlinks = "z"\n')
    _write(tmp_path, "rust/build.rs", 'fn main() { println!("cargo:rustc-link-lib=z"); }\n')
    out_json = tmp_path / "r.json"
    monkeypatch.setattr(sys, "argv", [
        "analyze_rust_deps.py", str(tmp_path),
        "--manifest-path", str(tmp_path / "rust" / "Cargo.toml"),
        "-o", str(out_json),
    ])
    mod.main()
    result = json.loads(out_json.read_text())
    assert result["links"] == ["z"]
    assert result["link_libs"] == ["z"]


def test_main_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["analyze_rust_deps.py", str(tmp_path / "nope")])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1


def test_main_check_rpm_no_sys_deps_skips(tmp_path, capsys, monkeypatch):
    _write(tmp_path, "Cargo.toml", '[package]\nname = "x"\n\n[dependencies]\nserde = "1.0"\n')
    monkeypatch.setattr(sys, "argv", ["analyze_rust_deps.py", str(tmp_path), "--check-rpm"])
    mod.main()
    assert "跳过 RPM 查询" in capsys.readouterr().out


def test_main_check_rpm_missing_exit2(tmp_path, monkeypatch):
    _write(tmp_path, "Cargo.toml", 'links = "foo"\n')
    monkeypatch.setattr(sys, "argv", ["analyze_rust_deps.py", str(tmp_path), "--check-rpm"])
    def fake(tasks, timeout=120, **kw):
        out = []
        for t in tasks:
            base = {k: v for k, v in t.items() if k not in {"queries", "prefer_devel"}}
            out.append({**base, "rpm": None, "version": None, "release": None, "level": ""})
        return out
    monkeypatch.setattr(mod, "run_batch_lookup", fake)
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 2
