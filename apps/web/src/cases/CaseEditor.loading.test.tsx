/**
 * 已有内容到达之前的编辑窗口（V1／V2）。
 *
 * 这里覆盖的是“屏幕上这表单到底是谁的数据、什么时候才允许改”这两个窗口，用挂起的
 * 请求屏障精确停在窗口中间，不靠 sleep 也不靠“等测试就绪”把问题掩过去：
 *
 * 1. 已有用例的详情还没到达时，表单不开放编辑——先能打字、随后被回填覆盖，等于把
 *    用户的输入当垃圾丢掉，界面上看不出发生过什么。
 * 2. 切换到另一条用例时，新详情到达之前屏幕上不能留着上一条用例的内容。特别是两条
 *    用例修订号相同时：若把“同一版重复到达”的判断只建立在修订号上，新详情会被当成
 *    重复而丢掉，表单留着上一条的名称与断言，保存时却写到新那一条上——数据被贴上了
 *    别人的标签。
 * 3. 新建用例是另一种明确状态：它没有“已有内容”要等，空白草稿本身就是初始状态，
 *    从一开始就可编辑，也不该去读一条还不存在的用例。
 * 4. 新建保存成功后紧跟的重新拉取会带回刚写入的那一版：那次到达不能覆盖用户在这
 *    之间继续敲进去的内容，也不能让表单退回未就绪状态。
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const WORKSPACE_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const CASE_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const CASE_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const NEW_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const ENV_ID = "55555555-5555-4555-8555-555555555555";

vi.mock("../session/useSession", () => ({
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

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, apiGet: vi.fn(), apiSend: vi.fn(), apiSendWithMeta: vi.fn(), apiDelete: vi.fn() };
});

import { apiGet, apiSend, apiSendWithMeta } from "../api/client";
import { CaseEditor } from "./CaseEditor";

const apiGetMock = vi.mocked(apiGet);
const apiSendMock = vi.mocked(apiSend);

interface Call {
  method: string;
  path: string;
  body: unknown;
}

let calls: Call[] = [];
/** 挂起的详情请求：屏障由测试决定何时、带着哪一版内容返回。 */
let gates: Record<string, { promise: Promise<unknown>; resolve: (value: unknown) => void }> = {};
/** 未挂起时直接返回的详情，按用例 id 索引。 */
let immediate: Record<string, unknown> = {};
let createGate: { promise: Promise<unknown>; resolve: (value: unknown) => void } | null = null;

/** 挂起某条用例的详情请求，返回放行用的屏障。 */
function hold(caseId: string): (detail: unknown) => void {
  let release!: (value: unknown) => void;
  const promise = new Promise<unknown>((resolve) => {
    release = resolve;
  });
  gates[caseId] = { promise, resolve: release };
  return (detail: unknown) => release(detail);
}

function holdCreate(): (value: unknown) => void {
  let release!: (value: unknown) => void;
  const promise = new Promise<unknown>((resolve) => { release = resolve; });
  createGate = { promise, resolve: release };
  return release;
}

function detail(caseId: string, name: string, rev: number) {
  return {
    id: caseId,
    folder_id: null,
    name,
    request: { method: "GET", path: "/orders", query_params: [], headers: [], body_type: "none", body: "" },
    assertions: [],
    rev,
    status: "draft",
    latest_version: 0,
    updated_at: "2026-09-14T00:00:00Z",
    snapshot_hash: `hash-${caseId}-${rev}`,
  };
}

const TYPES = [
  {
    id: "status_in",
    label: "属于",
    group: "HTTP 状态",
    applies_to: ["integer", "number", "string"],
    params_schema: { values: { control: "value_list" as const, type: "number", label: "允许的状态码" } },
    summary: "属于这些值之一",
    operator_version: 1,
  },
];

/** 只实现本用例真正会走到的路由；其余请求直接失败，避免悄悄漏测。 */
function route(method: string, path: string, body: unknown): unknown {
  calls.push({ method, path, body });
  if (method === "GET" && path.endsWith("/assertion-types")) return TYPES;
  if (method === "GET" && path.endsWith("/versions")) return [];
  const caseDetailPath = /\/cases\/([0-9a-f-]+)$/.exec(path);
  if (method === "GET" && caseDetailPath) {
    const caseId = caseDetailPath[1];
    const gate = gates[caseId];
    if (gate) return gate.promise;
    const answer = immediate[caseId];
    if (answer) return answer;
    throw new Error(`测试未准备这条用例的详情：${caseId}`);
  }
  if (method === "POST" && path.endsWith("/cases")) {
    const created = detail(NEW_ID, (body as { name: string }).name, 1);
    if (createGate !== null) return createGate.promise;
    return { data: created, etag: '"1"' };
  }
  throw new Error(`测试未覆盖的请求：${method} ${path}`);
}

