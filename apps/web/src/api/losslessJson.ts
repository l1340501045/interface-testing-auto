/**
 * 无损 JSON 展示树：只用于渲染，不用于断言求值。
 *
 * `JSON.parse` 会把 9007199254740993 变成 9007199254740992，因此正文原样扫描为
 * 标记流，数字只保留词法文本。这里生成的路径是**展示用**的可读路径；正式断言
 * 的定位路径由后端字段树接口给出，两者不混用。
 */

export interface DisplayNode {
  label: string;
  /** 展示用类型：object / array / string / number / integer / boolean / null。 */
  type: string;
  text: string;
  /** 展示用路径，例如 `data.items[0].price`；仅供人工识别。 */
  path: string;
  children: DisplayNode[];
}

export class JsonTextError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "JsonTextError";
  }
}

const MAX_DISPLAY_NODES = 2000;

interface Cursor {
  index: number;
  text: string;
}

function skipWhitespace(cursor: Cursor): void {
  while (cursor.index < cursor.text.length && /\s/.test(cursor.text[cursor.index] ?? "")) cursor.index += 1;
}

function readString(cursor: Cursor): string {
  const start = cursor.index;
  cursor.index += 1;
  while (cursor.index < cursor.text.length) {
    const char = cursor.text[cursor.index];
    if (char === "\\") {
      cursor.index += 2;
      continue;
    }
    if (char === '"') {
      cursor.index += 1;
      return JSON.parse(cursor.text.slice(start, cursor.index)) as string;
    }
    cursor.index += 1;
  }
  throw new JsonTextError("字符串未闭合");
}

function readPrimitive(cursor: Cursor): { type: string; text: string } {
  const start = cursor.index;
  while (cursor.index < cursor.text.length && !/[\s,\]}]/.test(cursor.text[cursor.index] ?? "")) {
    cursor.index += 1;
  }
  const raw = cursor.text.slice(start, cursor.index);
  if (raw === "true" || raw === "false") return { type: "boolean", text: raw };
  if (raw === "null") return { type: "null", text: "null" };
  if (/^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$/.test(raw)) {
    return { type: /[.eE]/.test(raw) ? "number" : "integer", text: raw };
  }
  throw new JsonTextError(`无法识别的值：${raw.slice(0, 20)}`);
}

function countNodes(node: DisplayNode): number {
  return 1 + node.children.reduce((sum, child) => sum + countNodes(child), 0);
}

function readValue(cursor: Cursor, label: string, path: string, budget: { left: number }): DisplayNode {
  skipWhitespace(cursor);
  budget.left -= 1;
  const char = cursor.text[cursor.index];
  if (char === undefined) throw new JsonTextError("内容意外结束");
  if (char === "{") {
    cursor.index += 1;
    const node: DisplayNode = { label, type: "object", text: "", path, children: [] };
    skipWhitespace(cursor);
    if (cursor.text[cursor.index] === "}") {
      cursor.index += 1;
      return node;
    }
    for (;;) {
      skipWhitespace(cursor);
      const key = readString(cursor);
      skipWhitespace(cursor);
      if (cursor.text[cursor.index] !== ":") throw new JsonTextError("对象缺少冒号");
      cursor.index += 1;
      if (budget.left <= 0) {
        node.text = "（字段过多，已截断展示）";
        return node;
      }
      node.children.push(readValue(cursor, key, path ? `${path}.${key}` : key, budget));
      skipWhitespace(cursor);
      const next = cursor.text[cursor.index];
      if (next === ",") {
        cursor.index += 1;
        continue;
      }
      if (next === "}") {
        cursor.index += 1;
        return node;
      }
      throw new JsonTextError("对象缺少逗号或右花括号");
    }
  }
  if (char === "[") {
    cursor.index += 1;
    const node: DisplayNode = { label, type: "array", text: "", path, children: [] };
    skipWhitespace(cursor);
    if (cursor.text[cursor.index] === "]") {
      cursor.index += 1;
      return node;
    }
    let position = 0;
    for (;;) {
      if (budget.left <= 0) {
        node.text = "（元素过多，已截断展示）";
        return node;
      }
      node.children.push(readValue(cursor, `[${position}]`, `${path}[${position}]`, budget));
      position += 1;
      skipWhitespace(cursor);
      const next = cursor.text[cursor.index];
      if (next === ",") {
        cursor.index += 1;
        continue;
      }
      if (next === "]") {
        cursor.index += 1;
        return node;
      }
      throw new JsonTextError("数组缺少逗号或右方括号");
    }
  }
  if (char === '"') {
    return { label, type: "string", text: readString(cursor), path, children: [] };
  }
  const primitive = readPrimitive(cursor);
  return { label, type: primitive.type, text: primitive.text, path, children: [] };
}

/** 解析 JSON 原文为展示树；顶层必须是对象或数组。 */
export function buildDisplayTree(text: string): DisplayNode {
  const stripped = text.trim();
  if (!stripped) throw new JsonTextError("正文为空");
  const cursor: Cursor = { index: 0, text: stripped };
  const node = readValue(cursor, "$", "", { left: MAX_DISPLAY_NODES });
  skipWhitespace(cursor);
  if (cursor.index !== cursor.text.length) throw new JsonTextError("正文包含多余内容");
  if (node.type !== "object" && node.type !== "array") {
    throw new JsonTextError("顶层需为对象或数组");
  }
  return node;
}

export function totalNodes(node: DisplayNode): number {
  return countNodes(node);
}
