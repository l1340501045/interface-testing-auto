/**
 * 运行状态与原因的呈现（最终收尾）。
 *
 * 两条容易被写成误导文案的分支：
 *
 * 1. **已取消的运行不附加原因。** 取消接口把 `reason_category` 写成 `policy`（那是它的
 *    收尾方式），按分类映射直接显示就变成“已取消（被安全策略阻止）”——用户会以为这次
 *    运行是被策略拦下的，而实际是有人点了取消。
 * 2. **来源未确定不等于已有旧结果。** 新建请求第一次排队时还没有响应与 context，此前会
 *    显示“显示的是上一次发送的结果”。排队中应显示等待，只有确有终态结果且与当前输入
 *    不同源时才提示历史。
 */
import { describe, expect, it } from "vitest";

import type { RunSummary } from "../api/types";
import { isTerminal, runOutcomeLabel, runReasonLabel, runStateLabel } from "./useRuns";

function run(over: Partial<RunSummary> = {}): RunSummary {
  return {
    id: "run-1",
    target_type: "debug_snapshot",
    case_version_id: null,
    environment_id: "env-1",
    state: "finished",
    outcome: "passed",
    reason_category: null,
    pool_id: null,
    created_at: "2026-09-15T00:00:00Z",
    ...over,
  };
}

describe("运行状态与原因", () => {
  it("已取消：只显示“已取消”，不附加任何原因", () => {
    // 取消接口写入的 reason_category 就是 policy；这正是主审在真实浏览器里看到
    // “已取消（被安全策略阻止）”的来源。
    const canceled = run({ outcome: "canceled", reason_category: "policy" });
    expect(runOutcomeLabel(canceled)).toBe("已取消");
    expect(runReasonLabel(canceled)).toBeNull();
  });

  it("已取消不因分类不同而例外：取消本身已经说明一切", () => {
    for (const category of ["policy", "configuration", "assertion", "network", "authentication"]) {
      expect(runReasonLabel(run({ outcome: "canceled", reason_category: category }))).toBeNull();
    }
  });

  it("非取消的运行仍按分类给出原因", () => {
    expect(runReasonLabel(run({ outcome: "error", reason_category: "policy" }))).toBe("被安全策略阻止");
    expect(runReasonLabel(run({ outcome: "failed", reason_category: "assertion" }))).toBe("断言未通过");
    expect(runReasonLabel(run({ outcome: "error", reason_category: "authentication" }))).toBe(
      "认证或凭证被拒绝",
    );
    expect(runReasonLabel(run({ outcome: "passed", reason_category: null }))).toBeNull();
  });

  it("排队与执行中都不是终态：此时没有可展示的结果", () => {
    // 终态判据是服务端状态，不是“有没有报告对象”。排队中的运行会有一个 RunOut，
    // 但它没有任何结论——界面上不能说“上一次发送的结果”。
    expect(isTerminal(run({ state: "created", outcome: null }))).toBe(false);
    expect(isTerminal(run({ state: "queued", outcome: null }))).toBe(false);
    expect(isTerminal(run({ state: "running", outcome: null }))).toBe(false);
    expect(isTerminal(run({ state: "finished" }))).toBe(true);
    expect(runStateLabel(run({ state: "queued" }))).toBe("排队中");
  });
});
