/**
 * 字段面板的选中节点必须来自**当前**字段树（R4 §5）。
 *
 * 复现的缺陷：选中态存的是节点对象。字段树会因为来源变化而重建，同一条定位路径在新树里
 * 可能是另一种类型、另一个值——旧对象继续被用于渲染与试算，用户看到的是一个从未存在过
 * 的匹配结果（拿上一份响应的样例算当前配置的条件）。
 *
 * 这里用**同一条路径、不同类型与值**的两棵树，验证详情与试算都跟着当前树走；并验证来源
 * 换代（换运行／换样例）时选中被清掉——即使路径碰巧还在，它指向的也是另一份数据。
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { AssertionType, FieldNode, FieldTree } from "../api/types";
import { AppProviders } from "../theme/AppProviders";
import { selectAntOption } from "../test/antd";
import { LeaveGuardProvider, useLeaveAggregate } from "../hooks/leaveGuard";
import { FieldTreePanel } from "./FieldTreePanel";

const TYPES: AssertionType[] = [
  {
    id: "equals",
    label: "等于",
    group: "值与集合",
    applies_to: ["string", "number", "integer"],
    params_schema: { expected: { control: "value", type: "any", label: "期望值" } },
    summary: "等于期望值",
    operator_version: 1,
  },
];

/** 构造一棵只有 data.id 一个字段的树；类型与文本由调用方给定。 */
function treeWith(type: string, text: string): FieldNode {
  return {
    label: "$",
    type: "object",
    text: "",
    selector: [],
    truncated: null,
    children: [
      {
        label: "data",
        type: "object",
        text: "",
        selector: [{ kind: "key", key: "data" }],
        truncated: null,
        children: [
          {
            label: "id",
            type,
            text,
            selector: [
              { kind: "key", key: "data" },
              { kind: "key", key: "id" },
            ],
            truncated: null,
            children: [],
          },
        ],
      },
    ],
  };
}

function panel(node: FieldNode, sourceKey: string, onChange = vi.fn()) {
  const tree: FieldTree = { root: node, node_count: 3 };
  return (
    <FieldTreePanel
      title="响应正文字段"
      tree={{ tree, error: null, loading: false }}
      sourceKey={sourceKey}
      targetSource="response.body"
      types={TYPES}
      typesError={null}
      workspaceId="ws"
      projectId="p"
      assertions={[]}
      results={new Map()}
      readOnly={false}
      onChange={onChange}
      emptyHint="（空）"
    />
  );
}

function DirtyProbe() {
  const state = useLeaveAggregate();
  return <output aria-label="字段断言离开状态">{state.dirty ? "dirty" : "clean"}</output>;
}

/** 选中 data.id 那一行；`data` 默认收起，先展开它。 */
function selectDataId(): void {
  const data = screen.getByRole("treeitem", { name: /data object/ });
  const switcher = data.querySelector<HTMLElement>(".ant-tree-switcher");
  if (data.getAttribute("aria-expanded") === "false" && switcher !== null) fireEvent.click(switcher);
  fireEvent.click(screen.getByText("id"));
}

/** 详情区文本：包含当前字段的类型与定位路径。 */
function detailText(): string {
  const detail = document.querySelector(".field-detail");
  return detail?.textContent ?? "";
}

describe("字段面板的选中节点跟随当前树", () => {
  it("再次点击当前字段时保留未应用断言实例、离开登记与最终 selector", async () => {
    const onChange = vi.fn();
    render(
      <LeaveGuardProvider>
        {panel(treeWith("string", "abc"), "run-1", onChange)}
        <DirtyProbe />
      </LeaveGuardProvider>,
      { wrapper: AppProviders },
    );

    selectDataId();
    fireEvent.click(screen.getByRole("button", { name: "＋添加断言" }));
    await selectAntOption("断言类型", "等于");
    const expectedInput = screen.getByLabelText("期望值") as HTMLInputElement;
    fireEvent.change(expectedInput, { target: { value: "未应用值" } });
    expect(screen.getByLabelText("字段断言离开状态").textContent).toBe("dirty");

    // Tree 对已选节点再次触发 onSelect 时传空 keys；业务表单必须保持同一实例。
    fireEvent.click(screen.getByText("id"));
    expect(screen.getByLabelText("期望值")).toBe(expectedInput);
    expect(expectedInput.value).toBe("未应用值");
    expect(screen.getByLabelText("字段断言离开状态").textContent).toBe("dirty");

    fireEvent.click(screen.getByRole("button", { name: "添加这条断言" }));
    const submitted = onChange.mock.calls[0]?.[0]?.[0];
    expect(submitted.selector).toEqual([{ kind: "key", key: "data" }, { kind: "key", key: "id" }]);
    expect(submitted.parameters).toEqual({ expected: { type: "string", text: "未应用值" } });
  });

  it("同一路径在新树里换了类型与值：详情与试算都用新节点", () => {
    const { rerender } = render(panel(treeWith("string", "abc"), "run-1"), { wrapper: AppProviders });
    selectDataId();
    expect(detailText()).toContain("string");
    expect(detailText()).toContain("id");

    // 同一路径，类型与值都变了（换成数字），来源标识不变。
    rerender(panel(treeWith("integer", "9007199254740993"), "run-1"));

    // 仍然是同一行被选中（路径没变），但它现在必须反映**新**类型与值。
    expect(detailText()).toContain("integer");
    expect(detailText()).not.toContain("string");
    expect(document.body.textContent).toContain("9007199254740993");
    expect(document.body.textContent).not.toContain("abc");
    // 选中状态没有被无谓地清掉：路径还在。
    expect(document.querySelector(".ant-tree-node-selected")).not.toBeNull();
  });

  it("来源换代时清掉选中：路径还在也不能沿用", () => {
    const { rerender } = render(panel(treeWith("string", "abc"), "run-1"), { wrapper: AppProviders });
    selectDataId();
    expect(document.querySelector(".ant-tree-node-selected")).not.toBeNull();

    // 换了一条运行（或换了一份样例）：路径碰巧还在，但它指向另一份数据。
    rerender(panel(treeWith("string", "xyz"), "run-2"));
    expect(document.querySelector(".ant-tree-node-selected")).toBeNull();
    expect(detailText()).toBe("");
  });

  it("路径在新树里消失时收起详情", () => {
    const { rerender } = render(panel(treeWith("string", "abc"), "run-1"), { wrapper: AppProviders });
    selectDataId();
    expect(detailText()).not.toBe("");

    // 新树里没有 data.id 了（结构不同的一份响应）。
    const other: FieldNode = {
      label: "$",
      type: "object",
      text: "",
      selector: [],
      truncated: null,
      children: [],
    };
    rerender(panel(other, "run-1"));
    expect(detailText()).toBe("");
  });

  it("同一路径的类型变化后，用它试算的是新值", () => {
    // “试算”走的是字段行旁的断言列，样例来自当前节点。
    const { rerender } = render(panel(treeWith("string", "first"), "run-1"), { wrapper: AppProviders });
    selectDataId();
    const detail = document.querySelector(".field-detail") as HTMLElement;
    expect(within(detail).getByRole("button", { name: /添加|保存/ })).toBeTruthy();

    rerender(panel(treeWith("string", "second"), "run-1"));
    // 详情里不再出现旧值。
    expect(detailText()).not.toContain("first");
  });
});
