# hermes-agent overlay：为 Hermes Desktop 多用户接入补"环回转发"能力 + 常用办公处理包。
#
# 背景（详见 ../docs/design.md）：desktop 的 token 鉴权模式要求 dashboard 绑定环回
# （非环回绑定会强制启用 OAuth/密码 gate，且 WS 拒绝 ?token=）。因此 dashboard 固定绑
# 127.0.0.1:9119，由本 overlay 增加一个 s6 服务把 0.0.0.0:9120 纯 TCP 转发到环回 9119，
# 供 docker 内网的 hermes-dispatch 反代访问。不改 hermes 任何代码。
#
# 办公处理（基线镜像只有 hermes 自身依赖，无任何办公/PDF/邮件库）：
#   - Office：python-docx / python-pptx / openpyxl / docxtpl / odfpy …（清单见 requirements-office.txt）
#   - PDF：pypdf / PyMuPDF / pdfplumber / reportlab / pikepdf …
#   - 邮件：Himalaya CLI（IMAP/SMTP/JMAP/Gmail 命令行客户端）+ IMAPClient /
#     beautifulsoup4 / lxml / html2text（收发协议另可走标准库）
#   - 系统层：LibreOffice（CLI 做 doc/ppt/xls→pdf 转换）+ poppler + tesseract(中)
#     + Noto CJK 字体（基线仅有文泉驿正黑，matplotlib/LibreOffice 出中文会缺字）
#   hermes 既有关键依赖用 constraints-hermes.txt 锁死，避免 uv 解析时被顶掉版本。
#
# 基线可经 build-arg 覆盖；默认走 1ms.run 加速（Docker Hub 直连受限的环境）。
ARG BASE_IMAGE=docker.1ms.run/nousresearch/hermes-agent:latest
FROM ${BASE_IMAGE}

# Himalaya CLI 邮件客户端（pimalaya/himalaya，静态二进制；IMAP/SMTP/JMAP/Gmail）。
# 二进制随仓库分发（vendor/），构建时不依赖 GitHub——构建容器直连其 CDN 常被重置。
# 升级版本：下载 <tag>/himalaya.x86_64-linux.tgz 覆盖 vendor/ 下同名文件，
# 同步更新 HIMAYAYA_VERSION 注释与下方 sha256（v1.x 资产命名不同，注意核对）。
ARG HIMAYAYA_VERSION=v2.1.0

# 系统源 / PyPI 源加速；出海构建可分别传空串 / 官方地址覆盖
ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
ARG PYPI_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple

COPY --chmod=0755 s6/dashboard-forwarder/run /etc/s6-overlay/s6-rc.d/dashboard-forwarder/run
COPY --chmod=0755 s6/dashboard_forwarder.py /opt/hermes/dashboard_forwarder.py
# s6-overlay v3 约定：服务目录需 type=longrun；user contents.d 里出现即纳入启动集
RUN mkdir -p /etc/s6-overlay/s6-rc.d/dashboard-forwarder && \
    printf 'longrun\n' > /etc/s6-overlay/s6-rc.d/dashboard-forwarder/type && \
    touch /etc/s6-overlay/s6-rc.d/user/contents.d/dashboard-forwarder

# ---- Dashboard SPA：浏览器经反代 subpath 访问白屏修复 ----
# 上游以 vite 默认 base="/" 构建内置 dashboard，懒加载 chunk 的 modulepreload/
# CSS 预加载 URL 由编译期常量拼成站点根绝对路径，不认 X-Forwarded-Prefix；
# 经 /hermes/u/<uid>/ 反代访问时这些请求绕过前缀 → nginx 404 → CSS 预加载
# 失败 reject 整个懒加载 → React 树崩 → 白屏（HTML 入口/CSS url()/API/WS
# 上游本就改写或按 __HERMES_BASE_PATH__ 解析，均正常，故只有懒加载碎裂）。
# Desktop 客户端自带渲染层走 JSON-RPC，不经此 SPA，不受影响。
# 修法：预加载 helper 的 base 拼接改为读取上游 _serve_index 已注入的
# window.__HERMES_BASE_PATH__（反代=/hermes/u/<uid>；直连=undefined 行为不变），
# 被改 chunk 重命名 + 全局更新引用，防止 immutable 缓存拿到旧内容。
COPY fix-dashboard-base.py /tmp/fix-dashboard-base.py
RUN python3 /tmp/fix-dashboard-base.py && rm /tmp/fix-dashboard-base.py

