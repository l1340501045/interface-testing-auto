/**
 * 发送生命周期与报告来源（R2 F1–F10）。
 *
 * 这些用例全部用 deferred 固定竞态窗口——不是等页面稳定后再断言最终值，那样两种实现
 * 都会通过。每一条都对应一个已复现的缺陷，修复前必须失败。
 *
 * 覆盖：双击只建一条链路、预检返回即用（不读旧闭包）、授权需明确确认、受理不明保留
 * 原键与可达入口、编辑立即失效、迟到预检不覆盖、报告按运行选择与世代隔离、卸载后
 * 不再提交、首次保存保留历史、已知 id 即可取消、停止等待真实生效。
 */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const WORKSPACE_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const CASE_ID = "44444444-4444-4444-8444-444444444444";
const ENV_ID = "55555555-5555-4555-8555-555555555555";
const ENV_ALT_ID = "55555555-5555-4555-8555-555555555556";
const RUN_1 = "77777777-7777-4777-8777-777777777771";
const RUN_2 = "77777777-7777-4777-8777-777777777772";
const PROFILE_ID = "88888888-8888-4888-8888-888888888888";
const USER_ID = "99999999-9999-4999-8999-999999999999";

vi.mock("../session/useSession", () => ({
  useSession: () => ({
    session: {
      user: { user_id: USER_ID, username: "tester", display_name: "测试员", is_admin: true },
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

import { NetworkError, apiGet, apiSend, apiSendWithMeta, projectPath } from "../api/client";
const panelHarness = vi.hoisted(() => ({
  report: null as import("../api/types").RunReport | null,
}));

vi.mock("../runs/RunPanel", async () => {
  const { useEffect } = await import("react");
  return {
    RunPanel: ({
      onReport,
      onSelectRun,
    }: {
      onReport: (report: import("../api/types").RunReport) => void;
      onSelectRun: (runId: string) => void;
    }) => {
      const report = panelHarness.report;
      // 依赖数组与真实 RunPanel 一致（`[report.data, onReport]`）：报告身份变化或回调变化时
      // 才上报。没有依赖数组等于每次渲染都上报，那会与被测组件的更新形成闭环（上报 → 新选择
      // → 重渲染 → 上报），替身自己制造出真实组件不会有的死循环。
      useEffect(() => {
        if (report) onReport(report);
      }, [report, onReport]);
      // 选择是**显式事件**：真实 RunPanel 的「查看报告」按钮就是调 onSelectRun。
      // 替身只提供一个同名的入口，让用例能像用户那样点它，而不是绕过事件直接改状态。
      return (
        <div data-testid="run-panel">
          <button type="button" onClick={() => report && onSelectRun(report.run.id)}>
            查看报告
          </button>
        </div>
      );
    },
  };
});

import { LeaveGuardProvider } from "../hooks/leaveGuard";
import { CaseEditor } from "./CaseEditor";

const apiGetMock = vi.mocked(apiGet);
const apiSendMock = vi.mocked(apiSend);
const apiSendWithMetaMock = vi.mocked(apiSendWithMeta);

interface Call {
  method: string;
  path: string;
  body: unknown;
  headers?: Record<string, string> | undefined;
}

let calls: Call[] = [];

/**
 * 可挂起的响应。
 *
 * 竞态测试的关键是**在请求已发出、响应未返回**的窗口里制造第二次用户操作；等页面
 * 稳定后再断言只能验证最终值，两种实现都会通过。
 */
const gates = new Map<string, Array<{ resolve: (value: unknown) => void; reject: (cause: unknown) => void }>>();

function hold(name: string): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const bucket = gates.get(name) ?? [];
    bucket.push({ resolve, reject });
    gates.set(name, bucket);
  });
}

function release(name: string, value: unknown): void {
  const bucket = gates.get(name) ?? [];
  gates.set(name, []);
  // 放行之后取消这个挂起开关：用例放行的语义是“从这里开始它正常返回”。留着开关会让
  // 之后每一次同类请求（例如报告轮询）都被永久挂住，表现为“界面莫名其妙变空”，
  // 那是替身制造的假象，不是被测代码的行为。
  gated.delete(name);
  for (const item of bucket) item.resolve(value);
}

/**
 * 只放行同名的**第 index 个**挂起请求（从 0 数）。
 *
 * 迟到用例必须能按顺序放行：先让后发的那一个返回，再让先发的返回，才能构造出“旧响应
 * 迟到”的窗口。一次性全部放行等于没有竞态，测不出任何东西。
 */
function releaseNth(name: string, index: number, value: unknown): void {
  const bucket = gates.get(name) ?? [];
  const item = bucket[index];
  if (item === undefined) throw new Error(`没有第 ${index} 个挂起的 ${name}`);
  bucket.splice(index, 1);
  if (bucket.length === 0) gated.delete(name);
  item.resolve(value);
}

/** 已有多少个该名字的请求在挂起。 */
function heldCount(name: string): number {
  return (gates.get(name) ?? []).length;
}

function releaseError(name: string, cause: unknown): void {
  const bucket = gates.get(name) ?? [];
  gates.set(name, []);
  for (const item of bucket) item.reject(cause);
}

/** 让某条运行的报告读取持续失败（用于“读不到报告也要能取消”这类场景）。 */
const failingReports = new Set<string>();

/** 哪些端点需要挂起（由用例逐个开启）。 */
let gated = new Set<string>();

function maybeGate(name: string, value: unknown): unknown {
  return gated.has(name) ? hold(name) : value;
}

let preflightBody: unknown;
let runCount = 0;

const OK_PREFLIGHT = {
  ready: true,
  issues: [],
  can_authorize: true,
  auth: { required: false, state: "none", profile_id: null },
  context: {
    snapshot_fingerprint: "fp-current",
    environment: { id: ENV_ID, name: "测试环境", kind: "test", base_url: "http://echo.test" },
    input_fingerprint: "in-current",
  },
};

const AMBIGUOUS_PREFLIGHT = {
  ready: false,
  issues: [
    { code: "credential_ambiguous", message: "该环境配置了 2 份可用身份，无法确定使用哪一份。", action: "contact_admin" },
  ],
  can_authorize: true,
  auth: { required: false, state: "ambiguous", profile_id: null },
  context: null,
};

const NEEDS_AUTH_PREFLIGHT = {
  ready: false,
  issues: [{ code: "credential_not_granted", message: "当前身份未获授权。", action: "authorize" }],
  can_authorize: true,
  auth: { required: false, state: "needs_authorization", profile_id: PROFILE_ID },
  context: null,
};

function runSummary(id: string, environmentId = ENV_ID) {
  return {
    id,
    target_type: "debug_snapshot",
    case_version_id: null,
    environment_id: environmentId,
    state: "queued",
    outcome: null,
    reason_category: null,
    pool_id: null,
    created_at: "2026-09-15T00:00:00Z",
  };
}

function debugReport(runId: string, fingerprint: string, status = 200) {
  return {
    run: { ...runSummary(runId), state: "finished", outcome: "passed" },
    steps: [],
    assertions: [],
    request: null,
    response: {
      status,
      elapsed_ms: 7,
      headers: [],
      body: '{"ok":true}',
      body_format: "json",
      body_truncated: false,
      body_omitted_reason: null,
      size_bytes: 11,
    },
    context: {
      snapshot_fingerprint: fingerprint,
      environment: { id: ENV_ID, name: "测试环境", kind: "test", base_url: "http://echo.test" },
      input_fingerprint: "in-current",
    },
  };
}

const CASE_DETAIL = {
  id: CASE_ID,
  folder_id: null,
  name: "查询订单",
  request: {
    method: "GET",
    path: "/echo",
    query_params: [],
    headers: [],
    body_type: "none" as const,
    body: "",
  },
  assertions: [],
  rev: 3,
  status: "draft",
  latest_version: null,
  updated_at: "2026-09-15T00:00:00Z",
  snapshot_hash: "hash-draft",
};

function route(method: string, path: string, body: unknown, headers?: Record<string, string>): unknown {
  calls.push({ method, path, body, headers });
  if (method === "GET" && path.endsWith("/assertion-types")) return [];
  if (method === "GET" && path.endsWith(`/cases/${CASE_ID}/versions`)) return [];
  if (method === "GET" && path.endsWith(`/cases/${CASE_ID}`)) return CASE_DETAIL;
  if (method === "GET" && path.endsWith("/report")) {
    const runId = path.split("/runs/")[1]?.split("/")[0] ?? "";
    // 持续失败：报告读取失败的场景必须**一直**失败。
    // 一次性拒绝挡不住轮询——下一次读取会成功，运行随之变成终态，界面也就合理地不再
    // 提供取消入口。那不是被测行为出错，而是替身只演了一半（用例因此随负载时好时坏）。
    if (failingReports.has(runId)) throw new NetworkError("报告不可用");
    return maybeGate(`report:${runId}`, debugReport(runId, "fp-current"));
  }
  if (method === "GET" && path.endsWith("/runs")) return [];
  if (method === "POST" && path.endsWith("/debug-preflight")) {
    return maybeGate("preflight", preflightBody);
  }
  if (method === "POST" && path.endsWith("/debug-snapshot-digest")) return "digest-1";
  if (method === "POST" && path.endsWith("/credentials/grants")) {
    return maybeGate("grant", { id: "grant-1" });
  }
  if (method === "POST" && path.endsWith("/cancel")) return { id: "canceled" };
  if (method === "POST" && path.endsWith("/runs")) {
    runCount += 1;
    return maybeGate("runs", runSummary(runCount === 1 ? RUN_1 : RUN_2));
  }
  if (method === "POST" && path.endsWith("/cases")) {
    return maybeGate("create", { ...CASE_DETAIL });
  }
  if (method === "PATCH" && path.endsWith(`/cases/${CASE_ID}`)) return CASE_DETAIL;
  throw new Error(`测试未覆盖的请求：${method} ${path}`);
}

interface EditorProps {
  caseSummaryId?: string | null;
  configEpoch?: number;
}

function editorElement(props: EditorProps = {}) {
  return (
    <LeaveGuardProvider>
      <CaseEditor
        workspaceId={WORKSPACE_ID}
        projectId={PROJECT_ID}
        caseSummaryId={props.caseSummaryId === undefined ? CASE_ID : props.caseSummaryId}
        environments={[
          {
            id: ENV_ID,
            name: "测试环境",
            kind: "test",
            base_url: "http://echo.test",
            pool_id: null,
            variables: {},
            status: "active",
          },
          {
            id: ENV_ALT_ID,
            name: "备用环境",
            kind: "test",
            base_url: "http://echo-alt.test",
            pool_id: null,
            variables: {},
            status: "active",
          },
        ]}
        selectedEnvironmentId={ENV_ID}
        onSelectEnvironment={() => {}}
        onSaved={() => {}}
        onClose={() => {}}
        folders={[]}
        currentUserId={USER_ID}
        configEpoch={props.configEpoch ?? 0}
      />
    </LeaveGuardProvider>
  );
}

function renderEditor(props: EditorProps = {}) {
  return render(editorElement(props));
}

async function renderLoaded(props: { caseSummaryId?: string | null; configEpoch?: number } = {}) {
  const view = renderEditor(props);
  await screen.findByLabelText("用例名称");
  await act(async () => {});
  return view;
}

function callsTo(path: string, method: string): Call[] {
  return calls.filter((call) => call.method === method && call.path === path);
}

function preflights(): Call[] {
  return callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/debug-preflight"), "POST");
}

function runPosts(): Call[] {
  return callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/runs"), "POST");
}

function grants(): Call[] {
  return callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/credentials/grants"), "POST");
}

/**
 * 本次调试记录里的运行按钮。
 *
 * 在**容器内**按类名定位，而不是全局按文本：运行编号只有 8 位、又都是数字，全局文本匹配
 * 容易撞上页面别处的数字（时间、长度等），定位会随无关渲染变化而失稳。
 */
function recordButtons(view: { container: HTMLElement }): HTMLElement[] {
  return Array.from(view.container.querySelectorAll<HTMLElement>(".record-list button"));
}

function sendButton(): HTMLElement {
  return screen.getByRole("button", { name: "发送" });
}

beforeEach(() => {
  calls = [];
  gates.clear();
  gated = new Set();
  failingReports.clear();
  runCount = 0;
  preflightBody = OK_PREFLIGHT;
  panelHarness.report = null;
  apiGetMock.mockReset();
  apiSendMock.mockReset();
  apiSendWithMetaMock.mockReset();
  apiGetMock.mockImplementation((async (path: string) => route("GET", path, undefined)) as never);
  apiSendMock.mockImplementation((async (
    path: string,
    method: string,
    body: unknown,
    _parse: unknown,
    options?: { headers?: Record<string, string> },
  ) => route(method, path, body, options?.headers)) as never);
  // `route` 可能返回被挂起的 Promise：必须 await，否则 `data` 是 Promise 本身，
  // `created.data.id` 会变成 undefined——替身自己制造的缺陷，与被测代码无关。
  apiSendWithMetaMock.mockImplementation((async (path: string, method: string, body: unknown) => ({
    data: await route(method, path, body),
    etag: `"3"`,
  })) as never);
});

describe("F1 双击只建一条链路", () => {
  it("点击入口立即锁住整条链：延迟返回时仍只有一次预检与一次受理", async () => {
    await renderLoaded();
    // 先让**首次展示性预检**落定（编辑后 400ms 防抖触发的那一次）。
    //
    // 它只是刷新按钮旁的提示，不是一次发送；不等它落定就开始点击，会把它和点击自身那次
    // 预检一起卷进竞态窗口——负载高时它落在点击之前还是之后并不确定，用例因此时好时坏。
    // 把它固定下来，窗口里就只剩“点击”这一个变量，测的才是锁本身。
    await waitFor(() => expect(preflights()).toHaveLength(1));

    // 从这里开始挂起预检：点击发出的那一次会一直悬着。
    gated.add("preflight");

    // 两次点击落在同一次渲染里：state 还没更新，只有同步 ref 锁能挡住第二次。
    fireEvent.click(sendButton());
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await act(async () => {
      release("preflight", OK_PREFLIGHT);
    });
    await act(async () => {});

    await waitFor(() => expect(runPosts()).toHaveLength(1));
    // 关键断言：两次点击只产生**一次受理与一个幂等键**。第二条链会各自生成新键并把请求
    // 发出去，因此长度与键的唯一性同时成立才说明锁在点击那一帧生效了。
    //
    // 这里不数预检次数：展示性预检（编辑防抖触发的那些）不是链路的一部分，它的次数会随
    // 负载在 2／3 之间摆动，用它当判据只会得到一条间歇失败的用例，而不是更严格的检查。
    expect(runPosts()[0].headers?.["Idempotency-Key"]).toBeTruthy();
    expect(new Set(runPosts().map((c) => c.headers?.["Idempotency-Key"])).size).toBe(1);
  });

  it("授权链同样只建一条：授权确认期间再次点击不新增链路", async () => {
    preflightBody = NEEDS_AUTH_PREFLIGHT;
    await renderLoaded();
    fireEvent.click(sendButton());
    await screen.findByRole("button", { name: "授权并发送" });

    fireEvent.click(sendButton());
    expect(grants()).toHaveLength(0);
    expect(runPosts()).toHaveLength(0);
  });
});

describe("F4 预检返回即用，不读旧闭包", () => {
  it("首次发送时预检返回需要授权，本次就要求授权而不是直接提交运行", async () => {
    preflightBody = NEEDS_AUTH_PREFLIGHT;
    await renderLoaded();

    fireEvent.click(sendButton());
    // 当前实现读的是闭包里的空预检，会直接提交运行（本断言因此失败）。
    await screen.findByRole("button", { name: "授权并发送" });
    expect(runPosts()).toHaveLength(0);
  });
});

describe("F5 授权需要明确确认", () => {
  it("确认面板显示环境、身份、本人与有效期，确认后才签发并发送", async () => {
    preflightBody = NEEDS_AUTH_PREFLIGHT;
    await renderLoaded();
    fireEvent.click(sendButton());

    const panel = await screen.findByRole("region", { name: "本次授权确认" });
    expect(within(panel).getByText(/测试环境/)).toBeTruthy();
    expect(within(panel).getByText(new RegExp(PROFILE_ID.slice(0, 8)))).toBeTruthy();
    expect(within(panel).getByText(new RegExp(USER_ID.slice(0, 8)))).toBeTruthy();
    expect(within(panel).getByText(/有效期/)).toBeTruthy();

    fireEvent.click(within(panel).getByRole("button", { name: "授权并发送" }));
    await waitFor(() => expect(runPosts()).toHaveLength(1));
    expect(grants()).toHaveLength(1);
  });

  it("取消授权确认不创建运行", async () => {
    preflightBody = NEEDS_AUTH_PREFLIGHT;
    await renderLoaded();
    fireEvent.click(sendButton());
    const panel = await screen.findByRole("region", { name: "本次授权确认" });

    fireEvent.click(within(panel).getByRole("button", { name: "取消" }));
    expect(runPosts()).toHaveLength(0);
    expect(grants()).toHaveLength(0);
    expect(screen.queryByRole("region", { name: "本次授权确认" })).toBeNull();
  });
});

describe("F2 受理结果不明", () => {
  it("网络错误后保留原键与原内容，并提供可达的确认受理入口", async () => {
    gated.add("runs");
    await renderLoaded();
    fireEvent.click(sendButton());
    await waitFor(() => expect(runPosts()).toHaveLength(1));
    const originalKey = runPosts()[0].headers?.["Idempotency-Key"];
    expect(originalKey).toBeTruthy();

    await act(async () => {
      releaseError("runs", new NetworkError("连接中断"));
    });

    const retry = await screen.findByRole("button", { name: /确认受理结果/ });
    // 普通发送入口此时不能再造新键重发。
    const plain = screen.queryByRole("button", { name: "发送" });
    if (plain !== null) expect((plain as HTMLButtonElement).disabled).toBe(true);

    gated.delete("runs");
    fireEvent.click(retry);
    await waitFor(() => expect(runPosts()).toHaveLength(2));
    expect(runPosts()[1].headers?.["Idempotency-Key"]).toBe(originalKey);
    expect(runPosts()[1].body).toEqual(runPosts()[0].body);
  });
});

describe("F3 配置与内容失效", () => {
  it("编辑请求后旧预检立即失效，字段结论不再沿用", async () => {
    await renderLoaded();
    // 先产生一份与当前内容同源的调试报告。
    fireEvent.click(sendButton());
    await waitFor(() => expect(runPosts()).toHaveLength(1));
    await waitFor(() =>
      expect(screen.getByRole("region", { name: "响应" }).textContent).toContain("200"),
    );

    // 改路径：这不是已发出的那份内容，旧结论必须立即失效（不等防抖结束）。
    fireEvent.change(screen.getByLabelText("路径"), { target: { value: "/orders" } });
    const response = screen.getByRole("region", { name: "响应" });
    await waitFor(() => expect(response.textContent).toContain("上一次发送"));
  });

  it("环境配置世代变化后，旧调试报告不再冒充当前结论", async () => {
    const view = await renderLoaded();
    fireEvent.click(sendButton());
    await waitFor(() => expect(runPosts()).toHaveLength(1));
    await waitFor(() =>
      expect(screen.getByRole("region", { name: "响应" }).textContent).toContain("200"),
    );

    // 变量／身份配置变更由外壳上报为世代递增。
    view.rerender(editorElement({ configEpoch: 1 }));
    const response = screen.getByRole("region", { name: "响应" });
    await waitFor(() => expect(response.textContent).toContain("上一次发送"));
  });

  it("迟到预检不覆盖新内容的结论", async () => {
    await renderLoaded();
    // 先让挂载后的首次展示性预检落定，窗口里只留下后面两次内容各自的预检。
    // 不等它落定，被挂起的那一次是“点击触发的”还是“挂载防抖触发的”并不确定，
    // 下面按序号放行的语义就随之漂移——负载高时会变成另一条用例。
    await waitFor(() => expect(preflights()).toHaveLength(1));

    gated.add("preflight");

    // 内容 A：防抖发出预检 A，一直悬着。
    fireEvent.change(screen.getByLabelText("路径"), { target: { value: "/changed-a" } });
    await waitFor(() => expect(heldCount("preflight")).toBe(1));

    // 预检 A 还在飞时再改内容 B：防抖为 B 再发一次预检。
    fireEvent.change(screen.getByLabelText("路径"), { target: { value: "/changed-b" } });
    await waitFor(() => expect(heldCount("preflight")).toBe(2));

    // 先放行 B（环境歧义），再放行 A（可以发送）。界面必须保持 B 的结论：
    // A 是针对旧内容的，让它覆盖会让用户看着“本次检查通过”去点一个实际发不出去的请求。
    await act(async () => {
      releaseNth("preflight", 1, AMBIGUOUS_PREFLIGHT);
    });
    await waitFor(() => expect(screen.getByText(/多份可用身份/)).toBeTruthy());

    await act(async () => {
      releaseNth("preflight", 0, OK_PREFLIGHT);
    });
    await act(async () => {});
    expect(screen.getByText(/多份可用身份/)).toBeTruthy();
  });
});

describe("授权期间的冻结与失效", () => {
  it("授权确认期间修改内容：撤销这次待确认，避免授权一份已经改变的请求", async () => {
    // R3 §4 采用“撤销”而不是“照旧授权”：确认面板上写着环境与路径，用户改完内容之后
    // 若仍按冻结的那份签发并提交，他看到的和实际授出去的就不是同一件事。
    // 已提交服务器之前的内容可以安全撤销——此时还没有任何写请求。
    preflightBody = NEEDS_AUTH_PREFLIGHT;
    await renderLoaded();
    fireEvent.click(sendButton());
    expect(await screen.findByRole("region", { name: "本次授权确认" })).toBeTruthy();

    fireEvent.change(screen.getByLabelText("路径"), { target: { value: "/changed-during-auth" } });

    await waitFor(() =>
      expect(screen.queryByRole("region", { name: "本次授权确认" })).toBeNull(),
    );
    // 撤销阶段没有产生任何写请求：没有摘要、没有授权、没有运行。
    expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/debug-snapshot-digest"), "POST")).toEqual([]);
    expect(grants()).toEqual([]);
    expect(runPosts()).toEqual([]);
    expect(await screen.findByText(/内容或环境已变化/)).toBeTruthy();

    // 再发一次走的是新内容：确认面板显示新路径，提交的也是新路径。
    fireEvent.click(sendButton());
    const panel = await screen.findByRole("region", { name: "本次授权确认" });
    expect(within(panel).getByText(/changed-during-auth/)).toBeTruthy();
    fireEvent.click(within(panel).getByRole("button", { name: "授权并发送" }));
    await waitFor(() => expect(runPosts()).toHaveLength(1));
    const sent = runPosts()[0].body as { debug_snapshot: { request: { path: string } } };
    expect(sent.debug_snapshot.request.path).toBe("/changed-during-auth");
  });

  it("等待授权时配置世代变化，确认入口消失且不再提交", async () => {
    preflightBody = NEEDS_AUTH_PREFLIGHT;
    const view = await renderLoaded();
    fireEvent.click(sendButton());
    await screen.findByRole("region", { name: "本次授权确认" });

    // 环境／变量／身份被改：旧预检与旧操作都不再代表当前配置。
    await act(async () => {
      view.rerender(editorElement({ configEpoch: 1 }));
    });

    expect(screen.queryByRole("region", { name: "本次授权确认" })).toBeNull();
    expect(runPosts()).toHaveLength(0);
    expect(grants()).toHaveLength(0);
  });
});

describe("F6 来源的显式切换", () => {
  it("版本报告在调试选择期间不抢来源；显式点选后来源与正文一起切换", async () => {
    const view = await renderLoaded();
    fireEvent.click(sendButton());
    await waitFor(() =>
      expect(screen.getByRole("region", { name: "响应" }).textContent).toContain("200"),
    );

    // 版本报告到达：调试选择优先，正文不变。
    panelHarness.report = {
      ...debugReport(RUN_2, "fp-current", 500),
      run: {
        ...runSummary(RUN_2),
        target_type: "case_version",
        case_version_id: "66666666-6666-4666-8666-666666666666",
        state: "finished",
        outcome: "passed",
      },
      context: null,
    };
    // 一次普通的界面重渲染（切到断言标签再切回来）就会让替身把报告上报出去。
    fireEvent.click(screen.getByRole("tab", { name: /^断言/ }));
    await act(async () => {});
    fireEvent.click(screen.getByRole("tab", { name: /^参数/ }));
    expect(screen.getByRole("region", { name: "响应" }).textContent).toContain("200");

    // 显式点选本次调试记录：来源切回调试，正文与字段结论同源。
    // 切换选择会先清空上一条报告（避免短暂显示与选择不符的内容），因此要等它重新读回。
    fireEvent.click(recordButtons(view)[0]);
    await waitFor(() =>
      expect(screen.getByRole("region", { name: "响应" }).textContent).toContain("200"),
    );
    expect(screen.getByRole("region", { name: "响应" }).textContent).toContain(RUN_1.slice(0, 8));
  });
});

describe("F7 报告按运行与读取世代隔离", () => {
  it("受理 r2 后 r2 的报告读失败：不得回落到 r1 的通过", async () => {
    // r1 的报告已经成功缓存（201 通过）。随后受理 r2，而 r2 的报告读取失败——
    // 此时**不能**把 r1 的 201 当成 r2 的正文与状态：那会让用户以为刚发出去的这次
    // 请求已经通过了。旧报告可以留在缓存里供以后查看，但不作为当前结论。
    await renderLoaded();

    // r1：受理后到达终态（201，明确区别于 r2 的默认 200）。
    gated.add("report:" + RUN_1);
    fireEvent.click(sendButton());
    await waitFor(() => expect(runPosts()).toHaveLength(1));
    await act(async () => {
      release("report:" + RUN_1, debugReport(RUN_1, "fp-current", 201));
    });
    await waitFor(() =>
      expect(screen.getByRole("region", { name: "响应" }).textContent).toContain("201"),
    );

    // r2 的报告读取失败。
    gated.add("report:" + RUN_2);
    fireEvent.click(sendButton());
    await waitFor(() => expect(runPosts()).toHaveLength(2));
    // 先证明 r2 已受理且它自己的报告读取确实进入挂起窗口；不能只看到 POST 就假定
    // 后续读取已经发出。CI 并发负载下这两次提交之间存在真实调度间隙。
    await waitFor(() =>
      expect(screen.getByRole("region", { name: "响应" }).textContent).toContain(RUN_2.slice(0, 8)),
    );
    await waitFor(() => expect(heldCount("report:" + RUN_2)).toBeGreaterThan(0));

    // 这条场景的前提是 r2 报告**持续**不可用。只拒绝当前 bucket 后立刻恢复成功，
    // 下一次并发读取／轮询就会拿到终态并合理收起取消按钮，测试测到的是替身时序而非产品。
    failingReports.add(RUN_2);
    await act(async () => {
      releaseError("report:" + RUN_2, new NetworkError("报告不可用"));
    });

    const response = screen.getByRole("region", { name: "响应" });
    // 标题指向 r2，正文不得显示 r1 的结论。
    await waitFor(() => expect(response.textContent).toContain("报告读取失败"));
    expect(response.textContent).toContain(RUN_2.slice(0, 8));
    expect(response.textContent).not.toContain("201");
    // 报告读失败也要有取消入口：run_id 是已知的。
    expect(screen.getByRole("button", { name: /取消/ })).toBeTruthy();
  });
});

describe("F8 卸载后不再提交", () => {
  it("授权请求在飞时卸载组件，完成后不再提交运行", async () => {
    preflightBody = NEEDS_AUTH_PREFLIGHT;
    const view = await renderLoaded();
    fireEvent.click(sendButton());
    const panel = await screen.findByRole("region", { name: "本次授权确认" });

    gated.add("grant");
    fireEvent.click(within(panel).getByRole("button", { name: "授权并发送" }));
    await waitFor(() => expect(grants()).toHaveLength(1));

    view.unmount();
    await act(async () => {
      release("grant", { id: "grant-1" });
    });

    expect(runPosts()).toHaveLength(0);
  });
});

describe("F9 首次保存不改编辑实例", () => {
  it("新建用例保存得到 id 后，本次调试历史不丢", async () => {
    const view = await renderLoaded({ caseSummaryId: null });
    fireEvent.click(sendButton());
    await waitFor(() => expect(runPosts()).toHaveLength(1));
    await waitFor(() => expect(runPosts()).toHaveLength(1));
    // 首次保存：currentId 从 null 变成 id，编辑实例必须保持稳定——历史不能因此被清空，
    // 正在受理的链路也不能被丢掉。
    gated.add("create");
    fireEvent.click(screen.getByRole("button", { name: "创建用例" }));
    await act(async () => {
      release("create", { ...CASE_DETAIL });
    });

    await waitFor(() => expect(recordButtons(view)).toHaveLength(1));
    expect(recordButtons(view)[0].textContent).toBe(RUN_1.slice(0, 8));
  });
});

describe("F10 取消与停止等待", () => {
  it("报告读取失败时仍能取消已知运行", async () => {
    await renderLoaded();
    gated.add("report:" + RUN_1);
    fireEvent.click(sendButton());
    await waitFor(() => expect(runPosts()).toHaveLength(1));

    await act(async () => {
      releaseError("report:" + RUN_1, new NetworkError("报告不可用"));
    });
    await waitFor(() =>
      expect(screen.getByRole("region", { name: "响应" }).textContent).toContain("读取失败"),
    );

    const cancel = screen.getByRole("button", { name: /取消/ });
    fireEvent.click(cancel);
    await waitFor(() =>
      expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, `/runs/${RUN_1}/cancel`), "POST")).toHaveLength(1),
    );
  });

  it("停止等待真的停止轮询，且不谎称服务端已取消", async () => {
    gated.add("preflight");
    await renderLoaded();
    fireEvent.click(sendButton());
    const stop = await screen.findByRole("button", { name: "停止等待" });

    fireEvent.click(stop);
    await act(async () => {
      release("preflight", OK_PREFLIGHT);
    });
    await act(async () => {});

    // 本地已停止等待：不产生运行，也不声称服务器已取消。
    expect(runPosts()).toHaveLength(0);
    expect(
      callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, `/runs/${RUN_1}/cancel`), "POST"),
    ).toHaveLength(0);
    expect(screen.queryByText(/服务器.*取消/)).toBeNull();
  });
});

