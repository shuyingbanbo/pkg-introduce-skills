# ROS spec 规范

当 `<lang>=ros`（伪 gate_result 由 `ros_prep.py` 产出，`ros_pkg_manifest.json` 提供包元数据）时，spec 初稿应遵循以下规范。ROS 包构建进 `/opt/ros/%{ros_distro}` 前缀，与普通包（`/usr`）的布局、依赖模型、二进制扫描策略完全不同——**严禁按普通包经验写 ROS spec**。

## 1. 适用范围

适用于：
- `package.xml` 存在、由 ament/colcon 构建的 ROS 2 包（ament_cmake / ament_python）
- 纯 CMake 的 ROS 风格包（3rdparty、vendor 包）
- ROS 依赖元包（`ros_workspace` 等基座包）

**包名纪律（最高优先级）**：
- `Name: ros-%{ros_distro}-%{RosPkgName}`，其中 `%define ros_distro humble`（取自 `session.json` 的 `ros_distro` 字段，**禁止硬编码**），`%define RosPkgName <包名>`（package.xml 的 `<name>`）
- 例：`Name: ros-humble-rclcpp`、`Name: ros-humble-ament-cmake`
- **严禁**在 spec 中硬编码 `/opt/ros/humble`、`humble`、具体版本号——一律走 `%{ros_distro}` / `%{RosPkgName}` 宏。这是 ROS spec 最容易出错的地方（rpmbuild 宏展开在 `%prep` 前，`%{ros_distro}` 必须用 `%define` 而非 `%global` 时机错误的写法——两者在本文件中统一用 `%define`，放在 `Name:` 之前）

## 2. 命名与基础结构

```spec
%define ros_distro humble
%define RosPkgName rclcpp

Name:       ros-%{ros_distro}-%{RosPkgName}
Version:    <清单版本（去发布号）>
Release:    1%{?dist}
Summary:    <package.xml 的 <description> 或摘要>
License:    <package.xml 的 <license>，多许可证空格分隔>
Source0:    <上游仓库 URL>/archive/<ref>.tar.gz

# debug 子包无意义（/opt 前缀，strip/debugedit 路径非常规），SIG 模板统一关闭
%global debug_package %{nil}
# /opt 下 python 字节码编译会写标准 sitelib 路径，摘掉 brp-python-bytecompile
%global __os_install_post %(echo '%{__os_install_post}' | sed -e 's!/usr/lib[^[:space:]]*/brp-python-bytecompile[[:space:]].*$!!g')
# ROS 包不做自动依赖扫描（.so 全在 /opt/ros 下，由 ament 间接依赖模型替代）
%global __provides_exclude_from ^/opt/ros/%{ros_distro}/.*$
%global __requires_exclude_from ^/opt/ros/%{ros_distro}/.*$
```

**Version 来源**：`ros_pkg_manifest.json` 的 `target_version`（格式 `2.0.2-3`，发布号 `-3` 剥离），缺省时回退 `listed_version`。`target_version` 的取值优先级：用户显式指定 > 清单版本（SIG 清单 / rosdistro 全量清单）。package.xml 的 `<version>` 只作最后兜底（那是上游开发版本，可能落后于发布版本）。

## 3. 安装前缀（所有形态强制）

- CMake 形态：`-DCMAKE_INSTALL_PREFIX="/opt/ros/%{ros_distro}"`
- 同时注入 ament 前缀（让 colcon/ament_cmake 找到已装包）：
  `-DAMENT_PREFIX_PATH="/opt/ros/%{ros_distro}"`、`-DCMAKE_PREFIX_PATH="/opt/ros/%{ros_distro}"`
- ament 布局强制（禁用 ament 的默认前缀拆分，全部装进前缀内）：
  `-UINCLUDE_INSTALL_DIR -ULIB_INSTALL_DIR -USYSCONF_INSTALL_DIR -USHARE_INSTALL_PREFIX -ULIB_SUFFIX`
- Python 布局强制：`-DSETUPTOOLS_DEB_LAYOUT=OFF`——ament_cmake_python 类包 Python 部分安装布局的关键参数，缺失会把 python 文件装进 deb 风格的 dist-packages 路径（SIG 模板对所有包统一带上，无害）
- Python 形态：`export PYTHONPATH=/opt/ros/%{ros_distro}/lib/python%{python3_version}/site-packages`，`%{python3_sitearch}` 替换为 `/opt/ros/%{ros_distro}/lib/python%{python3_version}/site-packages`

