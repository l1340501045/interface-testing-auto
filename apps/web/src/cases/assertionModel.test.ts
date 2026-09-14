/**
 * 断言参数按 ValueLiteral 类型编码与回填的行为回归。
 *
 * 目录里“等于／属于”的参数声明为 `any`，含义是字面量类型跟着被比较字段走。
 * 若一律编成字符串，布尔字段的候选 "true" 与布尔值 true 严格比较永远不等，
 * 而报告里两边文本长得一模一样；对象／数组候选编成字符串同样不可能相等。
 * 反过来，回填时只读 .text 会丢掉布尔／null／JSON，用户改完别的字段再保存
 * 就把原条件悄悄清空。这里同时固定“编码”和“往返”两端。
 *
 * 另：数字始终以十进制文本原样提交，9007199254740993 不能经过 Number。
 */
import { describe, expect, it } from "vitest";

import type { AssertionType, CaseAssertion } from "../api/types";
import { buildParameters, literalText, summarize } from "./assertionModel";

const ANY_VALUE = { control: "value" as const, type: "any", label: "期望值" };
const ANY_LIST = { control: "value_list" as const, type: "any", label: "候选值" };

const EQUALS: AssertionType = {
  id: "equals",
  label: "等于",
  group: "值与集合",
  applies_to: ["string", "number", "integer", "boolean", "null", "object", "array"],
  params_schema: { expected: ANY_VALUE },
  summary: "值严格等于期望值",
  operator_version: 1,
};

const IN_SET: AssertionType = {
  id: "in_set",
  label: "属于",
  group: "值与集合",
  applies_to: ["string", "number", "integer", "boolean", "null", "object", "array"],
  params_schema: { values: ANY_LIST },
  summary: "值属于给定集合之一",
  operator_version: 1,
};

describe("buildParameters 按字段类型编码字面量", () => {
  it("布尔字段的期望值编码为布尔字面量，不是字符串", () => {
    expect(buildParameters(EQUALS, { expected: "true" }, "boolean")).toEqual({
      expected: { type: "boolean", value: true },
    });
    expect(buildParameters(EQUALS, { expected: "false" }, "boolean")).toEqual({
      expected: { type: "boolean", value: false },
    });
  });

  it("null 字段的期望值编码为 null 字面量，并忽略输入文本", () => {
    expect(buildParameters(EQUALS, { expected: "null" }, "null")).toEqual({
      expected: { type: "null" },
    });
  });

  it("对象与数组字段按 JSON 原文编码，长数字不经过 Number", () => {
    const objectText = '{"id": 9007199254740993}';
    expect(buildParameters(EQUALS, { expected: objectText }, "object")).toEqual({
      expected: { type: "json", text: objectText },
    });
    expect(buildParameters(EQUALS, { expected: "[1, 2, 3]" }, "array")).toEqual({
      expected: { type: "json", text: "[1, 2, 3]" },
    });
  });

  it("数字字段仍用十进制文本，字符串 0 不与数字 0 混淆", () => {
    expect(buildParameters(EQUALS, { expected: "9007199254740993" }, "integer")).toEqual({
      expected: { type: "number", text: "9007199254740993" },
    });
    expect(buildParameters(EQUALS, { expected: "0" }, "string")).toEqual({
      expected: { type: "string", text: "0" },
    });
  });

  it("集合候选逐项按字段类型编码，布尔集合不是一串字符串", () => {
    expect(buildParameters(IN_SET, { values: "true\nfalse" }, "boolean")).toEqual({
      values: [
        { type: "boolean", value: true },
        { type: "boolean", value: false },
      ],
    });
  });

  it("集合候选里的 JSON 元素保持原文", () => {
    expect(buildParameters(IN_SET, { values: '{"a": 1}\n{"a": 2}' }, "object")).toEqual({
      values: [
        { type: "json", text: '{"a": 1}' },
        { type: "json", text: '{"a": 2}' },
      ],
    });
  });

  it("非法的数字文本当场报错，不静默取 0", () => {
    expect(() => buildParameters(EQUALS, { expected: "abc" }, "number")).toThrow(/数字/);
  });
});

describe("literalText 回填", () => {
  it("布尔、null、JSON 都能读回界面文本", () => {
    expect(literalText({ type: "boolean", value: true })).toBe("true");
    expect(literalText({ type: "boolean", value: false })).toBe("false");
    expect(literalText({ type: "null" })).toBe("null");
    expect(literalText({ type: "json", text: '{"a": 1}' })).toBe('{"a": 1}');
    expect(literalText({ type: "number", text: "9007199254740993" })).toBe("9007199254740993");
  });
});

