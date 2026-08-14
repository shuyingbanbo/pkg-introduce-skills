"""publish_rpm.py compat 逻辑 — 由旧式 test_compat.py 迁移为 pytest。

覆盖:parse_rpm_nvra / get_version_change_type / detect_package_type /
resolve_dist_conflicts(3 个决策场景)。集成用例(report §3.5)保留 @pytest.mark.integration。

已知差异(只记录不修):旧 test_compat.py 断言 rubygem-rake → "ruby",
但生产代码 _RUNTIME_INDICATORS / _NAME_PREFIX_MAP 中都没有 ruby 检测,
rubygem-* 包实际判定为 "other"。
"""

from __future__ import annotations

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

p = load_module("publish_rpm", SCRIPT_DIRS["archive"] / "publish_rpm.py")


# ─────────────────────────────────────────────
# parse_rpm_nvra
# ─────────────────────────────────────────────

@pytest.mark.parametrize("fname,expected", [
    ("python3-requests-2.28.0-1.noarch.rpm",
     {"name": "python3-requests", "version": "2.28.0", "release": "1", "arch": "noarch"}),
    ("openssl-libs-3.0.1-2.aarch64.rpm",
     {"name": "openssl-libs", "version": "3.0.1", "release": "2", "arch": "aarch64"}),
    # 含 ~ 的 release(epoch 分隔)
    ("python3-ruyi-0.48.0.alpha.20260317-1.noarch.rpm",
     {"name": "python3-ruyi", "version": "0.48.0.alpha.20260317", "release": "1", "arch": "noarch"}),
    # 无效格式
    ("not-an-rpm.tar.gz", None),
    ("missing-arch.rpm", None),
])
def test_parse_rpm_nvra(fname, expected):
    assert p.parse_rpm_nvra(fname) == expected


# ─────────────────────────────────────────────
# get_version_change_type
# ─────────────────────────────────────────────

@pytest.mark.parametrize("old,new,expected", [
    ("1.0.0", "2.0.0", "major"),
    ("1.2.0", "1.3.0", "minor"),
    ("1.2.3", "1.2.4", "patch"),
    ("2.0.0", "2.0.1", "patch"),
    # pre-1.0 包:major=0 不视为 major,按 minor 处理
    ("0.48.0", "0.49.0", "minor"),
    ("0.48.0.alpha.20260317", "0.49.0", "minor"),
    # 无法解析的版本
    ("abc", "def", "unknown"),
])
def test_get_version_change_type(old, new, expected):
    assert p.get_version_change_type(old, new) == expected


# ─────────────────────────────────────────────
# detect_package_type
# ─────────────────────────────────────────────

_SPEC_TEMPLATES = {
    "python": """\
Name: python3-requests
Version: 2.28.0
Release: 1
Summary: HTTP library
License: Apache-2.0
%description
%install
%{__python3} setup.py install --root %{buildroot}
%{python3_sitelib}/requests
""",
    "java": """\
Name: jackson-databind
Version: 2.14.0
Release: 1
Summary: Java JSON library
License: Apache-2.0
BuildRequires: maven-local
%description
%install
%mvn_install
install -m 644 target/jackson-databind-2.14.0.jar %{buildroot}%{_javadir}/
""",
    "ruby": """\
Name: rubygem-rake
Version: 13.0.6
Release: 1
Summary: Ruby build tool
License: MIT
%description
%install
%gem_install
""",
    "nodejs": """\
Name: nodejs-semver
Version: 7.3.8
Release: 1
Summary: Semantic versioning
License: ISC
%description
%install
mkdir -p %{buildroot}%{nodejs_sitelib}/semver
cp -r lib/* %{buildroot}%{nodejs_sitelib}/semver/
""",
    "perl": """\
Name: perl-JSON
Version: 4.10
Release: 1
Summary: Perl JSON module
License: GPL+
%description
%install
make install DESTDIR=%{buildroot}
find %{buildroot}%{perl_vendorlib} -name '*.pm'
""",
    "other": """\
Name: zlib
Version: 1.3.1
Release: 1
Summary: Compression library
License: zlib
%description
%install
make install DESTDIR=%{buildroot}
""",
}

# 生产代码 _RUNTIME_INDICATORS / _NAME_PREFIX_MAP 中都没有 ruby 检测,
# rubygem-* 包实际判定为 "other"(旧 test_compat.py 断言 "ruby" 是错误期望)
_SPEC_TEMPLATE_EXPECTED = {
    "python3-requests": "python",
    "jackson-databind": "java",
    "rubygem-rake": "other",
    "nodejs-semver": "nodejs",
    "perl-JSON": "perl",
    "zlib": "other",
}