function renderEditor(caseSummaryId: string | null) {
  return render(
    <CaseEditor
      workspaceId={WORKSPACE_ID}
      projectId={PROJECT_ID}
      caseSummaryId={caseSummaryId}
      environments={[
        {
          id: ENV_ID,
          name: "测试环境",
          kind: "test",
          base_url: "http://echo.test",
          pool_id: null,
          variables: {},
          status: "active",
          rev: 1,
          config_version: 1,
        },
      ]}
      selectedEnvironmentId={ENV_ID}
      onSelectEnvironment={() => {}}
      onSaved={() => {}}
      onClose={() => {}}
    />,
  );
}

function nameInput(): HTMLInputElement {
  return screen.getByLabelText("用例名称") as HTMLInputElement;
}

function detailCalls(): Call[] {
  return calls.filter((call) => call.method === "GET" && /\/cases\/[0-9a-f-]+$/.test(call.path));
}

/** 载入完成的标志：名称与请求都来自服务端那一版。 */
async function waitForName(expected: string) {
  await waitFor(() => expect(nameInput().value).toBe(expected));
}

describe("已有内容到达之前的编辑窗口", () => {
  beforeEach(() => {
    calls = [];
    gates = {};
    immediate = {};
    createGate = null;
    apiGetMock.mockReset();
    apiSendMock.mockReset();
    vi.mocked(apiSendWithMeta).mockReset();
    apiGetMock.mockImplementation((async (path: string) => route("GET", path, undefined)) as never);
    apiSendMock.mockImplementation((async (path: string, method: string, body: unknown) =>
      route(method, path, body)) as never);
    vi.mocked(apiSendWithMeta).mockImplementation(
      (async (path: string, method: string, body: unknown) => route(method, path, body)) as never,
    );
  });

  it("已有用例的详情到达前不开放编辑，屏幕上也不留下上一条用例的内容", async () => {
    immediate[CASE_A] = detail(CASE_A, "用例 A", 4);
    const view = renderEditor(CASE_A);
    await waitForName("用例 A");

    // 切到另一条用例：它的详情被屏障挂住，停在“还没到达”的那一刻。
    const release = hold(CASE_B);
    view.rerender(
      <CaseEditor
        workspaceId={WORKSPACE_ID}
        projectId={PROJECT_ID}
        caseSummaryId={CASE_B}
        environments={[]}
        selectedEnvironmentId={ENV_ID}
        onSelectEnvironment={() => {}}
        onSaved={() => {}}
        onClose={() => {}}
      />,
    );

    // 请求确实发出去了，我们才真的停在这个窗口里。
    expect(detailCalls().some((call) => call.path.endsWith(CASE_B))).toBe(true);
    expect(screen.getByText("正在载入用例内容…")).toBeTruthy();
    // 关键：此时没有可编辑的名称输入，上一条用例的名称也不会留在屏幕上冒充新的。
    expect(screen.queryByLabelText("用例名称")).toBeNull();
    expect(screen.queryByDisplayValue("用例 A")).toBeNull();

    release(detail(CASE_B, "用例 B", 4));
    await waitForName("用例 B");
  });

  it("两条用例修订号相同时，新详情不会被当成“同一版重复到达”而丢掉", async () => {
    immediate[CASE_A] = detail(CASE_A, "用例 A", 4);
    immediate[CASE_B] = detail(CASE_B, "用例 B", 4);
    const view = renderEditor(CASE_A);
    await waitForName("用例 A");

    // 修订号相同不代表内容相同：这是两条用例各自的一版。
    view.rerender(
      <CaseEditor
        workspaceId={WORKSPACE_ID}
        projectId={PROJECT_ID}
        caseSummaryId={CASE_B}
        environments={[]}
        selectedEnvironmentId={ENV_ID}
        onSelectEnvironment={() => {}}
        onSaved={() => {}}
        onClose={() => {}}
      />,
    );

    await waitForName("用例 B");
    expect(screen.queryByDisplayValue("用例 A")).toBeNull();
  });

  /**
   * VQ5：详情到达与基线应用之间不能存在“已经能编辑、内容还没应用”的窗口。
   *
   * 只按“effectiveDetail 非空”开放编辑，表单会在详情到达的那一次提交里先把**还没回填**
   * 的内容画出来，再靠随后的副作用回填成服务端内容。这里用 MutationObserver 盯住每一次
   * 提交：它的回调是微任务，而 React 的被动副作用排在宏任务里，所以“表单第一次出现”的
   * 那一刻能完整看到——不是等页面稳定后再读最终值（那样两种实现都合格）。
   */
  it("详情到达后表单第一次出现时就已经是服务端那一版，不存在未回填的可编辑中间态", async () => {
    const release = hold(CASE_A);
    const view = renderEditor(CASE_A);

    const seen: string[] = [];
    const observer = new MutationObserver(() => {
      const input = view.container.querySelector<HTMLInputElement>("#case-name");
      if (input !== null) seen.push(input.value);
    });
    observer.observe(view.container, { childList: true, subtree: true });

    release(detail(CASE_A, "用例 A", 4));
    await waitForName("用例 A");
    await act(async () => {});
    observer.disconnect();

    // 观测确实生效：表单出现这件事被记到了，否则下面的断言等于没测。
    expect(seen.length).toBeGreaterThan(0);
    // 旧实现这里会先记下一个空名称（表单已出现、内容还没回填），修好后每次观测到的都是
    // 服务端内容。留成过滤而不是等值比较，失败时会直接把那个中间态的值打出来。
    expect(seen.filter((value) => value !== "用例 A")).toEqual([]);
  });

  it("新建草稿从一开始就可编辑，也不会去读一条还不存在的用例", async () => {
    renderEditor(null);

    // 没有“已有内容”要等：空白草稿本身就是初始状态。
    expect(screen.queryByText("正在载入用例内容…")).toBeNull();
    fireEvent.change(nameInput(), { target: { value: "新用例" } });
    expect(nameInput().value).toBe("新用例");
    expect(detailCalls()).toEqual([]);
  });

  it("新建保存后紧跟的重新拉取不覆盖随后输入，也不让表单退回未就绪状态", async () => {
    renderEditor(null);
    fireEvent.change(nameInput(), { target: { value: "新用例" } });

    // 创建返回之后，服务端会在新 id 上重新拉一次这条用例——这次拉取被挂住。
    const release = hold(NEW_ID);
    fireEvent.click(screen.getByRole("button", { name: "创建用例" }));
    await waitFor(() => expect(calls.some((call) => call.method === "POST")).toBe(true));
    await waitFor(() => expect(detailCalls().some((call) => call.path.endsWith(NEW_ID))).toBe(true));

    // 这一段时间里表单仍然可编辑，不闪回未就绪状态。
    expect(screen.queryByText("正在载入用例内容…")).toBeNull();
    fireEvent.change(nameInput(), { target: { value: "保存后继续输入的名称" } });

    // 回来后带的是刚写入的那一版（同一修订号）：那是重复到达，不是新内容。
    release(detail(NEW_ID, "新用例", 1));
    await waitFor(() =>
      expect(calls.filter((call) => call.path.endsWith(NEW_ID) && call.method === "GET")).toHaveLength(1),
    );
    expect(nameInput().value).toBe("保存后继续输入的名称");
  });

  it("保存按钮与 Ctrl+S 共用同步锁，首次 POST 只创建一次", async () => {
    renderEditor(null);
    fireEvent.change(nameInput(), { target: { value: "只创建一次" } });
    const release = holdCreate();

    fireEvent.click(screen.getByRole("button", { name: "创建用例" }));
    fireEvent.keyDown(window, { key: "s", ctrlKey: true });
    expect(calls.filter((call) => call.method === "POST" && call.path.endsWith("/cases"))).toHaveLength(1);

    release({ data: detail(NEW_ID, "只创建一次", 1), etag: '"1"' });
    await waitFor(() => expect(screen.getByText("用例已创建，继续编辑后仍可保存。")).toBeTruthy());
    expect(calls.filter((call) => call.method === "POST" && call.path.endsWith("/cases"))).toHaveLength(1);
  });
});
