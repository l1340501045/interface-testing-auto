/**
 * 运行列表与报告：运行是服务端持久状态，页面重开后仍可查到。
 *
 * 轮询只在存在未结束运行时开启，结束后自动停止，避免离开页面前一直空转。
 * 运行状态与结果全部来自服务端，前端不推算、不缓存成“看起来已完成”。
 */
import { useEffect } from "react";

import { apiGet, projectPath } from "../api/client";
import { toRunList, toRunReport } from "../api/guards";
import type { RunReport, RunSummary } from "../api/types";
import { useResource } from "../hooks/useResource";

/** 终态只有一个：finished。其余状态都还可能继续推进。 */
export function isTerminal(run: RunSummary): boolean {
  return run.state === "finished";
}

export function runStateLabel(run: RunSummary): string {
  if (run.state === "created") return "已创建";
  if (run.state === "queued") return "排队中";
  if (run.state === "running") return "执行中";
  if (run.state === "finished") return "已结束";
  return run.state;
}

export function runOutcomeLabel(run: RunSummary): string {
  switch (run.outcome) {
    case "passed":
      return "通过";
    case "failed":
      return "断言失败";
    case "completed_unchecked":
      return "入参校验通过，响应未校验";
    case "canceled":
      return "已取消";
    case "interrupted":
      return "结果不明，需人工确认";
    case "timed_out":
      return "超过截止时间";
    case "error":
      return "执行错误";
    case null:
      return "未结束";
    default:
      return run.outcome;
  }
}

/** 失败原因分类：配置错误与业务断言失败要分开看，前者说明断言本身写错了。 */
export function runReasonLabel(run: RunSummary): string | null {
  switch (run.reason_category) {
    case "configuration":
      return "断言或请求配置有误";
    case "assertion":
      return "断言未通过";
    case "policy":
      return "被安全策略阻止";
    case "network":
      return "网络访问失败";
    case null:
      return null;
    default:
      return run.reason_category;
  }
}

const POLL_INTERVAL_MS = 2000;

export function useRuns(
  workspaceId: string | null,
  projectId: string | null,
  environmentId: string | null,
) {
  const key = workspaceId && projectId ? `${workspaceId}/${projectId}#${environmentId ?? "all"}` : null;
  const resource = useResource<RunSummary[]>(key, (signal) => {
    const suffix = environmentId ? `/runs?environment_id=${environmentId}` : "/runs";
    return apiGet(projectPath(workspaceId ?? "", projectId ?? "", suffix), toRunList, signal);
  });
  const { data, reload } = resource;

  useEffect(() => {
    if (!data || data.every(isTerminal)) return;
    const timer = window.setTimeout(reload, POLL_INTERVAL_MS);
    return () => window.clearTimeout(timer);
  }, [data, reload]);

  return resource;
}

export function useRunReport(
  workspaceId: string | null,
  projectId: string | null,
  runId: string | null,
) {
  const key = workspaceId && projectId && runId ? `${workspaceId}/${projectId}/${runId}` : null;
  const resource = useResource<RunReport>(key, (signal) =>
    apiGet(projectPath(workspaceId ?? "", projectId ?? "", `/runs/${runId ?? ""}/report`), toRunReport, signal),
  );
  const { data, reload } = resource;

  // 报告本身也要轮询：提交运行后页面立刻拉到的仍是排队中的快照，不刷新的话
  // 运行早已结束，报告区却一直停在“未结束”。与列表用同一终态判据。
  useEffect(() => {
    if (!data || isTerminal(data.run)) return;
    const timer = window.setTimeout(reload, POLL_INTERVAL_MS);
    return () => window.clearTimeout(timer);
  }, [data, reload]);

  return resource;
}
