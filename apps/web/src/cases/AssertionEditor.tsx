/**
 * 断言编辑表单：按后端目录的参数定义动态展开，不维护第二套规则。
 *
 * 表单只负责收集参数与调用试算；比较语义全部由后端同一份公共方法决定。
 * 试算是纯计算，不发被测请求，也不产生正式运行报告。
 */
import { useMemo, useState } from "react";

import { ApiError, apiSend, projectPath } from "../api/client";
import { describeValue } from "../api/literals";
import type { AssertionType, CaseAssertion, LocatorStep, ParamField, ValueLiteral } from "../api/types";
import { ErrorText, StatusTag } from "../components/Feedback";
import {
  AssertionFormError,
  buildParameters,
  compareAsLabel,
  describeSelector,
  initialParameters,
  inputTypeOf,
  literalText,
  normalizeForCompare,
  summarize,
} from "./assertionModel";

export interface FieldContext {
  targetSource: CaseAssertion["target_source"];
  selector: LocatorStep[];
  /** 样例值类型，用于筛选适用的断言类型。 */
  fieldType: string;
}

export interface AssertionDraft {
  type: string;
  parameters: Record<string, unknown>;
  compare_as: "number" | "integer" | null;
  severity: "error" | "warning";
  enabled: boolean;
}

/** 试算返回值。后端成功时 message 为空，因此必须同时保留 status 才能显示结论。 */
interface PreviewOutcome {
  status: string;
  message: string;
  expected: unknown;
  actual: unknown;
}

/** 只列出适用于当前字段类型的断言，未实现的能力不出现在选择列表里。 */
export function applicableTypes(types: AssertionType[], fieldType: string): AssertionType[] {
  return types.filter((item) => item.applies_to.includes(fieldType) || item.applies_to.includes("any"));
}

function groupsOf(types: AssertionType[]): { group: string; items: AssertionType[] }[] {
  const groups = new Map<string, AssertionType[]>();
  for (const item of types) {
    const bucket = groups.get(item.group) ?? [];
    bucket.push(item);
    groups.set(item.group, bucket);
  }
  return [...groups.entries()].map(([group, items]) => ({ group, items }));
}

function parametersToInputs(type: AssertionType | null, parameters: unknown): Record<string, unknown> {
  if (!type) return {};
  const defaults = initialParameters(type);
  if (typeof parameters !== "object" || parameters === null) return defaults;
  const stored = parameters as Record<string, unknown>;
  const inputs: Record<string, unknown> = {};
  for (const [name, field] of Object.entries(type.params_schema)) {
    const raw = stored[name];
    if (raw === undefined) {
      inputs[name] = defaults[name];
    } else if (field.control === "switch") {
      inputs[name] = raw === true;
    } else if (field.control === "value_list") {
      inputs[name] = Array.isArray(raw) ? raw.map(literalText).join("\n") : "";
    } else {
      inputs[name] = literalText(raw);
    }
  }
  return inputs;
}

