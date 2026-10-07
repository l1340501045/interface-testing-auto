/**
 * 项目普通变量：按不可变版本新增，历史版本保留。
 *
 * 每次保存都是新版本，界面显示当前版本号，让“改过的值还能查回去”这件事可见。
 * 秘密不能从这条入口进来：服务端单列一个错误码，这里如实转述。
 *
 * 编辑边界有两条，都是本任务修过的真实缺陷：
 * - **基线到达前不可编辑**。资源内容带着自己的 scope（见 useResource），当前项目的
 *   内容没到就不建立基线，此时不开放输入，避免“先能打字、随后被回填覆盖”。
 * - **保存后的解锁不依赖刷新完成**。保存成功用服务器返回的那一版原子更新基线，
 *   即使后台刷新还没回来，用户紧接着新增的行也不会被旧内容覆盖。
 *
 * 服务端版本在本地有未保存草稿时被别人推进（并行编辑）不清空草稿：草稿一律保留，
 * 由用户明确选择“改用服务端新版”还是“在新版基础上继续用我的内容”。静默二选一
 * 都会丢东西，而这里丢的是别人或自己的配置。
 */
import { useEffect, useMemo, useState } from "react";
import { Button, Collapse, Flex, Space, Typography } from "antd";

import { ApiError, apiSend, projectPath } from "../api/client";
import { toVariablesSet } from "../api/guards";
import type { VariablesSet } from "../api/types";
import { Empty, ErrorText, Hint, Loading, Notice } from "../components/Feedback";
import { useLeaveReport } from "../hooks/leaveGuard";
import { useResource } from "../hooks/useResource";
import { VariableRowsEditor } from "./VariableRowsEditor";
import { toPayload, toRows, validateRows, type VariableRow } from "./variableRows";

/** 本地草稿：以哪一版为基线、改成了什么样。 */
interface Draft {
  baseVersion: number;
  rows: VariableRow[];
}

/** 行内容是否一致；名称、类型、文本任一处不同都算改动。 */
function sameRows(left: VariableRow[], right: VariableRow[]): boolean {
  if (left.length !== right.length) return false;
  return left.every((row, index) => {
    const other = right[index];
    return row.name === other.name && row.kind === other.kind && row.text === other.text;
  });
}

