import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// 测试与开发共用同一套 React／TS 编译配置，避免“页面能跑、测试另用一套转译”。
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    // App 级回归会同时挂载完整工作台。按容器实际可用 CPU 使用一半 worker；CI 只有
    // 2 核时自动使用 1 个，避免固定高并发制造假超时，同时保留高配开发机的合理并行度。
    maxWorkers: "50%",
    // 只用于兜住“卡住不返回”的测试（例如渲染死循环），不做性能指标。比
    // setup.ts 里 Testing Library 的异步等待预算大，这样“元素一直不出现”会先以
    // 带 DOM 快照的库报错暴露出来，而不是被这里超时截断成一句无信息的时间到。
    testTimeout: 15000,
  },
});
