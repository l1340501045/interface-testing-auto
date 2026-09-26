/**
 * 未保存内容的离开保护。
 *
 * 切换工作空间／项目／用例、关闭编辑器、退出登录都会卸载编辑器。没有统一拦截时，
 * 用户刚敲进去的草稿会被静默丢弃。这里通过真实外壳验证：
 * 有未保存修改时必须先确认，拒绝确认就不切换，接受确认才切换。
 */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";

/**
 * 会话状态可改：工作空间角色决定“有没有新建项目的入口”，工作空间列表决定能不能切换。
 * `vi.mock` 的工厂被提升到模块顶部执行，共享可变状态只能放在 vi.hoisted 的容器里。
 */
const fixtures = vi.hoisted(() => ({
  workspaceId: "11111111-1111-4111-8111-111111111111",
  workspaceB: "45454545-4545-4545-8545-454545454545",
  workspaces: [] as { id: string; name: string; role: string }[],
}));

const WORKSPACE_ID = fixtures.workspaceId;
const WORKSPACE_B = fixtures.workspaceB;
const PROJECT_A = "22222222-2222-4222-8222-222222222222";
const PROJECT_B = "33333333-3333-4333-8333-333333333333";
const ENV_ID = "44444444-4444-4444-8444-444444444444";
const CREATED_1 = "67676767-6767-4767-8767-676767676767";
const CREATED_2 = "68686868-6868-4868-8868-686868686868";
const CASE_2 = "69696969-6969-4969-8969-696969696969";
const CREATED_CASE = "70707070-7070-4070-8070-707070707070";
const FOLDER_A = "81818181-8181-4181-8181-818181818181";
const FOLDER_B = "82828282-8282-4282-8282-828282828282";

vi.mock("./session/useSession", () => ({
  useSession: () => ({
    session: {
      user: { id: "u-1", username: "tester", display_name: "测试员", is_admin: true },
      workspaces: fixtures.workspaces,
    },
    loading: false,
    error: null,
    expired: false,
    login: vi.fn(),
    logout: vi.fn(),
  }),
}));

vi.mock("./api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api/client")>();
  return {
    ...actual,
    apiGet: vi.fn(),
    apiSend: vi.fn(),
    apiSendWithMeta: vi.fn(),
    apiDelete: vi.fn(),
  };
});

// 执行面板自己会拉环境与运行状态，与本文件关注的离开保护无关，替换为占位。
vi.mock("./runs/RunPanel", () => ({
  RunPanel: () => <div data-testid="run-panel" />,
}));

import { apiGet, apiSend, apiSendWithMeta } from "./api/client";

const apiGetMock = vi.mocked(apiGet);
const apiSendMock = vi.mocked(apiSend);
const apiSendWithMetaMock = vi.mocked(apiSendWithMeta);

let environmentList: unknown[] = [];
/** 项目按工作空间分开：切换工作空间必须看到它自己的那一份。 */
let projectsByWorkspace: Record<string, unknown[]> = {};
/** 用例按项目分开：切换项目必须只看到这个项目的用例。 */
let casesByProject: Record<string, unknown[]> = {};
/** 目录按项目分开：选中的目录只过滤它所属项目的列表。 */
let foldersByProject: Record<string, unknown[]> = {};
/** 创建用例的请求体，用来核对新建时的目录归属。 */
const casePosts: { path: string; body: unknown }[] = [];
/** 创建项目的请求体，以及“先挂起再放行”用的开关。 */
const postCalls: { path: string; body: unknown }[] = [];
let holdCreate = false;
let releaseCreate: (() => void) | null = null;
let createdId = "";

function project(key: string, name: string, workspaceId: string, id: string): unknown {
  return { id, workspace_id: workspaceId, key, name, status: "active", role: "admin", pool_id: null };
}

function caseRow(id: string, name: string, folderId: string | null = null): unknown {
  return { id, folder_id: folderId, name, method: "GET", status: "draft", rev: 1, latest_version: null };
}

function folderRow(id: string, name: string): unknown {
  return { id, parent_id: null, name, archived_at: null };
}

/**
 * 打开用例时的详情响应：名字与状态从**目录行**取同一条记录。
 *
 * 两边各写一份的话，列表上是某个名字、打开后却变成另一个，对照断言就会指向一个屏幕
 * 之外的取值。
 */
