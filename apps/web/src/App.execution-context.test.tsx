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
import { act, fireEvent, render, waitFor } from "@testing-library/react";
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
    rev: 1,
    config_version: 1,
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
const RESOLUTION = {
  schema_version: 1, scope: { workspace_id: WORKSPACE_ID, project_id: PROJECT_ID, environment_id: ENV_ID }, ready: true,
  ordinary_resolution: "ready", masked_target: { url: "http://echo.test/echo", method: "GET" }, bindings: [], issues: [],
  auth: { required: false, status: "none", injection_slots: [], requires_worker_verification: false },
  config_basis: { project_variables_version: 1, project_config_version_id: null, environment_rev: 1, environment_config_version: 1, environment_config_version_id: "cfg" },
  context_fingerprint: "source-fp", resolution_context: "resolution-context",
};
Object.assign(PREFLIGHT_READY, { resolution: RESOLUTION });

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
    resolution: { schema_version: 1, guard: "ordinary_binding_enforced_v1", context_fingerprint: "source-fp", binding_fingerprint: "binding-fp" },
  },
  resolution: { schema_version: 1, config_basis: RESOLUTION.config_basis, variable_sources: [], bindings: [], context_fingerprint: "source-fp" },
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
  if (method === "POST" && path.endsWith(`/case-preferences/${CASE_ID}/opened`)) {
    return { case_id: CASE_ID, favorite: false, last_opened_at: "2026-10-04T00:00:00Z" };
  }
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
        resolution: null,
      };
    }
    // 预检按**当前**环境回答：环境改了之后旧结论不再匹配。
    return {
      ...PREFLIGHT_READY,
      resolution: { ...RESOLUTION, masked_target: { url: `${environment.base_url}/echo`, method: "GET" } },
      context: {
        ...PREFLIGHT_READY.context,
        environment: { ...PREFLIGHT_READY.context.environment, base_url: environment.base_url },
      },
    };
  }
  if (method === "POST" && path.endsWith("/resolution-preview")) return RESOLUTION;
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
  // 必须短于外层 15 秒测试门槛；否则装配失败只会报测试整体超时，丢失具体 waitFor/DOM 证据。
  const SETUP_WAIT = { timeout: 5000 };

function exactButton(root: ParentNode, text: string): HTMLButtonElement {
  const matches = Array.from(root.querySelectorAll<HTMLButtonElement>("button"))
    .filter((button) => button.textContent?.trim() === text && button.closest('[hidden], [aria-hidden="true"]') === null);
  if (matches.length !== 1) throw new Error(`按钮“${text}”数量不是 1：${matches.length}`);
  const button = matches[0];
  expect(button.type).toBe("button");
  expect(button.textContent?.trim()).toBe(text);
  return button;
}

function activeEditor(): HTMLElement {
  const editor = document.querySelector<HTMLElement>(".workspace-editor:not([hidden])");
  if (editor === null) throw new Error("活动用例编辑器未挂载");
  expect(editor.getAttribute("aria-hidden")).toBe("false");
  return editor;
}

function environmentPanel(page: ParentNode): HTMLElement {
  const panel = page.querySelector<HTMLElement>("#environment-panel");
  if (panel === null) throw new Error("环境面板未挂载");
  return panel;
}

function projectVariablesPanel(page: ParentNode): HTMLElement {
  const header = Array.from(page.querySelectorAll<HTMLElement>('.ant-collapse-header[role="button"]'))
    .find((item) => item.textContent?.trim().startsWith("项目普通变量（"));
  const panel = header?.closest<HTMLElement>(".ant-collapse-item");
  if (header === undefined || panel === null || panel === undefined) throw new Error("项目普通变量面板未挂载");
  expect(header.getAttribute("aria-expanded")).toBe("true");
  return panel;
}

