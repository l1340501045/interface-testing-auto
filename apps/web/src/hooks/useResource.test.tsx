/**
 * 资源读取的范围边界（V1／V3）。
 *
 * 状态里必须同时保存“这份内容是哪个 key 的结果”。否则切换范围后的第一帧拿到的仍是
 * 上一个范围的数据，组件会把它当成“新范围已经加载好的内容”去回填，把 A 项目的数据
 * 按 B 项目的身份显示甚至保存回去；仅靠“清空数据”拦不住这一帧，因为清空要等到
 * effect 执行之后。
 *
 * 同时要求旧请求真的被取消：只丢弃晚到的响应，上一范围的错误提示仍会停在已经无关
 * 的界面上。
 */
import { renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { useResource } from "./useResource";

/** 挂起的加载：由测试决定什么时候、带着哪份内容返回。 */
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

describe("数据必须带着自己的范围", () => {
  it("key 一变，上一范围的旧数据立刻不算数", async () => {
    const first = deferred<string>();
    const second = deferred<string>();
    const loaders: Record<string, () => Promise<string>> = {
      "A/1": () => first.promise,
      "B/1": () => second.promise,
    };
    const { result, rerender } = renderHook(
      ({ key }: { key: string }) => useResource<string>(key, () => loaders[key]()),
      { initialProps: { key: "A/1" } },
    );

    first.resolve("A 项目的内容");
    await waitFor(() => expect(result.current.data).toBe("A 项目的内容"));

    rerender({ key: "B/1" });
    // 这是关键的一帧：B 的内容还没到，绝不能把 A 的内容当成 B 的。
    expect(result.current.data).toBeNull();
    expect(result.current.loading).toBe(true);

    second.resolve("B 项目的内容");
    await waitFor(() => expect(result.current.data).toBe("B 项目的内容"));
  });

  it("切换范围时取消旧请求，晚到的旧结果不会覆盖当前范围", async () => {
    const signals: Record<string, AbortSignal> = {};
    const pending = deferred<string>();
    const { result, rerender } = renderHook(
      ({ key }: { key: string }) =>
        useResource<string>(key, (signal) => {
          signals[key] = signal;
          return key === "A/1" ? pending.promise : Promise.resolve("B 项目的内容");
        }),
      { initialProps: { key: "A/1" } },
    );

    expect(signals["A/1"].aborted).toBe(false);
    rerender({ key: "B/1" });
    // 旧请求被真正取消，而不只是“不再看它的结果”。
    expect(signals["A/1"].aborted).toBe(true);
    await waitFor(() => expect(result.current.data).toBe("B 项目的内容"));

    // 旧请求随后迟到的结果不能把 B 的内容顶掉。
    pending.resolve("A 项目的内容");
    await waitFor(() => expect(result.current.data).toBe("B 项目的内容"));
  });
});
