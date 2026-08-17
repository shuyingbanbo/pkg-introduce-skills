# Python spec 规范

## Source0 规则（最高优先级）

**统一使用 GitHub 源码（git clone），不使用 PyPI sdist**。

原因：PyPI sdist 不包含 git submodule（如 vendored-meson、vendored 构建工具等），会导致依赖 submodule 的包构建失败。GitHub 源码包含完整内容，是最可靠的来源。

**Source0 写法**：
```spec
Source0: https://github.com/<owner>/<repo>/archive/refs/tags/v%{version}/%{name}-%{version}.tar.gz
```

或直接用 commit/tag 的 archive URL。

**硬链接问题处理**：GitHub archive 含硬链接，`tar --transform` 会报 `Cannot hard link: Not a directory`。
build-rpm skill 在 §4 打包时必须加 `--hard-dereference`：

```bash
tar --hard-dereference -czf ./build/SOURCES/<pkgname>-${VERSION_STR}.tar.gz \
  --transform "s|^./sources/<pkgname>|<pkgname>-${VERSION_STR}|" \
  ./sources/<pkgname>/
```

**vendored 构建工具**：若 `pyproject.toml` 里有 `[tool.meson-python] meson = 'vendored-xxx/...'`，说明包使用自带的构建工具，spec 里**不加系统 meson/cmake 等 BuildRequires**，也**不在 %prep 里删除 vendored 目录**。

**git submodule**：若源码有 submodule（`git submodule status` 有输出），build-rpm skill 在 clone 后必须执行：
```bash
git -C ./sources/<pkgname> submodule update --init --recursive
```
确保 vendored 内容完整后再打包。

---

## 第一步：确定构建方案

生成 spec 前，按以下顺序做三个判断：

### 判断1：是否是 bootstrap 包？

如果满足以下任一条件，认定为 bootstrap 包，**直接跳到方案 pip-bootstrap**：
- `pyproject.toml` 的 `[build-system].requires` 包含包自身名称（如 flit-core 依赖 flit-core）
- 包是构建工具本身（flit-core、setuptools、hatchling、pdm-backend、poetry-core 等）

### 判断2：当前 chroot 是哪个平台？

| chroot | 可用宏 | 选方案 |
|--------|--------|--------|
| `openeuler-22.03*` | 无 pyproject-rpm-macros | → 方案 pip |
| `openeuler-24.03_LTS` / `openeuler-24.03_LTS_SP1` | 无 pyproject-rpm-macros | → 方案 pip |
| `openeuler-24.03_LTS_SP2` 及以上 | pyproject-rpm-macros 可用 | → 方案 pyproject 或 pip |

> 当前默认 chroot：`openeuler-24.03_LTS_SP2-x86_64`，可用 pyproject 宏。

### 判断3：项目用什么构建系统？

```
判断顺序：

1. setup.py 存在且无 pyproject.toml → **方案 setup.py**
   原因：setup.py 方案使用 %py3_build/%py3_install，不依赖 wheel/pip，构建最简单稳定

2. pyproject.toml 存在：
   ├── build-backend = mesonpy / meson-python → **方案 pyproject**
   │   BuildRequires 必须包含 python3-pip、pyproject-rpm-macros、python3-meson-python、ninja-build
   │   不加 BuildRequires: meson（若 pyproject.toml 里有 [tool.meson-python] meson = 'vendored-xxx'，用 vendored meson）
   ├── build-backend = setuptools.build_meta  → **方案 setup.py**（setuptools 路径）
   ├── build-backend = hatchling/flit/pdm 等  → **方案 pyproject**（SP2+）或 **方案 pip**（其他）
   └── 无 build-backend 字段                  → **方案 setup.py**（setuptools 路径）

3. 既无 pyproject.toml 也无 setup.py → 方案 pip-bootstrap
```

---

## 第二步：按方案生成 spec

### 方案 pyproject（仅 SP2+ 可用）

