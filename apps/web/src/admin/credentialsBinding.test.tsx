/**
 * 凭证绑定的整份提交（V6）与“授权只看选择器”（V5）。
 *
 * 这一组用例盯的是服务端契约与界面之间最容易错位的地方：
 * - 切换当前凭证集合是**整份替换**，只提交改动过的那一个槽位，等于把其他槽位删掉；
 * - “读取绑定元数据”返回的是槽位与版本号，不含值；界面也不该把值渲染出来；
 * - 表单基于的 epoch 与当前 epoch 不一致时必须走冲突提示，而不是让旧表单覆盖新集合。
 *
 * 断言只使用槽位、版本号与响应里的非敏感字段：秘密是否真的注入由受控服务在集成
 * 层验证，这里不比秘密本身。
 */
import { act, render, screen, waitFor, within } from "@testing-library/react";
import { fireEvent } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, apiSend: vi.fn(), apiDelete: vi.fn() };
});

import { ApiError, apiSend } from "../api/client";
import { toCredentialSet } from "../api/guards";
import type { Environment } from "../api/types";
import { LeaveGuardProvider } from "../hooks/leaveGuard";
import { CredentialsPanel } from "./CredentialsPanel";
const WS = "11111111-1111-4111-8111-111111111111";
const PROJECT = "22222222-2222-4222-8222-222222222222";
const ENV_ID = "33333333-3333-4333-8333-333333333333";
const PROFILE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const TOKEN_SECRET = "55555555-5555-4555-8555-555555555555";
const COOKIE_SECRET = "66666666-6666-4666-8666-666666666666";
const APP_SECRET = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";
const TOKEN_V1 = "77777777-7777-4777-8777-777777777777";
const TOKEN_V2 = "88888888-8888-4888-8888-888888888888";
const COOKIE_V1 = "99999999-9999-4999-8999-999999999999";
const APP_V1 = "ffffffff-ffff-4fff-8fff-ffffffffffff";
const USER_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

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

/**
 * 请求替身。
 *
 * 处理器除了固定响应与 `Error`，还可以给一个**无参函数**：有些场景要验证的正是
 * “同一个请求在第二次读到时返回了不同的内容”（发布之后的那一版、保存之后前进的
 * epoch），固定值没法表达。
 */
function router(handlers: Record<string, unknown | Error | (() => unknown)>) {
  return (async (path: string, method: string, body: unknown, parse: (raw: unknown) => unknown) => {
    calls.push({ path, method, body });
    const handler = handlers[`${method} ${path}`];
    if (handler === undefined) throw new Error(`测试未覆盖的请求：${method} ${path}`);
    const value = typeof handler === "function" ? (handler as () => unknown)() : handler;
    if (value instanceof Error) throw value;
    return parse(value);
  }) as never;
}

const environments: Environment[] = [
  {
    id: ENV_ID,
    name: "本地测试环境",
    kind: "test",
    base_url: "http://target-service:8080",
    pool_id: null,
    variables: {},
    status: "active",
  },
];

function profile(allowedSlots: string[], currentEpoch: number): unknown {
  return {
    id: PROFILE_ID,
    environment_id: ENV_ID,
    name: "闭环验收身份",
    provider: "static",
    status: "available",
    current_epoch: currentEpoch,
    current_set_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    profile_version_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
    allowed_targets: [],
    allowed_auth_slots: allowedSlots,
    slot_count: allowedSlots.length,
  };
}

/** 两个槽位各绑一个秘密版本；响应里额外塞入 value 字段模拟“服务端多回了一个值”。 */
function credentialSet(epoch: number, extra: Record<string, unknown> = {}): unknown {
  return {
    profile_id: PROFILE_ID,
    set_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    epoch,
    status: "active",
    expires_at: null,
    slots: [
      {
        auth_slot: "header.Authorization",
        secret_id: TOKEN_SECRET,
        secret_name: "闭环验收令牌",
        secret_version_id: TOKEN_V1,
        secret_version: 2,
        ...extra,
      },
      {
        auth_slot: "header.Cookie",
        secret_id: COOKIE_SECRET,
        secret_name: "闭环验收身份",
        secret_version_id: COOKIE_V1,
        secret_version: 1,
        ...extra,
      },
    ],
  };
}

const BOTH_SLOTS = ["header.Authorization", "header.Cookie", "query.access_token"];

const OTHER_USER_ID = "12121212-1212-4212-8212-121212121212";
const CASE_ID = "13131313-1313-4313-8313-131313131313";
const VERSION_3 = "14141414-1414-4414-8414-141414141414";
const VERSION_2 = "15151515-1515-4515-8515-151515151515";

