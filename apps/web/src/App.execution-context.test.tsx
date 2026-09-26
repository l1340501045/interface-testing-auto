/**
 * 执行配置变更的真实失效链（R3 §3／R3-06～09）。
 *
 * 这里走**真实 App**：真实外壳 → 真实 EnvironmentPanel／VariablesPanel／CredentialsPanel
 * → 真实 CaseEditor。没有替身参与通知链，也不注入 `configEpoch` ——那只能证明“传进去会
 * 失效”，证明不了“管理面板保存成功后确实会传进去”。R2 的问题正是后者：props 有了，App
 * 没接。
 *
 * 断言的是**可观察结果**：在管理面板里保存成功后，工作台里基于旧配置的预检提示与“当前
 * 通过”立即失效（不等列表刷新回来）。同时验证反向：读取列表、保存失败都**不**推进时钟，
 * 否则界面会无端作废一份刚算好的结论。
 */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const WORKSPACE_ID = "11111111-1111-4111-8111-111111111111";
const WORKSPACE_ID_B = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const PROJECT_ID_B = "33333333-3333-4333-8333-333333333333";
const CASE_ID = "44444444-4444-4444-8444-444444444444";
const ENV_ID = "55555555-5555-4555-8555-555555555555";
const RUN_ID = "77777777-7777-4777-8777-777777777777";
const RUN_ID_B = "88888888-8888-4888-8888-888888888888";
const USER_ID = "99999999-9999-4999-8999-999999999999";