function labeledInput(root: ParentNode, labelText: string): HTMLInputElement {
  const labels = Array.from(root.querySelectorAll<HTMLLabelElement>("label"))
    .filter((label) => label.textContent?.trim() === labelText);
  if (labels.length !== 1) throw new Error(`标签“${labelText}”数量不是 1：${labels.length}`);
  const controlId = labels[0].htmlFor;
  const input = controlId === "" ? null : Array.from(root.querySelectorAll<HTMLElement>("[id]"))
    .find((candidate) => candidate.id === controlId);
  if (!(input instanceof HTMLInputElement)) throw new Error(`标签“${labelText}”没有关联输入框`);
  expect(input.id).toBe(controlId);
  return input;
}

/** 打开项目并选中用例，返回编辑器就绪后的容器。 */
async function openCase(): Promise<void> {
  render(<App />);
  await waitFor(() => expect(selectedValueById("scope-project")).toBe(PROJECT_ID), SETUP_WAIT);
  await act(async () => {});
  // 等侧栏（目录／用例列表）真正挂载：项目选择到位不等于环境与目录已经读完。
  const browser = await waitFor(() => {
    const current = document.querySelector<HTMLElement>('.workspace:not([hidden]) [aria-label="用例目录"]');
    if (current === null) throw new Error("用例目录未挂载");
    expect(current.getAttribute("aria-label")).toBe("用例目录");
    return current;
  }, SETUP_WAIT);
  const caseButton = await waitFor(() => {
    const button = Array.from(browser.querySelectorAll<HTMLButtonElement>(".case-list button"))
      .find((item) => item.textContent?.includes("查询订单"));
    if (button === undefined) throw new Error("查询订单入口未挂载");
    expect(button.type).toBe("button");
    expect(button.textContent).toContain("查询订单");
    return button;
  }, SETUP_WAIT);
  fireEvent.click(caseButton);
  await waitFor(
    () => {
      const input = activeEditor().querySelector<HTMLInputElement>(".case-name-input");
      if (input === null) throw new Error("活动用例名称输入框未挂载");
      const label = activeEditor().querySelector<HTMLLabelElement>(`label[for="${input.id}"]`);
      expect(label?.textContent?.trim()).toBe("用例名称");
      expect(input.value).toBe("查询订单");
    },
    SETUP_WAIT,
  );
  // 等编辑防抖触发的展示性预检真实进入 API 边界。
  //
  // “当前通过”的判据要求存在一份**针对当前内容**的预检结论——这是设计：没有它就无法
  // 说明这份报告对应的配置。因此在这里等它出现，而不是把断言放宽成“包含 200 就算通过”，
  // 后者会让“结论过期”伪装成通过。
  await waitFor(
    () => expect(calls.some((call) => call.method === "POST" && call.path.endsWith("/debug-preflight"))).toBe(true),
    SETUP_WAIT,
  );
  expect(calls.filter((call) => call.method === "POST" && call.path.endsWith(`/case-preferences/${CASE_ID}/opened`))).toHaveLength(1);
}

/**
 * 进入独立「环境配置」页，返回**包含环境面板与管理面板的页面容器**。
 *
 * 环境编辑与凭证管理同在这个折叠区里（环境在侧栏，凭证在其下的 #admin-panel）；
 * 只取 #admin-panel 会漏掉环境面板，测出来的“改环境不失效”其实是没找到控件。
 */
function environmentCollapseTrigger(panel: HTMLElement): HTMLElement {
  const trigger = Array.from(panel.querySelectorAll<HTMLElement>('.ant-collapse-header[role="button"]'))
    .find((item) => /^环境（\d+）$/.test(item.textContent?.trim() ?? ""));
  if (trigger === undefined) throw new Error("环境折叠入口未挂载");
  return trigger;
}

