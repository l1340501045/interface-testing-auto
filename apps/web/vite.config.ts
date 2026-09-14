import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      // 业务接口：后端本身挂在 /api/v1 下，代理不能再剥掉 /api，
      // 否则 /api/v1/me 会变成 /v1/me 而全部 404。
      "/api/v1": { target: "http://api:8000" },
      // 开发底座既有契约：前端 /api/health 对应后端 /health，健康检查继续沿用。
      "/api/health": { target: "http://api:8000", rewrite: (path) => path.replace(/^\/api/, "") },
    },
  },
});
