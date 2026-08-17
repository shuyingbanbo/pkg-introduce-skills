---
name: build-rpm
description: RPM 构建核心（COPR 模式）：spec 生成 + rpmbuild -bs 打 SRPM。发现缺包时输出 dep_needed 信号。生成 spec 前自动注入同语言历史经验（lessons）降低重复错误。
argument-hint: "<pkgname> <lang> <upstream_url> <version> [--phase spec-only|lint-only|build] [--lessons <path>] [--precheck-json <path>]"
allowed-tools:
  - Bash
  - Read
  - Edit
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

### 0. 读取 gate 决策，确定构建路径

**在开始任何操作之前**，先读取 gate_result 获取处置策略：

```bash
GATE_RESULT="./reports/gate_result_<pkgname>.json"
GATE_DECISION=""
if [ -f "$GATE_RESULT" ]; then
  GATE_DECISION=$(python3 -c "import json; d=json.load(open('$GATE_RESULT')); print(d.get('result',{}).get('decision',''))" 2>/dev/null)
  echo "[build-rpm] gate decision: $GATE_DECISION"
fi
```

根据 `GATE_DECISION` 分支：

#### `reuse_eur_srpm` — EUR SRPM 重建

仅当 EUR 命中的 chroot 与目标 chroot 精确匹配（OS 版本 + 架构）时才会得到该决策；
chroot 不匹配的 EUR 命中会被级联降级为 `introduce_new_with_ref`（参考源）。

gate 阶段已下载 SRPM 到 `./srpms/` 并提取 spec 到 `./pkgs/<pkgname>/reference/`。

**跳过 §1-§5，直接到 §6 提交 COPR 构建**：

```bash
echo "[build-rpm] EUR SRPM 重建模式 — 跳过 spec 生成，直接提交 COPR"
SRPM_FILE=$(ls ./srpms/<pkgname>*.src.rpm 2>/dev/null | head -1)
if [ -f "$SRPM_FILE" ]; then
  python3 $SCRIPTS_DIR/copr_client.py \
    "$SRPM_FILE" \
    --output ./pkgs/<pkgname>/build_rpm_result.json \
    --chroots "${COPR_BUILD_CHROOTS:-$COPR_CHROOTS}"
  echo "✓ EUR SRPM 已提交 COPR 构建"
  exit 0
else
  echo "[build-rpm] WARN: EUR SRPM 未找到，回退到完整构建流程"
fi
```

若 SRPM 下载失败（网络问题），回退到完整构建流程。

#### `introduce_new_with_ref` — 有参考源的新引入

参考源有两种：gitcode src-openeuler 仓库，或 chroot 不匹配的 EUR 命中（降级）。
gate 阶段已拉取参考 spec/yaml/patches 到 `./pkgs/<pkgname>/reference/`
（EUR 参考源为 SRPM 中提取的 spec）。

**跳过 §2.5**（参考源已在 gate 阶段拉取），§3 会自动检测参考 spec 并进入适配模式。

#### 其他决策（`introduce_new` / 空）

走完整流程（§1 → §2 → §2.5 → §3 → ...）。

---

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
- `PRECHECK_RC=2`（dep_needed）：执行以下命令将缺包写入 `dep_registry.json`，退出等待 lead 处理。

```bash
python3 /app/.claude/skills/import-package-step/scripts/update-dep-registry.py \
  --session-dir ${SESSION_DIR} --pkg <pkgname>
```

- `PRECHECK_RC=3`（needs_ai）：web search 补全 upstream URL 后重新执行本步骤。
- `PRECHECK_RC=0`（precheck_done）：继续 §3。

> **§2.5（检查参考源）已移除。** 参考源的查询和拉取由 gate 阶段的 4 级级联查找统一完成。若 gate 决定 `introduce_new_with_ref`，参考 spec/yaml/patches 已在 `./pkgs/<pkgname>/reference/` 中；若 gate 决定 `introduce_new`，说明 gitcode 也没有参考源，无需再查。

### 3. 生成 spec（仅首次构建）

**⚠️ 前置检查**：若 `./pkgs/<pkgname>/<pkgname>.spec` 已存在，说明是失败修复场景——**停止并退出**，该任务应由 `pkg-fixer` 处理，不要重新生成 spec。

