"""worker.py — pick_next_job 公平调度 / CancelHandler / main 编排(不真正轮询)。

worker.py 顶层 import redis 且把环境变量固化为模块常量,所以每个测试都通过
_load_worker() 在 redis_stub + 目标 env 下重新加载模块。
"""

from __future__ import annotations

import http.server
import io
import json
import runpy
import sys

import pytest

from tests.conftest import SCRIPT_DIRS


def _load_worker(worker_scripts, redis_stub, loaded_modules, monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return loaded_modules("worker", SCRIPT_DIRS["worker"] / "worker.py")


class _StopMain(BaseException):
    """time.sleep 被替换为抛该异常,用于切断 main() 的无限循环。

    继承 BaseException 而非 Exception:main() 的 except Exception 兜底不会吞掉
    哨兵,保证循环在第一次命中时即退出。"""


def _seed_queue(w, stub, projects, queues):
    for p in projects:
        stub.sadd(w.ACTIVE_SET, p)
    for p, jobs in queues.items():
        for j in jobs:
            stub.rpush(f"{w.QUEUE_PREFIX}{p}", j)


def _quiet_cancel_server(w, monkeypatch):
    class _FakeHTTPServer:
        def __init__(self, addr, handler_cls):
            pass

        def serve_forever(self):
            pass

    monkeypatch.setattr(w, "HTTPServer", _FakeHTTPServer)


def _stop_on_sleep(w, monkeypatch, record=None):
    def sleep(secs):
        if record is not None:
            record.append(secs)
        raise _StopMain()

    monkeypatch.setattr(w.time, "sleep", sleep)


def _fake_job_runner(monkeypatch, calls, exc=None):
    mod = type(sys)("job_runner")

    def run_job(r, proj, job_id):
        calls.append((r, proj, job_id))
        if exc is not None:
            raise exc

    mod.run_job = run_job
    monkeypatch.setitem(sys.modules, "job_runner", mod)


def _capture_cancel_server(w, monkeypatch):
    """替换 HTTPServer 捕获 (addr, handler_cls),serve_forever 直接返回。"""
    captured = {}

    class _FakeHTTPServer:
        def __init__(self, addr, handler_cls):
            captured["addr"] = addr
            captured["handler_cls"] = handler_cls

        def serve_forever(self):
            pass

    monkeypatch.setattr(w, "HTTPServer", _FakeHTTPServer)
    w.start_cancel_server()
    return captured


def _do_post(handler_cls, path):
    """绕过 socket 直接驱动 do_POST。"""
    h = handler_cls.__new__(handler_cls)
    h.path = path
    h.wfile = io.BytesIO()
    codes = []
    h.send_response = lambda code: codes.append(code)
    h.end_headers = lambda: None
    h.do_POST()
    h.log_message("GET /")  # 顺带覆盖 log_message(no-op)
    return codes, h.wfile.getvalue()


# ─────────────────────────────────────────────
# 环境变量常量(默认值 + 覆盖)
# ─────────────────────────────────────────────

@pytest.mark.parametrize("env,attr,expected", [
    ({}, "REDIS_HOST", "redis"),
    ({}, "REDIS_PORT", 6379),
    ({}, "REDIS_PASSWORD", ""),
    ({}, "CANCEL_PORT", 8080),
    ({"REDIS_HOST": "rh.example"}, "REDIS_HOST", "rh.example"),
    ({"REDIS_PORT": "6380"}, "REDIS_PORT", 6380),
    ({"CANCEL_PORT": "9090"}, "CANCEL_PORT", 9090),
])
def test_env_constants(worker_scripts, redis_stub, loaded_modules, monkeypatch,
                       env, attr, expected):
    for k in ("REDIS_HOST", "REDIS_PORT", "REDIS_PASSWORD", "CANCEL_PORT"):
        monkeypatch.delenv(k, raising=False)
    w = _load_worker(worker_scripts, redis_stub, loaded_modules, monkeypatch, **env)
    assert getattr(w, attr) == expected


def test_static_constants(worker_scripts, redis_stub, loaded_modules, monkeypatch):
    w = _load_worker(worker_scripts, redis_stub, loaded_modules, monkeypatch)
    assert w.QUEUE_PREFIX == "queue:ai:"
    assert w.ACTIVE_SET == "queues:ai:active"
    assert w.LOCK_PREFIX == "lock:ai:"
    assert w.JOB_PREFIX == "job:ai:"
    assert w.LOGS_PREFIX == "logs:ai:"
    assert w.LOCK_TTL == 7200
    assert w.LOCK_TTL_ROS == 21600


# ─────────────────────────────────────────────
# make_redis
# ─────────────────────────────────────────────

def test_make_redis_passes_env(worker_scripts, redis_stub, loaded_modules, monkeypatch):
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)
    w = _load_worker(worker_scripts, redis_stub, loaded_modules, monkeypatch,
                     REDIS_HOST="h1", REDIS_PORT="6390")
    redis_mod = sys.modules["redis"]
    calls = []
    redis_mod.Redis = lambda *a, **kw: (calls.append((a, kw)), redis_stub)[1]
    assert w.make_redis() is redis_stub
    assert calls == [((), {"host": "h1", "port": 6390,
                           "password": None, "decode_responses": True})]


