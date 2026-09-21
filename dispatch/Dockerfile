# 基础镜像可经 build-arg 覆盖；默认走 1ms.run 加速（Docker Hub 直连受限的环境）
ARG PYTHON_IMAGE=docker.1ms.run/library/python:3.12-slim
FROM ${PYTHON_IMAGE}

WORKDIR /app

# PyPI 源可经 build-arg 覆盖（默认清华镜像，适配受限网络）
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8644
# ws-max-size 与上游 dashboard 对齐（desktop 会经 WS 传大文件）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8644", \
     "--ws-max-size", "134217728", "--ws-ping-interval", "30", "--ws-ping-timeout", "60"]
