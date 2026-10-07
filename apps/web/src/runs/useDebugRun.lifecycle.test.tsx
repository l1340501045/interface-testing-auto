/**
 * 发送生命周期的真实 Hook 级回归（R3 §2、§6）。
 *
 * 直接在真实 `useDebugRun` 上驱动状态，不走编辑器界面：这些边界（排队期间的锁、受理
 * 不明在配置变化后的存续、停止等待对在飞尝试的影响、主体变化）在组件层会被别的异步
 * 步骤掩盖，而它们本身是执行契约的一部分。
 *
 * 网络用替身；React Hook 真实运行。
 */
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const WORKSPACE_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const ENV_ID = "55555555-5555-4555-8555-555555555555";
const RUN_1 = "77777777-7777-4777-8777-777777777771";
const RUN_2 = "77777777-7777-4777-8777-777777777772";
const USER_ID = "99999999-9999-4999-8999-999999999999";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, apiGet: vi.fn(), apiSend: vi.fn(), apiSendWithMeta: vi.fn(), apiDelete: vi.fn() };
});

import { ApiError, NetworkError, apiSend, projectPath } from "../api/client";
import { submissionKey, useDebugRun } from "./useDebugRun";

const apiSendMock = vi.mocked(apiSend);

interface Call {
  method: string;
  path: string;
  headers?: Record<string, string> | undefined;
  body: unknown;
}

let calls: Call[] = [];
const gates = new Map<string, Array<{ resolve: (v: unknown) => void; reject: (c: unknown) => void }>>();

function hold(name: string): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const bucket = gates.get(name) ?? [];
    bucket.push({ resolve, reject });
    gates.set(name, bucket);
  });
}
function release(name: string, value: unknown): void {
  const bucket = gates.get(name) ?? [];
  gates.set(name, []);
  gates.delete(name);
  for (const item of bucket) item.resolve(value);
}
function fail(name: string, cause: unknown): void {
  const bucket = gates.get(name) ?? [];
  gates.set(name, []);
  gates.delete(name);
  for (const item of bucket) item.reject(cause);
}
let gated = new Set<string>();
function maybeGate(name: string, value: unknown): unknown {
  return gated.has(name) ? hold(name) : value;
}

const REQUEST = {
  method: "GET",
  path: "/echo",
  query_params: [],
  headers: [],
  body_type: "none" as const,
  body: "",
};
function submission(environmentId = ENV_ID) {
  return { environmentId, request: REQUEST, assertions: [] };
}

const RESOLUTION = {
  schema_version: 1 as const,
  scope: { workspace_id: WORKSPACE_ID, project_id: PROJECT_ID, environment_id: ENV_ID },
  ready: true,
  ordinary_resolution: "ready" as const,
  masked_target: { url: "http://echo.test/echo", method: "GET" },
  bindings: [],
  issues: [],
  auth: { required: false, status: "none" as const, injection_slots: [], requires_worker_verification: false },
  config_basis: { project_variables_version: 1, project_config_version_id: null, environment_rev: 1, environment_config_version: 1, environment_config_version_id: "config-1" },
  context_fingerprint: "context-fingerprint-1",
  resolution_context: "resolution-context-1",
};

const NEEDS_AUTH = {
  ready: false,
  issues: [{ code: "credential_not_granted", message: "当前身份未获授权。", action: "authorize" }],
  can_authorize: true,
  auth: { required: false, state: "needs_authorization", profile_id: "88888888-8888-4888-8888-888888888888" },
  context: null,
  resolution: { ...RESOLUTION, ready: false, auth: { ...RESOLUTION.auth, status: "needs_authorization" as const } },
};

const READY = {
  ready: true,
  issues: [],
  can_authorize: true,
  auth: { required: false, state: "none", profile_id: null },
  context: {
    snapshot_fingerprint: "fp-1",
    environment: { id: ENV_ID, name: "测试环境", kind: "test", base_url: "http://echo.test" },
    input_fingerprint: "in-1",
  },
  resolution: RESOLUTION,
};

function readyWith(fingerprint: string, token: string) {
  return {
    ...READY,
    resolution: { ...RESOLUTION, context_fingerprint: fingerprint, resolution_context: token },
  };
}

