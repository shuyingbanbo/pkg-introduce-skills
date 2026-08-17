"""四个语言 metadata 模块的本地解析函数测试(python/nodejs/rust/java,不测网络)。

python_metadata: load_toml / _scan_version_in_file / _dynamic_version_fallback / extract_python_version
nodejs_metadata: extract_nodejs_version
rust_metadata:   load_toml / extract_rust_version
java_metadata:   _strip_namespace / _find_first_text / _extract_*_version / extract_java_version / detect_java_build_system
"""

from __future__ import annotations

import contextlib
import io
import json
import xml.etree.ElementTree as ET

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

_D = SCRIPT_DIRS["pkg_introduce"]

py_md = load_module("python_metadata", _D / "python_metadata.py")
node_md = load_module("nodejs_metadata", _D / "nodejs_metadata.py")
rust_md = load_module("rust_metadata", _D / "rust_metadata.py")
java_md = load_module("java_metadata", _D / "java_metadata.py")


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def _run_cli(module_path, argv):
    """以 __name__="__main__" 重新执行模块源码,测试 CLI 入口块。"""
    src = module_path.read_text(encoding="utf-8")
    ns = {"__name__": "__main__", "__file__": str(module_path)}
    out, err = io.StringIO(), io.StringIO()
    import sys as _sys
    old_argv = _sys.argv
    _sys.argv = argv
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                exec(compile(src, str(module_path), "exec"), ns)
                code = 0
            except SystemExit as ei:
                code = ei.code
    finally:
        _sys.argv = old_argv
    return code, out.getvalue(), err.getvalue()


# ═════════════════════════════════════════
# nodejs_metadata
# ═════════════════════════════════════════

@pytest.mark.parametrize("content,expected", [
    (json.dumps({"name": "x", "version": "1.0.0"}), "1.0.0"),
    (json.dumps({"version": " 1.2.3 "}), "1.2.3"),       # strip 首尾空白
    (json.dumps({"version": "v1.0.0"}), "v1.0.0"),       # 注:v 前缀原样保留(实际行为)
    (json.dumps({"version": 123}), ""),                  # 非字符串版本 → 空
    (json.dumps({"name": "no-version"}), ""),            # 无 version 字段
    ("{not json", ""),                                   # 非法 JSON
])
def test_extract_nodejs_version(tmp_path, content, expected):
    _write(tmp_path, "package.json", content)
    assert node_md.extract_nodejs_version(str(tmp_path)) == expected


def test_extract_nodejs_version_no_file(tmp_path):
    assert node_md.extract_nodejs_version(str(tmp_path)) == ""


# ═════════════════════════════════════════
# rust_metadata
# ═════════════════════════════════════════

@pytest.mark.parametrize("content,expected", [
    ('[package]\nname = "x"\nversion = "0.1.0"\n', "0.1.0"),
    ('[package]\nname = "x"\nversion = " 0.2.0 "\n', "0.2.0"),
    ('[workspace]\nmembers = ["x"]\n', ""),              # 只有 workspace,无 package → 空
    ('[package]\nname = "x"\nversion = 1.2\n', ""),      # 非字符串版本 → 空
    ("[not-valid-toml\n", ""),
])
def test_extract_rust_version(tmp_path, content, expected):
    _write(tmp_path, "Cargo.toml", content)
    assert rust_md.extract_rust_version(str(tmp_path)) == expected


def test_extract_rust_version_no_file(tmp_path):
    assert rust_md.extract_rust_version(str(tmp_path)) == ""


# ═════════════════════════════════════════
# python_metadata — load_toml / _scan_version_in_file
# ═════════════════════════════════════════

def test_python_load_toml(tmp_path):
    assert py_md.load_toml(tmp_path / "nope.toml") == {}          # 文件不存在
    p = _write(tmp_path, "bad.toml", "[not-valid\n")
    assert py_md.load_toml(p) == {}
    p = _write(tmp_path, "ok.toml", 'x = "1"\n[tool]\ny = "2"\n')
    assert py_md.load_toml(p) == {"x": "1", "tool": {"y": "2"}}


@pytest.mark.parametrize("content,expected", [
    ('__version__ = "1.2.3"\n', "1.2.3"),
    ("version = '1.0'\n", "1.0"),
    ('def f():\n    __version__ = "x"\n', "x"),   # 注:^[ \t]* 允许缩进,函数体内也会命中
    ("no version here\n", ""),
])
def test_python_scan_version_in_file(tmp_path, content, expected):
    p = _write(tmp_path, "f.py", content)
    assert py_md._scan_version_in_file(p) == expected


