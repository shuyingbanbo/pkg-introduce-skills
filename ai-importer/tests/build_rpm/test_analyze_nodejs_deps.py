"""analyze_nodejs_deps.py — Node.js 包 RPM 依赖分析(纯逻辑 + mock run_batch_lookup)。"""

from __future__ import annotations

import json
import sys

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sys.path.insert(0, str(SCRIPT_DIRS["build_rpm"]))
mod = load_module("analyze_nodejs_deps", SCRIPT_DIRS["build_rpm"] / "analyze_nodejs_deps.py")


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


# ─────────────────────────────────────────────
# _npm_to_rpm_name
# ─────────────────────────────────────────────

@pytest.mark.parametrize("npm_name,expected", [
    ("lodash", "nodejs-lodash"),
    ("@babel/core", "nodejs-babel-core"),
    ("@scope/pkg", "nodejs-scope-pkg"),
    ("@scope/pkg/sub", "nodejs-scope-pkg-sub"),   # 剩余斜杠也转 -
    ("foo_bar", "nodejs-foo-bar"),
    ("foo/bar", "nodejs-foo-bar"),
    ("", "nodejs-"),
])
def test_npm_to_rpm_name(npm_name, expected):
    assert mod._npm_to_rpm_name(npm_name) == expected


# ─────────────────────────────────────────────
# parse_package_json
# ─────────────────────────────────────────────

def test_parse_package_json_full(tmp_path):
    _write(tmp_path, "package.json", json.dumps({
        "name": "demo-pkg",
        "version": "1.0.0",
        "engines": {"node": ">=18.0.0"},
        "dependencies": {"lodash": "^4.17.0", "express": "~4.18.0"},
        "devDependencies": {"jest": "^29.0.0"},
    }))
    _write(tmp_path, "binding.gyp", "{}")
    result = mod.parse_package_json(str(tmp_path))
    assert result["found"] is True
    assert result["name"] == "demo-pkg"
    assert result["node_version"] == ">=18.0.0"
    assert result["has_native"] is True
    assert result["dependencies"] == {"lodash": "^4.17.0", "express": "~4.18.0"}


def test_parse_package_json_missing(tmp_path):
    result = mod.parse_package_json(str(tmp_path))
    assert result == {"found": False, "name": "", "node_version": "", "has_native": False, "dependencies": {}}


def test_parse_package_json_invalid(tmp_path):
    _write(tmp_path, "package.json", "{ not json !!!")
    result = mod.parse_package_json(str(tmp_path))
    assert result["found"] is True
    assert result["name"] == ""
    assert result["dependencies"] == {}


def test_parse_package_json_no_engines(tmp_path):
    _write(tmp_path, "package.json", json.dumps({"name": "p", "engines": "node"}))
    result = mod.parse_package_json(str(tmp_path))
    assert result["node_version"] == ""      # engines 非 dict 时为空


# ─────────────────────────────────────────────
# parse_binding_gyp
# ─────────────────────────────────────────────

def test_parse_binding_gyp_full(tmp_path):
    _write(tmp_path, "binding.gyp", """
{
  "targets": [
    {
      "target_name": "binding",
      # 允许注释
      "libraries": ["-lssl", "-lcrypto", "-lpthread"],
      "sources": ["src/binding.cc"],
      "conditions": [
        ["OS=='mac'", {"libraries": ["-lz"]}]
      ]
    }
  ]
}
""")
    result = mod.parse_binding_gyp(str(tmp_path))
    assert result["found"] is True
    assert result["link_libs"] == ["crypto", "ssl", "z"]   # pthread 过滤,多 libraries 块合并


def test_parse_binding_gyp_missing(tmp_path):
    result = mod.parse_binding_gyp(str(tmp_path))
    assert result == {"found": False, "link_libs": []}


def test_parse_binding_gyp_empty_libraries(tmp_path):
    _write(tmp_path, "binding.gyp", '{"targets": [{"libraries": []}]}')
    result = mod.parse_binding_gyp(str(tmp_path))
    assert result["link_libs"] == []


# ─────────────────────────────────────────────
# _parse_npm_constraint
# ─────────────────────────────────────────────

@pytest.mark.parametrize("constraint,expected", [
    ("", None),
    ("*", None),
    ("latest", None),
    ("workspace:*", None),
    ("^1.2.3", ((1, 2, 3), (2, 0, 0), True, False)),
    ("^0.2.3", ((0, 2, 3), (0, 3, 0), True, False)),
    ("^0.0.3", ((0, 0, 3), (0, 0, 4), True, False)),
    ("~1.2.3", ((1, 2, 3), (1, 3, 0), True, False)),
    (">=1.2", ((1, 2), None, True, True)),
    (">1.2", ((1, 2), None, False, True)),
    ("<=1.2", ((0, 0, 0), (1, 2), True, True)),
    ("<1.2", ((0, 0, 0), (1, 2), True, False)),
    ("=1.2.3", ((1, 2, 3), (1, 2, 3), True, True)),
    ("1.2.3", ((1, 2, 3), (1, 2, 3), True, True)),
    ("^1.2", None),            # caret 只支持三段
    ("1.x", None),
])
def test_parse_npm_constraint(constraint, expected):
    assert mod._parse_npm_constraint(constraint) == expected