function caseDetail(id: string): unknown {
  for (const rows of Object.values(casesByProject)) {
    const row = (rows as { id: string; name: string; folder_id?: string | null }[]).find((item) => item.id === id);
    if (row === undefined) continue;
    return {
      id,
      folder_id: row.folder_id ?? null,
      name: row.name,
      request: {
        method: "GET",
        path: "/orders",
        query_params: [],
        headers: [],
        body_type: "none",
        body: "",
      },
      assertions: [],
      rev: 1,
      status: "draft",
      latest_version: null,
      updated_at: "2026-09-14T00:00:00Z",
      snapshot_hash: `hash-${id}-1`,
    };
  }
  throw new Error(`测试未覆盖的用例：${id}`);
}

/** 请求路径里的项目 id：用例与目录都按项目分开，靠它选出该返回哪一份。 */
function projectInPath(path: string): string {
  return /\/projects\/([0-9a-f-]{36})/.exec(path)?.[1] ?? "";
}

function routes(path: string, method = "GET", body?: unknown): unknown {
  // 列表请求带查询串（按目录过滤），而 `endsWith` 判断的是路径本身：先分开。
  const clean = path.split("?")[0]!;
  if (method === "POST" && path.endsWith("/projects")) {
    const workspaceId = path.split("/")[2] ?? "";
    postCalls.push({ path, body });
    const record = {
      id: createdId,
      workspace_id: workspaceId,
      key: (body as { key: string }).key,
      name: (body as { name: string }).name,
      status: "active",
      role: "admin",
      pool_id: null,
    };
    projectsByWorkspace[workspaceId] = [...(projectsByWorkspace[workspaceId] ?? []), record];
    return record;
  }
  if (clean.endsWith("/projects")) {
    const workspaceId = path.split("/")[2] ?? "";
    return projectsByWorkspace[workspaceId] ?? [];
  }
  if (clean.endsWith("/environments")) return environmentList;
  if (method === "POST" && clean.endsWith("/cases")) {
    const payload = (body ?? {}) as { name?: string; folder_id?: string | null };
    casePosts.push({ path, body });
    const id = CREATED_CASE;
    const row = caseRow(id, String(payload.name ?? ""), payload.folder_id ?? null);
    const project = projectInPath(clean);
    casesByProject[project] = [...(casesByProject[project] ?? []), row];
    return caseDetail(id);
  }
  if (clean.endsWith("/cases")) {
    const rows = casesByProject[projectInPath(clean)] ?? [];
    const folderFilter = /[?&]folder_id=([^&]+)/.exec(path);
    if (folderFilter === null) return rows;
    const folderId = decodeURIComponent(folderFilter[1]!);
    return rows.filter((row) => (row as { folder_id: string | null }).folder_id === folderId);
  }
  if (clean.endsWith("/versions")) return [];
  const openedCase = /\/cases\/([0-9a-f-]{36})$/.exec(clean);
  if (openedCase !== null) return caseDetail(openedCase[1]!);
  if (clean.endsWith("/folders")) return foldersByProject[projectInPath(clean)] ?? [];
  if (clean.endsWith("/assertion-types")) return [];
  throw new Error(`测试未覆盖的请求：${method} ${path}`);
}

/**
 * 本文件所有用例共用的起点：清掉上个用例留下的数据、装回请求桩、默认给当前工作空间
 * 两个项目（离开保护的用例要在两个项目之间切换）。放在最外层，是因为这些替身数据是
 * 模块级的——只有 describe 级别的钩子会让另一个 describe 里的用例读到上个用例的残留。
 */
