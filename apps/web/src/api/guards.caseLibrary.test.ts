import { describe, expect, it } from "vitest";

import { ContractError, toAssetFolderPage, toCaseLibraryPage, toCaseSavedView, toCaseSavedViewList, toFolderList } from "./guards";

describe("用例库运行时合同", () => {
  it("旧目录数组增量保留修订与可用性", () => {
    expect(toFolderList([{ id: "f", parent_id: null, name: "旧目录", archived_at: null, rev: 4, availability: "ancestor_archived" }])[0])
      .toEqual(expect.objectContaining({ rev: 4, availability: "ancestor_archived" }));
  });
  it("保留个人字段、归档阻塞解释和真实游标", () => {
    expect(toCaseLibraryPage({
      items: [{
        id: "1", name: "订单", method: "GET", path: "/orders", folder_id: "f1",
        folder_path: [{ id: "f1", name: "订单目录" }],
        asset_status: "active", availability: "folder_unavailable", draft_rev: 3,
        updated_at: "2026-10-04T00:00:00Z", latest_version: 2, favorite: true, last_opened_at: null,
      }],
      total: 1,
      next_cursor: "next",
    })).toEqual(expect.objectContaining({
      total: 1,
      next_cursor: "next",
      items: [expect.objectContaining({ asset_status: "active", availability: "folder_unavailable", favorite: true })],
    }));
  });

  it("目录节点保留祖先路径与归档定位方式", () => {
    const page = toAssetFolderPage({
      items: [{
        id: "f2", name: "子目录", parent_id: "f1", rev: 2, archived_at: "2026-10-04T00:00:00Z",
        availability: "archived", has_children: false, archive_operation_id: "op1", archive_root_id: "f1",
        restore_mode: "locate_root", ancestor_path: [{ id: "f1", name: "根目录" }],
      }], total: 1, next_cursor: null,
    });
    expect(page.items[0]).toEqual(expect.objectContaining({ restore_mode: "locate_root", ancestor_path: [{ id: "f1", name: "根目录" }] }));
  });

  it("归档来源无法核验时保留unavailable，不伪装成旧单项恢复", () => {
    const page = toAssetFolderPage({
      items: [{
        id: "f3", name: "来源异常", parent_id: null, rev: 3, archived_at: "2026-10-04T00:00:00Z",
        availability: "archived", has_children: false, archive_operation_id: "op2", archive_root_id: null,
        restore_mode: "unavailable", ancestor_path: [],
      }], total: 1, next_cursor: null,
    });
    expect(page.items[0]?.restore_mode).toBe("unavailable");
  });

  it("保存视图拒绝未知筛选字段，避免把页码或请求内容带入个人偏好", () => {
    expect(() => toCaseSavedView({
      id: "v1", name: "危险视图", rev: 1, created_at: "x", updated_at: "x",
      filters: {
        schema_version: 1, state: "active", folder: "all", folder_id: null,
        include_descendants: true, collection: "all", sort: "updated_desc", cursor: "secret",
      },
    })).toThrow(ContractError);
  });

  it("接受后端完整回显中的空搜索/方法和最近集合普通排序", () => {
    const view = toCaseSavedView({
      id: "v2", name: "最近名称排序", rev: 2, created_at: "x", updated_at: "x",
      filters: {
        schema_version: 1, q: null, method: null, state: "all", folder: "all", folder_id: null,
        include_descendants: false, collection: "recent", sort: "name_asc",
      },
    });
    expect(view.filters).toEqual({
      schema_version: 1, state: "all", folder: "all", folder_id: null,
      include_descendants: false, collection: "recent", sort: "name_asc",
    });
  });

  it("GET列表使用与POST/PATCH相同的完整model_dump形状", () => {
    expect(toCaseSavedViewList([{
      id: "v4", name: "完整回显", rev: 1, created_at: "x", updated_at: "x",
      filters: {
        schema_version: 1, q: null, method: null, state: "active", folder: "exact", folder_id: "f1",
        include_descendants: true, collection: "all", sort: "updated_desc",
      },
    }])[0]?.filters).toEqual({
      schema_version: 1, state: "active", folder: "exact", folder_id: "f1",
      include_descendants: true, collection: "all", sort: "updated_desc",
    });
  });

  it("只拒绝非最近集合使用最近排序", () => {
    expect(() => toCaseSavedView({
      id: "v3", name: "非法排序", rev: 1, created_at: "x", updated_at: "x",
      filters: {
        schema_version: 1, q: null, method: null, state: "active", folder: "all", folder_id: null,
        include_descendants: false, collection: "all", sort: "recent_desc",
      },
    })).toThrow(ContractError);
  });
});
