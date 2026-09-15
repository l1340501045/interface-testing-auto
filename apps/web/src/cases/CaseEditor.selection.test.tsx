/**
 * 运行选择的显式事件与取消目标（R3 §5／§6，R3-12／R3-13）。
 *
 * 用**真实 RunPanel**（不是替身）+ 真实 CaseEditor：要验证的正是“用户点历史里的某条运行”
 * 与“用户点本次调试记录”这两条显式路径能不能把正文、字段、断言与取消目标一起切过去。
 * 只调用 `onReport` 的替身测不出这件事——那恰恰是 R2 里被绕过去的接线。
 */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const WORKSPACE_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const CASE_ID = "44444444-4444-4444-8444-444444444444";
const ENV_ID = "55555555-5555-4555-8555-555555555555";
const DEBUG_RUN = "77777777-7777-4777-8777-777777777771";
const VERSION_RUN = "77777777-7777-4777-8777-777777777772";
const VERSION_ID = "66666666-6666-4666-8666-666666666666";
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

import { ApiError, apiGet, apiSend, apiSendWithMeta, projectPath } from "../api/client";
import { CaseEditor } from "./CaseEditor";

const apiGetMock = vi.mocked(apiGet);
const apiSendMock = vi.mocked(apiSend);
const apiSendWithMetaMock = vi.mocked(apiSendWithMeta);
void ApiError;

interface Call {
  method: string;
  path: string;
  headers?: Record<string, string> | undefined;
  body: unknown;
}

let calls: Call[] = [];
/** 让某条运行的报告读取失败（用于验证“读不到报告也要能取消已知运行”）。 */
let failReportFor: string | null = null;

const READY = {
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
  latest_version: 1,
  updated_at: "2026-09-15T00:00:00Z",
  snapshot_hash: "hash-v1",
};

function reportBody(runId: string, targetType: string, status: number, state = "finished") {
  return {
    run: {
      id: runId,
      target_type: targetType,
      case_version_id: targetType === "case_version" ? VERSION_ID : null,
      environment_id: ENV_ID,
      state,
      outcome: state === "finished" ? "passed" : null,
      reason_category: null,
      pool_id: null,
      created_at: "2026-09-15T00:00:00Z",
    },
    steps: [],
    assertions: [],
    request: null,
    response:
      state === "finished"
        ? {
            status,
            elapsed_ms: 4,
            headers: [],
            body: `{"status":${status}}`,
            body_format: "json",
            body_truncated: false,
            body_omitted_reason: null,
            size_bytes: 12,
          }
        : null,
    context:
      targetType === "debug_snapshot"
        ? {
            snapshot_fingerprint: "fp-1",
            environment: { id: ENV_ID, name: "测试环境", kind: "test", base_url: "http://echo.test" },
            input_fingerprint: "in-1",
          }
        : null,
  };
}

function route(rawPath: string, method: string, body: unknown, headers?: Record<string, string>): unknown {
  // 去掉查询串：运行列表会带 environment_id 等筛选参数。
  const path = rawPath.split("?")[0];
  calls.push({ method, path, headers, body });
  if (method === "GET" && path.endsWith("/assertion-types")) return [];
  if (method === "GET" && path.endsWith(`/cases/${CASE_ID}/versions`)) {
    return [
      {
        id: VERSION_ID,
        case_id: CASE_ID,
        version: 1,
        schema_version: 1,
        side_effect: "read",
        snapshot_hash: "hash-v1",
        created_by: USER_ID,
        created_at: "2026-09-15T00:00:00Z",
      },
    ];
  }
  if (method === "GET" && path.endsWith(`/cases/${CASE_ID}`)) return CASE_DETAIL;
  if (method === "POST" && path.endsWith("/debug-preflight")) return READY;
  if (method === "POST" && path.endsWith("/runs")) {
    return reportBody(DEBUG_RUN, "debug_snapshot", 200, "queued").run;
  }
  // 项目／环境历史：一条**尚未结束**的已发布版本运行（未结束才有取消入口）。
  if (method === "GET" && path.endsWith("/runs")) {
    const row = reportBody(VERSION_RUN, "case_version", 201, "queued").run;
    return [row];
  }
  if (method === "GET" && path.endsWith(`/runs/${DEBUG_RUN}/report`)) {
    return reportBody(DEBUG_RUN, "debug_snapshot", 200);
  }
  if (method === "GET" && path.endsWith(`/runs/${VERSION_RUN}/report`)) {
    if (failReportFor === VERSION_RUN) throw new Error("报告不可用");
    return reportBody(VERSION_RUN, "case_version", 201, "queued");
  }
  if (method === "POST" && path.endsWith("/cancel")) return { id: "x" };
  throw new Error(`测试未覆盖的请求：${method} ${path}`);
}

