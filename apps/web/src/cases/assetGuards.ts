import { ContractError } from "../api/guards";
import type { AssetAction, AssetMetadata, AssetOperation, AssetOperationItem, AssetOperationResult, AssetResourceType, AssetSelection, AssetSelectionItem } from "../api/types";

type JsonObject = Record<string, unknown>;
function object(raw: unknown, field: string): JsonObject { if (typeof raw !== "object" || raw === null || Array.isArray(raw)) throw new ContractError(`${field} 应为对象`); return raw as JsonObject; }
function string(raw: unknown, field: string): string { if (typeof raw !== "string") throw new ContractError(`${field} 应为文本`); return raw; }
function integer(raw: unknown, field: string): number { if (!Number.isInteger(raw)) throw new ContractError(`${field} 应为整数`); return raw as number; }
function nullableString(raw: unknown, field: string): string | null { return raw === null ? null : string(raw, field); }
function array<T>(raw: unknown, field: string, parse: (item: unknown, field: string) => T): T[] { if (!Array.isArray(raw)) throw new ContractError(`${field} 应为列表`); return raw.map((item, index) => parse(item, `${field}[${index}]`)); }
function oneOf<T extends string>(raw: unknown, field: string, values: readonly T[]): T { const value = string(raw, field); if (!values.includes(value as T)) throw new ContractError(`${field} 未知`); return value as T; }

const ACTIONS = ["case_copy", "move", "archive", "restore", "folder_archive", "folder_restore"] as const;
const STATES = ["active", "archived"] as const;
function action(raw: unknown, field: string): AssetAction { return oneOf(raw, field, ACTIONS); }
function resource(raw: unknown, field: string): AssetResourceType { return oneOf(raw, field, ["case", "folder"] as const); }
function selectionItem(raw: unknown, field: string): AssetSelectionItem {
  const value = object(raw, field);
  const outcome = oneOf(value.outcome, `${field}.outcome`, ["eligible", "excluded"] as const);
  const code = nullableString(value.code, `${field}.code`);
  const invisible = outcome === "excluded" && code === "not_found_or_inaccessible";
  const rev = value.rev === null && invisible ? null : integer(value.rev, `${field}.rev`);
  const state = value.state === null && invisible ? null : oneOf(value.state, `${field}.state`, STATES);
  if (outcome === "eligible" && (rev === null || state === null)) throw new ContractError(`${field} eligible 元数据不完整`);
  return { resource_type: resource(value.resource_type, `${field}.resource_type`), id: string(value.id, `${field}.id`), rev, state, name: nullableString(value.name, `${field}.name`), parent_id: nullableString(value.parent_id, `${field}.parent_id`), folder_id: nullableString(value.folder_id, `${field}.folder_id`), outcome, code };
}