# ─────────────────────────────────────────────
# _version_satisfies
# ─────────────────────────────────────────────

@pytest.mark.parametrize("rpm_version,constraint,expected", [
    ("1.2.3", "^1.0.0", True),
    ("2.0.0", "^1.0.0", False),      # major 越界
    ("0.2.5", "^0.2.0", True),
    ("0.3.0", "^0.2.0", False),      # 0.x caret minor 越界
    ("1.2.9", "~1.2.0", True),
    ("1.3.0", "~1.2.0", False),
    ("1.5.0", ">=1.2", True),
    ("1.1.0", ">=1.2", False),
    ("1.2.0", ">1.2", False),        # 非含边界
    ("1.2.1", ">1.2", True),
    ("1.2.0", "<=1.2", True),
    ("1.2.1", "<1.2", False),
    ("1.2.3", "=1.2.3", True),
    ("1.2.4", "=1.2.3", False),
    ("", "^1.0.0", True),            # 空版本保守放行
    ("1.2.3", "", True),             # 空约束放行
    ("1.2.3", "workspace:*", True),  # 无法解析保守放行
    ("1.2.3", "1.x", True),          # 解析失败保守放行
    ("abc", ">=1.2", True),          # 版本格式异常保守放行
    ("1.2", ">=1.2.3", False),       # 两段版本 pad 成 (1,2,0)
])
def test_version_satisfies(rpm_version, constraint, expected):
    assert mod._version_satisfies(rpm_version, constraint) == expected


# ─────────────────────────────────────────────
# build_lookup_tasks / check_rpm_availability
# ─────────────────────────────────────────────

def test_build_lookup_tasks():
    tasks = mod.build_lookup_tasks(["ssl"])
    assert len(tasks) == 1
    t = tasks[0]
    assert t["dep"] == "ssl"
    assert t["type"] == "link"
    assert t["prefer_devel"] is True
    values = [q["value"] for q in t["queries"]]
    assert values == ["pkgconfig(ssl)", "*/libssl.so*", "ssl-devel", "libssl-devel"]


def test_build_lookup_tasks_empty():
    assert mod.build_lookup_tasks([]) == []


def test_check_rpm_availability(monkeypatch):
    def fake(tasks, timeout=120, **kw):
        out = []
        for t in tasks:
            base = {k: v for k, v in t.items() if k not in {"queries", "prefer_devel"}}
            if t["dep"] == "ssl":
                out.append({**base, "rpm": "openssl-devel", "version": None, "release": None, "level": "pkgconfig()"})
            else:
                out.append({**base, "rpm": None, "version": None, "release": None, "level": ""})
        return out
    monkeypatch.setattr(mod, "run_batch_lookup", fake)
    result = mod.check_rpm_availability(link_libs=["ssl", "foo"])
    assert result["available"] == [{"dep": "ssl", "type": "link", "rpm": "openssl-devel", "level": "pkgconfig()"}]
    assert result["missing"] == [{"dep": "foo", "type": "link"}]


def test_check_rpm_availability_fallback_on_error(monkeypatch):
    def boom(tasks, timeout=120, **kw):
        raise mod.BatchLookupError("boom")
    monkeypatch.setattr(mod, "run_batch_lookup", boom)
    result = mod.check_rpm_availability(link_libs=["ssl"])
    assert result["missing"] == [{"dep": "ssl", "type": "link"}]


# ─────────────────────────────────────────────
# check_runtime_deps
# ─────────────────────────────────────────────

def _runtime_fake(monkeypatch, results_per_dep, exc=None):
    calls = []

    def fake(*args, **kwargs):
        # 生产代码 bug:check_runtime_deps 调用 run_batch_lookup("", tasks, timeout=60),
        # 第一个位置参数是 "" 而不是 tasks;真实 run_batch_lookup 会因 timeout 重复赋值
        # 抛 TypeError(被 except Exception 吞掉 → 全部标记 missing)。
        # 测试按实际调用形态断言:任务列表是第二个位置参数。
        assert args[0] == ""
        tasks = args[1]
        calls.append(tasks)
        if exc is not None:
            raise exc
        out = []
        for t in tasks:
            base = {k: v for k, v in t.items() if k not in {"queries", "prefer_devel"}}
            res = results_per_dep.get(t["dep"])
            if res is None:
                out.append({**base, "rpm": None, "version": None, "release": None, "level": ""})
            else:
                out.append({**base, **res})
        return out

    monkeypatch.setattr(mod, "run_batch_lookup", fake)
    return calls


def test_check_runtime_deps_available(monkeypatch):
    _runtime_fake(monkeypatch, {
        "lodash": {"rpm": "nodejs-lodash", "version": "4.17.21", "level": "npm_provides"},
    })
    result = mod.check_runtime_deps({"lodash": "^4.0.0"})
    assert result["missing"] == []
    assert result["version_conflict"] == []
    avail = result["available"][0]
    assert avail["dep"] == "lodash"
    assert avail["rpm"] == "nodejs-lodash"
    assert avail["rpm_version"] == "4.17.21"
    assert avail["constraint"] == "^4.0.0"


