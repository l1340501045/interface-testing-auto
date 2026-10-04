import { apiDelete, apiGet, apiSend, projectPath } from "../api/client";
import {
  toAssetFolderPage,
  toCaseLibraryPage,
  toCasePreference,
  toCaseSavedView,
  toCaseSavedViewList,
} from "../api/guards";
import type {
  AssetFolder,
  CaseLibraryFilters,
  CaseLibraryPage,
  CasePreference,
  CaseSavedView,
} from "../api/types";
import { useResource } from "../hooks/useResource";
import { useCallback, useEffect, useRef, useState } from "react";

function scopeKey(workspaceId: string, projectId: string): string | null {
  return workspaceId && projectId ? `${workspaceId}/${projectId}` : null;
}

export function defaultCaseLibraryFilters(): CaseLibraryFilters {
  return {
    schema_version: 1,
    state: "active",
    folder: "all",
    folder_id: null,
    include_descendants: false,
    collection: "all",
    sort: "updated_desc",
  };
}

/** 查询与保存视图共用的唯一筛选规范化，避免同一 UI 状态产生两种合同形状。 */
export function normalizeCaseLibraryFilters(filters: CaseLibraryFilters): CaseLibraryFilters {
  const q = filters.q?.trim();
  const exact = filters.folder === "exact";
  return {
    schema_version: 1,
    ...(q ? { q } : {}),
    ...(filters.method ? { method: filters.method } : {}),
    state: filters.state,
    folder: filters.folder,
    folder_id: exact ? filters.folder_id : null,
    include_descendants: exact ? filters.include_descendants : false,
    collection: filters.collection,
    sort: filters.collection !== "recent" && filters.sort === "recent_desc" ? "updated_desc" : filters.sort,
  };
}

export function libraryQuery(filters: CaseLibraryFilters, limit: 20 | 50 | 100, cursor: string | null): string {
  const normalized = normalizeCaseLibraryFilters(filters);
  const query = new URLSearchParams();
  const q = normalized.q;
  if (q) query.set("q", q);
  if (normalized.method) query.set("method", normalized.method);
  query.set("state", normalized.state);
  query.set("folder", normalized.folder);
  if (normalized.folder === "exact" && normalized.folder_id) query.set("folder_id", normalized.folder_id);
  if (normalized.folder === "exact") query.set("include_descendants", String(normalized.include_descendants));
  query.set("collection", normalized.collection);
  query.set("sort", normalized.sort);
  query.set("limit", String(limit));
  if (cursor) query.set("cursor", cursor);
  return query.toString();
}

export function useCaseLibrary(
  workspaceId: string,
  projectId: string,
  filters: CaseLibraryFilters,
  limit: 20 | 50 | 100,
  cursor: string | null,
  enabled = true,
) {
  const scope = enabled ? scopeKey(workspaceId, projectId) : null;
  const query = libraryQuery(filters, limit, cursor);
  return useResource<CaseLibraryPage>(scope === null ? null : `${scope}#library#${query}`, (signal) =>
    apiGet(projectPath(workspaceId, projectId, `/case-library?${query}`), toCaseLibraryPage, signal),
  );
}

interface FolderBucket {
  items: AssetFolder[];
  nextCursor: string | null;
  complete: boolean;
  loading: boolean;
  error: Error | null;
}

interface FolderTreeSnapshot {
  owner: string;
  buckets: Record<string, FolderBucket>;
}

const EMPTY_BUCKET: FolderBucket = { items: [], nextCursor: null, complete: false, loading: false, error: null };

function folderBucketQuery(key: string): URLSearchParams {
  if (key === "root") return new URLSearchParams({ parent_mode: "root", state: "active", limit: "100" });
  if (key === "archived") return new URLSearchParams({ parent_mode: "all", state: "archived", limit: "100" });
  if (key.startsWith("child:")) return new URLSearchParams({ parent_mode: "exact", parent_id: key.slice(6), state: "active", limit: "100" });
  if (key.startsWith("search:")) return new URLSearchParams({ parent_mode: "all", state: "all", q: key.slice(7), limit: "100" });
  throw new Error(`未知目录读取：${key}`);
}

