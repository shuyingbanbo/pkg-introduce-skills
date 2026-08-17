# pkg-introduce-skills

AI 驱动的 openEuler RPM 软件包自动引入系统 —— Claude Code skills、agent 定义和 Python 工作引擎。

## 概述

本项目是 openEuler EUR（Enterprise User Repo）构建系统的 AI 引包子系统。它通过 Claude Code 自动化完成从上游源码分析、RPM spec 生成、SRPM 构建、COPR 提交、依赖管理到质量审查的完整包引入流程。

## 工作流程

```
用户提交引包请求 (copr-frontend)
        │
        ▼
Redis 队列 queue:ai:<project>
        │
        ▼
worker.py 轮询取任务
        │
        ▼
step_supervisor.py 状态机判下一步
        │
        ├─ evaluate → pkg-evaluator agent
        │     └─ 检查仓库活跃度/许可证/版本 → 决策: reuse | introduce_new
        │
        ├─ build → pkg-builder agent
        │     └─ 生成 spec → rpmbuild -bs → 提交 COPR
        │
        ├─ wait → Python sleep 轮询 COPR 构建状态
        │
        ├─ analyze → pkg-failure-analyzer / pkg-evaluate-analyzer
        │     └─ 诊断失败原因 → retry | rebuild | abort
        │
        ├─ feedback → pkg-feedback agent
        │     └─ 提取 lessons → 生成引入报告
        │
        └─ done / fail → 归档 → 写回 Redis
```

## 快速开始

### 构建镜像

```bash
cd ai-importer
docker build -f docker/importer-worker/Dockerfile \
  -t ai-importer-worker:latest .
```

### 本地运行

```bash
docker run -d \
  -e REDIS_HOST=<redis-host> \
  -e ANTHROPIC_API_KEY=<your-api-key> \
  -e COPR_API_URL=http://copr-frontend:5000 \
  ai-importer-worker:latest
```

### 部署到 Kubernetes

```bash
kubectl apply -k infra-common/common-applications/test-environment/openeuler-cn4-copr/
```

## 目录结构

```
ai-importer/
├── docker/importer-worker/     # Worker 镜像
│   ├── Dockerfile
│   ├── worker.py               # 主入口，Redis 轮询 + 任务调度
│   ├── job_runner.py           # 单任务执行引擎
│   ├── copr_client.py          # COPR API 客户端
│   └── requirements.txt
├── .claude/
│   ├── skills/                 # 6 个 Claude skills
│   │   ├── import-package/     # 顶层 Supervisor
│   │   ├── import-package-step/# 单步执行器
│   │   ├── build-rpm/          # RPM spec 生成 + 构建
│   │   ├── review-rpm/         # 质量审查 + 经验提取
│   │   ├── archive-rpm-sources/# 产物归档
│   │   └── pkg-introduce/      # 合规检查脚本集
│   └── agents/                 # 5 个 Claude agent 定义
│       └── pkg-introduce/
└── .dockerignore
```

## Skills 说明

| Skill | 触发方式 | 功能 |
|-------|---------|------|
| `import-package` | job_runner 调用 | 读取 session，spawn pkg-evaluator，启动 supervisor loop |
| `import-package-step` | `claude -p` 触发 | 单步执行：读状态 → spawn agent → 写回状态 → 退出 |
| `build-rpm` | pkg-builder 调用 | 生成 RPM spec + rpmbuild -bs 打 SRPM，支持 8 种语言 |
| `review-rpm` | pkg-feedback 调用 | critique/feedback/summary 三阶段事后复盘 |
| `archive-rpm-sources` | done/fail 分支 | 归档 spec/日志/报告到 `/var/lib/ai-importer/archives/` |
| `pkg-introduce` | pkg-evaluator 调用 | check + gate 合规检查脚本 |

## 支持的语言

Go、Python、C、C++、Rust、Java、Node.js、Ruby

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `REDIS_HOST` | Redis 地址 | `redis` |
| `REDIS_PORT` | Redis 端口 | `6379` |
| `ANTHROPIC_API_KEY` | Claude API key | - |
| `ANTHROPIC_BASE_URL` | API 代理地址 | - |
| `COPR_API_URL` | COPR 前端地址 | `http://copr-frontend:5000` |
| `SKILLS_DIR` | Skills 目录 | `/app/.claude/skills` |
| `SESSIONS_BASE` | Session 目录 | `/tmp/ai-sessions` |
