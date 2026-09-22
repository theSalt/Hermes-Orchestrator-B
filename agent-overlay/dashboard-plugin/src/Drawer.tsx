/** overlay 插槽：全屏附件抽屉（清单 + 预览 + 下载 / 删除 / 复制路径）。
 * 预览策略：image→<img>；pdf→<iframe>（浏览器原生查看器）；office→POST /convert
 * 得 PDF 后 iframe；other→占位提示。所有 blob objectURL 统一收账、及时 revoke。
 */
import React, { useEffect, useRef, useState } from 'react'
import { api, fileUrl, fmtSize, fmtTime, KIND_ICON, KIND_LABEL, type AttItem } from './api'
import { useToastSafe } from './sdk'
import { store, useStore } from './store'

interface PreviewState {
  item: AttItem
  phase: 'loading' | 'ready' | 'unsupported' | 'error'
  url?: string
  error?: string
}

const BIG_PREVIEW_BYTES = 50 * 1024 * 1024 // 非 PDF 超过此大小提示改用下载

export function Drawer(): React.ReactElement | null {
  const { items, drawerOpen } = useStore()
  const [preview, setPreview] = useState<PreviewState | null>(null)
  const urlsRef = useRef<Set<string>>(new Set())
  const toast = useToastSafe()

  function revokeAll(): void {
    urlsRef.current.forEach((u) => URL.revokeObjectURL(u))
    urlsRef.current.clear()
  }

  useEffect(() => {
    if (drawerOpen) {
      // 打开时拉最新清单：覆盖 agent 产出文件 / 其他会话上传的变更
      void store.refresh()
    } else {
      revokeAll()
      setPreview(null)
    }
    return revokeAll // 卸载兜底
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [drawerOpen])

  if (!drawerOpen) return null

  function track(url: string): string {
    urlsRef.current.add(url)
    return url
  }

  async function openPreview(item: AttItem): Promise<void> {
    revokeAll()
    setPreview({ item, phase: 'loading' })
    try {
      if (item.kind === 'image') {
        if (item.size > BIG_PREVIEW_BYTES) {
          setPreview({ item, phase: 'unsupported', error: '图片较大，建议下载后查看' })
          return
        }
        const blob = await api.fetchBlob(fileUrl(item.path))
        setPreview({ item, phase: 'ready', url: track(URL.createObjectURL(blob)) })
      } else if (item.kind === 'pdf') {
        const blob = await api.fetchBlob(fileUrl(item.path))
        setPreview({ item, phase: 'ready', url: track(URL.createObjectURL(blob)) })
      } else if (item.kind === 'office') {
        const blob = await api.convert(item.path) // 后端有 mtime 缓存，重复预览零转换
        setPreview({ item, phase: 'ready', url: track(URL.createObjectURL(blob)) })
      } else {
        setPreview({ item, phase: 'unsupported', error: '该类型不支持在线预览' })
      }
    } catch (e) {
      setPreview({ item, phase: 'error', error: e instanceof Error ? e.message : String(e) })
    }
  }

  async function download(item: AttItem): Promise<void> {
    try {
      const blob = await api.fetchBlob(fileUrl(item.path))
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = item.name
      document.body.appendChild(a)
      a.click()
      a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 5000)
    } catch (e) {
      toast.showToast(e instanceof Error ? e.message : '下载失败', 'error')
    }
  }

  async function removeItem(item: AttItem): Promise<void> {
    if (!window.confirm(`删除附件「${item.name}」？此操作不可恢复。`)) return
    try {
      await api.remove(item.path)
      if (preview?.item.path === item.path) {
        revokeAll()
        setPreview(null)
      }
      await store.refresh()
      toast.showToast('已删除', 'success')
    } catch (e) {
      toast.showToast(e instanceof Error ? e.message : '删除失败', 'error')
    }
  }

  function copyPath(item: AttItem): void {
    void navigator.clipboard?.writeText(item.path).then(
      () => toast.showToast('路径已复制', 'success'),
      () => toast.showToast('复制失败，请手动选择路径文本', 'error')
    )
  }

  return (
    <div className="ha-overlay" onClick={() => store.setDrawer(false)}>
      <div className="ha-drawer" onClick={(e) => e.stopPropagation()}>
        <div className="ha-drawer-head">
          <span>附件</span>
          <button className="ha-drawer-close" title="关闭" onClick={() => store.setDrawer(false)}>
            ✕
          </button>
        </div>
        <div className="ha-drawer-body">
          <div className="ha-list">
            {items.length === 0 ? (
              <div className="ha-empty">
                暂无附件
                <br />
                用聊天输入框下方的 📎 上传，文件会存到 /opt/data/attachments
              </div>
            ) : (
              items.map((item) => (
                <div
                  key={item.path}
                  className={`ha-item${preview?.item.path === item.path ? ' ha-item-active' : ''}`}
                  onClick={() => void openPreview(item)}
                >
                  <span>{KIND_ICON[item.kind]}</span>
                  <span className="ha-item-name" title={item.name}>
                    {item.name}
                  </span>
                  <span className="ha-badge">{KIND_LABEL[item.kind]}</span>
                  <span className="ha-item-meta">{fmtSize(item.size)}</span>
                  <span className="ha-item-btn" title="复制路径" onClick={(e) => { e.stopPropagation(); copyPath(item) }}>
                    复制
                  </span>
                  <span
                    className="ha-item-btn"
                    onClick={(e) => {
                      e.stopPropagation()
                      void download(item)
                    }}
                  >
                    下载
                  </span>
                  <span
                    className="ha-item-btn danger"
                    onClick={(e) => {
                      e.stopPropagation()
                      void removeItem(item)
                    }}
                  >
                    删除
                  </span>
                </div>
              ))
            )}
          </div>
          <div className="ha-preview">
            {preview === null ? (
              <div className="ha-empty">选择左侧附件进行预览</div>
            ) : (
              <>
                <div className="ha-preview-head">
                  <span className="ha-preview-name">
                    {KIND_ICON[preview.item.kind]} {preview.item.name} · {fmtSize(preview.item.size)} ·{' '}
                    {fmtTime(preview.item.mtime)}
                  </span>
                  <span
                    className="ha-item-btn"
                    onClick={() => {
                      void download(preview.item)
                    }}
                  >
                    下载原文件
                  </span>
                </div>
                <div className="ha-preview-body">
                  {preview.phase === 'loading' && (
                    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
                      <span className="ha-spin" /> 转换中…
                    </span>
                  )}
                  {preview.phase === 'ready' && preview.item.kind === 'image' && (
                    <img src={preview.url} alt={preview.item.name} />
                  )}
                  {preview.phase === 'ready' && preview.item.kind !== 'image' && (
                    <iframe src={preview.url} title={preview.item.name} />
                  )}
                  {(preview.phase === 'unsupported' || preview.phase === 'error') && (
                    <div className="ha-error">
                      {preview.error}
                      <br />
                      <span
                        className="ha-item-btn"
                        onClick={() => {
                          void download(preview.item)
                        }}
                      >
                        下载原文件
                      </span>
                    </div>
                  )}
                </div>
              </>
            )}
          </div>
        </div>
        {toast.toastNode}
      </div>
    </div>
  )
}
