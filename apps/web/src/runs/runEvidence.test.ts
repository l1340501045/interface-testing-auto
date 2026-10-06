import { describe, expect, it } from "vitest";

import type { RunReport } from "../api/types";
import { matchesResolutionEvidence } from "./runEvidence";

function report(): RunReport {
  return {
    run: { id: "r1", target_type: "debug_snapshot", case_version_id: null, environment_id: "e1", state: "finished", outcome: "passed", reason_category: null, pool_id: null, created_at: "now" },
    steps: [], assertions: [], request: null, response: null,
    context: { snapshot_fingerprint: "snapshot", environment: { id: "e1", name: "环境", kind: "test", base_url: "http://echo" }, input_fingerprint: "input", resolution: { schema_version: 1, guard: "ordinary_binding_enforced_v1", context_fingerprint: "source-a", binding_fingerprint: "binding-a" } },
    resolution: { schema_version: 1, config_basis: { project_variables_version: 1, project_config_version_id: null, environment_rev: 1, environment_config_version: 1, environment_config_version_id: "cfg" }, variable_sources: [], bindings: [], context_fingerprint: "source-a" },
  };
}

describe("报告的 S1 来源证明", () => {
  it("冻结来源、worker 证明与当前预览三者同源才匹配", () => {
    expect(matchesResolutionEvidence(report(), "source-a")).toBe(true);
    expect(matchesResolutionEvidence(report(), "source-b")).toBe(false);
  });

  it("旧报告或只有受理来源、没有 worker 证明时不能贴当前结论", () => {
    const old = report();
    old.context = old.context ? { ...old.context, resolution: null } : null;
    expect(matchesResolutionEvidence(old, "source-a")).toBe(false);
    expect(matchesResolutionEvidence({ ...report(), resolution: null }, "source-a")).toBe(false);
  });
});
