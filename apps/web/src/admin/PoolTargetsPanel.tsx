/**
 * 当前项目已授权执行池与出网目标白名单。
 *
 * 这里改的是“运行器能访问哪些目标”，属于管理员动作：编辑者只能看，不能自行扩大
 * 出网范围。保存只写配置，服务端不会解析或连接任何目标，因此目标服务停着也能改完。
 * 列表同时给出绑定了该池的环境数量：改之前能看出会影响谁。
 */
import { useEffect, useState } from "react";

import { ApiError, apiSend, projectPath } from "../api/client";
import { toRunnerPool, toRunnerPoolList } from "../api/guards";
import type { RunnerPool } from "../api/types";
import { Empty, ErrorText, Hint, Loading, Notice } from "../components/Feedback";
import { useLeaveReport } from "../hooks/leaveGuard";
import { useResource } from "../hooks/useResource";

export function PoolTargetsPanel({
  workspaceId,
  projectId,
  canAdmin,
}: {
  workspaceId: string;
  projectId: string;
  canAdmin: boolean;
}) {
  const pools = useResource<RunnerPool[]>(`${workspaceId}/${projectId}/pools`, (signal) =>
    apiSend(projectPath(workspaceId, projectId, "/pools"), "GET", undefined, toRunnerPoolList, {
      signal,
    }),
  );

  /**
   * 每个池的白名单草稿。**显示与提交共用同一个来源**（见 textOf）。
   *
   * 草稿只记“用户改过的”，不在数据到达时铺一份副本：铺副本会让“服务端已经变成
   * 别的值”和“用户自己改的”看起来一模一样，既分不出未保存内容，也无法在保存后
   * 干净地回到服务端真相。
   *
   * 这里曾经把文本框绑成 `drafts[pool.id] ?? ""`，而提交读的是 `textOf`：没有草稿
   * 时框里是空的、提交的却是服务端那一整份名单。用户看到空框，填一个新目标就等于
   * 把池上原有的目标全部删掉；保存成功后草稿被清理回服务端值，框里又变回空白，
   * 屏幕上始终看不出池上到底有哪些目标。显示与保存必须来自同一份完整名单。
   */
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [message, setMessage] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  /** 服务端当前的完整名单；草稿与它比较得出脏状态，没有草稿时它就是要提交的内容。 */
  const serverText = (pool: RunnerPool) => pool.allowed_targets.join("\n");
  /** 一个池当前要显示、也要提交的完整名单：用户改过就是草稿，否则是服务端那一份。 */
  const textOf = (pool: RunnerPool) => drafts[pool.id] ?? serverText(pool);

  // 与服务端内容一致的草稿不再留下：否则服务端后来变了，界面上还是那份旧文本，
  // 看起来像“保存没生效”。只清理相等的项，不碰用户真正改过的内容。
  useEffect(() => {
    if (!pools.data) return;
    setDrafts((current) => {
      let touched = false;
      const next = { ...current };
      for (const pool of pools.data ?? []) {
        if (next[pool.id] !== undefined && next[pool.id] === serverText(pool)) {
          delete next[pool.id];
          touched = true;
        }
      }
      return touched ? next : current;
    });
  }, [pools.data]);

  const dirty =
    pools.data?.some((pool) => textOf(pool) !== serverText(pool)) ?? false;
  useLeaveReport(`pools:${workspaceId}/${projectId}`, { dirty, busy: busyId !== null });

  async function save(pool: RunnerPool) {
    setMessage(null);
    setFailure(null);
    const entries = textOf(pool)
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => line !== "");
    if (entries.length === 0) {
      setFailure("白名单至少要保留一个目标；清空不是收窄范围，而是配置错误。");
      return;
    }
    setBusyId(pool.id);
    try {
      const updated = await apiSend(
        projectPath(workspaceId, projectId, `/pools/${pool.id}/targets`),
        "PUT",
        { allowed_targets: entries },
        toRunnerPool,
      );
      setDrafts((current) => ({ ...current, [pool.id]: updated.allowed_targets.join("\n") }));
      setMessage(`已保存「${pool.name}」的目标白名单；本次保存没有访问任何目标。`);
      pools.reload();
    } catch (cause) {
      setFailure(cause instanceof ApiError ? cause.message : "保存白名单失败");
    } finally {
      setBusyId(null);
    }
  }

  return (
    <details className="block">
      <summary>执行池与目标白名单（{pools.data?.length ?? 0}）</summary>
      <p className="caption">
        只有列在这里的目标才允许被访问，审批顺序为：先加白名单，再执行用例。保存只改配置，不访问目标。
      </p>
      {pools.loading ? <Loading label="正在加载执行池…" /> : null}
      {pools.error ? <ErrorText message={pools.error.message} /> : null}
      {pools.data && pools.data.length === 0 ? (
        <Empty label="当前项目没有已授权的执行池，请联系执行池管理者授权。" />
      ) : null}
      {pools.data?.map((pool) => (
        <div className="pool-row" key={pool.id}>
          <div className="pool-head">
            <strong>{pool.name}</strong>
            <span className="caption">
              网络区域 {pool.network_zone} · 状态 {pool.status} · 授权 {pool.grant_status} ·
              绑定环境 {pool.environment_ids.length} 个
            </span>
          </div>
          <label htmlFor={`pool-targets-${pool.id}`}>允许的目标（每行一个，格式 scheme://host:port）</label>
          <textarea
            id={`pool-targets-${pool.id}`}
            rows={4}
            value={textOf(pool)}
            disabled={!canAdmin || busyId === pool.id}
            onChange={(event) =>
              setDrafts((current) => ({ ...current, [pool.id]: event.target.value }))
            }
          />
          {canAdmin ? (
            <div className="actions">
              <button type="button" onClick={() => void save(pool)} disabled={busyId === pool.id}>
                {busyId === pool.id ? "保存中…" : "保存白名单"}
              </button>
            </div>
          ) : (
            <Hint>查看者不能扩大出网范围；白名单维护需要管理员权限。</Hint>
          )}
        </div>
      ))}
      {message ? <Notice tone="info" title={message} /> : null}
      {failure ? <ErrorText message={failure} /> : null}
    </details>
  );
}
