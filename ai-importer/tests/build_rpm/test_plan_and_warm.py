"""plan_dependency_layer.py + warm_repo_cache.py — 层计划与缓存预热(小脚本)。"""

from __future__ import annotations

import json
import sys

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sys.path.insert(0, str(SCRIPT_DIRS["build_rpm"]))

pdl = load_module("plan_dependency_layer", SCRIPT_DIRS["build_rpm"] / "plan_dependency_layer.py")
wrc = load_module("warm_repo_cache", SCRIPT_DIRS["build_rpm"] / "warm_repo_cache.py")


def _main(monkeypatch, mod, argv):
    monkeypatch.setattr("sys.argv", ["x.py"] + argv)
    return mod.main()


# ─────────────────────────────────────────────
# plan_dependency_layer
# ─────────────────────────────────────────────

def test_plan_writes_output_and_returns_0(tmp_path, monkeypatch, capsys):
    req = tmp_path / "requests.json"
    req.write_text(json.dumps({"pkgname": "main", "lang": "python", "requests": [
        {"name": "dep1", "constraint": ">=1.0"},
    ]}))
    out = tmp_path / "plan.json"
    monkeypatch.setattr(pdl, "resolve_layer_candidates", lambda *a, **k: {
        "pkgname": "main", "layers": [], "blocked": False,
    })
    rc = _main(monkeypatch, pdl, ["--requests-json", str(req),
                                  "--requested-by", "main", "-o", str(out)])
    assert rc == 0
    assert json.loads(out.read_text())["blocked"] is False
    assert "blocked" in capsys.readouterr().out


def test_plan_blocked_returns_2(tmp_path, monkeypatch, capsys):
    req = tmp_path / "requests.json"
    req.write_text(json.dumps({"requests": []}))
    monkeypatch.setattr(pdl, "resolve_layer_candidates", lambda *a, **k: {"blocked": True})
    rc = _main(monkeypatch, pdl, ["--requests-json", str(req), "--requested-by", "main"])
    assert rc == 2


def test_plan_read_failure_returns_1(tmp_path, monkeypatch, capsys):
    req = tmp_path / "requests.json"
    req.write_text("{bad")
    rc = _main(monkeypatch, pdl, ["--requests-json", str(req), "--requested-by", "main"])
    assert rc == 1
    assert "错误" in capsys.readouterr().err


def test_plan_no_requests_field(tmp_path, monkeypatch, capsys):
    """requests 缺失 → 空列表,不崩。"""
    req = tmp_path / "requests.json"
    req.write_text(json.dumps({"pkgname": "main"}))
    monkeypatch.setattr(pdl, "resolve_layer_candidates", lambda *a, **k: {"blocked": False})
    rc = _main(monkeypatch, pdl, ["--requests-json", str(req), "--requested-by", "main"])
    assert rc == 0


# ─────────────────────────────────────────────
# warm_repo_cache
# ─────────────────────────────────────────────

def test_warm_no_chroot(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["warm_repo_cache.py"])
    wrc.main()
    assert "no chroot" in capsys.readouterr().err


def test_warm_unknown_chroot(monkeypatch, capsys):
    monkeypatch.setattr(wrc, "chroot_to_repofrompath", lambda c: [])
    monkeypatch.setattr(sys, "argv", ["warm_repo_cache.py", "unknown-chroot"])
    wrc.main()
    assert "unknown chroot" in capsys.readouterr().err


class _FakePath:
    """替换 warm_repo_cache 模块的 Path 绑定,控制 glob 结果。"""

    def __init__(self, p, cached=False):
        self._cached = cached

    def glob(self, pattern):
        # cache_root.glob("repo-a-*/repodata/repomd.xml")
        return ["/fake/repomd.xml"] if (self._cached and "repomd.xml" in pattern) else []


def test_warm_cache_fresh_skips(monkeypatch, capsys):
    monkeypatch.setattr(wrc, "chroot_to_repofrompath", lambda c: [("repo-a", "/x")])
    monkeypatch.setattr(wrc, "Path", lambda p: _FakePath(p, cached=True))
    monkeypatch.setattr(sys, "argv", ["warm_repo_cache.py", "chroot"])
    wrc.main()
    assert "cache already fresh" in capsys.readouterr().err


def test_warm_runs_batch_lookup(monkeypatch, capsys):
    monkeypatch.setattr(wrc, "chroot_to_repofrompath", lambda c: [("repo-a", "/x")])
    monkeypatch.setattr(wrc, "Path", lambda p: _FakePath(p, cached=False))
    calls = []
    monkeypatch.setattr(wrc, "run_batch_lookup", lambda *a, **k: (calls.append(k) or {}))
    monkeypatch.setattr(sys, "argv", ["warm_repo_cache.py", "openeuler-24.03-x86_64"])
    wrc.main()
    assert calls[0]["chroot"] == "openeuler-24.03-x86_64"
    assert "done for openeuler-24.03-x86_64" in capsys.readouterr().err


def test_warm_lookup_failure_warns(monkeypatch, capsys):
    monkeypatch.setattr(wrc, "chroot_to_repofrompath", lambda c: [("repo-a", "/x")])
    monkeypatch.setattr(wrc, "Path", lambda p: _FakePath(p, cached=False))
    def boom(*a, **k):
        raise RuntimeError("dnf down")
    monkeypatch.setattr(wrc, "run_batch_lookup", boom)
    monkeypatch.setattr(sys, "argv", ["warm_repo_cache.py", "chroot"])
    wrc.main()
    assert "warning" in capsys.readouterr().err