export function toAssetSelection(raw: unknown): AssetSelection {
  const value = object(raw, "资产预览");
  if (integer(value.schema_version, "资产预览.schema_version") !== 1) throw new ContractError("未知资产预览版本");
  const counts = object(value.counts, "资产预览.counts");
  const rootValue = value.root === null ? null : object(value.root, "资产预览.root");
  const parsed: AssetSelection = {
    selection_id: string(value.selection_id, "资产预览.selection_id"), schema_version: 1,
    action: action(value.action, "资产预览.action"), mode: oneOf(value.mode, "资产预览.mode", ["explicit", "filter", "folder"] as const),
    workspace_id: string(value.workspace_id, "资产预览.workspace_id"), project_id: string(value.project_id, "资产预览.project_id"), principal_id: string(value.principal_id, "资产预览.principal_id"),
    created_at: string(value.created_at, "资产预览.created_at"), expires_at: string(value.expires_at, "资产预览.expires_at"),
    counts: { selected: integer(counts.selected, "counts.selected"), eligible: integer(counts.eligible, "counts.eligible"), excluded: integer(counts.excluded, "counts.excluded"), cases: integer(counts.cases, "counts.cases"), folders: integer(counts.folders, "counts.folders") },
    root: rootValue === null ? null : { resource_type: oneOf(rootValue.resource_type, "root.resource_type", ["folder"] as const), id: string(rootValue.id, "root.id"), expected_rev: integer(rootValue.expected_rev, "root.expected_rev") },
    preview_items: array(value.preview_items, "preview_items", selectionItem), excluded_items: array(value.excluded_items, "excluded_items", selectionItem),
  };
  const folderAction = parsed.action === "folder_archive" || parsed.action === "folder_restore";
  if ((parsed.mode === "folder") !== folderAction || (parsed.root !== null) !== folderAction) throw new ContractError("资产预览动作与选择模式不匹配");
  if (parsed.counts.selected !== parsed.counts.eligible + parsed.counts.excluded) throw new ContractError("资产预览数量不守恒");
  if ((parsed.mode === "folder" ? parsed.counts.eligible : parsed.counts.selected) > 500) throw new ContractError("资产预览超过单次500项上限");
  if (parsed.preview_items.length !== parsed.counts.eligible || parsed.excluded_items.length !== parsed.counts.excluded) throw new ContractError("资产预览明细数量不匹配");
  const identities = [...parsed.preview_items, ...parsed.excluded_items].map((item) => `${item.resource_type}:${item.id}`);
  if (new Set(identities).size !== identities.length) throw new ContractError("资产预览包含重复对象");
  if (parsed.preview_items.some((item) => item.outcome !== "eligible") || parsed.excluded_items.some((item) => item.outcome !== "excluded")) throw new ContractError("资产预览对象分组错误");
  const allItems = [...parsed.preview_items, ...parsed.excluded_items];
  const actualCases = allItems.filter((item) => item.resource_type === "case").length;
  const actualFolders = allItems.filter((item) => item.resource_type === "folder").length;
  if (parsed.counts.cases !== actualCases || parsed.counts.folders !== actualFolders || parsed.counts.selected !== allItems.length) throw new ContractError("资产预览分类数量不匹配");
  if (parsed.mode !== "folder" && (parsed.counts.selected < 1 || actualFolders !== 0)) throw new ContractError("普通用例预览必须包含至少一个Case");
  return parsed;
}

