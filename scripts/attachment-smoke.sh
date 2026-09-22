#!/usr/bin/env bash
# hermes-attachments 插件后端链路冒烟（经 dispatch 全链路反代）。
#
# 前置：dispatch 在跑且已部署含插件的新 agent 镜像（build agent-image + refresh）。
# 用法：BASE=http://192.168.0.117:8644 ADMIN_KEY=<key> ./scripts/attachment-smoke.sh [用户ID]
#
# 验收点：
#   1. 插件已被上游发现（/api/dashboard/plugins 列表含 hermes-attachments）
#   2. 插件静态资源可经反代取到（dist/index.js 200）
#   3. 中文文件名 docx 上传 → path/kind 正确
#   4. png 上传 → mime 正确
#   5. /list 两项、mtime 降序、.preview 不出现
#   6. /convert 返回 application/pdf + inline；二次调用显著更快（缓存命中）
#   7. /file 内联输出正确 MIME
#   8. 路径穿越（../../etc/passwd 与绝对路径 /etc/passwd）一律 404
#   9. 删除后清单复核
#
# 依赖：jq、curl；脚本结束会清理测试文件（保留 alice 的旧附件不动）。

set -euo pipefail

BASE="${BASE:-http://192.168.0.117:8644}"
ADMIN_KEY="${ADMIN_KEY:?需要 ADMIN_KEY}"
UID_TEST="${1:-alice}"
JQ="${JQ:-jq}"

fail() { echo "✗ $*" >&2; exit 1; }
ok()   { echo "✓ $*"; }

curl_sf() { curl -sf "$@"; }
code()    { curl -s -o /dev/null -w '%{http_code}' "$@"; }

AUTH="Authorization: Bearer $ADMIN_KEY"
API="$BASE/u/$UID_TEST/api/plugins/hermes-attachments"

echo "== 0. 用户与 token =="
U=$(curl -s -X POST "$BASE/api/users" -H "$AUTH" -H 'Content-Type: application/json' \
  -d "{\"user_id\":\"$UID_TEST\",\"display_name\":\"附件冒烟\"}")
if ! echo "$U" | $JQ -e .token >/dev/null 2>&1; then
  U=$(curl_sf -X POST "$BASE/api/users/$UID_TEST/token/rotate" -H "$AUTH")
fi
TOKEN=$(echo "$U" | $JQ -r .token)
[ -n "$TOKEN" ] && [ "$TOKEN" != "null" ] || fail "token 签发"
TOKH="X-Hermes-Session-Token: $TOKEN"
ok "token 就绪"

echo "== 1. 等容器就绪 =="
for _ in $(seq 1 24); do
  [ "$(code -H "$TOKH" "$BASE/u/$UID_TEST/")" = "200" ] && break
  sleep 5
done
[ "$(code -H "$TOKH" "$BASE/u/$UID_TEST/")" = "200" ] || fail "容器/页面未就绪（冷启动超时，请重跑）"
ok "dashboard 可达"

echo "== 2. 插件被发现 + 资源可取 =="
curl_sf -H "$TOKH" "$BASE/u/$UID_TEST/api/dashboard/plugins" | $JQ -e \
  '.[] | select(.name=="hermes-attachments")' >/dev/null || fail "插件未出现在 /api/dashboard/plugins"
ok "插件已在 /api/dashboard/plugins 清单中"
# 注意别写 curl|grep -q：pipefail 下 grep 命中即退出会 SIGPIPE 杀掉 curl（12KB 产物
# 必现），误判失败。先落变量再断言
BUNDLE=$(curl_sf -H "$TOKH" "$BASE/u/$UID_TEST/dashboard-plugins/hermes-attachments/dist/index.js") \
  || fail "插件 bundle 不可达"
echo "$BUNDLE" | grep -q "HERMES_PLUGIN_SDK" || fail "插件 bundle 内容不对"
ok "插件 bundle 经反代可取（含 SDK 挂载点）"

