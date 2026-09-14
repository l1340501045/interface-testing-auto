/**
 * 普通变量的行编辑器（项目级与环境级共用）。
 *
 * 类型下拉列出后端支持的全部字面量类型：文本、数字、布尔、空值、JSON。载入的行
 * 按它自己保存的类型显示，用户不显式改类型就按原类型原样保存，不会被界面“顺手”
 * 改成文本。认不出的类型只读展示并原样保留，不静默降级。
 *
 * 这里不提供“秘密”类型：秘密必须走身份凭证配置，界面上不给入口，用户就不会把
 * 口令贴进普通变量，再从执行结果或报告里看到它。
 */
import { Empty, Hint } from "../components/Feedback";
import {
  KIND_LABELS,
  editRow,
  emptyRow,
  textForKind,
  type EditableKind,
  type VariableKind,
  type VariableRow,
} from "./variableRows";

const EDITABLE_KINDS: EditableKind[] = ["string", "number", "boolean", "null", "json"];

export function VariableRowsEditor({
  rows,
  disabled,
  onChange,
  emptyHint,
}: {
  rows: VariableRow[];
  disabled: boolean;
  onChange: (rows: VariableRow[]) => void;
  emptyHint: string;
}) {
  function patch(index: number, change: { name?: string; kind?: EditableKind; text?: string }) {
    onChange(rows.map((row, position) => (position === index ? editRow(row, change) : row)));
  }

  function changeKind(index: number, kind: EditableKind) {
    const row = rows[index];
    patch(index, { kind, text: textForKind(kind, row.text) });
  }

  function renderValueControl(row: VariableRow, index: number) {
    const id = `variable-value-${index}`;
    const locked = disabled || row.kind === "unknown";
    if (row.kind === "boolean") {
      return (
        <select
          id={id}
          value={row.text === "true" ? "true" : "false"}
          disabled={locked}
          onChange={(event) => patch(index, { text: event.target.value })}
        >
          <option value="true">true</option>
          <option value="false">false</option>
        </select>
      );
    }
    if (row.kind === "null") {
      // null 的取值只有 null 本身，没有可填的文本；给一个占位而不是让用户以为漏填了。
      return <span className="caption">固定为 null，无需填写</span>;
    }
    if (row.kind === "json") {
      return (
        <textarea
          id={id}
          rows={2}
          value={row.text}
          disabled={locked}
          placeholder='例如 {"id": 9007199254740993}'
          onChange={(event) => patch(index, { text: event.target.value })}
        />
      );
    }
    if (row.kind === "unknown") {
      return <input id={id} value={row.text} readOnly disabled />;
    }
    return (
      <input
        id={id}
        value={row.text}
        disabled={locked}
        onChange={(event) => patch(index, { text: event.target.value })}
      />
    );
  }

  return (
    <div className="variable-rows">
      {rows.length === 0 ? <Empty label={emptyHint} /> : null}
      {rows.map((row, index) => (
        <div className="variable-row" key={index}>
          <label htmlFor={`variable-name-${index}`}>名称</label>
          <input
            id={`variable-name-${index}`}
            value={row.name}
            disabled={disabled}
            onChange={(event) => patch(index, { name: event.target.value })}
          />
          <label htmlFor={`variable-type-${index}`}>类型</label>
          <select
            id={`variable-type-${index}`}
            value={row.kind}
            // 认不出的类型不给切换入口：能改就等于允许把它悄悄改成别的类型。
            disabled={disabled || row.kind === "unknown"}
            onChange={(event) => changeKind(index, event.target.value as EditableKind)}
          >
            {row.kind === "unknown" ? (
              <option value="unknown">{KIND_LABELS.unknown}</option>
            ) : null}
            {EDITABLE_KINDS.map((kind) => (
              <option key={kind} value={kind}>
                {KIND_LABELS[kind as VariableKind]}
              </option>
            ))}
          </select>
          <label htmlFor={`variable-value-${index}`}>值</label>
          {renderValueControl(row, index)}
          <button
            type="button"
            disabled={disabled}
            onClick={() => onChange(rows.filter((_, position) => position !== index))}
          >
            删除
          </button>
        </div>
      ))}
      {disabled ? (
        <Hint>当前角色只能查看这些变量；修改需要编辑者或管理员。</Hint>
      ) : (
        <button type="button" onClick={() => onChange([...rows, emptyRow()])}>
          ＋添加变量
        </button>
      )}
    </div>
  );
}