def test_python_scan_version_in_file_missing(tmp_path):
    assert py_md._scan_version_in_file(tmp_path / "nope.py") == ""


# ─────────────────────────────────────────────
# _dynamic_version_fallback
# ─────────────────────────────────────────────

def test_dynamic_fallback_init_in_src_layout(tmp_path):
    _write(tmp_path, "src/my_pkg/__init__.py", '__version__ = "3.0.0"\n')
    assert py_md._dynamic_version_fallback(tmp_path, "my-pkg") == "3.0.0"


def test_dynamic_fallback_init_top_level(tmp_path):
    _write(tmp_path, "my_pkg/__init__.py", '__version__ = "3.1.0"\n')
    assert py_md._dynamic_version_fallback(tmp_path, "my-pkg") == "3.1.0"


def test_dynamic_fallback_single_file_module(tmp_path):
    _write(tmp_path, "my_pkg.py", '__version__ = "3.2.0"\n')
    assert py_md._dynamic_version_fallback(tmp_path, "my-pkg") == "3.2.0"


def test_dynamic_fallback_version_file_variants(tmp_path):
    _write(tmp_path, "my_pkg/_version.py", '__version__ = "3.3.0"\n')
    assert py_md._dynamic_version_fallback(tmp_path, "my-pkg") == "3.3.0"
    (tmp_path / "my_pkg").rename(tmp_path / "other")
    _write(tmp_path, "_version.py", '__version__ = "3.4.0"\n')
    assert py_md._dynamic_version_fallback(tmp_path, "my-pkg") == "3.4.0"


def test_dynamic_fallback_src_glob(tmp_path):
    _write(tmp_path, "src/pkg_a/__init__.py", '__version__ = "9.1.0"\n')
    _write(tmp_path, "src/pkg_b/__init__.py", '__version__ = "9.2.0"\n')
    assert py_md._dynamic_version_fallback(tmp_path, "no-such-pkg") == "9.1.0"


def test_dynamic_fallback_changelog(tmp_path):
    _write(tmp_path, "CHANGELOG.md", "# Changelog\n\n## 1.5.0 (2024-01-01)\n...\n")
    assert py_md._dynamic_version_fallback(tmp_path, "no-such-pkg") == "1.5.0"


def test_dynamic_fallback_nothing(tmp_path):
    assert py_md._dynamic_version_fallback(tmp_path, "no-such-pkg") == ""


# ─────────────────────────────────────────────
# extract_python_version
# ─────────────────────────────────────────────

def test_extract_python_version_static_pyproject(tmp_path):
    _write(tmp_path, "pyproject.toml", '[project]\nname = "my-pkg"\nversion = "1.2.3"\n')
    assert py_md.extract_python_version(str(tmp_path)) == "1.2.3"


def test_extract_python_version_dynamic_hatch_path(tmp_path):
    _write(tmp_path, "pyproject.toml",
           '[project]\nname = "my-pkg"\ndynamic = ["version"]\n\n'
           '[tool.hatch.version]\npath = "pkg/__version__.py"\n')
    _write(tmp_path, "pkg/__version__.py", '__version__ = "2.0.0"\n')
    assert py_md.extract_python_version(str(tmp_path)) == "2.0.0"


def test_extract_python_version_dynamic_fallback_init(tmp_path):
    _write(tmp_path, "pyproject.toml",
           '[project]\nname = "my-pkg"\ndynamic = ["version"]\n')
    _write(tmp_path, "src/my_pkg/__init__.py", '__version__ = "3.0.0"\n')
    assert py_md.extract_python_version(str(tmp_path)) == "3.0.0"


def test_extract_python_version_poetry(tmp_path):
    _write(tmp_path, "pyproject.toml", '[tool.poetry]\nname = "x"\nversion = "4.5.6"\n')
    assert py_md.extract_python_version(str(tmp_path)) == "4.5.6"


def test_extract_python_version_project_beats_poetry(tmp_path):
    _write(tmp_path, "pyproject.toml",
           '[project]\nversion = "1.0.0"\n\n[tool.poetry]\nversion = "4.5.6"\n')
    assert py_md.extract_python_version(str(tmp_path)) == "1.0.0"


def test_extract_python_version_version_file(tmp_path):
    _write(tmp_path, "VERSION", "v2.5.0\n")
    # 注:VERSION 分支只 strip 不剥 v 前缀(与 extract_generic_version 不同)
    assert py_md.extract_python_version(str(tmp_path)) == "v2.5.0"


