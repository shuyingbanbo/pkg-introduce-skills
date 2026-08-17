"""pkg_introduce_result.py — pkg-introduce 结果文件管理测试。

覆盖:parse_bool/now_iso/result_path/load_result/write_result、
validate_choice、build_updates、merge_result、四个 command 与 main。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

pir = load_module("pkg_introduce_result",
                  SCRIPT_DIRS["pkg_introduce"] / "pkg_introduce_result.py")


def make_args(**overrides):
    """构造与 add_common_arguments 输出一致的 argparse.Namespace。"""
    defaults = dict(
        pkgname="testpkg", reports_dir="./reports", upstream_url=None, lang=None,
        requested_version=None, version=None, decision=None, action=None,
        reason=None, mode=None, depth=None, existing_check=None, repo_check=None,
        license_check=None, analysis_file=None, failure_type=None,
        failure_reason=None, archived=None, status=None, field="",
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ─────────────────────────────────────────────
# parse_bool
# ─────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("true", True),
    ("True", True),
    ("TRUE", True),
    (" true ", True),  # 空白容忍
    ("false", False),
    ("False", False),
    ("FALSE", False),
    (" false ", False),
])
def test_parse_bool(value, expected):
    assert pir.parse_bool(value) is expected


@pytest.mark.parametrize("value", ["", "yes", "1", "0", "t", "f", None])
def test_parse_bool_invalid(value):
    with pytest.raises(argparse.ArgumentTypeError):
        pir.parse_bool(value)


# ─────────────────────────────────────────────
# now_iso / result_path / load_result / write_result
# ─────────────────────────────────────────────

def test_now_iso():
    iso = pir.now_iso()
    dt = datetime.fromisoformat(iso)
    assert dt.tzinfo is not None
    assert iso.endswith("+00:00")


@pytest.mark.parametrize("pkgname,reports_dir", [
    ("foo", "/tmp/reports"),
    ("bar.baz", "rel/dir"),
    ("", ""),
])
def test_result_path(pkgname, reports_dir):
    expected = str(Path(reports_dir) / f"pkg_introduce_result_{pkgname}.json")
    assert str(pir.result_path(pkgname, reports_dir)) == expected


def test_load_result_missing(tmp_path):
    assert pir.load_result(tmp_path / "nope.json") == {}


def test_load_result_existing(tmp_path):
    f = tmp_path / "r.json"
    f.write_text('{"decision": "reuse_official"}', encoding="utf-8")
    assert pir.load_result(f) == {"decision": "reuse_official"}


def test_write_result_creates_parents(tmp_path):
    f = tmp_path / "a" / "b" / "r.json"
    pir.write_result(f, {"x": "y"})
    assert json.loads(f.read_text(encoding="utf-8")) == {"x": "y"}


def test_write_result_overwrites(tmp_path):
    f = tmp_path / "r.json"
    pir.write_result(f, {"x": 1})
    pir.write_result(f, {"x": 2})
    assert json.loads(f.read_text(encoding="utf-8")) == {"x": 2}


# ─────────────────────────────────────────────
# validate_choice
# ─────────────────────────────────────────────

@pytest.mark.parametrize("value,allowed", [
    ("introduce_new", pir.VALID_DECISIONS),
    ("reuse_official", pir.VALID_DECISIONS),
    ("upgrade_user_repo", pir.VALID_DECISIONS),
    ("done", pir.VALID_STATUSES),
    ("building", pir.VALID_STATUSES),
    ("failed", pir.VALID_STATUSES),
    ("top-level", pir.VALID_MODES),
    ("dependency", pir.VALID_MODES),
    ("blocked", pir.VALID_ACTIONS),
    ("retryable_version_conflict", pir.VALID_FAILURE_TYPES),
])
def test_validate_choice_valid(value, allowed):
    assert pir.validate_choice(value, allowed, "field") == value


def test_validate_choice_empty_passthrough():
    # 空值/None 原样返回,不报错(生产代码现状)
    assert pir.validate_choice("", pir.VALID_DECISIONS, "decision") == ""
    assert pir.validate_choice(None, pir.VALID_DECISIONS, "decision") is None


def test_validate_choice_invalid():
    with pytest.raises(ValueError) as ei:
        pir.validate_choice("nonsense", pir.VALID_DECISIONS, "decision")
    msg = str(ei.value)
    assert "decision 非法" in msg
    assert "nonsense" in msg
    assert "reuse_official" in msg  # 允许值列表随错误信息输出


# ─────────────────────────────────────────────
# build_updates
# ─────────────────────────────────────────────

def test_build_updates_full():
    args = make_args(
        upstream_url="https://example.com/x", lang="python",
        requested_version="1.0", version="1.0", decision="introduce_new",
        action="built_new", reason="new pkg",
        failure_type="non_retryable_build_failure", failure_reason="boom",
        existing_check="ec.json", repo_check="rc.json", license_check="lc.json",
        analysis_file="af.json", status="done", mode="top-level", depth=0,
        archived=False)
    updates = pir.build_updates(args)
    assert updates == {
        "upstream_url": "https://example.com/x", "lang": "python",
        "requested_version": "1.0", "version": "1.0",
        "decision": "introduce_new", "action": "built_new", "reason": "new pkg",
        "failure_type": "non_retryable_build_failure", "failure_reason": "boom",
        "existing_check": "ec.json", "repo_check": "rc.json",
        "license_check": "lc.json", "analysis_file": "af.json",
        "status": "done", "depth": 0, "mode": "top-level", "archived": False,
    }


def test_build_updates_none_omitted():
    # None 字段不写入 updates
    args = make_args(upstream_url="u", decision="reuse_official")
    updates = pir.build_updates(args)
    assert updates == {"upstream_url": "u", "decision": "reuse_official"}
    for key in ("lang", "version", "action", "status", "depth", "mode",
                "archived", "failure_type", "failure_reason"):
        assert key not in updates


def test_build_updates_falsy_included():
    # 0/False 非 None,照常写入
    args = make_args(depth=0, archived=False, mode="top-level")
    updates = pir.build_updates(args)
    assert updates["depth"] == 0
    assert updates["archived"] is False
    assert updates["mode"] == "top-level"


def test_build_updates_missing_getattr_fields():
    # failure_type/failure_reason/status/depth/mode/archived 缺失时走 getattr 默认 None
    args = argparse.Namespace(
        pkgname="p", reports_dir="./reports", upstream_url="u", lang="python",
        requested_version=None, version=None, decision=None, action=None,
        reason=None, existing_check=None, repo_check=None, license_check=None,
        analysis_file=None)
    updates = pir.build_updates(args)
    assert updates == {"upstream_url": "u", "lang": "python"}


def test_build_updates_from_real_parser():
    # 与 add_common_arguments 产出的参数对象对接
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("write")
    pir.add_common_arguments(p, require_action_reason=True)
    args = parser.parse_args(["write", "foo", "--decision", "introduce_new",
                              "--action", "built_new", "--reason", "r",
                              "--archived", "false", "--depth", "2",
                              "--mode", "top-level"])
    updates = pir.build_updates(args)
    assert updates["decision"] == "introduce_new"
    assert updates["action"] == "built_new"
    assert updates["archived"] is False  # parse_bool 转换
    assert updates["depth"] == 2
    assert updates["mode"] == "top-level"


def test_add_common_arguments_requires_action_reason():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("write")
    pir.add_common_arguments(p, require_action_reason=True)
    with pytest.raises(SystemExit):
        parser.parse_args(["write", "foo"])  # 缺 --action/--reason
    args = parser.parse_args(["write", "foo", "--action", "built_new", "--reason", "r"])
    assert args.action == "built_new"
    assert args.reason == "r"


def test_add_common_arguments_optional_action():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("update")
    pir.add_common_arguments(p, require_action_reason=False)
    args = parser.parse_args(["update", "foo"])
    assert args.action is None
    assert args.reason is None


# ─────────────────────────────────────────────
# merge_result
# ─────────────────────────────────────────────

def test_merge_result_defaults():
    merged = pir.merge_result({}, "foo", {})
    assert merged["pkgname"] == "foo"
    assert merged["upstream_url"] == ""
    assert merged["lang"] == ""
    assert merged["requested_version"] == ""
    assert merged["version"] == ""
    assert merged["decision"] == ""
    assert merged["action"] == ""
    assert merged["reason"] == ""
    assert merged["failure_type"] == ""
    assert merged["failure_reason"] == ""
    assert merged["status"] == ""
    assert merged["mode"] == "top-level"
    assert merged["depth"] == 0
    assert merged["archived"] is False
    assert merged["analysis_file"] == ""
    datetime.fromisoformat(merged["created_at"])  # 可解析
    datetime.fromisoformat(merged["updated_at"])
    assert merged["created_at"] <= merged["updated_at"]


def test_merge_result_preserves_existing():
    existing = {
        "upstream_url": "u", "lang": "python", "version": "1.0",
        "created_at": "2024-01-01T00:00:00+00:00",
        "archived": True, "depth": 3, "mode": "dependency", "status": "done",
    }
    merged = pir.merge_result(existing, "foo", {})
    assert merged["upstream_url"] == "u"
    assert merged["lang"] == "python"
    assert merged["version"] == "1.0"
    assert merged["created_at"] == "2024-01-01T00:00:00+00:00"  # created_at 保留
    assert merged["archived"] is True
    assert merged["depth"] == 3
    assert merged["mode"] == "dependency"
    assert merged["status"] == "done"
    assert merged["updated_at"]  # updated_at 总是刷新


def test_merge_result_updates_override():
    existing = {"version": "1.0", "decision": "reuse_official",
                "created_at": "2024-01-01T00:00:00+00:00"}
    merged = pir.merge_result(existing, "foo",
                              {"version": "2.0", "status": "done"})
    assert merged["version"] == "2.0"
    assert merged["decision"] == "reuse_official"  # 未更新字段保留
    assert merged["status"] == "done"


def test_merge_result_updates_none_overrides():
    # 直接传 None 值会覆盖已有字段(merge_result 不过滤 None,生产代码现状)
    merged = pir.merge_result({"version": "1.0"}, "foo", {"version": None})
    assert merged["version"] is None


# ─────────────────────────────────────────────
# command_write / command_update
# ─────────────────────────────────────────────

def test_command_write_creates_file(tmp_path, capsys):
    args = make_args(reports_dir=str(tmp_path), decision="introduce_new",
                     action="built_new", reason="new pkg", version="1.0",
                     archived=True, mode="top-level", status="done")
    assert pir.command_write(args) == 0
    assert str(tmp_path / "pkg_introduce_result_testpkg.json") in capsys.readouterr().out
    data = json.loads((tmp_path / "pkg_introduce_result_testpkg.json").read_text(encoding="utf-8"))
    assert data["pkgname"] == "testpkg"
    assert data["decision"] == "introduce_new"
    assert data["action"] == "built_new"
    assert data["version"] == "1.0"
    assert data["archived"] is True
    assert data["status"] == "done"
    assert data["mode"] == "top-level"


@pytest.mark.parametrize("field,value", [
    ("decision", "nonsense"),
    ("action", "nonsense"),
    ("status", "weird"),
    ("mode", "weird"),
])
def test_command_write_invalid_choice(tmp_path, field, value):
    args = make_args(reports_dir=str(tmp_path), **{field: value})
    with pytest.raises(ValueError):
        pir.command_write(args)
    assert not (tmp_path / "pkg_introduce_result_testpkg.json").exists()


def test_command_update_existing(tmp_path, capsys):
    existing = {"pkgname": "testpkg", "version": "1.0",
                "created_at": "2024-01-01T00:00:00+00:00"}
    pir.write_result(pir.result_path("testpkg", str(tmp_path)), existing)
    args = make_args(reports_dir=str(tmp_path), version="2.0", status="done")
    assert pir.command_update(args) == 0
    data = json.loads((tmp_path / "pkg_introduce_result_testpkg.json").read_text(encoding="utf-8"))
    assert data["version"] == "2.0"
    assert data["created_at"] == "2024-01-01T00:00:00+00:00"  # created_at 保留
    assert data["status"] == "done"


def test_command_update_missing_file_creates(tmp_path):
    args = make_args(reports_dir=str(tmp_path), version="1.0")
    assert pir.command_update(args) == 0
    assert (tmp_path / "pkg_introduce_result_testpkg.json").exists()


def test_command_update_invalid_action(tmp_path):
    args = make_args(reports_dir=str(tmp_path), action="bad")
    with pytest.raises(ValueError):
        pir.command_update(args)


@pytest.mark.parametrize("field,value", [
    ("decision", "reuse_official"),
    ("action", "reused_official"),
    ("status", "done"),
    ("mode", "top-level"),
])
def test_command_update_validates_choices(tmp_path, field, value):
    args = make_args(reports_dir=str(tmp_path), **{field: value})
    assert pir.command_update(args) == 0
    data = json.loads((tmp_path / "pkg_introduce_result_testpkg.json").read_text(encoding="utf-8"))
    assert data[field] == value


@pytest.mark.parametrize("field,value", [
    ("decision", "bad"),
    ("status", "bad"),
    ("mode", "bad"),
])
def test_command_update_invalid_choice(tmp_path, field, value):
    args = make_args(reports_dir=str(tmp_path), **{field: value})
    with pytest.raises(ValueError):
        pir.command_update(args)


# ─────────────────────────────────────────────
# command_show / command_path
# ─────────────────────────────────────────────

def test_command_show_missing(tmp_path, capsys):
    args = make_args(reports_dir=str(tmp_path))
    assert pir.command_show(args) == 1
    assert "不存在" in capsys.readouterr().err


def test_command_show_full(tmp_path, capsys):
    pir.write_result(pir.result_path("testpkg", str(tmp_path)),
                     {"pkgname": "testpkg", "decision": "reuse_official"})
    args = make_args(reports_dir=str(tmp_path))
    assert pir.command_show(args) == 0
    assert "reuse_official" in capsys.readouterr().out


@pytest.mark.parametrize("field,expected", [
    ("decision", "reuse_official"),   # str 字段
    ("version", "1.2.3"),
    ("meta.k", "v"),                  # 点分嵌套字段
    ("flag", "true"),                 # bool 输出 true/false
    ("nothing", "null"),              # None 输出 null
    ("meta", '{\n  "k": "v"\n}'),     # dict/list 输出 JSON
    ("tags", '[\n  "a",\n  "b"\n]'),
])
def test_command_show_field(tmp_path, capsys, field, expected):
    pir.write_result(pir.result_path("testpkg", str(tmp_path)), {
        "pkgname": "testpkg", "decision": "reuse_official", "version": "1.2.3",
        "meta": {"k": "v"}, "flag": True, "nothing": None, "tags": ["a", "b"],
    })
    args = make_args(reports_dir=str(tmp_path), field=field)
    assert pir.command_show(args) == 0
    assert capsys.readouterr().out.strip() == expected


def test_command_show_field_missing(tmp_path, capsys):
    pir.write_result(pir.result_path("testpkg", str(tmp_path)),
                     {"pkgname": "testpkg"})
    args = make_args(reports_dir=str(tmp_path), field="nope")
    assert pir.command_show(args) == 1
    assert "字段不存在" in capsys.readouterr().err


def test_command_path(tmp_path, capsys):
    args = make_args(reports_dir=str(tmp_path))
    assert pir.command_path(args) == 0
    expected = str(tmp_path / "pkg_introduce_result_testpkg.json")
    assert capsys.readouterr().out.strip() == expected


# ─────────────────────────────────────────────
# main
# ─────────────────────────────────────────────

def test_main_write_flow(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "pkg_introduce_result.py", "write", "foo", "--reports-dir", str(tmp_path),
        "--decision", "introduce_new", "--action", "built_new", "--reason", "r",
        "--archived", "true"])
    assert pir.main() == 0
    data = json.loads((tmp_path / "pkg_introduce_result_foo.json").read_text(encoding="utf-8"))
    assert data["decision"] == "introduce_new"
    assert data["archived"] is True


def test_main_invalid_decision_returns_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "pkg_introduce_result.py", "write", "foo", "--reports-dir", str(tmp_path),
        "--decision", "bad", "--action", "built_new", "--reason", "r"])
    assert pir.main() == 1
    assert "错误:" in capsys.readouterr().err


def test_main_update_flow(tmp_path, monkeypatch):
    pir.write_result(pir.result_path("foo", str(tmp_path)),
                     {"pkgname": "foo", "version": "1.0"})
    monkeypatch.setattr(sys, "argv", [
        "pkg_introduce_result.py", "update", "foo", "--reports-dir", str(tmp_path),
        "--version", "2.0"])
    assert pir.main() == 0
    data = json.loads((tmp_path / "pkg_introduce_result_foo.json").read_text(encoding="utf-8"))
    assert data["version"] == "2.0"


def test_main_show_missing_returns_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "pkg_introduce_result.py", "show", "foo", "--reports-dir", str(tmp_path)])
    assert pir.main() == 1
    assert "不存在" in capsys.readouterr().err


def test_main_invalid_archived_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "pkg_introduce_result.py", "write", "foo", "--reports-dir", str(tmp_path),
        "--archived", "yes"])
    with pytest.raises(SystemExit) as ei:  # argparse 类型错误 → SystemExit(2)
        pir.main()
    assert ei.value.code == 2


def test_main_unknown_command_raises(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["pkg_introduce_result.py", "bogus", "foo"])
    with pytest.raises(SystemExit):
        pir.main()
