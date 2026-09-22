/**
 * React 取自宿主 SDK，绝不打包（见 vite.config.ts 的 alias）。
 * 本文件只应在 SDK 就绪时被 import 执行——main.tsx 已做守卫，SDK 缺失时
 * 组件根本不会注册/渲染。
 */
const sdk = (globalThis as { __HERMES_PLUGIN_SDK__?: { React?: unknown } }).__HERMES_PLUGIN_SDK__
const React = sdk?.React as typeof import('react')

if (!React) {
  console.warn('[hermes-attachments] SDK.React 缺失：组件将不可渲染（预期于 SDK 未就绪场景）')
}

export default React
export const useState = React.useState
export const useEffect = React.useEffect
export const useCallback = React.useCallback
export const useMemo = React.useMemo
export const useRef = React.useRef
export const createElement = React.createElement
