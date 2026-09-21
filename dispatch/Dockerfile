# 基础镜像可经 build-arg 覆盖（Docker Hub 直连受限的环境加加速前缀）
ARG NODE_IMAGE=node:22-alpine
ARG PYTHON_IMAGE=docker.1ms.run/library/python:3.12-slim

# ── 阶段 1：管理台前端（React + Vite 构建）────────────────────
FROM ${NODE_IMAGE} AS web

WORKDIR /src/web
# npm 源可经 build-arg 覆盖（默认 npmmirror，适配受限网络）
ARG NPM_REGISTRY=https://registry.npmmirror.com
COPY web/package.json web/package-lock.json* ./
RUN npm config set registry ${NPM_REGISTRY} && \
    { [ -f package-lock.json ] && npm ci --no-audit --no-fund || npm install --no-audit --no-fund; }
COPY web/ ./
# 产物输出 /src/app/static/admin（vite outDir=../app/static/admin）
RUN npm run build

# ── 阶段 2：dispatch 后端 ────────────────────────────────────
FROM ${PYTHON_IMAGE}

WORKDIR /app

# PyPI 源可经 build-arg 覆盖（默认清华镜像，适配受限网络）
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# 管理台构建产物（app/static/admin 已在 .dockerignore 排除本地残留，以此为准）
COPY --from=web /src/app/static/admin ./app/static/admin

EXPOSE 8644
# ws-max-size 与上游 dashboard 对齐（desktop 会经 WS 传大文件）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8644", \
     "--ws-max-size", "134217728", "--ws-ping-interval", "30", "--ws-ping-timeout", "60"]
