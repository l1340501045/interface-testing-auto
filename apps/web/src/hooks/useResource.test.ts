/**
 * 资源读取的范围归属。
 *
 * 这里盯的是切换范围时最容易漏掉的一帧：状态里还留着上一个范围的数据，而组件
 * 已经按新范围渲染并回填。A、B 两个范围故意用相同的版本号（真实场景里版本号就是
 * 各自从 1 开始的），只有值不同——只按版本号判断“是否已回填”的实现会在这里把
 * A 的值当成 B 的第 1 版显示，甚至保存回 B。
 */
import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { useResource } from "./useResource";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

interface Payload {
  version: number;
  values: string[];
}

describe("useResource 的范围归属", () => {
  it("切换范围的第一帧不交出上一个范围的数据，即使两边版本号相同", async () => {
    const gateA = deferred<Payload>();
    const gateB = deferred<Payload>();
    const gates: Record<string, Promise<Payload>> = { A: gateA.promise, B: gateB.promise };
    const requested: string[] = [];

    const { result, rerender } = renderHook(
      ({ scope }: { scope: string }) =>
        useResource<Payload>(scope, () => {
          requested.push(scope);
          return gates[scope];
        }),
      { initialProps: { scope: "A" } },
    );

    expect(result.current.loading).toBe(true);
    gateA.resolve({ version: 1, values: ["A1", "A2"] });
    await waitFor(() => expect(result.current.data?.values).toEqual(["A1", "A2"]));

    rerender({ scope: "B" });

    // 关键一帧：B 的响应还没回来，此时绝不能把 A 的内容当成 B 的第 1 版。
    expect(result.current.data).toBeNull();
    expect(result.current.loading).toBe(true);

    gateB.resolve({ version: 1, values: ["B1"] });
    await waitFor(() => expect(result.current.data?.values).toEqual(["B1"]));
    expect(requested).toEqual(["A", "B"]);
  });

  it("上一个范围的迟到响应不会写进当前范围", async () => {
    const gateA = deferred<Payload>();
    const gateB = deferred<Payload>();

    const { result, rerender } = renderHook(
      ({ scope }: { scope: string }) =>
        useResource<Payload>(scope, () => (scope === "A" ? gateA.promise : gateB.promise)),
      { initialProps: { scope: "A" } },
    );

    rerender({ scope: "B" });
    gateB.resolve({ version: 1, values: ["B1"] });
    await waitFor(() => expect(result.current.data?.values).toEqual(["B1"]));

    // A 的响应此时才回来：它属于已经离开的范围，既不能覆盖数据，也不能报错。
    await act(async () => {
      gateA.resolve({ version: 1, values: ["A1"] });
      await Promise.resolve();
    });

    expect(result.current.data?.values).toEqual(["B1"]);
    expect(result.current.error).toBeNull();
  });

  it("同一范围内刷新时保留当前内容直到新结果到达", async () => {
    const gates = [deferred<Payload>(), deferred<Payload>()];
    let call = 0;
    const { result } = renderHook(() => useResource<Payload>("A", () => gates[call++].promise));

    gates[0].resolve({ version: 2, values: ["v2"] });
    await waitFor(() => expect(result.current.data?.values).toEqual(["v2"]));

    act(() => result.current.reload());

    // 刷新期间旧内容仍在：同一个范围的内容不会在重新拉取时空掉一帧。
    expect(result.current.loading).toBe(true);
    expect(result.current.data?.values).toEqual(["v2"]);

    gates[1].resolve({ version: 3, values: ["v3"] });
    await waitFor(() => expect(result.current.data?.values).toEqual(["v3"]));
    expect(result.current.loading).toBe(false);
  });
});
