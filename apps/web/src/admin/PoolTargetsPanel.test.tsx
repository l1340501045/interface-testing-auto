/**
 * 目标白名单：**显示的就是要提交的那一份完整名单**（VQ2）。
 *
 * 这里挡的是一次真实的数据丢失：文本框曾经绑成“没有草稿时显示空字符串”，而提交读
 * 的是“草稿或服务端那一整份”。用户打开页面看到空框，填一个新目标保存，池上原有的
 * 目标就被整份替换掉了；保存成功后草稿被清理回服务端值，框里又变回空白，屏幕上始终
 * 看不出池上到底有哪些目标。
 *
 * 因此断言分三层：初次打开就要看到完整名单；在它后面**追加**一个目标后提交的必须是
 * 三项（含原有的两项）；保存后重新读回来仍然显示完整名单。整个用例只使用被 mock 的
 * 接口，不访问任何被测服务。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, apiSend: vi.fn() };
});

import { apiSend } from "../api/client";
import type { RunnerPool } from "../api/types";
import { LeaveGuardProvider, useLeaveAggregate } from "../hooks/leaveGuard";
import { AppProviders } from "../theme/AppProviders";
import { PoolTargetsPanel } from "./PoolTargetsPanel";

const WS = "11111111-1111-4111-8111-111111111111";
const PROJECT = "22222222-2222-4222-8222-222222222222";
const POOL_ID = "33333333-3333-4333-8333-333333333333";

const apiSendMock = vi.mocked(apiSend);

interface Call {
  path: string;
  method: string;
  body: unknown;
}

let calls: Call[] = [];

function pool(allowedTargets: string[]): RunnerPool {
  return {
    id: POOL_ID,
    name: "本地执行池",
    status: "active",
    network_zone: "local",
    allowed_targets: allowedTargets,
    grant_id: "44444444-4444-4444-8444-444444444444",
    grant_status: "granted",
    granted_at: "2026-09-14T00:00:00Z",
    environment_ids: [],
  };
}

/**
 * 只实现本用例真的会走到的两条路由；其余请求直接失败，避免悄悄测漏。
 *
 * 读路由是有状态的：保存成功后再读回来的就是刚写入的那一份，这样“保存后重新读回
 * 来”才是真实的服务端往返，而不是拿初始值再骗自己一次。
 */
function router(stored: RunnerPool, saved: RunnerPool) {
  let current = stored;
  return (async (path: string, method: string, body: unknown, parse: (raw: unknown) => unknown) => {
    calls.push({ path, method, body });
    if (method === "GET" && path === `/workspaces/${WS}/projects/${PROJECT}/pools`) {
      return parse([current]);
    }
    if (method === "PUT" && path === `/workspaces/${WS}/projects/${PROJECT}/pools/${POOL_ID}/targets`) {
      current = saved;
      return parse(saved);
    }
    throw new Error(`测试未覆盖的请求：${method} ${path}`);
  }) as never;
}

/** 离开保护登记的观察点：证明这份草稿确实参与了范围切换前的拦截。 */
function LeaveProbe() {
  const { dirty } = useLeaveAggregate();
  return <span data-testid="leave-dirty">{dirty ? "dirty" : "clean"}</span>;
}

function renderPanel(stored: RunnerPool, saved: RunnerPool) {
  apiSendMock.mockImplementation(router(stored, saved));
  render(
    <LeaveGuardProvider>
      <LeaveProbe />
      <PoolTargetsPanel workspaceId={WS} projectId={PROJECT} canAdmin />
    </LeaveGuardProvider>,
    { wrapper: AppProviders },
  );
}

function targetsBox(): HTMLTextAreaElement {
  return screen.getByLabelText("允许的目标（每行一个，格式 scheme://host:port）") as HTMLTextAreaElement;
}

const ECHO = "http://echo:8080";
const ECHO_ALT = "http://echo-alt:8080";
const ECHO_NEW = "http://echo-new:8080";

describe("目标白名单的显示与提交来自同一份名单", () => {
  beforeEach(() => {
    calls = [];
    apiSendMock.mockReset();
  });

  it("初次打开就显示池上完整的目标名单，不是空框", async () => {
    renderPanel(pool([ECHO, ECHO_ALT]), pool([ECHO, ECHO_ALT]));

    await waitFor(() => expect(targetsBox().value).toBe(`${ECHO}\n${ECHO_ALT}`));
    // 没有草稿时框里显示的就是服务端那一份，因此此刻没有未保存内容。
    expect(screen.getByTestId("leave-dirty").textContent).toBe("clean");
  });

  it("在完整名单后面追加一个目标：提交的仍是三项，原有目标不被覆盖", async () => {
    renderPanel(pool([ECHO, ECHO_ALT]), pool([ECHO, ECHO_ALT, ECHO_NEW]));

    await waitFor(() => expect(targetsBox().value).toBe(`${ECHO}\n${ECHO_ALT}`));

    // 用户做的动作是“在已有内容后面补一行”，不是在空框里重新输入一遍。
    fireEvent.change(targetsBox(), { target: { value: `${targetsBox().value}\n${ECHO_NEW}` } });
    await waitFor(() => expect(screen.getByTestId("leave-dirty").textContent).toBe("dirty"));

    fireEvent.click(screen.getByRole("button", { name: "保存白名单" }));
    await waitFor(() => expect(calls.some((call) => call.method === "PUT")).toBe(true));

    const put = calls.find((call) => call.method === "PUT");
    expect(put?.body).toEqual({ allowed_targets: [ECHO, ECHO_ALT, ECHO_NEW] });
  });

  it("保存后重新读回来，框里仍是完整名单，不会变回空白", async () => {
    renderPanel(pool([ECHO, ECHO_ALT]), pool([ECHO, ECHO_ALT, ECHO_NEW]));

    await waitFor(() => expect(targetsBox().value).toBe(`${ECHO}\n${ECHO_ALT}`));
    fireEvent.change(targetsBox(), { target: { value: `${ECHO}\n${ECHO_ALT}\n${ECHO_NEW}` } });
    fireEvent.click(screen.getByRole("button", { name: "保存白名单" }));

    // 保存成功后列表会重新读一次；草稿随即被清理成服务端值，显示必须还是这一整份。
    await waitFor(() => expect(calls.filter((call) => call.method === "GET")).toHaveLength(2));
    await waitFor(() => expect(targetsBox().value).toBe(`${ECHO}\n${ECHO_ALT}\n${ECHO_NEW}`));
    await waitFor(() => expect(screen.getByTestId("leave-dirty").textContent).toBe("clean"));
  });

  it("查看者只读，看不到保存入口", async () => {
    apiSendMock.mockImplementation(router(pool([ECHO]), pool([ECHO])));
    render(
      <LeaveGuardProvider>
        <PoolTargetsPanel workspaceId={WS} projectId={PROJECT} canAdmin={false} />
      </LeaveGuardProvider>,
    );

    await waitFor(() => expect(targetsBox().value).toBe(ECHO));
    expect(targetsBox().disabled).toBe(true);
    expect(screen.queryByRole("button", { name: "保存白名单" })).toBeNull();
    // 只读时不发任何写请求：这里连 PUT 都不该出现。
    expect(calls.every((call) => call.method === "GET")).toBe(true);
  });
});
