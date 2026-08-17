"""evaluate-deps.py — 依赖评估与注册(extract/cascade/register,全 subprocess mock)。"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sys.path.insert(0, str(SCRIPT_DIRS["step"]))

ed = load_module("evaluate-deps", SCRIPT_DIRS["step"] / "evaluate-deps.py")


# ─────────────────────────────────────────────
# _extract_deps:分析 JSON 合并解析
# ─────────────────────────────────────────────

def _analysis(dep_items=None, build_items=None, missing=None, conflict=None):
    return {
        "dependency_items": dep_items or [],
        "build_sys_dependency_items": build_items or [],
        "rpm_check": {
            "missing": missing or [],
            "version_conflict": conflict or [],
        },
    }


def test_extract_deps_merges_all_sources(tmp_path, fake_subprocess):
    """dependency_items + build_sys + missing + version_conflict 全来源合并去重。"""
    pkg_dir = tmp_path / "pkgs" / "main"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "evaluate_deps_main_analysis.json").write_text(json.dumps(_analysis(
        dep_items=[{"name": "liba", "requirement": ">= 1.0", "upstream_url": "u1", "rpm_pkg_name": "liba"}],
        build_items=[{"name": "libb", "requirement": "", "upstream_url": "", "rpm_pkg_name": "libb"}],
        missing=[{"name": "libc", "requirement": ">= 2.0", "upstream_url": "", "rpm_name": "libc"}],
        conflict=[{"name": "liba", "requirement": ">= 1.0", "upstream_url": "", "rpm": "liba"}],  # 重复 → 跳过
    )))
    fake_subprocess.when(lambda s: "analyze_python_deps.py" in s, returncode=0)

    deps = ed._extract_deps("python", str(tmp_path / "src"), "main", pkg_dir)
    names = [d["name"] for d in deps]
    assert names == ["liba", "libb", "libc"]  # 去重 + 顺序


def test_extract_deps_excludes_pkgname_in_build_items(tmp_path, fake_subprocess):
    """build_sys 里的主包自身被排除。"""
    pkg_dir = tmp_path / "pkgs" / "main"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "evaluate_deps_main_analysis.json").write_text(json.dumps(_analysis(
        build_items=[{"name": "main", "requirement": "", "upstream_url": "", "rpm_pkg_name": "main"}])))
    fake_subprocess.when(lambda s: "analyze_python_deps.py" in s, returncode=0)
    deps = ed._extract_deps("python", str(tmp_path / "src"), "main", pkg_dir)
    assert deps == []


def test_extract_deps_unsupported_lang(tmp_path, fake_subprocess):
    assert ed._extract_deps("haskell", "src", "main", tmp_path) == []


def test_extract_deps_analyzer_failure(tmp_path, fake_subprocess):
    pkg_dir = tmp_path / "pkgs" / "main"
    pkg_dir.mkdir(parents=True)
    fake_subprocess.when(lambda s: "analyze_python_deps.py" in s, returncode=3, stderr="boom")
    deps = ed._extract_deps("python", "src", "main", pkg_dir)
    assert deps == []


def test_extract_deps_analysis_read_failure(tmp_path, fake_subprocess):
    pkg_dir = tmp_path / "pkgs" / "main"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "evaluate_deps_main_analysis.json").write_text("{bad")
    fake_subprocess.when(lambda s: "analyze_python_deps.py" in s, returncode=0)
    deps = ed._extract_deps("python", "src", "main", pkg_dir)
    assert deps == []


# ─────────────────────────────────────────────
# _cascade_check_dep:环境变量门控
# ─────────────────────────────────────────────

def test_cascade_check_dep_missing_env(monkeypatch):
    monkeypatch.delenv("COPR_FRONTEND_URL", raising=False)
    assert ed._cascade_check_dep({"name": "liba"}, "python", None) is None


def test_cascade_check_dep_calls_cascade(monkeypatch):
    for k in ("COPR_FRONTEND_URL", "COPR_OWNER", "COPR_PROJECT",
              "COPR_API_LOGIN", "COPR_API_TOKEN", "COPR_CHROOT"):
        monkeypatch.setenv(k, f"v-{k}")
    calls = []
    fake_cascade = SimpleNamespace(check_package_existence=(
        lambda *a, **k: (calls.append((a, k)) or {"decision": "introduce_new"})))
    result = ed._cascade_check_dep({"name": "liba", "requirement": ">= 1.0"}, "python", fake_cascade)
    assert result["decision"] == "introduce_new"
    args, kwargs = calls[0]
    assert args[0] == "liba"
    assert kwargs["requirement"] == ">= 1.0"
    assert kwargs["target"] == "v-COPR_CHROOT"


def test_cascade_check_dep_exception_returns_none(monkeypatch, capsys):
    for k in ("COPR_FRONTEND_URL", "COPR_OWNER", "COPR_PROJECT",
              "COPR_API_LOGIN", "COPR_API_TOKEN"):
        monkeypatch.setenv(k, "v")
    def boom(*a, **k):
        raise RuntimeError("net down")
    assert ed._cascade_check_dep({"name": "liba"}, "python",
                                 SimpleNamespace(check_package_existence=boom)) is None
    assert "级联检查" in capsys.readouterr().err


# ─────────────────────────────────────────────
# _register_dep:register-dep 子进程
# ─────────────────────────────────────────────

def test_register_dep_success(tmp_path, fake_subprocess):
    fake_subprocess.when(lambda s: "register-dep.py" in s, returncode=0)
    assert ed._register_dep({"name": "liba", "requirement": ">= 1.0"}, "main", str(tmp_path)) is True


def test_register_dep_toolchain_skip(tmp_path, fake_subprocess, capsys):
    """exit 2 + toolchain → 跳过而非报错。"""
    fake_subprocess.when(lambda s: "register-dep.py" in s, returncode=2,
                         stderr="is a toolchain package")
    assert ed._register_dep({"name": "gcc"}, "main", str(tmp_path)) is True
    assert "跳过工具链包" in capsys.readouterr().err


def test_register_dep_failure(tmp_path, fake_subprocess, capsys):
    fake_subprocess.when(lambda s: "register-dep.py" in s, returncode=1, stderr="boom")
    assert ed._register_dep({"name": "liba"}, "main", str(tmp_path)) is False
    assert "register-dep 失败" in capsys.readouterr().err


def test_register_dep_with_url_and_lang(tmp_path, fake_subprocess):
    fake_subprocess.when(lambda s: "register-dep.py" in s, returncode=0)
    ed._register_dep({"name": "liba", "requirement": "", "upstream_url": "https://u",
                      "lang": "rust"}, "main", str(tmp_path))
    cmd = next(c for c, _ in fake_subprocess.calls if "register-dep.py" in " ".join(c))
    assert "--url" in cmd and "https://u" in cmd
    assert "--lang" in cmd and "rust" in cmd