function renderEditor(
  projectRole: string | null = "admin",
  extra: { configEpoch?: number; getConfigEpoch?: () => number | null } = {},
) {
  return render(
    <CaseEditor
      projectRole={projectRole}
      {...extra}
      workspaceId={WORKSPACE_ID}
      projectId={PROJECT_ID}
      caseSummaryId={CASE_ID}
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
      ]}
      selectedEnvironmentId={ENV_ID}
      onSelectEnvironment={() => {}}
      onSaved={() => {}}
      onClose={() => {}}
      folders={[]}
      currentUserId={USER_ID}
    />,
  );
}

async function renderLoaded(
  projectRole: string | null = "admin",
  extra: { configEpoch?: number; getConfigEpoch?: () => number | null } = {},
) {
  const view = renderEditor(projectRole, extra);
  await screen.findByLabelText("用例名称");
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 450));
  });
  await act(async () => {});
  return view;
}

function responseRegion(): HTMLElement {
  return screen.getByRole("region", { name: "响应" });
}

beforeEach(() => {
  calls = [];
  failReportFor = null;
  apiGetMock.mockReset();
  apiSendMock.mockReset();
  apiSendWithMetaMock.mockReset();
  apiGetMock.mockImplementation((async (path: string) => route(path, "GET", undefined)) as never);
  apiSendMock.mockImplementation((async (
    path: string,
    method: string,
    body: unknown,
    _parse: unknown,
    options?: { headers?: Record<string, string> },
  ) => route(path, method, body, options?.headers)) as never);
  apiSendWithMetaMock.mockImplementation((async (path: string, method: string, body: unknown) => ({
    data: await route(path, method, body),
    etag: `"3"`,
  })) as never);
});

describe("R3-12 真实 RunPanel 的显式选择", () => {
  it("本次调试 → 项目历史版本 → 回到本次调试：正文与取消目标三次一致", async () => {
    await renderLoaded();

    // 本次调试：受理后正文显示 200。
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(responseRegion().textContent).toContain("200"));
    expect(responseRegion().textContent).toContain(DEBUG_RUN.slice(0, 8));

    // 项目／环境历史里的那条版本运行（真实 RunPanel 渲染的列表）。
    const history = screen.getByRole("heading", { name: "已发布版本执行与项目历史" }).closest(".block");
    expect(history).not.toBeNull();
    await waitFor(() =>
      expect(within(history as HTMLElement).getByText(VERSION_RUN.slice(0, 8))).toBeTruthy(),
    );
    fireEvent.click(within(history as HTMLElement).getByRole("button", { name: "查看报告" }));
    await waitFor(() =>
      expect(responseRegion().textContent).toContain(VERSION_RUN.slice(0, 8)),
    );
    // 切过去之后显示的是**那条运行**的状态（报告到了才显示），而不是上一次调试的完成结论。
    // 报告还没到时标题只给出已知编号，不补造状态——因此这里等它到达再断言。
    await waitFor(() => expect(responseRegion().textContent).toContain("排队中"));
    expect(responseRegion().textContent).not.toContain("已结束");

    // 回到本次调试记录：来源与正文一起切回来。
    fireEvent.click(screen.getByRole("button", { name: DEBUG_RUN.slice(0, 8) }));
    await waitFor(() => expect(responseRegion().textContent).toContain("200"));
    expect(responseRegion().textContent).toContain(DEBUG_RUN.slice(0, 8));
    expect(responseRegion().textContent).not.toContain("排队中");
  });

  it("后台到达的版本报告不抢走当前选择", async () => {
    await renderLoaded();
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(responseRegion().textContent).toContain("200"));

    // 历史列表刷新（后台读取）不改变来源：报告到达只更新缓存。
    const history = screen.getByRole("heading", { name: "已发布版本执行与项目历史" }).closest(".block");
    fireEvent.click(within(history as HTMLElement).getByRole("button", { name: "刷新运行列表" }));
    await act(async () => {});

    expect(responseRegion().textContent).toContain(DEBUG_RUN.slice(0, 8));
    expect(responseRegion().textContent).toContain("200");
  });
});

