/**
 * 环境编辑的行为：角色门槛、空态、保存内容与失败提示。
 *
 * 保存环境时会一并提交普通变量。这些变量按字面量文本承载，长整数必须原样送出，
 * 界面上过一次 Number 就会把 9007199254740993 悄悄改成 9007199254740992，
 * 用例里的取值跟着变，而用户完全看不出来。
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, apiSend: vi.fn(), apiDelete: vi.fn() };
});

import { ApiError, apiSend } from "../api/client";
import type { Environment } from "../api/types";
import { LeaveGuardProvider, useLeaveAggregate } from "../hooks/leaveGuard";
import { AppProviders } from "../theme/AppProviders";
import { antSelectedValue, selectAntOption } from "../test/antd";
import {
  ENVIRONMENT_URL_EXAMPLE,
  ENVIRONMENT_URL_IP_EXAMPLE,
} from "./environmentUrl";
import { EnvironmentPanel } from "./EnvironmentPanel";

const WS = "11111111-1111-4111-8111-111111111111";
const PROJECT = "22222222-2222-4222-8222-222222222222";
const ENV_ID = "33333333-3333-4333-8333-333333333333";

const apiSendMock = vi.mocked(apiSend);

interface Call {
  path: string;
  method: string;
  body: unknown;
  headers?: Record<string, string>;
}

let calls: Call[] = [];

function environment(overrides: Partial<Environment> = {}): Environment {
  return {
    id: ENV_ID,
    name: "本地测试环境",
    kind: "test",
    base_url: "http://target-service:8080",
    pool_id: null,
    variables: {},
    status: "active",
    rev: 1,
    config_version: 1,
    ...overrides,
  };
}

/** 离开保护登记的观察点：这里只挂这一个表单，别的表单的脏状态盖不住它的漏报。 */
function LeaveProbe() {
  const { dirty } = useLeaveAggregate();
  return <span data-testid="leave-dirty">{dirty ? "dirty" : "clean"}</span>;
}

function renderPanel(list: Environment[], options: { canEdit?: boolean } = {}) {
  const onChanged = vi.fn();
  const onOpenChange = vi.fn();
  const node = (items: Environment[]) => (
    <LeaveGuardProvider>
      <LeaveProbe />
      <EnvironmentPanel
        workspaceId={WS}
        projectId={PROJECT}
        environments={items}
        loading={false}
        error={null}
        selectedId={null}
        onSelect={vi.fn()}
        canEdit={options.canEdit ?? true}
        onChanged={onChanged}
        // 折叠状态由外壳持有；这里默认展开，测试聚焦面板内容而不是开合动画。
        open
        onOpenChange={onOpenChange}
      />
    </LeaveGuardProvider>
  );
  const view = render(node(list), { wrapper: AppProviders });
  return { onChanged, onOpenChange, rerenderList: (items: Environment[]) => view.rerender(node(items)) };
}

function leaveState(): string {
  return screen.getByTestId("leave-dirty").textContent ?? "";
}

function mockSave(result: unknown | Error) {
  apiSendMock.mockImplementation((async (
    path: string,
    method: string,
    body: unknown,
    parse: (raw: unknown) => unknown,
    options?: { headers?: Record<string, string> },
  ) => {
    calls.push({ path, method, body, ...(options?.headers ? { headers: options.headers } : {}) });
    if (result instanceof Error) throw result;
    return parse(result);
  }) as never);
}