function renderControl(
  idPrefix: string,
  name: string,
  field: ParamField,
  value: unknown,
  onChange: (next: unknown) => void,
) {
  const id = `${idPrefix}-param-${name}`;
  const label = field.label ?? name;
  if (field.control === "switch") {
    return (
      <span className="param-switch" key={name}>
        <input id={id} type="checkbox" checked={value === true} onChange={(e) => onChange(e.target.checked)} />
        <label htmlFor={id}>{label}</label>
      </span>
    );
  }
  if (field.control === "select") {
    return (
      <span className="param" key={name}>
        <label htmlFor={id}>{label}</label>
        <select id={id} value={typeof value === "string" ? value : ""} onChange={(e) => onChange(e.target.value)}>
          {(field.options ?? []).map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      </span>
    );
  }
  if (field.control === "value_list") {
    return (
      <span className="param" key={name}>
        <label htmlFor={id}>{label}（每行一个）</label>
        <textarea
          id={id}
          rows={3}
          value={typeof value === "string" ? value : ""}
          onChange={(e) => onChange(e.target.value)}
        />
      </span>
    );
  }
  return (
    <span className="param" key={name}>
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        // 数字用文本框而不是 number 输入框：number 输入框会经 Number 取值，
        // 长整数在提交前就已经失真。
        type={inputTypeOf(field) === "text" ? "text" : inputTypeOf(field)}
        inputMode={field.control === "number" ? "decimal" : undefined}
        value={typeof value === "string" ? value : ""}
        onChange={(e) => onChange(e.target.value)}
      />
    </span>
  );
}

export function AssertionEditor({
  types,
  field,
  workspaceId,
  projectId,
  initial,
  sample,
  editing,
  onSubmit,
  onCancel,
  idPrefix = "assertion",
}: {
  types: AssertionType[];
  field: FieldContext;
  workspaceId: string;
  projectId: string;
  initial: CaseAssertion | null;
  /** 当前字段的标量样例；为 null 表示没有可用于试算的样例（对象、数组或尚未收到响应）。 */
  sample: ValueLiteral | null;
  editing: boolean;
  onSubmit: (draft: AssertionDraft) => void;
  onCancel: () => void;
  idPrefix?: string;
}) {
  const candidates = useMemo(() => applicableTypes(types, field.fieldType), [types, field.fieldType]);
  const [selected, setSelected] = useState(() => candidates.some((item) => item.id === initial?.type) ? (initial?.type ?? "") : "");
  const type = candidates.find((item) => item.id === selected) ?? null;
  const [inputs, setInputs] = useState<Record<string, unknown>>(() => parametersToInputs(type, initial?.parameters));
  const [compareAs, setCompareAs] = useState<"number" | "integer" | null>(initial?.compare_as ?? null);
  const [severity, setSeverity] = useState<"error" | "warning">(initial?.severity ?? "error");
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<PreviewOutcome | null>(null);
  const [busy, setBusy] = useState(false);

  function chooseType(nextId: string) {
    setSelected(nextId);
    setPreview(null);
    setError(null);
    const next = candidates.find((item) => item.id === nextId) ?? null;
    setInputs(next ? initialParameters(next) : {});
    setCompareAs(null);
  }

  function currentParameters(): Record<string, unknown> {
    if (!type) throw new AssertionFormError("请先选择断言类型");
    // 修改既有断言时把保存的 parameters 一起传下去：字面量类型以保存值为准。
    return buildParameters(type, inputs, field.fieldType, initial?.parameters);
  }

  async function runPreview() {
    setError(null);
    setPreview(null);
    if (!type) {
      setError("请先选择断言类型");
      return;
    }
    let parameters: Record<string, unknown>;
    try {
      parameters = currentParameters();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "参数不合法");
      return;
    }
    setBusy(true);
    try {
      const result = await apiSend(
        projectPath(workspaceId, projectId, "/assertion-previews"),
        "POST",
        {
          type: type.id,
          parameters,
          // compare_as 在真正执行时作用于实际值（例如把查询参数的文本
          // "25" 当数字比较）；试算必须用同样的规则处理样例，否则试算结论
          // 与执行结论会不一致。
          value: sample === null ? null : normalizeForCompare(sample, compareAs),
          found: sample !== null,
          compare_as: compareAs,
        },
        (raw) => raw,
      );
      const record = (result ?? {}) as Record<string, unknown>;
      setPreview({
        status: typeof record.status === "string" ? record.status : "error",
        message: typeof record.message === "string" ? record.message : "",
        expected: record.expected,
        actual: record.actual,
      });
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : cause instanceof Error ? cause.message : "试算失败");
    } finally {
      setBusy(false);
    }
  }

  function submit() {
    if (!type) {
      setError("请先选择断言类型");
      return;
    }
    try {
      const parameters = currentParameters();
      onSubmit({ type: type.id, parameters, compare_as: compareAs, severity, enabled: true });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "参数不合法");
    }
  }

  const groups = groupsOf(candidates);

  return (
    <div className="assertion-editor">
      <p className="caption">
        检查位置：{field.targetSource} · {describeSelector(field.selector)} · 字段类型 {field.fieldType}
        {editing ? "（修改既有断言）" : ""}
      </p>

      <span className="param">
        <label htmlFor={`${idPrefix}-type`}>断言类型</label>
        <select id={`${idPrefix}-type`} value={selected} onChange={(e) => chooseType(e.target.value)}>
          <option value="">请选择</option>
          {groups.map((group) => (
            <optgroup key={group.group} label={group.group}>
              {group.items.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.label}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
      </span>

      {type ? <p className="caption">{type.summary}</p> : null}

      <div className="param-grid">
        {type
          ? Object.entries(type.params_schema).map(([name, paramField]) =>
              renderControl(idPrefix, name, paramField, inputs[name], (next) =>
                setInputs((current) => ({ ...current, [name]: next })),
              ),
            )
          : null}
      </div>

      {type && (type.id === "range" || type.id === "not_range" || type.id === "length_range") ? (
        <p className="caption">默认不包含边界：打开“包含”开关后对应一侧才变成 ≤。</p>
      ) : null}

      <div className="param-row">
        <span className="param">
          <label htmlFor={`${idPrefix}-severity`}>严重级别</label>
          <select
            id={`${idPrefix}-severity`}
            value={severity}
            onChange={(e) => setSeverity(e.target.value === "warning" ? "warning" : "error")}
          >
            <option value="error">失败即判定用例不通过</option>
            <option value="warning">仅提示，不判定失败</option>
          </select>
        </span>
        {field.fieldType === "string" || field.fieldType === "integer" ? (
          <span className="param">
            <label htmlFor={`${idPrefix}-compare`}>比较方式</label>
            <select
              id={`${idPrefix}-compare`}
              value={compareAs ?? ""}
              onChange={(e) => {
                const next = e.target.value;
                setCompareAs(next === "number" || next === "integer" ? next : null);
              }}
            >
              <option value="">按字段自身类型</option>
              <option value="number">按数字比较</option>
              <option value="integer">按整数比较</option>
            </select>
          </span>
        ) : null}
      </div>

      {compareAs && compareAsLabel(compareAs) ? <p className="caption">已选择：{compareAsLabel(compareAs)}</p> : null}

      {error ? <ErrorText message={error} /> : null}
      {preview ? (
        <p className="hint">
          <StatusTag status={preview.status} />
          {preview.message ? ` ${preview.message}` : " 按当前样例判断，未发现问题"}
          {"（期望 "}
          {describeValue(preview.expected)}
          {"，实际 "}
          {describeValue(preview.actual)}
          {"）"}
        </p>
      ) : null}

      <div className="actions">
        <button type="button" onClick={submit}>
          {editing ? "保存这条断言" : "添加这条断言"}
        </button>
        <button type="button" onClick={() => void runPreview()} disabled={busy || sample === null}>
          {busy ? "试算中…" : "用当前样例试算"}
        </button>
        <button type="button" onClick={onCancel}>
          取消
        </button>
      </div>
      <p className="caption">
        {sample === null
          ? "当前字段没有可用的标量样例（对象、数组或响应尚未返回），暂不能试算；配置仍可保存，执行时按真实响应核对。"
          : "试算不发送被测请求，也不写入正式报告；执行时才用本次运行的真实请求与响应核对。"}
      </p>
    </div>
  );
}
