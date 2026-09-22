# hermes-dispatch（B 方案：Hermes Desktop 多用户接入）

用户在桌面安装官方 **Hermes Desktop**（[hermes-agent](https://github.com/NousResearch/hermes-agent) 仓库 `apps/desktop`，macOS/Windows/Linux），通过服务器上的 **统一调度服务** 连接各自的 **Hermes Agent**。多用户；每用户服务器上一个独立 Docker 容器。与 A 方案（Open WebUI + hermes-orchestrator，:8643）并存，测试服务器 192.168.0.117。

```
Hermes Desktop（用户桌面机）
   │  Settings → Gateway → Remote gateway
   │  Remote URL = http://192.168.0.117:8644/u/<user_id>
   │  Token      = dispatch 签发（管理后台/API 建用户时返回）
   │  REST: X-Hermes-Session-Token 头 / WS: ?token=
   ▼
hermes-dispatch（:8644，FastAPI）
   │  鉴权 → 按需冷启动 → HTTP/WS 反向代理 → 空闲回收
   │  管理后台 /admin（React SPA；登录会话 cookie 或 admin key Bearer）
   ▼  docker 网络 hermes-b-net
hb-<slug8>（每用户一个，overlay 镜像）
   ├─ gateway run → API Server :8642（程序化访问）
   └─ hermes dashboard :9119（绑 127.0.0.1，token 模式）
        └─ 镜像内 forwarder s6 服务: 0.0.0.0:9120 → 127.0.0.1:9119
   卷：/opt/data = /data/DockerVolume/hermes-b/hb-<slug8>/

用户注册表：内置 postgres（compose 服务，pgdata 卷）
   └─ users 表含 token_version：支持单用户 token 轮换（见「管理后台」）
```

设计依据与协议调研（desktop 远端模式 / dashboard 鉴权 / 为何绑环回 + forwarder）：[docs/design.md](docs/design.md)。

## 部署（192.168.0.117）

前置：Docker + Compose；Docker Hub 直连不通，走 `docker.1ms.run` 前缀。

```bash
# 1. 预拉 agent 基线镜像（约 1-2 GB；A 方案部署过的机器上已存在可跳过）
docker pull docker.1ms.run/nousresearch/hermes-agent:latest

# 2. 上传项目（本机执行；注意在仓库根目录执行）
rsync -a --exclude .env --exclude .git --exclude 'dispatch/.venv' --exclude 'dispatch/.pytest_cache' \
  --exclude '**/__pycache__' --exclude 'dispatch/web/node_modules' --exclude 'dispatch/app/static/admin' \
  ./ ubuntu@192.168.0.117:/data/workspace/hermes-orchestrator-b/

# 3. 配置并启动
ssh ubuntu@192.168.0.117
cd /data/workspace/hermes-orchestrator-b
cp .env.example .env
vi .env        # 必改：HERMES_ADMIN_KEY / HERMES_SECRET_KEY / POSTGRES_PASSWORD
               # 镜像加速：POSTGRES_IMAGE / NODE_IMAGE 加 docker.1ms.run/library/ 前缀
docker compose up -d --build
docker compose ps        # postgres 应为 healthy
docker compose logs -f dispatch
```

首次 `up` 会自动构建 overlay 镜像 `hermes-agent:desktop`（基于 1ms.run 基线 + dashboard-forwarder + 办公处理包）。基线镜像更新后：`docker compose build agent-image && curl -XPOST .../api/agents/refresh`。

**overlay 内置办公处理能力**（`agent-overlay/`，agent 的 Python 沙箱可直接用）：
- Python 包（`requirements-office.txt`）：Word（python-docx / docxtpl）、PPT（python-pptx）、Excel（openpyxl / XlsxWriter / xlrd）、ODF（odfpy）、PDF（pypdf / PyMuPDF / pdfplumber / reportlab / pikepdf / pdf2image）、邮件（IMAPClient + 标准库 smtplib/imaplib/email，bs4 / lxml / html2text 处理 HTML 正文）、数据图表（pandas / numpy / matplotlib）、OCR（pytesseract）。
- 系统工具：LibreOffice（`soffice --headless --convert-to pdf` 做 doc/ppt/xls→pdf）、poppler、qpdf、tesseract（含中文）、Noto CJK 中文字体、zip/unzip。
- hermes 既有关键依赖（httpx / openai / pydantic / pillow 等）由 `constraints-hermes.txt` 锁版本；**基线镜像升级后需对照基线 `uv pip list` 同步更新该文件**，否则约束里是旧版本号会导致构建失败或依赖被顶。

管理台前端（React + Vite）由 dispatch 镜像的多阶段构建自动完成（node 阶段 `npm run build` → 产物进 `/app/static/admin`）；本地开发用 `cd dispatch/web && npm install && npm run dev`（vite :5173 代理到本机 :8644）。改动前端后需重新 `docker compose build dispatch`。

**用户注册表存储于内置 PostgreSQL**（compose 服务 `postgres`，数据在 named volume `pgdata`；连接串由 `POSTGRES_*` 组装成 `HERMES_DATABASE_URL`，也可整体覆盖。密码建议纯字母数字，避免 URL 转义问题）。2026-09 换库前的旧 SQLite 文件（数据卷上 `registry.db`）不再读取，可手动清理；历史用户需在管理台重建（容器名确定性派生，同 user_id 重建后会复用原数据目录）。

## 经 nginx subpath 访问（117 现行：devdemo :8888 ssl，公网端口映射）

统一入口 `https://devdemo.devpod.cn:8888/hermes/...`（域名解析公网隧道 IP 47.92.92.6，
端口映射回 117 的 nginx；办公室内外同一 URL）。dispatch 侧 `.env`：

```
HERMES_PUBLIC_PATH=/hermes                            # 外部前缀
HERMES_PUBLIC_URL=https://devdemo.devpod.cn:8888      # 管理 API 回显的 gateway_url 基座
```

nginx 侧是 devdemo 的完整替换文件 `deploy/nginx-devdemo-hermes.conf`（内含 WS 升级/长超时/
流式/上传体积四项关键配置）。**公网暴露面已收口**：`/hermes/v1/*`（无鉴权冒烟口）与
`/hermes/api/*`（内网管理 API）在 nginx 直接 403；放行 `/hermes/u/<uid>/`（token 鉴权）、
`/hermes/health` 与 `/hermes/admin/*`（管理台，require_admin 保护，见下节）。

```bash
sudo cp /data/workspace/hermes-orchestrator-b/deploy/nginx-devdemo-hermes.conf \
        /etc/nginx/sites-enabled/devdemo
sudo nginx -t && sudo systemctl reload nginx
```

原理：nginx 在 `location /hermes/` 处剥掉前缀转发（dispatch 路由不变）；dispatch 依
`HERMES_PUBLIC_PATH` 修正三处对外语义——转发上游的 `X-Forwarded-Prefix`（dashboard 用它
重建 SPA 资源 URL，必须带外部前缀）、会话 cookie 的 `Path`、管理 API 的 `gateway_url`。
nginx 不改写响应内容。Desktop Remote URL / 浏览器链接统一用
`https://devdemo.devpod.cn:8888/hermes/u/<user_id>`（建用户时管理 API 直接返回）；
直连 `:8644` 在内网依旧可用。

**自签证书（CN=devdemo.devpod.cn，无 IP SAN）的客户端信任**（URL 均须域名形态，Node
严格校验主机名，IP 形态即使证书受信也报 mismatch）：
- 浏览器：首次访问点继续/信任即可；要消警告就把证书导入系统信任库。
- Hermes Desktop 分两条腿，信任来源不同（源码 `windows-system-ca.ts`：加载系统证书库
  仅 Windows 生效）：
  - **Windows**：一步到位——证书导入「受信任的根证书颁发机构」，主进程（自动读系统证书库）
    与渲染进程（Chromium）同时覆盖：
    ```powershell
    certutil -addstore -f ROOT devdemo.devpod.cn.crt   # 管理员；或双击 .crt 图形导入
    ```
  - **macOS**：两条腿分别配——
    - 主进程（Node，不读钥匙串）：`NODE_EXTRA_CA_CERTS` 启动
      ```bash
      openssl s_client -connect devdemo.devpod.cn:8888 -servername devdemo.devpod.cn </dev/null 2>/dev/null \
        | openssl x509 -outform PEM > ~/.trustssl/devdemo.devpod.cn.pem
      # 当前登录会话一次，之后 Dock 点开也带（注销/重启后需重设）：
      launchctl setenv NODE_EXTRA_CA_CERTS "$HOME/.trustssl/devdemo.devpod.cn.pem"
      # 或单次启动：open -a Hermes --env NODE_EXTRA_CA_CERTS="$HOME/.trustssl/devdemo.devpod.cn.pem"
      ```
    - 渲染进程（Chromium，不认环境变量）：导入钥匙串并信任
      ```bash
      sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain \
        ~/.trustssl/devdemo.devpod.cn.pem
      ```
  - 配完 **⌘Q 完全退出重启** Desktop 生效。

## 管理后台（/admin）

浏览器打开（两个入口等价，同一套 cookie 会话）：

- 内网直连：`http://192.168.0.117:8644/admin`
- 公网域名：`https://devdemo.devpod.cn:8888/hermes/admin`

登录即输入 `.env` 里的 `HERMES_ADMIN_KEY`；dispatch 种下 HMAC 签名的 HttpOnly 会话 cookie
（`SameSite=Strict`，TTL `HERMES_ADMIN_SESSION_TTL_SECONDS` 默认 24h，https 入口自动加
`Secure`；换 ADMIN_KEY 或 SECRET_KEY 即全体会话失效）。脚本/curl 仍可用
`Authorization: Bearer <ADMIN_KEY>` 走同一批管理 API（内网 `/api/*`；管理台 UI 走
`/admin/api/*`，与页面同前缀，经 nginx subpath 也自动正确）。

功能：

- **概览**：用户数、容器运行数、活跃 WS、空闲回收策略
- **用户**：创建（惰性，首次连接才冷启动）/ 删除（可选 purge 连数据目录一起清）/ 复制 gateway_url
- **容器**：启动 / 停止 / 重启 / 查看最近日志（tail≤1000，可选 30s 自动刷新）
- **token 只展示一次**：仅在创建成功和轮换确认时弹出面板（可复制），清单不回显、
  `GET /api/users` 不返回——admin key 泄露不等于存量 token 泄露；**遗忘 token 只能轮换**
- **token 轮换（单用户）**：`token_version+1` 重派生该用户 token——旧 token 立即失效，
  容器自动删除重建（**数据保留**），用户需在 desktop 更新 token 后重连。
  全局换 `HERMES_SECRET_KEY` 则所有人 token 同时变化
  （实现注记：token 无状态派生、库里不存任何 token 材料，数据库泄露不泄露 token；
  不回显是收紧管理面，容器重建所需 token 由派生随时可得）

admin key 走公网时务必用长随机串；如需进一步加固可在 nginx 对
`location = /hermes/admin/api/login` 加 `limit_req` 防爆破。

## 建用户 & 桌面接入

```bash
BASE=http://192.168.0.117:8644
curl -s -XPOST $BASE/api/users \
  -H "Authorization: Bearer $HERMES_ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"alice","display_name":"Alice"}'
# → {"user_id":"alice","gateway_url":".../u/alice","token":"...","slug":"hb-2bd806c9","token_version":0,...}
```

用户在 **Hermes Desktop** 上：

1. 打开 **Settings → Gateway → Remote gateway**（远端网关）
2. Remote URL 填 `gateway_url`（带 `/u/<user_id>` 前缀，官方支持反代路径前缀）
3. Token 填 `token`
4. 连接后正常聊天/会话/文件操作；模型/技能等配置都在该用户自己的容器卷里

### 浏览器访问（免装 Desktop 的 Web 控制台）

每个用户的 agent 自带完整 Web 控制台（会话/文件/技能/设置），经代理直接用浏览器打开：

```
http://192.168.0.117:8644/u/<user_id>/?token=<token>
```

首次带 `?token=` 打开后，dispatch 种下 HttpOnly 会话 cookie（Path 限定 `/u/<uid>`，
经 nginx subpath 时为 `/hermes/u/<uid>`，30 天），
之后该路径下的静态资源/API/WS 全部凭 cookie 放行，把整条链接发给用户即可。
注意：内网 HTTP 明文，cookie 与 token 同源同暴露面；跨站侧 SameSite=Lax + 上游 CORS 白名单兜底。
（`8644/` 根路径本身仍是纯 API：调度服务的用户界面就是官方 Desktop / 各用户的 Web 控制台。）

## 故障排查（Desktop 连不上）

### ① macOS「本地网络」权限（实测踩坑：报 could not reach，服务器零请求）

macOS 15+ 按应用管控对局域网设备的访问。症状：浏览器/curl 都能访问 `http://192.168.0.117:8644`，
但 Desktop 输入 Remote URL 立即报 **"Could not reach this gateway yet"**，且 dispatch 日志里
**完全没有对应请求**（请求被系统丢弃，根本没出 Mac）。

修复：

```bash
# 方式 A：系统设置 → 隐私与安全性 → 本地网络 → Hermes → 打开开关
# 方式 B：开关已有/找不到时重置授权，重启 Desktop（⌘Q 完全退出）后重新弹框允许
tccutil reset LocalNetwork com.nousresearch.hermes
```

判别方法：重试输入 URL 的同时看 dispatch 日志——`docker logs -f hermes-dispatch | grep <user_id>`，
看到 `GET /u/<uid>/api/status 200` 即通；没有请求 = 客户端侧被拦（本地网络权限/防火墙），
有请求 200 = 服务端正常，问题在 Desktop UI 状态（见 ③）。

### ② URL 和 Token 必须分开填

Remote URL 填 `http://192.168.0.117:8644/u/<user_id>`（**不带 `?token=`**——Desktop 会丢弃
URL 里的 query，Token 框空着就报 "remote gateway incomplete"）。Token 填 Token 框。
带 `?token=` 的完整链接是给浏览器用的。

### ③ 探测报错不会自动恢复，要重新触发

Desktop 只在 **URL 文本变化**（或重开设置页）时重新探测。若输入时容器正在冷启动/被回收，
之后容器就绪了错误也不会自己消失——把 URL 删了重新输入（或重开设置页）即可。
dispatch 侧探活 5 秒内快速返回并自动后台预热，重试一两次即命中。

### ④ 空闲回收语义

匿名探活（`/api/health`、`/api/status`）**不计活跃度**——新建用户若只被探测过，60 分钟后
容器会被自动停机（数据保留）。Desktop 正常连上后的任意操作都会续活；有活跃 WS 连接的用户
不会被回收。刚（重）启动的容器有 10 分钟保活宽限。容器被回收后再次访问会自动拉起
（约 10-30 秒），无需干预。

### ⑤ 下载文件报 404（路径末尾混入 markdown 残留）

Desktop 把 agent 消息里的文件引用渲染成下载链接时，会把 markdown 尾渣带进 `path` 参数，
上游按字面 404（实际文件存在）。已知两类实际案例：

- `` `...` `` 代码格式路径 → 结尾反引号：`/path/report.pptx``
- agent 把 MEDIA: 标签写成加粗并追加中文标注（`**MEDIA:/path/x.pptx**（源文件）`）
  → path 收到 `/path/x.pptx**（源文件）`

dispatch 已对 `path` 参数统一剥尾（反引号/星号/结尾括号标注；括号仅在前面主干以扩展名
收尾时才剥，`报告（终稿）.docx` 这类真实文件名不受影响），日志留痕
`stripped trailing markdown residue from path param`，此类下载链接可直接用。临时绕过：
在 Desktop 文件页浏览 workspace 下载，或让 agent 把文件复制成不带特殊字符的名字。

## API 一览

| 方法/路径 | 鉴权 | 说明 |
|---|---|---|
| `GET /health` | 无 | dispatch 健康检查 |
| `ANY /u/{uid}/{path}` | dispatch token¹ | desktop 反向代理（HTTP，流式） |
| `WS /u/{uid}/{path}` | dispatch token | desktop 反向代理（WebSocket 双向泵） |
| `POST /u/{uid}/v1/chat/completions` | dispatch token | OpenAI 兼容直通（程序化调用） |
| `POST /v1/chat/completions` | —（X-User-Id 头） | 同上，无用户路由（冒烟用；仅内网） |
| `GET /admin` | 无（壳页面） | 管理后台 SPA；数据全部经下列 API 获取 |
| `POST /admin/api/login` `/logout` | admin key | 管理台登录/登出（种/清会话 cookie） |
| `GET /api/overview` | admin key² | 概览：用户/容器计数与关键配置 |
| `GET /api/users` | admin key² | 用户 + 容器状态清单（不回显 token） |
| `POST /api/users` | admin key² | 建用户（惰性），**唯一一次**返回 URL+token |
| `DELETE /api/users/{uid}` | admin key² | 删用户+容器（数据保留）；`?purge=1` 连数据清 |
| `POST /api/users/{uid}/start` `/restart` `/stop` | admin key² | 运维：启动/重启/空闲停机 |
| `GET /api/users/{uid}/logs?tail=` | admin key² | 最近容器日志（1..1000 行） |
| `POST /api/users/{uid}/token/rotate` | admin key² | 单用户 token 轮换（删容器重建，数据保留） |
| `POST /api/agents/refresh` | admin key² | 镜像/配置变更后删容器（数据保留）按新配置重建 |

管理 API 双前缀挂载：`/api/*`（内网脚本，nginx 公网 403）与 `/admin/api/*`（管理台相对路径，公网经 `/hermes/admin/*` 可达）。
² admin 凭据两种形态：`Authorization: Bearer <ADMIN_KEY>` 或登录会话 cookie。

¹ token 四种携带形态：`X-Hermes-Session-Token` 头 / `Authorization: Bearer` / `?token=` / 浏览器会话 cookie（`?token=` 打开首页后自动种下）。
例外：`/u/{uid}/api/health`、`/u/{uid}/api/status` 匿名放行（desktop 启动握手，上游本就公开）；CORS 预检（OPTIONS）放行。

## 冒烟验证

```bash
BASE=http://192.168.0.117:8644 ADMIN_KEY=<key> ./scripts/smoke.sh
# 覆盖：健康/建用户/匿名探活/401/用户隔离/容器冷启动/dashboard token 模式确认

python3 scripts/ws_probe.py http://192.168.0.117:8644/u/alice <token>
# 覆盖：WS 全链路（dispatch → forwarder → dashboard /api/ws）

cd dispatch && python3 -m pytest tests -q   # 单元测试（不起容器；PG 用例自动跳过）
# PG 集成测试（可选，起一次性 PG）：
#   docker run -d --name hd-pg-test -e POSTGRES_USER=hermes -e POSTGRES_PASSWORD=test \
#     -e POSTGRES_DB=hermes -p 54329:5432 postgres:16-alpine
#   HERMES_TEST_DATABASE_URL=postgresql://hermes:test@127.0.0.1:54329/hermes \
#     python3 -m pytest tests/test_registry_pg.py -q && docker rm -f hd-pg-test
```

## 关键机制

| 机制 | 实现 |
|---|---|
| 用户映射 | 确定性容器名 `hb-<sha256(uid)[:8]>`；dispatch token/agent key 由 HMAC(secret, uid, token_version) 派生（无状态，库里不存明文） |
| 管理台会话 | HMAC(secret, sha256(admin_key), issued_at) 签名 cookie，无服务端状态；换任一 key 全体失效 |
| desktop 协议面 | 每用户 dashboard 绑容器内 127.0.0.1（token 模式唯一可用形态），overlay 镜像内 s6 forwarder 暴露到 docker 内网 :9120 并在 TCP 侧改写 Host 头为环回（dashboard 的 DNS-rebinding 防护要求环回 Host + 环回对端），dispatch 为唯一入口 |
| 鉴权纵深 | dispatch 校验 dispatch token → 透传 → dashboard 校验同一 token（同源派生） |
| 冷启动 | 首个请求触发 provision + 健康等待（探活路径最多等 `HERMES_PROXY_START_WAIT_SECONDS`，超时 503 由 desktop 重试）；首启用 `hermes config set` 种子模型配置后重启（幂等，marker 版本化） |
| 生命周期 | `restart_policy: unless-stopped`；空闲清扫（有活跃 WS 的用户不回收）；连接失败自动重启一轮 |
| 资源/存储 | `--cpus/--memory` 限额（env 全局）；`/opt/data` 持久卷（config/记忆/会话/workspace） |
| 模型配置 | 首启 `hermes config set`（`HERMES_MODEL_*`）+ `HERMES_AGENT_EXTRA_ENV`（API Key 等） |

## 运维备忘

- **换模型/改 env**：改 `.env` → `docker compose up -d dispatch`（env 变了）→ `POST /api/agents/refresh`；只改模型配置种子则还需清用户卷上的 INIT 标记或提升 `driver.INIT_VERSION`
- **基线镜像升级**：`docker pull <基线>` → 同步更新 `agent-overlay/constraints-hermes.txt`（对照基线 `uv pip list`）→ `docker compose build agent-image` → 冒烟验证（`scripts/smoke.sh` 协议面 + `scripts/office-smoke.sh` 办公包，后者在服务器上 `docker run --rm --entrypoint bash --memory=2g -v .../office-smoke.sh:/tmp/office-smoke.sh hermes-agent:desktop /tmp/office-smoke.sh`）→ `POST /api/agents/refresh`
- **overlay 办公包增删**：改 `agent-overlay/requirements-office.txt` 后重新 `docker compose build agent-image`；注意 uv 步骤必须带 `--no-config`（基线 WORKDIR=/opt/hermes 的 pyproject.toml 设了 `exclude-newer="14 days"`，否则新包解析被冻结在两周前）。Himalaya 二进制随仓库分发（`agent-overlay/vendor/`），升级时下载新 release 覆盖并同步 Dockerfile 里的 sha256
- **用户忘 token**：token 不再回显（仅创建/轮换时展示一次）——走单用户轮换，把新 token 交给用户重配 desktop
- **单用户换 token**：管理台「轮换 token」或 `POST /api/users/{uid}/token/rotate`——旧 token 立即失效、容器重建（数据保留），desktop 需更新 token 重连
- **彻底删用户**：`DELETE /api/users/{uid}?purge=1`（容器+记忆/会话/文件全清）
- **注册表（PG）**：数据在 named volume `pgdata`；备份 `docker compose exec postgres pg_dump -U hermes hermes > backup.sql`；换 `POSTGRES_PASSWORD` 后需进库改密码或删卷重建（用户记录重建即可，容器/数据目录按 user_id 确定性复用）
- **dispatch 单副本约束**：per-user 冷启动锁/活跃 WS 计数是进程内状态，不要水平扩容 dispatch
- 已知限制：docker.sock root 权限（内网可接受，P2 K8s）；单用户内 hermes 会话串行；overlay 依赖 s6 `user/contents.d` 约定与 `HERMES_DASHBOARD_SESSION_TOKEN` env（升级基线后跑一遍 smoke 即验）