describe("环境编辑", () => {
  beforeEach(() => {
    calls = [];
    apiSendMock.mockReset();
  });

  it("保存时把地址、状态与变量一并提交，长整数原样送出", async () => {
    mockSave(environment({ name: "灰度环境", base_url: "http://gray:9000", status: "archived" }));
    const { onChanged } = renderPanel([environment()]);

    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    fireEvent.change(screen.getByLabelText("环境名称"), { target: { value: "灰度环境" } });
    fireEvent.change(screen.getByLabelText("服务地址"), { target: { value: "http://gray:9000" } });
    await selectAntOption("状态", "停用");

    fireEvent.click(screen.getByRole("button", { name: "＋添加变量" }));
    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "order_id" } });
    await selectAntOption("类型", "数字");
    fireEvent.change(screen.getByLabelText("值"), { target: { value: "9007199254740993" } });
    fireEvent.click(screen.getByRole("button", { name: "保存环境" }));

    await screen.findByText("环境「灰度环境」已保存。");
    expect(calls).toHaveLength(1);
    expect(calls[0].path).toBe(
      `/workspaces/${WS}/projects/${PROJECT}/environments/${ENV_ID}`,
    );
    expect(calls[0].method).toBe("PATCH");
    expect(calls[0].headers?.["If-Match"]).toBe("1");
    expect(calls[0].body).toEqual({
      name: "灰度环境",
      base_url: "http://gray:9000",
      status: "archived",
      variables: { order_id: { type: "number", text: "9007199254740993" } },
    });
    expect(onChanged).toHaveBeenCalled();
  });

  it("服务端拒绝时如实转述原因，草稿留在界面上", async () => {
    mockSave(new ApiError(409, "environment_name_exists", "环境名称已存在", null));
    renderPanel([environment()]);

    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    fireEvent.change(screen.getByLabelText("环境名称"), { target: { value: "本地测试环境" } });
    fireEvent.click(screen.getByRole("button", { name: "保存环境" }));

    expect(await screen.findByText("环境名称已存在")).toBeTruthy();
    expect((screen.getByLabelText("环境名称") as HTMLInputElement).value).toBe("本地测试环境");
  });

  it("修订冲突保留整份草稿并提示刷新，不把旧值覆盖回表单", async () => {
    mockSave(new ApiError(409, "config_revision_conflict", "环境配置已被其他人更新", null));
    renderPanel([environment({ rev: 7 })]);
    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    fireEvent.change(screen.getByLabelText("环境名称"), { target: { value: "我的未保存环境" } });
    fireEvent.click(screen.getByRole("button", { name: "保存环境" }));
    expect(await screen.findByText(/输入均已保留/)).toBeTruthy();
    expect((screen.getByLabelText("环境名称") as HTMLInputElement).value).toBe("我的未保存环境");
    expect(calls[0]?.headers?.["If-Match"]).toBe("7");
  });

  it("列表刷新不偷换编辑基线，明确采纳后才使用新修订", async () => {
    let saves = 0;
    apiSendMock.mockImplementation((async (path: string, method: string, body: unknown, parse: (raw: unknown) => unknown, options?: { headers?: Record<string, string> }) => {
      calls.push({ path, method, body, ...(options?.headers ? { headers: options.headers } : {}) });
      saves += 1;
      if (saves === 1) throw new ApiError(409, "config_revision_conflict", "环境已更新", null);
      return parse(environment({ rev: 3, name: "我的草稿" }));
    }) as never);
    const first = environment({ rev: 1 });
    const { rerenderList } = renderPanel([first]);
    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    fireEvent.change(screen.getByLabelText("环境名称"), { target: { value: "我的草稿" } });
    rerenderList([environment({ rev: 2, name: "别人保存的名称" })]);
    expect(screen.getByText(/仍基于旧修订/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "保存环境" }));
    await screen.findByText(/输入均已保留/);
    expect(calls[0]?.headers?.["If-Match"]).toBe("1");
    fireEvent.click(screen.getByRole("button", { name: "保留输入并采用修订 2 作为新基线" }));
    fireEvent.click(screen.getByRole("button", { name: "保存环境" }));
    await screen.findByText("环境「我的草稿」已保存。");
    expect(calls[1]?.headers?.["If-Match"]).toBe("2");
  });

  it("变量名重复在本地挡住，不发请求", async () => {
    mockSave(environment());
    renderPanel([environment()]);

    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    fireEvent.click(screen.getByRole("button", { name: "＋添加变量" }));
    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "token" } });
    fireEvent.click(screen.getByRole("button", { name: "＋添加变量" }));
    fireEvent.change(screen.getAllByLabelText("名称")[1], { target: { value: "token" } });
    fireEvent.click(screen.getByRole("button", { name: "保存环境" }));

    expect(await screen.findByText("变量名重复：token")).toBeTruthy();
    expect(calls).toHaveLength(0);
  });

  it("查看者只能选环境，看不到创建与编辑入口", async () => {
    mockSave(environment());
    renderPanel([environment()], { canEdit: false });

    expect(screen.queryByRole("button", { name: "编辑" })).toBeNull();
    expect(screen.queryByRole("button", { name: "创建测试环境" })).toBeNull();
    expect(screen.getByRole("button", { name: "本地测试环境" })).toBeTruthy();
  });

  it("没有环境时说明执行前需要什么", () => {
    mockSave(environment());
    renderPanel([], { canEdit: false });

    expect(
      screen.getByText("当前项目还没有环境。执行前至少需要一个测试环境，其地址由执行池白名单放行。"),
    ).toBeTruthy();
  });

  it("环境带回的普通变量在编辑时能读回成行", async () => {
    mockSave(environment());
    renderPanel([
      environment({
        variables: {
          flag: { type: "boolean", value: true },
          base: { type: "string", text: "http://echo:8080" },
        },
      }),
    ]);

    fireEvent.click(screen.getByRole("button", { name: "编辑" }));

    await waitFor(() => expect(screen.getAllByLabelText("名称")).toHaveLength(2));
    const names = screen.getAllByLabelText("名称") as HTMLInputElement[];
    expect(names.map((input) => input.value).sort()).toEqual(["base", "flag"]);
    // 布尔变量的取值控件应当直接选中 true，而不是让人从 "true"/"false" 文本里猜。
    expect(antSelectedValue("值")).toBe("true");
  });
});