vi.mock("./session/useSession", () => ({
  useSession: () => ({
    session: {
      user: { user_id: USER_ID, username: "tester", display_name: "测试员", is_admin: true },
      workspaces: [
        { id: WORKSPACE_ID, name: "默认工作空间", role: "admin" },
        { id: WORKSPACE_ID_B, name: "第二工作空间", role: "admin" },
      ],
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

import { apiDelete, apiGet, apiSend, apiSendWithMeta } from "./api/client";
import { App } from "./App";

const apiGetMock = vi.mocked(apiGet);
const apiSendMock = vi.mocked(apiSend);
const apiSendWithMetaMock = vi.mocked(apiSendWithMeta);
void vi.mocked(apiDelete);

interface Call {
  method: string;
  path: string;
  body: unknown;
}

let calls: Call[] = [];
/** 环境替身：base_url 保存后被替换，后续预检按**当前**环境返回结论。 */
function makeEnvironment(baseUrl: string) {
  return {
    id: ENV_ID,
    name: "测试环境",
    kind: "test",
    base_url: baseUrl,
    pool_id: null,
    variables: {},
    status: "active",
  };
}
let environment = makeEnvironment("http://echo.test");
let projectVariables = { version: 1, variables: [] as { name: string; value: unknown }[] };
/**
 * 管理**写请求**是否失败：用于验证“失败不推进时钟”。
 *
 * 只作用于管理面板的写（PATCH 环境／PUT 变量）。预检是只读的，不受它影响——让预检也
 * 失败会把“配置变更未推进时钟”与“结论因为缺少预检而失效”两件事混在一起。
 */
let failWrites = false;
let preflightOverride: unknown | null = null;
let historyRuns: unknown[] = [];

const BASE = `/api/v1/workspaces/${WORKSPACE_ID}/projects/${PROJECT_ID}`;

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

const PREFLIGHT_READY = {
  ready: true,
  issues: [],
  can_authorize: true,
  auth: { required: false, state: "none", profile_id: null },
  context: {
    snapshot_fingerprint: "fp-1",
    environment: { id: ENV_ID, name: "测试环境", kind: "test", base_url: "http://echo.test" },
    input_fingerprint: "in-1",
  },
};

const RUN_REPORT = {
  run: {
    id: RUN_ID,
    target_type: "debug_snapshot",
    case_version_id: null,
    environment_id: ENV_ID,
    state: "finished",
    outcome: "passed",
    reason_category: null,
    pool_id: null,
    created_at: "2026-09-15T00:00:00Z",
  },
  steps: [],
  assertions: [],
  request: null,
  response: {
    status: 200,
    elapsed_ms: 5,
    headers: [],
    body: '{"ok":true}',
    body_format: "json",
    body_truncated: false,
    body_omitted_reason: null,
    size_bytes: 11,
  },
  context: {
    snapshot_fingerprint: "fp-1",
    environment: { id: ENV_ID, name: "测试环境", kind: "test", base_url: "http://echo.test" },
    input_fingerprint: "in-1",
  },
};

/** 去掉查询串：列表接口会带筛选参数，路由按基础路径匹配。 */
function basePath(path: string): string {
  const index = path.indexOf("?");
  return index < 0 ? path : path.slice(0, index);
}

function route(rawPath: string, method: string, body?: unknown): unknown {
  const path = basePath(rawPath);
  calls.push({ method, path, body });
  if (method === "GET" && path.endsWith("/projects")) {
    if (path.includes(`/workspaces/${WORKSPACE_ID_B}/`)) {
      return [{
        id: PROJECT_ID_B,
        workspace_id: WORKSPACE_ID_B,
        key: "beta",
        name: "项目乙",
        status: "active",
        role: "admin",
        pool_id: null,
      }];
    }
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
      {
        id: PROJECT_ID_B,
        workspace_id: WORKSPACE_ID,
        key: "beta",
        name: "项目乙",
        status: "active",
        role: "admin",
        pool_id: null,
      },
    ];
  }
  if (method === "GET" && path.endsWith("/environments")) return [environment];
  if (method === "GET" && path.endsWith("/folders")) return [];
  if (method === "GET" && path.endsWith("/cases")) {
    return [
      { id: CASE_ID, folder_id: null, name: "查询订单", method: "GET", status: "draft", rev: 3, latest_version: null },
    ];
  }
  if (method === "GET" && path.endsWith("/assertion-types")) return [];
  if (method === "GET" && path.endsWith("/variables")) return projectVariables;
  if (method === "GET" && path.includes("/credentials/")) return [];
  if (method === "GET" && path.includes("/pools")) return [];
  if (method === "GET" && path.endsWith("/runs")) return historyRuns;
  if (method === "GET" && path.endsWith(`/cases/${CASE_ID}/versions`)) return [];
  if (method === "GET" && path.endsWith(`/cases/${CASE_ID}`)) return CASE_DETAIL;
  if (method === "POST" && path.endsWith("/debug-preflight")) {
    if (preflightOverride !== null) return preflightOverride;
    // 地址缺协议时服务端不会走到白名单判断，而是直接报“环境地址不合法”并建议去改环境。
    // 这里按同一口径回答，页面看到的结论与真实服务端一致。
    if (!environment.base_url.includes("://")) {
      return {
        ready: false,
        issues: [
          {
            code: "environment_url_invalid",
            message: "环境地址必须以 http:// 或 https:// 开头，不能只写主机名或“主机:端口”。",
            action: "configure_environment",
          },
        ],
        can_authorize: true,
        auth: { required: false, state: "none", profile_id: null },
        context: null,
      };
    }
    // 预检按**当前**环境回答：环境改了之后旧结论不再匹配。
    return {
      ...PREFLIGHT_READY,
      context: {
        ...PREFLIGHT_READY.context,
        environment: { ...PREFLIGHT_READY.context.environment, base_url: environment.base_url },
      },
    };
  }
  if (method === "POST" && path.endsWith("/runs")) {
    return { ...RUN_REPORT.run, state: "queued", outcome: null };
  }
  if (method === "GET" && path.endsWith("/report")) {
    const runId = /\/runs\/([^/]+)\/report$/.exec(path)?.[1] ?? RUN_ID;
    return { ...RUN_REPORT, run: { ...RUN_REPORT.run, id: runId } };
  }
  if (method === "PATCH" && path.endsWith(`/environments/${ENV_ID}`)) {
    if (failWrites) throw new Error("环境保存失败");
    const patch = (body ?? {}) as { base_url?: string };
    if (typeof patch.base_url === "string") environment = makeEnvironment(patch.base_url);
    return environment;
  }
  if (method === "PUT" && path.endsWith("/variables")) {
    if (failWrites) throw new Error("变量保存失败");
    const payload = (body ?? {}) as { variables?: { name: string; value: unknown }[] };
    projectVariables = { version: projectVariables.version + 1, variables: payload.variables ?? [] };
    return projectVariables;
  }
  throw new Error(`测试未覆盖的请求：${method} ${path}`);
}

  // 装配阶段的等待预算：这里等的是“用例编辑器挂载完成”，不是某个断言内容。
  // 整套测试并行跑十几个 jsdom 环境，CPU 争用会把同一段代码的墙上时间放大数倍
  // （与 test/setup.ts 记录的同一现象）。断言内容不因此放宽：期望的元素一字未改。
  const SETUP_WAIT = { timeout: 20000 };

/** 打开项目并选中用例，返回编辑器就绪后的容器。 */
async function openCase(): Promise<void> {
  render(<App />);
  const projectSelect = (await screen.findByLabelText("项目")) as HTMLSelectElement;
  await waitFor(() => expect(projectSelect.value).toBe(PROJECT_ID), SETUP_WAIT);
  await act(async () => {});
  // 等侧栏（目录／用例列表）真正挂载：项目选择到位不等于环境与目录已经读完。
  const browser = await screen.findByLabelText("用例目录");
  fireEvent.click(await within(browser).findByRole("button", { name: /查询订单/ }));
  await waitFor(
    () => expect((screen.getByLabelText("用例名称") as HTMLInputElement).value).toBe("查询订单"),
    SETUP_WAIT,
  );
  // 等编辑防抖触发的展示性预检落定（400ms）。
  //
  // “当前通过”的判据要求存在一份**针对当前内容**的预检结论——这是设计：没有它就无法
  // 说明这份报告对应的配置。因此在这里等它出现，而不是把断言放宽成“包含 200 就算通过”，
  // 后者会让“结论过期”伪装成通过。
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 450));
  });
  await act(async () => {});
}

/**
 * 进入独立「环境配置」页，返回**包含环境面板与管理面板的页面容器**。
 *
 * 环境编辑与凭证管理同在这个折叠区里（环境在侧栏，凭证在其下的 #admin-panel）；
 * 只取 #admin-panel 会漏掉环境面板，测出来的“改环境不失效”其实是没找到控件。
 */
async function openAdmin(): Promise<HTMLElement> {
  fireEvent.click(screen.getByRole("button", { name: "环境配置" }));
  await act(async () => {});
  const panel = screen.getByRole("region", { name: "环境配置" });
  if (panel === null) throw new Error("管理入口未挂载");
  return panel as HTMLElement;
}

/** 发一次调试并等它显示真实结论，返回响应区。 */
async function debugOnce(): Promise<HTMLElement> {
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() =>
    expect(screen.getByRole("region", { name: "响应" }).textContent).toContain("200"),
  );
  return screen.getByRole("region", { name: "响应" });
}

