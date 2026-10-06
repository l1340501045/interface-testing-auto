/**
 * PV1：发布完成后的版本信息同步（编辑器这一侧）。
 *
 * 主审的实际复现是：新建用例 → 保存并发布 → 页面提示“已发布版本 v1”，但版本区
 * 持续显示“尚未发布任何版本”，左侧列表仍为“未发布”，授权选择器显示“这条用例还
 * 没有已发布版本”；只读 SQL 核对确认该用例确实已有版本 1。界面与数据库对同一件
 * 事给出了相反的答案，而且等多久都不恢复——这不是提示文案的问题。
 *
 * 责任层的原因是把“发布”当成了“一次读取”：发布成功后界面靠**再读一次列表**来得知
 * 结果，而发布之前已经发出的那次读取（它读到的是还没有这一版的那一份）可以晚到，
 * 并把它读到的那一份写回去。发布是写，它确认下来的那一版必须原子地进入界面，并且
 * 能够作废一切在飞的旧读取。
 *
 * 这里的证明方式是把这个窗口**制造出来**：把版本读取挂起，使得发布之前发出的那次
 * 读取确实是在“服务端还没有任何版本”的时候发起的；先放行发布、看到界面已经显示 v1，
 * 再放行那次旧读取，断言界面不会退回未发布。
 *
 * 第二个症状是左侧列表：列表接口在**保存草稿**时拉取，而发布在保存之后，所以那次
 * 读到的永远是没有新版本的清单，左侧就停在“未发布”或落后一版。因此发布／复用确认
 * 后要显式上报一次，由外壳重新拉列表——这里断言的是上报本身。
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const WORKSPACE_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const CASE_ID = "44444444-4444-4444-8444-444444444444";
const NEW_CASE_ID = "33333333-3333-4333-8333-333333333333";
const ENV_ID = "55555555-5555-4555-8555-555555555555";
const V1 = "66666666-6666-4666-8666-666666666666";
const V2 = "77777777-7777-4777-8777-777777777777";
const ASSERTION_ID = "assert-1";
const PUBLISHED_HASH = "hash-published";
const CHANGED_HASH = "hash-after-edit";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, apiGet: vi.fn(), apiSend: vi.fn(), apiSendWithMeta: vi.fn(), apiDelete: vi.fn() };
});

import { apiGet, apiSend, apiSendWithMeta } from "../api/client";
import { CaseEditor } from "./CaseEditor";

const apiGetMock = vi.mocked(apiGet);
const apiSendMock = vi.mocked(apiSend);
const apiSendWithMetaMock = vi.mocked(apiSendWithMeta);

interface Call {
  method: string;
  path: string;
  body: unknown;
}

interface HeldVersionRead {
  /** 发起这次读取时服务端**已有的**版本，也就是这次“旧响应”的内容。 */
  snapshot: VersionRow[];
  resolve: () => void;
}

interface VersionRow {
  id: string;
  case_id: string;
  version: number;
  schema_version: number;
  side_effect: string;
  snapshot_hash: string;
  created_by: string;
  created_at: string;
}

let calls: Call[] = [];
/** 服务端当前存着的用例身份与草稿摘要。 */
let caseRow: { id: string; rev: number; hash: string } | null = null;
/** 服务端已发布的版本，按版本号倒序，与后端一致。 */
let versionRows: VersionRow[] = [];
/** 把版本读取挂起，由用例决定何时放行——用来制造“旧响应迟到”的窗口。 */
let holdVersionReads = false;
let heldVersionReads: HeldVersionRead[] = [];
/** 把发布挂起，保证旧读取确实是在“服务端还没有这一版”的时候发起的。 */
let holdPublish = false;
let releasePublish: (() => void) | null = null;
/** 编辑器上报给外壳的两件事。 */
let reportedVersions: ({ caseId: string; versionId: string | null } | null)[] = [];
let versionsChanged: string[] = [];

function route(method: string, path: string, body: unknown): unknown | Promise<unknown> {
  calls.push({ method, path, body });
  if (method === "GET" && path.endsWith("/assertion-types")) return TYPES;
  if (method === "POST" && path.endsWith("/cases")) {
    caseRow = { id: NEW_CASE_ID, rev: 1, hash: PUBLISHED_HASH };
    return detail();
  }
  if (method === "PATCH" && path.endsWith(`/cases/${CASE_ID}`)) {
    caseRow = { id: CASE_ID, rev: (caseRow?.rev ?? 0) + 1, hash: CHANGED_HASH };
    return detail();
  }
  if (method === "GET" && path.endsWith("/versions")) {
    // 读取**在发起这一刻**取快照：晚到的响应带回的就是当时的那一份，而不是现在的。
    const snapshot = [...versionRows];
    if (holdVersionReads) {
      return new Promise((resolve) => {
        const held: HeldVersionRead = {
          snapshot,
          resolve: () => {
            heldVersionReads = heldVersionReads.filter((item) => item !== held);
            resolve(snapshot);
          },
        };
        heldVersionReads = [...heldVersionReads, held];
      });
    }
    return snapshot;
  }
  if (method === "GET" && path.endsWith(`/cases/${caseRow?.id}`)) return detail();
  if (method === "POST" && path.endsWith("/publish")) {
    const next = (versionRows[0]?.version ?? 0) + 1;
    const published = versionRow(next === 1 ? V1 : V2, next, caseRow?.hash ?? PUBLISHED_HASH);
    const commit = () => {
      // 发布把当前草稿固化成新版本：此后任何**新发起**的读取都能读到它。
      versionRows = [published, ...versionRows];
      return published;
    };
    if (holdPublish) {
      return new Promise((resolve) => {
        releasePublish = () => resolve(commit());
      });
    }
    return commit();
  }
  throw new Error(`测试未覆盖的请求：${method} ${path}`);
}

