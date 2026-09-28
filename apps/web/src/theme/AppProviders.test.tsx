import { StrictMode, useState } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { Button, Form, Input, Modal, Select, Splitter, Table, Tabs, Tree } from "antd";
import { describe, expect, it } from "vitest";

import { selectAntOption } from "../test/antd";
import { AppProviders } from "./AppProviders";

function CompatibilitySurface() {
  const [environment, setEnvironment] = useState("test");
  const [fallbackEnvironment, setFallbackEnvironment] = useState("fallback");
  const [tab, setTab] = useState("request");
  const [dialogOpen, setDialogOpen] = useState(false);
  const [orientation, setOrientation] = useState<"horizontal" | "vertical">("horizontal");

  return (
    <AppProviders>
      <Form layout="vertical" aria-label="组件兼容表单">
        <Form.Item label="执行环境" htmlFor="compat-environment">
          <Select
            id="compat-environment"
            aria-label="执行环境"
            value={environment}
            onChange={setEnvironment}
            options={[{ value: "test", label: "测试环境" }, { value: "stage", label: "预发布环境" }, { value: "literal", label: "环境 A (test)" }]}
          />
        </Form.Item>
        <output data-testid="environment-value">{environment}</output>
        <Form.Item label="备用环境" htmlFor="compat-fallback-environment">
          <Select
            id="compat-fallback-environment"
            aria-label="备用环境"
            value={fallbackEnvironment}
            onChange={setFallbackEnvironment}
            options={[{ value: "fallback", label: "备用" }, { value: "literal-fallback", label: "环境 A (test)" }]}
          />
        </Form.Item>
        <output data-testid="fallback-environment-value">{fallbackEnvironment}</output>
        <Tabs
          activeKey={tab}
          destroyOnHidden={false}
          onChange={setTab}
          items={[
            { key: "request", label: "请求", children: <Input aria-label="无损输入" defaultValue="001-9007199254740993" /> },
            { key: "fields", label: "字段", children: <Tree aria-label="响应字段" defaultExpandAll treeData={[{ key: "body", title: "body", children: [{ key: "body.id", title: "id" }] }]} /> },
          ]}
        />
        <Table
          size="small"
          rowKey="id"
          pagination={false}
          columns={[{ key: "name", title: "名称", dataIndex: "name" }, { key: "value", title: "值", dataIndex: "value" }]}
          dataSource={[{ id: "row-a", name: "重复键", value: "001" }, { id: "row-b", name: "重复键", value: "9007199254740993" }]}
        />
        <Button htmlType="button" onClick={() => setOrientation((value) => value === "horizontal" ? "vertical" : "horizontal")}>切换分栏</Button>
        <Splitter orientation={orientation} style={{ width: 600, height: 220 }}>
          <Splitter.Panel defaultSize="50%" min="25%" max="75%"><Input aria-label="分栏草稿" defaultValue="draft" /></Splitter.Panel>
          <Splitter.Panel>响应内容</Splitter.Panel>
        </Splitter>
        <Button htmlType="button" onClick={() => setDialogOpen(true)}>打开确认</Button>
        <Modal
          open={dialogOpen}
          title="关闭请求确认"
          destroyOnHidden={false}
          mask={{ closable: false }}
          onCancel={() => setDialogOpen(false)}
          footer={<Button htmlType="button" onClick={() => setDialogOpen(false)}>取消</Button>}
        >
          <Input aria-label="确认备注" defaultValue="保留" />
        </Modal>
      </Form>
    </AppProviders>
  );
}

describe("Ant Design 根上下文", () => {
  it("真实 Form、Select、Tabs、Tree、Table、Splitter 与 Modal 可组合且保持受控状态", async () => {
    render(<StrictMode><CompatibilitySurface /></StrictMode>);

    await selectAntOption("执行环境", "预发布环境");
    expect(screen.getByTestId("environment-value").textContent).toBe("stage");
    await selectAntOption("执行环境", "环境 A (test)");
    expect(screen.getByTestId("environment-value").textContent).toBe("literal");
    expect(screen.getByTestId("fallback-environment-value").textContent).toBe("fallback");
    await selectAntOption("备用环境", "环境 A (test)");
    expect(screen.getByTestId("environment-value").textContent).toBe("literal");
    expect(screen.getByTestId("fallback-environment-value").textContent).toBe("literal-fallback");

    const lossless = screen.getByLabelText("无损输入") as HTMLInputElement;
    fireEvent.change(lossless, { target: { value: "0001\\path-9007199254740993" } });
    fireEvent.click(screen.getByRole("tab", { name: "字段" }));
    expect(await screen.findByText("id")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "请求" }));
    expect(screen.getByLabelText("无损输入")).toBe(lossless);
    expect(lossless.value).toBe("0001\\path-9007199254740993");
    expect(screen.getByText("9007199254740993")).toBeTruthy();

    const splitterDraft = screen.getByLabelText("分栏草稿");
    fireEvent.click(screen.getByRole("button", { name: "切换分栏" }));
    expect(screen.getByLabelText("分栏草稿")).toBe(splitterDraft);

    fireEvent.click(screen.getByRole("button", { name: "打开确认" }));
    const dialog = await screen.findByRole("dialog", { name: "关闭请求确认" });
    fireEvent.change(within(dialog).getByLabelText("确认备注"), { target: { value: "仍然保留" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    fireEvent.click(screen.getByRole("button", { name: "打开确认" }));
    expect((within(await screen.findByRole("dialog", { name: "关闭请求确认" })).getByLabelText("确认备注") as HTMLInputElement).value).toBe("仍然保留");
  });
});
