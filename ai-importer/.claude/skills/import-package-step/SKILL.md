---
name: import-package-step
description: >
  openEuler 包引入单步执行。由 job_runner.py 驱动，每次执行一步：读状态 → 按优先级 spawn 一个 agent → 更新状态文件 → 退出。
  不要手动调用，由 job_runner 通过 claude -p 触发。
argument-hint: "<session_dir>"
allowed-tools:
  - Bash
  - Read
  - Agent
  - Skill
  - ScheduleWakeup
---

## 职责边界

本 skill 是**单步执行器**，每次调用只做一件事然后退出。

| 职责 | 负责方 |
|------|--------|
| 状态机决策（下一步是什么）| `step_supervisor.py`（纯 Python，job_runner 调用） |
| 修复路由 / 轮数计数 / 熔断（fix_round、no_output、resubmit 判定） | `step_supervisor.py`（计数存 `pkgs/<pkg>/fix_state.json`，builder/fixer 子模式由 `SUBMODE` 输出） |
| COPR 构建轮询 / wait loop | `job_runner.py`（Python 进程，不启 Claude） |
| 日志拉取 / build_rpm_result 写入 | `job_runner.py` 的 `_sync_copr_result` |
| 首次构建：spec 生成 + SRPM 提交 | `pkg-builder` agent（提交后立即退出） |
| 失败修复：诊断 + 改 spec + 重新提交 | `pkg-fixer` agent（修复闭环，完成即退出） |

**⚠️ 本 skill 内的 agent 禁止：**
- sleep / 轮询 COPR API / 等待构建完成
- 直接调用 `step_supervisor.py --update-action`（由 skill 的 case 分支统一调用）
- 跨步骤处理多个 action（每次只处理 supervisor 给的一个 action）


## 输入

```bash
SESSION_DIR="<arguments>"
SKILLS_DIR="/app/.claude/skills"
SUPERVISOR="$SKILLS_DIR/import-package-step/scripts/step_supervisor.py"
READ_BUILD_RESULT="$SKILLS_DIR/import-package-step/scripts/read-build-result.py"
SCRIPTS_DIR="$SKILLS_DIR/import-package-step/scripts"
NOTIFY_JOB="$SCRIPTS_DIR/notify_job.py"
```

## 执行步骤

### 1. 读状态，确定下一步 action

```bash
_SUPERVISOR_OUT="$(python3 "$SUPERVISOR" --session-dir "$SESSION_DIR")"
# 进展摘要输出给人看
echo "$_SUPERVISOR_OUT" | grep -vE '^[A-Z_]+='
# 只 eval 赋值行，避免人类可读摘要触发 syntax error
eval "$(echo "$_SUPERVISOR_OUT" | grep -E '^[A-Z_]+=')"
echo "[step] loop=$LOOP action=$ACTION($TARGET)"
```

### 2. 执行 ACTION

