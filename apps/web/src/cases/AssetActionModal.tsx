import { Alert, Button, Input, Modal, Select, Space, Table, Tag } from "antd";
import { useEffect, useMemo, useRef, useState } from "react";
import type { AssetAction, AssetFolder, AssetOperation, AssetOperationItem, AssetSelectionItem, CaseLibraryItem } from "../api/types";
import type { AcquireAssetOperation, AssetOperationLease } from "./assetController";
import { useLeaveReport } from "../hooks/leaveGuard";
import { apiSend, projectPath } from "../api/client";
import { toAssetFolder, toAssetFolderPage, toCaseDetail } from "../api/guards";
import { newAssetOperationKey, useAssetOperation, type AssetOperationPayload } from "./useAssetOperation";

export type AssetActionTarget =
  | { resourceType: "case"; item: CaseLibraryItem }
  | { resourceType: "folder"; item: AssetFolder };

const TITLES: Record<AssetAction, string> = {
  case_copy: "复制用例", move: "移动用例", archive: "归档用例", restore: "恢复用例",
  folder_archive: "归档目录树", folder_restore: "恢复目录树",
};
const REASON_LABELS: Record<string, string> = {
  revision_or_state_conflict: "修订或状态已变化，请重新读取后再试",
  revision_conflict: "修订已变化，请重新读取后再试",
  no_longer_controlled: "对象已不再受原归档批次控制",
  not_found_or_inaccessible: "对象不存在或当前不可访问",
  already_archived: "此前已经归档",
  already_restored: "此前已经恢复",
  not_in_archive_operation: "不属于本次归档批次",
  selection_changed: "预览范围已经变化",
  selection_expired: "预览已经过期",
  target_invalid: "目标目录当前不可用",
  invalid_target: "目标目录当前不可用",
  folder_unavailable: "原目录不可用，请明确选择新位置",
  name_conflict: "名称与现有对象冲突",
  archive_source_unavailable: "归档来源无法核验",
  archive_root_required: "必须从当前归档根恢复",
};
function reasonLabel(code: string | null): string { return code === null ? "不适用于本次操作" : REASON_LABELS[code] ?? "状态已变化，请刷新后重试"; }
function outcomeLabel(item: AssetOperationItem): string {
  const outcome = item.outcome === "succeeded" ? "成功" : item.outcome === "no_change" ? "无变化" : item.outcome === "conflict" ? "冲突" : "失败";
  const reason = item.code ? reasonLabel(item.code) : item.message;
  return `${outcome}：${reason}`;
}

