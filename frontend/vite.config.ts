import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  build: {
    modulePreload: {
      resolveDependencies: (_filename, deps) =>
        deps.filter((dependency) => !dependency.includes('echarts-')),
    },
    rollupOptions: {
      output: {
        manualChunks(id) {
          return id.includes('echarts') ? 'echarts' : undefined
        },
      },
    },
  },
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8026',
      },
    },
  },
})
