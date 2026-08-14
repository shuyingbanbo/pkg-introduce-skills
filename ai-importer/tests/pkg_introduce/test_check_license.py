"""check_license.py — 源码仓库 License 检查脚本测试。

覆盖:SPDX 分类/表达式解析/ID 归一、8 种 manifest 读取、LICENSE 文件
识别、check_license 全流程、print_report 与 main。
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

cl = load_module("check_license", SCRIPT_DIRS["pkg_introduce"] / "check_license.py")


def _mk(source_dir, files: dict[str, str]):
    """按 {相对路径: 内容} 写入夹具文件。"""
    for name, content in files.items():
        p = source_dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


# ─────────────────────────────────────────────
# classify
# ─────────────────────────────────────────────

@pytest.mark.parametrize("spdx_id,expected", [
    ("MIT", "permissive"),
    ("Apache-2.0", "permissive"),
    ("Apache-1.1", "permissive"),
    ("BSD-2-Clause", "permissive"),
    ("BSD-3-Clause", "permissive"),
    ("BSD-4-Clause", "permissive"),
    ("ISC", "permissive"),
    ("Unlicense", "permissive"),
    ("CC0-1.0", "permissive"),
    ("Artistic-2.0", "permissive"),
    ("Ruby", "permissive"),
    ("WTFPL", "permissive"),
    ("Zlib", "permissive"),
    ("libpng", "permissive"),
    ("LGPL-2.0", "weak_copyleft"),
    ("LGPL-2.1", "weak_copyleft"),
    ("LGPL-3.0", "weak_copyleft"),
    ("MPL-1.1", "weak_copyleft"),
    ("MPL-2.0", "weak_copyleft"),
    ("CDDL-1.0", "weak_copyleft"),
    ("EPL-1.0", "weak_copyleft"),
    ("EPL-2.0", "weak_copyleft"),
    ("EUPL-1.2", "weak_copyleft"),
    ("Artistic-1.0", "weak_copyleft"),
    ("CC-BY", "weak_copyleft"),
    ("CC-BY-SA", "weak_copyleft"),
    ("GPL-2.0", "strong_copyleft"),
    ("GPL-3.0", "strong_copyleft"),
    ("AGPL-3.0", "strong_copyleft"),
    ("CC-BY-NC", "no_commercial"),
    ("CC-BY-NC-SA", "no_commercial"),
    ("CC-BY-NC-ND", "no_commercial"),
    ("BUSL-1.1", "no_commercial"),
    ("SSPL-1.0", "no_commercial"),
    ("Proprietary", "unknown"),
    ("MIT-0", "unknown"),
    ("", "unknown"),
])
def test_classify_known(spdx_id, expected):
    assert cl.classify(spdx_id) == expected


@pytest.mark.parametrize("spdx_id,expected", [
    ("GPL-3.0-only", "strong_copyleft"),
    ("GPL-3.0-or-later", "strong_copyleft"),
    ("GPL-2.0-or-later", "strong_copyleft"),
    ("LGPL-3.0-only", "weak_copyleft"),
    ("LGPL-2.1-or-later", "weak_copyleft"),
    ("MIT-or-later", "permissive"),
    ("GPL-3.0-or-AND-later", "strong_copyleft"),  # 代码实际支持该怪异后缀
    ("GPL-2.0 WITH Classpath-exception", "strong_copyleft"),
    ("Apache-2.0 WITH LLVM-exception", "permissive"),
    # 已知实现边界(只记录不修):-only 剥离在 WITH 剥除之前,
    # "GPL-2.0-only WITH Classpath-exception-2.0" 剥 WITH 后留下 GPL-2.0-only → unknown
    ("GPL-2.0-only WITH Classpath-exception-2.0", "unknown"),
])
def test_classify_suffix_and_with(spdx_id, expected):
    assert cl.classify(spdx_id) == expected


def test_classify_suffix_before_with_not_stripped():
    # 后缀剥离先于 WITH 剥离执行:"-only" 不在串尾时不匹配 → unknown
    # (生产代码现状,与 normalize_license_ids 丢弃该串的行为一致)
    assert cl.classify("GPL-2.0-only WITH Classpath-exception-2.0") == "unknown"


def test_classify_lowercase_unknown():
    # 注意:classify 大小写敏感,"mit" 归为 unknown(生产代码现状)
    assert cl.classify("mit") == "unknown"


def test_classify_none_raises():
    # None 无防护,re.sub 直接抛 TypeError(生产代码现状)
    with pytest.raises(TypeError):
        cl.classify(None)


# ─────────────────────────────────────────────
# parse_spdx_expression
# ─────────────────────────────────────────────

@pytest.mark.parametrize("expr,expected", [
    ("MIT OR Apache-2.0", ["MIT", "Apache-2.0"]),
    ("MIT or Apache-2.0", ["MIT", "Apache-2.0"]),  # OR/AND 大小写不敏感
    ("GPL-2.0 AND Classpath-exception-2.0", ["GPL-2.0"]),  # exception 标识符跳过
    ("GPL-2.0 AND LGPL-2.1 AND MPL-2.0", ["GPL-2.0", "LGPL-2.1", "MPL-2.0"]),
    ("(MIT OR Apache-2.0)", ["MIT", "Apache-2.0"]),
    ("(MIT OR (Apache-2.0 AND BSD-3-Clause))", ["MIT", "Apache-2.0", "BSD-3-Clause"]),
    ("MIT", ["MIT"]),
    ("  MIT  ", ["MIT"]),
    ("MIT OR (LGPL-2.1 AND LicenseRef-exception-xyz)", ["MIT", "LGPL-2.1"]),
])
def test_parse_spdx_expression(expr, expected):
    assert cl.parse_spdx_expression(expr) == expected


def test_parse_spdx_expression_empty():
    # 空串无有效 part → 回退 [expr](生产代码现状)
    assert cl.parse_spdx_expression("") == [""]
    assert cl.parse_spdx_expression("   ") == [""]


def test_parse_spdx_expression_case_preserved():
    # 小写输入原样保留大小写(不归一)
    assert cl.parse_spdx_expression("mit or apache-2.0") == ["mit", "apache-2.0"]


def test_parse_spdx_expression_with_exception_fallback():
    # WITH 异常条款整体含 exception 被跳过 → 回退原表达式(生产代码现状)
    assert cl.parse_spdx_expression("GPL-2.0 WITH Classpath-exception-2.0") == \
        ["GPL-2.0 WITH Classpath-exception-2.0"]


def test_parse_spdx_expression_none_raises():
    with pytest.raises(AttributeError):
        cl.parse_spdx_expression(None)


# ─────────────────────────────────────────────
# normalize_license_ids
# ─────────────────────────────────────────────

@pytest.mark.parametrize("values,expected", [
    (["MIT", "Apache-2.0"], ["MIT", "Apache-2.0"]),
    (["MIT", "Apache-2.0", "MIT"], ["MIT", "Apache-2.0"]),  # 去重
    (["SPDX-License-Identifier: MIT"], ["MIT"]),
    (["spdx-license-identifier: Apache-2.0"], ["Apache-2.0"]),
    (["GPL-3.0-or-later"], ["GPL-3.0-or-later"]),  # 已知 ID 保留原串(不剥后缀)
    (["GNU GENERAL PUBLIC LICENSE Version 3"], ["GPL-3.0"]),  # 全文关键词识别
    (["Permission is hereby granted, free of charge, to any person"], ["MIT"]),
    ([], []),
    (["   "], []),
    (["MIT", "mit"], ["MIT"]),  # 小写 "mit" 无法识别被丢弃(生产代码现状)
    (["Proprietary"], []),      # 无关键词匹配 → 丢弃
    (["GPL-2.0-only WITH Classpath-exception-2.0"], []),  # 后缀+WITH 组合未归一 → 丢弃
])
def test_normalize_license_ids(values, expected):
    assert cl.normalize_license_ids(values) == expected


# ─────────────────────────────────────────────
# manifest 读取
# ─────────────────────────────────────────────

@pytest.mark.parametrize("content,expected", [
    ('[project]\nname = "demo"\nlicense = "MIT"\n', "MIT"),
    ('[project]\nlicense = { text = "MIT" }\n', "MIT"),
    ('[project]\nlicense = { expression = "Apache-2.0" }\n', "Apache-2.0"),
    ('[project]\nlicense = { text = "" }\n', None),  # 空 text 且无 file → None
    ('[project]\nname = "demo"\n', None),  # 无 license 字段
    ('[tool.poetry]\nlicense = "MIT"\n', "MIT"),  # Poetry 格式
    ('', None),  # 空文件
])
def test_read_pyproject_toml(tmp_path, content, expected):
    f = tmp_path / "pyproject.toml"
    f.write_text(content, encoding="utf-8")
    assert cl.read_pyproject_toml(str(f)) == expected


def test_read_pyproject_toml_file_license(tmp_path):
    # PEP 621 {file=...}:读目标文件并做关键词识别
    f = tmp_path / "pyproject.toml"
    f.write_text('[project]\nlicense = { file = "LICENSE-Apache.txt" }\n', encoding="utf-8")
    (tmp_path / "LICENSE-Apache.txt").write_text("Apache License\nVersion 2.0", encoding="utf-8")
    assert cl.read_pyproject_toml(str(f)) == "Apache-2.0"


def test_read_pyproject_toml_file_license_missing(tmp_path):
    f = tmp_path / "pyproject.toml"
    f.write_text('[project]\nlicense = { file = "MISSING.txt" }\n', encoding="utf-8")
    assert cl.read_pyproject_toml(str(f)) is None


def test_read_pyproject_toml_file_license_no_keyword(tmp_path):
    f = tmp_path / "pyproject.toml"
    f.write_text('[project]\nlicense = { file = "EMPTY.txt" }\n', encoding="utf-8")
    (tmp_path / "EMPTY.txt").write_text("no license keyword here", encoding="utf-8")
    assert cl.read_pyproject_toml(str(f)) is None


def test_read_pyproject_toml_fallback_simple(tmp_path, monkeypatch):
    # 强制 tomllib/tomli 不可用 → 手动解析分支
    monkeypatch.setitem(sys.modules, "tomllib", None)
    monkeypatch.setitem(sys.modules, "tomli", None)
    f = tmp_path / "pyproject.toml"
    f.write_text('[project]\nlicense = "MIT"\n', encoding="utf-8")
    assert cl.read_pyproject_toml(str(f)) == "MIT"


@pytest.mark.parametrize("content,expected", [
    ('license = "MIT"', "MIT"),
    ("license = 'Apache-2.0'", "Apache-2.0"),
    ('license = { text = "MIT" }', "MIT"),
    ('license = { expression = "GPL-3.0" }', "GPL-3.0"),
    ('[project]\nname = "x"\n', None),
    ('', None),
])
def test_parse_toml_license_simple(tmp_path, content, expected):
    f = tmp_path / "pyproject.toml"
    f.write_text(content, encoding="utf-8")
    assert cl._parse_toml_license_simple(str(f)) == expected


@pytest.mark.parametrize("content,expected", [
    ('[metadata]\nname = x\nlicense = MIT\n', "MIT"),
    ('[metadata]\nlicense = Apache-2.0\n', "Apache-2.0"),
    ('[metadata]\nname = x\n', None),        # 无 license
    ('license = MIT\n', None),               # 无 section 头 → 解析异常 → None
    ('[metadata\nlicense = MIT\n', None),    # 畸形 section → None
])
def test_read_setup_cfg(tmp_path, content, expected):
    f = tmp_path / "setup.cfg"
    f.write_text(content, encoding="utf-8")
    assert cl.read_setup_cfg(str(f)) == expected


@pytest.mark.parametrize("content,expected", [
    ('setup(name="x", license="MIT")', "MIT"),
    ("setup(license='Apache-2.0')", "Apache-2.0"),
    ('setup(name="x")', None),
    ('', None),
])
def test_read_setup_py(tmp_path, content, expected):
    f = tmp_path / "setup.py"
    f.write_text(content, encoding="utf-8")
    assert cl.read_setup_py(str(f)) == expected


@pytest.mark.parametrize("content,expected", [
    ('[package]\nname = "x"\nlicense = "MIT"\n', "MIT"),
    ('[package]\nlicense = "MIT OR Apache-2.0"\n', "MIT OR Apache-2.0"),
    ("[package]\nlicense = 'GPL-3.0'\n", "GPL-3.0"),
    ('[package]\nname = "x"\n', None),
])
def test_read_cargo_toml(tmp_path, content, expected):
    f = tmp_path / "Cargo.toml"
    f.write_text(content, encoding="utf-8")
    assert cl.read_cargo_toml(str(f)) == expected


@pytest.mark.parametrize("data,expected", [
    ({"name": "x", "license": "MIT"}, "MIT"),
    ({"license": {"type": "Apache-2.0", "url": "https://example.com"}}, "Apache-2.0"),
    ({"license": {"type": "MIT"}}, "MIT"),
    ({"name": "x"}, None),           # 无 license
    ({"license": None}, None),       # license 为 null
])
def test_read_package_json(tmp_path, data, expected):
    f = tmp_path / "package.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    assert cl.read_package_json(str(f)) == expected


def test_read_package_json_invalid_raises(tmp_path):
    # 非法 JSON 未捕获,直接抛 JSONDecodeError(生产代码现状)
    f = tmp_path / "package.json"
    f.write_text("{invalid json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        cl.read_package_json(str(f))


POM_TEMPLATE = '''<?xml version="1.0" encoding="UTF-8"?>
<project {ns}>
  <modelVersion>4.0.0</modelVersion>
  <groupId>g</groupId>
  <artifactId>a</artifactId>
  <licenses>
    <license>
      <name>Apache License, Version 2.0</name>
      <url>http://www.apache.org/licenses/LICENSE-2.0.txt</url>
    </license>
  </licenses>
</project>'''


@pytest.mark.parametrize("ns,expected", [
    ("", "Apache License, Version 2.0"),
    ('xmlns="http://maven.apache.org/POM/4.0.0"', "Apache License, Version 2.0"),
])
def test_read_pom_xml(tmp_path, ns, expected):
    f = tmp_path / "pom.xml"
    f.write_text(POM_TEMPLATE.format(ns=ns), encoding="utf-8")
    assert cl.read_pom_xml(str(f)) == expected


@pytest.mark.parametrize("content", [
    "<project></project>",                                            # 无 licenses
    "<project><licenses><license><url>x</url></license></licenses></project>",  # 无 name
    "<project><licenses><license><name /></license></licenses></project>",     # name 为空
    "not xml at all",                                                 # 畸形
    "",                                                               # 空文件
])
def test_read_pom_xml_none(tmp_path, content):
    f = tmp_path / "pom.xml"
    f.write_text(content, encoding="utf-8")
    assert cl.read_pom_xml(str(f)) is None


@pytest.mark.parametrize("content,expected", [
    ('Gem::Specification.new do |s|\n  s.license = "MIT"\nend', "MIT"),
    ('spec.licenses = ["MIT", "Apache-2.0"]', "MIT"),
    ("spec.license = 'Ruby'", "Ruby"),
    ('Gem::Specification.new do |s|\n  s.name = "x"\nend', None),
])
def test_read_gemspec(tmp_path, content, expected):
    f = tmp_path / "demo.gemspec"
    f.write_text(content, encoding="utf-8")
    assert cl.read_gemspec(str(f)) == expected


def test_read_gemfile_or_gemspec(tmp_path):
    (tmp_path / "demo.gemspec").write_text('s.license = "MIT"', encoding="utf-8")
    assert cl.read_gemfile_or_gemspec(str(tmp_path)) == "MIT"


def test_read_gemfile_or_gemspec_no_license(tmp_path):
    (tmp_path / "demo.gemspec").write_text('s.name = "x"', encoding="utf-8")
    assert cl.read_gemfile_or_gemspec(str(tmp_path)) is None


def test_read_gemfile_or_gemspec_empty(tmp_path):
    assert cl.read_gemfile_or_gemspec(str(tmp_path)) is None


def test_detect_license_from_manifest_priority(tmp_path):
    # pyproject.toml 优先级最高(即使 package.json 也存在)
    _mk(tmp_path, {
        "pyproject.toml": '[project]\nlicense = "MIT"\n',
        "package.json": '{"license": "Apache-2.0"}',
    })
    assert cl.detect_license_from_manifest(str(tmp_path)) == ("MIT", "pyproject.toml")


def test_detect_license_from_manifest_setup_cfg_over_setup_py(tmp_path):
    _mk(tmp_path, {
        "setup.cfg": "[metadata]\nlicense = MIT\n",
        "setup.py": 'setup(license="Apache-2.0")',
    })
    assert cl.detect_license_from_manifest(str(tmp_path)) == ("MIT", "setup.cfg")


def test_detect_license_from_manifest_gemspec(tmp_path):
    (tmp_path / "demo.gemspec").write_text('s.license = "MIT"', encoding="utf-8")
    assert cl.detect_license_from_manifest(str(tmp_path)) == ("MIT", "*.gemspec")


def test_detect_license_from_manifest_none(tmp_path):
    assert cl.detect_license_from_manifest(str(tmp_path)) == (None, "")


# ─────────────────────────────────────────────
# LICENSE 文件识别
# ─────────────────────────────────────────────

@pytest.mark.parametrize("name", cl.LICENSE_FILENAMES)
def test_find_license_files_exact(tmp_path, name):
    (tmp_path / name).write_text("x", encoding="utf-8")
    found = cl.find_license_files(str(tmp_path))
    assert str(tmp_path / name) in found


def test_find_license_files_variants(tmp_path):
    for n in ("LICENSE-MIT", "LICENSE.APACHE", "LICENCE_GPL", "COPYING_2",
              "NOTICE", "README.md"):
        (tmp_path / n).write_text("x", encoding="utf-8")
    found = {os.path.basename(p) for p in cl.find_license_files(str(tmp_path))}
    assert found == {"LICENSE-MIT", "LICENSE.APACHE", "LICENCE_GPL", "COPYING_2"}


def test_find_license_files_missing_dir(tmp_path):
    assert cl.find_license_files(str(tmp_path / "nope")) == []


@pytest.mark.parametrize("content,expected", [
    ("GNU AFFERO GENERAL PUBLIC LICENSE Version 3", "AGPL-3.0"),
    ("GNU GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007", "GPL-3.0"),
    ("GNU GENERAL PUBLIC LICENSE Version 2", "GPL-2.0"),
    ("GNU LESSER GENERAL PUBLIC LICENSE Version 3", "LGPL-3.0"),
    ("GNU LESSER GENERAL PUBLIC LICENSE Version 2.1", "LGPL-2.1"),
    ("GNU LESSER GENERAL PUBLIC LICENSE Version 2", "LGPL-2.0"),
    ("Mozilla Public License Version 2.0", "MPL-2.0"),
    ("Mozilla Public License Version 1.1", "MPL-1.1"),
    ("Apache License\nVersion 2.0, January 2004", "Apache-2.0"),
    ("Apache License Version 1.1", "Apache-1.1"),
    ("Permission is hereby granted, free of charge, to any person", "MIT"),
    ("Redistribution and use in source and binary forms ... neither the name of the holder", "BSD-3-Clause"),
    ("Redistribution and use in source and binary forms", "BSD-2-Clause"),
    ("Permission to use, copy, modify, and/or distribute this software", "ISC"),
    ("This is free and unencumbered software released into the public domain", "Unlicense"),
    ("Creative Commons Attribution-NonCommercial 4.0", "CC-BY-NC"),
    ("Creative Commons Attribution-ShareAlike 4.0", "CC-BY-SA"),
    ("Creative Commons Attribution 4.0", "CC-BY"),
    ("Business Source License 1.1", "BUSL-1.1"),
    ("Server Side Public License", "SSPL-1.0"),
    ("Ruby's License", "Ruby"),
    ("The Artistic License 2.0", "Artistic-2.0"),
    ("Artistic License", "Artistic-1.0"),
    ("European Union Public Licence v1.2", "EUPL-1.2"),
    ("some random text without any license", None),
    ("", None),
    ("GNU GENERAL PUBLIC LICENSE", None),  # GPL 无版本号无法匹配(生产代码现状)
])
def test_match_license_from_content(content, expected):
    assert cl.match_license_from_content(content) == expected


def test_match_license_agpl_priority_over_gpl():
    # AGPL 文本同时含 GPL 关键词,AGPL 规则在前优先命中
    content = "GNU AFFERO GENERAL PUBLIC LICENSE\nGNU GENERAL PUBLIC LICENSE\nVersion 3"
    assert cl.match_license_from_content(content) == "AGPL-3.0"


def test_match_license_truncates_at_3000():
    # 关键词在 3000 字符之后 → 不识别;之前 → 识别
    assert cl.match_license_from_content("x" * 3000 + "Permission is hereby granted, free of charge") is None
    assert cl.match_license_from_content("Permission is hereby granted, free of charge" + "y" * 3000) == "MIT"


def test_detect_license_from_files_single(tmp_path):
    (tmp_path / "LICENSE").write_text(
        "MIT License\n\nPermission is hereby granted, free of charge, to any person",
        encoding="utf-8")
    assert cl.detect_license_from_files(str(tmp_path)) == (["MIT"], "LICENSE")


def test_detect_license_from_files_dedupe(tmp_path):
    # 两个文件同为 MIT → id 去重,source 列出全部文件名
    (tmp_path / "LICENSE").write_text("Permission is hereby granted, free of charge", encoding="utf-8")
    (tmp_path / "LICENSE.md").write_text("MIT License\nPermission is hereby granted, free of charge", encoding="utf-8")
    ids, src = cl.detect_license_from_files(str(tmp_path))
    assert ids == ["MIT"]
    assert set(src.split(", ")) == {"LICENSE", "LICENSE.md"}


def test_detect_license_from_files_multiple_ids(tmp_path):
    (tmp_path / "LICENSE").write_text("Permission is hereby granted, free of charge", encoding="utf-8")
    (tmp_path / "COPYING").write_text("GNU GENERAL PUBLIC LICENSE\nVersion 3", encoding="utf-8")
    ids, _ = cl.detect_license_from_files(str(tmp_path))
    assert sorted(ids) == ["GPL-3.0", "MIT"]


def test_detect_license_from_files_unrecognized(tmp_path):
    # 文件存在但无法识别:id 为空,source 仍记录文件名
    (tmp_path / "LICENSE").write_text("custom terms, not recognized", encoding="utf-8")
    assert cl.detect_license_from_files(str(tmp_path)) == ([], "LICENSE")


def test_detect_license_from_files_none(tmp_path):
    assert cl.detect_license_from_files(str(tmp_path)) == ([], "")


def test_detect_license_from_files_unreadable(tmp_path):
    # LICENSE 是目录 → open 抛 IsADirectoryError(OSError 子类)→ 跳过该文件
    (tmp_path / "LICENSE").mkdir()
    assert cl.detect_license_from_files(str(tmp_path)) == ([], "")


# ─────────────────────────────────────────────
# check_license 全流程
# ─────────────────────────────────────────────

def test_check_license_permissive_via_manifest(tmp_path):
    _mk(tmp_path, {"package.json": '{"license": "MIT"}'})
    r = cl.check_license(str(tmp_path))
    assert r["license_ids"] == ["MIT"]
    assert r["category"] == "permissive"
    assert r["source"] == "package.json"
    assert r["blocking"] is False
    assert r["needs_ai_fallback"] is False
    assert r["final_blocking"] is False
    assert "宽松" in r["message"]
    assert r["all_categories"] == ["permissive"]


def test_check_license_manifest_and_file_merged(tmp_path):
    # manifest + LICENSE 文件都命中:ids 合并,取最严格分类
    _mk(tmp_path, {
        "package.json": '{"license": "MIT"}',
        "LICENSE": "GNU GENERAL PUBLIC LICENSE\nVersion 3",
    })
    r = cl.check_license(str(tmp_path))
    assert r["license_ids"] == ["MIT", "GPL-3.0"]
    assert r["category"] == "strong_copyleft"
    assert r["source"] == "package.json, LICENSE"
    assert r["blocking"] is False
    assert r["needs_ai_fallback"] is False
    assert r["all_categories"] == ["permissive", "strong_copyleft"]
    assert "强 Copyleft" in r["message"]


def test_check_license_no_commercial_blocking(tmp_path):
    _mk(tmp_path, {"package.json": '{"license": "BUSL-1.1"}'})
    r = cl.check_license(str(tmp_path))
    assert r["category"] == "no_commercial"
    assert r["blocking"] is True
    assert r["final_blocking"] is True
    assert r["needs_ai_fallback"] is False
    assert "阻断" in r["message"]


def test_check_license_multi_worst_wins(tmp_path):
    # permissive + no_commercial → no_commercial 阻断
    _mk(tmp_path, {
        "package.json": '{"license": "MIT"}',
        "LICENSE": "Business Source License 1.1",
    })
    r = cl.check_license(str(tmp_path))
    assert r["category"] == "no_commercial"
    assert r["blocking"] is True
    assert r["all_categories"] == ["permissive", "no_commercial"]


def test_check_license_unknown_manifest_ai_fallback(tmp_path):
    # manifest 存在但规则无法识别 → AI 兜底,不阻断
    _mk(tmp_path, {"package.json": '{"license": "Proprietary"}'})
    r = cl.check_license(str(tmp_path))
    assert r["license_ids"] == []
    assert r["category"] == "unknown"
    assert r["source"] == "package.json"
    assert r["blocking"] is False
    assert r["needs_ai_fallback"] is True
    assert r["final_blocking"] is False
    assert r["all_categories"] == ["unknown"]
    assert "AI 兜底" in r["message"]


def test_check_license_unknown_license_file(tmp_path):
    _mk(tmp_path, {"LICENSE": "custom terms, not recognized"})
    r = cl.check_license(str(tmp_path))
    assert r["category"] == "unknown"
    assert r["source"] == "LICENSE"
    assert r["needs_ai_fallback"] is True
    assert r["blocking"] is False


def test_check_license_both_unknown(tmp_path):
    # manifest 与 LICENSE 均无法识别 → source 拼接两者
    _mk(tmp_path, {
        "package.json": '{"license": "Proprietary"}',
        "LICENSE": "weird custom terms",
    })
    r = cl.check_license(str(tmp_path))
    assert r["category"] == "unknown"
    assert r["source"] == "package.json, LICENSE"
    assert r["needs_ai_fallback"] is True


def test_check_license_unknown_manifest_with_known_file(tmp_path):
    # manifest 无法识别但 LICENSE 文件可识别:manifest 不进入 source
    _mk(tmp_path, {
        "package.json": '{"license": "Proprietary"}',
        "LICENSE": "Permission is hereby granted, free of charge",
    })
    r = cl.check_license(str(tmp_path))
    assert r["license_ids"] == ["MIT"]
    assert r["category"] == "permissive"
    assert r["source"] == "LICENSE"


def test_check_license_unlicensed(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    r = cl.check_license(str(src))
    assert r["category"] == "unlicensed"
    assert r["license_ids"] == []
    assert r["source"] == "none"
    assert r["blocking"] is False
    assert r["needs_ai_fallback"] is True
    assert r["final_blocking"] is False
    assert r["all_categories"] == ["unlicensed"]


def test_check_license_weak_copyleft_message(tmp_path):
    _mk(tmp_path, {"Cargo.toml": '[package]\nname = "x"\nlicense = "LGPL-2.1"\n'})
    r = cl.check_license(str(tmp_path))
    assert r["category"] == "weak_copyleft"
    assert r["blocking"] is False
    assert "弱 Copyleft" in r["message"]


def test_check_license_or_later_via_cargo(tmp_path):
    # or-later 后缀原样保留在 license_ids,classify 归一为 strong_copyleft
    _mk(tmp_path, {"Cargo.toml": '[package]\nlicense = "GPL-2.0-or-later"\n'})
    r = cl.check_license(str(tmp_path))
    assert r["license_ids"] == ["GPL-2.0-or-later"]
    assert r["category"] == "strong_copyleft"


def test_check_license_unknown_id_step5_fallback(tmp_path, monkeypatch):
    # Step 5 的 unknown 分支:spdx_ids 中存在 classify 无法识别的 ID。
    # 正常流程(normalize/match)不会产出 unknown ID,此处 monkeypatch
    # 文件识别结果以覆盖该分支(生产代码现状:该分支实际不可达)。
    _mk(tmp_path, {"package.json": '{"license": "MIT"}'})
    monkeypatch.setattr(cl, "detect_license_from_files",
                        lambda sd: (["Custom-License"], "LICENSE"))
    r = cl.check_license(str(tmp_path))
    assert r["license_ids"] == ["MIT", "Custom-License"]
    assert r["category"] == "unknown"
    assert r["needs_ai_fallback"] is True
    assert r["blocking"] is False
    assert r["final_blocking"] is False
    assert "无法识别" in r["message"]
    assert r["all_categories"] == ["permissive", "unknown"]


def test_check_license_gemspec_only(tmp_path):
    (tmp_path / "demo.gemspec").write_text('s.license = "MIT"', encoding="utf-8")
    r = cl.check_license(str(tmp_path))
    assert r["license_ids"] == ["MIT"]
    assert r["category"] == "permissive"
    assert r["source"] == "*.gemspec"


# ─────────────────────────────────────────────
# print_report / main
# ─────────────────────────────────────────────

def _report(**overrides):
    result = {
        "license_ids": ["MIT"], "category": "permissive", "source": "package.json",
        "blocking": False, "needs_ai_fallback": False, "final_blocking": False,
        "message": "MIT 为宽松许可证，直接通过", "all_categories": ["permissive"],
    }
    result.update(overrides)
    return result


def test_print_report_blocking(capsys):
    cl.print_report(_report(license_ids=["BUSL-1.1"], category="no_commercial",
                            blocking=True, final_blocking=True,
                            message="BUSL-1.1 限制商用，不符合 openEuler 开源要求，阻断"), "testpkg")
    out = capsys.readouterr().out
    assert "[testpkg]" in out
    assert "规则阻断" in out
    assert "BUSL-1.1" in out


def test_print_report_ai_fallback(capsys):
    cl.print_report(_report(license_ids=[], category="unknown", needs_ai_fallback=True,
                            message="需要 AI 兜底判断"))
    out = capsys.readouterr().out
    assert "需要 AI 兜底判断" in out
    assert "未识别" in out  # license_ids 空时的占位


def test_print_report_warning(capsys):
    cl.print_report(_report(license_ids=["GPL-3.0"], category="strong_copyleft",
                            message="x"))
    out = capsys.readouterr().out
    assert "警告" in out


def test_print_report_pass(capsys):
    cl.print_report(_report())
    out = capsys.readouterr().out
    assert "通过" in out
    assert "MIT" in out


def test_main_missing_dir(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["check_license.py", "/nonexistent/dir"])
    with pytest.raises(SystemExit) as ei:
        cl.main()
    assert ei.value.code == 1
    assert "目录不存在" in capsys.readouterr().err


def test_main_pass_and_output(tmp_path, monkeypatch, capsys):
    _mk(tmp_path, {"package.json": '{"license": "MIT"}'})
    out_json = tmp_path / "result.json"
    monkeypatch.setattr(sys, "argv",
                        ["check_license.py", str(tmp_path), "-o", str(out_json)])
    with pytest.raises(SystemExit) as ei:
        cl.main()
    assert ei.value.code == 0
    saved = json.loads(out_json.read_text(encoding="utf-8"))
    assert saved["category"] == "permissive"
    assert saved["license_ids"] == ["MIT"]
    assert "结果已写入" in capsys.readouterr().out


def test_main_blocking_exit_code(tmp_path, monkeypatch):
    _mk(tmp_path, {"package.json": '{"license": "BUSL-1.1"}'})
    monkeypatch.setattr(sys, "argv", ["check_license.py", str(tmp_path)])
    with pytest.raises(SystemExit) as ei:
        cl.main()
    assert ei.value.code == 1
