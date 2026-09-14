/**
 * 环境管理：创建测试环境、修改地址与普通变量、停用不再使用的环境。
 *
 * 本阶段不启用生产环境执行，因此界面只提供测试环境；地址由管理员填写，
 * 目标是否可达由执行池的受控目标白名单在服务端决定，前端不做放行判断。
 * 环境级普通变量与项目级同源：只收字面量，口令等秘密必须走身份凭证配置。
 */
import { useState } from "react";

import { ApiError, apiSend, projectPath } from "../api/client";
import { toEnvironment } from "../api/guards";
import type { Environment, VariableItem } from "../api/types";
import { ErrorText, Hint, Loading } from "../components/Feedback";
import { useLeaveReport } from "../hooks/leaveGuard";
import { VariableRowsEditor } from "../admin/VariableRowsEditor";
import {
  describeLiteralText,
  sameLiteral,
  toLiteral,
  toPayload,
  toRows,
  validateRows,
  type VariableRow,
} from "../admin/variableRows";

/** 一行环境变量的显示摘要：名称 = 值，让人一眼看出这条环境带了什么。 */
function summarize(variables: Record<string, unknown>): string {
  const names = Object.keys(variables);
  if (names.length === 0) return "无普通变量";
  return names.map((name) => `${name}=${describeLiteralText(variables[name])}`).join("，");
}

function variableRows(variables: Record<string, unknown>): VariableRow[] {
  return toRows(Object.entries(variables).map(([name, value]): VariableItem => ({ name, value })));
}

/**
 * 草稿是否与原环境不同：比较**将要提交的字面量**与已保存的那一份。
 *
 * 这里曾经比的是显示文本（`describeLiteralText`），并且拿 `row.original` 当草稿侧
 * 的取值。两个问题都会让保护失效：显示文本把数字 1 与字符串 "1" 一律写成 "1"，
 * 只改类型算不出差别；`row.original` 从载入起就不再变化，只把已有变量的值从 1 改成
 * 2、其他字段不动时，脏状态仍然是“干净”的——切换项目或退出时不会提示，输入直接丢。
 * 类型与值的差别只能由字面量本身回答。
 */
function edited(environment: Environment, rows: VariableRow[]): boolean {
  if (rows.length !== Object.keys(environment.variables).length) return true;
  for (const row of rows) {
    const key = row.name.trim();
    if (!(key in environment.variables)) return true;
    if (!sameLiteral(toLiteral(row), environment.variables[key])) return true;
  }
  return false;
}