describe("R5 作用域失效时不发起版本运行", () => {
  it("同步时钟返回 null：零运行 POST，并给出可操作提示", async () => {
    // 时钟返回 null 表示当前范围已经失效（切了项目／主体）。此时**不能**退回 props 里的
    // 旧世代继续提交：那会给一条按旧配置跑的运行登记出“当前配置”的依据，旧结论于是又
    // 匹配上当前。两种“没有值”必须分开——getter 不存在才回退。
    await renderLoaded("admin", { configEpoch: 5, getConfigEpoch: () => null });

    const history = screen.getByRole("heading", { name: "已发布版本执行与项目历史" }).closest(".block");
    fireEvent.click(within(history as HTMLElement).getByRole("button", { name: "保存并执行" }));
    await act(async () => {});

    expect(
      calls.filter((c) => c.method === "POST" && c.path === projectPath(WORKSPACE_ID, PROJECT_ID, "/runs")),
    ).toEqual([]);
    expect(await screen.findByText(/当前范围或执行配置已变化/)).toBeTruthy();
  });

  it("getter 不存在时仍按 props 的世代提交：回退分支没有被误删", async () => {
    await renderLoaded("admin", { configEpoch: 5 });

    const history = screen.getByRole("heading", { name: "已发布版本执行与项目历史" }).closest(".block");
    fireEvent.click(within(history as HTMLElement).getByRole("button", { name: "保存并执行" }));
    await waitFor(() =>
      expect(
        calls.filter(
          (c) => c.method === "POST" && c.path === projectPath(WORKSPACE_ID, PROJECT_ID, "/runs"),
        ),
      ).toHaveLength(1),
    );
  });
});

describe("R4 F10 查看者得不到取消入口", () => {
  it("查看者看未结束的运行：能读报告，但没有取消按钮，也不会发出取消请求", async () => {
    // 权限守卫要在界面这一层就把动作去掉，而不是让用户点下去收一个 403——那是把一个
    // 权限事实伪装成一次失败操作。
    await renderLoaded("viewer");
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    // 查看者的发送入口本就不可用（只读），这里直接选历史里那条未结束的运行。
    const history = screen.getByRole("heading", { name: "已发布版本执行与项目历史" }).closest(".block");
    await waitFor(() =>
      expect(within(history as HTMLElement).getByText(VERSION_RUN.slice(0, 8))).toBeTruthy(),
    );
    fireEvent.click(within(history as HTMLElement).getByRole("button", { name: "查看报告" }));
    await waitFor(() =>
      expect(responseRegion().textContent).toContain(VERSION_RUN.slice(0, 8)),
    );

    // 报告可读，但没有取消动作，并说明原因。
    expect(within(responseRegion()).queryByRole("button", { name: /取消/ })).toBeNull();
    expect(responseRegion().textContent).toContain("不能取消");
    expect(
      calls.filter((c) => c.method === "POST" && c.path.endsWith("/cancel")),
    ).toEqual([]);
  });

  it("编辑者仍能取消：不因权限守卫回退", async () => {
    await renderLoaded("editor");
    const history = screen.getByRole("heading", { name: "已发布版本执行与项目历史" }).closest(".block");
    await waitFor(() =>
      expect(within(history as HTMLElement).getByText(VERSION_RUN.slice(0, 8))).toBeTruthy(),
    );
    fireEvent.click(within(history as HTMLElement).getByRole("button", { name: "查看报告" }));
    await waitFor(() => expect(responseRegion().textContent).toContain(VERSION_RUN.slice(0, 8)));

    fireEvent.click(within(responseRegion()).getByRole("button", { name: /取消/ }));
    await waitFor(() =>
      expect(
        calls.filter(
          (c) =>
            c.method === "POST" &&
            c.path === projectPath(WORKSPACE_ID, PROJECT_ID, `/runs/${VERSION_RUN}/cancel`),
        ),
      ).toHaveLength(1),
    );
  });
});

describe("R3-13 取消目标来自明确的 run_id", () => {
  it("报告读失败时仍能取消那条已知的运行（r2），而不是回落到 r1", async () => {
    // r1 已经缓存了一份通过的 201。随后选中历史里的 r2，而 r2 的报告读取失败：
    // 取消入口必须按**已知 run_id** 提供，不能因为报告没读回来就消失，也不能变成取消 r1。
    await renderLoaded();
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(responseRegion().textContent).toContain("200"));
    expect(responseRegion().textContent).toContain(DEBUG_RUN.slice(0, 8));

    // 历史里的 r2 尚未结束，且它的报告读取失败。
    failReportFor = VERSION_RUN;
    const history = screen.getByRole("heading", { name: "已发布版本执行与项目历史" }).closest(".block");
    await waitFor(() =>
      expect(within(history as HTMLElement).getByText(VERSION_RUN.slice(0, 8))).toBeTruthy(),
    );
    fireEvent.click(within(history as HTMLElement).getByRole("button", { name: "查看报告" }));
    await act(async () => {});

    // 旧报告（r1 的 200）不得冒充当前选择的正文。
    expect(responseRegion().textContent).not.toContain("200");
    // 报告读失败也有取消入口。
    const cancel = within(responseRegion()).getByRole("button", { name: /取消/ });
    fireEvent.click(cancel);
    await waitFor(() =>
      expect(
        calls.filter(
          (c) =>
            c.method === "POST" &&
            c.path === projectPath(WORKSPACE_ID, PROJECT_ID, `/runs/${VERSION_RUN}/cancel`),
        ),
      ).toHaveLength(1),
    );
  });
});
