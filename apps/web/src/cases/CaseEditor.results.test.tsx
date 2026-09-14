/**
 * 字段行旁运行结论的时效性（F6）。
 *
 * 断言结果按断言标识回填，而修改一条断言不会换标识。若只看标识就把上一轮的结论贴在
 * 字段行旁，用户改完条件不重新执行会看到“通过”，误以为新条件也通过了——这条结论
 * 既不能说明当前配置，也不能说明当前环境。
 *
 * 这里用真实编辑器验证：只有运行快照与当前内容、当前环境一致时，字段行才显示那一轮的
 * 结论；否则显示“未执行”。历史报告本身不受影响，仍在“执行与历史”里可查。
 */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const WORKSPACE_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const CASE_ID = "44444444-4444-4444-8444-444444444444";
const ENV_ID = "55555555-5555-4555-8555-555555555555";
const VERSION_ID = "66666666-6666-4666-8666-666666666666";
const SNAPSHOT_HASH = "hash-v1";
const RUN_ID = "77777777-7777-4777-8777-777777777777";
const ASSERTION_ID = "assert-1";

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

// RunPanel 会拉取运行列表与报告，并把报告回调出来。这里替换成一个可注入报告的替身：
// 测试通过在 `vi.hoisted` 里声明的状态传入“最近一次运行的报告”，替身每次渲染后都
// 调用 onReport 把它送回编辑器。`vi.mock` 工厂会被提升到文件顶部，只有同样被提升的
// `vi.hoisted` 状态在工厂创建时一定已经存在。
//
// 上报必须发生在挂载之后（effect）而不是渲染中：渲染期间回调会触发 React 的
// “渲染另一个组件时更新状态”警告。也不做“只报一次”的优化——编辑器在载入用例详情时
// 会先清空报告，只报一次会让报告停在空值上，与真实组件“有报告就上报”的行为不一致。
const harness = vi.hoisted(() => ({
  report: null as import("../api/types").RunReport | null,
}));

vi.mock("../runs/RunPanel", async () => {
  const { useEffect } = await import("react");
  return {
    RunPanel: (props: {
      onReport: (report: import("../api/types").RunReport) => void;
    }) => {
      const report = harness.report;
      useEffect(() => {
        if (report) props.onReport(report);
      });
      return <div data-testid="run-panel" />;
    },
  };
});

import { CaseEditor } from "./CaseEditor";
import { apiSend, apiSendWithMeta } from "../api/client";
import type { CaseVersion, RunReport } from "../api/types";

const apiSendMock = vi.mocked(apiSend);
vi.mocked(apiSendWithMeta);
const TYPES = [
  {
    id: "status_in",
    label: "属于",
    group: "HTTP 状态",
    applies_to: ["integer", "number", "string"],
    params_schema: {
      values: { control: "value_list" as const, type: "number", label: "允许的状态码" },
    },
    summary: "属于这些值之一",
    operator_version: 1,
  },
];

