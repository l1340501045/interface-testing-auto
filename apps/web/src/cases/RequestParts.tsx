/**
 * 请求编辑器：可重复的键值行与正文编辑。
 *
 * 方法与路径在地址行（工作台的发送栏）里编辑，不在这里重复渲染第二份：两个输入框绑
 * 同一个字段，改一个另一个不会同步，用户看到的是“打了字没生效”。
 *
 * 目标地址不在这里填写：路径只是相对路径，实际 origin 由所选环境决定，
 * 用例无法把请求指向环境白名单之外的目标。导入 cURL 与保存都不会发出请求。
 */
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
}: {
  request: RawRequest;
  readOnly: boolean;
  onChange: (body: string) => void;
  onTypeChange: (bodyType: RawRequest["body_type"]) => void;
}) {
  return (
    <div className="body-editor">
      <span className="param">
        <label htmlFor="request-body-type">正文类型</label>
        <select
          id="request-body-type"
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
