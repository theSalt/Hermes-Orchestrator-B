# hermes-dispatch：Hermes Desktop 多用户接入方案（B 方案）

> 日期：2026-09-20
> 替代 A 方案（Open WebUI + hermes-orchestrator + hermes-agents）的前端形态：
> 用户在桌面安装 **官方 Hermes Desktop**（macOS/Windows/Linux，hermes-agent 仓库 `apps/desktop`），
> 通过服务器上的 **统一调度服务（hermes-dispatch）** 连接各自的 **Hermes Agent** 容器。
> 多用户；仍为每用户一个 Docker 容器（复用 A 方案已验证的生命周期机制）。
> 测试服务器：192.168.0.117。

---

## 1. 总体架构

```
Hermes Desktop（用户桌面机，官方应用）
   │  Settings → Gateway → Remote gateway
   │  Remote URL = http://192.168.0.117:8644/u/<user_id>
   │  Token      = 调度服务签发的 per-user dispatch token
   │  REST: X-Hermes-Session-Token 头 / WS: ?token= 查询参数
   ▼
hermes-dispatch（统一调度服务，FastAPI :8644，服务器 117）
   │  · 用户注册表（SQLite）：user_id ↔ 容器 ↔ token 派生
   │  · 鉴权：所有 /u/* 请求校验 dispatch token（除两个公开探活路径）
   │  · 按需冷启动容器 + 健康等待 + 连接失败自动重启（复用 A 方案 manager）
   │  · 反向代理：HTTP 流式 + WebSocket 双向泵，路径前缀 /u/<user_id> 剥离
   │  · 空闲回收清扫循环（复用 A 方案，有活跃 WS 的用户不回收）
   ▼  docker 网络 hermes-b-net（内网，唯一入口是 dispatch）
hermes-<slug8>（每用户一个容器）
   ├─ gateway run → API Server :8642（脚本/程序化访问，per-user key）
   └─ hermes dashboard :9119 ★绑定 127.0.0.1★（token 鉴权模式，gate 关闭）
        └─ 镜像内置 forwarder s6 服务：0.0.0.0:9120 → 127.0.0.1:9119
            （纯 docker 网内可达；desktop 流量必经 dispatch 的用户级鉴权）
   卷：/opt/data = /data/DockerVolume/hermes-b/hermes-<slug8>/（config/记忆/会话/workspace）
```

## 2. 协议调研结论（决定方案形态，均已在 hermes-agent 源码中核实）

Hermes Desktop 的"Remote gateway"模式连的是 agent 侧的 **dashboard 网关服务**
（`hermes dashboard` / `hermes serve`，默认端口 9119），有两种远端鉴权模式
（`apps/desktop/electron/connection-config.ts`）：

| 模式 | REST | WebSocket | 服务端前提 |
|---|---|---|---|
| **token**（自建部署适用） | `X-Hermes-Session-Token: <token>` 头 | `ws://…/api/ws?token=<token>` | 绑定环回地址（gate 关闭），token 取 `HERMES_DASHBOARD_SESSION_TOKEN` |
| oauth（Nous 云托管） | HttpOnly 会话 cookie | `?ticket=`（单次票据） | 非环回绑定，需 OAuth/密码 provider |

**关键约束（2026-06 安全加固，`web_server.py::should_require_auth`）**：
dashboard 绑定非环回地址（如 0.0.0.0）时 auth gate **强制启用**，且 gate 模式下
WS **无条件拒绝** `?token=`（`_ws_auth_reason`：`token_mismatch`→gate 拒绝 token 路径）。
`HERMES_DASHBOARD_INSECURE` 已失效，不能再用作旁路。

**因此本方案**：让每用户的 dashboard 绑定 **容器内 127.0.0.1**（gate 关闭、token 模式
原生可用，desktop 全链路走它最简单、最不易碎的协议面），再由镜像内一个十几行的
s6 服务把 `0.0.0.0:9120` 纯 TCP 转发到 `127.0.0.1:9119`。9120 只在 docker 内网可见，
dispatcher 是唯一入口并在代理前完成用户级鉴权——安全边界不弱于 gate，且多了
"用户 ↔ token"这一层。

其余核实过的协议事实（实施依据）：