/** 用例的执行快照内容；摘要的输入。 */
function caseContent() {
  return {
    name: "独立验收-请求头凭证",
    request: {
      method: "GET",
      path: "/require-header",
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

function detail() {
  if (caseRow === null) throw new Error("还没有用例");
  return {
    id: caseRow.id,
    folder_id: null,
    ...caseContent(),
    rev: caseRow.rev,
    status: "draft",
    latest_version: versionRows[0]?.version ?? 0,
    updated_at: "2026-09-14T00:00:00Z",
    snapshot_hash: caseRow.hash,
  };
}

function versionRow(id: string, version: number, snapshotHash: string): VersionRow {
  return {
    id,
    case_id: caseRow?.id ?? CASE_ID,
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

function renderEditor(caseSummaryId: string | null) {
  return render(
    <CaseEditor
      workspaceId={WORKSPACE_ID}
      projectId={PROJECT_ID}
      caseSummaryId={caseSummaryId}
      environments={[
        {
          id: ENV_ID,
          name: "环境审查备用环境",
          kind: "test",
          base_url: "http://echo.test",
          pool_id: null,
          variables: {},
          status: "active",
          rev: 1,
          config_version: 1,
        },
      ]}
      selectedEnvironmentId={ENV_ID}
      onSelectEnvironment={() => {}}
      onSaved={() => {}}
      onClose={() => {}}
      onCurrentVersion={(target) => reportedVersions.push(target)}
      onVersionsChanged={(caseId) => versionsChanged.push(caseId)}
      folders={[]}
    />,
  );
}

const PUBLISH_BUTTON = "保存并发布";

/** 把版本读取一个个放行，直到发布请求真的发出去为止。 */
async function releaseVersionReadsUntilPublish(): Promise<void> {
  while (!calls.some((call) => call.method === "POST" && call.path.endsWith("/publish"))) {
    await waitFor(() => expect(heldVersionReads.length).toBeGreaterThan(0));
    const held = heldVersionReads[0];
    await act(async () => held.resolve());
  }
}

describe("发布后的版本信息同步", () => {
  beforeEach(() => {
    calls = [];
    caseRow = null;
    versionRows = [];
    holdVersionReads = false;
    heldVersionReads = [];
    holdPublish = false;
    releasePublish = null;
    reportedVersions = [];
    versionsChanged = [];
    apiGetMock.mockReset();
    apiSendMock.mockReset();
    apiSendWithMetaMock.mockReset();
    apiGetMock.mockImplementation((async (path: string) => route("GET", path, undefined)) as never);
    apiSendMock.mockImplementation((async (path: string, method: string, body: unknown) =>
      route(method, path, body)) as never);
    apiSendWithMetaMock.mockImplementation((async (path: string, method: string, body: unknown) => ({
      data: await route(method, path, body),
      etag: `"${caseRow?.rev ?? 1}"`,
    })) as never);
  });

  it("新建用例发布成功后，无需刷新就能看到 v1 并把这一版报给授权表单", async () => {
    renderEditor(null);
    fireEvent.change(screen.getByLabelText("用例名称"), {
      target: { value: "独立验收-请求头凭证" },
    });
    fireEvent.click(screen.getByRole("button", { name: PUBLISH_BUTTON }));

    // 界面自己显示的那一份必须是 v1，而不是“尚未发布任何版本”加一句 v1 提示：
    // 只改提示文案的话，版本区与授权选择器读的仍然是空列表。
    await screen.findByText(/^v1 · 副作用/);
    expect(screen.queryByText("尚未发布任何版本。")).toBeNull();
    expect(screen.getByText("已发布版本 v1，执行将固定在该版本上。")).toBeTruthy();

    // 授权表单据此预选：拿到的是刚发布的这一版，不是 null。
    await waitFor(() =>
      expect(reportedVersions.at(-1)).toEqual({ caseId: NEW_CASE_ID, versionId: V1 }),
    );
    // 左侧列表按同一条用例收到一次刷新信号（它的那次读取发生在保存时，早于发布）。
    expect(versionsChanged).toEqual([NEW_CASE_ID]);
  });

  it("发布之前发出的那次版本读取迟到，不会把界面打回未发布", async () => {
    holdPublish = true;
    holdVersionReads = true;
    renderEditor(null);
    fireEvent.change(screen.getByLabelText("用例名称"), {
      target: { value: "独立验收-请求头凭证" },
    });
    fireEvent.click(screen.getByRole("button", { name: PUBLISH_BUTTON }));

    // 放开流程里那次读取，让它走到发布；读到的确实是空列表（服务端还没有任何版本）。
    await waitFor(() => expect(heldVersionReads.length).toBeGreaterThan(0));
    expect(heldVersionReads[0].snapshot).toEqual([]);
    await act(async () => heldVersionReads[0].resolve());
    await releaseVersionReadsUntilPublish();

    // 拿到新用例 id 后编辑器又重新读了一次版本列表；这次读取同样在发布成功**之前**
    // 发起，因此它带回来的还是空列表，并且会在发布成功之后才回来。这就是主审的窗口。
    await waitFor(() => expect(heldVersionReads.length).toBeGreaterThan(0));
    expect(heldVersionReads.every((item) => item.snapshot.length === 0)).toBe(true);

    await act(async () => releasePublish?.());
    await screen.findByText(/^v1 · 副作用/);
    await waitFor(() =>
      expect(reportedVersions.at(-1)).toEqual({ caseId: NEW_CASE_ID, versionId: V1 }),
    );

    // 旧响应此刻到达。它不能覆盖发布确认下来的那一版。
    const stale = [...heldVersionReads];
    await act(async () => stale.forEach((item) => item.resolve()));
    expect(screen.getByText(/^v1 · 副作用/)).toBeTruthy();
    expect(screen.queryByText("尚未发布任何版本。")).toBeNull();
    expect(reportedVersions.at(-1)).toEqual({ caseId: NEW_CASE_ID, versionId: V1 });
  });

  it("编辑已有用例发布下一版，版本区前进到 v2 且旧版本仍在", async () => {
    caseRow = { id: CASE_ID, rev: 3, hash: PUBLISHED_HASH };
    versionRows = [versionRow(V1, 1, PUBLISHED_HASH)];
    renderEditor(CASE_ID);
    await screen.findByText(/^v1 · 副作用/);

    // 改名称＝执行快照内容变了，必须固化新版本（只改目录才复用）。
    fireEvent.change(screen.getByLabelText("用例名称"), { target: { value: "独立验收-请求头凭证（改）" } });
    fireEvent.click(screen.getByRole("button", { name: PUBLISH_BUTTON }));

    await screen.findByText(/^v2 · 副作用/);
    expect(screen.getByText("已发布版本 v2，执行将固定在该版本上。")).toBeTruthy();
    // v1 仍在列表里：发布不是“只剩最新一版”的替换。
    expect(screen.getByText(/^v1 · 副作用/)).toBeTruthy();
    await waitFor(() =>
      expect(reportedVersions.at(-1)).toEqual({ caseId: CASE_ID, versionId: V2 }),
    );
    expect(versionsChanged).toEqual([CASE_ID]);
  });

  it("复用内容一致的已发布版本时同样上报，左侧列表不会滞后", async () => {
    caseRow = { id: CASE_ID, rev: 3, hash: PUBLISHED_HASH };
    versionRows = [versionRow(V1, 1, PUBLISHED_HASH)];
    renderEditor(CASE_ID);
    await screen.findByText(/^v1 · 副作用/);

    // 内容没变：不新增版本，复用 v1。复用同样是服务端确认的一版，也要上报——
    // 否则左侧在“只改目录再保存”这条路径上依旧落后一版。
    fireEvent.click(screen.getByRole("button", { name: PUBLISH_BUTTON }));

    await screen.findByText("已保存；执行复用内容一致的已发布版本 v1，未新增版本。");
    expect(calls.filter((call) => call.method === "POST" && call.path.endsWith("/publish"))).toHaveLength(0);
    expect(versionsChanged).toEqual([CASE_ID]);
    expect(reportedVersions.at(-1)).toEqual({ caseId: CASE_ID, versionId: V1 });
  });

  it("读不到版本列表时不发布，也不上报一条并不存在的版本变化", async () => {
    caseRow = { id: CASE_ID, rev: 3, hash: CHANGED_HASH };
    versionRows = [versionRow(V1, 1, PUBLISHED_HASH)];
    renderEditor(CASE_ID);
    await screen.findByText(/^v1 · 副作用/);

    // 读取失败：不能静默当成“没有版本可复用”而多发一版。这里让那一次失败发生在
    // 点击之后——读取一旦失败，既不能发布，也不能对外声称版本变了。
    apiSendMock.mockImplementation((async (path: string, method: string, body: unknown) => {
      if (method === "POST" && path.endsWith("/publish")) throw new Error("不该走到发布");
      if (method === "GET" && path.endsWith("/versions")) throw new Error("版本列表暂时读不到");
      return route(method, path, body);
    }) as never);

    fireEvent.click(screen.getByRole("button", { name: PUBLISH_BUTTON }));
    await screen.findByText("版本列表读取失败，未提交执行");
    expect(calls.filter((call) => call.method === "POST" && call.path.endsWith("/publish"))).toHaveLength(0);
    expect(versionsChanged).toEqual([]);
  });
});