function namedNeedsAuth(serviceName = "订单服务") {
  const serviceKey = "svc_11111111111111111111111111111111";
  const selectedTarget = {
    kind: "service" as const, service_id: "service-orders", service_key: serviceKey, service_name: serviceName,
    service_rev: 2, service_status: "active" as const, availability: "ready" as const,
    mapping: { id: "mapping-orders", rev: 3, status: "active" as const, base_url: "http://orders", version: 3, version_id: "mapping-version-orders" },
  };
  return {
    ...NEEDS_AUTH,
    resolution: {
      ...NEEDS_AUTH.resolution,
      schema_version: 2 as const,
      selected_target: selectedTarget,
      target_ref: {
        kind: "service" as const, service_id: "service-orders", service_key: serviceKey, service_rev: 2,
        mapping_id: "mapping-orders", mapping_rev: 3, mapping_version_id: "mapping-version-orders", mapping_version: 3, environment_id: ENV_ID,
      },
    },
  };
}

function runSummary(id: string, state = "queued") {
  return {
    id,
    target_type: "debug_snapshot",
    case_version_id: null,
    environment_id: ENV_ID,
    state,
    outcome: null,
    reason_category: null,
    pool_id: null,
    created_at: "2026-09-15T00:00:00Z",
  };
}

function report(id: string, state = "finished", outcome: string | null = "passed") {
  return {
    run: { ...runSummary(id, state), outcome },
    steps: [],
    assertions: [],
    request: null,
    response: null,
    context: null,
  };
}

let runCount = 0;
/** 预检是否要求授权。 */
let needsAuth = false;
let preflightOverride: unknown = null;
let preflightSequence: unknown[] = [];

function route(method: string, path: string, body: unknown, headers?: Record<string, string>): unknown {
  calls.push({ method, path, headers, body });
  if (method === "POST" && path.endsWith("/debug-preflight")) {
    const next = preflightSequence.length > 0 ? preflightSequence.shift() : preflightOverride ?? (needsAuth ? NEEDS_AUTH : READY);
    return maybeGate("preflight", next);
  }
  if (method === "POST" && path.endsWith("/debug-snapshot-digest")) {
    return maybeGate("digest", "digest-1");
  }
  if (method === "POST" && path.endsWith("/credentials/grants")) {
    return maybeGate("grant", { id: "grant-1" });
  }
  if (method === "POST" && path.endsWith("/runs")) {
    runCount += 1;
    return maybeGate("runs", runSummary(runCount === 1 ? RUN_1 : RUN_2));
  }
  if (method === "GET" && path.endsWith("/report")) {
    const runId = path.split("/runs/")[1]?.split("/")[0] ?? "";
    return maybeGate(`report:${runId}`, report(runId));
  }
  if (method === "POST" && path.endsWith("/cancel")) return { id: "x" };
  throw new Error(`测试未覆盖的请求：${method} ${path}`);
}

function runs(): Call[] {
  return calls.filter((c) => c.method === "POST" && c.path === projectPath(WORKSPACE_ID, PROJECT_ID, "/runs"));
}
/** 同步配置时钟：模拟“时钟已变、React props 尚未更新”的窗口。 */
const epochClock: { current: number | null } = { current: 0 };

function preflightCalls(): Call[] {
  return calls.filter((c) => c.method === "POST" && c.path.endsWith("/debug-preflight"));
}
function digests(): Call[] {
  return calls.filter((c) => c.method === "POST" && c.path.endsWith("/debug-snapshot-digest"));
}
function grants(): Call[] {
  return calls.filter((c) => c.method === "POST" && c.path.endsWith("/credentials/grants"));
}
function keys(): (string | undefined)[] {
  return runs().map((c) => c.headers?.["Idempotency-Key"]);
}

interface MountProps {
  epoch: number;
  principal: string;
  /** 当前内容键：切换环境或改请求都会换它。 */
  inputKey: string;
  editorKey?: string;
  sourceCaseId?: string;
}

function mount(principalId = USER_ID, configEpoch = 0) {
  return renderHook(
    ({ epoch, principal, inputKey, editorKey, sourceCaseId }: MountProps) => {
      // 真实 App 里这是同一个时钟：props 渲染时会读到当前值。测试需要单独制造
      // “时钟已变、props 仍旧”的窗口，因此时钟放在外面，由用例自行推进。
      if (epochClock.current !== null && epoch === epochClock.current + 1) epochClock.current = epoch;
      return useDebugRun(
        WORKSPACE_ID,
        PROJECT_ID,
        editorKey ?? "instance-1",
        epoch,
        principal,
        inputKey,
        undefined,
        () => epochClock.current,
        sourceCaseId,
      );
    },
    {
      initialProps: {
        epoch: configEpoch,
        principal: principalId,
        inputKey: submissionKey(submission()),
        editorKey: "instance-1",
        sourceCaseId: undefined as string | undefined,
      },
    },
  );
}

