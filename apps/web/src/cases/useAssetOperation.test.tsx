import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LeaveGuardProvider } from "../hooks/leaveGuard";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, apiSend: vi.fn() };
});

import { ApiError, apiSend } from "../api/client";
import { useAssetOperation } from "./useAssetOperation";

const apiSendMock = vi.mocked(apiSend);
const payload = { schema_version: 1 as const, action: "archive" as const, selection_id: "selection-1", parameters: {} };
const selectionBody = { schema_version: 1, action: "archive", mode: "explicit", items: [{ resource_type: "case", id: "case-1", expected_rev: 1 }], parameters: {} };
const selection = { selection_id: "selection-1", schema_version: 1, action: "archive", mode: "explicit", workspace_id: "w", project_id: "p", principal_id: "u", created_at: "2026-10-04T00:00:00Z", expires_at: "2026-10-04T00:10:00Z", counts: { selected: 1, eligible: 1, excluded: 0, cases: 1, folders: 0 }, root: null, preview_items: [{ resource_type: "case", id: "case-1", rev: 1, state: "active", name: "用例", parent_id: null, folder_id: null, outcome: "eligible", code: null }], excluded_items: [] };
const operation = {
  operation_id: "operation-1", operation_key: "asset:key-1", action: "archive",
  workspace_id: "w", project_id: "p", principal_id: "u", result_schema_version: 1, created_at: "2026-10-04T00:00:00Z",
  result: { result_kind: "completed", selection_id: "selection-1", root: null, counts: { input: 1, succeeded: 1, no_change: 0, conflict: 0, failed: 0 }, items: [{ resource_type: "case", id: "case-1", outcome: "succeeded", code: null, message: "已归档", new_rev: 2, asset: { id: "case-1", resource_type: "case", name: "用例", rev: 2, state: "archived", folder_id: null, parent_id: null, archived_at: "2026-10-04T00:00:00Z" } }], members: [] },
};
function wrapper({ children }: { children: ReactNode }) { return <LeaveGuardProvider>{children}</LeaveGuardProvider>; }

describe("资产操作控制器", () => {
  beforeEach(() => apiSendMock.mockReset());

  it("网络结果不明后先按原key查询，404才用完全相同的key和payload确认原POST", async () => {
    const calls: Array<{ path: string; method: string; body: unknown; key?: string }> = [];
    let operationPosts = 0;
    apiSendMock.mockImplementation((async (path: string, method: string, body: unknown, parse: (raw: unknown) => unknown, options?: { headers?: Record<string, string> }) => {
      calls.push({ path, method, body, key: options?.headers?.["Idempotency-Key"] });
      if (String(path).endsWith("/asset-selections")) return parse(selection);
      if (String(path).endsWith("/asset-operations")) {
        operationPosts += 1;
        if (operationPosts === 1) throw new ApiError(503, "unavailable", "服务暂不可用", null);
      }
      if (method === "GET") throw new ApiError(404, "not_found", "未找到", null);
      return parse ? parse(operation) : operation;
    }) as never);
    const { result } = renderHook(() => useAssetOperation("w", "p", "u", true), { wrapper });
    await act(async () => { await result.current.preview(selectionBody); });
    await act(async () => { await result.current.submit(payload, "asset:key-1"); });
    expect(result.current.state.phase).toBe("unknown");
    await act(async () => { await result.current.reconcile(); });
    await waitFor(() => expect(result.current.state.phase).toBe("done"));
    expect(calls.slice(1)).toEqual([
      expect.objectContaining({ method: "POST", body: payload, key: "asset:key-1" }),
      expect.objectContaining({ method: "GET", body: undefined }),
      expect.objectContaining({ method: "POST", body: payload, key: "asset:key-1" }),
    ]);
  });

  it("结果不明后失权保持unknown且不重放写请求", async () => {
    apiSendMock.mockImplementationOnce((async (_path: string, _method: string, _body: unknown, parse: (raw: unknown) => unknown) => parse(selection)) as never)
      .mockRejectedValueOnce(new ApiError(503, "unavailable", "服务暂不可用", null))
      .mockRejectedValueOnce(new ApiError(403, "forbidden", "无权确认", null));
    const { result } = renderHook(() => useAssetOperation("w", "p", "u", true), { wrapper });
    await act(async () => { await result.current.preview(selectionBody); });
    await act(async () => { await result.current.submit(payload, "asset:key-1"); });
    await act(async () => { await result.current.reconcile(); });
    expect(result.current.state.phase).toBe("unknown");
    expect(apiSendMock).toHaveBeenCalledTimes(3);
  });
  it("真实幂等键冲突停止自动重试但保留原意图", async () => {
    apiSendMock.mockImplementationOnce((async (_path: string, _method: string, _body: unknown, parse: (raw: unknown) => unknown) => parse(selection)) as never)
      .mockRejectedValueOnce(new ApiError(409, "idempotency_key_conflict", "操作键已绑定其他请求", null));
    const { result } = renderHook(() => useAssetOperation("w", "p", "u", true), { wrapper });
    await act(async () => { await result.current.preview(selectionBody); });
    await act(async () => { await result.current.submit(payload, "asset:key-1"); });
    expect(result.current.state).toMatchObject({ phase: "unknown", key: "asset:key-1", payload, retryBlocked: true });
    await act(async () => { await result.current.reconcile(); });
    expect(apiSendMock).toHaveBeenCalledTimes(2);
  });
  it("外层归属相同但selection_id错误时保持unknown", async () => {
    apiSendMock.mockImplementationOnce((async (_path: string, _method: string, _body: unknown, parse: (raw: unknown) => unknown) => parse(selection)) as never)
      .mockImplementationOnce((async (_path: string, _method: string, _body: unknown, parse: (raw: unknown) => unknown) => parse({ ...operation, result: { ...operation.result, selection_id: "selection-other" } })) as never);
    const { result } = renderHook(() => useAssetOperation("w", "p", "u", true), { wrapper });
    await act(async () => { await result.current.preview(selectionBody); });
    await act(async () => { await result.current.submit(payload, "asset:key-1"); });
    expect(result.current.state).toMatchObject({ phase: "unknown", key: "asset:key-1", payload });
  });
  it("预览对象与请求ID不一致时拒绝建立可确认预览", async () => {
    const wrong={...selection,preview_items:[{...selection.preview_items[0],id:"case-other"}]};
    apiSendMock.mockImplementationOnce((async (_path: string, _method: string, _body: unknown, parse: (raw: unknown) => unknown) => parse(wrong)) as never);
    const { result } = renderHook(() => useAssetOperation("w", "p", "u", true), { wrapper });
    await act(async () => { await result.current.preview(selectionBody); });
    expect(result.current.state).toMatchObject({ phase: "error", message: "资产预览对象与请求不匹配" });
  });
  it("completed缺少原selection结果项时保持unknown", async () => {
    apiSendMock.mockImplementationOnce((async (_path: string, _method: string, _body: unknown, parse: (raw: unknown) => unknown) => parse(selection)) as never)
      .mockImplementationOnce((async (_path: string, _method: string, _body: unknown, parse: (raw: unknown) => unknown) => parse({ ...operation, result: { ...operation.result, counts: { input: 0, succeeded: 0, no_change: 0, conflict: 0, failed: 0 }, items: [] } })) as never);
    const { result } = renderHook(() => useAssetOperation("w", "p", "u", true), { wrapper });
    await act(async () => { await result.current.preview(selectionBody); });
    await act(async () => { await result.current.submit(payload, "asset:key-1"); });
    expect(result.current.state.phase).toBe("unknown");
  });
});
