/**
 * 地址行对“环境地址本身不合法”的呈现（ENV-03／ENV-04）。
 *
 * 服务端把这类问题单独归成 `environment_url_invalid`，并给出建议动作
 * `configure_environment`。地址行必须做到两件事：
 *
 * 1. 建议动作是**真能点的下一步**——只写一句“建议：前往环境设置”，用户还得自己回侧栏
 *    里找那一段；入口必须调用外壳已有的展开动作。
 * 2. 地址预览不能再以“实际目标”的口吻展示一个拼不出来的地址。把它当正常目标显示，
 *    等于告诉用户请求会发到一个根本发不出去的地方。
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { DebugPreflight, Environment } from "../api/types";
import type { RawRequest } from "../cases/requestDraft";
import { SendBar } from "./SendBar";

const ENV_ID = "33333333-3333-4333-8333-333333333333";

const ENVIRONMENT: Environment = {
  id: ENV_ID,
  name: "本地测试环境",
  kind: "test",
  base_url: "target-service:8080",
  pool_id: null,
  variables: {},
  status: "active",
};

const REQUEST: RawRequest = {
  method: "GET",
  path: "/orders",
  query_params: [],
  headers: [],
  body_type: "none",
  body: "",
};

function blockedPreflight(code: string, action: DebugPreflight["issues"][number]["action"]): DebugPreflight {
  return {
    ready: false,
    issues: [{ code, message: "环境地址必须以 http:// 或 https:// 开头。", action }],
    can_authorize: false,
    auth: { required: false, state: "none", profile_id: null },
    context: null,
  };
}

function renderSendBar(preflight: DebugPreflight | null) {
  const onOpenAdmin = vi.fn();
  const onOpenEnvironment = vi.fn();
  render(
    <SendBar
      request={REQUEST}
      environments={[ENVIRONMENT]}
      selectedEnvironmentId={ENV_ID}
      onSelectEnvironment={vi.fn()}
      onPatch={vi.fn()}
      onSend={vi.fn()}
      onStopWaiting={vi.fn()}
      onRetryAcceptance={vi.fn()}
      onResumeWaiting={vi.fn()}
      paused={false}
      stage="idle"
      readOnly={false}
      readOnlyReason={null}
      preflight={preflight}
      preflightError={null}
      preflighting={false}
      onOpenAdmin={onOpenAdmin}
      onOpenEnvironment={onOpenEnvironment}
      canAuthorize={false}
      onSubmitAuthorization={vi.fn()}
      onCancelAuthorization={vi.fn()}
      authorization={null}
    />,
  );
  return { onOpenAdmin, onOpenEnvironment };
}

describe("环境地址无效时的发送栏", () => {
  it("configure_environment 建议是可点击的入口，点了调用环境设置", () => {
    const { onOpenAdmin, onOpenEnvironment } = renderSendBar(
      blockedPreflight("environment_url_invalid", "configure_environment"),
    );

    const entry = screen.getByRole("button", { name: "前往环境设置" });
    fireEvent.click(entry);

    expect(onOpenEnvironment).toHaveBeenCalledTimes(1);
    // 环境问题不该被送去凭证列表。
    expect(onOpenAdmin).not.toHaveBeenCalled();
  });

  it("地址拼不出目标时，不再把它当成“实际目标”展示", () => {
    renderSendBar(blockedPreflight("environment_url_invalid", "configure_environment"));

    expect(screen.getByText(/环境地址不合法，暂时无法确定/)).toBeTruthy();
    expect(screen.queryByText(/实际目标：target-service:8080/)).toBeNull();
  });

  it("其它内容类问题仍然只给文字建议，不冒充可点击动作", () => {
    renderSendBar(blockedPreflight("target_not_allowed", "edit_request"));

    expect(screen.getByText("建议：修改请求内容")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "修改请求内容" })).toBeNull();
    // 地址本身没被判为不合法，预览照常显示。
    expect(screen.getByText(/实际目标：target-service:8080\/orders/)).toBeTruthy();
  });
});
