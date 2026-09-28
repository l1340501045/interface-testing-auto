import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { RequestTabs } from "./RequestTabs";

function Subject() {
  const [activeId, setActiveId] = useState("params");
  return (
    <RequestTabs
      idPrefix="request-test"
      activeId={activeId}
      onChange={setActiveId}
      tabs={[
        { id: "params", label: "参数", summary: 1, content: <input aria-label="参数草稿" defaultValue="001" /> },
        { id: "body", label: "请求体", content: <textarea aria-label="正文草稿" defaultValue="9007199254740993" /> },
      ]}
    />
  );
}

describe("请求编辑标签", () => {
  it("真实Tabs从首次渲染起保持全部业务面板，往返不重建未应用输入", () => {
    render(<Subject />);
    const params = screen.getByLabelText("参数草稿") as HTMLInputElement;
    const body = screen.getByLabelText("正文草稿", { selector: "textarea" }) as HTMLTextAreaElement;
    fireEvent.change(params, { target: { value: "0001" } });
    fireEvent.click(screen.getByRole("tab", { name: /^请求体/ }));
    fireEvent.change(body, { target: { value: "9007199254740993\\path" } });
    fireEvent.click(screen.getByRole("tab", { name: /^参数/ }));

    expect(screen.getByLabelText("参数草稿")).toBe(params);
    expect(screen.getByLabelText("正文草稿", { selector: "textarea" })).toBe(body);
    expect(params.value).toBe("0001");
    expect(body.value).toBe("9007199254740993\\path");
  });
});