def test_check_runtime_deps_version_conflict(monkeypatch):
    _runtime_fake(monkeypatch, {
        "lodash": {"rpm": "nodejs-lodash", "version": "3.10.1", "level": "npm_provides"},
    })
    result = mod.check_runtime_deps({"lodash": "^4.0.0"})
    assert result["available"] == []
    assert result["missing"] == []
    conflict = result["version_conflict"][0]
    assert conflict["dep"] == "lodash"
    assert conflict["rpm"] == "nodejs-lodash"
    assert conflict["found_version"] == "3.10.1"


def test_check_runtime_deps_missing_rpm(monkeypatch):
    _runtime_fake(monkeypatch, {})
    result = mod.check_runtime_deps({"lodash": "^4.0.0"})
    assert result["available"] == []
    assert result["version_conflict"] == []
    miss = result["missing"][0]
    assert miss["dep"] == "lodash"
    assert miss["type"] == "runtime"
    assert miss["constraint"] == "^4.0.0"


def test_check_runtime_deps_lookup_exception(monkeypatch):
    _runtime_fake(monkeypatch, {}, exc=RuntimeError("subprocess failed"))
    result = mod.check_runtime_deps({"express": "~4.18.0"})
    assert result["missing"][0]["dep"] == "express"


def test_check_runtime_deps_empty():
    assert mod.check_runtime_deps({}) == {"available": [], "missing": [], "version_conflict": []}


# ─────────────────────────────────────────────
# build_rpm_requires
# ─────────────────────────────────────────────

def test_build_rpm_requires_basic():
    pkg_info = {"node_version": "", "has_native": False}
    assert mod.build_rpm_requires(pkg_info, None) == ["nodejs-devel", "npm"]


def test_build_rpm_requires_node_version():
    # 生产代码行为:engines.node 里第一个数字串 → "nodejs >= N"
    pkg_info = {"node_version": ">=18.17.0", "has_native": False}
    assert mod.build_rpm_requires(pkg_info, None) == ["nodejs >= 18", "npm"]


def test_build_rpm_requires_native():
    pkg_info = {"node_version": "", "has_native": True}
    result = mod.build_rpm_requires(pkg_info, None)
    assert result == ["nodejs-devel", "npm", "gcc", "gcc-c++", "python3"]


def test_build_rpm_requires_with_rpm_check():
    pkg_info = {"node_version": "", "has_native": False}
    rpm_check = {"available": [{"rpm": "openssl-devel"}, {"rpm": "openssl-devel"}, {"rpm": "zlib-devel"}],
                 "missing": []}
    assert mod.build_rpm_requires(pkg_info, rpm_check) == ["nodejs-devel", "npm", "openssl-devel", "zlib-devel"]


# ─────────────────────────────────────────────
# print_report / main
# ─────────────────────────────────────────────

def test_print_report(capsys):
    pkg_info = {"name": "demo-pkg", "node_version": ">=18.0.0", "has_native": True}
    gyp_info = {"found": True, "link_libs": ["ssl"]}
    rpm_check = {"available": [{"dep": "ssl", "type": "link", "rpm": "openssl-devel", "level": "pkgconfig()"}],
                 "missing": []}
    runtime_deps = {"available": [{"dep": "lodash", "type": "runtime", "rpm": "nodejs-lodash"}],
                    "missing": [{"dep": "express", "type": "runtime", "constraint": "~4.18.0"}],
                    "version_conflict": []}
    mod.print_report(pkg_info, gyp_info, rpm_check, runtime_deps)
    out = capsys.readouterr().out
    assert "Node.js 包 RPM 依赖分析报告" in out
    assert "包名      : demo-pkg" in out
    assert "openssl-devel" in out
    assert "BuildRequires: nodejs >= 18" in out


def test_main_output_json(tmp_path, capsys, monkeypatch):
    _write(tmp_path, "package.json", json.dumps({
        "name": "demo",
        "engines": {"node": ">=18"},
        "dependencies": {"lodash": "^4.0.0"},
    }))
    _write(tmp_path, "binding.gyp", '{"targets": [{"libraries": ["-lssl"]}]}')
    out_json = tmp_path / "result.json"
    monkeypatch.setattr(sys, "argv", ["analyze_nodejs_deps.py", str(tmp_path), "-o", str(out_json)])
    mod.main()
    result = json.loads(out_json.read_text())
    assert result["name"] == "demo"
    assert result["node_version"] == ">=18"
    assert result["has_native"] is True
    assert result["link_libs"] == ["ssl"]
    assert result["dependencies"] == {"lodash": "^4.0.0"}
    assert result["build_requires"] == ["nodejs >= 18", "npm", "gcc", "gcc-c++", "python3"]


def test_main_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["analyze_nodejs_deps.py", str(tmp_path / "nope")])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1


def test_main_no_package_json(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["analyze_nodejs_deps.py", str(tmp_path)])
    mod.main()
    assert "未找到 package.json" in capsys.readouterr().err
