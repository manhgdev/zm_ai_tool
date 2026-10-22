import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { resolve } from 'path'

export default defineConfig({
  root: resolve(__dirname),
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': resolve(__dirname, 'src') } },
  build: {
    outDir: resolve(__dirname, 'dist'),
    emptyOutDir: true,
    sourcemap: false,
    minify: 'esbuild',
    chunkSizeWarningLimit: 600,
    rollupOptions: {
      output: {
        manualChunks: {
          'react-vendor': ['react', 'react-dom'],
          'ui-panels': ['react-resizable-panels'],
        },
      },
    },
  },
  server: {
    // Windows: mặc định đôi khi chỉ ::1 → 127.0.0.1 fail → @vite/client hủy env.mjs
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': process.env.ZM_AI_TOOL_API_URL || 'http://127.0.0.1:8787',
      '/data': process.env.ZM_AI_TOOL_API_URL || 'http://127.0.0.1:8787',
    },
    // ponytail: đừng watch data/venv/backup — Windows handle phình → Vite listen nhưng treo HTML
    watch: {
      ignored: [
        '**/backend/data/**',
        '**/backend/public/**',
        '**/backend/.venv/**',
        '**/backend/.venv-*/**',
        '**/.git/**',
        '**/*.bk',
        '**/*.bk[0-9]',
        '**/*.mp4',
        '**/*.wav',
      ],
    },
  },
})