beforeEach(() => {
  window.history.replaceState(null, "", "#/workbench");
  calls = [];
  environment = makeEnvironment("http://echo.test");
  projectVariables = { version: 1, variables: [] };
  failWrites = false;
  preflightOverride = null;
  historyRuns = [];
  window.confirm = vi.fn(() => true);
  apiGetMock.mockReset();
  apiSendMock.mockReset();
  apiSendWithMetaMock.mockReset();
  apiGetMock.mockImplementation((async (path: string) => route(path, "GET")) as never);
  apiSendMock.mockImplementation((async (path: string, method: string, body: unknown) => route(path, method, body)) as never);
  apiSendWithMetaMock.mockImplementation((async (path: string, method: string, body: unknown) => ({
    data: route(path, method, body),
    etag: `"${CASE_DETAIL.rev}"`,
  })) as never);
});

describe("执行配置变更的真实失效链", () => {
  it("在真实环境面板里保存 base_url 后，工作台里基于旧环境的结论立即失效", async () => {
    await openCase();
    const response = await debugOnce();
    expect(response.textContent).not.toContain("上一次发送");

    const admin = await openAdmin();
    // 真实环境编辑表单：打开编辑、改地址、保存。
    fireEvent.click(within(admin).getByRole("button", { name: "编辑" }));
    const baseInput = await within(admin).findByLabelText("服务地址");
    fireEvent.change(baseInput, { target: { value: "http://echo-alt.test" } });
    const saveButton = within(admin).getByRole("button", { name: "保存环境" });
    await act(async () => {
      fireEvent.click(saveButton);
    });

    // PATCH 一旦成功，工作台当场把旧结论标成“上一次发送”——不等环境列表刷新回来。
    await waitFor(() => expect(response.textContent).toContain("上一次发送"));
  });

  it("在真实变量面板里保存项目变量后，当前结论立即失效", async () => {
    await openCase();
    const response = await debugOnce();

    const admin = await openAdmin();
    // 项目变量面板：新增一行并保存。
    fireEvent.click(within(admin).getByRole("button", { name: "＋添加变量" }));
    const nameInput = await within(admin).findByLabelText("名称");
    fireEvent.change(nameInput, { target: { value: "shared" } });
    await act(async () => {
      fireEvent.click(within(admin).getByRole("button", { name: "保存为新版本" }));
    });

    await waitFor(() => expect(response.textContent).toContain("上一次发送"));
  });

  it("管理写失败不推进失效：结论保持可用", async () => {
    await openCase();
    const response = await debugOnce();

    const admin = await openAdmin();
    failWrites = true;
    fireEvent.click(within(admin).getByRole("button", { name: "编辑" }));
    const baseInput = await within(admin).findByLabelText("服务地址");
    fireEvent.change(baseInput, { target: { value: "http://echo-alt.test" } });
    await act(async () => {
      fireEvent.click(within(admin).getByRole("button", { name: "保存环境" }));
    });
    await act(async () => {});

    // 保存失败：没有发生配置变更，不该作废一份仍然成立的结论。
    expect(response.textContent).not.toContain("上一次发送");
  });

  it("只读地读取管理列表不会反复作废结论", async () => {
    await openCase();
    const response = await debugOnce();

    const admin = await openAdmin();
    // 展开各管理面板会触发多次 GET；读取不是变更。
    fireEvent.click(within(admin).getByRole("button", { name: "编辑" }));
    await act(async () => {});
    await act(async () => {});

    expect(response.textContent).not.toContain("上一次发送");
  });
});

