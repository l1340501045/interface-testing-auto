import { describe, expect, it } from "vitest";

import { toEnvironmentServiceConfig, toResolutionPreview, toRunReport, toServiceCatalog } from "./guards";

const KEY = "svc_11111111111111111111111111111111";
const mapping = { id: "m1", rev: 2, status: "active", base_url: "http://orders:8080/base", version: 2, version_id: "mv2" };
const selected = { kind: "service", service_id: "s1", service_key: KEY, service_name: "订单服务", service_rev: 3, service_status: "active", availability: "ready", mapping };
const target = { kind: "service", service_id: "s1", service_key: KEY, service_rev: 3, mapping_id: "m1", mapping_rev: 2, mapping_version_id: "mv2", mapping_version: 2, environment_id: "e1" };

describe("S2 服务 DTO 严格解码", () => {
  it.each([
    ["environment_archived", "active"],
    ["service_archived", "archived"],
    ["mapping_disabled", "active"],
  ] as const)("合法高优先级状态 %s 可保留disabled命名映射", (availability, serviceStatus) => {
    const raw = {
      schema_version: 2, scope: { workspace_id: "w1", project_id: "p1", environment_id: "e1" }, ready: false,
      ordinary_resolution: "ready", masked_target: null, bindings: [], issues: [],
      auth: { required: false, status: "none", injection_slots: [], requires_worker_verification: false },
      config_basis: { project_variables_version: 1, project_config_version_id: null, environment_rev: 4, environment_config_version: 3, environment_config_version_id: "ec3" },
      context_fingerprint: "hmac", resolution_context: null, target_ref: null,
      selected_target: { ...selected, availability, service_status: serviceStatus, mapping: { ...mapping, status: "disabled" } },
    };
    expect(toResolutionPreview(raw)).toMatchObject({ selected_target: { availability, mapping: { status: "disabled" } } });
    if (availability === "environment_archived") {
      expect(() => toResolutionPreview({ ...raw, selected_target: { ...raw.selected_target, availability: "ready" } })).toThrow("启用状态不一致");
      expect(() => toResolutionPreview({ ...raw, selected_target: { ...raw.selected_target, availability: "mapping_disabled", service_status: "archived" } })).toThrow("活动服务状态不一致");
    }
  });

  it("目录要求唯一default并保留archived命名服务", () => {
    const result = toServiceCatalog({ schema_version: 1, items: [
      { id: "s0", service_key: "default", name: "默认服务", is_default: true, status: "active", rev: 1, created_at: "t", updated_at: "t" },
      { id: "s1", service_key: KEY, name: "订单服务", is_default: false, status: "archived", rev: 3, created_at: "t", updated_at: "t" },
    ] });
    expect(result.items[1]).toMatchObject({ service_key: KEY, status: "archived" });
    expect(() => toServiceCatalog({ schema_version: 1, items: [] })).toThrow("默认服务");
  });

  it("环境映射把availability与mapping nullable联合、身份和池分开", () => {
    const result = toEnvironmentServiceConfig({
      schema_version: 1,
      environment: { id: "e1", name: "测试", kind: "test", status: "active", rev: 4, config_version: 3 },
      inheritance: { identity: { mode: "shared_environment", state: "ready", profile_id: "p1" }, pool: { state: "not_granted", id: "pool", name: "默认池", status: "active" } },
      items: [
        { service_id: "s0", service_key: "default", service_name: "默认服务", is_default: true, service_status: "active", service_rev: 1, mapping: { ...mapping, status: "active" }, availability: "ready" },
        { service_id: "s1", service_key: KEY, service_name: "订单服务", is_default: false, service_status: "active", service_rev: 3, mapping: null, availability: "mapping_missing" },
      ],
    });
    expect(result.inheritance.pool.state).toBe("not_granted");
    expect(result.items[0].availability).toBe("ready");
    expect(result.items[1].mapping).toBeNull();
    expect(() => toEnvironmentServiceConfig({
      schema_version: 1,
      environment: { id: "e1", name: "测试", kind: "test", status: "active", rev: 4, config_version: 3 },
      inheritance: { identity: { mode: "shared_environment", state: "unchecked" }, pool: { state: "missing" } },
      items: [{ service_id: "s1", service_key: KEY, service_name: "订单服务", is_default: false, service_status: "active", service_rev: 3, mapping, availability: "mapping_missing" }],
    })).toThrow("mapping 与 availability");
  });

  it("schema2预览要求selected target并核target_ref同源", () => {
    const preview = {
      schema_version: 2, scope: { workspace_id: "w1", project_id: "p1", environment_id: "e1" }, ready: true,
      ordinary_resolution: "ready", masked_target: { method: "GET", url: "http://orders:8080/base/echo" }, bindings: [], issues: [],
      auth: { required: false, status: "none", injection_slots: [], requires_worker_verification: false },
      config_basis: { project_variables_version: 1, project_config_version_id: null, environment_rev: 4, environment_config_version: 3, environment_config_version_id: "ec3" },
      context_fingerprint: "hmac", resolution_context: "token", selected_target: selected, target_ref: target,
    };
    expect(toResolutionPreview(preview)).toMatchObject({ schema_version: 2, selected_target: { service_key: KEY }, target_ref: { mapping_rev: 2 } });
    expect(() => toResolutionPreview({ ...preview, target_ref: { ...target, service_key: "default" } })).toThrow("目标类型");
    expect(() => toResolutionPreview({ ...preview, selected_target: { ...selected, availability: "mapping_missing", mapping } })).toThrow("mapping 与 availability");
  });

  it("报告严格双读schema2 target与worker证明，损坏旁路不破坏基本历史", () => {
    const frozen = {
      schema_version: 2,
      config_basis: { project_variables_version: 1, project_config_version_id: null, environment_rev: 4, environment_config_version: 3, environment_config_version_id: "ec3" },
      variable_sources: [], bindings: [], context_fingerprint: "hmac", target_ref: target,
    };
    const raw = {
      run: { id: "r1", target_type: "debug_snapshot", case_version_id: null, environment_id: "e1", state: "finished", outcome: "passed", reason_category: null, pool_id: null, created_at: "t" },
      steps: [], assertions: [], request: null, response: null,
      resolution: frozen,
      context: {
        snapshot_fingerprint: "snapshot", environment: { id: "e1", name: "测试", kind: "test", base_url: "http://orders:8080/base" }, input_fingerprint: "input",
        resolution: { schema_version: 2, guard: "selected_target_binding_enforced_v1", context_fingerprint: "hmac", binding_fingerprint: "binding-hmac", target_fingerprint: "target-hmac" },
      },
    };
    expect(toRunReport(raw)).toMatchObject({ resolution: { schema_version: 2, target_ref: target }, context: { resolution: { schema_version: 2, target_fingerprint: "target-hmac" } } });
    expect(toRunReport({ ...raw, context: { ...raw.context, resolution: { ...raw.context.resolution, target_fingerprint: null } } }).context?.resolution).toBeNull();
    expect(toRunReport({ ...raw, resolution: { ...frozen, target_ref: { ...target, service_key: "default" } } }).resolution).toBeNull();
  });
});
