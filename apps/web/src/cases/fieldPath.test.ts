/**
 * 字段路径的展示与读回（G6）。
 *
 * 界面上给用户看的是 data.id、items[0].price 这样的路径，不是内部定位结构。
 * 两个方向必须一致：界面上显示的路径，用户原样粘回“字段路径”输入框时，必须读回
 * 同一份定位。否则用户看到的是 A、真正保存的是 B，而执行结果按 B 核对。
 */
import { describe, expect, it } from "vitest";

import type { LocatorStep } from "../api/types";
import { formatSelector, parsePathInput } from "./assertionGroups";

const key = (name: string): LocatorStep => ({ kind: "key", key: name });
const index = (value: number): LocatorStep => ({ kind: "index", index: value });

describe("字段路径展示", () => {
  it("普通字段名按点号连接，下标紧跟字段名", () => {
    expect(formatSelector([key("data"), key("id")])).toBe("data.id");
    expect(formatSelector([key("items"), index(0), key("price")])).toBe("items[0].price");
  });

  it("空定位显示为响应正文根，不是空字符串", () => {
    expect(formatSelector([])).toBe("$");
  });

  it("含点号或空格的字段名用引号包起来，不假装成两级字段", () => {
    expect(formatSelector([key("user.name")])).toBe('["user.name"]');
    expect(formatSelector([key("data"), key("user name")])).toBe('data["user name"]');
  });

  it("字段名里的引号与反斜杠被转义，展示结果不会截断路径", () => {
    expect(formatSelector([key('a"b')])).toBe('["a\\"b"]');
    expect(formatSelector([key("a\\b")])).toBe('["a\\\\b"]');
  });
});

describe("字段路径读回", () => {
  it("界面显示的路径原样读回同一份定位", () => {
    const cases: LocatorStep[][] = [
      [key("data"), key("id")],
      [key("items"), index(0), key("price")],
      [key("user.name")],
      [key("data"), key("user name")],
      [key('a"b')],
      [key("a\\b")],
      [key("items"), index(12), key("tags"), index(3)],
    ];
    for (const selector of cases) {
      expect(parsePathInput(formatSelector(selector))).toEqual(selector);
    }
  });

  it("留空与 $ 都表示响应正文根", () => {
    expect(parsePathInput("")).toEqual([]);
    expect(parsePathInput("   ")).toEqual([]);
    expect(parsePathInput("$")).toEqual([]);
  });

  it("读不懂的输入返回 null，不悄悄读成别的字段", () => {
    expect(parsePathInput("a]b")).toBeNull();
    expect(parsePathInput("data..id")).toBeNull();
    expect(parsePathInput("[abc]")).toBeNull();
  });
});
