import { describe, expect, it } from "vitest";

import { scheduleEditorFieldFocus } from "./resolutionFocus";

describe("解析问题只定位当前编辑器", () => {
  it("两个保活编辑器含同名字段时只聚焦目标 root 内的输入", () => {
    const first = document.createElement("section");
    const second = document.createElement("section");
    first.innerHTML = '<input aria-label="查询参数值 1" value="first">';
    second.innerHTML = '<input aria-label="查询参数值 1" value="second">';
    document.body.append(first, second);
    scheduleEditorFieldFocus({ root: second, elementId: null, ariaLabel: "查询参数值 1", span: { start: 1, end: 3 }, isCurrent: () => true, schedule: (callback) => { callback(0); return 1; } });
    expect(document.activeElement).toBe(second.querySelector("input"));
    expect((second.querySelector("input") as HTMLInputElement).selectionStart).toBe(1);
    first.remove(); second.remove();
  });

  it("同帧切标签或修改输入后旧回调不抢焦点", () => {
    const root = document.createElement("section");
    root.innerHTML = '<input aria-label="查询参数值 1" value="target">';
    const outside = document.createElement("button");
    document.body.append(root, outside);
    outside.focus();
    let callback: FrameRequestCallback | null = null;
    let current = true;
    scheduleEditorFieldFocus({ root, elementId: null, ariaLabel: "查询参数值 1", isCurrent: () => current, schedule: (next) => { callback = next; return 1; } });
    current = false;
    (callback as FrameRequestCallback | null)?.(0);
    expect(document.activeElement).toBe(outside);
    root.remove(); outside.remove();
  });
});
