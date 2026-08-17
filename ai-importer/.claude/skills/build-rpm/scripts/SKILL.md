---
name: build-rpm
description: RPM 构建核心（COPR 模式）：spec 生成 + rpmbuild -bs 打 SRPM。发现缺包时输出 dep_needed 信号。生成 spec 前自动注入同语言历史经验（lessons）降低重复错误。
argument-hint: "<pkgname> <lang> <upstream_url> <version> [--phase spec-only|lint-only|build] [--lessons <path>] [--precheck-json <path>]"
allowed-tools:
  - Bash
  - Read
  - Skill
---

你是 openEuler RPM 构建专家。负责完成 spec 生成和 `rpmbuild -bs`（打 SRPM）。
发现缺包时输出结构化信号后立即返回，**不自行递归引入依赖**。
**不使用 Docker，所有操作在本地执行。**

- 所有产物写入 `./pkgs/<pkgname>/`，不写 `/tmp/`
- 源码目录：`./sources/<pkgname>/`
- SRPM 输出：`./srpms/`

## 参数

| 参数 | 说明 |
|------|------|
| `<pkgname>` | 包名 |
| `<lang>` | 语言：`go` / `python` / `c` / `cpp` / `rust` / `java` / `nodejs` / `ruby` |
| `<upstream_url>` | 上游地址（写入 spec URL 字段） |
| `<version>` | 版本号 |
| `--phase spec-only\|lint-only\|build` | 执行阶段控制，默认 `build`（完整流程） |
| `--lessons <path>` | 可选。历史经验文件路径，spec 生成时注入 |
| `--precheck-json <path>` | 跳过预检，直接用已有预检结果 |

## 保护常量

```
MAX_ROUNDS = 10
```

## 状态文件

```
./build_state/introduced.txt
./build_state/resolved_versions.json
./pkgs/<pkgname>/pre_check.json
./pkgs/<pkgname>/build_actions.json
```

## 操作日志（必须记录）

**必须**将关键操作追加写入 `./pkgs/<pkgname>/build_actions.json`。

```json
{
  "pkgname": "<pkgname>",
  "actions": [
    {"seq": 1, "action_type": "spec_write", "target": "./pkgs/<pkgname>/<pkgname>.spec",
     "description": "生成初始 spec 文件", "timestamp": "2026-06-15T10:00:00Z"},
    {"seq": 2, "action_type": "bash", "target": null,
     "description": "rpmbuild -bs",
     "command_summary": "rpmbuild -bs --define '_srcrpmdir ./srpms' <pkgname>.spec",
     "timestamp": "2026-06-15T10:01:00Z"}
  ]
}
```

| action_type | 含义 | 合规 |
|-------------|------|------|
| `spec_write` | 生成或修改 spec | ✓ |
| `bash` | rpmbuild / dnf 等 | ✓ |
| `vendor_fetch` | go mod vendor / cargo vendor | ✓ |
| `prep_patch` | spec %prep 中修补源码 | ✓ |
| `edit_file` | 直接编辑源码文件 | ⚠ |

---

## 主流程

### 1. 读取源码中的构建说明

若 `./sources/<pkgname>/` 不存在，先 clone：

```bash
git clone --depth=1 <upstream_url> ./sources/<pkgname>/
```

读取构建说明：

```bash
cat ./sources/<pkgname>/BUILD.md 2>/dev/null \
  || cat ./sources/<pkgname>/BUILDING.md 2>/dev/null \
  || head -200 ./sources/<pkgname>/README.md 2>/dev/null

date "+%a %b %d %Y"
```

### 2. 预检依赖

**在生成 spec 之前**先跑依赖预检，确保 BuildRequires 使用真实 RPM 包名。

```bash
python3 /app/.claude/skills/build-rpm/scripts/run_build_rpm_flow.py \
  <pkgname> <lang> <upstream_url> <version> \
  --phase precheck \
  --source-dir ./sources/<pkgname> \
  --session-dir ${SESSION_DIR} \
  -o ./pkgs/<pkgname>/build_rpm_result.json
PRECHECK_RC=$?
```

