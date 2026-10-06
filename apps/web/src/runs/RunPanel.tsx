/**
 * 已发布版本的执行与项目／环境历史。
 *
 * 这是**次级操作**：它固定的是已发布用例版本，需要先保存（必要时发布），与工作台
 * 顶部“发送当前编辑内容”的临时调试不是同一条路。两者的结论互不借用：调试授权不会
 * 被用来跑版本，版本运行也不会贴到未保存的内容上。
 *
 * 页面只展示服务端持久化的运行与结果，不根据本地状态推断“已完成”。入参失败不发请求，
 * 网络失败与断言失败在报告里是不同类别，这里按类别如实呈现。
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Button, Collapse, Descriptions, Empty, Space, Table, Tag } from "antd";

import { ApiError, apiSend, projectPath } from "../api/client";
import type { CaseVersion, RunReport, RunSummary } from "../api/types";
import { toResolutionPreview } from "../api/guards";
import { ErrorText, Hint, Loading, StatusTag } from "../components/Feedback";
import { describeValue, literalToInput } from "../api/literals";
import { isTerminal, runOutcomeLabel, runReasonLabel, runStateLabel, runTimeLabel, stepOutcomeLabel, stepStateLabel, uncheckedReasonLabel, useRunReport, useRuns } from "./useRuns";
import { isInitialRunRejection } from "./runAcceptance";

/** 一次版本提交的执行配置依据：提交**之前**冻结，受理后原样登记。 */
export interface RunProvenance {
  environmentId: string;
  configEpoch: number;
  contextFingerprint?: string | null;
}

function toRunId(raw: unknown): string | null {
  if (typeof raw !== "object" || raw === null) throw new Error("运行受理响应损坏：缺少运行编号");
  const id = (raw as Record<string, unknown>).id;
  if (typeof id !== "string" || id === "") throw new Error("运行受理响应损坏：缺少运行编号");
  return id;
}

function sourceValueLabel(value: import("../api/types").ValueLiteral | null): string {
  if (value === null) return "（受保护或不可用）";
  const parsed = literalToInput(value);
  if (parsed === null) return "（无法显示）";
  return parsed.type === "null" ? "null" : parsed.text;
}

