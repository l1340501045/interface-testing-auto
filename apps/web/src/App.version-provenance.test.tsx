/**
 * 版本运行“当前结论”的执行配置依据（R4 §2）。
 *
 * 走**真实 App**：真实外壳 → 真实 RunPanel（真实受理）→ 真实管理面板 → 真实 CaseEditor。
 *
 * 要验证的规则是：历史列表里的报告只能说明“这条运行跑过”，不能证明它按**当前**配置跑过。
 * 因此“当前字段结论”要求一份可证明的依据——由**真实受理**记下的 run_id、环境与提交时的
 * 配置世代。任何一次执行配置变更（环境地址、项目变量、身份凭证）都会让这份依据失效；
 * 报告仍可完整查看，但不再贴当前通过。在新配置下重新运行后结论恢复。
 *
 * 这是 R4 明确要求的覆盖：现有 App 测试没有凭证绑定／撤销与版本结果这两部分。
 */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const WORKSPACE_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const CASE_ID = "44444444-4444-4444-8444-444444444444";
const ENV_ID = "55555555-5555-4555-8555-555555555555";
const VERSION_ID = "66666666-6666-4666-8666-666666666666";
const RUN_V1 = "77777777-7777-4777-8777-777777777771";
const RUN_V2 = "77777777-7777-4777-8777-777777777772";
const PROFILE_ID = "88888888-8888-4888-8888-888888888888";
const SECRET_ID = "99999999-9999-4999-8999-999999999991";
const GRANT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const USER_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

vi.mock("./session/useSession", () => ({
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

vi.mock("./api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api/client")>();
  return { ...actual, apiGet: vi.fn(), apiSend: vi.fn(), apiSendWithMeta: vi.fn(), apiDelete: vi.fn() };
});

import { apiDelete, apiGet, apiSend, apiSendWithMeta } from "./api/client";
import { App } from "./App";

const apiGetMock = vi.mocked(apiGet);
const apiSendMock = vi.mocked(apiSend);
const apiSendWithMetaMock = vi.mocked(apiSendWithMeta);
const apiDeleteMock = vi.mocked(apiDelete);

const SNAPSHOT_HASH = "hash-v1";
/** 每次受理分配一个新的运行 id，便于区分“重新运行”。 */
let runSeq = 0;

function environment() {
  return {
    id: ENV_ID,
    name: "测试环境",
    kind: "test",
    base_url: envBaseUrl,
    pool_id: null,
    variables: {},
    status: "active",
  };
}
let envBaseUrl = "http://echo.test";
let variables = { version: 1, variables: [] as { name: string; value: unknown }[] };

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
  assertions: [
    {
      id: "assert-1",
      target_source: "response.status",
      selector: [],
      type: "status_in",
      parameters: { values: [{ type: "number", text: "200" }] },
      compare_as: null,
      severity: "error",
      enabled: true,
      sort_order: 0,
    },
  ],
  rev: 3,
  status: "draft",
  latest_version: 1,
  updated_at: "2026-09-15T00:00:00Z",
  snapshot_hash: SNAPSHOT_HASH,
};

function versionReport(runId: string) {
  return {
    run: {
      id: runId,
      target_type: "case_version",
      case_version_id: VERSION_ID,
      environment_id: ENV_ID,
      state: "finished",
      outcome: "passed",
      reason_category: null,
      pool_id: null,
      created_at: "2026-09-15T00:00:00Z",
    },
    steps: [],
    assertions: [
      {
        assertion_id: "assert-1",
        type: "status_in",
        phase: "response",
        target: { target_source: "response.status", selector: [] },
        status: "passed",
        expected: [{ type: "number", text: "200" }],
        actual: { type: "number", text: "200" },
        reason_code: null,
        elapsed_ms: 2,
      },
    ],
    request: null,
    response: {
      status: 200,
      elapsed_ms: 3,
      headers: [],
      body: "{}",
      body_format: "json",
      body_truncated: false,
      body_omitted_reason: null,
      size_bytes: 2,
    },
    context: null,
  };
}

let lastRunId = RUN_V1;