/** 用例版本列表：界面上只能按“用例名 / 第几版 / 发布时间”选。 */
const CASE = {
  id: CASE_ID,
  folder_id: null,
  name: "闭环验收-认证GET",
  method: "GET",
  status: "draft",
  rev: 7,
  latest_version: 3,
};

const CASE_VERSIONS = [
  {
    id: VERSION_3,
    case_id: CASE_ID,
    version: 3,
    schema_version: 1,
    side_effect: "read_only",
    snapshot_hash: "hash-3",
    created_by: USER_ID,
    created_at: "2026-09-14T08:30:00+00:00",
  },
  {
    id: VERSION_2,
    case_id: CASE_ID,
    version: 2,
    schema_version: 1,
    side_effect: "read_only",
    snapshot_hash: "hash-2",
    created_by: USER_ID,
    created_at: "2026-09-13T08:30:00+00:00",
  },
];

const MEMBERS = [
  { user_id: USER_ID, username: "demo", display_name: "演示账号", role: "admin" },
  { user_id: OTHER_USER_ID, username: "peer", display_name: "另一位同事", role: "editor" },
];

function baseHandlers(setOverride: Record<string, unknown | Error> = {}) {
  return {
    [`GET ${url("/variables")}`]: { version: 0, variables: [] },
    [`GET ${url("/pools")}`]: [],
    [`GET ${url("/cases")}`]: [],
    [`GET /workspaces/${WS}/members`]: [
      { user_id: USER_ID, username: "demo", display_name: "演示账号", role: "admin" },
    ],
    [`GET ${url("/credentials/secrets")}`]: [
      {
        id: TOKEN_SECRET,
        name: "闭环验收令牌",
        kind: "manual",
        latest_version: 2,
        latest_version_id: TOKEN_V2,
      },
      {
        id: COOKIE_SECRET,
        name: "闭环验收身份",
        kind: "manual",
        latest_version: 1,
        latest_version_id: COOKIE_V1,
      },
      {
        id: APP_SECRET,
        name: "闭环验收应用凭据",
        kind: "manual",
        latest_version: 1,
        latest_version_id: APP_V1,
      },
    ],
    [`GET ${url("/credentials/profiles")}`]: [profile(BOTH_SLOTS, 4)],
    [`GET ${url("/credentials/grants")}`]: [],
    [`GET ${url(`/credentials/secrets/${TOKEN_SECRET}/versions`)}`]: [
      { secret_id: TOKEN_SECRET, version_id: TOKEN_V1, version: 2 },
      { secret_id: TOKEN_SECRET, version_id: TOKEN_V2, version: 1 },
    ],
    [`GET ${url(`/credentials/secrets/${COOKIE_SECRET}/versions`)}`]: [
      { secret_id: COOKIE_SECRET, version_id: COOKIE_V1, version: 1 },
    ],
    [`GET ${url(`/credentials/secrets/${APP_SECRET}/versions`)}`]: [
      { secret_id: APP_SECRET, version_id: APP_V1, version: 1 },
    ],
    ...setOverride,
  };
}

type CurrentCase = { caseId: string; versionId: string | null } | null;

/**
 * 面板的组件树。
 *
 * 单独拿出来是为了让用例能**换一份 `currentCase` 重新渲染**：编辑器发布成功后，
 * 外壳就是这样把“当前打开的用例改成了哪一版”告诉面板的。
 */
function panelTree(currentCase: CurrentCase = null) {
  return (
    <LeaveGuardProvider>
      <CredentialsPanel
        workspaceId={WS}
        projectId={PROJECT}
        environments={environments}
        canAdmin
        currentUser={{ user_id: USER_ID, display_name: "演示账号" }}
        currentCase={currentCase}
      />
    </LeaveGuardProvider>
  );
}

function renderPanel(currentCase: CurrentCase = null) {
  return render(panelTree(currentCase));
}

