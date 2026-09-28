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
import { useId, useRef } from "react";
import { Button, Flex, Form, Input, Select, Typography } from "antd";

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

interface RowIdentityState {
  rows: VariableRow[];
  ids: string[];
  sequence: number;
}

/**
 * 变量没有业务行 ID，但界面仍需要稳定身份。
 *
 * 优先按对象身份找回原行（删除中间行时后续行不会换身份）；编辑会替换当前行对象，
 * 此时再复用同一位置尚未占用的 ID。生成的 ID 只存在组件内，不会进入变量 payload。
 */
function useRowIdentities(rows: VariableRow[]): string[] {
  const stateRef = useRef<RowIdentityState>({ rows: [], ids: [], sequence: 0 });
  const previous = stateRef.current;
  const nextIds: Array<string | undefined> = rows.map((row) => {
    const previousIndex = previous.rows.indexOf(row);
    return previousIndex >= 0 ? previous.ids[previousIndex] : undefined;
  });
  const claimed = new Set(nextIds.filter((id): id is string => id !== undefined));

  rows.forEach((_, index) => {
    if (nextIds[index] !== undefined) return;
    const positional = previous.ids[index];
    if (positional !== undefined && !claimed.has(positional)) {
      nextIds[index] = positional;
      claimed.add(positional);
      return;
    }
    previous.sequence += 1;
    const created = `row-${previous.sequence}`;
    nextIds[index] = created;
    claimed.add(created);
  });

  const resolved = nextIds as string[];
  stateRef.current = { rows, ids: resolved, sequence: previous.sequence };
  return resolved;
}

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
  const instanceId = useId().replaceAll(":", "");
  const rowIds = useRowIdentities(rows);

  function patch(index: number, change: { name?: string; kind?: EditableKind; text?: string }) {
    onChange(rows.map((row, position) => (position === index ? editRow(row, change) : row)));
  }

  function changeKind(index: number, kind: EditableKind) {
    const row = rows[index];
    patch(index, { kind, text: textForKind(kind, row.text) });
  }

  function renderValueControl(row: VariableRow, index: number, rowId: string) {
    const id = `variable-${instanceId}-${rowId}-value`;
    const locked = disabled || row.kind === "unknown";
    if (row.kind === "boolean") {
      return (
        <Select
          id={id}
          data-selected-value={row.text === "true" ? "true" : "false"}
          value={row.text === "true" ? "true" : "false"}
          disabled={locked}
          options={[{ value: "true", label: "true" }, { value: "false", label: "false" }]}
          onChange={(value) => patch(index, { text: value })}
        />
      );
    }
    if (row.kind === "null") {
      // null 的取值只有 null 本身，没有可填的文本；给一个占位而不是让用户以为漏填了。
      return <Typography.Text type="secondary">固定为 null，无需填写</Typography.Text>;
    }
    if (row.kind === "json") {
      return (
        <Input.TextArea
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
      return <Input id={id} value={row.text} readOnly disabled />;
    }
    return (
      <Input
        id={id}
        value={row.text}
        disabled={locked}
        onChange={(event) => patch(index, { text: event.target.value })}
      />
    );
  }

  return (
    <Flex className="variable-rows" vertical gap="small">
      {rows.length === 0 ? <Empty label={emptyHint} /> : null}
      {rows.map((row, index) => {
        const rowId = rowIds[index];
        const nameId = `variable-${instanceId}-${rowId}-name`;
        const typeId = `variable-${instanceId}-${rowId}-type`;
        const valueId = `variable-${instanceId}-${rowId}-value`;
        return (
          <Flex className="variable-row" key={rowId} align="flex-end" gap="small" wrap>
            <Form.Item
              label="名称"
              htmlFor={nameId}
              layout="vertical"
              style={{ flex: "1 1 160px", minWidth: 140, marginBottom: 0 }}
            >
              <Input
                id={nameId}
                value={row.name}
                disabled={disabled}
                onChange={(event) => patch(index, { name: event.target.value })}
              />
            </Form.Item>
            <Form.Item
              label="类型"
              htmlFor={typeId}
              layout="vertical"
              style={{ flex: "0 1 120px", minWidth: 110, marginBottom: 0 }}
            >
              <Select
                id={typeId}
                data-selected-value={row.kind}
                value={row.kind}
                // 认不出的类型不给切换入口：能改就等于允许把它悄悄改成别的类型。
                disabled={disabled || row.kind === "unknown"}
                options={[
                  ...(row.kind === "unknown"
                    ? [{ value: "unknown", label: KIND_LABELS.unknown }]
                    : []),
                  ...EDITABLE_KINDS.map((kind) => ({
                    value: kind,
                    label: KIND_LABELS[kind as VariableKind],
                  })),
                ]}
                onChange={(value) => changeKind(index, value as EditableKind)}
              />
            </Form.Item>
            <Form.Item
              label="值"
              htmlFor={valueId}
              layout="vertical"
              style={{ flex: "2 1 240px", minWidth: 180, marginBottom: 0 }}
            >
              {renderValueControl(row, index, rowId)}
            </Form.Item>
            <Button
              htmlType="button"
              danger
              disabled={disabled}
              onClick={() => onChange(rows.filter((_, position) => position !== index))}
            >
              删除
            </Button>
          </Flex>
        );
      })}
      {disabled ? (
        <Hint>当前角色只能查看这些变量；修改需要编辑者或管理员。</Hint>
      ) : (
        <Button htmlType="button" onClick={() => onChange([...rows, emptyRow()])}>
          ＋添加变量
        </Button>
      )}
    </Flex>
  );
}