/**
 * 环境地址不合法时的“下一步”必须是真能点的入口（ENV-03／ENV-04）。
 *
 * 走**真实 App**：真实外壳 → 真实 CaseEditor → 真实 SendBar → 独立环境配置页。
 * 只测 SendBar 收到回调会调用它，证明不了外壳把它接到了环境那一段；这里断言点击之后
 * 展开的确实是含环境编辑的那个折叠区。
 */
describe("环境地址无效时的真实入口", () => {
  it("点“前往环境设置”直接进入独立配置页并展开环境面板", async () => {
    // 存量坏数据：地址缺协议，请求发不出去，但记录仍在。
    environment = makeEnvironment("target-service:8080");
    await openCase();

    const entry = await screen.findByRole("button", { name: "前往环境设置" });
    // 无效地址不能被当成“实际目标”展示。
    expect(screen.getByText(/环境地址不合法，暂时无法确定/)).toBeTruthy();
    expect(screen.queryByText(/实际目标：target-service:8080/)).toBeNull();

    const panel = document.getElementById("environment-panel") as HTMLDetailsElement;
    /*
      只断言 `sidebar.open` 是不够的：外层展开后，“环境（N）”这一层仍可能是折叠的，
      用户点进来看到的还是一个没有编辑入口的空标题。这里必须断言**内层**的 open。

      为什么不断言“编辑按钮可见”：jsdom 不建模 details 折叠对内容可见性的影响，折叠时
      同样能找到按钮——那样的断言恒真，测不出这个缺陷。`open` 才是用户看到的状态。
    */
    expect(panel.open).toBe(false);

    fireEvent.click(entry);
    await act(async () => {});

    expect(screen.getByRole("region", { name: "环境配置" })).toBeTruthy();
    expect(window.location.hash).toBe("#/environments");
    expect(panel.open).toBe(true);

    // 进入配置页必须保住同一份未保存用例：工作台只是隐藏，编辑器没有重挂载。
    expect((document.getElementById("request-path") as HTMLInputElement).value).toBe("/echo");
  });

  it("展开后确实能走到环境编辑表单（编辑入口不再藏在折叠标题下）", async () => {
    environment = makeEnvironment("target-service:8080");
    await openCase();

    fireEvent.click(await screen.findByRole("button", { name: "前往环境设置" }));
    await act(async () => {});

    const panel = document.getElementById("environment-panel") as HTMLDetailsElement;
    expect(panel.open).toBe(true);
    // 面板内的编辑入口真的可用：点开后能看到地址输入框。
    fireEvent.click(within(panel).getByRole("button", { name: "编辑" }));
    expect((await within(panel).findByLabelText("服务地址")) as HTMLInputElement).toBeTruthy();
  });

  it("缺协议的地址在环境面板里就地挡住，不发写请求", async () => {
    environment = makeEnvironment("target-service:8080");
    await openCase();
    await screen.findByRole("button", { name: "前往环境设置" });

    const admin = await openAdmin();
    fireEvent.click(within(admin).getByRole("button", { name: "编辑" }));
    const baseInput = await within(admin).findByLabelText("服务地址");
    // 不改地址直接保存：服务端会拒绝这条存量值，本地也应当先挡住。
    const writesBefore = calls.filter((call) => call.method === "PATCH").length;
    await act(async () => {
      fireEvent.click(within(admin).getByRole("button", { name: "保存环境" }));
    });

    expect(calls.filter((call) => call.method === "PATCH")).toHaveLength(writesBefore);
    expect((baseInput as HTMLInputElement).value).toBe("target-service:8080");
  });

  it("把环境地址修好之后，同一份未保存草稿可以继续调试", async () => {
    environment = makeEnvironment("target-service:8080");
    await openCase();
    await screen.findByRole("button", { name: "前往环境设置" });

    // 走真实环境编辑表单改地址。
    const admin = await openAdmin();
    fireEvent.click(within(admin).getByRole("button", { name: "编辑" }));
    const baseInput = await within(admin).findByLabelText("服务地址");
    fireEvent.change(baseInput, { target: { value: "http://echo.test" } });
    await act(async () => {
      fireEvent.click(within(admin).getByRole("button", { name: "保存环境" }));
    });

    // 配置变更由外壳广播；预检按新地址重新给结论，无效提示随之消失。
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "前往环境设置" })).toBeNull(),
    );
    // 草稿没有被清掉或重载：还是原来那一份请求内容。
    expect((screen.getByLabelText("路径") as HTMLInputElement).value).toBe("/echo");
  });
});

