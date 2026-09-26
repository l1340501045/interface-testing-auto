/**
 * 断言的展示与参数构造：把目录中的参数定义变成可读条件与可提交参数。
 *
 * 这里只负责界面表达与参数组装，不实现任何比较语义——试算与执行都由后端同一
 * 份公共方法完成，前端复制一套判断只会产生两个互相矛盾的真相。
 */
import { describeValue, isValidNumberText, literalFromInput, literalToInput, numberLiteral } from "../api/literals";
import type { AssertionType, LiteralType, LocatorStep, ParamField, ValueLiteral } from "../api/types";

export class AssertionFormError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "AssertionFormError";
  }
}

const STEP_LABELS: Record<LocatorStep["kind"], string> = {
  key: "字段",
  index: "下标",
  repeat_key: "重复键",
  row: "参数行",
};

/** 把定位步骤渲染为中文路径，例如 `data.items[0].price` 或 `query.tag 第 2 次`。 */
export function describeSelector(selector: LocatorStep[]): string {
  if (selector.length === 0) return "当前字段";
  return selector
    .map((step) => {
      switch (step.kind) {
        case "key":
          return step.key;
        case "index":
          return `[${step.index}]`;
        case "repeat_key":
          return `${step.key}（第 ${step.occurrence + 1} 次）`;
        case "row":
          return `参数行 ${step.row_id.slice(0, 8)}…`;
        default: {
          const exhaustive: never = step;
          throw new AssertionFormError(`未知定位步骤：${String(exhaustive)}`);
        }
      }
    })
    .join(".")
    .replace(/\.\[/g, "[");
}

export function stepLabel(kind: LocatorStep["kind"]): string {
  return STEP_LABELS[kind];
}

/** 按参数定义为控件生成初始值；区间类默认关闭边界，与产品默认一致。 */
export function defaultParameters(field: ParamField): unknown {
  if (field.control === "switch") return field.default ?? false;
  if (field.control === "value_list") return "";
  if (field.control === "select") return field.options?.[0] ?? "";
  return "";
}

export function initialParameters(type: AssertionType): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const [name, field] of Object.entries(type.params_schema)) {
    result[name] = defaultParameters(field);
  }
  return result;
}

function textValue(raw: unknown): string {
  return typeof raw === "string" ? raw : raw === undefined || raw === null ? "" : String(raw);
}

/**
 * 校验并组装提交给后端的 parameters。
 *
 * 数字边界必须原样以十进制文本提交；这里做词法校验后立即失败，避免把
 * `Number("")` 得到的 0 当成用户填的 0 送出去。
 *
 * 期望值与候选集合都按 **ValueLiteral 类型** 编码，类型来自被比较字段自身：
 * 目录里“等于／属于”这类参数声明为 `any`，含义是“字面量类型跟着字段走”。
 * 布尔字段的候选若一律编成字符串 "true"，后端拿字符串和布尔值严格比较永远不等，
 * 而报告里两边文本还长得一模一样；对象／数组候选编成字符串同样不可能相等。
 * JSON 原文直接透传，前端不 parse，长数字在往返保存中不失真。
 *
 * **已保存的字面量是类型真相**：修改一条既有断言时，参数的编码类型取自它自己保存
 * 下来的字面量，而不是界面对字段类型的推断。否则“只改一下严重级别”也会把
 * `{type:"number",text:"9007199254740993"}` 重新编码成文本字面量——数值比较悄悄
 * 变成文本比较，界面上看不出任何变化。字段推断只用于**新建**断言（没有保存真相
 * 可依）以及这次新加的候选值。
 */