适用场景：24.03-SP2+，build-backend 为 hatchling / flit / pdm 等（非 setuptools）。

**BuildRequires：**
```spec
BuildRequires:  python3-devel
BuildRequires:  python3-pip
BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3-<build-backend-rpm-name>
```

> `python3-<build-backend-rpm-name>` 示例：
> - `flit_core.buildapi` → `python3-flit-core`
> - `hatchling.build` → `python3-hatchling`
> - `pdm.pep517.api` → `python3-pdm-pep517`

**%build / %install：**
```spec
%build
%pyproject_build

%install
%pyproject_install
```

**%files：**
```spec
%files -n python3-%{pypi_name}
%license LICENSE
%{python3_sitelib}/<module>/
%{python3_sitelib}/<dist_name>-%{version}*.dist-info/
```

---

### 方案 pip（全平台兼容）

适用场景：22.03、24.03 LTS/SP1，或 SP2+ 上使用 setuptools/无宏路径。

**BuildRequires：**
```spec
BuildRequires:  python3-devel
BuildRequires:  python3-pip
BuildRequires:  python3-setuptools
```

**%build / %install：**
```spec
%build
pip3 wheel --no-build-isolation --no-deps --wheel-dir %{_builddir}/wheels .

%install
pip3 install --no-build-isolation --no-index \
    --find-links %{_builddir}/wheels \
    --root %{buildroot} --prefix /usr \
    %{pypi_name}==%{version}
```

**%files：**
```spec
%files -n python3-%{pypi_name}
%license LICENSE
%{python3_sitelib}/<module_dir>/
%{python3_sitelib}/<dist_name>-%{version}*.dist-info/
```

---

### 方案 setup.py（传统 setuptools）

适用场景：存在 `setup.py`（不管有没有 `pyproject.toml`）。

**BuildRequires：**
```spec
BuildRequires:  python3-devel
BuildRequires:  python3-setuptools
```

**%build / %install：**
```spec
%build
%py3_build

%install
%py3_install
```

**%files：**
```spec
%files -n python3-%{pypi_name}
%license LICENSE
%{python3_sitelib}/<module_dir>/
%{python3_sitelib}/<dist_name>-%{version}*.egg-info/
```

---

### 方案 pip-bootstrap（bootstrap 包专用）

适用场景：flit-core、setuptools、hatchling、pdm-backend 等构建工具包本身。

**关键原则：**
- `--no-build-isolation` 会忽略 `[build-system].requires`，不需要 build-backend 预装
- BuildRequires **不写**包自身作为依赖（不写 `python3-flit-core` 来构建 flit-core）
- 强制使用 pip 路径，不使用 `%pyproject_build`（即使在 SP2+）

```spec
BuildRequires:  python3-devel
BuildRequires:  python3-pip
BuildRequires:  python3-setuptools

%build
pip3 wheel --no-build-isolation --no-deps --wheel-dir %{_builddir}/wheels .

%install
pip3 install --no-build-isolation --no-index \
    --find-links %{_builddir}/wheels \
    --root %{buildroot} --prefix /usr \
    %{pypi_name}==%{version}
```

---

### 方案 C 扩展

适用场景：存在 `.c`、`.pyx` 文件，或 `setup.py` 中有 `Extension()` 调用，或 PyPI wheel 含架构标记（如 `cp311-cp311-linux_x86_64`）。

注意：C 扩展包**不设 `BuildArch: noarch`**，必须在 `%build` 前禁用 LTO + BTI。

**生成 spec 前必须检查源码目录：**

```bash
# 检查是否有 CMakeLists.txt
ls sources/<pkgname>/CMakeLists.txt 2>/dev/null && echo "HAS_CMAKE"
# 检查是否有 .pyx 文件
find sources/<pkgname> -name "*.pyx" | head -1
```

