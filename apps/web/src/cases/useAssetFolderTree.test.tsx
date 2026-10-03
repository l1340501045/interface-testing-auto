import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useAssetFolderTree } from "./useCaseLibrary";

const fetchCalls: string[] = [];

const ROOT = "11111111-1111-4111-8111-111111111111";
const ROOT_2 = "22222222-2222-4222-8222-222222222222";
const CHILD = "33333333-3333-4333-8333-333333333333";
const ARCHIVED = "44444444-4444-4444-8444-444444444444";

function folder(id: string, name: string, parentId: string | null, hasChildren = false, archived = false) {
  return {
    id, name, parent_id: parentId, rev: 1, archived_at: archived ? "2026-10-04T00:00:00Z" : null,
    availability: archived ? "archived" : "available", has_children: hasChildren,
    archive_operation_id: null, archive_root_id: null, restore_mode: archived ? "legacy_single" : null,
    ancestor_path: parentId ? [{ id: ROOT, name: "根目录" }] : [],
  };
}

function installRoutes(options: { failSecondRoot?: boolean } = {}) {
  let failSecondRoot = options.failSecondRoot ?? false;
  let rootOverride: string | null = null;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const requestPath = String(input);
    fetchCalls.push(requestPath);
    const query = new URLSearchParams(requestPath.split("?")[1] ?? "");
    let body: unknown;
    if (query.get("parent_mode") === "root") {
      const rootName = rootOverride ?? (requestPath.includes("/projects/p2/") ? "新范围根目录" : "根目录");
      if (query.get("cursor") === "root-next") {
        if (failSecondRoot) return new Response(JSON.stringify({ code: "temporary", message: "根目录第二页失败", trace_id: null }), { status: 500 });
        body = { items: [folder(ROOT_2, "第二页根目录", null)], total: 2, next_cursor: null };
      } else {
        body = { items: [folder(ROOT, rootName, null, true)], total: 2, next_cursor: "root-next" };
      }
    } else if (query.get("state") === "archived") body = { items: [folder(ARCHIVED, "空归档目录", ROOT, false, true)], total: 1, next_cursor: null };
    else if (query.get("parent_mode") === "exact") body = { items: [folder(CHILD, "子目录", ROOT)], total: 1, next_cursor: null };
    else if (query.get("q") === "深层") body = { items: [folder(CHILD, "深层目录", ROOT)], total: 1, next_cursor: null };
    else return new Response(JSON.stringify({ code: "uncovered", message: `未覆盖目录请求：${requestPath}`, trace_id: null }), { status: 500 });
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  return { allowSecondRoot: () => { failSecondRoot = false; }, setRootName: (name: string) => { rootOverride = name; } };
}

beforeEach(() => { fetchCalls.length = 0; vi.unstubAllGlobals(); });

describe("资产目录分层读取", () => {
  it("root逐页、exact按展开加载，搜索和归档使用独立all查询，并在页面往返复用缓存", async () => {
    const controls = installRoutes();
    const { result, rerender } = renderHook(
      ({ search, enabled }) => useAssetFolderTree("w", "p", search, 0, enabled),
      { initialProps: { search: "", enabled: true } },
    );
    await waitFor(() => expect(result.current.root.items.map((item) => item.id)).toEqual([ROOT]));
    expect(result.current.root.nextCursor).toBe("root-next");
    expect(fetchCalls.filter((path) => path.includes("parent_mode=root"))).toHaveLength(1);
    await act(async () => result.current.loadMore("root"));
    expect(result.current.root.items.map((item) => item.id)).toEqual([ROOT, ROOT_2]);
    expect(result.current.archived.items.map((item) => item.id)).toEqual([ARCHIVED]);
    expect(fetchCalls.filter((path) => path.includes("parent_mode=root"))).toHaveLength(2);
    expect(fetchCalls.some((path) => path.includes("parent_mode=all&state=all"))).toBe(false);

    await act(async () => result.current.loadChildren(ROOT));
    expect(result.current.child(ROOT).items.map((item) => item.id)).toEqual([CHILD]);
    expect(fetchCalls.some((path) => path.includes(`parent_mode=exact&parent_id=${ROOT}`))).toBe(true);

    rerender({ search: "深层", enabled: true });
    await waitFor(() => expect(result.current.search?.items[0]?.name).toBe("深层目录"));
    expect(fetchCalls.some((path) => path.includes("parent_mode=all&state=all&q=%E6%B7%B1%E5%B1%82"))).toBe(true);

    const before = fetchCalls.length;
    rerender({ search: "", enabled: false });
    rerender({ search: "", enabled: true });
    await waitFor(() => expect(result.current.root.complete).toBe(true));
    expect(fetchCalls.length).toBe(before);

    controls.setRootName("刷新后的根目录");
    await act(async () => result.current.refresh());
    await waitFor(() => expect(result.current.root.items[0]?.name).toBe("刷新后的根目录"));
    expect(fetchCalls.filter((path) => path.includes("parent_mode=root")).length).toBeGreaterThan(2);
  });

  it("后续页失败保留已加载节点，局部重试从失败游标继续", async () => {
    const controls = installRoutes({ failSecondRoot: true });
    const { result } = renderHook(() => useAssetFolderTree("w", "p", "", 0, true));
    await waitFor(() => expect(result.current.root.nextCursor).toBe("root-next"));
    await act(async () => result.current.loadMore("root"));
    await waitFor(() => expect(result.current.root.error?.message).toBe("根目录第二页失败"));
    expect(result.current.root.items.map((item) => item.id)).toEqual([ROOT]);
    controls.allowSecondRoot();
    await act(async () => result.current.retry("root"));
    await waitFor(() => expect(result.current.root.complete).toBe(true));
    expect(result.current.root.items.map((item) => item.id)).toEqual([ROOT, ROOT_2]);
  });

  it("切换owner首帧不交出旧目录，随后只接纳新范围结果", async () => {
    installRoutes();
    const { result, rerender } = renderHook(
      ({ projectId }) => useAssetFolderTree("w", projectId, "", 0, true),
      { initialProps: { projectId: "p" } },
    );
    await waitFor(() => expect(result.current.root.items[0]?.name).toBe("根目录"));
    rerender({ projectId: "p2" });
    expect(result.current.root.items).toEqual([]);
    await waitFor(() => expect(result.current.root.items[0]?.name).toBe("新范围根目录"));
    expect(fetchCalls.some((path) => path.includes("/projects/p2/asset-folders"))).toBe(true);
  });
});
