import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { RunReport } from "../api/types";
import { AppProviders } from "../theme/AppProviders";
import { ReportView } from "./RunPanel";

function report(overrides: Partial<RunReport> = {}): RunReport {
  return {
    run: {
      id: "run-1",
      target_type: "case_version",
      case_version_id: "version-1",
      environment_id: "env-1",
      state: "finished",
      outcome: "completed_unchecked",
      reason_category: null,
      pool_id: null,
      created_at: "2026-09-29T00:00:00Z",
    },
    steps: [],
    assertions: [],
    request: null,
    response: null,
    context: null,
    ...overrides,
  };
}

describe("报告步骤结果", () => {
  it("显示历史兼容解释，并可展开查看原始执行错误", () => {
    render(<ReportView report={report({
      steps: [{
        step_key: "main",
        attempt_no: 1,
        state: "finished",
        outcome: "error",
        elapsed_ms: 12,
        error_code: null,
        interpretation: {
          outcome: "completed_unchecked",
          reason_code: "legacy_unchecked_mapping",
        },
      }],
    })} />, { wrapper: AppProviders });

    const stepItem = screen.getByText((_content, element) => (
      element?.tagName === "LI" && element.textContent?.includes("第 1 次") === true
    ));
    expect(stepItem.textContent).toContain("响应未校验");
    expect(stepItem.textContent).not.toContain("原始结果：执行错误");
    expect(within(stepItem).getByText("历史记录兼容解释")).toBeTruthy();
    fireEvent.click(within(stepItem).getByText("查看原始记录"));
    expect(within(stepItem).getByText("原始结果：执行错误")).toBeTruthy();
  });

  it("终态无步骤与终态 sending/null 分别说明保存事实", () => {
    const view = render(<ReportView report={report()} />, { wrapper: AppProviders });
    expect(screen.getByText("没有已保存的步骤记录")).toBeTruthy();

    view.rerender(<AppProviders><ReportView report={report({
      steps: [{
        step_key: "main",
        attempt_no: 1,
        state: "sending",
        outcome: null,
        elapsed_ms: null,
        error_code: null,
      }],
    })} /></AppProviders>);
    expect(screen.getByText(/该尝试未保存最终结论/)).toBeTruthy();
  });
});