describe("凭证绑定的整份提交", () => {
  beforeEach(() => {
    calls = [];
    apiSendMock.mockReset();
  });

  it("替换一个槽位的版本时，另一个槽位随整份集合一起提交", async () => {
    const updated = profile(BOTH_SLOTS, 5);
    apiSendMock.mockImplementation(
      router(
        baseHandlers({
          [`GET ${url(`/credentials/profiles/${PROFILE_ID}/set`)}`]: credentialSet(4),
          [`PUT ${url(`/credentials/profiles/${PROFILE_ID}/set`)}`]: updated,
        }),
      ),
    );
    renderPanel();

    // 集合读回来后两行都在：没有“只显示本次要改的那一个槽位”的入口。
    await waitFor(() => expect(screen.getAllByLabelText("秘密版本")).toHaveLength(2));
    // 版本列表没到之前选择框是禁用的；等它真的能选，避免在“选项还不存在”时白点一下。
    await waitFor(() => {
      const rows = screen.getAllByLabelText("秘密版本") as HTMLSelectElement[];
      expect(Array.from(rows[0].options).map((option) => option.value)).toContain(TOKEN_V2);
    });
    const rows = screen.getAllByLabelText("秘密版本") as HTMLSelectElement[];
    // 第一行显示的就是服务端当前绑定的那一版（第 2 版的 id），不是列表里的第一项。
    expect(rows[0].value).toBe(TOKEN_V1);

    // 只把第一个槽位轮换到同一秘密的另一个版本；第二行完全没被碰过。
    fireEvent.change(rows[0], { target: { value: TOKEN_V2 } });
    const save = screen.getByRole("button", { name: "保存整份绑定集合" });
    await waitFor(() => expect(save.hasAttribute("disabled")).toBe(false));
    fireEvent.click(save);

    await screen.findByText("「闭环验收身份」已整份切换凭证集合，共 2 个槽位。");
    const put = calls.find(
      (call) => call.method === "PUT" && call.path === url(`/credentials/profiles/${PROFILE_ID}/set`),
    );
    // 没被改动的那一行同样在请求体里；只有整份提交才不会把它删掉。
    expect(put?.body).toEqual({
      slots: { "header.Authorization": TOKEN_V2, "header.Cookie": COOKIE_V1 },
      expected_epoch: 4,
    });
  });

  it("新增一个槽位时保留原有槽位，并按整份集合提交", async () => {
    const updated = profile(BOTH_SLOTS, 5);
    apiSendMock.mockImplementation(
      router(
        baseHandlers({
          [`GET ${url(`/credentials/profiles/${PROFILE_ID}/set`)}`]: credentialSet(4),
          [`PUT ${url(`/credentials/profiles/${PROFILE_ID}/set`)}`]: updated,
        }),
      ),
    );
    renderPanel();

    await waitFor(() => expect(screen.getAllByLabelText("秘密版本")).toHaveLength(2));
    // 声明里还有一个没被绑定的槽位；“添加槽位”只从声明里挑，不允许手写槽位字符串。
    fireEvent.click(screen.getByRole("button", { name: "＋添加槽位" }));
    await waitFor(() => expect(screen.getAllByLabelText("秘密")).toHaveLength(3));
    const slotSelects = screen.getAllByLabelText("槽位") as HTMLSelectElement[];
    expect(slotSelects[2].value).toBe("query.access_token");

    const secretSelects = screen.getAllByLabelText("秘密");
    fireEvent.change(secretSelects[2], { target: { value: APP_SECRET } });
    await waitFor(() => expect(screen.getAllByLabelText("秘密版本")).toHaveLength(3));
    const versionSelects = screen.getAllByLabelText("秘密版本");
    fireEvent.change(versionSelects[2], { target: { value: APP_V1 } });

    const save = screen.getByRole("button", { name: "保存整份绑定集合" });
    await waitFor(() => expect(save.hasAttribute("disabled")).toBe(false));
    fireEvent.click(save);
    await screen.findByText("「闭环验收身份」已整份切换凭证集合，共 3 个槽位。");

    const put = calls.find((call) => call.method === "PUT");
    // 新加的槽位与两个原有槽位在同一份集合里：只提交新增的那一个会把另外两个删掉。
    expect(put?.body).toEqual({
      slots: {
        "header.Authorization": TOKEN_V1,
        "header.Cookie": COOKIE_V1,
        "query.access_token": APP_V1,
      },
      expected_epoch: 4,
    });
  });

  it("读取绑定元数据不返回值，界面也只显示槽位与版本号", async () => {
    apiSendMock.mockImplementation(
      router(
        baseHandlers({
          // 故意多塞一个 value：即使服务端某天多回了字段，界面也不能把它渲染出来。
          [`GET ${url(`/credentials/profiles/${PROFILE_ID}/set`)}`]: credentialSet(4, {
            value: "s3cr3t-must-not-render",
          }),
        }),
      ),
    );
    const view = renderPanel();

    await waitFor(() => expect(screen.getAllByLabelText("秘密版本")).toHaveLength(2));
    expect(view.container.textContent).not.toContain("s3cr3t-must-not-render");
    // 解析契约里只有这五个字段：绑定元数据不含任何值的形态。
    const parsed = toCredentialSet(credentialSet(4, { value: "s3cr3t-must-not-render" }));
    expect(Object.keys(parsed.slots[0]).sort()).toEqual(
      ["auth_slot", "secret_id", "secret_name", "secret_version", "secret_version_id"].sort(),
    );
  });

  it("epoch 已被别人推进时按冲突拒绝，不覆盖别人的集合", async () => {
    const conflict = new ApiError(
      409,
      "credential_set_epoch_conflict",
      "凭证集合已被更新，请以最新集合重新提交",
      null,
    );
    const route = router(
      baseHandlers({
        [`GET ${url(`/credentials/profiles/${PROFILE_ID}/set`)}`]: credentialSet(4),
        [`PUT ${url(`/credentials/profiles/${PROFILE_ID}/set`)}`]: conflict,
      }),
    ) as unknown as (
      path: string,
      method: string,
      body: unknown,
      parse: (raw: unknown) => unknown,
    ) => Promise<unknown>;
    // 冲突后的刷新会再读一次集合，这一次返回别人推进后的 epoch。
    let setReads = 0;
    apiSendMock.mockImplementation((async (
      path: string,
      method: string,
      body: unknown,
      parse: (raw: unknown) => unknown,
    ) => {
      if (path === url(`/credentials/profiles/${PROFILE_ID}/set`) && method === "GET") {
        calls.push({ path, method, body });
        setReads += 1;
        return parse(credentialSet(setReads === 1 ? 4 : 5));
      }
      return route(path, method, body, parse);
    }) as never);

    renderPanel();
    await waitFor(() => expect(screen.getAllByLabelText("秘密版本")).toHaveLength(2));
    fireEvent.click(screen.getAllByRole("button", { name: "移除这行" })[1]);
    fireEvent.click(screen.getByRole("button", { name: "保存整份绑定集合" }));

    await screen.findByText("凭证集合已被更新，请以最新集合重新提交");
    // 冲突后必须刷新到服务端当前集合，并提示“你的表单基于旧版本”，由用户决定是否改用。
    await screen.findByText(/已被别人切换到 epoch 5，你的表单基于 epoch 4/);
    expect(screen.getByRole("button", { name: "改用服务端当前集合" })).toBeTruthy();
    // 冲突时草稿没有被服务端数据静默替换：那一行仍然只有 1 行（用户删掉了一行）。
    expect(screen.getAllByLabelText("秘密版本")).toHaveLength(1);
  });
});

