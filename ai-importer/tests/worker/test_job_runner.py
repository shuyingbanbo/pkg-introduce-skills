"""job_runner.py — 纯逻辑 helpers + 编排路径测试。

不启动真实 claude(monkeypatch subprocess.Popen)、不访问 COPR 网络
(注入假 copr_client 模块 / 假 urllib urlopen)。加载方式:每个测试通过
_load() 注入 env 后 importlib 重载 job_runner——顶层常量(SKILLS_DIR /
MAX_LOOPS / MAX_SCRIPT_FAILS / SESSIONS_BASE ...)在 import 时固化,
不同 env 组合必须重新加载(loaded_modules 会在测试后清理 sys.modules)。
"""

from __future__ import annotations

import gzip
import io
import json
import subprocess
import sys
import time
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.conftest import SCRIPT_DIRS


def _load(worker_scripts, skills_env, loaded_modules, monkeypatch, **env):
    """注入 env(建议带 SESSIONS_BASE 指向 tmp)后重载 job_runner 模块。"""
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return loaded_modules("job_runner", SCRIPT_DIRS["worker"] / "job_runner.py")


@pytest.fixture
def jr(worker_scripts, skills_env, loaded_modules, monkeypatch, tmp_path):
    """默认 env 的 job_runner 模块实例。"""
    return _load(worker_scripts, skills_env, loaded_modules, monkeypatch,
                 SESSIONS_BASE=str(tmp_path / "sessions"))


# ─────────────────────────────────────────────
# 测试用 fakes / 辅助
# ─────────────────────────────────────────────

class _JobRedis:
    """包装 conftest 的 _FakeRedis:job_runner 以位置参数调用
    hset(key, field, value),而 fake 的签名是 hset(key, mapping=None, **kwargs),
    需要在此转换。"""

    def __init__(self, fake):
        self._f = fake

    def hgetall(self, key):
        return self._f.hgetall(key)

    def hset(self, key, field=None, value=None, mapping=None):
        if mapping is not None:
            return self._f.hset(key, mapping=mapping)
        return self._f.hset(key, mapping={field: value})

    def hget(self, key, field):
        return self._f.hget(key, field)

    def rpush(self, key, *values):
        return self._f.rpush(key, *values)


def _logs(r, job_id="job1"):
    return [json.loads(m).get("msg", "") for m in r._f._lists.get(f"logs:ai:{job_id}", [])]


def _valid_job(**overrides):
    job = {
        "pkgname": "setuptools",
        "url": "https://pypi.org/project/setuptools/",
        "version": "68.0.0",
        "mode": "normal",
        "ros_distro": "",
        "deep_dependency": "0",
        "copr_login": "user",
        "copr_token": "token",
        "copr_chroots": json.dumps(["openeuler-24.03-x86_64"]),
    }
    job.update(overrides)
    return job


def _make_r(redis_stub, job_id="job1", **overrides):
    r = _JobRedis(redis_stub)
    r.hset(f"job:ai:{job_id}", mapping=_valid_job(**overrides))
    return r


def _job_status(r, job_id="job1"):
    return r.hget(f"job:ai:{job_id}", "status")


def _session_dir(tmp_path, job_id="job1"):
    return tmp_path / "sessions" / job_id


def _events(session_dir, type_=None):
    path = session_dir / "timeline.jsonl"
    if not path.exists():
        return []
    evts = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [e for e in evts if type_ is None or e.get("type") == type_]


def _sup_always(fake, stdout):
    fake.when(lambda s: "step_supervisor.py" in s, stdout=stdout)


def _sup_rule(fake, stdout, n=1):
    """前 n 次 supervisor 调用返回 stdout,之后落入默认空输出。"""
    state = [0]

    def pred(cmd):
        if "step_supervisor.py" in cmd:
            state[0] += 1
            return state[0] <= n
        return False

    fake.when(pred, stdout=stdout)


class _FakeProc:
    """假 claude 子进程:stdout/stderr 为内存流,wait() 稍作等待让 watchdog 线程调度。"""

    def __init__(self, stdout_text="", stderr_text="", returncode=0,
                 wait_terminate=False):
        self.stdout = io.StringIO(stdout_text)
        self.stderr = io.StringIO(stderr_text)
        self.returncode = returncode
        self.terminated = False
        self._wait_terminate = wait_terminate

    def poll(self):
        if self._wait_terminate:
            return None  # watchdog 进入循环
        return self.returncode

    def wait(self, timeout=None):
        if self._wait_terminate:
            deadline = time.time() + 1.0
            while not self.terminated and time.time() < deadline:
                time.sleep(0.001)
        else:
            time.sleep(0.01)  # 给 watchdog 线程调度机会
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True


def _install_popen(monkeypatch, proc):
    captured = {}

    def popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return proc

    monkeypatch.setattr(subprocess, "Popen", popen)
    return captured


class _Resp:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install_urlopen(monkeypatch, html, log_bytes=b"build log line\n",
                     fail_logs=False):
    calls = []

    def urlopen(url, timeout=None):
        calls.append(url)
        if url.endswith(".gz") or url.endswith(".log"):
            if fail_logs:
                raise urllib.error.URLError("no log")
            if url.endswith(".gz"):
                return _Resp(gzip.compress(log_bytes))
            return _Resp(log_bytes)
        return _Resp(html.encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return calls


def _inject_copr(monkeypatch, get_build):
    mod = types.ModuleType("copr_client")
    mod.get_build = get_build
    monkeypatch.setitem(sys.modules, "copr_client", mod)
    return mod


def _build_data(state="succeeded", name="setuptools", chroot="a-x86_64"):
    return {"state": state, "source_package": {"name": name},
            "chroots": {chroot: state}}


def _dir_html(bid):
    return f'<a href="{bid:08d}-build-dir/">x</a>'


# ─────────────────────────────────────────────
# _strip_unicode_controls
# ─────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("setuptools", "setuptools"),
    ("foo\u200bbar", "foobar"),            # 零宽空格(Cf)
    ("foo\u202ebar", "foobar"),            # 双向覆写(Cf)
    ("a\nb\tc\rd", "a\nb\tc\rd"),          # 保留换行/制表/回车
    ("", ""),
    ("中文包", "中文包"),                    # 非控制字符原样保留
    ("x\x00y\x1bz", "xyz"),                # 其他 Cc 控制字符移除
])
def test_strip_unicode_controls(jr, raw, expected):
    assert jr._strip_unicode_controls(raw) == expected


def test_strip_unicode_controls_line_separator_kept(jr):
    # U+2028 属 Zl(行分隔符)而非 Cf/Cc,当前实现不清理(白名单校验兜底)——按实际行为断言
    assert jr._strip_unicode_controls("a\u2028b") == "a\u2028b"


# ─────────────────────────────────────────────
# _safe_int
# ─────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    (123, 123),
    ("123", 123),
    (" 42 ", 42),
    ("-7", -7),
    (None, None),
    ("", None),
    ("abc", None),
    ("1.5", None),
    (True, None),          # bool 显式按缺失处理
    (False, None),
    (12.5, 12),            # 实际行为:int() 直接截断 float(生产调用侧传 int/str)
    ([1], None),
])
def test_safe_int(jr, value, expected):
    assert jr._safe_int(value) == expected


# ─────────────────────────────────────────────
# _parse_job_chroots
# ─────────────────────────────────────────────

@pytest.mark.parametrize("job,expected", [
    ({"copr_chroots": json.dumps(["a-x86_64"])}, ["a-x86_64"]),
    ({"copr_chroots": json.dumps(["a-x86_64", "b-aarch64"])},
     ["a-x86_64", "b-aarch64"]),
    ({"copr_chroots": json.dumps(["a", "", "b", 3, None])}, ["a", "b"]),
    ({"copr_chroots": json.dumps([])}, []),
    ({"copr_chroots": ""}, []),
    ({"copr_chroots": "not-json"}, []),
    ({"copr_chroot": "single"}, ["single"]),
    ({"copr_chroots": "not-json", "copr_chroot": "single"}, ["single"]),
    ({}, []),
])
def test_parse_job_chroots(jr, job, expected):
    assert jr._parse_job_chroots(job) == expected


# ─────────────────────────────────────────────
# _primary_chroot
# ─────────────────────────────────────────────

@pytest.mark.parametrize("chroots,expected", [
    (["z-aarch64", "m-x86_64", "a-aarch64"], "m-x86_64"),
    (["openeuler-24.03-x86_64"], "openeuler-24.03-x86_64"),
    (["b-aarch64", "a-aarch64"], "a-aarch64"),
    (["single"], "single"),
])
def test_primary_chroot(jr, chroots, expected):
    assert jr._primary_chroot(chroots) == expected


def test_primary_chroot_empty_raises_indexerror(jr):
    # 实际行为:空列表 IndexError(生产调用侧保证非空)——按实际行为断言
    with pytest.raises(IndexError):
        jr._primary_chroot([])


# ─────────────────────────────────────────────
# 脚本 fail 熔断计数四件套
# ─────────────────────────────────────────────

