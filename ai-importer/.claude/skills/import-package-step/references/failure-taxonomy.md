# 构建失败分类表（pkg-fixer 诊断参考）

pkg-fixer 阶段 2 诊断时对照本表判断失败类别，再回到 pkg-fixer.md 确定 verdict。
结合错误报告、submitted spec 快照、历史修法（fix_instructions.md）三者综合判断，
不得仅凭日志推断 spec 状态；结合包的语言（lang）用语义理解，不要只做字面匹配。

## 类别 A：基础设施 / 网络问题（与语言无关）

| 特征 | verdict |
|------|---------|
| Chroot config not found / Three host tried / copr_base repository not found / results.json file not found / took \d+ seconds.*too fast | `abort` |
| timeout / mirror / Cannot download / Connection refused | `retry-transient`（瞬态，原样重交，不改 spec） |

## 类别 B：缺少依赖（各语言表现不同，用语义理解）

不同语言"缺少依赖"的报错形式各异，根据日志语义判断。
本类别的最终 verdict 由 `check_existing_package.py` 的 decision 写死映射（见 pkg-fixer.md 阶段 2）：
`reuse_official` / `reuse_copr_project` / `reuse_additional_repo` → `rebuild`；`introduce_new` → `retry-dep`。

> ⛔ **缺包判定必须实跑验证，禁止凭先验**：判 `retry-dep`（= 认定官方源/外挂源没有该包）之前，
> 必须对失败 chroot 实跑 `check_existing_package.py` 得出 decision。openEuler everything/EPOL 源的
> 覆盖面远超模型直觉——已有事故：`Could NOT find libzip` 被断言"官方源没有 libzip-devel"而注册新依赖，
> 实际 `libzip-devel` 就在 everything 源，真正缺的只是 spec 里的一行 `BuildRequires`。
> 查询时必须用构建系统**实际需要的名字**：CMake `find_package(X)` / 头文件 / 链接库缺失 → 查 `X-devel`
> （或 `dnf provides '*/xxx.h'` 反查）；只查 runtime 包名 `X` 是假阳性——runtime 在不等于 devel 在。

| 根因语义 | 典型表现（举例，非穷举） |
|---------|----------------------|
| **RPM 包安装失败** | `No matching package to install` / `nothing provides` |
| **CMake 包查找失败** | `Could NOT find Xxx (missing: ...)` / `find_package(Xxx REQUIRED)` 失败——**先对照 submitted spec 确认是否缺对应 `BuildRequires: xxx-devel`**：缺 BR 且官方源可得 → 直接 `rebuild` 加 BR，不要注册依赖 |
| **语言运行时缺模块** | Python: `ModuleNotFoundError` / `ImportError: No module named`；Ruby: `cannot load such file`；Java: `package xxx does not exist` / `cannot find symbol`；Node: `Cannot find module` |
| **语言运行时版本不足** | Python: `TypeError` / `ImportError` + 版本信息；Java: `class file has wrong version` |
| **C/C++ 头文件缺失** | `fatal error: xxx.h: No such file or directory` |
| **pkg-config 缺失** | `Package 'xxx' not found` / `No package 'xxx' found` |
| **链接库缺失** | `cannot find -lxxx` / `undefined reference to` / `ld: library not found for -lxxx` |
| **构建工具版本不足** | `Xxx version is A.B.C but project requires >=X.Y.Z` / `CMake X or higher is required` / `Autoconf version X or higher is required` / `Module "xxx" does not exist`（meson 模块缺失，该模块在更高版本才引入）/ Go: `go.mod requires go >= X.Y` |

**构建工具版本不足**是特例，verdict 固定为 `rebuild`：**修改 spec/源码适应当前 chroot 的工具链版本**；
禁止引入/升级构建工具（红线）；确实无法适配 → `abort`。

**混合包 vendor 语言依赖缺失**也是特例，verdict 固定为 `rebuild`：主包语言不是 go/rust
（如 python），但日志出现 cargo/crates.io 报错（`no matching package named 'xxx' found` /
`failed to download from crates.io`）或 go 报错（`missing go.sum entry` /
`cannot find module providing package`）——说明包内含 Cargo.toml/go.mod 的混合组件需要
vendor（如 pendulum 类）。修法：读 `./pkgs/${PKGNAME}/pre_check.json` 的 `secondary_langs` /
`secondary_manifests`，按对应规范的混合包变体节（rust → spec-rules-rust.md §3.4，
go → spec-rules-go.md §2.4）补 vendor：cargo vendor / go mod vendor 打 Source1 tarball +
`%prep` 解包配离线源。**⛔ 严禁把 crate/module 名用 register-dep.py 注册为依赖**——
crate/module 由父包 vendor 解决，永远不会以 RPM 形式存在；注册了也只会被 supervisor
置 vendor_only 或构建出无意义产物。

