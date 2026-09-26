/**
 * 请求编辑器：可重复的键值行与正文编辑。
 *
 * 方法与路径在地址行（工作台的发送栏）里编辑，不在这里重复渲染第二份：两个输入框绑
 * 同一个字段，改一个另一个不会同步，用户看到的是“打了字没生效”。
 *
 * 目标地址不在这里填写：路径只是相对路径，实际 origin 由所选环境决定，
 * 用例无法把请求指向环境白名单之外的目标。导入 cURL 与保存都不会发出请求。
 */
import { useId, useState, type ClipboardEvent, type ReactNode } from "react";

import { useLeaveReport } from "../hooks/leaveGuard";
import { decodeRawText, encodeRawText, needsRawTextEditor, parseParameterImport, type ParameterImportFormat } from "./parameterImport";
import { ltrimPath, newRequestRow, type RawKeyValue, type RawRequest } from "./requestDraft";
export interface KeyValueRowProps {
  rows: RawKeyValue[];
  label: string;
  onChange: (rows: RawKeyValue[]) => void;
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

function RawTextField({ value, label, readOnly, onChange, pendingKey }: { value: string; label: string; readOnly: boolean; onChange: (value: string) => void; pendingKey?: string }) {
  const localId = useId();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(() => encodeRawText(value));
  const [error, setError] = useState<string | null>(null);
  useLeaveReport(pendingKey ?? `raw-text:${localId}`, {
    dirty: editing && draft !== encodeRawText(value),
    busy: false,
  });
  if (!needsRawTextEditor(value) && !editing) {
    return <input aria-label={label} value={value} readOnly={readOnly} onChange={(event) => onChange(event.target.value)} />;
  }
  if (!editing) {
    return (
      <span className="raw-text-summary">
        <code>{encodeRawText(value)}</code>
        {readOnly ? null : <button type="button" onClick={() => { setDraft(encodeRawText(value)); setEditing(true); }}>编辑原始文本</button>}
      </span>
    );
  }
  return (
    <span className="raw-text-editor">
      <input aria-label={`${label}转义文本`} value={draft} onChange={(event) => setDraft(event.target.value)} />
      <button type="button" onClick={() => { try { onChange(decodeRawText(draft)); setError(null); setEditing(false); } catch (cause) { setError(cause instanceof Error ? cause.message : "转义文本不合法"); } }}>应用</button>
      <button type="button" onClick={() => { setError(null); setEditing(false); }}>取消</button>
      {error ? <span className="error" role="alert">{error}</span> : null}
    </span>
  );
}

function BatchImport({ rows, otherRows, kind, onApply, readOnly, pendingKey, ownerRevision, relatedAssertionCount }: { rows: RawKeyValue[]; otherRows: RawKeyValue[]; kind: "query" | "header"; onApply: (rows: RawKeyValue[]) => void; readOnly: boolean; pendingKey: string; ownerRevision: string; relatedAssertionCount: number }) {
  const [raw, setRaw] = useState("");
  const [format, setFormat] = useState<ParameterImportFormat>(kind === "header" ? "headers" : "equals");
  const [preview, setPreview] = useState<ReturnType<typeof parseParameterImport> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<"append" | "replace">("append");
  const [revision, setRevision] = useState(0);
  const [previewRevision, setPreviewRevision] = useState<number | null>(null);
  const [previewRowsKey, setPreviewRowsKey] = useState<string | null>(null);
  const [appliedRevision, setAppliedRevision] = useState(0);
  const rowsKey = `${JSON.stringify(rows)}|${JSON.stringify(otherRows)}|${ownerRevision}`;
  useLeaveReport(pendingKey, { dirty: raw !== "" && revision !== appliedRevision, busy: false });
  if (readOnly) return null;
  function capture(event: ClipboardEvent<HTMLTextAreaElement>) {
    event.preventDefault();
    const text = event.clipboardData.getData("text/plain");
    setRaw(encodeRawText(text));
    setRevision((value) => value + 1);
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
  return (
    <details className="batch-import">
      <summary>批量录入</summary>
      <p className="caption">直接捕获剪贴板原文；转义框中的 \\r、\\n、\\t、\\\\ 会按字符还原。</p>
      <label>格式
        <select value={format} onChange={(event) => { setFormat(event.target.value as ParameterImportFormat); setPreview(null); }}>
          <option value="tsv">表格 TSV（2～3 列）</option>
          <option value="equals">逐行 名称=值</option>
          {kind === "header" ? <option value="headers">逐行 Header: 值</option> : null}
        </select>
      </label>
      <textarea aria-label={`${kind === "query" ? "Query" : "Header"} 批量原文转义文本`} rows={5} value={raw} onPaste={capture} onChange={(event) => { setRaw(event.target.value); setRevision((value) => value + 1); setPreview(null); }} />
      <label><input type="radio" checked={mode === "append"} onChange={() => setMode("append")} />追加</label>
      <label><input type="radio" checked={mode === "replace"} onChange={() => setMode("replace")} />替换</label>
      <button type="button" onClick={createPreview}>生成预览</button>
      {error ? <p className="error" role="alert">{error}</p> : null}
      {preview ? (
        <div className="batch-preview">
          <p>共 {preview.rows.length} 行{preview.duplicateNames.length ? `；重复名称：${preview.duplicateNames.join("、")}` : ""}。</p>
          {mode === "replace" ? <p className="caption">替换将移除当前 {rows.length} 行，并使 {relatedAssertionCount} 条行条件显示为“字段已删除”；不会自动重绑。</p> : null}
          <ol>{preview.rows.map((row, index) => <li key={`${index}-${row.name}`}><code>{encodeRawText(row.name)}</code> = <code>{encodeRawText(row.value)}</code></li>)}</ol>
          <button type="button" onClick={() => {
            if (previewRevision !== revision || previewRowsKey !== rowsKey) { setError("批量原文或参数表已改变，请重新预览。"); return; }
            const imported = preview.rows.map((row) => newRequestRow(row));
            const nextCount = (mode === "append" ? rows.length + imported.length : imported.length) + otherRows.length;
            if (nextCount > 500) { setError("应用后 Query 和 Header 合计不能超过 500 行。"); return; }
            onApply(mode === "append" ? [...rows, ...imported] : imported);
            setAppliedRevision(revision);
            setPreview(null);
          }}>应用{mode === "append" ? "追加" : "替换"}</button>
          <button type="button" onClick={() => setPreview(null)}>取消</button>
        </div>
      ) : null}
    </details>
  );
}

export function KeyValueRows({ rows, label, onChange, readOnly, addLabel, idPrefix = "request", kind = "query", version = 1, assertionSlot, pendingPrefix = idPrefix, otherRows = [], ownerRevision = "", relatedAssertionCount = 0 }: KeyValueRowProps) {
  function update(index: number, patch: Partial<RawKeyValue>) {
    onChange(rows.map((row, position) => (position === index ? { ...row, ...patch } : row)));
  }
  return (
    <div className="kv-block">
      {rows.length === 0 ? <p className="hint">还没有{label}。</p> : null}
      {rows.map((row, index) => (
        <div className={`kv-row${row.enabled === false ? " kv-row-disabled" : ""}`} key={row.row_id ?? `${idPrefix}-${index}`}>
          {version === 2 ? <label className="kv-enabled"><input type="checkbox" checked={row.enabled === true} disabled={readOnly} onChange={(event) => update(index, { enabled: event.target.checked })} />发送</label> : null}
          <RawTextField pendingKey={`${pendingPrefix}:raw-${kind}-${row.row_id ?? index}-name`} label={`${label}名称 ${index + 1}`} value={row.name} readOnly={readOnly} onChange={(name) => update(index, { name })} />
          <RawTextField pendingKey={`${pendingPrefix}:raw-${kind}-${row.row_id ?? index}-value`} label={`${label}值 ${index + 1}`} value={row.value} readOnly={readOnly} onChange={(value) => update(index, { value })} />
          {version === 2 ? <RawTextField pendingKey={`${pendingPrefix}:raw-${kind}-${row.row_id ?? index}-description`} label={`${label}说明 ${index + 1}`} value={row.description ?? ""} readOnly={readOnly} onChange={(description) => update(index, { description })} /> : null}
          {assertionSlot?.(row, index)}
          {readOnly ? null : (
            <span className="inline-actions">
              <button type="button" disabled={index === 0} onClick={() => { const next = [...rows]; [next[index - 1], next[index]] = [next[index], next[index - 1]]; onChange(next); }} aria-label={`上移第 ${index + 1} 项${label}`}>上移</button>
              <button type="button" disabled={index === rows.length - 1} onClick={() => { const next = [...rows]; [next[index], next[index + 1]] = [next[index + 1], next[index]]; onChange(next); }} aria-label={`下移第 ${index + 1} 项${label}`}>下移</button>
              <button type="button" onClick={() => onChange(rows.filter((_, position) => position !== index))} aria-label={`删除第 ${index + 1} 项${label}`}>删除</button>
            </span>
          )}
        </div>
      ))}
      {readOnly ? null : (
        <button type="button" onClick={() => onChange([...rows, version === 2 ? newRequestRow() : { name: "", value: "" }])}>
          {addLabel}
        </button>
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
        <select
          id={`${idPrefix}-body-type`}
          value={request.body_type}
          disabled={readOnly}
          onChange={(event) => onTypeChange(event.target.value as RawRequest["body_type"])}
        >
          {BODY_TYPES.map((item) => (
            <option key={item.id} value={item.id}>
              {item.label}
            </option>
          ))}
        </select>
      </span>
      {request.body_type === "none" ? (
        <p className="hint">当前没有正文；把正文类型改为 JSON、纯文本或表单即可开始编辑。</p>
      ) : (
        <>
          <label htmlFor={`${idPrefix}-body`}>正文原文</label>
          <textarea
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
