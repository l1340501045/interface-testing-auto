import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, apiSend: vi.fn() };
});

vi.mock("./useRuns", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./useRuns")>();
  return {
    ...actual,
    useRuns: () => ({ data: [], error: null, loading: false, reload: vi.fn() }),
    useRunReport: () => ({ data: null, error: null, loading: false, reload: vi.fn() }),
  };
});

import { ApiError, NetworkError, apiSend } from "../api/client";
import { RunPanel } from "./RunPanel";

const version = {
  id: "66666666-6666-4666-8666-666666666666",
  case_id: "44444444-4444-4444-8444-444444444444",
  version: 2,
  schema_version: 2,
  side_effect: "unknown",
  snapshot_hash: "hash",
  created_by: "u1",
  created_at: "2026-09-27T00:00:00Z",
};

const preview = {
  schema_version: 1,
  scope: { workspace_id: "w1", project_id: "p1", environment_id: "e1" },
  ready: true,
  ordinary_resolution: "ready",
  masked_target: { url: "http://echo.test/version", method: "GET" },
  bindings: [], issues: [],
  auth: { required: false, status: "none", injection_slots: [], requires_worker_verification: false },
  config_basis: { project_variables_version: 1, project_config_version_id: null, environment_rev: 1, environment_config_version: 1, environment_config_version_id: "cfg-1" },
  context_fingerprint: "version-context-fingerprint",
  resolution_context: "version-resolution-context",
};
let runResponses: unknown[] = [];
function setRunResponses(...responses: unknown[]) { runResponses = responses; }

