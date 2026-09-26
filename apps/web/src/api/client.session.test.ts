import { afterEach, describe, expect, it, vi } from "vitest";

import { apiSend, invalidateClientSession, onUnauthorized } from "./client";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

describe("API 客户端主体代际", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("response.text 等待期间失效时，旧成功结果不能返回", async () => {
    const body = deferred<string>();
    let reading = false;
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: new Headers(),
      text: () => { reading = true; return body.promise; },
    })));
    const pending = apiSend("/synthetic", "POST", {}, (raw) => raw);
    await vi.waitFor(() => expect(reading).toBe(true));
    invalidateClientSession();
    body.resolve('{"accepted":true}');
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  });

  it("旧 401 正文迟到不会广播并清理新主体", async () => {
    const body = deferred<string>();
    const unauthorized = vi.fn();
    const unsubscribe = onUnauthorized(unauthorized);
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: false,
      status: 401,
      headers: new Headers(),
      text: () => body.promise,
    })));
    const pending = apiSend("/old", "POST", {}, (raw) => raw);
    invalidateClientSession();
    body.resolve('{"code":"unauthorized","message":"旧主体失效","trace_id":null}');
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(unauthorized).not.toHaveBeenCalled();
    unsubscribe();
  });
});