def test_extract_python_version_setup_py(tmp_path):
    _write(tmp_path, "setup.py", 'setup(\n    version = "5.0.0",\n)\n')
    assert py_md.extract_python_version(str(tmp_path)) == "5.0.0"
    _write(tmp_path, "setup.py", '__version__ = "5.1.0"\n')
    assert py_md.extract_python_version(str(tmp_path)) == "5.1.0"


def test_extract_python_version_empty(tmp_path):
    assert py_md.extract_python_version(str(tmp_path)) == ""


def test_extract_python_version_invalid_pyproject_falls_through(tmp_path):
    _write(tmp_path, "pyproject.toml", "[not-valid\n")
    _write(tmp_path, "VERSION", "7.7.7\n")
    assert py_md.extract_python_version(str(tmp_path)) == "7.7.7"


# ═════════════════════════════════════════
# java_metadata — 内部辅助函数
# ═════════════════════════════════════════

@pytest.mark.parametrize("tag,expected", [
    ("{http://maven.apache.org/POM/4.0.0}version", "version"),
    ("version", "version"),
    ("{}version", "version"),
])
def test_java_strip_namespace(tag, expected):
    assert java_md._strip_namespace(tag) == expected


def test_java_find_first_text():
    root = ET.fromstring(
        "<project><groupId>com.x</groupId><version>1.0</version></project>")
    assert java_md._find_first_text(root, "version") == "1.0"
    assert java_md._find_first_text(root, "artifactId") == ""
    # 空文本节点被跳过
    root = ET.fromstring("<project><version>   </version><name>n</name></project>")
    assert java_md._find_first_text(root, "version") == ""


def test_java_extract_project_version_direct_child():
    root = ET.fromstring("<project><version>1.0</version></project>")
    assert java_md._extract_project_version(root) == "1.0"
    # 属性占位跳过
    root = ET.fromstring("<project><version>${revision}</version></project>")
    assert java_md._extract_project_version(root) == ""
    # 插件配置里的 version 不是直接子节点 → 不误取
    root = ET.fromstring(
        "<project><build><plugins><plugin><version>2.5</version></plugin></plugins></build></project>")
    assert java_md._extract_project_version(root) == ""


def test_java_extract_parent_version():
    root = ET.fromstring(
        "<project><parent><version>9.9</version></parent></project>")
    assert java_md._extract_parent_version(root) == "9.9"
    root = ET.fromstring(
        "<project><parent><version>${revision}</version></parent></project>")
    assert java_md._extract_parent_version(root) == ""


def test_java_try_pom_invalid_xml(tmp_path):
    p = _write(tmp_path, "pom.xml", "<project><version>1.0")
    assert java_md._try_pom(p) == ""


# ─────────────────────────────────────────────
# extract_java_version
# ─────────────────────────────────────────────

def test_extract_java_version_direct(tmp_path):
    _write(tmp_path, "pom.xml", "<project><version>1.2.3</version></project>")
    assert java_md.extract_java_version(str(tmp_path)) == "1.2.3"


def test_extract_java_version_namespace_parent_fallback(tmp_path):
    _write(tmp_path, "pom.xml",
           '<project xmlns="http://maven.apache.org/POM/4.0.0">'
           "<parent><version>9.9</version></parent></project>")
    assert java_md.extract_java_version(str(tmp_path)) == "9.9"


def test_extract_java_version_direct_beats_parent(tmp_path):
    _write(tmp_path, "pom.xml",
           "<project><version>1.0</version>"
           "<parent><version>9.9</version></parent></project>")
    assert java_md.extract_java_version(str(tmp_path)) == "1.0"


def test_extract_java_version_submodule_pom(tmp_path):
    _write(tmp_path, "pom.xml",
           "<project><parent><version>${revision}</version></parent></project>")
    _write(tmp_path, "sub/pom.xml", "<project><version>3.0.0</version></project>")
    assert java_md.extract_java_version(str(tmp_path)) == "3.0.0"


def test_extract_java_version_gradle_properties(tmp_path):
    _write(tmp_path, "gradle.properties", "version=1.2.3\n")
    assert java_md.extract_java_version(str(tmp_path)) == "1.2.3"


def test_extract_java_version_gradle_properties_beats_build_gradle(tmp_path):
    _write(tmp_path, "gradle.properties", "version=1.2.3\n")
    _write(tmp_path, "build.gradle", "version = '9.9.9'\n")
    assert java_md.extract_java_version(str(tmp_path)) == "1.2.3"


