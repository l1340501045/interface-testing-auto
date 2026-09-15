/**
 * 一次调试发送的**完整生命周期**：预检 → 等待授权确认 → 授权 → 提交受理 → 结果不明
 * → 运行中 → 终态／取消。
 *
 * 这个 Hook 只负责“把一份**已经冻结**的内容按一条权威链路发出去”，不读草稿：调用方在
 * 点击的那一刻交出快照，之后用户继续编辑与本次已提交的内容无关。这正是调试与“保存并
 * 执行已发布版本”在职责上的分界——后者的内容由发布版本固定，前者的内容由这一次点击固定。
 *
 * ## 三个必须分开的概念
 *
 * 1. **操作所有者**（owner）：挂载生成的 editorKey、工作空间、项目、登录主体。所有者变化
 *    或卸载后，旧回调**不得**再写界面、授权、提交或重试受理。首次保存取得 caseId 不改变
 *    所有者——那还是同一次编辑。
 * 2. **当前配置依据**（inputStamp）：所选环境 + 请求／断言的内容键 + 观察到的配置世代。
 *    它决定“预检与当前通过”能不能用；**变化不等于已经提交的运行消失**。
 * 3. **活动操作与查看选择**：活动操作持有提交锁、原键、冻结输入与 activeRunId；查看选择
 *    只表示用户正在看哪条运行。切到历史**不结束**活动操作，历史终态也不能释放另一个活动
 *    运行的锁。
 *
 * ## 为什么阶段必须写在一个同步 ref 里
 *
 * 一次发送跨多个异步阶段，每个阶段都可能被第二次点击、编辑、切项目或卸载打断。若把
 * “正在提交”和“正在预检”拆成互不知情的布尔标志，两处都会各自认为自己还有权继续，
 * 于是出现“同帧确认发两次 grant”“双击建两条链用两个新键提交”这类竞态。因此权威状态是
 * 一个 `stateRef`：所有公开动作先读它、在**第一个 await 之前**完成迁移，React state 只
 * 用于呈现同一个状态。`setPhase(...)` 这类只更新 state 的写法挡不住同帧重复调用。
 *
 * ## 受理结果不明（acceptance_unknown）
 *
 * 网络错误、5xx、成功信封无法解码都说明“请求可能已经到达服务端”。此时既不能当成成功
 * （会谎称已有运行），也不能当成失败（会让用户再点普通发送、用新键产生第二次业务请求）。
 * 它保留原键与原内容，只允许用同一个幂等键再问一次“刚才受理了没有”。
 *
 * **配置变化不清掉 unknown**：改了草稿或环境并不证明上一次没被受理。unknown 因此只受
 * 所有者约束，不受配置世代约束。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ApiError, NetworkError, apiSend, projectPath } from "../api/client";
import { toDebugPreflight, toDebugSnapshotDigest, toRunReport, toRunSummary } from "../api/guards";
import type { CaseAssertion, DebugPreflight, RequestSpec, RunReport, RunSummary } from "../api/types";
import { isTerminal } from "./useRuns";

/** 本次编辑实例里已受理的一次调试；按受理顺序倒序展示。 */
export interface DebugRecord {
  runId: string;
  /** 提交时的环境与内容摘要，仅用于在本页说明“这条记录对应哪一次发送”。 */
  environmentId: string;
  submittedAt: number;
}

export interface DebugSubmission {
  environmentId: string;
  request: RequestSpec;
  assertions: CaseAssertion[];
}

/** 操作所有者：它变化就意味着这是“另一件事”，旧回调一律无权继续。 */
interface Owner {
  editorKey: string;
  workspaceId: string;
  projectId: string;
  principalId: string;
}

/** 当前配置依据：决定预检与“当前通过”能不能用。 */
interface InputStamp {
  ownerKey: string;
  /** 环境 + 请求 + 断言的内容键（不含名称、目录——它们不是执行输入）。 */
  inputKey: string;
  configEpoch: number;
}

/** 一次发送操作的不可变上下文；整条链共用同一个对象。 */
interface Operation {
  /** 操作身份：阶段迁移与迟到回调都靠它判断“我还是当前那个操作吗”。 */
  token: string;
  /** 幂等键：预检、授权、提交、确认受理全部共用。 */
  key: string;
  owner: Owner;
  inputStamp: InputStamp;
  submission: DebugSubmission;
  /** 发送时所选环境的名称：确认面板显示它，而不是当前选择。 */
  environmentLabel: string;
  /** 预检确认可用且本次用途尚未授权时的身份坐标；只有一次预检能写入它。 */
  profileId: string | null;
}

/**
 * 权威执行状态。
 *
 * `running` 是独立阶段而不是“完成”：202 只说明已受理，锁要一直持有到**那条运行**的
 * 可信终态到达。否则排队期间用户又能用新键发出一份内容，同一份内容产生两次请求。
 */
type ExecutionState =
  | { phase: "idle" }
  | { phase: "preflighting"; op: Operation }
  | { phase: "awaiting_authorization"; op: Operation }
  | { phase: "authorizing"; op: Operation }
  | { phase: "submitting"; op: Operation; attempt: number; mode: "initial" | "reconcile" }
  | { phase: "acceptance_unknown"; op: Operation }
  | {
      phase: "running";
      op: Operation;
      runId: string;
      /** 停止等待只暂停本地轮询，不释放锁、不取消服务端运行。 */
      paused: boolean;
      cancelState: "none" | "requesting" | "requested";
    };

