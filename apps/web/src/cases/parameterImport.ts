import type { RawKeyValue } from "./requestDraft";

export type ParameterImportFormat = "tsv" | "equals" | "headers";

export interface ParameterImportPreview {
  rows: RawKeyValue[];
  duplicateNames: string[];
}

const MAX_BYTES = 1024 * 1024;
const MAX_ROWS = 500;

function fail(line: number, message: string): never {
  throw new Error(`第 ${line} 行：${message}`);
}

function ensureSize(text: string): void {
  if (new TextEncoder().encode(text).byteLength > MAX_BYTES) throw new Error("单次粘贴最多 1 MiB。");
}

function logicalLines(text: string): string[] {
  const lines = text.split(/\r\n|\n/);
  if (lines.at(-1) === "") lines.pop();
  return lines;
}

function parseSeparated(text: string, separator: "=" | ":"): RawKeyValue[] {
  return logicalLines(text).map((line, index) => {
    if (line === "") fail(index + 1, "空白记录不能自动忽略。");
    const split = line.indexOf(separator);
    if (split < 0) fail(index + 1, `缺少分隔符“${separator}”。`);
    const name = line.slice(0, split);
    if (name === "") fail(index + 1, "名称不能为空。");
    let value = line.slice(split + 1);
    if (separator === ":") value = value.replace(/^[ \t]+/, "");
    return { name, value };
  });
}

/** 表格剪贴板解析器：只实现已批准的 TSV 引号规则，不猜测 CSV 方言。 */
function parseTsv(text: string): RawKeyValue[] {
  const records: string[][] = [];
  let record: string[] = [];
  let cell = "";
  let quoted = false;
  let afterQuote = false;
  let line = 1;
  let recordLine = 1;

  const pushCell = () => {
    record.push(cell);
    cell = "";
    afterQuote = false;
  };
  const pushRecord = () => {
    pushCell();
    if (record.length === 1 && record[0] === "") fail(recordLine, "空白记录不能自动忽略。");
    if (record.length !== 2 && record.length !== 3) fail(recordLine, "TSV 每行必须是 2 或 3 列。");
    records.push(record);
    record = [];
    recordLine = line + 1;
  };

  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (quoted) {
      if (char === '"') {
        if (text[index + 1] === '"') {
          cell += '"';
          index += 1;
        } else {
          quoted = false;
          afterQuote = true;
        }
      } else if (char === "\r" && text[index + 1] === "\n") {
        cell += "\r\n";
        index += 1;
        line += 1;
      } else {
        cell += char;
        if (char === "\n") line += 1;
      }
      continue;
    }
    if (afterQuote && char !== "\t" && char !== "\r" && char !== "\n") {
      fail(line, "引号闭合后只能结束单元格或记录。");
    }
    if (char === '"' && cell === "" && !afterQuote) quoted = true;
    else if (char === "\t") pushCell();
    else if (char === "\r" && text[index + 1] === "\n") {
      pushRecord();
      index += 1;
      line += 1;
    } else if (char === "\n") {
      pushRecord();
      line += 1;
    } else if (char === "\r") fail(line, "记录分隔只接受 LF 或 CRLF。");
    else cell += char;
  }
  if (quoted) fail(recordLine, "引号没有闭合。");
  if (record.length > 0 || cell !== "") pushRecord();
  if (records.length === 0) throw new Error("没有可预览的参数。");
  return records.map((columns, index) => {
    if (columns[0] === "") fail(index + 1, "名称不能为空。");
    const description = columns[2] ?? "";
    if ([...description].length > 1024) fail(index + 1, "说明最多 1024 个字符。");
    return { name: columns[0], value: columns[1], description };
  });
}

export function parseParameterImport(text: string, format: ParameterImportFormat): ParameterImportPreview {
  ensureSize(text);
  const rows = format === "tsv" ? parseTsv(text) : parseSeparated(text, format === "equals" ? "=" : ":");
  if (rows.length > MAX_ROWS) throw new Error("单次最多录入 500 行参数。");
  const counts = new Map<string, number>();
  for (const row of rows) counts.set(row.name, (counts.get(row.name) ?? 0) + 1);
  return { rows, duplicateNames: [...counts].filter(([, count]) => count > 1).map(([name]) => name) };
}

/** 特殊字符编辑使用显式转义，避免 input/textarea 把 CRLF 隐式归一化。 */
export function encodeRawText(value: string): string {
  return value.replace(/\\/g, "\\\\").replace(/\r/g, "\\r").replace(/\n/g, "\\n").replace(/\t/g, "\\t");
}

export function decodeRawText(value: string): string {
  let result = "";
  for (let index = 0; index < value.length; index += 1) {
    const char = value[index];
    if (char === "\r" || char === "\n" || char === "\t") {
      throw new Error("转义编辑框中请使用 \\r、\\n 或 \\t 表示特殊字符。");
    }
    if (char !== "\\") {
      result += char;
      continue;
    }
    const escaped = value[index + 1];
    if (escaped === undefined) throw new Error("转义文本末尾不能只有反斜杠。");
    if (escaped === "r") result += "\r";
    else if (escaped === "n") result += "\n";
    else if (escaped === "t") result += "\t";
    else if (escaped === "\\") result += "\\";
    else throw new Error(`不支持的转义：\\${escaped}`);
    index += 1;
  }
  return result;
}

export function needsRawTextEditor(value: string): boolean {
  return /[\r\n\t]/.test(value);
}