/**
 * 未保存保护的基线是**要提交的字面量**，不是显示文本（VQ3）。
 *
 * 这里的每一个用例只挂环境面板这一个表单，避免别的表单的脏状态把漏报盖过去。
 */
describe("环境草稿的未保存保护", () => {
  beforeEach(() => {
    calls = [];
    apiSendMock.mockReset();
  });

  it("只把已有变量的值从 1 改成 2、其他字段都不动，也算有未保存修改", async () => {
    const saved = environment({ variables: { count: { type: "number", text: "1" } } });
    mockSave(saved);
    renderPanel([saved]);

    // 没有改动时不该报“有未保存修改”。
    expect(leaveState()).toBe("clean");

    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    fireEvent.change(screen.getByLabelText("值"), { target: { value: "2" } });

    await waitFor(() => expect(leaveState()).toBe("dirty"));
  });

  it("只把已有变量的类型从数字改成文本（值文本仍是 1）也算有未保存修改", async () => {
    const saved = environment({ variables: { count: { type: "number", text: "1" } } });
    mockSave(saved);
    renderPanel([saved]);

    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    await selectAntOption("类型", "文本");

    // 显示文本同样是 "1"：只有比较字面量本身才看得出类型变了。
    expect((screen.getByLabelText("值") as HTMLInputElement).value).toBe("1");
    await waitFor(() => expect(leaveState()).toBe("dirty"));
  });

  it("保存成功后保护清除，提交的是改动后的字面量", async () => {
    const saved = environment({ variables: { count: { type: "number", text: "1" } } });
    mockSave(environment({ variables: { count: { type: "number", text: "2" } } }));
    renderPanel([saved]);

    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    fireEvent.change(screen.getByLabelText("值"), { target: { value: "2" } });
    await waitFor(() => expect(leaveState()).toBe("dirty"));

    fireEvent.click(screen.getByRole("button", { name: "保存环境" }));
    await screen.findByText("环境「本地测试环境」已保存。");

    await waitFor(() => expect(leaveState()).toBe("clean"));
    expect(calls[0].body).toMatchObject({ variables: { count: { type: "number", text: "2" } } });
  });
});

/**
 * 地址语法就地反馈（ENV-04）。
 *
 * 环境地址会拼到用例路径前面，写错它等于所有请求都发不出去——而服务端过去只在执行时
 * 报“目标不在白名单”，把人引向去修改本来正确的用例路径。这里锁的是：地址本身的问题
 * 在**输入框旁**当场说清楚，非法输入一个字节都不写进服务端，已经输入的内容原样留着。
 */