describe("授权只通过选择器与表单完成", () => {
  beforeEach(() => {
    calls = [];
    apiSendMock.mockReset();
  });

  function grantHandlers(extra: Record<string, unknown | Error> = {}) {
    return baseHandlers({
      [`GET /workspaces/${WS}/members`]: MEMBERS,
      [`GET ${url("/cases")}`]: [CASE],
      [`GET ${url(`/cases/${CASE_ID}/versions`)}`]: CASE_VERSIONS,
      [`GET ${url(`/credentials/profiles/${PROFILE_ID}/set`)}`]: credentialSet(4),
      ...extra,
    });
  }

  it("认证方案考表单填写，界面上没有要用户拼的配置 JSON", async () => {
    apiSendMock.mockImplementation(router(grantHandlers()));
    const view = renderPanel();

    await screen.findByLabelText("认证方式");
    // 认证声明四件套都在表单里：方案、头名/参数名、值前缀、失效判据。
    expect(screen.getByLabelText("请求头名")).toBeTruthy();
    expect(screen.getByLabelText("值前缀")).toBeTruthy();
    expect(screen.getByLabelText("响应 401 视为凭证失效")).toBeTruthy();
    expect(screen.getByLabelText("响应 403 视为凭证失效")).toBeTruthy();
    // 整个面板没有任何自由文本域：不存在“把内部配置抄进来”的入口。
    expect(view.container.querySelectorAll("textarea")).toHaveLength(0);
    expect(screen.queryByText(/认证位置与失效判据/)).toBeNull();
    // 表单实时回显它将要保存的槽位与判据，提交前可复核。
    expect(screen.getByText(/将保存的认证位置：header\.Authorization/)).toBeTruthy();
  });

  it("被授权主体默认是当前账号，并且只能从成员名单里选", async () => {
    apiSendMock.mockImplementation(
      router(
        grantHandlers({
          [`POST ${url("/credentials/grants")}`]: {
            id: "16161616-1616-4616-8616-161616161616",
            profile_id: PROFILE_ID,
            environment_id: ENV_ID,
            grant_type: "case_version",
            principal_id: USER_ID,
            case_version_id: VERSION_3,
            debug_snapshot_hash: null,
            allowed_targets: [],
            allowed_auth_slots: ["header.Authorization"],
            status: "active",
            expires_at: null,
            used_at: null,
          },
        }),
      ),
    );
    renderPanel({ caseId: CASE_ID, versionId: VERSION_3 });

    const subject = (await screen.findByLabelText("被授权主体（当前工作空间成员）")) as HTMLSelectElement;
    await waitFor(() => expect(subject.value).toBe(USER_ID));
    // 选项是中文姓名，不是 id：用户不需要（也不能）手抄 UUID。
    const labels = Array.from(subject.options).map((option) => option.textContent ?? "");
    expect(labels.some((label) => label.includes("演示账号"))).toBe(true);
    expect(labels.some((label) => label.includes("另一位同事"))).toBe(true);
    expect(labels.some((label) => label.includes(OTHER_USER_ID))).toBe(false);

    fireEvent.change(screen.getByLabelText("身份配置"), { target: { value: PROFILE_ID } });
    fireEvent.click(await screen.findByLabelText("header.Authorization"));
    fireEvent.click(screen.getByRole("button", { name: "签发用途授权" }));

    await screen.findByText(/用途授权已签发/);
    const post = calls.find((call) => call.method === "POST");
    expect(post?.body).toMatchObject({
      profile_id: PROFILE_ID,
      principal_id: USER_ID,
      case_version_id: VERSION_3,
      allowed_auth_slots: ["header.Authorization"],
    });
  });

  it("用例版本按“用例名 / 第几版 / 时间”选，并从当前打开的用例预选", async () => {
    apiSendMock.mockImplementation(router(grantHandlers()));
    renderPanel({ caseId: CASE_ID, versionId: VERSION_3 });

    const caseSelect = (await screen.findByLabelText("用例")) as HTMLSelectElement;
    await waitFor(() => expect(caseSelect.value).toBe(CASE_ID));
    expect(Array.from(caseSelect.options).some((option) => option.textContent === "闭环验收-认证GET（当前打开的用例）")).toBe(
      true,
    );

    const versionSelect = (await screen.findByLabelText("已发布版本")) as HTMLSelectElement;
    // 预选的是编辑器当前打开的那一版，不是列表里的第一项。
    await waitFor(() => expect(versionSelect.value).toBe(VERSION_3));
    const versionLabels = Array.from(versionSelect.options).map((option) => option.textContent ?? "");
    expect(versionLabels.some((label) => label === "闭环验收-认证GET · 第 3 版 · 2026-09-14T08:30:00+00:00")).toBe(true);
    expect(versionLabels.some((label) => label === "闭环验收-认证GET · 第 2 版 · 2026-09-13T08:30:00+00:00")).toBe(true);
  });

  /**
   * 用例列表是进入面板时读一次的。刚在编辑器里新建并发布的用例不在里面——这正是
   * 管理员要授权的那一条。此时：
   * - 选择器里必须能选到它（否则只能授权给别的用例）；
   * - 版本选项必须仍然写清这一版属于哪条用例，而不是退回一个通用词“用例”。
   */
  it("当前打开的用例不在列表里时补读一次，并始终按用例名显示版本", async () => {
    let caseListReads = 0;
    apiSendMock.mockImplementation((async (
      path: string,
      method: string,
      body: unknown,
      parse: (raw: unknown) => unknown,
    ) => {
      calls.push({ path, method, body });
      if (method === "GET" && path === url("/cases")) {
        caseListReads += 1;
        // 第一次读到的是过期列表：新建的那条用例还不在里面。
        return parse(caseListReads === 1 ? [] : [CASE]);
      }
      const handler = grantHandlers()[`${method} ${path}`];
      if (handler === undefined) throw new Error(`测试未覆盖的请求：${method} ${path}`);
      if (handler instanceof Error) throw handler;
      return parse(handler);
    }) as never);

    renderPanel({ caseId: CASE_ID, versionId: VERSION_3 });

    // 补读一次后，选择器里就有这条用例了。
    await waitFor(() => expect(caseListReads).toBe(2));
    const caseSelect = (await screen.findByLabelText("用例")) as HTMLSelectElement;
    await waitFor(() => expect(caseSelect.value).toBe(CASE_ID));
    expect(caseSelect.options[caseSelect.selectedIndex].textContent).toBe("闭环验收-认证GET（当前打开的用例）");

    const versionSelect = (await screen.findByLabelText("已发布版本")) as HTMLSelectElement;
    await waitFor(() => expect(versionSelect.value).toBe(VERSION_3));
    expect(versionSelect.options[versionSelect.selectedIndex].textContent).toBe(
      "闭环验收-认证GET · 第 3 版 · 2026-09-14T08:30:00+00:00",
    );
  });

  it("补读后仍读不到的用例：显示可辨认的 id 前缀，不印成通用词", async () => {
    let caseListReads = 0;
    apiSendMock.mockImplementation((async (
      path: string,
      method: string,
      body: unknown,
      parse: (raw: unknown) => unknown,
    ) => {
      calls.push({ path, method, body });
      if (method === "GET" && path === url("/cases")) {
        caseListReads += 1;
        return parse([]);
      }
      const handler = grantHandlers()[`${method} ${path}`];
      if (handler === undefined) throw new Error(`测试未覆盖的请求：${method} ${path}`);
      if (handler instanceof Error) throw handler;
      return parse(handler);
    }) as never);

    renderPanel({ caseId: CASE_ID, versionId: VERSION_3 });

    const caseSelect = (await screen.findByLabelText("用例")) as HTMLSelectElement;
    await waitFor(() => expect(caseSelect.value).toBe(CASE_ID));
    // 屏幕上显示的就是实际会签发的那条用例：不能显示“请选择用例”却把 id 提交出去。
    expect(caseSelect.options[caseSelect.selectedIndex].textContent).toBe("当前打开的用例 13131313…（尚未读入列表）");

    const versionSelect = (await screen.findByLabelText("已发布版本")) as HTMLSelectElement;
    await waitFor(() => expect(versionSelect.value).toBe(VERSION_3));
    const label = versionSelect.options[versionSelect.selectedIndex].textContent ?? "";
    expect(label).toBe("当前打开的用例 13131313… · 第 3 版 · 2026-09-14T08:30:00+00:00");
    expect(label).not.toMatch(/^用例 · /);

    // 补读是首读返回之后才由 effect 发起的第二次请求；上面两个下拉框断言靠的是
    // “列表里没有这条用例”的占位分支，首读那次就已经成立。直接同步取计数会与补读赛跑，
    // 补读还没发出时读到 1。这里等它真的发出。
    await waitFor(() => expect(caseListReads).toBe(2));
    // 补读只发生一次：已经不在列表里的用例不会让这里反复重试。再放一轮 effect 跑完，
    // 计数不变才算数——只看“某一刻等于 2”，恰好穿过 2 的重复补读会被放过。
    await act(async () => {});
    expect(caseListReads).toBe(2);
  });
});

