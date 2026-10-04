import { describe, expect, it } from "vitest";

import type { CaseLibraryFilters } from "../api/types";
import { defaultCaseLibraryFilters, libraryQuery, normalizeCaseLibraryFilters } from "./useCaseLibrary";

describe("用例库查询合同", () => {
  it("目录筛选显式包含后代，并且游标与页大小只属于查询而不进入保存视图", () => {
    const filters: CaseLibraryFilters = {
      ...defaultCaseLibraryFilters(),
      q: "订单_%",
      method: "POST",
      folder: "exact",
      folder_id: "11111111-1111-4111-8111-111111111111",
      include_descendants: true,
    };
    const query = new URLSearchParams(libraryQuery(filters, 50, "opaque-cursor"));

    expect(Object.fromEntries(query)).toEqual({
      q: "订单_%",
      method: "POST",
      state: "active",
      folder: "exact",
      folder_id: "11111111-1111-4111-8111-111111111111",
      include_descendants: "true",
      collection: "all",
      sort: "updated_desc",
      limit: "50",
      cursor: "opaque-cursor",
    });
    expect(filters).not.toHaveProperty("cursor");
    expect(filters).not.toHaveProperty("limit");
  });

  it("最近打开允许名称排序，未分组不携带目录ID", () => {
    const query = new URLSearchParams(libraryQuery({
      ...defaultCaseLibraryFilters(),
      folder: "unfiled",
      folder_id: "不应发送",
      collection: "recent",
      sort: "name_asc",
    }, 20, null));

    expect(query.get("sort")).toBe("name_asc");
    expect(query.has("folder_id")).toBe(false);
    expect(query.has("include_descendants")).toBe(false);
  });

  it("非精确目录统一清除目录附属条件，最近集合保留合法普通排序", () => {
    expect(normalizeCaseLibraryFilters({
      ...defaultCaseLibraryFilters(), folder: "all", folder_id: "旧目录", include_descendants: true,
    })).toEqual(expect.objectContaining({ folder: "all", folder_id: null, include_descendants: false }));
    const recentName = normalizeCaseLibraryFilters({
      ...defaultCaseLibraryFilters(), collection: "recent", sort: "name_asc",
    });
    expect(new URLSearchParams(libraryQuery(recentName, 20, null)).get("sort")).toBe("name_asc");
  });
});
