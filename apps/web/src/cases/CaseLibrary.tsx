import { DeleteOutlined, EditOutlined, FolderOutlined, SaveOutlined, StarFilled, StarOutlined } from "@ant-design/icons";
import { Alert, Button, Checkbox, Input, Modal, Segmented, Select, Space, Table, Tag, Tree } from "antd";
import type { ColumnsType } from "antd/es/table";
import type { DataNode } from "antd/es/tree";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type Key } from "react";

import { ApiError, apiSend, projectPath } from "../api/client";
import { toAssetFolder } from "../api/guards";
import type { AssetAction, AssetFolder, AssetOperation, CaseLibraryFilters, CaseLibraryItem, CaseSavedView } from "../api/types";
import { Empty, ErrorText, Loading } from "../components/Feedback";
import { useLeaveReport } from "../hooks/leaveGuard";
import {
  createCaseView,
  defaultCaseLibraryFilters,
  deleteCaseView,
  normalizeCaseLibraryFilters,
  setFavorite,
  updateCaseView,
  useAssetFolderTree,
  useCaseLibrary,
  useCaseViews,
} from "./useCaseLibrary";
import { AssetActionModal, type AssetActionTarget } from "./AssetActionModal";
import type { AcquireAssetOperation } from "./assetController";

export type OpenCaseResult = "opened" | "focused" | "limit_reached" | "unavailable" | "busy";

const METHOD_OPTIONS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"].map((value) => ({ value, label: value }));
const SPECIAL_KEYS = new Set(["all", "unfiled", "favorites", "recent"]);

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "saved_view_limit_reached") return "每个项目最多保存 20 个个人视图，请先删除不再使用的视图。";
    if (error.code === "precondition_failed") return "这个视图已在别处更新，请刷新后再修改。";
    return error.message;
  }
  return error instanceof Error ? error.message : "操作失败，请稍后重试";
}

function folderTitle(folder: AssetFolder): string {
  if (folder.restore_mode === "unavailable") return `${folder.name}（归档来源不可定位）`;
  if (folder.availability === "available") return folder.name;
  if (folder.availability === "invalid_parent_chain") return `${folder.name}（目录关系异常）`;
  return `${folder.name}（已归档）`;
}

function folderPath(folderId: string | null, folder: AssetFolder | undefined, loading: boolean, failed: boolean): string {
  if (folderId === null) return "未分组";
  if (folder === undefined) return loading ? "目录加载中…" : failed ? "目录读取失败" : "目录不可读取";
  return [...folder.ancestor_path.map((item) => item.name), folder.name].join(" / ");
}

