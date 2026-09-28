/**
 * 新来源加载期间不开放旧字段树（R5 §F7）。
 *
 * 复现的缺陷：`useFieldTree` 在换输入时保留上一棵 `tree` 并置 `loading`，而字段面板只在
 * “树为空”时才显示加载态。于是切到 r2 之后、r2 的树还没回来之前，用户仍能点到 **r1** 的
 * 节点，并按 r1 的类型与值去配置条件或试算——那份数据与当前响应毫无关系。
 *
 * 这里用一个真实 Harness：真实 `useFieldTree` + 真实字段面板，字段树响应由用例控制何时
 * 放行。断言的是“能不能点到”，不是内部状态。
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, apiGet: vi.fn(), apiSend: vi.fn(), apiSendWithMeta: vi.fn(), apiDelete: vi.fn() };
});

import { apiSend, projectPath } from "../api/client";
import { selectAntOption } from "../test/antd";
import { FieldTreePanel } from "./FieldTreePanel";
import { useFieldTree } from "./useFieldTree";

const apiSendMock = vi.mocked(apiSend);

const DATA_ID = [
  { kind: "key" as const, key: "data" },
  { kind: "key" as const, key: "id" },
];

/** 一棵只有 data.id 的树；类型与文本由调用方给定。 */
function treeFor(type: string, text: string) {
  const leaf = {
    label: "id",
    type,
    text,
    selector: DATA_ID,
    truncated: null,
    children: [],
  };
  return {
    root: {
      label: "$",
      type: "object",
      text: "",
      selector: [],
      truncated: null,
      children: [{ label: "data", type: "object", text: "", selector: [{ kind: "key", key: "data" }], truncated: null, children: [leaf] }],
    },
    node_count: 3,
  };
}

/** 挂起某段正文的字段树响应。 */
const gates = new Map<string, () => void>();
function hold(text: string): void {
  gates.set(text, () => undefined);
}
function releaseFor(text: string, tree: unknown): void {
  const pending = gates.get(text);
  gates.delete(text);
  void tree;
  pending?.();
}

const bodies: Record<string, unknown> = {};

function route(path: string, method: string, body: unknown): unknown {
  if (method === "POST" && path === projectPath("ws", "p", "/field-tree")) {
    const text = (body as { text: string }).text;
    const tree = bodies[text] ?? treeFor("string", "unknown");
    if (gates.has(text)) {
      return new Promise((resolve) => {
        gates.set(text, () => resolve(tree));
      });
    }
    return tree;
  }
  throw new Error(`测试未覆盖的请求：${method} ${path}`);
}

const onChange = vi.fn();
const TYPES = [{
  id: "equals",
  label: "等于",
  group: "通用",
  applies_to: ["string", "integer", "number"],
  params_schema: { expected: { control: "value" as const, type: "string", label: "期望值" } },
  summary: "等于期望值",
  operator_version: 1,
}];

