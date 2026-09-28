/**
 * 请求编辑器：可重复的键值行与正文编辑。
 *
 * 方法与路径在地址行（工作台的发送栏）里编辑，不在这里重复渲染第二份：两个输入框绑
 * 同一个字段，改一个另一个不会同步，用户看到的是“打了字没生效”。
 *
 * 目标地址不在这里填写：路径只是相对路径，实际 origin 由所选环境决定，
 * 用例无法把请求指向环境白名单之外的目标。导入 cURL 与保存都不会发出请求。
 */
import { useId, useRef, useState, type ClipboardEvent, type ReactNode } from "react";
import { Button, Checkbox, Collapse, Input, Radio, Select, Space, Table } from "antd";

import { useLeaveReport } from "../hooks/leaveGuard";
import { decodeRawText, encodeRawText, needsRawTextEditor, parseParameterImport, type ParameterImportFormat } from "./parameterImport";
import { ltrimPath, newRequestRow, type RawKeyValue, type RawRequest } from "./requestDraft";
export interface KeyValueRowProps {
  rows: RawKeyValue[];
  label: string;
  /** 仅 true 表示父层已实际接纳；取消、拒绝、过期或超限必须返回 false。 */
  onChange: (rows: RawKeyValue[]) => boolean | Promise<boolean>;
  readOnly: boolean;
  addLabel: string;
  idPrefix?: string;
  kind?: "query" | "header";
  version?: 1 | 2;
  assertionSlot?: (row: RawKeyValue, index: number) => ReactNode;
  pendingPrefix?: string;
  otherRows?: RawKeyValue[];
  ownerRevision?: string;
  relatedAssertionCount?: number;
}

function RawTextField({ value, label, readOnly, onChange, pendingKey }: { value: string; label: string; readOnly: boolean; onChange: (value: string) => boolean | Promise<boolean>; pendingKey?: string }) {
  const localId = useId();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(() => encodeRawText(value));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const revisionRef = useRef(0);
  const attemptRef = useRef(0);
  useLeaveReport(pendingKey ?? `raw-text:${localId}`, {
    dirty: editing && draft !== encodeRawText(value),
    busy,
  });

  async function applyDraft() {
    if (busyRef.current) return;
    let decoded: string;
    try {
      decoded = decodeRawText(draft);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "转义文本不合法");
      return;
    }
    const submittedRevision = revisionRef.current;
    const attempt = attemptRef.current + 1;
    attemptRef.current = attempt;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    try {
      const accepted = await onChange(decoded);
      if (accepted === true && attemptRef.current === attempt && revisionRef.current === submittedRevision) {
        setEditing(false);
      }
    } catch (cause) {
      if (attemptRef.current === attempt) {
        setError(cause instanceof Error ? cause.message : "原始文本未能应用");
      }
    } finally {
      if (attemptRef.current === attempt) {
        busyRef.current = false;
        setBusy(false);
      }
    }
  }
  if (!needsRawTextEditor(value) && !editing) {
    return <Input aria-label={label} value={value} readOnly={readOnly} onChange={(event) => onChange(event.target.value)} />;
  }
  if (!editing) {
    return (
      <span className="raw-text-summary">
        <code>{encodeRawText(value)}</code>
        {readOnly ? null : <Button htmlType="button" size="small" onClick={() => { revisionRef.current += 1; setDraft(encodeRawText(value)); setEditing(true); }}>编辑原始文本</Button>}
      </span>
    );
  }
  return (
    <span className="raw-text-editor">
      <Input aria-label={`${label}转义文本`} value={draft} onChange={(event) => { revisionRef.current += 1; setDraft(event.target.value); }} />
      <Button htmlType="button" size="small" aria-label="应用" loading={busy} onClick={() => void applyDraft()}>应用</Button>
      <Button htmlType="button" size="small" aria-label="取消" disabled={busy} onClick={() => { setError(null); setEditing(false); }}>取消</Button>
      {error ? <span className="error" role="alert">{error}</span> : null}
    </span>
  );
}

