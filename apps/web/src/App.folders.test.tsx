/**
 * 用例目录真正承载用例（FC1）。
 *
 * 一条用例的归属有四种去向，界面必须四种都走通：在某个目录里新建时落进该目录、
 * 改成另一个目录、改回未分组、以及**目录失效后老实保持原样**。缺少其中任何一条，
 * 用户看到的就是“屏幕上一个样、保存完另一个样”：在 A 目录里点新建，用例却进了未分组，
 * 而左侧列表正按 A 过滤——刚建出来的那一条当场看不见。
 *
 * 这里走的是真实外壳与真实组件（App → CaseBrowser → CaseEditor），请求打到一份
 * 按服务端语义实现的替身上：目录归属按 `folder_id` **字段是否出现**分三态处理，
 * 列表按目录过滤。前端这一侧因此验证的是“发出去的请求对不对、渲染出来的结果对不对”；
 * 服务端那三态语义本身由 `tests/integration/test_api_flow.py` 在真实数据库上验证。
 *
 * 不做的事：不替换 CaseEditor 或 CaseBrowser，不直接断言 React 状态，只从界面上
 * 操作、从界面上读结果。
 */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const WORKSPACE_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const FOLDER_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const FOLDER_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const NEW_CASE_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";

vi.mock("./session/useSession", () => ({
  useSession: () => ({
    session: {
      user: { id: "u-1", username: "tester", display_name: "测试员", is_admin: true },
      workspaces: [{ id: WORKSPACE_ID, name: "默认工作空间", role: "admin" }],
    },
    loading: false,
    error: null,
    expired: false,
    login: vi.fn(),
    logout: vi.fn(),
  }),
}));

vi.mock("./api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api/client")>();
  return {
    ...actual,
    apiGet: vi.fn(),
    apiSend: vi.fn(),
    apiSendWithMeta: vi.fn(),
    apiDelete: vi.fn(),
  };
});

// 执行面板自己会拉环境与运行状态，与本文件关注的目录归属无关。
vi.mock("./runs/RunPanel", () => ({
  RunPanel: () => <div data-testid="run-panel" />,
}));

import { apiDelete, apiGet, apiSend, apiSendWithMeta, projectPath } from "./api/client";
import { App } from "./App";

const apiGetMock = vi.mocked(apiGet);
const apiSendMock = vi.mocked(apiSend);
const apiSendWithMetaMock = vi.mocked(apiSendWithMeta);

interface FolderRow {
  id: string;
  parent_id: string | null;
  name: string;
  archived_at: string | null;
}

interface CaseRow {
  id: string;
  folder_id: string | null;
  name: string;
  request: Record<string, unknown>;
  rev: number;
  snapshot_hash: string;
}

/** 服务端替身里的两张表；每条用例之前重置。 */
let folders: FolderRow[] = [];
let cases: CaseRow[] = [];
/** 记录写请求，用于断言“发出去的到底是哪一份”。 */
const writes: { method: string; path: string; body: Record<string, unknown> }[] = [];

let nextCaseId = NEW_CASE_ID;

function emptyRequest(): Record<string, unknown> {
  return { method: "GET", path: "/orders", query_params: [], headers: [], body_type: "none", body: "" };
}

function caseDetail(row: CaseRow): unknown {
  return {
    id: row.id,
    folder_id: row.folder_id,
    name: row.name,
    request: row.request,
    assertions: [],
    rev: row.rev,
    status: "draft",
    latest_version: null,
    updated_at: "2026-09-14T00:00:00Z",
    snapshot_hash: row.snapshot_hash,
  };
}

function caseSummary(row: CaseRow): unknown {
  return {
    id: row.id,
    folder_id: row.folder_id,
    name: row.name,
    method: (row.request.method as string) ?? "GET",
    status: "draft",
    rev: row.rev,
    latest_version: null,
  };
}

/** 路径里的目录过滤参数：`/cases?folder_id=…` 与 `/cases` 是两种不同的列表。 */
function folderFilter(path: string): { filtered: boolean; folderId: string | null } {
  const index = path.indexOf("?folder_id=");
  if (index < 0) return { filtered: false, folderId: null };
  return { filtered: true, folderId: decodeURIComponent(path.slice(index + "?folder_id=".length)) };
}