const calls: string[] = [];
/**
 * 挂起 `POST /runs` 的应答：用例据此制造“提交已发出、202 还没回来”的窗口。
 *
 * 这正是需要固定的时序——受理响应要等一次网络往返，期间配置可能已经被改。等响应回来
 * 才读配置，会把按旧配置跑的运行记成按新配置跑的。
 */
const runGate: { pending: (() => void) | null } = { pending: null };
function holdNextRun(): void {
  runGate.pending = () => undefined;
}
function releaseRun(value: unknown): void {
  const pending = runGate.pending;
  runGate.pending = null;
  pending?.();
  void value;
}

function route(rawPath: string, method: string, body?: unknown): unknown {
  const path = rawPath.split("?")[0];
  calls.push(`${method} ${path.replace(/^\/api\/v1\/workspaces\/[0-9a-f-]+\/projects\/[0-9a-f-]+/, "")}`);
  if (method === "GET" && path.endsWith("/projects")) {
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
  if (method === "GET" && path.endsWith("/environments")) return [environment()];
  if (method === "GET" && path.endsWith("/folders")) return [];
  if (method === "GET" && path.endsWith("/cases")) {
    return [{ id: CASE_ID, folder_id: null, name: "查询订单", method: "GET", status: "draft", rev: 3, latest_version: 1 }];
  }
  if (method === "GET" && path.endsWith("/assertion-types")) {
    return [
      {
        id: "status_in",
        label: "属于",
        group: "HTTP 状态",
        applies_to: ["integer", "number", "string"],
        params_schema: {
          values: { control: "value_list", type: "number", label: "允许的状态码" },
        },
        summary: "属于这些值之一",
        operator_version: 1,
      },
    ];
  }
  if (method === "GET" && path.endsWith("/variables")) return variables;
  if (method === "GET" && path.endsWith("/members")) {
    return [{ user_id: USER_ID, username: "tester", display_name: "测试员", role: "admin" }];
  }
  if (method === "GET" && path.endsWith("/pools")) return [];
  if (method === "GET" && path.endsWith("/credentials/secrets")) {
    return [{ id: SECRET_ID, name: "演示令牌", kind: "static", latest_version: 2, latest_version_id: "sv-2" }];
  }
  if (method === "GET" && path.includes("/credentials/secrets/") && path.endsWith("/versions")) {
    return [
      { secret_id: SECRET_ID, version_id: "sv-1", version: 1 },
      { secret_id: SECRET_ID, version_id: "sv-2", version: 2 },
    ];
  }
  if (method === "GET" && path.endsWith("/credentials/profiles")) {
    return [
      {
        id: PROFILE_ID,
        environment_id: ENV_ID,
        name: "演示身份",
        provider: "manual_credential",
        status: "available",
        current_epoch: 1,
        current_set_id: "set-1",
        profile_version_id: "pv-1",
        allowed_targets: ["http://echo.test"],
        allowed_auth_slots: ["header.Authorization"],
        slot_count: 1,
      },
    ];
  }
  if (method === "GET" && path.endsWith(`/credentials/profiles/${PROFILE_ID}/set`)) {
    return {
      profile_id: PROFILE_ID,
      set_id: "set-1",
      epoch: 1,
      status: "active",
      expires_at: null,
      slots: [
        {
          auth_slot: "header.Authorization",
          secret_id: SECRET_ID,
          secret_name: "演示令牌",
          secret_version_id: "sv-1",
          secret_version: 1,
        },
      ],
    };
  }
  if (method === "GET" && path.endsWith("/credentials/grants")) {
    return [
      {
        id: GRANT_ID,
        profile_id: PROFILE_ID,
        environment_id: ENV_ID,
        grant_type: "case_version",
        principal_id: USER_ID,
        case_version_id: VERSION_ID,
        debug_snapshot_hash: null,
        allowed_targets: [],
        allowed_auth_slots: [],
        status: "active",
        expires_at: null,
        used_at: null,
      },
    ];
  }
  if (method === "GET" && path.endsWith("/runs")) return [];
  if (method === "GET" && path.endsWith("/report")) return versionReport(lastRunId);
  if (method === "GET" && path.endsWith(`/cases/${CASE_ID}/versions`)) {
    return [
      {
        id: VERSION_ID,
        case_id: CASE_ID,
        version: 1,
        schema_version: 1,
        side_effect: "read",
        snapshot_hash: SNAPSHOT_HASH,
        created_by: USER_ID,
        created_at: "2026-09-15T00:00:00Z",
      },
    ];
  }
  if (method === "GET" && path.endsWith(`/cases/${CASE_ID}`)) return CASE_DETAIL;
  if (method === "POST" && path.endsWith("/debug-preflight")) {
    return {
      ready: true,
      issues: [],
      can_authorize: true,
      auth: { required: false, state: "none", profile_id: null },
      context: {
        snapshot_fingerprint: "fp-1",
        environment: { id: ENV_ID, name: "测试环境", kind: "test", base_url: envBaseUrl },
        input_fingerprint: "in-1",
      },
    };
  }
  if (method === "POST" && path.endsWith("/runs")) {
    runSeq += 1;
    lastRunId = runSeq === 1 ? RUN_V1 : RUN_V2;
    const payload = { ...versionReport(lastRunId).run, state: "queued", outcome: null };
    if (runGate.pending !== null) {
      return new Promise((resolve) => {
        const gate = runGate;
        gate.pending = () => resolve(payload);
      });
    }
    return payload;
  }
  if (method === "PATCH" && path.endsWith(`/environments/${ENV_ID}`)) {
    const patch = (body ?? {}) as { base_url?: string };
    if (typeof patch.base_url === "string") envBaseUrl = patch.base_url;
    return environment();
  }
  if (method === "PUT" && path.endsWith("/variables")) {
    const payload = (body ?? {}) as { variables?: { name: string; value: unknown }[] };
    variables = { version: variables.version + 1, variables: payload.variables ?? [] };
    return variables;
  }
  if (method === "PUT" && path.endsWith(`/credentials/profiles/${PROFILE_ID}/set`)) {
    return {
      id: PROFILE_ID,
      environment_id: ENV_ID,
      name: "演示身份",
      provider: "manual_credential",
      status: "available",
      current_epoch: 2,
      current_set_id: "set-2",
      profile_version_id: "pv-1",
      allowed_targets: ["http://echo.test"],
      allowed_auth_slots: ["header.Authorization"],
      slot_count: 1,
    };
  }
  if (method === "DELETE" && path.includes("/credentials/grants/")) return undefined;
  throw new Error(`测试未覆盖的请求：${method} ${path}`);
}

  // 装配阶段的等待预算：这里等的是“用例编辑器挂载完成”，不是某个断言内容。
  // 整套测试并行跑十几个 jsdom 环境，CPU 争用会把同一段代码的墙上时间放大数倍
  // （与 test/setup.ts 记录的同一现象）。断言内容不因此放宽：期望的元素一字未改。
  const SETUP_WAIT = { timeout: 20000 };

/** 打开项目与用例，返回编辑器容器。 */
async function openCase(): Promise<void> {
  render(<App />);
  const projectSelect = (await screen.findByLabelText("项目")) as HTMLSelectElement;
  await waitFor(() => expect(projectSelect.value).toBe(PROJECT_ID), SETUP_WAIT);
  await act(async () => {});
  const browser = await screen.findByLabelText("用例目录");
  fireEvent.click(await within(browser).findByRole("button", { name: /查询订单/ }));
  await waitFor(
    () => expect((screen.getByLabelText("用例名称") as HTMLInputElement).value).toBe("查询订单"),
    SETUP_WAIT,
  );
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 450));
  });
  await act(async () => {});
}

