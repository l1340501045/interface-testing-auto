import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { VariableContext } from "../api/types";
import { AppProviders } from "../theme/AppProviders";
import { selectAntOption } from "../test/antd";
import { KeyValueRows } from "./RequestParts";
import type { RawKeyValue } from "./requestDraft";
import { VariablePicker, VariableSourceDrawer, insertReference } from "./VariablePicker";

const context: VariableContext = {
  schema_version: 1,
  scope: { workspace_id: "w1", project_id: "p1", environment_id: "e1" },
  config_basis: { project_variables_version: 3, project_config_version_id: null, environment_rev: 4, environment_config_version: 2, environment_config_version_id: "cfg-2" },
  variables: [{
    name: "地区.代码", reference: "{{地区.代码}}", value: { type: "string", text: "cn" },
    effective_source: { level: "environment", resource_id: "e1", revision: 4, value: { type: "string", text: "cn" }, unavailable_reason: null },
    overridden_sources: [{ level: "project", resource_id: "p1", revision: 3, value: { type: "number", text: "9007199254740993" }, unavailable_reason: null }],
    available_locations: ["path", "query_value", "header_value", "body"], unavailable_reason: null,
    restricted_body_types: [],
  }],
};

describe("变量选择与来源", () => {
  it("按浏览器 UTF-16 选区替换非 BMP 字符", () => {
    expect(insertReference("a😀b", "{{地区.代码}}", 1, 3)).toEqual({ value: "a{{地区.代码}}b", cursor: 10 });
  });

  it("重复行只修改当前行并保留另一行", async () => {
    const onChange = vi.fn<(rows: RawKeyValue[]) => boolean>(() => true);
    render(<KeyValueRows
      rows={[{ row_id: "11111111-1111-7111-8111-111111111111", name: "x", value: "keep", enabled: true, description: "" }, { row_id: "22222222-2222-7222-8222-222222222222", name: "x", value: "a😀b", enabled: true, description: "" }]}
      label="查询参数" addLabel="添加" kind="query" version={2} readOnly={false}
      onChange={onChange} ownerRevision="owner:1" variablePicker={{ context, loading: false, error: null }}
    />, { wrapper: AppProviders });
    const second = screen.getByLabelText("查询参数值 2") as HTMLInputElement;
    second.focus();
    second.setSelectionRange(1, 3);
    fireEvent.select(second);
    await selectAntOption("插入变量到查询参数值 2", "地区.代码 · string · 环境配置第 4 版");
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange.mock.calls[0]?.[0]).toEqual([
      expect.objectContaining({ value: "keep" }),
      expect.objectContaining({ value: "a{{地区.代码}}b" }),
    ]);
  });

  it("选择器打开后 owner 修订变化，旧选择不写入新行状态", async () => {
    const onChange = vi.fn<(rows: RawKeyValue[]) => boolean>(() => true);
    const rows: RawKeyValue[] = [{ row_id: "11111111-1111-7111-8111-111111111111", name: "x", value: "before", enabled: true, description: "" }];
    const view = render(<KeyValueRows rows={rows} label="查询参数" addLabel="添加" kind="query" version={2} readOnly={false} onChange={onChange} ownerRevision="owner:1" variablePicker={{ context, loading: false, error: null }} />, { wrapper: AppProviders });
    const picker = screen.getByRole("combobox", { name: "插入变量到查询参数值 1" });
    fireEvent.mouseDown(picker);
    view.rerender(<KeyValueRows rows={rows} label="查询参数" addLabel="添加" kind="query" version={2} readOnly={false} onChange={onChange} ownerRevision="owner:2" variablePicker={{ context, loading: false, error: null }} />);
    const option = await screen.findByText("地区.代码 · string · 环境配置第 4 版");
    fireEvent.click(option);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("来源抽屉显示生效环境项及被覆盖项目项的独立类型", () => {
    render(<VariableSourceDrawer context={context} loading={false} error={null} onReload={vi.fn()} />, { wrapper: AppProviders });
    fireEvent.click(screen.getByRole("button", { name: "查看变量来源" }));
    expect(screen.getByText("环境配置第 4 版")).toBeTruthy();
    expect(screen.getByText(/项目变量第 3 版/)).toBeTruthy();
    expect(screen.getByText("9007199254740993")).toBeTruthy();
  });

  it("不可表达旧名不插入，受限名称不开放 form 正文入口", () => {
    const restricted: VariableContext = {
      ...context,
      variables: [
        { ...context.variables[0]!, name: "请求 ID", reference: null, available_locations: [], unavailable_reason: "名称不能表示为模板" },
        { ...context.variables[0]!, name: "a+b", reference: "{{a+b}}", restricted_body_types: ["form"] },
      ],
    };
    const view = render(<VariablePicker label="正文原文" location="body" bodyType="form" context={restricted} loading={false} error={null} disabled={false} ownerRevision="1" onInsert={vi.fn()} />, { wrapper: AppProviders });
    expect(screen.getByRole("combobox", { name: "插入变量到正文原文" }).hasAttribute("disabled")).toBe(true);
    view.rerender(<VariablePicker label="路径" location="path" context={restricted} loading={false} error={null} disabled={false} ownerRevision="1" onInsert={vi.fn()} />);
    expect(screen.getByRole("combobox", { name: "插入变量到路径" }).hasAttribute("disabled")).toBe(false);
  });
});