beforeEach(() => {
  window.history.replaceState(null, "", "#/workbench");
  apiGetMock.mockReset();
  apiSendMock.mockReset();
  apiSendWithMetaMock.mockReset();
  environmentList = [];
  projectsByWorkspace = {
    [WORKSPACE_ID]: [
      project("alpha", "项目甲", WORKSPACE_ID, PROJECT_A),
      project("beta", "项目乙", WORKSPACE_ID, PROJECT_B),
    ],
  };
  casesByProject = {};
  foldersByProject = {};
  casePosts.length = 0;
  postCalls.length = 0;
  holdCreate = false;
  releaseCreate = null;
  createdId = CREATED_1;
  fixtures.workspaces = [{ id: WORKSPACE_ID, name: "默认工作空间", role: "admin" }];
  apiGetMock.mockImplementation((async (path: string) => routes(path)) as never);
  apiSendMock.mockImplementation((async (path: string, method: string, body: unknown) =>
    routes(path, method, body)) as never);
  // 用例保存走带 ETag 的调用；同样打到替身上，让“新建用例”这一段是真的走通的。
  apiSendWithMetaMock.mockImplementation((async (path: string, method: string, body: unknown) => ({
    data: routes(path, method, body),
    etag: '"1"',
  })) as never);
  // 每条用例换一个全新的确认框替身：调用次数要能按用例清零，否则“这次有没有弹确认”
  // 这类断言会读到上一条用例留下的调用记录。
  window.confirm = vi.fn(() => true);
});

/** 新建项目表单里项目键那一栏的标签；两个 describe 都要按它定位。 */
const KEY_LABEL = "项目键（字母、数字、下划线或短横线）";