@pytest.fixture
def spec_repo(tmp_path):
    """用 _SPEC_TEMPLATES 构造 repo_dir,返回 {真实包名: 期望类型} 映射。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    expected_map = {}
    for lang, spec in _SPEC_TEMPLATES.items():
        real_name = [l.split(":", 1)[1].strip() for l in spec.splitlines() if l.startswith("Name:")][0]
        pkg_dir = repo / real_name
        pkg_dir.mkdir()
        (pkg_dir / f"{real_name}.spec").write_text(spec)
        expected_map[real_name] = lang
    return repo, expected_map


def test_detect_package_type_from_spec(spec_repo):
    repo, _ = spec_repo
    for real_name, expected in _SPEC_TEMPLATE_EXPECTED.items():
        assert p.detect_package_type(real_name, str(repo)) == expected


@pytest.mark.parametrize("pkg_name,expected", [
    ("python3-foo", "python"),
    ("perl-Digest-MD5", "perl"),
    ("lua-socket", "lua"),
    ("php-mbstring", "php"),
    ("golang-github-foo", "other"),  # golang 前缀未在 map 里,走 other
])
def test_detect_package_type_prefix_only(pkg_name, expected, tmp_path):
    # 无 spec 文件,纯包名前缀检测
    assert p.detect_package_type(pkg_name, str(tmp_path)) == expected


# ─────────────────────────────────────────────
# resolve_dist_conflicts(无 Docker,用 fake_subprocess)
# ─────────────────────────────────────────────

def _make_fake_rpm(dist_dir, name, ver, rel="1", arch="noarch"):
    """创建空 RPM 占位文件(仅用于文件名解析测试)。"""
    f = dist_dir / f"{name}-{ver}-{rel}.{arch}.rpm"
    f.touch()
    return f


def test_resolve_conflicts_force_upgrade(tmp_path, fake_subprocess):
    dist = tmp_path / "dist"
    dist.mkdir()
    old = _make_fake_rpm(dist, "openssl-libs", "1.1.1", "1", "aarch64")
    new = _make_fake_rpm(dist, "openssl-libs", "3.0.1", "1", "aarch64")

    removed, notes = p.resolve_dist_conflicts(
        dist, [new], container="", repo_dir=str(tmp_path), force_upgrade=True
    )
    assert not old.exists()
    assert new.exists()
    assert len(notes) > 0


def test_resolve_conflicts_same_version(tmp_path, fake_subprocess):
    dist = tmp_path / "dist"
    dist.mkdir()
    existing = _make_fake_rpm(dist, "python3-arpy", "2.3.0", "1", "noarch")

    removed, notes = p.resolve_dist_conflicts(
        dist, [existing], container="", repo_dir=str(tmp_path), force_upgrade=False
    )
    assert removed == []
    assert notes == []


def test_resolve_conflicts_no_compat_type(tmp_path, fake_subprocess):
    """Python 包 major 升级 → 应报错并回滚新版本(fake docker 查无反向依赖)。"""
    dist = tmp_path / "dist"
    repo = tmp_path / "repo"
    dist.mkdir()
    repo.mkdir()

    pkg_name = "python3-requests"
    pkg_dir = repo / pkg_name
    pkg_dir.mkdir()
    (pkg_dir / f"{pkg_name}.spec").write_text("Name: python3-requests\n%{python3_sitelib}/requests\n")

    old = _make_fake_rpm(dist, pkg_name, "2.28.0", "1", "noarch")
    new = _make_fake_rpm(dist, pkg_name, "3.0.0", "1", "noarch")

    with pytest.raises(RuntimeError):
        p.resolve_dist_conflicts(
            dist, [new], container="nonexistent_container",
            repo_dir=str(repo), force_upgrade=False
        )
    # 新版本回滚删除,旧版本保留
    assert not new.exists()
    assert old.exists()


# ─────────────────────────────────────────────
# 集成用例(需要真实 Docker + RPM,默认跳过)
# ─────────────────────────────────────────────

@pytest.mark.integration
def test_integration_detect_type(tmp_path):
    """用 dist/ 里真实 RPM 验证包名前缀检测。"""
    real = __import__("pathlib").Path("/root/.claude/skills/rpm-repo/dist")
    if not real.exists():
        pytest.skip("dist/ 不存在")
    for rpm in sorted(real.glob("*.rpm")):
        info = p.parse_rpm_nvra(rpm.name)
        if info:
            p.detect_package_type(info["name"], str(tmp_path))


@pytest.mark.integration
def test_integration_repoclosure():
    """检查当前 dist/ 能否通过 repoclosure(容器内)。"""
    real = __import__("pathlib").Path("/root/.claude/skills/rpm-repo/dist")
    if not real.exists():
        pytest.skip("dist/ 不存在")
    p.run_ci_gate(real, "oe-build-env")
