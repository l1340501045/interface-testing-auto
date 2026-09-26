import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { ResizableWorkbench } from "./ResizableWorkbench";

describe("请求响应分栏", () => {
  beforeEach(() => window.localStorage.clear());

  it("切换方向时不卸载两侧内容，并只保存布局偏好", () => {
    const { container } = render(
      <ResizableWorkbench request={<input aria-label="请求草稿" defaultValue="未保存内容" />} response={<p>响应证据</p>} />,
    );
    const draft = screen.getByLabelText("请求草稿") as HTMLInputElement;
    fireEvent.change(draft, { target: { value: "继续编辑" } });
    fireEvent.click(screen.getByRole("button", { name: "左右" }));

    expect(screen.getByLabelText("请求草稿")).toBe(draft);
    expect(draft.value).toBe("继续编辑");
    expect(container.querySelector(".workbench-horizontal")).not.toBeNull();
    expect(window.localStorage.getItem("interface-workbench-direction")).toBe("horizontal");
  });

  it("分隔条支持键盘调整和重置", () => {
    render(<ResizableWorkbench request={<p>请求</p>} response={<p>响应</p>} />);
    const separator = screen.getByRole("separator", { name: "调整请求与响应区域大小" });
    expect(separator.getAttribute("aria-valuenow")).toBe("50");
    fireEvent.keyDown(separator, { key: "ArrowRight" });
    expect(separator.getAttribute("aria-valuenow")).toBe("55");
    fireEvent.keyDown(separator, { key: "Home" });
    expect(separator.getAttribute("aria-valuenow")).toBe("50");
  });

  it("关键控制区位于可调分栏之外，调整布局不会卸载它", () => {
    const { container } = render(
      <ResizableWorkbench
        controls={<button type="button">发送当前请求</button>}
        request={<p>请求参数</p>}
        response={<p>响应</p>}
      />,
    );

    const control = screen.getByRole("button", { name: "发送当前请求" });
    expect(control.closest(".workbench-controls")).not.toBeNull();
    expect(control.closest(".workbench-split")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "左右" }));
    expect(screen.getByRole("button", { name: "发送当前请求" })).toBe(control);
    expect(container.querySelector(".workbench-horizontal")).not.toBeNull();
  });
});
