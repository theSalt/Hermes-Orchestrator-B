/**
 * window.__HERMES_PLUGIN_SDK__ 的最小类型面 + 取用入口。
 *
 * 上游 SDK 标注 SPIKE（sdkVersion "1.1.0"）：这里只声明本插件实际用到的成员，
 * 上游漂移会在 `npm run build`（tsc --noEmit）阶段暴露，而不是运行时才炸。
 * 组件库按名字宽松取用（feature-detect），hook/API 这类强依赖则严格声明。
 */
import type { ComponentType, ReactNode } from 'react'
import { createElement, useEffect, useState } from 'react'

export interface ToastHandle {
  showToast(message: string, kind?: 'success' | 'error'): void
  /** 可直接渲染的 toast 元素（由本包装层构造）；无 toast 时为 null */
  toastNode: ReactNode
}

export interface SdkApi {
  fetchJSON<T = unknown>(url: string, init?: RequestInit): Promise<T>
  authedFetch(url: string, init?: RequestInit): Promise<Response>
  buildWsUrl(path: string, params?: Record<string, string>): Promise<string>
}

export interface HermesSdk {
  sdkVersion: string
  React: typeof import('react')
  hooks: {
    useState?: typeof import('react').useState
    useEffect?: typeof import('react').useEffect
    useCallback?: typeof import('react').useCallback
    useMemo?: typeof import('react').useMemo
    useRef?: typeof import('react').useRef
    useToast?: () => ToastHandle
  }
  /** REST 端点客户端（getPlugins/getThemes/...）。注意 0.21.2 里它没有
   * authedFetch/buildWsUrl——那两个挂在顶层；新版本可能挪进来，两者都兼容 */
  api?: Partial<SdkApi> & Record<string, unknown>
  fetchJSON?: SdkApi['fetchJSON']
  authedFetch?: SdkApi['authedFetch']
  buildWsUrl?: SdkApi['buildWsUrl']
  components?: Record<string, ComponentType<Record<string, unknown>>>
  utils?: {
    cn?: (...classes: unknown[]) => string
    timeAgo?: (ts: number) => string
  }
}

export interface PluginRegistry {
  register(name: string, component: ComponentType<unknown>): void
  /** 参数顺序：(插件名, 插槽名, 组件)——上游 registry.ts 运行时签名 */
  registerSlot(plugin: string, slot: string, component: ComponentType<unknown>): void
}

export function getSdk(): HermesSdk | null {
  return (globalThis as { __HERMES_PLUGIN_SDK__?: HermesSdk }).__HERMES_PLUGIN_SDK__ ?? null
}

export function getRegistry(): PluginRegistry | null {
  return (globalThis as { __HERMES_PLUGINS__?: PluginRegistry }).__HERMES_PLUGINS__ ?? null
}

/**
 * 归一化取 auth 工具：0.21.2 挂 SDK 顶层（sdk.authedFetch / sdk.buildWsUrl），
 * `sdk.api` 只是 REST 端点客户端（getPlugins 等，无 fetch 工具）；更新版本
 * 可能挪进 api 命名空间——两处都找，都没有才算缺。
 */
export function getApi(): SdkApi | null {
  const sdk = getSdk()
  if (!sdk) return null
  const authedFetch = sdk.authedFetch ?? sdk.api?.authedFetch
  const buildWsUrl = sdk.buildWsUrl ?? sdk.api?.buildWsUrl
  if (typeof authedFetch !== 'function' || typeof buildWsUrl !== 'function') return null
  const fetchJSON =
    sdk.fetchJSON ??
    sdk.api?.fetchJSON ??
    (<T>(url: string, init?: RequestInit) =>
      authedFetch!(url, init).then(async (res) => {
        if (!res.ok) throw new Error(`${res.status}`)
        return (await res.json()) as T
      }))
  return { authedFetch, buildWsUrl, fetchJSON }
}

/** 宿主组件按名取用；SDK 缺失/未提供时返回 null，调用方自行退回原生元素。 */
export function comp(name: string): ComponentType<Record<string, unknown>> | null {
  return getSdk()?.components?.[name] ?? null
}

/**
 * toast 的 feature-detect 包装。
 *
 * 宿主 useToast() 返回 {showToast, toast}，其中 toast 是 **数据对象**
 * {message, type}（3 秒自动清空），不是可渲染节点——直接当 child 渲染会
 * React #31 崩整棵树（踩过）。这里把它转成自绘的 toast 元素再交给组件树。
 * SDK 缺 hook 时退化为 console.warn。
 */
export function useToastSafe(): ToastHandle {
  // eslint-disable-next-line react-hooks/rules-of-hooks
  const [, tick] = useState(0)
  const sdk = getSdk()
  if (sdk?.hooks?.useToast) {
    try {
      const h = sdk.hooks.useToast()
      const raw: unknown = (h as { toast?: unknown }).toast
      let node: ReactNode = null
      if (raw != null && typeof raw === 'object' && 'message' in (raw as object)) {
        const d = raw as { message?: unknown; type?: unknown }
        node = createElement(
          'div',
          { className: `ha-toast ha-toast-${String(d.type ?? 'info')}` },
          String(d.message ?? '')
        )
      } else if (typeof raw === 'string' || typeof raw === 'number') {
        node = raw
      }
      return { showToast: (m, k) => h.showToast(m, k), toastNode: node }
    } catch {
      /* 落到 fallback */
    }
  }
  return {
    showToast: (message: string) => {
      console.warn('[hermes-attachments]', message)
      tick((n) => n + 1)
    },
    toastNode: null,
  }
}

/** useSyncExternalStore 的保守替代（老宿主 React 也可用）：订阅 store 快照。 */
export function useSubscription(subscribe: (fn: () => void) => () => void): void {
  const [, setVer] = useState(0)
  useEffect(() => subscribe(() => setVer((v) => v + 1)), [subscribe])
}
