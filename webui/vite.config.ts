import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The console is served by the Python hub from `stackflowos/webassets`, so the
// build writes plain static assets with no server-side rendering.
export default defineConfig({
  plugins: [react()],
  base: '/',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1600,
  },
  server: {
    port: 5180,
    strictPort: false,
    // `python3 scripts/ui_mock.py` serves the fixture API on 17877.  The dev
    // server proxies it so screens iterate against realistic data.
    proxy: {
      '/api': { target: 'http://127.0.0.1:17877', changeOrigin: true, ws: false },
    },
  },
});
