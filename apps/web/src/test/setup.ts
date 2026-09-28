/**
 * 组件测试的公共准备。
 *
 * jsdom 不实现 crypto.randomUUID，而断言标识正是由它生成；不补上这个全局，
 * 测试跑不起来也测不到真实的标识生成路径。这里只补运行时缺口，不改业务代码。
 */
import { configure } from "@testing-library/dom";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// Ant Design 只查询这些浏览器能力；jsdom 没有真实媒体布局和元素测量。
// 这里提供接口级替身，让组件能运行，但不伪造几何结果，尺寸与裁剪仍交给桌面浏览器验收。
if (!window.matchMedia) {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: (query: string): MediaQueryList => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => undefined,
      removeListener: () => undefined,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      dispatchEvent: () => false,
    }),
  });
}

if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

/**
 * 异步等待的预算显式写出来，不靠 Testing Library 默认的 1000ms。
 *
 * 这类等待是“界面最终会显示某个状态”，不是 1 秒性能指标。整套测试并行跑 15 个 jsdom
 * 环境（默认每个测试文件一个），环境创建本身占掉约一半的跟踪时间，CPU 争用把同一段
 * 代码的墙上时间放大数倍：同一条用例单独跑 306ms，满负载下 1209ms，原日志里到过
 * 1937ms。默认预算下超时会报成“找不到某个元素”，把“界面正在正常推进、只是慢”误报成
 * 产品缺陷，而失败时的 DOM 快照恰恰显示组件停在正确的中间状态（数据已到、行还没
 * 播种），不是错误状态。
 *
 * 断言内容完全不因此放宽：期望的元素、数量与文案一字未改，只给“最终会出现”留出与
 * 实测相符的预算。vitest 的 testTimeout 比它大（见 vitest.config.ts），真正的“元素
 * 一直不出现”仍会以 Testing Library 的报错暴露，并带上完整的 DOM 快照。
 */
configure({ asyncUtilTimeout: 5000 });

if (!globalThis.crypto.randomUUID) {
  let counter = 0;
  Object.defineProperty(globalThis.crypto, "randomUUID", {
    configurable: true,
    value: () => {
      counter += 1;
      const tail = counter.toString(16).padStart(12, "0");
      return `00000000-0000-4000-8000-${tail}` as `${string}-${string}-${string}-${string}-${string}`;
    },
  });
}

afterEach(() => {
  cleanup();
});
