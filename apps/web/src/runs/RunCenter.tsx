import { useEffect, useMemo, useRef, useState } from "react";
import { Button, Empty, Select, Space, Table, Tag } from "antd";
import type { RunSummary } from "../api/types";

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
          <Select
            id={`${mode}-environment`}
            value={environmentId ?? ""}
            aria-label="环境筛选"
            data-selected-value={environmentId ?? ""}
            onChange={(value: string) => {
              setEnvironmentId(value || null);
              setSelectedRunId(null);
            }}
            options={[{ value: "", label: "全部环境" }, ...environments.map((environment) => ({ value: environment.id, label: environment.name }))]}
          />
          <Button htmlType="button" aria-label="刷新" onClick={runs.reload} loading={runs.loading}>刷新</Button>
        </div>
      </header>

      <section className="block run-center-card">
        {actionError ? <ErrorText message={actionError} /> : null}
        {runs.error ? <ErrorText message={runs.error.message} /> : null}
        {runs.loading && runs.data === null ? <Loading label="正在加载运行记录…" /> : null}
        {runs.data?.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前筛选下没有最近运行记录" />
        ) : null}
        {runs.data && runs.data.length > 0 ? (
          <Table
            className="report-table"
            size="small"
            pagination={false}
            rowKey="id"
            dataSource={runs.data}
            rowClassName={(run) => run.id === selectedRunId ? "row-active" : ""}
            columns={[
              { title: "运行", render: (_: unknown, run: RunSummary) => run.id.slice(0, 8) },
              { title: "来源", render: (_: unknown, run: RunSummary) => runTargetLabel(run) },
              { title: "环境", render: (_: unknown, run: RunSummary) => environments.find((item) => item.id === run.environment_id)?.name ?? run.environment_id.slice(0, 8) },
              { title: "状态", render: (_: unknown, run: RunSummary) => <Tag>{runStateLabel(run)}</Tag> },
              { title: "结果", render: (_: unknown, run: RunSummary) => `${runOutcomeLabel(run)}${runReasonLabel(run) ? `（${runReasonLabel(run)}）` : ""}` },
              { title: "提交时间", render: (_: unknown, run: RunSummary) => <time dateTime={run.created_at} title={run.created_at}>{runTimeLabel(run.created_at)}</time> },
              { title: "操作", render: (_: unknown, run: RunSummary) => <Space size={4}>
                <Button htmlType="button" size="small" onClick={() => mode === "reports" ? setSelectedRunId(run.id) : onOpenReport?.(run.id)}>查看报告</Button>
                {mode === "tasks" && canCancel && !isTerminal(run) ? <Button htmlType="button" size="small" danger aria-label="取消" onClick={() => void cancel(run.id)}>取消</Button> : null}
              </Space> },
            ]}
          />
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
