/**
 * 样式：bundle 加载时注入一次 <style>（不占 manifest css 字段，随产物原子更新）。
 * 配色走中性灰 + 透明度，宿主深色主题下自然融合，浅色主题也可读。
 */

const CSS = `
.ha-toolbar{display:flex;align-items:center;gap:8px;padding:4px 8px;flex-wrap:wrap;font-size:13px}
/* 平按钮：无底色无边框，hover 才有淡底——不抢 chat 页视觉 */
.ha-btn{display:inline-flex;align-items:center;gap:4px;border:none;background:transparent;
  color:var(--text-secondary,#9aa0a6);border-radius:6px;padding:3px 8px;font-size:12px;cursor:pointer;line-height:1.6}
.ha-btn:hover{background:rgba(127,127,127,.12);color:inherit}
.ha-btn[disabled]{opacity:.5;cursor:default}
.ha-chip{display:inline-flex;align-items:center;gap:6px;max-width:260px;padding:2px 10px;border-radius:999px;
  background:rgba(127,127,127,.15);font-size:12px}
.ha-chip-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ha-chip-ok{color:#4ade80}.ha-chip-err{color:#f87171}
.ha-spin{display:inline-block;width:13px;height:13px;border:2px solid rgba(127,127,127,.35);
  border-top-color:currentColor;border-radius:50%;animation:ha-spin 1s linear infinite}
@keyframes ha-spin{to{transform:rotate(360deg)}}
.ha-overlay{position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,.55);display:flex;justify-content:flex-end}
.ha-drawer{width:min(920px,92vw);height:100vh;background:#16181d;color:#e6e6e6;display:flex;flex-direction:column;
  box-shadow:-8px 0 32px rgba(0,0,0,.4)}
.ha-drawer-head{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;
  border-bottom:1px solid rgba(127,127,127,.2);font-size:15px;font-weight:600}
.ha-drawer-close{border:none;background:none;color:inherit;font-size:18px;cursor:pointer;padding:2px 8px}
.ha-drawer-body{flex:1;display:flex;min-height:0}
.ha-list{width:360px;min-width:220px;border-right:1px solid rgba(127,127,127,.2);overflow-y:auto}
.ha-item{display:flex;align-items:center;gap:8px;padding:8px 12px;cursor:pointer;
  border-bottom:1px solid rgba(127,127,127,.08);font-size:13px}
.ha-item:hover,.ha-item-active{background:rgba(127,127,127,.12)}
.ha-item-name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ha-item-meta{color:#8a8f98;font-size:11px;white-space:nowrap}
.ha-item-btn{border:none;background:none;color:#7aa2f7;cursor:pointer;padding:2px 4px;font-size:12px;white-space:nowrap}
.ha-item-btn:hover{text-decoration:underline}
.ha-item-btn.danger{color:#f87171}
.ha-badge{flex-shrink:0;padding:1px 8px;border-radius:4px;background:rgba(127,127,127,.2);font-size:11px}
.ha-preview{flex:1;display:flex;flex-direction:column;min-width:0}
.ha-preview-head{display:flex;align-items:center;gap:8px;padding:8px 12px;
  border-bottom:1px solid rgba(127,127,127,.2);font-size:12px;color:#8a8f98}
.ha-preview-name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ha-preview-body{flex:1;display:flex;align-items:center;justify-content:center;overflow:auto;padding:12px;min-height:0}
.ha-preview-body img{max-width:100%;max-height:100%;object-fit:contain}
.ha-preview-body iframe{width:100%;height:100%;border:none;background:#fff}
.ha-empty{color:#8a8f98;padding:24px;text-align:center;font-size:13px;line-height:1.8}
.ha-error{color:#f87171;font-size:13px;text-align:center;line-height:1.8}
.ha-toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);z-index:10001;
  max-width:min(560px,90vw);padding:8px 16px;border-radius:8px;font-size:13px;color:#e6e6e6;
  background:#23262d;border:1px solid rgba(127,127,127,.25);box-shadow:0 6px 24px rgba(0,0,0,.4)}
.ha-toast-success{border-color:rgba(74,222,128,.4)}
.ha-toast-error{border-color:rgba(248,113,113,.5)}
@media (max-width:760px){
  .ha-drawer{width:100vw}
  .ha-drawer-body{flex-direction:column}
  .ha-list{width:100%;max-height:45%;border-right:none;border-bottom:1px solid rgba(127,127,127,.2)}
}
`

export function injectStyles(): void {
  if (document.getElementById('ha-styles')) return
  const el = document.createElement('style')
  el.id = 'ha-styles'
  el.textContent = CSS
  document.head.appendChild(el)
}
