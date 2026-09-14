/**
 * 断言列编辑目标切换的行为回归。
 *
 * 编辑表单在挂载时用被编辑的那条断言初始化；若切换编辑目标时复用同一个组件实例，
 * 表单里留下的还是上一条的参数。用户接着保存，就会把上一条的条件（连同它的边界值）
 * 写进当前这条，目标自身配置的上限随之丢失。
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import type { AssertionResult, AssertionType, CaseAssertion } from "../api/types";
import { AssertionColumn } from "./AssertionColumn";

const EXPECTED_NUMBER = { control: "value" as const, type: "number", label: "边界" };

const TYPES: AssertionType[] = [
  {
    id: "greater_than",
    label: "大于",
    group: "数值比较",
    applies_to: ["number", "integer"],
    params_schema: { expected: EXPECTED_NUMBER },
    summary: "值大于边界",
    operator_version: 1,
  },
  {
    id: "less_than",
    label: "小于",
    group: "数值比较",
    applies_to: ["number", "integer"],
    params_schema: { expected: EXPECTED_NUMBER },
    summary: "值小于边界",
    operator_version: 1,
  },
];

function makeAssertion(id: string, type: string, expected: string, sortOrder: number): CaseAssertion {
  return {
    id,
    target_source: "response.body",
    selector: [{ kind: "key", key: "data" }, { kind: "key", key: "amount" }],
    type,
    parameters: { expected: { type: "number", text: expected } },
    compare_as: null,
    severity: "error",
    enabled: true,
    sort_order: sortOrder,
  };
}

function Harness({ initial, onChange }: { initial: CaseAssertion[]; onChange?: (next: CaseAssertion[]) => void }) {
  const [assertions, setAssertions] = useState<CaseAssertion[]>(initial);
  return (
    <AssertionColumn
      types={TYPES}
      typesError={null}
      workspaceId="11111111-1111-4111-8111-111111111111"
      projectId="22222222-2222-4222-8222-222222222222"
      field={{ targetSource: "response.body", selector: [{ kind: "key", key: "data" }], fieldType: "number" }}
      own={assertions}
      sample={null}
      results={new Map<string, AssertionResult>()}
      readOnly={false}
      onUpsert={(next) => {
        const merged = assertions.some((item) => item.id === next.id)
          ? assertions.map((item) => (item.id === next.id ? next : item))
          : [...assertions, next];
        setAssertions(merged);
        onChange?.(merged);
      }}
      onRemove={(id) => setAssertions(assertions.filter((item) => item.id !== id))}
    />
  );
}

/** 打开第 index 条条件的编辑器。 */
function openEditor(index: number) {
  const modifyButtons = screen.getAllByRole("button", { name: "修改" });
  fireEvent.click(modifyButtons[index]);
}

describe("AssertionColumn 切换编辑目标", () => {
  it("切到另一条条件时表单显示目标自身的参数，不残留上一条", () => {
    render(
      <Harness
        initial={[makeAssertion("a-gt", "greater_than", "0", 0), makeAssertion("b-lt", "less_than", "100", 1)]}
      />,
    );

    openEditor(0);
    const typeSelect = screen.getByLabelText("断言类型") as HTMLSelectElement;
    expect(typeSelect.value).toBe("greater_than");
    expect((screen.getByLabelText("边界") as HTMLInputElement).value).toBe("0");

    openEditor(1);
    const switchedType = screen.getByLabelText("断言类型") as HTMLSelectElement;
    expect(switchedType.value).toBe("less_than");
    // 上限必须还是目标自己配置的 100，而不是上一条的 0。
    expect((screen.getByLabelText("边界") as HTMLInputElement).value).toBe("100");
  });

  it("切到另一条条件后保存，改写的是目标这条且不丢它的上限", () => {
    let captured: CaseAssertion[] = [];
    render(
      <Harness
        initial={[makeAssertion("a-gt", "greater_than", "0", 0), makeAssertion("b-lt", "less_than", "100", 1)]}
        onChange={(next) => {
          captured = next;
        }}
      />,
    );

    openEditor(1);
    fireEvent.click(screen.getByRole("button", { name: "保存这条断言" }));

    const saved = captured.find((item) => item.id === "b-lt");
    expect(saved?.type).toBe("less_than");
    expect(saved?.parameters).toEqual({ expected: { type: "number", text: "100" } });

    // 另一条条件保持原样，没有被这次编辑顺手改写。
    const untouched = captured.find((item) => item.id === "a-gt");
    expect(untouched?.type).toBe("greater_than");
    expect(untouched?.parameters).toEqual({ expected: { type: "number", text: "0" } });
  });

  it("在同一字段上先改大于再改小于，两条条件各自保留自己的边界", () => {
    let captured: CaseAssertion[] = [];
    render(
      <Harness
        initial={[makeAssertion("a-gt", "greater_than", "0", 0), makeAssertion("b-lt", "less_than", "100", 1)]}
        onChange={(next) => {
          captured = next;
        }}
      />,
    );

    // 第一条：把 0 改成 5。
    openEditor(0);
    fireEvent.change(screen.getByLabelText("边界"), { target: { value: "5" } });
    fireEvent.click(screen.getByRole("button", { name: "保存这条断言" }));
    expect(captured.find((item) => item.id === "a-gt")?.parameters).toEqual({
      expected: { type: "number", text: "5" },
    });

    // 第二条：表单必须是它自己的 100，直接保存不改动。
    openEditor(1);
    expect((screen.getByLabelText("边界") as HTMLInputElement).value).toBe("100");
    fireEvent.click(screen.getByRole("button", { name: "保存这条断言" }));

    expect(captured.find((item) => item.id === "b-lt")?.parameters).toEqual({
      expected: { type: "number", text: "100" },
    });
  });
});

// 断言列表本身仍逐条渲染：这里的关注点是编辑表单，不是列表结构。
describe("AssertionColumn 列表", () => {
  it("同字段的两条条件都出现在列表里", () => {
    render(
      <Harness
        initial={[makeAssertion("a-gt", "greater_than", "0", 0), makeAssertion("b-lt", "less_than", "100", 1)]}
      />,
    );
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(within(items[0]).getByText(/大于/)).toBeTruthy();
    expect(within(items[1]).getByText(/小于/)).toBeTruthy();
  });
});
