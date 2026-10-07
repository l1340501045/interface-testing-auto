import { describe, expect, it } from "vitest";

import { ContractError, toResolutionPreview, toVariableContext } from "./guards";

const basis = {
  project_variables_version: 3,
  project_config_version_id: null,
  environment_rev: 4,
  environment_config_version: 2,
  environment_config_version_id: "cfg-2",
};
const source = { level: "environment", resource_id: "env-1", revision: 4, value: { type: "number", text: "9007199254740993" }, unavailable_reason: null };

describe("S1 变量与解析合同边界", () => {
  it("目录保留环境覆盖项目时各自的类型和无损文本", () => {
    const result = toVariableContext({
      schema_version: 1,
      scope: { workspace_id: "w1", project_id: "p1", environment_id: "env-1" },
      config_basis: basis,
      variables: [{
        name: "地区.代码",
        reference: "{{地区.代码}}",
        value: { type: "number", text: "9007199254740993" },
        effective_source: source,
        overridden_sources: [
          { level: "project", resource_id: "p1", revision: 3, value: { type: "string", text: "9007199254740993" }, unavailable_reason: null },
          { level: "project", resource_id: "p1", revision: 2, value: null, unavailable_reason: "历史类型不可展示" },
        ],
        available_locations: ["path", "query_value", "header_value", "body"],
        restricted_body_types: [],
        unavailable_reason: null,
      }],
    });
    expect(result.variables[0]?.value).toEqual({ type: "number", text: "9007199254740993" });
    expect(result.variables[0]?.overridden_sources[0]?.value).toEqual({ type: "string", text: "9007199254740993" });
    expect(result.variables[0]?.overridden_sources[1]).toMatchObject({ value: null, unavailable_reason: "历史类型不可展示" });
  });

  it("解析结果严格保留稳定 issue、UTF-16 位置和认证槽位状态", () => {
    const result = toResolutionPreview({
      schema_version: 1,
      scope: { workspace_id: "w1", project_id: "p1", environment_id: "env-1" },
      ready: false,
      ordinary_resolution: "invalid",
      masked_target: null,
      bindings: [{
        binding_id: "binding-v1", reference: "{{地区.代码}}", name: "地区.代码",
        location: { kind: "query", field: "value", row_id: null, index: 1, occurrence: 1, input_fingerprint: "input-v1", selector: null, utf16_span: null },
        source: { level: "environment", resource_id: "env-1", revision: 4, value: { type: "string", text: "cn" }, unavailable_reason: null },
        overridden_sources: [], value_type: "string", rendered_preview: "cn",
      }],
      issues: [{ issue_id: "issue-1", code: "variable_undefined", message: "变量不存在", action: "edit_request", location: { kind: "body", field: "body", row_id: null, index: null, occurrence: null, input_fingerprint: null, selector: [], utf16_span: { start: 1, end: 3 } } }],
      auth: { required: false, status: "ready", injection_slots: [{ kind: "header", name: "Authorization", status: "conflict" }], requires_worker_verification: true },
      config_basis: basis,
      context_fingerprint: "fp-1",
      resolution_context: null,
    });
    expect(result.issues[0]?.location).toEqual({ kind: "body", field: "body", selector: [], utf16_span: { start: 1, end: 3 } });
    expect(result.bindings[0]?.location).toEqual({ kind: "query", field: "value", index: 1, occurrence: 1, input_fingerprint: "input-v1" });
    expect(result.auth.injection_slots[0]?.status).toBe("conflict");
  });

  it("false 与 null 保持原 ValueLiteral 判别联合，不转成文本或空值", () => {
    const result = toVariableContext({
      schema_version: 1,
      scope: { workspace_id: "w1", project_id: "p1", environment_id: "env-1" },
      config_basis: basis,
      variables: [
        { name: "开关", reference: "{{开关}}", value: { type: "boolean", value: false }, effective_source: { level: "environment", resource_id: "env-1", revision: 4, value: { type: "boolean", value: false }, unavailable_reason: null }, overridden_sources: [], available_locations: ["body"], restricted_body_types: [], unavailable_reason: null },
        { name: "空值", reference: "{{空值}}", value: { type: "null" }, effective_source: { level: "project", resource_id: "p1", revision: 3, value: { type: "null" }, unavailable_reason: null }, overridden_sources: [], available_locations: ["query_value"], restricted_body_types: [], unavailable_reason: null },
      ],
    });
    expect(result.variables.map((item) => item.value)).toEqual([{ type: "boolean", value: false }, { type: "null" }]);
  });

  it("未知来源层和倒置 UTF-16 范围不能被弱收窄", () => {
    expect(() => toVariableContext({
      schema_version: 1,
      scope: { workspace_id: "w1", project_id: "p1", environment_id: "env-1" },
      config_basis: basis,
      variables: [{ name: "x", reference: "{{x}}", value: { type: "null" }, effective_source: { ...source, level: "secret" }, overridden_sources: [], available_locations: ["path"], restricted_body_types: [], unavailable_reason: null }],
    })).toThrow(ContractError);
    expect(() => toResolutionPreview({
      schema_version: 1,
      scope: { workspace_id: "w1", project_id: "p1", environment_id: "env-1" }, ready: false,
      ordinary_resolution: "invalid", masked_target: null, bindings: [],
      issues: [{ issue_id: "i", code: "binding_invalid", message: "bad", action: "edit_request", location: { kind: "body", field: "body", selector: [], utf16_span: { start: 4, end: 2 } } }],
      auth: { required: false, status: "none", injection_slots: [], requires_worker_verification: false }, config_basis: basis, context_fingerprint: "fp-1", resolution_context: null,
    })).toThrow(/end 不能小于 start/);
  });

  it("全空行坐标不能被当成 v1 位置接受", () => {
    expect(() => toResolutionPreview({
      schema_version: 1,
      scope: { workspace_id: "w1", project_id: "p1", environment_id: "env-1" }, ready: true,
      ordinary_resolution: "ready", masked_target: { url: "http://echo", method: "GET" },
      bindings: [{ binding_id: "bad", reference: "{{x}}", name: "x", location: { kind: "query", field: "value", row_id: null, index: null, occurrence: null, input_fingerprint: null, selector: null, utf16_span: null }, source: { level: "project", resource_id: "p1", revision: 1, value: { type: "string", text: "x" }, unavailable_reason: null }, overridden_sources: [], value_type: "string", rendered_preview: "x" }],
      issues: [], auth: { required: false, status: "none", injection_slots: [], requires_worker_verification: false },
      config_basis: basis, context_fingerprint: "fp", resolution_context: "token",
    })).toThrow(/v1 行缺少/);
  });

  it("viewer 普通预览的 unchecked 身份元数据可读但不等于可运行", () => {
    const result = toResolutionPreview({
      schema_version: 1, scope: { workspace_id: "w1", project_id: "p1", environment_id: "env-1" },
      ready: false, ordinary_resolution: "ready", masked_target: { url: "http://echo", method: "GET" }, bindings: [], issues: [],
      auth: { required: true, status: "unchecked", injection_slots: [], requires_worker_verification: true },
      config_basis: basis, context_fingerprint: "viewer-fp", resolution_context: "viewer-token",
    });
    expect(result.auth).toMatchObject({ required: true, status: "unchecked", requires_worker_verification: true });
    expect(result.ready).toBe(false);
    expect(result.resolution_context).toBe("viewer-token");
  });
});