describe("未保存内容的离开保护", () => {

  /**
   * 外壳在项目列表到达后会自动选中第一个项目。这发生在挂载之后的一次副作用里，
   * 因此必须先等它落定再操作用户控件，否则“用户选择”会跟自动选中互相覆盖，
   * 测出来的失败与产品行为无关。
   */
  async function renderShell() {
    render(<App />);
    const select = (await screen.findByLabelText("项目")) as HTMLSelectElement;
    await waitFor(() => expect(select.value).toBe(PROJECT_A));
    // 选中项目之后，外壳还要跑一次“重置下游选择”的副作用（会把编辑器清空）。
    // 不等这部分副作用落定就点击，用户操作会和它排进同一批更新而互相覆盖：
    // 症状是点“新建”后编辑器又被立刻关闭，看起来像产品没反应。
    await act(async () => {});
  }

  /** 打开新建用例并在名称里输入内容，制造未保存修改。 */
  async function openDirtyNewCase() {
    await renderShell();
    const create = await screen.findByRole("button", { name: "＋新建用例" });
    fireEvent.click(create);
    const name = await screen.findByLabelText("用例名称");
    fireEvent.change(name, { target: { value: "尚未保存的用例" } });
    await act(async () => {});
  }

  it("新建用例里输入内容后算作有未保存修改，切换用例前先确认", async () => {
    await openDirtyNewCase();
    // 阻止离开，编辑器必须还在，草稿不能被丢掉。
    vi.mocked(window.confirm).mockReturnValue(false);
    fireEvent.click(screen.getByRole("button", { name: "＋新建用例" }));

    await waitFor(() => expect(window.confirm).toHaveBeenCalled());
    expect((screen.getByLabelText("用例名称") as HTMLInputElement).value).toBe("尚未保存的用例");
  });

  it("确认离开后才真正切换用例", async () => {
    await openDirtyNewCase();
    vi.mocked(window.confirm).mockReturnValue(true);
    // 再点一次“新建”会重建编辑器；确认后应当回到空白草稿。
    fireEvent.click(screen.getByRole("button", { name: "＋新建用例" }));

    await waitFor(() => expect(window.confirm).toHaveBeenCalled());
    await waitFor(() => expect((screen.getByLabelText("用例名称") as HTMLInputElement).value).toBe(""));
  });

  it("在四个页面间往返不会重建编辑器或丢失草稿", async () => {
    await openDirtyNewCase();
    const draft = screen.getByLabelText("用例名称") as HTMLInputElement;

    fireEvent.click(screen.getByRole("button", { name: "环境配置" }));
    expect(window.location.hash).toBe("#/environments");
    expect(screen.queryByRole("region", { name: "接口工作台" })).toBeNull();
    expect(document.getElementById("case-name")).toBe(draft);

    fireEvent.click(screen.getByRole("button", { name: "接口工作台" }));
    expect(screen.getByLabelText("用例名称")).toBe(draft);
    expect(draft.value).toBe("尚未保存的用例");
    expect(window.confirm).not.toHaveBeenCalled();
  });

  it("配置页浏览另一个环境不会改变工作台执行目标", async () => {
    environmentList = [
      { id: ENV_ID, name: "环境 A", kind: "test", base_url: "http://a.test", pool_id: null, variables: {}, status: "active" },
      { id: "66666666-6666-4666-8666-666666666666", name: "环境 B", kind: "test", base_url: "http://b.test", pool_id: null, variables: {}, status: "active" },
    ];
    await openDirtyNewCase();
    const draft = screen.getByLabelText("用例名称") as HTMLInputElement;
    await waitFor(() => expect((screen.getByLabelText("执行环境") as HTMLSelectElement).value).toBe(ENV_ID));

    fireEvent.click(screen.getByRole("button", { name: "环境配置" }));
    fireEvent.click(screen.getByText("环境（2）"));
    fireEvent.click(screen.getByRole("button", { name: "环境 B" }));
    fireEvent.click(screen.getByRole("button", { name: "返回接口工作台" }));

    expect(screen.getByLabelText("用例名称")).toBe(draft);
    expect(draft.value).toBe("尚未保存的用例");
    expect((screen.getByLabelText("执行环境") as HTMLSelectElement).value).toBe(ENV_ID);
    expect(casePosts).toHaveLength(0);
    expect(postCalls).toHaveLength(0);

    fireEvent.change(screen.getByLabelText("执行环境"), {
      target: { value: "66666666-6666-4666-8666-666666666666" },
    });
    expect((screen.getByLabelText("执行环境") as HTMLSelectElement).value).toBe(
      "66666666-6666-4666-8666-666666666666",
    );
  });

  it("切换项目会带走编辑器，因此同样要确认；拒绝后仍停在原项目", async () => {
    await openDirtyNewCase();
    const projectSelect = screen.getByLabelText("项目") as HTMLSelectElement;
    expect(projectSelect.value).toBe(PROJECT_A);

    vi.mocked(window.confirm).mockReturnValue(false);
    fireEvent.change(projectSelect, { target: { value: PROJECT_B } });

    await waitFor(() => expect(window.confirm).toHaveBeenCalled());
    expect((screen.getByLabelText("项目") as HTMLSelectElement).value).toBe(PROJECT_A);
    expect((screen.getByLabelText("用例名称") as HTMLInputElement).value).toBe("尚未保存的用例");
  });

  it("没有未保存修改时切换项目不打扰用户", async () => {
    await renderShell();
    const projectSelect = screen.getByLabelText("项目") as HTMLSelectElement;
    fireEvent.change(projectSelect, { target: { value: PROJECT_B } });

    await waitFor(() => expect((screen.getByLabelText("项目") as HTMLSelectElement).value).toBe(PROJECT_B));
    expect(window.confirm).not.toHaveBeenCalled();
  });

  /**
   * 环境里的普通变量也是草稿。**只把已有变量的值改一下**同样必须触发拦截：
   * 基线曾经比的是显示文本、而且拿永不变化的 `row.original` 当草稿侧取值，
   * 这种改动算不出差别，用户切走时输入直接被丢掉。
   */
  it("只改已有环境变量的值也算未保存修改：切换项目前先确认，拒绝后输入仍在", async () => {
    environmentList = [
      {
        id: ENV_ID,
        name: "本地测试环境",
        kind: "test",
        base_url: "http://target-service:8080",
        pool_id: null,
        variables: { count: { type: "number", text: "1" } },
        status: "active",
      },
    ];
    await renderShell();

    fireEvent.click(screen.getByRole("button", { name: "环境配置" }));
    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    fireEvent.change(screen.getByLabelText("值"), { target: { value: "2" } });
    await act(async () => {});

    vi.mocked(window.confirm).mockReturnValue(false);
    fireEvent.change(screen.getByLabelText("项目"), { target: { value: PROJECT_B } });

    await waitFor(() => expect(window.confirm).toHaveBeenCalled());
    // 拒绝离开后仍停在原项目，刚改的值还在输入框里。
    expect((screen.getByLabelText("项目") as HTMLSelectElement).value).toBe(PROJECT_A);
    expect((screen.getByLabelText("值") as HTMLInputElement).value).toBe("2");
  });

  /**
   * 反过来：确实有别的表单没保存时，切到新项目的确认不能省。
   *
   * 上一条用例证明了“排除创建表单自己”不会退化成“永远不确认”——这里让编辑器里带着
   * 未保存的名称再建项目，确认必须出现；确认后才切范围，编辑器也确实被卸载了。
   */
  it("创建项目后自动切到新项目：别的表单确实有未保存内容时仍然要确认", async () => {
    await openDirtyNewCase();
    fireEvent.click(screen.getByRole("button", { name: "＋新建项目" }));
    fireEvent.change(screen.getByLabelText(KEY_LABEL), { target: { value: "gamma" } });
    fireEvent.change(screen.getByLabelText("项目名称"), { target: { value: "项目丙" } });
    createdId = CREATED_1;

    vi.mocked(window.confirm).mockReturnValue(true);
    fireEvent.click(screen.getByRole("button", { name: "创建项目" }));

    await waitFor(() => expect(window.confirm).toHaveBeenCalled());
    await waitFor(() => expect((screen.getByLabelText("项目") as HTMLSelectElement).value).toBe(CREATED_1));
    // 编辑器被卸载，那条没保存的名称真的丢了：所以这次确认是必要的。
    expect(screen.queryByLabelText("用例名称")).toBeNull();
  });
});