/** 真实字段树 Hook + 真实字段面板。 */
function Harness({ text, sourceKey, enabled = true }: { text: string; sourceKey: string; enabled?: boolean }) {
  // `bodySourceKey` 与正文一起构成来源身份，与 CaseEditor / ResponseFieldPanel 的用法一致。
  const tree = useFieldTree("ws", "p", text, sourceKey, enabled);
  return (
    <FieldTreePanel
      title="响应正文字段"
      tree={tree}
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

/**
 * 等字段树就绪并展开 `data`。
 *
 * 根节点默认展开，`data` 这一层默认收起，因此 `id` 要先展开才可见——等待就绪必须等
 * `data` 出现，直接等 `id` 只会超时。
 */
async function expandToId(): Promise<void> {
  const data = await findTreeItem("data");
  if (data.getAttribute("aria-expanded") !== "true") {
    const switcher = data.querySelector(".ant-tree-switcher");
    if (!(switcher instanceof HTMLElement)) throw new Error("data 节点展开入口未挂载");
    fireEvent.click(switcher);
    await waitFor(() => expect(data.getAttribute("aria-expanded")).toBe("true"));
  }
  await findTreeItem("id");
}

/** 选中 data.id 那一行。 */
function selectDataId(): void {
  const item = queryTreeItem("id");
  const title = item?.querySelector(".ant-tree-title");
  if (!(title instanceof HTMLElement)) throw new Error("id 节点标题未挂载");
  fireEvent.click(title);
}

function treeRoot(): HTMLElement | null {
  return document.querySelector('[role="tree"][aria-label="响应正文字段"]');
}

function queryTreeItem(label: string): HTMLElement | null {
  const tree = treeRoot();
  if (tree === null) return null;
  return Array.from(tree.querySelectorAll<HTMLElement>('[role="treeitem"]'))
    .find((item) => item.querySelector(".field-name")?.textContent?.trim() === label) ?? null;
}

async function findTreeItem(label: string): Promise<HTMLElement> {
  await waitFor(() => expect(queryTreeItem(label) instanceof HTMLElement).toBe(true));
  const item = queryTreeItem(label);
  if (item === null) throw new Error(`${label} 树节点未挂载`);
  return item;
}

async function waitForFieldTreeRequest(text: string): Promise<void> {
  await waitFor(() => expect(apiSendMock.mock.calls.some(([, method, body]) =>
    method === "POST" && (body as { text?: string } | undefined)?.text === text
  )).toBe(true));
}

function fieldTreeRequestCount(text: string): number {
  return apiSendMock.mock.calls.filter(([, method, body]) =>
    method === "POST" && (body as { text?: string } | undefined)?.text === text
  ).length;
}

function detailText(): string {
  return document.querySelector(".field-detail")?.textContent ?? "";
}

/** `id` 那一行的整行文本：包含类型与**样例值**（试算用的就是它）。 */
function idRowText(): string {
  const row = queryTreeItem("id")?.querySelector(".field-row");
  return row?.textContent ?? "";
}

beforeEach(() => {
  gates.clear();
  onChange.mockReset();
  for (const key of Object.keys(bodies)) delete bodies[key];
  apiSendMock.mockReset();
  apiSendMock.mockImplementation((async (
    path: string,
    method: string,
    body: unknown,
    parse: (raw: unknown) => unknown,
  ) => parse(await route(path, method, body))) as never);
});

describe("新来源加载期间不开放旧字段树", () => {
  it("切到 r2 且其树未返回时：旧节点不可选、不可试算", async () => {
    const r1 = '{"data":{"id":1}}';
    const r2 = '{"data":{"id":"新值"}}';
    bodies[r2] = treeFor("string", "新值");

    const { rerender } = render(<Harness text={r1} sourceKey="run-1" />);
    // r1 的树：integer = 1。
    bodies[r1] = treeFor("integer", "1");
    await expandToId();
    selectDataId();
    expect(detailText()).toContain("integer");
    expect(idRowText()).toContain("1");

    // 切到 r2，并挂起它的字段树响应。
    hold(r2);
    rerender(<Harness text={r2} sourceKey="run-2" />);
    await waitForFieldTreeRequest(r2);

    // 关键：r1 的节点此刻一个都不该在页面上——它们属于另一份响应。
    expect(queryTreeItem("id")).toBeNull();
    expect(queryTreeItem("data")).toBeNull();
    expect(document.querySelector(".field-row-active")).toBeNull();
    expect(detailText()).toBe("");
    // 草稿与已配置断言不受影响：这里没有任何 onChange。
    expect(onChange).not.toHaveBeenCalled();

    // r2 返回后：同一条路径，但类型与值都是 r2 的。
    await act(async () => {
      releaseFor(r2, bodies[r2]);
    });
    await expandToId();
    selectDataId();
    expect(detailText()).toContain("string");
    expect(idRowText()).toContain("新值");
    expect(idRowText()).not.toContain("integer");
    expect(document.body.textContent).not.toContain("新值旧");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("换正文（同一编辑实例）同样不开放旧树", async () => {
    const before = '{"a":1}';
    const after = '{"a":"x"}';
    bodies[before] = treeFor("integer", "1");
    bodies[after] = treeFor("string", "x");

    const { rerender } = render(<Harness text={before} sourceKey={before} />);
    await expandToId();

    hold(after);
    rerender(<Harness text={after} sourceKey={after} />);
    await waitForFieldTreeRequest(after);
    expect(queryTreeItem("id")).toBeNull();

    await act(async () => {
      releaseFor(after, bodies[after]);
    });
    await expandToId();
    selectDataId();
    expect(detailText()).toContain("string");
  });

  it("隐藏同一来源只暂停新读取，保留已加载树与未应用字段表单", async () => {
    const text = '{"data":{"id":1}}';
    bodies[text] = treeFor("integer", "1");
    const { rerender } = render(<Harness text={text} sourceKey="same" />);
    await expandToId();
    selectDataId();
    expect(detailText()).toContain("integer");
    fireEvent.click(screen.getByRole("button", { name: "＋添加断言" }));
    await selectAntOption("断言类型", "等于");
    fireEvent.change(screen.getByLabelText("期望值"), { target: { value: "未应用条件" } });
    const readsBeforeHide = fieldTreeRequestCount(text);

    rerender(<Harness text={text} sourceKey="same" enabled={false} />);
    expect(queryTreeItem("id")).toBeTruthy();
    expect(detailText()).toContain("integer");
    expect(screen.getByDisplayValue("未应用条件")).toBeTruthy();

    rerender(<Harness text={text} sourceKey="same" enabled />);
    expect(screen.getByDisplayValue("未应用条件")).toBeTruthy();
    expect(queryTreeItem("id")).toBeTruthy();
    expect(fieldTreeRequestCount(text)).toBe(readsBeforeHide);
  });
});