describe("环境地址的就地校验", () => {
  beforeEach(() => {
    calls = [];
    apiSendMock.mockReset();
  });

  it("创建时缺协议：不发请求，错误贴在地址输入框上，输入保留", async () => {
    mockSave(environment());
    renderPanel([]);

    fireEvent.change(screen.getByLabelText("新环境名称"), { target: { value: "本地环境" } });
    const urlInput = screen.getByLabelText("新环境地址") as HTMLInputElement;
    fireEvent.change(urlInput, { target: { value: "target-service:8080" } });
    fireEvent.click(screen.getByRole("button", { name: "创建测试环境" }));

    const error = await screen.findByText(/必须以 http:\/\/ 或 https:\/\//);
    expect(calls).toHaveLength(0);
    expect(urlInput.value).toBe("target-service:8080");
    expect(urlInput.getAttribute("aria-invalid")).toBe("true");
    expect(urlInput.getAttribute("aria-describedby")).toBe("env-url-error");
    expect(error.id).toBe("env-url-error");
  });

  it("创建时地址合法：按原文提交，路径不被改写", async () => {
    mockSave(environment());
    renderPanel([]);

    fireEvent.change(screen.getByLabelText("新环境名称"), { target: { value: "本地环境" } });
    fireEvent.change(screen.getByLabelText("新环境地址"), {
      target: { value: "  http://echo:8080/api/v1  " },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建测试环境" }));

    await screen.findByText("环境已创建，可在用例编辑页顶部的“执行环境”中选择。");
    expect(calls).toHaveLength(1);
    expect(calls[0].method).toBe("POST");
    expect((calls[0].body as { base_url: string }).base_url).toBe("http://echo:8080/api/v1");
  });

  it("编辑时缺协议：整条 PATCH 都不发，草稿留在输入框里", async () => {
    mockSave(environment());
    renderPanel([environment()]);

    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    const urlInput = screen.getByLabelText("服务地址") as HTMLInputElement;
    fireEvent.change(urlInput, { target: { value: "echo:8080" } });
    fireEvent.click(screen.getByRole("button", { name: "保存环境" }));

    const error = await screen.findByText(/必须以 http:\/\/ 或 https:\/\//);
    expect(calls).toHaveLength(0);
    expect(urlInput.value).toBe("echo:8080");
    expect(urlInput.getAttribute("aria-describedby")).toBe("env-edit-url-error");
    expect(error.id).toBe("env-edit-url-error");
  });

  it("改回合法地址后就地错误立即消失，不需要重新提交一次", async () => {
    mockSave(environment());
    renderPanel([]);

    const urlInput = screen.getByLabelText("新环境地址") as HTMLInputElement;
    fireEvent.change(screen.getByLabelText("新环境名称"), { target: { value: "本地环境" } });
    fireEvent.change(urlInput, { target: { value: "echo:8080" } });
    fireEvent.click(screen.getByRole("button", { name: "创建测试环境" }));
    await screen.findByText(/必须以 http:\/\/ 或 https:\/\//);

    fireEvent.change(urlInput, { target: { value: "http://echo:8080" } });
    expect(screen.queryByText(/必须以 http:\/\/ 或 https:\/\//)).toBeNull();
    expect(urlInput.getAttribute("aria-invalid")).toBe("false");
  });

  it("说明同时给出域名与 IP＋端口的示例，创建与编辑都有", () => {
    renderPanel([environment()]);

    // 只给域名示例，用户会以为这里必须写域名；IP＋端口是同样受支持的常见写法。
    const createHint = screen.getByText(
      new RegExp(`${ENVIRONMENT_URL_EXAMPLE}/api/v1`),
    );
    expect(createHint.textContent).toContain(ENVIRONMENT_URL_IP_EXAMPLE);

    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    const editHint = screen.getAllByText(new RegExp(ENVIRONMENT_URL_IP_EXAMPLE));
    expect(editHint.length).toBeGreaterThan(0);
  });

  it("提交时保留 IP＋端口地址，不被改写成域名或去掉端口", async () => {
    mockSave(environment({ base_url: ENVIRONMENT_URL_IP_EXAMPLE }));
    renderPanel([]);

    fireEvent.change(screen.getByLabelText("新环境名称"), { target: { value: "内网环境" } });
    fireEvent.change(screen.getByLabelText("新环境地址"), {
      target: { value: ENVIRONMENT_URL_IP_EXAMPLE },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建测试环境" }));

    await screen.findByText("环境已创建，可在用例编辑页顶部的“执行环境”中选择。");
    expect((calls[0].body as { base_url: string }).base_url).toBe(ENVIRONMENT_URL_IP_EXAMPLE);
  });
});

/**
 * 折叠状态由外壳持有，因此面板必须**只回报用户操作**（R1-1）。
 *
 * 这一层的开合不再是组件内部状态：调试入口要能把它打开，外壳才能让用户直接看到环境编辑。
 * 随之而来的责任是分清“用户点击”和“React 同步 open 属性”——两者都会触发 `toggle`。
 * 把它们混为一谈时，环境列表读回来导致属性被移除的那次同步会被记成“用户想展开”，
 * 面板从此永远合不上。
 */
describe("环境面板的折叠回报", () => {
  beforeEach(() => {
    calls = [];
    apiSendMock.mockReset();
  });

  it("用户点击折叠标题时回报 false", () => {
    const { onOpenChange } = renderPanel([environment()]);
    fireEvent.click(screen.getByRole("button", { name: "环境（1）" }));

    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("受控展开的初次同步不回报用户操作", () => {
    const { onOpenChange } = renderPanel([environment()]);
    expect(screen.getByRole("button", { name: "环境（1）" }).getAttribute("aria-expanded")).toBe("true");
    expect(onOpenChange).not.toHaveBeenCalled();
  });
});

/**
 * 服务端地址校验错误必须落到**它对应的那个输入框**上（R1-2）。
 *
 * 前端是早反馈，后端是权威：两边的 URL 库不可能完全一致——例如 `http://[1]/` 在前端只按
 * 形态检查能通过，服务端判定它不是一个可用的 IPv6 字面量并返回 `environment_url_invalid`。
 * 这时错误必须标在地址输入框上（aria-invalid + aria-describedby），而不是只在面板底部
 * 给一句泛化提示——后者会让用户不知道**哪个字段**要改，正是本次要修的缺陷。
 *
 * 保存期间锁定地址与编辑目标，迟到的旧地址错误只能标记实际提交的那份表单。
 */
describe("服务端地址错误的字段归属", () => {
  beforeEach(() => {
    calls = [];
    apiSendMock.mockReset();
  });

  /** 挂起的保存：由测试决定什么时候带着哪份结果返回。 */
  function deferredSave() {
    let resolve: (value: unknown) => void;
    let reject: (reason: unknown) => void;
    const response = new Promise<unknown>((resolveFn, rejectFn) => {
      resolve = resolveFn;
      reject = rejectFn;
    });
    apiSendMock.mockImplementation(async (path, method, body, parse, options) => {
      calls.push({ path, method, body, ...(options?.headers ? { headers: options.headers } : {}) });
      return parse(await response);
    });
    return {
      succeed: (result: unknown) => resolve(result),
      fail: (error: unknown) => reject(error),
    };
  }

  const urlRejected = () =>
    new ApiError(
      400,
      "environment_url_invalid",
      "环境地址无法被 HTTP 客户端解析：主机名或端口不是受支持的写法。",
      null,
    );

  it("创建时服务端拒绝地址：错误关联新建地址输入框，草稿保留", async () => {
    const gate = deferredSave();
    renderPanel([]);

    fireEvent.change(screen.getByLabelText("新环境名称"), { target: { value: "内网环境" } });
    const urlInput = screen.getByLabelText("新环境地址") as HTMLInputElement;
    // 本地只按形态检查放行（服务端会更严格），因此这里能发出去并拿到服务端错误。
    fireEvent.change(urlInput, { target: { value: "http://[1]/health" } });
    fireEvent.click(screen.getByRole("button", { name: "创建测试环境" }));

    await waitFor(() => expect(calls).toHaveLength(1));
    // 保存期间锁定地址：错误返回时输入不可能已被改成另一个值（与编辑表单同一机制）。
    expect(urlInput.disabled).toBe(true);
    gate.fail(urlRejected());
    await act(async () => {});

    const error = await screen.findByText(/无法被 HTTP 客户端解析/);
    expect(error.id).toBe("env-url-error");
    expect(urlInput.getAttribute("aria-invalid")).toBe("true");
    expect(urlInput.getAttribute("aria-describedby")).toBe("env-url-error");
    // 草稿保留：地址与名称都还在，锁定解除后可以就地改完重试。
    expect(urlInput.value).toBe("http://[1]/health");
    expect(urlInput.disabled).toBe(false);
    expect((screen.getByLabelText("新环境名称") as HTMLInputElement).value).toBe("内网环境");
  });

  it("编辑时服务端拒绝地址：错误关联服务地址输入框，草稿保留", async () => {
    const gate = deferredSave();
    renderPanel([environment()]);

    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    const urlInput = (await screen.findByLabelText("服务地址")) as HTMLInputElement;
    fireEvent.change(urlInput, { target: { value: "http://[1]/health" } });
    fireEvent.click(screen.getByRole("button", { name: "保存环境" }));

    await waitFor(() => expect(calls).toHaveLength(1));
    gate.fail(urlRejected());
    await act(async () => {});

    const error = await screen.findByText(/无法被 HTTP 客户端解析/);
    expect(error.id).toBe("env-edit-url-error");
    expect(urlInput.getAttribute("aria-describedby")).toBe("env-edit-url-error");
    // 还在编辑态，用户改完可以就地重试。
    expect(urlInput.value).toBe("http://[1]/health");
    expect(screen.getByRole("button", { name: "保存环境" })).toBeTruthy();
  });

  it("保存期间锁定地址输入：迟到的旧地址错误只会落在它校验的那一份值上", async () => {
    /* 采用“锁定输入”这条最小合规做法：请求未返回时地址不可编辑，因此不存在“错误标到另一个
       值上”的窗口。这条同时锁住锁定的存在与它的目的——两个方向缺一不可：只断言 disabled
       证明不了错误归属，只断言错误归属又看不出是怎么保证的。 */
    const gate = deferredSave();
    renderPanel([environment()]);

    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    const urlInput = (await screen.findByLabelText("服务地址")) as HTMLInputElement;
    fireEvent.change(urlInput, { target: { value: "http://[1]/health" } });
    fireEvent.click(screen.getByRole("button", { name: "保存环境" }));
    await waitFor(() => expect(calls).toHaveLength(1));

    // 请求还在飞：地址输入被锁定，用户改不出“另一个值”。
    expect(urlInput.disabled).toBe(true);

    gate.fail(urlRejected());
    await act(async () => {});

    // 错误落到锁定期间那份值所属的输入框上；失败后解锁，用户可以就地改完重试。
    const error = await screen.findByText(/无法被 HTTP 客户端解析/);
    expect(error.id).toBe("env-edit-url-error");
    expect(urlInput.value).toBe("http://[1]/health");
    expect(urlInput.getAttribute("aria-describedby")).toBe("env-edit-url-error");
    expect(urlInput.disabled).toBe(false);
    expect(screen.getByRole("button", { name: "保存环境" })).toBeTruthy();
  });

  it.each([false, true])("保存期间点击编辑不切换目标，迟到错误仍属于 A（同帧：%s）", async (sameFrame) => {
    const gate = deferredSave();
    const first = environment({ name: "环境 A" });
    const second = environment({
      id: "44444444-4444-4444-8444-444444444444",
      name: "环境 B",
      base_url: "http://other-service:8080",
    });
    const { onChanged } = renderPanel([first, second]);
    const [editFirst, editSecond] = screen.getAllByRole("button", { name: "编辑" });

    fireEvent.click(editFirst);
    fireEvent.change(screen.getByLabelText("环境名称"), { target: { value: "环境 A 草稿" } });
    fireEvent.change(screen.getByLabelText("服务地址"), { target: { value: "http://[1]/health" } });
    const save = screen.getByRole("button", { name: "保存环境" });

    if (sameFrame) {
      // React 提交 busy 之前立即点击：disabled 尚未生效，处理函数也必须保护目标。
      act(() => {
        save.click();
        editFirst.click();
        editSecond.click();
      });
    } else {
      fireEvent.click(save);
      fireEvent.click(editFirst);
      fireEvent.click(editSecond);
    }
    const editButtonsLocked = screen.getAllByRole("button", { name: "编辑" })
      .every((button) => button.hasAttribute("disabled"));

    await act(async () => gate.fail(urlRejected()));

    const error = await screen.findByText(/无法被 HTTP 客户端解析/);
    const urlInput = screen.getByLabelText("服务地址") as HTMLInputElement;
    expect((screen.getByLabelText("环境名称") as HTMLInputElement).value).toBe("环境 A 草稿");
    expect(urlInput.value).toBe("http://[1]/health");
    expect(error.id).toBe("env-edit-url-error");
    expect(urlInput.getAttribute("aria-invalid")).toBe("true");
    expect(urlInput.getAttribute("aria-describedby")).toBe(error.id);
    expect(editButtonsLocked).toBe(true);
    expect(calls).toEqual([{
      path: `/workspaces/${WS}/projects/${PROJECT}/environments/${first.id}`,
      method: "PATCH",
      body: { name: "环境 A 草稿", base_url: "http://[1]/health", status: "active", variables: {} },
      headers: { "If-Match": "1" },
    }]);
    expect(onChanged).not.toHaveBeenCalled();

    // 失败后解锁，切到 B 时只显示 B 的地址，A 的错误不能残留。
    expect(urlInput.disabled).toBe(false);
    expect(editSecond.hasAttribute("disabled")).toBe(false);
    fireEvent.click(editSecond);
    const secondUrl = screen.getByLabelText("服务地址") as HTMLInputElement;
    expect((screen.getByLabelText("环境名称") as HTMLInputElement).value).toBe(second.name);
    expect(secondUrl.value).toBe(second.base_url);
    expect(secondUrl.getAttribute("aria-invalid")).toBe("false");
    expect(secondUrl.hasAttribute("aria-describedby")).toBe(false);
    expect(screen.queryByText(/无法被 HTTP 客户端解析/)).toBeNull();
    expect(calls).toHaveLength(1);
  });

  it("同帧取消和重复提交不能打断保存，成功后重新开放编辑", async () => {
    const gate = deferredSave();
    const first = environment({ name: "环境 A" });
    const second = environment({
      id: "44444444-4444-4444-8444-444444444444",
      name: "环境 B",
      base_url: "http://other-service:8080",
    });
    const { onChanged } = renderPanel([first, second]);
    const [editFirst, editSecond] = screen.getAllByRole("button", { name: "编辑" });
    fireEvent.click(editFirst);
    fireEvent.change(screen.getByLabelText("服务地址"), { target: { value: "http://saved-service:8080" } });
    fireEvent.change(screen.getByLabelText("新环境名称"), { target: { value: "环境 C" } });
    fireEvent.change(screen.getByLabelText("新环境地址"), { target: { value: "http://new-service:8080" } });
    const save = screen.getByRole("button", { name: "保存环境" });
    const cancel = screen.getByRole("button", { name: "取消" });
    const create = screen.getByRole("button", { name: "创建测试环境" });

    act(() => {
      save.click();
      cancel.click();
      save.click();
      create.click();
      editSecond.click();
    });
    const pendingUrl = (screen.queryByLabelText("服务地址") as HTMLInputElement | null)?.value;
    await act(async () => gate.succeed(environment({ base_url: "http://saved-service:8080" })));

    expect(pendingUrl).toBe("http://saved-service:8080");
    expect(calls).toHaveLength(1);
    expect(calls[0].path).toBe(`/workspaces/${WS}/projects/${PROJECT}/environments/${first.id}`);
    expect(calls[0].method).toBe("PATCH");
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("环境「环境 A」已保存。")).toBeTruthy();
    expect(screen.queryByLabelText("服务地址")).toBeNull();
    expect(editSecond.hasAttribute("disabled")).toBe(false);
    fireEvent.click(editSecond);
    expect((screen.getByLabelText("服务地址") as HTMLInputElement).value).toBe(second.base_url);
  });
});