export function AssetActionModal({ workspaceId, projectId, principalId, active, target, action, folders, onAcquire, onClose, onDone }: {
  workspaceId: string;
  projectId: string;
  principalId: string;
  active: boolean;
  target: AssetActionTarget | null;
  action: AssetAction | null;
  folders: AssetFolder[];
  onAcquire?: AcquireAssetOperation;
  onClose: () => void;
  onDone: (operation: AssetOperation) => void;
}) {
  const flow = useAssetOperation(workspaceId, projectId, principalId, active && target !== null && action !== null);
  const [name, setName] = useState("");
  const [targetFolder, setTargetFolder] = useState<string | null | undefined>(undefined);
  const [rootName, setRootName] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const [blockedLocate, setBlockedLocate] = useState<(() => void) | null>(null);
  const operationKeyRef = useRef(newAssetOperationKey());
  const [sourceTarget, setSourceTarget] = useState<AssetActionTarget | null>(target);
  const sourceTargetRef = useRef<AssetActionTarget | null>(target); sourceTargetRef.current = sourceTarget;
  const sourceReadGeneration = useRef(0);
  const sourceReadRef = useRef<{ generation: number; owner: string; action: AssetAction; sourceId: string; phase: string } | null>(null);
  const flowPhaseRef = useRef(flow.state.phase); flowPhaseRef.current = flow.state.phase;
  const activeRef = useRef(active); activeRef.current = active;
  const ownerRef = useRef(`${workspaceId}/${projectId}/${principalId}`); ownerRef.current = `${workspaceId}/${projectId}/${principalId}`;
  const nameTouched = useRef(false);
  const rootNameTouched = useRef(false);
  const nameBaseline = useRef("");
  const rootNameBaseline = useRef("");
  const initialArchiveOperationId = useRef<string | null>(target?.resourceType === "folder" ? target.item.archive_operation_id : null);
  const leaseRef = useRef<AssetOperationLease | null>(null);
  const targetReadGeneration = useRef(0);
  const inputRevision = useRef(0);
  const nameRef = useRef(name); nameRef.current = name;
  const targetFolderRef = useRef(targetFolder); targetFolderRef.current = targetFolder;
  const rootNameRef = useRef(rootName); rootNameRef.current = rootName;
  const [targetQuery, setTargetQuery] = useState("");
  const [appliedTargetQuery, setAppliedTargetQuery] = useState("");
  const [selectedTargetLabel, setSelectedTargetLabel] = useState<string | null>(null);
  const [targetCandidates, setTargetCandidates] = useState<AssetFolder[]>(folders);
  const [targetNextCursor, setTargetNextCursor] = useState<string | null>(null);
  const [targetLoading, setTargetLoading] = useState(false);
  const [sourceLoading, setSourceLoading] = useState(false);
  const initialName = action === "case_copy" && target?.resourceType === "case" ? `${target.item.name} 副本` : "";
  const initialRootName = action === "folder_restore" ? target?.item.name ?? "" : "";
  const formDirty = name !== nameBaseline.current || targetFolder !== undefined || rootName !== rootNameBaseline.current;
  useLeaveReport(`asset-modal:${workspaceId}/${projectId}/${principalId}`, { dirty: formDirty, busy: flow.state.phase === "submitting" || flow.state.phase === "unknown" });
  const folderOptions = useMemo(() => [
    { value: "__keep__", label: "保留原位置" }, { value: "__root__", label: "未分组 / 根目录" },
    ...targetCandidates.filter((folder) => folder.availability === "available" && folder.id !== target?.item.id).map((folder) => ({ value: folder.id, label: [...folder.ancestor_path.map((part) => part.name), folder.name].join(" / ") })),
    ...(typeof targetFolder === "string" && !targetCandidates.some((folder) => folder.id === targetFolder) ? [{ value: targetFolder, label: selectedTargetLabel ?? "原目标（当前搜索结果中不可见）" }] : []),
  ], [selectedTargetLabel, targetCandidates, target?.item.id, targetFolder]);

  const release = () => { leaseRef.current?.release(); leaseRef.current = null; };
  useEffect(() => {
    if (flowPhaseRef.current !== "submitting" && flowPhaseRef.current !== "unknown") release();
    sourceReadGeneration.current += 1;
    sourceReadRef.current = null;
    operationKeyRef.current = newAssetOperationKey();
    sourceTargetRef.current = target; setSourceTarget(target);
    nameTouched.current = false; rootNameTouched.current = false;
    initialArchiveOperationId.current = target?.resourceType === "folder" ? target.item.archive_operation_id : null;
    setName(initialName);
    setRootName(initialRootName);
    nameBaseline.current = initialName; rootNameBaseline.current = initialRootName;
    setTargetFolder(undefined); setLocalError(null); setBlockedLocate(null); flow.reset();
    setTargetQuery(""); setAppliedTargetQuery(""); setSelectedTargetLabel(null); setTargetCandidates(folders); setTargetNextCursor(null); setTargetLoading(false); setSourceLoading(false); targetReadGeneration.current += 1; inputRevision.current += 1;
    return () => { sourceReadGeneration.current += 1; sourceReadRef.current = null; if (flowPhaseRef.current !== "submitting" && flowPhaseRef.current !== "unknown") release(); };
    // flow.reset 是稳定回调；仅在目标身份变化时重置。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [action, target?.item.id, workspaceId, projectId, principalId]);
  useEffect(() => { if (!active) { sourceReadGeneration.current += 1; sourceReadRef.current = null; setSourceLoading(false); } }, [active]);
  useEffect(() => { if (flow.state.phase === "error") release(); }, [flow.state.phase]);

  const parameters = (): Record<string, unknown> => {
    if (action === "move") return { target_folder_id: targetFolderRef.current === undefined ? null : targetFolderRef.current };
    if (action === "restore") return targetFolderRef.current === undefined ? {} : { target_folder_id: targetFolderRef.current };
    if (action === "folder_restore") return {
      ...(targetFolderRef.current === undefined ? {} : { target_parent_id: targetFolderRef.current }),
      ...(rootNameRef.current.trim() && rootNameRef.current.trim() !== (sourceTargetRef.current?.item.name ?? target?.item.name) ? { root_name: rootNameRef.current.trim() } : {}),
    };
    return {};
  };
  const refreshSource = async () => {
    if (!target || !action || flowPhaseRef.current === "submitting" || flowPhaseRef.current === "unknown" || sourceReadRef.current !== null) return;
    const generation = ++sourceReadGeneration.current;
    const startedOwner = ownerRef.current;
    const intent = { generation, owner: startedOwner, action, sourceId: target.item.id, phase: flowPhaseRef.current };
    sourceReadRef.current = intent;
    setSourceLoading(true); setLocalError(null); setBlockedLocate(null);
    try {
      let next: AssetActionTarget;
      let defaultName: string | null = null;
      let defaultRootName: string | null = null;
      if (target.resourceType === "case") {
        const detail = await apiSend(projectPath(workspaceId, projectId, `/cases/${target.item.id}`), "GET", undefined, toCaseDetail);
        if (["case_copy", "move", "archive"].includes(action) && detail.status !== "draft") throw new Error("来源用例当前已不可用于这项操作，请刷新用例库后重新选择。");
        if (action === "restore" && detail.status !== "archived") throw new Error("来源用例当前不再是归档状态，请刷新用例库后重新选择。");
        next = { resourceType: "case", item: { ...target.item, id: detail.id, name: detail.name, draft_rev: detail.rev, asset_status: detail.status === "archived" ? "archived" : "active", folder_id: detail.folder_id } };
        if (action === "case_copy") defaultName = `${detail.name} 副本`;
      } else {
        const folder = await apiSend(projectPath(workspaceId, projectId, `/asset-folders/${target.item.id}`), "GET", undefined, toAssetFolder);
        if (folder.id !== target.item.id) throw new Error("目录读取结果与原目标不一致，请刷新后重选。");
        if (action === "folder_archive" && folder.availability !== "available") throw new Error("目录当前已不可归档，请刷新后重选。");
        if (action === "folder_restore" && (folder.availability === "available" || (folder.restore_mode !== "batch_root" && folder.restore_mode !== "legacy_single") || folder.archive_operation_id !== initialArchiveOperationId.current)) throw new Error("归档来源或批次已经变化，请刷新目录后重新选择恢复对象。");
        next = { resourceType: "folder", item: folder };
        if (action === "folder_restore") defaultRootName = folder.name;
      }
      if (sourceReadRef.current !== intent || generation !== sourceReadGeneration.current || ownerRef.current !== startedOwner || !activeRef.current || action !== intent.action || target.item.id !== intent.sourceId || flowPhaseRef.current !== intent.phase) return;
      if (defaultName !== null) { nameBaseline.current = defaultName; if (!nameTouched.current) { nameRef.current = defaultName; setName(defaultName); } }
      if (defaultRootName !== null) { rootNameBaseline.current = defaultRootName; if (!rootNameTouched.current) { rootNameRef.current = defaultRootName; setRootName(defaultRootName); } }
      sourceTargetRef.current = next; setSourceTarget(next); operationKeyRef.current = newAssetOperationKey(); flow.reset();
    } catch (cause) {
      if (sourceReadRef.current === intent && generation === sourceReadGeneration.current && ownerRef.current === startedOwner && activeRef.current && flowPhaseRef.current === intent.phase) setLocalError(cause instanceof Error ? cause.message : "来源读取失败，请重试。");
    } finally { if (sourceReadRef.current === intent) { sourceReadRef.current = null; setSourceLoading(false); } }
  };
  const searchTargets = async (query: string, append = false) => {
    const generation = ++targetReadGeneration.current;
    const appliedQuery = append ? appliedTargetQuery : query.trim();
    if (!append) { setAppliedTargetQuery(appliedQuery); setTargetNextCursor(null); }
    setTargetLoading(true); setLocalError(null);
    const params = new URLSearchParams({ parent_mode: "all", state: "active", limit: "100" });
    if (appliedQuery) params.set("q", appliedQuery);
    if (append && targetNextCursor) params.set("cursor", targetNextCursor);
    try {
      const page = await apiSend(projectPath(workspaceId, projectId, `/asset-folders?${params}`), "GET", undefined, toAssetFolderPage);
      if (generation !== targetReadGeneration.current) return;
      setTargetCandidates((current) => append ? [...new Map([...current, ...page.items].map((item) => [item.id, item])).values()] : page.items);
      setTargetNextCursor(page.next_cursor);
    } catch (error) {
      if (generation === targetReadGeneration.current) setLocalError(error instanceof Error ? error.message : "目标目录读取失败");
    } finally { if (generation === targetReadGeneration.current) setTargetLoading(false); }
  };
  const acquire = (targets: Array<{ caseId: string; sourceRev: number }>): boolean => {
    if (target === null || leaseRef.current !== null) return true;
    const acquired = onAcquire?.(targets, operationKeyRef.current);
    if (acquired === undefined) return true;
    if ("message" in acquired) { setLocalError(acquired.message); setBlockedLocate(() => acquired.locate ?? null); return false; }
    leaseRef.current = acquired; return true;
  };
  const finish = (operation: AssetOperation | null) => {
    if (operation === null) return;
    if (operation.result.result_kind === "rejected") { release(); return; }
    let stale = false;
    for (const item of operation.result.items) if (leaseRef.current?.accept(item) === "stale") stale = true;
    release();
    if (stale) setLocalError("操作已完成，但有打开标签的正文或修订已变化；未用回执推进其 ETag，请回到标签处理冲突。");
    onDone(operation);
  };
  const preview = async () => {
    if (!target || !action || sourceReadRef.current !== null) return;
    sourceReadGeneration.current += 1;
    if (flow.state.phase === "done" || flow.state.phase === "error") operationKeyRef.current = newAssetOperationKey();
    setLocalError(null);
    const currentTarget = sourceTarget ?? target;
    const selection = currentTarget.resourceType === "case"
      ? { schema_version: 1, action, mode: "explicit", items: [{ resource_type: "case", id: currentTarget.item.id, expected_rev: currentTarget.item.draft_rev }], parameters: parameters() }
      : { schema_version: 1, action, mode: "folder", root: { resource_type: "folder", id: currentTarget.item.id, expected_rev: currentTarget.item.rev }, parameters: parameters() };
    const frozenParameters = JSON.stringify(selection.parameters);
    const frozenRevision = inputRevision.current;
    const result = await flow.preview(selection);
    if (result !== null && (frozenRevision !== inputRevision.current || frozenParameters !== JSON.stringify(parameters()))) {
      flow.reset(); setLocalError("预览期间目标参数已变化，请按当前输入重新生成预览。");
    }
  };
  const submit = async () => {
    if (!target || !action) return;
    if (sourceReadRef.current !== null) { setLocalError("正在读取最新来源，请等待完成后再确认。"); return; }
    sourceReadGeneration.current += 1;
    const currentTarget = sourceTarget ?? target;
    setLocalError(null);
    let payload: AssetOperationPayload;
    if (action === "case_copy" && currentTarget.resourceType === "case") {
      if (!acquire([{ caseId: currentTarget.item.id, sourceRev: currentTarget.item.draft_rev }])) return;
      payload = { schema_version: 1, action, source_id: currentTarget.item.id, expected_rev: currentTarget.item.draft_rev, ...(nameRef.current.trim() ? { name: nameRef.current.trim() } : {}), ...(targetFolderRef.current === undefined ? {} : { folder_id: targetFolderRef.current }) };
    } else {
      if (flow.state.phase !== "preview") return;
      const cases = flow.state.selection.preview_items.filter((item) => item.resource_type === "case" && item.outcome === "eligible" && item.rev !== null).map((item) => ({ caseId: item.id, sourceRev: item.rev as number }));
      if (!acquire(cases)) return;
      payload = { schema_version: 1, action, selection_id: flow.state.selection.selection_id, parameters: (flow.state.request.parameters ?? {}) as Record<string, unknown> };
    }
    finish(await flow.submit(payload, operationKeyRef.current));
  };
  const reconcile = async () => finish(await flow.reconcile());
  const busy = flow.state.phase === "submitting" || flow.state.phase === "unknown";
  const error = localError ?? (flow.state.phase === "error" ? flow.state.message : flow.state.phase === "unknown" ? `结果不明：${flow.state.message}` : null);
  const requiresPreview = action !== "case_copy";
  const canSubmit = !requiresPreview || flow.state.phase === "preview";
  const chooseFolder = action === "case_copy" || action === "move" || action === "restore" || action === "folder_restore";
  return <Modal width={760} styles={{ body: { maxHeight: "calc(100vh - 220px)", overflowY: "auto" } }} open={active && target !== null && action !== null} title={action ? TITLES[action] : "资产操作"} mask={{ closable: false }} closable={!busy} onCancel={() => { if (!busy) { sourceReadGeneration.current += 1; sourceReadRef.current = null; release(); onClose(); } }} footer={<Space>
    {flow.state.phase === "unknown" ? <Button disabled={flow.state.retryBlocked} onClick={() => void reconcile()}>确认原操作</Button> : null}
    {flow.state.phase === "preview" ? <Button loading={sourceLoading} onClick={() => void refreshSource()}>修改参数并读取最新来源</Button> : null}
    {flow.state.phase === "done" && flow.state.operation.result.result_kind === "rejected" ? <Button loading={sourceLoading} onClick={() => void refreshSource()}>修正并重试</Button> : null}
    <Button disabled={busy} onClick={() => { sourceReadGeneration.current += 1; sourceReadRef.current = null; release(); onClose(); }}>取消</Button>
    {flow.state.phase === "done" ? flow.state.operation.result.result_kind === "completed" ? <Button type="primary" onClick={onClose}>完成</Button> : null : requiresPreview && flow.state.phase !== "preview" ? <Button type="primary" loading={flow.state.phase === "submitting" || sourceLoading} disabled={busy || sourceLoading} onClick={() => void preview()}>生成预览</Button> : <Button type="primary" loading={flow.state.phase === "submitting" || sourceLoading} disabled={busy || sourceLoading || !canSubmit} onClick={() => void submit()}>确认执行</Button>}
  </Space>}>
    <p>操作对象：{sourceTarget?.item.name ?? target?.item.name}{action === "case_copy" ? "（复制当前已保存内容，不保存屏幕上的未提交修改）" : ""}</p>
    {action === "case_copy" ? <label className="param"><span>副本名称</span><Input disabled={busy || sourceLoading} maxLength={200} value={name} onChange={(event) => { inputRevision.current += 1; nameTouched.current = true; nameRef.current = event.target.value; setName(event.target.value); }} /></label> : null}
    {chooseFolder ? <label className="param"><span>{action === "folder_restore" ? "恢复到父目录" : "目标目录"}</span><Space.Compact block><Input.Search aria-label="搜索目标目录" disabled={busy || sourceLoading || flow.state.phase === "preview"} value={targetQuery} onChange={(event) => setTargetQuery(event.target.value)} onSearch={(value) => void searchTargets(value)} loading={targetLoading} /><Select style={{ minWidth: 260 }} disabled={busy || sourceLoading || flow.state.phase === "preview"} value={targetFolder === undefined ? (action === "move" ? "__root__" : "__keep__") : targetFolder === null ? "__root__" : targetFolder} options={action === "move" ? folderOptions.filter((option) => option.value !== "__keep__") : folderOptions} onChange={(value, option) => { const next = value === "__keep__" ? undefined : value === "__root__" ? null : value; inputRevision.current += 1; targetFolderRef.current = next; setSelectedTargetLabel(typeof option === "object" && "label" in option ? String(option.label) : null); setTargetFolder(next); }} /></Space.Compact>{targetNextCursor ? <Button disabled={targetLoading || busy || sourceLoading || flow.state.phase === "preview"} onClick={() => void searchTargets(appliedTargetQuery, true)}>加载更多目标目录</Button> : null}</label> : null}
    {action === "folder_restore" ? <label className="param"><span>根目录名称</span><Input disabled={busy || sourceLoading || flow.state.phase === "preview"} maxLength={200} value={rootName} onChange={(event) => { inputRevision.current += 1; rootNameTouched.current = true; rootNameRef.current = event.target.value; setRootName(event.target.value); }} /></label> : null}
    {flow.state.phase === "preview" ? <><Alert type="info" showIcon title={`将处理 ${flow.state.selection.counts.eligible} 项，排除 ${flow.state.selection.counts.excluded} 项`} description={`其中目录 ${flow.state.selection.counts.folders} 项、用例 ${flow.state.selection.counts.cases} 项；预览于 ${new Date(flow.state.selection.expires_at).toLocaleString("zh-CN", { hour12: false })} 过期。`} /><Table size="small" rowKey={(item) => `${item.resource_type}:${item.id}`} pagination={{ pageSize: 10, showSizeChanger: false, hideOnSinglePage: true }} scroll={{ y: 280, x: 640 }} dataSource={[...flow.state.selection.preview_items, ...flow.state.selection.excluded_items]} columns={[{ title: "对象", render: (_, item: AssetSelectionItem) => item.name ?? `不可见${item.resource_type === "case" ? "用例" : "目录"}` }, { title: "类型", render: (_, item: AssetSelectionItem) => item.resource_type === "case" ? "用例" : "目录" }, { title: "结果", render: (_, item: AssetSelectionItem) => item.outcome === "eligible" ? <Tag color="green">将处理</Tag> : <Tag>排除：{reasonLabel(item.code)}</Tag> }]} /></> : null}
    {flow.state.phase === "done" && flow.state.operation.result.result_kind === "rejected" ? <Alert type="warning" showIcon title={flow.state.operation.result.message} description={flow.state.operation.result.conflicts.map((item) => item.message).join("；") || "本次未修改任何资产，请修正参数后重新预览。"} /> : null}
    {flow.state.phase === "done" && flow.state.operation.result.result_kind === "completed" ? <Table size="small" rowKey={(item) => `${item.resource_type}:${item.id}`} pagination={{ pageSize: 10, showSizeChanger: false, hideOnSinglePage: true }} scroll={{ y: 280, x: 560 }} dataSource={flow.state.operation.result.items} columns={[{ title: "对象", render: (_, item: AssetOperationItem) => item.asset?.name ?? item.id }, { title: "结果", render: (_, item: AssetOperationItem) => <Tag color={item.outcome === "succeeded" ? "green" : item.outcome === "no_change" ? "blue" : "orange"}>{outcomeLabel(item)}</Tag> }]} /> : null}
    {error ? <Alert type="error" showIcon title={error} action={blockedLocate ? <Button onClick={blockedLocate}>去该标签处理</Button> : undefined} /> : null}
  </Modal>;
}
