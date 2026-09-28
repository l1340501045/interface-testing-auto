/**
 * 出参字段断言面板的行为回归。
 *
 * 复现主审浏览器实测的场景：没有响应样例时，对 data.id 依次添加“存在”和“非 null”
 * 两条条件，删除“存在”后“非 null”必须仍然存在。条目的归属单位是断言标识，不是
 * 整个字段——按字段整组替换会让同字段的其他条件一起消失。
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import type { AssertionType, CaseAssertion, RunReport } from "../api/types";
import { ResponseFieldPanel } from "./ResponseFieldPanel";

/** 与后端目录同形的类型定义；测试只关心表单控件与保存下来的参数。 */
const TYPES: AssertionType[] = [
  {
    id: "exists",
    label: "存在",
    group: "字段存在",
    applies_to: ["string", "number", "integer", "boolean", "null", "object", "array"],
    params_schema: {},
    summary: "字段存在",
    operator_version: 1,
  },
  {
    id: "not_null",
    label: "非 null",
    group: "空值",
    applies_to: ["string", "number", "integer", "boolean", "null", "object", "array"],
    params_schema: {},
    summary: "字段值不是 null",
    operator_version: 1,
  },
  {
    id: "equals",
    label: "等于",
    group: "值与集合",
    applies_to: ["string", "number", "integer", "boolean", "null", "object", "array"],
    params_schema: { expected: { control: "value", type: "any", label: "期望值" } },
    summary: "值严格等于期望值",
    operator_version: 1,
  },
];

function Harness({ initial }: { initial: CaseAssertion[] }) {  const [assertions, setAssertions] = useState<CaseAssertion[]>(initial);
  return (
    <ResponseFieldPanel
      workspaceId="11111111-1111-4111-8111-111111111111"
      projectId="22222222-2222-4222-8222-222222222222"
      report={null as RunReport | null}
      types={TYPES}
      typesError={null}
      assertions={assertions}
      results={new Map()}
      readOnly={false}
      onChange={setAssertions}
    />
  );
}

/** 最近一次提交给面板的断言集合，用于断言请求体而不是界面文本。 */
let latest: CaseAssertion[] = [];

function makeAssertion(id: string, type: string, sortOrder: number): CaseAssertion {
  return {
    id,
    target_source: "response.body",
    selector: [{ kind: "key", key: "data" }, { kind: "key", key: "id" }],
    type,
    parameters: {},
    compare_as: null,
    severity: "error",
    enabled: true,
    sort_order: sortOrder,
  };
}

/** 该字段行（含标题的那一块）内可见的条件文本。 */
function conditionTexts(fieldLabel: string): string[] {
  const heading = screen.getByText(fieldLabel);
  const block = heading.closest(".response-field");
  if (block === null) throw new Error(`找不到字段块：${fieldLabel}`);
  return within(block as HTMLElement)
    .getAllByRole("listitem")
    .map((item) => item.textContent ?? "");
}

describe("ResponseFieldPanel 同字段多条断言", () => {
  it("删除其中一条时不牵连同字段的其他条件", () => {
    render(
      <Harness
        initial={[
          makeAssertion("a-exists", "exists", 0),
          makeAssertion("b-not-null", "not_null", 1),
        ]}
      />,
    );

    expect(conditionTexts("响应正文 data.id")).toHaveLength(2);

    const heading = screen.getByText("响应正文 data.id");
    const block = heading.closest(".response-field") as HTMLElement;
    const firstRow = within(block).getAllByRole("listitem")[0];
    fireEvent.click(within(firstRow).getByRole("button", { name: "删除" }));

    const remaining = conditionTexts("响应正文 data.id");
    expect(remaining).toHaveLength(1);
    // 剩下的是“非 null”那条，不是被连带清空后的空白行。
    expect(remaining[0]).toContain(TYPES[1].summary);
    expect(remaining[0]).not.toContain(TYPES[0].summary);
  });

  it("同一字段上的两条条件并排显示，而不是拆成两行", () => {
    render(
      <Harness
        initial={[
          makeAssertion("a-exists", "exists", 0),
          makeAssertion("b-not-null", "not_null", 1),
        ]}
      />,
    );

    // 字段标题只出现一次：同字段的多条条件必须聚在同一行里。
    expect(screen.getAllByText("响应正文 data.id")).toHaveLength(1);
    expect(conditionTexts("响应正文 data.id")).toHaveLength(2);
  });

  it("删除一条后仍可为该字段继续添加条件", () => {
    render(
      <Harness
        initial={[
          makeAssertion("a-exists", "exists", 0),
          makeAssertion("b-not-null", "not_null", 1),
        ]}
      />,
    );

    const heading = screen.getByText("响应正文 data.id");
    const block = heading.closest(".response-field") as HTMLElement;
    const firstRow = within(block).getAllByRole("listitem")[0];
    fireEvent.click(within(firstRow).getByRole("button", { name: "删除" }));

    expect(conditionTexts("响应正文 data.id")).toHaveLength(1);
    expect(within(block).getByRole("button", { name: "＋添加断言" })).toBeTruthy();
  });
});

describe("不在样例树里的字段：已保存字面量是类型证据", () => {
  /** 记录每次提交的结果，用来断言提交出去的参数本身，而不只是界面文本。 */
  function RecordingHarness({ initial }: { initial: CaseAssertion[] }) {
    const [assertions, setAssertions] = useState<CaseAssertion[]>(initial);
    latest = assertions;
    return (
      <ResponseFieldPanel
        workspaceId="11111111-1111-4111-8111-111111111111"
        projectId="22222222-2222-4222-8222-222222222222"
        report={null as RunReport | null}
        types={TYPES}
        typesError={null}
        assertions={assertions}
        results={new Map()}
        readOnly={false}
        onChange={(next) => {
          latest = next;
          setAssertions(next);
        }}
      />
    );
  }

  it("保存过的布尔字面量让字段类型显示为布尔，而不是退回字符串", () => {
    const assertion: CaseAssertion = {
      ...makeAssertion("a-equals", "equals", 0),
      parameters: { expected: { type: "boolean", value: true } },
    };
    render(<RecordingHarness initial={[assertion]} />);

    const heading = screen.getByText("响应正文 data.id");
    const block = heading.closest(".response-field") as HTMLElement;
    const select = within(block).getByRole("combobox", { name: "字段类型" });
    const selectRoot = select.closest<HTMLElement>(".ant-select");
    expect(selectRoot?.textContent?.trim()).toBe("boolean");

    const row = within(block).getAllByRole("listitem")[0];
    fireEvent.click(within(row).getByRole("button", { name: "修改" }));
    fireEvent.click(screen.getByRole("button", { name: "保存这条断言" }));
    expect(latest[0].parameters).toEqual({ expected: { type: "boolean", value: true } });
  });

  it("修改既有断言后原样提交：数字字面量不会变成文本字面量", () => {
    const saved = { expected: { type: "number", text: "9007199254740993" } };
    const assertion: CaseAssertion = {
      ...makeAssertion("a-equals", "equals", 0),
      parameters: saved,
    };
    render(<RecordingHarness initial={[assertion]} />);

    const heading = screen.getByText("响应正文 data.id");
    const block = heading.closest(".response-field") as HTMLElement;
    const row = within(block).getAllByRole("listitem")[0];
    fireEvent.click(within(row).getByRole("button", { name: "修改" }));
    fireEvent.click(screen.getByRole("button", { name: "保存这条断言" }));

    // 只打开再保存，值一个字都没改：类型与文本都必须原样。
    expect(latest[0].parameters).toEqual(saved);
  });
});
