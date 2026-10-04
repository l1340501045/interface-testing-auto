import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, apiSend } from "../api/client";
import { AssetActionModal, type AssetActionTarget } from "./AssetActionModal";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, apiSend: vi.fn() };
});

const apiSendMock = vi.mocked(apiSend);
const CASE_A = "11111111-1111-4111-8111-111111111111";
const CASE_B = "22222222-2222-4222-8222-222222222222";
const FOLDER_A = "33333333-3333-4333-8333-333333333333";
const FOLDER_B = "44444444-4444-4444-8444-444444444444";
const NOW = "2026-10-04T00:00:00Z";

function deferred() {
  let resolve!: () => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<void>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function target(ids = [CASE_A]): AssetActionTarget {
  return { resourceType: "bulk", intentId: "bulk-intent", label: `当前页所选（${ids.length} 条）`, selector: { mode: "explicit", items: ids.map((id) => ({ resource_type: "case", id, expected_rev: 1 })) } };
}

function selection(body: Record<string, unknown>, number: number) {
  const items = body.items as Array<{ id: string; expected_rev: number }>;
  return {
    selection_id: `aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa${number}`,
    schema_version: 1,
    action: body.action,
    mode: "explicit",
    workspace_id: "w",
    project_id: "p",
    principal_id: "u",
    created_at: NOW,
    expires_at: "2030-10-04T00:00:00Z",
    counts: { selected: items.length, eligible: items.length, excluded: 0, cases: items.length, folders: 0 },
    root: null,
    preview_items: items.map((item) => ({ resource_type: "case", id: item.id, rev: item.expected_rev, state: "active", name: item.id === CASE_A ? "用例甲" : "用例乙", parent_id: null, folder_id: null, outcome: "eligible", code: null })),
    excluded_items: [],
  };
}

function modalButton(name: string): HTMLButtonElement {
  return within(screen.getByRole("dialog")).getByRole("button", { name: new RegExp(name.split("").join("\\s*")) }) as HTMLButtonElement;
}

const folders = [
  { id: FOLDER_A, name: "目标甲", parent_id: null, rev: 1, archived_at: null, availability: "available" as const, has_children: false, archive_operation_id: null, archive_root_id: null, restore_mode: null, ancestor_path: [] },
  { id: FOLDER_B, name: "目标乙", parent_id: null, rev: 1, archived_at: null, availability: "available" as const, has_children: false, archive_operation_id: null, archive_root_id: null, restore_mode: null, ancestor_path: [] },
];

async function chooseTarget(name: string) {
  const comboboxes = within(screen.getByRole("dialog")).getAllByRole("combobox");
  const select = comboboxes.at(-1) as HTMLElement;
  await waitFor(() => expect(select.closest(".ant-select")?.classList.contains("ant-select-disabled")).toBe(false));
  fireEvent.mouseDown(select);
  fireEvent.click(await screen.findByText(name, { selector: ".ant-select-item-option-content" }));
}

describe("资产动作弹窗的异步归属", () => {
  beforeEach(() => apiSendMock.mockReset());

  it.each([
    ["成功", false],
    ["错误", true],
  ])("同帧第二份预览迟到%s也不能覆盖已进入unknown的原操作", async (_label, lateFailure) => {
    const first = deferred();
    const second = deferred();
    const posts: Array<{ key: string | undefined; body: unknown }> = [];
    let previews = 0;
    apiSendMock.mockImplementation((async (path: string, method: string, body: Record<string, unknown>, parse: (raw: unknown) => unknown, options?: { headers?: Record<string, string> }) => {
      if (path === undefined) return undefined;
      if (String(path).endsWith("/asset-selections")) {
        previews += 1;
        await (previews === 1 ? first.promise : second.promise);
        if (previews === 2 && lateFailure) throw new Error("迟到预览失败");
        return parse(selection(body, previews));
      }
      if (String(path).endsWith("/asset-operations") && method === "POST") {
        posts.push({ key: options?.headers?.["Idempotency-Key"], body });
        throw new ApiError(503, "unavailable", "结果不明", null);
      }
      throw new Error(`未覆盖 ${method} ${path}`);
    }) as never);
    const release = vi.fn();
    render(<AssetActionModal workspaceId="w" projectId="p" principalId="u" active target={target()} action="move" folders={[]} onAcquire={() => ({ accept: () => "ignored", release })} onClose={() => undefined} onDone={() => undefined} />);
    const generate = modalButton("生成预览");
    fireEvent.click(generate);
    await waitFor(() => expect(previews).toBe(1));
    await act(async () => {
      first.resolve();
      for (let index = 0; index < 30; index += 1) await Promise.resolve();
      expect(generate.isConnected).toBe(true);
      expect(generate.disabled).toBe(false);
      fireEvent.click(generate);
    });
    expect(previews).toBe(2);
    const confirm = modalButton("确认执行");
    fireEvent.click(confirm);
    expect(await screen.findByRole("button", { name: "确认原操作" })).toBeTruthy();
    const frozen = structuredClone(posts[0]);
    expect(modalButton("取消").disabled).toBe(true);
    if (lateFailure) second.reject(new Error("迟到预览失败")); else second.resolve();
    await act(async () => { for (let index = 0; index < 20; index += 1) await Promise.resolve(); });
    expect(screen.getByRole("button", { name: "确认原操作" })).toBeTruthy();
    expect(modalButton("取消").disabled).toBe(true);
    expect(posts).toEqual([frozen]);
    expect(release).not.toHaveBeenCalled();
  });

  it("普通来源连续2到1再到0时以第二轮1/0/1替换旧摘要", async () => {
    let previewCount = 0;
    let readRound = 1;
    let operationPosts = 0;
    apiSendMock.mockImplementation((async (path: string, method: string, body: Record<string, unknown>, parse: (raw: unknown) => unknown) => {
      if (path === undefined) return undefined;
      if (String(path).endsWith("/asset-selections")) { previewCount += 1; return parse(selection(body, previewCount)); }
      if (method === "GET" && String(path).includes("/cases/")) {
        const id = String(path).split("/").at(-1) as string;
        if (readRound === 2 || id === CASE_B) throw new ApiError(404, "not_found", "当前不可访问", null);
        return parse({ id, name: "用例甲", folder_id: null, request: { method: "GET", path: "/a", query_params: [], headers: [], body_type: "none", body: "" }, assertions: [], rev: 2, status: "draft", latest_version: null, updated_at: NOW, snapshot_hash: "h2" });
      }
      if (String(path).endsWith("/asset-operations")) { operationPosts += 1; throw new Error("不应执行写请求"); }
      throw new Error(`未覆盖 ${method} ${path}`);
    }) as never);
    render(<AssetActionModal workspaceId="w" projectId="p" principalId="u" active target={target([CASE_A, CASE_B])} action="archive" folders={[]} onClose={() => undefined} onDone={() => undefined} />);
    fireEvent.click(modalButton("生成预览"));
    await screen.findByText("将处理 2 项，排除 0 项");
    fireEvent.click(modalButton("修改参数并读取最新来源"));
    expect(await screen.findByText("原对象 2 项，本次可读取 1 项，跳过 1 项")).toBeTruthy();
    await waitFor(() => expect(modalButton("生成预览").disabled).toBe(false));
    fireEvent.click(modalButton("生成预览"));
    await screen.findByText("将处理 1 项，排除 0 项");
    readRound = 2;
    fireEvent.click(modalButton("修改参数并读取最新来源"));
    expect(await screen.findByText("原对象 1 项，本次可读取 0 项，跳过 1 项")).toBeTruthy();
    expect(screen.queryByText("原对象 2 项，本次可读取 1 项，跳过 1 项")).toBeNull();
    expect(operationPosts).toBe(0);
  });

  it("失败项自动预览隐藏后失效，返回改目标只按新预览执行", async () => {
    const stalePreview = deferred();
    const previewBodies: Record<string, unknown>[] = [];
    const operationBodies: Array<{ body: Record<string, unknown>; key?: string }> = [];
    apiSendMock.mockImplementation((async (path: string, method: string, body: Record<string, unknown>, parse: (raw: unknown) => unknown, options?: { headers?: Record<string, string> }) => {
      if (path === undefined) return undefined;
      if (String(path).endsWith("/asset-selections")) {
        previewBodies.push(structuredClone(body));
        if (previewBodies.length === 2) await stalePreview.promise;
        return parse(selection(body, previewBodies.length));
      }
      if (method === "GET" && String(path).endsWith(`/cases/${CASE_B}`)) return parse({ id: CASE_B, name: "用例乙", folder_id: null, request: { method: "GET", path: "/b", query_params: [], headers: [], body_type: "none", body: "" }, assertions: [], rev: 2, status: "draft", latest_version: null, updated_at: NOW, snapshot_hash: "h2" });
      if (String(path).endsWith("/asset-operations") && method === "POST") {
        operationBodies.push({ body: structuredClone(body), key: options?.headers?.["Idempotency-Key"] });
        const retry = operationBodies.length === 2;
        const ids = retry ? [CASE_B] : [CASE_A, CASE_B];
        return parse({
          operation_id: `bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb${operationBodies.length}`,
          operation_key: options?.headers?.["Idempotency-Key"], action: "move", workspace_id: "w", project_id: "p", principal_id: "u", result_schema_version: 1, created_at: NOW,
          result: { result_kind: "completed", selection_id: body.selection_id, root: null, counts: { input: ids.length, succeeded: 1, no_change: 0, conflict: retry ? 0 : 1, failed: 0 }, items: retry ? [{ resource_type: "case", id: CASE_B, outcome: "succeeded", code: null, message: "已移动", new_rev: 3, asset: { resource_type: "case", id: CASE_B, name: "用例乙", rev: 3, state: "active", folder_id: (body.parameters as Record<string, unknown>).target_folder_id, parent_id: null, archived_at: null } }] : [{ resource_type: "case", id: CASE_A, outcome: "succeeded", code: null, message: "已移动", new_rev: 2, asset: { resource_type: "case", id: CASE_A, name: "用例甲", rev: 2, state: "active", folder_id: FOLDER_A, parent_id: null, archived_at: null } }, { resource_type: "case", id: CASE_B, outcome: "conflict", code: "revision_conflict", message: "修订冲突", new_rev: null, asset: null }], members: [] },
        });
      }
      throw new Error(`未覆盖 ${method} ${path}`);
    }) as never);
    const props = { workspaceId: "w", projectId: "p", principalId: "u", active: true, target: target([CASE_A, CASE_B]), action: "move" as const, folders, onClose: () => undefined, onDone: () => undefined };
    const view = render(<AssetActionModal {...props} />);
    await chooseTarget("目标甲");
    fireEvent.click(modalButton("生成预览"));
    await screen.findByText("将处理 2 项，排除 0 项");
    fireEvent.click(modalButton("确认执行"));
    expect(await screen.findByText("冲突：修订已变化，请重新读取后再试")).toBeTruthy();
    fireEvent.click(await screen.findByRole("button", { name: "重新读取并重试失败项" }));
    await waitFor(() => expect(previewBodies).toHaveLength(2));
    view.rerender(<AssetActionModal {...props} active={false} />);
    view.rerender(<AssetActionModal {...props} active />);
    await chooseTarget("目标乙");
    stalePreview.resolve();
    await act(async () => { for (let index = 0; index < 20; index += 1) await Promise.resolve(); });
    expect(screen.queryByRole("button", { name: "确认执行" })).toBeNull();
    expect(operationBodies).toHaveLength(1);
    fireEvent.click(modalButton("生成预览"));
    await screen.findByText("将处理 1 项，排除 0 项");
    fireEvent.click(modalButton("确认执行"));
    await waitFor(() => expect(operationBodies).toHaveLength(2));
    expect(previewBodies[1]).toMatchObject({ items: [{ id: CASE_B, expected_rev: 2 }], parameters: { target_folder_id: FOLDER_A } });
    expect(previewBodies[2]).toMatchObject({ items: [{ id: CASE_B, expected_rev: 2 }], parameters: { target_folder_id: FOLDER_B } });
    expect(operationBodies[1].body).toMatchObject({ parameters: { target_folder_id: FOLDER_B } });
    expect(operationBodies[1].key).not.toBe(operationBodies[0].key);
  });

  it("冻结时已归档的不适用项与后续修订冲突使用不同原因", async () => {
    apiSendMock.mockImplementation((async (path: string, method: string, body: Record<string, unknown>, parse: (raw: unknown) => unknown, options?: { headers?: Record<string, string> }) => {
      if (path === undefined) return undefined;
      if (String(path).endsWith("/asset-selections")) return parse({
        selection_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", schema_version: 1, action: "archive", mode: "explicit", workspace_id: "w", project_id: "p", principal_id: "u", created_at: NOW, expires_at: "2030-10-04T00:00:00Z",
        counts: { selected: 1, eligible: 0, excluded: 1, cases: 1, folders: 0 }, root: null, preview_items: [], excluded_items: [{ resource_type: "case", id: CASE_A, rev: 1, state: "archived", name: "已归档用例", parent_id: null, folder_id: null, outcome: "excluded", code: "revision_or_state_conflict" }],
      });
      if (String(path).endsWith("/asset-operations") && method === "POST") return parse({
        operation_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", operation_key: options?.headers?.["Idempotency-Key"], action: "archive", workspace_id: "w", project_id: "p", principal_id: "u", result_schema_version: 1, created_at: NOW,
        result: { result_kind: "completed", selection_id: body.selection_id, root: null, counts: { input: 1, succeeded: 0, no_change: 0, conflict: 1, failed: 0 }, items: [{ resource_type: "case", id: CASE_A, outcome: "conflict", code: "revision_or_state_conflict", message: "未改变", new_rev: null, asset: null }], members: [] },
      });
      throw new Error(`未覆盖 ${method} ${path}`);
    }) as never);
    render(<AssetActionModal workspaceId="w" projectId="p" principalId="u" active target={target()} action="archive" folders={[]} onClose={() => undefined} onDone={() => undefined} />);
    fireEvent.click(modalButton("生成预览"));
    expect(await screen.findByText("排除：已经归档，不适用于再次归档")).toBeTruthy();
    fireEvent.click(modalButton("确认执行"));
    expect(await screen.findByText("冲突：预览时已经归档，不适用于再次归档")).toBeTruthy();
    expect(screen.queryByText(/修订已变化/)).toBeNull();
  });

  it.each([
    ["archive" as const, "archived" as const, "已归档用例"],
    ["restore" as const, "active" as const, "活动用例"],
  ])("真实asset_state_conflict在%s预览与结果中说明当前状态不适用", async (action, state, name) => {
    apiSendMock.mockImplementation((async (path: string, method: string, body: Record<string, unknown>, parse: (raw: unknown) => unknown, options?: { headers?: Record<string, string> }) => {
      if (path === undefined) return undefined;
      if (String(path).endsWith("/asset-selections")) return parse({
        selection_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", schema_version: 1, action, mode: "explicit", workspace_id: "w", project_id: "p", principal_id: "u", created_at: NOW, expires_at: "2030-10-04T00:00:00Z",
        counts: { selected: 1, eligible: 0, excluded: 1, cases: 1, folders: 0 }, root: null, preview_items: [], excluded_items: [{ resource_type: "case", id: CASE_A, rev: 1, state, name, parent_id: null, folder_id: null, outcome: "excluded", code: "asset_state_conflict" }],
      });
      if (String(path).endsWith("/asset-operations") && method === "POST") return parse({
        operation_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", operation_key: options?.headers?.["Idempotency-Key"], action, workspace_id: "w", project_id: "p", principal_id: "u", result_schema_version: 1, created_at: NOW,
        result: { result_kind: "completed", selection_id: body.selection_id, root: null, counts: { input: 1, succeeded: 0, no_change: 0, conflict: 1, failed: 0 }, items: [{ resource_type: "case", id: CASE_A, outcome: "conflict", code: "asset_state_conflict", message: "未改变", new_rev: null, asset: null }], members: [] },
      });
      throw new Error(`未覆盖 ${method} ${path}`);
    }) as never);
    render(<AssetActionModal workspaceId="w" projectId="p" principalId="u" active target={target()} action={action} folders={[]} onClose={() => undefined} onDone={() => undefined} />);
    fireEvent.click(modalButton("生成预览"));
    expect(await screen.findByText("排除：当前状态不适用于本次操作")).toBeTruthy();
    fireEvent.click(modalButton("确认执行"));
    expect(await screen.findByText("冲突：当前状态不适用于本次操作")).toBeTruthy();
    expect(screen.getByText(/冲突 1/)).toBeTruthy();
    expect(screen.queryByText(/无变化：/)).toBeNull();
  });
});
