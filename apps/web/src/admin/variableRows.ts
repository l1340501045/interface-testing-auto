/**
 * 普通变量的编辑模型。
 *
 * 已保存的 ValueLiteral 是**类型真相**：载入时按它自己的 type 决定行的类型，保存时
 * 未显式改过的值原样回传。数值与字符串都以文本承载，不经过 Number，避免
 * 9007199254740993 这类长整数在界面上被四舍五入。
 *
 * 这里曾经把 json、null 以及任何认不出的类型统一降级成字符串：用户只改环境地址再
 * 保存，JSON 变量就变成了字符串变量，null 变成长度为 4 的文本 "null"，而界面上
 * 看不出发生过什么。降级不是兼容，是悄悄改写用户的数据，因此：
 * - string／number／boolean／null／json 都能无损回填并原样保存；
 * - 认不出的类型只读展示，保存时原样保留，不猜也不降级。
 *
 * 这里不提供“秘密”类型：秘密只能通过身份凭证配置保存，界面不给入口也就不会有
 * 用户把口令贴进普通变量再从执行结果里看到它。
 */
import { literalToInput } from "../api/literals";
import type { LiteralType, VariableItem } from "../api/types";

export type VariableKind = LiteralType | "unknown";

/** 可编辑的类型；unknown 只读，不参与重编码。 */
export type EditableKind = LiteralType;

export const KIND_LABELS: Record<VariableKind, string> = {
  string: "文本",
  number: "数字",
  boolean: "布尔",
  null: "空值（null）",
  json: "JSON",
  unknown: "无法识别的类型（只读）",
};

/** 行 → 后端字面量；布尔值只有 true/false 两种取值。 */
export interface VariableRow {
  name: string;
  kind: VariableKind;
  /** 编辑中的文本；null 没有文本形态，unknown 的文本仅供参考。 */
  text: string;
  /** 载入时的原始字面量；未改动时原样回传。 */
  original: unknown;
  /** 用户是否显式改过值或类型；没改过就不重编码。 */
  changed: boolean;
}

/** 新加的空行：是用户造出来的，按“已改动”对待。 */
export const EMPTY_ROW: VariableRow = { name: "", kind: "string", text: "", original: undefined, changed: true };

export function emptyRow(): VariableRow {
  return { ...EMPTY_ROW };
}

/** 认不识的类型只在界面上展示成文本，不参与任何编码。 */
function describeUnknown(value: unknown): string {
  if (value === undefined) return "";
  try {
    return JSON.stringify(value) ?? String(value);
  } catch {
    return String(value);
  }
}

/** 一行变量的可读值，用于列表摘要；null 与 JSON 都按各自的真实形态显示。 */
export function describeLiteralText(value: unknown): string {
  const parsed = literalToInput(value);
  if (parsed === null) return describeUnknown(value);
  return parsed.type === "null" ? "null" : parsed.text;
}

/** 后端字面量 → 行；认不出的类型标记为 unknown 并只读展示。 */
export function toRow(item: VariableItem): VariableRow {
  const parsed = literalToInput(item.value);
  if (parsed === null) {
    return { name: item.name, kind: "unknown", text: describeUnknown(item.value), original: item.value, changed: false };
  }
  return { name: item.name, kind: parsed.type, text: parsed.text, original: item.value, changed: false };
}

export function toRows(items: VariableItem[]): VariableRow[] {
  return items.map(toRow);
}

/** 行 → 后端字面量。 */
export function toLiteral(row: VariableRow): unknown {
  if (!row.changed) return row.original;
  switch (row.kind) {
    case "boolean":
      return { type: "boolean", value: row.text === "true" };
    case "null":
      return { type: "null" };
    case "json":
      return { type: "json", text: row.text };
    case "number":
      return { type: "number", text: row.text };
    case "string":
      return { type: "string", text: row.text };
    case "unknown":
      // 认不出的类型不重编码：只读行不可能被改动，这里只是兜底。
      return row.original;
  }
}

/**
 * 本地校验：空名称与重名都在提交前挡住，并给出可操作的中文提示。
 *
 * 只校验**改动过**的值：未改动的值原样回传，服务端已经接受过一次，不该因为前端
 * 的词法规则更窄而让用户连“只改环境地址”都保存不了。
 */
export function validateRows(rows: VariableRow[]): string | null {
  const seen = new Set<string>();
  for (const row of rows) {
    const name = row.name.trim();
    if (!name) return "变量名不能为空。";
    if (seen.has(name)) return `变量名重复：${name}`;
    seen.add(name);
    if (!row.changed) continue;
    if (row.kind === "number") {
      const text = row.text.trim();
      if (!text) return `变量 ${name} 的值不能为空。`;
      if (!/^-?\d+(\.\d+)?$/.test(text)) return `变量 ${name} 的值不是合法数字。`;
    }
    if (row.kind === "json" && row.text.trim() === "") {
      return `变量 ${name} 的 JSON 值不能为空。`;
    }
  }
  return null;
}

export function toPayload(rows: VariableRow[]): VariableItem[] {
  return rows.map((row) => ({ name: row.name.trim(), value: toLiteral(row) }));
}

/**
 * 两个字面量是否完全相同（含类型与结构）。
 *
 * 判断“有没有未保存的修改”必须看**要提交的那一份字面量**，而不是它的显示文本：
 * 数字 1 与字符串 "1" 的显示文本都是 "1"，只改类型时脏状态就算不出来，用户以为
 * 什么都没动就切走了范围。递归比较保留类型，未知类型的 JSON 结构也一视同仁；
 * 键的顺序不算差别。
 */
export function sameLiteral(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  if (left === null || right === null) return false;
  if (typeof left !== "object" || typeof right !== "object") return false;
  if (Array.isArray(left) || Array.isArray(right)) {
    if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) return false;
    return left.every((item, index) => sameLiteral(item, right[index]));
  }
  const leftRecord = left as Record<string, unknown>;
  const rightRecord = right as Record<string, unknown>;
  const leftKeys = Object.keys(leftRecord);
  if (leftKeys.length !== Object.keys(rightRecord).length) return false;
  return leftKeys.every(
    (key) =>
      Object.prototype.hasOwnProperty.call(rightRecord, key) &&
      sameLiteral(leftRecord[key], rightRecord[key]),
  );
}

/**
 * 改动一行：只有值或类型变了才算 changed。
 *
 * 只改名称不重编码值——载入时是什么字面量，保存时就还是什么字面量，名称与值
 * 是两件互不相干的事。
 */
export function editRow(
  row: VariableRow,
  patch: { name?: string; kind?: EditableKind; text?: string },
): VariableRow {
  const next: VariableRow = { ...row, ...patch };
  if (patch.kind !== undefined || patch.text !== undefined) next.changed = true;
  return next;
}

/** 切换类型时把文本整理成该类型可用的形态，避免留下上一个类型的残值。 */
export function textForKind(kind: EditableKind, text: string): string {
  switch (kind) {
    case "boolean":
      return text === "true" ? "true" : "false";
    case "null":
      return "";
    case "number":
      return text === "" ? "" : text;
    default:
      return text;
  }
}