export function ReportView({ report }: { report: RunReport }) {
  return (
    <div className="report">
      <Descriptions size="small" column={1} items={[{ key: "outcome", label: "最终结果", children: `${runOutcomeLabel(report.run)}${runReasonLabel(report.run) ? `（${runReasonLabel(report.run)}）` : ""}` }]} />

      <h4>步骤</h4>
      {report.steps.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={isTerminal(report.run) ? "没有已保存的步骤记录" : "还没有步骤记录，工作项可能仍在排队"}
        />
      ) : (
        <ul className="caption">
          {report.steps.map((step) => {
            const displayedOutcome = step.interpretation?.outcome ?? step.outcome;
            const isUnchecked = displayedOutcome === "completed_unchecked";
            return (
              <li key={`${step.step_key}-${step.attempt_no}`}>
                第 {step.attempt_no} 次 · {stepStateLabel(step.state)}
                {stepOutcomeLabel(displayedOutcome) ? ` · ${stepOutcomeLabel(displayedOutcome)}` : ""}
                {step.elapsed_ms !== null ? ` · ${step.elapsed_ms} 毫秒` : ""}
                {step.outcome === null && isTerminal(report.run)
                  ? " · 该尝试未保存最终结论"
                  : ""}
                {isUnchecked ? ` · ${uncheckedReasonLabel(step.error_code)}` : step.error_code ? ` · ${step.error_code}` : ""}
                {step.interpretation ? (
                  <>
                    <Tag color="warning">历史记录兼容解释</Tag>
                    <Collapse
                      size="small"
                      items={[{
                        key: "recorded-outcome",
                        label: "查看原始记录",
                        children: `原始结果：${stepOutcomeLabel(step.outcome) ?? "无最终结果"}${step.error_code ? `；原始原因：${step.error_code}` : ""}`,
                      }]}
                    />
                  </>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}

      <h4>断言结果</h4>
      {report.assertions.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="本次运行没有断言结果记录" />
      ) : (
        <Table
          className="report-table"
          size="small"
          pagination={false}
          rowKey={(item) => `${item.assertion_id}-${item.phase}`}
          dataSource={report.assertions}
          columns={[
            { title: "断言", dataIndex: "type" },
            { title: "阶段", render: (_: unknown, item: RunReport["assertions"][number]) => item.phase === "pre_request" ? "发送前" : "响应后" },
            { title: "结果", render: (_: unknown, item: RunReport["assertions"][number]) => <StatusTag status={item.status} /> },
            { title: "期望", render: (_: unknown, item: RunReport["assertions"][number]) => describeValue(item.expected) },
            { title: "实际", render: (_: unknown, item: RunReport["assertions"][number]) => describeValue(item.actual) },
            { title: "说明", render: (_: unknown, item: RunReport["assertions"][number]) => item.reason_code ?? "" },
          ]}
        />
      )}

      <h4>冻结变量来源</h4>
      {report.resolution == null ? <Hint>该运行没有记录 S1 变量来源；不会用当前配置补造历史。</Hint> : (
        <Table
          size="small"
          pagination={false}
          rowKey="name"
          dataSource={report.resolution.variable_sources}
          columns={[
            { title: "变量", dataIndex: "name" },
            { title: "实际来源", render: (_: unknown, item: NonNullable<RunReport["resolution"]>["variable_sources"][number]) => `${item.source.level === "environment" ? "环境" : "项目"}第 ${item.source.revision} 版：${sourceValueLabel(item.source.value)}` },
            { title: "被覆盖来源", render: (_: unknown, item: NonNullable<RunReport["resolution"]>["variable_sources"][number]) => item.overridden_sources.length === 0 ? "无" : item.overridden_sources.map((source) => `${source.level === "environment" ? "环境" : "项目"}第 ${source.revision} 版：${sourceValueLabel(source.value)}`).join("；") },
          ]}
        />
      )}

      <Collapse items={[{ key: "evidence", label: "脱敏后的请求与响应证据", children: <>
        <pre className="evidence">{JSON.stringify(report.request, null, 2)}</pre>
        <pre className="evidence">{JSON.stringify(report.response, null, 2)}</pre>
        <p className="caption">证据中的凭证值已由服务端遮蔽，页面不保存任何密钥。</p>
      </> }]} />
    </div>
  );
}

export function RunPanel({
  workspaceId,
  projectId,
  environmentId,
  caseId,
  needsVersion,
  publishedVersion,
  onEnsureVersion,
  onReport,
  selectedRunId,
  onSelectRun,
  onRunSubmitted,
  captureProvenance,
  canCancel = false,
  readOnly = false,
  showHistory = true,
  operationBlocked = false,
  onOperationActive,
  tryAcquireOperation,
  releaseOperation,
}: {
  workspaceId: string;
  projectId: string;
  environmentId: string | null;
  caseId: string | null;
  /**
   * 执行前是否必须先走一次“保存并确保版本”。
   *
   * 有未保存改动，或屏幕上这份内容还没有对应版本时为真。为真不等于会新增版本：
   * 目录这类不属于执行快照的改动保存后会复用内容一致的已有版本。
   */
  needsVersion: boolean;
  publishedVersion: CaseVersion | null;
  onEnsureVersion: () => Promise<CaseVersion | null>;
  /** 后台到达的报告：只更新缓存，**不改变**查看选择。 */
  onReport: (report: RunReport) => void;
  /**
   * 当前被选中的历史运行；由调用方持有，面板是受控的。
   *
   * 面板自己再存一份“我在看哪条”会造成两个真相：点选与报告到达分别在两边改状态，
   * 正文与字段区就可能来自不同的运行。
   */
  selectedRunId: string | null;
  /** 用户点选某条记录：这是**显式**的来源切换事件。 */
  onSelectRun: (runId: string) => void;
  /**
   * 版本运行被受理（202）时上报：`runId` 与该次提交**之前**捕获的执行配置依据。
   *
   * 只有从这里（以及调试自己的受理）才能得到“这条运行的执行配置依据”。历史列表里的
   * 报告只说明它曾经跑过，不能证明它按**当前**配置跑过，因此不能凭它贴当前结论。
   *
   * `provenance` 必须是在**发起 POST 之前**捕获的那一份，调用方不得在此刻重新读取配置。
   */
  onRunSubmitted: (runId: string, provenance: RunProvenance) => void;
  /**
   * 捕获本次提交的执行配置依据；返回 null 表示当前范围已失效，不应发起写请求。
   *
   * 必须在 `POST /runs` **之前**调用：受理响应要等一次网络往返，期间配置可能已经被改。
   * 等响应回来再读配置，会把“提交时的配置”记成“受理时的配置”，于是一条按旧配置跑的
   * 运行被当成按新配置跑的，旧结论又贴回当前。
   */
  captureProvenance: () => RunProvenance | null;
  /**
   * 当前角色是否可以取消运行。
   *
   * 查看者能看未结束的历史运行，但没有取消能力。守卫要在这里就把按钮去掉，而不是让
   * 用户点下去收一个 403。
   */
  canCancel?: boolean;
  /** 只读角色：可以看历史与报告，不能提交版本执行或取消。 */
  readOnly?: boolean;
  /** 独立任务／报告页已承接历史时，只保留版本执行动作。 */
  showHistory?: boolean;
  operationBlocked?: boolean;
  onOperationActive?: (active: boolean) => void;
  tryAcquireOperation?: () => boolean;
  releaseOperation?: () => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [accepted, setAccepted] = useState<string | null>(null);
  const [unknown, setUnknown] = useState<{
    key: string;
    payload: { environment_id: string; case_version_id: string; resolution_context: string };
    version: CaseVersion;
    provenance: RunProvenance;
  } | null>(null);
  const localOperation = useRef(false);

  useLayoutEffect(() => onOperationActive?.(busy || unknown !== null), [busy, unknown, onOperationActive]);
  useLayoutEffect(() => () => onOperationActive?.(false), [onOperationActive]);

  const runs = useRuns(workspaceId, projectId, environmentId, showHistory);
  // 独立历史页只替代列表呈现；当前编辑器刚提交的版本运行仍要把报告回填给响应区。
  const report = useRunReport(workspaceId, projectId, selectedRunId);

  useEffect(() => {
    if (report.data) onReport(report.data);
  }, [report.data, onReport]);

  const selected = useMemo(
    () => runs.data?.find((item) => item.id === selectedRunId) ?? null,
    [runs.data, selectedRunId],
  );

  async function startRun() {
    setError(null);
    setAccepted(null);
    if (operationBlocked || unknown !== null || localOperation.current) return;
    if (environmentId === null) {
      setError("请先选择执行环境。");
      return;
    }
    // 在**任何写请求之前**固定本次的执行配置依据——包括确保版本那一步（它自己也会写）。
    const provenance = captureProvenance();
    if (provenance === null) {
      setError("当前范围或执行配置已变化，请确认后重新提交。");
      return;
    }
    if (tryAcquireOperation && !tryAcquireOperation()) return;
    localOperation.current = true;
    let keepOperation = false;
    setBusy(true);
    try {
      // 执行固定已发布版本：草稿变化不会影响本次运行，也不会有中间态请求被发出去。
      // 有未保存改动、或屏幕上这份内容还没有对应版本时，必须先走一次“保存并确保版本”；
      // 否则会把改过（或只改过目录）的用例执行在旧版本上，而运行本身不会报任何异常。
      const version = needsVersion ? await onEnsureVersion() : publishedVersion;
      if (version === null) {
        setError("发布未完成，未提交运行。");
        return;
      }
      const currentProvenance = captureProvenance();
      if (
        currentProvenance === null ||
        currentProvenance.environmentId !== provenance.environmentId ||
        currentProvenance.configEpoch !== provenance.configEpoch
      ) {
        setError("保存或版本准备期间执行环境／配置已变化；已保存成果保留，本次没有继续提交运行。");
        return;
      }
      const preview = await apiSend(
        projectPath(workspaceId, projectId, "/resolution-preview"),
        "POST",
        { environment_id: environmentId, case_version_id: version.id },
        toResolutionPreview,
      );
      if (!preview.ready || preview.ordinary_resolution !== "ready" || preview.resolution_context === null) {
        setError(preview.issues[0]?.message ?? "该已发布版本未取得可提交的解析依据；请修正环境或变量配置后重试。");
        return;
      }
      const afterPreview = captureProvenance();
      if (
        afterPreview === null ||
        afterPreview.environmentId !== provenance.environmentId ||
        afterPreview.configEpoch !== provenance.configEpoch
      ) {
        setError("解析预览期间执行环境／配置已变化；本次没有提交运行，请按当前配置重试。");
        return;
      }
      const pending = {
        key: typeof crypto !== "undefined" && typeof crypto.randomUUID === "function" ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`,
        payload: { environment_id: environmentId, case_version_id: version.id, resolution_context: preview.resolution_context },
        version,
        provenance: { ...provenance, contextFingerprint: preview.context_fingerprint },
      };
      keepOperation = await submitPending(pending, "initial");
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : cause instanceof Error ? cause.message : "提交运行失败");
    } finally {
      setBusy(false);
      if (!keepOperation) {
        localOperation.current = false;
        releaseOperation?.();
      }
    }
  }

  async function submitPending(pending: NonNullable<typeof unknown>, mode: "initial" | "confirm"): Promise<boolean> {
    try {
      const run = await apiSend(
        projectPath(workspaceId, projectId, "/runs"),
        "POST",
        pending.payload,
        toRunId,
        { headers: { "Idempotency-Key": pending.key } },
      );
      setUnknown(null);
      // 不写“正在等待领取”这类静态预测：真实阶段与结果由上方响应区和本列表按服务端
      // 状态呈现，长期挂着的 banner 只会与已经结束的运行自相矛盾。
      setAccepted(
        run
          ? `已提交运行（编号 ${run.slice(0, 8)}），执行已发布版本 v${pending.version.version}。`
          : "已提交运行。",
      );
      if (run !== null) {
        onSelectRun(run);
        // 传的是提交前捕获的那一份，不是此刻重新读的配置。
        onRunSubmitted(run, pending.provenance);
      }
      runs.reload();
      return false;
    } catch (cause) {
      if (
        mode === "initial" &&
        isInitialRunRejection(cause)
      ) {
        setUnknown(null);
        setError(cause.message);
        return false;
      }
      // 除服务端明确声明“创建运行前已拒绝”的准入错误外，网络、正文解析、损坏信封以及
      // 确认阶段的 403/409 都不能证明首次未受理。
      setUnknown(pending);
      const detail = cause instanceof Error ? `（${cause.message}）` : "";
      setError(`服务端是否已受理暂时未知${detail}。请使用原操作确认；不会更换内容或重复生成操作键。`);
      return true;
    }
  }

  async function cancelRun(runId: string) {
    setError(null);
    try {
      await apiSend(projectPath(workspaceId, projectId, `/runs/${runId}/cancel`), "POST", undefined, (raw) => raw);
      runs.reload();
      if (selectedRunId === runId) report.reload();
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "取消失败");
    }
  }

  return (
    <div className="block">
      <h2>{showHistory ? "已发布版本执行与项目历史" : "执行已发布版本"}</h2>
      <p className="caption">
        这里固定<strong>已发布版本</strong>：需要先保存（内容变了还要发布）。调试当前编辑内容请用页面
        顶部的「发送」，它不写用例、不产生版本。{showHistory ? "最近记录也会显示在下方。" : "提交后的状态请到任务中心查看。"}
      </p>
      <Space className="actions">
        <Button
          htmlType="button"
          type="primary"
          onClick={() => void startRun()}
          disabled={busy || caseId === null || readOnly || operationBlocked || unknown !== null}
        >
          {busy ? "提交中…" : "保存并执行"}
        </Button>
        {showHistory ? (
          <Button htmlType="button" onClick={runs.reload} loading={runs.loading}>刷新运行列表</Button>
        ) : null}
      </Space>
      {readOnly || !canCancel ? (
        <Hint>当前角色只能查看历史报告，不能提交或取消运行。</Hint>
      ) : null}
      {caseId === null ? <Hint>请先创建并保存用例，再提交运行。</Hint> : null}
      {accepted ? <Hint>{accepted}</Hint> : null}
      {unknown ? (
        <div className="notice">
          <p>这次版本运行是否受理仍未知；确认会提交完全相同的版本、环境和操作键。</p>
          <Button htmlType="button" type="primary" disabled={busy} onClick={() => { setBusy(true); void submitPending(unknown, "confirm").then((keep) => { if (!keep) { localOperation.current = false; releaseOperation?.(); } }).catch((cause) => setError(cause instanceof Error ? cause.message : "确认失败")).finally(() => setBusy(false)); }}>确认原操作</Button>
        </div>
      ) : null}
      {error ? <ErrorText message={error} /> : null}
      {showHistory && runs.error ? <ErrorText message={runs.error.message} /> : null}

      {showHistory && runs.loading && runs.data === null ? <Loading label="正在加载运行记录…" /> : null}
      {showHistory && runs.data && runs.data.length === 0 ? (
        <Hint>当前环境还没有运行记录。提交执行后，这里会显示排队、执行与结果。</Hint>
      ) : null}

      {showHistory && runs.data && runs.data.length > 0 ? (
        <Table
          className="report-table"
          size="small"
          pagination={false}
          rowKey="id"
          dataSource={runs.data}
          rowClassName={(run) => run.id === selectedRunId ? "row-active" : ""}
          columns={[
            { title: "运行", render: (_: unknown, run: RunSummary) => run.id.slice(0, 8) },
            { title: "状态", render: (_: unknown, run: RunSummary) => <Tag>{runStateLabel(run)}</Tag> },
            { title: "结果", render: (_: unknown, run: RunSummary) => `${runOutcomeLabel(run)}${runReasonLabel(run) ? `（${runReasonLabel(run)}）` : ""}` },
            { title: "提交时间", render: (_: unknown, run: RunSummary) => <time dateTime={run.created_at} title={run.created_at}>{runTimeLabel(run.created_at)}</time> },
            { title: "操作", render: (_: unknown, run: RunSummary) => <Space size={4}>
              <Button htmlType="button" size="small" onClick={() => onSelectRun(run.id)}>查看报告</Button>
              {isTerminal(run) || readOnly || !canCancel ? null : <Button htmlType="button" size="small" danger aria-label="取消" onClick={() => void cancelRun(run.id)}>取消</Button>}
            </Space> },
          ]}
        />
      ) : null}

      {showHistory && selectedRunId ? (
        <div className="run-report">
          <h3>运行 {selectedRunId.slice(0, 8)} 的报告</h3>
          {selected && !isTerminal(selected) ? (
            <Hint>该运行尚未结束，报告会在完成后自动刷新；已产生的结果也会先显示。</Hint>
          ) : null}
          {report.error ? <ErrorText message={report.error.message} /> : null}
          {report.loading && report.data === null ? <Loading label="正在加载报告…" /> : null}
          {report.data ? <ReportView report={report.data} /> : null}
        </div>
      ) : null}
    </div>
  );
}
