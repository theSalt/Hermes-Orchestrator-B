import path from 'node:path'
import { defineConfig } from 'vite'

// 产物是注入宿主页面的 IIFE 单文件（上游 usePlugins 以 <script src> 直接加载，
// 约定 entry=dist/index.js）。React 一律取宿主 SDK（window.__HERMES_PLUGIN_SDK__
// .React），经 alias 指到 shim，绝不打包进 bundle——产物应 <100KB。
export default defineConfig({
  resolve: {
    alias: { react: path.resolve(__dirname, 'src/react-shim.ts') },
  },
  build: {
    target: 'es2020',
    outDir: 'dashboard/dist',
    emptyOutDir: true,
    lib: {
      entry: path.resolve(__dirname, 'src/main.tsx'),
      name: 'HermesAttachments',
      formats: ['iife'],
      fileName: () => 'index.js',
    },
  },
})