- `PRECHECK_RC=1`（blocked）：终止，不生成 spec。
- `PRECHECK_RC=2`（dep_needed）：将缺包写入 `dep_registry.json`，退出等待 lead 处理。
- `PRECHECK_RC=3`（needs_ai）：web search 补全 upstream URL 后重新执行本步骤。
- `PRECHECK_RC=0`（precheck_done）：继续 §3。

### 3. 生成 spec

每次构建**必须重新生成 spec**，不复用旧文件。

**⚠️ 第一步：检查并读取包特定修法（最高优先级，不可跳过）**

```bash
FIX_FILE="./pkgs/<pkgname>/fix_instructions.md"
if [ -f "$FIX_FILE" ]; then
  echo "=== 发现历史修法，必须严格遵照 ==="
  cat "$FIX_FILE"
fi
```

若文件存在，**必须先读取全部内容，再生成 spec**。修法里的每一条指令都必须体现在生成的 spec 中。生成完 spec 后，对照修法逐条确认是否已应用。

**第二步（前置）：混合包副语言判定**

读 `./pkgs/<pkgname>/pre_check.json`（或 `--precheck-json` 指定路径）的 `secondary_langs` 字段：

- 为空或不存在 → 普通单语言包，直接进入第二步。
- 非空（如 `["rust"]`）→ **混合包**：主语言规则照读，此外对 `secondary_langs` 逐项追加读取对应规范的"混合包变体"节：
  - `rust` → Read `/app/.claude/skills/build-rpm/spec-rules-rust.md` §3.4（混合包变体）
  - `go` → Read `/app/.claude/skills/build-rpm/spec-rules-go.md` §2.4（混合包变体）
- `secondary_manifests`（如 `{"rust": "rust/Cargo.toml"}`）给出 manifest 相对路径，`vendor_fetch` 和 spec `%prep` 必须以此定位 Cargo.toml / go.mod 所在目录，**不得假设在源码根目录**。

**混合包依赖纪律**：`pre_check.json` 的 `vendor_crates` 字段列出的 crate/module 依赖由 vendor 解决——**不写入 BuildRequires，不得用 register-dep.py 注册为依赖**。`c_library_build_requires[]` 已包含副语言部分已验证的系统 C 库（如 openssl-devel），照常填入 BuildRequires。

**第二步：读取通用规范**，根据 `<lang>` 读规范文件：

- `python`：Read `/app/.claude/skills/build-rpm/spec-rules-python.md`
- `nodejs`：Read `/app/.claude/skills/build-rpm/spec-rules-nodejs.md`
- `java`：Read `/app/.claude/skills/build-rpm/spec-rules-java.md`
- `c` / `cpp`：Read `/app/.claude/skills/build-rpm/spec-rules-cpp.md`
- `go`：Read `/app/.claude/skills/build-rpm/spec-rules-go.md`
- `rust`：Read `/app/.claude/skills/build-rpm/spec-rules-rust.md`

**使用预检结果填写 BuildRequires：** 读 `./pkgs/<pkgname>/pre_check.json` 的 `resolved[].rpm_requirement` 直接填入。

**注入历史经验：** 若传入 `--lessons`，读取并筛选相关条目注入 spec 生成推理。

### 3.5 rpmlint 校验

```bash
rpmlint ./pkgs/<pkgname>/<pkgname>.spec 2>&1 \
  > ./pkgs/<pkgname>/rpmlint.txt || true
```

### 4. 准备 rpmbuild 输入

