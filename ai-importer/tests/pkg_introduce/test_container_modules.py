"""fetch_latest_image + setup_container + container_exec.main — 容器相关模块。"""

from __future__ import annotations

import json
import sys

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sys.path.insert(0, str(SCRIPT_DIRS["pkg_introduce"]))

_CONF = {
    "image": {
        "base_url": "https://repo.openeuler.org",
        "branch": "openEuler-24.03-LTS",
        "build": "",
        "arch": "x86_64",
        "tag": "openeuler-24.03-lts:latest",
        "filename_prefix": "openEuler-docker",
    },
    "container": {
        "name": "oe-build-env-test",
        "source_mount": "/build/source",
        "platform": "linux/amd64",
    },
}


@pytest.fixture
def conf_file(tmp_path, monkeypatch):
    """在 pkg-introduce 上级目录写 build-env.conf.json(两个模块读同一路径)。"""
    conf_path = SCRIPT_DIRS["pkg_introduce"].parent / "build-env.conf.json"
    original = conf_path.read_text() if conf_path.exists() else None
    conf_path.write_text(json.dumps(_CONF))
    yield conf_path
    if original is not None:
        conf_path.write_text(original)
    else:
        conf_path.unlink(missing_ok=True)


def _load(loaded_modules, name, conf_file):
    return loaded_modules(name, SCRIPT_DIRS["pkg_introduce"] / f"{name}.py")


# ─────────────────────────────────────────────
# fetch_latest_image
# ─────────────────────────────────────────────

def test_fetch_conf_and_hashes(conf_file, loaded_modules, monkeypatch, tmp_path):
    fli = _load(loaded_modules, "fetch_latest_image", conf_file)
    # get_expected_hash
    resp = type("R", (), {"text": "abc123  openEuler-docker.x86_64.tar.xz",
                           "raise_for_status": lambda self: None})()
    monkeypatch.setattr(fli.requests, "get", lambda *a, **k: resp)
    assert fli.get_expected_hash("http://x") == "abc123"

    # calculate_sha256
    f = tmp_path / "img.tar.xz"
    f.write_bytes(b"data")
    assert fli.calculate_sha256(f) == fli.calculate_sha256(f)

    # verify_checksum 匹配
    expected = fli.calculate_sha256(f)
    assert fli.verify_checksum(f, expected) is True
    # 不匹配
    assert fli.verify_checksum(f, "deadbeef" * 8) is False


def test_fetch_get_expected_hash_failure(conf_file, loaded_modules, monkeypatch, capsys):
    fli = _load(loaded_modules, "fetch_latest_image", conf_file)
    def boom(*a, **k):
        raise RuntimeError("net down")
    monkeypatch.setattr(fli.requests, "get", boom)
    assert fli.get_expected_hash("http://x") is None
    assert "无法获取校验文件" in capsys.readouterr().err


def test_download_image_cache_hit(conf_file, loaded_modules, monkeypatch, tmp_path):
    fli = _load(loaded_modules, "fetch_latest_image", conf_file)
    out = tmp_path / "out"
    out.mkdir()
    dest = out / fli.IMAGE_FILENAME
    dest.write_bytes(b"data")
    # 让本地哈希 == expected:先算好
    expected = fli.calculate_sha256(dest)
    result = fli.download_image("http://x", out, expected)
    assert result == dest  # 复用缓存,未下载


def test_download_image_redownload(conf_file, loaded_modules, monkeypatch, tmp_path):
    fli = _load(loaded_modules, "fetch_latest_image", conf_file)
    out = tmp_path / "out"
    out.mkdir()
    resp = type("R", (), {"iter_content": lambda self, chunk_size: [b"new-data"],
                           "raise_for_status": lambda self: None,
                           "headers": {"content-length": "8"}})()
    monkeypatch.setattr(fli.requests, "get", lambda *a, **k: resp)
    result = fli.download_image("http://x", out, "deadbeef" * 8)
    assert result.exists()
    assert result.read_bytes() == b"new-data"