function formatTime(value: string | null): string {
  if (value === null) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

export function CaseLibrary({
  workspaceId,
  projectId,
  principalId,
  folderRefreshToken = 0,
  active,
  onOpen,
  canEdit = false,
  onAcquireAssetOperation,
  assetFocus,
  onAssetsChanged,
}: {
  workspaceId: string;
  projectId: string;
  principalId: string;
  folderRefreshToken?: number;
  active: boolean;
  onOpen: (caseId: string) => Promise<OpenCaseResult>;
  canEdit?: boolean;
  onAcquireAssetOperation?: AcquireAssetOperation;
  assetFocus?: { token: number; workspaceId: string; projectId: string; principalId: string; caseId: string; name: string; method: string; path: string; mode: "restore" | "organize" } | null;
  onAssetsChanged?: () => void;
}) {
  const owner = `${workspaceId}/${projectId}/${principalId}`;
  const [filters, setFilters] = useState<CaseLibraryFilters>(defaultCaseLibraryFilters);
  const [limit, setLimit] = useState<20 | 50 | 100>(20);
  const [cursorStack, setCursorStack] = useState<Array<string | null>>([null]);
  const cursor = cursorStack.at(-1) ?? null;
  const [selectedViewId, setSelectedViewId] = useState<string | null>(null);
  const [expandedFolderKeys, setExpandedFolderKeys] = useState<Key[]>(["folders", "archived-folders"]);
  const [folderQuery, setFolderQuery] = useState("");
  const [viewModalOpen, setViewModalOpen] = useState(false);
  const [viewName, setViewName] = useState("");
  const [viewBaseline, setViewBaseline] = useState("");
  const [editingView, setEditingView] = useState<CaseSavedView | null>(null);
  const [busyCaseId, setBusyCaseId] = useState<string | null>(null);
  const [viewBusy, setViewBusy] = useState(false);
  const [viewError, setViewError] = useState<string | null>(null);
  const [tableScrollY, setTableScrollY] = useState(240);
  const [feedback, setFeedback] = useState<{ type: "success" | "warning" | "error"; message: string } | null>(null);
  const [assetAction, setAssetAction] = useState<{ action: AssetAction; target: AssetActionTarget } | null>(null);
  const assetActionGeneration = useRef(0);
  const [copiedCase, setCopiedCase] = useState<{ id: string; name: string } | null>(null);
  const [bulkMode, setBulkMode] = useState<"page" | "filter">("page");
  const [selectedCaseIds, setSelectedCaseIds] = useState<string[]>([]);
  const [assetPhase, setAssetPhase] = useState("idle");
  const assetPhaseReaderRef = useRef<(() => string) | null>(null);
  const ownerRef = useRef(owner);
  const activeRef = useRef(active);
  const tableRegionRef = useRef<HTMLDivElement | null>(null);
  const seenFolderRefreshToken = useRef(folderRefreshToken);
  const viewWriteRef = useRef<{ owner: string; token: number } | null>(null);
  const viewWriteToken = useRef(0);
  const consumedFocusTokens = useRef(new Set<number>());
  ownerRef.current = owner;
  activeRef.current = active;
  const beginAssetAction = (next: { action: AssetAction; target: AssetActionTarget }) => { assetActionGeneration.current += 1; setAssetAction(next); };
  const closeAssetAction = () => { assetActionGeneration.current += 1; setAssetAction(null); setAssetPhase("idle"); assetPhaseReaderRef.current = null; };
  const currentAssetPhase = () => assetPhaseReaderRef.current?.() ?? assetPhase;
  const prepareQueryChange = () => {
    setSelectedCaseIds([]);
    const phase = currentAssetPhase();
    if (assetAction !== null && phase !== "submitting" && phase !== "unknown" && phase !== "done") closeAssetAction();
  };
  const registerAssetPhaseReader = useCallback((reader: (() => string) | null) => { assetPhaseReaderRef.current = reader; }, []);

  const folderTreeState = useAssetFolderTree(workspaceId, projectId, folderQuery, folderRefreshToken, active);
  const library = useCaseLibrary(workspaceId, projectId, filters, limit, cursor, active);
  const views = useCaseViews(workspaceId, projectId, active);
  const folderById = useMemo(() => new Map(folderTreeState.allItems.map((folder) => [folder.id, folder])), [folderTreeState.allItems]);
  const viewDirty = viewModalOpen && viewName !== viewBaseline;
  const synchronousAssetPhase = currentAssetPhase();
  const assetQueryLocked = assetAction !== null && synchronousAssetPhase !== "submitting" && synchronousAssetPhase !== "unknown" && synchronousAssetPhase !== "done";
  const validAssetFocus = assetFocus?.workspaceId === workspaceId && assetFocus.projectId === projectId && assetFocus.principalId === principalId ? assetFocus : null;
  useLeaveReport(`case-library-view:${owner}`, { dirty: viewDirty, busy: viewBusy });

  useEffect(() => {
    setFilters(defaultCaseLibraryFilters());
    setLimit(20);
    setCursorStack([null]);
    setSelectedViewId(null);
    setExpandedFolderKeys(["folders", "archived-folders"]);
    setFolderQuery("");
    setViewModalOpen(false);
    setViewName("");
    setViewBaseline("");
    setEditingView(null);
    setViewError(null);
    viewWriteToken.current += 1;
    viewWriteRef.current = null;
    setViewBusy(false);
    setFeedback(null);
    setAssetAction(null);
    assetPhaseReaderRef.current = null;
    assetActionGeneration.current += 1;
    setCopiedCase(null);
    setBulkMode("page"); setSelectedCaseIds([]);
  }, [owner]);
  useEffect(() => { if (!active) assetActionGeneration.current += 1; }, [active]);

  useEffect(() => {
    if (!active || validAssetFocus == null || consumedFocusTokens.current.has(validAssetFocus.token)) return;
    prepareQueryChange();
    consumedFocusTokens.current.add(validAssetFocus.token);
    setSelectedViewId(null);
    setFilters(normalizeCaseLibraryFilters({ ...defaultCaseLibraryFilters(), q: validAssetFocus.name, state: validAssetFocus.mode === "restore" ? "archived" : "all" }));
    setCursorStack([null]);
  }, [active, validAssetFocus]);

  useEffect(() => {
    if (seenFolderRefreshToken.current === folderRefreshToken) return;
    prepareQueryChange();
    seenFolderRefreshToken.current = folderRefreshToken;
    setCursorStack([null]);
    library.reload();
  }, [folderRefreshToken, library]);

  useLayoutEffect(() => {
    const region = tableRegionRef.current;
    if (region === null) return;
    const measure = () => {
      const regionHeight = region.getBoundingClientRect().height || region.clientHeight;
      const header = region.querySelector<HTMLElement>(".ant-table-thead");
      const headerHeight = header?.getBoundingClientRect().height ?? 55;
      // 横向滚动条也属于表格可用区域；留出其标准高度，避免末行或滚动条被wrapper裁剪。
      const next = Math.max(120, Math.floor(regionHeight - headerHeight - 16));
      setTableScrollY((current) => current === next ? current : next);
    };
    measure();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    observer?.observe(region);
    const header = region.querySelector<HTMLElement>(".ant-table-thead");
    if (header !== null) observer?.observe(header);
    window.addEventListener("resize", measure);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [feedback, filters.folder, library.data, library.error, views.error]);

  const replaceFilters = (next: CaseLibraryFilters) => {
    prepareQueryChange();
    setFilters(normalizeCaseLibraryFilters(next));
    setCursorStack([null]);
  };
  const startBulkAction = (action: "move" | "archive" | "restore") => {
    if (!library.data) return;
    if (bulkMode === "filter" && library.data.total > 500) { setFeedback({ type: "error", message: `当前筛选命中 ${library.data.total} 条，单次最多处理 500 条；请缩小筛选范围。` }); return; }
    const selector = bulkMode === "filter"
      ? { mode: "filter" as const, filters: normalizeCaseLibraryFilters(filters) }
      : { mode: "explicit" as const, items: library.data.items.filter((item) => selectedCaseIds.includes(item.id)).map((item) => ({ resource_type: "case" as const, id: item.id, expected_rev: item.draft_rev })) };
    const count = selector.mode === "filter" ? library.data.total : selector.items.length;
    if (count === 0) { setFeedback({ type: "warning", message: bulkMode === "filter" ? "当前筛选没有可处理的用例。" : "请先勾选当前页中的用例。" }); return; }
    beginAssetAction({ action, target: { resourceType: "bulk", intentId: crypto.randomUUID(), label: bulkMode === "filter" ? `当前筛选全部命中（${count} 条）` : `当前页所选（${count} 条）`, selector } });
  };
  const refreshFoldersAndLibrary = (preserveAssetIntent = false) => {
    if (!preserveAssetIntent) prepareQueryChange();
    folderTreeState.refresh();
    setCursorStack([null]);
    library.reload();
  };
  const patchFilters = (patch: Partial<CaseLibraryFilters>) => replaceFilters({ ...filters, ...patch });

  const selectTreeNode = (key: string) => {
    setSelectedViewId(null);
    const standardSort = filters.sort === "recent_desc" ? "updated_desc" : filters.sort;
    if (key === "all") return replaceFilters({ ...filters, folder: "all", folder_id: null, include_descendants: false, collection: "all", sort: standardSort });
    if (key === "unfiled") return replaceFilters({ ...filters, folder: "unfiled", folder_id: null, include_descendants: false, collection: "all", sort: standardSort });
    if (key === "favorites") return replaceFilters({ ...filters, folder: "all", folder_id: null, include_descendants: false, collection: "favorites", sort: standardSort });
    if (key === "recent") return replaceFilters({ ...filters, folder: "all", folder_id: null, collection: "recent", sort: "recent_desc" });
    const folder = folderById.get(key);
    replaceFilters({
      ...filters,
      folder: "exact",
      folder_id: key,
      include_descendants: true,
      collection: "all",
      sort: standardSort,
      state: folder?.availability === "available" ? "active" : "archived",
    });
  };

  const selectedTreeKey = filters.collection !== "all"
    ? filters.collection
    : filters.folder === "exact" && filters.folder_id
      ? filters.folder_id
      : filters.folder;

  const openViewModal = (view: CaseSavedView | null) => {
    setEditingView(view);
    const name = view?.name ?? "";
    setViewName(name);
    setViewBaseline(name);
    setViewModalOpen(true);
    setViewError(null);
    setFeedback(null);
  };

  const saveView = async () => {
    if (viewWriteRef.current !== null || !active) return;
    const name = viewName.trim();
    if (!name) {
      setViewError("请输入视图名称。");
      return;
    }
    const actionOwner = owner;
    viewWriteToken.current += 1;
    const token = viewWriteToken.current;
    viewWriteRef.current = { owner: actionOwner, token };
    setViewBusy(true);
    setViewError(null);
    try {
      const saved = editingView === null
        ? await createCaseView(workspaceId, projectId, name, filters)
        : await updateCaseView(workspaceId, projectId, editingView, name, filters);
      if (ownerRef.current !== actionOwner || viewWriteRef.current?.token !== token) return;
      setSelectedViewId(saved.id);
      setViewBaseline(saved.name);
      setViewModalOpen(false);
      views.reload();
      setFeedback({ type: "success", message: editingView === null ? "筛选视图已保存。" : "筛选视图已更新。" });
    } catch (error) {
      if (ownerRef.current === actionOwner && viewWriteRef.current?.token === token) setViewError(errorMessage(error));
    } finally {
      if (ownerRef.current === actionOwner && viewWriteRef.current?.token === token) {
        viewWriteRef.current = null;
        setViewBusy(false);
      }
    }
  };

  const removeView = async () => {
    if (editingView === null || viewWriteRef.current !== null || !active) return;
    const actionOwner = owner;
    viewWriteToken.current += 1;
    const token = viewWriteToken.current;
    viewWriteRef.current = { owner: actionOwner, token };
    setViewBusy(true);
    setViewError(null);
    try {
      await deleteCaseView(workspaceId, projectId, editingView);
      if (ownerRef.current !== actionOwner || viewWriteRef.current?.token !== token) return;
      setSelectedViewId(null);
      setViewModalOpen(false);
      views.reload();
      setFeedback({ type: "success", message: "筛选视图已删除。" });
    } catch (error) {
      if (ownerRef.current === actionOwner && viewWriteRef.current?.token === token) setViewError(errorMessage(error));
    } finally {
      if (ownerRef.current === actionOwner && viewWriteRef.current?.token === token) {
        viewWriteRef.current = null;
        setViewBusy(false);
      }
    }
  };

  const toggleFavorite = async (item: CaseLibraryItem) => {
    const actionOwner = owner;
    setBusyCaseId(item.id);
    try {
      await setFavorite(workspaceId, projectId, item.id, !item.favorite);
      if (ownerRef.current !== actionOwner) return;
      library.reload();
    } catch (error) {
      if (ownerRef.current === actionOwner) setFeedback({ type: "error", message: errorMessage(error) });
    } finally {
      if (ownerRef.current === actionOwner) setBusyCaseId(null);
    }
  };

  const openFolderRestore = async (folder: AssetFolder) => {
    const actionOwner = owner;
    const generation = ++assetActionGeneration.current;
    const expectedArchiveOperationId = folder.archive_operation_id;
    if (folder.restore_mode === null) { setFeedback({ type: "warning", message: "这个目录自身仍是活动状态；请先恢复它的归档上级，或整理受阻用例的归属。" }); return; }
    if (folder.restore_mode === "unavailable") { setFeedback({ type: "error", message: "旧归档来源无法核验，不能猜测恢复根。" }); return; }
    let root = folder;
    if (folder.restore_mode === "locate_root") {
      if (!folder.archive_root_id) { setFeedback({ type: "error", message: "服务端没有提供可核验的归档根。" }); return; }
      try {
        root = await apiSend(projectPath(workspaceId, projectId, `/asset-folders/${folder.archive_root_id}`), "GET", undefined, toAssetFolder);
      } catch (error) {
        if (ownerRef.current === actionOwner && activeRef.current) setFeedback({ type: "error", message: errorMessage(error) }); return;
      }
    }
    if (ownerRef.current !== actionOwner || !activeRef.current || generation !== assetActionGeneration.current) return;
    const locatedRootInvalid = folder.restore_mode === "locate_root" && (root.restore_mode !== "batch_root" || root.archive_operation_id !== expectedArchiveOperationId);
    if (root.id !== (folder.restore_mode === "locate_root" ? folder.archive_root_id : folder.id) || root.availability === "available" || root.restore_mode === null || root.restore_mode === "unavailable" || locatedRootInvalid) { setFeedback({ type: "warning", message: "归档根或批次已经变化，请刷新目录后重新选择恢复对象。" }); return; }
    beginAssetAction({ action: "folder_restore", target: { resourceType: "folder", item: root } });
  };

  const finishAssetOperation = (operation: AssetOperation) => {
    if (operation.result.result_kind === "rejected") {
      closeAssetAction();
      setFeedback({ type: "warning", message: operation.result.message });
      return;
    }
    onAssetsChanged?.();
    refreshFoldersAndLibrary(true);
    const copied = operation.action === "case_copy" ? operation.result.items.find((item) => item.resource_type === "case" && item.outcome === "succeeded") : undefined;
    if (copied) {
      setCopiedCase({ id: copied.id, name: copied.asset?.name ?? "新副本" });
      setFeedback({ type: "success", message: "副本已创建并保留在用例库；需要时可明确打开，不会打断你后来选择的页面或标签。" });
      return;
    }
    const counts = operation.result.counts;
    setFeedback({ type: counts.failed || counts.conflict ? "warning" : "success", message: `操作完成：成功 ${counts.succeeded} 项，无变化 ${counts.no_change} 项，冲突或失败 ${counts.conflict + counts.failed} 项。` });
  };

  const columns: ColumnsType<CaseLibraryItem> = [
    {
      title: "用例",
      key: "case",
      render: (_, item) => (
        <div className="case-library-name">
          <strong>{item.name}{validAssetFocus?.caseId === item.id ? <Tag color="gold">待处理目标</Tag> : null}</strong>
          <span>{item.path}</span>
        </div>
      ),
    },
    { title: "方法", dataIndex: "method", width: 92, render: (method: string) => <Tag color="blue">{method}</Tag> },
    {
      title: "目录",
      key: "folder",
      render: (_, item) => item.folder_id === null
        ? "未分组"
        : item.folder_path === null
          ? "目录不可读取"
          : item.folder_path.length === 0
            ? "目录路径暂不可用"
            : item.folder_path.map((part) => part.name).join(" / "),
    },
    {
      title: "状态",
      key: "state",
      width: 170,
      render: (_, item) => item.availability === "available"
        ? <Tag color="green">可用</Tag>
        : item.availability === "case_archived"
          ? <Tag>用例已归档</Tag>
          : <Tag color="orange">旧目录归档，待整理</Tag>,
    },
    { title: "更新时间", dataIndex: "updated_at", width: 180, render: formatTime },
    {
      title: "操作",
      key: "actions",
      width: 310,
      render: (_, item) => (
        <Space>
          <Button
            type="text"
            aria-label={item.favorite ? `取消收藏 ${item.name}` : `收藏 ${item.name}`}
            icon={item.favorite ? <StarFilled /> : <StarOutlined />}
            loading={busyCaseId === item.id}
            onClick={() => void toggleFavorite(item)}
          />
          <Button
            type="link"
            disabled={item.availability !== "available" || busyCaseId === item.id}
            title={item.availability === "available" ? undefined : "归档对象当前不能在工作台打开"}
            onClick={async () => {
              setBusyCaseId(item.id);
              const result = await onOpen(item.id);
              setBusyCaseId(null);
              if (result === "limit_reached") setFeedback({ type: "warning", message: "工作台已打开 20 个请求，请先关闭一个标签。" });
              else if (result === "busy") setFeedback({ type: "warning", message: "这条用例正在确认资产操作，结果明确前不能打开新标签。" });
              else if (result === "unavailable") setFeedback({ type: "error", message: "这条用例已不可读取，请刷新用例库。" });
              else library.reload();
            }}
          >
            打开
          </Button>
          {canEdit ? <>
            {item.availability === "available" ? <>
              <Button type="link" onClick={() => beginAssetAction({ action: "case_copy", target: { resourceType: "case", item } })}>复制</Button>
              <Button type="link" onClick={() => beginAssetAction({ action: "move", target: { resourceType: "case", item } })}>移动</Button>
              <Button danger type="link" onClick={() => beginAssetAction({ action: "archive", target: { resourceType: "case", item } })}>归档</Button>
            </> : item.asset_status === "archived"
              ? <Button type="link" onClick={() => beginAssetAction({ action: "restore", target: { resourceType: "case", item } })}>恢复</Button>
              : <Button type="link" onClick={() => beginAssetAction({ action: "move", target: { resourceType: "case", item } })}>整理归属</Button>}
          </> : null}
        </Space>
      ),
    },
  ];

  function activeNodes(items: AssetFolder[]): DataNode[] {
    return items.map((folder) => {
      const bucket = folderTreeState.child(folder.id);
      return {
        key: folder.id,
        title: <Space size={4}><span>{folderTitle(folder)}</span>{canEdit && folder.availability === "available" ? <Button size="small" type="link" danger aria-label={`归档目录 ${folder.name}`} onKeyDown={(event) => event.stopPropagation()} onKeyUp={(event) => event.stopPropagation()} onClick={(event) => { event.stopPropagation(); beginAssetAction({ action: "folder_archive", target: { resourceType: "folder", item: folder } }); }}>归档</Button> : null}</Space>,
        icon: <FolderOutlined />,
        isLeaf: !folder.has_children,
        children: bucket.items.length > 0 || bucket.nextCursor !== null
          ? [...activeNodes(bucket.items), ...(bucket.nextCursor ? [moreNode(`child:${folder.id}`, bucket.loading)] : [])]
          : undefined,
      };
    });
  }
  function moreNode(bucketKey: string, loading: boolean): DataNode {
    return {
      key: `more:${bucketKey}`,
      selectable: false,
      isLeaf: true,
      title: (
        <Button
          size="small"
          loading={loading}
          onKeyDown={(event) => event.stopPropagation()}
          onKeyUp={(event) => event.stopPropagation()}
          onClick={(event) => { event.stopPropagation(); void folderTreeState.loadMore(bucketKey); }}
        >
          加载更多
        </Button>
      ),
    };
  }
  const archivedNodes: DataNode[] = folderTreeState.archived.items.map((folder) => ({
    key: folder.id,
    title: <Space size={4}><span>{`${folderPath(folder.id, folder, false, false)}${folder.restore_mode === "legacy_single" ? "（旧记录，仅恢复此目录）" : folder.restore_mode === "unavailable" ? "（归档来源不可定位）" : folder.restore_mode === null ? "（受归档上级阻挡）" : "（已归档）"}`}</span>{canEdit ? <Button size="small" type="link" aria-label={`恢复目录 ${folder.name}`} disabled={folder.restore_mode === "unavailable" || folder.restore_mode === null} title={folder.restore_mode === "unavailable" ? "归档来源无法核验，不能猜测恢复根" : folder.restore_mode === null ? "请处理真正归档的上级目录" : undefined} onKeyDown={(event) => event.stopPropagation()} onKeyUp={(event) => event.stopPropagation()} onClick={(event) => { event.stopPropagation(); void openFolderRestore(folder); }}>恢复</Button> : null}</Space>,
    icon: <FolderOutlined />,
    isLeaf: true,
  }));
  const searchNodes: DataNode[] = (folderTreeState.search?.items ?? []).map((folder) => ({
    key: folder.id,
    title: <Space size={4}><span>{folderPath(folder.id, folder, false, false)}</span>{canEdit && folder.availability === "available"
      ? <Button size="small" type="link" danger aria-label={`归档目录 ${folder.name}`} onKeyDown={(event) => event.stopPropagation()} onKeyUp={(event) => event.stopPropagation()} onClick={(event) => { event.stopPropagation(); beginAssetAction({ action: "folder_archive", target: { resourceType: "folder", item: folder } }); }}>归档</Button>
      : canEdit ? <Button size="small" type="link" aria-label={`恢复目录 ${folder.name}`} disabled={folder.restore_mode === "unavailable" || folder.restore_mode === null} onKeyDown={(event) => event.stopPropagation()} onKeyUp={(event) => event.stopPropagation()} onClick={(event) => { event.stopPropagation(); void openFolderRestore(folder); }}>恢复</Button> : null}</Space>,
    icon: <FolderOutlined />,
    isLeaf: true,
  }));
  const treeData: DataNode[] = [
    { key: "all", title: "全部用例", isLeaf: true },
    { key: "unfiled", title: "未分组", isLeaf: true },
    { key: "favorites", title: "我的收藏", isLeaf: true },
    { key: "recent", title: "最近打开", isLeaf: true },
    ...(folderTreeState.search === null
      ? [
          { key: "folders", title: "项目目录", selectable: false, children: [...activeNodes(folderTreeState.root.items), ...(folderTreeState.root.nextCursor ? [moreNode("root", folderTreeState.root.loading)] : [])] },
          { key: "archived-folders", title: "归档目录", selectable: false, children: [...archivedNodes, ...(folderTreeState.archived.nextCursor ? [moreNode("archived", folderTreeState.archived.loading)] : [])] },
        ]
      : [{ key: "folder-search-results", title: "目录搜索结果", selectable: false, children: [...searchNodes, ...(folderTreeState.search?.nextCursor ? [moreNode(folderTreeState.searchKey ?? "", folderTreeState.search.loading)] : [])] }]),
  ];

  useEffect(() => {
    if (!active) return;
    for (const key of expandedFolderKeys) {
      const id = String(key);
      const folder = folderById.get(id);
      const bucket = folderTreeState.child(id);
      if (folder?.availability === "available" && folder.has_children && bucket.error === null && !bucket.complete && !bucket.loading && bucket.items.length === 0) {
        void folderTreeState.loadChildren(id);
      }
    }
  }, [active, expandedFolderKeys, folderById, folderTreeState]);

  return (
    <section className="case-library-page" aria-label="用例库内容">
      <header className="page-title-row">
        <div>
          <span className="eyebrow">当前项目</span>
          <h2>用例库</h2>
          <p className="caption">查找项目内的用例，并管理只属于你的收藏、最近记录和筛选视图。</p>
        </div>
      </header>
      {feedback ? <Alert showIcon closable type={feedback.type} title={feedback.message} onClose={() => setFeedback(null)} /> : null}
      {copiedCase ? <Alert showIcon type="success" title={`副本“${copiedCase.name}”已创建`} action={<Button onClick={async () => {
        const result = await onOpen(copiedCase.id);
        if (result === "limit_reached") setFeedback({ type: "warning", message: "工作台已打开 20 个请求；副本仍保留在用例库，请关闭一个标签后再打开。" });
        else if (result === "unavailable" || result === "busy") setFeedback({ type: "warning", message: "副本当前无法打开，但已保留在用例库。" });
      }}>打开副本</Button>} /> : null}
      {validAssetFocus ? <Alert
        showIcon
        type="info"
        title={library.data?.items.some((item) => item.id === validAssetFocus.caseId)
          ? `已精确找到“${validAssetFocus.name}”` : library.loading ? "正在查找原用例…" : library.data?.next_cursor ? "原用例不在当前页，请点击下一页继续查找" : "当前查询结果中未找到原用例，请刷新后重试"}
        description={`目标：${validAssetFocus.method} ${validAssetFocus.path}。只对标记为“待处理目标”的原用例执行${validAssetFocus.mode === "restore" ? "恢复" : "整理"}；同名用例不会被自动选中。`}
      /> : null}
      <div className="case-library-layout">
        <aside className="case-library-tree" aria-label="用例目录与个人集合">
          <Space.Compact block>
          <Input.Search aria-label="搜索目录" allowClear disabled={viewBusy} maxLength={200} placeholder="搜索目录" value={folderQuery} onChange={(event) => {
            const value = event.target.value;
            setFolderQuery(value);
            setExpandedFolderKeys(value.trim() ? ["folder-search-results"] : ["folders", "archived-folders"]);
          }} />
          <Button disabled={viewBusy} onClick={() => refreshFoldersAndLibrary()}>刷新目录</Button>
          </Space.Compact>
          {(folderTreeState.root.loading && folderTreeState.root.items.length === 0) ? <Loading label="正在加载目录…" /> : null}
          {folderTreeState.failures.map(([key, bucket]) => (
            <Alert key={key} type="error" showIcon title={bucket.error?.message ?? "目录加载失败"} action={<Button size="small" onClick={() => void folderTreeState.retry(key)}>重试</Button>} />
          ))}
          <Tree
            disabled={viewBusy}
            showIcon
            blockNode
            loadedKeys={folderTreeState.loadedFolderIds}
            loadData={(node) => {
              const key = String(node.key);
              const folder = folderById.get(key);
              const bucket = folderTreeState.child(key);
              if (folder === undefined || folder.availability !== "available" || !folder.has_children || bucket.error !== null) return Promise.resolve();
              return folderTreeState.loadChildren(key);
            }}
            onLoad={() => undefined}
            expandedKeys={expandedFolderKeys}
            onExpand={(keys) => setExpandedFolderKeys(keys)} selectedKeys={[selectedTreeKey]} treeData={treeData} onSelect={(keys) => {
            const key = String(keys[0] ?? "");
            if (key && !SPECIAL_KEYS.has(key) && !folderById.has(key)) return;
            if (key) selectTreeNode(key);
          }} />
        </aside>
        <div className="case-library-main">
          <div className="case-library-toolbar">
            <Input.Search
              aria-label="搜索用例名称或请求路径"
              allowClear
              disabled={viewBusy}
              maxLength={200}
              value={filters.q ?? ""}
              placeholder="搜索用例名称或请求路径"
              onChange={(event) => patchFilters({ q: event.target.value || undefined })}
            />
            <Select aria-label="请求方法" disabled={viewBusy} allowClear placeholder="全部方法" value={filters.method} options={METHOD_OPTIONS} onChange={(method) => patchFilters({ method })} />
            <Select
              aria-label="资产状态"
              disabled={viewBusy}
              value={filters.state}
              options={[{ value: "active", label: "活动" }, { value: "archived", label: "归档入口" }, { value: "all", label: "全部状态" }]}
              onChange={(state) => patchFilters({ state })}
            />
            <Select
              aria-label="排序"
              disabled={viewBusy}
              value={filters.sort}
              options={[
                { value: "updated_desc", label: "最近更新" }, { value: "name_asc", label: "名称升序" },
                { value: "name_desc", label: "名称降序" }, { value: "method_asc", label: "方法升序" },
                { value: "method_desc", label: "方法降序" },
                ...(filters.collection === "recent" ? [{ value: "recent_desc", label: "最近打开时间" }] : []),
              ]}
              onChange={(sort) => patchFilters({ sort })}
            />
            {filters.folder === "exact" ? (
              <Checkbox disabled={viewBusy} checked={filters.include_descendants} onChange={(event) => patchFilters({ include_descendants: event.target.checked })}>包含子目录</Checkbox>
            ) : null}
          </div>
          <div className="case-library-control-stack"><div className="case-library-views">
            <Select
              aria-label="保存的筛选视图"
              allowClear
              disabled={viewBusy}
              placeholder="保存的筛选视图"
              loading={views.loading}
              value={selectedViewId ?? undefined}
              options={(views.data ?? []).map((view) => ({ value: view.id, label: view.name }))}
              onChange={(id) => {
                const view = (views.data ?? []).find((item) => item.id === id);
                setSelectedViewId(id ?? null);
                if (view) replaceFilters(view.filters);
              }}
            />
            <Button aria-label="保存当前视图" aria-haspopup="dialog" aria-expanded={active && viewModalOpen && editingView === null} icon={<SaveOutlined />} onClick={() => openViewModal(null)} disabled={viewBusy || (views.data?.length ?? 0) >= 20}>保存当前视图</Button>
            <Button aria-label="更新所选视图" icon={<EditOutlined />} disabled={viewBusy || selectedViewId === null} onClick={() => openViewModal((views.data ?? []).find((item) => item.id === selectedViewId) ?? null)}>更新所选视图</Button>
            <span className="caption">{views.data === null ? "—" : views.data.length}/20</span>
            {views.error ? <ErrorText message={views.error.message} /> : null}
          </div>
          {canEdit ? <div className="case-library-views" aria-label="批量整理">
            <Segmented disabled={assetQueryLocked} value={bulkMode} options={[{ value: "page", label: `当前页所选（${selectedCaseIds.length}）` }, { value: "filter", label: `筛选全部命中（${library.data?.total ?? "—"}）` }]} onChange={(value) => { setBulkMode(value as "page" | "filter"); setSelectedCaseIds([]); }} />
            <Button disabled={assetAction !== null || library.loading} onClick={() => startBulkAction("move")}>批量移动</Button>
            <Button disabled={assetAction !== null || library.loading} danger onClick={() => startBulkAction("archive")}>批量归档</Button>
            <Button disabled={assetAction !== null || library.loading} onClick={() => startBulkAction("restore")}>批量恢复</Button>
            <span className="caption">本页处理当前勾选项；全部命中包含其它页面中符合当前筛选的用例。</span>
          </div> : null}</div>
          <div ref={tableRegionRef} className="case-library-table-region">
          {library.error ? (
            <Alert
              type="error"
              showIcon
              title={library.error.message}
              action={<Space><Button onClick={() => { prepareQueryChange(); library.reload(); }}>重试当前查询</Button><Button onClick={() => { prepareQueryChange(); setCursorStack([null]); }}>返回第一页</Button></Space>}
            />
          ) : library.loading && library.data === null ? (
            <Loading label="正在查询用例…" />
          ) : library.data !== null && library.data.items.length === 0 ? (
            <Empty label="没有符合当前条件的用例。" />
          ) : library.data !== null ? (
            <Table<CaseLibraryItem>
              rowKey="id"
              loading={library.loading}
              columns={columns}
              dataSource={library.data.items}
              pagination={false}
              rowSelection={bulkMode === "page" ? { selectedRowKeys: selectedCaseIds, preserveSelectedRowKeys: false, getCheckboxProps: () => ({ disabled: library.loading || assetAction !== null }), onChange: (keys) => { if (library.loading || assetAction !== null || library.data === null) return; const currentIds = new Set(library.data.items.map((item) => item.id)); setSelectedCaseIds(keys.map(String).filter((id) => currentIds.has(id))); } } : undefined}
              scroll={{ x: 980, y: tableScrollY }}
            />
          ) : null}
          </div>
          <div className="case-library-pagination" aria-label="用例库分页">
            <span>{library.data === null ? "总数尚未加载" : `共 ${library.data.total} 条，第 ${cursorStack.length} 页`}</span>
            <Select aria-label="每页数量" disabled={assetQueryLocked} value={limit} options={[20, 50, 100].map((value) => ({ value, label: `${value} 条/页` }))} onChange={(value) => { prepareQueryChange(); setLimit(value); setCursorStack([null]); }} />
            <Button disabled={assetQueryLocked || cursorStack.length <= 1 || library.loading} onClick={() => { prepareQueryChange(); setCursorStack((current) => current.slice(0, -1)); }}>上一页</Button>
            <Button disabled={assetQueryLocked || library.data?.next_cursor == null || library.loading} onClick={() => { prepareQueryChange(); setCursorStack((current) => [...current, library.data?.next_cursor ?? null]); }}>下一页</Button>
          </div>
        </div>
      </div>
      <Modal
        open={active && viewModalOpen}
        title={editingView === null ? "保存筛选视图" : "更新筛选视图"}
        destroyOnHidden={false}
        onCancel={() => { if (!viewBusy) setViewModalOpen(false); }}
        footer={[
          editingView ? <Button key="delete" danger icon={<DeleteOutlined />} disabled={viewBusy} onClick={() => void removeView()}>删除视图</Button> : null,
          <Button key="cancel" disabled={viewBusy} onClick={() => setViewModalOpen(false)}>取消</Button>,
          <Button key="save" type="primary" loading={viewBusy} onClick={() => void saveView()}>保存</Button>,
        ]}
      >
        <label className="param">
          <span>视图名称</span>
          <Input autoFocus disabled={viewBusy} maxLength={100} value={viewName} onChange={(event) => setViewName(event.target.value)} />
        </label>
        <p className="caption">保存当前搜索、方法、状态、目录、个人集合和排序；不会保存页码或用例内容。</p>
        {viewError ? <Alert type="error" showIcon title={viewError} /> : null}
      </Modal>
      {assetAction ? <AssetActionModal
        workspaceId={workspaceId}
        projectId={projectId}
        principalId={principalId}
        active={active}
        target={assetAction.target}
        action={assetAction.action}
        folders={folderTreeState.allItems}
        onAcquire={onAcquireAssetOperation}
        onClose={closeAssetAction}
        onDone={finishAssetOperation}
        onPhaseChange={setAssetPhase}
        onPhaseReader={registerAssetPhaseReader}
      /> : null}
    </section>
  );
}