export function EnvironmentPanel({
  workspaceId,
  projectId,
  environments,
  loading,
  error,
  selectedId,
  onSelect,
  canEdit,
  onChanged,
}: {
  workspaceId: string;
  projectId: string;
  environments: Environment[];
  loading: boolean;
  error: string | null;
  selectedId: string | null;
  onSelect: (environmentId: string) => void;
  canEdit: boolean;
  onChanged: () => void;
}) {
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  /** 正在编辑的环境与它的草稿：名称、地址、状态与变量行。 */
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftName, setDraftName] = useState("");
  const [draftUrl, setDraftUrl] = useState("");
  const [draftStatus, setDraftStatus] = useState("active");
  const [draftRows, setDraftRows] = useState<VariableRow[]>([]);
  const [busy, setBusy] = useState(false);

  // 未保存的环境编辑也是草稿：地址与变量都是别人依赖的配置，切换范围前必须问一次。
  const editingTarget = environments.find((item) => item.id === editingId) ?? null;
  const formDirty =
    (editingTarget !== null &&
      (draftName !== editingTarget.name ||
        draftUrl !== editingTarget.base_url ||
        draftStatus !== editingTarget.status ||
        edited(editingTarget, draftRows))) ||
    name.trim() !== "" ||
    baseUrl.trim() !== "";
  useLeaveReport(`environment:${workspaceId}/${projectId}`, { dirty: formDirty, busy });

  async function create() {
    setMessage(null);
    setFailure(null);
    if (!name.trim() || !baseUrl.trim()) {
      setFailure("请填写环境名称和地址。");
      return;
    }
    setBusy(true);
    try {
      await apiSend(
        projectPath(workspaceId, projectId, "/environments"),
        "POST",
        { name: name.trim(), kind: "test", base_url: baseUrl.trim(), variables: {} },
        toEnvironment,
      );
      setName("");
      setBaseUrl("");
      setMessage("环境已创建，可在用例编辑页顶部的“执行环境”中选择。");
      onChanged();
    } catch (cause) {
      setFailure(cause instanceof ApiError ? cause.message : "创建环境失败");
    } finally {
      setBusy(false);
    }
  }

  function startEdit(environment: Environment) {
    setMessage(null);
    setFailure(null);
    setEditingId(environment.id);
    setDraftName(environment.name);
    setDraftUrl(environment.base_url);
    setDraftStatus(environment.status);
    setDraftRows(variableRows(environment.variables));
  }

  async function saveEdit(environment: Environment) {
    setMessage(null);
    setFailure(null);
    if (!draftName.trim() || !draftUrl.trim()) {
      setFailure("环境名称和地址都不能为空。");
      return;
    }
    const invalid = validateRows(draftRows);
    if (invalid !== null) {
      setFailure(invalid);
      return;
    }
    setBusy(true);
    try {
      await apiSend(
        projectPath(workspaceId, projectId, `/environments/${environment.id}`),
        "PATCH",
        {
          name: draftName.trim(),
          base_url: draftUrl.trim(),
          status: draftStatus,
          variables: Object.fromEntries(toPayload(draftRows).map((item) => [item.name, item.value])),
        },
        toEnvironment,
      );
      setEditingId(null);
      setMessage(`环境「${draftName.trim()}」已保存。`);
      onChanged();
    } catch (cause) {
      setFailure(cause instanceof ApiError ? cause.message : "保存环境失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <details className="block" open={environments.length === 0}>
      <summary>环境（{environments.length}）</summary>
      {loading ? <Loading label="正在加载环境…" /> : null}
      {error ? <ErrorText message={error} /> : null}
      {environments.length === 0 && !loading ? (
        <Hint>当前项目还没有环境。执行前至少需要一个测试环境，其地址由执行池白名单放行。</Hint>
      ) : (
        <ul className="env-list">
          {environments.map((item) => (
            <li key={item.id}>
              <button
                type="button"
                className={selectedId === item.id ? "active" : undefined}
                onClick={() => onSelect(item.id)}
              >
                {item.name}
              </button>
              <span className="caption">
                {item.kind === "production" ? "生产" : "测试"} · {item.base_url}
                {item.status === "archived" ? " · 已停用" : ""}
                {item.pool_id ? "" : " · 未绑定执行池"}
              </span>
              <span className="caption">{summarize(item.variables)}</span>
              {canEdit ? (
                <button type="button" onClick={() => startEdit(item)}>
                  编辑
                </button>
              ) : null}
              {editingId === item.id ? (
                <div className="env-edit">
                  <label htmlFor="env-edit-name">环境名称</label>
                  <input
                    id="env-edit-name"
                    value={draftName}
                    onChange={(event) => setDraftName(event.target.value)}
                  />
                  <label htmlFor="env-edit-url">服务地址</label>
                  <input
                    id="env-edit-url"
                    value={draftUrl}
                    onChange={(event) => setDraftUrl(event.target.value)}
                  />
                  <label htmlFor="env-edit-status">状态</label>
                  <select
                    id="env-edit-status"
                    value={draftStatus}
                    onChange={(event) => setDraftStatus(event.target.value)}
                  >
                    <option value="active">启用</option>
                    <option value="archived">停用</option>
                  </select>
                  <h4>环境普通变量</h4>
                  <VariableRowsEditor
                    rows={draftRows}
                    disabled={busy}
                    onChange={setDraftRows}
                    emptyHint="该环境还没有普通变量。"
                  />
                  <div className="actions">
                    <button type="button" onClick={() => void saveEdit(item)} disabled={busy}>
                      {busy ? "保存中…" : "保存环境"}
                    </button>
                    <button type="button" onClick={() => setEditingId(null)} disabled={busy}>
                      取消
                    </button>
                  </div>
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      )}
      {canEdit ? (
        <div className="env-create">
          <label htmlFor="env-name">新环境名称</label>
          <input id="env-name" value={name} onChange={(event) => setName(event.target.value)} />
          <label htmlFor="env-url">新环境地址</label>
          <input
            id="env-url"
            value={baseUrl}
            placeholder="http://target-service:8080"
            onChange={(event) => setBaseUrl(event.target.value)}
          />
          <button type="button" onClick={() => void create()} disabled={busy}>
            创建测试环境
          </button>
          <p className="caption">本阶段不启用生产环境；创建生产环境会被服务端拒绝。</p>
        </div>
      ) : null}
      {message ? <Hint>{message}</Hint> : null}
      {failure ? <ErrorText message={failure} /> : null}
    </details>
  );
}