describe("固定版本运行受理确认", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    runResponses = [];
    vi.mocked(apiSend).mockImplementation((async (path: string, _method: string, _body: unknown, parse: (raw: unknown) => unknown) => {
      if (path.endsWith("/resolution-preview")) return parse(preview);
      const next = runResponses.shift();
      if (next instanceof Error) throw next;
      return parse(next);
    }) as never);
  });

  it("网络结果未知时用同一 payload 和 Idempotency-Key 确认，不重新发布", async () => {
    setRunResponses(new NetworkError("断线"), { id: "77777777-7777-4777-8777-777777777777" });
    const ensure = vi.fn();
    const submitted = vi.fn();
    render(
      <RunPanel
        workspaceId="w1"
        projectId="p1"
        environmentId="e1"
        caseId={version.case_id}
        needsVersion={false}
        publishedVersion={version}
        onEnsureVersion={ensure}
        onReport={vi.fn()}
        selectedRunId={null}
        onSelectRun={vi.fn()}
        onRunSubmitted={submitted}
        captureProvenance={() => ({ environmentId: "e1", configEpoch: 3 })}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "保存并执行" }));
    const confirm = await screen.findByRole("button", { name: "确认原操作" });
    fireEvent.click(confirm);
    await waitFor(() => expect(submitted).toHaveBeenCalledTimes(1));

    expect(ensure).not.toHaveBeenCalled();
    expect(apiSend).toHaveBeenCalledTimes(3);
    const previewCall = vi.mocked(apiSend).mock.calls[0]!;
    const first = vi.mocked(apiSend).mock.calls[1];
    const second = vi.mocked(apiSend).mock.calls[2];
    expect(previewCall[0]).toContain("/resolution-preview");
    expect(previewCall[2]).toEqual({ environment_id: "e1", case_version_id: version.id });
    expect(first[2]).toEqual({ environment_id: "e1", case_version_id: version.id, resolution_context: "version-resolution-context" });
    expect(second[2]).toEqual(first[2]);
    expect(second[4]?.headers?.["Idempotency-Key"]).toBe(first[4]?.headers?.["Idempotency-Key"]);
  });

  it("成功信封缺少 id 时保留原操作，确认仍使用原 body 与键", async () => {
    setRunResponses({}, { id: "77777777-7777-4777-8777-777777777777" });
    render(
      <RunPanel workspaceId="w1" projectId="p1" environmentId="e1" caseId={version.case_id} needsVersion={false} publishedVersion={version} onEnsureVersion={vi.fn()} onReport={vi.fn()} selectedRunId={null} onSelectRun={vi.fn()} onRunSubmitted={vi.fn()} captureProvenance={() => ({ environmentId: "e1", configEpoch: 3 })} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "保存并执行" }));
    const confirm = await screen.findByRole("button", { name: "确认原操作" });
    expect(screen.queryByText(/^已提交运行/)).toBeNull();
    fireEvent.click(confirm);
    await waitFor(() => expect(apiSend).toHaveBeenCalledTimes(3));
    const first = vi.mocked(apiSend).mock.calls[1]!;
    const second = vi.mocked(apiSend).mock.calls[2]!;
    expect(second[2]).toEqual(first[2]);
    expect(second[4]?.headers?.["Idempotency-Key"]).toBe(first[4]?.headers?.["Idempotency-Key"]);
  });

  it("确认阶段 403 仍保持 unknown 与原键", async () => {
    setRunResponses(new NetworkError("断线"), new ApiError(403, "forbidden", "无权确认", null));
    render(
      <RunPanel workspaceId="w1" projectId="p1" environmentId="e1" caseId={version.case_id} needsVersion={false} publishedVersion={version} onEnsureVersion={vi.fn()} onReport={vi.fn()} selectedRunId={null} onSelectRun={vi.fn()} onRunSubmitted={vi.fn()} captureProvenance={() => ({ environmentId: "e1", configEpoch: 3 })} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "保存并执行" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认原操作" }));
    await waitFor(() => expect(apiSend).toHaveBeenCalledTimes(3));
    expect(screen.getByRole("button", { name: "确认原操作" })).toBeTruthy();
    const first = vi.mocked(apiSend).mock.calls[1]!;
    const second = vi.mocked(apiSend).mock.calls[2]!;
    expect(second[4]?.headers?.["Idempotency-Key"]).toBe(first[4]?.headers?.["Idempotency-Key"]);
  });

  it("首次明确 target_not_allowed 显示原错误并释放，可纠正后开始新操作", async () => {
    setRunResponses(new ApiError(400, "target_not_allowed", "目标不在允许范围", null), { id: "77777777-7777-4777-8777-777777777777" });
    const acquire = vi.fn(() => true);
    const release = vi.fn();
    render(
      <RunPanel workspaceId="w1" projectId="p1" environmentId="e1" caseId={version.case_id} needsVersion={false} publishedVersion={version} onEnsureVersion={vi.fn()} onReport={vi.fn()} selectedRunId={null} onSelectRun={vi.fn()} onRunSubmitted={vi.fn()} captureProvenance={() => ({ environmentId: "e1", configEpoch: 3 })} tryAcquireOperation={acquire} releaseOperation={release} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "保存并执行" }));
    expect(await screen.findByText("目标不在允许范围")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "确认原操作" })).toBeNull();
    await waitFor(() => expect(release).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: "保存并执行" }));
    await waitFor(() => expect(apiSend).toHaveBeenCalledTimes(4));
    expect(acquire).toHaveBeenCalledTimes(2);
  });

  it.each([
    [400, "case_invalid", "用例配置无效"],
    [403, "production_blocked", "生产环境禁止执行"],
    [404, "not_found", "环境或用例版本不存在"],
  ])("首次明确准入拒绝 %s/%s 释放操作", async (status, code, message) => {
    setRunResponses(new ApiError(status, code, message, null));
    const release = vi.fn();
    render(
      <RunPanel workspaceId="w1" projectId="p1" environmentId="e1" caseId={version.case_id} needsVersion={false} publishedVersion={version} onEnsureVersion={vi.fn()} onReport={vi.fn()} selectedRunId={null} onSelectRun={vi.fn()} onRunSubmitted={vi.fn()} captureProvenance={() => ({ environmentId: "e1", configEpoch: 3 })} tryAcquireOperation={() => true} releaseOperation={release} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "保存并执行" }));
    expect(await screen.findByText(message)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "确认原操作" })).toBeNull();
    await waitFor(() => expect(release).toHaveBeenCalledTimes(1));
  });

  it.each([
    [500, "case_invalid"],
    [503, "target_not_allowed"],
  ])("同码非可信状态 %s/%s 仍保持 unknown", async (status, code) => {
    setRunResponses(new ApiError(status, code, "服务暂时不可用", null));
    render(
      <RunPanel workspaceId="w1" projectId="p1" environmentId="e1" caseId={version.case_id} needsVersion={false} publishedVersion={version} onEnsureVersion={vi.fn()} onReport={vi.fn()} selectedRunId={null} onSelectRun={vi.fn()} onRunSubmitted={vi.fn()} captureProvenance={() => ({ environmentId: "e1", configEpoch: 3 })} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "保存并执行" }));
    expect(await screen.findByRole("button", { name: "确认原操作" })).toBeTruthy();
    expect(screen.getByText(/服务端是否已受理暂时未知/)).toBeTruthy();
  });

  it("首次断网后确认遇到 case_invalid 仍保持原 unknown", async () => {
    setRunResponses(new NetworkError("断线"), new ApiError(400, "case_invalid", "当前确认被拒绝", null));
    render(
      <RunPanel workspaceId="w1" projectId="p1" environmentId="e1" caseId={version.case_id} needsVersion={false} publishedVersion={version} onEnsureVersion={vi.fn()} onReport={vi.fn()} selectedRunId={null} onSelectRun={vi.fn()} onRunSubmitted={vi.fn()} captureProvenance={() => ({ environmentId: "e1", configEpoch: 3 })} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "保存并执行" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认原操作" }));
    await waitFor(() => expect(apiSend).toHaveBeenCalledTimes(3));
    expect(screen.getByRole("button", { name: "确认原操作" })).toBeTruthy();
    const first = vi.mocked(apiSend).mock.calls[1]!;
    const second = vi.mocked(apiSend).mock.calls[2]!;
    expect(second[2]).toEqual(first[2]);
    expect(second[4]?.headers?.["Idempotency-Key"]).toBe(first[4]?.headers?.["Idempotency-Key"]);
  });

  it("版本准备期间配置变化时保留准备成果但不提交运行", async () => {
    let resolveVersion!: (value: typeof version) => void;
    const versionPromise = new Promise<typeof version>((resolve) => { resolveVersion = resolve; });
    let epoch = 3;
    render(
      <RunPanel workspaceId="w1" projectId="p1" environmentId="e1" caseId={version.case_id} needsVersion publishedVersion={null} onEnsureVersion={() => versionPromise} onReport={vi.fn()} selectedRunId={null} onSelectRun={vi.fn()} onRunSubmitted={vi.fn()} captureProvenance={() => ({ environmentId: "e1", configEpoch: epoch })} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "保存并执行" }));
    epoch = 4;
    resolveVersion(version);
    await screen.findByText(/配置已变化/);
    expect(apiSend).not.toHaveBeenCalled();
  });

  it("初始依据失效不会占住共享 gate", async () => {
    const acquire = vi.fn(() => true);
    const release = vi.fn();
    render(
      <RunPanel workspaceId="w1" projectId="p1" environmentId="e1" caseId={version.case_id} needsVersion={false} publishedVersion={version} onEnsureVersion={vi.fn()} onReport={vi.fn()} selectedRunId={null} onSelectRun={vi.fn()} onRunSubmitted={vi.fn()} captureProvenance={() => null} tryAcquireOperation={acquire} releaseOperation={release} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "保存并执行" }));
    expect(acquire).not.toHaveBeenCalled();
    expect(release).not.toHaveBeenCalled();
    expect(apiSend).not.toHaveBeenCalled();
  });
});