def test_make_redis_with_password(worker_scripts, redis_stub, loaded_modules, monkeypatch):
    w = _load_worker(worker_scripts, redis_stub, loaded_modules, monkeypatch,
                     REDIS_PASSWORD="secret")
    redis_mod = sys.modules["redis"]
    calls = []
    redis_mod.Redis = lambda *a, **kw: (calls.append((a, kw)), redis_stub)[1]
    w.make_redis()
    assert calls[0][1]["password"] == "secret"


# ─────────────────────────────────────────────
# pick_next_job
# ─────────────────────────────────────────────

@pytest.mark.parametrize("projects,queues", [
    (["pA"], {"pA": ["j1"]}),
    (["pA"], {"pA": ["j1", "j2"]}),
    (["pA", "pB"], {"pA": ["a1", "a2"], "pB": ["b1"]}),
    (["pA", "pB", "pC"], {"pA": ["a1"], "pB": ["b1"], "pC": ["c1"]}),
])
def test_pick_next_job_drains_all_jobs(worker_scripts, redis_stub, loaded_modules,
                                       monkeypatch, projects, queues):
    w = _load_worker(worker_scripts, redis_stub, loaded_modules, monkeypatch)
    _seed_queue(w, redis_stub, projects, queues)
    expected = {(p, j) for p, jobs in queues.items() for j in jobs}
    got = set()
    # 每次调用至多弹一个 job;多一次调用确认队列空后返回 (None, None)
    for _ in range(sum(len(v) for v in queues.values()) + 1):
        proj, job = w.pick_next_job(redis_stub)
        if job:
            got.add((proj, job))
    assert got == expected
    assert redis_stub.smembers(w.ACTIVE_SET) == set()


@pytest.mark.parametrize("projects,queues", [
    ([], {}),
    (["pA"], {}),
    (["pA", "pB"], {}),
    (["pA"], {"pA": []}),
])
def test_pick_next_job_empty_returns_none(worker_scripts, redis_stub, loaded_modules,
                                          monkeypatch, projects, queues):
    w = _load_worker(worker_scripts, redis_stub, loaded_modules, monkeypatch)
    _seed_queue(w, redis_stub, projects, queues)
    assert w.pick_next_job(redis_stub) == (None, None)
    if projects:
        # 实际行为:队列已空的项目不会被本轮移出 ACTIVE_SET(靠下次 push 重新激活)
        assert redis_stub.smembers(w.ACTIVE_SET) == set(projects)


def test_pick_next_job_fair_share_counts(worker_scripts, redis_stub, loaded_modules,
                                         monkeypatch):
    """3 项目各 5 个 job,共 15 次调用:每个项目恰好被弹 5 次(随机 shuffle
    不影响该不变量——队列弹空即移出 ACTIVE_SET,总量守恒)。"""
    w = _load_worker(worker_scripts, redis_stub, loaded_modules, monkeypatch)
    n = 5
    projects = ["p1", "p2", "p3"]
    queues = {p: [f"{p}-{i}" for i in range(n)] for p in projects}
    _seed_queue(w, redis_stub, projects, queues)
    pops = {p: 0 for p in projects}
    for _ in range(len(projects) * n):
        proj, job = w.pick_next_job(redis_stub)
        assert job is not None
        pops[proj] += 1
    assert pops == {"p1": n, "p2": n, "p3": n}
    assert redis_stub.smembers(w.ACTIVE_SET) == set()


def test_pick_next_job_keeps_active_until_queue_empty(worker_scripts, redis_stub,
                                                      loaded_modules, monkeypatch):
    w = _load_worker(worker_scripts, redis_stub, loaded_modules, monkeypatch)
    _seed_queue(w, redis_stub, ["pA"], {"pA": ["j1", "j2"]})
    assert w.pick_next_job(redis_stub) == ("pA", "j1")
    assert redis_stub.smembers(w.ACTIVE_SET) == {"pA"}  # 队列还有 j2,保持 active
    assert w.pick_next_job(redis_stub) == ("pA", "j2")
    assert redis_stub.smembers(w.ACTIVE_SET) == set()


# ─────────────────────────────────────────────
# CancelHandler(经 start_cancel_server 捕获)
# ─────────────────────────────────────────────