export function buildParameters(
  type: AssertionType,
  inputs: Record<string, unknown>,
  valueType: string,
  /** 这条断言已保存的 parameters；有保存值就按它的字面量类型重编码。 */
  saved?: unknown,
): Record<string, unknown> {
  const savedParams = (typeof saved === "object" && saved !== null ? saved : {}) as Record<string, unknown>;
  const result: Record<string, unknown> = {};
  for (const [name, field] of Object.entries(type.params_schema)) {
    const raw = inputs[name];
    const label = field.label ?? name;
    const inferred = literalTypeFor(field.type === "any" ? valueType : field.type);
    const savedRaw = savedParams[name];
    const savedItems = Array.isArray(savedRaw) ? (savedRaw as unknown[]) : [];
    const savedKind = savedLiteralKind(savedRaw);
    const literalType = savedKind === null || savedKind === "unknown" ? inferred : savedKind;
    switch (field.control) {
      case "switch":
        result[name] = raw === true;
        break;
      case "select": {
        const value = textValue(raw).trim();
        if (!value) throw new AssertionFormError(`请选择${label}`);
        if (field.options && !field.options.includes(value)) {
          throw new AssertionFormError(`${label} 只能是：${field.options.join("、")}`);
        }
        result[name] = value;
        break;
      }
      case "number": {
        const text = textValue(raw).trim();
        if (!text) throw new AssertionFormError(`请填写${label}`);
        if (!isValidNumberText(text)) {
          throw new AssertionFormError(`${label} 必须是数字，例如 0、100 或 9007199254740993`);
        }
        result[name] = numberLiteral(text);
        break;
      }
      case "value_list": {
        const items = textValue(raw)
          .split("\n")
          .map((item) => item.trim())
          .filter((item) => item.length > 0);
        if (items.length === 0) throw new AssertionFormError(`${label}至少填写一项`);
        if (literalType === "null" && items.length > 1) {
          throw new AssertionFormError(`${label}只能有一个候选值：null 字段的取值只有 null`);
        }
        result[name] = items.map((item, index) => {
          // 候选值逐个按自己的保存类型还原：一个集合里可以同时有数字与文本候选。
          const savedItem = savedItems[index];
          const itemKind = savedLiteralKind(savedItem);
          if (itemKind === "unknown") {
            return keepUnknown(savedItem, item, `${label}里的候选值`);
          }
          const itemType = itemKind ?? literalType;
          try {
            return literalFromInput(itemType, item);
          } catch (cause) {
            throw new AssertionFormError(
              `${label}里的每一项都要符合${literalLabel(itemType)}：${cause instanceof Error ? cause.message : item}`,
            );
          }
        });
        break;
      }
      case "value": {
        const text = textValue(raw);
        if (text === "") throw new AssertionFormError(`请填写${label}`);
        if (savedKind === "unknown") {
          result[name] = keepUnknown(savedRaw, text, label);
          break;
        }
        try {
          result[name] = literalFromInput(literalType, text);
        } catch (cause) {
          throw new AssertionFormError(
            `${label}要符合${literalLabel(literalType)}：${cause instanceof Error ? cause.message : text}`,
          );
        }
        break;
      }
      default: {
        const exhaustive: never = field.control;
        throw new AssertionFormError(`未知控件：${String(exhaustive)}`);
      }
    }
  }
  return result;
}

/** 已保存字面量自己的类型；认不出的结构返回 null，由调用方回退到字段推断。 */
export function literalTypeOf(raw: unknown): LiteralType | null {
  return literalToInput(raw)?.type ?? null;
}

/**
 * 已保存参数的形态：解析得出的字面量类型、`unknown`（存过但认不出），或没有保存值。
 *
 * 认不出必须与“没有保存值”分开：前者是**已存在的用户数据**，界面不能拿字段推断
 * 去重写它；后者只是这条断言还没配过这个参数，按字段类型给个默认编码即可。
 */
function savedLiteralKind(raw: unknown): LiteralType | "unknown" | null {
  if (raw === undefined) return null;
  const parsed = literalToInput(raw);
  return parsed === null ? "unknown" : parsed.type;
}

/**
 * 认不出类型的已保存值：原样保留，或者明确拒绝。
 *
 * 界面把认不出的字面量按原文展示，用户没动它就原样回传——降级成字符串会改写别人的
 * 数据，而界面上看不出差别。用户改了文本就说明他以为自己在改一个能识别的值，这时
 * 只能拒绝并说清原因，不能猜。
 */
function keepUnknown(saved: unknown, text: string, label: string): unknown {
  if (text === describeValue(saved)) return saved;
  throw new AssertionFormError(
    `${label}的类型无法识别，界面不能安全改写；请删除这条断言后重新配置。`,
  );
}

