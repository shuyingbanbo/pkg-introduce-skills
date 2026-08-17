FROM openeuler/openeuler:24.03

# Claude Code CLI 依赖 Node.js 18+（openEuler 24.03 自带 Node.js 20）
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN dnf -y update && dnf -y install \
      python3 python3-pip \
      nodejs npm \
      git unzip curl gnupg \
      rpm-build rpm-devel \
    && npm install -g @anthropic-ai/claude-code \
    && dnf clean all && rm -rf /var/cache/dnf

WORKDIR /app

# Python 依赖
COPY docker/importer-worker/requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Worker 代码
COPY docker/importer-worker/worker.py docker/importer-worker/job_runner.py docker/importer-worker/copr_client.py ./

# Skills & Agents（替代 hostPath 挂载）
COPY .claude/skills/ .claude/skills/
COPY .claude/agents/ .claude/agents/

CMD ["python3", "worker.py"]
