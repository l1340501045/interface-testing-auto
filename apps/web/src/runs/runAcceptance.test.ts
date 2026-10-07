import { describe, expect, it } from "vitest";

import { ApiError } from "../api/client";
import { isInitialRunRejection } from "./runAcceptance";

describe("首次受理拒绝必须同时匹配 status 与 code", () => {
  it.each([
    [400, "variable_undefined"],
    [400, "binding_invalid"],
    [400, "credential_slot_conflict"],
    [409, "resolution_context_changed"],
    [409, "config_inconsistent"],
    [400, "service_invalid"],
    [409, "service_unavailable"],
    [409, "mapping_missing"],
    [409, "service_contract_required"],
  ])("识别 %s/%s 为受理前确定拒绝", (status, code) => {
    expect(isInitialRunRejection(new ApiError(status, code, "拒绝", null))).toBe(true);
  });

  it("同码错误状态和未知 4xx 信封仍不能释放 unknown", () => {
    expect(isInitialRunRejection(new ApiError(503, "variable_undefined", "上游异常", null))).toBe(false);
    expect(isInitialRunRejection(new ApiError(400, "future_rejection", "未知", null))).toBe(false);
  });
});