def test_extract_java_version_build_gradle_equals(tmp_path):
    _write(tmp_path, "build.gradle", "plugins { }\nversion = '1.2.4'\n")
    assert java_md.extract_java_version(str(tmp_path)) == "1.2.4"


def test_extract_java_version_build_gradle_no_equals(tmp_path):
    _write(tmp_path, "build.gradle", "plugins { }\nversion '1.2.9'\n")
    assert java_md.extract_java_version(str(tmp_path)) == "1.2.9"


def test_extract_java_version_build_gradle_kts(tmp_path):
    _write(tmp_path, "build.gradle.kts", 'version = "1.2.5"\n')
    assert java_md.extract_java_version(str(tmp_path)) == "1.2.5"


def test_extract_java_version_libs_versions_toml_exact_key(tmp_path):
    _write(tmp_path / "my-pkg", "gradle/libs.versions.toml", '[versions]\nmy_pkg = "2.0.0"\n')
    assert java_md.extract_java_version(str(tmp_path / "my-pkg")) == "2.0.0"


def test_extract_java_version_libs_versions_toml_generic(tmp_path):
    _write(tmp_path / "my-pkg", "gradle/libs.versions.toml", '[versions]\nother = "1.2.3"\n')
    assert java_md.extract_java_version(str(tmp_path / "my-pkg")) == "1.2.3"


def test_extract_java_version_nothing(tmp_path):
    assert java_md.extract_java_version(str(tmp_path)) == ""


def test_extract_java_version_pom_plugin_version_only_is_ignored(tmp_path):
    # 只有插件 version 的 pom 不应被误取,无其他文件 → 空
    _write(tmp_path, "pom.xml",
           "<project><build><plugins><plugin><version>2.5</version></plugin></plugins></build></project>")
    assert java_md.extract_java_version(str(tmp_path)) == ""


# ─────────────────────────────────────────────
# detect_java_build_system
# ─────────────────────────────────────────────

@pytest.mark.parametrize("files,expected", [
    (["pom.xml"], "maven"),
    (["pom.xml", "build.gradle"], "maven"),          # 混合项目 maven 优先
    (["build.gradle"], "gradle"),
    (["build.gradle.kts"], "gradle"),
    (["settings.gradle"], "gradle"),
    (["settings.gradle.kts"], "gradle"),
    ([], "unknown"),
])
def test_detect_java_build_system(tmp_path, files, expected):
    for f in files:
        _write(tmp_path, f, "")
    assert java_md.detect_java_build_system(str(tmp_path)) == expected


# ═════════════════════════════════════════
# OSError 守卫分支
# ═════════════════════════════════════════

def test_python_dynamic_fallback_changelog_read_error(tmp_path):
    (tmp_path / "CHANGELOG.md").mkdir()   # 目录 → read_text 抛 IsADirectoryError → 跳过
    assert py_md._dynamic_version_fallback(tmp_path, "no-such-pkg") == ""


def test_java_libs_toml_read_error(tmp_path):
    (tmp_path / "gradle" / "libs.versions.toml").mkdir(parents=True)
    assert java_md.extract_java_version(str(tmp_path)) == ""


# ═════════════════════════════════════════
# tomllib → tomli → pip._vendor.tomli 兜底链
# ═════════════════════════════════════════

def _exec_with_blocked_imports(module_name, blocked):
    import builtins
    src = (_D / f"{module_name}.py").read_text(encoding="utf-8")
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name in blocked:
            raise ImportError(f"blocked: {name}")
        return real_import(name, *a, **kw)

    return src, real_import, fake_import


def test_python_metadata_pip_vendor_tomli_fallback(monkeypatch, tmp_path):
    import builtins
    src, real_import, fake_import = _exec_with_blocked_imports(
        "python_metadata", ("tomllib", "tomli"))
    monkeypatch.setattr(builtins, "__import__", fake_import)
    ns = {"__name__": "python_metadata_vendor_fallback"}
    exec(compile(src, str(_D / "python_metadata.py"), "exec"), ns)
    assert ns["tomllib"] is not None          # 兜底链最终落在 pip._vendor.tomli
    _write(tmp_path, "pyproject.toml", '[project]\nversion = "6.6.6"\n')
    # 注:本机 pip._vendor.tomli(1.0.3)是 str 模式 shim,而 load_toml 以 "rb" 打开
    # → TypeError 被 except Exception 吞掉 → 静默返回 {}(实际行为,版本解析结果为空)
    assert ns["load_toml"](tmp_path / "pyproject.toml") == {}
    assert ns["extract_python_version"](str(tmp_path)) == ""
    assert ns["load_toml"](tmp_path / "nope.toml") == {}