/**
 * 发布与绑定保存之后的刷新联动（PV1）。
 *
 * 主审的复现里，编辑器已经提示“已发布版本 v1”，授权面板的版本下拉却仍然显示
 * “这条用例还没有已发布版本”，于是从版本选择器签不出授权；另外绑定集合保存后，
 * 下面显示的“当前集合：epoch 1”与上面身份摘要里的“epoch 0”同时出现在屏幕上。
 *
 * 两处的责任层是一样的：**界面上有第二份数据源，而它没有跟着这一次写入前进**。
 * 授权用的版本列表是进入面板时读一次的，`useResource` 的键是“用例 id”，同一个
 * 用例发新版本并不改变这个键，所以它不会自己更新；身份列表也是进入时读一次的，
 * 绑定保存只刷新了下面那一份集合。这里分别验证两者都跟着服务端确认的结果前进。
 */
describe("发布与绑定保存后的刷新联动", () => {
  beforeEach(() => {
    calls = [];
    apiSendMock.mockReset();
  });

  function grantHandlers(extra: Record<string, unknown | Error | (() => unknown)> = {}) {
    return baseHandlers({
      [`GET /workspaces/${WS}/members`]: MEMBERS,
      [`GET ${url("/cases")}`]: [CASE],
      [`GET ${url(`/cases/${CASE_ID}/versions`)}`]: CASE_VERSIONS,
      [`GET ${url(`/credentials/profiles/${PROFILE_ID}/set`)}`]: credentialSet(4),
      ...extra,
    });
  }

  it("编辑器刚发布的版本会补读进版本选择器，不必刷新页面", async () => {
    const VERSION_4 = "17171717-1717-4717-8717-171717171717";
    const published = {
      id: VERSION_4,
      case_id: CASE_ID,
      version: 4,
      schema_version: 1,
      side_effect: "unknown",
      snapshot_hash: "hash-4",
      created_by: USER_ID,
      created_at: "2026-09-14T09:30:00+00:00",
    };
    let versionReads = 0;
    let caseReads = 0;
    apiSendMock.mockImplementation(
      router(
        grantHandlers({
          // 这条用例刚在编辑器里被改过名：用例列表第一次读到的是旧名字。
          [`GET ${url("/cases")}`]: () => {
            caseReads += 1;
            return caseReads === 1 ? [CASE] : [{ ...CASE, name: "闭环验收-认证GET（改名）" }];
          },
          // 第一次读到的是发布之前的那一份：第 4 版还不在里面。
          [`GET ${url(`/cases/${CASE_ID}/versions`)}`]: () => {
            versionReads += 1;
            return versionReads === 1 ? CASE_VERSIONS : [published, ...CASE_VERSIONS];
          },
        }),
      ),
    );

    const view = renderPanel({ caseId: CASE_ID, versionId: VERSION_3 });
    const versionSelect = (await screen.findByLabelText("已发布版本")) as HTMLSelectElement;
    await waitFor(() => expect(versionSelect.value).toBe(VERSION_3));
    // 此刻下拉里确实还没有第 4 版——补读不是“本来就有”。
    expect(Array.from(versionSelect.options).some((option) => option.value === VERSION_4)).toBe(false);

    // 编辑器发布成功，把“当前这一版”换成了刚发布的第 4 版。
    view.rerender(panelTree({ caseId: CASE_ID, versionId: VERSION_4 }));

    await waitFor(() => expect(versionReads).toBe(2));
    // 预选到刚发布的这一版，并且选项是按“用例名 / 第几版 / 时间”显示的。
    await waitFor(() => expect(versionSelect.value).toBe(VERSION_4));
    expect(versionSelect.options[versionSelect.selectedIndex].textContent).toBe(
      "闭环验收-认证GET（改名） · 第 4 版 · 2026-09-14T09:30:00+00:00",
    );
    // 用例下拉里也是同一个新名字：左侧列表改了名，这里不能继续显示旧名字。
    const caseSelect = (await screen.findByLabelText("用例")) as HTMLSelectElement;
    await waitFor(() => expect(caseSelect.value).toBe(CASE_ID));
    expect(caseSelect.options[caseSelect.selectedIndex].textContent).toBe("闭环验收-认证GET（改名）（当前打开的用例）");
    // 补读只发生一次：这一版已经在列表里，不会反复重读。
    await act(async () => {});
    expect(versionReads).toBe(2);
    expect(caseReads).toBe(2);
  });

  it("绑定集合保存后，身份摘要的 epoch 与当前集合一起前进", async () => {
    let epoch = 4;
    let profileReads = 0;
    let releaseProfiles: (() => void) | null = null;
    const handlers = baseHandlers({
      [`GET ${url(`/credentials/profiles/${PROFILE_ID}/set`)}`]: credentialSet(4),
      [`PUT ${url(`/credentials/profiles/${PROFILE_ID}/set`)}`]: () => {
        // 服务端把集合推进到 epoch 5，并回显新 epoch。
        epoch = 5;
        return profile(BOTH_SLOTS, 5);
      },
    });
    apiSendMock.mockImplementation((async (
      path: string,
      method: string,
      body: unknown,
      parse: (raw: unknown) => unknown,
    ) => {
      calls.push({ path, method, body });
      if (method === "GET" && path === url("/credentials/profiles")) {
        profileReads += 1;
        if (profileReads === 2) {
          // 保存之后的那次刷新挂起：要检查它在飞的这段时间里界面显示了什么。
          return new Promise((resolve) => {
            releaseProfiles = () => resolve(parse([profile(BOTH_SLOTS, epoch)]));
          });
        }
        return parse([profile(BOTH_SLOTS, epoch)]);
      }
      const handler = handlers[`${method} ${path}`];
      if (handler === undefined) throw new Error(`测试未覆盖的请求：${method} ${path}`);
      const value = typeof handler === "function" ? (handler as () => unknown)() : handler;
      if (value instanceof Error) throw value;
      return parse(value);
    }) as never);

    renderPanel();

    // 保存前：身份摘要与集合都停在 epoch 4。
    await waitFor(() => expect(screen.getByText(/· epoch 4/)).toBeTruthy());
    await waitFor(() => expect(screen.getAllByLabelText("秘密版本")).toHaveLength(2));
    await waitFor(() => {
      const rows = screen.getAllByLabelText("秘密版本") as HTMLSelectElement[];
      expect(Array.from(rows[0].options).map((option) => option.value)).toContain(TOKEN_V2);
    });
    const rows = screen.getAllByLabelText("秘密版本") as HTMLSelectElement[];
    fireEvent.change(rows[0], { target: { value: TOKEN_V2 } });
    const save = screen.getByRole("button", { name: "保存整份绑定集合" });
    await waitFor(() => expect(save.hasAttribute("disabled")).toBe(false));
    fireEvent.click(save);

    await screen.findByText("「闭环验收身份」已整份切换凭证集合，共 2 个槽位。");
    // 刷新在飞：列表里那一份数据还在，这时不该显示“正在加载身份配置…”——同一个身份
    // 上面不会同时出现“当前集合：epoch 5”和一句看起来像“什么都没读到”的加载提示。
    await waitFor(() => expect(profileReads).toBe(2));
    expect(screen.queryByText("正在加载身份配置…")).toBeNull();

    // 放行这次刷新：上面那份身份摘要跟着服务端确认的新 epoch 一起前进，而不是留在
    // 进入面板时读到的那一份上。
    await act(async () => releaseProfiles?.());
    await waitFor(() => expect(screen.getByText(/· epoch 5/)).toBeTruthy());
    expect(screen.queryByText(/· epoch 4/)).toBeNull();
  });
});