/**
 * 一次预检的结局。
 *
 * 三态而不是“结果或 null”：`stale`（过期／被取消）与 `failed`（确实读失败）都表示
 * **没有得到可用的结论**，两者都**不能**推导出“可以发送”。用 `null` 表示它们，调用方
 * 就会把“没读到”当成“没问题”，跳过授权检查把请求直接发出去——那正是“两次检查都需要
 * 授权、却零授权建了运行、也没有确认面板”的原因。
 */
type PreflightOutcome =
  | { kind: "ok"; value: DebugPreflight }
  | { kind: "failed"; message: string }
  | { kind: "stale" };


/** 授权确认面板要展示的完整冻结摘要；全部字段来自同一个有效操作。 */
export interface AuthorizationView {
  environmentId: string;
  environmentLabel: string;
  method: string;
  path: string;
  profileId: string | null;
  principalId: string;
  ttlMinutes: number;
}

const AUTHORIZATION_TTL_MINUTES = 10;

/**
 * 幂等键：一次点击一个，与冻结内容绑定。
 *
 * 用 `crypto.randomUUID` 而不是时间戳或递增计数：同一个毫秒内的两次点击会拿到同一个
 * 键，服务端据此判为“同一次受理”，用户以为发了两次、实际只发了一次。
 */
function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  // 运行环境没有 randomUUID 时退回一个足够唯一的组合；不引入依赖。
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

/**
 * 冻结提交内容的稳定键：环境 + 请求定义 + 断言。
 *
 * 与发送用的载荷同源，因此“键相同”就等于“这份预检结论针对的就是将要发出的内容”。
 * 名称与目录**不参与**：它们不是执行输入，改个名字不该让一份通过的结论失效。
 */
export function submissionKey(submission: DebugSubmission): string {
  return JSON.stringify({
    environmentId: submission.environmentId,
    request: submission.request,
    assertions: submission.assertions,
  });
}

/**
 * 请求是否“根本没得到服务端应答”。
 *
 * 只有这类情况才可能是“已受理但不知道结果”。服务端明确回绝（ApiError 且是 4xx 语义的
 * 确定性拒绝）说明请求被处理过、结果是确定的失败，不占用一次性幂等键。
 *
 * 5xx 不能算确定失败：网关或上游在写库之后才出错时，运行可能已经建好。
 */
function isAcceptanceUnknown(cause: unknown): boolean {
  if (cause instanceof NetworkError) return true;
  if (cause instanceof ApiError) return cause.status >= 500;
  return true;
}

