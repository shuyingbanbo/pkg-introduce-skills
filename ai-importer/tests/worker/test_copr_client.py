"""copr_client.py — COPR HTTP API 封装(requests 全部 mock)。

copr_client.py 顶层 import requests/urllib3 并把 COPR_API_URL / VERIFY_SSL
固化为模块常量,需要自定义 env 的用例通过 _load_cc() 重载模块。
"""

from __future__ import annotations

import pytest
import requests as real_requests
import urllib3

from tests.conftest import SCRIPT_DIRS


class FakeResp:
    def __init__(self, ok=True, status_code=200, text="", json_data=None,
                 raise_exc=None):
        self._ok = ok
        self.status_code = status_code
        self.text = text
        self._json = json_data
        self._raise = raise_exc

    @property
    def ok(self):
        return self._ok

    def json(self):
        return self._json

    def raise_for_status(self):
        if self._raise is not None:
            raise self._raise


class FakeRequests:
    def __init__(self):
        self.post_calls = []
        self.get_calls = []
        self.post_resp = None
        self.get_resp = None

    def post(self, *args, **kwargs):
        self.post_calls.append((args, kwargs))
        return self.post_resp

    def get(self, *args, **kwargs):
        self.get_calls.append((args, kwargs))
        return self.get_resp


def _load_cc(loaded_modules, monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return loaded_modules("copr_client", SCRIPT_DIRS["worker"] / "copr_client.py")


@pytest.fixture
def cc(loaded_modules, monkeypatch):
    monkeypatch.delenv("COPR_API_URL", raising=False)
    monkeypatch.delenv("COPR_API_VERIFY_SSL", raising=False)
    return _load_cc(loaded_modules, monkeypatch)


def _fake_requests(cc_mod, monkeypatch, post_resp=None, get_resp=None):
    fr = FakeRequests()
    fr.post_resp = post_resp
    fr.get_resp = get_resp
    monkeypatch.setattr(cc_mod, "requests", fr)
    return fr


def _make_srpm(tmp_path):
    p = tmp_path / "pkg-1.0.src.rpm"
    p.write_bytes(b"dummy-srpm")
    return p


# ─────────────────────────────────────────────
# 模块常量
# ─────────────────────────────────────────────

def test_auth_returns_tuple(cc):
    assert cc._auth("user", "token") == ("user", "token")


def test_default_constants(cc):
    assert cc.COPR_API_URL == "http://copr-frontend:5000"
    assert cc.VERIFY_SSL is True


@pytest.mark.parametrize("raw,expected", [
    ("true", True),
    ("1", True),
    ("yes", True),
    ("", True),          # 空字符串不匹配 false/0/no → 视为开启
    ("false", False),
    ("0", False),
    ("no", False),
    ("False", False),
])
def test_verify_ssl_parsing(loaded_modules, monkeypatch, raw, expected):
    m = _load_cc(loaded_modules, monkeypatch, COPR_API_VERIFY_SSL=raw)
    assert m.VERIFY_SSL is expected


def test_custom_url_and_ssl_off(loaded_modules, monkeypatch, tmp_path):
    warned = []
    monkeypatch.setattr(urllib3, "disable_warnings", lambda *a: warned.append(1))
    m = _load_cc(loaded_modules, monkeypatch,
                 COPR_API_URL="https://copr.example.com",
                 COPR_API_VERIFY_SSL="false")
    assert m.VERIFY_SSL is False
    assert warned == [1]  # 关闭校验时禁用了 urllib3 告警
    srpm = _make_srpm(tmp_path)
    fr = _fake_requests(m, monkeypatch, post_resp=FakeResp(ok=True, json_data={"id": 3}))
    bid, err = m.submit_srpm_upload("o", "p", srpm, "u", "t")
    assert (bid, err) == (3, None)
    (url,), kw = fr.post_calls[0]
    assert url == "https://copr.example.com/api/v3/build/create/upload"
    assert kw["verify"] is False


# ─────────────────────────────────────────────
# submit_srpm_upload
# ─────────────────────────────────────────────

@pytest.mark.parametrize(
    "chroots,ok,status_code,text,json_data,exp_bid,exp_err,exp_chroots", [
        (["c1", "c2"], True, 200, "", {"id": 42}, 42, None, ["c1", "c2"]),
        (None, True, 200, "", {"id": 7}, 7, None, []),
        ([], True, 200, "", {"id": 1}, 1, None, []),      # 空列表视为无 chroots
        (["c1"], False, 500, "x" * 400, None, None,
         "HTTP 500: " + "x" * 300, ["c1"]),               # 错误文本截断 300
        (None, False, 403, "forbidden", None, None, "HTTP 403: forbidden", []),
        # 实际行为:resp.ok 但响应无 id 字段时按 (None, None) 返回,不报错
        (None, True, 200, "", {}, None, None, []),
    ])
def test_submit_srpm_upload(cc, monkeypatch, tmp_path, chroots, ok, status_code,
                            text, json_data, exp_bid, exp_err, exp_chroots):
    srpm = _make_srpm(tmp_path)
    fr = _fake_requests(cc, monkeypatch, post_resp=FakeResp(
        ok=ok, status_code=status_code, text=text, json_data=json_data))
    bid, err = cc.submit_srpm_upload("owner1", "proj1", srpm, "user", "tok",
                                     chroots=chroots)
    assert bid == exp_bid
    assert err == exp_err
    (url,), kw = fr.post_calls[0]
    assert url == "http://copr-frontend:5000/api/v3/build/create/upload"
    assert kw["auth"] == ("user", "tok")
    assert kw["timeout"] == 120
    assert kw["verify"] is True
    assert ("ownername", "owner1") in kw["data"]
    assert ("projectname", "proj1") in kw["data"]
    got_chroots = [v for k, v in kw["data"] if k == "chroots"]
    assert got_chroots == exp_chroots
    fname, _, ctype = kw["files"]["pkgs"]
    assert fname == "pkg-1.0.src.rpm"
    assert ctype == "application/x-rpm"


def test_submit_srpm_upload_missing_file_propagates(cc, monkeypatch, tmp_path):
    fr = _fake_requests(cc, monkeypatch)
    with pytest.raises(FileNotFoundError):
        cc.submit_srpm_upload("o", "p", tmp_path / "missing.src.rpm", "u", "t")
    assert fr.post_calls == []


# ─────────────────────────────────────────────
# submit_scm_build
# ─────────────────────────────────────────────

@pytest.mark.parametrize(
    "committish,spec,ok,status_code,text,json_data,exp_bid,exp_err", [
        ("abc123", "%name x", True, 200, "", {"id": 1}, 1, None),
        ("", "", True, 200, "", {"id": 2}, 2, None),       # 默认空 committish/spec
        ("", "", False, 403, "no access", None, None, "HTTP 403: no access"),
        ("", "", True, 200, "", {}, None, None),           # ok 但无 id(同 upload 行为)
    ])
def test_submit_scm_build(cc, monkeypatch, committish, spec, ok, status_code, text,
                          json_data, exp_bid, exp_err):
    fr = _fake_requests(cc, monkeypatch, post_resp=FakeResp(
        ok=ok, status_code=status_code, text=text, json_data=json_data))
    bid, err = cc.submit_scm_build("o", "p", "https://gitee.com/o/r.git", "u", "t",
                                   committish=committish, spec=spec)
    assert bid == exp_bid
    assert err == exp_err
    (url,), kw = fr.post_calls[0]
    assert url == "http://copr-frontend:5000/api/v3/build/create/scm"
    assert kw["auth"] == ("u", "t")
    assert kw["timeout"] == 30
    payload = kw["json"]
    assert payload == {
        "ownername": "o",
        "projectname": "p",
        "scmtype": "git",
        "clone_url": "https://gitee.com/o/r.git",
        "committish": committish,
        "spec": spec,
        "srpm_build_method": "rpkg",
    }


# ─────────────────────────────────────────────
# get_build
# ─────────────────────────────────────────────

def test_get_build_returns_json(cc, monkeypatch):
    fr = _fake_requests(cc, monkeypatch,
                        get_resp=FakeResp(json_data={"state": "succeeded", "id": 5}))
    assert cc.get_build(5, "u", "t") == {"state": "succeeded", "id": 5}
    (url,), kw = fr.get_calls[0]
    assert url == "http://copr-frontend:5000/api_3/build/5"
    assert kw["auth"] == ("u", "t")
    assert kw["timeout"] == 10
    assert kw["verify"] is True


def test_get_build_propagates_http_error(cc, monkeypatch):
    _fake_requests(cc, monkeypatch, get_resp=FakeResp(
        raise_exc=real_requests.exceptions.HTTPError("500 Server Error")))
    with pytest.raises(real_requests.exceptions.HTTPError):
        cc.get_build(5, "u", "t")


# ─────────────────────────────────────────────
# poll_build_until_done
# ─────────────────────────────────────────────

@pytest.mark.parametrize("state", ["succeeded", "failed", "canceled", "skipped"])
def test_poll_terminal_immediately(cc, monkeypatch, state):
    monkeypatch.setattr(cc, "get_build", lambda bid, l, t: {"state": state})
    sleeps = []
    monkeypatch.setattr(cc.time, "sleep", sleeps.append)
    log = []
    assert cc.poll_build_until_done(5, "u", "t", log.append) == state
    assert log == ["  构建状态: {}".format(state)]
    assert sleeps == []  # 终态立即返回,不 sleep


def test_poll_transitions_logged_once(cc, monkeypatch):
    states = iter(["building", "building", "failed"])
    monkeypatch.setattr(cc, "get_build", lambda *a: {"state": next(states)})
    log = []
    assert cc.poll_build_until_done(5, "u", "t", log.append) == "failed"
    # 相同状态不重复记日志
    assert log == ["  构建状态: building", "  构建状态: failed"]


def test_poll_exception_then_timeout(cc, monkeypatch):
    def get_build(*a):
        raise RuntimeError("api down")

    monkeypatch.setattr(cc, "get_build", get_build)
    sleeps = []
    monkeypatch.setattr(cc.time, "sleep", sleeps.append)
    values = iter([0, 100, 10 ** 9])  # deadline=3600;第一次轮询进入,第二次超时
    monkeypatch.setattr(cc.time, "time", lambda: next(values))
    log = []
    state = cc.poll_build_until_done(5, "u", "t", log.append, max_wait=3600, interval=5)
    assert state == "failed"
    assert sleeps == [5]
    assert "轮询出错" in log[0]
    assert log[-1] == "  构建超时（超过 1 小时）"


def test_poll_timeout_without_polling(cc, monkeypatch):
    log = []
    state = cc.poll_build_until_done(5, "u", "t", log.append, max_wait=0, interval=10)
    assert state == "failed"
    assert log == ["  构建超时（超过 1 小时）"]
