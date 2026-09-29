import { describe, expect, it } from "vitest";

import { toRunReport } from "./guards";

function rawReport(interpretation?: unknown) {
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
    steps: [{
      step_key: "main",
      attempt_no: 1,
      state: "finished",
      outcome: "error",
      elapsed_ms: 10,
      error_code: null,
      ...(interpretation === undefined ? {} : { interpretation }),
    }],
    assertions: [],
    request: null,
    response: { status: 200 },
    context: null,
  };
}

describe("运行报告兼容解释", () => {
  it("只采信精确支持的解释对象", () => {
    const report = toRunReport(rawReport({
      outcome: "completed_unchecked",
      reason_code: "legacy_unchecked_mapping",
    }));
    expect(report.steps[0]?.interpretation).toEqual({
      outcome: "completed_unchecked",
      reason_code: "legacy_unchecked_mapping",
    });
    expect(report.steps[0]?.outcome).toBe("error");
  });

  it.each([
    undefined,
    null,
    "completed_unchecked",
    { outcome: "passed", reason_code: "legacy_unchecked_mapping" },
    { outcome: "completed_unchecked", reason_code: "unknown" },
  ])("缺失、未知或损坏解释回退原始合法报告：%o", (interpretation) => {
    const report = toRunReport(rawReport(interpretation));
    expect(report.steps[0]?.outcome).toBe("error");
    expect(report.steps[0]?.interpretation).toBeNull();
  });
});