export function useDebugRun(
  workspaceId: string,
  projectId: string,
  /**
   * 编辑实例：切换用例、关闭重开都会换值。
   *
   * **首次保存得到 id 不换值**——那还是同一次编辑、同一份内容，换掉会让本次调试历史
   * 与正在受理的运行一起被清空。
   */
  editorKey: string,
  /** 配置世代：环境／变量／身份配置成功变更时由外壳递增。 */
  configEpoch: number,
  /** 当前登录主体；授权只能给本人签发。 */
  principalId: string,
  /**
   * 当前编辑内容的内容键（`submissionKey`）；没有合法内容时为 null。
   *
   * 由调用方每次渲染传入，用于判断“预检结论／未提交操作是否还对应屏幕上的内容”。
   * 传进来而不是让 Hook 自己读表单：Hook 不持有草稿，也就不该猜。
   */
  currentInputKey: string | null,
  /**
   * 一条运行被受理时通知调用方。
   *
   * 这是**允许改变查看选择**的显式事件之一（另一个是用户点选历史记录）。让它显式发生，
   * 而不是靠“第一份报告到达”自动选择：报告是异步的，任何一次迟到都可能把界面从一种
   * 来源切到另一种，出现“正文是调试 B、字段区却是版本 A”。
   */
  onRunAccepted?: (runId: string) => void,
  /**
   * 读取**当前**配置世代。
   *
   * 有些异步回调（授权签发之后、提交之前）需要确认"配置没有被人在我等待期间改过"。
   * props 里的 `configEpoch` 是本次渲染的值，`await` 之后它可能已经旧了；这个函数读的是
   * 时钟的当下值。返回 `null` 表示所有者已经失效（切了范围或主体）。
   */
  getConfigEpoch?: () => number | null,
) {
  const base = projectPath(workspaceId, projectId, "");
  const ownerKey = `${workspaceId}/${projectId}/${editorKey}/${principalId}`;
  const owner: Owner = useMemo(
    () => ({ editorKey, workspaceId, projectId, principalId }),
    [editorKey, workspaceId, projectId, principalId],
  );

  /** 权威状态：同步 ref 是判据，state 只用于呈现。 */
  const stateRef = useRef<ExecutionState>({ phase: "idle" });
  const [state, setState] = useState<ExecutionState>(stateRef.current);
  const [records, setRecords] = useState<DebugRecord[]>([]);
  /** 展示性预检（编辑防抖触发的那些）；不是一次发送操作。 */
  const [preview, setPreview] = useState<{
    value: DebugPreflight | null;
    stamp: InputStamp | null;
  }>({ value: null, stamp: null });
  const [previewError, setPreviewError] = useState<{
    message: string;
    stamp: InputStamp | null;
  } | null>(null);
  const [checking, setChecking] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  /**
   * 按 run_id 缓存的报告。
   *
   * 缓存而不是单一槽位：查看 r1 时 r2 的报告读失败，r1 仍然留在缓存里可供以后查看，
   * 但**不会**被当成 r2 的正文——显示层只按当前选择的 run_id 取。
   */
  const [reports, setReports] = useState<Record<string, RunReport>>({});
  const [reportErrors, setReportErrors] = useState<Record<string, string>>({});

  /** 组件是否仍挂载；卸载后不再写状态，也不再发起后续请求。 */
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      // 卸载即作废操作：旧链的迟到回调不能再授权、提交或重试受理。
      stateRef.current = { phase: "idle" };
    };
  }, []);

  /** 所有者与配置依据的实时值，供异步回调在 await 之后核对。 */
  const ownerRef = useRef(owner);
  ownerRef.current = owner;
  const epochRef = useRef(configEpoch);
  epochRef.current = configEpoch;
  const inputKeyRef = useRef(currentInputKey);
  inputKeyRef.current = currentInputKey;

  const getEpochRef = useRef(getConfigEpoch);
  getEpochRef.current = getConfigEpoch;
  /** 当下的配置世代：优先读时钟，没有时钟时退回本次渲染的值。 */
  const liveEpoch = useCallback((): number | null => {
    const fromClock = getEpochRef.current?.();
    return fromClock === undefined ? epochRef.current : fromClock;
  }, []);

  const stampFor = useCallback(
    (key: string | null): InputStamp | null => {
      const epoch = liveEpoch();
      return key === null || epoch === null ? null : { ownerKey, inputKey: key, configEpoch: epoch };
    },
    [ownerKey, liveEpoch],
  );
  const liveStamp = useMemo(
    () => stampFor(currentInputKey),
    [stampFor, currentInputKey, configEpoch],
  );
  const liveStampRef = useRef(liveStamp);
  liveStampRef.current = liveStamp;

  /**
   * 所有者是否仍然是同一个。
   *
   * 这是**唯一**因为“换了别的事”而让操作作废的条件。配置变化不在其中：它只影响“能不能
   * 新发请求”，不影响“上一次提交是否已经受理”。
   */
  const owns = useCallback((op: Operation): boolean => {
    if (!mountedRef.current) return false;
    const live = ownerRef.current;
    return (
      op.owner.editorKey === live.editorKey &&
      op.owner.workspaceId === live.workspaceId &&
      op.owner.projectId === live.projectId &&
      op.owner.principalId === live.principalId
    );
  }, []);

  /** 当前的配置依据是否与这次操作冻结的一致——只有一致才允许**发起新的写请求**。 */
  const canStartNewWrite = useCallback((op: Operation): boolean => {
    if (!owns(op)) return false;
    // 配置世代要读**当下**的：这个判断会在多个 await 之后被调用，props 里的值是发起时
    // 那一份，用它会让"等待期间有人改了环境"这条路径漏过去。
    const epochNow = liveEpoch();
    if (epochNow === null || epochNow !== op.inputStamp.configEpoch) return false;
    const live = liveStampRef.current;
    if (live === null) return false;
    return live.ownerKey === op.inputStamp.ownerKey && live.inputKey === op.inputStamp.inputKey;
  }, [owns]);

  /** 同步状态迁移：前态必须正是 `expected`，否则说明有另一个操作已经接管。 */
  const replace = useCallback(
    (expected: ExecutionState, next: ExecutionState): boolean => {
      if (stateRef.current !== expected) return false;
      stateRef.current = next;
      if (mountedRef.current) setState(next);
      return true;
    },
    [],
  );

  const toIdle = useCallback(() => {
    stateRef.current = { phase: "idle" };
    if (mountedRef.current) setState({ phase: "idle" });
  }, []);

  /**
   * 展示性预检与发送前检查使用**独立**的读取世代与取消控制器。
   *
   * 分开是必需的：展示预检由编辑防抖触发，随时可能重发。共用一个世代／控制器时，一次
   * 普通的内容编辑就能把**正在进行的发送检查**判成过期，发送流程于是失去判断依据。
   */
  const displayGenerationRef = useRef(0);
  const displayAbortRef = useRef<AbortController | null>(null);
  const checkGenerationRef = useRef(0);
  const checkAbortRef = useRef<AbortController | null>(null);

  const fetchPreflight = useCallback(
    async (submission: DebugSubmission, signal: AbortSignal): Promise<DebugPreflight> =>
      apiSend(
        `${base}/debug-preflight`,
        "POST",
        {
          environment_id: submission.environmentId,
          debug_snapshot: { request: submission.request, assertions: submission.assertions },
        },
        toDebugPreflight,
        { signal },
      ),
    [base],
  );

  /**
   * 展示性预检：只刷新按钮旁的提示，**不参与发送决策**。
   *
   * 它的成败只影响界面文案；发送走 `checkPreflight`，因此这里被编辑取消或迟到都不会
   * 改变一次正在进行的发送。
   */
  const runPreflight = useCallback(
    async (submission: DebugSubmission): Promise<PreflightOutcome> => {
      const stamp = stampFor(submissionKey(submission));
      const generation = ++displayGenerationRef.current;
      displayAbortRef.current?.abort();
      const controller = new AbortController();
      displayAbortRef.current = controller;
      if (mountedRef.current) {
        setPreviewError(null);
        setChecking(true);
      }
      const stale = (): PreflightOutcome => ({ kind: "stale" });
      try {
        const result = await fetchPreflight(submission, controller.signal);
        if (!mountedRef.current) return stale();
        if (generation !== displayGenerationRef.current) return stale();
        if (!sameStamp(stamp, liveStampRef.current)) return stale();
        setPreview({ value: result, stamp });
        return { kind: "ok", value: result };
      } catch (cause) {
        if (!mountedRef.current) return stale();
        if (generation !== displayGenerationRef.current) return stale();
        if (!sameStamp(stamp, liveStampRef.current)) return stale();
        const message = cause instanceof Error ? cause.message : "预检失败";
        setPreview({ value: null, stamp: null });
        setPreviewError({ message, stamp });
        return { kind: "failed", message };
      } finally {
        if (generation === displayGenerationRef.current && mountedRef.current) setChecking(false);
      }
    },
    [fetchPreflight, stampFor],
  );

  /**
   * 发送决策专用的检查：独立世代与取消控制器。
   *
   * 只有它自己被新的发送检查取代时才作废——界面上的内容刷新不会让一次正在进行的发送
   * 失去判断依据。
   */
  const checkPreflight = useCallback(
    async (submission: DebugSubmission): Promise<PreflightOutcome> => {
      const generation = ++checkGenerationRef.current;
      checkAbortRef.current?.abort();
      const controller = new AbortController();
      checkAbortRef.current = controller;
      try {
        const result = await fetchPreflight(submission, controller.signal);
        if (generation !== checkGenerationRef.current) return { kind: "stale" };
        return { kind: "ok", value: result };
      } catch (cause) {
        if (generation !== checkGenerationRef.current) return { kind: "stale" };
        return {
          kind: "failed",
          message: cause instanceof Error ? cause.message : "预检失败",
        };
      }
    },
    [fetchPreflight],
  );


  /** 报告读取世代；同一个 run 的多次读取按它排除迟到者。 */
  const reportGenerationRef = useRef<Record<string, number>>({});
  /** 每个被观察 run 的读取中止控制器；切换范围时统一中止。 */
  const reportAbortRef = useRef(new Map<string, AbortController>());

  const loadReport = useCallback(
    async (runId: string): Promise<void> => {
      const generation = (reportGenerationRef.current[runId] ?? 0) + 1;
      reportGenerationRef.current[runId] = generation;
      const ownerAtCall = ownerRef.current;
      try {
        const loaded = await apiSend(`${base}/runs/${runId}/report`, "GET", undefined, toRunReport, {
          signal: reportAbortRef.current.get(runId)?.signal,
        });
        if (!mountedRef.current) return;
        if (reportGenerationRef.current[runId] !== generation) return;
        if (!sameOwner(ownerAtCall, ownerRef.current)) return;
        // 信封必须真的是**这条运行**的报告。形状不符（代理返回 HTML 错误页、后端改名、
        // 或替身给出空值）时按读取失败处理：存进缓存会让后面的匹配逻辑拿到一个没有 run
        // 的对象，而那正是“读到了别的运行/读到空值却当成结论”的入口。
        if (loaded === null || typeof loaded !== "object" || loaded.run?.id !== runId) {
          setReportErrors((current) => ({ ...current, [runId]: "报告内容与本次运行不符" }));
          return;
        }
        setReports((current) => ({ ...current, [runId]: loaded }));
        setReportErrors((current) => {
          if (!(runId in current)) return current;
          const next = { ...current };
          delete next[runId];
          return next;
        });
      } catch (cause) {
        if (!mountedRef.current) return;
        if (reportGenerationRef.current[runId] !== generation) return;
        if (!sameOwner(ownerAtCall, ownerRef.current)) return;
        const message = cause instanceof Error ? cause.message : "报告加载失败";
        setReportErrors((current) => ({ ...current, [runId]: message }));
      }
    },
    [base],
  );

  /**
   * 取得某个 run 的读取中止控制器。
   *
   * 每条被观察的运行各持一个：切范围时统一 abort，避免上一范围的读取在卸载后继续写状态。
   */
  const reportAbortFor = (runId: string): AbortController => {
    const existing = reportAbortRef.current.get(runId);
    if (existing !== undefined) return existing;
    const created = new AbortController();
    reportAbortRef.current.set(runId, created);
    return created;
  };

  /** 受理通知的实时引用：它变化不该影响已发出的提交闭包。 */
  const onRunAcceptedRef = useRef(onRunAccepted);
  onRunAcceptedRef.current = onRunAccepted;

  const activeRunId = state.phase === "running" ? state.runId : null;
  const paused = state.phase === "running" ? state.paused : false;

  /**
   * 需要观察的运行：当前查看的 + 活动操作持有的那个。
   *
   * 两者可能是同一条（多数情况），也可能是不同的两条——用户查看 r1 历史时 r2 还在跑，
   * 两条都要读，但用途不同：查看那条决定界面显示什么，活动那条决定锁什么时候释放。
   */
  const watchedRunIds = useMemo(() => {
    const ids = new Set<string>();
    if (activeRunId !== null) ids.add(activeRunId);
    return [...ids];
  }, [activeRunId]);

  /**
   * 活动运行的轮询：读回该 run 的报告，直到它到达服务端终态。
   *
   * 暂停（停止等待）时完全不发请求；恢复后立刻读一次。判据用 ref 而不是在状态更新函数里
   * 发请求——更新函数必须是纯的，在里面触发副作用会在 React 严格模式下被调用两次。
   */
  const reportsRef = useRef(reports);
  reportsRef.current = reports;

  useEffect(() => {
    if (paused) return;
    const timers: number[] = [];
    for (const runId of watchedRunIds) {
      void loadReport(runId);
      const timer = window.setInterval(() => {
        const current = reportsRef.current[runId];
        // 终态后停止轮询；报告缺失（还没拿到第一份）时继续试。
        if (current !== undefined && isTerminal(current.run)) return;
        void loadReport(runId);
      }, 2000);
      timers.push(timer);
    }
    return () => {
      for (const timer of timers) window.clearInterval(timer);
    };
  }, [watchedRunIds, paused, loadReport, ownerKey]);

  /**
   * 活动运行到达可信终态 → 释放锁。
   *
   * 只有**这条 run_id** 的终态能结束它：用户切去看历史 r1 的终态时，仍在跑的 r2 不能被
   * 顺带解锁，否则排队期间用户又能发一份新内容。
   */
  useEffect(() => {
    if (state.phase !== "running") return;
    const report = reports[state.runId];
    if (report?.run?.id !== state.runId) return;
    if (!isTerminal(report.run)) return;
    if (!owns(state.op)) return;
    replace(state, { phase: "idle" });
  }, [state, reports, owns, replace]);

  /**
   * 内容或配置变化 → 作废**尚未提交**的操作。
   *
   * 只处理尚未发出任何写请求的阶段：这时作废是安全的，也不会留下未知受理。已经在飞的
   * 提交、unknown 与 running 都保留——配置变化不证明它们没有发生。
   */
  useEffect(() => {
    const current = stateRef.current;
    if (
      current.phase !== "preflighting" &&
      current.phase !== "awaiting_authorization" &&
      current.phase !== "authorizing"
    ) {
      return;
    }
    if (canStartNewWrite(current.op)) return;
    toIdle();
    if (mountedRef.current) {
      setNotice("内容或环境已变化，本次发送已取消；请按当前内容重新发送。");
    }
  }, [currentInputKey, configEpoch, state, canStartNewWrite, toIdle]);

  /** 所有者变化（切项目／切主体／换编辑实例）时清空本页状态。 */
  useEffect(() => {
    stateRef.current = { phase: "idle" };
    setState({ phase: "idle" });
    setRecords([]);
    setReports({});
    setReportErrors({});
    setPreview({ value: null, stamp: null });
    setPreviewError(null);
    setNotice(null);
    setError(null);
    for (const controller of reportAbortRef.current.values()) controller.abort();
    reportAbortRef.current.clear();
  }, [ownerKey]);

  /**
   * 开始一条发送链路。**点击的那一帧就锁住**，锁一直持有到这条链收敛。
   *
   * 已有未完成的操作时直接返回 null：两次点击因此只会建一条链。
   */
  const start = useCallback(
    async (submission: DebugSubmission, environmentLabel: string): Promise<void> => {
      if (!mountedRef.current) return;
      const current = stateRef.current;
      if (current.phase !== "idle") {
        setNotice("上一次发送仍在处理中；同一份内容不会重复提交。");
        return;
      }
      const stamp = stampFor(submissionKey(submission));
      if (stamp === null) return;
      const op: Operation = {
        token: newIdempotencyKey(),
        key: newIdempotencyKey(),
        owner: ownerRef.current,
        inputStamp: stamp,
        submission,
        environmentLabel,
        profileId: null,
      };
      if (!replace(current, { phase: "preflighting", op })) return;
      setError(null);
      setNotice(null);

      const outcome = await checkPreflight(submission);
      // 预检期间可能已经被取消、被配置变化作废，或用户点了停止等待。
      if (stateRef.current.phase !== "preflighting" || stateRef.current.op.token !== op.token) return;
      if (!owns(op)) return;

      if (outcome.kind !== "ok") {
        // 没有得到可用的检查结论（读取失败，或这次检查已被取代）。
        //
        // **绝不能据此发送**：检查失败恰恰意味着我们不知道这次请求是否需要用环境身份。
        // 把“没读到”当成“没问题”直接提交，就会跳过授权确认——服务端最终会拒绝，但用户
        // 已经白等了一次受理，而界面上还出现过“需要授权”的提示。
        toIdle();
        setError(
          outcome.kind === "failed"
            ? `发送前检查未能完成（${outcome.message}）；未提交，请稍后重试。`
            : "发送前检查已被新的操作取代；未提交，请重试。",
        );
        return;
      }
      const result = outcome.value;
      if (result.ready) {
        await submitForward(op, "initial");
        return;
      }
      if (result.auth.state === "needs_authorization" && result.can_authorize) {
        // 需要授权且本人可管理身份：停下等**明确确认**，不自动签发。
        // 身份坐标来自**本次预检结果**，与确认面板显示、随后签发用的是同一个值。
        op.profileId = result.auth.profile_id;
        replace(stateRef.current, { phase: "awaiting_authorization", op });
        return;
      }
      // 其余情况（缺环境、环境歧义、身份不可用、只读角色等）都不可自助解决：
      // 保留原因，由界面按动作给出下一步，不创建运行。
      toIdle();
    },
    [checkPreflight, owns, replace, stampFor, toIdle],
  );

  /** attempt 世代：每个新的提交尝试独立编号，旧尝试的迟到回调无权改阶段。 */
  const attemptRef = useRef(0);
  const submitAbortRef = useRef<AbortController | null>(null);

  /**
   * 提交受理（首次或确认受理）。
   *
   * 键取自 `op.key`，因此“确认受理结果”与首次提交走同一条幂等记录：服务端对同键同内容
   * 返回同一次运行。**首次提交要求配置依据仍然一致**（不能拿旧内容发新请求）；确认受理
   * 只要求所有者一致——那是在问“刚才那次到底受理了没有”，与用户现在改了什么无关。
   */
  const submitForward = useCallback(
    async (op: Operation, mode: "initial" | "reconcile"): Promise<string | null> => {
      const current = stateRef.current;
      if (mode === "initial" && !canStartNewWrite(op)) {
        toIdle();
        return null;
      }
      if (mode === "reconcile" && !owns(op)) return null;
      // 允许进入提交的前态：预检完（不需授权）、授权完成后、以及确认旧受理时。
      // **`authorizing` 必须在列**：授权成功后的提交正是从那个阶段发起，漏掉它会让
      // 「授权并发送」在签发完成后静默什么都不做——用户看到授权已生效、运行却没建。
      const canSubmitFrom =
        current.phase === "preflighting" ||
        current.phase === "awaiting_authorization" ||
        current.phase === "authorizing" ||
        current.phase === "submitting" ||
        current.phase === "acceptance_unknown";
      if (!canSubmitFrom || current.op.token !== op.token) return null;
      const attempt = ++attemptRef.current;
      if (!replace(current, { phase: "submitting", op, attempt, mode })) return null;
      if (mountedRef.current) setError(null);
      submitAbortRef.current?.abort();
      const controller = new AbortController();
      submitAbortRef.current = controller;

      /** 本次尝试是否仍然有权改写状态。 */
      const stillMine = (): boolean => {
        const now = stateRef.current;
        return (
          mountedRef.current &&
          now.phase === "submitting" &&
          now.op.token === op.token &&
          now.attempt === attempt
        );
      };

      try {
        const run = await apiSend<RunSummary | null>(
          `${base}/runs`,
          "POST",
          {
            environment_id: op.submission.environmentId,
            debug_snapshot: { request: op.submission.request, assertions: op.submission.assertions },
          },
          (raw) => (raw === null || raw === undefined ? null : toRunSummary(raw)),
          { headers: { "Idempotency-Key": op.key }, signal: controller.signal },
        );
        const runId = run?.id ?? null;
        if (runId === null) {
          // 成功信封里没有运行 id：不能当成受理成功，也不能当成未受理。
          if (stillMine()) {
            replace(stateRef.current, { phase: "acceptance_unknown", op });
            setNotice("受理结果无法确认。请用「确认受理结果」核对；这里不会重发。");
          }
          return null;
        }
        if (!mountedRef.current) return runId;
        if (stillMine()) {
          // 记录与锁都在这里建立：即使期间配置变了，这次受理也是真实的。
          setRecords((previous) =>
            [
              { runId, environmentId: op.submission.environmentId, submittedAt: Date.now() },
              ...previous.filter((item) => item.runId !== runId),
            ].slice(0, 20),
          );
          // 202 只表示已受理／排队：锁交给 running 持有，直到这条运行的终态。
          replace(stateRef.current, {
            phase: "running",
            op,
            runId,
            paused: false,
            cancelState: "none",
          });
          void reportAbortFor(runId);
          void loadReport(runId);
          onRunAcceptedRef.current?.(runId);
        }
        return runId;
      } catch (cause) {
        if (!mountedRef.current) return null;
        if (isAcceptanceUnknown(cause)) {
          // 受理结果不明：保留原键与原内容，只允许用同一个键再确认一次。
          if (stillMine()) {
            replace(stateRef.current, { phase: "acceptance_unknown", op });
            setNotice("受理结果不明。请求可能已经到达服务端，请用「确认受理结果」核对；这里不会重发。");
          }
          return null;
        }
        if (mode === "reconcile") {
          // 确认受理时的 403／409 等**不能**证明原运行不存在：它们同样可能来自幂等记录
          // 已过期、权限在等待期间被收回、或请求内容与首次不同的原因码，与“第一次到底
          // 受理没有”无关。因此这里保留原操作与原键，只把原因显示出来；一旦退回空闲，
          // 用户点普通发送就会用新键产生第二次业务请求。
          if (stillMine()) {
            replace(stateRef.current, { phase: "acceptance_unknown", op });
            setError(
              `确认受理结果未成功：${cause instanceof Error ? cause.message : "未知原因"}。` +
                "这不代表上次没有受理；原请求内容与幂等键已保留，可再次确认或联系管理员。",
            );
          }
          return null;
        }
        // 初次提交收到服务端明确拒绝（非 5xx 的 ApiError）：请求被处理过，结果是确定的
        // 失败，不占用幂等键。5xx 与损坏的成功信封都已在上面按“受理不明”处理。
        if (stillMine()) {
          toIdle();
          setError(cause instanceof Error ? cause.message : "提交调试失败");
        }
        return null;
      }
    },
    [base, canStartNewWrite, loadReport, owns, replace, toIdle],
  );

  /**
   * 用户确认“授权并发送”。
   *
   * 同步比较“同一个操作且阶段是 awaiting_authorization”，并在第一个 await 之前转入
   * authorizing：同帧的第二次确认因此立即返回，不会签发第二份授权。
   *
   * 摘要由服务端按冻结内容算出，用户不接触内部摘要；签发给**本人**，随后提交的是同一份
   * 冻结内容——三步串起来的意义正是“授权绑定的就是将要发出去的那份内容”。
   */
  const confirmAuthorization = useCallback(
    async (): Promise<string | null> => {
      const before = stateRef.current;
      if (before.phase !== "awaiting_authorization") return null;
      const op = before.op;
      if (!owns(op)) return null;
      // 已提交服务器之前的内容才算“可继续”：配置变了就要求重新发送。
      if (!canStartNewWrite(op)) {
        toIdle();
        setNotice("内容或环境已变化，本次发送已取消；请按当前内容重新发送。");
        return null;
      }
      if (!replace(before, { phase: "authorizing", op })) return null;
      setError(null);

      const stillAuthorizing = (): boolean => {
        const now = stateRef.current;
        return mountedRef.current && now.phase === "authorizing" && now.op.token === op.token;
      };

      try {
        const snapshot = { request: op.submission.request, assertions: op.submission.assertions };
        const hash = await apiSend(`${base}/debug-snapshot-digest`, "POST", snapshot, toDebugSnapshotDigest);
        if (!stillAuthorizing()) return null;
        if (!owns(op)) return null;
        // 摘要返回与签发之间隔着一次网络往返。配置时钟可能**已经**变化而 React props
        // 还没更新（管理面板刚保存成功）：此时按旧配置签发，授权绑定的就是一份已经被
        // 改掉的输入。读同步时钟复核，不满足就撤销这次未提交的确认——零 grant、零 run。
        if (!canStartNewWrite(op)) {
          toIdle();
          setNotice("内容或配置已变化，本次授权已取消；请按当前内容重新发送。");
          return null;
        }
        // 与后端约定一致的短期有效期；它不是凭证本身的有效期，只是这份用途授权的时限。
        const expiresAt = new Date(
          Date.now() + AUTHORIZATION_TTL_MINUTES * 60_000,
        ).toISOString();
        await apiSend(
          `${base}/credentials/grants`,
          "POST",
          {
            profile_id: op.profileId,
            grant_type: "debug_snapshot",
            principal_id: op.owner.principalId,
            debug_snapshot_hash: hash,
            allowed_targets: [],
            allowed_auth_slots: [],
            allowed_inputs: {},
            expires_at: expiresAt,
          },
          (raw) => raw,
        );
      } catch (cause) {
        if (stillAuthorizing()) {
          toIdle();
          setError(cause instanceof Error ? cause.message : "授权失败，未发送请求");
        }
        return null;
      }
      if (!stillAuthorizing()) return null;
      // 授权成功后立刻提交：中间的编辑不会改变已冻结的内容。
      return submitForward(op, "initial");
    },
    [base, canStartNewWrite, owns, replace, submitForward, toIdle],
  );

  /**
   * 用户取消授权确认。
   *
   * 只丢弃本地操作，**不创建运行**、不签发授权；此时还没有任何写请求发出，因此不存在
   * “已受理”。
   */
  const cancelOperation = useCallback(() => {
    const current = stateRef.current;
    if (current.phase !== "awaiting_authorization" && current.phase !== "authorizing") return;
    toIdle();
    setNotice("已取消本次授权，未创建运行。");
  }, [toIdle]);

  /**
   * 确认受理结果：用**原键与原内容**再问一次“刚才那次受理了没有”。
   *
   * 不重新签发授权、不换键、不读新草稿。确认失败也不证明第一次没有受理，因此继续保留
   * unknown 状态。
   */
  const retryAcceptance = useCallback(async (): Promise<string | null> => {
    const current = stateRef.current;
    if (current.phase !== "acceptance_unknown") return null;
    return submitForward(current.op, "reconcile");
  }, [submitForward]);

  /**
   * 停止等待。
   *
   * 各阶段语义不同，都不谎称服务端已取消：
   * - 预检／授权中：作废操作，不会创建运行（已签发的授权不假称已撤回）；
   * - 提交在飞：中止本地等待并转入 unknown，**保留**原键与原内容；
   * - 受理不明：保留原键与原内容，提示走确认入口；
   * - 运行中：只暂停本地轮询，保留锁与取消能力，可恢复等待。
   */
  const stopWaiting = useCallback(() => {
    const current = stateRef.current;
    if (current.phase === "idle") return;
    if (current.phase === "submitting") {
      // 顺序很重要：先同步作废本次尝试并转入 unknown，再中止本地等待。
      // 反过来的话，迟到的成功回调可能先看到“仍是当前尝试”而把状态改成 running。
      attemptRef.current += 1;
      if (!replace(stateRef.current, { phase: "acceptance_unknown", op: current.op })) return;
      submitAbortRef.current?.abort();
      setNotice("已停止等待受理结果。请求可能已经到达服务端，请用「确认受理结果」核对；这里不会重发。");
      return;
    }
    if (current.phase === "acceptance_unknown") {
      setNotice("本次受理结果仍未确认；请用「确认受理结果」核对，这里不会重发。");
      return;
    }
    if (current.phase === "running") {
      replace(stateRef.current, { ...current, paused: true });
      setNotice("已暂停等待该运行的结果；运行仍在服务端继续，可随时恢复等待或取消。");
      return;
    }
    // preflighting / awaiting_authorization / authorizing
    stateRef.current = { phase: "idle" };
    setState({ phase: "idle" });
    setNotice("已停止等待，未创建运行。");
  }, [replace]);

  /** 恢复等待：只读同一条运行，不创建新运行。 */
  const resumeWaiting = useCallback(() => {
    const current = stateRef.current;
    if (current.phase !== "running" || !current.paused) return;
    replace(stateRef.current, { ...current, paused: false });
    setNotice(null);
  }, [replace]);

  /**
   * 取消服务端运行：只对**已知 run_id** 生效。
   *
   * 与报告无关——报告读失败、还没读回来，都不影响取消已知运行；反过来，没拿到 run_id
   * 时也不能假装服务端执行已停止。取消成功只说明“已请求”，仍等服务端终态才释放锁。
   */
  const cancel = useCallback(
    async (runId: string): Promise<void> => {
      const current = stateRef.current;
      // 同一运行的重复取消在同步状态里挡掉。
      if (
        current.phase === "running" &&
        current.runId === runId &&
        current.cancelState !== "none"
      ) {
        return;
      }
      if (current.phase === "running" && current.runId === runId) {
        replace(stateRef.current, { ...current, cancelState: "requesting" });
      }
      setError(null);
      try {
        await apiSend(`${base}/runs/${runId}/cancel`, "POST", undefined, (raw) => raw);
        if (!mountedRef.current) return;
        const now = stateRef.current;
        if (now.phase === "running" && now.runId === runId) {
          replace(stateRef.current, { ...now, cancelState: "requested" });
        }
        setNotice("已请求取消该运行；已经发出的请求不会因此撤回。");
      } catch (cause) {
        if (!mountedRef.current) return;
        const now = stateRef.current;
        if (now.phase === "running" && now.runId === runId) {
          replace(stateRef.current, { ...now, cancelState: "none" });
        }
        setError(cause instanceof Error ? cause.message : "取消失败");
      }
    },
    [base, replace],
  );

  const selectedRunId = activeRunId;
  const selectedRecord = useMemo(
    () => records.find((item) => item.runId === selectedRunId) ?? null,
    [records, selectedRunId],
  );

  /** 当前可见的预检结论：stamp 与实时依据一致才显示。 */
  const visiblePreview =
    preview.value !== null && sameStamp(preview.stamp, liveStamp) ? preview.value : null;
  const visiblePreviewError =
    previewError !== null && sameStamp(previewError.stamp, liveStamp) ? previewError.message : null;

  /** 授权确认面板的完整冻结摘要；没有待确认操作时为 null。 */
  const authorizationView: AuthorizationView | null =
    state.phase === "awaiting_authorization"
      ? {
          environmentId: state.op.submission.environmentId,
          environmentLabel: state.op.environmentLabel,
          method: state.op.submission.request.method,
          path: state.op.submission.request.path,
          profileId: state.op.profileId,
          principalId: state.op.owner.principalId,
          ttlMinutes: AUTHORIZATION_TTL_MINUTES,
        }
      : null;

  return {
    // —— 展示性预检 ——
    preflight: visiblePreview,
    /** 当前预检结论对应的内容键；调用方与当前内容的键比对即可判断是否已失效。 */
    preflightFor: sameStamp(preview.stamp, liveStamp) ? preview.stamp?.inputKey ?? null : null,
    preflightError: visiblePreviewError,
    preflighting: checking,

    // —— 阶段 ——
    phase: state.phase,
    /**
     * 有未结束的操作：界面据此锁住普通发送入口，并纳入离开保护。
     *
     * `acceptance_unknown` 也算未结束——它是一条可能已经受理的请求，不能因为阶段名字里
     * 没有“运行”就当成空闲。
     */
    operationActive: state.phase !== "idle",
    acceptanceUnknown: state.phase === "acceptance_unknown",
    activeRunId,
    paused,
    resumeWaiting,
    authorizationView,

    // —— 动作 ——
    runPreflight,
    start,
    confirmAuthorization,
    cancelOperation,
    retryAcceptance,
    stopWaiting,
    cancel,

    // —— 数据 ——
    records,
    selectedRunId,
    selectedRecord,
    /** 按 run_id 缓存的报告；显示层只取与当前选择匹配的那一条。 */
    reports,
    reportErrors,
    reloadReport: (runId: string | null) =>
      runId === null ? Promise.resolve() : loadReport(runId),
    notice,
    error,
    setError,
    setNotice,
  };
}

/** 两个所有者是否完全相同。 */
function sameOwner(left: Owner, right: Owner): boolean {
  return (
    left.editorKey === right.editorKey &&
    left.workspaceId === right.workspaceId &&
    left.projectId === right.projectId &&
    left.principalId === right.principalId
  );
}

/** 两个配置依据是否完全相同；null 表示“当前没有合法内容”，一律不相等。 */
function sameStamp(left: InputStamp | null, right: InputStamp | null): boolean {
  if (left === null || right === null) return false;
  return (
    left.ownerKey === right.ownerKey &&
    left.inputKey === right.inputKey &&
    left.configEpoch === right.configEpoch
  );
}
