import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ResponsePanel } from "./ResponsePanel";

describe("响应标签生命周期", () => {
  it("字段表单往返正文和响应头后保持同一实例、未应用输入与活动信号", () => {
    render(
      <ResponsePanel
        report={null}
        reportError={null}
        loading={false}
        matchesCurrent={false}
        selectedRunId={null}
        onCancel={() => undefined}
        canCancel={false}
        fieldsTab={(active) => <input aria-label="未应用字段条件" data-active={String(active)} defaultValue="001" />}
      />,
    );

    fireEvent.click(screen.getByRole("tab", { name: "字段与断言" }));
    const draft = screen.getByLabelText("未应用字段条件") as HTMLInputElement;
    expect(draft.dataset.active).toBe("true");
    fireEvent.change(draft, { target: { value: "9007199254740993" } });

    fireEvent.click(screen.getByRole("tab", { name: "正文" }));
    expect(draft.dataset.active).toBe("false");
    fireEvent.click(screen.getByRole("tab", { name: "响应头" }));
    fireEvent.click(screen.getByRole("tab", { name: "字段与断言" }));

    expect(screen.getByLabelText("未应用字段条件")).toBe(draft);
    expect(draft.value).toBe("9007199254740993");
    expect(draft.dataset.active).toBe("true");
  });
});