function basePath(path: string): string {
  const index = path.indexOf("?");
  return index < 0 ? path : path.slice(0, index);
}

/**
 * 服务端替身。目录归属的语义与真实后端一致：
 *
 * - `folder_id` **字段缺席** → 不改目录；
 * - `folder_id: null` → 移到未分组；
 * - `folder_id: <id>` → 移到该目录，且该目录必须属于本项目且未归档。
 *
 * 三态里最容易写错的是第一条与第二条：两者序列化出来都是 `null`，只有“字段在不在”
 * 能把它们分开。替身按同样的规矩实现，前端漏发或多发字段都会在这里露出来。
 */
function route(path: string, method: string, body?: Record<string, unknown>): unknown {
  const clean = basePath(path);
  if (method === "GET" && clean.endsWith("/projects")) {
    return [
      {
        id: PROJECT_ID,
        workspace_id: WORKSPACE_ID,
        key: "alpha",
        name: "项目甲",
        status: "active",
        role: "admin",
        pool_id: null,
      },
    ];
  }
  if (method === "GET" && clean.endsWith("/environments")) return [];
  if (method === "GET" && clean.endsWith("/assertion-types")) return [];
  if (method === "GET" && clean.endsWith("/folders")) {
    return folders.filter((item) => item.archived_at === null);
  }
  if (method === "DELETE" && /\/folders\/[0-9a-f-]{36}$/.test(clean)) {
    const id = clean.slice(clean.lastIndexOf("/") + 1);
    const folder = folders.find((item) => item.id === id);
    if (folder === undefined) throw new Error(`目录不存在：${id}`);
    folder.archived_at = "2026-09-14T00:00:00Z";
    return undefined;
  }
  if (clean.endsWith("/cases") && method === "GET") {
    const { filtered, folderId } = folderFilter(path);
    const rows = filtered ? cases.filter((item) => item.folder_id === folderId) : cases;
    return rows.map(caseSummary);
  }
  if (clean.endsWith("/cases") && method === "POST") {
    const payload = body ?? {};
    const folderId = (payload.folder_id as string | null) ?? null;
    if (folderId !== null) {
      const folder = folders.find((item) => item.id === folderId && item.archived_at === null);
      if (folder === undefined) throw new Error("目录不存在");
    }
    const row: CaseRow = {
      id: nextCaseId,
      folder_id: folderId,
      name: String(payload.name ?? ""),
      request: (payload.request as Record<string, unknown>) ?? emptyRequest(),
      rev: 1,
      snapshot_hash: `hash-${nextCaseId}-1`,
    };
    cases = [...cases, row];
    return caseDetail(row);
  }
  if (method === "PATCH" && /\/cases\/[0-9a-f-]{36}$/.test(clean)) {
    const id = clean.slice(clean.lastIndexOf("/") + 1);
    const row = cases.find((item) => item.id === id);
    if (row === undefined) throw new Error(`用例不存在：${id}`);
    const payload = body ?? {};
    if (payload.name !== undefined) row.name = String(payload.name);
    if (payload.request !== undefined) row.request = payload.request as Record<string, unknown>;
    if ("folder_id" in payload) {
      const folderId = payload.folder_id as string | null;
      if (folderId !== null) {
        const folder = folders.find((item) => item.id === folderId && item.archived_at === null);
        if (folder === undefined) throw new Error("目录不存在");
      }
      row.folder_id = folderId;
    }
    row.rev += 1;
    row.snapshot_hash = `hash-${row.id}-${row.rev}`;
    return caseDetail(row);
  }
  if (method === "GET" && /\/cases\/[0-9a-f-]{36}$/.test(clean)) {
    const id = clean.slice(clean.lastIndexOf("/") + 1);
    const row = cases.find((item) => item.id === id);
    if (row === undefined) throw new Error(`用例不存在：${id}`);
    return caseDetail(row);
  }
  if (method === "GET" && clean.endsWith("/versions")) return [];
  throw new Error(`测试未覆盖的请求：${method} ${path}`);
}