- URL **支持路径前缀**：desktop 生成 WS URL 时保留 baseUrl 的 path（`buildGatewayWsUrl`）；
  dashboard 有 `X-Forwarded-Prefix` 机制重建 SPA 资源 URL/cookie path
  （`dashboard_auth/prefix.py`）→ dispatcher 用 `/u/<user_id>` 前缀路由是官方支持的拓扑。
- **WS 升级有三重环回校验**（loopback 绑定下）：Host 头必须环回名（`_ws_host_origin_reason`）、
  对端 IP 必须环回（`_ws_client_reason`）、`?token=` 必须匹配。转发器同时解决前两者：
  上游连接由转发器进程发起（对端=环回），并在 TCP 侧改写 Host 头
  （websockets 客户端的 `Headers` 是追加语义，无法在客户端替换 Host）。
- 启动握手：desktop 轮询 **`/api/health`**（匿名探针，超时/401 会一直重试；
  旧版本后端回退 `/api/status`）→ dispatcher 放行这两个**上游本就公开**的探活路径
  （`public_paths.py`，无敏感信息），其余一律要 token。
- 首页 HTML 在环回模式会内嵌 `window.__HERMES_SESSION_TOKEN__` → dispatcher 对
  `/`（SPA HTML）同样要求 token，防止匿名拉取 HTML 收割 token。
  desktop 的 token 发现探针（无头 GET /）失败时会回退用户手填的 token（`dashboard-token.ts`），无影响。
- WS 大帧：desktop 会经 WS 传大文件，上游把 uvicorn ws-max-size 调到 128MiB；
  dispatcher 同样调大（uvicorn `--ws-max-size`）并在 websockets 客户端 `max_size=None`。
- 每用户 `HERMES_HOME` 卷隔离 config/记忆/会话/workspace；desktop 的会话模型
  （JSON-RPC over WS）作用在各自容器内，天然按用户隔离。

## 3. 组件与职责

### 3.1 hermes-dispatch（新建，`dispatch/`）

FastAPI 单进程（多用户代理场景 io-bound，单 worker + asyncio 足够）：

| 模块 | 职责 |
|---|---|
| `config.py` | env 配置（端口 8644、admin key、镜像、限额、空闲超时、数据目录…） |
| `security.py` | 确定性派生：`slug = sha256(user_id)[:8]`、容器名 `hermes-<slug>`、per-user `dispatch_token = HMAC(secret, "dispatch:<uid>")`、agent API key（沿用 A 方案） |
| `registry.py` | SQLite（数据卷上，WAL）：`users(user_id, display_name, created_at, last_active)`；user_id 强校验 `[a-z0-9][a-z0-9._-]{0,63}` |
| `driver.py` | docker-py 薄封装（改造自 A 方案）：创建/启停/删除容器；环境注入 dashboard 三件套 + forwarder 端口；**就绪探针 = dashboard `:9120/api/health`** |
| `manager.py` | `ensure_ready`（per-user 锁、冷启动、失败重启一轮）、活跃度 `touch`、清扫循环（有活跃 WS 的用户跳过回收） |
| `proxy.py` | HTTP 流式代理（httpx，剥离 hop-by-hop 头，注入 `Host: 127.0.0.1:9119`、`X-Forwarded-Prefix: /u/<uid>`）+ WS 代理（websockets 双向泵，text/binary/close 全透传） |
| `routes.py` | `GET /health`；`ANY /u/{uid}/{path}`（HTTP+WS）；管理 API；`/v1/chat/completions` 直通代理（程序化调用复用 A 方案协议） |
| `main.py` | lifespan 清扫循环 |

**鉴权规则**（`/u/*`）：

- `/api/health`、`/api/status` → 匿名放行（上游公开探针，desktop 启动握手需要）
- 其余全部路径（含 `/`、静态资源、WS）→ 要求 dispatch token，接受
  `X-Hermes-Session-Token` 头 / `Authorization: Bearer` / `?token=` 三种形态
- 匿名请求不 touch 活跃度（防止登出的 desktop 轮询把容器钉住）

**管理 API**（`Authorization: Bearer <HERMES_ADMIN_KEY>`）：

