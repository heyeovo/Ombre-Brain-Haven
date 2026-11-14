# ============================================================
# Ombre Brain Docker Build
# Docker 构建文件
#
# Build: docker build -t ombre-brain .
# Run:   docker run -e OMBRE_API_KEY=your-key -p 8000:8000 ombre-brain
# ============================================================

# --- Stage 1: grab the claude CLI binary ---
FROM node:22-bookworm-slim AS claude-cli
WORKDIR /tmp/claude-pkg
RUN npm init -y && npm install @anthropic-ai/claude-agent-sdk@0.3.220 2>/dev/null; exit 0
RUN test -f node_modules/@anthropic-ai/claude-agent-sdk-linux-x64/claude

# --- GPT subscription helper: pinned DevKit, compiled and tested separately ---
FROM node:22-bookworm-slim AS gpt-helper
WORKDIR /helper
COPY integrations/gpt_subscription/package.json integrations/gpt_subscription/package-lock.json integrations/gpt_subscription/tsconfig.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY integrations/gpt_subscription/vendor ./vendor
COPY integrations/gpt_subscription/test ./test
COPY integrations/gpt_subscription/*.mjs ./
RUN npm test && npm prune --omit=dev --ignore-scripts --no-audit --no-fund

# --- Stage 3: main image ---
FROM python:3.12-slim

WORKDIR /app

# Runtime tools needed by claude CLI for recovery mode
RUN apt-get update \
    && apt-get install --no-install-recommends --yes ca-certificates curl git libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first (leverage Docker cache)
# 先装依赖（利用 Docker 缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Claude CLI binary for recovery console
COPY --from=claude-cli /tmp/claude-pkg/node_modules/@anthropic-ai/claude-agent-sdk-linux-x64/claude /usr/local/bin/claude
COPY --from=gpt-helper /usr/local/bin/node /usr/local/bin/node
COPY --from=gpt-helper /helper /app/integrations/gpt_subscription
RUN chmod +x /usr/local/bin/claude \
    && groupadd --gid 10001 cc \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/cc --shell /bin/bash recovery

# Copy project files / 复制项目文件
COPY *.py .
COPY resources ./resources
COPY scripts ./scripts
COPY dashboard.html .
COPY recovery.html .
COPY dashboard_assets ./dashboard_assets
COPY config.example.yaml ./config.yaml
RUN chmod +x scripts/*.sh

# Persistent mount point: bucket data
# 持久化挂载点：记忆数据
VOLUME ["/app/buckets"]

# Default to streamable-http for container (remote access)
# 容器场景默认用 streamable-http
ENV OMBRE_TRANSPORT=streamable-http
ENV OMBRE_BUCKETS_DIR=/app/buckets
ENV OMBRE_PORT=8080

EXPOSE 8080

CMD ["python", "server.py"]