async function openAdmin(): Promise<HTMLElement> {
  const nav = Array.from(document.querySelectorAll<HTMLButtonElement>(".primary-nav .nav-item"))
    .find((button) => button.textContent?.trim() === "环境配置");
  if (nav === undefined) throw new Error("环境配置导航未挂载");
  expect(nav.type).toBe("button");
  fireEvent.click(nav);
  await act(async () => {});
  const panel = document.querySelector<HTMLElement>('section.content-page[aria-label="环境配置"]:not([hidden])');
  if (panel === null) throw new Error("管理入口未挂载");
  expect(panel.getAttribute("aria-label")).toBe("环境配置");
  const environmentTrigger = environmentCollapseTrigger(panel);
  if (environmentTrigger.getAttribute("aria-expanded") !== "true") {
    fireEvent.click(environmentTrigger);
    await waitFor(() => expect(environmentTrigger.getAttribute("aria-expanded")).toBe("true"));
  }
  return panel;
}

/**
 * App 会持续挂载四个页面；测试只在当前可见页面内驱动控件，避免全局可访问树把同名隐藏页
 * 一并扫描。真实浏览器同样只把未 hidden 的页面作为当前操作区域。
 */
async function activePageRegion(label: "任务中心" | "测试报告"): Promise<HTMLElement> {
  const selector = `section[aria-label="${label}"]`;
  const current = () => Array.from(document.querySelectorAll<HTMLElement>(selector))
    .find((region) => !region.hasAttribute("hidden"));
  await waitFor(() => expect(current() instanceof HTMLElement).toBe(true));
  const region = current();
  if (region === undefined) throw new Error(`当前${label}页面未挂载`);
  return region;
}

async function waitForReportHeading(region: HTMLElement, runId: string): Promise<void> {
  const expected = `运行 ${runId.slice(0, 8)} 的报告`;
  await waitFor(() => expect(
    Array.from(region.querySelectorAll("h1, h2, h3, h4, h5, h6"))
      .some((heading) => heading.textContent?.trim() === expected),
  ).toBe(true));
}

async function leaveDialog(): Promise<HTMLElement> {
  return waitFor(() => {
    const dialogs = Array.from(document.querySelectorAll<HTMLElement>('[role="dialog"]'))
      .filter((dialog) => dialog.closest('[hidden], [aria-hidden="true"]') === null && dialog.textContent?.includes("确认离开"));
    if (dialogs.length !== 1) throw new Error(`确认离开对话框数量不是 1：${dialogs.length}`);
    expect(dialogs[0].getAttribute("role")).toBe("dialog");
    expect(dialogs[0].textContent).toContain("确认离开");
    return dialogs[0];
  });
}

async function waitForRunRow(region: HTMLElement, runId: string): Promise<HTMLElement> {
  const shortId = runId.slice(0, 8);
  await waitFor(() => expect(
    Array.from(region.querySelectorAll("tr"))
      .some((row) => row.textContent?.includes(shortId)),
  ).toBe(true));
  const row = Array.from(region.querySelectorAll<HTMLElement>("tr"))
    .find((candidate) => candidate.textContent?.includes(shortId));
  if (row === undefined) throw new Error(`运行 ${shortId} 的表格行未挂载`);
  return row;
}

function viewReportButton(row: HTMLElement): HTMLButtonElement {
  const button = Array.from(row.querySelectorAll<HTMLButtonElement>("button"))
    .find((candidate) => candidate.textContent?.trim() === "查看报告");
  if (button === undefined) throw new Error("运行行没有查看报告入口");
  return button;
}

function primaryNavButton(label: "接口工作台" | "任务中心" | "测试报告"): HTMLButtonElement {
  const button = Array.from(document.querySelectorAll<HTMLButtonElement>(".primary-nav .nav-item"))
    .find((candidate) => candidate.textContent?.trim() === label);
  if (button === undefined) throw new Error(`主导航没有“${label}”入口`);
  return button;
}

function selectedValueById(id: string): string {
  const control = document.getElementById(id);
  const root = control?.closest<HTMLElement>("[data-selected-value]");
  return root?.dataset.selectedValue ?? "";
}

