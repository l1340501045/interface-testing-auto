import { describe, expect, it } from "vitest";

import { pageFromHash, pageHash } from "./navigation";

describe("应用页面路由", () => {
  it.each([
    ["#/workbench", "workbench"],
    ["#/environments", "environments"],
    ["#/tasks", "tasks"],
    ["#/reports?environment=e1", "reports"],
  ] as const)("解析 %s", (hash, page) => {
    expect(pageFromHash(hash)).toBe(page);
    expect(pageHash(page)).toBe(`#/${page}`);
  });

  it("未知地址回到接口工作台", () => {
    expect(pageFromHash("#/old-api-debug")).toBe("workbench");
    expect(pageFromHash("")).toBe("workbench");
  });
});
