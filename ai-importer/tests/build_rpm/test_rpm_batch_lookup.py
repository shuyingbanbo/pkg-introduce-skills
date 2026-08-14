"""rpm_batch_lookup.py — 共享 RPM 批量查询(chroot 映射/任务构造/缓存/子进程)。

子进程侧(dnf 容器脚本)用 fake_subprocess 模拟;
宿主机缓存读写重定向到 tmp_path(SESSION_TMP_DIR 在 import 时固化)。
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sys.path.insert(0, str(SCRIPT_DIRS["build_rpm"]))
mod = load_module("rpm_batch_lookup", SCRIPT_DIRS["build_rpm"] / "rpm_batch_lookup.py")


# ─────────────────────────────────────────────
# chroot → repofrompath
# ─────────────────────────────────────────────

@pytest.mark.parametrize("chroot,expected_base", [
    ("openeuler-22.03_LTS_SP2-x86_64", "http://repo.openeuler.org/openEuler-22.03-LTS-SP2"),
    ("openeuler-22.03_LTS-aarch64", "http://repo.openeuler.org/openEuler-22.03-LTS"),
    ("openeuler-22.03_LTS_SP4-x86_64", "http://repo.openeuler.org/openEuler-22.03-LTS-SP4"),
    ("openeuler-24.03_LTS-x86_64", "http://repo.openeuler.org/openEuler-24.03-LTS"),
    ("openeuler-24.03_LTS_SP3-x86_64", "http://repo.openeuler.org/openEuler-24.03-LTS-SP3"),
])
def test_chroot_to_repofrompath_known(chroot, expected_base):
    result = mod.chroot_to_repofrompath(chroot)
    assert len(result) == 3
    # (repo_id, url) 且 id 带 chroot 后缀(避免跨 chroot 缓存误判)
    for repo_id, url in result:
        assert chroot in repo_id
    assert result[0][1].startswith(expected_base + "/everything/")
    assert result[1][1].startswith(expected_base + "/update/")
    assert result[2][1].startswith(expected_base + "/EPOL/main/")


def test_chroot_to_repofrompath_aarch64():
    result = mod.chroot_to_repofrompath("openeuler-24.03_LTS-aarch64")
    assert result[0][1].endswith("/everything/aarch64/")


def test_chroot_to_repofrompath_unknown():
    assert mod.chroot_to_repofrompath("fedora-39-x86_64") == []
    assert mod.chroot_to_repofrompath("") == []
    assert mod.chroot_to_repofrompath("openeuler") == []


# ─────────────────────────────────────────────
# 查询构造器
# ─────────────────────────────────────────────

def test_provides_query():
    assert mod.provides_query("python3dist(x)", "python3dist()") == {
        "kind": "provides", "value": "python3dist(x)", "level": "python3dist()"}


def test_name_query():
    assert mod.name_query("foo") == {"kind": "name", "value": "foo",
                                     "level": "name", "prefer_devel": False}
    assert mod.name_query("foo", level="x", prefer_devel=True) == {
        "kind": "name", "value": "foo", "level": "x", "prefer_devel": True}


def test_name_glob_query():
    assert mod.name_glob_query("lib*") == {"kind": "name_glob", "value": "lib*",
                                           "level": "name-glob", "prefer_devel": False}


def test_file_query():
    assert mod.file_query("/usr/lib/libssl.so") == {
        "kind": "file", "value": "/usr/lib/libssl.so",
        "level": "file", "prefer_devel": False}


def test_file_glob_query():
    assert mod.file_glob_query("*/libssl.so*", prefer_devel=True) == {
        "kind": "file_glob", "value": "*/libssl.so*",
        "level": "file-glob", "prefer_devel": True}


# ─────────────────────────────────────────────
# fallback / task key
# ─────────────────────────────────────────────

def test_fallback_results_strips_internal_keys():
    tasks = [{
        "dep": "ssl", "type": "link", "requirement": "",
        "queries": [mod.provides_query("pkgconfig(ssl)", "pkgconfig()")],
        "prefer_devel": True, "enabled_repos": ["r"], "repofrompath": [["a", "b"]],
    }]
    results = mod.fallback_results(tasks)
    assert len(results) == 1
    assert results[0] == {"dep": "ssl", "type": "link", "requirement": "",
                          "rpm": None, "version": None, "release": None, "level": ""}


def test_fallback_results_empty():
    assert mod.fallback_results([]) == []


def test_task_key_deterministic_and_stable():
    task = {"dep": "ssl", "queries": [{"kind": "name", "value": "ssl"}]}
    key1 = mod._task_key(task, ["a", "b"])
    key2 = mod._task_key(task, ["b", "a"])      # 顺序无关
    assert key1 == key2
    assert len(key1) == 32                       # md5 hex
    assert mod._task_key(task, None) == mod._task_key(task, [])


def test_task_key_depends_on_payload_and_repos():
    base = {"dep": "ssl", "queries": [{"kind": "name", "value": "ssl"}]}
    assert mod._task_key(base, []) != mod._task_key({**base, "dep": "zlib"}, [])
    assert mod._task_key(base, []) != mod._task_key(base, ["repo-x"])
    # internal keys(queries/prefer_devel/...)不参与 key
    other = {**base, "queries": [{"kind": "name", "value": "other"}],
             "prefer_devel": True, "enabled_repos": ["r"], "repofrompath": [["a", "b"]]}
    assert mod._task_key(other, []) == mod._task_key({**base, "queries": [
        {"kind": "name", "value": "other"}]}, [])


# ─────────────────────────────────────────────
# 宿主机缓存
# ─────────────────────────────────────────────

@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "rpm_lookup_cache"
    monkeypatch.setattr(mod, "_CACHE_DIR", d)
    monkeypatch.setattr(mod, "_CACHE_FILE", d / "batch_lookup_cache.json")
    return d


def test_load_cache_empty(cache_dir):
    assert mod._load_cache() == {}


def test_cache_roundtrip(cache_dir):
    mod._save_cache({"k1": {"rpm": "foo"}})
    assert mod._load_cache() == {"k1": {"rpm": "foo"}}


def test_load_cache_corrupt(cache_dir):
    cache_dir.mkdir()
    (cache_dir / "batch_lookup_cache.json").write_text("{not json")
    assert mod._load_cache() == {}


def test_save_cache_creates_dir(cache_dir):
    assert not cache_dir.exists()
    mod._save_cache({"a": 1})
    assert cache_dir.exists()
    assert mod._load_cache() == {"a": 1}


# ─────────────────────────────────────────────
# run_batch_lookup
# ─────────────────────────────────────────────

def _task(dep="ssl"):
    return {"dep": dep, "queries": [mod.name_query(dep)]}


def test_run_batch_lookup_empty_tasks():
    assert mod.run_batch_lookup([]) == []


def test_run_batch_lookup_cache_hit(cache_dir, fake_subprocess):
    task = _task()
    key = mod._task_key(task, [])
    mod._save_cache({key: {"dep": "ssl", "rpm": "openssl-devel", "version": "1.1",
                           "release": "1", "level": "name"}})
    results = mod.run_batch_lookup([task])
    assert results[0]["rpm"] == "openssl-devel"
    assert results[0]["version"] == "1.1"
    assert fake_subprocess.calls == []      # 命中缓存不触发子进程


def test_run_batch_lookup_miss_hits_subprocess(cache_dir, fake_subprocess):
    task = _task()
    fake_subprocess.when(
        "python3 -c",
        stdout=json.dumps([{"dep": "ssl", "rpm": "openssl-devel", "version": "3.0.9",
                            "release": "1", "level": "name"}]),
    )
    results = mod.run_batch_lookup([task])
    assert results[0]["rpm"] == "openssl-devel"
    assert results[0]["release"] == "1"
    # 结果写回缓存
    key = mod._task_key(task, [])
    assert mod._load_cache()[key]["rpm"] == "openssl-devel"
    # 子进程收到 JSON payload
    _, kwargs = fake_subprocess.calls[0]
    assert isinstance(kwargs["input"], str)
    payload = json.loads(kwargs["input"])
    assert payload[0]["dep"] == "ssl"
    assert "queries" in payload[0]


def test_run_batch_lookup_returncode_error(cache_dir, fake_subprocess):
    fake_subprocess.when("python3 -c", returncode=1, stderr="dnf crashed hard")
    with pytest.raises(mod.BatchLookupError) as exc:
        mod.run_batch_lookup([_task()])
    assert "dnf crashed hard" in str(exc.value)


def test_run_batch_lookup_empty_stdout(cache_dir, fake_subprocess):
    fake_subprocess.when("python3 -c", stdout="", stderr="")
    with pytest.raises(mod.BatchLookupError):
        mod.run_batch_lookup([_task()])


def test_run_batch_lookup_bad_json(cache_dir, fake_subprocess):
    fake_subprocess.when("python3 -c", stdout="not-json")
    with pytest.raises(mod.BatchLookupError) as exc:
        mod.run_batch_lookup([_task()])
    assert "JSON parse error" in str(exc.value)


def test_run_batch_lookup_timeout_propagates(cache_dir, fake_subprocess):
    fake_subprocess.when("python3 -c", exc=subprocess.TimeoutExpired("python3", 300))
    with pytest.raises(subprocess.TimeoutExpired):
        mod.run_batch_lookup([_task()])


def test_run_batch_lookup_chroot_adds_repofrompath(cache_dir, fake_subprocess):
    fake_subprocess.when("python3 -c",
                         stdout=json.dumps([{"dep": "ssl", "rpm": None, "version": None,
                                             "release": None, "level": ""}]))
    mod.run_batch_lookup([_task()], chroot="openeuler-24.03_LTS_SP3-x86_64")
    _, kwargs = fake_subprocess.calls[0]
    payload = json.loads(kwargs["input"])
    assert payload[0]["repofrompath"][0][0].endswith("openeuler-24.03_LTS_SP3-x86_64")
    assert payload[0]["repofrompath"][0][1].endswith("/everything/x86_64/")


def test_run_batch_lookup_chroot_cache_key(cache_dir, fake_subprocess):
    fake_subprocess.when("python3 -c",
                         stdout=json.dumps([{"dep": "ssl", "rpm": "x", "version": None,
                                             "release": None, "level": "name"}]))
    chroot = "openeuler-24.03_LTS_SP3-x86_64"
    mod.run_batch_lookup([_task()], chroot=chroot)
    cache = mod._load_cache()
    # chroot 参与缓存键,与无 chroot 查询不共用
    assert list(cache) == [mod._task_key(_task(), [f"chroot:{chroot}"])]


def test_run_batch_lookup_enabled_repos(cache_dir, fake_subprocess):
    fake_subprocess.when("python3 -c",
                         stdout=json.dumps([{"dep": "ssl", "rpm": "x", "version": None,
                                             "release": None, "level": "name"}]))
    mod.run_batch_lookup([_task()], enabled_repos=["oe-official", "oe-epol"])
    _, kwargs = fake_subprocess.calls[0]
    payload = json.loads(kwargs["input"])
    assert payload[0]["enabled_repos"] == ["oe-official", "oe-epol"]


def test_run_batch_lookup_partial_cache_hit(cache_dir, fake_subprocess):
    t1, t2 = _task("ssl"), _task("zlib")
    mod._save_cache({mod._task_key(t1, []): {"dep": "ssl", "rpm": "cached",
                                             "version": None, "release": None, "level": ""}})
    fake_subprocess.when(
        "python3 -c",
        stdout=json.dumps([{"dep": "zlib", "rpm": "zlib-devel", "version": "1.2.11",
                            "release": "1", "level": "name"}]),
    )
    results = mod.run_batch_lookup([t1, t2])
    assert results[0]["rpm"] == "cached"
    assert results[1]["rpm"] == "zlib-devel"
    # 子进程只收到未命中任务
    payload = json.loads(fake_subprocess.calls[0][1]["input"])
    assert [p["dep"] for p in payload] == ["zlib"]


def test_run_batch_lookup_unknown_chroot_uses_local_repos(cache_dir, fake_subprocess):
    # 未知 chroot → repofrompath 为空 → 不注入 repofrompath,等同本地查询
    fake_subprocess.when("python3 -c",
                         stdout=json.dumps([{"dep": "ssl", "rpm": None, "version": None,
                                             "release": None, "level": ""}]))
    mod.run_batch_lookup([_task()], chroot="fedora-39-x86_64")
    payload = json.loads(fake_subprocess.calls[0][1]["input"])
    assert "repofrompath" not in payload[0]
