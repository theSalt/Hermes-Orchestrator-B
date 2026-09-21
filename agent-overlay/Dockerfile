# hermes-agent overlay：为 Hermes Desktop 多用户接入补一块"环回转发"能力。
#
# 背景（详见 ../docs/design.md）：desktop 的 token 鉴权模式要求 dashboard 绑定环回
# （非环回绑定会强制启用 OAuth/密码 gate，且 WS 拒绝 ?token=）。因此 dashboard 固定绑
# 127.0.0.1:9119，由本 overlay 增加一个 s6 服务把 0.0.0.0:9120 纯 TCP 转发到环回 9119，
# 供 docker 内网的 hermes-dispatch 反代访问。不装包、不改 hermes 任何代码。
#
# 基线可经 build-arg 覆盖；默认走 1ms.run 加速（Docker Hub 直连受限的环境）。
ARG BASE_IMAGE=docker.1ms.run/nousresearch/hermes-agent:latest
FROM ${BASE_IMAGE}

COPY --chmod=0755 s6/dashboard-forwarder/run /etc/s6-overlay/s6-rc.d/dashboard-forwarder/run
COPY --chmod=0755 s6/dashboard_forwarder.py /opt/hermes/dashboard_forwarder.py
# s6-overlay v3 约定：服务目录需 type=longrun；user contents.d 里出现即纳入启动集
RUN mkdir -p /etc/s6-overlay/s6-rc.d/dashboard-forwarder && \
    printf 'longrun\n' > /etc/s6-overlay/s6-rc.d/dashboard-forwarder/type && \
    touch /etc/s6-overlay/s6-rc.d/user/contents.d/dashboard-forwarder