## 类别 C：spec 问题（与语言无关）→ `rebuild`

| 特征 | 修法方向 |
|------|---------|
| `cd: <xxx>: No such file or directory`（%prep 失败） | %autosetup -n 目录名错误，应为 `%{name}-%{version}` |
| `fg: no job control` / `bg:` / shell job control 错误（%build 段，configure 已完成，`%cmake_build` 或 `%make_build` 等宏在非交互 shell 中依赖后台任务控制） | 将 `%cmake_build` 替换为 `cmake --build . -j$(nproc)`，将 `%make_build` 替换为 `make -j$(nproc)`；**必须同时保留 `%cmake` 或 `%configure` configure 步骤，只替换 build 步骤** |
| rpmbuild error / bad exit status（spec 语法/宏错误） | 修语法/宏 |
| %check 失败 / 测试未通过 | 修测试或合理跳过 |
| `Installed (but unpackaged) file(s) found`（%files 缺条目） | 补全 %files 列表 |
| `Package name mismatch` / `MISMATCH: build N is X, expected Y`（spec 的 `Name:`/`%global pypi_name`/`Source0:` 写成了另一个包的内容，patch 修不了） | **不是 rebuild，是 `regenerate`**。MISMATCH 次数由 job_runner 在 `fix_state.json` 计数（fix_context 的 `mismatch_count` 可见），第 2 次由 supervisor 直接 fail——fixer 无需翻历史计数 |

> 📌 本类别中 `fg: no job control` / `bg:` 与 `cd: ...: No such file or directory`（%prep）两个 pattern
> 已由 precheck_failure.py 脚本检测并写 `failure_hint_<pkg>_<build_id>.json`（含 spec_patch 建议），
> 是 fixer 阶段 0 必读输入 #5——验证后采用或推翻，脚本不直接改 spec。

> 📌 类别 B 的 `ModuleNotFoundError` / `Cannot find module`，extract-build-failure.py 会在
> `build_failure_<id>.json` 中生成 `missing_module_hints`（语言→RPM 名映射，低置信），可采用或自行修正。

> ⚠️ **常见误判提醒**：以下错误**不是**基础设施/环境问题，属于类别 C，应判 `rebuild`：
> - `fg: no job control` / `bg:` — shell 作业控制错误。只要 configure 阶段已成功，替换 `%cmake_build` → `cmake --build . -j$(nproc)` 即可修复
> - `line X: fg: no job control` — 同上，是 `%cmake_build` 宏展开后的代码，不是 shell 环境缺陷

## 类别 D：无法修复 → `abort`

gcc / python3 / 系统运行时版本不足且无法引入替换、架构不支持、循环依赖、
chroot 不支持的构建系统（如 Gradle）。

## 类别 E：CI 安装验证失败（仅主包发生）

`ci_check_result.json` 存在且 `status: "fail"`：构建成功但 RPM 运行时依赖不闭合
（repoclosure 检查未通过），`errors` 字段列出了缺失的依赖。
dep 构建成功即 build_done，不做安装验证，所以本类别只对主包发生。

**处置规则**：

1. **默认处置**（verdict=`retry-dep`）：将 `errors` 中的缺失依赖逐一注册到 dep_registry，走递归引入：
   ```bash
   python3 $SCRIPTS_DIR/register-dep.py \
     --session-dir . --pkg <缺失包名> --constraint "<版本约束>" --required-by ${PKGNAME}
   ```
   版本约束从 `ci_check_result.json` 的 errors 中提取（如 `astroid >= 3.3`）。

2. **⛔ 严格禁止**通过以下方式"通过"验证（红线）：
   - 删除或注释 spec 中的 `Requires:` 行
   - 添加 `AutoReq: no` 禁用自动依赖生成
   - sed 删除 RPM 的 Requires 元数据

   以上行为属于"消灭证据"，会导致 RPM 表面上可安装但实际运行时 import 失败。

3. **例外**：如果能证明 Requires 是误生成（如 `pythondistdeps` 已知 bug、`~=` 替换产生的虚假约束），
   允许在 spec 中精确过滤该条 Requires（此时按 rebuild 处理）。
