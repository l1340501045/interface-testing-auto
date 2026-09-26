import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { KeyValueRows } from "./RequestParts";
import { LeaveGuardProvider, useLeaveAggregate } from "../hooks/leaveGuard";

function DirtyProbe() {
  const state = useLeaveAggregate();
  return <output aria-label="离开状态">{state.dirty ? "dirty" : "clean"}</output>;
}

describe("参数表格浏览器输入边界", () => {
  it("直接从 paste 事件捕获 CRLF/Tab，预览应用后逐字符保持", () => {
    const onChange = vi.fn();
    render(<KeyValueRows rows={[]} label="查询参数" addLabel="添加" readOnly={false} kind="query" version={2} onChange={onChange} />);
    fireEvent.click(screen.getByText("批量录入"));
    fireEvent.change(screen.getByLabelText("格式"), { target: { value: "tsv" } });
    const source = 'name\t"A\r\nB\tC"\t"说明\r\n二"';
    fireEvent.paste(screen.getByLabelText("Query 批量原文转义文本"), {
      clipboardData: { getData: (type: string) => type === "text/plain" ? source : "" },
    });
    fireEvent.click(screen.getByText("生成预览"));
    fireEvent.click(screen.getByText("应用追加"));
    const row = onChange.mock.calls[0][0][0];
    expect(row.name).toBe("name");
    expect(row.value).toBe("A\r\nB\tC");
    expect(row.description).toBe("说明\r\n二");
  });

  it("编辑相邻字段不会回写含特殊字符的值", () => {
    const row = { row_id: "00000000-0000-4000-8000-000000000001", name: "n", value: "A\r\nB\tC", enabled: true, description: "d" };
    const onChange = vi.fn();
    render(<KeyValueRows rows={[row]} label="查询参数" addLabel="添加" readOnly={false} kind="query" version={2} onChange={onChange} />);
    fireEvent.change(screen.getByLabelText("查询参数名称 1"), { target: { value: "renamed" } });
    expect(onChange.mock.calls[0][0][0]).toEqual({ ...row, name: "renamed" });
  });

  it("未应用的原始文本草稿登记到离开保护", () => {
    const row = { row_id: "00000000-0000-4000-8000-000000000001", name: "n", value: "A\r\nB", enabled: true, description: "" };
    render(
      <LeaveGuardProvider>
        <KeyValueRows rows={[row]} label="查询参数" addLabel="添加" readOnly={false} kind="query" version={2} pendingPrefix="case-tab:t1" onChange={vi.fn()} />
        <DirtyProbe />
      </LeaveGuardProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "编辑原始文本" }));
    fireEvent.change(screen.getByLabelText("查询参数值 1转义文本"), { target: { value: "A\\r\\nB-edited" } });
    expect(screen.getByLabelText("离开状态").textContent).toBe("dirty");
  });

  it("两表合计 500 行时追加在应用前拒绝，原表零修改", () => {
    const onChange = vi.fn();
    const row = { row_id: "00000000-0000-4000-8000-000000000001", name: "q", value: "1", enabled: true, description: "" };
    const otherRows = Array.from({ length: 499 }, (_, index) => ({ row_id: `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`, name: `h${index}`, value: "", enabled: true, description: "" }));
    render(<KeyValueRows rows={[row]} otherRows={otherRows} ownerRevision="r1" label="查询参数" addLabel="添加" readOnly={false} kind="query" version={2} onChange={onChange} />);
    fireEvent.click(screen.getByText("批量录入"));
    fireEvent.change(screen.getByLabelText("格式"), { target: { value: "equals" } });
    fireEvent.change(screen.getByLabelText("Query 批量原文转义文本"), { target: { value: "extra=2" } });
    fireEvent.click(screen.getByText("生成预览"));
    fireEvent.click(screen.getByText("应用追加"));
    expect(screen.getByRole("alert").textContent).toContain("合计不能超过 500 行");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("替换预览列出原行与关联条件影响", () => {
    render(<KeyValueRows rows={[{ row_id: "00000000-0000-4000-8000-000000000001", name: "q", value: "1", enabled: true, description: "" }]} relatedAssertionCount={2} label="查询参数" addLabel="添加" readOnly={false} kind="query" version={2} onChange={vi.fn()} />);
    fireEvent.click(screen.getByText("批量录入"));
    fireEvent.click(screen.getByLabelText("替换"));
    fireEvent.change(screen.getByLabelText("Query 批量原文转义文本"), { target: { value: "next=2" } });
    fireEvent.click(screen.getByText("生成预览"));
    expect(screen.getByText(/替换将移除当前 1 行，并使 2 条行条件/)).toBeTruthy();
  });
});
