"""Dashboard SPA 修复：反代 subpath 下懒加载 chunk 预加载 404 → 白屏。

上游以 vite 默认 base="/" 构建，动态 chunk 的 modulepreload/CSS 预加载链接由
编译期常量拼接（Xt=function(e){return`/`+e}），不认 X-Forwarded-Prefix。
经 /hermes/u/<uid>/ 反代访问时这些请求绕过前缀 → nginx 404 → CSS 预加载失败
reject 整个懒加载 → React 树崩 → 白屏。Desktop 直连根路径不受影响。

修法：改为读取上游 _serve_index 已注入的 window.__HERMES_BASE_PATH__
（反代场景 = /hermes/u/<uid>；Desktop 直连 = undefined → 行为不变）。
同时把被改内容的 chunk 重命名（内容哈希名不能与内容不符），
并全局更新所有引用，防浏览器 immutable 缓存继续用旧文件。

注意：web_dist 里 api chunk 本身合法引用 __HERMES_BASE_PATH__（lib/api.ts），
不能拿它当"已打补丁"依据——判定只认本补丁写出的精确签名。
"""

import glob
import os
import re
import sys

WEB_DIST = "/opt/hermes/hermes_cli/web_dist"
MARK = 'window.__HERMES_BASE_PATH__'
FIXED_SIG = re.compile(re.escape(f'return({MARK}||"")+"/"'))

# vite/rolldown preload helper 的 base 拼接点：Xt=function(e){return`/`+e}
PAT = re.compile(r"\b(\w+)=function\((\w)\)\{return`/`\+\2\}")

# 引用只可能出现在文本产物里（js/html/css）；字体等二进制不扫
all_files = [
    p
    for p in glob.glob(os.path.join(WEB_DIST, "**", "*"), recursive=True)
    if os.path.isfile(p) and p.endswith((".js", ".html", ".css"))
]

touched = []
for f in sorted(glob.glob(os.path.join(WEB_DIST, "assets", "*.js"))):
    s = open(f, encoding="utf-8").read()
    if FIXED_SIG.search(s):
        continue  # 已打过（幂等），此前运行应已重命名
    s2, n = PAT.subn(
        lambda m: f'{m.group(1)}=function({m.group(2)}){{return({MARK}||"")+"/"+{m.group(2)}}}', s
    )
    if n:
        open(f, "w", encoding="utf-8").write(s2)
        touched.append(f)
        print(f"patched {os.path.basename(f)}: {n} site(s)")

if not touched:
    any_sig = any(FIXED_SIG.search(open(f, encoding="utf-8").read()) for f in all_files if f.endswith(".js"))
    if not any_sig:
        sys.exit("FATAL: preload-base helper not found — upstream build changed, revisit patch")
    print("already patched, nothing to do")
    sys.exit(0)

# ---- 重命名被改的 chunk 并全局更新引用（防 immutable 缓存踩旧内容）----
for f in touched:
    base = os.path.basename(f)
    new_base = base.replace(".js", "-fix1.js")
    refs = 0
    for g in all_files:
        if not os.path.isfile(g) or g == f:
            continue
        t = open(g, encoding="utf-8").read()
        if base in t:
            open(g, "w", encoding="utf-8").write(t.replace(base, new_base))
            refs += 1
    os.rename(f, os.path.join(os.path.dirname(f), new_base))
    print(f"renamed {base} -> {new_base} ({refs} referring files updated)")
print("OK")
