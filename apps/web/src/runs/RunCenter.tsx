import { useEffect, useMemo, useRef, useState } from "react";

import { ApiError, apiSend, projectPath } from "../api/client";
import type { Environment } from "../api/types";
import { ErrorText, Hint, Loading } from "../components/Feedback";
import { ReportView } from "./RunPanel";
import { isTerminal, runOutcomeLabel, runReasonLabel, runStateLabel, runTargetLabel, runTimeLabel, useRunReport, useRuns } from "./useRuns";

export function RunCenter({
  workspaceId,
  projectId,
  environments,
  canCancel,
  mode,
  active,
  onOpenReport,
  reportRequest = null,
}: {
  workspaceId: string;
  projectId: string;
  environments: Environment[];
  canCancel: boolean;
  mode: "tasks" | "reports";
  active: boolean;
  onOpenReport?: (runId: string) => void;
  reportRequest?: {
    runId: string;
    token: number;
    workspaceId: string;
    projectId: string;
  } | null;
}) {
  const [environmentId, setEnvironmentId] = useState<string | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const consumedReportToken = useRef<number | null>(null);
  const runs = useRuns(workspaceId, projectId, environmentId, active);
  const report = useRunReport(workspaceId, projectId, active && mode === "reports" ? selectedRunId : null);
  const selected = useMemo(
    () => runs.data?.find((item) => item.id === selectedRunId) ?? null,
    [runs.data, selectedRunId],
  );

  useEffect(() => {
    setEnvironmentId(null);
    setSelectedRunId(null);
    setActionError(null);
    consumedReportToken.current = null;
  }, [workspaceId, projectId]);

  useEffect(() => {
    if (!active || mode !== "reports" || reportRequest === null) return;
    if (reportRequest.workspaceId !== workspaceId || reportRequest.projectId !== projectId) return;
    if (consumedReportToken.current === reportRequest.token) return;
    consumedReportToken.current = reportRequest.token;
    setSelectedRunId(reportRequest.runId);
  }, [active, mode, projectId, reportRequest, workspaceId]);

  async function cancel(runId: string) {
    setActionError(null);
    try {
      await apiSend(projectPath(workspaceId, projectId, `/runs/${runId}/cancel`), "POST", undefined, (raw) => raw);
      runs.reload();
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : "取消失败");
    }
  }

  const title = mode === "tasks" ? "任务中心" : "测试报告";
  const description = mode === "tasks"
    ? "查看服务端最近返回的运行状态，并取消仍在执行的任务。"
    : "按环境查看最近运行的脱敏报告；这里的筛选不会改变工作台当前执行环境。";

  return (
    <main className="content-page" aria-labelledby={`${mode}-title`}>
      <header className="page-title-row">
        <div>
          <span className="eyebrow">当前项目</span>
          <h2 id={`${mode}-title`}>{title}</h2>
          <p className="caption">{description}</p>
        </div>
        <div className="page-tools">
          <label htmlFor={`${mode}-environment`}>环境筛选</label>
          <select
            id={`${mode}-environment`}
            value={environmentId ?? ""}
            onChange={(event) => {
              setEnvironmentId(event.target.value || null);
              setSelectedRunId(null);
            }}
          >
            <option value="">全部环境</option>
            {environments.map((environment) => (
              <option key={environment.id} value={environment.id}>{environment.name}</option>
            ))}
          </select>
          <button type="button" onClick={runs.reload} disabled={runs.loading}>刷新</button>
        </div>
      </header>

      <section className="block run-center-card">
        {actionError ? <ErrorText message={actionError} /> : null}
        {runs.error ? <ErrorText message={runs.error.message} /> : null}
        {runs.loading && runs.data === null ? <Loading label="正在加载运行记录…" /> : null}
        {runs.data?.length === 0 ? (
          <Hint>当前筛选下没有最近运行记录。列表范围以服务端当前返回为准。</Hint>
        ) : null}
        {runs.data && runs.data.length > 0 ? (
          <div className="table-scroll">
            <table className="report-table">
              <thead>
                <tr>
                  <th>运行</th><th>来源</th><th>环境</th><th>状态</th><th>结果</th><th>提交时间</th><th>操作</th>
                </tr>
              </thead>
              <tbody>
                {runs.data.map((run) => (
                  <tr key={run.id} className={run.id === selectedRunId ? "row-active" : undefined}>
                    <td>{run.id.slice(0, 8)}</td>
                    <td>{runTargetLabel(run)}</td>
                    <td>{environments.find((item) => item.id === run.environment_id)?.name ?? run.environment_id.slice(0, 8)}</td>
                    <td>{runStateLabel(run)}</td>
                    <td>{runOutcomeLabel(run)}{runReasonLabel(run) ? `（${runReasonLabel(run)}）` : ""}</td>
                    <td><time dateTime={run.created_at} title={run.created_at}>{runTimeLabel(run.created_at)}</time></td>
                    <td className="inline-actions">
                      <button
                        type="button"
                        onClick={() => mode === "reports" ? setSelectedRunId(run.id) : onOpenReport?.(run.id)}
                      >查看报告</button>
                      {mode === "tasks" && canCancel && !isTerminal(run) ? (
                        <button type="button" className="danger" onClick={() => void cancel(run.id)}>取消</button>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>

      {mode === "reports" && selectedRunId !== null ? (
        <section className="block run-report">
          <h3>运行 {selectedRunId.slice(0, 8)} 的报告</h3>
          {selected && !isTerminal(selected) ? <Hint>运行尚未结束，报告会随服务端状态刷新。</Hint> : null}
          {report.error ? <ErrorText message={report.error.message} /> : null}
          {report.loading && report.data === null ? <Loading label="正在加载报告…" /> : null}
          {report.data ? <ReportView report={report.data} /> : null}
        </section>
      ) : null}
    </main>
  );
}
