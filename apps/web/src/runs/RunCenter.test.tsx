import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, apiSend: vi.fn() };
});

const reload = vi.fn();
const run = {
  id: "77777777-7777-4777-8777-777777777777",
  target_type: "case_version",
  case_version_id: "version-1",
  environment_id: "env-1",
  state: "running",
  outcome: null,
  reason_category: null,
  pool_id: null,
  created_at: "2026-09-26T00:00:00Z",
};
const runB = { ...run, id: "88888888-8888-4888-8888-888888888888", state: "finished", outcome: "passed" };

vi.mock("./useRuns", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./useRuns")>();
  return {
    ...actual,
    useRuns: () => ({ data: [run, runB], loading: false, error: null, reload }),
    useRunReport: (_workspaceId: string, _projectId: string, runId: string | null) => ({
      data: runId ? { run: runId === runB.id ? runB : run, steps: [], assertions: [], request: null, response: null, context: null } : null,
      loading: false,
      error: null,
      reload,
    }),
  };
});

import { apiSend } from "../api/client";
import { AppProviders } from "../theme/AppProviders";
import { RunCenter } from "./RunCenter";

const environments = [{
  id: "env-1",
  name: "测试环境",
  kind: "test",
  base_url: "http://echo:8080",
  pool_id: null,
  variables: {},
  status: "active",
}];

describe("独立任务与报告页面", () => {
  beforeEach(() => {
    vi.mocked(apiSend).mockReset();
    reload.mockReset();
  });

  it("任务中心按真实运行 id 取消并刷新列表", async () => {
    vi.mocked(apiSend).mockResolvedValue({});
    const openReport = vi.fn();
    render(<RunCenter workspaceId="ws-1" projectId="project-1" environments={environments} canCancel mode="tasks" active onOpenReport={openReport} />, { wrapper: AppProviders });

    fireEvent.click(screen.getAllByRole("button", { name: "查看报告" })[0]!);
    expect(openReport).toHaveBeenCalledWith(run.id);
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    await waitFor(() => expect(apiSend).toHaveBeenCalledWith(
      "/workspaces/ws-1/projects/project-1/runs/77777777-7777-4777-8777-777777777777/cancel",
      "POST",
      undefined,
      expect.any(Function),
    ));
    expect(reload).toHaveBeenCalled();
  });

  it("测试报告只有显式选择后才展示详情", () => {
    render(<RunCenter workspaceId="ws-1" projectId="project-1" environments={environments} canCancel={false} mode="reports" active />, { wrapper: AppProviders });
    expect(screen.queryByText("最终结果")).toBeNull();
    fireEvent.click(screen.getAllByRole("button", { name: "查看报告" })[0]!);
    expect(screen.getByText("最终结果")).toBeTruthy();
  });

  it("任务跳转只消费一次；切页不覆盖新选择，同一运行的新跳转仍生效", () => {
    const firstRequest = { runId: run.id, token: 1, workspaceId: "ws-1", projectId: "project-1" };
    const view = render(
      <RunCenter workspaceId="ws-1" projectId="project-1" environments={environments} canCancel={false} mode="reports" active reportRequest={firstRequest} />,
      { wrapper: AppProviders },
    );
    expect(screen.getByRole("heading", { name: `运行 ${run.id.slice(0, 8)} 的报告` })).toBeTruthy();

    fireEvent.click(screen.getAllByRole("button", { name: "查看报告" })[1]!);
    expect(screen.getByRole("heading", { name: `运行 ${runB.id.slice(0, 8)} 的报告` })).toBeTruthy();

    view.rerender(<RunCenter workspaceId="ws-1" projectId="project-1" environments={environments} canCancel={false} mode="reports" active={false} reportRequest={firstRequest} />);
    view.rerender(<RunCenter workspaceId="ws-1" projectId="project-1" environments={environments} canCancel={false} mode="reports" active reportRequest={firstRequest} />);
    expect(screen.getByRole("heading", { name: `运行 ${runB.id.slice(0, 8)} 的报告` })).toBeTruthy();

    view.rerender(<RunCenter workspaceId="ws-1" projectId="project-1" environments={environments} canCancel={false} mode="reports" active reportRequest={{ runId: run.id, token: 2, workspaceId: "ws-1", projectId: "project-1" }} />);
    expect(screen.getByRole("heading", { name: `运行 ${run.id.slice(0, 8)} 的报告` })).toBeTruthy();
  });

  it("拒绝消费属于其他项目的报告意图", () => {
    render(
      <RunCenter
        workspaceId="ws-1"
        projectId="project-2"
        environments={environments}
        canCancel={false}
        mode="reports"
        active
        reportRequest={{ runId: run.id, token: 1, workspaceId: "ws-1", projectId: "project-1" }}
      />,
      { wrapper: AppProviders },
    );
    expect(screen.queryByRole("heading", { name: `运行 ${run.id.slice(0, 8)} 的报告` })).toBeNull();
  });
});