/**
 * 新建项目入口（VQ1）。
 *
 * 空态曾经是唯一入口：工作空间里有了第一个项目之后，管理员再也建不出第二个——组件
 * 要求里没有“项目列表管理”，但“把下一个项目建出来”是同一个接口、同一份表单的事。
 * 这里盯住四件事：入口在已有项目时仍然在；创建过程算忙碌，离开要先确认；迟到的结果
 * 不会把用户切回旧工作空间的项目；查看者连入口都看不到。
 */
describe("新建项目入口", () => {
  beforeEach(() => {
    // 创建项目的 POST 要记录请求体，还可以先挂起（验证迟到结果）；其余请求照旧走 routes。
    apiSendMock.mockImplementation((async (
      path: string,
      method: string,
      body: unknown,
      parse: (raw: unknown) => unknown,
    ) => {
      if (method === "POST" && path.endsWith("/projects")) {
        postCalls.push({ path, body });
        if (holdCreate) await new Promise<void>((resolve) => { releaseCreate = resolve; });
        const workspaceId = path.split("/")[2] ?? "";
        const record = {
          id: createdId,
          workspace_id: workspaceId,
          key: (body as { key: string }).key,
          name: (body as { name: string }).name,
          status: "active",
          role: "admin",
          pool_id: null,
        };
        projectsByWorkspace[workspaceId] = [...(projectsByWorkspace[workspaceId] ?? []), record];
        return parse(record);
      }
      return routes(path);
    }) as never);
  });

  async function renderShell(): Promise<HTMLSelectElement> {
    render(<App />);
    const select = (await screen.findByLabelText("项目")) as HTMLSelectElement;
    await waitFor(() => expect(select.value).toBe(PROJECT_A));
    await act(async () => {});
    return select;
  }

  function fillCreateForm(projectKey: string, projectName: string) {
    fireEvent.change(screen.getByLabelText(KEY_LABEL), { target: { value: projectKey } });
    fireEvent.change(screen.getByLabelText("项目名称"), { target: { value: projectName } });
  }

  it("已有项目时仍能新建：第二个项目也被正确选中，并且只看到属于它的数据", async () => {
    casesByProject[PROJECT_A] = [caseRow(CASE_2, "项目甲的用例")];
    const select = await renderShell();
    // 用例名也会出现在凭证授权的“用例”下拉里，所以断言限定在用例目录内。
    /**
     * 用例目录按工作空间／项目重挂载（选中的目录属于当前范围，不能带到下一个项目），
     * 因此每换一次项目都要重新取节点：旧节点的身份已经被换掉了。
     */
    const browserNow = () => screen.getByLabelText("用例目录");
    expect(await within(browserNow()).findByText("项目甲的用例")).toBeTruthy();

    // 已经有项目：表单默认收起（不挤占选择区），但入口必须在。
    expect(screen.queryByLabelText(KEY_LABEL)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "＋新建项目" }));

    casesByProject[CREATED_1] = [caseRow(CASE_2, "项目丙的用例")];
    createdId = CREATED_1;
    fillCreateForm("gamma", "项目丙");
    fireEvent.click(screen.getByRole("button", { name: "创建项目" }));

    await waitFor(() => expect(select.value).toBe(CREATED_1));
    // 屏幕上没有别的未保存内容：切到新项目不该弹确认框。创建表单自己的草稿不算——
    // 它刚被清掉、结果也已经看到了（真实页面上这里曾弹出一个无所指的确认框）。
    expect(window.confirm).not.toHaveBeenCalled();
    expect(postCalls).toEqual([
      { path: `/workspaces/${WORKSPACE_ID}/projects`, body: { key: "gamma", name: "项目丙" } },
    ]);
    // 数据隔离：切到新项目后列表换成它自己的用例，上一个项目的用例不再出现。
    expect(await within(browserNow()).findByText("项目丙的用例")).toBeTruthy();
    expect(within(browserNow()).queryByText("项目甲的用例")).toBeNull();

    // 再建一个：入口第二次使用要同样成立。
    casesByProject[CREATED_2] = [caseRow(CASE_2, "项目丁的用例")];
    createdId = CREATED_2;
    fireEvent.click(screen.getByRole("button", { name: "＋新建项目" }));
    fillCreateForm("delta", "项目丁");
    fireEvent.click(screen.getByRole("button", { name: "创建项目" }));

    await waitFor(() => expect(select.value).toBe(CREATED_2));
    expect(postCalls[1]).toEqual({
      path: `/workspaces/${WORKSPACE_ID}/projects`,
      body: { key: "delta", name: "项目丁" },
    });
    expect(await within(browserNow()).findByText("项目丁的用例")).toBeTruthy();
    expect(within(browserNow()).queryByText("项目丙的用例")).toBeNull();
    // 四个项目都在选择器里，顺序与创建顺序一致。
    expect(Array.from(select.options).map((option) => option.value)).toEqual([
      PROJECT_A,
      PROJECT_B,
      CREATED_1,
      CREATED_2,
    ]);
  });

  it("查看者看不到新建项目的入口，也看不到可提交的表单", async () => {
    fixtures.workspaces = [{ id: WORKSPACE_ID, name: "默认工作空间", role: "viewer" }];
    await renderShell();

    expect(screen.queryByRole("button", { name: "＋新建项目" })).toBeNull();
    expect(screen.queryByLabelText(KEY_LABEL)).toBeNull();
    expect(screen.queryByRole("button", { name: "创建项目" })).toBeNull();
  });

  it("查看者在还没有项目的工作空间里只看到提示，没有可提交的表单", async () => {
    fixtures.workspaces = [{ id: WORKSPACE_ID, name: "默认工作空间", role: "viewer" }];
    projectsByWorkspace[WORKSPACE_ID] = [];
    render(<App />);

    expect(await screen.findByText("当前工作空间还没有项目")).toBeTruthy();
    expect(screen.getByText(/当前角色不能创建项目/)).toBeTruthy();
    expect(screen.queryByLabelText(KEY_LABEL)).toBeNull();
    expect(screen.queryByRole("button", { name: "创建项目" })).toBeNull();
  });

  it("创建期间切换工作空间要先确认；确认后迟到的结果不会把用户切回旧工作空间的项目", async () => {
    fixtures.workspaces = [
      { id: WORKSPACE_ID, name: "默认工作空间", role: "admin" },
      { id: WORKSPACE_B, name: "第二工作空间", role: "admin" },
    ];
    projectsByWorkspace[WORKSPACE_B] = [project("beta", "项目乙", WORKSPACE_B, PROJECT_B)];
    const select = await renderShell();
    expect(select.value).toBe(PROJECT_A);

    fireEvent.click(screen.getByRole("button", { name: "＋新建项目" }));
    fillCreateForm("gamma", "项目丙");
    holdCreate = true;
    createdId = CREATED_1;
    fireEvent.click(screen.getByRole("button", { name: "创建项目" }));
    await waitFor(() => expect(postCalls).toHaveLength(1));

    // 请求在飞时离开走的是 busy 分支：提示说的是“有正在进行的操作”，不是单纯的
    // “表单没保存”。拒绝确认就停在原工作空间。
    vi.mocked(window.confirm).mockReturnValue(false);
    fireEvent.change(screen.getByLabelText("工作空间"), { target: { value: WORKSPACE_B } });
    await waitFor(() =>
      expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("正在进行的操作")),
    );
    expect((screen.getByLabelText("工作空间") as HTMLSelectElement).value).toBe(WORKSPACE_ID);

    // 确认之后才真的换工作空间；新工作空间的表单不被上一个范围的忙碌锁住。
    vi.mocked(window.confirm).mockReturnValue(true);
    fireEvent.change(screen.getByLabelText("工作空间"), { target: { value: WORKSPACE_B } });
    await waitFor(() =>
      expect((screen.getByLabelText("项目") as HTMLSelectElement).value).toBe(PROJECT_B),
    );
    fireEvent.click(screen.getByRole("button", { name: "＋新建项目" }));
    fillCreateForm("epsilon", "项目戊");

    // 旧工作空间的创建结果这时才回来：不切项目、不清空刚输入的内容、也不报错。
    await act(async () => {
      releaseCreate?.();
    });
    expect((screen.getByLabelText("项目") as HTMLSelectElement).value).toBe(PROJECT_B);
    expect((screen.getByLabelText(KEY_LABEL) as HTMLInputElement).value).toBe("epsilon");
    expect((screen.getByLabelText("项目名称") as HTMLInputElement).value).toBe("项目戊");
    expect(screen.queryByText(/创建项目失败/)).toBeNull();
    // 项目本身确实建在了发起它的工作空间里，只是没有反过来改变用户当前的范围。
    expect((projectsByWorkspace[WORKSPACE_ID] ?? []).map((item) => (item as { key: string }).key)).toEqual([
      "alpha",
      "beta",
      "gamma",
    ]);
    expect((projectsByWorkspace[WORKSPACE_B] ?? []).map((item) => (item as { key: string }).key)).toEqual([
      "beta",
    ]);
  });

  /**
   * VR1：创建请求**提交之后**才产生的草稿，同样必须挡住这次自动切换。
   *
   * 库里那条“切到新项目”的判断发生在 `await` 之后，而它用的是发起创建时那一帧的
   * 离开登记表：等待期间用户回到当前用例继续敲字，新产生的 dirty 它看不到，于是不弹
   * 确认就切范围、把编辑器连同刚输入的内容一起卸载——草稿静默丢失。
   *
   * 时序是本条用例的全部要点：创建响应先挂住，提交之后再编辑用例，最后才放行响应。
   * “提交前就已经脏”的那条既有用例（上面 describe 里）走的是同步分支，替代不了它：
   * 提交前脏时登记表本来就带着那一条，缺陷不会暴露。
   */
  it("创建请求在飞时才编辑的用例，放行响应后仍要先确认；取消后新输入还在", async () => {
    casesByProject[PROJECT_A] = [caseRow(CASE_2, "项目甲的用例")];
    const select = await renderShell();

    // 打开一条干净的用例：此时屏幕上没有任何未保存内容。
    const browser = screen.getByLabelText("用例目录");
    fireEvent.click(await within(browser).findByRole("button", { name: /项目甲的用例/ }));
    const nameField = (await screen.findByLabelText("用例名称")) as HTMLInputElement;
    await act(async () => {});
    expect(nameField.value).toBe("项目甲的用例");

    fireEvent.click(screen.getByRole("button", { name: "＋新建项目" }));
    fillCreateForm("gamma", "项目丙");
    holdCreate = true;
    createdId = CREATED_1;
    fireEvent.click(screen.getByRole("button", { name: "创建项目" }));
    await waitFor(() => expect(postCalls).toHaveLength(1));
    // 提交那一刻用例还是干净的：这条时序才考得住后面的判断。
    expect(nameField.value).toBe("项目甲的用例");

    // 请求在飞时回到用例里继续编辑 —— 新草稿产生在创建**提交之后**。
    fireEvent.change(nameField, { target: { value: "提交后才改的名字" } });
    await act(async () => {});

    vi.mocked(window.confirm).mockReturnValue(false);
    await act(async () => {
      releaseCreate?.();
    });

    // 必须问过一次，而且问的是“表单没保存”而不是“有正在进行的操作”：创建本身已经结束，
    // 挡路的是刚输入的草稿。
    await waitFor(() => expect(window.confirm).toHaveBeenCalled());
    expect(vi.mocked(window.confirm).mock.calls[0]?.[0]).toContain("未保存的修改");
    // 取消：不切项目、编辑器还在、刚敲进去的名字原样保留。
    expect(select.value).toBe(PROJECT_A);
    expect((screen.getByLabelText("用例名称") as HTMLInputElement).value).toBe("提交后才改的名字");

    // 项目本身确实建出来了，只是这次切换被用户拒掉；重新发起时确认一次就能正常切过去，
    // 不会被上一轮的草稿永久卡住。
    expect((projectsByWorkspace[WORKSPACE_ID] ?? []).map((item) => (item as { key: string }).key)).toEqual([
      "alpha",
      "beta",
      "gamma",
    ]);
    fireEvent.change(screen.getByLabelText("用例名称"), { target: { value: "" } });
    await act(async () => {});
    vi.mocked(window.confirm).mockReturnValue(true);
    fireEvent.change(select, { target: { value: CREATED_1 } });
    await waitFor(() => expect(select.value).toBe(CREATED_1));
  });
});

