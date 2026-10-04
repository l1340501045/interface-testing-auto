import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LeaveGuardProvider } from "../hooks/leaveGuard";
import { AppProviders } from "../theme/AppProviders";
import { CaseLibrary } from "./CaseLibrary";

const calls = vi.hoisted(() => ({
  get: [] as string[],
  send: [] as Array<{ path: string; method: string; body: unknown }>,
  viewGate: null as Promise<void> | null,
  viewFailure: null as Error | null,
  libraryFailure: null as Error | null,
  folderFailure: null as Error | null,
  emptyLibrary: false,
  rootHasMore: false,
  rootHasChildren: false,
  childFailure: null as Error | null,
  folderName: "订单目录",
  libraryFolderName: "订单目录",
  operationRejected: false,
  operationGate: null as Promise<void> | null,
  operationFailure: null as Error | null,
  operationNoChange: false,
  sourceGate: null as Promise<void> | null,
  sourceUnreadableIds: [] as string[],
  libraryTotal: 2,
}));

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  const raw = (path: string): unknown => {
    if (path.includes("/asset-folders") && calls.folderFailure !== null) throw calls.folderFailure;
    if (path.includes("/asset-folders")) {
      const query = new URLSearchParams(path.split("?")[1] ?? "");
      if (query.get("parent_mode") === "exact" && !/^[0-9a-f-]{36}$/i.test(query.get("parent_id") ?? "")) {
        throw new Error(`非法父目录：${query.get("parent_id")}`);
      }
      if (query.get("parent_mode") === "exact" && calls.childFailure !== null) throw calls.childFailure;
      if (query.get("parent_mode") === "exact") return {
        items: [{
          id: "66666666-6666-4666-8666-666666666666", name: "子目录", parent_id: "11111111-1111-4111-8111-111111111111", rev: 1,
          archived_at: null, availability: "available", has_children: false,
          archive_operation_id: null, archive_root_id: null, restore_mode: null, ancestor_path: [{ id: "11111111-1111-4111-8111-111111111111", name: calls.folderName }],
        }], total: 1, next_cursor: null,
      };
    }
    if (path.includes("/asset-folders") && path.includes("state=archived")) return { items: [], total: 0, next_cursor: null };
    if (path.includes("/asset-folders") && path.includes("cursor=root-next")) return {
      items: [{
        id: "55555555-5555-4555-8555-555555555555", name: "第二页目录", parent_id: null, rev: 1,
        archived_at: null, availability: "available", has_children: false,
        archive_operation_id: null, archive_root_id: null, restore_mode: null, ancestor_path: [],
      }], total: 2, next_cursor: null,
    };
    if (path.includes("/asset-folders")) return {
      items: [{
        id: "11111111-1111-4111-8111-111111111111", name: calls.folderName, parent_id: null, rev: 1,
        archived_at: null, availability: "available", has_children: calls.rootHasChildren,
        archive_operation_id: null, archive_root_id: null, restore_mode: null, ancestor_path: [],
      }], total: calls.rootHasMore ? 2 : 1, next_cursor: calls.rootHasMore ? "root-next" : null,
    };
    if (path.includes("/case-views")) return [];
    if (path.includes("/case-library") && calls.libraryFailure !== null) throw calls.libraryFailure;
    if (path.includes("/case-library") && calls.emptyLibrary) return { items: [], total: 0, next_cursor: null };
    if (path.includes("/case-library")) return {
      items: [
        {
          id: "22222222-2222-4222-8222-222222222222", name: "查询订单", method: "GET", path: "/orders",
          folder_id: "11111111-1111-4111-8111-111111111111", asset_status: "active", availability: "available",
          folder_path: [{ id: "99999999-9999-4999-8999-999999999999", name: "父目录" }, { id: "11111111-1111-4111-8111-111111111111", name: calls.libraryFolderName }],
          draft_rev: 2, updated_at: "2026-10-04T01:02:03Z", latest_version: 1, favorite: false, last_opened_at: null,
        },
        {
          id: "33333333-3333-4333-8333-333333333333", name: "旧目录用例", method: "POST", path: "/legacy",
          folder_id: null, asset_status: "active", availability: "folder_unavailable",
          folder_path: [],
          draft_rev: 1, updated_at: "2026-10-03T01:02:03Z", latest_version: null, favorite: true, last_opened_at: null,
        },
      ], total: calls.libraryTotal, next_cursor: null,
    };
    throw new Error(`未覆盖 GET ${path}`);
  };
  return {
    ...actual,
    apiGet: vi.fn(async (path: string, parse: (value: unknown) => unknown) => {
      calls.get.push(path);
      return parse(raw(path));
    }),
    apiSend: vi.fn(async (path: string, method: string, body: unknown, parse: (value: unknown) => unknown, options?: { headers?: Record<string, string> }) => {
      calls.send.push({ path, method, body });
      if (method === "GET" && calls.sourceUnreadableIds.some((id) => path.endsWith(`/cases/${id}`))) throw new Error("当前不可读取");
      if (path.endsWith("/asset-selections") && method === "POST") { const request=body as {action:string;mode:string;items?:Array<{id:string;expected_rev:number}>}; const selected=request.mode==="filter"?[{id:"22222222-2222-4222-8222-222222222222",expected_rev:2},{id:"33333333-3333-4333-8333-333333333333",expected_rev:1}]:(request.items??[]); return parse({ selection_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", schema_version: 1, action: request.action, mode: request.mode, workspace_id: "w", project_id: "p", principal_id: "u", created_at: "2026-10-04T00:00:00Z", expires_at: "2026-10-04T00:10:00Z", counts: { selected: selected.length, eligible: selected.length, excluded: 0, cases: selected.length, folders: 0 }, root: null, preview_items: selected.map((item)=>({ resource_type: "case", id: item.id, rev: item.expected_rev, state: "active", name: "查询订单", parent_id: null, folder_id: "11111111-1111-4111-8111-111111111111", outcome: "eligible", code: null })), excluded_items: [] }); }
      if (path.endsWith("/cases/22222222-2222-4222-8222-222222222222") && method === "GET") { if (calls.sourceGate) await calls.sourceGate; return parse({ id:"22222222-2222-4222-8222-222222222222",folder_id:"11111111-1111-4111-8111-111111111111",name:"查询订单",request:{method:"GET",path:"/orders",query_params:[],headers:[],body_type:"none",body:""},assertions:[],rev:3,status:"draft",latest_version:null,updated_at:"2026-10-04T00:00:00Z",snapshot_hash:"h3" }); }
      if (path.endsWith("/asset-operations") && method === "POST") {
        if (calls.operationGate !== null) await calls.operationGate;
        if (calls.operationFailure !== null) throw calls.operationFailure;
        const request = body as { action: string };
        if (request.action === "case_copy") return parse({ operation_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", operation_key: options?.headers?.["Idempotency-Key"], action: "case_copy", workspace_id: "w", project_id: "p", principal_id: "u", result_schema_version: 1, created_at: "2026-10-04T00:00:01Z", result: calls.operationRejected ? { result_kind: "rejected", selection_id: null, code: "name_conflict", message: "副本名称冲突", no_asset_changes: true, conflicts: [] } : { result_kind: "completed", selection_id: null, root: null, counts: { input: 1, succeeded: 1, no_change: 0, conflict: 0, failed: 0 }, items: [{ resource_type: "case", id: "77777777-7777-4777-8777-777777777777", outcome: "succeeded", code: null, message: "已复制", new_rev: 1, asset: { id: "77777777-7777-4777-8777-777777777777", resource_type: "case", name: "查询订单 副本", rev: 1, state: "active", folder_id: null, parent_id: null, archived_at: null } }], members: [] } });
        return parse({ operation_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", operation_key: options?.headers?.["Idempotency-Key"], action: request.action, workspace_id: "w", project_id: "p", principal_id: "u", result_schema_version: 1, created_at: "2026-10-04T00:00:01Z", result: { result_kind: "completed", selection_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", root: null, counts: calls.operationNoChange ? { input: 1, succeeded: 0, no_change: 1, conflict: 0, failed: 0 } : { input: 1, succeeded: 1, no_change: 0, conflict: 0, failed: 0 }, items: calls.operationNoChange ? [{ resource_type: "case", id: "22222222-2222-4222-8222-222222222222", outcome: "no_change", code: "already_applied", message: "未发生变化", new_rev: null, asset: { id: "22222222-2222-4222-8222-222222222222", resource_type: "case", name: "查询订单", rev: 2, state: "active", folder_id: "11111111-1111-4111-8111-111111111111", parent_id: null, archived_at: null } }] : [{ resource_type: "case", id: "22222222-2222-4222-8222-222222222222", outcome: "succeeded", code: null, message: "已归档", new_rev: 3, asset: { id: "22222222-2222-4222-8222-222222222222", resource_type: "case", name: "查询订单", rev: 3, state: "archived", folder_id: "11111111-1111-4111-8111-111111111111", parent_id: null, archived_at: "2026-10-04T00:00:01Z" } }], members: [] } });
      }
      if (path.includes("/favorite")) return parse({ case_id: "22222222-2222-4222-8222-222222222222", favorite: true, last_opened_at: null });
      if (path.endsWith("/case-views") && method === "POST") {
        if (calls.viewGate !== null) await calls.viewGate;
        if (calls.viewFailure !== null) throw calls.viewFailure;
        const request = body as { name: string; filters: Record<string, unknown> };
        return parse({
        id: "44444444-4444-4444-8444-444444444444",
        name: request.name,
        filters: { ...request.filters, q: request.filters.q ?? null, method: request.filters.method ?? null },
        rev: 1,
        created_at: "2026-10-04T01:02:03Z",
        updated_at: "2026-10-04T01:02:03Z",
      });
      }
      throw new Error(`未覆盖 ${method} ${path}`);
    }),
  };
});

beforeEach(() => {
  calls.get.length = 0;
  calls.send.length = 0;
  calls.viewGate = null;
  calls.viewFailure = null;
  calls.libraryFailure = null;
  calls.folderFailure = null;
  calls.emptyLibrary = false;
  calls.rootHasMore = false;
  calls.rootHasChildren = false;
  calls.childFailure = null;
  calls.folderName = "订单目录";
  calls.libraryFolderName = "订单目录";
  calls.operationRejected = false;
  calls.operationGate = null;
  calls.operationFailure = null;
  calls.operationNoChange = false;
  calls.sourceGate = null;
  calls.sourceUnreadableIds = [];
  calls.libraryTotal = 2;
});

describe("独立用例库", () => {
  it("筛选全部命中501条时明确要求缩小范围且不创建selection", async () => {
    calls.libraryTotal = 501;
    render(<AppProviders><LeaveGuardProvider><CaseLibrary workspaceId="w" projectId="p" principalId="u" canEdit active onOpen={async () => "opened"} /></LeaveGuardProvider></AppProviders>);
    await screen.findByText("查询订单");
    fireEvent.click(screen.getByText(/筛选全部命中/));
    fireEvent.click(screen.getByRole("button", { name: "批量归档" }));
    expect(await screen.findByText(/单次最多处理 500 条/)).toBeTruthy();
    expect(calls.send.filter((call) => call.path.endsWith("/asset-selections"))).toHaveLength(0);
  });
  it("批量明确区分当前页勾选与筛选全部命中selector", async () => {
    render(<AppProviders><LeaveGuardProvider><CaseLibrary workspaceId="w" projectId="p" principalId="u" canEdit active onOpen={async () => "opened"} /></LeaveGuardProvider></AppProviders>);
    await screen.findByText("查询订单");
    const checkboxes = screen.getAllByRole("checkbox");
    fireEvent.click(checkboxes[1]);
    fireEvent.click(screen.getByRole("button", { name: "批量归档" }));
    fireEvent.click(await screen.findByRole("button", { name: "生成预览" }));
    await waitFor(() => expect(calls.send.find((call) => call.path.endsWith("/asset-selections"))?.body).toMatchObject({ mode: "explicit", items: [{ id: "22222222-2222-4222-8222-222222222222", expected_rev: 2 }] }));
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    fireEvent.click(screen.getByText(/筛选全部命中/));
    fireEvent.click(screen.getByRole("button", { name: "批量归档" }));
    fireEvent.click(await screen.findByRole("button", { name: "生成预览" }));
    await waitFor(() => {
      const previews = calls.send.filter((call) => call.path.endsWith("/asset-selections"));
      expect(previews.at(-1)?.body).toMatchObject({ mode: "filter", filters: { schema_version: 1, state: "active", folder: "all", collection: "all" } });
      expect(previews.at(-1)?.body).not.toHaveProperty("items");
    });
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
  });
  it("处理阻断后明确新预览按ID读取最新rev并保留目标输入", async () => {
    render(<AppProviders><LeaveGuardProvider><CaseLibrary workspaceId="w" projectId="p" principalId="u" canEdit active onAcquireAssetOperation={() => ({ message: "修订已变化" })} onOpen={async () => "opened"} /></LeaveGuardProvider></AppProviders>);
    const row = (await screen.findByText("查询订单")).closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByText("移动"));
    fireEvent.click(await screen.findByRole("button", { name: "生成预览" }));
    expect(await screen.findByText(/将处理 1 项/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    expect(await screen.findByText("修订已变化")).toBeTruthy();
    let releaseSource!: () => void;
    calls.sourceGate = new Promise<void>((resolve) => { releaseSource = resolve; });
    fireEvent.click(screen.getByRole("button", { name: "修改参数并读取最新来源" }));
    await waitFor(() => expect(calls.send.some((call) => call.method === "GET" && call.path.endsWith("/cases/22222222-2222-4222-8222-222222222222"))).toBe(true));
    const confirmWhileReading = screen.getByText("确认执行").closest("button") as HTMLButtonElement;
    expect(confirmWhileReading.disabled).toBe(true);
    fireEvent.click(confirmWhileReading);
    expect(calls.send.filter((call) => call.path.endsWith("/asset-operations"))).toHaveLength(0);
    releaseSource();
    fireEvent.click(await screen.findByRole("button", { name: "生成预览" }));
    await waitFor(() => {
      const previews = calls.send.filter((call) => call.path.endsWith("/asset-selections"));
      expect(previews.at(-1)?.body).toMatchObject({ items: [{ id: "22222222-2222-4222-8222-222222222222", expected_rev: 3 }], parameters: { target_folder_id: null } });
    });
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
  });
  it("写请求首个await后目录刷新仍按同步阶段保留原unknown意图", async () => {
    let releaseOperation!: () => void;
    calls.operationGate = new Promise<void>((resolve) => { releaseOperation = resolve; });
    calls.operationFailure = new Error("结果暂时无法确认");
    const view = render(<AppProviders><LeaveGuardProvider><CaseLibrary workspaceId="w" projectId="p" principalId="u" canEdit folderRefreshToken={0} active onOpen={async () => "opened"} /></LeaveGuardProvider></AppProviders>);
    const row = (await screen.findByText("查询订单")).closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByText("归档"));
    fireEvent.click(await screen.findByRole("button", { name: "生成预览" }));
    await screen.findByText(/将处理 1 项/);
    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    view.rerender(<AppProviders><LeaveGuardProvider><CaseLibrary workspaceId="w" projectId="p" principalId="u" canEdit folderRefreshToken={1} active onOpen={async () => "opened"} /></LeaveGuardProvider></AppProviders>);
    expect(screen.getByText("归档用例")).toBeTruthy();
    releaseOperation();
    expect(await screen.findByRole("button", { name: "确认原操作" })).toBeTruthy();
    expect(calls.send.filter((call) => call.path.endsWith("/asset-operations"))).toHaveLength(1);
  });
  it("普通批量读取最新来源全部失败时以本次读取汇总替换旧预览", async () => {
    calls.sourceUnreadableIds = ["22222222-2222-4222-8222-222222222222", "33333333-3333-4333-8333-333333333333"];
    render(<AppProviders><LeaveGuardProvider><CaseLibrary workspaceId="w" projectId="p" principalId="u" canEdit active onOpen={async () => "opened"} /></LeaveGuardProvider></AppProviders>);
    await screen.findByText("查询订单");
    const checkboxes = screen.getAllByRole("checkbox");
    fireEvent.click(checkboxes[1]);
    fireEvent.click(checkboxes[2]);
    fireEvent.click(screen.getByRole("button", { name: "批量归档" }));
    fireEvent.click(await screen.findByRole("button", { name: "生成预览" }));
    await screen.findByText(/将处理 2 项/);
    fireEvent.click(screen.getByRole("button", { name: "修改参数并读取最新来源" }));
    expect(await screen.findByText("原对象 2 项，本次可读取 0 项，跳过 2 项")).toBeTruthy();
    expect(screen.getByText(/所选用例当前均不可读取/)).toBeTruthy();
    expect(calls.send.filter((call) => call.path.endsWith("/asset-operations"))).toHaveLength(0);
  });
  it("同目录移动的already_applied按无变化解释且不冒充修订冲突", async () => {
    calls.operationNoChange = true;
    render(<AppProviders><LeaveGuardProvider><CaseLibrary workspaceId="w" projectId="p" principalId="u" canEdit active onOpen={async () => "opened"} /></LeaveGuardProvider></AppProviders>);
    const row = (await screen.findByText("查询订单")).closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByText("移动"));
    const dialog = await screen.findByRole("dialog");
    const targetSelect = within(dialog).getAllByRole("combobox").at(-1) as HTMLElement;
    fireEvent.mouseDown(targetSelect);
    fireEvent.click(await screen.findByText("订单目录", { selector: ".ant-select-item-option-content" }));
    fireEvent.click(await screen.findByRole("button", { name: "生成预览" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认执行" }));
    expect(await screen.findByText("无变化：已在目标位置，无需再次处理")).toBeTruthy();
    expect(screen.queryByText(/状态已变化|修订已变化/)).toBeNull();
    expect(screen.getAllByText(/无变化 1/)).toHaveLength(2);
  });
  it("持久rejected保留Modal输入并允许建立新意图", async () => {
    calls.operationRejected = true;
    render(<AppProviders><LeaveGuardProvider><CaseLibrary workspaceId="w" projectId="p" principalId="u" canEdit active onOpen={async () => "opened"} /></LeaveGuardProvider></AppProviders>);
    const row = (await screen.findByText("查询订单")).closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByText("复制"));
    const dialog = await screen.findByRole("dialog");
    const name = await within(dialog).findByDisplayValue("查询订单 副本");
    fireEvent.change(name, { target: { value: "自定义副本" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认执行" }));
    expect(await within(dialog).findByText("副本名称冲突")).toBeTruthy();
    expect(within(dialog).getByDisplayValue("自定义副本")).toBeTruthy();
    expect(within(dialog).getByRole("button", { name: "修正并重试" })).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
  });
  it("复制完成只保留显式打开入口，不在后台抢焦点", async () => {
    const onOpen = vi.fn(async () => "opened" as const);
    render(<AppProviders><LeaveGuardProvider><CaseLibrary workspaceId="w" projectId="p" principalId="u" canEdit active onOpen={onOpen} /></LeaveGuardProvider></AppProviders>);
    const row = (await screen.findByText("查询订单")).closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByText("复制"));
    fireEvent.click(await screen.findByRole("button", { name: "确认执行" }));
    expect(await screen.findByText(/副本“查询订单 副本”已创建/)).toBeTruthy();
    expect(onOpen).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole("button", { name: "完成" }));
    fireEvent.click(await screen.findByRole("button", { name: "打开副本" }));
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith("77777777-7777-4777-8777-777777777777"));
  });
  it("运行纠错按caseId标记原对象，同名搜索不冒充精确目标", async () => {
    const focus = { token: 1, workspaceId: "w", projectId: "p", principalId: "u", caseId: "33333333-3333-4333-8333-333333333333", name: "查询订单", method: "POST", path: "/legacy", mode: "restore" as const };
    const { rerender } = render(<AppProviders><LeaveGuardProvider><CaseLibrary workspaceId="w" projectId="p" principalId="u" active assetFocus={focus} onOpen={async () => "opened"} /></LeaveGuardProvider></AppProviders>);
    const targetRow = (await screen.findByText("旧目录用例")).closest("tr");
    expect(targetRow).not.toBeNull();
    expect(within(targetRow as HTMLElement).getByText("待处理目标")).toBeTruthy();
    expect(screen.getByText(/目标：POST \/legacy/)).toBeTruthy();
    expect(screen.queryByText(/33333333-3333-4333-8333-333333333333/)).toBeNull();
    fireEvent.change(screen.getByLabelText("搜索用例名称或请求路径"), { target: { value: "后来筛选" } });
    rerender(<AppProviders><LeaveGuardProvider><CaseLibrary workspaceId="w" projectId="p" principalId="u" active={false} assetFocus={focus} onOpen={async () => "opened"} /></LeaveGuardProvider></AppProviders>);
    rerender(<AppProviders><LeaveGuardProvider><CaseLibrary workspaceId="w" projectId="p" principalId="u" active assetFocus={focus} onOpen={async () => "opened"} /></LeaveGuardProvider></AppProviders>);
    expect(screen.getByLabelText("搜索用例名称或请求路径")).toHaveProperty("value", "后来筛选");
  });
  it("Tree内目录动作聚焦后可用Space和Enter激活", async () => {
    const user = userEvent.setup();
    render(<AppProviders><LeaveGuardProvider><CaseLibrary workspaceId="w" projectId="p" principalId="u" canEdit active onOpen={async () => "opened"} /></LeaveGuardProvider></AppProviders>);
    const archive = await screen.findByRole("button", { name: "归档目录 订单目录" });
    archive.focus();
    await user.keyboard(" ");
    expect(await screen.findByText("归档目录树")).toBeTruthy();
    fireEvent.click(screen.getByText("取消"));
    archive.focus();
    await user.keyboard("{Enter}");
    expect(await screen.findByText("归档目录树")).toBeTruthy();
  });
  it("单用例归档必须先预览，再以selection确认同一动作", async () => {
    render(<AppProviders><LeaveGuardProvider><CaseLibrary workspaceId="w" projectId="p" principalId="u" canEdit active onOpen={async () => "opened"} /></LeaveGuardProvider></AppProviders>);
    const caseName = await screen.findByText("查询订单");
    const row = caseName.closest("tr");
    expect(row).not.toBeNull();
    fireEvent.click(within(row as HTMLElement).getByText("归档"));
    fireEvent.click(await screen.findByText("生成预览"));
    expect(await screen.findByText(/将处理 1 项/)).toBeTruthy();
    expect(calls.send.filter((call) => call.path.endsWith("/asset-operations"))).toHaveLength(0);
    fireEvent.click(screen.getByText("确认执行"));
    await waitFor(() => expect(calls.send.filter((call) => call.path.endsWith("/asset-operations"))).toHaveLength(1));
    expect(calls.send.find((call) => call.path.endsWith("/asset-operations"))?.body).toMatchObject({ action: "archive", selection_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", parameters: {} });
  });
  it("使用服务端查询、目录含后代筛选、个人收藏，并阻止归档对象打开", async () => {
    const onOpen = vi.fn(async () => "opened" as const);
    render(
      <AppProviders>
        <LeaveGuardProvider>
          <CaseLibrary
            workspaceId="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            projectId="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
            principalId="cccccccc-cccc-4ccc-8ccc-cccccccccccc"
            active
            onOpen={onOpen}
          />
        </LeaveGuardProvider>
      </AppProviders>,
    );

    const activeName = await screen.findByText("查询订单");
    const activeRow = activeName.closest("tr");
    if (activeRow === null) throw new Error("活动用例不在表格行内");
    const legacyRow = screen.getByText("旧目录用例").closest("tr");
    if (legacyRow === null) throw new Error("旧目录用例不在表格行内");
    expect(within(legacyRow).getByText("旧目录归档，待整理")).toBeTruthy();
    expect((within(legacyRow).getByRole("button", { name: "打开" }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(within(screen.getByLabelText("用例目录与个人集合")).getByText("订单目录"));
    await waitFor(() => {
      const path = [...calls.get].reverse().find((item) => item.includes("/case-library?"));
      expect(path).toBeDefined();
      const query = new URLSearchParams(path?.split("?")[1] ?? "");
      expect(Object.fromEntries(query)).toEqual(expect.objectContaining({
        folder: "exact",
        folder_id: "11111111-1111-4111-8111-111111111111",
        include_descendants: "true",
      }));
    });

    const tree = screen.getByLabelText("用例目录与个人集合");
    fireEvent.click(within(tree).getByText("最近打开"));
    await waitFor(() => {
      const path = [...calls.get].reverse().find((item) => item.includes("/case-library?"));
      const query = new URLSearchParams(path?.split("?")[1] ?? "");
      expect({ collection: query.get("collection"), sort: query.get("sort") }).toEqual({ collection: "recent", sort: "recent_desc" });
    });
    expect(screen.getByLabelText("排序").closest(".ant-select")?.textContent).toContain("最近打开时间");
    for (const label of ["全部用例", "未分组", "我的收藏", "订单目录"]) {
      fireEvent.click(within(tree).getByText(label));
      await waitFor(() => {
        const path = [...calls.get].reverse().find((item) => item.includes("/case-library?"));
        expect(new URLSearchParams(path?.split("?")[1] ?? "").get("sort")).not.toBe("recent_desc");
      });
    }

    const filteredRow = screen.getByText("查询订单").closest("tr");
    if (filteredRow === null) throw new Error("筛选后活动用例不在表格行内");
    fireEvent.click(within(filteredRow).getByRole("button", { name: "收藏 查询订单" }));
    await waitFor(() => expect(calls.send).toContainEqual(expect.objectContaining({
      method: "PUT",
      body: { favorite: true },
    })));

    const refreshedRow = screen.getByText("查询订单").closest("tr");
    if (refreshedRow === null) throw new Error("收藏后活动用例不在表格行内");
    fireEvent.click(within(refreshedRow).getByRole("button", { name: "打开" }));
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith("22222222-2222-4222-8222-222222222222"));
  });

  it("保存个人筛选视图时不携带游标、页大小或结果集", async () => {
    render(
      <AppProviders>
        <LeaveGuardProvider>
          <CaseLibrary
            workspaceId="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            projectId="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
            principalId="cccccccc-cccc-4ccc-8ccc-cccccccccccc"
            active
            onOpen={async () => "opened"}
          />
        </LeaveGuardProvider>
      </AppProviders>,
    );
    await screen.findByText("查询订单");
    const saveCurrent = screen.getByRole("button", { name: "保存当前视图" }) as HTMLButtonElement;
    expect(saveCurrent.disabled).toBe(false);
    fireEvent.click(saveCurrent);
    await waitFor(() => expect(saveCurrent.getAttribute("aria-expanded")).toBe("true"));
    const dialog = await screen.findByRole("dialog");
    expect(dialog.textContent).toContain("保存筛选视图");
    fireEvent.change(within(dialog).getByRole("textbox"), { target: { value: "我的活动用例" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存" }));

    await waitFor(() => expect(calls.send.some((call) => call.path.endsWith("/case-views") && call.method === "POST")).toBe(true));
    const request = calls.send.find((call) => call.path.endsWith("/case-views") && call.method === "POST");
    expect(request?.body).toEqual({
      name: "我的活动用例",
      filters: expect.objectContaining({ schema_version: 1, state: "active", collection: "all" }),
    });
    expect((request?.body as { filters: object }).filters).not.toHaveProperty("cursor");
    expect((request?.body as { filters: object }).filters).not.toHaveProperty("limit");
  });

  it("视图写入期间同步锁定输入并拒绝重复提交，隐藏页迟到完成不重开弹窗", async () => {
    let release!: () => void;
    calls.viewGate = new Promise<void>((resolve) => { release = resolve; });
    const props = { workspaceId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", projectId: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", principalId: "cccccccc-cccc-4ccc-8ccc-cccccccccccc" };
    const { rerender } = render(<AppProviders><LeaveGuardProvider><CaseLibrary {...props} active onOpen={async () => "opened"} /></LeaveGuardProvider></AppProviders>);
    fireEvent.click(screen.getByRole("button", { name: "保存当前视图" }));
    const dialog = await screen.findByRole("dialog");
    const input = within(dialog).getByRole("textbox") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "提交时名称" } });
    const save = within(dialog).getByRole("button", { name: "保存" });
    fireEvent.click(save);
    await waitFor(() => expect(calls.send.filter((call) => call.path.endsWith("/case-views"))).toHaveLength(1));
    expect(input.disabled).toBe(true);
    fireEvent.change(input, { target: { value: "等待期间输入" } });
    fireEvent.click(save);
    expect(input.disabled).toBe(true);
    expect(calls.send.filter((call) => call.path.endsWith("/case-views"))).toHaveLength(1);

    rerender(<AppProviders><LeaveGuardProvider><CaseLibrary {...props} active={false} onOpen={async () => "opened"} /></LeaveGuardProvider></AppProviders>);
    release();
    rerender(<AppProviders><LeaveGuardProvider><CaseLibrary {...props} active onOpen={async () => "opened"} /></LeaveGuardProvider></AppProviders>);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("保存失败在Modal内显示并保留名称", async () => {
    calls.viewFailure = new Error("保存视图失败");
    render(<AppProviders><LeaveGuardProvider><CaseLibrary workspaceId="w" projectId="p" principalId="u" active onOpen={async () => "opened"} /></LeaveGuardProvider></AppProviders>);
    await screen.findByText("查询订单");
    fireEvent.click(screen.getByRole("button", { name: "保存当前视图" }));
    const dialog = await screen.findByRole("dialog");
    const input = within(dialog).getByRole("textbox") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "失败仍保留" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存" }));
    expect(await within(dialog).findByText("保存视图失败")).toBeTruthy();
    expect(input.value).toBe("失败仍保留");
  });

  it("查询失败、目录失败与真正空结果使用不同反馈", async () => {
    calls.libraryFailure = new Error("用例查询失败");
    const props = { workspaceId: "w", projectId: "p", principalId: "u", active: true, onOpen: async () => "opened" as const };
    const first = render(<AppProviders><LeaveGuardProvider><CaseLibrary {...props} /></LeaveGuardProvider></AppProviders>);
    expect(await screen.findByText("用例查询失败")).toBeTruthy();
    expect(screen.getByText("总数尚未加载")).toBeTruthy();
    expect(screen.queryByText("没有符合当前条件的用例。")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "重试当前查询" }));
    await waitFor(() => expect(calls.get.filter((path) => path.includes("/case-library?")).length).toBeGreaterThan(1));
    first.unmount();

    calls.libraryFailure = null;
    calls.folderFailure = new Error("目录服务失败");
    render(<AppProviders><LeaveGuardProvider><CaseLibrary {...props} /></LeaveGuardProvider></AppProviders>);
    await screen.findByText("查询订单");
    expect(screen.getAllByText("目录服务失败").length).toBeGreaterThan(0);
    expect(screen.getByText("父目录 / 订单目录")).toBeTruthy();
  });

  it("成功空页才显示零条空状态", async () => {
    calls.emptyLibrary = true;
    render(<AppProviders><LeaveGuardProvider><CaseLibrary workspaceId="w" projectId="p" principalId="u" active onOpen={async () => "opened"} /></LeaveGuardProvider></AppProviders>);
    expect(await screen.findByText("没有符合当前条件的用例。")).toBeTruthy();
    expect(screen.getByText("共 0 条，第 1 页")).toBeTruthy();
  });

  it("真实Tree首屏不发送虚拟parent_id，目录下一页只在点击加载更多后读取", async () => {
    calls.rootHasMore = true;
    render(<AppProviders><LeaveGuardProvider><CaseLibrary workspaceId="w" projectId="p" principalId="u" folderRefreshToken={0} active onOpen={async () => "opened"} /></LeaveGuardProvider></AppProviders>);
    const tree = await screen.findByLabelText("用例目录与个人集合");
    await within(tree).findByText("订单目录");
    const rootCalls = () => calls.get.filter((path) => path.includes("parent_mode=root"));
    expect(rootCalls()).toHaveLength(1);
    expect(calls.get.some((path) => /parent_id=(folders|archived-folders|all|unfiled|favorites|recent)/.test(path))).toBe(false);
    for (const label of ["全部用例", "未分组", "我的收藏", "最近打开"]) fireEvent.click(within(tree).getByText(label));
    expect(calls.get.some((path) => path.includes("parent_mode=exact"))).toBe(false);
    fireEvent.click(within(tree).getByRole("button", { name: "加载更多" }));
    expect(await within(tree).findByText("第二页目录")).toBeTruthy();
    expect(rootCalls()).toHaveLength(2);
  });

  it.each([
    ["Space", " "],
    ["Enter", "{Enter}"],
  ])("Tree内加载更多聚焦后可用%s激活", async (_label, key) => {
    calls.rootHasMore = true;
    const user = userEvent.setup();
    render(<AppProviders><LeaveGuardProvider><CaseLibrary workspaceId="w" projectId="p" principalId="u" folderRefreshToken={0} active onOpen={async () => "opened"} /></LeaveGuardProvider></AppProviders>);
    const tree = await screen.findByLabelText("用例目录与个人集合");
    await within(tree).findByText("订单目录");
    const more = within(tree).getByRole("button", { name: "加载更多" });
    more.focus();
    expect(document.activeElement).toBe(more);
    await user.keyboard(key);
    expect(await within(tree).findByText("第二页目录")).toBeTruthy();
    expect(calls.get.filter((path) => path.includes("parent_mode=root"))).toHaveLength(2);
  });

  it("手动刷新同时重读目录与用例列表并更新folder_path", async () => {
    render(<AppProviders><LeaveGuardProvider><CaseLibrary workspaceId="w" projectId="p" principalId="u" folderRefreshToken={0} active onOpen={async () => "opened"} /></LeaveGuardProvider></AppProviders>);
    const tree = await screen.findByLabelText("用例目录与个人集合");
    await within(tree).findByText("订单目录");
    expect(await screen.findByText("父目录 / 订单目录")).toBeTruthy();
    const rootBefore = calls.get.filter((path) => path.includes("parent_mode=root")).length;
    const libraryBefore = calls.get.filter((path) => path.includes("/case-library?")).length;
    calls.folderName = "更名后的目录";
    calls.libraryFolderName = "更名后的目录";
    fireEvent.click(within(tree).getByRole("button", { name: "刷新目录" }));
    expect(await within(tree).findByText("更名后的目录")).toBeTruthy();
    expect(await screen.findByText("父目录 / 更名后的目录")).toBeTruthy();
    expect(calls.get.filter((path) => path.includes("parent_mode=root")).length).toBeGreaterThan(rootBefore);
    expect(calls.get.filter((path) => path.includes("/case-library?")).length).toBeGreaterThan(libraryBefore);
  });

  it("子层首屏失败后停留错误态，显式重试才发第二次请求", async () => {
    calls.rootHasChildren = true;
    calls.childFailure = new Error("子目录读取失败");
    render(<AppProviders><LeaveGuardProvider><CaseLibrary workspaceId="w" projectId="p" principalId="u" folderRefreshToken={0} active onOpen={async () => "opened"} /></LeaveGuardProvider></AppProviders>);
    const tree = await screen.findByLabelText("用例目录与个人集合");
    const title = await within(tree).findByText("订单目录");
    const node = title.closest<HTMLElement>('[role="treeitem"]');
    if (node === null) throw new Error("目录节点未挂载");
    const switcher = node.querySelector<HTMLElement>(".ant-tree-switcher");
    if (switcher === null) throw new Error("目录展开入口未挂载");
    fireEvent.click(switcher);
    expect(await screen.findByText("子目录读取失败")).toBeTruthy();
    const exactCalls = () => calls.get.filter((path) => path.includes("parent_mode=exact"));
    expect(exactCalls()).toHaveLength(1);
    fireEvent.click(screen.getByText("全部用例"));
    expect(exactCalls()).toHaveLength(1);
    calls.childFailure = null;
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await within(tree).findByText("子目录")).toBeTruthy();
    expect(exactCalls()).toHaveLength(2);
  });

  it("Table scroll.y跟随右侧真实剩余高度变化", async () => {
    let regionHeight = 315;
    const observers: Array<{ targets: Element[]; callback: ResizeObserverCallback }> = [];
    class MeasuredResizeObserver implements ResizeObserver {
      readonly targets: Element[] = [];
      constructor(readonly callback: ResizeObserverCallback) { observers.push(this); }
      observe(target: Element) { this.targets.push(target); }
      unobserve() {}
      disconnect() {}
    }
    vi.stubGlobal("ResizeObserver", MeasuredResizeObserver);
    const rect = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const height = this.classList.contains("case-library-table-region") ? regionHeight : this.classList.contains("ant-table-thead") ? 55 : 0;
      return { x: 0, y: 0, top: 0, left: 0, right: 1000, bottom: height, width: 1000, height, toJSON: () => ({}) } as DOMRect;
    });
    try {
      render(<AppProviders><LeaveGuardProvider><CaseLibrary workspaceId="w" projectId="p" principalId="u" active onOpen={async () => "opened"} /></LeaveGuardProvider></AppProviders>);
      await screen.findByText("查询订单");
      const body = document.querySelector<HTMLElement>(".ant-table-body");
      if (body === null) throw new Error("表体未挂载");
      await waitFor(() => expect(body.style.maxHeight).toBe("244px"));
      regionHeight = 270;
      const ownObserver = observers.find((observer) => observer.targets.some((target) => target.classList.contains("case-library-table-region")));
      if (ownObserver === undefined) throw new Error("表格区域ResizeObserver未挂载");
      ownObserver.callback([], ownObserver as unknown as ResizeObserver);
      await waitFor(() => expect(body.style.maxHeight).toBe("199px"));
    } finally {
      rect.mockRestore();
      vi.unstubAllGlobals();
    }
  });
});