beforeEach(() => {
  apiGetMock.mockReset();
  apiSendMock.mockReset();
  apiSendWithMetaMock.mockReset();
  vi.mocked(apiDelete).mockReset();
  folders = [
    { id: FOLDER_A, parent_id: null, name: "A 模块", archived_at: null },
    { id: FOLDER_B, parent_id: null, name: "B 模块", archived_at: null },
  ];
  cases = [];
  writes.length = 0;
  nextCaseId = NEW_CASE_ID;
  window.confirm = vi.fn(() => true);

  apiGetMock.mockImplementation((async (path: string) => route(path, "GET")) as never);
  apiDeleteMock();
});

/** 让 `apiDelete` 也走同一份替身；单独包一层是为了在 mockReset 之后重新装回。 */
function apiDeleteMock() {
  vi.mocked(apiDelete).mockImplementation((async (path: string) => route(path, "DELETE")) as never);
}

function installSendMocks() {
  apiSendMock.mockImplementation((async (
    path: string,
    method: string,
    body: Record<string, unknown>,
  ) => {
    if (method !== "GET") writes.push({ method, path, body });
    return route(path, method, body);
  }) as never);
  apiSendWithMetaMock.mockImplementation((async (
    path: string,
    method: string,
    body: Record<string, unknown>,
  ) => {
    writes.push({ method, path, body });
    return { data: route(path, method, body), etag: '"1"' };
  }) as never);
}

async function renderShell(): Promise<HTMLElement> {
  render(<App />);
  const projectSelect = (await screen.findByLabelText("项目")) as HTMLSelectElement;
  await waitFor(() => expect(projectSelect.value).toBe(PROJECT_ID));
  // 选中项目后还有一次“重置下游选择”的副作用，等它落定再操作控件。
  await act(async () => {});
  return screen.getByLabelText("用例目录");
}

function browserList(browser: HTMLElement): string[] {
  return Array.from(browser.querySelectorAll(".case-name")).map((node) => node.textContent ?? "");
}

function pickFolder(browser: HTMLElement, name: string) {
  fireEvent.click(within(browser).getByRole("button", { name }));
}

function folderSelect(): HTMLSelectElement {
  const active = document.querySelector<HTMLElement>(".workspace-editor:not([hidden])");
  if (active === null) throw new Error("没有活动请求标签");
  return within(active).getByLabelText("所属目录") as HTMLSelectElement;
}

/** 选择器上当前显示的那一项。用来判定失效目录是如实显示成失效，还是被伪装成未分组。 */
function selectedFolderLabel(): string {
  const select = folderSelect();
  return Array.from(select.options).find((option) => option.value === select.value)?.textContent ?? "";
}

