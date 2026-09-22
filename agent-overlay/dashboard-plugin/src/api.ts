/** 后端（plugin_api.py，挂载于 /api/plugins/hermes-attachments/）的调用封装。
 * 鉴权/basePath 全部交给宿主 SDK 的 authedFetch（自动带 token 头或 cookie，
 * 并拼 window.__HERMES_BASE_PATH__ 前缀——nginx subpath 部署自动正确）。
 */
import { getApi } from './sdk'

const BASE = '/api/plugins/hermes-attachments'

export interface AttItem {
  path: string
  name: string
  size: number
  mtime: number
  mime: string
  kind: 'image' | 'pdf' | 'office' | 'other'
}

async function authed(url: string, init?: RequestInit): Promise<Response> {
  const api = getApi()
  if (!api) throw new Error('插件 SDK 不可用')
  const res = await api.authedFetch(url, init)
  if (!res.ok) {
    let detail = ''
    try {
      detail = ((await res.json()) as { detail?: string }).detail ?? ''
    } catch {
      /* 非 JSON 错误体 */
    }
    throw new Error(detail || `请求失败（${res.status}）`)
  }
  return res
}

export function fileUrl(path: string): string {
  return `${BASE}/file?path=${encodeURIComponent(path)}`
}

export const api = {
  async upload(file: File): Promise<AttItem> {
    const fd = new FormData()
    fd.append('file', file, file.name)
    const res = await authed(`${BASE}/upload`, { method: 'POST', body: fd })
    return (await res.json()) as AttItem
  },
  async list(): Promise<AttItem[]> {
    const res = await authed(`${BASE}/list`)
    return ((await res.json()) as { items: AttItem[] }).items
  },
  async fetchBlob(url: string): Promise<Blob> {
    return await (await authed(url)).blob()
  },
  /** Office → PDF：后端直接回 PDF 字节（有 mtime 缓存，命中零转换）。 */
  async convert(path: string): Promise<Blob> {
    return await (
      await authed(`${BASE}/convert`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path }),
      })
    ).blob()
  },
  async remove(path: string): Promise<void> {
    await authed(`${BASE}/file?path=${encodeURIComponent(path)}`, { method: 'DELETE' })
  },
}

export function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

export function fmtTime(mtime: number): string {
  return new Date(mtime * 1000).toLocaleString()
}

export const KIND_LABEL: Record<AttItem['kind'], string> = {
  image: '图片',
  pdf: 'PDF',
  office: '文档',
  other: '文件',
}

export const KIND_ICON: Record<AttItem['kind'], string> = {
  image: '🖼️',
  pdf: '📄',
  office: '📊',
  other: '📎',
}
