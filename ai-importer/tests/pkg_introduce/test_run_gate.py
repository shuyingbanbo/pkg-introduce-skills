"""run_gate.py — 引入门禁（COPR 模式）测试。

覆盖：
- _get_project_chroots（x86_64 优先 / 无 x86 兜底 / 异常）、_save、_already_done
- _download_eur_srpm / _fetch_reference（幂等跳过、成功命令、失败告警）
- run_gate 编排：8 种 decision 的 reason 文本、决策后动作触发、
  chroot 解析优先级（参数 > session.json > COPR API）、异常路径、
  幂等续跑、result 字段、出口码
- main（--pkg-dir 重定向、argparse 缺参）
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.request

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sys.path.insert(0, str(SCRIPT_DIRS["pkg_introduce"]))
gate = load_module("run_gate", SCRIPT_DIRS["pkg_introduce"] / "run_gate.py")


def make_args(**overrides):
    defaults = dict(
        pkg="testpkg", upstream_url="https://github.com/x/testpkg",
        lang="", version="", constraint="", mode="top-level", pkg_dir=None,
        copr_url="", copr_owner="", copr_project="", copr_login="",
        copr_token="", copr_chroot="", reports_dir="./reports",
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _write_check_result(reports_dir, pkg="testpkg", lang="python",
                        version="1.2.3", overall="done"):
    p = reports_dir / f"check_result_{pkg}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "overall_status": overall,
        "result": {"lang": lang, "version": version},
    }), encoding="utf-8")
    return p


def _cascade(decision, level=4, match=None, reference=None, **extra):
    return {"decision": decision, "level": level,
            "match": match, "reference": reference, **extra}


def _fake_check(monkeypatch, cascade, record=None):
    def check_package_existence(pkg, **kw):
        if record is not None:
            record["pkg"] = pkg
            record.update(kw)
        return cascade
    monkeypatch.setattr(gate, "check_package_existence", check_package_existence)
    return check_package_existence


class _FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ─────────────────────────────────────────────
# _get_project_chroots
# ─────────────────────────────────────────────

def test_get_project_chroots_prefers_x86(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        captured["timeout"] = timeout
        return _FakeResponse({
            "chroot_repos": {
                "openeuler-24.03-aarch64": "http://a",
                "openeuler-24.03-x86_64": "http://x",
            }
        })

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    chroots = gate._get_project_chroots("http://copr/", "owner", "proj", "login", "tok")
    assert chroots == ["openeuler-24.03-x86_64"]
    req = captured["req"]
    assert req.full_url == "http://copr/api_3/project?ownername=owner&projectname=proj"
    expected_auth = "Basic " + base64.b64encode(b"login:tok").decode()
    assert req.headers["Authorization"] == expected_auth
    assert captured["timeout"] == 10


def test_get_project_chroots_no_x86_falls_back_to_all(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResponse({
                            "chroot_repos": {"openeuler-24.03-aarch64": "http://a"}}))
    assert gate._get_project_chroots("http://c/", "o", "p", "l", "t") == \
        ["openeuler-24.03-aarch64"]


def test_get_project_chroots_empty(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResponse({"chroot_repos": {}}))
    assert gate._get_project_chroots("http://c/", "o", "p", "l", "t") == []


def test_get_project_chroots_urlopen_error(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("network down")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert gate._get_project_chroots("http://c/", "o", "p", "l", "t") == []


# ─────────────────────────────────────────────
# _save / _already_done
# ─────────────────────────────────────────────

def test_save_creates_parents(tmp_path):
    target = tmp_path / "a" / "b" / "report.json"
    gate._save({"k": "v"}, target)
    assert json.loads(target.read_text(encoding="utf-8")) == {"k": "v"}


@pytest.mark.parametrize("step,expected", [
    ({"status": "done"}, True),
    ({"status": "skipped"}, True),
    ({"status": "pending"}, False),
    ({"status": "failed"}, False),
    ({}, False),
])
def test_already_done(step, expected):
    assert gate._already_done(step) is expected


# ─────────────────────────────────────────────
# _download_eur_srpm
# ─────────────────────────────────────────────

def test_download_eur_srpm_skips_existing(fake_subprocess, tmp_path):
    srpms_dir = tmp_path / "srpms"
    srpms_dir.mkdir()
    (srpms_dir / "foo-1.0.src.rpm").write_text("x")
    gate._download_eur_srpm(
        {"srpm_url": "http://x/foo.src.rpm", "srpm_file": "foo-1.0.src.rpm"},
        "foo", tmp_path / "pkgs" / "foo", srpms_dir)
    assert fake_subprocess.calls == []


def test_download_eur_srpm_success(fake_subprocess, tmp_path, capsys):
    pkgs_dir = tmp_path / "pkgs" / "foo"
    srpms_dir = tmp_path / "srpms"
    gate._download_eur_srpm(
        {"srpm_url": "http://x/foo.src.rpm", "srpm_file": "foo-1.0.src.rpm"},
        "foo", pkgs_dir, srpms_dir)
    assert fake_subprocess.called_with("curl -sL -o")
    assert fake_subprocess.called_with("http://x/foo.src.rpm")
    assert fake_subprocess.called_with("rpm2cpio")
    assert fake_subprocess.called_with("cpio -idmv")
    assert (pkgs_dir / "reference").is_dir()
    assert "[gate] spec 已提取到" in capsys.readouterr().err


def test_download_eur_srpm_default_filename(fake_subprocess, tmp_path):
    gate._download_eur_srpm({"srpm_url": "http://x/a.src.rpm"},
                            "foo", tmp_path / "pkgs" / "foo", tmp_path / "srpms")
    assert fake_subprocess.called_with("-o")
    assert fake_subprocess.called_with("foo.src.rpm")


def test_download_eur_srpm_failure_warns(fake_subprocess, tmp_path, capsys):
    fake_subprocess.when("curl", exc=OSError("boom"))
    gate._download_eur_srpm({"srpm_url": "http://x/foo.src.rpm"},
                            "foo", tmp_path / "pkgs" / "foo", tmp_path / "srpms")
    err = capsys.readouterr().err
    assert "WARN: 下载/提取 SRPM 失败" in err
    assert "boom" in err


# ─────────────────────────────────────────────
# _fetch_reference
# ─────────────────────────────────────────────

def test_fetch_reference_skips_existing_spec(fake_subprocess, tmp_path):
    ref_dir = tmp_path / "pkgs" / "foo" / "reference"
    ref_dir.mkdir(parents=True)
    (ref_dir / "foo.spec").write_text("x")
    gate._fetch_reference({"repo_name": "bar"}, "foo", tmp_path / "pkgs" / "foo")
    assert fake_subprocess.calls == []


def test_fetch_reference_with_branch(fake_subprocess, tmp_path, capsys):
    gate._fetch_reference({"repo_name": "bar", "target_branch": "master"},
                          "foo", tmp_path / "pkgs" / "foo")
    assert fake_subprocess.called_with("--pkgname bar")
    assert fake_subprocess.called_with("--target-branch master")
    assert "branch=master" in capsys.readouterr().err


def test_fetch_reference_no_branch(fake_subprocess, tmp_path):
    gate._fetch_reference({"repo_name": "bar"}, "foo", tmp_path / "pkgs" / "foo")
    assert fake_subprocess.called_with("--pkgname bar")
    for cmd, _ in fake_subprocess.calls:
        joined = " ".join(cmd)
        assert "--target-branch" not in joined


def test_fetch_reference_failure_warns(fake_subprocess, tmp_path, capsys):
    fake_subprocess.when(lambda s: "fetch_reference_spec.py" in s,
                         exc=OSError("boom"))
    gate._fetch_reference({"repo_name": "bar"}, "foo", tmp_path / "pkgs" / "foo")
    assert "WARN: 拉取参考源失败" in capsys.readouterr().err


# ─────────────────────────────────────────────
# run_gate — 前置校验
# ─────────────────────────────────────────────

def test_run_gate_missing_check_report(tmp_path, capsys):
    rc = gate.run_gate(make_args(reports_dir=str(tmp_path / "reports")))
    assert rc == 1
    assert "check_result_testpkg.json not found" in capsys.readouterr().err


def test_run_gate_check_not_done(tmp_path, capsys):
    reports = tmp_path / "reports"
    _write_check_result(reports, overall="needs_ai")
    rc = gate.run_gate(make_args(reports_dir=str(reports)))
    assert rc == 1
    assert "check phase not done (status=needs_ai)" in capsys.readouterr().err


def test_run_gate_missing_lang_version(tmp_path, capsys):
    reports = tmp_path / "reports"
    _write_check_result(reports, lang="", version="")
    rc = gate.run_gate(make_args(reports_dir=str(reports)))
    assert rc == 1
    assert "lang or version missing" in capsys.readouterr().err


def test_run_gate_lang_version_from_check_result(tmp_path, monkeypatch):
    # args 为空时回退到 check_result 的 lang/version
    reports = tmp_path / "reports"
    _write_check_result(reports, lang="rust", version="1.60.0")
    record = {}
    _fake_check(monkeypatch, _cascade("introduce_new", 4), record)
    rc = gate.run_gate(make_args(reports_dir=str(reports), copr_chroot="ch-x86_64"))
    assert rc == 0
    assert record["lang"] == "rust"
    assert record["version"] == "1.60.0"


def test_run_gate_args_lang_overrides(tmp_path, monkeypatch):
    reports = tmp_path / "reports"
    _write_check_result(reports, lang="python", version="1.0")
    record = {}
    _fake_check(monkeypatch, _cascade("introduce_new", 4), record)
    rc = gate.run_gate(make_args(reports_dir=str(reports), lang="go",
                                 copr_chroot="ch-x86_64"))
    assert rc == 0
    assert record["lang"] == "go"


# ─────────────────────────────────────────────
# run_gate — decision 分支 reason 文本
# ─────────────────────────────────────────────

def _run_decision(monkeypatch, tmp_path, cascade, **arg_overrides):
    """跑一次 run_gate 并返回 (returncode, gate report dict)。"""
    reports = tmp_path / "reports"
    _write_check_result(reports)
    _fake_check(monkeypatch, cascade)
    args = make_args(reports_dir=str(reports), copr_chroot="new-x86_64")
    for k, v in arg_overrides.items():
        setattr(args, k, v)
    rc = gate.run_gate(args)
    rep = json.loads((reports / "gate_result_testpkg.json").read_text(encoding="utf-8"))
    return rc, rep


@pytest.mark.parametrize("cascade,expected_reason", [
    (_cascade("reuse_copr_project", 0, match={"source": "copr-src", "version": "2.0"}),
     "用户 COPR project 已有构建：copr-src version=2.0，直接复用"),
    (_cascade("reuse_eur_srpm", 1, match={"eur_owner": "o", "eur_project": "p",
                                          "chroot": "c-x86_64", "version": "3.0"}),
     "EUR 找到 o/p chroot=c-x86_64 version=3.0，下载 SRPM 重建"),
    (_cascade("reuse_official", 2, match={"rpm_name": "foo", "version": "1.5"}),
     "openEuler 目标版本已有满足要求的包：foo 1.5"),
    (_cascade("reuse_additional_repo", 5, match=None,
              rpm_name="ros-foo", version="2.1", source="ros-sig"),
     "项目 additional_repos（外挂源）已有满足要求的包：ros-foo 2.1（ros-sig），直接复用"),
    (_cascade("evaluate", 2, match={"rpm_name": "foo", "version": "0.9"}),
     "openEuler 目标版本有包但版本不满足要求：foo 0.9"),
    (_cascade("introduce_new_with_ref", 3, match={"gitcode_repo": "x-repo"}),
     "src-openeuler 仓库存在：x-repo，以参考 spec 为起点构建"),
    (_cascade("introduce_new_with_ref", 1, match=None,
              reference={"source": "eur", "eur_owner": "o", "eur_project": "p",
                         "version": "1.0", "chroot": "old-x86_64"}),
     "EUR 有 o/p version=1.0 但 chroot 不匹配（old-x86_64 vs new-x86_64），以其 SRPM 为参考重建"),
    (_cascade("introduce_new", 4, match=None),
     "所有来源均未找到，从头构建"),
    (_cascade("weird_decision", 4, match=None),
     "decision=weird_decision"),
])
def test_run_gate_decision_reasons(monkeypatch, tmp_path, cascade, expected_reason):
    rc, rep = _run_decision(monkeypatch, tmp_path, cascade)
    assert rc == 0
    step = rep["steps"]["existing_check"]
    assert step["status"] == "done"
    assert step["decision"] == cascade["decision"]
    assert step["level"] == cascade["level"]
    assert step["reason"] == expected_reason
    assert step["chroot"] == "new-x86_64"
    assert rep["overall_status"] == "done"


def test_run_gate_reason_empty_match_defaults(monkeypatch, tmp_path):
    # match 为空 dict 时字段取空串默认值
    rc, rep = _run_decision(monkeypatch, tmp_path,
                            _cascade("reuse_official", 2, match={}))
    assert rc == 0
    assert rep["steps"]["existing_check"]["reason"] == \
        "openEuler 目标版本已有满足要求的包： "


# ─────────────────────────────────────────────
# run_gate — 决策后动作
# ─────────────────────────────────────────────

def test_run_gate_action_reuse_eur_downloads_srpm(monkeypatch, tmp_path):
    reports = tmp_path / "reports"
    _write_check_result(reports)
    cascade = _cascade("reuse_eur_srpm", 1,
                       match={"eur_owner": "o", "eur_project": "p",
                              "srpm_url": "http://eur/foo.src.rpm",
                              "srpm_file": "foo.src.rpm"})
    _fake_check(monkeypatch, cascade)
    recorded = {}
    monkeypatch.setattr(gate, "_download_eur_srpm",
                        lambda match, pkg, pkgs_dir, srpms_dir: recorded.update(
                            match=match, pkg=pkg, pkgs_dir=pkgs_dir, srpms_dir=srpms_dir))
    rc = gate.run_gate(make_args(reports_dir=str(reports), copr_chroot="c"))
    assert rc == 0
    assert recorded["pkg"] == "testpkg"
    assert recorded["match"]["srpm_url"] == "http://eur/foo.src.rpm"
    assert recorded["pkgs_dir"] == reports.parent / "pkgs" / "testpkg"
    assert recorded["srpms_dir"] == reports.parent / "srpms"


def test_run_gate_action_eur_ref_downloads_srpm(monkeypatch, tmp_path):
    reports = tmp_path / "reports"
    _write_check_result(reports)
    ref = {"source": "eur", "eur_owner": "o", "eur_project": "p",
           "srpm_url": "http://eur/foo.src.rpm"}
    _fake_check(monkeypatch, _cascade("introduce_new_with_ref", 1, reference=ref))
    recorded = {}
    monkeypatch.setattr(gate, "_download_eur_srpm",
                        lambda match, pkg, pkgs_dir, srpms_dir: recorded.update(match=match))
    rc = gate.run_gate(make_args(reports_dir=str(reports), copr_chroot="c"))
    assert rc == 0
    assert recorded["match"] == ref  # ref_info 作为 match 传入


def test_run_gate_action_gitcode_fetches_reference(monkeypatch, tmp_path):
    reports = tmp_path / "reports"
    _write_check_result(reports)
    _fake_check(monkeypatch, _cascade(
        "introduce_new_with_ref", 3,
        match={"gitcode_repo": "x-repo", "repo_name": "x-repo"}))
    recorded = {}
    monkeypatch.setattr(gate, "_fetch_reference",
                        lambda match, pkg, pkgs_dir: recorded.update(
                            match=match, pkg=pkg, pkgs_dir=pkgs_dir))
    rc = gate.run_gate(make_args(reports_dir=str(reports), copr_chroot="c"))
    assert rc == 0
    assert recorded["pkg"] == "testpkg"
    assert recorded["match"]["repo_name"] == "x-repo"


def test_run_gate_no_action_when_reuse_eur_without_url(monkeypatch, tmp_path):
    # reuse_eur_srpm 无 srpm_url → 不触发下载
    reports = tmp_path / "reports"
    _write_check_result(reports)
    _fake_check(monkeypatch, _cascade("reuse_eur_srpm", 1, match={"eur_owner": "o"}))
    called = {"n": 0}
    monkeypatch.setattr(gate, "_download_eur_srpm",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    monkeypatch.setattr(gate, "_fetch_reference",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    rc = gate.run_gate(make_args(reports_dir=str(reports), copr_chroot="c"))
    assert rc == 0
    assert called["n"] == 0


# ─────────────────────────────────────────────
# run_gate — chroot 解析优先级
# ─────────────────────────────────────────────

def _recorded_target(monkeypatch, tmp_path, reports_dir, **arg_overrides):
    reports = reports_dir
    _write_check_result(reports)
    record = {}
    _fake_check(monkeypatch, _cascade("introduce_new", 4), record)
    args = make_args(reports_dir=str(reports))
    for k, v in arg_overrides.items():
        setattr(args, k, v)
    rc = gate.run_gate(args)
    assert rc == 0
    return record.get("target", "")


def test_chroot_from_args(monkeypatch, tmp_path):
    target = _recorded_target(monkeypatch, tmp_path, tmp_path / "pkgs" / "testpkg",
                              copr_chroot="arg-x86_64")
    assert target == "arg-x86_64"


def test_chroot_from_session_json(monkeypatch, tmp_path):
    sd = tmp_path / "session"
    (sd / "pkgs" / "testpkg").mkdir(parents=True)
    (sd / "session.json").write_text(json.dumps({"copr_chroot": "sess-x86_64"}))
    target = _recorded_target(monkeypatch, tmp_path, sd / "pkgs" / "testpkg")
    assert target == "sess-x86_64"


def test_chroot_scan_upper_parent(monkeypatch, tmp_path):
    # session.json 不在 parents[1]，扫描祖先目录找到
    sd = tmp_path / "session"
    reports = sd / "a" / "b" / "testpkg"
    reports.mkdir(parents=True)
    (sd / "session.json").write_text(json.dumps({"copr_chroot": "deep-x86_64"}))
    target = _recorded_target(monkeypatch, tmp_path, reports)
    assert target == "deep-x86_64"


def test_chroot_missing_in_session_falls_back_to_api(monkeypatch, tmp_path):
    sd = tmp_path / "session"
    (sd / "pkgs" / "testpkg").mkdir(parents=True)
    (sd / "session.json").write_text(json.dumps({"other": 1}))
    monkeypatch.setattr(gate, "_get_project_chroots",
                        lambda *a: ["api-x86_64", "api-aarch64"])
    target = _recorded_target(monkeypatch, tmp_path, sd / "pkgs" / "testpkg")
    assert target == "api-x86_64"


def test_chroot_corrupt_session_falls_back_to_api(monkeypatch, tmp_path):
    sd = tmp_path / "session"
    (sd / "pkgs" / "testpkg").mkdir(parents=True)
    (sd / "session.json").write_text("{not json")
    monkeypatch.setattr(gate, "_get_project_chroots",
                        lambda *a: ["api-x86_64"])
    target = _recorded_target(monkeypatch, tmp_path, sd / "pkgs" / "testpkg")
    assert target == "api-x86_64"


def test_chroot_api_empty(monkeypatch, tmp_path):
    reports = tmp_path / "reports"  # 无 session.json
    reports.mkdir()
    _write_check_result(reports)
    monkeypatch.setattr(gate, "_get_project_chroots", lambda *a: [])
    record = {}
    _fake_check(monkeypatch, _cascade("introduce_new", 4), record)
    rc = gate.run_gate(make_args(reports_dir=str(reports)))
    assert rc == 0
    assert record["target"] == ""


def test_copr_creds_from_env(monkeypatch, tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    _write_check_result(reports)
    monkeypatch.setenv("COPR_OWNER", "env-owner")
    monkeypatch.setenv("COPR_PROJECT", "env-proj")
    monkeypatch.setenv("COPR_FRONTEND_URL", "http://env-copr/")
    record = {}
    _fake_check(monkeypatch, _cascade("introduce_new", 4), record)
    rc = gate.run_gate(make_args(reports_dir=str(reports), copr_chroot="c"))
    assert rc == 0
    assert record["copr_url"] == "http://env-copr/"
    assert record["copr_owner"] == "env-owner"
    assert record["copr_project"] == "env-proj"
    rep = json.loads((reports / "gate_result_testpkg.json").read_text())
    assert rep["result"]["copr_owner"] == "env-owner"
    assert rep["result"]["copr_project"] == "env-proj"


def test_copr_defaults(monkeypatch, tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    _write_check_result(reports)
    record = {}
    _fake_check(monkeypatch, _cascade("introduce_new", 4), record)
    rc = gate.run_gate(make_args(reports_dir=str(reports), copr_chroot="c"))
    assert rc == 0
    assert record["copr_url"] == "http://copr-frontend:5000"
    assert record["copr_owner"] == ""
    assert record["copr_project"] == ""


# ─────────────────────────────────────────────
# run_gate — 幂等 / 异常 / result 字段
# ─────────────────────────────────────────────

def test_run_gate_idempotent_skip(tmp_path, monkeypatch, capsys):
    reports = tmp_path / "reports"
    reports.mkdir()
    _write_check_result(reports)
    (reports / "gate_result_testpkg.json").write_text(json.dumps({
        "pkgname": "testpkg", "lang": "python", "version": "1.2.3",
        "overall_status": "pending",
        "steps": {"existing_check": {"status": "done", "decision": "introduce_new",
                                     "reason": "所有来源均未找到，从头构建"}},
        "result": None,
    }))
    called = {"n": 0}

    def check_package_existence(*a, **k):
        called["n"] += 1
        return _cascade("introduce_new", 4)

    monkeypatch.setattr(gate, "check_package_existence", check_package_existence)
    rc = gate.run_gate(make_args(reports_dir=str(reports), copr_chroot="c"))
    assert rc == 0
    assert called["n"] == 0
    rep = json.loads((reports / "gate_result_testpkg.json").read_text())
    assert rep["overall_status"] == "done"
    assert rep["result"]["decision"] == "introduce_new"
    assert rep["result"]["lang"] == "python"
    assert '"status": "done"' in capsys.readouterr().out


def test_run_gate_skipped_step_counts_done(tmp_path, monkeypatch):
    # existing_check 已为 skipped 时同样跳过级联并判定 done
    reports = tmp_path / "reports"
    reports.mkdir()
    _write_check_result(reports)
    (reports / "gate_result_testpkg.json").write_text(json.dumps({
        "steps": {"existing_check": {"status": "skipped", "decision": "",
                                     "reason": ""}},
    }))
    called = {"n": 0}
    monkeypatch.setattr(gate, "check_package_existence",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    rc = gate.run_gate(make_args(reports_dir=str(reports), copr_chroot="c"))
    assert rc == 0
    assert called["n"] == 0


def test_run_gate_pending_extra_step_fails_overall(tmp_path, monkeypatch):
    # 生产行为:所有 steps(含未知键)都需 done/skipped,否则 overall=failed
    reports = tmp_path / "reports"
    reports.mkdir()
    _write_check_result(reports)
    (reports / "gate_result_testpkg.json").write_text(json.dumps({
        "steps": {
            "existing_check": {"status": "done", "decision": "introduce_new",
                               "reason": "r"},
            "build": {"status": "pending"},
        },
    }))
    monkeypatch.setattr(gate, "check_package_existence",
                        lambda *a, **k: _cascade("introduce_new", 4))
    rc = gate.run_gate(make_args(reports_dir=str(reports), copr_chroot="c"))
    assert rc == 1
    rep = json.loads((reports / "gate_result_testpkg.json").read_text())
    assert rep["overall_status"] == "failed"


def test_run_gate_exception_sets_failed(tmp_path, monkeypatch, capsys):
    reports = tmp_path / "reports"
    _write_check_result(reports)

    def boom(*a, **k):
        raise ValueError("cascade boom")

    monkeypatch.setattr(gate, "check_package_existence", boom)
    rc = gate.run_gate(make_args(reports_dir=str(reports), copr_chroot="c"))
    assert rc == 1
    rep = json.loads((reports / "gate_result_testpkg.json").read_text())
    assert rep["overall_status"] == "failed"
    assert rep["steps"]["existing_check"] == {"status": "failed",
                                              "reason": "cascade boom"}
    assert '"status": "failed"' in capsys.readouterr().out


def test_run_gate_result_fields(monkeypatch, tmp_path):
    reports = tmp_path / "reports"
    _write_check_result(reports)
    _fake_check(monkeypatch, _cascade("reuse_official", 2,
                                      match={"rpm_name": "foo", "version": "1.5"}))
    args = make_args(reports_dir=str(reports), copr_chroot="c",
                     copr_owner="o", copr_project="p")
    rc = gate.run_gate(args)
    assert rc == 0
    rep = json.loads((reports / "gate_result_testpkg.json").read_text())
    assert rep["pkgname"] == "testpkg"
    assert rep["lang"] == "python"
    assert rep["version"] == "1.2.3"
    assert rep["result"] == {
        "lang": "python", "version": "1.2.3",
        "decision": "reuse_official",
        "reason": "openEuler 目标版本已有满足要求的包：foo 1.5",
        "copr_owner": "o", "copr_project": "p",
    }
    # steps 中透传 match/reference
    assert rep["steps"]["existing_check"]["match"] == \
        {"rpm_name": "foo", "version": "1.5"}


def test_run_gate_requirement_passed(monkeypatch, tmp_path):
    reports = tmp_path / "reports"
    _write_check_result(reports)
    record = {}
    _fake_check(monkeypatch, _cascade("introduce_new", 4), record)
    rc = gate.run_gate(make_args(reports_dir=str(reports), copr_chroot="c",
                                 constraint=">= 1.4.0"))
    assert rc == 0
    assert record["requirement"] == ">= 1.4.0"


# ─────────────────────────────────────────────
# main
# ─────────────────────────────────────────────

def test_main_missing_pkg_exits(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run_gate.py"])
    with pytest.raises(SystemExit) as ei:
        gate.main()
    assert ei.value.code == 2


def test_main_pkg_dir_overrides_reports_dir(tmp_path, monkeypatch):
    pkg_dir = tmp_path / "pkgdir"
    pkg_dir.mkdir()
    _write_check_result(pkg_dir)
    _fake_check(monkeypatch, _cascade("introduce_new", 4))
    monkeypatch.setattr(sys, "argv", ["run_gate.py", "--pkg", "testpkg",
                                      "--pkg-dir", str(pkg_dir),
                                      "--copr-chroot", "c"])
    assert gate.main() == 0
    assert (pkg_dir / "gate_result_testpkg.json").exists()


def test_main_full_run(tmp_path, monkeypatch, capsys):
    reports = tmp_path / "reports"
    _write_check_result(reports)
    _fake_check(monkeypatch, _cascade("introduce_new", 4))
    monkeypatch.setattr(sys, "argv", ["run_gate.py", "--pkg", "testpkg",
                                      "--reports-dir", str(reports),
                                      "--copr-chroot", "c"])
    assert gate.main() == 0
    assert '"status": "done"' in capsys.readouterr().out