| 方法/路径 | 说明 |
|---|---|
| `GET /api/users` | 用户清单 + 容器状态/空闲时长 |
| `POST /api/users` | 建用户（惰性，不拉容器），返回 `{user_id, gateway_url, token}` 供粘贴进 desktop |
| `DELETE /api/users/{uid}` | 删注册 + 删容器（数据保留）；`?purge=1` 连数据一起清 |
| `POST /api/users/{uid}/restart` / `stop` | 运维操作（沿用 A 方案语义） |
| `POST /api/agents/refresh` | 镜像/配置变更后删容器（数据保留），下次访问按新配置重建 |

### 3.2 agent overlay 镜像（`agent-overlay/`）

```dockerfile
FROM docker.1ms.run/nousresearch/hermes-agent:latest     # build-arg 可覆盖
COPY s6/dashboard-forwarder/run /etc/s6-overlay/s6-rc.d/dashboard-forwarder/run
RUN touch /etc/s6-overlay/s6-rc.d/user/contents.d/dashboard-forwarder
```

forwarder = ~30 行 stdlib asyncio TCP 转发（9120→9119，带并发上限），
不装任何包、不改 hermes 一行代码；s6 负责其存活。升级 hermes 只需换 FROM 基线重建。

### 3.3 部署形态（117）

- 目录 `/data/workspace/hermes-orchestrator-b`；数据卷 `/data/DockerVolume/hermes-b`
- 端口 **8644**（A 方案 8643 继续并存）；网络 `hermes-b-net`（与 A 的 hermes-net/owu_default 隔离）
- compose 两服务：`dispatch`（build dispatch/）+ 无 agent 服务（agent 容器由 dispatch 动态创建）

## 4. 安全要点

- dispatch token = HMAC(secret, per-user) 派生：库里不存 token，泄露面最小；secret 泄露=全量轮换
- agent 容器对 docker 内网暴露两个面：API Server 8642（自带 key）+ forwarder 9120
  （token 鉴权由上游 dashboard 兜底，前置还有 dispatch 的用户级校验）
- `docker.sock` 挂载＝宿主 root 级能力，仅限内网（与 A 方案同，P2 K8s 解决）
- 空闲回收默认 60min；有活跃 WS 连接的用户不回收（desktop 长连接场景）

## 5. 实施与验证清单

1. [x] 协议调研（desktop 远端模式 / dashboard 鉴权 / 前缀代理 / 探活路径）
2. [x] overlay 镜像 + dispatch 服务 + 测试（41 项单测，含转发器 Host 改写）
3. [x] 部署 117：compose up、预拉基线镜像、构建 overlay
4. [x] 冒烟（2026-09-20 实测通过）：
   - 匿名 `/api/health`、`/api/status` 200（desktop 启动握手）
   - 无 token 401 / 错 token 401 / 未知用户 404 / alice token 打 bob 401（隔离）
   - 冷启动 + 首启配置种子 + 停止后请求自动拉起
   - WS 探针：握手 → 收到 `gateway.ready` JSON-RPC 事件 → 错误帧往返正常
   - `/u/<uid>/v1/chat/completions`：alice/bob 各自容器独立应答（GLM 真实调用）
5. [x] 真机验证：macOS Hermes Desktop（0.17.0）添加 Remote URL + token → 连接成功。
   实测唯一的拦路虎是 **macOS 本地网络隐私权限**（按应用管控，未授权时探测请求被系统丢弃，
   服务器零请求；修复见 README「故障排查①」），协议链路一次通过

## 6. 与 A 方案的取舍记录

| 维度 | A（Open WebUI） | B（本方案） |
|---|---|---|
| 前端 | OWU 网页（Pipe 虚拟模型） | 官方 Desktop 应用（本地渲染，JSON-RPC/WS） |
| 用户身份 | OWU 账号 + X-OWU-User-Id 头 | dispatch 签发 token，desktop 直填 |
| agent 协议面 | OpenAI 兼容 `/v1/chat/completions` | dashboard 全功能面（会话/文件/技能/审批） |
| 保留能力 | 附件管道、/h-* 命令、终端文件浏览（OWU 专属） | 全部移除；desktop 原生提供对应能力 |
| 生命周期/限额/回收 | ✔ | ✔（原样复用） |
