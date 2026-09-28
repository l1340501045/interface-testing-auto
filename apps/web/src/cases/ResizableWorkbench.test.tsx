import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ResizableWorkbench } from "./ResizableWorkbench";

describe("请求响应分栏", () => {
  beforeEach(() => window.localStorage.clear());

  it("切换方向时使用当前轴比例且不卸载两侧内容", async () => {
    const { container } = render(
      <ResizableWorkbench request={<input aria-label="请求草稿" defaultValue="未保存内容" />} response={<p>响应证据</p>} />,
    );
    const frame = container.querySelector<HTMLElement>(".workbench-split-frame");
    if (frame === null) throw new Error("工作台分栏容器未挂载");
    const draft = screen.getByLabelText("请求草稿") as HTMLInputElement;
    fireEvent.change(draft, { target: { value: "继续编辑" } });
    fireEvent.click(screen.getByRole("radio", { name: "左右" }));

    expect(screen.getByLabelText("请求草稿")).toBe(draft);
    expect(draft.value).toBe("继续编辑");
    expect(container.querySelector(".workbench-horizontal")).not.toBeNull();
    expect(frame.style.getPropertyValue("--split-ratio")).toBe("50%");
    expect(screen.getByRole("separator", { name: "调整请求与响应区域大小" }).getAttribute("aria-orientation")).toBe("vertical");
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

  it("指针按当前方向的真实外框计算比例并限制在25至75", () => {
    const { container } = render(<ResizableWorkbench request={<p>请求</p>} response={<p>响应</p>} />);
    const frame = container.querySelector<HTMLElement>(".workbench-split-frame");
    if (frame === null) throw new Error("工作台分栏容器未挂载");
    vi.spyOn(frame, "getBoundingClientRect").mockImplementation(() => ({
      x: 10, y: 20, top: 20, left: 10, right: 1046, bottom: 540,
      width: 1036, height: 520, toJSON: () => ({}),
    }));
    fireEvent.click(screen.getByRole("radio", { name: "左右" }));
    const separator = screen.getByRole("separator", { name: "调整请求与响应区域大小" });
    Object.defineProperties(separator, {
      setPointerCapture: { configurable: true, value: vi.fn() },
      hasPointerCapture: { configurable: true, value: () => true },
    });

    fireEvent.pointerDown(separator, { pointerId: 1, clientX: 787, clientY: 100 });
    expect(separator.getAttribute("aria-valuenow")).toBe("75");
    fireEvent.pointerMove(separator, { pointerId: 1, clientX: 114, clientY: 100 });
    expect(separator.getAttribute("aria-valuenow")).toBe("25");
    fireEvent.doubleClick(separator);
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

    fireEvent.click(screen.getByRole("radio", { name: "左右" }));
    expect(screen.getByRole("button", { name: "发送当前请求" })).toBe(control);
    expect(container.querySelector(".workbench-horizontal")).not.toBeNull();
  });
});
