"""流程尾模块组:finalize_dependency_result / prepare_build_inputs /
run_build_rpm_flow / gen_ros_upstream_list / verify_ros_spec_deps /
gen_build_env_conf / container_exec(参数校验 + 纯逻辑 + 编排主路径)。"""

from __future__ import annotations

import json
import sys

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sys.path.insert(0, str(SCRIPT_DIRS["build_rpm"]))
sys.path.insert(0, str(SCRIPT_DIRS["pkg_introduce"]))

fdr = load_module("finalize_dependency_result", SCRIPT_DIRS["build_rpm"] / "finalize_dependency_result.py")
pbi = load_module("prepare_build_inputs", SCRIPT_DIRS["build_rpm"] / "prepare_build_inputs.py")
rbf = load_module("run_build_rpm_flow", SCRIPT_DIRS["build_rpm"] / "run_build_rpm_flow.py")
gbu = load_module("gen_ros_upstream_list", SCRIPT_DIRS["build_rpm"] / "gen_ros_upstream_list.py")
vrd = load_module("verify_ros_spec_deps", SCRIPT_DIRS["build_rpm"] / "verify_ros_spec_deps.py")
gbc = load_module("gen_build_env_conf", SCRIPT_DIRS["pkg_introduce"] / "gen_build_env_conf.py")
cex = load_module("container_exec", SCRIPT_DIRS["pkg_introduce"] / "container_exec.py")


# ─────────────────────────────────────────────
# gen_build_env_conf.generate
# ─────────────────────────────────────────────

def test_generate_build_env_conf():
    result = gbc.generate("openEuler-24.03-LTS", "123")
    assert result["container"]["name"] == "oe-build-env-123"
    assert result["image"]["branch"] == "openEuler-24.03-LTS"
    assert result["image"]["arch"] == "x86_64"


def test_generate_unknown_version_exits(monkeypatch, capsys):
    with pytest.raises(SystemExit) as e:
        gbc.generate("unknown-version", "1")
    assert e.value.code == 1
    assert "未知 OE 版本" in capsys.readouterr().err


# ─────────────────────────────────────────────
# container_exec
# ─────────────────────────────────────────────

def test_container_exec_cmd(fake_subprocess):
    fake_subprocess.when("docker exec", stdout="ok output", returncode=0)
    result = cex.exec_cmd("container", "cmd", "/workdir")
    assert result["success"] is True
    assert result["returncode"] == 0
    assert result["stdout"] == "ok output"


def test_container_exec_failure(fake_subprocess):
    fake_subprocess.when("docker exec", returncode=1, stderr="boom")
    result = cex.exec_cmd("c", "cmd", "/w")
    assert result["success"] is False
    assert result["stderr"] == "boom"


# ─────────────────────────────────────────────
# prepare_build_inputs
# ─────────────────────────────────────────────

def test_detect_lang(tmp_path):
    (tmp_path / "go.mod").write_text("x")
    assert pbi.detect_lang(tmp_path) == "go"  # go.mod 优先
    (tmp_path / "Cargo.toml").write_text("x")
    assert pbi.detect_lang(tmp_path) == "go"
    (tmp_path / "go.mod").unlink()
    assert pbi.detect_lang(tmp_path) == "rust"
    (tmp_path / "Cargo.toml").unlink()
    assert pbi.detect_lang(tmp_path) == "other"


def test_write_cargo_config(tmp_path):
    pbi._write_cargo_config(tmp_path)
    content = (tmp_path / ".cargo" / "config.toml").read_text()
    assert "[source.crates-io]" in content
    assert "replace-with" in content


def test_ensure_vendor_rust(tmp_path, fake_subprocess):
    (tmp_path / "Cargo.toml").write_text("x")
    fake_subprocess.when(lambda s: "cargo vendor" in s, returncode=0)
    pbi.ensure_vendor(tmp_path, "container")
    assert fake_subprocess.called_with("cargo vendor")