describe("独立页面的状态归属", () => {
  it("凭证纠错进入配置页后展开并聚焦真实凭证块", async () => {
    preflightOverride = {
      ready: false,
      issues: [{ code: "credential_missing", message: "当前环境还没有可用身份。", action: "manage_credentials" }],
      can_authorize: true,
      auth: { required: true, state: "unavailable", profile_id: null },
      context: null,
    };
    await openCase();

    const entry = await screen.findByRole("button", { name: "环境与凭证管理" });
    fireEvent.click(entry);
    await act(async () => {});

    const credentials = document.getElementById("credentials-panel") as HTMLDetailsElement;
    expect(screen.getByRole("region", { name: "环境配置" })).toBeTruthy();
    expect(credentials.open).toBe(true);
    expect(document.activeElement).toBe(credentials);
    expect((document.getElementById("request-path") as HTMLInputElement).value).toBe("/echo");
  });

  it("任务报告跳转只消费一次，往返保留新选择，再点同一任务仍可跳回", async () => {
    historyRuns = [
      { ...RUN_REPORT.run, id: RUN_ID, state: "finished", outcome: "passed" },
      { ...RUN_REPORT.run, id: RUN_ID_B, state: "finished", outcome: "failed" },
    ];
    await openCase();
    const selectedEnvironment = (screen.getByLabelText("执行环境") as HTMLSelectElement).value;

    fireEvent.click(screen.getByRole("button", { name: "任务中心" }));
    const rowA = (await screen.findByText(RUN_ID.slice(0, 8))).closest("tr") as HTMLElement;
    fireEvent.click(within(rowA).getByRole("button", { name: "查看报告" }));
    await screen.findByRole("heading", { name: `运行 ${RUN_ID.slice(0, 8)} 的报告` });

    const rowB = (await screen.findByText(RUN_ID_B.slice(0, 8))).closest("tr") as HTMLElement;
    fireEvent.click(within(rowB).getByRole("button", { name: "查看报告" }));
    await screen.findByRole("heading", { name: `运行 ${RUN_ID_B.slice(0, 8)} 的报告` });

    fireEvent.click(screen.getByRole("button", { name: "接口工作台" }));
    fireEvent.click(screen.getByRole("button", { name: "测试报告" }));
    expect(screen.getByRole("heading", { name: `运行 ${RUN_ID_B.slice(0, 8)} 的报告` })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "任务中心" }));
    const rowAAgain = (await screen.findByText(RUN_ID.slice(0, 8))).closest("tr") as HTMLElement;
    fireEvent.click(within(rowAAgain).getByRole("button", { name: "查看报告" }));
    await screen.findByRole("heading", { name: `运行 ${RUN_ID.slice(0, 8)} 的报告` });

    fireEvent.click(screen.getByRole("button", { name: "接口工作台" }));
    expect((screen.getByLabelText("执行环境") as HTMLSelectElement).value).toBe(selectedEnvironment);
    expect(calls.filter((call) => call.method === "POST" && call.path.endsWith("/runs"))).toHaveLength(0);
  });

  it("跨项目首帧拒绝旧报告意图，不向新项目请求旧运行", async () => {
    historyRuns = [{ ...RUN_REPORT.run, id: RUN_ID, state: "finished", outcome: "passed" }];
    await openCase();
    fireEvent.click(screen.getByRole("button", { name: "任务中心" }));
    const rowA = (await screen.findByText(RUN_ID.slice(0, 8))).closest("tr") as HTMLElement;
    fireEvent.click(within(rowA).getByRole("button", { name: "查看报告" }));
    await screen.findByRole("heading", { name: `运行 ${RUN_ID.slice(0, 8)} 的报告` });

    // 报告页隐藏着同一个编辑器；未保存草稿仍必须拦住跨项目切换。
    fireEvent.change(document.getElementById("request-path") as HTMLInputElement, {
      target: { value: "/changed" },
    });
    const projectSelect = screen.getByLabelText("项目") as HTMLSelectElement;
    vi.mocked(window.confirm).mockReturnValueOnce(false);
    fireEvent.change(projectSelect, { target: { value: PROJECT_ID_B } });
    expect(projectSelect.value).toBe(PROJECT_ID);
    expect((document.getElementById("request-path") as HTMLInputElement).value).toBe("/changed");

    vi.mocked(window.confirm).mockReturnValueOnce(true);
    fireEvent.change(projectSelect, { target: { value: PROJECT_ID_B } });
    await waitFor(() => expect(projectSelect.value).toBe(PROJECT_ID_B));
    expect(screen.queryByRole("heading", { name: `运行 ${RUN_ID.slice(0, 8)} 的报告` })).toBeNull();
    expect(
      calls.some((call) =>
        call.method === "GET" &&
        call.path.includes(`/projects/${PROJECT_ID_B}/runs/${RUN_ID}/report`),
      ),
    ).toBe(false);
  });

  it("跨工作空间首帧同样拒绝旧报告意图", async () => {
    historyRuns = [{ ...RUN_REPORT.run, id: RUN_ID, state: "finished", outcome: "passed" }];
    await openCase();
    fireEvent.click(screen.getByRole("button", { name: "任务中心" }));
    const rowA = (await screen.findByText(RUN_ID.slice(0, 8))).closest("tr") as HTMLElement;
    fireEvent.click(within(rowA).getByRole("button", { name: "查看报告" }));
    await screen.findByRole("heading", { name: `运行 ${RUN_ID.slice(0, 8)} 的报告` });

    fireEvent.change(screen.getByLabelText("工作空间"), { target: { value: WORKSPACE_ID_B } });
    await waitFor(() => expect((screen.getByLabelText("工作空间") as HTMLSelectElement).value).toBe(WORKSPACE_ID_B));
    await waitFor(() => expect((screen.getByLabelText("项目") as HTMLSelectElement).value).toBe(PROJECT_ID_B));
    expect(screen.queryByRole("heading", { name: `运行 ${RUN_ID.slice(0, 8)} 的报告` })).toBeNull();
    expect(
      calls.some((call) =>
        call.method === "GET" &&
        call.path.includes(`/workspaces/${WORKSPACE_ID_B}/projects/${PROJECT_ID_B}/runs/${RUN_ID}/report`),
      ),
    ).toBe(false);
  });
});
