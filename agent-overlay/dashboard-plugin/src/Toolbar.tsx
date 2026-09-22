/** chat:bottom 插槽：📎 上传 + 上传 chips + 附件抽屉入口 + 拖拽上传。
 * 宿主渲染 slot 组件时不传 props；状态经 store.ts 与 Drawer 共享。
 * 按钮用自绘 .ha-btn（透明平按钮）而非宿主 Button——宿主主题底色在 chat 页太跳。
 */
import React, { useRef, useState } from 'react'
import { api } from './api'
import { prefill } from './pty'
import { useToastSafe } from './sdk'
import { store, useStore } from './store'

export function Toolbar(): React.ReactElement {
  const { chips } = useStore()
  const inputRef = useRef<HTMLInputElement>(null)
  const busyRef = useRef(false)
  const [dragOver, setDragOver] = useState(false)
  const toast = useToastSafe()

  async function uploadAll(fileList: FileList | File[]): Promise<void> {
    const files = Array.from(fileList)
    if (!files.length || busyRef.current) return
    busyRef.current = true
    const okPaths: string[] = []
    // 顺序上传：容器只有 2 核，避免并发写放大；单个失败不阻断后续
    for (const file of files) {
      const chip = store.addChip(file.name)
      try {
        const item = await api.upload(file)
        store.setChip(chip, 'ok')
        okPaths.push(item.path)
      } catch (e) {
        store.setChip(chip, 'err', e instanceof Error ? e.message : String(e))
        toast.showToast(`上传失败：${file.name}`, 'error')
      }
    }
    if (okPaths.length) {
      await store.refresh()
      try {
        await prefill('[附件] ' + okPaths.join('  ') + ' ')
        toast.showToast(
          okPaths.length === 1 ? '附件已上传，路径已填入输入框' : `已上传 ${okPaths.length} 个附件，路径已填入输入框`,
          'success'
        )
      } catch {
        toast.showToast('已上传，但无法自动填入输入框——请在附件抽屉中复制路径', 'error')
      }
    }
    busyRef.current = false
  }

  const btnProps = { disabled: chips.some((c) => c.state === 'uploading') }

  return (
    <div
      className="ha-toolbar"
      onDragOver={(e) => {
        e.preventDefault()
        setDragOver(true)
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => {
        e.preventDefault()
        setDragOver(false)
        if (e.dataTransfer.files.length) void uploadAll(e.dataTransfer.files)
      }}
      style={dragOver ? { outline: '1px dashed rgba(127,127,127,.6)', outlineOffset: '-2px' } : undefined}
    >
      <input
        ref={inputRef}
        type="file"
        multiple
        style={{ display: 'none' }}
        onChange={(e) => {
          if (e.target.files?.length) void uploadAll(e.target.files)
          e.currentTarget.value = ''
        }}
      />
      <button
        className="ha-btn"
        title="上传附件"
        {...btnProps}
        onClick={() => inputRef.current?.click()}
      >
        📎
      </button>
      {chips.map((chip) => (
        <span key={chip.id} className="ha-chip" title={chip.message ?? chip.name}>
          {chip.state === 'uploading' ? (
            <span className="ha-spin" />
          ) : chip.state === 'ok' ? (
            <span className="ha-chip-ok">✓</span>
          ) : (
            <span className="ha-chip-err">✗</span>
          )}
          <span className="ha-chip-name">{chip.name}</span>
        </span>
      ))}
      <span style={{ flex: 1 }} />
      <button className="ha-btn" title="附件管理" onClick={() => store.setDrawer(true)}>
        🗂
      </button>
      {toast.toastNode}
    </div>
  )
}