async function selectScopeOption(id: "scope-project" | "scope-workspace", label: "项目" | "工作空间", optionLabel: string): Promise<void> {
  const combobox = document.getElementById(id);
  if (!(combobox instanceof HTMLElement)) throw new Error(`${label}下拉框未挂载`);
  expect(combobox.getAttribute("aria-label")).toBe(label);
  expect(combobox.getAttribute("role")).toBe("combobox");
  fireEvent.mouseDown(combobox);
  const listId = combobox.getAttribute("aria-controls");
  if (listId === null) throw new Error(`${label}下拉框没有关联选项列表`);
  const option = await waitFor(() => {
    const list = document.getElementById(listId);
    if (list === null) throw new Error(`${label}下拉选项列表未挂载`);
    const popup = list.closest<HTMLElement>(".ant-select-dropdown");
    if (popup === null) throw new Error(`${label}下拉选项列表不在当前弹层中`);
    const matches = Array.from(popup.querySelectorAll<HTMLElement>(".ant-select-item-option"))
      .filter((candidate) => candidate.textContent?.trim() === optionLabel && candidate.closest('[hidden], [aria-hidden="true"]') === null);
    if (matches.length !== 1) throw new Error(`${label}选项“${optionLabel}”数量不是 1：${matches.length}`);
    return matches[0];
  });
  expect(option.classList.contains("ant-select-item-option")).toBe(true);
  fireEvent.click(option);
}

/** 发一次调试并等它显示真实结论，返回响应区。 */
async function debugOnce(): Promise<HTMLElement> {
  const editor = activeEditor();
  const send = editor.querySelector<HTMLButtonElement>('button[aria-label="发送"]');
  if (send === null) throw new Error("活动用例发送按钮未挂载");
  expect(send.type).toBe("button");
  expect(send.textContent?.trim()).toBe("发送");
  expect(send.disabled).toBe(false);
  fireEvent.click(send);
  const response = await waitFor(() => {
    const current = document.querySelector<HTMLElement>('.workspace-editor:not([hidden]) [aria-label="响应"]');
    if (current === null) throw new Error("活动编辑器响应区未挂载");
    expect(current.getAttribute("aria-label")).toBe("响应");
    expect(current.textContent).toContain("200");
    return current;
  });
  return response;
}