/** 执行已发布版本（真实 RunPanel 的「保存并执行」），返回提交次数。 */
async function runVersion(): Promise<void> {
  const history = screen.getByRole("heading", { name: "执行已发布版本" }).closest(".block");
  fireEvent.click(within(history as HTMLElement).getByRole("button", { name: "保存并执行" }));
  await act(async () => {});
}

function assertionColumnText(): string {
  fireEvent.click(screen.getByRole("tab", { name: /^断言/ }));
  return (document.querySelector(".assertion-column") as HTMLElement).textContent ?? "";
}

async function expectCurrentPassed(): Promise<void> {
  try {
    await waitFor(() => expect(assertionColumnText()).toContain("通过"));
  } catch (cause) {
    // eslint-disable-next-line no-console
    console.log("DIAG-HIST", JSON.stringify((screen.getByRole("heading", { name: "执行已发布版本" }).closest(".block") as HTMLElement).textContent?.slice(0, 300)), "| calls:", JSON.stringify(calls.slice(-6)));
    throw cause;
  }
}

async function expectNotCurrent(): Promise<void> {
  await waitFor(() => {
    const text = assertionColumnText();
    expect(text).toContain("未执行");
    expect(text).not.toContain("通过");
  });
}

function admin(): HTMLElement {
  const panel = screen.queryByRole("region", { name: "环境配置" });
  if (panel === null) throw new Error("管理入口未挂载");
  return panel as HTMLElement;
}

