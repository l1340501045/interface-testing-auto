/**
 * 用例内断言的归属与单条增删改。
 *
 * 断言按“检查位置 + 定位路径”归属到某一字段行，但增删改都以**断言标识**为单位：
 * 一条条件的变化只影响它自己，同一字段上的其他条件原样保留。若按字段整组替换，
 * 只拿着其中一行渲染的调用方（例如“不在样例树中的条件”逐条显示）就会在删除一条
 * 时把整组条件一起丢掉。用例之间天然隔离，修改 A 不会影响 B。
 */
import type { CaseAssertion, LocatorStep } from "../api/types";
import { fieldKey } from "./AssertionColumn";

export function groupByField(assertions: CaseAssertion[]): Map<string, CaseAssertion[]> {
  const groups = new Map<string, CaseAssertion[]>();
  for (const item of assertions) {
    const key = fieldKey(item.target_source, item.selector);
    const bucket = groups.get(key) ?? [];
    bucket.push(item);
    groups.set(key, bucket);
  }
  for (const bucket of groups.values()) bucket.sort((a, b) => a.sort_order - b.sort_order);
  return groups;
}

export function fieldAssertions(
  groups: Map<string, CaseAssertion[]>,
  targetSource: CaseAssertion["target_source"],
  selector: LocatorStep[],
): CaseAssertion[] {
  return groups.get(fieldKey(targetSource, selector)) ?? [];
}

/** 稳定顺序：每条断言的位置由列表顺序决定。 */
function renumber(items: CaseAssertion[]): CaseAssertion[] {
  return items.map((item, index) => ({ ...item, sort_order: index }));
}

/** 新的断言标识：服务端按字符串标识回填结果，不要求是 UUID。 */
export function newAssertionId(): string {
  return globalThis.crypto.randomUUID().replace(/-/g, "");
}

/**
 * 新增或按标识替换一条条件，其余条件（含同字段的其他条件）不受影响。
 *
 * 传入的 id 已存在就是修改该条；不存在就是新增，追加到末尾。
 */
export function upsertAssertion(assertions: CaseAssertion[], next: CaseAssertion): CaseAssertion[] {
  if (assertions.some((item) => item.id === next.id)) {
    return renumber(assertions.map((item) => (item.id === next.id ? next : item)));
  }
  return renumber([...assertions, next]);
}

/** 只删除指定标识的一条条件。 */
export function removeAssertion(assertions: CaseAssertion[], id: string): CaseAssertion[] {
  return renumber(assertions.filter((item) => item.id !== id));
}

/** 新条件的排序值：排到当前最后，不依赖调用方传入的列表长度。 */
export function nextSortOrder(assertions: CaseAssertion[]): number {
  return assertions.reduce((max, item) => Math.max(max, item.sort_order), -1) + 1;
}

/**
 * 路径文本（data.name / items[0].price）转定位步骤；空路径表示响应正文根。
 *
 * 无法完整解析时返回 null，而不是把残渣当成字段名：手工填写的路径会直接保存为
 * 断言条件，若把 `a]b` 悄悄读成 `a.b`，用户看到的条件和真正执行的条件就不一致了。
 */
export function parsePathInput(text: string): LocatorStep[] | null {
  const trimmed = text.trim();
  if (!trimmed || trimmed === "$") return [];
  const steps: LocatorStep[] = [];
  // 三种写法：带引号的字段名、普通字段名、数组下标。带引号的形式与 formatSelector
  // 的输出一致，用户把界面上显示的路径原样粘回输入框就能读回同一份定位。
  const pattern = /\["((?:[^"\\]|\\.)*)"\]|([^.[\]]+)|\[(\d+)\]/g;
  let consumed = 0;
  let match = pattern.exec(trimmed);
  while (match !== null) {
    // 相邻两段之间只允许 "." 或直接进入下标，其他字符说明输入无法按路径解释。
    const gap = trimmed.slice(consumed, match.index);
    if (gap !== "" && gap !== ".") return null;
    consumed = match.index + match[0].length;
    if (match[1] !== undefined) {
      steps.push({ kind: "key", key: match[1].replace(/\\(.)/g, "$1") });
    } else if (match[2] !== undefined) {
      steps.push({ kind: "key", key: match[2] });
    } else {
      steps.push({ kind: "index", index: Number(match[3]) });
    }
    match = pattern.exec(trimmed);
  }
  const tail = trimmed.slice(consumed);
  if (tail !== "" && tail !== ".") return null;
  return steps;
}

const PLAIN_KEY = /^[A-Za-z_][A-Za-z0-9_]*$/;

/** 含点号、方括号、引号或空格的字段名：包成 ["..."] 并对内部引号与反斜杠转义。 */
function quoteKey(key: string): string {
  return `["${key.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"]`;
}

/**
 * 定位步骤转可读路径，只用于界面展示。
 *
 * 页面上直接显示定位的内部结构（一串 JSON 对象）对用户没有意义；这里按
 * data.id、items[0].price 的形式呈现。展示永远是派生值，保存与执行仍用原始
 * 定位步骤，两者不会因为显示形式不同而漂移。字段名含点号、括号或空格时用
 * 引号包起来，保证展示出来的路径能被 parsePathInput 原样读回。
 */
export function formatSelector(selector: LocatorStep[]): string {
  if (selector.length === 0) return "$";
  let text = "";
  for (const step of selector) {
    if (step.kind === "row") {
      text += `${text === "" ? "" : "."}行(${step.row_id.slice(0, 8)}…)`;
      continue;
    }
    if (step.kind === "index") {
      text += `[${step.index}]`;
      continue;
    }
    if (step.kind === "repeat_key") {
      text += PLAIN_KEY.test(step.key) ? `.${step.key}` : quoteKey(step.key);
      text += `[${step.occurrence}]`;
      continue;
    }
    text += PLAIN_KEY.test(step.key) ? `${text === "" ? "" : "."}${step.key}` : quoteKey(step.key);
  }
  return text;
}