beforeEach(() => {
  calls = [];
  gates.clear();
  gated = new Set();
  runCount = 0;
  needsAuth = false;
  preflightOverride = null;
  preflightSequence = [];
  epochClock.current = 0;
  apiSendMock.mockReset();
  apiSendMock.mockImplementation((async (
    path: string,
    method: string,
    body: unknown,
    _parse: unknown,
    options?: { headers?: Record<string, string> },
  ) => route(method, path, body, options?.headers)) as never);
});

describe("R3-02 同帧二次确认授权", () => {
  it("真实 needs_authorization 组合普通解析有效、总ready为false且token已冻结", () => {
    expect(NEEDS_AUTH).toMatchObject({ ready: false, auth: { state: "needs_authorization" } });
    expect(NEEDS_AUTH.resolution).toMatchObject({ ready: false, ordinary_resolution: "ready", resolution_context: "resolution-context-1", auth: { status: "needs_authorization" } });
  });
  it("同一帧里确认两次：只发一次摘要、一次授权、一次受理", async () => {
    // 主审复现的问题是：确认入口没有在第一个 await 之前完成阶段迁移，同帧的第二次调用
    // 会各自走完一遍，于是签发两份授权、提交两次运行。
    needsAuth = true;
    const { result } = mount();
    await act(async () => {
      await result.current.start(submission(), "测试环境");
    });
    expect(result.current.phase).toBe("awaiting_authorization");
    expect(grants()).toHaveLength(0);

    // 同一次 act：两次确认落在同一帧里。
    await act(async () => {
      void result.current.confirmAuthorization();
      void result.current.confirmAuthorization();
    });

    await waitFor(() => expect(runs()).toHaveLength(1));
    expect(digests()).toHaveLength(1);
    expect(grants()).toHaveLength(1);
    expect(runs()).toHaveLength(1);
    expect(runs()[0]?.body).toMatchObject({ resolution_context: "resolution-context-1" });
  });

  it("named授权确认显示本次发送预检冻结的服务名，后续展示结果不能换名", async () => {
    const serviceKey = "svc_11111111111111111111111111111111";
    const named = { ...submission(), request: { ...REQUEST, service_contract: 1 as const, service_key: serviceKey } };
    preflightSequence = [namedNeedsAuth("订单服务"), namedNeedsAuth("后来改名的服务")];
    const { result, rerender } = mount();
    rerender({ epoch: 0, principal: USER_ID, inputKey: submissionKey(named), editorKey: "instance-1", sourceCaseId: undefined });

    await act(async () => { await result.current.start(named, "测试环境"); });
    expect(result.current.phase).toBe("awaiting_authorization");
    expect(result.current.authorizationView).toMatchObject({ serviceName: "订单服务", serviceKey });

    await act(async () => { await result.current.runPreflight(named); });
    expect(result.current.preflight?.resolution?.schema_version).toBe(2);
    expect(result.current.authorizationView).toMatchObject({ serviceName: "订单服务", serviceKey });
    expect(grants()).toEqual([]);
    expect(runs()).toEqual([]);
  });

  it("授权请求挂起期间再次确认不新增授权，也不新增运行", async () => {
    needsAuth = true;
    gated.add("grant");
    const { result } = mount();
    await act(async () => {
      await result.current.start(submission(), "测试环境");
    });
    act(() => {
      void result.current.confirmAuthorization();
    });
    await waitFor(() => expect(grants()).toHaveLength(1));
    expect(result.current.phase).toBe("authorizing");

    // 第二次确认：阶段已经不是 awaiting_authorization，直接返回。
    await act(async () => {
      await result.current.confirmAuthorization();
    });
    expect(digests()).toHaveLength(1);
    expect(grants()).toHaveLength(1);
    expect(runs()).toHaveLength(0);

    await act(async () => {
      release("grant", { id: "grant-1" });
    });
    await waitFor(() => expect(runs()).toHaveLength(1));
    expect(digests()).toHaveLength(1);
    expect(grants()).toHaveLength(1);
  });

  it("等待授权时 owner 变化，旧确认不能签发或提交", async () => {
    needsAuth = true;
    const { result, rerender } = mount();
    await act(async () => result.current.start(submission(), "测试环境"));
    expect(result.current.phase).toBe("awaiting_authorization");
    rerender({ epoch: 0, principal: "other-user", inputKey: submissionKey(submission()), editorKey: "instance-1", sourceCaseId: undefined });
    await act(async () => {});
    expect(result.current.phase).toBe("idle");
    await act(async () => { await result.current.confirmAuthorization(); });
    expect(grants()).toEqual([]);
    expect(runs()).toEqual([]);
  });
});

