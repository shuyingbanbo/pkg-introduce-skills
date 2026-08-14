"""chroot_toolchain.py — 构建工具链名单与清单生成/查询。

名单/归一化为纯函数;清单生成用注入 sys.modules 的假 dnf 模块驱动
(_query_toolchain_versions 内部 `import dnf`)。
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sys.path.insert(0, str(SCRIPT_DIRS["build_rpm"]))
ct = load_module("chroot_toolchain", SCRIPT_DIRS["build_rpm"] / "chroot_toolchain.py")


# ─────────────────────────────────────────────
# 名单与归一化
# ─────────────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("Python3-setuptools_scm", "setuptools-scm"),
    ("setuptools_scm", "setuptools-scm"),
    ("python-setuptools", "setuptools"),
    ("py3-build", "build"),
    ("gcc", "gcc"),
    ("GCC-C++", "gcc-c++"),
    ("python3", "python3"),
])
def test_normalize_toolchain_name(name, expected):
    assert ct._normalize_toolchain_name(name) == expected


@pytest.mark.parametrize("name,expected", [
    ("gcc", True),
    ("python3-setuptools", True),
    ("setuptools-scm", True),
    ("setuptools_scm", True),       # 上游 PyPI 风格
    ("cargo", True),
    ("nodejs", True),
    ("java-latest-openjdk-devel", True),
    ("python3-hatchling", True),
    ("requests", False),
    ("libssl", False),
    ("", False),
])
def test_is_toolchain(name, expected):
    assert ct.is_toolchain(name) is expected


@pytest.mark.parametrize("name,expected", [
    ("setuptools", True),
    ("hatchling", True),
    ("poetry-core", True),
    ("cmake", True),          # cmake 在 BUILD_SYSTEM_TOOLS 中
    ("ninja", True),
    ("pbr", True),
    ("gcc", False),           # gcc 只在 TOOLCHAIN_PACKAGES,不属于 build-system 后端
    ("nodejs", False),
    ("python3-pip", True),    # python3- 前缀剥离后归一为 pip(在 BUILD_SYSTEM_TOOLS)
])
def test_is_build_system_tool(name, expected):
    assert ct.is_build_system_tool(name) is expected


# ─────────────────────────────────────────────
# 假 dnf 模块
# ─────────────────────────────────────────────

class _FakePkg:
    def __init__(self, name, version="1.0", release="1"):
        self.name = name
        self.version = version
        self.release = release


class _FakeQuery:
    def __init__(self, pkgs):
        self._pkgs = pkgs

    def filter(self, **kw):
        name = kw.get("name")
        return [p for p in self._pkgs if p.name == name]


class _FakeSack:
    def __init__(self, pkgs):
        self._pkgs = pkgs

    def query(self):
        return _FakeQuery(self._pkgs)


class _FakeRepo:
    def __init__(self):
        self.disabled = False

    def disable(self):
        self.disabled = True


class _FakeRepos:
    def __init__(self):
        self._repos = [_FakeRepo()]
        self.added = []

    def iter_enabled(self):
        return iter(self._repos)

    def add_new_repo(self, repo_id, conf, baseurl=None):
        self.added.append((repo_id, list(baseurl or [])))


class _FakeConf:
    cachedir = None
    cacheonly = None


class _FakeBase:
    def __init__(self, pkgs):
        self.conf = _FakeConf()
        self.repos = _FakeRepos()
        self.sack = _FakeSack(pkgs)
        self.fill_sack_kwargs = None

    def read_all_repos(self):
        pass

    def fill_sack(self, **kwargs):
        self.fill_sack_kwargs = kwargs


@pytest.fixture
def fake_dnf(monkeypatch):
    """注入假 dnf 模块,返回本次安装创建的 Base 实例列表。"""
    def _install(pkgs):
        bases = []

        class FakeDnfBase(_FakeBase):
            def __init__(self):
                super().__init__(pkgs)
                bases.append(self)

        m = types.ModuleType("dnf")
        m.Base = FakeDnfBase
        monkeypatch.setitem(sys.modules, "dnf", m)
        return bases
    return _install


# ─────────────────────────────────────────────
# _query_toolchain_versions
# ─────────────────────────────────────────────

def test_query_toolchain_versions_ok(fake_dnf):
    fake_dnf([
        _FakePkg("gcc", "10.3.1", "3"),
        _FakePkg("make", "4.3", "1"),
    ])
    result = ct._query_toolchain_versions("openeuler-24.03_LTS_SP3-x86_64")
    # 名单中所有工具都有条目
    assert set(result) == set(ct.TOOLCHAIN_PACKAGES)
    assert result["gcc"] == {"version": "10.3.1", "release": "3", "available": True}
    assert result["make"] == {"version": "4.3", "release": "1", "available": True}
    # 未查询到的工具 → available False
    assert result["rust"] == {"version": None, "release": None, "available": False}


def test_query_toolchain_versions_dnf_config(fake_dnf):
    bases = fake_dnf([_FakePkg("gcc", "10.3.1", "3")])
    ct._query_toolchain_versions("openeuler-24.03_LTS_SP3-x86_64")
    base = bases[0]
    assert base.conf.cachedir == "/var/cache/dnf"
    assert base.conf.cacheonly is False
    assert base.fill_sack_kwargs == {"load_system_repo": False,
                                     "load_available_repos": True}
    # 本地默认 repo 全部禁用,改用目标 chroot 的三个官方源
    assert base.repos._repos[0].disabled is True
    assert len(base.repos.added) == 3
    assert base.repos.added[0][0].startswith("oe-official-openeuler-24.03_LTS_SP3-x86_64")
    assert base.repos.added[0][1] == [
        "http://repo.openeuler.org/openEuler-24.03-LTS-SP3/everything/x86_64/"]


def test_query_toolchain_versions_unknown_chroot(fake_dnf):
    fake_dnf([_FakePkg("gcc")])
    with pytest.raises(RuntimeError) as exc:
        ct._query_toolchain_versions("fedora-39-x86_64")
    assert "unknown chroot" in str(exc.value)


def test_query_toolchain_versions_dnf_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "dnf", None)   # import dnf 将抛 ImportError
    with pytest.raises(RuntimeError) as exc:
        ct._query_toolchain_versions("openeuler-24.03_LTS_SP3-x86_64")
    assert "dnf Python module not available" in str(exc.value)


# ─────────────────────────────────────────────
# generate_manifest
# ─────────────────────────────────────────────

def test_generate_manifest_with_output(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "_query_toolchain_versions", lambda chroot: {
        "gcc": {"version": "10.3.1", "release": "3", "available": True},
    })
    out = tmp_path / "sub" / "toolchain.json"
    manifest = ct.generate_manifest("openeuler-24.03_LTS_SP3-x86_64", out)
    assert manifest["chroot"] == "openeuler-24.03_LTS_SP3-x86_64"
    assert manifest["toolchain"] == {
        "gcc": {"version": "10.3.1", "release": "3", "available": True}}
    # generated_at 为 UTC ISO 时间戳
    import datetime
    datetime.datetime.fromisoformat(manifest["generated_at"])
    assert out.exists()
    saved = json.loads(out.read_text())
    assert saved["chroot"] == "openeuler-24.03_LTS_SP3-x86_64"


def test_generate_manifest_without_output(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "_query_toolchain_versions", lambda chroot: {})
    manifest = ct.generate_manifest("openeuler-24.03_LTS_SP3-x86_64")
    assert manifest["toolchain"] == {}
    assert list(tmp_path.iterdir()) == []   # 未写任何文件


# ─────────────────────────────────────────────
# load_manifest / get_tool_version
# ─────────────────────────────────────────────

def test_load_manifest_empty(tmp_path):
    assert ct.load_manifest(tmp_path) == {}


def test_load_manifest_single(tmp_path):
    (tmp_path / "toolchain_openeuler-24.03.json").write_text(json.dumps({
        "chroot": "x", "generated_at": "t",
        "toolchain": {"gcc": {"version": "10.3.1", "release": "3", "available": True}},
    }))
    manifest = ct.load_manifest(tmp_path)
    assert manifest["toolchain"]["gcc"]["version"] == "10.3.1"


def test_load_manifest_merges_multiple(tmp_path):
    (tmp_path / "toolchain_aaa.json").write_text(json.dumps({
        "toolchain": {"gcc": {"version": "1", "available": True}},
    }))
    (tmp_path / "toolchain_bbb.json").write_text(json.dumps({
        "toolchain": {"make": {"version": "2", "available": True},
                      "gcc": {"version": "9", "available": True}},   # 后者覆盖同名
    }))
    manifest = ct.load_manifest(tmp_path)
    assert manifest["toolchain"]["gcc"]["version"] == "9"
    assert manifest["toolchain"]["make"]["version"] == "2"


def test_load_manifest_skips_corrupt(tmp_path):
    (tmp_path / "toolchain_good.json").write_text(json.dumps({
        "toolchain": {"gcc": {"version": "1", "available": True}}}))
    (tmp_path / "toolchain_bad.json").write_text("{not json")
    (tmp_path / "toolchain_empty_toolchain.json").write_text(json.dumps({"other": 1}))
    manifest = ct.load_manifest(tmp_path)
    assert list(manifest["toolchain"]) == ["gcc"]


def test_get_tool_version_available(tmp_path):
    (tmp_path / "toolchain_x.json").write_text(json.dumps({
        "toolchain": {"gcc": {"version": "10.3.1", "release": "3", "available": True},
                      "rust": {"version": None, "release": None, "available": False}},
    }))
    assert ct.get_tool_version(tmp_path, "gcc") == "10.3.1"
    assert ct.get_tool_version(tmp_path, "rust") is None     # available False
    assert ct.get_tool_version(tmp_path, "make") is None     # 不存在


def test_get_tool_version_no_manifest(tmp_path):
    assert ct.get_tool_version(tmp_path, "gcc") is None
