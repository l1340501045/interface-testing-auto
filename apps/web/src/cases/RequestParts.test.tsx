import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { KeyValueRows } from "./RequestParts";
import type { RawKeyValue } from "./requestDraft";
import { LeaveGuardProvider, useLeaveAggregate } from "../hooks/leaveGuard";
import { AppProviders } from "../theme/AppProviders";
import { selectAntOption } from "../test/antd";

function DirtyProbe() {
  const state = useLeaveAggregate();
  return <output aria-label="离开状态">{state.dirty ? "dirty" : "clean"}</output>;
}

function ControlledV1Rows({ onRows }: { onRows: (rows: { name: string; value: string }[]) => void }) {
  const [rows, setRows] = useState([{ name: "legacy", value: "A\r\nB" }]);
  return (
    <KeyValueRows
      rows={rows}
      label="查询参数"
      addLabel="添加"
      readOnly={false}
      kind="query"
      version={1}
      onChange={(next) => {
        setRows(next);
        onRows(next);
        return true;
      }}
    />
  );
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function ControlledBatchRows({ decide }: { decide: (rows: RawKeyValue[]) => boolean | Promise<boolean> }) {
  const [rows, setRows] = useState<RawKeyValue[]>([]);
  return (
    <>
      <KeyValueRows
        rows={rows}
        label="查询参数"
        addLabel="添加"
        readOnly={false}
        kind="query"
        version={2}
        onChange={async (next) => {
          const accepted = await decide(next);
          if (accepted === true) setRows(next);
          return accepted;
        }}
      />
      <output aria-label="已接纳请求行">{JSON.stringify(rows)}</output>
    </>
  );
}

function openBatch(raw: string) {
  fireEvent.click(screen.getByText("批量录入"));
  fireEvent.change(screen.getByLabelText("Query 批量原文转义文本"), { target: { value: raw } });
  fireEvent.click(screen.getByText("生成预览"));
}

describe("参数表格浏览器输入边界", () => {
  it("窄分栏为编辑列保留最小宽度并由框架表格提供局部横向滚动", () => {
    render(
      <KeyValueRows
        rows={[{ row_id: "00000000-0000-4000-8000-000000000001", name: "name", value: "value", enabled: true, description: "description" }]}
        label="查询参数"
        addLabel="添加"
        readOnly={false}
        kind="query"
        version={2}
        assertionSlot={() => <span>条件</span>}
        onChange={vi.fn(() => false)}
      />,
      { wrapper: AppProviders },
    );

    const columnWidths = Array.from(document.querySelectorAll<HTMLTableColElement>("colgroup col"), (column) => column.style.width);
    expect(columnWidths).toEqual(["72px", "200px", "240px", "200px", "260px", "190px"]);
    const table = screen.getByRole("table");
    expect(table.style.width).toBe("1162px");
  });

  it("v1 无 row_id 的受控更新保持输入实例与未应用原文，UI 身份不进入 payload", async () => {
    let latest: { name: string; value: string }[] = [];
    render(
      <LeaveGuardProvider>
        <ControlledV1Rows onRows={(rows) => { latest = rows; }} />
        <DirtyProbe />
      </LeaveGuardProvider>,
      { wrapper: AppProviders },
    );

    fireEvent.click(screen.getByRole("button", { name: "编辑原始文本" }));
    const rawInput = screen.getByLabelText("查询参数值 1转义文本") as HTMLInputElement;
    fireEvent.change(rawInput, { target: { value: "A\\r\\nB\\t未应用" } });
    expect(screen.getByLabelText("离开状态").textContent).toBe("dirty");

    const nameInput = screen.getByLabelText("查询参数名称 1") as HTMLInputElement;
    fireEvent.change(nameInput, { target: { value: "renamed" } });
    expect(screen.getByLabelText("查询参数名称 1")).toBe(nameInput);
    expect(screen.getByLabelText("查询参数值 1转义文本")).toBe(rawInput);
    expect(rawInput.value).toBe("A\\r\\nB\\t未应用");
    fireEvent.change(nameInput, { target: { value: "renamed-again" } });
    expect(screen.getByLabelText("查询参数名称 1")).toBe(nameInput);

    fireEvent.click(screen.getByRole("button", { name: "应用" }));
    await waitFor(() => expect(screen.queryByLabelText("查询参数值 1转义文本")).toBeNull());
    expect(screen.getByLabelText("离开状态").textContent).toBe("clean");
    expect(latest).toEqual([{ name: "renamed-again", value: "A\r\nB\t未应用" }]);
    expect("row_id" in latest[0]!).toBe(false);
  });

  it("原始文本应用被父层取消时保留输入、原请求与 dirty", async () => {
    const row = { name: "legacy", value: "A\r\nB" };
    const onChange = vi.fn<(rows: RawKeyValue[]) => Promise<boolean>>(async (_rows) => false);
    render(
      <LeaveGuardProvider>
        <KeyValueRows rows={[row]} label="查询参数" addLabel="添加" readOnly={false} kind="query" version={1} onChange={onChange} />
        <DirtyProbe />
      </LeaveGuardProvider>,
      { wrapper: AppProviders },
    );
    fireEvent.click(screen.getByRole("button", { name: "编辑原始文本" }));
    const rawInput = screen.getByLabelText("查询参数值 1转义文本") as HTMLInputElement;
    fireEvent.change(rawInput, { target: { value: "取消后保留\\r\\n原文" } });
    fireEvent.click(screen.getByRole("button", { name: "应用" }));

    await waitFor(() => expect(onChange).toHaveBeenCalledTimes(1));
    expect(onChange).toHaveBeenCalledWith([{ name: "legacy", value: "取消后保留\r\n原文" }]);
    expect(screen.getByLabelText("查询参数值 1转义文本")).toBe(rawInput);
    expect(rawInput.value).toBe("取消后保留\\r\\n原文");
    expect(screen.getByLabelText("离开状态").textContent).toBe("dirty");
    expect(row).toEqual({ name: "legacy", value: "A\r\nB" });
  });

  it("原始文本等待接纳时继续修改，旧成功不关闭新修订", async () => {
    const acceptance = deferred<boolean>();
    const onChange = vi.fn<(rows: RawKeyValue[]) => Promise<boolean>>((_rows) => acceptance.promise);
    render(
      <LeaveGuardProvider>
        <KeyValueRows rows={[{ name: "legacy", value: "A\r\nB" }]} label="查询参数" addLabel="添加" readOnly={false} kind="query" version={1} onChange={onChange} />
        <DirtyProbe />
      </LeaveGuardProvider>,
      { wrapper: AppProviders },
    );
    fireEvent.click(screen.getByRole("button", { name: "编辑原始文本" }));
    const rawInput = screen.getByLabelText("查询参数值 1转义文本") as HTMLInputElement;
    fireEvent.change(rawInput, { target: { value: "提交版本\\r\\n一" } });
    fireEvent.click(screen.getByRole("button", { name: "应用" }));
    fireEvent.change(rawInput, { target: { value: "等待期间的新版本\\t二" } });
    acceptance.resolve(true);

    await waitFor(() => expect((screen.getByRole("button", { name: "应用" }) as HTMLButtonElement).disabled).toBe(false));
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith([{ name: "legacy", value: "提交版本\r\n一" }]);
    expect(screen.getByLabelText("查询参数值 1转义文本")).toBe(rawInput);
    expect(rawInput.value).toBe("等待期间的新版本\\t二");
    expect(screen.getByLabelText("离开状态").textContent).toBe("dirty");
  });

  it("批量应用被父层取消时保留预览、原文、空请求与 dirty", async () => {
    const decide = vi.fn<(rows: RawKeyValue[]) => Promise<boolean>>(async (_rows) => false);
    render(
      <LeaveGuardProvider>
        <ControlledBatchRows decide={decide} />
        <DirtyProbe />
      </LeaveGuardProvider>,
      { wrapper: AppProviders },
    );
    openBatch("cancelled=1");
    fireEvent.click(screen.getByText("应用追加"));

    await waitFor(() => expect(decide).toHaveBeenCalledTimes(1));
    expect(screen.getByText("应用追加")).toBeTruthy();
    expect((screen.getByLabelText("Query 批量原文转义文本") as HTMLTextAreaElement).value).toBe("cancelled=1");
    expect(screen.getByLabelText("已接纳请求行").textContent).toBe("[]");
    expect(screen.getByLabelText("离开状态").textContent).toBe("dirty");
  });

  it("批量应用等待时原文再变，旧成功只接纳旧 payload 且新修订保持 dirty", async () => {
    const acceptance = deferred<boolean>();
    const decide = vi.fn<(rows: RawKeyValue[]) => Promise<boolean>>((_rows) => acceptance.promise);
    render(
      <LeaveGuardProvider>
        <ControlledBatchRows decide={decide} />
        <DirtyProbe />
      </LeaveGuardProvider>,
      { wrapper: AppProviders },
    );
    openBatch("submitted=1");
    fireEvent.click(screen.getByText("应用追加"));
    const source = screen.getByLabelText("Query 批量原文转义文本") as HTMLTextAreaElement;
    fireEvent.change(source, { target: { value: "newer=2" } });
    acceptance.resolve(true);

    await waitFor(() => expect(screen.getByLabelText("已接纳请求行").textContent).toContain("submitted"));
    expect(decide).toHaveBeenCalledTimes(1);
    expect(source.value).toBe("newer=2");
    expect(screen.getByLabelText("离开状态").textContent).toBe("dirty");
    expect(screen.queryByText("应用追加")).toBeNull();
  });

  it("批量应用成功后仅提交一次、更新请求并清除子表单 dirty", async () => {
    const decide = vi.fn<(rows: RawKeyValue[]) => Promise<boolean>>(async (_rows) => true);
    render(
      <LeaveGuardProvider>
        <ControlledBatchRows decide={decide} />
        <DirtyProbe />
      </LeaveGuardProvider>,
      { wrapper: AppProviders },
    );
    openBatch("accepted=001");
    fireEvent.click(screen.getByText("应用追加"));

    await waitFor(() => expect(screen.queryByText("应用追加")).toBeNull());
    expect(decide).toHaveBeenCalledTimes(1);
    const submitted = decide.mock.calls[0]![0];
    expect(submitted).toHaveLength(1);
    expect(submitted[0]).toMatchObject({ name: "accepted", value: "001", enabled: true, description: "" });
    expect(screen.getByLabelText("已接纳请求行").textContent).toContain('"name":"accepted"');
    expect(screen.getByLabelText("离开状态").textContent).toBe("clean");
  });

  it("v1 历史 501 行完整可读且不触发升级写入", () => {
    const rows = Array.from({ length: 501 }, (_, index) => ({ name: `legacy-${index + 1}`, value: String(index + 1) }));
    const onChange = vi.fn(() => false);
    const startedAt = performance.now();
    render(<KeyValueRows rows={rows} label="查询参数" addLabel="添加" readOnly kind="query" version={1} onChange={onChange} />, { wrapper: AppProviders });
    expect((screen.getByLabelText("查询参数名称 1") as HTMLInputElement).value).toBe("legacy-1");
    expect((screen.getByLabelText("查询参数名称 501") as HTMLInputElement).value).toBe("legacy-501");
    expect(screen.queryByText("下一页")).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
    expect(performance.now() - startedAt).toBeLessThan(15_000);
  });

  it("v2 合法上限 500 行完整挂载并保留末行文本身份", () => {
    const rows = Array.from({ length: 500 }, (_, index) => ({
      row_id: `00000000-0000-7000-8000-${String(index + 1).padStart(12, "0")}`,
      name: `row-${index + 1}`,
      value: String(index + 1),
      enabled: true,
      description: `说明-${index + 1}`,
    }));
    const startedAt = performance.now();
    render(<KeyValueRows rows={rows} label="查询参数" addLabel="添加" readOnly kind="query" version={2} onChange={vi.fn(() => false)} />, { wrapper: AppProviders });
    const lastName = screen.getByLabelText("查询参数名称 500") as HTMLInputElement;
    expect(lastName.value).toBe("row-500");
    expect((screen.getByLabelText("查询参数说明 500") as HTMLInputElement).value).toBe("说明-500");
    const dataRows = document.querySelectorAll("tbody tr.ant-table-row");
    expect(dataRows).toHaveLength(500);
    expect(dataRows[499]?.contains(lastName)).toBe(true);
    expect(performance.now() - startedAt).toBeLessThan(15_000);
  });

  it("直接从 paste 事件捕获 CRLF/Tab，预览应用后逐字符保持", async () => {
    const decide = vi.fn<(rows: RawKeyValue[]) => Promise<boolean>>(async (_rows) => true);
    render(<ControlledBatchRows decide={decide} />, { wrapper: AppProviders });
    fireEvent.click(screen.getByText("批量录入"));
    await selectAntOption("Query 批量格式", "表格 TSV（2～3 列）");
    const source = 'name\t"A\r\nB\tC"\t"说明\r\n二"';
    fireEvent.paste(screen.getByLabelText("Query 批量原文转义文本"), {
      clipboardData: { getData: (type: string) => type === "text/plain" ? source : "" },
    });
    fireEvent.click(screen.getByText("生成预览"));
    fireEvent.click(screen.getByText("应用追加"));
    await waitFor(() => expect(screen.getByLabelText("已接纳请求行").textContent).toContain("name"));
    const row = decide.mock.calls[0]![0][0];
    expect(row.name).toBe("name");
    expect(row.value).toBe("A\r\nB\tC");
    expect(row.description).toBe("说明\r\n二");
  });

  it("编辑相邻字段不会回写含特殊字符的值", () => {
    const row = { row_id: "00000000-0000-4000-8000-000000000001", name: "n", value: "A\r\nB\tC", enabled: true, description: "d" };
    const onChange = vi.fn<(rows: RawKeyValue[]) => boolean>((_rows) => false);
    render(<KeyValueRows rows={[row]} label="查询参数" addLabel="添加" readOnly={false} kind="query" version={2} onChange={onChange} />, { wrapper: AppProviders });
    fireEvent.change(screen.getByLabelText("查询参数名称 1"), { target: { value: "renamed" } });
    expect(onChange.mock.calls[0][0][0]).toEqual({ ...row, name: "renamed" });
  });

  it("未应用的原始文本草稿登记到离开保护", () => {
    const row = { row_id: "00000000-0000-4000-8000-000000000001", name: "n", value: "A\r\nB", enabled: true, description: "" };
    render(
      <LeaveGuardProvider>
        <KeyValueRows rows={[row]} label="查询参数" addLabel="添加" readOnly={false} kind="query" version={2} pendingPrefix="case-tab:t1" onChange={vi.fn(() => false)} />
        <DirtyProbe />
      </LeaveGuardProvider>,
      { wrapper: AppProviders },
    );
    fireEvent.click(screen.getByRole("button", { name: "编辑原始文本" }));
    fireEvent.change(screen.getByLabelText("查询参数值 1转义文本"), { target: { value: "A\\r\\nB-edited" } });
    expect(screen.getByLabelText("离开状态").textContent).toBe("dirty");
  });

  it("两表合计 500 行时追加在应用前拒绝，原表零修改", () => {
    const onChange = vi.fn(() => false);
    const row = { row_id: "00000000-0000-4000-8000-000000000001", name: "q", value: "1", enabled: true, description: "" };
    const otherRows = Array.from({ length: 499 }, (_, index) => ({ row_id: `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`, name: `h${index}`, value: "", enabled: true, description: "" }));
    render(<KeyValueRows rows={[row]} otherRows={otherRows} ownerRevision="r1" label="查询参数" addLabel="添加" readOnly={false} kind="query" version={2} onChange={onChange} />, { wrapper: AppProviders });
    fireEvent.click(screen.getByText("批量录入"));
    fireEvent.change(screen.getByLabelText("Query 批量原文转义文本"), { target: { value: "extra=2" } });
    fireEvent.click(screen.getByText("生成预览"));
    fireEvent.click(screen.getByText("应用追加"));
    expect(screen.getByRole("alert").textContent).toContain("合计不能超过 500 行");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("替换预览列出原行与关联条件影响", () => {
    render(<KeyValueRows rows={[{ row_id: "00000000-0000-4000-8000-000000000001", name: "q", value: "1", enabled: true, description: "" }]} relatedAssertionCount={2} label="查询参数" addLabel="添加" readOnly={false} kind="query" version={2} onChange={vi.fn(() => false)} />, { wrapper: AppProviders });
    fireEvent.click(screen.getByText("批量录入"));
    fireEvent.click(screen.getByLabelText("替换"));
    fireEvent.change(screen.getByLabelText("Query 批量原文转义文本"), { target: { value: "next=2" } });
    fireEvent.click(screen.getByText("生成预览"));
    expect(screen.getByText(/替换将移除当前 1 行，并使 2 条行条件/)).toBeTruthy();
  });
});