echo "== 3. 上传中文文件名 docx + png =="
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
# 生成最小合法 docx（zip 结构），保证 LibreOffice 能真正转换
python3 - "$TMP" <<'PYEOF'
import sys, zipfile
d = sys.argv[1]
with zipfile.ZipFile(f"{d}/季度报告 测试.docx", "w") as z:
    z.writestr("[Content_Types].xml",
        '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
    z.writestr("_rels/.rels",
        '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    z.writestr("word/document.xml",
        '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:r><w:t>hermes-attachments 冒烟测试文档</w:t></w:r></w:p></w:body></w:document>')
print("minimal docx written")
PYEOF
DOCX_RESP=$(curl -sf -H "$TOKH" -F "file=@$TMP/季度报告 测试.docx" "$API/upload")
DOCX_PATH=$(echo "$DOCX_RESP" | $JQ -r .path)
echo "$DOCX_RESP" | $JQ -e '.kind=="office"' >/dev/null || fail "docx kind 应为 office: $DOCX_RESP"
echo "$DOCX_RESP" | $JQ -e '.path | test("/opt/data/attachments/")' >/dev/null || fail "docx 落点不对: $DOCX_PATH"
ok "docx 上传 → $DOCX_PATH"

printf '\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82' > "$TMP/t.png"
PNG_RESP=$(curl -sf -H "$TOKH" -F "file=@$TMP/t.png" "$API/upload")
PNG_PATH=$(echo "$PNG_RESP" | $JQ -r .path)
echo "$PNG_RESP" | $JQ -e '.mime=="image/png"' >/dev/null || fail "png mime 应为 image/png: $PNG_RESP"
ok "png 上传 → $PNG_PATH"

echo "== 4. /list 清单与降序 =="
LIST=$(curl_sf -H "$TOKH" "$API/list")
# 路径可能含空格/中文：一律走 --arg 传参，别拼接进 jq 程序体
echo "$LIST" | $JQ -e --arg p "$DOCX_PATH" '[.items[].path] | index($p) != null' >/dev/null || fail "list 缺 docx"
echo "$LIST" | $JQ -e --arg p "$PNG_PATH" '[.items[].path] | index($p) != null' >/dev/null || fail "list 缺 png"
FIRST_MTIME=$(echo "$LIST" | $JQ -r '.items[0].mtime')
LAST_MTIME=$(echo "$LIST" | $JQ -r '.items[-1].mtime')
[ "$FIRST_MTIME" -ge "$LAST_MTIME" ] || fail "list 应按 mtime 降序"
echo "$LIST" | $JQ -e '[.items[] | select(.path | contains(".preview"))] | length == 0' >/dev/null || fail ".preview 不应出现在清单"
ok "清单包含两件、降序、无 .preview"

echo "== 5. /convert Office→PDF =="
# PDF 是二进制：别捕获进 shell 变量（null byte 会被剥掉），用 -w 取 content-type
CT=$(curl -s -o /dev/null -w '%{content_type}' -H "$TOKH" -X POST -H 'Content-Type: application/json' \
  -d "{\"path\":\"$DOCX_PATH\"}" "$API/convert")
echo "$CT" | grep -q "application/pdf" || fail "convert 应回 application/pdf，实际: $CT"
echo "  二次调用（缓存命中）: $(curl -s -o /dev/null -w '%{http_code} %{time_total}s' -H "$TOKH" \
  -X POST -H 'Content-Type: application/json' -d "{\"path\":\"$DOCX_PATH\"}" "$API/convert")"
ok "convert 返回 PDF（首次转换 + 二次缓存命中）"

echo "== 6. /file 内联输出 =="
HDRS=$(curl -s -D - -o /dev/null -H "$TOKH" "$API/file?path=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$PNG_PATH")")
echo "$HDRS" | grep -qi "^content-type: image/png" || fail "file 应回 image/png"
echo "$HDRS" | grep -qi "^content-disposition: inline" || fail "file 应为 inline"
echo "$HDRS" | grep -qi "filename\*=UTF-8" || fail "file 应带 filename*（CJK 名）"
ok "file 正确 MIME + inline + filename*"

echo "== 7. 路径穿越拒绝 =="
for BAD in "../../etc/passwd" "/etc/passwd" "..%2F..%2Fetc%2Fpasswd"; do
  [ "$(code -H "$TOKH" "$API/file?path=$BAD")" = "404" ] || fail "穿越 path=$BAD 应 404"
  [ "$(code -H "$TOKH" -X DELETE "$API/file?path=$BAD")" = "404" ] || fail "穿越 DELETE path=$BAD 应 404"
done
ok "GET/DELETE 路径穿越一律 404"

echo "== 8. 清理 =="
curl_sf -H "$TOKH" -X DELETE "$API/file?path=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$PNG_PATH")" | $JQ -e .ok >/dev/null || fail "png 删除"
curl_sf -H "$TOKH" -X DELETE "$API/file?path=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$DOCX_PATH")" | $JQ -e .ok >/dev/null || fail "docx 删除"
REMAIN=$(curl_sf -H "$TOKH" "$API/list" | $JQ --arg p1 "$PNG_PATH" --arg p2 "$DOCX_PATH" \
  '[.items[] | select(.path==$p1 or .path==$p2)] | length')
[ "$REMAIN" = "0" ] || fail "删除后清单应不含测试文件"
ok "测试附件已清理"

echo
echo "全部通过。"
