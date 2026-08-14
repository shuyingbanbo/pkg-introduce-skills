"""analyze_java_deps.py — Java 包 RPM 依赖分析(pom/gradle 解析 + mock run_batch_lookup)。"""

from __future__ import annotations

import json
import sys

import pytest

from tests.conftest import SCRIPT_DIRS, load_module

sys.path.insert(0, str(SCRIPT_DIRS["build_rpm"]))
mod = load_module("analyze_java_deps", SCRIPT_DIRS["build_rpm"] / "analyze_java_deps.py")


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


# ─────────────────────────────────────────────
# _strip_ns / shaded 收集
# ─────────────────────────────────────────────

@pytest.mark.parametrize("tag,expected", [
    ("{http://maven.apache.org/POM/4.0.0}dependency", "dependency"),
    ("dependency", "dependency"),
    ("{}", "{}"),     # 空花括号不含内容,原样保留(生产行为)
])
def test_strip_ns(tag, expected):
    assert mod._strip_ns(tag) == expected


POM_WITH_SHADE = """<?xml version="1.0"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example</groupId>
  <artifactId>demo</artifactId>
  <modules>
    <module>sub</module>
  </modules>
  <build>
    <plugins>
      <plugin>
        <artifactId>maven-shade-plugin</artifactId>
        <configuration>
          <artifactSet>
            <includes>
              <include>com.google.guava:guava</include>
              <include>org.slf4j:*</include>
            </includes>
          </artifactSet>
        </configuration>
      </plugin>
    </plugins>
  </build>
</project>
"""


def test_collect_shaded_deps_with_submodule(tmp_path):
    _write(tmp_path, "pom.xml", POM_WITH_SHADE)
    _write(tmp_path, "sub/pom.xml", """<project>
  <build><plugins>
    <plugin>
      <artifactId>maven-shade-plugin</artifactId>
      <configuration><artifactSet><includes>
        <include>com.example:inner</include>
      </includes></artifactSet></configuration>
    </plugin>
  </plugins></build>
</project>
""")
    shaded = mod.collect_shaded_deps(tmp_path)
    assert shaded == {"com.google.guava:guava", "org.slf4j:*", "com.example:inner"}


def test_collect_shaded_deps_missing_pom(tmp_path):
    assert mod.collect_shaded_deps(tmp_path) == set()


@pytest.mark.parametrize("group,artifact,shaded,expected", [
    ("com.google.guava", "guava", {"com.google.guava:guava"}, True),
    ("com.google.guava", "other", {"com.google.guava:guava"}, False),
    ("org.slf4j", "slf4j-api", {"org.slf4j:*"}, True),      # 通配符
    ("org.slf4j", "slf4j-api", set(), False),
])
def test_is_shaded(group, artifact, shaded, expected):
    assert mod._is_shaded(group, artifact, shaded) == expected


# ─────────────────────────────────────────────
# parse_pom
# ─────────────────────────────────────────────

POM_FULL = """<?xml version="1.0"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <properties>
    <maven.compiler.source>11</maven.compiler.source>
  </properties>
  <dependencies>
    <dependency>
      <groupId>org.apache.commons</groupId>
      <artifactId>commons-lang3</artifactId>
      <version>3.12.0</version>
    </dependency>
    <dependency>
      <groupId>junit</groupId>
      <artifactId>junit</artifactId>
      <version>4.13</version>
      <scope>test</scope>
    </dependency>
    <dependency>
      <groupId>org.slf4j</groupId>
      <artifactId>slf4j-api</artifactId>
      <version>2.0.0</version>
      <scope>provided</scope>
    </dependency>
    <dependency>
      <groupId>com.google.guava</groupId>
      <artifactId>guava</artifactId>
      <version>31.0</version>
    </dependency>
    <dependency>
      <groupId>com.google.guava</groupId>
      <artifactId>guava</artifactId>
      <version>31.0</version>
    </dependency>
    <dependency>
      <groupId></groupId>
      <artifactId>no-group</artifactId>
    </dependency>
  </dependencies>
</project>
"""


def test_parse_pom_full(tmp_path):
    _write(tmp_path, "pom.xml", POM_FULL)
    _write(tmp_path, "module-a/pom.xml", "")    # 不干扰
    parsed = mod.parse_pom(str(tmp_path))
    assert parsed["found"] is True
    assert parsed["build_system"] == "maven"
    assert parsed["java_version"] == "11"
    # test/provided scope 跳过、空 groupId 跳过、重复去重
    assert [(d["group"], d["artifact"]) for d in parsed["deps"]] == [
        ("org.apache.commons", "commons-lang3"),
        ("com.google.guava", "guava"),
    ]
    dep = parsed["deps"][0]
    assert dep["version"] == "3.12.0"
    assert dep["scope"] == "compile"