describe("R4 F4 展示预检不得抢占发送检查", () => {
  it("展示预检穿插在发送检查之间：不得让发送检查过期后直接发送", async () => {
    // 复现的缺陷：展示预检与发送检查共用世代／取消控制器。用户在发送途中的一次普通编辑
    // 触发展示预检，把发送检查判成过期并返回 null，发送流程把 null 当成“读取失败、可以
    // 直发”，于是跳过授权确认建了运行——两次检查都需要授权，却零授权、零确认。
    needsAuth = true;
    gated.add("preflight");
    const { result } = mount();

    // 点击发送：操作预检发出（第 1 个挂起的 preflight）。
    act(() => {
      void result.current.start(submission(), "测试环境");
    });
    await waitFor(() => expect(preflightCalls()).toHaveLength(1));

    // 展示预检穿插进来（第 2 个挂起的 preflight）。
    act(() => {
      void result.current.runPreflight(submission());
    });
    await waitFor(() => expect(preflightCalls()).toHaveLength(2));

    // 两次预检都返回“需要授权”。
    await act(async () => {
      release("preflight", NEEDS_AUTH);
    });
    await act(async () => {});

    // 必须停在确认阶段：零运行、零授权，且出现确认入口。
    expect(runs()).toEqual([]);
    expect(grants()).toEqual([]);
    await waitFor(() => expect(result.current.phase).toBe("awaiting_authorization"));
    expect(result.current.authorizationView).not.toBeNull();
  });

  it("发送检查读失败时不提交运行", async () => {
    gated.add("preflight");
    const { result } = mount();
    act(() => {
      void result.current.start(submission(), "测试环境");
    });
    await waitFor(() => expect(preflightCalls()).toHaveLength(1));

    await act(async () => {
      const bucket = gates.get("preflight") ?? [];
      gates.set("preflight", []);
      gated.delete("preflight");
      for (const item of bucket) item.reject(new Error("检查服务不可用"));
    });
    await act(async () => {});

    // 没有得到可用结论 → 不发送。
    expect(runs()).toEqual([]);
    expect(result.current.phase).toBe("idle");
  });
});

describe("R4 F5 签发前复核同步配置", () => {
  it("同步时钟已变、props 仍旧时：摘要返回后不签发授权、不提交", async () => {
    // 摘要返回与签发之间隔着一次网络往返。若只比 token／owner，配置在等待期间被改掉
    // （管理面板刚保存成功，props 还没更新）仍会按旧配置签发，授权绑定的是一份已被
    // 改掉的输入。
    needsAuth = true;
    gated.add("digest");
    const { result } = mount();
    await act(async () => {
      await result.current.start(submission(), "测试环境");
    });
    expect(result.current.phase).toBe("awaiting_authorization");

    act(() => {
      void result.current.confirmAuthorization();
    });
    await waitFor(() => expect(digests()).toHaveLength(1));

    // 配置时钟推进，但**不**重新渲染（props 里的 epoch 仍是旧值）。
    epochClock.current = 1;
    await act(async () => {
      release("digest", "digest-1");
    });
    await act(async () => {});

    expect(grants()).toEqual([]);
    expect(runs()).toEqual([]);
    expect(result.current.phase).toBe("idle");
  });
});

