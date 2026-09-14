/**
 * 执行与报告面板：提交运行、查看状态与历史、读取脱敏报告。
 *
 * 执行固定在选择的环境与已发布版本上；页面只展示服务端持久化的运行与结果，
 * 不根据本地状态推断“已完成”。入参失败不发请求、网络失败与断言失败在报告中
 * 是不同类别，这里按类别如实呈现。
 */
import { useEffect, useMemo, useState } from "react";

import { ApiError, apiSend, projectPath } from "../api/client";
import type { CaseVersion, RunReport } from "../api/types";
import { ErrorText, Hint, Loading, StatusTag } from "../components/Feedback";
import { describeValue } from "../api/literals";
import { isTerminal, runOutcomeLabel, runReasonLabel, runStateLabel, useRunReport, useRuns } from "./useRuns";

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
  onReport: (report: RunReport) => void;
}) {
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
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
      setAccepted(
        run
          ? `已提交运行（编号 ${run.slice(0, 8)}），执行已发布版本 v${version.version}，由独立执行进程领取。`
          : "已提交运行。",
      );
      setSelectedRunId(run);
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
      <h2>执行与历史</h2>
      <div className="actions">
        <button type="button" onClick={() => void startRun()} disabled={busy || caseId === null}>
          {busy ? "提交中…" : "保存并执行"}
        </button>
        <button type="button" onClick={runs.reload} disabled={runs.loading}>
          刷新运行列表
        </button>
      </div>
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
                  <button type="button" onClick={() => setSelectedRunId(run.id)}>
                    查看报告
                  </button>
                  {isTerminal(run) ? null : (
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