function metadata(raw: unknown, field: string): AssetMetadata | null {
  if (raw === null) return null;
  const value = object(raw, field);
  return { id: string(value.id, `${field}.id`), resource_type: resource(value.resource_type, `${field}.resource_type`), name: string(value.name, `${field}.name`), rev: integer(value.rev, `${field}.rev`), state: oneOf(value.state, `${field}.state`, STATES), folder_id: nullableString(value.folder_id, `${field}.folder_id`), parent_id: nullableString(value.parent_id, `${field}.parent_id`), archived_at: nullableString(value.archived_at, `${field}.archived_at`) };
}
function operationItem(raw: unknown, field: string): AssetOperationItem {
  const value = object(raw, field);
  return { resource_type: resource(value.resource_type, `${field}.resource_type`), id: string(value.id, `${field}.id`), outcome: oneOf(value.outcome, `${field}.outcome`, ["succeeded", "no_change", "conflict", "not_found_or_inaccessible", "invalid_target"] as const), code: nullableString(value.code, `${field}.code`), message: string(value.message, `${field}.message`), new_rev: value.new_rev === null ? null : integer(value.new_rev, `${field}.new_rev`), asset: metadata(value.asset, `${field}.asset`) };
}
function result(raw: unknown, operationAction: AssetAction): AssetOperationResult {
  const value = object(raw, "资产操作.result");
  if (value.result_kind === "rejected") {
    if (value.no_asset_changes !== true) throw new ContractError("rejected 必须零资产变化");
    const selectionId = nullableString(value.selection_id, "selection_id");
    if ((operationAction === "case_copy") !== (selectionId === null)) throw new ContractError("资产拒绝结果选择来源不匹配");
    return { result_kind: "rejected", selection_id: selectionId, code: string(value.code, "code"), message: string(value.message, "message"), no_asset_changes: true, conflicts: array(value.conflicts, "conflicts", (rawConflict, field) => { const conflict = object(rawConflict, field); return { resource_type: resource(conflict.resource_type, `${field}.resource_type`), id: nullableString(conflict.id, `${field}.id`), code: string(conflict.code, `${field}.code`), message: string(conflict.message, `${field}.message`), current_rev: conflict.current_rev === null ? null : integer(conflict.current_rev, `${field}.current_rev`) }; }) };
  }
  if (value.result_kind !== "completed") throw new ContractError("未知资产结果");
  const rawCounts = object(value.counts, "counts");
  const counts = { input: integer(rawCounts.input, "counts.input"), succeeded: integer(rawCounts.succeeded, "counts.succeeded"), no_change: integer(rawCounts.no_change, "counts.no_change"), conflict: integer(rawCounts.conflict, "counts.conflict"), failed: integer(rawCounts.failed, "counts.failed") };
  if (counts.input !== counts.succeeded + counts.no_change + counts.conflict + counts.failed) throw new ContractError("资产结果数量不守恒");
  const rootValue = value.root === null ? null : object(value.root, "root");
  const parsed: AssetOperationResult = { result_kind: "completed", selection_id: nullableString(value.selection_id, "selection_id"), root: rootValue === null ? null : { resource_type: oneOf(rootValue.resource_type, "root.resource_type", ["folder"] as const), id: string(rootValue.id, "root.id") }, counts, items: array(value.items, "items", operationItem), members: array(value.members, "members", (rawMember, field) => { const member = object(rawMember, field); return { resource_type: resource(member.resource_type, `${field}.resource_type`), id: string(member.id, `${field}.id`), before_rev: integer(member.before_rev, `${field}.before_rev`), after_rev: integer(member.after_rev, `${field}.after_rev`), original_parent_id: nullableString(member.original_parent_id, `${field}.original_parent_id`), before_state: oneOf(member.before_state, `${field}.before_state`, STATES) }; }) };
  if (parsed.items.length !== counts.input) throw new ContractError("资产结果明细数量不匹配");
  const outcomeCounts = parsed.items.reduce((acc, item) => { acc[item.outcome] += 1; if (item.asset !== null && (item.asset.id !== item.id || item.asset.resource_type !== item.resource_type)) throw new ContractError("资产结果元数据身份不匹配"); return acc; }, { succeeded: 0, no_change: 0, conflict: 0, not_found_or_inaccessible: 0, invalid_target: 0 });
  if (outcomeCounts.succeeded !== counts.succeeded || outcomeCounts.no_change !== counts.no_change || outcomeCounts.conflict !== counts.conflict || outcomeCounts.not_found_or_inaccessible + outcomeCounts.invalid_target !== counts.failed) throw new ContractError("资产结果分类数量不匹配");
  const folderAction = operationAction === "folder_archive" || operationAction === "folder_restore";
  if ((parsed.root !== null) !== folderAction || (!folderAction && parsed.members.length > 0)) throw new ContractError("资产结果动作投影不匹配");
  if ((operationAction === "case_copy") !== (parsed.selection_id === null)) throw new ContractError("资产结果选择来源不匹配");
  const itemIds = parsed.items.map((item) => `${item.resource_type}:${item.id}`);
  if (new Set(itemIds).size !== itemIds.length) throw new ContractError("资产结果包含重复对象");
  for (const item of parsed.items) {
    if (item.outcome === "succeeded") {
      if (item.asset === null || item.new_rev === null || item.new_rev !== item.asset.rev) throw new ContractError("成功资产结果修订号不匹配");
    } else if (item.new_rev !== null) throw new ContractError("非变更资产结果不得推进修订号");
  }
  const memberIds = parsed.members.map((item) => `${item.resource_type}:${item.id}`);
  if (new Set(memberIds).size !== memberIds.length) throw new ContractError("资产成员包含重复对象");
  if (folderAction && parsed.members.some((member) => !itemIds.includes(`${member.resource_type}:${member.id}`) || member.after_rev < member.before_rev)) throw new ContractError("资产成员与结果不匹配");
  return parsed;
}
export function toAssetOperation(raw: unknown): AssetOperation {
  const value = object(raw, "资产操作");
  if (integer(value.result_schema_version, "result_schema_version") !== 1) throw new ContractError("未知资产结果版本");
  const parsedAction = action(value.action, "action");
  return { operation_id: string(value.operation_id, "operation_id"), operation_key: string(value.operation_key, "operation_key"), action: parsedAction, workspace_id: string(value.workspace_id, "workspace_id"), project_id: string(value.project_id, "project_id"), principal_id: string(value.principal_id, "principal_id"), result_schema_version: 1, created_at: string(value.created_at, "created_at"), result: result(value.result, parsedAction) };
}