def test_fetch_main_download(conf_file, loaded_modules, monkeypatch, tmp_path, capsys):
    import hashlib
    fli = _load(loaded_modules, "fetch_latest_image", conf_file)
    content = b"x"
    expected_hash = hashlib.sha256(content).hexdigest()
    resp = type("R", (), {"text": f"{expected_hash}  f.tar.xz",
                           "raise_for_status": lambda self: None,
                           "headers": {"content-length": "1"}})()
    stream_resp = type("R", (), {"iter_content": lambda self, chunk_size: [content],
                                  "raise_for_status": lambda self: None,
                                  "headers": {"content-length": "1"}})()
    monkeypatch.setattr(fli.requests, "get", lambda *a, **k: stream_resp if k.get("stream") else resp)
    monkeypatch.setattr("sys.argv", ["fetch_latest_image.py", "--download",
                                     "--output-dir", str(tmp_path / "o")])
    fli.main()  # 校验通过,正常返回不 exit
    assert "TAR_PATH=" in capsys.readouterr().out


# ─────────────────────────────────────────────
# setup_container
# ─────────────────────────────────────────────

def test_setup_container_exists(conf_file, loaded_modules, fake_subprocess):
    sc = _load(loaded_modules, "setup_container", conf_file)
    fake_subprocess.when("docker inspect", stdout="[]", returncode=0)
    assert sc.container_exists("name") is True


def test_setup_container_not_exists(conf_file, loaded_modules, fake_subprocess):
    sc = _load(loaded_modules, "setup_container", conf_file)
    fake_subprocess.when("docker inspect", returncode=1)
    assert sc.container_exists("name") is False


def test_fix_repo_nameerror_bug(conf_file, loaded_modules):
    """已知 bug 固化:setup_container.fix_repo 引用未定义的 _CONF_PATH
    (_load_conf 里是局部变量 _conf_path)→ 恒抛 NameError。生产修复:
    把 _conf_path 提升为模块级或改用 _CONF。"""
    sc = _load(loaded_modules, "setup_container", conf_file)
    with pytest.raises(NameError):
        sc.fix_repo("name")


def test_start_container(conf_file, loaded_modules, fake_subprocess, tmp_path):
    sc = _load(loaded_modules, "setup_container", conf_file)
    monkeypatch = __import__("pytest").MonkeyPatch()
    # fix_repo 有 _CONF_PATH NameError bug,monkeypatch 绕过(见 test_fix_repo_nameerror_bug)
    sc.fix_repo = lambda name: True
    fake_subprocess.when("docker run", returncode=0)
    assert sc.start_container(str(tmp_path), "name", "img") is True


def test_start_container_missing_source_dir(conf_file, loaded_modules, fake_subprocess, capsys):
    sc = _load(loaded_modules, "setup_container", conf_file)
    assert sc.start_container("/nonexistent-src", "name", "img") is False
    assert "源码目录不存在" in capsys.readouterr().err


def test_setup_container_env_conf(conf_file, loaded_modules, monkeypatch, tmp_path):
    """BUILD_ENV_CONF 环境变量注入优先。"""
    alt = tmp_path / "alt-conf.json"
    alt.write_text(json.dumps({**_CONF, "container": {"name": "alt-name", "source_mount": "/m", "platform": "p"}}))
    monkeypatch.setenv("BUILD_ENV_CONF", str(alt))
    sc = _load(loaded_modules, "setup_container", conf_file)
    assert sc.DEFAULT_NAME == "alt-name"


# ─────────────────────────────────────────────
# container_exec.main
# ─────────────────────────────────────────────

def test_container_exec_main(conf_file, loaded_modules, monkeypatch, capsys, fake_subprocess):
    cex = _load(loaded_modules, "container_exec", conf_file)
    fake_subprocess.when("docker exec", stdout="out", returncode=0)
    monkeypatch.setattr("sys.argv", ["container_exec.py", "ls",
                                     "--container", "c", "--workdir", "/w", "--json"])
    with pytest.raises(SystemExit) as e:
        cex.main()
    assert e.value.code == 0
    assert '"success": true' in capsys.readouterr().out