describe("R4 F2 确认受理失败不证明未受理", () => {
  it("无来源受理不明后首次保存，原键确认仍保持source_case_id缺席", async () => {
    gated.add("runs");
    const { result, rerender } = mount();
    act(() => { void result.current.start(submission(), "测试环境"); });
    await waitFor(() => expect(runs()).toHaveLength(1));
    await act(async () => { fail("runs", new NetworkError("连接中断")); });
    rerender({ epoch: 0, principal: USER_ID, inputKey: submissionKey(submission()), editorKey: "instance-1", sourceCaseId: "saved-case" });
    gated.add("runs");
    act(() => { void result.current.retryAcceptance(); });
    await waitFor(() => expect(runs()).toHaveLength(2));
    expect(runs()[0].body).not.toHaveProperty("source_case_id");
    expect(runs()[1].body).toEqual(runs()[0].body);
    expect(keys()[1]).toBe(keys()[0]);
  });
  it("首次受理不明 → 配置变化 → 确认收到 403：仍是 unknown，原键保留", async () => {
    gated.add("runs");
    const { result, rerender } = mount();
    act(() => {
      void result.current.start(submission(), "测试环境");
    });
    await waitFor(() => expect(runs()).toHaveLength(1));
    const originalKey = keys()[0];
    await act(async () => {
      fail("runs", new NetworkError("连接中断"));
    });
    expect(result.current.phase).toBe("acceptance_unknown");

    // 展示读取可以前进到 C，但 unknown 的确认仍绑定发送检查 B 的原 payload/token/key。
    preflightOverride = readyWith("fingerprint-c", "token-c");
    await act(async () => { await result.current.runPreflight(submission()); });
    expect(result.current.preflight?.resolution?.context_fingerprint).toBe("fingerprint-c");
    const preflightCountBeforeConfirm = preflightCalls().length;

    // 配置变化：unknown 必须保留（改动草稿不证明上一次没被受理）。
    rerender({ epoch: 1, principal: USER_ID, inputKey: submissionKey(submission()), editorKey: "instance-1", sourceCaseId: undefined });
    await act(async () => {});
    expect(result.current.phase).toBe("acceptance_unknown");

    // 确认受理时收到 403：**不能**断言原运行不存在，也不能丢掉原键。
    gated.add("runs");
    act(() => {
      void result.current.retryAcceptance();
    });
    await waitFor(() => expect(runs()).toHaveLength(2));
    await act(async () => {
      fail("runs", new ApiError(403, "forbidden", "无权访问该幂等记录", null));
    });
    await act(async () => {});

    expect(result.current.phase).toBe("acceptance_unknown");
    expect(result.current.operationActive).toBe(true);
    // 普通发送不会用新键开第二条链。
    act(() => {
      void result.current.start(submission(), "测试环境");
    });
    await act(async () => {});
    expect(runs()).toHaveLength(2);

    // 再次确认：仍是原键、原内容。
    gated.add("runs");
    act(() => {
      void result.current.retryAcceptance();
    });
    await waitFor(() => expect(runs()).toHaveLength(3));
    expect(keys()[2]).toBe(originalKey);
    expect(runs()[2].body).toEqual(runs()[0].body);
    expect((runs()[2].body as { resolution_context?: string }).resolution_context).toBe("resolution-context-1");
    expect(preflightCalls()).toHaveLength(preflightCountBeforeConfirm);
  });
});

describe("R3-11 待确认授权随内容变化撤销", () => {
  it("等待确认时切换环境：撤销未提交的确认，且不签发、不提交", async () => {
    // 确认面板上写着环境与路径。用户在等待期间切到另一个环境，若仍按冻结的那份签发并提交，
    // 他看到的和实际授出去的就不是同一件事。此时还没有任何写请求，撤销是安全的。
    needsAuth = true;
    // 必须是**另一个**环境：与 ENV_ID 相同的话内容键不变，测的就不是切换环境。
    const otherEnv = "55555555-5555-4555-8555-555555555556";
    const { result, rerender } = mount();
    await act(async () => {
      await result.current.start(submission(), "测试环境");
    });
    expect(result.current.phase).toBe("awaiting_authorization");
    expect(result.current.authorizationView?.environmentLabel).toBe("测试环境");

    // 切换到另一个环境：内容键随之变化。
    rerender({
      epoch: 0,
      principal: USER_ID,
      inputKey: submissionKey({ ...submission(), environmentId: otherEnv }),
      editorKey: "instance-1",
      sourceCaseId: undefined,
    });
    await act(async () => {});

    expect(result.current.phase).toBe("idle");
    expect(result.current.authorizationView).toBeNull();
    expect(digests()).toHaveLength(0);
    expect(grants()).toHaveLength(0);
    expect(runs()).toHaveLength(0);
  });

  it("只改名称／目录不撤销确认：它们不是执行输入", async () => {
    // 名称与目录不参与 submissionKey，因此在这个 Hook 的视角里内容没有变化。
    needsAuth = true;
    const { result, rerender } = mount();
    await act(async () => {
      await result.current.start(submission(), "测试环境");
    });
    expect(result.current.phase).toBe("awaiting_authorization");

    rerender({
      epoch: 0,
      principal: USER_ID,
      inputKey: submissionKey(submission()),
      editorKey: "instance-1",
      sourceCaseId: undefined,
    });
    await act(async () => {});
    expect(result.current.phase).toBe("awaiting_authorization");
  });
});

