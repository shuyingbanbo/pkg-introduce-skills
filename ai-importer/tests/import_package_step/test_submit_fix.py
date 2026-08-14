"""submit_fix.py — fixer 修复后重新提交 COPR(纯函数 + tarball/SRPM 流程)。"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

# submit_fix 顶层 from timeline import write_event → step 目录;
# _resolve_chroots 内部 from copr_client import → build-rpm 目录
sys.path.insert(0, str(SCRIPT_DIRS["step"]))
sys.path.insert(0, str(SCRIPT_DIRS["build_rpm"]))

sf = load_module("submit_fix", SCRIPT_DIRS["step"] / "submit_fix.py")


# ─────────────────────────────────────────────
# _strip_tar_suffix
# ─────────────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("foo-1.0.tar.gz", "foo-1.0"),
    ("foo-1.0.tar.xz", "foo-1.0"),
    ("foo-1.0.tar.bz2", "foo-1.0"),
    ("foo-1.0.tgz", "foo-1.0"),
    ("foo-1.0.tar", "foo-1.0"),
    ("foo-1.0", "foo-1.0"),
    ("foo.tar.gz.tar.gz", "foo.tar.gz"),
])
def test_strip_tar_suffix(name, expected):
    assert sf._strip_tar_suffix(name) == expected


# ─────────────────────────────────────────────
# _fail / _read_json / _gate_version
# ─────────────────────────────────────────────

def test_fail_returns_code(capsys):
    assert sf._fail(5, "stage", "msg") == 5
    assert "FAIL[stage] msg" in capsys.readouterr().err


def test_gate_version(tmp_path):
    pkg_dir = tmp_path / "pkgs" / "pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "gate_result_pkg.json").write_text(json.dumps(
        {"result": {"version": "1.2.3"}}))
    assert sf._gate_version(tmp_path, "pkg") == "1.2.3"


def test_gate_version_missing_or_bad(tmp_path):
    assert sf._gate_version(tmp_path, "pkg") == ""
    pkg_dir = tmp_path / "pkgs" / "pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "gate_result_pkg.json").write_text("{bad")
    assert sf._gate_version(tmp_path, "pkg") == ""


# ─────────────────────────────────────────────
# _failed_chroots
# ─────────────────────────────────────────────

def _reg(tmp_path, data):
    (tmp_path / "dep_registry.json").write_text(json.dumps(data))


def test_failed_chroots_basic(tmp_path):
    _reg(tmp_path, {"pkg": {"chroots": {
        "a": {"status": "failed"}, "b": {"status": "build_done"}, "c": {"status": "failed"},
    }}})
    # 保持入参顺序
    assert sf._failed_chroots(tmp_path, "pkg", ["c", "b", "a"]) == ["c", "a"]


def test_failed_chroots_no_registry(tmp_path):
    assert sf._failed_chroots(tmp_path, "pkg", ["a"]) == []


def test_failed_chroots_no_entry_or_old_format(tmp_path):
    _reg(tmp_path, {"other": {"status": "failed"}})
    assert sf._failed_chroots(tmp_path, "pkg", ["a"]) == []
    _reg(tmp_path, {"pkg": {"status": "failed"}})  # 旧单 chroot 格式
    assert sf._failed_chroots(tmp_path, "pkg", ["a"]) == []


def test_failed_chroots_bad_json(tmp_path):
    (tmp_path / "dep_registry.json").write_text("{bad")
    assert sf._failed_chroots(tmp_path, "pkg", ["a"]) == []


# ─────────────────────────────────────────────
# _resolve_chroots(优先级 + failed 子集退化)
# ─────────────────────────────────────────────

def _args(chroots="", chroot="", all_chroots=False):
    return SimpleNamespace(chroots=chroots, chroot=chroot, all_chroots=all_chroots, pkg="pkg")


def test_resolve_chroots_explicit_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("COPR_BUILD_CHROOTS", "env-chroot")
    session = {"copr_chroots": ["session-chroot"]}
    assert sf._resolve_chroots(_args(chroots="cli-a,cli-b"), session, tmp_path) == ["cli-a", "cli-b"]
    assert sf._resolve_chroots(_args(chroot="cli-c"), session, tmp_path) == ["cli-c"]


def test_resolve_chroots_env_priority(tmp_path, monkeypatch):
    monkeypatch.setenv("COPR_BUILD_CHROOTS", "env-a, env-b")
    session = {"copr_chroots": ["session-chroot"]}
    assert sf._resolve_chroots(_args(), session, tmp_path) == ["env-a", "env-b"]


def test_resolve_chroots_session_fallback(tmp_path, monkeypatch):
    session = {"copr_chroots": ["session-a", "session-b"]}
    assert sf._resolve_chroots(_args(), session, tmp_path) == ["session-a", "session-b"]
    session2 = {"copr_chroot": "single-old"}
    assert sf._resolve_chroots(_args(), session2, tmp_path) == ["single-old"]


def test_resolve_chroots_failed_subset(tmp_path, monkeypatch):
    """无显式 chroot 时只重交 failed 子集(增量重建)。"""
    _reg(tmp_path, {"pkg": {"chroots": {"a": {"status": "failed"}, "b": {"status": "build_done"}}}})
    session = {"copr_chroots": ["a", "b"]}
    assert sf._resolve_chroots(_args(), session, tmp_path) == ["a"]


def test_resolve_chroots_failed_subset_empty_falls_back_full(tmp_path, monkeypatch):
    """failed 子集为空(无 per-chroot 信息)→ 退化为全量。"""
    session = {"copr_chroots": ["a", "b"]}
    assert sf._resolve_chroots(_args(), session, tmp_path) == ["a", "b"]
    _reg(tmp_path, {"pkg": {"status": "failed"}})  # 旧格式 → 退化全量
    assert sf._resolve_chroots(_args(), session, tmp_path) == ["a", "b"]


def test_resolve_chroots_all_chroots_flag(tmp_path, monkeypatch):
    _reg(tmp_path, {"pkg": {"chroots": {"a": {"status": "failed"}, "b": {"status": "build_done"}}}})
    session = {"copr_chroots": ["a", "b"]}
    # all_chroots=True 时跳过 failed 子集,全量
    assert sf._resolve_chroots(_args(all_chroots=True), session, tmp_path) == ["a", "b"]


# ─────────────────────────────────────────────
# _ensure_tarball
# ─────────────────────────────────────────────

def _gate_with_version(sd, pkg, version):
    pkg_dir = sd / "pkgs" / pkg
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / f"gate_result_{pkg}.json").write_text(json.dumps(
        {"result": {"version": version}}))


def test_ensure_tarball_fresh_no_rebuild(tmp_path, fake_subprocess):
    spec = tmp_path / "specs" / "pkg.spec"
    spec.parent.mkdir()
    spec.write_text("x")
    _gate_with_version(tmp_path, "pkg", "1.0")

    build_sources = tmp_path / "build" / "SOURCES"
    build_sources.mkdir(parents=True)
    target = build_sources / "pkg-1.0.tar.gz"
    target.write_text("old tar")
    # tarball 比 spec 新
    import os as _os
    _os.utime(target, (spec.stat().st_mtime + 10, spec.stat().st_mtime + 10))

    rc = sf._ensure_tarball(tmp_path, "pkg", spec)
    assert rc == 0
    assert not fake_subprocess.called_with("tar")  # 未触发重打


def test_ensure_tarball_rebuild_when_missing(tmp_path, fake_subprocess):
    spec = tmp_path / "specs" / "pkg.spec"
    spec.parent.mkdir()
    spec.write_text("x")
    _gate_with_version(tmp_path, "pkg", "1.0")
    (tmp_path / "sources" / "pkg").mkdir(parents=True)

    fake_subprocess.when("tar", returncode=0)
    rc = sf._ensure_tarball(tmp_path, "pkg", spec)
    assert rc == 0
    assert fake_subprocess.called_with("tar")
    # fake 不真执行 tar,断言目标路径出现在命令参数里
    assert any("pkg-1.0.tar.gz" in " ".join(c) for c, _ in fake_subprocess.calls)


def test_ensure_tarball_missing_sources_dir(tmp_path, fake_subprocess):
    spec = tmp_path / "specs" / "pkg.spec"
    spec.parent.mkdir()
    spec.write_text("x")
    _gate_with_version(tmp_path, "pkg", "1.0")
    # 无 sources/ 目录
    rc = sf._ensure_tarball(tmp_path, "pkg", spec)
    assert rc == 5


def test_ensure_tarball_no_version_no_candidate(tmp_path, fake_subprocess):
    spec = tmp_path / "specs" / "pkg.spec"
    spec.parent.mkdir()
    spec.write_text("x")
    (tmp_path / "sources" / "pkg").mkdir(parents=True)
    rc = sf._ensure_tarball(tmp_path, "pkg", spec)
    assert rc == 1  # 无法确定 tarball 名称


def test_ensure_tarball_tar_failure(tmp_path, fake_subprocess):
    spec = tmp_path / "specs" / "pkg.spec"
    spec.parent.mkdir()
    spec.write_text("x")
    _gate_with_version(tmp_path, "pkg", "1.0")
    (tmp_path / "sources" / "pkg").mkdir(parents=True)

    fake_subprocess.when("tar", returncode=1, stderr="boom")
    rc = sf._ensure_tarball(tmp_path, "pkg", spec)
    assert rc == 2


# ─────────────────────────────────────────────
# _build_srpm
# ─────────────────────────────────────────────

def _spec(tmp_path):
    spec = tmp_path / "specs" / "pkg.spec"
    spec.parent.mkdir()
    spec.write_text("Name: pkg\nVersion: 1.0\n")
    # _build_srpm 追加写 pkgs/pkg/build.log,目录须已存在(生产由 session 初始化保证)
    (tmp_path / "pkgs" / "pkg").mkdir(parents=True)
    return spec


def test_build_srpm_success(tmp_path, fake_subprocess):
    srpms = tmp_path / "srpms"
    srpms.mkdir()
    srpm_path = srpms / "pkg-1.0-1.src.rpm"
    srpm_path.touch()
    fake_subprocess.when("rpmbuild", stdout=f"Wrote: {srpm_path}")

    srpm, rc = sf._build_srpm(tmp_path, "pkg", _spec(tmp_path))
    assert rc == 0
    assert srpm == srpm_path
    # 构建日志存档
    assert (tmp_path / "pkgs" / "pkg" / "build.log").exists()


def test_build_srpm_rpmbuild_failure(tmp_path, fake_subprocess):
    fake_subprocess.when("rpmbuild", returncode=1, stderr="error: bad spec")
    srpm, rc = sf._build_srpm(tmp_path, "pkg", _spec(tmp_path))
    assert rc == 3
    assert srpm is None


def test_build_srpm_no_wrote_line(tmp_path, fake_subprocess):
    fake_subprocess.when("rpmbuild", returncode=0, stdout="no output")
    srpm, rc = sf._build_srpm(tmp_path, "pkg", _spec(tmp_path))
    assert rc == 3
    assert srpm is None


def test_build_srpm_wrote_but_missing_file(tmp_path, fake_subprocess):
    fake_subprocess.when("rpmbuild", returncode=0,
                         stdout="Wrote: /nonexistent/pkg-1.0-1.src.rpm")
    srpm, rc = sf._build_srpm(tmp_path, "pkg", _spec(tmp_path))
    assert rc == 3
    assert srpm is None