# ---- 系统层：LibreOffice + PDF/OCR 工具 + 中文字体 ----
# --no-install-recommends 去掉 java 等推荐包；writer/calc/impress 三件套覆盖
# docx/xlsx/pptx → pdf 的 headless 转换（soffice --convert-to）。
RUN set -eux; \
    if [ -n "$APT_MIRROR" ]; then \
      for f in /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list; do \
        if [ -f "$f" ]; then \
          sed -i "s@deb.debian.org@$APT_MIRROR@g; s@security.debian.org@$APT_MIRROR@g" "$f"; \
        fi; \
      done; \
    fi; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      libreoffice-writer libreoffice-calc libreoffice-impress \
      poppler-utils qpdf \
      tesseract-ocr tesseract-ocr-chi-sim \
      fonts-noto-cjk \
      zip unzip file \
    ; \
    rm -rf /var/lib/apt/lists/*

# ---- Himalaya CLI 邮件客户端 ----
COPY vendor/himalaya.x86_64-linux.tgz /tmp/himalaya.tgz
RUN set -eux; \
    echo "683a2ab8e1534f01e6bda3a69e204d564c31fbfbe20511fc7bc60b67f2e85884  /tmp/himalaya.tgz" | sha256sum -c -; \
    tar -xzf /tmp/himalaya.tgz -C /tmp himalaya; \
    install -m 0755 /tmp/himalaya /usr/local/bin/himalaya; \
    rm -rf /tmp/himalaya /tmp/himalaya.tgz

# ---- Python 层：办公处理包 ----
# venv 由 uv 管理（无 pip 模块），统一用 uv 安装；
# --no-config 必须带上：基线 WORKDIR=/opt/hermes，uv 会发现该目录 pyproject.toml 里
# [tool.uv] exclude-newer="14 days"（hermes 可复现构建用），会把新包解析冻结到两周前，
# 且 tuna 部分包缺上传时间导致解析倒退（matplotlib 被降到 2015 年的 1.4.3）。
# -c 约束保证 httpx/openai/pydantic/pillow 等既有关键依赖版本不动。
COPY requirements-office.txt constraints-hermes.txt /tmp/
RUN UV_LINK_MODE=copy uv pip install --no-config \
      --python /opt/hermes/.venv/bin/python \
      --index-url "$PYPI_INDEX" \
      -c /tmp/constraints-hermes.txt \
      -r /tmp/requirements-office.txt \
    && rm /tmp/requirements-office.txt /tmp/constraints-hermes.txt

# ---- 默认 python3/pip 对齐到办公 venv ----
# agent 的执行会话是 login shell，会重走 /etc/profile 把 PATH 覆盖成 Debian 默认
#（不含 /opt/hermes/.venv）——`python3` 落到系统 Python：无任何办公包，且 PEP 668
# 禁 pip，agent 会误判"环境不具备"而自建 venv 重装一遍。
# /usr/local/bin 在 profile PATH 里排最前，放 wrapper 兜底所有会话形态。
# 注意必须用 wrapper 而非符号链接：venv 靠 argv0 位置定位 pyvenv.cfg，
# 链接到别处会退化为系统解释器（实测 prefix=/usr、包全部不可见）。
# pip 顺带装进 venv：agent 运行时 `pip install 补包` 可用（uv venv 默认无 pip）。
RUN set -eux; \
    UV_LINK_MODE=copy uv pip install --no-config \
      --python /opt/hermes/.venv/bin/python --index-url "$PYPI_INDEX" pip; \
    printf '#!/bin/sh\nexec /opt/hermes/.venv/bin/python "$@"\n' > /usr/local/bin/python3; \
    cp /usr/local/bin/python3 /usr/local/bin/python; \
    printf '#!/bin/sh\nexec /opt/hermes/.venv/bin/pip "$@"\n' > /usr/local/bin/pip3; \
    cp /usr/local/bin/pip3 /usr/local/bin/pip; \
    chmod 0755 /usr/local/bin/python3 /usr/local/bin/python /usr/local/bin/pip3 /usr/local/bin/pip

# 冒烟：办公包全部可 import，hermes 自身及其关键依赖未被动到；
# 再用无 venv 的"干净 profile PATH"验证默认 python3/pip 落在办公 venv 上
RUN env PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
      python3 -c "import pptx, pandas; print('default python3 -> office venv OK')" && \
    env PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin pip3 --version && \
    /opt/hermes/.venv/bin/python - <<'EOF' && hermes --version >/dev/null && himalaya --version
import docx, pptx, openpyxl, xlsxwriter, xlrd, odf
import fitz, pypdf, pdfplumber, reportlab, pikepdf
import bs4, lxml, html2text, imapclient
import pandas, numpy, matplotlib, tabulate, chardet, pytesseract
import httpx, openai, pydantic, PIL
print("office overlay: python imports OK")
EOF
