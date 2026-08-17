"""pkg-introduce-skills 测试共享基建。

约定(与 test_case_design_report.md §3 一致):
- load_module():importlib 加载器,统一处理连字符文件名(register-dep.py 等),
  与 evaluate-deps.py 的 _load_cascade 同款实现。
- sys.path 按需注入,不全局混入:build-rpm/ 与 pkg-introduce/ 存在重复模块名
  (rpm_batch_lookup / check_existing_package / cascade_package_check,pkg-introduce
  侧是 runpy 转发 wrapper),同时注入会歧义;worker/ 的 copr_client.py 与
  build-rpm/ 的也是两个独立实现。每个测试模块按需请求对应 fixture。
- redis_stub():纯内存假 redis,注入 sys.modules(worker.py / notify_job.py
  顶层 import redis,无 stub 无法加载)。
- fake_subprocess():可编程 subprocess.run mock,按命令前缀/谓词返回预设结果。
- tmp_session():标准 session 目录骨架(session.json / dep_registry.json /
  workflow_*.json / pkgs/)。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / ".claude" / "skills"

SCRIPT_DIRS = {
    "build_rpm": SKILLS_ROOT / "build-rpm" / "scripts",
    "step": SKILLS_ROOT / "import-package-step" / "scripts",
    "pkg_introduce": SKILLS_ROOT / "pkg-introduce" / "scripts",
    "archive": SKILLS_ROOT / "archive-rpm-sources" / "scripts",
    "review": SKILLS_ROOT / "review-rpm" / "scripts",
    "worker": REPO_ROOT / "docker" / "importer-worker",
}


def load_module(name: str, path: Path):
    """把任意 .py 文件(含连字符名)加载为模块并注册到 sys.modules。"""
    path = Path(path)
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot create spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def loaded_modules():
    """跟踪 load_module 创建的模块,测试结束后从 sys.modules 清除,避免
    import 时固化环境变量/常量的模块(worker.py 等)跨测试污染。"""

    created: list[str] = []

    def _load(name: str, path: Path):
        created.append(name)
        return load_module(name, path)

    yield _load
    for name in created:
        sys.modules.pop(name, None)


def _prepend(monkeypatch, *dirs):
    for d in dirs:  # 逐个插到最前,最后注入的优先级最高
        monkeypatch.syspath_prepend(str(d))


@pytest.fixture
def build_rpm_scripts(monkeypatch):
    """把 build-rpm/scripts 注入 sys.path(真身实现所在目录)。"""
    _prepend(monkeypatch, SCRIPT_DIRS["build_rpm"])
    return SCRIPT_DIRS["build_rpm"]


@pytest.fixture
def step_scripts(monkeypatch):
    """把 import-package-step/scripts + build-rpm/scripts 注入 sys.path。
    step 侧脚本顶层依赖 build-rpm/scripts(cascade_package_check 等),
    两目录间无重复模块名。"""
    _prepend(monkeypatch, SCRIPT_DIRS["step"], SCRIPT_DIRS["build_rpm"])
    return SCRIPT_DIRS["step"]


@pytest.fixture
def pkg_introduce_scripts(monkeypatch):
    """把 pkg-introduce/scripts 注入 sys.path。"""
    _prepend(monkeypatch, SCRIPT_DIRS["pkg_introduce"])
    return SCRIPT_DIRS["pkg_introduce"]


@pytest.fixture
def archive_scripts(monkeypatch):
    _prepend(monkeypatch, SCRIPT_DIRS["archive"])
    return SCRIPT_DIRS["archive"]


@pytest.fixture
def worker_scripts(monkeypatch):
    """把 docker/importer-worker 注入 sys.path。job_runner 还需 SKILLS_DIR
    环境变量指向 skills 根,由 skills_env fixture 提供。"""
    _prepend(monkeypatch, SCRIPT_DIRS["worker"])
    return SCRIPT_DIRS["worker"]


@pytest.fixture
def skills_env(monkeypatch):
    """SKILLS_DIR 指向仓库内 skills 根(job_runner 加载 timeline 需要)。"""
    monkeypatch.setenv("SKILLS_DIR", str(SKILLS_ROOT))
    return SKILLS_ROOT


# ─────────────────────────────────────────────
# redis stub
# ─────────────────────────────────────────────

class RedisError(Exception):
    """与 redis.RedisError 对齐的异常基类。"""


class _FakeRedis:
    """纯内存假 redis,覆盖 worker.py / notify_job.py 用到的 API 子集:
    smembers/sadd/srem、lpop/lpush/rpush/llen、hset/hgetall/hget、
    get/set(nx/ex)/delete、blpop。"""

    def __init__(self, **kwargs):
        self._sets: dict[str, set] = {}
        self._lists: dict[str, list] = {}
        self._hashes: dict[str, dict] = {}
        self._strings: dict[str, str] = {}

    # sets
    def smembers(self, key):
        return set(self._sets.get(key, set()))

    def sadd(self, key, *values):
        s = self._sets.setdefault(key, set())
        s.update(values)
        return len(s)

    def srem(self, key, *values):
        s = self._sets.get(key, set())
        removed = sum(1 for v in values if v in s)
        s.difference_update(values)
        return removed

    # lists
    def lpop(self, key):
        lst = self._lists.get(key)
        return lst.pop(0) if lst else None

    def blpop(self, keys, timeout=0):
        for key in keys if isinstance(keys, (list, tuple)) else [keys]:
            val = self.lpop(key)
            if val is not None:
                return (key, val)
        return None

    def rpush(self, key, *values):
        lst = self._lists.setdefault(key, [])
        lst.extend(values)
        return len(lst)

    def lpush(self, key, *values):
        lst = self._lists.setdefault(key, [])
        lst[0:0] = list(values)
        return len(lst)

    def llen(self, key):
        return len(self._lists.get(key, []))

    # hashes(兼容 dict 映射与旧式 hset(key, field, value) 三参数)
    def hset(self, key, mapping=None, *args, **kwargs):
        h = self._hashes.setdefault(key, {})
        if isinstance(mapping, dict):
            h.update(mapping)
        elif args:
            h[mapping] = args[0]
        elif mapping is not None:
            h[mapping] = kwargs.pop("value", "")
        if kwargs:
            h.update(kwargs)
        return len(h)

    def hgetall(self, key):
        return dict(self._hashes.get(key, {}))

    def hget(self, key, field):
        return self._hashes.get(key, {}).get(field)

    # strings
    def get(self, key):
        return self._strings.get(key)

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self._strings:
            return None
        self._strings[key] = value
        return True

    def delete(self, *keys):
        n = 0
        for key in keys:
            for store in (self._sets, self._lists, self._hashes, self._strings):
                if store.pop(key, None) is not None:
                    n += 1
        return n


@pytest.fixture
def redis_stub(monkeypatch):
    """向 sys.modules 注入假 redis 模块。"""
    fake = _FakeRedis()
    module = type(sys)("redis")
    module.Redis = lambda *a, **kw: fake
    module.RedisError = RedisError
    original = sys.modules.get("redis")
    sys.modules["redis"] = module
    yield fake
    if original is not None:
        sys.modules["redis"] = original
    else:
        sys.modules.pop("redis", None)


# ─────────────────────────────────────────────
# fake subprocess
# ─────────────────────────────────────────────

class _FakeSubprocess:
    """可编程 subprocess.run mock。

    用法:
        fake = fake_subprocess
        fake.when("rpm -q", stdout="foo-1.0-1.x86_64")        # 按命令前缀匹配
        fake.when(lambda s: "git" in s, returncode=1, stderr="boom")
        fake.when("missing-cmd", exc=FileNotFoundError())
        fake.called_with("rpm -q")                            # 断言调用过

    规则按注册顺序取第一个匹配;无匹配时返回 rc=0 空输出。
    stdout/stderr 传入 str 时,调用方若以 text 模式执行则保持 str,
    否则编码为 bytes(subprocess.CompletedProcess 的真实语义)。
    """

    def __init__(self):
        self._rules = []
        self.calls: list[tuple] = []

    def when(self, predicate, stdout="", stderr="", returncode=0, exc=None):
        if isinstance(predicate, str):
            prefix = predicate
            predicate = lambda cmd: cmd.startswith(prefix)  # noqa: E731
        self._rules.append(
            (predicate, {"stdout": stdout, "stderr": stderr,
                         "returncode": returncode, "exc": exc})
        )

    def run(self, cmd, **kwargs):
        cmd_str = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        self.calls.append((cmd, kwargs))
        for pred, res in self._rules:
            if not pred(cmd_str):
                continue
            if res["exc"] is not None:
                raise res["exc"]
            return self._result(cmd, res, kwargs)
        return self._result(cmd, None, kwargs)

    def _result(self, cmd, res, kwargs):
        if res is None:
            res = {"stdout": "", "stderr": "", "returncode": 0}
        capture = kwargs.get("capture_output", False) or "stdout" in kwargs or "stderr" in kwargs
        if not capture:
            return subprocess.CompletedProcess(cmd, res["returncode"], None, None)
        stdout, stderr = res["stdout"], res["stderr"]
        text = kwargs.get("text") or kwargs.get("universal_newlines")
        if not text:
            if isinstance(stdout, str):
                stdout = stdout.encode()
            if isinstance(stderr, str):
                stderr = stderr.encode()
        return subprocess.CompletedProcess(cmd, res["returncode"], stdout, stderr)

    def called_with(self, cmd_str: str) -> bool:
        """是否调用过包含 cmd_str 的命令。"""
        for cmd, _ in self.calls:
            if isinstance(cmd, str):
                joined = cmd  # shell=True 字符串命令,原样匹配
            else:
                joined = " ".join(str(c) for c in cmd)
            if cmd_str in joined:
                return True
        return False


@pytest.fixture
def fake_subprocess(monkeypatch):
    """把 subprocess.run 替换为可编程 fake,返回 _FakeSubprocess 实例。"""
    fake = _FakeSubprocess()
    monkeypatch.setattr(subprocess, "run", fake.run)
    return fake


# ─────────────────────────────────────────────
# session 目录骨架
# ─────────────────────────────────────────────

@pytest.fixture
def tmp_session(tmp_path):
    """标准 session 目录骨架:
    session.json(COPR 凭据 + pkgs + copr_chroots)、dep_registry.json、
    workflow_<pkg>.json、pkgs/ 目录。测试按需覆写具体字段。"""
    sd = tmp_path / "session"
    sd.mkdir()
    (sd / "pkgs").mkdir()
    (sd / "session.json").write_text(json.dumps({
        "copr_url": "http://copr-frontend:5000",
        "copr_login": "test-user",
        "copr_token": "test-token",
        "pkgs": ["testpkg"],
        "copr_chroots": ["openeuler-24.03-x86_64"],
    }, ensure_ascii=False))
    (sd / "dep_registry.json").write_text(json.dumps({"deps": {}}))
    (sd / "workflow_testpkg.json").write_text(json.dumps({}))
    return sd
