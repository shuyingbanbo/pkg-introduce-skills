# Rust spec 规范

当 `<lang>=rust` 时，spec 初稿应遵循以下规范，优先保证离线可重复构建。

## 1. 适用范围

适用于：
- 存在 `Cargo.toml` + `Cargo.lock` 的 Rust 项目
- 生成二进制（bin）或 C 动态库（cdylib）的项目

不适用（直接阻断）：
- `rust-toolchain.toml` 指定 `channel = "nightly"` → 标准容器不支持
- 需要 `wasm32` 或其他非标准 target
- 依赖私有 registry（非 crates.io）

## 2. 构建前必检（生成 spec 前执行）

> **已自动化，无需手动执行**（更新于 2026-07-13）：以下脚本基于已废弃的容器架构
> （`docker exec ${SESSION_CONTAINER}`），当前 COPR 模式下无此容器，本节代码不会
> 被执行。MSRV / `rust-toolchain.toml` channel 检查现由预检阶段自动完成，见
> `build-rpm/scripts/pre_check_deps.py` 的 `check_rust_toolchain()`（查询 COPR
> 目标 chroot 仓库里 rust 包的实际版本，而非本地/容器内的 rustc）。此处代码块
> 保留仅作历史参考，不应再照此手动执行。

```bash
# 检查工具链要求
if [ -f rust-toolchain.toml ]; then
    REQUIRED=$(grep 'channel' rust-toolchain.toml | grep -oP '"[^"]+"' | tr -d '"')
    if echo "$REQUIRED" | grep -q "nightly\|beta"; then
        echo "BLOCK: 需要 $REQUIRED 工具链，标准容器不支持"
        exit 1
    fi
fi

# 检查 MSRV（Minimum Supported Rust Version）
MSRV=$(grep 'rust-version' Cargo.toml | grep -oP '"[^"]+"' | tr -d '"')
CONTAINER_RUSTC=$(docker exec ${SESSION_CONTAINER} rustc --version | grep -oP '\d+\.\d+\.\d+')
# 若 MSRV > 容器 rustc，必须阻断，不得用 --ignore-rust-version 绕过
```

## 3. vendor 构建（强制要求）

Rust RPM **必须使用 vendor + offline + locked 三件套**，不支持在线构建。

### 3.1 生成 vendor

> COPR 模式下无 `SESSION_CONTAINER`（pkg-builder.md 明确标注），以下命令直接在
> 当前工作目录本地执行，不经过容器。`cargo vendor` 本身需要联网拉取 crate
> 源码才能生成 vendor（这是预检/构建准备阶段，不是离线的 rpmbuild 执行阶段），
> 与 pre_check_deps.py 里 dnf repoquery 联网查询社区源用的是同一个执行环境。

```bash
# action_type=vendor_fetch，必须记录到 build_actions.json
(cd ./sources/${pkgname} && cargo vendor vendor/)
# 验证完整性（每个 crate 必须有 .cargo-checksum.json）
find ./sources/${pkgname}/vendor/ -name '.cargo-checksum.json' | wc -l
# 导出 vendor tarball
tar czf ./sources/${pkgname}/${pkgname}-vendor.tar.gz -C ./sources/${pkgname} vendor/
```

### 3.2 spec 结构

```spec
Source0: https://github.com/<owner>/<repo>/archive/v%{version}.tar.gz#/%{name}-%{version}.tar.gz
Source1: %{name}-vendor.tar.gz

%prep
%autosetup -n %{name}-%{version}
tar xf %{SOURCE1}

# 配置 cargo 使用本地 vendor
mkdir -p .cargo
cat > .cargo/config.toml << 'EOF'
[source.crates-io]
replace-with = "vendored-sources"

[source.vendored-sources]
directory = "vendor"
EOF

%build
export RUSTFLAGS="%{build_rustflags}"
cargo build --release --offline --locked

%install
install -Dm755 target/release/%{name} %{buildroot}%{_bindir}/%{name}
```

### 3.3 关键参数说明

| 参数 | 原因 |
|------|------|
| `--offline` | 禁止访问网络，确保离线可重复构建 |
| `--locked` | 强制使用 Cargo.lock，不更新依赖版本 |
| `%{build_rustflags}` | 注入 openEuler 标准编译 flags（ASLR、PIE 等安全加固） |

**严禁**：`--ignore-rust-version`（掩盖 MSRV 冲突，review-rpm 会标记 E 级错误）

### 3.4 混合包变体（主语言非 rust，源码内含 Cargo.toml）

适用场景：主包是 python / c / cpp / nodejs / ruby 等语言，但源码内嵌 rust 组件
（precheck 的 `secondary_langs` 含 `rust`，`secondary_manifests["rust"]` 给出
Cargo.toml 相对路径，如 pendulum 的 `rust/Cargo.toml`、cryptography 的
`src/rust/Cargo.toml`）。rust 部分的 crate 依赖处理与纯 rust 包相同：
**cargo vendor 打 Source1 + `%prep` 配 `.cargo/config.toml` 离线源**，差异只在
BuildRequires 和 config.toml 的落点目录。

**通用步骤**（两条路径共用）：

```bash
# 1. vendor_fetch（必须记录到 build_actions.json）：在 Cargo.toml 所在目录执行
MANIFEST_DIR=./sources/${pkgname}/<secondary_manifests["rust"] 的父目录>
(cd $MANIFEST_DIR && cargo vendor vendor/)
# 验证完整性（每个 crate 必须有 .cargo-checksum.json）
find $MANIFEST_DIR/vendor/ -name '.cargo-checksum.json' | wc -l
# 打包 vendor  tarball（含 .cargo/config.toml）
tar czf ./sources/${pkgname}/${pkgname}-vendor.tar.gz -C $MANIFEST_DIR vendor/
```

