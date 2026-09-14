/**
 * 请求编辑器：方法与路径、查询参数与请求头（可重复）、正文与字段树、断言列。
 *
 * 目标地址不在这里填写：路径只是相对路径，实际 origin 由所选环境决定，
 * 用例无法把请求指向环境白名单之外的目标。导入 cURL 与保存都不会发出请求。
 */
import { useMemo } from "react";

import { ltrimPath, type RawKeyValue, type RawRequest } from "./requestDraft";
export interface KeyValueRowProps {
  rows: RawKeyValue[];
  label: string;
  onChange: (rows: RawKeyValue[]) => void;
  readOnly: boolean;
  addLabel: string;
}

export function KeyValueRows({ rows, label, onChange, readOnly, addLabel }: KeyValueRowProps) {
  function update(index: number, patch: Partial<RawKeyValue>) {
    onChange(rows.map((row, position) => (position === index ? { ...row, ...patch } : row)));
  }
  return (
    <div className="kv-block">
      {rows.length === 0 ? <p className="hint">还没有{label}。</p> : null}
      {rows.map((row, index) => (
        <div className="kv-row" key={index}>
          <input
            aria-label={`${label}名称 ${index + 1}`}
            value={row.name}
            readOnly={readOnly}
            placeholder="名称"
            onChange={(event) => update(index, { name: event.target.value })}
          />
          <input
            aria-label={`${label}值 ${index + 1}`}
            value={row.value}
            readOnly={readOnly}
            placeholder="值"
            onChange={(event) => update(index, { value: event.target.value })}
          />
          {readOnly ? null : (
            <button
              type="button"
              onClick={() => onChange(rows.filter((_, position) => position !== index))}
              aria-label={`删除第 ${index + 1} 项${label}`}
            >
              删除
            </button>
          )}
        </div>
      ))}
      {readOnly ? null : (
        <button type="button" onClick={() => onChange([...rows, { name: "", value: "" }])}>
          {addLabel}
        </button>
      )}
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

export function MethodAndPath({
  request,
  readOnly,
  onChange,
}: {
  request: RawRequest;
  readOnly: boolean;
  onChange: (patch: Partial<RawRequest>) => void;
}) {
  const methods = useMemo(
    () => (METHODS.includes(request.method) ? METHODS : [request.method, ...METHODS]),
    [request.method],
  );
  return (
    <div className="request-line">
      <span className="param">
        <label htmlFor="request-method">方法</label>
        <select
          id="request-method"
          value={request.method}
          disabled={readOnly}
          onChange={(event) => onChange({ method: event.target.value })}
        >
          {methods.map((method) => (
            <option key={method} value={method}>
              {method}
            </option>
          ))}
        </select>
      </span>
      <span className="param grow">
        <label htmlFor="request-path">路径</label>
        <input
          id="request-path"
          value={request.path}
          readOnly={readOnly}
          placeholder="/orders"
          onChange={(event) => onChange({ path: ltrimPath(event.target.value) })}
        />
      </span>
      <span className="param">
        <label htmlFor="request-body-type">正文类型</label>
        <select
          id="request-body-type"
          value={request.body_type}
          disabled={readOnly}
          onChange={(event) =>
            onChange({ body_type: event.target.value as RawRequest["body_type"] })
          }
        >
          {BODY_TYPES.map((item) => (
            <option key={item.id} value={item.id}>
              {item.label}
            </option>
          ))}
        </select>
      </span>
    </div>
  );
}

export function BodyEditor({
  request,
  readOnly,
  onChange,
}: {
  request: RawRequest;
  readOnly: boolean;
  onChange: (body: string) => void;
}) {
  if (request.body_type === "none") return null;
  return (
    <div className="body-editor">
      <label htmlFor="request-body">正文原文</label>
      <textarea
        id="request-body"
        rows={8}
        value={request.body}
        readOnly={readOnly}
        placeholder={request.body_type === "json" ? '{"name": "abc"}' : ""}
        onChange={(event) => onChange(event.target.value)}
      />
      {request.body_type === "json" ? (
        <p className="caption">正文按原文保存与发送，数字不会经过 JavaScript 解析，长整数保持原样。</p>
      ) : null}
    </div>
  );
}