async function openAdmin(): Promise<HTMLElement> {
  fireEvent.click(screen.getByRole("button", { name: "环境配置" }));
  await act(async () => {});
  return admin();
}

function returnWorkbench(): void {
  fireEvent.click(screen.getByRole("button", { name: "接口工作台" }));
}

beforeEach(() => {
  window.history.replaceState(null, "", "#/workbench");
  runSeq = 0;
  calls.length = 0;
  runGate.pending = null;
  lastRunId = RUN_V1;
  envBaseUrl = "http://echo.test";
  variables = { version: 1, variables: [] };
  apiGetMock.mockReset();
  apiSendMock.mockReset();
  apiSendWithMetaMock.mockReset();
  apiDeleteMock.mockReset();
  apiGetMock.mockImplementation((async (path: string) => route(path, "GET")) as never);
  // 替身必须**应用解析函数**：真实 `apiSend` 返回的是 `parse(body)`，跳过它会让调用方
  // 拿到原始对象而不是契约类型（例如 `toRunId` 应得字符串，跳过则拿到整个对象）。
  // `route` 可能返回被挂起的 Promise：先 await 再交给解析函数，否则 `parse` 拿到的是
  // 一个 Promise 而不是响应体（被挂起的 202 会因此被解析成 null）。
  apiSendMock.mockImplementation((async (
    path: string,
    method: string,
    body: unknown,
    parse: (raw: unknown) => unknown,
  ) => parse(await route(path, method, body))) as never);
  apiSendWithMetaMock.mockImplementation((async (
    path: string,
    method: string,
    body: unknown,
    parse: (raw: unknown) => unknown,
  ) => ({
    data: parse(route(path, method, body)),
    etag: `"3"`,
  })) as never);
  apiDeleteMock.mockImplementation((async (path: string) => route(path, "DELETE")) as never);
});

describe("R5 版本配置依据在 POST 前固定", () => {
  it("提交已发出、配置在 202 返回前被改动：旧运行不得贴当前通过", async () => {
    await openCase();

    // 发起版本执行，但让 202 一直不回来。
    holdNextRun();
    const history = screen.getByRole("heading", { name: "执行已发布版本" }).closest(".block");
    fireEvent.click(within(history as HTMLElement).getByRole("button", { name: "保存并执行" }));
    await act(async () => {});
    expect(runGate.pending).not.toBeNull();

    // 202 还没到，配置保存成功（真实管理入口）→ 配置世代推进。
    const panel = await openAdmin();
    fireEvent.click(within(panel).getByRole("button", { name: "编辑" }));
    const baseInput = await within(panel).findByLabelText("服务地址");
    fireEvent.change(baseInput, { target: { value: "http://echo-alt.test" } });
    await act(async () => {
      fireEvent.click(within(panel).getByRole("button", { name: "保存环境" }));
    });
    returnWorkbench();

    // 现在放行 202。
    await act(async () => {
      releaseRun(null);
    });
    await act(async () => {});

    // 这次运行是按**旧**配置提交的：不得因为响应迟到而被登记成新配置的依据。
    // 报告本身仍可查看。
    const response = screen.getByRole("region", { name: "响应" });
    await waitFor(() => expect(response.textContent).toContain("已结束"));
    await waitFor(() => {
      const text = assertionColumnText();
      expect(text).toContain("未执行");
      expect(text).not.toContain("通过");
    });
  });

  it("新配置下明确重新执行后：当前结论恢复", async () => {
    await openCase();
    await runVersion();
    await expectCurrentPassed();

    // 改配置 → 失效。
    const panel = await openAdmin();
    fireEvent.click(within(panel).getByRole("button", { name: "编辑" }));
    const baseInput = await within(panel).findByLabelText("服务地址");
    fireEvent.change(baseInput, { target: { value: "http://echo-alt.test" } });
    await act(async () => {
      fireEvent.click(within(panel).getByRole("button", { name: "保存环境" }));
    });
    returnWorkbench();
    await expectNotCurrent();

    await runVersion();
    await expectCurrentPassed();
  });
});

