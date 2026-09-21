#!/usr/bin/env bash
# hermes-dispatch 冒烟验证（B 方案）。
#
# 用法：BASE=http://192.168.0.117:8644 ADMIN_KEY=<key> ./scripts/smoke.sh
#
# 验收点：
#   1. dispatch 健康
#   2. 建 alice / bob 两用户，gateway_url/token 签发
#   3. 匿名可达 /api/health、/api/status（desktop 启动握手前提）
#   4. 鉴权路径：无 token 401，有 token 200
#   5. 冷启动：/api/status 返回 agent 状态；docker ps 出现 hermes-xxxx 容器
#   6. token 错的用户互相隔离（alice 的 token 打 bob → 401）

set -euo pipefail

BASE="${BASE:-http://192.168.0.117:8644}"
ADMIN_KEY="${ADMIN_KEY:?需要 ADMIN_KEY}"
JQ="${JQ:-jq}"

fail() { echo "✗ $*" >&2; exit 1; }
ok()   { echo "✓ $*"; }

curl_sf() { curl -sf "$@"; }
code()    { curl -s -o /dev/null -w '%{http_code}' "$@"; }

echo "== 1. dispatch health =="
curl_sf "$BASE/health" | grep -q '"ok"' || fail "dispatch /health"
ok "dispatch /health"

echo "== 2. create users =="
# 幂等建用户：已存在（409）时改走轮换——清单不回显 token（仅创建/轮换
# 一次性展示），轮换是唯一能重新取得 token 的途径
ALICE=$(curl -s -X POST "$BASE/api/users" -H "Authorization: Bearer $ADMIN_KEY" \
  -H 'Content-Type: application/json' -d '{"user_id":"alice","display_name":"Alice"}')
BOB=$(curl -s -X POST "$BASE/api/users" -H "Authorization: Bearer $ADMIN_KEY" \
  -H 'Content-Type: application/json' -d '{"user_id":"bob"}')
if ! echo "$ALICE" | $JQ -e .token >/dev/null 2>&1; then
  ALICE=$(curl_sf -X POST "$BASE/api/users/alice/token/rotate" -H "Authorization: Bearer $ADMIN_KEY")
fi
if ! echo "$BOB" | $JQ -e .token >/dev/null 2>&1; then
  BOB=$(curl_sf -X POST "$BASE/api/users/bob/token/rotate" -H "Authorization: Bearer $ADMIN_KEY")
fi
ALICE_TOKEN=$(echo "$ALICE" | $JQ -r .token)
ALICE_URL=$(echo "$ALICE" | $JQ -r .gateway_url)
BOB_TOKEN=$(echo "$BOB" | $JQ -r .token)
[ -n "$ALICE_TOKEN" ] && [ "$ALICE_TOKEN" != "null" ] || fail "alice token 签发"
ok "alice: $ALICE_URL"

echo "== 3. anonymous probes (desktop 启动握手) =="
# 冷启动等待：探活 5s 快速失败由这里代 desktop 重试（新建/轮换后容器需重建）
for _ in $(seq 1 24); do
  [ "$(code "$BASE/u/alice/api/health")" = "200" ] && break
  sleep 5
done
code "$BASE/u/alice/api/health" | grep -q 200 || fail "匿名 /api/health 应 200（冷启动超时，请重跑）"
code "$BASE/u/alice/api/status" | grep -q 200 || fail "匿名 /api/status 应 200"
ok "匿名探活路径 200"

echo "== 4. auth gate =="
[ "$(code "$BASE/u/alice/")" = "401" ] || fail "无 token 访问 / 应 401"
[ "$(code -H "X-Hermes-Session-Token: $ALICE_TOKEN" "$BASE/u/alice/")" = "200" ] || fail "alice token 访问 / 应 200"
[ "$(code -H "X-Hermes-Session-Token: $ALICE_TOKEN" "$BASE/u/bob/")" = "401" ] || fail "alice token 打 bob 应 401"
[ "$(code "$BASE/u/nosuchuser/")" = "404" ] || fail "未知用户应 404"
ok "token 校验/用户隔离"

echo "== 5. agent containers =="
curl_sf "$BASE/api/users" -H "Authorization: Bearer $ADMIN_KEY" | $JQ -e '.users[] | select(.user_id=="alice") | .container.state == "running"' >/dev/null \
  || fail "alice 容器应 running（如冷启动超时请稍后重跑）"
ok "alice 容器 running"

echo "== 6. /api/status (token) =="
curl_sf -H "X-Hermes-Session-Token: $ALICE_TOKEN" "$BASE/u/alice/api/status" | $JQ -e '.auth_required == false' >/dev/null \
  || fail "dashboard 应处于 token 模式（auth_required=false）"
ok "dashboard token 模式确认（auth_required=false）"

echo
echo "全部通过。desktop 接入信息："
echo "  Remote URL: $ALICE_URL"
echo "  Token:      $ALICE_TOKEN"
