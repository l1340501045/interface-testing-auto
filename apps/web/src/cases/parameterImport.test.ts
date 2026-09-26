import { describe, expect, it } from "vitest";

import { decodeRawText, encodeRawText, parseParameterImport } from "./parameterImport";

describe("参数批量录入", () => {
  it("只按第一个等号和冒号分隔并保留长数字、重复名称和空值", () => {
    expect(parseParameterImport("id=9007199254740993\nid=\na=b=c", "equals")).toEqual({
      rows: [
        { name: "id", value: "9007199254740993" },
        { name: "id", value: "" },
        { name: "a", value: "b=c" },
      ],
      duplicateNames: ["id"],
    });
    expect(parseParameterImport("X-A:\t  value:tail ", "headers").rows[0]).toEqual({
      name: "X-A",
      value: "value:tail ",
    });
  });

  it("TSV 保留包裹单元格中的 CRLF、Tab、引号和反斜杠", () => {
    const source = 'name\t"A\r\nB\t""C""\\tail"\t"说明\r\n第二行"';
    expect(parseParameterImport(source, "tsv").rows).toEqual([
      { name: "name", value: 'A\r\nB\t"C"\\tail', description: "说明\r\n第二行" },
    ]);
  });

  it("任一记录错误时整批拒绝", () => {
    expect(() => parseParameterImport("ok=1\n\nbad=2", "equals")).toThrow("第 2 行");
    expect(() => parseParameterImport('a\t"unterminated', "tsv")).toThrow("引号没有闭合");
    expect(() => parseParameterImport('a\t"done"oops', "tsv")).toThrow("引号闭合后");
  });

  it("特殊字符转义可以逐字符往返且拒绝未知转义", () => {
    const source = "A\r\nB\t\\literal\\n";
    expect(decodeRawText(encodeRawText(source))).toBe(source);
    expect(() => decodeRawText("bad\\x")).toThrow("不支持的转义");
    expect(() => decodeRawText("bad\nline")).toThrow("请使用");
  });
});
