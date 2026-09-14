/**
 * 字段旁的断言列：一行字段直接添加、修改、删除条件，不跳转到别的规则页面。
 *
 * 每条条件归属于当前用例，复制出来的条件也是新归属，不形成隐藏的共享关系。
 * 执行结果按断言标识回填，与配置并列显示，便于对照期望与实际。
 */
import { useState } from "react";

import type { AssertionResult, AssertionType, CaseAssertion, LocatorStep, ValueLiteral } from "../api/types";
import { describeValue } from "../api/literals";
import { Loading, StatusTag } from "../components/Feedback";
import { AssertionEditor, type AssertionDraft, type FieldContext } from "./AssertionEditor";
import { newAssertionId } from "./assertionGroups";
import { summarize } from "./assertionModel";

export function fieldKey(targetSource: string, selector: LocatorStep[]): string {
  return `${targetSource}|${JSON.stringify(selector)}`;
}

export function AssertionColumn({
  types,
  typesError,
  workspaceId,
  projectId,
  field,
  own,
  sample,
  results,
  readOnly,
  onUpsert,
  onRemove,
}: {
  types: AssertionType[];
  typesError: string | null;
  workspaceId: string;
  projectId: string;
  field: FieldContext;
  /** 该字段当前的全部条件；调用方可以只渲染其中一部分，增删改仍按标识生效。 */
  own: CaseAssertion[];
  sample: ValueLiteral | null;
  results: Map<string, AssertionResult>;
  readOnly: boolean;
  onUpsert: (next: CaseAssertion) => void;
  onRemove: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);

  const editing = editingId ? (own.find((item) => item.id === editingId) ?? null) : null;

  function close() {
    setOpen(false);
    setEditingId(null);
  }

  function commit(draft: AssertionDraft) {
    if (editing) {
      onUpsert({
        ...editing,
        type: draft.type,
        parameters: draft.parameters,
        compare_as: draft.compare_as,
        severity: draft.severity,
      });
    } else {
      onUpsert({
        id: newAssertionId(),
        target_source: field.targetSource,
        selector: field.selector,
        type: draft.type,
        parameters: draft.parameters,
        compare_as: draft.compare_as,
        severity: draft.severity,
        enabled: true,
        sort_order: 0,
      });
    }
    close();
  }

  return (
    <div className="assertion-column">
      <ul className="assertion-list">
        {own.map((item) => {
          const type = types.find((entry) => entry.id === item.type);
          const result = results.get(item.id);
          return (
            <li key={item.id}>
              <span className="assertion-label">
                {type ? summarize(type, item.parameters) : item.type}
                {item.severity === "warning" ? "（仅提示）" : ""}
                {item.enabled ? "" : "（已停用）"}
              </span>
              {result ? (
                <span className="assertion-result">
                  <StatusTag status={result.status} />
                  {/* 未执行的断言没有采集到值，显示“期望 null / 实际 null”会被误读成
                      真的比较过 null，因此只在求值过的结果旁显示期望与实际。 */}
                  {result.status === "skipped" ? null : (
                    <span className="caption">
                      期望 {describeValue(result.expected)} / 实际 {describeValue(result.actual)}
                    </span>
                  )}
                </span>
              ) : null}
              {readOnly ? null : (
                <span className="inline-actions">
                  <button
                    type="button"
                    onClick={() => {
                      setEditingId(item.id);
                      setOpen(true);
                    }}
                  >
                    修改
                  </button>
                  <button type="button" onClick={() => onRemove(item.id)}>
                    删除
                  </button>
                </span>
              )}
            </li>
          );
        })}
      </ul>

      {open ? (
        typesError ? (
          <p className="error" role="alert">
            {typesError}
          </p>
        ) : types.length === 0 ? (
          <Loading label="正在加载可用断言类型…" />
        ) : (
          <AssertionEditor
            // 切换编辑目标时按目标标识重挂载：表单状态在挂载时初始化，若原地复用
            // 上一条的组件实例，改 B 时输入框里还是 A 的参数，保存即把 A 的条件写进 B。
            key={editingId ?? "new"}
            types={types}
            field={field}
            workspaceId={workspaceId}
            projectId={projectId}
            initial={editing}
            sample={sample}
            editing={editing !== null}
            onSubmit={commit}
            onCancel={close}
          />
        )
      ) : readOnly ? null : (
        <button
          type="button"
          className="add-assertion"
          onClick={() => {
            setEditingId(null);
            setOpen(true);
          }}
        >
          ＋添加断言
        </button>
      )}
    </div>
  );
}