**路径 A — maturin 包**（特征：`pyproject.toml` 的 `[build-system] build-backend = "maturin"`，
如 pendulum）：

```spec
BuildRequires: rust
BuildRequires: cargo
BuildRequires: maturin
Source1: %{name}-vendor.tar.gz

%prep
%autosetup -n %{name}-%{version}
# 在 Cargo.toml 所在目录解 vendor 并配置离线源（以 secondary_manifests 路径为准）
tar xf %{SOURCE1} -C <manifest父目录>
mkdir -p <manifest父目录>/.cargo
cat > <manifest父目录>/.cargo/config.toml << 'EOF'
[source.crates-io]
replace-with = "vendored-sources"

[source.vendored-sources]
directory = "vendor"
EOF

%build
%py3_build   # maturin 检测到 .cargo/config.toml 后自动走 vendored-sources 离线编译
```

**路径 B — setuptools-rust 包**（特征：`pyproject.toml` build-backend 为
`setuptools.build_meta`，或有 `setup.py` + `RustExtension`，如 cryptography）：

```spec
BuildRequires: rust
BuildRequires: cargo
BuildRequires: python3-setuptools-rust
Source1: %{name}-vendor.tar.gz

%prep
%autosetup -n %{name}-%{version}
tar xf %{SOURCE1} -C <manifest父目录>
mkdir -p <manifest父目录>/.cargo
cat > <manifest父目录>/.cargo/config.toml << 'EOF'
[source.crates-io]
replace-with = "vendored-sources"

[source.vendored-sources]
directory = "vendor"
EOF
```

**其他主语言（c / cpp / nodejs / ruby 等）**：vendor + Source1 + `%prep` 配置模式
相同，BuildRequires 只需 `rust` `cargo`（加主语言自身工具链），由主语言构建系统
（cmake / meson / make 等）触发 cargo build 时自动走 vendored-sources。

**注意**：
- 主语言的依赖检查、BuildRequires、spec 结构仍按主语言规则执行，本节只追加 rust 部分
- crate 依赖**不写入** BuildRequires、**不得**用 register-dep.py 注册（由 vendor 保证）
- 系统 C 库（如 openssl-devel）按 pre_check.json 的 `c_library_build_requires[]` 照常填入

## 4. git 依赖处理

Cargo.toml 中含 `git = "..."` 的依赖需特殊处理：

```bash
# 检查 git 依赖数量
git_deps=$(grep -c 'git = ' Cargo.toml || true)
if [ "$git_deps" -gt 0 ]; then
    echo "WARN: 存在 $git_deps 个 git 依赖，需人工确认 vendor/ checksum"
fi
```

git 依赖在 vendor/ 中以 checksum 方式固定版本，`cargo vendor` 会自动处理。若 `.cargo-checksum.json` 缺失或为空对象 `{}`，表示 vendor 不完整，构建会失败。

## 5. 命名规则

| 类型 | 命名规则 | 示例 |
|------|---------|------|
| 二进制工具 | 直接用项目名 | `ripgrep`、`fd-find` |
| 库（cdylib） | `rust-<name>` | `rust-openssl` |
| 系统库包装 | 与 C 库同名 | `libssl-devel` |

## 6. BuildRequires / Requires

```spec
BuildRequires: rust
BuildRequires: cargo
# C 绑定项目额外需要：
BuildRequires: gcc
BuildRequires: openssl-devel  # 按实际依赖填写

# 纯 Rust 静态链接通常无运行时 Requires
# cdylib 项目按实际 .so 填写 Requires
```

## 7. MSRV 冲突处理

| 情况 | 处理 |
|------|------|
| 容器 rustc >= MSRV | 正常构建 |
| 容器 rustc < MSRV | **阻断**，不得使用 `--ignore-rust-version` |
| MSRV 未声明 | 先尝试构建，失败再判断 |

MSRV 冲突的正确解决方案：
1. 引入更新版本的上游（若有低 MSRV 的旧版本）
2. 等待 openEuler 升级 rust 版本
3. 向上游提 PR 降低 MSRV

## 8. 多二进制项目

```spec
%build
export RUSTFLAGS="%{build_rustflags}"
cargo build --release --offline --locked --bins

%install
for bin in target/release/%{name}*; do
    [ -x "$bin" ] && install -Dm755 "$bin" %{buildroot}%{_bindir}/$(basename "$bin")
done
```

## 9. %check

```spec
%check
# 离线测试（不访问网络）
cargo test --offline --locked -- --test-threads=1
```

## 10. 常见问题

| 问题 | 原因 | 处理 |
|------|------|------|
| `cargo: no internet` | 未配置 vendored-sources | 检查 `.cargo/config.toml` |
| `checksum mismatch` | vendor 与 Cargo.lock 不一致 | 重新执行 `cargo vendor` |
| `crate not found in vendor` | vendor 不完整（git 依赖未下载）| `cargo vendor --sync Cargo.toml` |
| `error: package ... not found` | `--locked` 与 Cargo.lock 版本不符 | 用同版本 Cargo.lock 重新生成 vendor |
| `RUSTFLAGS` 导致链接失败 | openEuler flags 与某些 crate 不兼容 | 在 spec 中 unset 冲突 flag |
