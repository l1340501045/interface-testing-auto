/**
 * 最小管理页面的行为：角色门槛、加载失败、空态与保存成功。
 *
 * 这些用例盯的是“界面上有没有给人越权的入口”和“保存真的发出去了什么”，
 * 不重复服务端的权限与校验逻辑：角色判定仍在服务端，这里只验证前端不会
 * 替查看者发出一条本来就不该发出的写请求。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, apiSend: vi.fn(), apiDelete: vi.fn() };
});

import { ApiError, apiSend } from "../api/client";
import type { Environment } from "../api/types";
import { AppProviders } from "../theme/AppProviders";
import { selectAntOption } from "../test/antd";
import { AdminPanel } from "./AdminPanel";

const WS = "11111111-1111-4111-8111-111111111111";
const PROJECT = "22222222-2222-4222-8222-222222222222";
const ENV_ID = "33333333-3333-4333-8333-333333333333";
const POOL_ID = "44444444-4444-4444-8444-444444444444";
const SECRET_ID = "55555555-5555-4555-8555-555555555555";

const apiSendMock = vi.mocked(apiSend);

interface Call {
  path: string;
  method: string;
  body: unknown;
}

let calls: Call[] = [];

function url(suffix: string): string {
  return `/workspaces/${WS}/projects/${PROJECT}${suffix}`;
}

/** 每个用例只声明自己关心的路由；未声明的请求直接失败，避免悄悄漏测。 */
function router(handlers: Record<string, unknown | Error>) {
  return (async (path: string, method: string, body: unknown, parse: (raw: unknown) => unknown) => {
    calls.push({ path, method, body });
    const handler = handlers[`${method} ${path}`];
    if (handler === undefined) throw new Error(`测试未覆盖的请求：${method} ${path}`);
    if (handler instanceof Error) throw handler;
    return parse(handler);
  }) as never;
}

function pool(allowedTargets: string[]): unknown {
  return {
    id: POOL_ID,
    name: "默认执行池",
    status: "active",
    network_zone: "internal",
    allowed_targets: allowedTargets,
    grant_id: "66666666-6666-4666-8666-666666666666",
    grant_status: "active",
    granted_at: "2026-09-14T00:00:00+00:00",
    environment_ids: [ENV_ID],
  };
}

const environments: Environment[] = [
  {
    id: ENV_ID,
    name: "本地测试环境",
    kind: "test",
    base_url: "http://target-service:8080",
    pool_id: POOL_ID,
    variables: {},
    status: "active",
  },
];

const EMPTY_VARIABLES = { version: 0, variables: [] };
const CURRENT_USER_ID = "88888888-8888-4888-8888-888888888888";
/** 成员名单与用例目录是授权表单的选择器来源：界面不再接受手抄的 id。 */
const EMPTY_CREDENTIALS = {
  [`GET ${url("/credentials/secrets")}`]: [],
  [`GET ${url("/credentials/profiles")}`]: [],
  [`GET ${url("/credentials/grants")}`]: [],
  [`GET /workspaces/${WS}/members`]: [
    { user_id: CURRENT_USER_ID, username: "demo", display_name: "演示账号", role: "admin" },
  ],
  [`GET ${url("/cases")}`]: [],
};

/** 执行配置变更通知的 spy：面板自己决定哪些动作算“成功变更”，App 负责推进时钟。 */
const onExecutionConfigChanged = vi.fn();

function renderPanel(role: string) {
  return render(
    <AdminPanel
      workspaceId={WS}
      projectId={PROJECT}
      role={role}
      environments={environments}
      currentUser={{ user_id: CURRENT_USER_ID, display_name: "演示账号" }}
      currentCase={null}
      onExecutionConfigChanged={onExecutionConfigChanged}
    />,
    { wrapper: AppProviders },
  );
}