def test_create_tarball(tmp_path, monkeypatch):
    """create_tarball 硬编码 /tmp 目录,monkeypatch 到 tmp_path 避免污染。"""
    import tarfile
    monkeypatch.setattr(pbi, "Path", lambda p: __import__("pathlib").Path(
        str(p).replace("/tmp", str(tmp_path))))
    src = tmp_path / "src"
    src.mkdir()
    (src / "f.txt").write_text("x")
    tarball = pbi.create_tarball("pkg", "1.0", src)
    assert tarball.name == "pkg-1.0.tar.gz"
    with tarfile.open(tarball) as tf:
        names = tf.getnames()
    assert any("pkg-1.0" in n for n in names)


# ─────────────────────────────────────────────
# finalize_dependency_result
# ─────────────────────────────────────────────

def test_result_path(tmp_path):
    p = fdr.result_path("pkg", str(tmp_path))
    assert p == tmp_path / "pkg_introduce_result_pkg.json"


def test_main_missing_args(monkeypatch):
    monkeypatch.setattr("sys.argv", ["finalize_dependency_result.py"])
    with pytest.raises(SystemExit):
        fdr.main()


# ─────────────────────────────────────────────
# run_build_rpm_flow
# ─────────────────────────────────────────────

def test_build_result_payload():
    payload = rbf.build_result_payload(
        pkgname="pkg", lang="python", version="1.0", requested_version="1.0",
        depth=0, status="success", action="built_new", reason="ok",
        precheck_summary={}, dependency_resolution={}, artifacts={},
    )
    assert payload["pkgname"] == "pkg"
    assert payload["status"] == "success"
    assert payload["failure"]["failure_type"] == ""


def test_reconcile_pending_with_registry(tmp_path):
    (tmp_path / "dep_registry.json").write_text(json.dumps({
        "dep1": {"status": "evaluate_done"},
    }))
    pending = [{"name": "dep1", "constraint": ">=1.0"}, {"name": "dep2"}]
    result = rbf.reconcile_pending_with_registry(pending, tmp_path)
    names = [d["name"] for d in result]
    assert "dep1" in names and "dep2" in names


def test_main_missing_positional_args(monkeypatch):
    monkeypatch.setattr("sys.argv", ["run_build_rpm_flow.py"])
    with pytest.raises(SystemExit):
        rbf.main()


# ─────────────────────────────────────────────
# gen_ros_upstream_list
# ─────────────────────────────────────────────

def test_load_distribution_local_yaml(tmp_path):
    y = tmp_path / "dist.yaml"
    y.write_text("repositories:\n  ament_cmake:\n    release:\n      tags:\n        release: release/humble/ament_cmake\n")
    dist = gbu.load_distribution("humble", str(y))
    assert "repositories" in dist


def test_gen_rows():
    dist = {
        "repositories": {
            "z_pkg": {"release": {"version": "1.0.0", "tags": {"release": "x"}, "url": "https://u"}},
            "a_pkg": {"release": {"version": "2.0.0", "tags": {"release": "y"}, "url": "https://v"}},
        }
    }
    rows = gbu.gen_rows(dist)
    # 按包名排序(下划线归一连字符)
    assert [r[0] for r in rows] == ["a-pkg", "z-pkg"]


# ─────────────────────────────────────────────
# verify_ros_spec_deps
# ─────────────────────────────────────────────

def test_session_distro(tmp_path):
    (tmp_path / "session.json").write_text(json.dumps({"ros_distro": "jazzy"}))
    assert vrd._session_distro(str(tmp_path)) == "jazzy"


def test_session_distro_missing(tmp_path):
    assert vrd._session_distro(str(tmp_path)) == ""


def test_verify_ros_spec_deps_missing_spec(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["verify_ros_spec_deps.py", str(tmp_path / "nope.spec")])
    rc = vrd.main()
    assert rc == 2
    assert "spec 不存在" in capsys.readouterr().err


def test_verify_ros_spec_deps_no_projects_list(tmp_path, monkeypatch, capsys):
    """ros-projects.list 缺失(load_projects 空)→ 降级放行 rc 0。"""
    spec = tmp_path / "pkg.spec"
    spec.write_text("Name: ros-humble-foo\n")
    monkeypatch.setattr("sys.argv", ["verify_ros_spec_deps.py", str(spec)])
    monkeypatch.setattr(vrd, "load_projects", lambda d: {})
    rc = vrd.main()
    assert rc == 0
    assert "跳过校验" in capsys.readouterr().err
