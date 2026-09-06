import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// 开发模式: 后端由 ../run.py 提供。让它固定在这个端口，代理才不会指错地方:
//   python ../run.py --port 8720 --dev
// 生产模式: FastAPI 直接托管 dist/，同源，代理不参与。
const API = process.env.SC_API ?? 'http://127.0.0.1:8720'

export default defineConfig({
  // 图标只留一份真值: ../assets 既是桌面壳加载 stockcleaner.ico 的地方,
  // 也是浏览器模式 favicon 的来源 (构建时原样拷进 dist/)。
  publicDir: '../assets',
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: { '/api': { target: API, changeOrigin: true, ws: false } },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    target: 'es2022',
    cssMinify: true,
  },
})