**BuildRequires：**
```spec
BuildRequires:  python3-devel
BuildRequires:  python3-setuptools
BuildRequires:  gcc
# 若源码有 CMakeLists.txt，必须加：
# BuildRequires:  cmake
# 链接的系统库 -devel 包（如 libpq-devel）：见下方"链接库"说明
# 若有 .pyx 文件：
# BuildRequires:  python3-Cython
```

> **链接库 BuildRequires**：C 扩展链接的系统库（如 `libpq-devel`、`openssl-devel`）
> 不要靠猜。预检阶段已从 `setup.py` 的 `Extension(libraries=[...])` 和 `.pyx` 的
> `# distutils: libraries` 声明中解析链接库，并在目标 chroot 源中验证存在性，结果写入
> `pre_check.json` 的 `c_library_build_requires[]`。**直接读该字段填入 BuildRequires**。
> 该字段只含已验证存在的包；若某链接库没被解析出来或源中不存在（如库名是变量拼接、
> 或用 pkg-config 动态探测），则不在字段中，交由构建失败诊断循环（`fatal error: xxx.h`）兜底。

> **重要**：若源码目录存在 `CMakeLists.txt`，必须加 `BuildRequires: cmake`，
> 否则 cmake 生成的头文件（如 `encodings.h`）不会生成，`%build` 会报 `No such file or directory`。

**%build / %install：**
```spec
%build
%define _lto_cflags %{nil}
%undefine _hardened_build
%py3_build

%install
%py3_install
```

**%files：**
```spec
%files -n python3-%{pypi_name}
%license LICENSE
%{python3_sitearch}/<module>/
%{python3_sitearch}/<dist_name>-%{version}*.egg-info/
```

---

## 第三步：完整 spec 结构

所有方案共用以下 spec 骨架，只有 BuildRequires / %build / %install / %files 部分按方案替换。

```spec
%global pypi_name <pypi_name>

Name:           python-%{pypi_name}
Version:        <version>
Release:        1%{?dist}
Summary:        <one-line summary>
License:        <SPDX license identifier>
URL:            <upstream homepage>
Source0:        <见下方 Source0 规范>
BuildArch:      noarch          # C 扩展包删除此行

%description
<multi-line description>


%package -n python3-%{pypi_name}
Summary:        <one-line summary>
Provides:       python-%{pypi_name}
Provides:       python3dist(%{pypi_name}) = %{version}
<BuildRequires 按方案填写>
<Requires 只写运行时必须的包>

%description -n python3-%{pypi_name}
<multi-line description>


%package help
Summary:        Development documents and examples for %{pypi_name}
Provides:       python3-%{pypi_name}-doc
%description help
<multi-line description>


%prep
%autosetup -n %{name}-%{version} -p1


<build / install 按方案填写>


%files -n python3-%{pypi_name}
<按方案填写>

%files help
%doc README.md


%changelog
* <date> Python_Bot <Python_Bot@openeuler.org> - <version>-1
- Initial package
```

---

## 规则手册

### 命名规则（双包模式）

openEuler 社区采用**双包模式**：SRPM 用 `python-` 前缀，二进制包用 `python3-` 前缀。
spec 顶部必须声明 `%global pypi_name`，后续字段通过 `%{pypi_name}` 引用，避免硬编码包名导致 pip 无法识别。

| 字段 | 规则 | 示例（PyPI 名 `requests`） |
|------|------|--------------------------|
| `%global pypi_name` | 定义 PyPI 名 | `%global pypi_name requests` |
| `Name:`（SRPM 名） | `python-%{pypi_name}` | `python-%{pypi_name}` → `python-requests` |
| `%package -n`（二进制包名） | `python3-%{pypi_name}` | `python3-%{pypi_name}` → `python3-requests` |
| `Requires:` | `python3-<dep>` | `python3-click >= 8.0` |
| `pip3 install` 目标 | `%{pypi_name}` | `%{pypi_name}==%{version}` → pip 可识别 |