def test_rust_metadata_pip_vendor_tomli_fallback(monkeypatch, tmp_path):
    import builtins
    src, real_import, fake_import = _exec_with_blocked_imports(
        "rust_metadata", ("tomllib", "tomli"))
    monkeypatch.setattr(builtins, "__import__", fake_import)
    ns = {"__name__": "rust_metadata_vendor_fallback"}
    exec(compile(src, str(_D / "rust_metadata.py"), "exec"), ns)
    assert ns["tomllib"] is not None          # 兜底链最终落在 pip._vendor.tomli
    _write(tmp_path, "Cargo.toml", '[package]\nname = "x"\nversion = "0.9.9"\n')
    # 同上:rb 模式 + pip shim → TypeError 被吞 → 静默返回 {}(实际行为)
    assert ns["extract_rust_version"](str(tmp_path)) == ""


@pytest.mark.parametrize("filename", ["python_metadata", "rust_metadata"])
def test_metadata_tomllib_fully_unavailable(monkeypatch, tmp_path, filename):
    # 三级兜底全部失败 → tomllib = None → load_toml 恒返回 {}
    import builtins
    src, real_import, fake_import = _exec_with_blocked_imports(
        filename, ("tomllib", "tomli", "pip._vendor"))
    monkeypatch.setattr(builtins, "__import__", fake_import)
    ns = {"__name__": f"{filename}_no_toml"}
    exec(compile(src, str(_D / f"{filename}.py"), "exec"), ns)
    assert ns["tomllib"] is None
    _write(tmp_path, "pyproject.toml", '[project]\nversion = "6.6.6"\n')
    assert ns["load_toml"](tmp_path / "pyproject.toml") == {}
    if filename == "python_metadata":
        assert ns["extract_python_version"](str(tmp_path)) == ""
    else:
        assert ns["extract_rust_version"](str(tmp_path)) == ""


# ═════════════════════════════════════════
# 四个模块的 CLI 入口块
# ═════════════════════════════════════════

@pytest.mark.parametrize("filename", [
    "python_metadata.py", "nodejs_metadata.py", "rust_metadata.py", "java_metadata.py",
])
def test_metadata_cli_bad_args_exit2(filename):
    code, _, err = _run_cli(_D / filename, [filename])
    assert code == 2
    assert "usage" in err


def test_python_metadata_cli_prints_version(tmp_path):
    _write(tmp_path, "pyproject.toml", '[project]\nversion = "1.2.3"\n')
    code, out, _ = _run_cli(_D / "python_metadata.py",
                            ["python_metadata.py", "version", str(tmp_path)])
    assert code == 0
    assert out == "1.2.3\n"


def test_nodejs_metadata_cli_prints_version(tmp_path):
    _write(tmp_path, "package.json", json.dumps({"version": "1.0.0"}))
    code, out, _ = _run_cli(_D / "nodejs_metadata.py",
                            ["nodejs_metadata.py", "version", str(tmp_path)])
    assert code == 0
    assert out == "1.0.0\n"


def test_rust_metadata_cli_prints_version(tmp_path):
    _write(tmp_path, "Cargo.toml", '[package]\nname = "x"\nversion = "0.1.0"\n')
    code, out, _ = _run_cli(_D / "rust_metadata.py",
                            ["rust_metadata.py", "version", str(tmp_path)])
    assert code == 0
    assert out == "0.1.0\n"


def test_java_metadata_cli_prints_version(tmp_path):
    _write(tmp_path, "pom.xml", "<project><version>1.2.3</version></project>")
    code, out, _ = _run_cli(_D / "java_metadata.py",
                            ["java_metadata.py", "version", str(tmp_path)])
    assert code == 0
    assert out == "1.2.3\n"


def test_java_metadata_cli_prints_build_system(tmp_path):
    _write(tmp_path, "pom.xml", "<project><version>1.2.3</version></project>")
    code, out, _ = _run_cli(_D / "java_metadata.py",
                            ["java_metadata.py", "build-system", str(tmp_path)])
    assert code == 0
    assert out == "maven\n"


def test_java_metadata_cli_unknown_subcommand_exit2(tmp_path):
    code, _, err = _run_cli(_D / "java_metadata.py",
                            ["java_metadata.py", "other", str(tmp_path)])
    assert code == 2
    assert "usage" in err
