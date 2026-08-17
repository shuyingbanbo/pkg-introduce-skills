"""notify_job.py — job 完成通知:chroot 状态聚合 + redis 回写 + CLI。

notify_job.py 顶层 from dep_chroots import ...(step 目录),因此在文件顶部把
step 目录插入 sys.path;顶层 import redis,故模块经 redis_stub 后按需加载。
"""

from __future__ import annotations

import json
import runpy
import sys

import pytest

from tests.conftest import SCRIPT_DIRS

# notify_job.py 顶层 from dep_chroots import chroot_status_map → step 目录
sys.path.insert(0, str(SCRIPT_DIRS["step"]))


@pytest.fixture
def notify(redis_stub, loaded_modules):
    return loaded_modules("notify_job", SCRIPT_DIRS["step"] / "notify_job.py")


def _write(sd, name, obj):
    (sd / name).write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _session_dir(tmp_path, wf=None, reg=None, sess=None, name="job123"):
    sd = tmp_path / name
    sd.mkdir()
    if wf is not None:
        _write(sd, f"workflow_{name}.json", wf)
    if reg is not None:
        _write(sd, "dep_registry.json", reg)
    if sess is not None:
        _write(sd, "session.json", sess)
    return sd


# ─────────────────────────────────────────────
# _collect_chroot_status:跨包状态聚合
# ─────────────────────────────────────────────

@pytest.mark.parametrize("s1,s2,expected_status", [
    ("build_done", "build_done", "succeeded"),
    ("build_done", "failed", "failed"),     # 任一 failed → failed
    ("failed", "build_done", "failed"),
    ("build_done", "pending", "skipped"),   # 非终态 → skipped
    ("building", "skipped", "skipped"),
    ("reused", "reused", "succeeded"),
    ("skipped", "skipped", "skipped"),      # 无 succeeded 也非 failed → skipped
])
def test_collect_chroot_status_aggregation(notify, tmp_path, s1, s2, expected_status):
    _write(tmp_path, "session.json", {"pkgname": "mainpkg"})
    _write(tmp_path, "dep_registry.json", {
        "mainpkg": {"chroots": {"c1": {"status": s1, "build_id": 100}}},
        "dep1": {"chroots": {"c1": {"status": s2, "build_id": 200}}},
    })
    out = notify._collect_chroot_status(tmp_path)
    # build_id 主包(mainpkg)优先
    assert out == {"c1": {"status": expected_status, "build_id": 100}}


def test_collect_build_id_fallback_to_any_pkg(notify, tmp_path):
    _write(tmp_path, "session.json", {"pkgname": "mainpkg"})
    _write(tmp_path, "dep_registry.json", {
        "mainpkg": {"chroots": {"c1": {"status": "build_done"}}},   # 主包无 build_id
        "dep1": {"chroots": {"c1": {"status": "build_done", "build_id": 200}}},
    })
    assert notify._collect_chroot_status(tmp_path)["c1"]["build_id"] == 200


def test_collect_no_build_id_anywhere(notify, tmp_path):
    _write(tmp_path, "session.json", {"pkgname": "mainpkg"})
    _write(tmp_path, "dep_registry.json", {
        "mainpkg": {"chroots": {"c1": {"status": "build_done"}}},
    })
    assert notify._collect_chroot_status(tmp_path) == {
        "c1": {"status": "succeeded", "build_id": None}}


def test_collect_multiple_chroots(notify, tmp_path):
    _write(tmp_path, "session.json", {"pkgname": "mainpkg"})
    _write(tmp_path, "dep_registry.json", {
        "mainpkg": {"chroots": {"c1": {"status": "build_done", "build_id": 1}}},
        "dep1": {"chroots": {"c2": {"status": "failed", "build_id": 2}}},
    })
    assert notify._collect_chroot_status(tmp_path) == {
        "c1": {"status": "succeeded", "build_id": 1},
        "c2": {"status": "failed", "build_id": 2},
    }