```bash
case "$ACTION" in

ros_prep)
  # ROS 链第一步：定位 + 官方判定 + manifest + 伪 gate_result（脚本确定性完成）
  Agent(
    subagent_type="ros-prepper",
    prompt=f"task: prep\npkgname: {TARGET}\nsession_dir: {SESSION_DIR}"
  )
  # 无读回：ros_prep 已写 manifest + 伪 gate_result，supervisor 靠状态文件判定推进
  ;;

ros_fetch)
  # ROS 源码获取：upstream cache → sources/（cache 未命中时直接 clone）
  Agent(
    subagent_type="ros-prepper",
    prompt=f"task: fetch\npkgname: {TARGET}\nsession_dir: {SESSION_DIR}"
  )
  ;;

ros_spec)
  # ROS spec 生成：读 manifest + spec-rules-ros.md + reference/ 基线 + package_fix 修正
  Agent(
    subagent_type="ros-prepper",
    prompt=f"task: spec\npkgname: {TARGET}\nsession_dir: {SESSION_DIR}"
  )
  ;;

evaluate_main)
  # 主包首次 evaluate（与 dep evaluate 相同逻辑，但 mode=top-level）
  Agent(
    subagent_type="pkg-evaluator",
    prompt=f"pkgname: {TARGET}\nmode: top-level\nsession_dir: {SESSION_DIR}"
  )
  eval "$(python3 "$SCRIPTS_DIR/read-gate-result.py" \
    --session-dir "$SESSION_DIR" --pkgname "$TARGET")"
  # GATE_DECISION: reuse_official | reuse_copr_project | introduce_new
  python3 "$SUPERVISOR" --session-dir "$SESSION_DIR" \
    --update-action evaluate_main \
    --gate-decision "$GATE_DECISION"
  ;;

evaluate)
  Agent(
    subagent_type="pkg-evaluator",
    prompt=f"pkgname: {TARGET}\nmode: dependency\nconstraint: {CONSTRAINT}\nsession_dir: {SESSION_DIR}"
  )
  eval "$(python3 "$SCRIPTS_DIR/read-gate-result.py" \
    --session-dir "$SESSION_DIR" --pkgname "$TARGET")"
  # GATE_DECISION: reuse_official | reuse_copr_project | introduce_new
  python3 "$SUPERVISOR" --session-dir "$SESSION_DIR" \
    --update-action evaluate --update-target "$TARGET" \
    --gate-decision "$GATE_DECISION"
  ;;

resolve_upstream)
  Agent(
    subagent_type="resolve-upstream",
    prompt=f"target: {TARGET}\nsession_dir: {SESSION_DIR}"
  )
  ;;

build_dep)
  # COPR 模式：依赖包直接构建到 COPR project，不需要本地安装
  # 子模式由 supervisor 判定并输出 SUBMODE（原 [ -f spec ] 启发式已收回脚本）：
  # resubmit = spec 存在且已提交过 COPR → pkg-fixer（重交/修复模式，prompt 带 fix_context）；
  # builder  = 首次构建 / regenerate 后 / builder 自检失败重试 → pkg-builder
  if [ "$SUBMODE" = "resubmit" ]; then
    FIX_CTX=$(python3 -c "
import json
print('\n'.join(f'{k}: {v}' for k, v in json.load(open('$SESSION_DIR/pkgs/$TARGET/fix_context.json')).items()))
" 2>/dev/null)
    Agent(
      subagent_type="pkg-fixer",
      prompt=f"pkgname: {TARGET}\nmode: resubmit\nsession_dir: {SESSION_DIR}\n${FIX_CTX}"
    )
  else
    Agent(
      subagent_type="pkg-builder",
      prompt=f"pkgname: {TARGET}\nmode: build\nsession_dir: {SESSION_DIR}"
    )
  fi
  eval "$(python3 "$READ_BUILD_RESULT" \
    --session-dir "$SESSION_DIR" --pkgname "$TARGET")"
  python3 "$SUPERVISOR" --session-dir "$SESSION_DIR" \
    --update-action build_dep --update-target "$TARGET" \
    --build-result "$BUILD_STATUS"
  ;;

build_main)
  # 子模式路由同 build_dep（SUBMODE 由 supervisor 输出）
  if [ "$SUBMODE" = "resubmit" ]; then
    FIX_CTX=$(python3 -c "
import json
print('\n'.join(f'{k}: {v}' for k, v in json.load(open('$SESSION_DIR/pkgs/$PKGNAME/fix_context.json')).items()))
" 2>/dev/null)
    Agent(
      subagent_type="pkg-fixer",
      prompt=f"pkgname: {PKGNAME}\nmode: resubmit\nsession_dir: {SESSION_DIR}\n${FIX_CTX}"
    )
  else
    Agent(
      subagent_type="pkg-builder",
      prompt=f"pkgname: {PKGNAME}\nmode: build\nsession_dir: {SESSION_DIR}"
    )
  fi
  eval "$(python3 "$READ_BUILD_RESULT" \
    --session-dir "$SESSION_DIR" --pkgname "$PKGNAME")"
  python3 "$SUPERVISOR" --session-dir "$SESSION_DIR" \
    --update-action build_main --update-target "$PKGNAME" \
    --build-result "$BUILD_STATUS"
  ;;

verify_install)
  # 构建成功后：CI 门禁（依赖闭合 + 可安装性 + 编译期依赖）
  # ⚠️ 本步骤 Bash 必须显式设置 timeout ≥ 900000ms：run_ci_check.py 内部
  # repoclosure/dnf 子命令超时为 600s，agent Bash 默认 300s 会把脚本杀掉，
  # 然后诱发 agent 代写一个 status=timeout 的假 ci_check_result.json（实踩事故）
  echo "[step] 运行 CI 门禁检查..."
  eval "$(python3 $SCRIPTS_DIR/read-session.py --session-dir $SESSION_DIR)"
  _BID=$(python3 -c "import json; print(json.load(open('$SESSION_DIR/pkgs/$TARGET/build_rpm_result.json')).get('copr_build_id',''))" 2>/dev/null)
  _SRPM=""
  if [ -n "$_BID" ]; then
    _SRPM=$(python3 -c "
import json,urllib.request,base64
c=base64.b64encode(f'$COPR_LOGIN:$COPR_TOKEN'.encode()).decode()
r=urllib.request.Request(f'$COPR_FRONTEND_URL/api_3/build/$_BID',headers={'Authorization':f'Basic {c}'})
d=json.loads(urllib.request.urlopen(r,timeout=10).read())
print(d.get('source_package',{}).get('name',''))
" 2>/dev/null)
  fi
  _CI_START=$(date +%s)
  python3 $SKILLS_DIR/pkg-introduce/scripts/run_ci_check.py \
    --pkgs "${_SRPM:-$TARGET}" \
    --session-dir "$SESSION_DIR" \
    --reports-dir "$SESSION_DIR/pkgs/$TARGET"
  _CI_DURATION=$(( $(date +%s) - _CI_START ))
  echo "[step] CI 验证完成 (${_CI_DURATION}s)"

  # ── 时间线：ci_check 细节补充（骨架 action.start/end 由 job_runner 写）─────
  python3 "$SCRIPTS_DIR/timeline.py" --session-dir "$SESSION_DIR" \
    --type ci_check.end --pkg "$TARGET" \
    --data "$(python3 -c "
import json
ci = json.load(open('$SESSION_DIR/pkgs/$TARGET/ci_check_result.json'))
print(json.dumps({'status': ci.get('status','?'), 'errors': ci.get('errors',[])[:5], 'duration_s': $_CI_DURATION}))
" 2>/dev/null || echo '{\"status\":\"?\",\"errors\":[],\"duration_s\":'$_CI_DURATION'}')"
  ;;

feedback)
  Agent(
    subagent_type="pkg-feedback",
    prompt=f"pkgname: {PKGNAME}\nstage: feedback\nsession_dir: {SESSION_DIR}"
  )
  ;;

wait)
  # dep 或主包的 COPR 构建仍在进行中，supervisor 已记录 build_id，稍后轮询
  echo "[step] waiting for COPR build to finish (target=${TARGET}, delay=${DELAY}s)"
  # 不 spawn agent，直接返回；下次 cron 循环 supervisor 会继续轮询
  ;;

analyze_evaluate_main)
  # evaluate_main gate 失败，AI 分析是临时错误（retry）还是硬失败（abort）
  Agent(
    subagent_type="pkg-evaluate-analyzer",
    prompt=f"pkgname: {TARGET}\nmode: top-level\nsession_dir: {SESSION_DIR}"
  )
  ;;

analyze_evaluate)
  # dep evaluate gate 失败，AI 分析是临时错误（retry）还是硬失败（abort）
  Agent(
    subagent_type="pkg-evaluate-analyzer",
    prompt=f"pkgname: {TARGET}\nmode: dependency\nsession_dir: {SESSION_DIR}"
  )
  ;;

fix_failure)
  PRE=$(python3 "$SCRIPTS_DIR/precheck_failure.py" \
    --session-dir "$SESSION_DIR" --pkgname "$PKGNAME")
  if [ "$PRE" = "hint_written" ]; then
    echo "[step] precheck 命中高置信 pattern，已写修复线索 failure_hint（供 pkg-fixer 参考，可推翻）"
  fi
  # 读 supervisor 写入的 fix_context（trigger/round/预算/analysis_file 精确路径），逐行传入 prompt
  FIX_CTX=$(python3 -c "
import json
print('\n'.join(f'{k}: {v}' for k, v in json.load(open('$SESSION_DIR/pkgs/$PKGNAME/fix_context.json')).items()))
" 2>/dev/null)
  # 无论 precheck 是否命中，都由 pkg-fixer 完成修复闭环（precheck 的分析是它的输入之一）
  Agent(
    subagent_type="pkg-fixer",
    prompt=f"pkgname: {PKGNAME}\nmode: fix\nsession_dir: {SESSION_DIR}\n${FIX_CTX}"
  )
  eval "$(python3 "$READ_BUILD_RESULT" --session-dir "$SESSION_DIR" --pkgname "$PKGNAME")"
  python3 "$SUPERVISOR" --session-dir "$SESSION_DIR" \
    --update-action build_main --update-target "$PKGNAME" \
    --build-result "${BUILD_STATUS:-failed}"
  ;;

fix_failure_dep)
  PRE=$(python3 "$SCRIPTS_DIR/precheck_failure.py" \
    --session-dir "$SESSION_DIR" --pkgname "$TARGET")
  if [ "$PRE" = "hint_written" ]; then
    echo "[step] precheck 命中高置信 pattern，已写修复线索 failure_hint（供 pkg-fixer 参考，可推翻）"
  fi
  # 读 supervisor 写入的 fix_context（trigger/round/预算/analysis_file 精确路径），逐行传入 prompt
  FIX_CTX=$(python3 -c "
import json
print('\n'.join(f'{k}: {v}' for k, v in json.load(open('$SESSION_DIR/pkgs/$TARGET/fix_context.json')).items()))
" 2>/dev/null)
  Agent(
    subagent_type="pkg-fixer",
    prompt=f"pkgname: {TARGET}\nmode: fix\nsession_dir: {SESSION_DIR}\n${FIX_CTX}"
  )
  eval "$(python3 "$READ_BUILD_RESULT" \
    --session-dir "$SESSION_DIR" --pkgname "$TARGET")"
  python3 "$SUPERVISOR" --session-dir "$SESSION_DIR" \
    --update-action build_dep --update-target "$TARGET" \
    --build-result "$BUILD_STATUS"
  ;;

summary)
  Agent(
    subagent_type="pkg-feedback",
    prompt=f"pkgname: {PKGNAME}\nstage: summary\nsession_dir: {SESSION_DIR}"
  )
  ;;

done)
  # COPR 模式：从 COPR result URL 归档，无容器清理
  INTRODUCED=$(sort -u "$SESSION_DIR/build_state/introduced.txt" 2>/dev/null | tr '\n' ' ')
  Agent(
    prompt=f"/archive-rpm-sources --pkgs {PKGNAME} {INTRODUCED} --session-dir {SESSION_DIR} --reports-dir {SESSION_DIR}/pkgs/{PKGNAME}"
  )
  python3 "$SUPERVISOR" --session-dir "$SESSION_DIR" \
    --update-action done --update-target "$PKGNAME"
  python3 "$NOTIFY_JOB" --session-dir "$SESSION_DIR" --status success \
    || echo "[引包] redis 通知失败（非 worker 模式）"
  DELAY=""
  ;;

fail)
  # COPR 模式：先生成失败报告，再归档
  # feedback：提炼经验
  Agent(
    subagent_type="pkg-reviewer",
    prompt=f"pkgname: {PKGNAME}\nstage: feedback\nsession_dir: {SESSION_DIR}"
  )
  # summary：生成失败报告
  Agent(
    subagent_type="pkg-reviewer",
    prompt=f"pkgname: {PKGNAME}\nstage: summary\nsession_dir: {SESSION_DIR}"
  )
  python3 "$SUPERVISOR" --session-dir "$SESSION_DIR" \
    --update-action fail --update-target "$TARGET"
  Agent(
    prompt=f"/archive-rpm-sources --pkgs {PKGNAME} --session-dir {SESSION_DIR} --reports-dir {SESSION_DIR}/pkgs/{PKGNAME}"
  )
  python3 "$NOTIFY_JOB" --session-dir "$SESSION_DIR" --status failed \
    || echo "[引包] redis 通知失败（非 worker 模式）"
  echo "[import-package-step] FAILED: $TARGET"
  DELAY=""
  ;;

esac
```

### 3. 输出结果摘要

```bash
# 读最终状态输出摘要（job_runner 负责调度下一轮，无需 ScheduleWakeup）
python3 "$SCRIPTS_DIR/print-summary.py" --session-dir "$SESSION_DIR"
```