describe("R3-03 排队期间锁不释放", () => {
  it("受理返回 queued 后仍锁住普通发送；只有该运行的终态才解锁", async () => {
    gated.add("report:" + RUN_1);
    const { result } = mount();

    await act(async () => {
      await result.current.start(submission(), "测试环境");
    });
    expect(runs()).toHaveLength(1);
    // 202 只说明已受理：运行还没结束，锁必须还在。
    expect(result.current.phase).toBe("running");
    expect(result.current.operationActive).toBe(true);
    expect(result.current.activeRunId).toBe(RUN_1);

    // 期间再点普通发送：不会产生第二条受理。
    await act(async () => {
      await result.current.start(submission(), "测试环境");
    });
    expect(runs()).toHaveLength(1);

    // 该运行到达终态 → 释放。
    await act(async () => {
      release("report:" + RUN_1, report(RUN_1));
    });
    await waitFor(() => expect(result.current.phase).toBe("idle"));
    expect(result.current.operationActive).toBe(false);
  });

  it("另一个运行的终态不释放当前活动的锁", async () => {
    // 用户查看历史 r2 的终态时，仍在跑的 r1 不能被顺带解锁。
    gated.add("report:" + RUN_1);
    const { result } = mount();
    await act(async () => {
      await result.current.start(submission(), "测试环境");
    });
    expect(result.current.activeRunId).toBe(RUN_1);

    // 缓存里出现 r2 的终态报告（另一个运行）。
    await act(async () => {
      release("report:" + RUN_2, report(RUN_2));
    });
    await act(async () => {});
    expect(result.current.phase).toBe("running");
    expect(result.current.operationActive).toBe(true);
  });
});

describe("R3-04 受理不明在配置变化后仍然保留", () => {
  it("提交网络错误后配置世代变化：仍是 unknown，原键与原内容不变，确认只用原键", async () => {
    gated.add("runs");
    const { result, rerender } = mount();
    // 发起但不等待：这次提交被替身挂起，await 它会一直等下去。
    act(() => {
      void result.current.start(submission(), "测试环境");
    });
    await waitFor(() => expect(runs()).toHaveLength(1));
    const original = keys()[0];
    await act(async () => {
      fail("runs", new NetworkError("连接中断"));
    });
    expect(result.current.phase).toBe("acceptance_unknown");

    // 用户改了环境／变量 → 配置世代推进。受理不明**不能**因此被清成空闲：
    // 改动草稿并不证明上一次没被受理。
    rerender({ epoch: 1, principal: USER_ID, inputKey: submissionKey(submission()), editorKey: "instance-1", sourceCaseId: undefined });
    await act(async () => {});
    expect(result.current.operationActive).toBe(true);

    // 普通发送仍然不可用；确认走的是原键与原内容。
    gated.add("runs");
    await act(async () => {
      await result.current.start(submission(), "测试环境");
    });
    // 新内容被配置变化挡住：不会新开一条链，也不会用新键。
    expect(runs()).toHaveLength(1);

    await act(async () => {
      void result.current.retryAcceptance();
    });
    await waitFor(() => expect(runs()).toHaveLength(2));
    expect(keys()[1]).toBe(original);
    expect(runs()[1].body).toEqual(runs()[0].body);
    expect((runs()[1].body as { resolution_context?: string }).resolution_context).toBe("resolution-context-1");
  });

  it("5xx 也算受理不明：运行可能已经建好", async () => {
    gated.add("runs");
    const { result } = mount();
    act(() => {
      void result.current.start(submission(), "测试环境");
    });
    await waitFor(() => expect(runs()).toHaveLength(1));
    await act(async () => {
      const error = new ApiError(503, "unavailable", "上游不可用", null);
      fail("runs", error);
    });
    expect(result.current.phase).toBe("acceptance_unknown");
  });
});

describe("R3-05 停止等待与迟到回调", () => {
  it("提交中停止等待转入 unknown；迟到的成功不改变阶段、不启动报告", async () => {
    gated.add("runs");
    const { result } = mount();
    act(() => {
      void result.current.start(submission(), "测试环境");
    });
    await waitFor(() => expect(runs()).toHaveLength(1));
    expect(result.current.phase).toBe("submitting");

    await act(async () => {
      result.current.stopWaiting();
    });
    expect(result.current.phase).toBe("acceptance_unknown");

    // 迟到的成功返回：本次尝试已经作废，无权把阶段改成 running。
    const reportsBefore = calls.filter((c) => c.method === "GET").length;
    await act(async () => {
      release("runs", runSummary(RUN_1));
    });
    await act(async () => {});
    expect(result.current.phase).toBe("acceptance_unknown");
    expect(calls.filter((c) => c.method === "GET").length).toBe(reportsBefore);
  });

  it("运行中停止等待只暂停轮询，保留锁与取消；恢复后继续读同一条运行", async () => {
    // 报告保持挂起：它一旦到达终态，锁就会正常释放，观察不到“运行中暂停”这件事。
    gated.add("report:" + RUN_1);
    const { result } = mount();
    await act(async () => {
      await result.current.start(submission(), "测试环境");
    });
    await waitFor(() => expect(result.current.phase).toBe("running"));

    await act(async () => {
      result.current.stopWaiting();
    });
    expect(result.current.paused).toBe(true);
    // 暂停仍然持有锁：运行还在服务端继续。
    expect(result.current.operationActive).toBe(true);
    expect(result.current.activeRunId).toBe(RUN_1);

    await act(async () => {
      result.current.resumeWaiting();
    });
    expect(result.current.paused).toBe(false);
  });

  it("受理不明时停止等待不丢弃待确认状态", async () => {
    gated.add("runs");
    const { result } = mount();
    act(() => {
      void result.current.start(submission(), "测试环境");
    });
    await waitFor(() => expect(runs()).toHaveLength(1));
    await act(async () => {
      fail("runs", new NetworkError("断了"));
    });
    expect(result.current.phase).toBe("acceptance_unknown");

    await act(async () => {
      result.current.stopWaiting();
    });
    // 仍然保留原操作：它可能已经在服务端受理。
    expect(result.current.phase).toBe("acceptance_unknown");
    expect(result.current.operationActive).toBe(true);
  });
});

