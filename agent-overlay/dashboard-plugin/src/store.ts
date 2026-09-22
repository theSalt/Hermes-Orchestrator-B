/** 模块级共享状态 + 订阅：Toolbar 与 Drawer 是两个独立挂载的 slot 组件
 * （宿主渲染时不传 props），附件清单 / 抽屉开关 / 上传 chips 需在两者间共享。
 */
import { useEffect, useState } from 'react'
import { api, type AttItem } from './api'

export interface UploadChip {
  id: number
  name: string
  state: 'uploading' | 'ok' | 'err'
  message?: string
}

let items: AttItem[] = []
let drawerOpen = false
let chips: UploadChip[] = []
let chipSeq = 0
const subs = new Set<() => void>()

function emit(): void {
  subs.forEach((fn) => fn())
}

export const store = {
  get items(): AttItem[] {
    return items
  },
  get drawerOpen(): boolean {
    return drawerOpen
  },
  get chips(): UploadChip[] {
    return chips
  },
  sub(fn: () => void): () => void {
    subs.add(fn)
    return () => {
      subs.delete(fn)
    }
  },
  setDrawer(open: boolean): void {
    drawerOpen = open
    emit()
  },
  async refresh(): Promise<void> {
    try {
      items = await api.list()
    } catch {
      /* 拉取失败保留旧清单，下次操作再试 */
    }
    emit()
  },
  addChip(name: string): number {
    const id = ++chipSeq
    chips = [...chips, { id, name, state: 'uploading' }]
    emit()
    return id
  },
  setChip(id: number, state: UploadChip['state'], message?: string): void {
    chips = chips.map((c) => (c.id === id ? { ...c, state, message } : c))
    emit()
    if (state !== 'uploading') {
      // 终态 chip 留存几秒供确认，随后自动消失
      setTimeout(() => {
        chips = chips.filter((c) => c.id !== id)
        emit()
      }, 6000)
    }
  },
}

export interface StoreSnapshot {
  items: AttItem[]
  drawerOpen: boolean
  chips: UploadChip[]
}

export function useStore(): StoreSnapshot {
  const [snap, setSnap] = useState<StoreSnapshot>({
    items: store.items,
    drawerOpen: store.drawerOpen,
    chips: store.chips,
  })
  useEffect(
    () =>
      store.sub(() =>
        setSnap({ items: store.items, drawerOpen: store.drawerOpen, chips: store.chips })
      ),
    []
  )
  return snap
}