## 4. 构建 bootstrap（`%build` / `%install` 开头）

```bash
# ROS 基座环境（chroot 已配置 ROS SIG repo，ros-humble-ros-workspace 等已安装）
source /opt/ros/%{ros_distro}/setup.sh 2>/dev/null || true
export PYTHONPATH=/opt/ros/%{ros_distro}/lib/python%{python3_version}/site-packages${PYTHONPATH:+:$PYTHONPATH}
export PKG_CONFIG_PATH=/opt/ros/%{ros_distro}/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}
```

## 5. 构建系统形态判定（读 package.xml 的 `<build_type>`，`ros_pkg_manifest.json` 可辅助）

| 形态 | 判定 | 构建要点 |
|------|------|---------|
| ament_cmake | `<build_type>ament_cmake</build_type>`（默认） | 显式 out-of-source 构建：`mkdir -p .obj && cd .obj && %cmake3 <§3 参数> ..`（或 `cmake -S .. -B .`）+ `make` + `make install DESTDIR=%{buildroot}`；**避免在 spec 顶层直接 `%cmake`**——openEuler 的 `%cmake` 宏带位置参数时会错误解析源码目录（build 444 实踩）；子目录为 `share/ament_index`、`share/<pkg>`、`lib` |
| ament_python | `<build_type>ament_python</build_type>` | `%{__python3} setup.py install` 或 `%py3_install`，前缀同上；`--install-layout` 由 `PYTHONPATH` 兜底 |
| 纯 cmake（3rdparty） | 无 ament 标记，上游纯 CMake | 标准 `%cmake` 流程 + §3 前缀 |
| vendor 包 | `_vendor` 后缀 / manifest 标 3rdparty | 见 §8 参考资产 |

**禁用测试优先转构建参数，而非 patch**：
- 优先 `-DBUILD_TESTING=OFF -DTESTING=OFF -DCMAKE_SUPPRESS_REGENERATION=ON` 等参数关闭
- 参数无法关闭时，其次考虑 `%cmake_build` 后删除测试目标；**最后**才用 patch
- 测试依赖（package.xml `<test_depend>`）**不写 BuildRequires**

## 6. 依赖填写（BuildRequires / Requires 纪律）

- 读 `ros_pkg_manifest.json`：
  - `official_deps_rpm[]` → 直接写 BuildRequires（官方 ROS SIG repo 已有，如 `ros-humble-ament-cmake-core`）
  - `official_deps[]`（原始名）→ 写作 `ros-%{ros_distro}-<dep>`
  - `registered_deps[]` → 已注册进 dep_registry 的依赖，**写 BuildRequires**（同一 COPR project 构建，安装时同源解析）
  - `missing_deps[]` → 缺口包，**不写 BuildRequires**（显式模式任务已终止，此文件不应出现在 spec 阶段）
- 系统依赖（`analyze_ros_deps.py` 的 `build_requires[]` / `unresolved[]` 经 `--check-rpm` 实证）→ 普通 BuildRequires（`-devel` 命名）
- **反幻觉铁律**：禁止凭 Fedora/Ubuntu ROS 经验猜依赖名。每个 `ros-humble-*` BuildRequires 必须能在 `ros-projects.list`（SIG 源已有）、`ros-upstream.list`（rosdistro 全量，SIG 未移植的可递归构建）或 manifest 的依赖清单里找到依据；查不到就留空让构建失败诊断循环兜底，不得编造。**机械门禁**：spec 写完后由 `verify_ros_spec_deps.py`（SKILL §3.6）逐一比对两级清单，幻觉名直接打回；`submit_fix.py` 提交前同样强制执行
- **`<build_type>` 不是依赖**：`ament_python` 形态是纯 setuptools 构建，**不产生任何 `ros-<distro>-*` 依赖**（清单中不存在 `ament-python`，凭 build_type 脑补它是历史实踩坑）；`ament_cmake` 形态的构建工具依赖是 `ros-%{ros_distro}-ament-cmake`，由 `analyze_ros_deps.py` 自动补入 ros_deps
- `pkg.remap` 命中（deb→rpm 映射，`data/ros/global_config/pkg.remap`）→ 按 rpm 名写