def test_collect_old_format_entries_ignored(notify, tmp_path):
    _write(tmp_path, "session.json", {"pkgname": "mainpkg"})
    _write(tmp_path, "dep_registry.json", {
        "mainpkg": {"status": "build_done"},  # 旧格式:无 chroots 键
        "dep1": "not-a-dict",                 # 非 dict 条目
    })
    assert notify._collect_chroot_status(tmp_path) == {}


def test_collect_missing_registry_returns_empty(notify, tmp_path):
    assert notify._collect_chroot_status(tmp_path) == {}


def test_collect_invalid_json_returns_empty(notify, tmp_path):
    (tmp_path / "dep_registry.json").write_text("{bad", encoding="utf-8")
    assert notify._collect_chroot_status(tmp_path) == {}


def test_collect_non_dict_registry_raises(notify, tmp_path):
    # 实际行为:dep_registry.json 顶层是数组/标量时 reg.items() 抛 AttributeError,
    # 仅 OSError/JSONDecodeError 被兜底——潜在 bug,测试按现状断言。
    (tmp_path / "dep_registry.json").write_text("[1,2]", encoding="utf-8")
    with pytest.raises(AttributeError):
        notify._collect_chroot_status(tmp_path)


def test_collect_missing_session_takes_first_build_id(notify, tmp_path):
    # session.json 缺失 → pkgname="" → 主包判定失效,取第一个出现的 build_id
    _write(tmp_path, "dep_registry.json", {
        "mainpkg": {"chroots": {"c1": {"status": "build_done", "build_id": 100}}},
        "dep1": {"chroots": {"c1": {"status": "build_done", "build_id": 200}}},
    })
    assert notify._collect_chroot_status(tmp_path)["c1"]["build_id"] == 100


def test_collect_invalid_session_json(notify, tmp_path):
    # session.json 损坏 → pkgname=""(与缺失同行为,由 OSError/JSONDecodeError 兜底)
    (tmp_path / "session.json").write_text("{bad", encoding="utf-8")
    _write(tmp_path, "dep_registry.json", {
        "p": {"chroots": {"c1": {"status": "build_done", "build_id": 5}}},
    })
    assert notify._collect_chroot_status(tmp_path) == {
        "c1": {"status": "succeeded", "build_id": 5}}


# ─────────────────────────────────────────────
# notify():redis 回写 + 打印
# ─────────────────────────────────────────────

def test_notify_success_writes_hash_and_log(notify, redis_stub, tmp_path, capsys):
    sd = _session_dir(tmp_path, wf={
        "built_pkgs": ["pkg-a", "pkg-b"],
        "reused_pkgs": ["pkg-c"],
        "loop_count": 3,
    })
    notify.notify(str(sd), "success")
    h = redis_stub.hgetall("job:ai:job123")
    assert h == {
        "status": "success",
        "built_pkgs": "pkg-a pkg-b",
        "reused_pkgs": "pkg-c",
        "loop_count": "3",
        "error": "",
    }
    assert "chroot_status" not in h  # 无 per-chroot 数据不写该字段
    log = json.loads(redis_stub.lpop("logs:ai:job123"))
    assert log == {"done": True, "status": "success"}
    out = capsys.readouterr().out
    assert "status=success" in out
    assert "built=pkg-a pkg-b" in out
    assert "reason=" not in out


def test_notify_failed_uses_error_field(notify, redis_stub, tmp_path, capsys):
    sd = _session_dir(tmp_path, wf={"error": "spec 解析失败"})
    notify.notify(str(sd), "failed")
    h = redis_stub.hgetall("job:ai:job123")
    assert h["status"] == "failed"
    assert h["error"] == "spec 解析失败"
    assert "reason=spec 解析失败" in capsys.readouterr().out
    assert json.loads(redis_stub.lpop("logs:ai:job123")) == {
        "done": True, "status": "failed"}


def test_notify_failed_falls_back_to_failure_reason(notify, redis_stub, tmp_path):
    sd = _session_dir(tmp_path, wf={"failure_reason": "COPR 构建失败"})
    notify.notify(str(sd), "failed")
    assert redis_stub.hget("job:ai:job123", "error") == "COPR 构建失败"