describe("管理页面", () => {
  beforeEach(() => {
    calls = [];
    apiSendMock.mockReset();
  });

  it("查看者没有保存入口，也不会替它发出任何凭证请求", async () => {
    apiSendMock.mockImplementation(
      router({
        [`GET ${url("/variables")}`]: EMPTY_VARIABLES,
        [`GET ${url("/pools")}`]: [pool(["http://echo:8080"])],
      }),
    );
    renderPanel("viewer");

    expect(await screen.findByText("项目还没有普通变量。")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "保存为新版本" })).toBeNull();
    expect(screen.queryByRole("button", { name: "保存白名单" })).toBeNull();
    expect(screen.getByText("查看者不能扩大出网范围；白名单维护需要管理员权限。")).toBeTruthy();
    expect(
      screen.getByText("秘密、身份配置与用途授权都需要管理员权限；当前角色只能查看环境与用例。"),
    ).toBeTruthy();
    expect(calls.some((call) => call.path.includes("/credentials"))).toBe(false);
  });

  it("执行池白名单按行解析后整份替换，并说明这次保存没有访问目标", async () => {
    apiSendMock.mockImplementation(
      router({
        [`GET ${url("/variables")}`]: EMPTY_VARIABLES,
        [`GET ${url("/pools")}`]: [pool(["http://echo:8080"])],
        // 保存接口按契约只回**一个**执行池（RunnerPoolOut）。这里必须照服务端的
        // 真实形状返回：若写成数组，界面按单个对象解析的错配就再也测不出来了。
        [`PUT ${url(`/pools/${POOL_ID}/targets`)}`]: pool(["http://a.test:80", "https://b.test:443"]),
        ...EMPTY_CREDENTIALS,
      }),
    );
    renderPanel("admin");

    const box = await screen.findByLabelText("允许的目标（每行一个，格式 scheme://host:port）");
    // 空行是手工编辑的常见残留，必须被丢掉而不是当成一个空目标提交。
    fireEvent.change(box, { target: { value: "http://a.test\n\n  https://b.test  \n" } });
    fireEvent.click(screen.getByRole("button", { name: "保存白名单" }));

    await screen.findByText("已保存「默认执行池」的目标白名单；本次保存没有访问任何目标。");
    const put = calls.find((call) => call.method === "PUT");
    expect(put?.body).toEqual({ allowed_targets: ["http://a.test", "https://b.test"] });
    // 保存成功后输入框按服务端返回值回填规范化后的目标，不保留用户敲进去的空行。
    await waitFor(() =>
      expect((screen.getByLabelText("允许的目标（每行一个，格式 scheme://host:port）") as HTMLTextAreaElement).value)
        .toBe("http://a.test:80\nhttps://b.test:443"),
    );
  });

  it("清空白名单在本地就被挡住，不会发出收窄范围之外的空配置", async () => {
    apiSendMock.mockImplementation(
      router({
        [`GET ${url("/variables")}`]: EMPTY_VARIABLES,
        [`GET ${url("/pools")}`]: [pool(["http://echo:8080"])],
        ...EMPTY_CREDENTIALS,
      }),
    );
    renderPanel("admin");

    const box = await screen.findByLabelText("允许的目标（每行一个，格式 scheme://host:port）");
    fireEvent.change(box, { target: { value: "   \n  " } });
    fireEvent.click(screen.getByRole("button", { name: "保存白名单" }));

    await screen.findByText("白名单至少要保留一个目标；清空不是收窄范围，而是配置错误。");
    expect(calls.filter((call) => call.method === "PUT")).toHaveLength(0);
  });

  it("执行池读取失败时如实显示服务端给的原因", async () => {
    apiSendMock.mockImplementation(
      router({
        [`GET ${url("/variables")}`]: EMPTY_VARIABLES,
        [`GET ${url("/pools")}`]: new ApiError(
          403,
          "insufficient_role",
          "当前角色没有执行池管理权限",
          null,
        ),
        ...EMPTY_CREDENTIALS,
      }),
    );
    renderPanel("admin");

    expect(await screen.findByText("当前角色没有执行池管理权限")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "保存白名单" })).toBeNull();
  });

  it("项目普通变量按新版本保存，并显示落库后的版本号", async () => {
    apiSendMock.mockImplementation(
      router({
        [`GET ${url("/variables")}`]: EMPTY_VARIABLES,
        [`GET ${url("/pools")}`]: [],
        [`PUT ${url("/variables")}`]: {
          version: 3,
          variables: [{ name: "base_url", value: { type: "string", text: "http://echo:8080" } }],
        },
        ...EMPTY_CREDENTIALS,
      }),
    );
    renderPanel("admin");

    await screen.findByText("项目还没有普通变量。");
    fireEvent.click(screen.getByRole("button", { name: "＋添加变量" }));
    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "base_url" } });
    fireEvent.change(screen.getByLabelText("值"), { target: { value: "http://echo:8080" } });
    fireEvent.click(screen.getByRole("button", { name: "保存为新版本" }));

    await screen.findByText("已保存为第 3 版；历史版本仍可查回。");
    const put = calls.find((call) => call.method === "PUT");
    // 数字与文本都按字面量文本承载，这里确认字符串没有被就地转成别的类型。
    expect(put?.body).toEqual({
      variables: [{ name: "base_url", value: { type: "string", text: "http://echo:8080" } }],
    });
  });

  it("保存秘密后不回显明文，输入框立即清空", async () => {
    apiSendMock.mockImplementation(
      router({
        [`GET ${url("/variables")}`]: EMPTY_VARIABLES,
        [`GET ${url("/pools")}`]: [],
        [`POST ${url("/credentials/secrets")}`]: {
          id: SECRET_ID,
          name: "svc-token",
          kind: "manual",
          latest_version: 1,
          latest_version_id: "77777777-7777-4777-8777-777777777777",
        },
        ...EMPTY_CREDENTIALS,
      }),
    );
    renderPanel("admin");

    const value = (await screen.findByLabelText("秘密值（保存后不再显示）")) as HTMLInputElement;
    fireEvent.change(screen.getByLabelText("秘密名称"), { target: { value: "svc-token" } });
    fireEvent.change(value, { target: { value: "s3cr3t-value" } });
    fireEvent.click(screen.getByRole("button", { name: "保存秘密" }));

    await screen.findByText("已保存秘密「svc-token」；值不会再次显示。");
    await waitFor(() => expect(value.value).toBe(""));
  });

  it("还没有凭证时给出空态而不是空白区块", async () => {
    apiSendMock.mockImplementation(
      router({
        [`GET ${url("/variables")}`]: EMPTY_VARIABLES,
        [`GET ${url("/pools")}`]: [],
        ...EMPTY_CREDENTIALS,
      }),
    );
    renderPanel("admin");

    expect(await screen.findByText("还没有人工登记的秘密。")).toBeTruthy();
    expect(screen.getByText("还没有身份配置；测试环境需要认证时再创建。")).toBeTruthy();
    expect(screen.getByText("还没有用途授权；没有授权时执行不会带上凭证。")).toBeTruthy();
  });

  it("切换项目后身份配置提交的是当前项目的环境，不是上一个项目的环境", async () => {
    // 环境下拉会带出默认选择。切换项目后旧 id 已不在新列表里，下拉回退显示第一个
    // 环境（DOM 的 value 也跟着变成它），但组件里留着的还是旧 id：只看界面会以为
    // 选的是当前项目环境，提交上去的却是上一个项目的环境，服务端只能报“环境不存在
    // 或不属于本项目”。这里盯的是真正发出去的请求体。
    const otherEnvironment: Environment = {
      id: "99999999-9999-4999-8999-999999999999",
      name: "另一个项目的环境",
      kind: "test",
      base_url: "http://echo:8080",
      pool_id: POOL_ID,
      variables: {},
      status: "active",
    };
    const OTHER_PROFILE = {
      id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      environment_id: otherEnvironment.id,
      name: "闭环验收身份",
      provider: "static",
      status: "available",
      current_epoch: 0,
      current_set_id: null,
      profile_version_id: null,
      allowed_targets: [],
      allowed_auth_slots: [],
      slot_count: 0,
    };
    apiSendMock.mockImplementation(
      router({
        [`GET ${url("/variables")}`]: EMPTY_VARIABLES,
        [`GET ${url("/pools")}`]: [],
        [`POST ${url("/credentials/profiles")}`]: OTHER_PROFILE,
        ...EMPTY_CREDENTIALS,
      }),
    );
    const view = renderPanel("admin");
    await screen.findByLabelText("环境");

    view.rerender(
      <AdminPanel
        workspaceId={WS}
        projectId={PROJECT}
        role="admin"
        environments={[otherEnvironment]}
        currentUser={{ user_id: CURRENT_USER_ID, display_name: "演示账号" }}
        currentCase={null}
          onExecutionConfigChanged={onExecutionConfigChanged}
      />,
    );
    fireEvent.change(screen.getByLabelText("身份名称"), { target: { value: "闭环验收身份" } });
    // 认证位置由表单生成：这里只选方案、填头名，配置 JSON 由界面拼（V5）。
    await selectAntOption("认证方式", "请求头（原样值）");
    fireEvent.change(screen.getByLabelText("请求头名"), { target: { value: "X-Demo-Token" } });
    fireEvent.click(screen.getByRole("button", { name: "创建身份配置" }));

    await screen.findByText("身份配置已创建，认证位置与失效判据一并保存为首个版本；接着绑定秘密即可。");
    // 同一路径既有列表 GET 也有创建 POST，必须按方法取，否则会取到没有请求体的列表调用。
    const created = calls.find(
      (call) => call.method === "POST" && call.path === url("/credentials/profiles"),
    );
    expect(created?.body).toMatchObject({
      environment_id: otherEnvironment.id,
      allowed_auth_slots: ["header.X-Demo-Token"],
      // 配置快照只能来自表单生成的声明，不存在“用户手写 JSON”这条路径。
      config: {
        auth_locations: [{ slot: "header.X-Demo-Token", scheme: "raw_header", prefix: "" }],
        invalidation: { status_codes: [401], redirect_to_login: false },
      },
    });
  });

  it("认证位置没填名称时本地就挡住，不发注定被执行内核拒绝的创建请求", async () => {
    // 名称为空的位置生成不出槽位（header.），身份建出来却没有任何可注入的位置，绑定秘密
    // 后执行必然被拒，而界面没有事后扩大的入口。这里确认界面不会先建出一个用不了的身份，
    // 再把问题留到运行时。
    apiSendMock.mockImplementation(
      router({
        [`GET ${url("/variables")}`]: EMPTY_VARIABLES,
        [`GET ${url("/pools")}`]: [],
        ...EMPTY_CREDENTIALS,
      }),
    );
    renderPanel("admin");

    await screen.findByLabelText("环境");
    fireEvent.change(screen.getByLabelText("身份名称"), { target: { value: "闭环验收身份" } });
    // “请求头（原样值）”没有默认头名，正是最容易漏填的一档。
    await selectAntOption("认证方式", "请求头（原样值）");
    fireEvent.click(screen.getByRole("button", { name: "创建身份配置" }));

    await screen.findByText("认证位置的头名或参数名不能为空。");
    expect(calls.filter((call) => call.method === "POST")).toHaveLength(0);
  });

  it("用途授权区说明变量变化需要重新授权", async () => {
    // 授权按签发时的变量冻结输入：改变量等于换请求，原授权会被拒绝。这条规则用户
    // 只能从界面知道，界面不说就等于执行时突然报错而无人能解释。
    apiSendMock.mockImplementation(
      router({
        [`GET ${url("/variables")}`]: EMPTY_VARIABLES,
        [`GET ${url("/pools")}`]: [],
        ...EMPTY_CREDENTIALS,
      }),
    );
    renderPanel("admin");

    expect(await screen.findByText(/并同时冻结当时生效的项目／环境普通变量/)).toBeTruthy();
    expect(screen.getByText(/需要在新的变量下重新签发授权/)).toBeTruthy();
  });
});
