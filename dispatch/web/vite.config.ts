import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// base './'：构建产物由 dispatch 挂在 /admin/ 下服务（直连或 nginx subpath
// 两种形态下，相对路径资源都能正确解析）。
// 构建产物输出到 ../app/static/admin/，FastAPI 直接服务该目录。
export default defineConfig({
  plugins: [react()],
  base: './',
  build: { outDir: '../app/static/admin', emptyOutDir: true },
  server: {
    port: 5173,
    // 本地开发：dev server 根路径下 UI 以相对路径 'api/*' 请求 → 代理到 dispatch
    proxy: {
      '/api': 'http://localhost:8644',
      '/admin/api': 'http://localhost:8644',
    },
  },
})
