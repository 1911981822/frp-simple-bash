import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 构建产物直接输出到 web/dist，由 lib/webui.py 作为静态目录托管。
// base 用 './' 便于被任意路径前缀托管（不依赖域名根）。
export default defineConfig({
  plugins: [vue()],
  base: './',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    assetsDir: 'assets',
    chunkSizeWarningLimit: 800,
  },
})
