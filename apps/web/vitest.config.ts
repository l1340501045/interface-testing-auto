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
    // App 级回归会同时挂载完整工作台。实测 5 worker 会持续占用约 6～8 核，并让重 App
    // 用例在 15 秒预算内漂移超时；2 worker 在同源码、同预算下完整通过。Vitest 5 按
    // Math.round(percent * availableParallelism) 取整，因此 20% 在 10 核为 2、2 核至少为 1。
    maxWorkers: "20%",
    // 只用于兜住“卡住不返回”的测试（例如渲染死循环），不做性能指标。比
    // setup.ts 里 Testing Library 的异步等待预算大，这样“元素一直不出现”会先以
    // 带 DOM 快照的库报错暴露出来，而不是被这里超时截断成一句无信息的时间到。
    testTimeout: 15000,
  },
});
