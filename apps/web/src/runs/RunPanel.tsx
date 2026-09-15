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
import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiError, apiSend, projectPath } from "../api/client";
import type { CaseVersion, RunReport } from "../api/types";
import { ErrorText, Hint, Loading, StatusTag } from "../components/Feedback";
import { describeValue } from "../api/literals";
import { isTerminal, runOutcomeLabel, runReasonLabel, runStateLabel, useRunReport, useRuns } from "./useRuns";

/** 一次版本提交的执行配置依据：提交**之前**冻结，受理后原样登记。 */
export interface RunProvenance {
  environmentId: string;
  configEpoch: number;
}

function toRunId(raw: unknown): string | null {
  if (typeof raw !== "object" || raw === null) return null;
  const id = (raw as Record<string, unknown>).id;
  return typeof id === "string" ? id : null;
}

function ReportView({ report }: { report: RunReport }) {
  return (
    <div className="report">
      <p>
        <strong>最终结果：</strong>
        {runOutcomeLabel(report.run)}
        {runReasonLabel(report.run) ? `（${runReasonLabel(report.run)}）` : ""}
      </p>

      <h4>步骤</h4>
      {report.steps.length === 0 ? (
        <Hint>还没有步骤记录，工作项可能仍在排队。</Hint>
      ) : (
        <ul className="caption">
          {report.steps.map((step) => (
            <li key={`${step.step_key}-${step.attempt_no}`}>
              第 {step.attempt_no} 次 · {step.state}
              {step.outcome ? ` · ${step.outcome}` : ""}
              {step.elapsed_ms !== null ? ` · ${step.elapsed_ms} 毫秒` : ""}
              {step.error_code ? ` · ${step.error_code}` : ""}
            </li>
          ))}
        </ul>
      )}

      <h4>断言结果</h4>
      {report.assertions.length === 0 ? (
        <Hint>本次运行没有断言结果记录。</Hint>
      ) : (
        <table className="report-table">
          <thead>
            <tr>
              <th>断言</th>
              <th>阶段</th>
              <th>结果</th>
              <th>期望</th>
              <th>实际</th>
              <th>说明</th>
            </tr>
          </thead>
          <tbody>
            {report.assertions.map((item) => (
              <tr key={`${item.assertion_id}-${item.phase}`}>
                <td>{item.type}</td>
                <td>{item.phase === "pre_request" ? "发送前" : "响应后"}</td>
                <td>
                  <StatusTag status={item.status} />
                </td>
                <td>{describeValue(item.expected)}</td>
                <td>{describeValue(item.actual)}</td>
                <td>{item.reason_code ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <details>
        <summary>脱敏后的请求与响应证据</summary>
        <pre className="evidence">{JSON.stringify(report.request, null, 2)}</pre>
        <pre className="evidence">{JSON.stringify(report.response, null, 2)}</pre>
        <p className="caption">证据中的凭证值已由服务端遮蔽，页面不保存任何密钥。</p>
      </details>
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
}) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [accepted, setAccepted] = useState<string | null>(null);

  const runs = useRuns(workspaceId, projectId, environmentId);
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
      const run = await apiSend(
        projectPath(workspaceId, projectId, "/runs"),
        "POST",
        { environment_id: environmentId, case_version_id: version.id },
        toRunId,
      );
      // 不写“正在等待领取”这类静态预测：真实阶段与结果由上方响应区和本列表按服务端
      // 状态呈现，长期挂着的 banner 只会与已经结束的运行自相矛盾。
      setAccepted(
        run
          ? `已提交运行（编号 ${run.slice(0, 8)}），执行已发布版本 v${version.version}。`
          : "已提交运行。",
      );
      if (run !== null) {
        onSelectRun(run);
        // 传的是提交前捕获的那一份，不是此刻重新读的配置。
        onRunSubmitted(run, provenance);
      }
      runs.reload();
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : cause instanceof Error ? cause.message : "提交运行失败");
    } finally {
      setBusy(false);
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
      <h2>已发布版本执行与项目历史</h2>
      <p className="caption">
        这里固定**已发布版本**：需要先保存（内容变了还要发布）。调试当前编辑内容请用页面
        顶部的「发送」，它不写用例、不产生版本。
      </p>
      <div className="actions">
        <button
          type="button"
          onClick={() => void startRun()}
          disabled={busy || caseId === null || readOnly}
        >
          {busy ? "提交中…" : "保存并执行"}
        </button>
        <button type="button" onClick={runs.reload} disabled={runs.loading}>
          刷新运行列表
        </button>
      </div>
      {readOnly || !canCancel ? (
        <Hint>当前角色只能查看历史报告，不能提交或取消运行。</Hint>
      ) : null}
      {caseId === null ? <Hint>请先创建并保存用例，再提交运行。</Hint> : null}
      {accepted ? <Hint>{accepted}</Hint> : null}
      {error ? <ErrorText message={error} /> : null}
      {runs.error ? <ErrorText message={runs.error.message} /> : null}

      {runs.loading && runs.data === null ? <Loading label="正在加载运行记录…" /> : null}
      {runs.data && runs.data.length === 0 ? (
        <Hint>当前环境还没有运行记录。提交执行后，这里会显示排队、执行与结果。</Hint>
      ) : null}

      {runs.data && runs.data.length > 0 ? (
        <table className="report-table">
          <thead>
            <tr>
              <th>运行</th>
              <th>状态</th>
              <th>结果</th>
              <th>提交时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {runs.data.map((run) => (
              <tr key={run.id} className={run.id === selectedRunId ? "row-active" : undefined}>
                <td>{run.id.slice(0, 8)}</td>
                <td>{runStateLabel(run)}</td>
                <td>
                  {runOutcomeLabel(run)}
                  {runReasonLabel(run) ? `（${runReasonLabel(run)}）` : ""}
                </td>
                <td>{run.created_at}</td>
                <td className="inline-actions">
                  <button type="button" onClick={() => onSelectRun(run.id)}>
                    查看报告
                  </button>
                  {isTerminal(run) || readOnly || !canCancel ? null : (
                    <button type="button" onClick={() => void cancelRun(run.id)}>
                      取消
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}

      {selectedRunId ? (
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
