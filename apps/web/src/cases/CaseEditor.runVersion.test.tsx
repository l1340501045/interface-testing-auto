/**
 * 执行固定在哪一条已发布版本上（F6 的 draft_snapshot_hash 前后端契约）。
 *
 * 执行必须固定在“内容与屏幕上这份一致”的已发布版本上。判断依据只能是服务端算出的
 * 草稿摘要：发布时写进版本的摘要用的是同一个函数，两者相同才说明那一版就是屏幕上的
 * 这一份内容。
 *
 * 摘要只由 `{name, request, assertions}` 决定，**所属目录不在其中**。因此“只改了目录”
 * 这一类编辑不该固化出新版本：按“固定用例版本”签发的用途授权绑定的是版本 id，多出来
 * 的那一版会把原本匹配的授权换掉，界面上看不出任何异常。目录编辑本身仍要按 ETag 保存。
 *
 * 这里不替换 RunPanel：要验证的正是点下“保存并执行”之后真正发出去的那些请求
 * ——保存有没有带上必要编辑、执行固定在哪一版、有没有必要新建版本。
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const WORKSPACE_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const CASE_ID = "44444444-4444-4444-8444-444444444444";
const ENV_ID = "55555555-5555-4555-8555-555555555555";
const VERSION_ID = "66666666-6666-4666-8666-666666666666";
const NEW_VERSION_ID = "88888888-8888-4888-8888-888888888888";
const RUN_ID = "77777777-7777-4777-8777-777777777777";
const ASSERTION_ID = "assert-1";
const PUBLISHED_HASH = "hash-published";
const CHANGED_HASH = "hash-after-edit";
const FOLDER_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const FOLDER_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

/** 当前项目的可选目录；只改目录的编辑也必须在保存时提交（见 save 的三态语义）。 */
const FOLDERS = [
  { id: FOLDER_A, parent_id: null, name: "A 模块", archived_at: null },
  { id: FOLDER_B, parent_id: null, name: "B 模块", archived_at: null },
];

vi.mock("../session/useSession", () => ({
  useSession: () => ({
    session: {
      user: { id: "u-1", username: "tester", display_name: "测试员", is_admin: true },
      workspaces: [{ id: WORKSPACE_ID, name: "默认工作空间", role: "admin" }],
    },
    loading: false,
    error: null,
    expired: false,
    login: vi.fn(),
    logout: vi.fn(),
  }),
}));

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, apiGet: vi.fn(), apiSend: vi.fn(), apiSendWithMeta: vi.fn(), apiDelete: vi.fn() };
});

import { apiGet, apiSend, apiSendWithMeta, projectPath } from "../api/client";
import { rawToSpec, requestToRaw } from "./requestDraft";
import { CaseEditor } from "./CaseEditor";

const apiGetMock = vi.mocked(apiGet);
const apiSendMock = vi.mocked(apiSend);
const apiSendWithMetaMock = vi.mocked(apiSendWithMeta);

interface Call {
  method: string;
  path: string;
  body: unknown;
}

let calls: Call[] = [];
/** 当前草稿摘要：模拟服务端在保存／载入时算出的那一份。 */
let draftHash = PUBLISHED_HASH;
/** 服务端当前存着的用例内容（只有执行快照里的字段，目录不参与）与归属。 */
let storedContent = "";
let storedFolder: string | null = null;
let caseRev = 3;
/** 已发布版本，按版本号倒序，与后端一致。 */
let versions: unknown[] = [];
/** 保存请求“先挂起再放行”：验证请求在飞时用户继续改目录的边界。 */
let holdPatch = false;
let releasePatch: (() => void) | null = null;

/**
 * 稳定序列化：对象键排序。
 *
 * 替身靠“服务端存的内容与这次提交的内容是否相同”决定摘要变不变。直接比较 JSON 字符串
 * 会把“同一份内容、键序不同”也判成内容变了，于是只改目录的那种编辑被误判成需要发布，
 * 测出来的东西与产品行为无关。
 */