/**
 * 用例目录的范围归属（FS1）。
 *
 * 选中哪个目录是**当前项目**的状态：它既决定列表怎么过滤，也决定「＋新建用例」的归属。
 * 跨项目留着它，切到 B 之后 B 的列表仍按 A 的目录过滤（看起来像“B 项目没有用例”），
 * 新建还会把 A 的目录 id 提交给 B，被服务端按“目录不存在”拒绝。
 */
describe("用例目录的范围归属", () => {
  it("在 A 项目选过目录后切到 B：列表默认不过滤，新建也不继承 A 的目录", async () => {
    foldersByProject[PROJECT_A] = [folderRow(FOLDER_A, "A 模块")];
    foldersByProject[PROJECT_B] = [folderRow(FOLDER_B, "B 模块")];
    // A 里那条挂在目录下，B 里那条未分组：B 的列表若还按 A 的目录过滤，它就看不见了。
    casesByProject[PROJECT_A] = [caseRow(CASE_2, "甲项目目录里的用例", FOLDER_A)];
    casesByProject[PROJECT_B] = [caseRow(CREATED_CASE, "乙项目的用例")];

    render(<App />);
    const projectSelect = (await screen.findByLabelText("项目")) as HTMLSelectElement;
    await waitFor(() => expect(projectSelect.value).toBe(PROJECT_A));
    await act(async () => {});

    // 在 A 项目里选中目录：列表按它过滤。
    fireEvent.click(within(screen.getByLabelText("用例目录")).getByRole("button", { name: "A 模块" }));
    await waitFor(() =>
      expect(within(screen.getByLabelText("用例目录")).getByText("甲项目目录里的用例")).toBeTruthy(),
    );

    fireEvent.change(projectSelect, { target: { value: PROJECT_B } });
    await waitFor(() => expect(projectSelect.value).toBe(PROJECT_B));
    await act(async () => {});

    // B 的列表按 B 自己的范围拉取：未分组的那条也在，说明没有沿用 A 的目录过滤。
    expect(await within(screen.getByLabelText("用例目录")).findByText("乙项目的用例")).toBeTruthy();

    // 新建用例：归属是「未分组」，不会把 A 的目录带进 B 项目。
    fireEvent.click(within(screen.getByLabelText("用例目录")).getByRole("button", { name: "＋新建用例" }));
    const folderSelect = (await screen.findByLabelText("所属目录")) as HTMLSelectElement;
    expect(folderSelect.value).toBe("");
    expect(Array.from(folderSelect.options).map((option) => option.textContent)).toEqual(["未分组", "B 模块"]);

    fireEvent.change(await screen.findByLabelText("用例名称"), { target: { value: "乙项目的新用例" } });
    fireEvent.click(screen.getByRole("button", { name: "创建用例" }));
    await waitFor(() => expect(casePosts).toHaveLength(1));
    expect(casePosts[0]?.body).toMatchObject({ name: "乙项目的新用例", folder_id: null });
  });
});
