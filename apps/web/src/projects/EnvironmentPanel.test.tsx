/**
 * 环境编辑的行为：角色门槛、空态、保存内容与失败提示。
 *
 * 保存环境时会一并提交普通变量。这些变量按字面量文本承载，长整数必须原样送出，
 * 界面上过一次 Number 就会把 9007199254740993 悄悄改成 9007199254740992，
 * 用例里的取值跟着变，而用户完全看不出来。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, apiSend: vi.fn(), apiDelete: vi.fn() };
});

import { ApiError, apiSend } from "../api/client";
import type { Environment } from "../api/types";
import { LeaveGuardProvider, useLeaveAggregate } from "../hooks/leaveGuard";
import { EnvironmentPanel } from "./EnvironmentPanel";

const WS = "11111111-1111-4111-8111-111111111111";
const PROJECT = "22222222-2222-4222-8222-222222222222";
const ENV_ID = "33333333-3333-4333-8333-333333333333";

const apiSendMock = vi.mocked(apiSend);

interface Call {
  path: string;
  method: string;
  body: unknown;
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
  render(
    <LeaveGuardProvider>
      <LeaveProbe />
      <EnvironmentPanel
        workspaceId={WS}
        projectId={PROJECT}
        environments={list}
        loading={false}
        error={null}
        selectedId={null}
        onSelect={vi.fn()}
        canEdit={options.canEdit ?? true}
        onChanged={onChanged}
      />
    </LeaveGuardProvider>,
  );
  return { onChanged };
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
  ) => {
    calls.push({ path, method, body });
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
    fireEvent.change(screen.getByLabelText("状态"), { target: { value: "archived" } });

    fireEvent.click(screen.getByRole("button", { name: "＋添加变量" }));
    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "order_id" } });
    fireEvent.change(screen.getByLabelText("类型"), { target: { value: "number" } });
    fireEvent.change(screen.getByLabelText("值"), { target: { value: "9007199254740993" } });
    fireEvent.click(screen.getByRole("button", { name: "保存环境" }));

    await screen.findByText("环境「灰度环境」已保存。");
    expect(calls).toHaveLength(1);
    expect(calls[0].path).toBe(
      `/workspaces/${WS}/projects/${PROJECT}/environments/${ENV_ID}`,
    );
    expect(calls[0].method).toBe("PATCH");
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
    const values = screen.getAllByLabelText("值");
    expect(
      values.some((element) => element.tagName === "SELECT" && (element as HTMLSelectElement).value === "true"),
    ).toBe(true);
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
    fireEvent.change(screen.getByLabelText("类型"), { target: { value: "string" } });

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
