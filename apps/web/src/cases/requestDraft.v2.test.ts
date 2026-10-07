import { describe, expect, it } from "vitest";

import { toRequestSpec } from "../api/guards";
import type { CaseAssertion } from "../api/types";
import { preserveServiceTarget, rawToSpec, requestToRaw, sameRequest, upgradeRequestV2 } from "./requestDraft";

const V1 = {
  method: "GET",
  path: "/items",
  query_params: [{ name: "tag", value: "a" }, { name: "tag", value: "b" }],
  headers: [],
  body_type: "none" as const,
  body: "",
};

describe("RequestSpec v1/v2 草稿", () => {
  it("旧请求读取后保持 v1 且不产生脏状态", () => {
    const parsed = toRequestSpec(V1);
    const raw = requestToRaw(parsed);
    expect(raw.schema_version).toBeUndefined();
    expect(raw.query_params[0].row_id).toBeUndefined();
    expect(sameRequest(raw, requestToRaw(parsed))).toBe(true);
    expect(rawToSpec(raw)).toEqual(V1);
  });

  it("首次行操作原子分配稳定 ID 并迁移可确定的重复键定位", () => {
    const assertion: CaseAssertion = {
      id: "a1",
      target_source: "request.query",
      selector: [{ kind: "repeat_key", key: "tag", occurrence: 1 }],
      type: "equals",
      parameters: {},
      compare_as: null,
      severity: "error",
      enabled: true,
      sort_order: 0,
    };
    const upgraded = upgradeRequestV2(requestToRaw(V1), [assertion]);
    expect(upgraded.request.schema_version).toBe(2);
    expect(upgraded.migrated).toBe(1);
    const ids = upgraded.request.query_params.map((row) => row.row_id);
    expect(new Set(ids).size).toBe(2);
    expect(upgraded.assertions[0].selector).toEqual([
      { kind: "row", row_id: ids[1] },
      { kind: "key", key: "value" },
    ]);
    const moved = { ...upgraded.request, query_params: [...upgraded.request.query_params].reverse() };
    expect(rawToSpec(moved).query_params.map((row) => "row_id" in row ? row.row_id : "")).toEqual([...ids].reverse());
  });

  it("index 迁移保留原尾部且不误加 value", () => {
    const assertion: CaseAssertion = {
      id: "index-row",
      target_source: "request.query",
      selector: [{ kind: "index", index: 0 }, { kind: "key", key: "name" }],
      type: "equals",
      parameters: {},
      compare_as: null,
      severity: "error",
      enabled: true,
      sort_order: 0,
    };
    const upgraded = upgradeRequestV2(requestToRaw(V1), [assertion]);
    expect(upgraded.assertions[0].selector).toEqual([
      { kind: "row", row_id: upgraded.request.query_params[0].row_id },
      { kind: "key", key: "name" },
    ]);
  });

  it("严格拒绝 v1 混入元数据、v2 重复 ID 和非布尔 enabled", () => {
    expect(() => toRequestSpec({ ...V1, query_params: [{ ...V1.query_params[0], enabled: false }] })).toThrow("旧请求");
    const id = "00000000-0000-4000-8000-000000000001";
    const row = { row_id: id, name: "a", value: "1", enabled: true, description: "" };
    expect(() => toRequestSpec({ ...V1, schema_version: 2, query_params: [row], headers: [row] })).toThrow("ID 重复");
    expect(() => toRequestSpec({ ...V1, schema_version: 2, query_params: [{ ...row, enabled: "true" }], headers: [] })).toThrow("布尔值");
  });

  it("v1 不受 v2 500 行上限影响，v2 接受 UUID v7 与按 Unicode 字符计数的说明", () => {
    const legacy = { ...V1, query_params: Array.from({ length: 501 }, (_, index) => ({ name: `q${index}`, value: "" })) };
    expect(toRequestSpec(legacy).query_params).toHaveLength(501);
    const row = {
      row_id: "01890f3e-7b8a-7cc2-9a1b-123456789abc",
      name: "emoji",
      value: "",
      enabled: true,
      description: "😀".repeat(600),
    };
    expect(toRequestSpec({ ...V1, schema_version: 2, query_params: [row], headers: [] }).query_params[0]).toEqual(row);
  });

  it("非法原始编辑态参与 dirty 比较但不会抛出 render", () => {
    const baseline = requestToRaw(V1);
    expect(() => sameRequest(baseline, { ...baseline, query_params: [{ name: "", value: "先填值" }] })).not.toThrow();
    expect(sameRequest(baseline, { ...baseline, query_params: [{ name: "", value: "先填值" }] })).toBe(false);
  });

  it("命名服务与行v1/v2正交往返，切回default时不补服务字段", () => {
    const serviceKey = "svc_11111111111111111111111111111111";
    const named = toRequestSpec({ ...V1, service_contract: 1, service_key: serviceKey });
    expect(rawToSpec(requestToRaw(named))).toEqual({ ...V1, service_contract: 1, service_key: serviceKey });
    const upgraded = upgradeRequestV2(requestToRaw(named), []).request;
    expect(rawToSpec(upgraded)).toMatchObject({ schema_version: 2, service_contract: 1, service_key: serviceKey });
    const defaultRaw = { ...requestToRaw(named), service_contract: undefined, service_key: undefined };
    expect(rawToSpec(defaultRaw)).toEqual(V1);
  });

  it("严格拒绝服务字段缺一、null、default显式目标和非法能力版本", () => {
    const serviceKey = "svc_11111111111111111111111111111111";
    expect(() => toRequestSpec({ ...V1, service_key: serviceKey })).toThrow("同时存在");
    expect(() => toRequestSpec({ ...V1, service_contract: 1, service_key: null })).toThrow("文本");
    expect(() => toRequestSpec({ ...V1, service_contract: 1, service_key: "default" })).toThrow("命名服务标识");
    expect(() => toRequestSpec({ ...V1, service_contract: 2, service_key: serviceKey })).toThrow("仅支持 1");
  });

  it("cURL应用保留现标签named选择，新独立default不猜服务", () => {
    const serviceKey = "svc_11111111111111111111111111111111";
    const imported = { ...requestToRaw(V1), method: "POST", path: "/from-curl" };
    expect(preserveServiceTarget({ ...requestToRaw(V1), service_contract: 1, service_key: serviceKey }, imported)).toMatchObject({ method: "POST", service_contract: 1, service_key: serviceKey });
    expect(preserveServiceTarget(requestToRaw(V1), { ...imported, service_contract: 1, service_key: serviceKey })).not.toHaveProperty("service_key");
  });
});
