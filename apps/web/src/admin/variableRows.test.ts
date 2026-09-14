/**
 * 普通变量行的“类型真相”回归。
 *
 * 真实缺陷的形状是：用户只想改环境地址，保存后 JSON 变量变成字符串变量、null 变成
 * 长度 4 的文本 "null"，而界面上看不出任何变化。因此这里的核心断言不是“能显示”，
 * 而是**载入后原样保存**：没有显式改过的值必须逐字回传。
 */
import { describe, expect, it } from "vitest";

import type { VariableItem } from "../api/types";
import {
  describeLiteralText,
  editRow,
  emptyRow,
  sameLiteral,
  toLiteral,
  toPayload,
  toRow,
  toRows,
  validateRows,
} from "./variableRows";

describe("已保存的字面量是类型真相", () => {
  it("json / null / 布尔 / 长整数载入后原样保存，不需要任何改动", () => {
    const items: VariableItem[] = [
      { name: "config", value: { type: "json", text: '{"retry": 3, "id": 9007199254740993}' } },
      { name: "nothing", value: { type: "null" } },
      { name: "flag", value: { type: "boolean", value: false } },
      { name: "big", value: { type: "number", text: "9007199254740993" } },
    ];
    expect(toPayload(toRows(items))).toEqual(items);
  });

  it("长整数不经过 Number：文本一字不差地回传", () => {
    const row = toRow({ name: "big", value: { type: "number", text: "9007199254740993" } });
    expect(row.text).toBe("9007199254740993");
    expect(toLiteral(row)).toEqual({ type: "number", text: "9007199254740993" });
  });

  it("只改名不重编码：名称与值是互不相干的两件事", () => {
    const row = toRow({ name: "old", value: { type: "json", text: "[1, 2]" } });
    const renamed = editRow(row, { name: "new" });
    expect(renamed.changed).toBe(false);
    expect(toLiteral(renamed)).toEqual({ type: "json", text: "[1, 2]" });
  });

  it("认不出的类型标记为只读，并按原样保留，不降级成字符串", () => {
    const saved = { type: "secret", ref: "vault://token" };
    const row = toRow({ name: "token", value: saved });
    expect(row.kind).toBe("unknown");
    expect(toLiteral(row)).toEqual(saved);
    // 只读行不可能被改成别的类型，这里连兜底路径也确认一遍。
    expect(toLiteral({ ...row, changed: true })).toEqual(saved);
  });

  it("显式改过的值才按新类型编码", () => {
    const row = toRow({ name: "flag", value: { type: "boolean", value: true } });
    expect(toLiteral(editRow(row, { text: "false" }))).toEqual({ type: "boolean", value: false });
  });
});

describe("变量行的显示", () => {
  it("null 显示成 null，布尔显示成 true／false，JSON 显示原文", () => {
    expect(describeLiteralText({ type: "null" })).toBe("null");
    expect(describeLiteralText({ type: "boolean", value: true })).toBe("true");
    expect(describeLiteralText({ type: "boolean", value: false })).toBe("false");
    expect(describeLiteralText({ type: "json", text: '{"a": 1}' })).toBe('{"a": 1}');
    expect(describeLiteralText({ type: "secret" })).toBe('{"type":"secret"}');
  });
});

describe("提交前的本地校验", () => {
  it("未改动的值不按更窄的词法拦截：只改环境地址也要能保存", () => {
    // 历史数据里的数字文本可能带正号或前导零，服务端已经接受过一次。
    const rows = [{ ...toRow({ name: "n", value: { type: "number", text: "+007" } }), name: "n" }];
    expect(validateRows(rows)).toBeNull();
  });

  it("改动过的数字必须合法，空名与重名都挡住", () => {
    const row = toRow({ name: "n", value: { type: "number", text: "1" } });
    expect(validateRows([editRow(row, { text: "abc" })])).toMatch(/不是合法数字/);
    expect(validateRows([editRow(row, { text: "" })])).toMatch(/不能为空/);
    expect(validateRows([{ ...emptyRow(), name: "" }])).toMatch(/变量名不能为空/);
    expect(validateRows([{ ...emptyRow(), name: "a" }, { ...emptyRow(), name: "a" }])).toMatch(/重复/);
  });
});

/**
 * “有没有未保存的修改”只能由字面量本身回答。
 *
 * 显示文本会把数字 1 与字符串 "1" 都写成 "1"：用它比较，只改类型时脏状态算不出来，
 * 用户以为没动过就切走范围，输入直接丢掉。
 */
describe("字面量相等判定保留类型", () => {
  it("数字 1 与字符串 \"1\" 不是同一个字面量", () => {
    expect(sameLiteral({ type: "number", text: "1" }, { type: "string", text: "1" })).toBe(false);
    expect(sameLiteral({ type: "number", text: "1" }, { type: "number", text: "1" })).toBe(true);
    expect(sameLiteral({ type: "number", text: "1" }, { type: "number", text: "2" })).toBe(false);
    // 显示文本确实一样，问题不在显示上，而在“用显示文本当基线”。
    expect(describeLiteralText({ type: "number", text: "1" })).toBe(
      describeLiteralText({ type: "string", text: "1" }),
    );
  });

  it("同类型的 null／布尔／JSON 按结构比较，键的顺序不算差别", () => {
    expect(sameLiteral({ type: "null" }, { type: "null" })).toBe(true);
    expect(sameLiteral({ type: "null" }, { type: "boolean", value: false })).toBe(false);
    expect(sameLiteral({ type: "json", text: '{"a": 1, "b": 2}' }, { type: "json", text: '{"a": 1, "b": 2}' })).toBe(true);
    expect(sameLiteral({ type: "json", text: "[1, 2]" }, { type: "json", text: "[1, 2, 3]" })).toBe(false);
    // 认不出的类型逐字段比较，多一个字段就是改动。
    expect(sameLiteral({ type: "secret", ref: "a" }, { type: "secret", ref: "a" })).toBe(true);
    expect(sameLiteral({ type: "secret", ref: "a" }, { type: "secret", ref: "b" })).toBe(false);
    expect(sameLiteral({ type: "secret", ref: "a" }, { type: "secret" })).toBe(false);
  });
});
