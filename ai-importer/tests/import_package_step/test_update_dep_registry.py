"""update-dep-registry.py — 从 build_rpm_result 的 dep_needed 写 dep_registry。"""

from __future__ import annotations

import json

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

ud = load_module("update-dep-registry", SCRIPT_DIRS["step"] / "update-dep-registry.py")


def _run(monkeypatch, session_dir, *args):
    monkeypatch.setattr("sys.argv", ["update-dep-registry.py",
                                     "--session-dir", str(session_dir)] + list(args))
    return ud.main()


def _setup(tmp_path, result=None, registry=None, pre_check=None):
    pkg_dir = tmp_path / "pkgs" / "pkg"
    pkg_dir.mkdir(parents=True)
    if result is not None:
        (pkg_dir / "build_rpm_result.json").write_text(json.dumps(result))
    if registry is not None:
        (tmp_path / "dep_registry.json").write_text(json.dumps(registry))
    if pre_check is not None:
        (tmp_path / "reports").mkdir(exist_ok=True)
        (tmp_path / "reports" / "pre_check_pkg.json").write_text(json.dumps(pre_check))
    return tmp_path


def test_missing_result_returns(tmp_path, monkeypatch, capsys):
    rc = _run(monkeypatch, tmp_path, "--pkg", "pkg")
    assert rc is None
    assert "not found" in capsys.readouterr().out


def test_add_new_deps(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, result={
        "deps": [{"name": "libfoo", "url": "https://github.com/x/libfoo", "constraint": ">= 1.0"}],
    })
    rc = _run(monkeypatch, tmp_path, "--pkg", "pkg")
    reg = json.loads((tmp_path / "dep_registry.json").read_text())
    assert reg["libfoo"] == {
        "url": "https://github.com/x/libfoo",
        "constraint": ">= 1.0",
        "status": "pending_evaluate",
        "required_by": "pkg",
    }
    assert "added=['libfoo']" in capsys.readouterr().out


def test_add_from_pending_deps_with_precheck(tmp_path, monkeypatch, capsys):
    """pending_deps 纯名称列表 + pre_check 报告补全 url/constraint。"""
    _setup(tmp_path,
           result={"dependency_resolution": {"pending_deps": ["libbar"]}},
           pre_check={"dependency_decisions": [
               {"name": "libbar", "upstream_url": "https://github.com/y/libbar",
                "constraint": ">= 2.0"},
           ]})
    _run(monkeypatch, tmp_path, "--pkg", "pkg")
    reg = json.loads((tmp_path / "dep_registry.json").read_text())
    assert reg["libbar"]["url"] == "https://github.com/y/libbar"
    assert reg["libbar"]["constraint"] == ">= 2.0"


def test_add_pending_deps_without_precheck(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, result={"dependency_resolution": {"pending_deps": ["libbaz"]}})
    _run(monkeypatch, tmp_path, "--pkg", "pkg")
    reg = json.loads((tmp_path / "dep_registry.json").read_text())
    assert reg["libbaz"]["constraint"] == ""


def test_dedup_full_and_pending(tmp_path, monkeypatch, capsys):
    """同名依赖在 deps 和 pending_deps 同时出现 → 只注册一次。"""
    _setup(tmp_path, result={
        "deps": [{"name": "libfoo", "url": "u", "constraint": ">= 1.0"}],
        "dependency_resolution": {"pending_deps": ["libfoo"]},
    })
    _run(monkeypatch, tmp_path, "--pkg", "pkg")
    reg = json.loads((tmp_path / "dep_registry.json").read_text())
    assert len(reg) == 1


def test_update_existing_merge_constraint(tmp_path, monkeypatch, capsys):
    _setup(tmp_path,
           result={"deps": [{"name": "libfoo", "constraint": "< 3.0"}]},
           registry={"libfoo": {"url": "", "constraint": ">= 2.0",
                                "status": "pending_evaluate"}})
    _run(monkeypatch, tmp_path, "--pkg", "pkg")
    reg = json.loads((tmp_path / "dep_registry.json").read_text())
    assert reg["libfoo"]["constraint"] == ">=2.0, <3.0"
    assert "updated=['libfoo']" in capsys.readouterr().out


def test_update_existing_empty_old_constraint(tmp_path, monkeypatch, capsys):
    _setup(tmp_path,
           result={"deps": [{"name": "libfoo", "constraint": ">= 5.0"}]},
           registry={"libfoo": {"url": "", "constraint": "", "status": "pending_evaluate"}})
    _run(monkeypatch, tmp_path, "--pkg", "pkg")
    reg = json.loads((tmp_path / "dep_registry.json").read_text())
    assert reg["libfoo"]["constraint"] == ">= 5.0"


def test_conflict_exits_1_but_others_written(tmp_path, monkeypatch, capsys):
    """冲突只影响冲突条目,其余正常写入;整体退出码 1。"""
    _setup(tmp_path,
           result={"deps": [
               {"name": "libfoo", "constraint": "< 1.5"},   # 与已登记 >= 2.0 冲突
               {"name": "libnew", "constraint": ""},          # 正常新增
           ]},
           registry={"libfoo": {"url": "", "constraint": ">= 2.0",
                                "status": "pending_evaluate"}})
    with pytest.raises(SystemExit) as e:
        _run(monkeypatch, tmp_path, "--pkg", "pkg")
    assert e.value.code == 1
    reg = json.loads((tmp_path / "dep_registry.json").read_text())
    # 旧约束保留,新依赖写入
    assert reg["libfoo"]["constraint"] == ">= 2.0"
    assert "libnew" in reg
    out = capsys.readouterr().out
    assert "conflicts=" in out


def test_skip_toolchain(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, result={"deps": [{"name": "gcc"}, {"name": "libfoo"}]})
    _run(monkeypatch, tmp_path, "--pkg", "pkg")
    reg = json.loads((tmp_path / "dep_registry.json").read_text())
    assert "gcc" not in reg
    assert "libfoo" in reg
    assert "skip toolchain: gcc" in capsys.readouterr().out


def test_skip_invalid_dep_name(tmp_path, monkeypatch, capsys):
    """非法包名(含换行注入)跳过。"""
    _setup(tmp_path, result={"deps": [{"name": "evil\nname"}, {"name": "ok-name"}]})
    _run(monkeypatch, tmp_path, "--pkg", "pkg")
    reg = json.loads((tmp_path / "dep_registry.json").read_text())
    assert "evil\nname" not in reg
    assert "ok-name" in reg


def test_gav_normalized(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, result={"deps": [{"name": "com.google.guava:guava"}]})
    _run(monkeypatch, tmp_path, "--pkg", "pkg")
    reg = json.loads((tmp_path / "dep_registry.json").read_text())
    assert "guava" in reg


def test_precheck_bad_json_ignored(tmp_path, monkeypatch, capsys):
    _setup(tmp_path,
           result={"dependency_resolution": {"pending_deps": ["libbar"]}})
    (tmp_path / "reports").mkdir(exist_ok=True)
    (tmp_path / "reports" / "pre_check_pkg.json").write_text("{bad")
    _run(monkeypatch, tmp_path, "--pkg", "pkg")
    reg = json.loads((tmp_path / "dep_registry.json").read_text())
    assert reg["libbar"]["url"] == ""  # pre_check 解析失败不阻塞
