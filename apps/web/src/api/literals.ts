/**
 * 界面输入与 ValueLiteral 之间的无损转换。
 *
 * 断言参数里的数字必须以十进制文本提交：把用户输入先 `Number(...)` 再序列化，
 * 9007199254740993 会在这一步变成 9007199254740992，之后后端再精确也无济于事。
 * 因此这里只做词法校验，不做数值转换。
 */
import type { LiteralType, ValueLiteral } from "./types";

const NUMBER_PATTERN = /^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$/;

export class LiteralInputError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "LiteralInputError";
  }
}

/** 用户输入的数字文本是否合法；不检查范围，不转成 float。 */
export function isValidNumberText(text: string): boolean {
  return NUMBER_PATTERN.test(text.trim());
}

export function numberLiteral(text: string): ValueLiteral {
  const trimmed = text.trim();
  if (!isValidNumberText(trimmed)) {
    throw new LiteralInputError("请输入合法的数字，例如 0、100 或 9007199254740993");
  }
  return { type: "number", text: trimmed };
}

export function stringLiteral(text: string): ValueLiteral {
  return { type: "string", text };
}

/** JSON 字面量保持原文；由后端做无损解析，前端不 parse。 */
export function jsonLiteral(text: string): ValueLiteral {
  return { type: "json", text };
}

export function literalFromInput(type: LiteralType, text: string): ValueLiteral {
  switch (type) {
    case "number":
      return numberLiteral(text);
    case "string":
      return stringLiteral(text);
    case "json":
      return jsonLiteral(text);
    case "boolean":
      return { type: "boolean", value: text === "true" };
    case "null":
      return { type: "null" };
    default: {
      const exhaustive: never = type;
      throw new LiteralInputError(`未知字面量类型：${String(exhaustive)}`);
    }
  }
}

/** 从后端返回的字面量对象读取可编辑文本；非法结构返回空串而不是抛错。 */
export function literalToInput(raw: unknown): { type: LiteralType; text: string } | null {
  if (typeof raw !== "object" || raw === null) return null;
  const record = raw as Record<string, unknown>;
  const type = record.type;
  if (type === "number" || type === "string" || type === "json") {
    return typeof record.text === "string" ? { type, text: record.text } : null;
  }
  if (type === "boolean") return { type, text: record.value === true ? "true" : "false" };
  if (type === "null") return { type, text: "" };
  return null;
}

/** 把任意后端返回的期望值渲染为可读文本，长数字保持原样。 */
export function describeValue(value: unknown): string {
  if (value === null || value === undefined) return "（无）";
  if (typeof value === "string") return value;
  if (typeof value === "number") return String(value);
  if (typeof value === "boolean") return value ? "true" : "false";
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}