**运行时 Requires（ROS 特有，必须手写）**：`/opt/ros` 被 §2 的 `__requires_exclude_from` 豁免、autodeps 扫不到，少了这些 Requires 构建照样成功，但装上的包环境不就绪：
- `Requires: ros-%{ros_distro}-ros-workspace`——提供 `/opt/ros/<distro>/setup.sh` 与环境 hooks，装上即环境就绪（所有 ROS 包强制）
- `Requires: ros-%{ros_distro}-ament-cmake`——ament 运行期钩子需要（ament_cmake / ament_python 形态强制）
- package.xml 的 `<exec_depend>` / `<run_depend>` → 按上面同样的映射规则写成 Requires（`ros-%{ros_distro}-<dep>` 或系统包名）

## 7. 子包策略与 %files（强制）

- **单包策略**：ROS 包**严禁拆实体子包**（`%package devel` 等）——主包 %files 整树拥有时必然与 devel 的路径列表重叠，同一路径被两个子包拥有导致安装事务冲突。改用虚拟别名兼容依赖方：
  ```spec
  Provides:       %{name}-devel = %{version}-%{release}
  Provides:       %{name}-doc = %{version}-%{release}
  Provides:       %{name}-runtime = %{version}-%{release}
  ```
- **%files 整树拥有**：`/opt/ros/%{ros_distro}` 一行收尾，对「装了没打包」类错误免疫（SIG 模板标准做法）；**不要**逐目录精确列——既易漏（换包重踩），又易与子包路径重叠。
- **%changelog**：日期用 `date` 实算（星期与日期必须一致，手算易出 bogus date warning）。

## 8. 参考资产（必须查）

- **spec 基线**：`./pkgs/<pkgname>/reference/<pkgname>.spec`（ros_fetch 从 cache 拷入）存在时，**以其为起点做 diff 审查**，而不是从零写（对应 ros-porting-tools 的 pkg-update 思路）：对照 §2-§6 纪律核对后适配版本/路径，保留其架构修正
- **76 包修正资产**：`/app/.claude/skills/build-rpm/scripts/data/ros/humble/package_fix/<pkg>/`，本包名存在时**必须读**：
  - `source.fix`（上游源码修正说明）、`prep.fix`（%prep 阶段修正，如 vendor 源码替换、patch 应用清单）
  - `custom.spec`（该包的全旁路 spec 参考）
  - `BuildRequires` / `Requires` / `Provides`（修正后的依赖清单，`-` 前缀=删除官方默认，`+` 前缀=添加）
  - `*.patch`（历史构建修正补丁，按需应用，不盲目）
  - `README.md`（该包构建要点说明）
- 修正资产的 `-` 前缀条目必须落实（官方默认依赖在 openEuler 不成立）

## 9. 常见失败模式（构建失败诊断参考）

| 症状 | 根因 | 处理 |
|------|------|------|
| `%cmake` 报源码目录/参数解析错误 | openEuler `%cmake` 宏位置参数坑 | 改显式 out-of-source：`mkdir -p .obj && cd .obj && %cmake3 ..`（§5） |
| 同一路径被主包和 devel 子包重复拥有 | 拆了实体 `%package devel` 且主包 %files 整树拥有 | 单包 + 虚拟 Provides 别名（§7） |
| `bogus date` warning | %changelog 星期与日期不符 | 日期用 `date` 实算（§7） |
| 装上后 ROS 环境不就绪（无 setup.sh） | 缺 `Requires: ros-%{ros_distro}-ros-workspace` | §6 运行时 Requires 三件套 |
| `ament_cmake` not found | BuildRequires 缺 `ros-humble-ament-cmake` | 补基座依赖 |
| `package 'rclcpp' not found`（cmake） | 缺依赖包或前缀参数漏 `-DCMAKE_PREFIX_PATH` | 补依赖 / 查 §3 参数 |
| `setup.sh: No such file or directory` | chroot 未装 `ros-humble-ros-workspace` / ROS SIG repo 未挂 | SIG 源由项目级 additional_repos 注入（2026-08 起自动配置），仍出现则查注入是否生效 |
| python 模块装进 `/usr/lib` | 漏 `PYTHONPATH` 前缀注入 | §4 bootstrap |
| `.so` 被自动打包 | 漏 `%global __provides_exclude_from` | §2 豁免 |
| vendor 包下载网络失败 | 上游 CMake FetchContent | 查 `prep.fix` / `custom.spec` 是否已有本地化方案 |