/** 目录按层缓存；只有搜索与归档入口使用全项目 all 查询。 */
export function useAssetFolderTree(workspaceId: string, projectId: string, search: string, refreshToken: number, enabled = true) {
  const owner = `${workspaceId}/${projectId}`;
  const [snapshot, setSnapshot] = useState<FolderTreeSnapshot>({ owner, buckets: {} });
  const liveSnapshot = useRef(snapshot);
  liveSnapshot.current = snapshot;
  const controllers = useRef(new Map<string, AbortController>());
  const liveOwner = useRef(owner);
  liveOwner.current = owner;

  const loadBucket = useCallback(async (key: string, restart = false, retryError = false): Promise<void> => {
    if (!enabled || !workspaceId || !projectId) return;
    if (!restart && controllers.current.has(key)) return;
    const currentSnapshot = liveSnapshot.current;
    const current = currentSnapshot.owner === owner ? currentSnapshot.buckets[key] : undefined;
    if (!restart && (current?.loading || current?.complete || (current !== undefined && current.error !== null && !retryError))) return;
    controllers.current.get(key)?.abort();
    const controller = new AbortController();
    controllers.current.set(key, controller);
    let items = restart ? [] : current?.items ?? [];
    let cursor = restart ? null : current?.nextCursor ?? null;
    setSnapshot((value) => ({
      owner,
      buckets: {
        ...(value.owner === owner ? value.buckets : {}),
        [key]: { items, nextCursor: cursor, complete: false, loading: true, error: null },
      },
    }));
    try {
      const query = folderBucketQuery(key);
      if (cursor) query.set("cursor", cursor);
      const page = await apiGet(projectPath(workspaceId, projectId, `/asset-folders?${query}`), toAssetFolderPage, controller.signal);
      if (controller.signal.aborted || liveOwner.current !== owner) return;
      const byId = new Map(items.map((item) => [item.id, item]));
      for (const item of page.items) byId.set(item.id, item);
      items = [...byId.values()];
      cursor = page.next_cursor;
      setSnapshot((value) => value.owner !== owner ? value : ({
        owner,
        buckets: { ...value.buckets, [key]: { items, nextCursor: cursor, complete: cursor === null, loading: false, error: null } },
      }));
    } catch (cause) {
      if (controller.signal.aborted || liveOwner.current !== owner) return;
      const error = cause instanceof Error ? cause : new Error("目录加载失败，请重试");
      setSnapshot((value) => value.owner !== owner ? value : ({
        owner,
        buckets: { ...value.buckets, [key]: { items, nextCursor: cursor, complete: false, loading: false, error } },
      }));
    } finally {
      if (controllers.current.get(key) === controller) controllers.current.delete(key);
    }
  }, [enabled, owner, projectId, workspaceId]);

  const refresh = useCallback(() => {
    for (const controller of controllers.current.values()) controller.abort();
    controllers.current.clear();
    setSnapshot({ owner, buckets: {} });
    if (enabled && workspaceId && projectId) {
      void loadBucket("root", true);
      void loadBucket("archived", true);
      const q = search.trim();
      if (q) void loadBucket(`search:${q}`, true);
    }
  }, [enabled, loadBucket, owner, projectId, search, workspaceId]);

  const seenRefreshToken = useRef(refreshToken);
  useEffect(() => {
    if (seenRefreshToken.current === refreshToken) return;
    seenRefreshToken.current = refreshToken;
    refresh();
  }, [refresh, refreshToken]);

  useEffect(() => {
    for (const controller of controllers.current.values()) controller.abort();
    controllers.current.clear();
    setSnapshot({ owner, buckets: {} });
    return () => {
      for (const controller of controllers.current.values()) controller.abort();
    };
  }, [owner]);

  useEffect(() => {
    if (!enabled || snapshot.owner !== owner) return;
    void loadBucket("root");
    void loadBucket("archived");
  }, [enabled, loadBucket, owner, snapshot.owner]);

  useEffect(() => {
    if (enabled) return;
    for (const controller of controllers.current.values()) controller.abort();
    controllers.current.clear();
    setSnapshot((value) => value.owner !== owner ? value : ({
      ...value,
      buckets: Object.fromEntries(Object.entries(value.buckets).map(([key, bucket]) => [key, { ...bucket, loading: false }])),
    }));
  }, [enabled, owner]);

  const normalizedSearch = search.trim();
  useEffect(() => {
    if (!enabled || !normalizedSearch || snapshot.owner !== owner) return;
    for (const [key, controller] of controllers.current) {
      if (key.startsWith("search:") && key !== `search:${normalizedSearch}`) {
        controller.abort();
        controllers.current.delete(key);
      }
    }
    void loadBucket(`search:${normalizedSearch}`, true);
  }, [enabled, loadBucket, normalizedSearch, owner, snapshot.owner]);

  const buckets = snapshot.owner === owner ? snapshot.buckets : {};
  const allById = new Map<string, AssetFolder>();
  for (const bucket of Object.values(buckets)) for (const item of bucket.items) allById.set(item.id, item);
  return {
    root: buckets.root ?? EMPTY_BUCKET,
    archived: buckets.archived ?? EMPTY_BUCKET,
    search: normalizedSearch ? buckets[`search:${normalizedSearch}`] ?? EMPTY_BUCKET : null,
    child: (parentId: string) => buckets[`child:${parentId}`] ?? EMPTY_BUCKET,
    loadChildren: (parentId: string) => loadBucket(`child:${parentId}`),
    retry: (key: string) => loadBucket(key, false, true),
    loadMore: (key: string) => loadBucket(key),
    refresh,
    searchKey: normalizedSearch ? `search:${normalizedSearch}` : null,
    allItems: [...allById.values()],
    failures: Object.entries(buckets).filter((entry): entry is [string, FolderBucket] => entry[1].error !== null),
    loadedFolderIds: Object.entries(buckets)
      .filter(([key, bucket]) => key.startsWith("child:") && (bucket.complete || bucket.items.length > 0))
      .map(([key]) => key.slice(6)),
  };
}