def test_parse_pom_with_shade_exclusion(tmp_path, capsys):
    _write(tmp_path, "pom.xml", """<project>
  <build><plugins>
    <plugin>
      <artifactId>maven-shade-plugin</artifactId>
      <configuration><artifactSet><includes>
        <include>com.google.guava:guava</include>
      </includes></artifactSet></configuration>
    </plugin>
  </plugins></build>
  <dependencies>
    <dependency>
      <groupId>com.google.guava</groupId><artifactId>guava</artifactId><version>31.0</version>
    </dependency>
    <dependency>
      <groupId>org.apache.commons</groupId><artifactId>commons-lang3</artifactId><version>3.12.0</version>
    </dependency>
  </dependencies>
</project>
""")
    parsed = mod.parse_pom(str(tmp_path))
    assert [(d["group"], d["artifact"]) for d in parsed["deps"]] == [
        ("org.apache.commons", "commons-lang3"),   # shaded 的 guava 被排除
    ]
    assert "shade 依赖" in capsys.readouterr().err


def test_parse_pom_missing(tmp_path):
    parsed = mod.parse_pom(str(tmp_path))
    assert parsed == {"build_system": "maven", "deps": [], "java_version": "", "found": False}


def test_parse_pom_broken_xml(tmp_path):
    _write(tmp_path, "pom.xml", "<project><broken>")
    parsed = mod.parse_pom(str(tmp_path))
    assert parsed["found"] is False
    assert parsed["deps"] == []


def test_parse_pom_java_version_property(tmp_path):
    _write(tmp_path, "pom.xml", """<project>
  <properties><java.version>17</java.version></properties>
  <dependencies><dependency><groupId>g</groupId><artifactId>a</artifactId><version>1</version></dependency></dependencies>
</project>
""")
    parsed = mod.parse_pom(str(tmp_path))
    assert parsed["java_version"] == "17"


# ─────────────────────────────────────────────
# parse_gradle
# ─────────────────────────────────────────────

def test_parse_gradle(tmp_path):
    _write(tmp_path, "build.gradle", """
plugins { id 'java' }
sourceCompatibility = '11'
dependencies {
    implementation 'com.google.guava:guava:31.0-jre'
    compile "org.apache.commons:commons-lang3:3.12.0"
    api 'org.slf4j:slf4j-api:2.0.0'
    runtimeOnly 'io.netty:netty-all:4.1.0'
    implementation 'com.google.guava:guava:31.0-jre'
}
""")
    parsed = mod.parse_gradle(str(tmp_path))
    assert parsed["build_system"] == "gradle"
    assert parsed["found"] is True
    assert parsed["java_version"] == "11"
    assert [(d["group"], d["artifact"]) for d in parsed["deps"]] == [
        ("com.google.guava", "guava"),
        ("org.apache.commons", "commons-lang3"),
        ("org.slf4j", "slf4j-api"),
        ("io.netty", "netty-all"),
    ]


def test_parse_gradle_kts(tmp_path):
    # 生产代码 quirk:依赖正则只匹配 `implementation 'g:a:1'` 引号形式,
    # kts 惯用的 implementation("g:a:1") 括号形式提取不到。按实际行为断言。
    _write(tmp_path, "build.gradle.kts", (
        'sourceCompatibility = "17"\n'
        'dependencies { implementation("g:a:1") }\n'
    ))
    parsed = mod.parse_gradle(str(tmp_path))
    assert parsed["build_system"] == "gradle"
    assert parsed["java_version"] == "17"
    assert parsed["deps"] == []
    assert parsed["found"] is False


def test_parse_gradle_missing(tmp_path):
    parsed = mod.parse_gradle(str(tmp_path))
    assert parsed["build_system"] == "gradle"
    assert parsed["found"] is False
    assert parsed["deps"] == []


# ─────────────────────────────────────────────
# detect_build_system
# ─────────────────────────────────────────────

@pytest.mark.parametrize("files,expected", [
    (["pom.xml"], "maven"),
    (["build.gradle"], "gradle"),
    (["build.gradle.kts"], "gradle"),
    ([], "unknown"),
])
def test_detect_build_system(tmp_path, files, expected):
    for f in files:
        _write(tmp_path, f, "")
    assert mod.detect_build_system(str(tmp_path)) == expected


# ─────────────────────────────────────────────
# build_lookup_tasks / check_rpm_availability
# ─────────────────────────────────────────────

def test_build_lookup_tasks():
    deps = [{"group": "g", "artifact": "a", "version": "1", "scope": "compile"}]
    tasks = mod.build_lookup_tasks(deps)
    assert len(tasks) == 1
    t = tasks[0]
    assert t["dep"] == "g:a"
    assert t["group"] == "g"
    assert [q["value"] for q in t["queries"]] == ["mvn(g:a)"]
    assert t["queries"][0]["level"] == "mvn()"


