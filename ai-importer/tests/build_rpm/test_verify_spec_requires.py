"""verify_spec_requires.py — spec Requires provider 预检 + 反向完整性校验。

重点覆盖 rc=4 完整性门禁（ros2-numpy/python3-transforms3d 事故：agent 静默
丢弃 package.xml 声明的无 provider 依赖，provider 预检与 CI 均无法发现）。
完整性校验在 provider 查询（dnf 网络访问）之前执行，main() 的 rc=4 路径
不需要 mock dnf。
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

vsr = load_module("verify_spec_requires",
                  SCRIPT_DIRS["build_rpm"] / "verify_spec_requires.py")


# ─────────────────────────────────────────────
# _expected_deps
# ─────────────────────────────────────────────

def test_expected_deps_ros_names_normalized():
    analysis = {
        "ros_deps": ["ament-cmake", "tf_transformations"],
        "ros_deps_upstream": ["upstream_only"],
        "build_requires": ["cmake"],
        "unresolved": ["python3-transforms3d"],
    }
    assert vsr._expected_deps(analysis, "humble") == [
        "ros-humble-ament-cmake",
        "ros-humble-tf-transformations",   # 下划线归一为连字符
        "ros-humble-upstream-only",
        "cmake",
        "python3-transforms3d",
    ]


def test_expected_deps_default_distro_and_dedup():
    analysis = {"ros_deps": ["foo", "foo"], "unresolved": []}
    assert vsr._expected_deps(analysis, "") == ["ros-humble-foo"]


def test_expected_deps_empty_analysis():
    assert vsr._expected_deps({}, "humble") == []


# ─────────────────────────────────────────────
# _load_analysis
# ─────────────────────────────────────────────

def _mk_session(tmp_path, pkg="ros2-numpy", analysis=None):
    sd = tmp_path / "session"
    (sd / "reports").mkdir(parents=True, exist_ok=True)
    (sd / "session.json").write_text(
        json.dumps({"ros_distro": "humble"}), encoding="utf-8")
    if analysis is not None:
        (sd / "reports" / f"pre_check_{pkg}_analysis.json").write_text(
            json.dumps(analysis), encoding="utf-8")
    return sd


def test_load_analysis_hyphen_and_underscore(tmp_path):
    sd = _mk_session(tmp_path, pkg="ros2-numpy", analysis={"unresolved": ["x"]})
    assert vsr._load_analysis(sd, "ros2-numpy") == {"unresolved": ["x"]}
    # pkg 入参是下划线形态也能命中连字符文件名
    assert vsr._load_analysis(sd, "ros2_numpy") == {"unresolved": ["x"]}


def test_load_analysis_missing_returns_none(tmp_path):
    sd = _mk_session(tmp_path)
    assert vsr._load_analysis(sd, "ros2-numpy") is None
    assert vsr._load_analysis(sd, "") is None


def test_load_analysis_broken_json_returns_none(tmp_path, capsys):
    sd = _mk_session(tmp_path)
    (sd / "reports" / "pre_check_p_analysis.json").write_text("{bad", encoding="utf-8")
    assert vsr._load_analysis(sd, "p") is None
    assert "WARN" in capsys.readouterr().err


# ─────────────────────────────────────────────
# _load_waivers
# ─────────────────────────────────────────────

def test_load_waivers_requires_reason(tmp_path, capsys):
    sd = _mk_session(tmp_path)
    pkg_dir = sd / "pkgs" / "p"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "waived_deps.txt").write_text(
        "# 注释行\n"
        "python3-transforms3d # 仅示例代码路径用到，运行时无需\n"
        "python3-foo\n"                      # 无理由 → 不予认可
        "  # 纯注释\n"
        "\n",
        encoding="utf-8")
    waived = vsr._load_waivers(sd, "p")
    assert waived == {"python3-transforms3d"}
    assert "无理由" in capsys.readouterr().err


def test_load_waivers_no_file(tmp_path):
    sd = _mk_session(tmp_path)
    assert vsr._load_waivers(sd, "p") == set()
    assert vsr._load_waivers(sd, "") == set()


# ─────────────────────────────────────────────
# _completeness_check
# ─────────────────────────────────────────────

ANALYSIS = {
    "ros_deps": ["ament-cmake"],
    "ros_deps_upstream": [],
    "build_requires": ["cmake"],
    "unresolved": ["python3-numpy", "python3-transforms3d"],
}

CAPS_FULL = ["ros-humble-ament-cmake", "cmake",
             "python3-numpy", "python3-transforms3d"]


def test_completeness_all_covered(tmp_path):
    sd = _mk_session(tmp_path, pkg="p", analysis=ANALYSIS)
    assert vsr._completeness_check(sd, "p", "humble", CAPS_FULL) == []


def test_completeness_dropped_dep_detected(tmp_path):
    sd = _mk_session(tmp_path, pkg="p", analysis=ANALYSIS)
    caps = [c for c in CAPS_FULL if c != "python3-transforms3d"]
    assert vsr._completeness_check(sd, "p", "humble", caps) == ["python3-transforms3d"]


def test_completeness_waiver_covers_drop(tmp_path):
    sd = _mk_session(tmp_path, pkg="p", analysis=ANALYSIS)
    pkg_dir = sd / "pkgs" / "p"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "waived_deps.txt").write_text(
        "python3-transforms3d # 上游仅示例用到\n", encoding="utf-8")
    caps = [c for c in CAPS_FULL if c != "python3-transforms3d"]
    assert vsr._completeness_check(sd, "p", "humble", caps) == []


def test_completeness_no_analysis_skips(tmp_path):
    sd = _mk_session(tmp_path)
    assert vsr._completeness_check(sd, "p", "humble", []) == []


# ─────────────────────────────────────────────
# main() rc=4 路径（完整性校验先于 dnf provider 查询，无需 mock 网络）
# ─────────────────────────────────────────────

SPEC_FULL = """Name:           ros2-numpy
Version:        2.0.12
BuildRequires:  cmake
BuildRequires:  ros-%{ros_distro}-ament-cmake
Requires:       python3-numpy
Requires:       python3-transforms3d
"""

SPEC_DROPPED = """Name:           ros2-numpy
Version:        2.0.12
BuildRequires:  cmake
BuildRequires:  ros-%{ros_distro}-ament-cmake
Requires:       python3-numpy
"""


def _run_main(monkeypatch, tmp_path, spec_text):
    sd = _mk_session(tmp_path, pkg="ros2-numpy", analysis=ANALYSIS)
    spec = tmp_path / "ros2-numpy.spec"
    spec.write_text(spec_text, encoding="utf-8")
    monkeypatch.setattr("sys.argv", [
        "verify_spec_requires.py", str(spec),
        "--session-dir", str(sd), "--pkg", "ros2-numpy",
    ])
    return vsr.main(), sd


def test_main_rc4_on_dropped_dep(monkeypatch, tmp_path, capsys):
    rc, _ = _run_main(monkeypatch, tmp_path, SPEC_DROPPED)
    assert rc == 4
    err = capsys.readouterr().err
    assert "python3-transforms3d" in err and "waived_deps.txt" in err


def test_main_rc4_waived_dep_passes_completeness(monkeypatch, tmp_path):
    # 豁免后完整性通过，进入 provider 查询——repoquery 不可用/无结果按 WARN
    # 降级或正常走 dnf，此处只断言不再返回 4
    rc, sd = _run_main(monkeypatch, tmp_path, SPEC_DROPPED)
    assert rc == 4  # 先确认无豁免时确实拦截
    pkg_dir = sd / "pkgs" / "ros2-numpy"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "waived_deps.txt").write_text(
        "python3-transforms3d # 测试豁免\n", encoding="utf-8")
    rc2, _ = _run_main(monkeypatch, tmp_path, SPEC_DROPPED)
    assert rc2 != 4


# ─────────────────────────────────────────────
# provider 查询失败(unknown)的诚实降级
# ─────────────────────────────────────────────

def _run_provider_path(monkeypatch, tmp_path, provider_fn, extra_argv=()):
    """跳过完整性校验(不放分析文件),mock dnf 相关外部依赖后跑 main()。"""
    sd = _mk_session(tmp_path)  # 无 analysis → 完整性校验跳过
    spec = tmp_path / "p.spec"
    spec.write_text("Name: p\nRequires: dep-a\nRequires: dep-b\n", encoding="utf-8")
    monkeypatch.setattr(vsr, "_repo_flags",
                        lambda _sd: (["--disablerepo=*"], "fake-chroot"))
    monkeypatch.setattr(vsr, "_warm_metadata", lambda _flags: True)
    monkeypatch.setattr(vsr, "_has_provider", provider_fn)
    monkeypatch.setattr("sys.argv", [
        "verify_spec_requires.py", str(spec),
        "--session-dir", str(sd), "--pkg", "p", *extra_argv,
    ])
    return vsr.main()


def test_provider_all_unknown_degrades_with_honest_warn(monkeypatch, tmp_path, capsys):
    """查询全挂(dnf 缺失/网络全断):rc=0 降级放行,但绝不允许报"全部有 provider"。"""
    rc = _run_provider_path(monkeypatch, tmp_path, lambda cap, flags: None)
    assert rc == 0
    out = capsys.readouterr()
    assert "未能完成 provider 验证" in out.err
    assert "dep-a" in out.err and "dep-b" in out.err
    assert "全部有 provider" not in out.out


def test_provider_missing_and_unknown_both_reported(monkeypatch, tmp_path, capsys):
    """确定缺失 + 未验证并存:rc=1 报缺失,stderr 同时列出未验证项。"""
    def fake(cap, flags):
        return False if cap == "dep-a" else None
    rc = _run_provider_path(monkeypatch, tmp_path, fake)
    assert rc == 1
    err = capsys.readouterr().err
    assert "dep-a" in err            # 缺失
    assert "未能验证" in err and "dep-b" in err  # 未验证


def test_provider_all_present_ok(monkeypatch, tmp_path, capsys):
    rc = _run_provider_path(monkeypatch, tmp_path, lambda cap, flags: True)
    assert rc == 0
    assert "全部有 provider" in capsys.readouterr().out
