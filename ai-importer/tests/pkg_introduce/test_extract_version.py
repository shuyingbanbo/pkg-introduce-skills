"""extract_version.py — 各语言版本抽取(VERSION 文件 / Go 常量 / git describe / EXTRACTORS)。"""

from __future__ import annotations

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

_D = SCRIPT_DIRS["pkg_introduce"]

# extract_version 顶层 import java/nodejs/python/rust 四个 metadata 模块,
# 先经 load_module 注册进 sys.modules,`from xxx_metadata import ...` 即可命中。
for _n in ("python_metadata", "nodejs_metadata", "rust_metadata", "java_metadata"):
    load_module(_n, _D / f"{_n}.py")

ev = load_module("extract_version", _D / "extract_version.py")


# ─────────────────────────────────────────────
# extract_generic_version(VERSION 文件)
# ─────────────────────────────────────────────

@pytest.mark.parametrize("content,expected", [
    ("1.2.3", "1.2.3"),
    ("v1.2.3", "1.2.3"),                # 剥 v 前缀
    ("v1.2.3\nsecond line", "1.2.3"),   # 只取第一行
    ("  1.0.0  ", "1.0.0"),             # 首尾空白
    ("", ""),                           # 空文件
])
def test_extract_generic_version(tmp_path, content, expected):
    (tmp_path / "VERSION").write_text(content)
    assert ev.extract_generic_version(str(tmp_path)) == expected


def test_extract_generic_version_no_file(tmp_path):
    assert ev.extract_generic_version(str(tmp_path)) == ""


# ─────────────────────────────────────────────
# c / cpp / ruby 直接复用 generic
# ─────────────────────────────────────────────

@pytest.mark.parametrize("fn_name", ["extract_c_version", "extract_cpp_version", "extract_ruby_version"])
def test_c_cpp_ruby_versions(tmp_path, fn_name):
    (tmp_path / "VERSION").write_text("v2.0.0\n")
    assert getattr(ev, fn_name)(str(tmp_path)) == "2.0.0"
    (tmp_path / "VERSION").unlink()
    assert getattr(ev, fn_name)(str(tmp_path)) == ""


# ─────────────────────────────────────────────
# extract_go_version
# ─────────────────────────────────────────────

def test_go_version_from_version_file(tmp_path):
    (tmp_path / "VERSION").write_text("v0.9\n")
    assert ev.extract_go_version(str(tmp_path)) == "0.9"


def test_go_version_var_in_top_level_file(tmp_path):
    (tmp_path / "main.go").write_text('package main\n\nvar version = "0.72"\n')
    assert ev.extract_go_version(str(tmp_path)) == "0.72"


def test_go_version_const_in_top_level_file(tmp_path):
    (tmp_path / "main.go").write_text('package main\n\nconst Version = "1.2.3"\n')
    assert ev.extract_go_version(str(tmp_path)) == "1.2.3"


def test_go_version_from_cmd_subdir(tmp_path):
    (tmp_path / "cmd").mkdir()
    (tmp_path / "cmd" / "x.go").write_text('package main\nconst Version = "1.2.3"\n')
    assert ev.extract_go_version(str(tmp_path)) == "1.2.3"


def test_go_version_main_subdir(tmp_path):
    (tmp_path / "main").mkdir()
    (tmp_path / "main" / "x.go").write_text('package main\nvar version = "0.5.0"\n')
    assert ev.extract_go_version(str(tmp_path)) == "0.5.0"


def test_go_version_v_prefix_not_matched(tmp_path):
    # 注:_GO_VERSION_RE 要求引号后首字符为数字,`"v0.72"` 不匹配(实际行为)
    (tmp_path / "main.go").write_text('package main\nvar version = "v0.72"\n')
    assert ev.extract_go_version(str(tmp_path)) == ""


def test_go_version_git_describe_fallback(fake_subprocess, tmp_path):
    fake_subprocess.when("git -C", stdout="v2.0.0\n", returncode=0)
    assert ev.extract_go_version(str(tmp_path)) == "2.0.0"


def test_go_version_git_describe_failure(fake_subprocess, tmp_path):
    fake_subprocess.when("git -C", returncode=128, stderr="fatal")
    assert ev.extract_go_version(str(tmp_path)) == ""


def test_go_version_git_describe_exception(fake_subprocess, tmp_path):
    fake_subprocess.when("git -C", exc=FileNotFoundError())
    assert ev.extract_go_version(str(tmp_path)) == ""


def test_go_version_unreadable_file_skipped(fake_subprocess, tmp_path):
    # 悬空符号链接的 .go 文件 → read_text 抛 OSError → 跳过继续
    import os
    os.symlink(str(tmp_path / "missing.go"), str(tmp_path / "x.go"))
    fake_subprocess.when("git -C", returncode=128, stderr="fatal")
    assert ev.extract_go_version(str(tmp_path)) == ""


# ─────────────────────────────────────────────
# EXTRACTORS 注册表
# ─────────────────────────────────────────────

def test_extractors_registry():
    assert set(ev.EXTRACTORS) == {"python", "rust", "nodejs", "java", "go", "c", "cpp", "ruby"}
    assert ev.EXTRACTORS["c"] is ev.extract_c_version
    assert ev.EXTRACTORS["cpp"] is ev.extract_cpp_version
    assert ev.EXTRACTORS["ruby"] is ev.extract_ruby_version
    assert ev.EXTRACTORS["go"] is ev.extract_go_version
    assert ev.EXTRACTORS["python"].__name__ == "extract_python_version"
    assert ev.EXTRACTORS["rust"].__name__ == "extract_rust_version"
    assert ev.EXTRACTORS["nodejs"].__name__ == "extract_nodejs_version"
    assert ev.EXTRACTORS["java"].__name__ == "extract_java_version"


# ─────────────────────────────────────────────
# CLI 入口(模块只有 if __name__ == "__main__" 块,没有 main() 函数,
# 以 __name__="__main__" 重新执行源码来测试入口逻辑)
# ─────────────────────────────────────────────

import contextlib
import io


def _run_cli(argv):
    src = (_D / "extract_version.py").read_text(encoding="utf-8")
    ns = {"__name__": "__main__", "__file__": str(_D / "extract_version.py")}
    out, err = io.StringIO(), io.StringIO()
    import sys as _sys
    old_argv = _sys.argv
    _sys.argv = argv
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                exec(compile(src, str(_D / "extract_version.py"), "exec"), ns)
                code = 0
            except SystemExit as ei:
                code = ei.code
    finally:
        _sys.argv = old_argv
    return code, out.getvalue(), err.getvalue()


def test_cli_no_args_exits():
    code, _, err = _run_cli(["extract_version.py"])
    assert code == 2
    assert "usage" in err


def test_cli_unsupported_language_exits(tmp_path):
    code, _, err = _run_cli(["extract_version.py", "haskell", str(tmp_path)])
    assert code == 2
    assert "unsupported language" in err


def test_cli_prints_version(tmp_path):
    (tmp_path / "VERSION").write_text("1.2.3\n")
    code, out, _ = _run_cli(["extract_version.py", "c", str(tmp_path)])
    assert code == 0
    assert out == "1.2.3\n"


def test_cli_lang_case_insensitive(tmp_path):
    (tmp_path / "VERSION").write_text("0.5\n")
    code, out, _ = _run_cli(["extract_version.py", "Go", str(tmp_path)])
    assert code == 0
    assert out == "0.5\n"
