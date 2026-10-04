import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, apiSend, projectPath } from "../api/client";
import type { AssetAction, AssetOperation, AssetSelection } from "../api/types";
import { useLeaveReport } from "../hooks/leaveGuard";
import { toAssetOperation, toAssetSelection } from "./assetGuards";

export type AssetOperationPayload = Record<string, unknown> & { schema_version: 1; action: AssetAction };
type PreviewState = { phase: "preview"; selection: AssetSelection; request: Record<string, unknown> };
type PendingState = { phase: "submitting" | "unknown"; key: string; payload: AssetOperationPayload; preview: AssetSelection | null; message?: string; retryBlocked?: boolean };
type State = { phase: "idle" } | PreviewState | PendingState | { phase: "done"; operation: AssetOperation } | { phase: "error"; message: string };
export function newAssetOperationKey(): string { return `asset:${crypto.randomUUID()}`; }

function identity(resourceType: string, id: string): string { return `${resourceType}:${id}`; }

export function useAssetOperation(workspaceId: string, projectId: string, principalId: string, active: boolean) {
  const owner = `${workspaceId}/${projectId}/${principalId}`;
  const liveOwner = useRef(owner); liveOwner.current = owner;
  const stateRef = useRef<State>({ phase: "idle" });
  const [state, setState] = useState<State>(stateRef.current);
  const previewGeneration = useRef(0);
  const replace = useCallback((next: State) => { stateRef.current = next; setState(next); }, []);
  useLeaveReport(`asset-operation:${owner}`, { dirty: state.phase === "preview", busy: state.phase === "submitting" || state.phase === "unknown" });
  useEffect(() => { previewGeneration.current += 1; replace({ phase: "idle" }); }, [owner, replace]);
  const writeActive = () => { const phase = stateRef.current.phase; return phase === "submitting" || phase === "unknown"; };

  const validate = useCallback((operation: AssetOperation, key: string, payload: AssetOperationPayload, preview: AssetSelection | null) => {
    if (operation.workspace_id !== workspaceId || operation.project_id !== projectId || operation.principal_id !== principalId || operation.operation_key !== key || operation.action !== payload.action) throw new Error("资产操作回执归属不匹配");
    const result = operation.result;
    if (payload.action === "case_copy") {
      if (result.selection_id !== null) throw new Error("复制回执错误关联了选择预览");
      if (result.result_kind === "completed") {
        const sourceId = String(payload.source_id ?? "");
        if (result.items.length !== 1 || result.items[0].resource_type !== "case" || result.items[0].id === sourceId || result.items[0].outcome !== "succeeded" || result.items[0].asset?.id !== result.items[0].id || result.items[0].asset.rev !== 1 || result.items[0].asset.state !== "active") throw new Error("复制回执没有唯一的新用例");
      }
      return operation;
    }
    if (preview === null || payload.selection_id !== preview.selection_id || result.selection_id !== preview.selection_id) throw new Error("资产回执与原预览不匹配");
    const folderAction = payload.action === "folder_archive" || payload.action === "folder_restore";
    if (folderAction && (result.result_kind === "completed" ? result.root?.id : preview.root?.id) !== preview.root?.id) throw new Error("目录回执根与原预览不匹配");
    if (result.result_kind === "completed") {
      const selected = new Map([...preview.preview_items, ...preview.excluded_items].map((item) => [identity(item.resource_type, item.id), item]));
      const resultIds = result.items.map((item) => identity(item.resource_type, item.id));
      if (resultIds.length !== selected.size || resultIds.some((id) => !selected.has(id))) throw new Error("资产回执对象集合与原预览不完整相等");
      const memberIds = new Set(result.members.map((member) => identity(member.resource_type, member.id)));
      const expectedMembers = folderAction ? new Set(result.items.filter((item) => item.outcome === "succeeded").map((item) => identity(item.resource_type, item.id))) : new Set<string>();
      if (memberIds.size !== expectedMembers.size || [...memberIds].some((id) => !expectedMembers.has(id))) throw new Error("目录成员投影不完整");
      for (const member of result.members) {
        const source = selected.get(identity(member.resource_type, member.id));
        const item = result.items.find((candidate) => candidate.resource_type === member.resource_type && candidate.id === member.id);
        const originalParent = member.resource_type === "folder" ? source?.parent_id : source?.folder_id;
        if (source === undefined || item === undefined || source.outcome !== "eligible" || source.rev !== member.before_rev || source.state !== member.before_state || originalParent !== member.original_parent_id || item.new_rev !== member.after_rev || member.after_rev <= member.before_rev) throw new Error("资产回执成员与冻结预览不匹配");
      }
    }
    return operation;
  }, [principalId, projectId, workspaceId]);

  const preview = useCallback(async (body: Record<string, unknown>, isCurrent: () => boolean = () => true) => {
    if (!active || stateRef.current.phase === "submitting" || stateRef.current.phase === "unknown") return null;
    previewGeneration.current += 1;
    const generation = previewGeneration.current;
    try {
      const value = await apiSend(projectPath(workspaceId, projectId, "/asset-selections"), "POST", body, toAssetSelection);
      if (value.workspace_id !== workspaceId || value.project_id !== projectId || value.principal_id !== principalId || value.action !== body.action) throw new Error("资产预览归属不匹配");
      if (value.mode !== body.mode) throw new Error("资产预览模式与请求不匹配");
      if (value.mode === "explicit") {
        const requested = Array.isArray(body.items) ? body.items as Array<{ resource_type?: unknown; id?: unknown; expected_rev?: unknown }> : [];
        const returned = [...value.preview_items, ...value.excluded_items];
        if (requested.length !== returned.length || requested.some((item) => !returned.some((candidate) => candidate.resource_type === item.resource_type && candidate.id === item.id && (candidate.outcome === "excluded" || candidate.rev === item.expected_rev)))) throw new Error("资产预览对象与请求不匹配");
      } else if (value.mode === "folder") {
        const requestedRoot = body.root as { resource_type?: unknown; id?: unknown; expected_rev?: unknown } | undefined;
        if (requestedRoot?.resource_type !== "folder" || value.root?.id !== requestedRoot.id || value.root?.expected_rev !== requestedRoot.expected_rev) throw new Error("目录预览根与请求不匹配");
      } else if (typeof body.filters !== "object" || body.filters === null || value.root !== null || value.counts.folders !== 0 || [...value.preview_items, ...value.excluded_items].some((item) => item.resource_type !== "case")) throw new Error("筛选预览与请求不匹配");
      if (liveOwner.current !== owner || generation !== previewGeneration.current || writeActive() || !isCurrent()) return null;
      replace({ phase: "preview", selection: value, request: body });
      return value;
    } catch (error) {
      if (liveOwner.current === owner && generation === previewGeneration.current && !writeActive() && isCurrent()) replace({ phase: "error", message: error instanceof Error ? error.message : "预览失败" });
      return null;
    }
  }, [active, owner, principalId, projectId, replace, workspaceId]);

  const post = useCallback(async (payload: AssetOperationPayload, key: string, previewValue: AssetSelection | null) => {
    const operation = await apiSend(projectPath(workspaceId, projectId, "/asset-operations"), "POST", payload, toAssetOperation, { headers: { "Idempotency-Key": key } });
    return validate(operation, key, payload, previewValue);
  }, [projectId, validate, workspaceId]);

  const submit = useCallback(async (payload: AssetOperationPayload, key = newAssetOperationKey()) => {
    if (!active || stateRef.current.phase === "submitting" || stateRef.current.phase === "unknown") return null;
    previewGeneration.current += 1;
    const previewValue = stateRef.current.phase === "preview" ? stateRef.current.selection : null;
    replace({ phase: "submitting", key, payload, preview: previewValue });
    try {
      const operation = await post(payload, key, previewValue);
      if (liveOwner.current !== owner) return null;
      replace({ phase: "done", operation });
      return operation;
    } catch (error) {
      if (liveOwner.current !== owner) return null;
      const definite = error instanceof ApiError && ((error.status === 400 && error.code === "invalid_idempotency_key") || (error.status === 422 && error.code === "invalid_request") || (error.status === 428 && error.code === "idempotency_key_required"));
      if (definite) replace({ phase: "error", message: error.message });
      else replace({ phase: "unknown", key, payload, preview: previewValue, message: error instanceof Error ? error.message : "结果不明", retryBlocked: error instanceof ApiError && error.code === "idempotency_key_conflict" });
      return null;
    }
  }, [active, owner, post, replace]);

  const reconcile = useCallback(async () => {
    const current = stateRef.current;
    if (!active || current.phase !== "unknown" || current.retryBlocked) return null;
    try {
      const operation = await apiSend(projectPath(workspaceId, projectId, `/asset-operations/by-key/${encodeURIComponent(current.key)}`), "GET", undefined, toAssetOperation);
      const valid = validate(operation, current.key, current.payload, current.preview);
      if (liveOwner.current === owner) replace({ phase: "done", operation: valid });
      return valid;
    } catch (error) {
      if (liveOwner.current !== owner) return null;
      if (!(error instanceof ApiError && error.status === 404)) {
        replace({ ...current, message: error instanceof Error ? error.message : "仍无法确认", retryBlocked: error instanceof ApiError && error.code === "idempotency_key_conflict" });
        return null;
      }
      try {
        const operation = await post(current.payload, current.key, current.preview);
        if (liveOwner.current === owner) replace({ phase: "done", operation });
        return operation;
      } catch (retry) {
        if (liveOwner.current === owner) replace({ ...current, message: retry instanceof Error ? retry.message : "仍无法确认", retryBlocked: retry instanceof ApiError && retry.code === "idempotency_key_conflict" });
        return null;
      }
    }
  }, [active, owner, post, projectId, replace, validate, workspaceId]);

  return {
    state,
    preview,
    submit,
    reconcile,
    getPhase: useCallback(() => stateRef.current.phase, []),
    invalidatePreview: useCallback(() => { previewGeneration.current += 1; }, []),
    reset: useCallback(() => { previewGeneration.current += 1; replace({ phase: "idle" }); }, [replace]),
  };
}