describe("F6 运行选择驱动来源", () => {
  it("版本报告不能把来源从调试抢走", async () => {
    const view = await renderLoaded();
    fireEvent.click(sendButton());
    await waitFor(() => expect(runPosts()).toHaveLength(1));
    await waitFor(() =>
      expect(screen.getByRole("region", { name: "响应" }).textContent).toContain("200"),
    );

    // 项目／环境历史里的版本运行报告到达（真实 RunPanel 就是这样上报的）。
    panelHarness.report = {
      ...debugReport(RUN_2, "fp-current", 500),
      run: {
        ...runSummary(RUN_2),
        target_type: "case_version",
        case_version_id: "66666666-6666-4666-8666-666666666666",
        state: "finished",
        outcome: "passed",
      },
      context: null,
    };
    // 触发一次渲染，让替身在 effect 里把它上报出去（真实 RunPanel 也是渲染后上报）。
    await act(async () => {
      view.rerender(editorElement());
    });

    // 调试选择仍然生效：正文与字段结论同源，版本报告的 500 不得改写正文。
    const response = screen.getByRole("region", { name: "响应" });
    expect(response.textContent).toContain("200");
    expect(response.textContent).not.toContain("500");
  });

  it("同一次版本运行的内容更新不会被丢掉", async () => {
    // 一次运行从排队走到结束，报告内容会变但 runId 不变。按 runId 丢掉这次更新，正文就会
    // 停在旧状态——界面看起来“没有结果”，而服务端其实已经写完了。
    const view = await renderLoaded();

    panelHarness.report = {
      ...debugReport(RUN_2, "fp-current", 500),
      run: {
        ...runSummary(RUN_2),
        target_type: "case_version",
        case_version_id: "66666666-6666-4666-8666-666666666666",
        state: "finished",
        outcome: "passed",
      },
      context: null,
    };
    // 选择是显式事件：用户点历史里的那条运行（替身提供同名入口）。
    await act(async () => {
      view.rerender(editorElement());
    });
    fireEvent.click(screen.getByRole("button", { name: "查看报告" }));
    await waitFor(() =>
      expect(screen.getByRole("region", { name: "响应" }).textContent).toContain("500"),
    );

    // 同一个 run 的报告内容更新（例如响应正文变化）：必须被接纳。
    panelHarness.report = {
      ...panelHarness.report,
      response: {
        ...(panelHarness.report.response as Record<string, unknown>),
        body: '{"updated":true}',
      },
    };
    await act(async () => {
      view.rerender(editorElement());
    });
    await waitFor(() =>
      expect(screen.getByRole("region", { name: "响应" }).textContent).toContain("updated"),
    );
    // 状态码仍是这一次运行的那一个，说明选择没有跳到别处。
    expect(screen.getByRole("region", { name: "响应" }).textContent).toContain("500");
  });
});
