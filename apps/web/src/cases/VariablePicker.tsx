import { useMemo, useState } from "react";
import { Button, Drawer, Select, Table, Tag } from "antd";

import { literalToInput } from "../api/literals";
import type { AvailableVariableSource, VariableContext, VariableContextItem, VariableLocation, VariableSource, ValueLiteral } from "../api/types";
import { ErrorText, Hint, Loading } from "../components/Feedback";

function sourceLabel(item: VariableSource): string {
  return item.level === "environment" ? `环境配置第 ${item.revision} 版` : `项目变量第 ${item.revision} 版`;
}

function literalLabel(value: ValueLiteral): string {
  const parsed = literalToInput(value);
  if (parsed === null) return "（无法显示）";
  if (parsed.type === "null") return "null";
  return parsed.text;
}

function isInsertable(item: VariableContextItem, location: VariableLocation, bodyType?: string): item is VariableContextItem & { reference: string; value: ValueLiteral; effective_source: AvailableVariableSource } {
  return item.reference !== null
    && item.unavailable_reason === null
    && item.value !== null
    && item.effective_source !== null
    && item.available_locations.includes(location)
    && !(location === "body" && bodyType !== undefined && item.restricted_body_types.includes(bodyType as "form"));
}

export function insertReference(value: string, reference: string, start: number, end: number) {
  const safeStart = Math.max(0, Math.min(start, value.length));
  const safeEnd = Math.max(safeStart, Math.min(end, value.length));
  return {
    value: `${value.slice(0, safeStart)}${reference}${value.slice(safeEnd)}`,
    cursor: safeStart + reference.length,
  };
}

export function VariablePicker({
  label,
  location,
  context,
  loading,
  error,
  disabled,
  ownerRevision,
  bodyType,
  onInsert,
}: {
  label: string;
  location: VariableLocation;
  context: VariableContext | null;
  loading: boolean;
  error: string | null;
  disabled: boolean;
  ownerRevision: string;
  bodyType?: string;
  onInsert: (reference: string, capturedRevision: string) => void | Promise<void>;
}) {
  const [capturedRevision, setCapturedRevision] = useState(ownerRevision);
  const candidates = useMemo(
    () => (context?.variables ?? []).filter((item): item is VariableContextItem & { reference: string; value: ValueLiteral; effective_source: AvailableVariableSource } => isInsertable(item, location, bodyType)),
    [bodyType, context, location],
  );
  return (
    <Select
      className="variable-picker"
      aria-label={`插入变量到${label}`}
      placeholder="插入变量"
      value={undefined}
      disabled={disabled || loading || error !== null || context === null || candidates.length === 0}
      loading={loading}
      showSearch
      optionFilterProp="label"
      onOpenChange={(open) => { if (open) setCapturedRevision(ownerRevision); }}
      onChange={(reference: string) => void onInsert(reference, capturedRevision)}
      options={candidates.map((item) => ({
        value: item.reference,
        label: `${item.name} · ${item.value.type} · ${sourceLabel(item.effective_source)}`,
      }))}
    />
  );
}

export function VariableSourceDrawer({
  context,
  loading,
  error,
  onReload,
}: {
  context: VariableContext | null;
  loading: boolean;
  error: string | null;
  onReload: () => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button htmlType="button" type="link" onClick={() => setOpen(true)}>查看变量来源</Button>
      <Drawer title="本次变量来源" open={open} onClose={() => setOpen(false)} size="large">
        {loading ? <Loading label="正在读取当前环境的变量来源…" /> : null}
        {error ? <><ErrorText message={`变量来源未能确认：${error}`} /><Button htmlType="button" onClick={onReload}>重试</Button></> : null}
        {!loading && !error && context === null ? <Hint>选择执行环境后才能确认本次生效来源。</Hint> : null}
        {context ? (
          <Table
            size="small"
            pagination={false}
            rowKey="name"
            dataSource={context.variables}
            columns={[
              { title: "变量", dataIndex: "name" },
              { title: "类型", render: (_: unknown, item: VariableContextItem) => <Tag>{item.value?.type ?? "不可用"}</Tag> },
              { title: "本次采用", render: (_: unknown, item: VariableContextItem) => item.value !== null && item.effective_source !== null ? <><div>{sourceLabel(item.effective_source)}</div><code>{literalLabel(item.value)}</code></> : <span>{item.unavailable_reason ?? "不可用"}</span> },
              { title: "被覆盖来源", render: (_: unknown, item: VariableContextItem) => item.overridden_sources.length === 0 ? "无" : item.overridden_sources.map((source) => <div key={`${source.level}:${source.resource_id}:${source.revision}`}>{sourceLabel(source)}：{source.value === null ? <span>{source.unavailable_reason}</span> : <code>{literalLabel(source.value)}</code>}</div>) },
            ]}
          />
        ) : null}
      </Drawer>
    </>
  );
}