@pytest.mark.parametrize("path,exp_code,exp_body", [
    ("/cancel/j1", 200, b'{"status": "cancelled"}'),
    ("/cancel/missing", 404, b'{"error": "job not found"}'),
    ("/cancel/", 404, b'{"error": "job not found"}'),  # 空 job_id 边界
    ("/health", 404, b""),
])
def test_cancel_handler_routes(worker_scripts, redis_stub, loaded_modules, monkeypatch,
                               path, exp_code, exp_body):
    w = _load_worker(worker_scripts, redis_stub, loaded_modules, monkeypatch)
    redis_stub.hset("job:ai:j1", mapping={"status": "queued"})
    captured = _capture_cancel_server(w, monkeypatch)
    codes, body = _do_post(captured["handler_cls"], path)
    assert codes == [exp_code]
    assert body == exp_body
    j1 = redis_stub.hgetall("job:ai:j1")
    if path == "/cancel/j1":
        assert j1["status"] == "failed"
        assert j1["error"] == "cancelled by user"
        assert json.loads(redis_stub.lpop("logs:ai:j1")) == {
            "msg": "Job cancelled by user", "done": True, "status": "failed"}
    else:
        assert j1["status"] == "queued"
        assert "error" not in j1


@pytest.mark.parametrize("env_port,exp_port", [
    (None, 8080),
    ("9090", 9090),
    ("0", 0),
])
def test_cancel_server_binds_port(worker_scripts, redis_stub, loaded_modules,
                                  monkeypatch, env_port, exp_port):
    monkeypatch.delenv("CANCEL_PORT", raising=False)
    env = {"CANCEL_PORT": env_port} if env_port else {}
    w = _load_worker(worker_scripts, redis_stub, loaded_modules, monkeypatch, **env)
    captured = _capture_cancel_server(w, monkeypatch)
    assert captured["addr"] == ("0.0.0.0", exp_port)


# ─────────────────────────────────────────────
# main():编排(校验 → 锁 → run_job → 清理/异常处理)
# ─────────────────────────────────────────────

@pytest.mark.parametrize("mode,exp_ttl", [
    ("ros", 21600),     # ROS 批量任务锁 TTL 延长
    ("normal", 7200),
    (None, 7200),       # 无 mode 字段 → 普通锁
])
def test_main_lock_ttl_by_mode(worker_scripts, redis_stub, loaded_modules,
                               monkeypatch, mode, exp_ttl):
    w = _load_worker(worker_scripts, redis_stub, loaded_modules, monkeypatch)
    stub = redis_stub
    _seed_queue(w, stub, ["pA"], {"pA": ["j1"]})
    job_meta = {"status": "queued"}
    if mode is not None:
        job_meta["mode"] = mode
    stub.hset("job:ai:j1", mapping=job_meta)

    set_kwargs = []
    orig_set = stub.set

    def record_set(*a, **kw):
        set_kwargs.append(kw)
        return orig_set(*a, **kw)

    stub.set = record_set
    calls = []
    _fake_job_runner(monkeypatch, calls)
    _quiet_cancel_server(w, monkeypatch)
    _stop_on_sleep(w, monkeypatch)
    with pytest.raises(_StopMain):
        w.main()
    assert set_kwargs == [{"nx": True, "ex": exp_ttl}]
    assert calls == [(stub, "pA", "j1")]
    assert stub.get("lock:ai:j1") is None  # finally 释放锁


def test_main_skips_cancelled_job(worker_scripts, redis_stub, loaded_modules,
                                  monkeypatch):
    w = _load_worker(worker_scripts, redis_stub, loaded_modules, monkeypatch)
    stub = redis_stub
    _seed_queue(w, stub, ["pA"], {"pA": ["j1"]})
    stub.hset("job:ai:j1", mapping={"status": "cancelled"})
    calls = []
    _fake_job_runner(monkeypatch, calls)
    _quiet_cancel_server(w, monkeypatch)
    _stop_on_sleep(w, monkeypatch)
    with pytest.raises(_StopMain):
        w.main()
    assert calls == []
    assert stub.get("lock:ai:j1") is None  # 未加锁
    assert stub.llen("queue:ai:pA") == 0   # 任务已从队列弹出


def test_main_lock_conflict_skips(worker_scripts, redis_stub, loaded_modules,
                                  monkeypatch):
    w = _load_worker(worker_scripts, redis_stub, loaded_modules, monkeypatch)
    stub = redis_stub
    _seed_queue(w, stub, ["pA"], {"pA": ["j1"]})
    stub.hset("job:ai:j1", mapping={"status": "queued"})
    stub.set("lock:ai:j1", "1")  # 预置锁
    calls = []
    _fake_job_runner(monkeypatch, calls)
    _quiet_cancel_server(w, monkeypatch)
    _stop_on_sleep(w, monkeypatch)
    with pytest.raises(_StopMain):
        w.main()
    assert calls == []
    assert stub.get("lock:ai:j1") == "1"  # 锁未被触碰


