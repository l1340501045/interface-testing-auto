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

  it("未知或损坏的 S1 来源证明不破坏旧报告，也不被当成已验证", () => {
    const raw = rawReport() as Record<string, unknown>;
    raw.context = {
      snapshot_fingerprint: "snapshot",
      environment: { id: "env-1", name: "环境", kind: "test", base_url: "http://echo.test" },
      input_fingerprint: "input",
      resolution: { schema_version: 99, guard: "future_guard" },
    };
    raw.resolution = { schema_version: 99, context_fingerprint: "unknown" };
    const report = toRunReport(raw);
    expect(report.run.id).toBe("run-1");
    expect(report.context?.resolution).toBeNull();
    expect(report.resolution).toBeNull();
  });

  it("报告冻结 binding 接受 response_model 的可空位置字段并保留 v1 坐标", () => {
    const raw = rawReport() as Record<string, unknown>;
    raw.resolution = {
      schema_version: 1,
      config_basis: { project_variables_version: 1, project_config_version_id: null, environment_rev: 1, environment_config_version: 1, environment_config_version_id: "cfg" },
      variable_sources: [],
      bindings: [{
        binding_id: "b1", reference: "{{地区.代码}}", name: "地区.代码",
        location: { kind: "header", field: "value", row_id: null, index: 0, occurrence: 0, input_fingerprint: "input", selector: null, utf16_span: null },
        source: { level: "project", resource_id: "p1", revision: 1, value: { type: "string", text: "cn" }, unavailable_reason: null },
        overridden_sources: [], value_type: "string", rendered_preview: "cn",
      }],
      context_fingerprint: "source-fp",
    };
    const report = toRunReport(raw);
    expect(report.resolution?.bindings[0]?.location).toEqual({ kind: "header", field: "value", index: 0, occurrence: 0, input_fingerprint: "input" });
  });
});
