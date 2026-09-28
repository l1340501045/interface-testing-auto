import { useState } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AppProviders } from "../theme/AppProviders";
import { VariableRowsEditor } from "./VariableRowsEditor";
import { toPayload, type VariableRow } from "./variableRows";

function row(name: string, text: string): VariableRow {
  return { name, kind: "string", text, original: text, changed: false };
}

function DualEditor({ onSubmit }: { onSubmit: (owner: string, payload: unknown) => void }) {
  const [projectRows, setProjectRows] = useState([
    row("project-a", "A"),
    row("project-b", "B"),
    row("project-c", "C"),
  ]);
  const [environmentRows, setEnvironmentRows] = useState([row("environment-a", "E")]);

  return (
    <>
      <section aria-label="项目变量表单">
        <VariableRowsEditor
          rows={projectRows}
          disabled={false}
          onChange={setProjectRows}
          emptyHint="项目变量为空"
        />
        <button type="button" onClick={() => onSubmit("project", toPayload(projectRows))}>
          提交项目变量
        </button>
      </section>
      <section aria-label="环境变量表单">
        <VariableRowsEditor
          rows={environmentRows}
          disabled={false}
          onChange={setEnvironmentRows}
          emptyHint="环境变量为空"
        />
        <button type="button" onClick={() => onSubmit("environment", toPayload(environmentRows))}>
          提交环境变量
        </button>
      </section>
    </>
  );
}

describe("VariableRowsEditor 的实例与行身份", () => {
  it("两个表单没有重复 ID，标签关联各自控件，删除中间行不重挂后续输入", () => {
    render(<DualEditor onSubmit={vi.fn()} />, { wrapper: AppProviders });

    const ids = [...document.querySelectorAll<HTMLElement>("[id]")].map((element) => element.id);
    expect(new Set(ids).size).toBe(ids.length);

    const project = screen.getByRole("region", { name: "项目变量表单" });
    const environment = screen.getByRole("region", { name: "环境变量表单" });
    const projectNames = within(project).getAllByLabelText("名称") as HTMLInputElement[];
    const environmentName = within(environment).getByLabelText("名称") as HTMLInputElement;
    expect(projectNames[0].id).not.toBe(environmentName.id);

    const thirdValue = within(project).getAllByLabelText("值")[2] as HTMLInputElement;
    fireEvent.change(thirdValue, { target: { value: "C-已编辑" } });
    expect(thirdValue.value).toBe("C-已编辑");

    fireEvent.click(within(project).getAllByRole("button", { name: "删除" })[1]);

    const remainingValues = within(project).getAllByLabelText("值") as HTMLInputElement[];
    expect(remainingValues).toHaveLength(2);
    expect(remainingValues[1]).toBe(thirdValue);
    expect(remainingValues[1].value).toBe("C-已编辑");
    expect((within(environment).getByLabelText("值") as HTMLInputElement).value).toBe("E");
  });

  it("两个表单分别提交自己的业务变量，界面行 ID 不进入配置 payload", () => {
    const onSubmit = vi.fn();
    render(<DualEditor onSubmit={onSubmit} />, { wrapper: AppProviders });

    const project = screen.getByRole("region", { name: "项目变量表单" });
    const environment = screen.getByRole("region", { name: "环境变量表单" });
    fireEvent.change(within(project).getAllByLabelText("值")[0], { target: { value: "P-新值" } });
    fireEvent.change(within(environment).getByLabelText("值"), { target: { value: "E-新值" } });

    fireEvent.click(within(project).getByRole("button", { name: "提交项目变量" }));
    fireEvent.click(within(environment).getByRole("button", { name: "提交环境变量" }));

    expect(onSubmit).toHaveBeenNthCalledWith(1, "project", [
      { name: "project-a", value: { type: "string", text: "P-新值" } },
      { name: "project-b", value: "B" },
      { name: "project-c", value: "C" },
    ]);
    expect(onSubmit).toHaveBeenNthCalledWith(2, "environment", [
      { name: "environment-a", value: { type: "string", text: "E-新值" } },
    ]);
  });
});