beforeEach(() => {
  window.history.replaceState(null, "", "#/workbench");
  calls = [];
  environment = makeEnvironment("http://echo.test");
  projectVariables = { version: 1, variables: [] };
  failWrites = false;
  preflightOverride = null;
  historyRuns = [];
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

    const page = await openAdmin();
    const admin = environmentPanel(page);
    // 真实环境编辑表单：打开编辑、改地址、保存。
    fireEvent.click(exactButton(admin, "编辑"));
    const baseInput = labeledInput(admin, "服务地址");
    fireEvent.change(baseInput, { target: { value: "http://echo-alt.test" } });
    const saveButton = exactButton(admin, "保存环境");
    await act(async () => {
      fireEvent.click(saveButton);
    });
    await waitFor(() => expect(
      calls.find((call) => call.method === "PATCH" && call.path.endsWith(`/environments/${ENV_ID}`))?.body,
    ).toMatchObject({ base_url: "http://echo-alt.test" }));

    // PATCH 一旦成功，工作台当场把旧结论标成“上一次发送”——不等环境列表刷新回来。
    await waitFor(() => expect(response.textContent).toContain("上一次发送"));
  });

  it("在真实变量面板里保存项目变量后，当前结论立即失效", async () => {
    await openCase();
    const response = await debugOnce();

    const page = await openAdmin();
    const admin = projectVariablesPanel(page);
    // 项目变量面板：新增一行并保存。
    fireEvent.click(exactButton(admin, "＋添加变量"));
    const nameInput = labeledInput(admin, "名称");
    fireEvent.change(nameInput, { target: { value: "shared" } });
    await act(async () => {
      fireEvent.click(exactButton(admin, "保存为新版本"));
    });
    await waitFor(() => expect(
      calls.find((call) => call.method === "PUT" && call.path.endsWith("/variables"))?.body,
    ).toEqual({ variables: [{ name: "shared", value: { type: "string", text: "" } }] }));

    await waitFor(() => expect(response.textContent).toContain("上一次发送"));
  });

  it("管理写失败不推进失效：结论保持可用", async () => {
    await openCase();
    const response = await debugOnce();

    const page = await openAdmin();
    const admin = environmentPanel(page);
    failWrites = true;
    fireEvent.click(exactButton(admin, "编辑"));
    const baseInput = labeledInput(admin, "服务地址");
    fireEvent.change(baseInput, { target: { value: "http://echo-alt.test" } });
    await act(async () => {
      fireEvent.click(exactButton(admin, "保存环境"));
    });
    await act(async () => {});
    expect(calls.find((call) => call.method === "PATCH")?.body).toMatchObject({ base_url: "http://echo-alt.test" });

    // 保存失败：没有发生配置变更，不该作废一份仍然成立的结论。
    expect(response.textContent).not.toContain("上一次发送");
  });

  it("只读地读取管理列表不会反复作废结论", async () => {
    await openCase();
    const response = await debugOnce();

    const page = await openAdmin();
    const admin = environmentPanel(page);
    // 展开各管理面板会触发多次 GET；读取不是变更。
    fireEvent.click(exactButton(admin, "编辑"));
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

    const editor = activeEditor();
    const entry = exactButton(editor, "前往环境设置");
    // 无效地址不能被当成“实际目标”展示。
    expect(editor.textContent).toContain("环境地址不合法，暂时无法确定");
    expect(editor.textContent).not.toContain("实际目标：target-service:8080");

    const panel = document.getElementById("environment-panel") as HTMLElement;
    const environmentTrigger = environmentCollapseTrigger(panel);
    /*
      只断言 `sidebar.open` 是不够的：外层展开后，“环境（N）”这一层仍可能是折叠的，
      用户点进来看到的还是一个没有编辑入口的空标题。这里必须断言**内层**的 open。

      为什么不断言“编辑按钮可见”：折叠内容仍可能保留 DOM；真实 Collapse 的
      `aria-expanded` 才是用户看到的展开状态。
    */
    expect(environmentTrigger.getAttribute("aria-expanded")).toBe("false");

    fireEvent.click(entry);
    await act(async () => {});

    const page = document.querySelector<HTMLElement>('section.content-page[aria-label="环境配置"]:not([hidden])');
    expect(page?.getAttribute("aria-label")).toBe("环境配置");
    expect(window.location.hash).toBe("#/environments");
    expect(environmentTrigger.getAttribute("aria-expanded")).toBe("true");

    // 进入配置页必须保住同一份未保存用例：工作台只是隐藏，编辑器没有重挂载。
    expect(labeledInput(editor, "路径").value).toBe("/echo");
  });

  it("展开后确实能走到环境编辑表单（编辑入口不再藏在折叠标题下）", async () => {
    environment = makeEnvironment("target-service:8080");
    await openCase();

    fireEvent.click(exactButton(activeEditor(), "前往环境设置"));
    await act(async () => {});

    const panel = document.getElementById("environment-panel") as HTMLElement;
    expect(environmentCollapseTrigger(panel).getAttribute("aria-expanded")).toBe("true");
    // 面板内的编辑入口真的可用：点开后能看到地址输入框。
    fireEvent.click(exactButton(panel, "编辑"));
    expect(labeledInput(panel, "服务地址")).toBeTruthy();
  });

  it("缺协议的地址在环境面板里就地挡住，不发写请求", async () => {
    environment = makeEnvironment("target-service:8080");
    await openCase();
    const entry = exactButton(activeEditor(), "前往环境设置");
    fireEvent.click(entry);
    const page = await waitFor(() => {
      const current = document.querySelector<HTMLElement>('section.content-page[aria-label="环境配置"]:not([hidden])');
      if (current === null) throw new Error("环境配置页未挂载");
      return current;
    });
    const admin = environmentPanel(page);
    await waitFor(() => expect(environmentCollapseTrigger(admin).getAttribute("aria-expanded")).toBe("true"));
    fireEvent.click(exactButton(admin, "编辑"));
    const baseInput = labeledInput(admin, "服务地址");
    // 不改地址直接保存：服务端会拒绝这条存量值，本地也应当先挡住。
    const writesBefore = calls.filter((call) => call.method === "PATCH").length;
    fireEvent.click(exactButton(admin, "保存环境"));

    await waitFor(() => expect(admin.textContent).toContain("环境地址不合法，请按提示修正后再保存。"));
    expect(calls.filter((call) => call.method === "PATCH")).toHaveLength(writesBefore);
    expect((baseInput as HTMLInputElement).value).toBe("target-service:8080");
  });

  it("把环境地址修好之后，同一份未保存草稿可以继续调试", async () => {
    environment = makeEnvironment("target-service:8080");
    await openCase();
    const editor = activeEditor();
    const entry = exactButton(editor, "前往环境设置");
    fireEvent.click(entry);

    // 走真实环境编辑表单改地址。
    const page = await waitFor(() => {
      const current = document.querySelector<HTMLElement>('section.content-page[aria-label="环境配置"]:not([hidden])');
      if (current === null) throw new Error("环境配置页未挂载");
      return current;
    });
    const admin = environmentPanel(page);
    await waitFor(() => expect(environmentCollapseTrigger(admin).getAttribute("aria-expanded")).toBe("true"));
    fireEvent.click(exactButton(admin, "编辑"));
    const baseInput = labeledInput(admin, "服务地址");
    fireEvent.change(baseInput, { target: { value: "http://echo.test" } });
    fireEvent.click(exactButton(admin, "保存环境"));
    await waitFor(() => expect(
      calls.find((call) => call.method === "PATCH" && call.path.endsWith(`/environments/${ENV_ID}`))?.body,
    ).toMatchObject({ base_url: "http://echo.test" }));

    // 配置变更由外壳广播；预检按新地址重新给结论，无效提示随之消失。
    await waitFor(() => expect(
      Array.from(editor.querySelectorAll("button")).some((button) => button.textContent?.trim() === "前往环境设置"),
    ).toBe(false));
    // 草稿没有被清掉或重载：还是原来那一份请求内容。
    expect(labeledInput(editor, "路径").value).toBe("/echo");
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

    const editor = activeEditor();
    const entry = exactButton(editor, "环境与凭证管理");
    fireEvent.click(entry);
    await act(async () => {});

    const credentials = document.getElementById("credentials-panel") as HTMLElement;
    const credentialsTrigger = Array.from(credentials.querySelectorAll<HTMLElement>('.ant-collapse-header[role="button"]'))
      .find((item) => item.textContent?.trim() === "人工凭证");
    if (credentialsTrigger === undefined) throw new Error("人工凭证折叠入口未挂载");
    expect(credentialsTrigger.getAttribute("role")).toBe("button");
    const page = document.querySelector<HTMLElement>('section.content-page[aria-label="环境配置"]:not([hidden])');
    expect(page?.getAttribute("aria-label")).toBe("环境配置");
    expect(credentialsTrigger.getAttribute("aria-expanded")).toBe("true");
    expect(document.activeElement).toBe(credentials);
    expect(labeledInput(editor, "路径").value).toBe("/echo");
  });

  it("任务报告跳转只消费一次，往返保留新选择，再点同一任务仍可跳回", async () => {
    historyRuns = [
      { ...RUN_REPORT.run, id: RUN_ID, state: "finished", outcome: "passed" },
      { ...RUN_REPORT.run, id: RUN_ID_B, state: "finished", outcome: "failed" },
    ];
    await openCase();
    const selectedEnvironment = selectedValueById("send-environment");

    fireEvent.click(primaryNavButton("任务中心"));
    const tasks = await activePageRegion("任务中心");
    const rowA = await waitForRunRow(tasks, RUN_ID);
    fireEvent.click(viewReportButton(rowA));
    const reports = await activePageRegion("测试报告");
    await waitForReportHeading(reports, RUN_ID);

    const rowB = await waitForRunRow(reports, RUN_ID_B);
    fireEvent.click(viewReportButton(rowB));
    await waitForReportHeading(reports, RUN_ID_B);

    fireEvent.click(primaryNavButton("接口工作台"));
    fireEvent.click(primaryNavButton("测试报告"));
    await waitForReportHeading(await activePageRegion("测试报告"), RUN_ID_B);

    fireEvent.click(primaryNavButton("任务中心"));
    const tasksAgain = await activePageRegion("任务中心");
    const rowAAgain = await waitForRunRow(tasksAgain, RUN_ID);
    fireEvent.click(viewReportButton(rowAAgain));
    await waitForReportHeading(await activePageRegion("测试报告"), RUN_ID);

    fireEvent.click(primaryNavButton("接口工作台"));
    expect(selectedValueById("send-environment")).toBe(selectedEnvironment);
    expect(calls.filter((call) => call.method === "POST" && call.path.endsWith("/runs"))).toHaveLength(0);
  });

  it("跨项目首帧拒绝旧报告意图，不向新项目请求旧运行", async () => {
    historyRuns = [{ ...RUN_REPORT.run, id: RUN_ID, state: "finished", outcome: "passed" }];
    await openCase();
    const editor = activeEditor();
    fireEvent.click(primaryNavButton("任务中心"));
    const tasks = await activePageRegion("任务中心");
    const rowA = await waitForRunRow(tasks, RUN_ID);
    fireEvent.click(viewReportButton(rowA));
    await waitForReportHeading(await activePageRegion("测试报告"), RUN_ID);

    // 报告页隐藏着同一个编辑器；未保存草稿仍必须拦住跨项目切换。
    fireEvent.change(labeledInput(editor, "路径"), {
      target: { value: "/changed" },
    });
    await selectScopeOption("scope-project", "项目", "项目乙");
    const firstLeave = await leaveDialog();
    fireEvent.click(exactButton(firstLeave, "取消"));
    expect(selectedValueById("scope-project")).toBe(PROJECT_ID);
    expect(labeledInput(editor, "路径").value).toBe("/changed");

    await selectScopeOption("scope-project", "项目", "项目乙");
    const secondLeave = await leaveDialog();
    fireEvent.click(exactButton(secondLeave, "继续离开"));
    await waitFor(() => expect(selectedValueById("scope-project")).toBe(PROJECT_ID_B));
    expect(document.querySelector('section[aria-label="测试报告"]')?.textContent).not.toContain(`运行 ${RUN_ID.slice(0, 8)} 的报告`);
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
    fireEvent.click(primaryNavButton("任务中心"));
    const tasks = await activePageRegion("任务中心");
    const rowA = await waitForRunRow(tasks, RUN_ID);
    fireEvent.click(viewReportButton(rowA));
    await waitForReportHeading(await activePageRegion("测试报告"), RUN_ID);

    await selectScopeOption("scope-workspace", "工作空间", "第二工作空间");
    await waitFor(() => expect(selectedValueById("scope-workspace")).toBe(WORKSPACE_ID_B));
    await waitFor(() => expect(selectedValueById("scope-project")).toBe(PROJECT_ID_B));
    expect(document.querySelector('section[aria-label="测试报告"]')?.textContent).not.toContain(`运行 ${RUN_ID.slice(0, 8)} 的报告`);
    expect(
      calls.some((call) =>
        call.method === "GET" &&
        call.path.includes(`/workspaces/${WORKSPACE_ID_B}/projects/${PROJECT_ID_B}/runs/${RUN_ID}/report`),
      ),
    ).toBe(false);
  });
});