def test_bump_script_fail_count_increments(jr, tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    assert jr._bump_script_fail_count(d, "evaluate:dep1") == 1
    assert jr._bump_script_fail_count(d, "evaluate:dep1") == 2
    assert jr._bump_script_fail_count(d, "evaluate:dep2") == 1
    assert jr._get_script_fail_counts(d) == {"evaluate:dep1": 2, "evaluate:dep2": 1}


def test_bump_corrupted_file_resets(jr, tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    (d / "script_fail_counts.json").write_text("not-json")
    assert jr._bump_script_fail_count(d, "k") == 1


def test_clear_script_fail_count_removes_key(jr, tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    jr._bump_script_fail_count(d, "a")
    jr._bump_script_fail_count(d, "b")
    jr._clear_script_fail_count(d, "a")
    assert jr._get_script_fail_counts(d) == {"b": 1}


def test_clear_missing_file_noop(jr, tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    jr._clear_script_fail_count(d, "a")   # 不应创建文件
    assert not (d / "script_fail_counts.json").exists()


def test_clear_corrupted_file_noop(jr, tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    (d / "script_fail_counts.json").write_text("{bad")
    jr._clear_script_fail_count(d, "a")
    assert (d / "script_fail_counts.json").read_text() == "{bad"


def test_get_script_fail_counts_missing(jr, tmp_path):
    assert jr._get_script_fail_counts(tmp_path / "nonexist") == {}


def test_get_script_fail_counts_corrupt(jr, tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    (d / "script_fail_counts.json").write_text("[1,2")
    assert jr._get_script_fail_counts(d) == {}


def test_cap_script_fail_count_sets_threshold(worker_scripts, skills_env,
                                              loaded_modules, monkeypatch, tmp_path):
    jr = _load(worker_scripts, skills_env, loaded_modules, monkeypatch,
               MAX_SCRIPT_FAILS="5", SESSIONS_BASE=str(tmp_path / "sessions"))
    d = tmp_path / "s"
    d.mkdir()
    jr._cap_script_fail_count(d, "evaluate:dep1")
    assert jr._get_script_fail_counts(d) == {"evaluate:dep1": 5}


# ─────────────────────────────────────────────
# _log
# ─────────────────────────────────────────────

def test_log_appends_json(jr, redis_stub):
    jr._log(redis_stub, "j1", "hello 世界")
    entries = redis_stub._lists["logs:ai:j1"]
    assert len(entries) == 1
    data = json.loads(entries[0])
    assert data["msg"] == "hello 世界"
    assert "t" in data


# ─────────────────────────────────────────────
# _init_workflow
# ─────────────────────────────────────────────

def test_init_workflow_creates_defaults(jr, tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    jr._init_workflow(d, "setuptools")
    wf = json.loads((d / "workflow_setuptools.json").read_text())
    assert wf["pkgname"] == "setuptools"
    assert wf["goal"] == "build_success"
    assert wf["loop_count"] == 0
    assert wf["max_loops"] == jr.MAX_LOOPS == 200
    assert wf["built_pkgs"] == []
    assert wf["reused_pkgs"] == []
    assert wf["error"] is None


def test_init_workflow_respects_env_max_loops(worker_scripts, skills_env,
                                              loaded_modules, monkeypatch, tmp_path):
    jr = _load(worker_scripts, skills_env, loaded_modules, monkeypatch,
               MAX_LOOPS="7", SESSIONS_BASE=str(tmp_path / "sessions"))
    d = tmp_path / "s"
    d.mkdir()
    jr._init_workflow(d, "p")
    assert json.loads((d / "workflow_p.json").read_text())["max_loops"] == 7


def test_init_workflow_preserves_existing(jr, tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    (d / "workflow_p.json").write_text('{"custom": 1}')
    jr._init_workflow(d, "p")
    assert json.loads((d / "workflow_p.json").read_text()) == {"custom": 1}


# ─────────────────────────────────────────────
# _finish
# ─────────────────────────────────────────────

def test_finish_sets_status_and_done_log(jr, redis_stub):
    r = _JobRedis(redis_stub)
    jr._finish(r, "j1", "success")
    assert r.hget("job:ai:j1", "status") == "success"
    assert "error" not in r.hgetall("job:ai:j1")
    done = json.loads(redis_stub._lists["logs:ai:j1"][-1])
    assert done["done"] is True
    assert done["status"] == "success"


def test_finish_with_error(jr, redis_stub):
    r = _JobRedis(redis_stub)
    jr._finish(r, "j1", "failed", "boom")
    assert r.hget("job:ai:j1", "error") == "boom"


def test_finish_writes_chroot_status_json(jr, redis_stub):
    r = _JobRedis(redis_stub)
    ch = {"c1": {"status": "succeeded", "build_id": 9}}
    jr._finish(r, "j1", "success", chroot_status=ch)
    assert json.loads(r.hget("job:ai:j1", "chroot_status")) == ch


def test_finish_no_chroot_status_key_when_empty(jr, redis_stub):
    r = _JobRedis(redis_stub)
    jr._finish(r, "j1", "success", chroot_status={})
    assert "chroot_status" not in r.hgetall("job:ai:j1")


# ─────────────────────────────────────────────
# _finish_with_timeline
# ─────────────────────────────────────────────

def _session_with_workflow(tmp_path, built=("setuptools",), reused=("dep1",),
                           loops=3):
    d = tmp_path / "s"
    d.mkdir()
    (d / "session.json").write_text(json.dumps({"copr_chroots": ["c-x86_64"]}))
    (d / "workflow_main.json").write_text(json.dumps({
        "pkgname": "setuptools", "built_pkgs": list(built),
        "reused_pkgs": list(reused), "loop_count": loops,
    }))
    return d


def test_finish_with_timeline_writes_event_and_redis(jr, redis_stub, tmp_path):
    d = _session_with_workflow(tmp_path)
    r = _JobRedis(redis_stub)
    jr._finish_with_timeline(r, "j1", d, "success", "", start_time=10.0)
    assert r.hget("job:ai:j1", "status") == "success"
    evts = _events(d, "session.completed")
    assert len(evts) == 1
    data = evts[0]["data"]
    assert data["status"] == "success"
    assert data["built_pkgs"] == ["setuptools"]
    assert data["reused_pkgs"] == ["dep1"]
    assert data["loop_count"] == 3
    assert data["duration_s"] >= 0
    ch = json.loads(r.hget("job:ai:j1", "chroot_status"))
    assert ch["c-x86_64"]["status"] == "succeeded"


def test_finish_with_timeline_error_and_no_start(jr, redis_stub, tmp_path):
    d = _session_with_workflow(tmp_path)
    r = _JobRedis(redis_stub)
    jr._finish_with_timeline(r, "j1", d, "failed", "reason x", start_time=None)
    data = _events(d, "session.completed")[0]["data"]
    assert data["status"] == "failed"
    assert data["error"] == "reason x"
    assert data["duration_s"] == 0
    assert r.hget("job:ai:j1", "error") == "reason x"


def test_finish_with_timeline_no_workflow_files(jr, redis_stub, tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    (d / "session.json").write_text(json.dumps({"copr_chroots": ["c-x86_64"]}))
    r = _JobRedis(redis_stub)
    jr._finish_with_timeline(r, "j1", d, "success", start_time=None)
    data = _events(d, "session.completed")[0]["data"]
    assert "built_pkgs" not in data
    assert data["status"] == "success"


# ─────────────────────────────────────────────
# _run_supervisor
# ─────────────────────────────────────────────

def test_run_supervisor_parses_key_value(jr, fake_subprocess):
    _sup_always(fake_subprocess,
                "ACTION=evaluate_main\nTARGET=foo\nDELAY=2\n")
    out = jr._run_supervisor(Path("/tmp/x"), "j1")
    assert out == {"action": "evaluate_main", "target": "foo", "delay": "2"}


def test_run_supervisor_strips_quotes_and_skips_lowercase(jr, fake_subprocess, capsys):
    _sup_always(fake_subprocess, "ACTION='done'\nprogress line here\n")
    out = jr._run_supervisor(Path("/tmp/x"), "j1")
    assert out == {"action": "done"}
    assert "progress line here" in capsys.readouterr().out


def test_run_supervisor_stderr_on_rc_failure(jr, fake_subprocess, capsys):
    fake_subprocess.when(lambda s: "step_supervisor.py" in s,
                         stdout="", stderr="boom", returncode=1)
    assert jr._run_supervisor(Path("/tmp/x"), "j1") == {}
    assert "stderr: boom" in capsys.readouterr().out


def test_run_supervisor_empty_stdout(jr, fake_subprocess):
    assert jr._run_supervisor(Path("/tmp/x"), "j1") == {}


# ─────────────────────────────────────────────
# _extract_build_failure
# ─────────────────────────────────────────────

def test_extract_build_failure_runs_extractor(jr, fake_subprocess, tmp_path):
    jr._extract_build_failure(tmp_path, "setuptools", "j1")
    assert fake_subprocess.called_with("extract-build-failure.py")
    assert fake_subprocess.called_with("--pkg setuptools")


def test_extract_build_failure_swallows_errors(jr, fake_subprocess, tmp_path, capsys):
    fake_subprocess.when(lambda s: "extract-build-failure.py" in s,
                         exc=OSError("boom"))
    jr._extract_build_failure(tmp_path, "setuptools", "j1")  # 不抛异常
    assert "extract-build-failure error" in capsys.readouterr().out


# ─────────────────────────────────────────────
# _poll_chroot_until_done
# ─────────────────────────────────────────────

def test_poll_returns_terminal_from_chroots(jr, monkeypatch):
    calls = []

    def get_build(bid, login, token):
        calls.append(bid)
        return {"state": "running", "chroots": {"c1": "succeeded"}}

    _inject_copr(monkeypatch, get_build)
    logs = []
    state = jr._poll_chroot_until_done(123, "c1", "u", "t", logs.append,
                                       max_wait=5, interval=0.01)
    assert state == "succeeded"
    assert calls == [123]


def test_poll_falls_back_to_overall_state(jr, monkeypatch):
    _inject_copr(monkeypatch, lambda b, l, t: {"state": "failed", "chroots": {}})
    logs = []
    state = jr._poll_chroot_until_done(1, "c1", "u", "t", logs.append,
                                       max_wait=5, interval=0.01)
    assert state == "failed"


def test_poll_transitions_until_terminal(jr, monkeypatch):
    states = iter(["running", "running", "succeeded"])

    def get_build(bid, login, token):
        s = next(states)
        return {"state": s, "chroots": {"c1": s}}

    _inject_copr(monkeypatch, get_build)
    logs = []
    state = jr._poll_chroot_until_done(1, "c1", "u", "t", logs.append,
                                       max_wait=5, interval=0.01)
    assert state == "succeeded"
    assert any("succeeded" in m for m in logs)


def test_poll_exceptions_then_deadline_unknown(jr, monkeypatch):
    def boom(bid, login, token):
        raise RuntimeError("api down")

    _inject_copr(monkeypatch, boom)
    logs = []
    state = jr._poll_chroot_until_done(1, "c1", "u", "t", logs.append,
                                       max_wait=0.03, interval=0.01)
    assert state == "unknown"
    assert any("轮询出错" in m for m in logs)


def test_poll_timeout_returns_last_state(jr, monkeypatch):
    _inject_copr(monkeypatch,
                 lambda b, l, t: {"state": "running", "chroots": {"c1": "running"}})
    logs = []
    state = jr._poll_chroot_until_done(1, "c1", "u", "t", logs.append,
                                       max_wait=0.03, interval=0.01)
    assert state == "running"


# ─────────────────────────────────────────────
# _collect_chroot_status
# ─────────────────────────────────────────────

def _write_collect_session(d, chroots=None, single=None, pkgname="", dep_registry=None,
                           main_chroot_status=None):
    d.mkdir(parents=True, exist_ok=True)
    sess = {}
    if pkgname:
        sess["pkgname"] = pkgname
    if chroots is not None:
        sess["copr_chroots"] = chroots
    if single:
        sess["copr_chroot"] = single
    (d / "session.json").write_text(json.dumps(sess))
    if dep_registry is not None:
        (d / "dep_registry.json").write_text(json.dumps(dep_registry))
    if main_chroot_status is not None and pkgname:
        pdir = d / "pkgs" / pkgname
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "build_rpm_result.json").write_text(
            json.dumps({"chroot_status": main_chroot_status}))


def test_collect_no_session_dir(jr, tmp_path):
    assert jr._collect_chroot_status(tmp_path / "none") == {}


def test_collect_session_without_chroots(jr, tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    (d / "session.json").write_text("{}")
    assert jr._collect_chroot_status(d) == {}


def test_collect_corrupt_session(jr, tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    (d / "session.json").write_text("{bad")
    assert jr._collect_chroot_status(d) == {}


def test_collect_corrupt_dep_registry(jr, tmp_path):
    d = tmp_path / "s"
    _write_collect_session(d, chroots=["c1"])
    (d / "dep_registry.json").write_text("{bad")
    assert jr._collect_chroot_status(d) == {}


@pytest.mark.parametrize("st,expected", [
    ("build_done", "succeeded"),
    ("reused", "succeeded"),
    ("failed", "failed"),
    ("skipped", "skipped"),
    ("building", "skipped"),
    ("pending_evaluate", "skipped"),
    ("", "skipped"),
])
def test_collect_dep_status_mapping(jr, tmp_path, st, expected):
    d = tmp_path / "s"
    _write_collect_session(d, chroots=["c1"],
                           dep_registry={"depA": {"chroots": {"c1": {
                               "status": st, "build_id": 42}}}})
    assert jr._collect_chroot_status(d) == {
        "c1": {"status": expected, "build_id": 42}}


def test_collect_corrupt_main_build_result(jr, tmp_path):
    d = tmp_path / "s"
    _write_collect_session(d, pkgname="mainpkg", chroots=["c1"])
    pdir = d / "pkgs" / "mainpkg"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "build_rpm_result.json").write_text("{bad")
    # 主包 br 损坏被吞掉,降级为无 per-chroot 数据 → 只含主 chroot 的默认映射
    assert jr._collect_chroot_status(d, "success") == {
        "c1": {"status": "succeeded", "build_id": None}}


def test_collect_failed_wins_across_deps(jr, tmp_path):
    d = tmp_path / "s"
    _write_collect_session(d, chroots=["c1"], dep_registry={
        "depA": {"chroots": {"c1": {"status": "build_done", "build_id": 1}}},
        "depB": {"chroots": {"c1": {"status": "failed", "build_id": 2}}},
    })
    assert jr._collect_chroot_status(d) == {
        "c1": {"status": "failed", "build_id": 1}}


def test_collect_skipped_over_succeeded(jr, tmp_path):
    d = tmp_path / "s"
    _write_collect_session(d, chroots=["c1"], dep_registry={
        "depA": {"chroots": {"c1": {"status": "build_done"}}},
        "depB": {"chroots": {"c1": {"status": "skipped"}}},
    })
    assert jr._collect_chroot_status(d) == {
        "c1": {"status": "skipped", "build_id": None}}


def test_collect_skips_non_dict_entries(jr, tmp_path):
    d = tmp_path / "s"
    _write_collect_session(d, chroots=["c1"], dep_registry={
        "depX": "not-dict",
        "depZ": {"chroots": {"c1": {"status": "build_done", "build_id": 3}}},
    })
    assert jr._collect_chroot_status(d) == {
        "c1": {"status": "succeeded", "build_id": 3}}


def test_collect_non_dict_chroots_poisons_whole_result(jr, tmp_path):
    # 实际行为:entry["chroots"] 为非 dict 字符串时 "x".get(...) 抛 AttributeError,
    # 被外层 except 吞掉 → 整个聚合返回 {}(而非跳过该条目)——按实际行为断言
    d = tmp_path / "s"
    _write_collect_session(d, chroots=["c1"], dep_registry={
        "depY": {"chroots": "not-dict"},
        "depZ": {"chroots": {"c1": {"status": "build_done", "build_id": 3}}},
    })
    assert jr._collect_chroot_status(d) == {}


def test_collect_main_pkg_priority(jr, tmp_path):
    d = tmp_path / "s"
    _write_collect_session(d, pkgname="mainpkg",
                           chroots=["a-x86_64", "b-aarch64"],
                           dep_registry={"depA": {"chroots": {
                               "a-x86_64": {"status": "build_done", "build_id": 1},
                               "b-aarch64": {"status": "failed", "build_id": 2}}}},
                           main_chroot_status={"a-x86_64": {
                               "status": "failed", "build_id": 9}})
    assert jr._collect_chroot_status(d) == {
        "a-x86_64": {"status": "failed", "build_id": 9},   # 主包优先
        "b-aarch64": {"status": "failed", "build_id": 2},
    }


def test_collect_build_id_prefers_main_entry(jr, tmp_path):
    d = tmp_path / "s"
    _write_collect_session(d, pkgname="mainpkg", chroots=["c1"], dep_registry={
        "depA": {"chroots": {"c1": {"status": "build_done", "build_id": 1}}},
        "mainpkg": {"chroots": {"c1": {"status": "build_done", "build_id": 7}}},
    })
    assert jr._collect_chroot_status(d) == {
        "c1": {"status": "succeeded", "build_id": 7}}


def test_collect_main_status_success_maps_skipped(jr, tmp_path):
    # _map_status 只识别 failed/build_done/reused,"success" 落入 skipped——按实际行为断言
    d = tmp_path / "s"
    _write_collect_session(d, pkgname="mainpkg", chroots=["c1"],
                           main_chroot_status={"c1": {
                               "status": "success", "build_id": 9}})
    assert jr._collect_chroot_status(d) == {
        "c1": {"status": "skipped", "build_id": 9}}


def test_collect_main_chroot_status_filters_non_dict(jr, tmp_path):
    d = tmp_path / "s"
    _write_collect_session(d, pkgname="mainpkg", chroots=["c1", "c2"],
                           main_chroot_status={
                               "c1": "not-dict",
                               "c2": {"status": "build_done", "build_id": 3},
                           })
    assert jr._collect_chroot_status(d) == {
        "c2": {"status": "succeeded", "build_id": 3}}


@pytest.mark.parametrize("job_status,expected", [
    ("success", "succeeded"),
    ("failed", "failed"),
    ("running", "skipped"),
    ("", "skipped"),
])
def test_collect_fallback_primary_chroot(jr, tmp_path, job_status, expected):
    d = tmp_path / "s"
    _write_collect_session(d, chroots=["z-aarch64", "m-x86_64"])
    assert jr._collect_chroot_status(d, job_status) == {
        "m-x86_64": {"status": expected, "build_id": None}}


def test_collect_uses_single_chroot_fallback(jr, tmp_path):
    d = tmp_path / "s"
    _write_collect_session(d, single="single-chroot", dep_registry={
        "depA": {"chroots": {"single-chroot": {
            "status": "build_done", "build_id": 5}}}})
    assert jr._collect_chroot_status(d) == {
        "single-chroot": {"status": "succeeded", "build_id": 5}}


# ─────────────────────────────────────────────
# _collect_pkg_decisions
# ─────────────────────────────────────────────

def _write_gate(d, pkg_dir, filename, content):
    p = d / "pkgs" / pkg_dir
    p.mkdir(parents=True, exist_ok=True)
    (p / filename).write_text(json.dumps(content))


def test_decisions_empty(jr, tmp_path):
    d = tmp_path / "s"
    (d / "pkgs").mkdir(parents=True)
    assert jr._collect_pkg_decisions(d) == {}


def test_decisions_with_disposition(jr, tmp_path):
    d = tmp_path / "s"
    _write_gate(d, "a", "gate_result_a.json", {
        "pkgname": "a", "disposition": "upgrade", "version": "1.2",
        "result": {"reason": "r"}})
    assert jr._collect_pkg_decisions(d) == {
        "a": {"disposition": "upgrade", "version": "1.2", "reason": "r"}}


@pytest.mark.parametrize("decision,expected", [
    ("reuse_official", "reuse"),
    ("reuse_copr_project", "reuse"),
    ("reuse_additional_repo", "reuse"),
    ("introduce_new", "introduce_new"),
    ("evaluate", "introduce_new"),
    ("", "introduce_new"),
])
def test_decisions_derived_from_decision(jr, tmp_path, decision, expected):
    d = tmp_path / "s"
    _write_gate(d, "p", "gate_result_p.json", {
        "pkgname": "p", "result": {"decision": decision, "version": "9",
                                   "reason": "why"}})
    assert jr._collect_pkg_decisions(d) == {
        "p": {"disposition": expected, "version": "9", "reason": "why"}}


def test_decisions_name_from_dir_and_version_fallback(jr, tmp_path):
    d = tmp_path / "s"
    _write_gate(d, "somedir", "gate_result_1.json", {
        "result": {"decision": "reuse_copr_project", "version": "5",
                   "reason": ""}})
    assert jr._collect_pkg_decisions(d) == {
        "somedir": {"disposition": "reuse", "version": "5", "reason": ""}}


def test_decisions_skips_corrupt_files(jr, tmp_path):
    d = tmp_path / "s"
    _write_gate(d, "good", "gate_result_good.json", {
        "pkgname": "good", "disposition": "reuse", "version": "1"})
    p = d / "pkgs" / "bad"
    p.mkdir(parents=True, exist_ok=True)
    (p / "gate_result_bad.json").write_text("{bad")
    assert list(jr._collect_pkg_decisions(d)) == ["good"]


def test_decisions_multiple_pkgs_sorted(jr, tmp_path):
    d = tmp_path / "s"
    _write_gate(d, "a", "gate_result_a.json", {
        "pkgname": "a", "disposition": "reuse", "version": "1"})
    _write_gate(d, "b", "gate_result_b.json", {
        "pkgname": "b", "disposition": "introduce_new", "version": "2"})
    dispos = jr._collect_pkg_decisions(d)
    assert list(dispos) == ["a", "b"]
    assert dispos["b"]["disposition"] == "introduce_new"


# ─────────────────────────────────────────────
# _sync_copr_result
# ─────────────────────────────────────────────

def _sync_session(tmp_path, pkgname="setuptools", br=None, session=None,
                  dep_registry=None):
    d = tmp_path / "s"
    (d / "pkgs" / pkgname).mkdir(parents=True)
    (d / "session.json").write_text(json.dumps({
        "copr_login": "u", "copr_token": "t", "copr_owner": "own",
        "copr_project": "proj", "copr_chroots": ["a-x86_64"],
        **(session or {}),
    }))
    if br is not None:
        (d / "pkgs" / pkgname / "build_rpm_result.json").write_text(json.dumps(br))
    if dep_registry is not None:
        (d / "dep_registry.json").write_text(json.dumps(dep_registry))
    return d


def _br(d, pkgname):
    return d / "pkgs" / pkgname / "build_rpm_result.json"


def _read_br(d, pkgname):
    return json.loads(_br(d, pkgname).read_text())


def test_sync_empty_pkgname_noop(jr, tmp_path):
    d = _sync_session(tmp_path, "setuptools", br={"copr_build_id": 1})
    jr._sync_copr_result(d, "", "j1")
    assert _read_br(d, "setuptools") == {"copr_build_id": 1}


def test_sync_missing_br_noop(jr, tmp_path):
    d = _sync_session(tmp_path, "setuptools")  # 无 build_rpm_result.json
    jr._sync_copr_result(d, "setuptools", "j1")


def test_sync_missing_build_id_noop(jr, monkeypatch, tmp_path):
    calls = []
    _inject_copr(monkeypatch, lambda b, l, t: calls.append(b) or _build_data())
    d = _sync_session(tmp_path, "setuptools",
                      br={"copr_chroots": ["a-x86_64"]})
    jr._sync_copr_result(d, "setuptools", "j1")
    assert calls == []
    assert _read_br(d, "setuptools") == {"copr_chroots": ["a-x86_64"]}


def test_sync_single_chroot_success(jr, monkeypatch, tmp_path):
    calls = []
    _inject_copr(monkeypatch, lambda b, l, t: calls.append(b) or _build_data())
    url_calls = _install_urlopen(monkeypatch, _dir_html(123))
    d = _sync_session(tmp_path, "setuptools",
                      br={"copr_build_id": 123, "copr_chroot": "a-x86_64"})
    jr._sync_copr_result(d, "setuptools", "j1")

    br = _read_br(d, "setuptools")
    assert calls == [123]
    assert br["status"] == "success"
    assert br["copr_status"] == "succeeded"
    cr = br["chroot_results"]["a-x86_64"]
    assert cr["state"] == "succeeded"
    assert cr["build_id"] == 123
    assert "=== BUILD LOG START" in cr["build_log"]
    assert "build log line" in cr["build_log"]
    assert br["build_log"] == cr["build_log"]          # legacy 字段镜像主 chroot
    assert br["build_log_tail"] == cr["build_log_tail"]
    assert any(u.endswith("builder-live.log.gz") for u in url_calls)
    evts = _events(d, "build.completed")
    assert evts[0]["data"]["build_id"] == "123"
    assert evts[0]["data"]["status"] == "succeeded"
    assert evts[0]["data"]["copr_chroot"] == "a-x86_64"


def test_sync_skips_already_fetched_chroot(jr, monkeypatch, tmp_path):
    calls = []
    _inject_copr(monkeypatch, lambda b, l, t: calls.append(b) or _build_data())
    url_calls = _install_urlopen(monkeypatch, _dir_html(123))
    d = _sync_session(tmp_path, "setuptools", br={
        "copr_build_id": 123, "copr_chroot": "a-x86_64",
        "chroot_results": {"a-x86_64": {"build_log": "x"}}})
    jr._sync_copr_result(d, "setuptools", "j1")
    assert calls == []
    assert url_calls == []


def test_sync_skips_primary_with_legacy_log(jr, monkeypatch, tmp_path):
    calls = []
    _inject_copr(monkeypatch, lambda b, l, t: calls.append(b) or _build_data())
    url_calls = _install_urlopen(monkeypatch, _dir_html(123))
    d = _sync_session(tmp_path, "setuptools", br={
        "copr_build_id": 123, "copr_chroot": "a-x86_64", "build_log": "old"})
    jr._sync_copr_result(d, "setuptools", "j1")
    assert calls == []
    assert url_calls == []


def test_sync_name_mismatch_normal_mode(jr, monkeypatch, fake_subprocess, tmp_path):
    _inject_copr(monkeypatch, lambda b, l, t: _build_data(name="python3-otherpkg"))
    _install_urlopen(monkeypatch, _dir_html(123))
    d = _sync_session(tmp_path, "setuptools",
                      br={"copr_build_id": 123, "copr_chroot": "a-x86_64"})
    jr._sync_copr_result(d, "setuptools", "j1")

    br = _read_br(d, "setuptools")
    assert br["status"] == "failed"
    assert "Package name mismatch" in br["failure_reason"]
    assert "otherpkg" in br["pkgname_mismatch"]
    fs = json.loads((d / "pkgs" / "setuptools" / "fix_state.json").read_text())
    assert fs["mismatch_count"] == 1
    assert fake_subprocess.called_with("extract-build-failure.py")


def test_sync_name_mismatch_ros_mode(jr, monkeypatch, fake_subprocess, tmp_path):
    _inject_copr(monkeypatch,
                 lambda b, l, t: _build_data(name="ros-humble-wrongpkg"))
    _install_urlopen(monkeypatch, _dir_html(123))
    d = _sync_session(tmp_path, "setuptools",
                      br={"copr_build_id": 123, "copr_chroot": "a-x86_64"},
                      session={"import_type": "ros", "ros_distro": "humble"})
    jr._sync_copr_result(d, "setuptools", "j1")
    br = _read_br(d, "setuptools")
    assert br["status"] == "failed"
    assert "expected 'ros-humble-setuptools'" in br["pkgname_mismatch"]


def test_sync_java_gav_name_matches(jr, monkeypatch, tmp_path):
    # Java:pkgname 是 Maven GAV,rpm_name_from_gav 归一后与 artifactId 相等 → 无 mismatch
    pkgname = "com.google.j2objc:j2objc-annotations"
    _inject_copr(monkeypatch,
                 lambda b, l, t: _build_data(name="j2objc-annotations"))
    _install_urlopen(monkeypatch, _dir_html(123))
    d = _sync_session(tmp_path, pkgname, br={
        "copr_build_id": 123, "copr_chroot": "a-x86_64"})
    gate = d / "pkgs" / pkgname / f"gate_result_{pkgname}.json"
    gate.write_text(json.dumps({"lang": "java"}))
    jr._sync_copr_result(d, pkgname, "j1")
    assert _read_br(d, pkgname)["status"] == "success"


def test_sync_failed_state(jr, monkeypatch, fake_subprocess, tmp_path):
    _inject_copr(monkeypatch, lambda b, l, t: _build_data(state="failed"))
    _install_urlopen(monkeypatch, _dir_html(123))
    d = _sync_session(tmp_path, "setuptools",
                      br={"copr_build_id": 123, "copr_chroot": "a-x86_64"})
    jr._sync_copr_result(d, "setuptools", "j1")
    br = _read_br(d, "setuptools")
    assert br["status"] == "failed"
    assert br["copr_status"] == "failed"
    assert br["failure_reason"] == "copr build failed"
    assert fake_subprocess.called_with("extract-build-failure.py")


def test_sync_multi_chroot_partial_failure(jr, monkeypatch, fake_subprocess, tmp_path):
    def get_build(bid, login, token):
        if bid == 1:
            return {"state": "succeeded", "source_package": {"name": "setuptools"},
                    "chroots": {"a-x86_64": "succeeded"}}
        return {"state": "failed", "source_package": {"name": "setuptools"},
                "chroots": {"b-aarch64": "failed"}}

    _inject_copr(monkeypatch, get_build)
    url_calls = _install_urlopen(monkeypatch, _dir_html(1) + _dir_html(2))
    d = _sync_session(tmp_path, "setuptools", br={
        "copr_build_ids": {"a-x86_64": 1, "b-aarch64": 2},
        "copr_chroots": ["a-x86_64", "b-aarch64"]})
    jr._sync_copr_result(d, "setuptools", "j1")

    br = _read_br(d, "setuptools")
    assert br["status"] == "failed"
    assert br["failure_reason"] == "copr build failed chroots: b-aarch64"
    assert br["chroot_results"]["a-x86_64"]["state"] == "succeeded"
    assert br["chroot_results"]["b-aarch64"]["state"] == "failed"
    assert br["chroot_results"]["b-aarch64"]["build_id"] == 2
    assert sum("a-x86_64" in u for u in url_calls) >= 1
    assert sum("b-aarch64" in u for u in url_calls) >= 1
    assert len(_events(d, "build.completed")) == 2


def test_sync_get_build_error_keeps_file(jr, monkeypatch, tmp_path):
    def boom(bid, login, token):
        raise RuntimeError("api down")

    _inject_copr(monkeypatch, boom)
    _install_urlopen(monkeypatch, _dir_html(123))
    d = _sync_session(tmp_path, "setuptools",
                      br={"copr_build_id": 123, "copr_chroot": "a-x86_64"})
    jr._sync_copr_result(d, "setuptools", "j1")
    assert _read_br(d, "setuptools") == {"copr_build_id": 123,
                                         "copr_chroot": "a-x86_64"}


def test_sync_log_fetch_failure_still_records_state(jr, monkeypatch, tmp_path):
    _inject_copr(monkeypatch, lambda b, l, t: _build_data())
    _install_urlopen(monkeypatch, _dir_html(123), fail_logs=True)
    d = _sync_session(tmp_path, "setuptools",
                      br={"copr_build_id": 123, "copr_chroot": "a-x86_64"})
    jr._sync_copr_result(d, "setuptools", "j1")
    cr = _read_br(d, "setuptools")["chroot_results"]["a-x86_64"]
    assert cr["state"] == "succeeded"
    assert cr["build_log"] == ""


def test_sync_non_terminal_polls(jr, monkeypatch, tmp_path):
    _inject_copr(monkeypatch, lambda b, l, t: {
        "state": "running", "source_package": {"name": "setuptools"},
        "chroots": {"a-x86_64": "running"}})
    _install_urlopen(monkeypatch, _dir_html(123))
    polled = []
    monkeypatch.setattr(jr, "_poll_chroot_until_done",
                        lambda bid, c, l, t, log_fn, max_wait=3600, interval=10:
                        polled.append((bid, c)) or "succeeded")
    d = _sync_session(tmp_path, "setuptools",
                      br={"copr_build_id": 123, "copr_chroot": "a-x86_64"})
    jr._sync_copr_result(d, "setuptools", "j1")
    assert polled == [(123, "a-x86_64")]
    assert _read_br(d, "setuptools")["status"] == "success"


def test_sync_dep_registry_fallback(jr, monkeypatch, tmp_path):
    calls = []

    def get_build(bid, login, token):
        calls.append(bid)
        return {"state": "succeeded", "source_package": {"name": "setuptools"},
                "chroots": {"c9": "succeeded"}}

    _inject_copr(monkeypatch, get_build)
    _install_urlopen(monkeypatch, _dir_html(55))
    d = _sync_session(tmp_path, "setuptools", br={}, dep_registry={
        "setuptools": {"copr_chroot": "c9", "copr_build_id": 9,
                       "chroots": {"c9": {"build_id": 55}}}})
    jr._sync_copr_result(d, "setuptools", "j1")
    assert calls == [55]
    cr = _read_br(d, "setuptools")["chroot_results"]["c9"]
    assert cr["build_id"] == 55


def test_sync_chroots_from_session(jr, monkeypatch, tmp_path):
    calls = []
    _inject_copr(monkeypatch, lambda b, l, t: calls.append(b) or _build_data())
    _install_urlopen(monkeypatch, _dir_html(7))
    d = _sync_session(tmp_path, "setuptools", br={"copr_build_id": 7})
    jr._sync_copr_result(d, "setuptools", "j1")
    assert calls == [7]
    assert _read_br(d, "setuptools")["chroot_results"]["a-x86_64"]["build_id"] == 7


def test_sync_log_escapes_fake_end_marker(jr, monkeypatch, tmp_path):
    _inject_copr(monkeypatch, lambda b, l, t: _build_data())
    _install_urlopen(monkeypatch, _dir_html(123),
                     log_bytes=b"pre\n=== BUILD LOG END ===\npost")
    d = _sync_session(tmp_path, "setuptools",
                      br={"copr_build_id": 123, "copr_chroot": "a-x86_64"})
    jr._sync_copr_result(d, "setuptools", "j1")
    cr = _read_br(d, "setuptools")["chroot_results"]["a-x86_64"]
    assert "=== BUILD LOG END (escaped) ===" in cr["build_log"]
    # 正文中的假 END 已转义,只剩 footer 一个真实 END
    assert cr["build_log"].count("=== BUILD LOG END ===") == 1


# ─────────────────────────────────────────────
# run_job — 输入校验(不跑完整循环)
# ─────────────────────────────────────────────

def _run(jr, r, proj="openeuler/test-proj", job_id="job1"):
    jr.run_job(r, proj, job_id)


@pytest.mark.parametrize("overrides,error", [
    ({"pkgname": "foo\u200bbar"}, "invalid pkgname: contains control characters"),
    ({"pkgname": "foo\u202ebar"}, "invalid pkgname: contains control characters"),
    ({"pkgname": "foo\nbar"}, "invalid pkgname: 'foo\\nbar'"),
    ({"pkgname": "foo bar"}, "invalid pkgname: 'foo bar'"),
    ({"pkgname": "foo;rm"}, "invalid pkgname: 'foo;rm'"),
    ({"pkgname": "x" * 129}, f"invalid pkgname: {('x' * 129)!r}"),
    ({"url": "https://a/\u200bx"}, "invalid url: contains control characters"),
    ({"url": "ftp://x/y"}, "invalid url: 'ftp://x/y'"),
    ({"url": "https://a/\nb"}, "invalid url: 'https://a/\\nb'"),
    ({"url": ""}, "invalid url: ''"),
    ({"url": "https://" + "a" * 600}, f"invalid url: {('https://' + 'a' * 600)!r}"),
    ({"version": "1.0\u200b"}, "invalid version: contains control characters"),
    ({"version": "1.0;rm"}, "invalid version: '1.0;rm'"),
])
def test_run_job_rejects_invalid_input(jr, redis_stub, fake_subprocess,
                                       overrides, error):
    r = _make_r(redis_stub, **overrides)
    _run(jr, r)
    assert _job_status(r) == "failed"
    assert r.hget("job:ai:job1", "error") == error


def test_run_job_normalizes_python_prefix(jr, redis_stub, fake_subprocess):
    r = _make_r(redis_stub, pkgname="python-numpy", copr_login="", copr_token="")
    _run(jr, r)
    assert _job_status(r) == "failed"
    assert r.hget("job:ai:job1", "error") == "missing credentials"
    assert any("归一化" in m and "numpy" in m for m in _logs(r))


def test_run_job_ros_skips_prefix_normalization(jr, redis_stub, fake_subprocess):
    r = _make_r(redis_stub, pkgname="python-numpy", mode="ros", url="",
                copr_login="", copr_token="")
    _run(jr, r)
    assert r.hget("job:ai:job1", "error") == "missing credentials"
    assert not any("归一化" in m for m in _logs(r))


def test_run_job_ros_allows_empty_url(jr, redis_stub, fake_subprocess):
    r = _make_r(redis_stub, mode="ros", url="")
    _run(jr, r)
    # 空 url 通过校验(ROS 由 rosdistro 索引定位),正常进入 supervisor 循环
    assert r.hget("job:ai:job1", "error") == "supervisor returned no action"


def test_run_job_cancelled_before_start(jr, redis_stub, fake_subprocess):
    r = _make_r(redis_stub, status="cancelled")
    _run(jr, r)
    assert _job_status(r) == "cancelled"
    assert "error" not in r.hgetall("job:ai:job1")


def test_run_job_missing_chroot(jr, redis_stub, fake_subprocess):
    r = _make_r(redis_stub, copr_chroots="", copr_chroot="")
    _run(jr, r)
    assert _job_status(r) == "failed"
    assert r.hget("job:ai:job1", "error") == "missing chroot"


# ─────────────────────────────────────────────
# run_job — session 初始化 + supervisor 循环终态
# ─────────────────────────────────────────────

def test_run_job_no_action_finishes(jr, redis_stub, fake_subprocess, tmp_path):
    r = _make_r(redis_stub)
    _run(jr, r)
    assert _job_status(r) == "failed"
    assert r.hget("job:ai:job1", "error") == "supervisor returned no action"
    d = _session_dir(tmp_path)
    sess = json.loads((d / "session.json").read_text())
    assert sess["pkgname"] == "setuptools"
    assert sess["upstream_url"].startswith("https://pypi.org")
    assert sess["version"] == "68.0.0"
    assert sess["import_type"] == "normal"
    assert sess["mode"] == "normal"
    assert sess["deep_dependency"] is False
    assert sess["copr_owner"] == "openeuler"
    assert sess["copr_project"] == "test-proj"
    assert sess["copr_login"] == "user"
    assert sess["copr_chroot"] == "openeuler-24.03-x86_64"
    assert sess["copr_chroots"] == ["openeuler-24.03-x86_64"]
    assert (d / "dep_registry.json").read_text() == "{}"
    assert (d / "build_state" / "introduced.txt").exists()
    wf = json.loads((d / "workflow_setuptools.json").read_text())
    assert wf["goal"] == "build_success"
    created = _events(d, "session.created")
    assert created[0]["data"]["copr_project"] == "openeuler/test-proj"


def test_run_job_chroot_fallback_and_primary(jr, redis_stub, fake_subprocess, tmp_path):
    r = _make_r(redis_stub, copr_chroots="", copr_chroot="legacy-x86_64")
    _run(jr, r)
    sess = json.loads((_session_dir(tmp_path) / "session.json").read_text())
    assert sess["copr_chroot"] == "legacy-x86_64"
    assert sess["copr_chroots"] == ["legacy-x86_64"]


def test_run_job_primary_chroot_prefers_x86_64(jr, redis_stub, fake_subprocess,
                                               tmp_path):
    r = _make_r(redis_stub, copr_chroots=json.dumps(["zz-aarch64", "aa-x86_64"]))
    _run(jr, r)
    sess = json.loads((_session_dir(tmp_path) / "session.json").read_text())
    assert sess["copr_chroot"] == "aa-x86_64"


def test_run_job_ros_session_fields(jr, redis_stub, fake_subprocess, tmp_path):
    r = _make_r(redis_stub, mode="ros", url="", ros_distro="humble",
                deep_dependency="1")
    _run(jr, r)
    sess = json.loads((_session_dir(tmp_path) / "session.json").read_text())
    assert sess["import_type"] == "ros"
    assert sess["mode"] == "ros"
    assert sess["ros_distro"] == "humble"
    assert sess["deep_dependency"] is True


def _precreate_done_session(tmp_path, job_id="job1", **wf_overrides):
    d = _session_dir(tmp_path, job_id)
    (d / "pkgs" / "setuptools").mkdir(parents=True, exist_ok=True)
    wf = {"pkgname": "setuptools", "goal": "build_success", "loop_count": 7,
          "max_loops": 200, "built_pkgs": ["setuptools"], "reused_pkgs": ["dep1"],
          "error": None}
    wf.update(wf_overrides)
    (d / "workflow_setuptools.json").write_text(json.dumps(wf))
    return d


def test_run_job_done_path(jr, redis_stub, fake_subprocess, tmp_path):
    d = _precreate_done_session(tmp_path)
    (d / "pkgs" / "setuptools" / "gate_result_setuptools.json").write_text(
        json.dumps({"pkgname": "setuptools", "disposition": "upgrade",
                    "version": "1.9", "result": {"reason": "need new"}}))
    (d / "pkgs" / "setuptools" / "setuptools_introduction_report.md").write_text(
        "# report\n")
    r = _make_r(redis_stub)
    _sup_always(fake_subprocess, "ACTION=done\n")
    _run(jr, r)

    assert _job_status(r) == "success"
    h = r.hgetall("job:ai:job1")
    assert h["built_pkgs"] == "setuptools"
    assert h["reused_pkgs"] == "dep1"
    assert h["loop_count"] == "7"
    assert h["error"] == ""
    assert json.loads(h["pkg_decisions"]) == {"setuptools": {
        "disposition": "upgrade", "version": "1.9", "reason": "need new"}}
    assert h["report"] == "# report\n"
    assert _events(d, "session.completed")[0]["data"]["status"] == "success"


@pytest.mark.parametrize("wf_error,target,expected", [
    (None, "supervisor says why", "supervisor says why"),
    ("wf specific reason", "supervisor says why", "wf specific reason"),
    ("unknown failure", "real reason", "real reason"),
    ("setuptools", "real reason", "real reason"),   # wf.error 只写了包名 → 用 target
])
def test_run_job_fail_path_error_selection(jr, redis_stub, fake_subprocess,
                                           tmp_path, wf_error, target, expected):
    over = {"error": wf_error} if wf_error is not None else {}
    d = _precreate_done_session(tmp_path, **over)
    r = _make_r(redis_stub)
    _sup_always(fake_subprocess, f"ACTION=fail\nTARGET={target}\n")
    _run(jr, r)
    assert _job_status(r) == "failed"
    assert r.hget("job:ai:job1", "error") == expected
    assert _events(d, "session.completed")[0]["data"]["status"] == "failed"


def test_run_job_fail_ros_missing_pkgs(jr, redis_stub, fake_subprocess, tmp_path):
    d = _precreate_done_session(tmp_path)
    (d / "pkgs" / "setuptools" / "missing_deps_setuptools.txt").write_text(
        "pkgA\npkgB\n\n")
    r = _make_r(redis_stub, mode="ros", url="", ros_distro="humble")
    _sup_always(fake_subprocess, "ACTION=fail\nTARGET=boom\n")
    _run(jr, r)
    assert r.hget("job:ai:job1", "missing_pkgs") == "pkgA pkgB"


def test_run_job_fail_path_writes_decisions_and_report(jr, redis_stub,
                                                       fake_subprocess, tmp_path):
    d = _precreate_done_session(tmp_path)
    (d / "pkgs" / "setuptools" / "gate_result_setuptools.json").write_text(
        json.dumps({"pkgname": "setuptools", "disposition": "introduce_new",
                    "version": "2.0", "result": {"reason": "new pkg"}}))
    (d / "pkgs" / "setuptools" / "setuptools_introduction_report.md").write_text(
        "# failed report\n")
    r = _make_r(redis_stub)
    _sup_always(fake_subprocess, "ACTION=fail\nTARGET=reason here\n")
    _run(jr, r)
    h = r.hgetall("job:ai:job1")
    assert h["error"] == "reason here"
    assert json.loads(h["pkg_decisions"]) == {"setuptools": {
        "disposition": "introduce_new", "version": "2.0", "reason": "new pkg"}}
    assert h["report"] == "# failed report\n"


# ─────────────────────────────────────────────
# run_job — wait / 超时 / 循环上限
# ─────────────────────────────────────────────

def test_run_job_wait_until_max_loops(worker_scripts, skills_env, loaded_modules,
                                      monkeypatch, redis_stub, fake_subprocess,
                                      tmp_path):
    jr = _load(worker_scripts, skills_env, loaded_modules, monkeypatch,
               MAX_LOOPS="3", SESSIONS_BASE=str(tmp_path / "sessions"))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    r = _make_r(redis_stub)
    _sup_always(fake_subprocess, "ACTION=wait\nDELAY=1\n")
    _run(jr, r)
    assert _job_status(r) == "failed"
    assert r.hget("job:ai:job1", "error") == "max_loops 3 exceeded"
    assert len(_events(_session_dir(tmp_path), "loop.wait")) == 3


def test_run_job_wait_cancelled(jr, redis_stub, fake_subprocess, tmp_path,
                                monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    _sup_always(fake_subprocess, "ACTION=wait\nDELAY=2\n")
    base = _JobRedis(redis_stub)
    base.hset("job:ai:job1", mapping=_valid_job())

    class _Cancel(_JobRedis):
        def __init__(self, fake):
            super().__init__(fake)
            self._n = 0

        def hget(self, key, field):
            if field == "status" and key == "job:ai:job1":
                self._n += 1
                if self._n >= 2:
                    return "cancelled"
            return super().hget(key, field)

    r = _Cancel(redis_stub)
    _run(jr, r)
    assert _job_status(r) == "cancelled"


def test_run_job_wait_invalid_delay_defaults_60(jr, redis_stub, fake_subprocess,
                                                tmp_path, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    _sup_rule(fake_subprocess, "ACTION=wait\nDELAY=abc\n", n=1)
    r = _make_r(redis_stub)
    _run(jr, r)
    # 非法 delay 按 60s 兜底处理,sleep 被 mock 后落到下一轮 supervisor(空 → 结束)
    assert r.hget("job:ai:job1", "error") == "supervisor returned no action"


def test_run_job_timeout(worker_scripts, skills_env, loaded_modules, monkeypatch,
                         redis_stub, fake_subprocess, tmp_path):
    jr = _load(worker_scripts, skills_env, loaded_modules, monkeypatch,
               MAX_JOB_SECONDS="0", SESSIONS_BASE=str(tmp_path / "sessions"))
    t = [0.0]

    def fake_time():
        t[0] += 1.0
        return t[0]

    monkeypatch.setattr(time, "time", fake_time)
    r = _make_r(redis_stub)
    _run(jr, r)
    assert _job_status(r) == "failed"
    assert r.hget("job:ai:job1", "error") == "timeout after 1s"
    assert fake_subprocess.calls == []   # 超时先于 supervisor 检查


def test_run_job_ros_max_loops_scaling(worker_scripts, skills_env, loaded_modules,
                                       monkeypatch, redis_stub, fake_subprocess,
                                       tmp_path):
    jr = _load(worker_scripts, skills_env, loaded_modules, monkeypatch,
               MAX_LOOPS="3", SESSIONS_BASE=str(tmp_path / "sessions"))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    d = _session_dir(tmp_path)
    (d / "pkgs" / "setuptools").mkdir(parents=True, exist_ok=True)
    (d / "dep_registry.json").write_text(json.dumps({"dep1": {"status": "build_done"}}))
    r = _make_r(redis_stub, mode="ros", url="", ros_distro="humble")
    _sup_always(fake_subprocess, "ACTION=wait\nDELAY=1\n")
    _run(jr, r)
    # ROS 按依赖规模缩放:max(3, 50*(1+1)) = 100
    assert r.hget("job:ai:job1", "error") == "max_loops 100 exceeded"


# ─────────────────────────────────────────────
# run_job — evaluate_main 脚本先行
# ─────────────────────────────────────────────

def test_run_job_evaluate_main_script_done(jr, redis_stub, fake_subprocess,
                                           tmp_path):
    _sup_rule(fake_subprocess, "ACTION=evaluate_main\nTARGET=mainpkg\n", n=1)
    fake_subprocess.when(lambda s: "run_evaluate_dep.py" in s,
                         stdout=json.dumps({"status": "done"}))
    r = _make_r(redis_stub)
    _run(jr, r)
    assert r.hget("job:ai:job1", "error") == "supervisor returned no action"
    skips = [e for e in _events(_session_dir(tmp_path), "loop.skip")
             if e["data"]["script_result"] == "done"]
    assert len(skips) == 1
    assert skips[0]["data"]["target"] == "mainpkg"
    assert skips[0]["data"]["reason"] == "script_direct_evaluate"
    assert not (_session_dir(tmp_path) / "script_fail_counts.json").exists()


def test_run_job_evaluate_main_failed_then_claude(worker_scripts, skills_env,
                                                  loaded_modules, monkeypatch,
                                                  redis_stub, fake_subprocess,
                                                  tmp_path):
    jr = _load(worker_scripts, skills_env, loaded_modules, monkeypatch,
               MAX_SCRIPT_FAILS="2", SESSIONS_BASE=str(tmp_path / "sessions"))
    _sup_rule(fake_subprocess, "ACTION=evaluate_main\nTARGET=mainpkg\n", n=2)
    fake_subprocess.when(lambda s: "run_evaluate_dep.py" in s,
                         stdout=json.dumps({"status": "failed",
                                            "reason": "no upstream"}))
    popen = _install_popen(monkeypatch, _FakeProc(""))
    r = _make_r(redis_stub)
    _run(jr, r)
    assert popen["cmd"][0] == "claude"
    counts = json.loads((_session_dir(tmp_path) /
                         "script_fail_counts.json").read_text())
    assert counts == {"evaluate_main:mainpkg": 2}
    assert any("连续 failed 2 次" in m for m in _logs(r))


def test_run_job_evaluate_main_needs_ai(jr, redis_stub, fake_subprocess,
                                        tmp_path, monkeypatch):
    _sup_rule(fake_subprocess, "ACTION=evaluate_main\nTARGET=mainpkg\n", n=1)
    fake_subprocess.when(lambda s: "run_evaluate_dep.py" in s,
                         stdout=json.dumps({"status": "needs_ai"}))
    popen = _install_popen(monkeypatch, _FakeProc(""))
    r = _make_r(redis_stub)
    _run(jr, r)
    assert popen["cmd"] is not None
    assert any("needs_ai" in m for m in _logs(r))


def test_run_job_evaluate_main_script_error(jr, redis_stub, fake_subprocess,
                                            tmp_path, monkeypatch):
    _sup_rule(fake_subprocess, "ACTION=evaluate_main\nTARGET=mainpkg\n", n=1)
    fake_subprocess.when(lambda s: "run_evaluate_dep.py" in s,
                         stdout="", returncode=1)
    popen = _install_popen(monkeypatch, _FakeProc(""))
    r = _make_r(redis_stub)
    _run(jr, r)
    assert popen["cmd"] is not None
    assert any("script error" in m for m in _logs(r))


def test_run_job_evaluate_main_timeout_exception(jr, redis_stub, fake_subprocess,
                                                 tmp_path, monkeypatch):
    _sup_rule(fake_subprocess, "ACTION=evaluate_main\nTARGET=mainpkg\n", n=1)
    fake_subprocess.when(lambda s: "run_evaluate_dep.py" in s,
                         exc=subprocess.TimeoutExpired("x", 300))
    popen = _install_popen(monkeypatch, _FakeProc(""))
    r = _make_r(redis_stub)
    _run(jr, r)
    assert popen["cmd"] is not None
    assert any("exception" in m for m in _logs(r))


def test_run_job_ros_evaluate_main_without_url(jr, redis_stub, fake_subprocess,
                                               tmp_path, monkeypatch):
    _sup_rule(fake_subprocess, "ACTION=evaluate_main\nTARGET=mainpkg\n", n=1)
    popen = _install_popen(monkeypatch, _FakeProc(""))
    r = _make_r(redis_stub, mode="ros", url="")
    _run(jr, r)
    assert popen["cmd"] is not None
    assert not any("trying direct evaluate" in m for m in _logs(r))


# ─────────────────────────────────────────────
# run_job — evaluate(依赖)脚本先行
# ─────────────────────────────────────────────

def _precreate_deps(tmp_path, deps):
    d = _session_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / "dep_registry.json").write_text(json.dumps(deps))
    return d


def test_run_job_evaluate_single_dep_done(jr, redis_stub, fake_subprocess,
                                          tmp_path):
    _precreate_deps(tmp_path, {"dep1": {"status": "pending_evaluate",
                                        "url": "https://d1"}})
    _sup_rule(fake_subprocess, "ACTION=evaluate\nTARGET=dep1\n", n=1)
    fake_subprocess.when(lambda s: "run_evaluate_dep.py" in s,
                         stdout=json.dumps({"status": "done"}))
    r = _make_r(redis_stub)
    _run(jr, r)
    skips = [e for e in _events(_session_dir(tmp_path), "loop.skip")
             if e["data"]["target"] == "dep1"]
    assert skips[0]["data"]["script_result"] == "done"
    assert skips[0]["data"]["reason"] == "script_direct_evaluate"
    assert any("dep1 evaluate done" in m for m in _logs(r))


def test_run_job_evaluate_no_eligible_deps(jr, redis_stub, fake_subprocess,
                                           tmp_path, monkeypatch):
    _precreate_deps(tmp_path, {"dep1": {"status": "pending_evaluate"}})  # 无 url
    _sup_rule(fake_subprocess, "ACTION=evaluate\nTARGET=dep1\n", n=1)
    popen = _install_popen(monkeypatch, _FakeProc(""))
    r = _make_r(redis_stub)
    _run(jr, r)
    assert popen["cmd"] is not None
    assert any("no script-eligible" in m for m in _logs(r))


def test_run_job_evaluate_excluded_by_fail_count(jr, redis_stub, fake_subprocess,
                                                 tmp_path, monkeypatch):
    d = _precreate_deps(tmp_path, {"dep1": {"status": "pending_evaluate",
                                            "url": "https://d1"}})
    (d / "script_fail_counts.json").write_text(json.dumps({"evaluate:dep1": 3}))
    _sup_rule(fake_subprocess, "ACTION=evaluate\nTARGET=dep1\n", n=1)
    popen = _install_popen(monkeypatch, _FakeProc(""))
    r = _make_r(redis_stub)
    _run(jr, r)
    assert popen["cmd"] is not None   # 熔断的 dep 交给 Claude


def test_run_job_evaluate_dep_script_error(jr, redis_stub, fake_subprocess,
                                           tmp_path, monkeypatch):
    _precreate_deps(tmp_path, {"dep1": {"status": "pending_evaluate",
                                        "url": "https://d1"}})
    _sup_rule(fake_subprocess, "ACTION=evaluate\nTARGET=dep1\n", n=1)
    fake_subprocess.when(lambda s: "run_evaluate_dep.py" in s,
                         stdout="", returncode=1)
    popen = _install_popen(monkeypatch, _FakeProc(""))
    r = _make_r(redis_stub)
    _run(jr, r)
    assert popen["cmd"] is not None
    assert any("dep1 script error" in m for m in _logs(r))


def test_run_job_evaluate_dep_failed_then_claude(worker_scripts, skills_env,
                                                 loaded_modules, monkeypatch,
                                                 redis_stub, fake_subprocess,
                                                 tmp_path):
    jr = _load(worker_scripts, skills_env, loaded_modules, monkeypatch,
               MAX_SCRIPT_FAILS="2", SESSIONS_BASE=str(tmp_path / "sessions"))
    _precreate_deps(tmp_path, {"dep1": {"status": "pending_evaluate",
                                        "url": "https://d1"}})
    _sup_rule(fake_subprocess, "ACTION=evaluate\nTARGET=dep1\n", n=2)
    fake_subprocess.when(lambda s: "run_evaluate_dep.py" in s,
                         stdout=json.dumps({"status": "failed",
                                            "reason": "no match"}))
    popen = _install_popen(monkeypatch, _FakeProc(""))
    r = _make_r(redis_stub)
    _run(jr, r)
    counts = json.loads((_session_dir(tmp_path) /
                         "script_fail_counts.json").read_text())
    assert counts == {"evaluate:dep1": 2}
    assert any("dep1 evaluate failed (2/2)" in m for m in _logs(r))
    assert popen["cmd"] is not None


@pytest.mark.parametrize("dep2_rule", [
    # needs_ai → 置满熔断阈值(默认 3)
    (lambda f: f.when(lambda s: "run_evaluate_dep.py" in s and "--pkg dep2" in s,
                      stdout=json.dumps({"status": "needs_ai"})), 3),
    # failed → +1
    (lambda f: f.when(lambda s: "run_evaluate_dep.py" in s and "--pkg dep2" in s,
                      stdout=json.dumps({"status": "failed", "reason": "x"})), 1),
    # 超时 → +1
    (lambda f: f.when(lambda s: "run_evaluate_dep.py" in s and "--pkg dep2" in s,
                      exc=subprocess.TimeoutExpired("x", 300)), 1),
])
def test_run_job_parallel_evaluate(jr, redis_stub, fake_subprocess, tmp_path,
                                   dep2_rule):
    _precreate_deps(tmp_path, {
        "dep1": {"status": "pending_evaluate", "url": "https://d1"},
        "dep2": {"status": "pending_evaluate", "url": "https://d2"},
    })
    _sup_rule(fake_subprocess, "ACTION=evaluate\nTARGET=dep1\n", n=1)
    fake_subprocess.when(
        lambda s: "run_evaluate_dep.py" in s and "--pkg dep1" in s,
        stdout=json.dumps({"status": "done", "lang": "rust"}))
    _, expected_count = dep2_rule
    dep2_rule[0](fake_subprocess)
    r = _make_r(redis_stub)
    _run(jr, r)

    reg = json.loads((_session_dir(tmp_path) / "dep_registry.json").read_text())
    assert reg["dep1"]["status"] == "evaluate_done"
    assert reg["dep1"]["lang"] == "rust"
    assert reg["dep2"]["status"] == "pending_evaluate"   # 非 done 不改注册表
    counts = json.loads((_session_dir(tmp_path) /
                         "script_fail_counts.json").read_text())
    assert counts == {"evaluate:dep2": expected_count}
    assert any("parallel done: 1 done, 1 not done" in m for m in _logs(r))


# ─────────────────────────────────────────────
# run_job — claude 子进程解析 + watchdog
# ─────────────────────────────────────────────

_CLAUDE_STREAM = "".join(json.dumps(e) + "\n" for e in [
    {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "hello from claude"},
        {"type": "tool_use", "name": "Bash",
         "input": {"command": "rpmbuild -bs demo.spec"}},
    ]}},
    {"type": "tool_result", "content": [
        {"type": "text", "text": "status=succeeded build_id=123\nERROR missing dep"},
        "plain-string",
    ]},
    {"type": "result", "result": "final line"},
    {"type": "system", "subtype": "init"},
    "not-json",
])


def test_run_job_claude_stream_parsed(jr, redis_stub, fake_subprocess, tmp_path,
                                      monkeypatch, capsys):
    _sup_rule(fake_subprocess,
              "ACTION=evaluate_main\nTARGET=mainpkg\n"
              "COPR_BUILD_CHROOTS=c1,c2\nCHROOT=c1\n", n=1)
    fake_subprocess.when(lambda s: "run_evaluate_dep.py" in s,
                         stdout=json.dumps({"status": "needs_ai"}))
    popen = _install_popen(monkeypatch,
                           _FakeProc(_CLAUDE_STREAM,
                                     stderr_text="claude debug output\n"))
    r = _make_r(redis_stub)
    _run(jr, r)

    logs = _logs(r)
    assert "hello from claude" in logs
    assert "status=succeeded build_id=123" in logs
    assert "ERROR missing dep" in logs
    assert "final line" in logs
    out = capsys.readouterr().out
    assert "[claude][job1] hello from claude" in out
    assert "[tool][job1] Bash" in out
    assert "[dbg][job1] claude debug output" in out
    assert "[claude][job1] exit=0" in out
    env = popen["kwargs"]["env"]
    assert env["COPR_BUILD_CHROOTS"] == "c1,c2"
    assert env["CHROOT"] == "c1"
    assert env["COPR_OWNER"] == "openeuler"
    assert env["COPR_PROJECT"] == "test-proj"
    ends = _events(_session_dir(tmp_path), "action.end")
    assert ends[-1]["data"]["exit_code"] == 0
    assert ends[-1]["data"]["action"] == "evaluate_main"


def test_run_job_watchdog_kills_on_cancel(jr, redis_stub, fake_subprocess,
                                          tmp_path, monkeypatch):
    _sup_rule(fake_subprocess, "ACTION=evaluate_main\nTARGET=mainpkg\n", n=1)
    fake_subprocess.when(lambda s: "run_evaluate_dep.py" in s,
                         stdout=json.dumps({"status": "needs_ai"}))
    proc = _FakeProc("", wait_terminate=True)
    _install_popen(monkeypatch, proc)
    base = _JobRedis(redis_stub)
    base.hset("job:ai:job1", mapping=_valid_job())

    class _AlwaysCancel(_JobRedis):
        def hget(self, key, field):
            if field == "status" and key == "job:ai:job1":
                return "cancelled"
            return super().hget(key, field)

    r = _AlwaysCancel(redis_stub)
    _run(jr, r)
    assert proc.terminated
    assert _job_status(r) == "cancelled"