describe("用例目录承载用例的闭环", () => {
  beforeEach(() => {
    installSendMocks();
  });

  it("在 A 目录里新建：用例落进 A，A 的过滤列表立刻能看到它", async () => {
    const browser = await renderShell();
    pickFolder(browser, "A 模块");
    await act(async () => {});

    fireEvent.click(within(browser).getByRole("button", { name: "＋新建用例" }));
    fireEvent.change(await screen.findByLabelText("用例名称"), { target: { value: "A 模块下的用例" } });
    // 新建时编辑器已经继承当前选中的目录，不需要用户再选一次。
    expect(folderSelect().value).toBe(FOLDER_A);

    fireEvent.click(screen.getByRole("button", { name: "创建用例" }));

    await waitFor(() =>
      expect(writes.filter((item) => item.method === "POST")).toHaveLength(1),
    );
    // 判别性断言：恒定发送 `folder_id: null` 时这里会变成 null，用例进未分组，
    // 而左侧列表正按 A 过滤——下面那条“A 里能看到它”就会失败。
    expect(writes[0].body.folder_id).toBe(FOLDER_A);
    await waitFor(() => expect(browserList(browser)).toEqual(["A 模块下的用例"]));
  });

  it("改成 B 目录后两个过滤列表同时换边，未分组也能改回去", async () => {
    cases = [
      {
        id: NEW_CASE_ID,
        folder_id: FOLDER_A,
        name: "已归入 A 的用例",
        request: emptyRequest(),
        rev: 1,
        snapshot_hash: "hash-1",
      },
    ];
    const browser = await renderShell();
    pickFolder(browser, "A 模块");
    fireEvent.click(await within(browser).findByRole("button", { name: /已归入 A 的用例/ }));

    await waitFor(() => expect(folderSelect().value).toBe(FOLDER_A));
    fireEvent.change(folderSelect(), { target: { value: FOLDER_B } });
    // 只改目录也算未保存修改：不把目录纳入脏状态的话，这次改动不会被提交。
    expect(await screen.findByText("有未保存修改")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(writes.filter((item) => item.method === "PATCH")).toHaveLength(1));
    await waitFor(() => expect(screen.queryByText("有未保存修改")).toBeNull());
    await waitFor(() => expect(screen.queryByLabelText("操作进行中")).toBeNull());
    expect(writes[0].body.folder_id).toBe(FOLDER_B);
    // A 的过滤列表里已经没有它了。
    await waitFor(() => expect(browserList(browser)).toEqual([]));

    pickFolder(browser, "B 模块");
    await waitFor(() => expect(browserList(browser)).toEqual(["已归入 A 的用例"]));

    // 再改成未分组：这一步走的是「显式 null」，与「不改目录」只能靠字段是否出现区分。
    fireEvent.click(within(browser).getByRole("button", { name: /已归入 A 的用例/ }));
    await waitFor(() => expect(folderSelect().value).toBe(FOLDER_B));
    fireEvent.change(folderSelect(), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));

    await waitFor(() => expect(writes.filter((item) => item.method === "PATCH")).toHaveLength(2));
    expect("folder_id" in writes[1].body).toBe(true);
    expect(writes[1].body.folder_id).toBeNull();
    // 两个目录的过滤列表里都不再有它——它现在确实不在任何目录里。
    await waitFor(() => expect(browserList(browser)).toEqual([]));
    pickFolder(browser, "全部用例");
    await waitFor(() => expect(browserList(browser)).toEqual(["已归入 A 的用例"]));
  });

  it("只改目录不碰别的字段，也算未保存修改：开新标签后原标签仍保留", async () => {
    cases = [
      {
        id: NEW_CASE_ID,
        folder_id: FOLDER_A,
        name: "待分组的用例",
        request: emptyRequest(),
        rev: 1,
        snapshot_hash: "hash-1",
      },
    ];
    const browser = await renderShell();
    pickFolder(browser, "A 模块");
    fireEvent.click(await within(browser).findByRole("button", { name: /待分组的用例/ }));
    await waitFor(() => expect(folderSelect().value).toBe(FOLDER_A));

    fireEvent.change(folderSelect(), { target: { value: FOLDER_B } });
    // 再点一次“新建”只新增标签，不卸载原编辑器。
    fireEvent.click(within(browser).getByRole("button", { name: "＋新建用例" }));

    expect(window.confirm).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("tab", { name: /待分组的用例/ }));
    expect(folderSelect().value).toBe(FOLDER_B);
    expect(screen.getByText("有未保存修改")).toBeTruthy();
  });

  it("重新打开时归属与请求内容一起回来，不只回来名称", async () => {
    const browser = await renderShell();
    pickFolder(browser, "A 模块");
    fireEvent.click(within(browser).getByRole("button", { name: "＋新建用例" }));
    fireEvent.change(await screen.findByLabelText("用例名称"), { target: { value: "闭环用例" } });
    // 改一处请求内容，重新打开时要连它一起核对：只保住目录而丢了请求，等于用例被换了内容。
    fireEvent.change(screen.getByLabelText("路径"), { target: { value: "/orders/42" } });
    fireEvent.click(screen.getByRole("button", { name: "创建用例" }));
    await waitFor(() => expect(browserList(browser)).toEqual(["闭环用例"]));

    // 改名并保存，然后关闭、重新打开。
    fireEvent.change(screen.getByLabelText("用例名称"), { target: { value: "闭环用例（改名）" } });
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(writes.filter((item) => item.method === "PATCH")).toHaveLength(1));
    // 归属没动过，保存请求里就不该出现 folder_id：字段缺席＝不改目录。
    expect("folder_id" in writes.filter((item) => item.method === "PATCH")[0]!.body).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    await waitFor(() => expect(screen.queryByLabelText("用例名称")).toBeNull());
    fireEvent.click(await within(browser).findByRole("button", { name: /闭环用例（改名）/ }));

    await waitFor(() => expect(folderSelect().value).toBe(FOLDER_A));
    expect((screen.getByLabelText("用例名称") as HTMLInputElement).value).toBe("闭环用例（改名）");
    expect((screen.getByLabelText("路径") as HTMLInputElement).value).toBe("/orders/42");
  });

  it("目录被归档：明确显示失效，且只改名称的保存不会把它悄悄移出原目录", async () => {
    cases = [
      {
        id: NEW_CASE_ID,
        folder_id: FOLDER_A,
        name: "归档目录里的用例",
        request: emptyRequest(),
        rev: 1,
        snapshot_hash: "hash-1",
      },
    ];
    const browser = await renderShell();
    pickFolder(browser, "A 模块");
    fireEvent.click(await within(browser).findByRole("button", { name: /归档目录里的用例/ }));
    await waitFor(() => expect(folderSelect().value).toBe(FOLDER_A));

    // 在目录树里归档 A：编辑器拿到的是同一份目录清单，必须立刻反映出来。
    fireEvent.click(within(browser).getAllByRole("button", { name: "归档" })[0]!);

    // A 不见与 B 还在必须**同时**成立，所以放在同一个等待条件里。
    //
    // 拆成“先等 A 不见、再同步断言 B 还在”会引入一个瞬时态：目录清单重取还没回来时，
    // 选择器里只剩失效占位，「A 模块」确实已经不见了，但「B 模块」也还没到——第一段
    // 因此通过，第二段随负载随机失败。这里的结论本来就是一个收敛条件（清单刷新过），
    // 不是某一帧的快照。
    await waitFor(() => {
      expect(within(folderSelect()).queryByRole("option", { name: "A 模块" })).toBeNull();
      expect(within(folderSelect()).getByRole("option", { name: "B 模块" })).toBeTruthy();
    });
    // 关键：失效状态必须如实显示，不能被显示成「未分组」——那等于在界面上宣布一个
    // 用户没做过的改动，用户一保存就真的被移出原目录。
    expect(selectedFolderLabel()).toContain("已失效");
    expect(screen.getByText(/不动目录直接保存不会改变它的归属/)).toBeTruthy();

    // 用户只改了名称：请求里不该出现 folder_id，归属必须原样保留。
    fireEvent.change(screen.getByLabelText("用例名称"), { target: { value: "归档后改名" } });
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() =>
      expect(writes.filter((item) => item.method === "PATCH")).toHaveLength(1),
    );
    expect("folder_id" in writes.filter((item) => item.method === "PATCH")[0]!.body).toBe(false);
    // 服务端替身按同样的三态语义处理，所以这里同时证明用例仍挂在原目录上。
    expect(cases[0]?.folder_id).toBe(FOLDER_A);
    // 再打开一次仍是失效目录，而不是未分组。
    expect(selectedFolderLabel()).toContain("已失效");
  });

  it("所属目录按名称选择，选项只含当前项目的未归档目录，没有填 UUID 的入口", async () => {
    const browser = await renderShell();
    pickFolder(browser, "A 模块");
    fireEvent.click(within(browser).getByRole("button", { name: "＋新建用例" }));
    await screen.findByLabelText("用例名称");

    // 选项就是目录名称本身；多出来的空值那一条是“未分组”这个明确状态。
    expect(Array.from(folderSelect().options).map((option) => option.textContent)).toEqual([
      "未分组",
      "A 模块",
      "B 模块",
    ]);
    // 没有让用户手写目录 id 的输入框：跨项目目录因此没有入口，
    // 服务端还会再拒一次（见 tests/integration/test_api_flow.py）。
    expect(document.querySelector('input[id="case-folder"]')).toBeNull();
  });
});