def test_main_job_crash_marks_failed(worker_scripts, redis_stub, loaded_modules,
                                     monkeypatch):
    w = _load_worker(worker_scripts, redis_stub, loaded_modules, monkeypatch)
    stub = redis_stub
    _seed_queue(w, stub, ["pA"], {"pA": ["j1"]})
    stub.hset("job:ai:j1", mapping={"status": "queued"})
    calls = []
    _fake_job_runner(monkeypatch, calls, exc=RuntimeError("boom"))
    _quiet_cancel_server(w, monkeypatch)
    _stop_on_sleep(w, monkeypatch)
    with pytest.raises(_StopMain):
        w.main()
    assert calls == [(stub, "pA", "j1")]
    assert stub.hget("job:ai:j1", "status") == "failed"
    assert json.loads(stub.lpop("logs:ai:j1")) == {
        "msg": "Worker internal error", "done": True, "status": "failed"}
    assert stub.get("lock:ai:j1") is None  # 崩溃也释放锁


def test_main_redis_error_reconnects_and_sleeps_5(worker_scripts, redis_stub,
                                                  loaded_modules, monkeypatch):
    w = _load_worker(worker_scripts, redis_stub, loaded_modules, monkeypatch)
    redis_mod = sys.modules["redis"]

    def boom(*a, **kw):
        raise redis_mod.RedisError("redis down")

    monkeypatch.setattr(redis_stub, "smembers", boom)
    _quiet_cancel_server(w, monkeypatch)
    sleeps = []

    def sleep(secs):
        sleeps.append(secs)
        if len(sleeps) >= 2:
            raise _StopMain()

    monkeypatch.setattr(w.time, "sleep", sleep)
    with pytest.raises(_StopMain):
        w.main()
    # 第一次故障:退避 5s 后重建连接(r = make_redis());第二次故障时哨兵切断循环
    assert sleeps == [5, 5]


def test_main_unexpected_error_sleeps_1(worker_scripts, redis_stub, loaded_modules,
                                        monkeypatch):
    w = _load_worker(worker_scripts, redis_stub, loaded_modules, monkeypatch)

    def bad_pick(r):
        raise ValueError("boom")

    monkeypatch.setattr(w, "pick_next_job", bad_pick)
    _quiet_cancel_server(w, monkeypatch)
    sleeps = []
    _stop_on_sleep(w, monkeypatch, sleeps)
    with pytest.raises(_StopMain):
        w.main()
    assert sleeps == [1]  # 未知异常退避 1s


def test_main_idle_loop_sleeps_between_polls(worker_scripts, redis_stub,
                                             loaded_modules, monkeypatch):
    w = _load_worker(worker_scripts, redis_stub, loaded_modules, monkeypatch)
    _quiet_cancel_server(w, monkeypatch)
    sleeps = []

    def sleep(secs):
        sleeps.append(secs)
        if len(sleeps) >= 2:
            raise _StopMain()

    monkeypatch.setattr(w.time, "sleep", sleep)
    with pytest.raises(_StopMain):
        w.main()
    # _StopMain 继承 BaseException,不会被 main 的 except Exception 吞掉:
    # 第一次 sleep 正常返回后 continue 进入下一轮,第二次 sleep 抛出即传播
    assert sleeps == [1, 1]  # 空轮询间隔 1s


def test_main_starts_daemon_cancel_server(worker_scripts, redis_stub, loaded_modules,
                                          monkeypatch):
    w = _load_worker(worker_scripts, redis_stub, loaded_modules, monkeypatch)
    threads = []

    class FakeThread:
        def __init__(self, target, daemon=False):
            threads.append((target, daemon))

        def start(self):
            pass

    monkeypatch.setattr(w.threading, "Thread", FakeThread)
    _stop_on_sleep(w, monkeypatch)
    with pytest.raises(_StopMain):
        w.main()
    assert len(threads) == 1
    target, daemon = threads[0]
    assert daemon is True
    assert target == w.start_cancel_server


def test_module_main_guard(worker_scripts, redis_stub, loaded_modules, monkeypatch):
    """以 __main__ 方式执行 worker.py,覆盖 __main__ 守卫。

    注意:run_path 使用全新命名空间,HTTPServer 需补丁到 http.server 模块
    (而非已加载的 worker 模块)才能生效。"""
    w = _load_worker(worker_scripts, redis_stub, loaded_modules, monkeypatch)

    class FakeHTTPServer:
        def __init__(self, addr, handler_cls):
            pass

        def serve_forever(self):
            pass

    monkeypatch.setattr(http.server, "HTTPServer", FakeHTTPServer)
    _stop_on_sleep(w, monkeypatch)
    with pytest.raises(_StopMain):
        runpy.run_path(str(SCRIPT_DIRS["worker"] / "worker.py"), run_name="__main__")