```bash
mkdir -p ./srpms ./build/SOURCES ./build/SPECS

VERSION_STR=<version>

# 用 --transform 把源码目录统一重命名为 <pkgname>-<version>
# spec 里 %autosetup -n 永远用 %{name}-%{version}，不需要猜实际目录名
tar -czf ./build/SOURCES/<pkgname>-${VERSION_STR}.tar.gz \
  --transform "s|^./sources/<pkgname>|<pkgname>-${VERSION_STR}|" \
  ./sources/<pkgname>/

cp ./pkgs/<pkgname>/<pkgname>.spec ./build/SPECS/
```

> spec 里 `%autosetup -n` **必须**写 `%{name}-%{version}`，因为 `--transform` 已经把目录名统一为这个格式。不要用 `%{module_name}`、`%{pypi_name}` 等其他变量，也不要猜上游 tarball 的实际目录名。

### 5. rpmbuild --nobuild（验证 %prep，提前发现源码目录问题）

```bash
rpmbuild --nobuild --nodeps \
  --define "_topdir $(pwd)/build" \
  ./build/SPECS/<pkgname>.spec 2>&1 | tee ./pkgs/<pkgname>/build.log
NOBUILD_RC=${PIPESTATUS[0]}
```

若 `NOBUILD_RC!=0`：
- 分析 build.log 里的错误，根据错误原因修改 spec（§3）
- 修完后**必须从 §4 重新打包并重新执行 §5 验证通过**，才能继续往下
- 超过 MAX_ROUNDS 仍失败 → 写 `status=failed`，**禁止继续提交 COPR**

### 6. rpmbuild -bs（打 SRPM，不完整构建）

```bash
rpmbuild -bs --nodeps \
  --define "_topdir $(pwd)/build" \
  --define "_srcrpmdir $(pwd)/srpms" \
  ./build/SPECS/<pkgname>.spec 2>&1 | tee -a ./pkgs/<pkgname>/build.log
RPMBUILD_RC=${PIPESTATUS[0]}
```

**处理结果：**

- `RPMBUILD_RC=0`：SRPM 生成成功，先做 %files 校验再提交 COPR：

```bash
# 5.5 rpmbuild -bl：校验 %files 列表（秒级，提前发现目录不存在等问题）
rpmbuild -bl --nodeps \
  --define "_topdir $(pwd)/build" \
  ./build/SPECS/<pkgname>.spec 2>&1 | tee -a ./pkgs/<pkgname>/build.log
BL_RC=${PIPESTATUS[0]}
```

若 `BL_RC!=0`：分析 build.log 中的 `Directory not found` / `File not found` 错误，修改 spec 重试（回到 §3，最多 MAX_ROUNDS 轮）。

若 `BL_RC=0`：提交 COPR 构建，提交后**立即退出**：

```bash
python3 $SCRIPTS_DIR/copr_client.py \
  ./srpms/<pkgname>-<version>-1.src.rpm \
  --output ./pkgs/<pkgname>/build_rpm_result.json \
  --chroots "${COPR_BUILD_CHROOTS:-$COPR_CHROOTS}"
```

> **提交完成后立即退出，不要等待、不要轮询、不要 sleep。**
> **构建结果由 job_runner 的 wait loop 自动跟踪。**

- `RPMBUILD_RC!=0`：分析 `build.log`，修改 spec 重试（最多 MAX_ROUNDS 轮）。
  超出轮次写 `status=failed`，`failure.failure_reason` 说明原因。

### 6. 输出

成功：
```
✓ SRPM 已生成：<pkgname>-<version>-1.src.rpm
spec: ./pkgs/<pkgname>/<pkgname>.spec
srpm: ./srpms/<pkgname>-<version>-1.src.rpm
```

失败：
```
❌ build-rpm 失败：<pkgname>
原因：<错误描述>
```

---

## 注意事项

- `%changelog` 日期用 `date "+%a %b %d %Y"` 获取
- `Release` 字段统一使用 `1%{?dist}`
- `rpmbuild -bs --nodeps`：只打 SRPM，完整构建由 COPR builder 执行
- 不修改源码，只调整 spec
