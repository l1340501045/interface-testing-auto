import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { antSelectedValue, selectAntOption } from "./test/antd";

const WS = "11111111-1111-4111-8111-111111111111";
const A = "22222222-2222-4222-8222-222222222222";
const B = "33333333-3333-4333-8333-333333333333";
const CASE = "44444444-4444-4444-8444-444444444444";
const ENV = "55555555-5555-4555-8555-555555555555";
const VERSION = "66666666-6666-4666-8666-666666666666";
const RUN = "77777777-7777-4777-8777-777777777777";

vi.mock("./session/useSession", () => ({
  useSession: () => ({
    session: { user: { user_id: "u1", username: "tester", display_name: "测试员", is_admin: true }, workspaces: [{ id: WS, name: "工作空间", role: "admin" }] },
    loading: false,
    error: null,
    expired: false,
    login: vi.fn(),
    logout: vi.fn(),
  }),
}));

vi.mock("./api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api/client")>();
  return { ...actual, apiGet: vi.fn(), apiSend: vi.fn(), apiSendWithMeta: vi.fn(), apiDelete: vi.fn() };
});

import { NetworkError, apiGet, apiSend, apiSendWithMeta } from "./api/client";
import { App } from "./App";

const calls: { path: string; method: string; body: unknown; headers?: Record<string, string> }[] = [];
let runAttempts = 0;

const request = { method: "GET", path: "/echo", query_params: [], headers: [], body_type: "none", body: "" };

function raw(path: string, method: string, body: unknown, headers?: Record<string, string>): unknown {
  calls.push({ path, method, body, headers });
  if (path === `/workspaces/${WS}/projects`) return [
    { id: A, workspace_id: WS, key: "a", name: "项目甲", status: "active", role: "admin", pool_id: null },
    { id: B, workspace_id: WS, key: "b", name: "项目乙", status: "active", role: "admin", pool_id: null },
  ];
  if (path.endsWith("/environments")) return [{ id: ENV, name: "环境", kind: "test", base_url: "http://echo.test", pool_id: null, variables: {}, status: "active" }];
  if (path.endsWith("/folders") || path.endsWith("/assertion-types")) return [];
  if (path.endsWith("/cases")) return path.includes(`/projects/${A}`) ? [{ id: CASE, folder_id: null, name: "unknown 用例", method: "GET", status: "draft", rev: 1, latest_version: 1 }] : [];
  if (path.endsWith(`/cases/${CASE}`)) return { id: CASE, folder_id: null, name: "unknown 用例", request, assertions: [], rev: 1, status: "draft", latest_version: 1, updated_at: "2026-09-27T00:00:00Z", snapshot_hash: "same" };
  if (path.endsWith(`/cases/${CASE}/versions`)) return [{ id: VERSION, case_id: CASE, version: 1, schema_version: 1, side_effect: "unknown", snapshot_hash: "same", created_by: "u1", created_at: "2026-09-27T00:00:00Z" }];
  if (method === "POST" && path.endsWith("/runs")) {
    runAttempts += 1;
    if (runAttempts === 1) throw new NetworkError("断线");
    return { id: RUN };
  }
  if (path.includes("/runs")) return [];
  if (method === "POST" && path.endsWith("/debug-preflight")) return { ready: true, issues: [], can_authorize: false, auth: { required: false, state: "none", profile_id: null }, context: null };
  if (path.endsWith("/variables") || path.endsWith("/runner-pools") || path.endsWith("/credentials/secrets") || path.endsWith("/credentials/profiles") || path.endsWith("/credentials/grants") || path.endsWith("/members")) return [];
  throw new Error(`未覆盖请求：${method} ${path}`);
}

beforeEach(() => {
  calls.length = 0;
  runAttempts = 0;
  vi.mocked(apiGet).mockReset().mockImplementation((async (path: string, parse: (raw: unknown) => unknown) => parse(raw(path, "GET", undefined))) as never);
  vi.mocked(apiSend).mockReset().mockImplementation((async (path: string, method: string, body: unknown, parse: (raw: unknown) => unknown, options?: { headers?: Record<string, string> }) => parse(raw(path, method, body, options?.headers))) as never);
  vi.mocked(apiSendWithMeta).mockReset();
});

describe("unknown 的普通范围保护", () => {
  it("普通切项目不能丢 unknown，确认仍使用原 body 与键", async () => {
    render(<App />);
    await screen.findByLabelText("项目");
    await waitFor(() => expect(antSelectedValue("项目")).toBe(A));
    await act(async () => {});
    const browser = screen.getByRole("complementary", { name: "用例目录" });
    fireEvent.click(await within(browser).findByRole("button", { name: /unknown 用例/ }));
    await screen.findByDisplayValue("unknown 用例");
    fireEvent.click(screen.getByText("版本执行与发布记录"));
    fireEvent.click(screen.getByRole("button", { name: "保存并执行" }));
    const confirm = await screen.findByRole("button", { name: "确认原操作" });

    await selectAntOption("项目", "项目乙");
    expect(antSelectedValue("项目")).toBe(A);
    expect(screen.getByText(/普通切换不能丢弃/)).toBeTruthy();
    expect(confirm).toBeTruthy();

    fireEvent.click(confirm);
    await waitFor(() => expect(runAttempts).toBe(2));
    const posts = calls.filter((item) => item.method === "POST" && item.path.endsWith("/runs"));
    expect(posts[1].body).toEqual(posts[0].body);
    expect(posts[1].headers?.["Idempotency-Key"]).toBe(posts[0].headers?.["Idempotency-Key"]);
  });
});