> **为什么必须用 `%{pypi_name}`**：`%{name}` 展开为 `python-requests`（含前缀），pip 不识别 RPM 包名。
> `%{pypi_name}` 展开为 `requests`，与 `pip3 wheel` 产出的 wheel 文件名匹配。

特殊情况：

| 场景 | srpm_name | rpm_pkg_name |
|------|-----------|--------------|
| 带下划线 | `python-typing-extensions` | `python3-typing-extensions` |
| PyPI 名含 `python-` 前缀 | `python-python-multipart` | `python3-python-multipart` |
| 大写名（Django）| `python-django` | `python3-django` |

若其他包的 `Requires` 可能写成 `python3-%{pypi_name}`，需在 spec 中显式声明：
```spec
Provides:       python3-%{pypi_name} = %{version}-%{release}
```

### 版本格式

| 上游版本 | RPM Version |
|---------|-------------|
| `1.2.3` | `1.2.3` |
| `1.2.3b0`（beta） | `1.2.3~b0` |
| `1.2.3rc1` | `1.2.3~rc1` |
| `1.2.3.post1` | `1.2.3^post1` |
| `1.2.3.dev0` | `1.2.3~dev0` |

`~` 低于基础版本（pre-release），`^` 高于基础版本（post-release）。

### Source0 规范

**PyPI 包**（推荐）：
```spec
Source0: %{pypi_source}                        # 包名与 PyPI 名相同
Source0: %{pypi_source <pypi_name>}             # 包名与 PyPI 名不同时
```

**GitHub 包**：
```spec
Source0: %{url}/archive/v%{version}/%{name}-%{version}.tar.gz
```

Source0 **必须**填写完整 URL，不得只写文件名。

### %files 填写规则

必须根据实际安装产物填写，不要照搬模板占位符。

| 类型 | 路径 |
|------|------|
| 纯 Python 模块目录 | `%{python3_sitelib}/<module>/` |
| C 扩展模块目录 | `%{python3_sitearch}/<module>/` |
| 单文件模块 | `%{python3_sitelib}/<module>.py` |
| dist-info | `%{python3_sitelib}/<Name>-%{version}*.dist-info/` |
| egg-info | `%{python3_sitelib}/<Name>-%{version}*.egg-info/` |
| 命令行入口 | `%{_bindir}/<command>` |
| license 文件 | `%license LICENSE` |
| 文档 | `%doc README.md CHANGELOG.md` |

注意：
- `dist-info` 目录名中连字符和下划线可能不一致，用 `*` 通配
- C 扩展包用 `%{python3_sitearch}`，纯 Python 包用 `%{python3_sitelib}`，不混用

### BuildRequires / Requires 原则

- `BuildRequires` 只写构建时真正需要的包
- `Requires` 只写运行时必须的包，版本约束从 `pyproject.toml` 读取真实约束
- **不写**测试依赖（pytest、coverage、tox 等）
- **不写** build-backend 的自身依赖（bootstrap 包场景）
- 运行时依赖以 `rpmbuild` 实际失败为准，不机械翻译上游依赖列表

### %changelog 格式

```spec
%changelog
* Fri Jun 20 2026 Python_Bot <Python_Bot@openeuler.org> - 1.2.3-1
- Initial package
```

日期格式：`%a %b %d %Y`（英文，与 `date "+%a %b %d %Y"` 输出一致）。

### 禁止行为

- 在 22.03 / 24.03 LTS / 24.03 SP1 上使用 `%pyproject_build` 或 `pyproject-rpm-macros`
- 无条件启用 `%pyproject_save_files`（当前宏实现不稳定）
- 把 bootstrap 包自身写入 BuildRequires（如构建 flit-core 时写 `BuildRequires: python3-flit-core`）
- 复用上次会话遗留的 spec 文件（每次构建必须重新生成）
- 在初稿阶段写入大量未经验证的 `BuildRequires`