function BatchImport({ rows, otherRows, kind, onApply, readOnly, pendingKey, ownerRevision, relatedAssertionCount }: { rows: RawKeyValue[]; otherRows: RawKeyValue[]; kind: "query" | "header"; onApply: (rows: RawKeyValue[]) => boolean | Promise<boolean>; readOnly: boolean; pendingKey: string; ownerRevision: string; relatedAssertionCount: number }) {
  const [raw, setRaw] = useState("");
  const [format, setFormat] = useState<ParameterImportFormat>(kind === "header" ? "headers" : "equals");
  const [preview, setPreview] = useState<ReturnType<typeof parseParameterImport> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<"append" | "replace">("append");
  const [revision, setRevision] = useState(0);
  const [previewRevision, setPreviewRevision] = useState<number | null>(null);
  const [previewRowsKey, setPreviewRowsKey] = useState<string | null>(null);
  const [appliedRevision, setAppliedRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const revisionRef = useRef(0);
  const busyRef = useRef(false);
  const attemptRef = useRef(0);
  const rowsKey = `${JSON.stringify(rows)}|${JSON.stringify(otherRows)}|${ownerRevision}`;
  useLeaveReport(pendingKey, { dirty: raw !== "" && revision !== appliedRevision, busy: false });
  if (readOnly) return null;
  function capture(event: ClipboardEvent<HTMLTextAreaElement>) {
    event.preventDefault();
    const text = event.clipboardData.getData("text/plain");
    setRaw(encodeRawText(text));
    revisionRef.current += 1;
    setRevision(revisionRef.current);
    setPreview(null);
  }
  function createPreview() {
    try {
      const text = decodeRawText(raw);
      const parsed = parseParameterImport(text, format);
      if (kind === "header") {
        const invalid = parsed.rows.findIndex((row) => !/^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/.test(row.name));
        if (invalid >= 0) throw new Error(`第 ${invalid + 1} 行：Header 名称不是合法的请求头名称。`);
      }
      setPreview(parsed);
      setPreviewRevision(revision);
      setPreviewRowsKey(rowsKey);
      setError(null);
    } catch (cause) {
      setPreview(null);
      setError(cause instanceof Error ? cause.message : "批量输入无法解析");
    }
  }
  async function applyPreview() {
    if (busyRef.current || preview === null) return;
    if (previewRevision !== revision || previewRowsKey !== rowsKey) {
      setError("批量原文或参数表已改变，请重新预览。");
      return;
    }
    const imported = preview.rows.map((row) => newRequestRow(row));
    const nextCount = (mode === "append" ? rows.length + imported.length : imported.length) + otherRows.length;
    if (nextCount > 500) {
      setError("应用后 Query 和 Header 合计不能超过 500 行。");
      return;
    }
    const submittedRevision = revisionRef.current;
    const attempt = attemptRef.current + 1;
    attemptRef.current = attempt;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    try {
      const accepted = await onApply(mode === "append" ? [...rows, ...imported] : imported);
      if (accepted === true && attemptRef.current === attempt && revisionRef.current === submittedRevision) {
        setAppliedRevision(submittedRevision);
        setPreview(null);
      }
    } catch (cause) {
      if (attemptRef.current === attempt) {
        setError(cause instanceof Error ? cause.message : "批量参数未能应用");
      }
    } finally {
      if (attemptRef.current === attempt) {
        busyRef.current = false;
        setBusy(false);
      }
    }
  }
  return (
    <Collapse className="batch-import" items={[{ key: "batch", label: "批量录入", children: <>
      <p className="caption">直接捕获剪贴板原文；转义框中的 \\r、\\n、\\t、\\\\ 会按字符还原。</p>
      <label>格式</label>
      <Select
        aria-label={`${kind === "query" ? "Query" : "Header"} 批量格式`}
        value={format}
        data-selected-value={format}
        onChange={(value: ParameterImportFormat) => { setFormat(value); setPreview(null); }}
        options={[
          { value: "tsv", label: "表格 TSV（2～3 列）" },
          { value: "equals", label: "逐行 名称=值" },
          ...(kind === "header" ? [{ value: "headers", label: "逐行 Header: 值" } as const] : []),
        ]}
      />
      <Input.TextArea aria-label={`${kind === "query" ? "Query" : "Header"} 批量原文转义文本`} rows={5} value={raw} onPaste={capture} onChange={(event) => { revisionRef.current += 1; setRaw(event.target.value); setRevision(revisionRef.current); setPreview(null); }} />
      <Radio.Group aria-label="批量应用方式" value={mode} onChange={(event) => setMode(event.target.value as "append" | "replace")}>
        <Radio value="append">追加</Radio>
        <Radio value="replace">替换</Radio>
      </Radio.Group>
      <Button htmlType="button" onClick={createPreview}>生成预览</Button>
      {error ? <p className="error" role="alert">{error}</p> : null}
      {preview ? (
        <div className="batch-preview">
          <p>共 {preview.rows.length} 行{preview.duplicateNames.length ? `；重复名称：${preview.duplicateNames.join("、")}` : ""}。</p>
          {mode === "replace" ? <p className="caption">替换将移除当前 {rows.length} 行，并使 {relatedAssertionCount} 条行条件显示为“字段已删除”；不会自动重绑。</p> : null}
          <ol>{preview.rows.map((row, index) => <li key={`${index}-${row.name}`}><code>{encodeRawText(row.name)}</code> = <code>{encodeRawText(row.value)}</code></li>)}</ol>
          <Button htmlType="button" type="primary" loading={busy} onClick={() => void applyPreview()}>应用{mode === "append" ? "追加" : "替换"}</Button>
          <Button htmlType="button" aria-label="取消" disabled={busy} onClick={() => setPreview(null)}>取消</Button>
        </div>
      ) : null}
    </> }]} />
  );
}

export function KeyValueRows({ rows, label, onChange, readOnly, addLabel, idPrefix = "request", kind = "query", version = 1, assertionSlot, pendingPrefix = idPrefix, otherRows = [], ownerRevision = "", relatedAssertionCount = 0 }: KeyValueRowProps) {
  const legacyRowKeys = useRef(new WeakMap<RawKeyValue, string>());
  const legacyRowSequence = useRef(0);
  function displayRowKey(row: RawKeyValue): string {
    if (row.row_id !== undefined) return row.row_id;
    const existing = legacyRowKeys.current.get(row);
    if (existing !== undefined) return existing;
    legacyRowSequence.current += 1;
    const created = `${idPrefix}-legacy-${legacyRowSequence.current}`;
    legacyRowKeys.current.set(row, created);
    return created;
  }
  function update(index: number, patch: Partial<RawKeyValue>): boolean | Promise<boolean> {
    const next = rows.map((row, position) => {
      if (position !== index) return row;
      const updated = { ...row, ...patch };
      // v1 的 UI 展示身份不能进入请求 payload，但受控更新会生成新对象。把当前行的
      // 本地 key 显式继承给新对象，避免同一输入过程重挂载、丢焦点或丢未应用原文。
      if (row.row_id === undefined && updated.row_id === undefined) {
        legacyRowKeys.current.set(updated, displayRowKey(row));
      }
      return updated;
    });
    return onChange(next);
  }
  return (
    <div className="kv-block">
      {rows.length === 0 ? <p className="hint">还没有{label}。</p> : null}
      <Table
        size="small"
        pagination={false}
        scroll={{ x: version === 2 ? (assertionSlot ? 1162 : 902) : (assertionSlot ? 890 : 630) }}
        rowKey={displayRowKey}
        dataSource={rows}
        rowClassName={(row) => row.enabled === false ? "kv-row-disabled" : ""}
        columns={[
          ...(version === 2 ? [{ title: "发送", width: 72, render: (_: unknown, row: RawKeyValue, index: number) => <Checkbox aria-label={`发送第 ${index + 1} 项${label}`} checked={row.enabled === true} disabled={readOnly} onChange={(event) => update(index, { enabled: event.target.checked })} /> }] : []),
          { title: "名称", width: 200, render: (_: unknown, row: RawKeyValue, index: number) => <RawTextField pendingKey={`${pendingPrefix}:raw-${kind}-${row.row_id ?? index}-name`} label={`${label}名称 ${index + 1}`} value={row.name} readOnly={readOnly} onChange={(name) => update(index, { name })} /> },
          { title: "值", width: 240, render: (_: unknown, row: RawKeyValue, index: number) => <RawTextField pendingKey={`${pendingPrefix}:raw-${kind}-${row.row_id ?? index}-value`} label={`${label}值 ${index + 1}`} value={row.value} readOnly={readOnly} onChange={(value) => update(index, { value })} /> },
          ...(version === 2 ? [{ title: "说明", width: 200, render: (_: unknown, row: RawKeyValue, index: number) => <RawTextField pendingKey={`${pendingPrefix}:raw-${kind}-${row.row_id ?? index}-description`} label={`${label}说明 ${index + 1}`} value={row.description ?? ""} readOnly={readOnly} onChange={(description) => update(index, { description })} /> }] : []),
          ...(assertionSlot ? [{ title: "断言", width: 260, render: (_: unknown, row: RawKeyValue, index: number) => assertionSlot(row, index) }] : []),
          { title: "操作", width: 190, render: (_: unknown, _row: RawKeyValue, index: number) => readOnly ? null : <Space size={4}>
            <Button htmlType="button" size="small" disabled={index === 0} onClick={() => { const next = [...rows]; [next[index - 1], next[index]] = [next[index], next[index - 1]]; onChange(next); }} aria-label={`上移第 ${index + 1} 项${label}`}>上移</Button>
            <Button htmlType="button" size="small" disabled={index === rows.length - 1} onClick={() => { const next = [...rows]; [next[index], next[index + 1]] = [next[index + 1], next[index]]; onChange(next); }} aria-label={`下移第 ${index + 1} 项${label}`}>下移</Button>
            <Button htmlType="button" size="small" danger onClick={() => onChange(rows.filter((_, position) => position !== index))} aria-label={`删除第 ${index + 1} 项${label}`}>删除</Button>
          </Space> },
        ]}
      />
      {readOnly ? null : (
        <Button htmlType="button" onClick={() => onChange([...rows, version === 2 ? newRequestRow() : { name: "", value: "" }])}>
          {addLabel}
        </Button>
      )}
      <BatchImport rows={rows} otherRows={otherRows} kind={kind} onApply={onChange} readOnly={readOnly} pendingKey={`${pendingPrefix}:batch-${kind}`} ownerRevision={ownerRevision} relatedAssertionCount={relatedAssertionCount} />
    </div>
  );
}

const METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"];
const BODY_TYPES: { id: RawRequest["body_type"]; label: string }[] = [
  { id: "none", label: "无正文" },
  { id: "json", label: "JSON" },
  { id: "text", label: "纯文本" },
  { id: "form", label: "表单" },
];

export { BODY_TYPES, METHODS };

/**
 * 正文编辑：类型选择与正文原文。
 *
 * 类型选择无条件渲染——选“无正文”时也要能看到并切回 JSON，否则用户一旦切成“无正文”
 * 就没有入口把正文加回来。
 */
export function BodyEditor({
  request,
  readOnly,
  onChange,
  onTypeChange,
  idPrefix = "request",
}: {
  request: RawRequest;
  readOnly: boolean;
  onChange: (body: string) => void;
  onTypeChange: (bodyType: RawRequest["body_type"]) => void;
  idPrefix?: string;
}) {
  return (
    <div className="body-editor">
      <span className="param">
        <label htmlFor={`${idPrefix}-body-type`}>正文类型</label>
        <Select
          id={`${idPrefix}-body-type`}
          value={request.body_type}
          disabled={readOnly}
          aria-label="正文类型"
          data-selected-value={request.body_type}
          onChange={(value: RawRequest["body_type"]) => onTypeChange(value)}
          options={BODY_TYPES.map((item) => ({ value: item.id, label: item.label }))}
        />
      </span>
      {request.body_type === "none" ? (
        <p className="hint">当前没有正文；把正文类型改为 JSON、纯文本或表单即可开始编辑。</p>
      ) : (
        <>
          <label htmlFor={`${idPrefix}-body`}>正文原文</label>
          <Input.TextArea
            id={`${idPrefix}-body`}
            rows={8}
            value={request.body}
            readOnly={readOnly}
            placeholder={request.body_type === "json" ? '{"name": "abc"}' : ""}
            onChange={(event) => onChange(event.target.value)}
          />
          {request.body_type === "json" ? (
            <p className="caption">
              正文按原文保存与发送，数字不会经过 JavaScript 解析，长整数保持原样。
            </p>
          ) : null}
        </>
      )}
    </div>
  );
}

/** 路径补前导斜杠；地址行与导入共用同一处规则。 */
export { ltrimPath };
