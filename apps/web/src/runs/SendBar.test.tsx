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
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { DebugPreflight, Environment, ProjectService } from "../api/types";
import type { RawRequest } from "../cases/requestDraft";
import { SendBar, serviceAvailabilityLabel } from "./SendBar";

const ENV_ID = "33333333-3333-4333-8333-333333333333";

const ENVIRONMENT: Environment = {
  id: ENV_ID,
  name: "本地测试环境",
  kind: "test",
  base_url: "target-service:8080",
  pool_id: null,
  variables: {},
  status: "active",
  rev: 1,
  config_version: 1,
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

function renderSendBar(preflight: DebugPreflight | null, request: RawRequest = REQUEST, onLocateIssue = vi.fn(), services: ProjectService[] = [], servicesRequested = services.length > 0, serviceState: { loaded?: boolean; loading?: boolean; error?: string | null } = {}) {
  const onOpenAdmin = vi.fn();
  const onOpenEnvironment = vi.fn();
  const onRestoreCase = vi.fn();
  const onOrganizeCase = vi.fn();
  const onPatch = vi.fn();
  const view = render(
    <SendBar
      request={request}
      services={services}
      servicesRequested={servicesRequested}
      servicesLoaded={serviceState.loaded ?? services.length > 0}
      servicesLoading={serviceState.loading ?? false}
      servicesError={serviceState.error ?? null}
      environments={[ENVIRONMENT]}
      selectedEnvironmentId={ENV_ID}
      onSelectEnvironment={vi.fn()}
      onPatch={onPatch}
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
      onRestoreCase={onRestoreCase}
      onOrganizeCase={onOrganizeCase}
      canAuthorize={false}
      onSubmitAuthorization={vi.fn()}
      onCancelAuthorization={vi.fn()}
      authorization={null}
      onLocateIssue={onLocateIssue}
    />,
  );
  return { onOpenAdmin, onOpenEnvironment, onRestoreCase, onOrganizeCase, onLocateIssue, onPatch, view };
}

describe("环境地址无效时的发送栏", () => {
  it.each([
    ["ready", "映射已配置"],
    ["mapping_missing", "尚未配置地址"],
    ["environment_archived", "环境已停用"],
    ["service_archived", "服务已停用"],
    ["mapping_disabled", "当前环境中的服务地址已暂停"],
    ["config_inconsistent", "服务配置不一致"],
  ] as const)("schema2服务状态 %s 使用操作文案，不暴露枚举和稳定key", (availability, label) => {
    expect(serviceAvailabilityLabel(availability)).toBe(label);
  });

  it("schema2普通操作区显示服务名和中文状态，不显示稳定key或枚举", () => {
    const serviceKey = "svc_11111111111111111111111111111111";
    const preflight: DebugPreflight = {
      ready: false,
      issues: [], can_authorize: false,
      auth: { required: false, state: "none", profile_id: null }, context: null,
      resolution: {
        schema_version: 2,
        scope: { workspace_id: "w1", project_id: "p1", environment_id: ENV_ID },
        ready: false, ordinary_resolution: "ready", masked_target: null,
        bindings: [], issues: [], auth: { required: false, status: "none", injection_slots: [], requires_worker_verification: false },
        config_basis: { project_variables_version: 1, project_config_version_id: null, environment_rev: 1, environment_config_version: 1, environment_config_version_id: "cfg" },
        context_fingerprint: "fp", resolution_context: null,
        selected_target: { kind: "service", service_id: "service-1", service_key: serviceKey, service_name: "订单服务", service_rev: 1, service_status: "active", availability: "mapping_missing", mapping: null },
        target_ref: null,
      },
    };
    renderSendBar(preflight, { ...REQUEST, service_contract: 1, service_key: serviceKey });
    expect(screen.getByLabelText("当前服务：订单服务 · 尚未配置地址")).toBeTruthy();
    expect(screen.queryByText(/当前状态：mapping_missing/)).toBeNull();
  });

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
    // 没有权威解析时不把客户端拼接冒充实际目标；本地值只作为配置预览。
    expect(screen.getByText(/实际目标：尚未取得权威解析结果/)).toBeTruthy();
    expect(screen.getByText(/配置预览：target-service:8080\/orders/)).toBeTruthy();
  });

  it("归档和目录异常预检给出真实用例库纠错入口", () => {
    const restore = renderSendBar(blockedPreflight("case_archived", "restore_case"));
    fireEvent.click(screen.getByRole("button", { name: "前往用例库恢复" }));
    expect(restore.onRestoreCase).toHaveBeenCalledTimes(1);
  });

  it("目录异常预检进入整理范围", () => {
    const organize = renderSendBar(blockedPreflight("folder_unavailable", "organize_case"));
    fireEvent.click(screen.getByRole("button", { name: "前往用例库整理" }));
    expect(organize.onOrganizeCase).toHaveBeenCalledTimes(1);
  });

  it("仅停用 Query 含变量时不显示地址变量提示", () => {
    renderSendBar(null, {
      ...REQUEST,
      schema_version: 2,
      query_params: [{ row_id: "11111111-1111-7111-8111-111111111111", name: "disabled", value: "{{missing}}", enabled: false, description: "" }],
    });
    expect(screen.queryByText(/含 \{\{变量\}\}/)).toBeNull();
  });

  it("cURL来源提示说明实际目标由环境与服务共同决定", () => {
    renderSendBar(null, { ...REQUEST, imported_origin: "http://source.example" });
    expect(screen.getByText(/实际目标始终由所选环境与服务共同决定/)).toBeTruthy();
    expect(screen.queryByText(/实际目标始终由所选环境决定/)).toBeNull();
  });

  it("解析问题用稳定 issue 定位到重复行，而不是只按 code 找第一行", () => {
    const preflight: DebugPreflight = {
      ...blockedPreflight("variable_undefined", "edit_request"),
      resolution: {
        schema_version: 1,
        scope: { workspace_id: "w1", project_id: "p1", environment_id: ENV_ID },
        ready: false,
        ordinary_resolution: "invalid",
        masked_target: null,
        bindings: [],
        issues: [{ issue_id: "issue-second", code: "variable_undefined", message: "环境地址必须以 http:// 或 https:// 开头。", action: "edit_request", location: { kind: "query", field: "value", row_id: "row-second" } }],
        auth: { required: false, status: "none", injection_slots: [], requires_worker_verification: false },
        config_basis: { project_variables_version: 1, project_config_version_id: null, environment_rev: 1, environment_config_version: 1, environment_config_version_id: "cfg" },
        context_fingerprint: "fp",
        resolution_context: null,
      },
    };
    const { onLocateIssue } = renderSendBar(preflight);
    fireEvent.click(screen.getByRole("button", { name: "定位修正" }));
    expect(onLocateIssue).toHaveBeenCalledWith({ kind: "query", field: "value", row_id: "row-second" });
  });

  it("服务单选明确写入named字段并可明确切回default", async () => {
    const user = userEvent.setup();
    const services: ProjectService[] = [
      { id: "s0", service_key: "default", name: "默认服务", is_default: true, status: "active", rev: 1, created_at: "t", updated_at: "t" },
      { id: "s1", service_key: "svc_11111111111111111111111111111111", name: "订单服务", is_default: false, status: "active", rev: 1, created_at: "t", updated_at: "t" },
    ];
    const first = renderSendBar(null, REQUEST, vi.fn(), services);
    await user.click(screen.getByRole("combobox", { name: "请求服务" }));
    await user.click(screen.getByText("订单服务"));
    expect(first.onPatch).toHaveBeenCalledWith({ service_contract: 1, service_key: services[1].service_key });
    first.view.unmount();

    const named = { ...REQUEST, service_contract: 1 as const, service_key: services[1].service_key };
    const second = renderSendBar(null, named, vi.fn(), services);
    await user.click(screen.getByRole("combobox", { name: "请求服务" }));
    await user.click(screen.getByText("默认服务", { selector: ".ant-select-item-option-content" }));
    expect(second.onPatch).toHaveBeenCalledWith({ service_contract: undefined, service_key: undefined });
  });

  it("目录缺少原named服务时保留失效占位且不自动回default", () => {
    const key = "svc_11111111111111111111111111111111";
    const { onPatch } = renderSendBar(null, { ...REQUEST, service_contract: 1, service_key: key }, vi.fn(), [], true, { loaded: true });
    expect(screen.getByText(`原服务 ${key}（已失效）`)).toBeTruthy();
    expect(screen.getByText(/当前请求原服务已停用或不可见/)).toBeTruthy();
    expect(onPatch).not.toHaveBeenCalled();
  });

  it.each([
    ["未读", { loaded: false, loading: false, error: null }, "目录尚未确认"],
    ["加载中", { loaded: false, loading: true, error: null }, "正在确认目录"],
    ["加载失败", { loaded: false, loading: false, error: "目录暂不可用" }, "目录加载失败"],
  ] as const)("named目录%s时保留选择且不伪称已失效", (_name, state, expected) => {
    const key = "svc_11111111111111111111111111111111";
    const { onPatch } = renderSendBar(null, { ...REQUEST, service_contract: 1, service_key: key }, vi.fn(), [], true, state);
    const serviceControl = screen.getByRole("combobox", { name: "请求服务" }).closest(".ant-select");
    expect(serviceControl?.textContent).toContain(expected);
    expect(serviceControl?.textContent).not.toContain("已失效");
    expect(screen.queryByText(/当前请求原服务已停用或不可见/)).toBeNull();
    expect(onPatch).not.toHaveBeenCalled();
  });
});