export function useCaseViews(workspaceId: string, projectId: string, enabled = true) {
  const scope = enabled ? scopeKey(workspaceId, projectId) : null;
  return useResource<CaseSavedView[]>(scope === null ? null : `${scope}#case-views`, (signal) =>
    apiGet(projectPath(workspaceId, projectId, "/case-views"), toCaseSavedViewList, signal),
  );
}

export function setFavorite(workspaceId: string, projectId: string, caseId: string, favorite: boolean): Promise<CasePreference> {
  return apiSend(projectPath(workspaceId, projectId, `/case-preferences/${caseId}/favorite`), "PUT", { favorite }, toCasePreference);
}

export function recordOpened(workspaceId: string, projectId: string, caseId: string): Promise<CasePreference> {
  return apiSend(projectPath(workspaceId, projectId, `/case-preferences/${caseId}/opened`), "POST", undefined, toCasePreference);
}

export function createCaseView(workspaceId: string, projectId: string, name: string, filters: CaseLibraryFilters): Promise<CaseSavedView> {
  return apiSend(projectPath(workspaceId, projectId, "/case-views"), "POST", { name, filters: normalizeCaseLibraryFilters(filters) }, toCaseSavedView);
}

export function updateCaseView(workspaceId: string, projectId: string, view: CaseSavedView, name: string, filters: CaseLibraryFilters): Promise<CaseSavedView> {
  return apiSend(projectPath(workspaceId, projectId, `/case-views/${view.id}`), "PATCH", { name, filters: normalizeCaseLibraryFilters(filters) }, toCaseSavedView, {
    headers: { "If-Match": `"${view.rev}"` },
  });
}

export function deleteCaseView(workspaceId: string, projectId: string, view: CaseSavedView): Promise<void> {
  return apiDelete(projectPath(workspaceId, projectId, `/case-views/${view.id}`), {
    headers: { "If-Match": `"${view.rev}"` },
  });
}
