# pkg-introduce-skills — AI 驱动的 RPM 包引入系统

为 openEuler EUR 构建系统提供 AI 辅助的自动化软件包引入能力。包含 Claude Code skills、agent 定义、Python 工作引擎和 Docker 部署配置。

## 架构

```
┌─────────────────────────────────────────────────────────────┐
│  copr-frontend (copr_docker)                                │
│  /ai-import/   →  提交表单  →  Redis queue:ai:<project>     │
└────────────────────────────┬────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│  ai-importer-worker (本仓库 docker/importer-worker/)         │
│                                                             │
│  worker.py  ── 长驻进程，轮询 Redis 队列                     │
│    ├─ pick_next_job()   公平调度所有活跃 project             │
│    ├─ run_job()         单任务执行循环                       │
│    │   ├─ step_supervisor.py  状态机，输出 next action       │
│    │   └─ claude -p /import-package-step  按需启 AI          │
│    └─ watchdog 线程     检测取消信号，强杀 claude             │
└────────────────────────────┬────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│  Claude Skills & Agents (.claude/)                          │
│                                                             │
│  6 Skills:                                                  │
│  ┌────────────────────┬───────────────────────────────────┐ │
│  │ import-package     │ 入口：读 session → spawn          │ │
│  │                    │ pkg-evaluator → supervisor loop    │ │
│  ├────────────────────┼───────────────────────────────────┤ │
│  │ import-package-step│ 单步执行器：读状态 → 按优先级     │ │
│  │                    │ spawn agent → 更新状态 → 退出      │ │
│  ├────────────────────┼───────────────────────────────────┤ │
│  │ build-rpm          │ spec 生成 + rpmbuild -bs 打 SRPM   │ │
│  │                    │ 8 语言 spec-rules + lessons 注入   │ │
│  ├────────────────────┼───────────────────────────────────┤ │
│  │ review-rpm         │ critique/feedback/summary 三阶段   │ │
│  │                    │ 事后复盘 + 经验提取 + 最终报告     │ │
│  ├────────────────────┼───────────────────────────────────┤ │
│  │ archive-rpm-sources│ 归档 spec/日志/报告到本地持久化    │ │
│  ├────────────────────┼───────────────────────────────────┤ │
│  │ pkg-introduce      │ check + gate 脚本集                │ │
│  │                    │ 合规检查 + 引入决策                │ │
│  └────────────────────┴───────────────────────────────────┘ │
│                                                             │
│  5 Agents (pkg-introduce/):                                 │
│  ┌──────────────────────┬─────────────────────────────────┐ │
│  │ pkg-evaluator        │ run_check → run_gate            │ │
│  │                      │ 输出：reuse | introduce_new      │ │
│  ├──────────────────────┼─────────────────────────────────┤ │
│  │ pkg-builder          │ 调 /build-rpm，生成 spec+SRPM   │ │
│  │                      │ dep_needed 时写 dep_registry     │ │
│  ├──────────────────────┼─────────────────────────────────┤ │
│  │ pkg-evaluate-analyzer│ 诊断 gate 失败原因              │ │
│  │                      │ 分类：retry | abort              │ │
│  ├──────────────────────┼─────────────────────────────────┤ │
│  │ pkg-failure-analyzer │ 诊断 COPR 构建失败               │ │
│  │                      │ 分类：retry | rebuild | abort    │ │
│  ├──────────────────────┼─────────────────────────────────┤ │
│  │ pkg-feedback         │ 调 /review-rpm                  │ │
│  │                      │ 提取 lessons + 生成 summary      │ │
│  └──────────────────────┴─────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

### 状态机

任务状态由 `step_supervisor.py` 驱动的文件状态机管理：

```
session_dir/
  session.json              COPR 凭据、包元数据
  workflow_<pkg>.json       全局进度
  dep_registry.json         依赖状态机
  build_state/introduced.txt
  pkgs/<pkg>/
    gate_result_<pkg>.json  评估决策 (lang, version, decision)
    <pkg>.spec              RPM spec
    build_rpm_result.json   构建结果
    failure_analysis_*.json 故障分析
    feedback_<pkg>.json     评审反馈
```

依赖状态转换：`pending_evaluate → evaluate_done → build_done | build_failed | copr_running | pending_deps`
crate/module 类依赖（go/rust，由父包 vendor 解决）：注册时带 `--lang` 或 evaluate 后按 crate 身份判定 → `vendor_only` 终态（等价 build_done，无 RPM 产物，不写父包 BuildRequires）
混合包（主语言 + 内嵌 Cargo.toml/go.mod）：pre_check_deps.py 输出 `secondary_langs` / `secondary_manifests` / `vendor_langs` / `vendor_crates`，builder 按 SKILL.md 副语言判定块追加读 rust §3.4 / go §2.4 混合包变体

## 构建

```bash
# Docker 镜像（worker + skills + agents，自包含）
cd ai-importer
docker build -f docker/importer-worker/Dockerfile \
  -t swr.cn-north-4.myhuaweicloud.com/opensourceway/copr/ai-importer-worker:<tag> .

# 仅安装 Python 依赖（本地开发）
pip install -r docker/importer-worker/requirements.txt
```

## 关键文件

| 路径 | 说明 |
|------|------|
| `docker/importer-worker/Dockerfile` | Worker 镜像构建 |
| `docker/importer-worker/worker.py` | Worker 主入口，Redis 轮询 + 任务调度 |
| `docker/importer-worker/job_runner.py` | 单任务执行引擎，Claude 进程管理 |
| `docker/importer-worker/copr_client.py` | COPR API HTTP 封装 |
| `.claude/skills/import-package/SKILL.md` | 顶层 Supervisor skill |
| `.claude/skills/import-package-step/SKILL.md` | 单步执行器 skill |
| `.claude/skills/build-rpm/SKILL.md` | RPM 构建 skill |
| `.claude/skills/import-package-step/scripts/step_supervisor.py` | 状态机引擎 |
| `.claude/agents/pkg-introduce/` | 5 个 agent 定义 |

## 环境变量（Worker）

| 变量 | 必填 | 说明 |
|------|------|------|
| `REDIS_HOST` | 是 | Redis 地址 |
| `REDIS_PORT` | 否 | Redis 端口，默认 6379 |
| `ANTHROPIC_API_KEY` | 是 | Claude API key |
| `ANTHROPIC_BASE_URL` | 否 | API 代理地址 |
| `ANTHROPIC_AUTH_TOKEN` | 否 | API 认证 token |
| `COPR_API_URL` | 否 | COPR 前端地址，默认 `http://copr-frontend:5000` |
| `SKILLS_DIR` | 否 | Skills 目录，默认 `/app/.claude/skills` |
| `SESSIONS_BASE` | 否 | Session 目录，默认 `/tmp/ai-sessions` |
| `HTTP_PROXY` / `HTTPS_PROXY` | 否 | HTTP 代理 |
| `NO_PROXY` | 否 | 代理例外 |

## 提交规范

- 本仓库属 opensourceways 生态，禁止在 commit message 中添加 `Co-Authored-By`、`Generated-By` 等 AI 署名
- 不允许将含真实凭证的 `config.json` 提交到仓库（只提交 `.example` 模板）

## 关联仓库

| 仓库 | 关系 |
|------|------|
| `copr_docker` (ai-importer 分支) | COPR 前端 Docker 镜像，AI import 页面和 API |
| `copr_design` (ai-importer 分支) | 前端模板设计源 |
| `infra-common` | K8s 部署清单（GitOps/ArgoCD） |
