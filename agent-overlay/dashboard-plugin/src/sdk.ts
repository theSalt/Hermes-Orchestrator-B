/**
 * window.__HERMES_PLUGIN_SDK__ 的最小类型面 + 取用入口。
 *
 * 上游 SDK 标注 SPIKE（sdkVersion "1.1.0"）：这里只声明本插件实际用到的成员，
 * 上游漂移会在 `npm run build`（tsc --noEmit）阶段暴露，而不是运行时才炸。
 * 组件库按名字宽松取用（feature-detect），hook/API 这类强依赖则严格声明。
 */
import type { ComponentType, ReactNode } from 'react'
import { useEffect, useState } from 'react'

export interface ToastHandle {
  showToast(message: string, kind?: 'success' | 'error'): void
  /** 宿主 Toast 组件的渲染物（放在组件树里一次即可）；SDK 缺失时为 null */
  toast: ReactNode
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
  api: SdkApi
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

/** 宿主组件按名取用；SDK 缺失/未提供时返回 null，调用方自行退回原生元素。 */
export function comp(name: string): ComponentType<Record<string, unknown>> | null {
  return getSdk()?.components?.[name] ?? null
}

/**
 * toast 的 feature-detect 包装。条件只在进程生命周期内取一次确定值（SDK 是否存在），
 * 不违反 hooks 规则的实际语义；SDK 缺 hook 时退化为 console.warn。
 */
export function useToastSafe(): ToastHandle {
  // eslint-disable-next-line react-hooks/rules-of-hooks
  const [, tick] = useState(0)
  const sdk = getSdk()
  if (sdk?.hooks?.useToast) {
    try {
      return sdk.hooks.useToast()
    } catch {
      /* 落到 fallback */
    }
  }
  return {
    showToast: (message: string) => {
      console.warn('[hermes-attachments]', message)
      tick((n) => n + 1)
    },
    toast: null,
  }
}

/** useSyncExternalStore 的保守替代（老宿主 React 也可用）：订阅 store 快照。 */
export function useSubscription(subscribe: (fn: () => void) => () => void): void {
  const [, setVer] = useState(0)
  useEffect(() => subscribe(() => setVer((v) => v + 1)), [subscribe])
}