def test_check_rpm_availability(monkeypatch):
    def fake(tasks, timeout=120, **kw):
        out = []
        for t in tasks:
            base = {k: v for k, v in t.items() if k not in {"queries", "prefer_devel"}}
            if t["dep"] == "g:a":
                out.append({**base, "rpm": "a", "version": "1.0", "release": "1", "level": "mvn()"})
            else:
                out.append({**base, "rpm": None, "version": None, "release": None, "level": ""})
        return out
    monkeypatch.setattr(mod, "run_batch_lookup", fake)
    deps = [
        {"group": "g", "artifact": "a", "version": "1", "scope": "compile"},
        {"group": "g", "artifact": "b", "version": "1", "scope": "compile"},
    ]
    result = mod.check_rpm_availability(deps=deps)
    avail = result["available"][0]
    assert avail["group"] == "g" and avail["artifact"] == "a"
    assert avail["rpm"] == "a"
    assert "queries" not in avail and "level" not in avail
    miss = result["missing"][0]
    assert miss["group"] == "g" and miss["artifact"] == "b"
    assert "queries" not in miss and "rpm" not in miss


def test_check_rpm_availability_fallback_on_error(monkeypatch):
    def boom(tasks, timeout=120, **kw):
        raise mod.BatchLookupError("boom")
    monkeypatch.setattr(mod, "run_batch_lookup", boom)
    result = mod.check_rpm_availability(deps=[{"group": "g", "artifact": "a", "version": "1", "scope": "compile"}])
    assert result["available"] == []
    assert result["missing"][0]["artifact"] == "a"


# ─────────────────────────────────────────────
# resolve_jdk_pkg / build_rpm_requires
# ─────────────────────────────────────────────

@pytest.mark.parametrize("java_version,expected", [
    ("1.7", "java-1.8.0-openjdk-devel"),   # 1.7 映射到 1.8
    ("7", "java-1.8.0-openjdk-devel"),
    ("1.8", "java-1.8.0-openjdk-devel"),
    ("8", "java-1.8.0-openjdk-devel"),
    ("11", "java-11-openjdk-devel"),
    ("17", "java-17-openjdk-devel"),
    ("21", "java-21-openjdk-devel"),
    ("23", "java-1.8.0-openjdk-devel"),    # 未知 → 默认
    ("", "java-1.8.0-openjdk-devel"),
])
def test_resolve_jdk_pkg(java_version, expected):
    assert mod.resolve_jdk_pkg(java_version) == expected


@pytest.mark.parametrize("bs,java_version,expected", [
    ("maven", "11", ["java-11-openjdk-devel", "maven-local"]),
    ("maven", "", ["java-1.8.0-openjdk-devel", "maven-local"]),
    ("gradle", "17", ["java-17-openjdk-devel", "gradle-local"]),
    ("unknown", "", ["java-1.8.0-openjdk-devel"]),
])
def test_build_rpm_requires(bs, java_version, expected):
    assert mod.build_rpm_requires(bs, java_version, None) == expected


def test_build_rpm_requires_with_rpm_check():
    rpm_check = {"available": [{"rpm": "maven-local"}, {"rpm": "commons-lang3"}, {"rpm": "commons-lang3"}],
                 "missing": []}
    result = mod.build_rpm_requires("maven", "11", rpm_check)
    assert result == ["java-11-openjdk-devel", "maven-local", "commons-lang3"]


# ─────────────────────────────────────────────
# print_report / main
# ─────────────────────────────────────────────

def test_print_report(capsys):
    parsed = {"build_system": "maven", "java_version": "11", "found": True,
              "deps": [{"group": "g", "artifact": "a", "version": "1", "scope": "compile"}]}
    rpm_check = {"available": [{"group": "g", "artifact": "a", "rpm": "a"}],
                 "missing": [{"group": "g", "artifact": "b"}]}
    mod.print_report(parsed, rpm_check)
    out = capsys.readouterr().out
    assert "Java 包 RPM 依赖分析报告" in out
    assert "构建系统 : maven" in out
    assert "g:a" in out
    assert "BuildRequires: java-11-openjdk-devel" in out


def test_main_output_json(tmp_path, capsys, monkeypatch):
    _write(tmp_path, "pom.xml", POM_FULL)
    out_json = tmp_path / "result.json"
    monkeypatch.setattr(sys, "argv", ["analyze_java_deps.py", str(tmp_path), "-o", str(out_json)])
    mod.main()
    result = json.loads(out_json.read_text())
    assert result["build_system"] == "maven"
    assert result["java_version"] == "11"
    assert result["build_requires"] == ["java-11-openjdk-devel", "maven-local"]


def test_main_unknown_build_system(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["analyze_java_deps.py", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1
    assert "未找到 pom.xml 或 build.gradle" in capsys.readouterr().err


def test_main_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["analyze_java_deps.py", str(tmp_path / "nope")])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1


def test_main_check_rpm_missing_exit2(tmp_path, monkeypatch):
    _write(tmp_path, "pom.xml", """<project><dependencies>
  <dependency><groupId>g</groupId><artifactId>a</artifactId><version>1</version></dependency>
</dependencies></project>
""")
    monkeypatch.setattr(sys, "argv", ["analyze_java_deps.py", str(tmp_path), "--check-rpm"])
    def fake(tasks, timeout=120, **kw):
        out = []
        for t in tasks:
            base = {k: v for k, v in t.items() if k not in {"queries", "prefer_devel"}}
            out.append({**base, "rpm": None, "version": None, "release": None, "level": ""})
        return out
    monkeypatch.setattr(mod, "run_batch_lookup", fake)
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 2