/** 字段类型到 ValueLiteral 类型的映射；对象／数组按 JSON 原文传递。 */
export function literalTypeFor(fieldType: string): LiteralType {  switch (fieldType) {
    case "number":
    case "integer":
      return "number";
    case "boolean":
      return "boolean";
    case "null":
      return "null";
    case "object":
    case "array":
      return "json";
    default:
      return "string";
  }
}

function literalLabel(literalType: LiteralType): string {
  switch (literalType) {
    case "number":
      return "数字";
    case "boolean":
      return "true 或 false";
    case "null":
      return "null";
    case "json":
      return "合法 JSON 原文";
    default:
      return "文本";
  }
}

const CONTROL_INPUT_TYPES: Record<ParamField["control"], string> = {
  value: "text",
  value_list: "text",
  number: "text",
  switch: "checkbox",
  select: "select",
};

/**
 * 把后端保存的字面量还原成界面可编辑文本。
 *
 * 布尔、null、JSON 都必须能回填：用户改完别的字段再保存时，这些条件若读不出文本，
 * 就会以空值重新提交，原本配好的条件被自己悄悄清空。null 没有文本形态，用 "null"
 * 占位，保证集合里的 null 候选在保存往返后仍然存在。
 */
export function literalText(raw: unknown): string {
  const parsed = literalToInput(raw);
  if (parsed === null) return describeValue(raw);
  return parsed.type === "null" ? "null" : parsed.text;
}

/**
 * 按“比较方式”把样例值转成数值字面量，与执行内核的 normalize_for_compare 同义。
 *
 * Query、Header 这类线上值本身就是文本，用户勾选“按数字比较”后执行时会先转成
 * 数字再比较；试算不跟着转就会得出与执行相反的结论。数字文本不合法时原样返回，
 * 由后端按类型不符报错，不在前端悄悄猜一个数。
 */
export function normalizeForCompare(sample: ValueLiteral, compareAs: "number" | "integer" | null): ValueLiteral {
  if (compareAs === null || sample.type !== "string") return sample;
  const text = sample.text.trim();
  return isValidNumberText(text) ? numberLiteral(text) : sample;
}

export function inputTypeOf(field: ParamField): string {
  return CONTROL_INPUT_TYPES[field.control];
}

/** 可读条件标签，例如「区间：0 < 值 < 100」；参数无法解析时退回类型摘要。 */
export function summarize(type: AssertionType, parameters: unknown): string {
  if (typeof parameters !== "object" || parameters === null) return type.summary;
  const params = parameters as Record<string, unknown>;
  const parts: string[] = [];

  // 参数值可能是数字、字符串、布尔、null 或 JSON 字面量；只读 .text 会让
  // 布尔与 null 条件显示成空白，用户看不出这条断言到底配了什么。
  const bound = (name: string): string | null => {
    const raw = params[name];
    if (typeof raw !== "object" || raw === null) return null;
    const literal = literalToInput(raw);
    return literal === null ? null : literal.type === "null" ? "null" : literal.text;
  };

  if (type.id === "range" || type.id === "not_range" || type.id === "length_range") {
    const min = bound("min");
    const max = bound("max");
    if (min !== null && max !== null) {
      const left = params.include_min === true ? "≤" : "<";
      const right = params.include_max === true ? "≤" : "<";
      parts.push(`${min} ${left} 值 ${right} ${max}`);
    }
  } else if (bound("expected") !== null) {
    parts.push(`${type.label} ${bound("expected")}`);
  } else if (Array.isArray(params.values)) {
    const rendered = params.values.map((item) => literalText(item)).filter((item) => item.length > 0);
    if (rendered.length > 0) parts.push(`${type.label} ${rendered.join("、")}`);
  } else if (typeof params.type === "string") {
    parts.push(`${type.label} ${params.type}`);
  }

  return parts.length > 0 ? parts.join("；") : type.summary;
}

export function compareAsLabel(value: string | null): string | null {
  if (value === "number") return "按数字比较";
  if (value === "integer") return "按整数比较";
  return null;
}