describe("保存往返", () => {
  it("布尔条件的参数回填后再保存，内容不发生变化", () => {
    const saved = { expected: { type: "boolean", value: false } };
    const backfilled = literalText(saved.expected);
    expect(buildParameters(EQUALS, { expected: backfilled }, "boolean")).toEqual(saved);
  });

  it("集合里的全部候选往返后仍是原来那些字面量", () => {
    const saved = {
      values: [
        { type: "boolean", value: true },
        { type: "null" },
        { type: "json", text: "[1, 2]" },
      ],
    };
    const text = (saved.values as unknown[]).map(literalText).join("\n");
    expect(text).toBe("true\nnull\n[1, 2]");
  });

  it("对象条件的 JSON 原文往返保持字面一致，长整数不失真", () => {
    const original = '{"id": 9007199254740993}';
    const saved = buildParameters(EQUALS, { expected: original }, "object");
    const back = literalText((saved.expected as Record<string, unknown>));
    expect(back).toBe(original);
    expect(buildParameters(EQUALS, { expected: back }, "object")).toEqual(saved);
  });
});

describe("已保存的字面量是类型真相", () => {
  // 修改一条既有断言时，参数类型必须取自它自己保存的字面量，而不是界面推断的
  // 字段类型。否则“只改一下严重级别”也会把数字期望值重新编码成文本，数值比较
  // 悄悄变成文本比较，界面上看不出任何变化。
  it("字段被推断为文本，但保存的是数字字面量时仍按数字原样提交", () => {
    const saved = { expected: { type: "number", text: "9007199254740993" } };
    const backfilled = literalText(saved.expected);
    expect(backfilled).toBe("9007199254740993");
    expect(buildParameters(EQUALS, { expected: backfilled }, "string", saved)).toEqual(saved);
  });

  it("保存的 null 与布尔字面量在文本字段上不被改写成文本", () => {
    expect(
      buildParameters(EQUALS, { expected: "null" }, "string", { expected: { type: "null" } }),
    ).toEqual({ expected: { type: "null" } });
    expect(
      buildParameters(EQUALS, { expected: "true" }, "string", {
        expected: { type: "boolean", value: true },
      }),
    ).toEqual({ expected: { type: "boolean", value: true } });
  });

  it("集合候选逐项沿用各自的保存类型，混合类型的集合不被拉平", () => {
    const saved = {
      values: [{ type: "number", text: "1" }, { type: "string", text: "01" }, { type: "null" }],
    };
    const text = (saved.values as unknown[]).map(literalText).join("\n");
    expect(text).toBe("1\n01\nnull");
    expect(buildParameters(IN_SET, { values: text }, "string", saved)).toEqual(saved);
  });

  it("新建断言没有保存真相，仍按字段类型编码", () => {
    expect(buildParameters(EQUALS, { expected: "true" }, "boolean")).toEqual({
      expected: { type: "boolean", value: true },
    });
  });

  it("改动后的文本仍按保存类型校验：数字条件填了非数字就当场报错", () => {
    const saved = { expected: { type: "number", text: "1" } };
    expect(() => buildParameters(EQUALS, { expected: "abc" }, "string", saved)).toThrow(/数字/);
  });

  it("认不出的保存类型不降级：没动过原样保留，动过就明确拒绝", () => {
    const saved = { expected: { type: "secret", ref: "vault://token" } };
    const shown = literalText(saved.expected);
    // 用户没改这一项：原样回传，不被重写成文本字面量。
    expect(buildParameters(EQUALS, { expected: shown }, "string", saved)).toEqual(saved);
    // 用户改了文本：说明他以为这是个可编辑的值，界面不猜，直接拒绝。
    expect(() => buildParameters(EQUALS, { expected: "vault://other" }, "string", saved)).toThrow(/无法识别/);
  });

  it("集合里认不出的候选同样原样保留，不被拉平成文本", () => {
    const saved = { values: [{ type: "secret", ref: "vault://token" }] };
    const shown = (saved.values as unknown[]).map(literalText).join("\n");
    expect(buildParameters(IN_SET, { values: shown }, "string", saved)).toEqual(saved);
  });
});

describe("summarize 展示非文本字面量", () => {
  it("布尔期望值显示 true／false，而不是空白", () => {
    const assertion: CaseAssertion = {
      id: "a",
      target_source: "response.body",
      selector: [{ kind: "key", key: "ok" }],
      type: "equals",
      parameters: { expected: { type: "boolean", value: false } },
      compare_as: null,
      severity: "error",
      enabled: true,
      sort_order: 0,
    };
    expect(summarize(EQUALS, assertion.parameters)).toBe("等于 false");
  });

  it("布尔集合候选逐项显示", () => {
    expect(summarize(IN_SET, { values: [{ type: "boolean", value: true }, { type: "boolean", value: false }] })).toBe(
      "属于 true、false",
    );
  });
});