describe("R3-15 主体变化使旧操作失效", () => {
  it("主体变化后，旧链的迟到回调不再提交后续写请求", async () => {
    gated.add("runs");
    const { result, rerender } = mount();
    act(() => {
      void result.current.start(submission(), "测试环境");
    });
    await waitFor(() => expect(runs()).toHaveLength(1));

    // 换登录主体：所有者变了，这次操作不再属于当前范围。
    rerender({
      epoch: 0,
      principal: "11111111-1111-4111-8111-111111111111",
      inputKey: submissionKey(submission()),
      editorKey: "instance-1",
      sourceCaseId: undefined,
    });
    await act(async () => {});
    expect(result.current.phase).toBe("idle");

    const before = runs().length;
    await act(async () => {
      release("runs", runSummary(RUN_1));
    });
    await act(async () => {});
    // 旧链没有能力再发出任何写请求。
    expect(runs()).toHaveLength(before);
  });

  it("主体变化不会让阶段卡在预检中", async () => {
    gated.add("preflight");
    const { result, rerender } = mount();
    act(() => {
      void result.current.start(submission(), "测试环境");
    });
    await waitFor(() => expect(calls.some((c) => c.path.endsWith("/debug-preflight"))).toBe(true));
    expect(result.current.phase).toBe("preflighting");

    rerender({
      epoch: 0,
      principal: "11111111-1111-4111-8111-111111111111",
      inputKey: submissionKey(submission()),
      editorKey: "instance-1",
      sourceCaseId: undefined,
    });
    await act(async () => {});
    expect(result.current.phase).not.toBe("preflighting");
    expect(result.current.operationActive).toBe(false);
  });
});

describe("S1 普通变量问题优先于授权", () => {
  it("普通解析失败且身份待授权时先保留输入纠错，零授权零运行", async () => {
    preflightOverride = {
      ...NEEDS_AUTH,
      resolution: {
        ...RESOLUTION,
        ready: false,
        ordinary_resolution: "invalid",
        issues: [{ issue_id: "missing-1", code: "variable_undefined", message: "变量缺失", action: "edit_request", location: { kind: "path", field: "path" } }],
        resolution_context: null,
      },
    };
    const { result } = mount();
    await act(async () => result.current.start(submission(), "测试环境"));
    expect(result.current.phase).toBe("idle");
    expect(result.current.error).toMatch(/未取得可提交的变量解析依据/);
    expect(grants()).toEqual([]);
    expect(runs()).toEqual([]);
  });
});

