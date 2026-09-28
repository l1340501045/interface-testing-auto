import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { CaseAssertion } from "../api/types";
import { AppProviders } from "../theme/AppProviders";
import { selectAntOption } from "../test/antd";
import { AssertionTab } from "./AssertionTab";
import type { RawRequest } from "./requestDraft";

const OLD = "11111111-1111-7111-8111-111111111111";
const CURRENT = "22222222-2222-7222-8222-222222222222";
const assertion: CaseAssertion = {
  id: "orphan-1",
  target_source: "request.query",
  selector: [{ kind: "row", row_id: OLD }, { kind: "key", key: "value" }],
  type: "equals",
  parameters: { expected: { type: "string", text: "two" } },
  compare_as: null,
  severity: "error",
  enabled: true,
  sort_order: 0,
};

function request(rowId = CURRENT, enabled = true): RawRequest {
  return {
    schema_version: 2,
    method: "GET",
    path: "/",
    query_params: [{ row_id: rowId, name: "fresh", value: "1", enabled, description: "" }],
    headers: [],
    body_type: "none",
    body: "",
  };
}

function view(onChange = vi.fn(), readOnly = false, currentRequest = request()) {
  render(
    <AssertionTab workspaceId="w" projectId="p" types={[]} typesError={null} assertions={[assertion]} results={new Map()} readOnly={readOnly} onChange={onChange} bodyTree={{ tree: null, error: null, loading: false }} bodySourceKey="" bodyHint="空" request={currentRequest} />,
    { wrapper: AppProviders },
  );
  return onChange;
}

describe("孤立 row 条件处理", () => {
  it("明确标记字段已删除，并由用户选择当前同类字段重绑，尾部语义保持", async () => {
    const onChange = view();
    expect(screen.getByText(/字段已删除/)).toBeTruthy();
    await selectAntOption("重新绑定条件 orphan-1", "1. fresh");
    const next = onChange.mock.calls[0][0][0] as CaseAssertion;
    expect(next.selector).toEqual([{ kind: "row", row_id: CURRENT }, { kind: "key", key: "value" }]);
    expect(next.parameters).toEqual(assertion.parameters);
  });

  it("可显式删除", () => {
    const onChange = view();
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    expect(onChange).toHaveBeenCalledWith([]);
  });

  it("只读角色无写入口", () => {
    view(vi.fn(), true);
    expect(screen.getByText(/字段已删除/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "删除" })).toBeNull();
    expect(screen.queryByLabelText("重新绑定条件 orphan-1")).toBeNull();
  });

  it("停用但仍存在的行不标记为已删除", () => {
    view(vi.fn(), false, request(OLD, false));
    expect(screen.queryByText(/字段已删除/)).toBeNull();
  });
});