def test_notify_success_ignores_wf_error(notify, redis_stub, tmp_path):
    sd = _session_dir(tmp_path, wf={"error": "旧错误"})
    notify.notify(str(sd), "success")
    assert redis_stub.hget("job:ai:job123", "error") == ""


def test_notify_no_workflow_files(notify, redis_stub, tmp_path, capsys):
    sd = _session_dir(tmp_path, wf=None)
    notify.notify(str(sd), "success")
    h = redis_stub.hgetall("job:ai:job123")
    assert h == {"status": "success", "built_pkgs": "", "reused_pkgs": "",
                 "loop_count": "", "error": ""}
    assert capsys.readouterr().out == "[引包] 完成  status=success\n"


@pytest.mark.parametrize("reg,expected_field", [
    ({"mainpkg": {"chroots": {"c1": {"status": "build_done", "build_id": 1}}}},
     {"c1": {"status": "succeeded", "build_id": 1}}),
    ({"dep1": {"chroots": {"c1": {"status": "failed", "build_id": 9}}}},
     {"c1": {"status": "failed", "build_id": 9}}),
    ({"mainpkg": {"status": "build_done"}}, None),  # 旧格式条目 → 不写 chroot_status
])
def test_notify_chroot_status_field(notify, redis_stub, tmp_path, reg, expected_field):
    sd = _session_dir(tmp_path, wf={}, reg=reg, sess={"pkgname": "mainpkg"})
    notify.notify(str(sd), "success")
    h = redis_stub.hgetall("job:ai:job123")
    if expected_field is None:
        assert "chroot_status" not in h
    else:
        assert json.loads(h["chroot_status"]) == expected_field


@pytest.mark.parametrize("host_env,expected_host", [
    (None, "redis"),                  # 默认
    ("myredis.example", "myredis.example"),
])
def test_notify_redis_connection(notify, redis_stub, monkeypatch, tmp_path,
                                 host_env, expected_host):
    redis_mod = sys.modules["redis"]
    calls = []
    redis_mod.Redis = lambda *a, **kw: (calls.append((a, kw)), redis_stub)[1]
    if host_env is None:
        monkeypatch.delenv("REDIS_HOST", raising=False)
    else:
        monkeypatch.setenv("REDIS_HOST", host_env)
    sd = _session_dir(tmp_path, wf={})
    notify.notify(str(sd), "success")
    assert calls == [((), {"host": expected_host, "port": 6379,
                           "decode_responses": True})]


# ─────────────────────────────────────────────
# CLI(__main__ 块,经 runpy 执行)
# ─────────────────────────────────────────────

def _run_cli(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["notify_job.py"] + argv)
    return runpy.run_path(str(SCRIPT_DIRS["step"] / "notify_job.py"),
                          run_name="__main__")


def test_main_cli_success(notify, redis_stub, tmp_path, monkeypatch, capsys):
    sd = _session_dir(tmp_path, wf={"built_pkgs": ["a"]})
    _run_cli(monkeypatch, ["--session-dir", str(sd), "--status", "success"])
    assert redis_stub.hget("job:ai:job123", "status") == "success"
    assert "status=success" in capsys.readouterr().out


def test_main_cli_notify_error_exits_1(notify, redis_stub, tmp_path, monkeypatch,
                                       capsys):
    sd = _session_dir(tmp_path, name="job9")
    (sd / "workflow_job9.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        _run_cli(monkeypatch, ["--session-dir", str(sd), "--status", "failed"])
    assert e.value.code == 1
    assert "[notify_job] warning" in capsys.readouterr().err


def test_main_cli_invalid_status_exits_2(notify, redis_stub, tmp_path, monkeypatch):
    sd = _session_dir(tmp_path)
    with pytest.raises(SystemExit) as e:
        _run_cli(monkeypatch, ["--session-dir", str(sd), "--status", "bogus"])
    assert e.value.code == 2