**第一步：检查 openEuler 已有 spec 作为参考**

```bash
REF_SPEC="./pkgs/<pkgname>/reference/<pkgname>.spec"
REF_YAML="./pkgs/<pkgname>/reference/<pkgname>.yaml"

if [ -f "$REF_SPEC" ]; then
  echo "=== 发现 openEuler 已有 spec 参考，以此为基础适配 ==="
  echo "--- 参考 spec ---"
  cat "$REF_SPEC"
  echo "--- 参考 spec 结束 ---"
  if [ -f "$REF_YAML" ]; then
    echo "--- 参考 yaml 元数据 ---"
    cat "$REF_YAML"
    echo "--- 参考 yaml 结束 ---"
  fi
  echo "参考 patches:"
  ls ./pkgs/<pkgname>/reference/*.patch 2>/dev/null || echo "(无)"
fi
```

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
- `ros`：Read `/app/.claude/skills/build-rpm/spec-rules-ros.md`

**使用预检结果填写 BuildRequires：** 读 `./pkgs/<pkgname>/pre_check.json` 的 `resolved[].rpm_requirement` 直接填入。

**C 扩展链接库 BuildRequires：** 若 `pre_check.json` 含 `c_library_build_requires[]`（非空），把其中每个 `-devel` 包名直接加入 `BuildRequires`——这些是预检阶段已在目标 chroot 源中验证存在的 C 扩展链接库（如 `libpq-devel`），无需再自行判断。字段为空或不存在时，按常规处理（缺的库由构建失败诊断循环兜底）。

**注入历史经验：** 若传入 `--lessons`，读取并筛选相关条目注入 spec 生成推理。

**第三步：根据是否有参考 spec 选择生成策略**

##### A. 有参考 spec（`$REF_SPEC` 存在时）

你**必须**以 openEuler 已有 spec 为起点进行适配，而不是从头生成：

1. **保留结构**：保留参考 spec 的整体结构、`%package` 子包定义（devel/help 等）、RPM 宏使用习惯（`%cmake`、`%autosetup`、`%cmake_build` 等）
2. **更新版本**：将 `Version` 更新为当前目标版本 `<version>`，`Release` 重置为 `1%{?dist}`
3. **更新 Source0**：将 `Source0` URL 更新为当前上游 URL（`<upstream_url>`）
4. **评估 patches**：
   - 读取每个参考 patch 的内容，判断是否仍然需要
   - 架构适配类 patch（如 RISC-V 修复、字节序修复）通常保留
   - 已合入上游的 patch 或针对旧版本的补丁应删除
   - 无法判断时保留并在 `%prep` 中应用，让 rpmbuild 验证
5. **更新 BuildRequires**：使用 `./pkgs/<pkgname>/pre_check.json` 中的预检结果替换/补充 BuildRequires，移除参考 spec 中不再需要的依赖
6. **清理 %changelog**：保留最近的条目格式作为参考，更新日期和版本号
7. **检查宏兼容性**：确保使用的 RPM 宏在目标 openEuler 版本中存在

> 参考 spec 来自 openEuler 社区维护者，经过了社区审查。**你的工作是把它适配到新版本，不是重写它。** 只有当参考 spec 与实际情况严重不符（如构建系统完全不同、上游项目重构）时，才回退到从头生成。

##### B. 无参考 spec（`$REF_SPEC` 不存在时）

从头生成 spec，遵循通用规范、预检结果和历史经验（当前行为不变）。

### 3.5 rpmlint 校验

```bash
rpmlint ./pkgs/<pkgname>/<pkgname>.spec 2>&1 \
  > ./pkgs/<pkgname>/rpmlint.txt || true
```

### 3.6 ROS 依赖名门禁（仅 `<lang>=ros`，强制）

spec 中所有 `ros-<distro>-*` 的 BuildRequires/Requires 必须在 `ros-projects.list` 中真实存在（§6 反幻觉铁律的机械校验）：

```bash
python3 /app/.claude/skills/build-rpm/scripts/verify_ros_spec_deps.py \
  ./pkgs/<pkgname>/<pkgname>.spec --session-dir ${SESSION_DIR}
VERIFY_RC=$?
```

