/**
 * 请求调试工作台的前端行为：发送入口、标签切换、预检提示与响应归属。
 *
 * 对应 DW-01（未保存也能发送）、DW-02（只提交一次、不串保存／发布）、DW-03（缺环境／
 * 待授权的可操作原因）、DW-04（标签切换不丢表单状态）、DW-05/06（响应与来源归属）。
 *
 * 这里不替换 RunPanel：要验证的正是点下「发送」之后真正发出去的那些请求，以及
 * 调试与“保存并执行”两条路互不干扰。
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const WORKSPACE_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const CASE_ID = "44444444-4444-4444-8444-444444444444";
const ENV_ID = "55555555-5555-4555-8555-555555555555";
const RUN_ID = "77777777-7777-4777-8777-777777777777";

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

import { apiGet, apiSend, apiSendWithMeta, projectPath } from "../api/client";
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
/** 预检结果：由用例决定“可以发送”“缺授权”还是“环境歧义”。 */
let preflightBody: unknown = {
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
let reportBody: unknown = null;

const CASE_DETAIL = {
  id: CASE_ID,
  folder_id: null,
  name: "查询订单",
  request: {
    method: "GET",
    path: "/echo",
    query_params: [{ name: "tag", value: "a" }],
    headers: [],
    body_type: "none" as const,
    body: "",
  },
  assertions: [],
  rev: 3,
  status: "draft",
  latest_version: null,
  updated_at: "2026-09-14T00:00:00Z",
  snapshot_hash: "hash-draft",
};

const TYPES: unknown[] = [];

function route(method: string, path: string, body: unknown, headers?: Record<string, string>): unknown {
  calls.push({ method, path, body, headers });
  if (method === "GET" && path.endsWith("/assertion-types")) return TYPES;
  if (method === "GET" && path.endsWith(`/cases/${CASE_ID}/versions`)) return [];
  if (method === "GET" && path.endsWith(`/cases/${CASE_ID}`)) return CASE_DETAIL;
  if (method === "GET" && path.includes("/runs/") && path.endsWith("/report")) return reportBody;
  if (method === "GET" && path.includes("/runs")) return [];
  if (method === "POST" && path.endsWith("/imports/curl/preview")) {
    // 与真实预览接口同形：draft / sendable / warnings / unsupported / auth_hint。
    // 含未知选项的命令按“无法保证等价”拒绝导入（sendable=false），草稿不被改写。
    const text = (body as { text?: string } | undefined)?.text ?? "";
    if (text.includes("--bogus")) {
      return {
        draft: CASE_DETAIL.request,
        sendable: false,
        warnings: [],
        unsupported: ["选项 --bogus 会改变发送行为，首版不建模；无法保证等价。"],
        auth_hint: null,
      };
    }
    return {
      draft: { ...CASE_DETAIL.request, path: "/echo" },
      sendable: true,
      warnings: ["已识别 1 个请求头。"],
      unsupported: [],
      auth_hint: null,
    };
  }
  if (method === "POST" && path.endsWith("/debug-preflight")) return preflightBody;
  if (method === "POST" && path.endsWith("/runs")) return { id: RUN_ID, target_type: "debug_snapshot", case_version_id: null, environment_id: ENV_ID, state: "queued", outcome: null, reason_category: null, pool_id: null, created_at: "2026-09-14T00:00:00Z" };
  // 替身绕过 parse：这里直接返回解析后的值（服务端返回的是 {"hash": ...}，
  // 组件用 toDebugSnapshotDigest 取 hash，因此替身要给出字符串）。
  if (method === "POST" && path.endsWith("/debug-snapshot-digest")) return "digest-1";
  if (method === "POST" && path.endsWith("/credentials/grants")) return { id: "grant-1" };
  throw new Error(`测试未覆盖的请求：${method} ${path}`);
}

function renderEditor(props: { projectRole?: string | null } = {}) {
  return render(
    <CaseEditor
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
      currentUserId="u-1"
      {...props}
    />,
  );
}

async function renderLoaded(props: { projectRole?: string | null } = {}) {
  renderEditor(props);
  await waitFor(() =>
    expect((screen.getByLabelText("用例名称") as HTMLInputElement).value).toBe("查询订单"),
  );
}

function callsTo(path: string, method: string): Call[] {
  return calls.filter((call) => call.method === method && call.path === path);
}

beforeEach(() => {
  calls = [];
  reportBody = null;
  preflightBody = {
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
  apiSendWithMetaMock.mockImplementation((async (path: string, method: string, body: unknown) => ({
    data: route(method, path, body),
    etag: `"3"`,
  })) as never);
});

describe("请求调试工作台", () => {
  it("未保存的用例也能直接发送当前内容，且不触发保存或发布", async () => {
    // DW-01／DW-02：导入或新建之后，地址行旁的发送入口直接提交临时快照。
    await renderLoaded();
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() =>
      expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/runs"), "POST")).toHaveLength(1),
    );
    const sent = callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/runs"), "POST")[0];
    // 提交的是 debug_snapshot，而不是 case_version_id：调试不落用例版本。
    expect(sent.body).toMatchObject({
      environment_id: ENV_ID,
      debug_snapshot: { request: { method: "GET", path: "/echo" }, assertions: [] },
    });
    // 完全没有保存或发布请求：调试与保存是两条路。
    expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, `/cases/${CASE_ID}`), "PATCH")).toEqual([]);
    expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, `/cases/${CASE_ID}/publish`), "POST")).toEqual([]);
  });

  it("每次发送都带幂等键，双击不会产生两次受理", async () => {
    // 同一个写请求在目标上产生两次副作用是最难收拾的一类问题，因此两次点击只允许一次
    // 受理；幂等键必须存在，服务端才有依据把重复请求收敛到同一次运行。
    await renderLoaded();
    const button = screen.getByRole("button", { name: "发送" });
    fireEvent.click(button);
    fireEvent.click(button);

    await waitFor(() =>
      expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/runs"), "POST").length).toBeGreaterThan(0),
    );
    const posts = callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/runs"), "POST");
    expect(posts).toHaveLength(1);
    expect(posts[0].headers?.["Idempotency-Key"]).toBeTruthy();
  });

  it("缺少可用授权时给出原因与确认入口，确认前不签发也不发送", async () => {
    // DW-03／DW-05：需要授权时页面必须说清缺什么、该找谁，而不是一个不可点击的按钮。
    //
    // R2 之后这里多一层要求（F5）：授权是一次真实的凭证使用决定，**不自动签发**。
    // 点击发送只是停下并展开确认面板；只有明确的「授权并发送」才会去算摘要、签发并提交。
    preflightBody = {
      ready: false,
      issues: [
        {
          code: "credential_not_granted",
          message: "当前身份未获授权使用该环境的凭证。",
          action: "authorize",
        },
      ],
      can_authorize: true,
      auth: { required: false, state: "needs_authorization", profile_id: "profile-1" },
      context: null,
    };
    await renderLoaded();
    await screen.findByText(/当前身份未获授权使用该环境的凭证/);

    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    const panel = await screen.findByRole("region", { name: "本次授权确认" });
    // 确认前：没有任何写请求。
    expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/debug-snapshot-digest"), "POST")).toEqual([]);
    expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/credentials/grants"), "POST")).toEqual([]);
    expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/runs"), "POST")).toEqual([]);

    // 确认后：服务端算摘要 → 签发给本人 → 提交同一份内容，用户不接触内部摘要。
    fireEvent.click(within(panel).getByRole("button", { name: "授权并发送" }));
    await waitFor(() =>
      expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/credentials/grants"), "POST")).toHaveLength(1),
    );
    await waitFor(() =>
      expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/runs"), "POST")).toHaveLength(1),
    );
    const grant = callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/credentials/grants"), "POST")[0];
    expect(grant.body).toMatchObject({ debug_snapshot_hash: "digest-1", grant_type: "debug_snapshot" });
  });

  it("不能管理凭证的成员只得到指引，不出现自助提权", async () => {
    preflightBody = {
      ready: false,
      issues: [
        {
          code: "credential_not_granted",
          message: "当前身份未获授权使用该环境的凭证。",
          action: "contact_admin",
        },
      ],
      can_authorize: false,
      auth: { required: false, state: "needs_authorization", profile_id: null },
      context: null,
    };
    await renderLoaded();
    await screen.findByText(/当前身份未获授权使用该环境的凭证/);

    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await screen.findByText(/请联系身份管理员/);
    expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/credentials/grants"), "POST")).toEqual([]);
    expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/runs"), "POST")).toEqual([]);
  });

  it("标签切换不卸载面板：切走再回来，输入还在", async () => {
    // DW-04：切标签看一眼请求头再回来，正文不该被重置。
    await renderLoaded();
    const pathInput = screen.getByLabelText("路径") as HTMLInputElement;
    fireEvent.change(pathInput, { target: { value: "/orders" } });

    fireEvent.click(screen.getByRole("tab", { name: /请求头/ }));
    expect(screen.getByRole("tab", { name: /请求头/ }).getAttribute("aria-selected")).toBe("true");
    fireEvent.click(screen.getByRole("tab", { name: /参数/ }));

    expect((screen.getByLabelText("路径") as HTMLInputElement).value).toBe("/orders");
    expect((screen.getByLabelText("查询参数名称 1") as HTMLInputElement).value).toBe("tag");
  });

  it("方法、路径与执行环境各只有一处输入，不出现重复表单", async () => {
    // 曾经有 request-path-inline 与 request-path、case-environment 与 send-environment
    // 两组控件绑同一个字段：改一个另一个不动，用户看到的是“打了字没生效”。地址行是
    // 这三项唯一的一组输入。
    await renderLoaded();
    expect(document.querySelectorAll("#request-path")).toHaveLength(1);
    expect(document.querySelectorAll("#request-method")).toHaveLength(1);
    expect(document.querySelectorAll("#send-environment")).toHaveLength(1);
    // 旧的第二份控件不再存在。
    expect(document.getElementById("request-path-inline")).toBeNull();
    expect(document.getElementById("case-environment")).toBeNull();

    // 改一次路径，预览与提交都用这一份。
    fireEvent.change(screen.getByLabelText("路径"), { target: { value: "/orders" } });
    expect(screen.getByText(/实际目标：http:\/\/echo\.test\/orders/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() =>
      expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/runs"), "POST")).toHaveLength(1),
    );
    const sent = callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/runs"), "POST")[0];
    const body = sent.body as { debug_snapshot: { request: { path: string } } };
    expect(body.debug_snapshot.request.path).toBe("/orders");
  });

  it("导入 cURL 在地址行工具栏里可达，解析成功只填入编辑器", async () => {
    // 它原先排在响应与样例长表单之后，首屏找不到——而导入是新请求的第一步。
    await renderLoaded();
    const toolbar = document.querySelector(".send-bar");
    expect(toolbar).not.toBeNull();
    const importButton = within(toolbar as HTMLElement).getByRole("button", { name: "导入 cURL" });

    fireEvent.click(importButton);
    const textarea = within(toolbar as HTMLElement).getByLabelText(/粘贴 cURL 命令/);
    fireEvent.change(textarea, { target: { value: "curl http://echo.test/echo" } });
    fireEvent.click(within(toolbar as HTMLElement).getByRole("button", { name: "解析并填入编辑器" }));

    await waitFor(() => expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/imports/curl/preview"), "POST")).toHaveLength(1));
    // 导入不发被测请求。
    expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/runs"), "POST")).toEqual([]);
    // 成功后面板收起，注意力回到请求区。
    await waitFor(() =>
      expect(within(toolbar as HTMLElement).getByRole("button", { name: "导入 cURL" })).toBeTruthy(),
    );
  });

  it("导入解析失败时保留原文，便于改一处再试", async () => {
    await renderLoaded();
    const toolbar = document.querySelector(".send-bar") as HTMLElement;
    fireEvent.click(within(toolbar).getByRole("button", { name: "导入 cURL" }));
    const textarea = within(toolbar).getByLabelText(/粘贴 cURL 命令/) as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "curl --bogus http://echo.test/echo" } });
    fireEvent.click(within(toolbar).getByRole("button", { name: "解析并填入编辑器" }));

    await screen.findByText(/命令含有暂不支持的能力|导入失败/);
    // 原文还在：用户可以就地改，不用重新粘贴。
    expect((within(toolbar).getByLabelText(/粘贴 cURL 命令/) as HTMLTextAreaElement).value).toBe(
      "curl --bogus http://echo.test/echo",
    );
    expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/runs"), "POST")).toEqual([]);
  });

  it("响应区在空状态也有自己的标签，样例与预期字段归入「字段与断言」", async () => {
    // 空状态也要让用户看出这里会有哪些内容；样例／预期字段是长表单，收进标签而非常驻。
    await renderLoaded();
    const response = screen.getByRole("region", { name: "响应" });
    const tabs = within(response).getByRole("tablist", { name: "响应" });
    for (const label of ["正文", "字段与断言", "响应头"]) {
      expect(within(tabs).getByRole("tab", { name: new RegExp(`^${label}`) })).toBeTruthy();
    }
    // 默认停在正文，且给的是空态而不是伪造的数值。
    expect(within(response).getByText(/发送后，这里显示服务端返回的真实状态码/)).toBeTruthy();
    expect(within(response).queryByText("状态码")).toBeNull();

    // 样例与预期字段只在「字段与断言」标签里出现，正文标签下没有它。
    expect(within(response).queryByText(/先粘贴一份响应样例/)).toBeNull();
    fireEvent.click(within(tabs).getByRole("tab", { name: /^字段与断言/ }));
    expect(within(response).getByText(/先粘贴一份响应样例/)).toBeTruthy();
  });

  it("响应区在收到报告前保留标题，且不伪造状态码", async () => {
    // DW-05：响应标题常驻在请求区下方，等待中不补造 200 与耗时。
    await renderLoaded();
    const response = screen.getByRole("region", { name: "响应" });
    expect(response).toBeTruthy();
    // 标题就在响应区里，且此时没有任何数值指标可显示。
    expect(within(response).getByRole("heading", { name: "响应" })).toBeTruthy();
    expect(within(response).queryByText("状态码")).toBeNull();
    expect(within(response).getByText(/还没有本次调试记录/)).toBeTruthy();
  });

  it("响应区紧跟在请求标签之后，不落在长表单末尾", async () => {
    // DW-08：请求区是标签式的紧凑工具区，响应标题必须紧接其后。原先固定响应断言、
    // 出参样例与预期字段常驻在请求区外面，把响应一路推到长表单末尾，首屏看不到。
    await renderLoaded();
    const tabs = screen.getByRole("tablist", { name: "请求编辑" });
    const response = screen.getByRole("region", { name: "响应" });
    const following =
      tabs.compareDocumentPosition(response) & Node.DOCUMENT_POSITION_FOLLOWING;
    expect(following).toBeTruthy();

    // 那些长表单确实在标签**内部**：切到断言标签能看到固定响应断言，
    // 而它并不位于标签区与响应区之间的常驻位置。
    fireEvent.click(screen.getByRole("tab", { name: /^断言/ }));
    const fixed = screen.getByText("固定响应断言");
    const tabsRoot = tabs.closest(".request-tabs");
    expect(tabsRoot).not.toBeNull();
    expect(tabsRoot?.contains(fixed)).toBe(true);
  });

  it("只读角色没有发送入口，并说明原因", async () => {
    await renderLoaded({ projectRole: "viewer" });
    expect(screen.queryByRole("button", { name: "发送" })).toBeTruthy();
    const send = screen.getByRole("button", { name: "发送" }) as HTMLButtonElement;
    expect(send.disabled).toBe(true);
    expect(screen.getByText(/当前项目角色是查看者/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "保存草稿" })).toBeTruthy();
    expect((screen.getByRole("button", { name: "保存草稿" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("认证标签说明身份按环境继承，并可标记必须认证", async () => {
    await renderLoaded();
    fireEvent.click(screen.getByRole("tab", { name: /认证/ }));
    expect(screen.getByText(/身份按环境继承/)).toBeTruthy();

    const checkbox = screen.getByRole("checkbox", { name: /必须使用环境登录态/ }) as HTMLInputElement;
    fireEvent.click(checkbox);
    await waitFor(() =>
      expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/runs"), "POST").length).toBe(0),
    );
    // 标记进入请求定义：发送时应当带上 auth_required，执行期据此拒绝匿名降级。
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() =>
      expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/runs"), "POST")).toHaveLength(1),
    );
    const sent = callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/runs"), "POST")[0];
    const body = sent.body as { debug_snapshot: { request: Record<string, unknown> } };
    expect(body.debug_snapshot.request.auth_required).toBe(true);
  });

  it("报告来源与当前内容不一致时，字段结论标为未执行但不丢报告", async () => {
    // DW-06：编辑或切环境后旧报告仍可查看，但不得冒充当前配置的通过。
    reportBody = {
      run: {
        id: RUN_ID,
        target_type: "debug_snapshot",
        case_version_id: null,
        environment_id: ENV_ID,
        state: "finished",
        outcome: "passed",
        reason_category: null,
        pool_id: null,
        created_at: "2026-09-14T00:00:00Z",
      },
      steps: [],
      assertions: [
        {
          assertion_id: "assert-1",
          type: "status_in",
          phase: "response",
          target: { target_source: "response.status", selector: [] },
          status: "passed",
          expected: null,
          actual: null,
          reason_code: null,
          elapsed_ms: 3,
        },
      ],
      request: null,
      response: { status: 200, elapsed_ms: 12, headers: [], body: "{\"ok\":true}", body_format: "json", body_truncated: false, body_omitted_reason: null, size_bytes: 11 },
      // 与当前内容的预检标记不同：这份报告来自别的内容。
      context: {
        snapshot_fingerprint: "fp-other",
        environment: { id: ENV_ID, name: "测试环境", kind: "test", base_url: "http://echo.test" },
        input_fingerprint: "in-other",
      },
    };
    await renderLoaded();
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() =>
      expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/runs"), "POST")).toHaveLength(1),
    );
    await screen.findByText(/上一次发送/);
    const response = screen.getByRole("region", { name: "响应" });
    // 真实响应照实显示，但明确标注它不是当前内容的结论。
    expect(within(response).getByText("200")).toBeTruthy();
    expect(within(response).getByText(/下面显示的是/)).toBeTruthy();
    expect(within(response).getByText(/不会贴用这次结论/)).toBeTruthy();
  });
});