/**
 * 重复槽位不能静默覆盖（VQ4）。
 *
 * 提交的是“槽位 → 秘密版本”的整份映射：同一个槽位出现两行时，后一行会覆盖前一行，
 * 而成功提示仍按行数报告“共 2 个槽位”，界面上看不出只生效了一行。这里验证两层保护：
 * 选择器把已被别行占用的槽位标成不可选；万一仍带着重复行提交，本地直接拒绝且**不发
 * 写请求**（只断言最终对象里键的数量会漏掉这种“发过请求但内容被覆盖”的形态）。
 */
describe("重复槽位不能静默覆盖", () => {
  beforeEach(() => {
    calls = [];
    apiSendMock.mockReset();
  });

  /** 服务端读回来的集合里同一个槽位占了两行，各绑一个版本。 */
  function duplicatedSet(epoch: number): unknown {
    return {
      profile_id: PROFILE_ID,
      set_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
      epoch,
      status: "active",
      expires_at: null,
      slots: [
        {
          auth_slot: "header.Authorization",
          secret_id: TOKEN_SECRET,
          secret_name: "闭环验收令牌",
          secret_version_id: TOKEN_V1,
          secret_version: 2,
        },
        {
          auth_slot: "header.Authorization",
          secret_id: TOKEN_SECRET,
          secret_name: "闭环验收令牌",
          secret_version_id: TOKEN_V2,
          secret_version: 1,
        },
      ],
    };
  }

  it("选择器把已被另一行占用的槽位标成不可选，其余槽位照常可选", async () => {
    apiSendMock.mockImplementation(
      router(
        baseHandlers({
          [`GET ${url(`/credentials/profiles/${PROFILE_ID}/set`)}`]: credentialSet(4),
        }),
      ),
    );
    renderPanel();
    await waitFor(() => expect(screen.getAllByLabelText("秘密版本")).toHaveLength(2));

    const slotSelects = screen.getAllByLabelText("槽位") as HTMLSelectElement[];
    const second = Array.from(slotSelects[1].options);
    const taken = second.find((option) => option.value === "header.Authorization");
    expect(taken?.disabled).toBe(true);
    expect(taken?.textContent).toBe("header.Authorization（已被另一行使用）");
    // 没被占用的槽位不受影响：正常加槽位照旧。
    expect(second.find((option) => option.value === "query.access_token")?.disabled).toBe(false);
  });

  it("带重复槽位提交时本地拒绝，不发写请求，也不报告保存了几行", async () => {
    apiSendMock.mockImplementation(
      router(
        baseHandlers({
          [`GET ${url(`/credentials/profiles/${PROFILE_ID}/set`)}`]: duplicatedSet(4),
          [`PUT ${url(`/credentials/profiles/${PROFILE_ID}/set`)}`]: profile(BOTH_SLOTS, 5),
        }),
      ),
    );
    renderPanel();
    // 两行就位**并且版本列表已经到达**：列表没到时选择框里只有一个“正在读取版本…”，
    // 这时候改值会落成空串（行上是“每一行都要选好槽位与秘密版本”而不是重复槽位），
    // 那样测的就不是本轮要盯的拒绝了。
    await waitFor(() => {
      const selects = screen.getAllByLabelText("秘密版本") as HTMLSelectElement[];
      expect(selects).toHaveLength(2);
      expect(Array.from(selects[1].options).map((option) => option.value)).toContain(TOKEN_V1);
    });

    // 重复状态在点保存之前就能看见。
    expect(screen.getByText(/槽位重复：header\.Authorization/)).toBeTruthy();

    // 把第二行的版本改成与第一行相同：这是一次真实的“用户动过表单”的提交。
    fireEvent.change(screen.getAllByLabelText("秘密版本")[1], { target: { value: TOKEN_V1 } });
    await waitFor(() =>
      expect((screen.getAllByLabelText("秘密版本") as HTMLSelectElement[])[1].value).toBe(TOKEN_V1),
    );

    const save = screen.getByRole("button", { name: "保存整份绑定集合" });
    await waitFor(() => expect(save.hasAttribute("disabled")).toBe(false));
    fireEvent.click(save);

    expect(await screen.findByText(/槽位 header\.Authorization 重复/)).toBeTruthy();
    // 关键：一个写请求都没有发出去，也没有“已整份切换…共 2 个槽位”的成功提示。
    expect(calls.filter((call) => call.method === "PUT")).toHaveLength(0);
    expect(screen.queryByText(/已整份切换凭证集合/)).toBeNull();
  });
});