- `VERIFY_RC=1`：幻觉依赖名——按输出的最近匹配建议回到 §3 修正 spec，重跑本节直至通过。**禁止跳过本门禁直接提交**。
- `VERIFY_RC=0`：继续 §3.7。

### 3.7 Requires provider 预检（所有语言，强制）

spec 声明的 Requires/BuildRequires 必须在 CI 源集合（官方 everything/update/EPOL + COPR result + 项目 additional_repos）有 provider。无 provider 的 Requires 要等构建成功后的 CI 可安装性检查才暴露——白烧一整轮构建（ros2-numpy/python3-transforms3d 事故）：

```bash
python3 /app/.claude/skills/build-rpm/scripts/verify_spec_requires.py \
  ./pkgs/<pkgname>/<pkgname>.spec --session-dir ${SESSION_DIR} \
  --pkg <pkgname> --register-missing
REQ_RC=$?
```

- `REQ_RC=4`：依赖完整性校验未通过——`pre_check` 分析（package.xml）声明的依赖被静默丢弃，未写进 spec。按输出指引三选一：写回 spec（无 provider 的下次执行会自动注册递归引入）、`pkgs/<pkgname>/waived_deps.txt` 带理由豁免、或修正依赖名。**不得强行提交**（spec-rules-ros §6）。
- `REQ_RC=3`：缺口依赖已自动注册进 dep_registry（待引入）。**禁止本次提交**——结束本轮构建动作，等 supervisor 调度依赖构建完成后再提交主包。
- `REQ_RC=1`：依赖无 provider 且注册失败——按输出指引修正 spec（依赖名写错）或确认该依赖确实无法引入后走 abort，**不得强行提交**。
- `REQ_RC=0`：继续 §4。
- 脚本/环境异常（WARN 降级放行）：继续 §4，CI 仍是最终门禁。

### 4. 准备 rpmbuild 输入

```bash
mkdir -p ./srpms ./build/SOURCES ./build/SPECS

VERSION_STR=<version>

# 若有 git submodule，先初始化
if [ -f "./sources/<pkgname>/.gitmodules" ]; then
  git -C ./sources/<pkgname> submodule update --init --recursive
fi

# 用 --hard-dereference 消除硬链接（GitHub clone 可能含硬链接）
# 用 --transform 把源码目录统一重命名为 <pkgname>-<version>
tar --hard-dereference -czf ./build/SOURCES/<pkgname>-${VERSION_STR}.tar.gz \
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

> **多 chroot 提交语义**：`--chroots` 接受逗号分隔的多个 chroot，同一份 SRPM 会在每个 chroot 上独立重建（`--chroot` 单值参数保留兼容）。提交范围用 `$COPR_BUILD_CHROOTS`——supervisor 注入的**本轮可提交 chroot 子集**（依赖未就绪的 chroot 本轮不提交，后续增量补交）；未设置时回退 `$COPR_CHROOTS`（全部目标 chroot）。
>
> `build_rpm_result.json` 多 chroot 结构：`copr_build_id` / `copr_chroot` 保留（主 chroot，兼容旧消费者）；新增 `copr_chroots`（本轮提交的全部 chroot，list）与 `copr_build_ids`（`{chroot: build_id}` 映射）。下方快照存档读的 `copr_build_id` 无需改动。

```bash
python3 $SCRIPTS_DIR/copr_client.py \
  ./srpms/<pkgname>-<version>-1.src.rpm \
  --output ./pkgs/<pkgname>/build_rpm_result.json \
  --chroots "${COPR_BUILD_CHROOTS:-$COPR_CHROOTS}"

# 【强制】spec 快照存档：记录本次实际提交的 spec（地面真值，供 pkg-fixer 下轮修复对照）
BUILD_ID="$(python3 -c "import json; print(json.load(open('./pkgs/<pkgname>/build_rpm_result.json')).get('copr_build_id',''))" 2>/dev/null)"
if [ -n "$BUILD_ID" ]; then
  mkdir -p ./pkgs/<pkgname>/submitted_specs
  cp ./pkgs/<pkgname>/<pkgname>.spec ./pkgs/<pkgname>/submitted_specs/spec_${BUILD_ID}.spec
fi
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