describe("R11 发送预检接纳为当前比较依据", () => {
  it("展示A完成后输入切到B立即发送，直接以B为比较依据并提交B token", async () => {
    const submissionA = submission();
    const submissionB = {
      ...submissionA,
      request: { ...submissionA.request, path: "/echo-b" },
    };
    preflightSequence = [
      readyWith("fingerprint-a", "token-a"),
      readyWith("fingerprint-b", "token-b"),
    ];
    const { result, rerender } = mount();

    await act(async () => { await result.current.runPreflight(submissionA); });
    expect(result.current.preflight?.resolution?.context_fingerprint).toBe("fingerprint-a");

    rerender({
      epoch: 0,
      principal: USER_ID,
      inputKey: submissionKey(submissionB),
      editorKey: "instance-1",
      sourceCaseId: undefined,
    });
    expect(result.current.preflight).toBeNull();

    await act(async () => { await result.current.start(submissionB, "测试环境"); });

    expect(preflightCalls()).toHaveLength(2);
    expect(preflightCalls()[1]?.body).toMatchObject({ debug_snapshot: { request: { path: "/echo-b" } } });
    expect(result.current.preflightFor).toBe(submissionKey(submissionB));
    expect(result.current.preflight?.resolution?.context_fingerprint).toBe("fingerprint-b");
    expect(runs()).toHaveLength(1);
    expect(runs()[0]?.body).toMatchObject({
      debug_snapshot: { request: { path: "/echo-b" } },
      resolution_context: "token-b",
    });
  });

  it("同输入旧epoch1展示仍残留时，epoch2立即发送接纳epoch2依据", async () => {
    preflightSequence = [
      readyWith("fingerprint-epoch-1", "token-epoch-1"),
      readyWith("fingerprint-epoch-2", "token-epoch-2"),
    ];
    epochClock.current = 1;
    const { result, rerender } = mount(USER_ID, 1);

    await act(async () => { await result.current.runPreflight(submission()); });
    expect(result.current.preflight?.resolution?.context_fingerprint).toBe("fingerprint-epoch-1");

    rerender({
      epoch: 2,
      principal: USER_ID,
      inputKey: submissionKey(submission()),
      editorKey: "instance-1",
      sourceCaseId: undefined,
    });
    expect(epochClock.current).toBe(2);
    expect(result.current.preflight).toBeNull();

    await act(async () => { await result.current.start(submission(), "测试环境"); });

    expect(preflightCalls()).toHaveLength(2);
    expect(result.current.preflightFor).toBe(submissionKey(submission()));
    expect(result.current.preflight?.resolution?.context_fingerprint).toBe("fingerprint-epoch-2");
    expect(runs()).toHaveLength(1);
    expect(runs()[0]?.body).toMatchObject({ resolution_context: "token-epoch-2" });
  });

  it("同输入展示A后发送B，立即以B展示且Run携B token", async () => {
    const preflightA = readyWith("fingerprint-a", "token-a");
    const preflightB = readyWith("fingerprint-b", "token-b");
    preflightSequence = [preflightA, preflightB];
    const { result } = mount();
    await act(async () => { await result.current.runPreflight(submission()); });
    expect(result.current.preflight?.resolution?.context_fingerprint).toBe("fingerprint-a");
    await act(async () => { await result.current.start(submission(), "测试环境"); });
    expect(result.current.preflight?.resolution?.context_fingerprint).toBe("fingerprint-b");
    expect(runs()[0]?.body).toMatchObject({ resolution_context: "token-b" });
  });

  it.each(["success", "error"] as const)("发送B接纳后迟到展示A的%s/finally不能覆盖B", async (outcome) => {
    let resolveA!: (value: unknown) => void;
    let rejectA!: (cause: unknown) => void;
    const delayedA = new Promise((resolve, reject) => { resolveA = resolve; rejectA = reject; });
    const preflightB = readyWith("fingerprint-b", "token-b");
    preflightSequence = [delayedA, preflightB];
    const { result } = mount();
    let pending!: Promise<unknown>;
    act(() => { pending = result.current.runPreflight(submission()); });
    await waitFor(() => expect(preflightCalls()).toHaveLength(1));
    await act(async () => { await result.current.start(submission(), "测试环境"); });
    expect(result.current.preflight?.resolution?.context_fingerprint).toBe("fingerprint-b");
    await act(async () => {
      if (outcome === "success") resolveA(readyWith("fingerprint-a", "token-a"));
      else rejectA(new Error("迟到A失败"));
      await pending;
    });
    expect(result.current.preflight?.resolution?.context_fingerprint).toBe("fingerprint-b");
    expect(result.current.preflightError).toBeNull();
    expect(result.current.preflighting).toBe(false);
    expect(runs()).toHaveLength(1);
  });

  it("发送B返回时实时配置getter为null，不接纳B也不提交", async () => {
    let resolveB!: (value: unknown) => void;
    const delayedB = new Promise((resolve) => { resolveB = resolve; });
    preflightSequence = [delayedB];
    const { result } = mount();
    act(() => { void result.current.start(submission(), "测试环境"); });
    await waitFor(() => expect(preflightCalls()).toHaveLength(1));
    epochClock.current = null;
    await act(async () => { resolveB(readyWith("fingerprint-b", "token-b")); });
    await waitFor(() => expect(result.current.phase).toBe("idle"));
    expect(result.current.preflight).toBeNull();
    expect(result.current.notice).toMatch(/当前依据暂未确认/);
    expect(runs()).toEqual([]);
  });
});