/** 用例详情：状态码属于 [200]，rev=3，快照摘要等于 v1 发布后的那份。 */
function caseDetail(snapshotHash: string) {
  return {
    id: CASE_ID,
    folder_id: null,
    name: "查询订单",
    request: {
      method: "GET",
      path: "/orders",
      query_params: [],
      headers: [],
      body_type: "none",
      body: "",
    },
    assertions: [
      {
        id: ASSERTION_ID,
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
    updated_at: "2026-09-14T00:00:00Z",
    snapshot_hash: snapshotHash,
  };
}

function versionRow(snapshotHash: string): CaseVersion {
  return {
    id: VERSION_ID,
    case_id: CASE_ID,
    version: 1,
    schema_version: 1,
    side_effect: "unknown",
    snapshot_hash: snapshotHash,
    created_by: "u-1",
    created_at: "2026-09-14T00:00:00Z",
  };
}

/** 上一轮运行报告：状态码 200 通过，运行固定在 VERSION_ID 上。 */
function reportBody(snapshotHash: string, status: string, expected: string): RunReport {
  return {
    run: {
      id: RUN_ID,
      target_type: "case_version",
      case_version_id: VERSION_ID,
      environment_id: ENV_ID,
      state: "finished",
      outcome: "passed",
      reason_category: null,
      pool_id: null,
      created_at: "2026-09-14T00:01:00Z",
    },
    steps: [],
    assertions: [
      {
        assertion_id: ASSERTION_ID,
        type: "status_in",
        phase: "response",
        target: { target_source: "response.status", selector: [] },
        status,
        expected: [{ type: "number", text: expected }],
        actual: { type: "number", text: "200" },
        reason_code: null,
        elapsed_ms: 3,
      },
    ],
    request: null,
    response: null,
  };
}

/** 当前内容（草稿）的快照摘要。测试通过它模拟“保存后服务端算出来的摘要”。 */
let currentSnapshotHash = SNAPSHOT_HASH;

function routes(path: string): unknown {
  if (path.endsWith("/assertion-types")) return TYPES;
  if (path.endsWith(`/cases/${CASE_ID}`)) return caseDetail(currentSnapshotHash);
  if (path.endsWith(`/cases/${CASE_ID}/versions`)) return [versionRow(SNAPSHOT_HASH)];
  throw new Error(`测试未覆盖的请求：${path}`);
}

describe("配置变更后旧运行结论的时效性", () => {
  beforeEach(() => {
    apiSendMock.mockReset();
    apiSendMock.mockImplementation((async (path: string) => routes(path)) as never);
    vi.mocked(apiSendWithMeta).mockReset();
    currentSnapshotHash = SNAPSHOT_HASH;
    harness.report = null;
  });

  it("内容与运行快照一致时，字段行显示那一轮的结论", async () => {
    harness.report = reportBody(SNAPSHOT_HASH, "passed", "200");
    const { container } = render(
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
      />,
    );
    await screen.findByLabelText("用例名称");
    await act(async () => {});

    await waitFor(() => expect(screen.getAllByText("通过").length).toBeGreaterThan(0));
    // 期望／实际也保留：那是这一轮的取值，不是别的条件的结论。
    const block = container.querySelector(".assertion-column") as HTMLElement;
    expect(block.textContent).toContain("期望");
    expect(block.textContent).toContain("实际");
  });

  it("修改断言参数后不重新执行，字段行必须显示未执行而不是上一轮的通过", async () => {
    // 上一轮用 200 通过；用户会把期望改成 500，但没有保存、没有重新执行。
    harness.report = reportBody(SNAPSHOT_HASH, "passed", "200");
    render(
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
      />,
    );
    await screen.findByLabelText("用例名称");
    await act(async () => {});
    await waitFor(() => expect(screen.getAllByText("通过").length).toBeGreaterThan(0));

    const responseBlock = screen.getByText("状态码").closest(".response-field") as HTMLElement;
    const row = within(responseBlock).getAllByRole("listitem")[0];
    fireEvent.click(within(row).getByRole("button", { name: "修改" }));

    const input = await screen.findByLabelText("允许的状态码（每行一个）");
    fireEvent.change(input, { target: { value: "500" } });
    fireEvent.click(screen.getByRole("button", { name: "保存这条断言" }));

    await waitFor(() => {
      const updated = within(
        screen.getByText("状态码").closest(".response-field") as HTMLElement,
      ).getAllByRole("listitem")[0];
      expect(updated.textContent).toContain("未执行");
      expect(updated.textContent).not.toContain("通过");
      // 改过的条件旁不应再挂着上一轮的期望／实际取值。
      expect(updated.textContent).not.toContain("期望");
    });
  });

  it("重新打开页面时，运行快照仍与当前内容一致就显示结论", async () => {
    // 关闭页面再打开没有“发布”这个动作：只能靠服务端草稿摘要与运行版本摘要比对，
    // 不能要求本次会话里必须点过一次发布才认结果，否则历史结论永远重开即失效。
    harness.report = reportBody(SNAPSHOT_HASH, "passed", "200");
    render(
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
      />,
    );
    await screen.findByLabelText("用例名称");
    await act(async () => {});
    await waitFor(() => expect(screen.getAllByText("通过").length).toBeGreaterThan(0));
  });

  it("保存过新内容后，上一轮的通过不再贴在字段行上", async () => {
    // 当前草稿已被保存成另一份内容（服务端摘要变成 hash-v2），上一轮运行的版本仍是 v1。
    // 只改过内容但还没重新发布，同样必须按未执行处理：屏幕上的条件不是那一轮跑过的条件。
    harness.report = reportBody(SNAPSHOT_HASH, "passed", "200");
    currentSnapshotHash = "hash-v2";
    render(
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
      />,
    );
    await screen.findByLabelText("用例名称");
    await act(async () => {});

    await waitFor(() => {
      const block = screen.getByText("状态码").closest(".response-field") as HTMLElement;
      expect(block.textContent).toContain("未执行");
      expect(block.textContent).not.toContain("通过");
    });
  });
});
