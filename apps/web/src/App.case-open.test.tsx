import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({
  detailGate: null as Promise<void> | null,
  detailEntered: false,
  opened: [] as string[],
  projects: [] as unknown[],
  projectListeners: new Set<() => void>(),
}));

const WORKSPACE = "11111111-1111-4111-8111-111111111111";
const PROJECT = "22222222-2222-4222-8222-222222222222";
const CASE = "33333333-3333-4333-8333-333333333333";

vi.mock("./session/useSession", () => ({
  useSession: () => ({
    session: { user: { user_id: "u1", username: "u", display_name: "用户", is_admin: true }, workspaces: [{ id: WORKSPACE, name: "空间", role: "admin" }] },
    loading: false, error: null, expired: false, login: vi.fn(), logout: vi.fn(),
  }),
}));

vi.mock("./projects/useProjects", async () => {
  const React = await import("react");
  return {
    useProjects: () => {
      const data = React.useSyncExternalStore(
        (listener) => { state.projectListeners.add(listener); return () => state.projectListeners.delete(listener); },
        () => state.projects,
      );
      return { data, loading: data.length === 0, error: null, reload: vi.fn() };
    },
    useEnvironments: () => ({ data: [], loading: false, error: null, reload: vi.fn() }),
  };
});
vi.mock("./cases/useCases", () => ({ useFolders: () => ({ data: [], loading: false, error: null, reload: vi.fn() }) }));
vi.mock("./cases/CaseBrowser", () => ({ CaseBrowser: () => <div>工作台目录</div> }));
vi.mock("./cases/CaseLibrary", () => ({
  CaseLibrary: ({ onOpen }: { onOpen: (id: string) => Promise<string> }) => <button type="button" onClick={() => void onOpen(CASE)}>打开测试用例</button>,
}));
vi.mock("./cases/CaseEditor", () => ({ CaseEditor: ({ caseSummaryId }: { caseSummaryId: string }) => <div data-testid="case-editor">编辑器 {caseSummaryId}</div> }));
vi.mock("./projects/EnvironmentPanel", () => ({ EnvironmentPanel: () => <div>环境面板</div> }));
vi.mock("./admin/AdminPanel", () => ({ AdminPanel: () => <div>管理面板</div> }));
vi.mock("./runs/RunCenter", () => ({ RunCenter: () => <div>运行中心</div> }));

vi.mock("./api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api/client")>();
  return {
    ...actual,
    apiGet: vi.fn(),
    apiSend: vi.fn(async (path: string, method: string, _body: unknown, parse: (raw: unknown) => unknown) => {
      if (method === "GET" && path.endsWith(`/cases/${CASE}`)) {
        state.detailEntered = true;
        if (state.detailGate !== null) await state.detailGate;
        return parse({
          id: CASE, folder_id: null, name: "用例", request: { method: "GET", path: "/", query_params: [], headers: [], body_type: "none", body: "" },
          assertions: [], rev: 1, status: "draft", latest_version: null, updated_at: "2026-10-04T00:00:00Z", snapshot_hash: "hash",
        });
      }
      if (method === "POST" && path.endsWith(`/case-preferences/${CASE}/opened`)) {
        state.opened.push(CASE);
        return parse({ case_id: CASE, favorite: false, last_opened_at: "2026-10-04T00:00:00Z" });
      }
      throw new Error(`未覆盖请求：${method} ${path}`);
    }),
  };
});

import { App } from "./App";

beforeEach(() => {
  window.history.replaceState(null, "", "#/cases");
  state.detailGate = null;
  state.detailEntered = false;
  state.opened.length = 0;
  state.projects = [{ id: PROJECT, workspace_id: WORKSPACE, key: "p", name: "项目", status: "active", role: "admin", pool_id: null }];
  state.projectListeners.clear();
});

async function setProjects(projects: unknown[]) {
  await act(async () => {
    state.projects = projects;
    for (const listener of [...state.projectListeners]) listener();
  });
}

function nav(label: string): HTMLButtonElement {
  const button = Array.from(document.querySelectorAll<HTMLButtonElement>(".primary-nav .nav-item"))
    .find((item) => item.textContent?.trim() === label);
  if (button === undefined) throw new Error(`导航未挂载：${label}`);
  return button;
}

describe("App 用例打开意图", () => {
  it("异步项目挂载、section替换、滚动遗留与resize都绑定当前用例库节点", async () => {
    state.projects = [];
    let sectionTop = 120;
    let viewportHeight = 768;
    let scrollY = 0;
    const rect = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const top = this.getAttribute("aria-label") === "用例库" ? sectionTop : 0;
      return { x: 0, y: top, top, left: 0, right: 1000, bottom: top + 100, width: 1000, height: 100, toJSON: () => ({}) } as DOMRect;
    });
    vi.spyOn(window, "innerHeight", "get").mockImplementation(() => viewportHeight);
    vi.spyOn(window, "scrollY", "get").mockImplementation(() => scrollY);
    const scrollTo = vi.spyOn(window, "scrollTo").mockImplementation(() => { scrollY = 0; sectionTop = 120; });
    try {
      render(<App />);
      expect(screen.queryByRole("region", { name: "用例库" })).toBeNull();
      await setProjects([{ id: PROJECT, workspace_id: WORKSPACE, key: "p", name: "项目", status: "active", role: "admin", pool_id: null }]);
      let section = await screen.findByRole("region", { name: "用例库" });
      await waitFor(() => expect(section.style.height).toBe("648px"));

      await setProjects([]);
      await waitFor(() => expect(screen.queryByRole("region", { name: "用例库" })).toBeNull());
      sectionTop = 150;
      await setProjects([{ id: PROJECT, workspace_id: WORKSPACE, key: "p", name: "项目", status: "active", role: "admin", pool_id: null }]);
      section = await screen.findByRole("region", { name: "用例库" });
      await waitFor(() => expect(section.style.height).toBe("618px"));

      viewportHeight = 900;
      window.dispatchEvent(new Event("resize"));
      await waitFor(() => expect(section.style.height).toBe("750px"));

      sectionTop = -200;
      scrollY = 300;
      window.dispatchEvent(new Event("resize"));
      await waitFor(() => expect(section.style.height).toBe("780px"));
      expect(scrollTo).toHaveBeenCalled();
    } finally {
      rect.mockRestore();
      scrollTo.mockRestore();
      vi.restoreAllMocks();
    }
  });
  it("页面ABA往返同步作废旧详情，迟到结果零导航、零标签、零最近写入", async () => {
    let release!: () => void;
    state.detailGate = new Promise<void>((resolve) => { release = resolve; });
    render(<App />);
    await screen.findByRole("button", { name: "打开测试用例" });
    fireEvent.click(screen.getByRole("button", { name: "打开测试用例" }));
    expect(state.detailEntered).toBe(true);
    fireEvent.click(nav("环境配置"));
    fireEvent.click(nav("用例库"));
    release();
    await act(async () => {});
    expect(window.location.hash).toBe("#/cases");
    expect(screen.queryByTestId("case-editor")).toBeNull();
    expect(state.opened).toEqual([]);
  });

  it("有效意图打开标签并且只记录一次最近", async () => {
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "打开测试用例" }));
    await waitFor(() => expect(window.location.hash).toBe("#/workbench"));
    expect(await screen.findByTestId("case-editor")).toBeTruthy();
    await waitFor(() => expect(state.opened).toEqual([CASE]));
  });
});