export function VariablesPanel({
  workspaceId,
  projectId,
  canEdit,
  onExecutionConfigChanged,
}: {
  workspaceId: string;
  projectId: string;
  canEdit: boolean;
  /** 项目变量写入成功后通知：它改变请求的解析输入。 */
  onExecutionConfigChanged: () => void;
}) {
  const scope = `${workspaceId}/${projectId}`;
  const variables = useResource<VariablesSet>(`${scope}/variables`, (signal) =>
    apiSend(projectPath(workspaceId, projectId, "/variables"), "GET", undefined, toVariablesSet, {
      signal,
    }),
  );

  const [draft, setDraft] = useState<Draft | null>(null);
  /** 最近一次保存返回的那一版：保存后到刷新完成之间，界面按它显示而不是按旧内容。 */
  const [savedSet, setSavedSet] = useState<{ scope: string; set: VariablesSet } | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // 换项目就是换一份资源：草稿属于上一个项目，不能带过来。
  useEffect(() => setDraft(null), [scope]);

  const loaded = variables.data;
  const local = savedSet !== null && savedSet.scope === scope ? savedSet.set : null;
  // 服务端已知的最新内容：优先本地刚保存的那一版，但服务端版本更高时以服务端为准
  // （期间别人又保存过一版，那才是当前真相）。
  const current = useMemo<VariablesSet | null>(() => {
    if (local === null) return loaded;
    if (loaded === null) return local;
    return loaded.version >= local.version ? loaded : local;
  }, [loaded, local]);

  const serverRows = useMemo(() => (current === null ? [] : toRows(current.variables)), [current]);
  const ready = current !== null;

  // 草稿一直保留：服务端版本推进只标记“基线已过期”，不顶掉用户正在编辑的内容。
  const stale = draft !== null && current !== null && draft.baseVersion !== current.version;
  const dirty = draft !== null && !sameRows(draft.rows, serverRows);
  const rows = draft !== null ? draft.rows : serverRows;

  useLeaveReport(`variables:${scope}`, { dirty, busy });

  function editRows(next: VariableRow[]) {
    setMessage(null);
    setFailure(null);
    setDraft({ baseVersion: current?.version ?? 0, rows: next });
  }

  async function save() {
    if (draft === null) return;
    setMessage(null);
    setFailure(null);
    if (stale) {
      setFailure("服务端变量已更新，请先选择保留你的草稿还是改用服务端内容。");
      return;
    }
    const invalid = validateRows(draft.rows);
    if (invalid !== null) {
      setFailure(invalid);
      return;
    }
    setBusy(true);
    try {
      const saved = await apiSend(
        projectPath(workspaceId, projectId, "/variables"),
        "PUT",
        { variables: toPayload(draft.rows) },
        toVariablesSet,
        { headers: { "If-Match": String(draft.baseVersion) } },
      );
      // **先通知**：这次写入改变了请求将以哪些变量解析，工作台里基于旧变量的预检与
      // “当前通过”必须当场失效，不能等列表刷新回来（那是另一条异步链）。
      onExecutionConfigChanged();
      // 服务器返回的就是刚刚写入的那一版：直接把它当成新基线，不等刷新。
      setSavedSet({ scope, set: saved });
      setDraft(null);
      setMessage(`已保存为第 ${saved.version} 版；历史版本仍可查回。`);
      variables.reload();
    } catch (cause) {
      if (cause instanceof ApiError && (cause.code === "config_revision_conflict" || cause.code === "config_revision_required")) {
        setFailure(`${cause.message}；你的输入已保留，请刷新服务端版本后再决定如何处理。`);
        variables.reload();
      } else {
        setFailure(cause instanceof ApiError ? cause.message : "保存变量失败");
      }
    } finally {
      setBusy(false);
    }
  }

  const content = (
    <Flex vertical gap="small">
      <Typography.Paragraph type="secondary">
        普通变量用于替换请求中的占位内容，随执行环境一起生效；口令等秘密必须走身份凭证配置。
        变量参与请求的最终形态，凭证用途授权会按签发时的变量冻结输入：改动这里的值之后，
        原来的凭证授权不再适用于新的请求，需要在“人工凭证”里重新签发。
      </Typography.Paragraph>
      {!ready && variables.loading ? <Loading label="正在加载项目变量…" /> : null}
      {variables.error ? <ErrorText message={variables.error.message} /> : null}
      {ready && current.version === 0 && rows.length === 0 ? (
        <Empty label="项目还没有普通变量。" />
      ) : null}
      {ready ? (
        <>
          {stale && dirty ? (
            <Notice
              tone="warning"
              title={`服务端已保存到第 ${current.version} 版，你的草稿基于第 ${draft?.baseVersion} 版。`}
            >
              <Space className="actions">
                <Button
                  htmlType="button"
                  onClick={() => {
                    setDraft(null);
                    setMessage(`已改用服务端第 ${current.version} 版；未保存的草稿已丢弃。`);
                  }}
                >
                  改用服务端第 {current.version} 版
                </Button>
                <Button htmlType="button" onClick={() => setDraft({ baseVersion: current.version, rows })}>
                  在第 {current.version} 版基础上继续用我的内容
                </Button>
              </Space>
            </Notice>
          ) : null}
          <VariableRowsEditor
            // 基线未建立前不给输入入口：此时回填还没发生，先让用户打字再被覆盖
            // 等于把用户的输入当垃圾丢掉，而界面上看不出发生过什么。
            rows={rows}
            disabled={!canEdit || busy || !ready}
            onChange={editRows}
            emptyHint="还没有变量；点“＋添加变量”开始配置。"
          />
          {canEdit ? (
            <Space className="actions">
              <Button
                type="primary"
                htmlType="button"
                onClick={() => void save()}
                disabled={busy || !ready || draft === null || stale}
              >
                {busy ? "保存中…" : "保存为新版本"}
              </Button>
            </Space>
          ) : null}
        </>
      ) : null}
      {message ? <Notice tone="info" title={message} /> : null}
      {failure ? <ErrorText message={failure} /> : null}
      {!ready && !variables.loading && !variables.error ? <Hint>暂时读不到项目变量。</Hint> : null}
    </Flex>
  );

  return (
    <div className="block">
      <Collapse
        defaultActiveKey={["variables"]}
        items={[{
          key: "variables",
          label: `项目普通变量（当前第 ${current?.version ?? 0} 版）`,
          children: content,
        }]}
      />
    </div>
  );
}