describe("版本运行当前结论的执行配置依据", () => {
  it("本次受理并仍在同一配置下：字段行显示那一轮的结论", async () => {
    await openCase();
    await runVersion();
    await expectCurrentPassed();
  });

  it("改环境地址后：当前结论失效，报告仍完整可读", async () => {
    await openCase();
    await runVersion();
    await expectCurrentPassed();

    const panel = await openAdmin();
    fireEvent.click(within(panel).getByRole("button", { name: "编辑" }));
    const baseInput = await within(panel).findByLabelText("服务地址");
    fireEvent.change(baseInput, { target: { value: "http://echo-alt.test" } });
    await act(async () => {
      fireEvent.click(within(panel).getByRole("button", { name: "保存环境" }));
    });
    returnWorkbench();

    await expectNotCurrent();
    // 旧报告仍可读：响应区照常显示那条运行的终态与结果。
    const response = screen.getByRole("region", { name: "响应" });
    expect(response.textContent).toContain("已结束");
  });

  it("改项目变量后：当前结论失效", async () => {
    await openCase();
    await runVersion();
    await expectCurrentPassed();

    const panel = await openAdmin();
    fireEvent.click(within(panel).getByRole("button", { name: "＋添加变量" }));
    const nameInput = await within(panel).findByLabelText("名称");
    fireEvent.change(nameInput, { target: { value: "shared" } });
    await act(async () => {
      fireEvent.click(within(panel).getByRole("button", { name: "保存为新版本" }));
    });
    returnWorkbench();

    await expectNotCurrent();
  });

  it("保存整份凭证绑定集合后：当前结论失效", async () => {
    // 整份集合切换改变请求实际注入的凭证，与身份配置同等重要；它走的是**自己的**成功
    // 出口（不经过表单的 submit 包装），因此单独验证。
    await openCase();
    await runVersion();
    await expectCurrentPassed();

    const panel = await openAdmin();
    // 版本下拉的可访问名是 `aria-label="秘密版本"`（旁边那句可见的「版本」标签的 htmlFor
    // 指向一个并不存在的 id，因此不构成可访问名——那是既有缺陷，已记入剩余项）。
    const versionSelect = (await within(panel).findByLabelText("秘密版本")) as HTMLSelectElement;
    fireEvent.change(versionSelect, { target: { value: "sv-2" } });
    await act(async () => {
      fireEvent.click(within(panel).getByRole("button", { name: "保存整份绑定集合" }));
    });
    returnWorkbench();

    await expectNotCurrent();
  });

  it("撤销用途授权后：当前结论失效", async () => {
    // 撤销走 apiDelete，是另一条成功出口。
    await openCase();
    await runVersion();
    await expectCurrentPassed();

    const panel = await openAdmin();
    await act(async () => {
      fireEvent.click(within(panel).getByRole("button", { name: "撤销授权" }));
    });
    returnWorkbench();

    await expectNotCurrent();
  });

  it("新配置下重新运行后：当前结论恢复", async () => {
    await openCase();
    await runVersion();
    await expectCurrentPassed();

    // 改配置 → 失效。
    const panel = await openAdmin();
    fireEvent.click(within(panel).getByRole("button", { name: "编辑" }));
    const baseInput = await within(panel).findByLabelText("服务地址");
    fireEvent.change(baseInput, { target: { value: "http://echo-alt.test" } });
    await act(async () => {
      fireEvent.click(within(panel).getByRole("button", { name: "保存环境" }));
    });
    returnWorkbench();
    await expectNotCurrent();

    // 在新配置下重新运行 → 依据重新建立（新 run_id、新配置世代）。
    await runVersion();
    await expectCurrentPassed();
  });
});