function stable(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => (left < right ? -1 : 1))
      .map(([key, item]) => `${JSON.stringify(key)}:${stable(item)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

/** 用例的执行快照内容：摘要的输入，目录不在其中。 */
function caseContent() {
  return {
    name: "查询订单",
    request: {
      method: "GET",
      path: "/orders",
      query_params: [],
      headers: [],
      body_type: "none",
      body: "",
    },
    assertions: [
      {
        id: ASSERTION_ID,
        target_source: "response.status",
        selector: [],
        type: "status_in",
        parameters: { values: [{ type: "number", text: "200" }] },
        compare_as: null,
        severity: "error",
        enabled: true,
        sort_order: 0,
      },
    ],
  };
}

function contentKey(content: {
  name?: unknown;
  request?: unknown;
  assertions?: unknown;
}): string {
  return stable({
    name: content.name,
    request:
      content.request === undefined ? undefined : rawToSpec(requestToRaw(content.request as never)),
    assertions: content.assertions,
  });
}

function caseDetail(snapshotHash: string, folderId: string | null = null) {
  return {
    id: CASE_ID,
    folder_id: folderId,
    ...caseContent(),
    rev: caseRev,
    status: "draft",
    latest_version: 1,
    updated_at: "2026-09-14T00:00:00Z",
    snapshot_hash: snapshotHash,
  };
}

function versionRow(id: string, version: number, snapshotHash: string) {
  return {
    id,
    case_id: CASE_ID,
    version,
    schema_version: 1,
    side_effect: "unknown",
    snapshot_hash: snapshotHash,
    created_by: "u-1",
    created_at: "2026-09-14T00:00:00Z",
  };
}

const TYPES = [
  {
    id: "status_in",
    label: "属于",
    group: "HTTP 状态",
    applies_to: ["integer", "number", "string"],
    params_schema: {
      values: { control: "value_list" as const, type: "number", label: "允许的状态码" },
    },
    summary: "属于这些值之一",
    operator_version: 1,
  },
];

/** 只实现本用例真正会走到的路由；其余请求直接失败，避免悄悄漏测。 */
function route(method: string, path: string, body: unknown): unknown | Promise<unknown> {
  calls.push({ method, path, body });
  if (method === "GET" && path.endsWith("/assertion-types")) return TYPES;
  if (method === "GET" && path.endsWith(`/cases/${CASE_ID}/versions`)) return versions;
  if (method === "GET" && path.endsWith(`/cases/${CASE_ID}`)) return caseDetail(draftHash, storedFolder);
  if (method === "GET" && path.includes("/runs")) return [];
  if (method === "PATCH" && path.endsWith(`/cases/${CASE_ID}`)) {
    const payload = (body ?? {}) as { name?: unknown; request?: unknown; assertions?: unknown };
    // 服务端按内容是否变化决定草稿摘要：目录不在摘要里，只改目录不会换摘要。
    const next = contentKey(payload);
    if (next !== storedContent) {
      storedContent = next;
      draftHash = CHANGED_HASH;
    }
    caseRev += 1;
    if ("folder_id" in (body as Record<string, unknown>)) {
      storedFolder = (body as { folder_id: string | null }).folder_id;
    }
    // 请求在飞时用户还能继续改目录：这里把响应挂起来，由用例决定何时放行。
    if (holdPatch) return new Promise((resolve) => { releasePatch = () => resolve(caseDetail(draftHash, storedFolder)); });
    return caseDetail(draftHash, storedFolder);
  }
  if (method === "POST" && path.endsWith(`/cases/${CASE_ID}/publish`)) {
    // 发布把**当前草稿**固化成新版本，摘要用的是同一个函数。
    const published = versionRow(NEW_VERSION_ID, 2, draftHash);
    versions = [published, ...versions];
    return published;
  }
  if (method === "POST" && path.endsWith("/runs")) return { id: RUN_ID };
  throw new Error(`测试未覆盖的请求：${method} ${path}`);
}

function renderEditor() {
  return render(
    <CaseEditor
      workspaceId={WORKSPACE_ID}
      projectId={PROJECT_ID}
      caseSummaryId={CASE_ID}
      environments={[
        {
          id: ENV_ID,
          name: "测试环境",
          kind: "test",
          base_url: "http://echo.test",
          pool_id: null,
          variables: {},
          status: "active",
        },
      ]}
      selectedEnvironmentId={ENV_ID}
      onSelectEnvironment={() => {}}
      onSaved={() => {}}
      onClose={() => {}}
      folders={FOLDERS}
    />,
  );
}

/**
 * 渲染并等到用例真的载入完成：名称、请求、断言与版本列表都要就位。
 *
 * 只等版本列表是不够的——详情与版本列表是两条独立的请求，先点按钮会在编辑器还没
 * 拿到详情时触发“有未保存修改”分支，转而先保存草稿，测的就不是执行固定在哪一版了。
 */
async function renderLoaded() {
  renderEditor();
  await waitFor(() =>
    expect((screen.getByLabelText("用例名称") as HTMLInputElement).value).toBe("查询订单"),
  );
  await screen.findByText(/^v1 · 副作用/);
}

function callsTo(path: string, method: string): Call[] {
  return calls.filter((call) => call.method === method && call.path === path);
}

/**
 * 取保存请求体里的目录字段。
 *
 * `Call.body` 是 `unknown`（替身存的是原始请求体），直接取 `.folder_id` 通不过类型检查。
 * 这里显式收窄一次，顺带把“字段缺席＝不改目录”与“显式 null＝移到未分组”区分开：
 * 缺席时返回 undefined，而不是把它当成 null。
 */
function folderIdOf(call: Call | undefined): unknown {
  if (call === undefined) return undefined;
  if (call.body === null || typeof call.body !== "object") return undefined;
  return (call.body as { folder_id?: unknown }).folder_id;
}

describe("执行固定在内容一致的已发布版本上", () => {
  beforeEach(() => {
    calls = [];
    draftHash = PUBLISHED_HASH;
    storedContent = contentKey(caseContent());
    storedFolder = null;
    caseRev = 3;
    versions = [versionRow(VERSION_ID, 1, PUBLISHED_HASH)];
    holdPatch = false;
    releasePatch = null;
    apiGetMock.mockReset();
    apiSendMock.mockReset();
    apiSendWithMetaMock.mockReset();
    apiGetMock.mockImplementation((async (path: string) => route("GET", path, undefined)) as never);
    apiSendMock.mockImplementation((async (path: string, method: string, body: unknown) =>
      route(method, path, body)) as never);
    // 保存走带 ETag 的调用：这里同样打到替身上，让“保存必要编辑”这一段是真的走通的。
    apiSendWithMetaMock.mockImplementation((async (path: string, method: string, body: unknown) => ({
      data: await route(method, path, body),
      etag: `"${caseRev}"`,
    })) as never);
  });

  it("内容没变时不新建版本，执行固定在已有的那一版上", async () => {
    // 这条用例的草稿摘要与 v1 的摘要相同：执行就该落在 v1 上。若这里仍旧发布一次，
    // 每次执行都会造出一个新版本，管理员按“固定用例版本”签发的用途授权永远对不上
    // 实际执行的那一版，而界面不会有任何异常提示。
    await renderLoaded();
    fireEvent.click(screen.getByRole("button", { name: "保存并执行" }));

    await waitFor(() =>
      expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/runs"), "POST")).toHaveLength(1),
    );
    expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, `/cases/${CASE_ID}/publish`), "POST")).toEqual([]);
    expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/runs"), "POST")[0].body).toEqual({
      environment_id: ENV_ID,
      case_version_id: VERSION_ID,
    });
  });

  it("草稿与已发布版本不一致时先发布，执行固定在新固化出来的那一版上", async () => {
    // 内容变了就必须固化出新版本再执行，否则会把改过的用例跑在旧版本上。
    draftHash = CHANGED_HASH;
    await renderLoaded();
    fireEvent.click(screen.getByRole("button", { name: "保存并执行" }));

    await waitFor(() =>
      expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/runs"), "POST")).toHaveLength(1),
    );
    expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, `/cases/${CASE_ID}/publish`), "POST")).toHaveLength(1);
    expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/runs"), "POST")[0].body).toEqual({
      environment_id: ENV_ID,
      case_version_id: NEW_VERSION_ID,
    });
  });

  it("只改目录时保存这一处编辑，但执行仍复用原本那一版，不新增版本", async () => {
    // 所属目录不属于执行快照：摘要与 v1 相同。这里要是顺手发布一次，就会固化出一条
    // 内容相同、id 不同的版本，把管理员按“固定用例版本”签发的授权换掉——执行固定到
    // 新版本上，授权却绑在旧版本上，直到执行前才暴露。
    await renderLoaded();
    fireEvent.change(screen.getByLabelText("所属目录"), { target: { value: FOLDER_A } });
    fireEvent.click(screen.getByRole("button", { name: "保存并执行" }));

    await waitFor(() =>
      expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/runs"), "POST")).toHaveLength(1),
    );
    // 必要编辑仍然按 ETag 保存了：只复用版本，不等于把目录的改动丢掉。
    const patches = callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, `/cases/${CASE_ID}`), "PATCH");
    expect(patches).toHaveLength(1);
    expect(folderIdOf(patches[0])).toBe(FOLDER_A);
    // 内容没变：不固化新版本，执行固定在原本那一版（授权绑定的就是它）。
    expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, `/cases/${CASE_ID}/publish`), "POST")).toEqual([]);
    expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/runs"), "POST")[0].body).toEqual({
      environment_id: ENV_ID,
      case_version_id: VERSION_ID,
    });
  });

  it("执行内容真的变了：仍旧固化新版本，执行固定在新版本上", async () => {
    // 与只改目录相对的另一半：请求定义变了，摘要与任何已发布版本都不同，必须发布。
    await renderLoaded();
    fireEvent.change(screen.getByLabelText("路径"), { target: { value: "/orders/42" } });
    fireEvent.click(screen.getByRole("button", { name: "保存并执行" }));

    await waitFor(() =>
      expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/runs"), "POST")).toHaveLength(1),
    );
    expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, `/cases/${CASE_ID}/publish`), "POST")).toHaveLength(1);
    expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, "/runs"), "POST")[0].body).toEqual({
      environment_id: ENV_ID,
      case_version_id: NEW_VERSION_ID,
    });
  });

  it("保存期间又改了目录：提交后的回显不覆盖用户的新选择", async () => {
    // 保存请求在飞的时候目录选择仍然可用。响应回来时若按服务端回显无条件回填，用户
    // 刚做的那次选择会被跳回去、且不留痕迹；只推进已提交的那一份基线才对。
    await renderLoaded();
    const folderSelect = () => screen.getByLabelText("所属目录") as HTMLSelectElement;

    holdPatch = true;
    fireEvent.change(folderSelect(), { target: { value: FOLDER_A } });
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(releasePatch).not.toBeNull());

    // 请求还没回来，用户改成了另一个目录。
    fireEvent.change(folderSelect(), { target: { value: FOLDER_B } });
    await act(async () => {
      releasePatch?.();
    });

    await waitFor(() =>
      expect(callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, `/cases/${CASE_ID}`), "PATCH")).toHaveLength(1),
    );
    // 提交并入库的是 A；界面上留着用户后来选的 B，而且这一处仍未保存。
    const patched = callsTo(projectPath(WORKSPACE_ID, PROJECT_ID, `/cases/${CASE_ID}`), "PATCH");
    expect(folderIdOf(patched[0])).toBe(FOLDER_A);
    expect(folderSelect().value).toBe(FOLDER_B);
    expect(screen.getByText("有未保存修改")).toBeTruthy();
  });
});
