/**
 * 网络返回的运行时校验。
 *
 * 编译期类型不能证明后端返回合法：后端升级、代理返回 HTML 错误页、Cookie 失效
 * 跳转都会让 `response.json()` 拿到意料之外的结构。这里把 unknown 收窄为契约
 * 类型，收窄失败就抛错，而不是让 undefined 一路渗透到渲染层变成空白页。
 */
import type {
  AssertionType,
  AssetFolder,
  AssetFolderPage,
  CaseDetail,
  CaseLibraryFilters,
  CaseLibraryPage,
  CasePreference,
  CaseSavedView,
  CaseSummary,
  CaseVersion,
  CredentialGrant,
  CredentialProfile,
  CredentialSet,
  CredentialSetSlot,
  CurlPreview,
  DebugPreflight,
  Environment,
  FieldNode,
  FieldTree,
  Folder,
  LocatorStep,
  NameValuePair,
  PreflightIssue,
  Project,
  RequestSpec,
  RequestSpecV2,
  RunContext,
  RunReport,
  RunStep,
  RunSummary,
  RunnerPool,
  ConfigBasis,
  ResolutionLocation,
  ResolutionPreview,
  ResolutionIssue,
  Secret,
  SecretVersion,
  SessionInfo,
  VariablesSet,
  VariableContext,
  VariableSource,
  AvailableVariableSource,
  FrozenResolution,
  EnvironmentServiceConfig,
  ProjectService,
  SelectedTarget,
  ServiceCatalog,
  ServiceMapping,
  TargetRefV2,
  ValueLiteral,
  Workspace,
  WorkspaceMember,
} from "./types";

export class ContractError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ContractError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asString(value: unknown, field: string): string {
  if (typeof value !== "string") throw new ContractError(`${field} 应为文本`);
  return value;
}

function asNullableString(value: unknown, field: string): string | null {
  if (value === null || value === undefined) return null;
  return asString(value, field);
}

function asNumber(value: unknown, field: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new ContractError(`${field} 应为数字`);
  }
  return value;
}

function asInteger(value: unknown, field: string, minimum = 0): number {
  const result = asNumber(value, field);
  if (!Number.isInteger(result) || result < minimum) throw new ContractError(`${field} 应为不小于 ${minimum} 的整数`);
  return result;
}

function asBoolean(value: unknown, field: string): boolean {
  if (typeof value !== "boolean") throw new ContractError(`${field} 应为布尔值`);
  return value;
}

function asArray(value: unknown, field: string): unknown[] {
  if (!Array.isArray(value)) throw new ContractError(`${field} 应为数组`);
  return value;
}

function asRecord(value: unknown, field: string): Record<string, unknown> {
  if (!isRecord(value)) throw new ContractError(`${field} 应为对象`);
  return value;
}

function mapList<T>(value: unknown, field: string, item: (raw: unknown, index: number) => T): T[] {
  return asArray(value, field).map(item);
}

function toValueLiteral(raw: unknown, field: string): ValueLiteral {
  const record = asRecord(raw, field);
  const type = asString(record.type, `${field}.type`);
  if (type === "number" || type === "string" || type === "json") {
    return { type, text: asString(record.text, `${field}.text`) };
  }
  if (type === "boolean") return { type, value: asBoolean(record.value, `${field}.value`) };
  if (type === "null") return { type };
  throw new ContractError(`${field}.type 不是支持的普通变量类型`);
}

function toConfigBasis(raw: unknown, field: string): ConfigBasis {
  const record = asRecord(raw, field);
  return {
    project_variables_version: asInteger(record.project_variables_version, `${field}.project_variables_version`),
    project_config_version_id: asNullableString(record.project_config_version_id, `${field}.project_config_version_id`),
    environment_rev: asInteger(record.environment_rev, `${field}.environment_rev`, 1),
    environment_config_version: asInteger(record.environment_config_version, `${field}.environment_config_version`, 1),
    environment_config_version_id: asString(record.environment_config_version_id, `${field}.environment_config_version_id`),
  };
}

function toResolutionScope(raw: unknown, field: string) {
  const record = asRecord(raw, field);
  return {
    workspace_id: asString(record.workspace_id, `${field}.workspace_id`),
    project_id: asString(record.project_id, `${field}.project_id`),
    environment_id: asString(record.environment_id, `${field}.environment_id`),
  };
}

function toVariableSource(raw: unknown, field: string): VariableSource {
  const record = asRecord(raw, field);
  const level = asString(record.level, `${field}.level`);
  if (level !== "project" && level !== "environment") throw new ContractError(`${field}.level 不是支持的变量来源`);
  const base = {
    level: level as VariableSource["level"],
    resource_id: asString(record.resource_id, `${field}.resource_id`),
    revision: asInteger(record.revision, `${field}.revision`),
  };
  if (record.unavailable_reason === null) {
    return { ...base, value: toValueLiteral(record.value, `${field}.value`), unavailable_reason: null };
  }
  const unavailableReason = asString(record.unavailable_reason, `${field}.unavailable_reason`);
  if (record.value !== null) throw new ContractError(`${field}.value 在来源不可用时应为 null`);
  return { ...base, value: null, unavailable_reason: unavailableReason };
}

function toAvailableVariableSource(raw: unknown, field: string): AvailableVariableSource {
  const source = toVariableSource(raw, field);
  if (source.unavailable_reason !== null) throw new ContractError(`${field} 必须是可用普通变量来源`);
  return source;
}

function toUtf16Span(raw: unknown, field: string): { start: number; end: number } | undefined {
  if (raw === undefined || raw === null) return undefined;
  const record = asRecord(raw, field);
  const start = asInteger(record.start, `${field}.start`);
  const end = asInteger(record.end, `${field}.end`);
  if (end < start) throw new ContractError(`${field}.end 不能小于 start`);
  return { start, end };
}

function toResolutionLocation(raw: unknown, field: string): ResolutionLocation {
  const record = asRecord(raw, field);
  const kind = asString(record.kind, `${field}.kind`);
  const span = toUtf16Span(record.utf16_span, `${field}.utf16_span`);
  if (kind === "path") {
    if (asString(record.field, `${field}.field`) !== "path") throw new ContractError(`${field}.field 应为 path`);
    return { kind, field: "path", ...(span === undefined ? {} : { utf16_span: span }) };
  }
  if (kind === "body") {
    if (asString(record.field, `${field}.field`) !== "body") throw new ContractError(`${field}.field 应为 body`);
    return { kind, field: "body", selector: mapList(record.selector, `${field}.selector`, itemGuard(`${field}.selector`, toLocatorStep)), ...(span === undefined ? {} : { utf16_span: span }) };
  }
  if (kind === "query" || kind === "header") {
    if (asString(record.field, `${field}.field`) !== "value") throw new ContractError(`${field}.field 应为 value`);
    if (record.row_id !== undefined && record.row_id !== null) {
      const rowId = asString(record.row_id, `${field}.row_id`);
      if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(rowId)) {
        throw new ContractError(`${field}.row_id 应为 UUID`);
      }
      if (record.index != null || record.occurrence != null || record.input_fingerprint != null) {
        throw new ContractError(`${field} 的 v2 行不能混入 v1 索引字段`);
      }
      return { kind, field: "value", row_id: rowId, ...(span === undefined ? {} : { utf16_span: span }) };
    }
    if (record.index == null || record.occurrence == null || record.input_fingerprint == null) {
      throw new ContractError(`${field} 的 v1 行缺少 index、occurrence 或 input_fingerprint`);
    }
    return {
      kind,
      field: "value",
      index: asInteger(record.index, `${field}.index`),
      occurrence: asInteger(record.occurrence, `${field}.occurrence`),
      input_fingerprint: asString(record.input_fingerprint, `${field}.input_fingerprint`),
      ...(span === undefined ? {} : { utf16_span: span }),
    };
  }
  throw new ContractError(`${field}.kind 不是支持的请求位置`);
}

const PREFLIGHT_ACTIONS = new Set([
  "edit_request", "select_environment", "manage_credentials", "authorize", "contact_admin",
  "configure_environment", "restore_case", "organize_case",
]);

function toPreflightAction(raw: unknown, field: string): PreflightIssue["action"] {
  const action = asString(raw, field);
  if (!PREFLIGHT_ACTIONS.has(action)) throw new ContractError(`未知预检动作：${action}`);
  return action as PreflightIssue["action"];
}

function toResolutionIssue(raw: unknown, field: string): ResolutionIssue {
  const record = asRecord(raw, field);
  return {
    issue_id: asString(record.issue_id, `${field}.issue_id`),
    code: asString(record.code, `${field}.code`),
    message: asString(record.message, `${field}.message`),
    action: toPreflightAction(record.action, `${field}.action`),
    location: record.location === null ? null : toResolutionLocation(record.location, `${field}.location`),
  };
}

function toResolutionBinding(raw: unknown, field: string): ResolutionPreview["bindings"][number] {
  const binding = asRecord(raw, field);
  const valueType = asString(binding.value_type, `${field}.value_type`);
  if (!["number", "string", "boolean", "null", "json"].includes(valueType)) {
    throw new ContractError(`${field}.value_type 未知`);
  }
  return {
    binding_id: asString(binding.binding_id, `${field}.binding_id`),
    reference: asString(binding.reference, `${field}.reference`),
    name: asString(binding.name, `${field}.name`),
    location: toResolutionLocation(binding.location, `${field}.location`),
    source: toAvailableVariableSource(binding.source, `${field}.source`),
    overridden_sources: mapList(binding.overridden_sources, `${field}.overridden_sources`, (source, sourceIndex) =>
      toVariableSource(source, `${field}.overridden_sources[${sourceIndex}]`)),
    value_type: valueType as ResolutionPreview["bindings"][number]["value_type"],
    rendered_preview: asString(binding.rendered_preview, `${field}.rendered_preview`),
  };
}

// —— 各资源的校验器 ——

function itemGuard<T>(field: string, guard: (raw: unknown, field: string) => T) {
  return (raw: unknown, index: number): T => guard(raw, `${field}[${index}]`);
}

function toNameValue(raw: unknown, field: string): NameValuePair {
  const item = asRecord(raw, field);
  return { name: asString(item.name, `${field}.name`), value: asString(item.value, `${field}.value`) };
}

function toRequestRowV2(raw: unknown, field: string): RequestSpecV2["query_params"][number] {
  const item = asRecord(raw, field);
  const rowId = asString(item.row_id, `${field}.row_id`);
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(rowId)) {
    throw new ContractError(`${field}.row_id 应为 UUID`);
  }
  const description = asString(item.description, `${field}.description`);
  if ([...description].length > 1024) throw new ContractError(`${field}.description 最多 1024 个字符`);
  return {
    row_id: rowId,
    name: asString(item.name, `${field}.name`),
    value: asString(item.value, `${field}.value`),
    enabled: asBoolean(item.enabled, `${field}.enabled`),
    description,
  };
}

function toLocatorStep(raw: unknown, field: string): LocatorStep {
  const step = asRecord(raw, field);
  const kind = asString(step.kind, `${field}.kind`);
  if (kind === "key") return { kind: "key", key: asString(step.key, `${field}.key`) };
  if (kind === "index") return { kind: "index", index: asNumber(step.index, `${field}.index`) };
  if (kind === "repeat_key") {
    return {
      kind: "repeat_key",
      key: asString(step.key, `${field}.key`),
      occurrence: asNumber(step.occurrence, `${field}.occurrence`),
    };
  }
  if (kind === "row") return { kind: "row", row_id: asString(step.row_id, `${field}.row_id`) };
  throw new ContractError(`未知定位步骤：${kind}`);
}

export function toRequestSpec(raw: unknown, field = "request"): RequestSpec {
  const spec = asRecord(raw, field);
  const bodyType = asString(spec.body_type, `${field}.body_type`);
  if (bodyType !== "none" && bodyType !== "json" && bodyType !== "text" && bodyType !== "form") {
    throw new ContractError(`未知正文类型：${bodyType}`);
  }
  const normalizedBodyType: RequestSpec["body_type"] = bodyType;
  const schemaVersion = spec.schema_version;
  if (schemaVersion !== undefined && schemaVersion !== 2) {
    throw new ContractError(`${field}.schema_version 仅支持 2`);
  }
  const common = {
    method: asString(spec.method, `${field}.method`),
    path: asString(spec.path, `${field}.path`),
    body_type: normalizedBodyType,
    body: asString(spec.body ?? "", `${field}.body`),
  };
  const hasServiceContract = Object.prototype.hasOwnProperty.call(spec, "service_contract");
  const hasServiceKey = Object.prototype.hasOwnProperty.call(spec, "service_key");
  let serviceTarget: { service_contract: 1; service_key: string } | Record<string, never> = {};
  if (hasServiceContract !== hasServiceKey) throw new ContractError(`${field} 的服务协议与服务标识必须同时存在`);
  if (hasServiceContract) {
    if (asInteger(spec.service_contract, `${field}.service_contract`, 1) !== 1) {
      throw new ContractError(`${field}.service_contract 仅支持 1`);
    }
    const serviceKey = asString(spec.service_key, `${field}.service_key`);
    if (!/^svc_[0-9a-f]{32}$/.test(serviceKey)) throw new ContractError(`${field}.service_key 不是有效的命名服务标识`);
    serviceTarget = { service_contract: 1, service_key: serviceKey };
  }
  let result: RequestSpec;
  if (schemaVersion === undefined) {
    for (const [index, item] of [...asArray(spec.query_params, `${field}.query_params`), ...asArray(spec.headers, `${field}.headers`)].entries()) {
      const row = asRecord(item, `${field}.rows[${index}]`);
      if ("row_id" in row || "enabled" in row || "description" in row) {
        throw new ContractError("旧请求不能携带新版行元数据");
      }
    }
    const queryParams = mapList(spec.query_params, `${field}.query_params`, itemGuard(`${field}.query_params`, toNameValue));
    const headers = mapList(spec.headers, `${field}.headers`, itemGuard(`${field}.headers`, toNameValue));
    result = { ...common, ...serviceTarget, query_params: queryParams, headers };
  } else {
    const queryParams = mapList(spec.query_params, `${field}.query_params`, itemGuard(`${field}.query_params`, toRequestRowV2));
    const headers = mapList(spec.headers, `${field}.headers`, itemGuard(`${field}.headers`, toRequestRowV2));
    if (queryParams.length + headers.length > 500) {
      throw new ContractError("Query 和 Header 合计最多 500 行");
    }
    const ids = new Set<string>();
    for (const row of [...queryParams, ...headers]) {
      if (ids.has(row.row_id)) throw new ContractError(`请求行 ID 重复：${row.row_id}`);
      ids.add(row.row_id);
    }
    result = { ...common, ...serviceTarget, schema_version: 2, query_params: queryParams, headers };
  }
  const origin = asNullableString(spec.imported_origin, `${field}.imported_origin`);
  if (origin !== null) result.imported_origin = origin;
  // auth_required 只在**显式为 true** 时保留：缺省、false、以及后端省略的形态都表示
  // 沿用“跟随环境”。写成 `auth_required: false` 会让服务端算出的摘要与既有版本、
  // 既有授权对不上——那是兼容性破坏，不是更明确的写法。
  if (spec.auth_required === true) result.auth_required = true;
  else if (spec.auth_required !== undefined && spec.auth_required !== null && spec.auth_required !== false) {
    throw new ContractError(`${field}.auth_required 只能是布尔值`);
  }
  return result;
}

function toCaseAssertion(raw: unknown, field: string): CaseDetail["assertions"][number] {
  const item = asRecord(raw, field);
  const compareAs = item.compare_as;
  if (compareAs !== null && compareAs !== undefined && compareAs !== "number" && compareAs !== "integer") {
    throw new ContractError(`${field}.compare_as 只能是 number 或 integer`);
  }
  const severity = asString(item.severity, `${field}.severity`);
  if (severity !== "error" && severity !== "warning") {
    throw new ContractError(`${field}.severity 只能 error 或 warning`);
  }
  return {
    id: asString(item.id, `${field}.id`),
    target_source: asString(item.target_source, `${field}.target_source`) as CaseDetail["assertions"][number]["target_source"],
    selector: mapList(item.selector, `${field}.selector`, itemGuard(`${field}.selector`, toLocatorStep)),
    type: asString(item.type, `${field}.type`),
    parameters: asRecord(item.parameters, `${field}.parameters`),
    compare_as: compareAs ?? null,
    severity,
    enabled: asBoolean(item.enabled, `${field}.enabled`),
    sort_order: asNumber(item.sort_order, `${field}.sort_order`),
  };
}

export function toSession(raw: unknown): SessionInfo {
  const data = asRecord(raw, "会话");
  const user = asRecord(data.user, "会话.user");
  return {
    user: {
      user_id: asString(user.user_id, "会话.user.user_id"),
      username: asString(user.username, "会话.user.username"),
      display_name: asString(user.display_name, "会话.user.display_name"),
      is_admin: asBoolean(user.is_admin, "会话.user.is_admin"),
    },
    workspaces: mapList(data.workspaces, "会话.workspaces", (item, index): Workspace => {
      const record = asRecord(item, `会话.workspaces[${index}]`);
      return {
        id: asString(record.id, "工作空间.id"),
        name: asString(record.name, "工作空间.name"),
        role: asString(record.role, "工作空间.role"),
      };
    }),
  };
}

export function toProjectList(raw: unknown): Project[] {
  return mapList(raw, "项目列表", (item, index): Project => {
    const record = asRecord(item, `项目[${index}]`);
    return {
      id: asString(record.id, "项目.id"),
      workspace_id: asString(record.workspace_id, "项目.workspace_id"),
      key: asString(record.key, "项目.key"),
      name: asString(record.name, "项目.name"),
      status: asString(record.status, "项目.status"),
      role: asString(record.role, "项目.role"),
      pool_id: asNullableString(record.pool_id, "项目.pool_id"),
    };
  });
}

function toEnvironmentItem(raw: unknown, field: string): Environment {
  const record = asRecord(raw, field);
  return {
    id: asString(record.id, `${field}.id`),
    name: asString(record.name, `${field}.name`),
    kind: asString(record.kind, `${field}.kind`),
    base_url: asString(record.base_url, `${field}.base_url`),
    pool_id: asNullableString(record.pool_id, `${field}.pool_id`),
    variables: asRecord(record.variables, `${field}.variables`),
    status: asString(record.status, `${field}.status`),
    rev: asInteger(record.rev, `${field}.rev`, 1),
    config_version: record.config_version === null ? null : asInteger(record.config_version, `${field}.config_version`, 1),
  };
}

export function toEnvironment(raw: unknown): Environment {
  return toEnvironmentItem(raw, "环境");
}

export function toEnvironmentList(raw: unknown): Environment[] {
  return mapList(raw, "环境列表", (item, index) => toEnvironmentItem(item, `环境[${index}]`));
}

const SERVICE_AVAILABILITIES = new Set(["ready", "environment_archived", "service_archived", "mapping_disabled", "mapping_missing", "config_inconsistent"]);

function toServiceMapping(raw: unknown, field: string): ServiceMapping {
  const record = asRecord(raw, field);
  const status = asEnum<ServiceMapping["status"]>(record.status, `${field}.status`, new Set(["active", "disabled", "archived"]));
  return {
    id: asString(record.id, `${field}.id`),
    rev: asInteger(record.rev, `${field}.rev`, 1),
    status,
    base_url: asString(record.base_url, `${field}.base_url`),
    version: asInteger(record.version, `${field}.version`, 1),
    version_id: asString(record.version_id, `${field}.version_id`),
  };
}

function toSelectedTarget(raw: unknown, field: string): SelectedTarget {
  const record = asRecord(raw, field);
  const kind = asEnum<SelectedTarget["kind"]>(record.kind, `${field}.kind`, new Set(["default", "service"]));
  const serviceKey = asString(record.service_key, `${field}.service_key`);
  const serviceStatus = asEnum<SelectedTarget["service_status"]>(record.service_status, `${field}.service_status`, new Set(["active", "archived"]));
  const availability = asEnum<SelectedTarget["availability"]>(record.availability, `${field}.availability`, SERVICE_AVAILABILITIES);
  if (kind === "default" && (serviceKey !== "default" || serviceStatus !== "active")) throw new ContractError(`${field} 默认服务形状不一致`);
  if (kind === "service" && !/^svc_[0-9a-f]{32}$/.test(serviceKey)) throw new ContractError(`${field}.service_key 不是命名服务标识`);
  if (kind === "service" && availability === "service_archived" && serviceStatus !== "archived") throw new ContractError(`${field} 服务归档状态不一致`);
  if (kind === "service" && (availability === "ready" || availability === "mapping_disabled") && serviceStatus !== "active") {
    throw new ContractError(`${field} 活动服务状态不一致`);
  }
  const mapping = record.mapping === null ? null : toServiceMapping(record.mapping, `${field}.mapping`);
  const requiresMapping = ["ready", "environment_archived", "service_archived", "mapping_disabled"].includes(availability);
  if (requiresMapping !== (mapping !== null)) throw new ContractError(`${field}.mapping 与 availability 不一致`);
  if (kind === "default" && ["service_archived", "mapping_disabled", "mapping_missing"].includes(availability)) throw new ContractError(`${field} 默认服务可用状态无效`);
  if (kind === "default" && mapping !== null && mapping.status !== "active" && mapping.status !== "archived") throw new ContractError(`${field} 默认映射状态无效`);
  if (kind === "service" && mapping !== null && mapping.status === "archived") throw new ContractError(`${field} 命名服务映射状态无效`);
  if (kind === "default" && mapping !== null) {
    if (availability === "ready" && mapping.status !== "active") throw new ContractError(`${field} 默认映射可用状态不一致`);
    if (availability === "environment_archived" && mapping.status !== "archived") throw new ContractError(`${field} 默认映射停用状态不一致`);
  }
  if (kind === "service" && mapping !== null) {
    if (availability === "mapping_disabled" && mapping.status !== "disabled") throw new ContractError(`${field} 命名映射暂停状态不一致`);
    if (availability === "ready" && mapping.status !== "active") throw new ContractError(`${field} 命名映射启用状态不一致`);
    // 环境/服务归档优先于映射暂停：归档发生前已经disabled的映射仍是合法历史状态。
    if ((availability === "environment_archived" || availability === "service_archived")
      && mapping.status !== "active" && mapping.status !== "disabled") {
      throw new ContractError(`${field} 归档服务的命名映射状态无效`);
    }
  }
  return {
    kind,
    service_id: asString(record.service_id, `${field}.service_id`),
    service_key: serviceKey,
    service_name: asString(record.service_name, `${field}.service_name`),
    service_rev: asInteger(record.service_rev, `${field}.service_rev`, 1),
    service_status: serviceStatus,
    availability,
    mapping,
  };
}

function toTargetRefV2(raw: unknown, field: string): TargetRefV2 {
  const record = asRecord(raw, field);
  const kind = asEnum<TargetRefV2["kind"]>(record.kind, `${field}.kind`, new Set(["default", "service"]));
  const serviceKey = asString(record.service_key, `${field}.service_key`);
  if ((kind === "default" && serviceKey !== "default") || (kind === "service" && !/^svc_[0-9a-f]{32}$/.test(serviceKey))) {
    throw new ContractError(`${field} 目标类型与服务标识不一致`);
  }
  return {
    kind,
    service_id: asString(record.service_id, `${field}.service_id`),
    service_key: serviceKey,
    service_rev: asInteger(record.service_rev, `${field}.service_rev`, 1),
    mapping_id: asString(record.mapping_id, `${field}.mapping_id`),
    mapping_rev: asInteger(record.mapping_rev, `${field}.mapping_rev`, 1),
    mapping_version_id: asString(record.mapping_version_id, `${field}.mapping_version_id`),
    mapping_version: asInteger(record.mapping_version, `${field}.mapping_version`, 1),
    environment_id: asString(record.environment_id, `${field}.environment_id`),
  };
}

function toProjectService(raw: unknown, field: string): ProjectService {
  const record = asRecord(raw, field);
  const isDefault = asBoolean(record.is_default, `${field}.is_default`);
  const serviceKey = asString(record.service_key, `${field}.service_key`);
  const status = asEnum<ProjectService["status"]>(record.status, `${field}.status`, new Set(["active", "archived"]));
  if (isDefault ? serviceKey !== "default" || status !== "active" : !/^svc_[0-9a-f]{32}$/.test(serviceKey)) {
    throw new ContractError(`${field} 服务类型与稳定标识不一致`);
  }
  return {
    id: asString(record.id, `${field}.id`), service_key: serviceKey,
    name: asString(record.name, `${field}.name`), is_default: isDefault, status,
    rev: asInteger(record.rev, `${field}.rev`, 1),
    created_at: asString(record.created_at, `${field}.created_at`), updated_at: asString(record.updated_at, `${field}.updated_at`),
  };
}

export function toServiceCatalog(raw: unknown): ServiceCatalog {
  const record = asRecord(raw, "服务目录");
  if (asInteger(record.schema_version, "服务目录.schema_version", 1) !== 1) throw new ContractError("服务目录.schema_version 不受支持");
  const items = mapList(record.items, "服务目录.items", (item, index) => toProjectService(item, `服务目录.items[${index}]`));
  if (items.filter((item) => item.is_default).length !== 1) throw new ContractError("服务目录必须且只能包含一个默认服务");
  if (new Set(items.map((item) => item.id)).size !== items.length || new Set(items.map((item) => item.service_key)).size !== items.length) {
    throw new ContractError("服务目录包含重复资源或稳定标识");
  }
  return { schema_version: 1, items };
}

export function toProjectServiceResponse(raw: unknown): ProjectService {
  return toProjectService(raw, "服务");
}

export function toEnvironmentServiceConfig(raw: unknown): EnvironmentServiceConfig {
  const record = asRecord(raw, "环境服务配置");
  if (asInteger(record.schema_version, "环境服务配置.schema_version", 1) !== 1) throw new ContractError("环境服务配置.schema_version 不受支持");
  const environment = asRecord(record.environment, "环境服务配置.environment");
  const environmentStatus = asEnum(environment.status, "环境服务配置.environment.status", new Set(["active", "archived"]));
  const inheritance = asRecord(record.inheritance, "环境服务配置.inheritance");
  const identity = asRecord(inheritance.identity, "环境服务配置.inheritance.identity");
  if (asString(identity.mode, "环境服务配置.inheritance.identity.mode") !== "shared_environment") throw new ContractError("环境身份继承模式未知");
  const identityState = asEnum<EnvironmentServiceConfig["inheritance"]["identity"]["state"]>(identity.state, "环境服务配置.inheritance.identity.state", new Set(["unchecked", "none", "ready", "unavailable", "ambiguous"]));
  let decodedIdentity: EnvironmentServiceConfig["inheritance"]["identity"];
  if (identityState === "ready" || identityState === "unavailable") {
    const profile = identity.profile_id;
    decodedIdentity = { mode: "shared_environment", state: identityState, ...(profile === undefined ? {} : { profile_id: asString(profile, "环境服务配置.inheritance.identity.profile_id") }) };
  } else {
    if (identity.profile_id !== undefined) throw new ContractError("当前环境身份状态不能返回 profile_id");
    decodedIdentity = { mode: "shared_environment", state: identityState };
  }
  const pool = asRecord(inheritance.pool, "环境服务配置.inheritance.pool");
  const poolState = asEnum<EnvironmentServiceConfig["inheritance"]["pool"]["state"]>(pool.state, "环境服务配置.inheritance.pool.state", new Set(["ready", "disabled", "not_granted", "missing"]));
  const decodedPool: EnvironmentServiceConfig["inheritance"]["pool"] = poolState === "missing"
    ? { state: "missing" }
    : { state: poolState, id: asString(pool.id, "环境服务配置.inheritance.pool.id"), name: asString(pool.name, "环境服务配置.inheritance.pool.name"), status: asEnum(pool.status, "环境服务配置.inheritance.pool.status", new Set(["active", "disabled"])) };
  if (decodedPool.state === "ready" && decodedPool.status !== "active") throw new ContractError("可用执行池必须为 active");
  if (decodedPool.state === "disabled" && decodedPool.status !== "disabled") throw new ContractError("停用执行池状态不一致");
  if (decodedPool.state === "not_granted" && decodedPool.status !== "active") throw new ContractError("未授权执行池必须为 active");
  const items = mapList(record.items, "环境服务配置.items", (item, index) => {
    const field = `环境服务配置.items[${index}]`;
    const entry = asRecord(item, field);
    const target = toSelectedTarget({
      kind: asBoolean(entry.is_default, `${field}.is_default`) ? "default" : "service",
      service_id: entry.service_id, service_key: entry.service_key, service_name: entry.service_name,
      service_rev: entry.service_rev, service_status: entry.service_status,
      availability: entry.availability, mapping: entry.mapping,
    }, field);
    return { ...target, is_default: target.kind === "default" };
  });
  if (items.filter((item) => item.is_default).length !== 1 || new Set(items.map((item) => item.service_key)).size !== items.length) {
    throw new ContractError("环境服务配置必须包含唯一默认服务且不能重复服务标识");
  }
  const defaultItem = items.find((item) => item.is_default)!;
  if (defaultItem.mapping !== null && defaultItem.mapping.status !== environmentStatus) throw new ContractError("默认服务映射状态必须与环境状态一致");
  return {
    schema_version: 1,
    environment: {
      id: asString(environment.id, "环境服务配置.environment.id"), name: asString(environment.name, "环境服务配置.environment.name"),
      kind: asString(environment.kind, "环境服务配置.environment.kind"), status: environmentStatus,
      rev: asInteger(environment.rev, "环境服务配置.environment.rev", 1),
      config_version: environment.config_version === null ? null : asInteger(environment.config_version, "环境服务配置.environment.config_version", 1),
    },
    inheritance: { identity: decodedIdentity, pool: decodedPool },
    items,
  };
}

export function toVariableContext(raw: unknown): VariableContext {
  const record = asRecord(raw, "变量目录");
  const schemaVersion = asInteger(record.schema_version, "变量目录.schema_version", 1);
  if (schemaVersion !== 1 && schemaVersion !== 2) {
    throw new ContractError("变量目录.schema_version 不受支持");
  }
  return {
    schema_version: schemaVersion,
    scope: toResolutionScope(record.scope, "变量目录.scope"),
    config_basis: toConfigBasis(record.config_basis, "变量目录.config_basis"),
    variables: mapList(record.variables, "变量目录.variables", (item, index) => {
      const field = `变量目录.variables[${index}]`;
      const entry = asRecord(item, field);
      const locations = mapList(entry.available_locations, `${field}.available_locations`, (value, locationIndex) => {
        const location = asString(value, `${field}.available_locations[${locationIndex}]`);
        if (!["path", "query_value", "header_value", "body"].includes(location)) {
          throw new ContractError(`${field}.available_locations[${locationIndex}] 不是支持的插入位置`);
        }
        return location as VariableContext["variables"][number]["available_locations"][number];
      });
      return {
        name: asString(entry.name, `${field}.name`),
        reference: asNullableString(entry.reference, `${field}.reference`),
        value: entry.value === null ? null : toValueLiteral(entry.value, `${field}.value`),
        effective_source: entry.effective_source === null ? null : toAvailableVariableSource(entry.effective_source, `${field}.effective_source`),
        overridden_sources: mapList(entry.overridden_sources, `${field}.overridden_sources`, (source, sourceIndex) =>
          toVariableSource(source, `${field}.overridden_sources[${sourceIndex}]`)),
        available_locations: locations,
        restricted_body_types: mapList(entry.restricted_body_types, `${field}.restricted_body_types`, (value, bodyIndex) => {
          const bodyType = asString(value, `${field}.restricted_body_types[${bodyIndex}]`);
          if (bodyType !== "form") throw new ContractError(`${field}.restricted_body_types[${bodyIndex}] 未知`);
          return bodyType;
        }),
        unavailable_reason: asNullableString(entry.unavailable_reason, `${field}.unavailable_reason`),
      };
    }),
    ...(schemaVersion === 2 ? { selected_target: toSelectedTarget(record.selected_target, "变量目录.selected_target") } : {}),
  };
}

export function toResolutionPreview(raw: unknown): ResolutionPreview {
  const record = asRecord(raw, "解析预览");
  const schemaVersion = asInteger(record.schema_version, "解析预览.schema_version", 1);
  if (schemaVersion !== 1 && schemaVersion !== 2) {
    throw new ContractError("解析预览.schema_version 不受支持");
  }
  const ordinary = asString(record.ordinary_resolution, "解析预览.ordinary_resolution");
  if (ordinary !== "ready" && ordinary !== "invalid") throw new ContractError("解析预览.ordinary_resolution 未知");
  const maskedTarget = record.masked_target === null
    ? null
    : (() => {
        const target = asRecord(record.masked_target, "解析预览.masked_target");
        return {
          url: asString(target.url, "解析预览.masked_target.url"),
          method: asString(target.method, "解析预览.masked_target.method"),
        };
      })();
  const auth = asRecord(record.auth, "解析预览.auth");
  const authStatus = asString(auth.status, "解析预览.auth.status");
  if (!["none", "ready", "needs_authorization", "unavailable", "ambiguous", "unchecked"].includes(authStatus)) {
    throw new ContractError("解析预览.auth.status 未知");
  }
  const common: Omit<ResolutionPreview, "schema_version" | "selected_target" | "target_ref"> = {
    scope: toResolutionScope(record.scope, "解析预览.scope"),
    ready: asBoolean(record.ready, "解析预览.ready"),
    ordinary_resolution: ordinary,
    masked_target: maskedTarget,
    bindings: mapList(record.bindings, "解析预览.bindings", (item, index) => toResolutionBinding(item, `解析预览.bindings[${index}]`)),
    issues: mapList(record.issues, "解析预览.issues", (item, index) =>
      toResolutionIssue(item, `解析预览.issues[${index}]`)),
    auth: {
      required: asBoolean(auth.required, "解析预览.auth.required"),
      status: authStatus as ResolutionPreview["auth"]["status"],
      injection_slots: mapList(auth.injection_slots, "解析预览.auth.injection_slots", (item, index) => {
        const field = `解析预览.auth.injection_slots[${index}]`;
        const slot = asRecord(item, field);
        const kind = asString(slot.kind, `${field}.kind`);
        const status = asString(slot.status, `${field}.status`);
        if (kind !== "header" && kind !== "query") throw new ContractError(`${field}.kind 未知`);
        if (!["available", "conflict", "pending_worker_verification"].includes(status)) throw new ContractError(`${field}.status 未知`);
        return { kind, name: asString(slot.name, `${field}.name`), status: status as ResolutionPreview["auth"]["injection_slots"][number]["status"] };
      }),
      requires_worker_verification: asBoolean(auth.requires_worker_verification, "解析预览.auth.requires_worker_verification"),
    },
    config_basis: toConfigBasis(record.config_basis, "解析预览.config_basis"),
    context_fingerprint: asString(record.context_fingerprint, "解析预览.context_fingerprint"),
    resolution_context: asNullableString(record.resolution_context, "解析预览.resolution_context"),
  };
  if (schemaVersion === 1) return { schema_version: 1, ...common };
  const selectedTarget = toSelectedTarget(record.selected_target, "解析预览.selected_target");
  const targetRef = record.target_ref === null ? null : toTargetRefV2(record.target_ref, "解析预览.target_ref");
  if (targetRef !== null && (targetRef.service_id !== selectedTarget.service_id || targetRef.service_key !== selectedTarget.service_key)) {
    throw new ContractError("解析预览.target_ref 与 selected_target 不一致");
  }
  if (targetRef !== null && selectedTarget.availability !== "ready") throw new ContractError("解析预览不可用目标不能形成 target_ref");
  if (targetRef !== null && (
    selectedTarget.mapping === null
    || targetRef.service_rev !== selectedTarget.service_rev
    || targetRef.environment_id !== common.scope.environment_id
    || targetRef.mapping_id !== selectedTarget.mapping.id
    || targetRef.mapping_rev !== selectedTarget.mapping.rev
    || targetRef.mapping_version_id !== selectedTarget.mapping.version_id
    || targetRef.mapping_version !== selectedTarget.mapping.version
  )) throw new ContractError("解析预览.target_ref 与当前映射坐标不一致");
  return { schema_version: 2, ...common, selected_target: selectedTarget, target_ref: targetRef };
}

export function toFolderList(raw: unknown): Folder[] {
  return mapList(raw, "目录列表", (item, index): Folder => {
    const record = asRecord(item, `目录[${index}]`);
    return {
      id: asString(record.id, "目录.id"),
      parent_id: asNullableString(record.parent_id, "目录.parent_id"),
      name: asString(record.name, "目录.name"),
      archived_at: asNullableString(record.archived_at, "目录.archived_at"),
      rev: asNumber(record.rev, "目录.rev"),
      availability: asEnum(record.availability, "目录.availability", new Set(["available", "ancestor_archived", "invalid_parent_chain"])),
    };
  });
}

export function toCaseSummaryList(raw: unknown): CaseSummary[] {
  return mapList(raw, "用例列表", (item, index): CaseSummary => {
    const record = asRecord(item, `用例[${index}]`);
    const latest = record.latest_version;
    return {
      id: asString(record.id, "用例.id"),
      folder_id: asNullableString(record.folder_id, "用例.folder_id"),
      name: asString(record.name, "用例.name"),
      method: asString(record.method, "用例.method"),
      status: asString(record.status, "用例.status"),
      rev: asNumber(record.rev, "用例.rev"),
      latest_version: latest === null || latest === undefined ? null : asNumber(latest, "用例.latest_version"),
    };
  });
}

const CASE_METHODS = new Set(["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]);
const CASE_STATES = new Set(["active", "archived", "all"]);
const CASE_COLLECTIONS = new Set(["all", "favorites", "recent"]);
const CASE_SORTS = new Set(["updated_desc", "name_asc", "name_desc", "method_asc", "method_desc", "recent_desc"]);

function asEnum<T extends string>(value: unknown, field: string, values: ReadonlySet<string>): T {
  const parsed = asString(value, field);
  if (!values.has(parsed)) throw new ContractError(`${field} 取值无效`);
  return parsed as T;
}

export function toCaseLibraryFilters(raw: unknown, field = "筛选视图.filters"): CaseLibraryFilters {
  const record = asRecord(raw, field);
  const allowed = new Set(["schema_version", "q", "method", "state", "folder", "folder_id", "include_descendants", "collection", "sort"]);
  for (const key of Object.keys(record)) if (!allowed.has(key)) throw new ContractError(`${field}.${key} 不受支持`);
  if (record.schema_version !== 1) throw new ContractError(`${field}.schema_version 仅支持 1`);
  const folder = asEnum<CaseLibraryFilters["folder"]>(record.folder, `${field}.folder`, new Set(["all", "unfiled", "exact"]));
  const method = record.method === undefined || record.method === null ? undefined : asEnum<string>(record.method, `${field}.method`, CASE_METHODS);
  const q = record.q === undefined || record.q === null ? undefined : asString(record.q, `${field}.q`);
  if (q !== undefined && [...q].length > 200) throw new ContractError(`${field}.q 最多 200 个字符`);
  const folderId = asNullableString(record.folder_id, `${field}.folder_id`);
  const includeDescendants = asBoolean(record.include_descendants, `${field}.include_descendants`);
  if (folder === "exact" && folderId === null) throw new ContractError(`${field}.folder_id 在精确目录筛选时必填`);
  if (folder !== "exact" && folderId !== null) throw new ContractError(`${field}.folder_id 只用于精确目录筛选`);
  if (folder !== "exact" && includeDescendants) throw new ContractError(`${field}.include_descendants 只用于精确目录筛选`);
  const collection = asEnum<CaseLibraryFilters["collection"]>(record.collection, `${field}.collection`, CASE_COLLECTIONS);
  const sort = asEnum<CaseLibraryFilters["sort"]>(record.sort, `${field}.sort`, CASE_SORTS);
  if (sort === "recent_desc" && collection !== "recent") throw new ContractError(`${field}.sort 与最近打开集合不匹配`);
  return {
    schema_version: 1,
    ...(q === undefined ? {} : { q }),
    ...(method === undefined ? {} : { method }),
    state: asEnum(record.state, `${field}.state`, CASE_STATES),
    folder,
    folder_id: folderId,
    include_descendants: includeDescendants,
    collection,
    sort,
  };
}

export function toCaseLibraryPage(raw: unknown): CaseLibraryPage {
  const record = asRecord(raw, "用例库");
  return {
    items: mapList(record.items, "用例库.items", (item, index) => {
      const row = asRecord(item, `用例库.items[${index}]`);
      const folderId = asNullableString(row.folder_id, "用例.folder_id");
      const folderPath = row.folder_path === null
        ? null
        : mapList(row.folder_path, "用例.folder_path", (part, partIndex) => {
            const segment = asRecord(part, `用例.folder_path[${partIndex}]`);
            return { id: asString(segment.id, "目录路径.id"), name: asString(segment.name, "目录路径.name") };
          });
      if (folderId === null && (folderPath === null || folderPath.length !== 0)) throw new ContractError("未分组用例的 folder_path 应为空数组");
      if (folderId !== null && folderPath !== null && (folderPath.length === 0 || folderPath.at(-1)?.id !== folderId)) {
        throw new ContractError("用例.folder_path 未指向所属目录");
      }
      return {
        id: asString(row.id, "用例.id"),
        name: asString(row.name, "用例.name"),
        method: asEnum(row.method, "用例.method", CASE_METHODS),
        path: asString(row.path, "用例.path"),
        folder_id: folderId,
        folder_path: folderPath,
        asset_status: asEnum(row.asset_status, "用例.asset_status", new Set(["active", "archived"])),
        availability: asEnum(row.availability, "用例.availability", new Set(["available", "case_archived", "folder_unavailable"])),
        draft_rev: asNumber(row.draft_rev, "用例.draft_rev"),
        updated_at: asString(row.updated_at, "用例.updated_at"),
        latest_version: row.latest_version === null ? null : asNumber(row.latest_version, "用例.latest_version"),
        favorite: asBoolean(row.favorite, "用例.favorite"),
        last_opened_at: asNullableString(row.last_opened_at, "用例.last_opened_at"),
      };
    }),
    total: asNumber(record.total, "用例库.total"),
    next_cursor: asNullableString(record.next_cursor, "用例库.next_cursor"),
  };
}

function toAncestorPath(raw: unknown, field: string): AssetFolder["ancestor_path"] {
  return mapList(raw, field, (item, index) => {
    const row = asRecord(item, `${field}[${index}]`);
    return { id: asString(row.id, `${field}[${index}].id`), name: asString(row.name, `${field}[${index}].name`) };
  });
}

export function toAssetFolderPage(raw: unknown): AssetFolderPage {
  const record = asRecord(raw, "资产目录");
  return {
    items: mapList(record.items, "资产目录.items", (item, index) => toAssetFolder(item, `资产目录.items[${index}]`)),
    total: asNumber(record.total, "资产目录.total"), next_cursor: asNullableString(record.next_cursor, "资产目录.next_cursor"),
  };
}

export function toAssetFolder(raw: unknown, field = "资产目录"): AssetFolder {
      const row = asRecord(raw, field);
      const restore = row.restore_mode;
      if (restore !== null && restore !== "batch_root" && restore !== "locate_root" && restore !== "legacy_single" && restore !== "unavailable") {
        throw new ContractError("目录.restore_mode 取值无效");
      }
      return {
        id: asString(row.id, "目录.id"), name: asString(row.name, "目录.name"),
        parent_id: asNullableString(row.parent_id, "目录.parent_id"), rev: asNumber(row.rev, "目录.rev"),
        archived_at: asNullableString(row.archived_at, "目录.archived_at"),
        availability: asEnum(row.availability, "目录.availability", new Set(["available", "archived", "ancestor_archived", "invalid_parent_chain"])),
        has_children: asBoolean(row.has_children, "目录.has_children"),
        archive_operation_id: asNullableString(row.archive_operation_id, "目录.archive_operation_id"),
        archive_root_id: asNullableString(row.archive_root_id, "目录.archive_root_id"),
        restore_mode: restore,
        ancestor_path: toAncestorPath(row.ancestor_path, "目录.ancestor_path"),
      };
}

export function toCasePreference(raw: unknown): CasePreference {
  const record = asRecord(raw, "用例偏好");
  return { case_id: asString(record.case_id, "用例偏好.case_id"), favorite: asBoolean(record.favorite, "用例偏好.favorite"), last_opened_at: asNullableString(record.last_opened_at, "用例偏好.last_opened_at") };
}

export function toCaseSavedView(raw: unknown, field = "筛选视图"): CaseSavedView {
  const record = asRecord(raw, field);
  const name = asString(record.name, `${field}.name`);
  if (name.trim() !== name || name.length < 1 || [...name].length > 100) throw new ContractError(`${field}.name 不是规范名称`);
  return {
    id: asString(record.id, `${field}.id`), name,
    filters: toCaseLibraryFilters(record.filters, `${field}.filters`), rev: asNumber(record.rev, `${field}.rev`),
    created_at: asString(record.created_at, `${field}.created_at`), updated_at: asString(record.updated_at, `${field}.updated_at`),
  };
}

export function toCaseSavedViewList(raw: unknown): CaseSavedView[] {
  return mapList(raw, "筛选视图列表", (item, index) => toCaseSavedView(item, `筛选视图[${index}]`));
}

export function toCaseDetail(raw: unknown): CaseDetail {
  const record = asRecord(raw, "用例");
  const latest = record.latest_version;
  return {
    id: asString(record.id, "用例.id"),
    folder_id: asNullableString(record.folder_id, "用例.folder_id"),
    name: asString(record.name, "用例.name"),
    request: toRequestSpec(record.request),
    assertions: mapList(record.assertions, "用例.assertions", itemGuard("用例.assertions", toCaseAssertion)),
    rev: asNumber(record.rev, "用例.rev"),
    status: asString(record.status, "用例.status"),
    latest_version: latest === null || latest === undefined ? null : asNumber(latest, "用例.latest_version"),
    updated_at: asString(record.updated_at, "用例.updated_at"),
    snapshot_hash: asString(record.snapshot_hash, "用例.snapshot_hash"),
  };
}

export function toCaseVersion(raw: unknown): CaseVersion {
  const record = asRecord(raw, "用例版本");
  return {
    id: asString(record.id, "版本.id"),
    case_id: asString(record.case_id, "版本.case_id"),
    version: asNumber(record.version, "版本.version"),
    schema_version: asNumber(record.schema_version, "版本.schema_version"),
    side_effect: asString(record.side_effect, "版本.side_effect"),
    snapshot_hash: asString(record.snapshot_hash, "版本.snapshot_hash"),
    created_by: asString(record.created_by, "版本.created_by"),
    created_at: asString(record.created_at, "版本.created_at"),
  };
}

export function toCurlPreview(raw: unknown): CurlPreview {
  const record = asRecord(raw, "导入预览");
  const hint = record.auth_hint;
  return {
    draft: toRequestSpec(record.draft, "导入预览.draft"),
    sendable: asBoolean(record.sendable, "导入预览.sendable"),
    warnings: mapList(record.warnings, "导入预览.warnings", (item, index) =>
      asString(item, `导入预览.warnings[${index}]`),
    ),
    unsupported: mapList(record.unsupported, "导入预览.unsupported", (item, index) =>
      asString(item, `导入预览.unsupported[${index}]`),
    ),
    auth_hint: hint === null || hint === undefined ? null : asRecord(hint, "导入预览.auth_hint"),
  };
}

export function toAssertionTypes(raw: unknown): AssertionType[] {
  return mapList(raw, "断言类型目录", (item, index): AssertionType => {
    const record = asRecord(item, `断言类型[${index}]`);
    const schema = asRecord(record.params_schema, "断言类型.params_schema");
    const params: AssertionType["params_schema"] = {};
    for (const [name, field] of Object.entries(schema)) {
      const spec = asRecord(field, `断言类型.params_schema.${name}`);
      const control = asString(spec.control, `参数 ${name} 的控件`);
      if (
        control !== "value" &&
        control !== "value_list" &&
        control !== "number" &&
        control !== "switch" &&
        control !== "select"
      ) {
        throw new ContractError(`参数 ${name} 的控件类型未知：${control}`);
      }
      params[name] = {
        control,
        type: asString(spec.type, `参数 ${name} 的类型`),
        label: spec.label === undefined ? undefined : asString(spec.label, `参数 ${name} 的标签`),
        default: spec.default,
        options:
          spec.options === undefined
            ? undefined
            : mapList(spec.options, `参数 ${name} 的选项`, (option, optionIndex) =>
                asString(option, `参数 ${name} 选项[${optionIndex}]`),
              ),
      };
    }
    return {
      id: asString(record.id, "断言类型.id"),
      label: asString(record.label, "断言类型.label"),
      group: asString(record.group, "断言类型.group"),
      applies_to: mapList(record.applies_to, "断言类型.applies_to", (value, i) =>
        asString(value, `断言类型.applies_to[${i}]`),
      ),
      params_schema: params,
      summary: asString(record.summary, "断言类型.summary"),
      operator_version: asNumber(record.operator_version, "断言类型.operator_version"),
    };
  });
}

function toFieldNode(raw: unknown, field: string): FieldNode {
  const record = asRecord(raw, field);
  return {
    label: asString(record.label, `${field}.label`),
    type: asString(record.type, `${field}.type`),
    text: asString(record.text, `${field}.text`),
    selector: mapList(record.selector, `${field}.selector`, itemGuard(`${field}.selector`, toLocatorStep)),
    children: mapList(record.children ?? [], `${field}.children`, (child, index) =>
      toFieldNode(child, `${field}.children[${index}]`),
    ),
    truncated: asNullableString(record.truncated, `${field}.truncated`),
  };
}

export function toFieldTree(raw: unknown): FieldTree {
  const record = asRecord(raw, "字段树");
  return {
    root: toFieldNode(record.root, "字段树.root"),
    node_count: asNumber(record.node_count, "字段树.node_count"),
  };
}

export function toRunSummary(raw: unknown, field = "运行"): RunSummary {
  const record = asRecord(raw, field);
  return {
    id: asString(record.id, `${field}.id`),
    target_type: asString(record.target_type, `${field}.target_type`),
    case_version_id: asNullableString(record.case_version_id, `${field}.case_version_id`),
    debug_source_case_id: record.debug_source_case_id === undefined ? null : asNullableString(record.debug_source_case_id, `${field}.debug_source_case_id`),
    environment_id: asString(record.environment_id, `${field}.environment_id`),
    state: asString(record.state, `${field}.state`),
    outcome: asNullableString(record.outcome, `${field}.outcome`),
    reason_category: asNullableString(record.reason_category, `${field}.reason_category`),
    pool_id: asNullableString(record.pool_id, `${field}.pool_id`),
    created_at: asString(record.created_at, `${field}.created_at`),
  };
}

export function toRunList(raw: unknown): RunSummary[] {
  return mapList(raw, "运行列表", (item, index) => toRunSummary(item, `运行[${index}]`));
}

function toRunStep(raw: unknown, field: string): RunStep {
  const record = asRecord(raw, field);
  const elapsed = record.elapsed_ms;
  const interpretation = record.interpretation;
  const validInterpretation = isRecord(interpretation)
    && interpretation.outcome === "completed_unchecked"
    && interpretation.reason_code === "legacy_unchecked_mapping";
  return {
    step_key: asString(record.step_key, `${field}.step_key`),
    attempt_no: asNumber(record.attempt_no, `${field}.attempt_no`),
    state: asString(record.state, `${field}.state`),
    outcome: asNullableString(record.outcome, `${field}.outcome`),
    elapsed_ms: elapsed === null || elapsed === undefined ? null : asNumber(elapsed, `${field}.elapsed_ms`),
    error_code: asNullableString(record.error_code, `${field}.error_code`),
    // 解释字段是增量兼容信息：旧 API 缺失、未知版本或损坏对象都只表示“不可采信”，
    // 不能让原本合法的历史报告整体读取失败，更不能猜成通过。
    interpretation: validInterpretation
      ? {
          outcome: "completed_unchecked",
          reason_code: "legacy_unchecked_mapping",
        }
      : null,
  };
}

/** 调试快照摘要；服务端按规范化内容算出，前端不自己算。 */
export function toDebugSnapshotDigest(raw: unknown): string {
  const record = asRecord(raw, "调试摘要");
  return asString(record.hash, "调试摘要.hash");
}

function toFrozenResolution(raw: unknown): FrozenResolution | null {
  if (raw === null || raw === undefined) return null;
  try {
    const record = asRecord(raw, "运行报告.resolution");
    const schemaVersion = asInteger(record.schema_version, "运行报告.resolution.schema_version", 1);
    if (schemaVersion !== 1 && schemaVersion !== 2) return null;
    const common = {
      config_basis: toConfigBasis(record.config_basis, "运行报告.resolution.config_basis"),
      variable_sources: mapList(record.variable_sources, "运行报告.resolution.variable_sources", (item, index) => {
        const field = `运行报告.resolution.variable_sources[${index}]`;
        const source = asRecord(item, field);
        return {
          name: asString(source.name, `${field}.name`),
          source: toAvailableVariableSource(source.source, `${field}.source`),
          overridden_sources: mapList(source.overridden_sources, `${field}.overridden_sources`, (entry, sourceIndex) =>
            toVariableSource(entry, `${field}.overridden_sources[${sourceIndex}]`)),
        };
      }),
      bindings: mapList(record.bindings, "运行报告.resolution.bindings", (item, index) =>
        toResolutionBinding(item, `运行报告.resolution.bindings[${index}]`)),
      context_fingerprint: asString(record.context_fingerprint, "运行报告.resolution.context_fingerprint"),
    };
    return schemaVersion === 1
      ? { schema_version: 1, ...common }
      : { schema_version: 2, ...common, target_ref: toTargetRefV2(record.target_ref, "运行报告.resolution.target_ref") };
  } catch {
    // 新旁路字段损坏或版本未知只表示“冻结来源不可验证”，不能让旧报告基本内容不可读。
    return null;
  }
}

function toRunContext(raw: unknown): RunContext | null {
  // 缺席与 null 同义：后端在“来源无法证明”（旧记录、旧执行器、主密钥已轮换）时不给
  // 结论。缺失不是错误，因此这里不抛异常，只是没有可用来关联的标记。
  if (raw === null || raw === undefined) return null;
  const record = asRecord(raw, "运行报告.context");
  const environment = asRecord(record.environment, "运行报告.context.environment");
  const resolution = record.resolution;
  let decodedResolution: RunContext["resolution"] = null;
  if (resolution !== null && resolution !== undefined) {
    try {
        const proof = asRecord(resolution, "运行报告.context.resolution");
        const schemaVersion = asInteger(proof.schema_version, "运行报告.context.resolution.schema_version", 1);
        const guard = asString(proof.guard, "运行报告.context.resolution.guard");
        const contextFingerprint = asString(proof.context_fingerprint, "运行报告.context.resolution.context_fingerprint");
        const bindingFingerprint = asString(proof.binding_fingerprint, "运行报告.context.resolution.binding_fingerprint");
        if (schemaVersion === 1 && guard === "ordinary_binding_enforced_v1") {
          decodedResolution = { schema_version: 1, guard, context_fingerprint: contextFingerprint, binding_fingerprint: bindingFingerprint };
        } else if (schemaVersion === 2 && guard === "selected_target_binding_enforced_v1") {
          decodedResolution = {
            schema_version: 2, guard, context_fingerprint: contextFingerprint, binding_fingerprint: bindingFingerprint,
            target_fingerprint: asString(proof.target_fingerprint, "运行报告.context.resolution.target_fingerprint"),
          };
        } else {
          throw new ContractError("运行报告.context.resolution 证明版本不受支持");
        }
    } catch {
      decodedResolution = null;
    }
  }
  return {
    snapshot_fingerprint: asString(
      record.snapshot_fingerprint,
      "运行报告.context.snapshot_fingerprint",
    ),
    environment: {
      id: asString(environment.id, "运行报告.context.environment.id"),
      name: asString(environment.name, "运行报告.context.environment.name"),
      kind: asString(environment.kind, "运行报告.context.environment.kind"),
      base_url: asString(environment.base_url, "运行报告.context.environment.base_url"),
    },
    input_fingerprint: asString(record.input_fingerprint, "运行报告.context.input_fingerprint"),
    resolution: decodedResolution,
  };
}

/** 发送前预检结果；`issues` 与建议动作是普通成员唯一能读到的准入原因。 */
export function toDebugPreflight(raw: unknown): DebugPreflight {
  const record = asRecord(raw, "预检结果");
  const auth = asRecord(record.auth, "预检结果.auth");
  const state = asString(auth.state, "预检结果.auth.state");
  if (
    state !== "none" &&
    state !== "ready" &&
    state !== "needs_authorization" &&
    state !== "unavailable" &&
    state !== "ambiguous"
  ) {
    throw new ContractError(`未知认证状态：${state}`);
  }
  return {
    ready: asBoolean(record.ready, "预检结果.ready"),
    issues: mapList(record.issues, "预检结果.issues", (item, index): PreflightIssue => {
      const field = `预检结果.issues[${index}]`;
      const entry = asRecord(item, field);
      return {
        code: asString(entry.code, `${field}.code`),
        message: asString(entry.message, `${field}.message`),
        action: toPreflightAction(entry.action, `${field}.action`),
      };
    }),
    can_authorize: asBoolean(record.can_authorize, "预检结果.can_authorize"),
    auth: {
      required: asBoolean(auth.required, "预检结果.auth.required"),
      state,
      profile_id: asNullableString(auth.profile_id, "预检结果.auth.profile_id"),
    },
    context: toRunContext(record.context),
    resolution: record.resolution === null || record.resolution === undefined
      ? null
      : toResolutionPreview(record.resolution),
  };
}

export function toRunReport(raw: unknown): RunReport {
  const record = asRecord(raw, "运行报告");
  const request = record.request;
  const response = record.response;
  const run = toRunSummary(record.run, "运行报告.run");
  const context = toRunContext(record.context);
  let resolution = toFrozenResolution(record.resolution);
  if (resolution?.schema_version === 2 && (
    resolution.target_ref.environment_id !== run.environment_id
    || (context !== null && context.environment.id !== resolution.target_ref.environment_id)
  )) resolution = null;
  return {
    run,
    steps: mapList(record.steps, "运行报告.steps", (item, index) =>
      toRunStep(item, `运行报告.steps[${index}]`),
    ),
    assertions: mapList(record.assertions, "运行报告.assertions", (item, index) => {
      const field = `运行报告.assertions[${index}]`;
      const entry = asRecord(item, field);
      const elapsed = entry.elapsed_ms;
      return {
        assertion_id: asString(entry.assertion_id, `${field}.assertion_id`),
        type: asString(entry.type, `${field}.type`),
        phase: asString(entry.phase, `${field}.phase`),
        target: asRecord(entry.target, `${field}.target`),
        status: asString(entry.status, `${field}.status`),
        expected: entry.expected,
        actual: entry.actual,
        reason_code: asNullableString(entry.reason_code, `${field}.reason_code`),
        elapsed_ms:
          elapsed === null || elapsed === undefined ? null : asNumber(elapsed, `${field}.elapsed_ms`),
      };
    }),
    request: request === null || request === undefined ? null : asRecord(request, "运行报告.request"),
    response: response === null || response === undefined ? null : asRecord(response, "运行报告.response"),
    context,
    resolution,
  };
}

// —— 管理页校验器 ——


function asStringList(value: unknown, field: string): string[] {
  return mapList(value, field, (item, index) => asString(item, `${field}[${index}]`));
}

export function toVariablesSet(raw: unknown): VariablesSet {
  const record = asRecord(raw, "普通变量");
  return {
    version: asNumber(record.version, "普通变量.version"),
    variables: mapList(record.variables, "普通变量.variables", (item, index): VariablesSet["variables"][number] => {
      const entry = asRecord(item, `普通变量.variables[${index}]`);
      return {
        name: asString(entry.name, `普通变量.variables[${index}].name`),
        value: entry.value,
      };
    }),
  };
}

function toRunnerPoolItem(raw: unknown, field: string): RunnerPool {
  const record = asRecord(raw, field);
  return {
    id: asString(record.id, `${field}.id`),
    name: asString(record.name, `${field}.name`),
    status: asString(record.status, `${field}.status`),
    network_zone: asString(record.network_zone, `${field}.network_zone`),
    allowed_targets: asStringList(record.allowed_targets, `${field}.allowed_targets`),
    grant_id: asString(record.grant_id, `${field}.grant_id`),
    grant_status: asString(record.grant_status, `${field}.grant_status`),
    granted_at: asString(record.granted_at, `${field}.granted_at`),
    environment_ids: asStringList(record.environment_ids, `${field}.environment_ids`),
  };
}

/** 保存白名单的响应是**单个**执行池；按列表解析会把成功的保存报成失败。 */
export function toRunnerPool(raw: unknown): RunnerPool {
  return toRunnerPoolItem(raw, "执行池");
}

export function toRunnerPoolList(raw: unknown): RunnerPool[] {
  return mapList(raw, "执行池列表", (item, index) => toRunnerPoolItem(item, `执行池[${index}]`));
}

function toSecretItem(raw: unknown, field: string): Secret {
  const record = asRecord(raw, field);
  return {
    id: asString(record.id, `${field}.id`),
    name: asString(record.name, `${field}.name`),
    kind: asString(record.kind, `${field}.kind`),
    latest_version: asNumber(record.latest_version, `${field}.latest_version`),
    latest_version_id: asString(record.latest_version_id, `${field}.latest_version_id`),
  };
}

export function toSecret(raw: unknown): Secret {
  return toSecretItem(raw, "秘密");
}

export function toSecretList(raw: unknown): Secret[] {
  return mapList(raw, "秘密列表", (item, index) => toSecretItem(item, `秘密[${index}]`));
}

export function toSecretVersion(raw: unknown): SecretVersion {
  return toSecretVersionItem(raw, "秘密版本");
}

function toCredentialProfileItem(raw: unknown, field: string): CredentialProfile {
  const record = asRecord(raw, field);
  return {
    id: asString(record.id, `${field}.id`),
    environment_id: asString(record.environment_id, `${field}.environment_id`),
    name: asString(record.name, `${field}.name`),
    provider: asString(record.provider, `${field}.provider`),
    status: asString(record.status, `${field}.status`),
    current_epoch: asNumber(record.current_epoch, `${field}.current_epoch`),
    current_set_id: asNullableString(record.current_set_id, `${field}.current_set_id`),
    profile_version_id: asNullableString(record.profile_version_id, `${field}.profile_version_id`),
    allowed_targets: asStringList(record.allowed_targets, `${field}.allowed_targets`),
    allowed_auth_slots: asStringList(record.allowed_auth_slots, `${field}.allowed_auth_slots`),
    slot_count: asNumber(record.slot_count, `${field}.slot_count`),
  };
}

export function toCredentialProfile(raw: unknown): CredentialProfile {
  return toCredentialProfileItem(raw, "身份配置");
}

export function toCredentialProfileList(raw: unknown): CredentialProfile[] {
  return mapList(raw, "身份配置列表", (item, index) =>
    toCredentialProfileItem(item, `身份配置[${index}]`),
  );
}

function toCredentialGrantItem(raw: unknown, field: string): CredentialGrant {
  const record = asRecord(raw, field);
  return {
    id: asString(record.id, `${field}.id`),
    profile_id: asString(record.profile_id, `${field}.profile_id`),
    environment_id: asString(record.environment_id, `${field}.environment_id`),
    grant_type: asString(record.grant_type, `${field}.grant_type`),
    principal_id: asString(record.principal_id, `${field}.principal_id`),
    case_version_id: asNullableString(record.case_version_id, `${field}.case_version_id`),
    debug_snapshot_hash: asNullableString(record.debug_snapshot_hash, `${field}.debug_snapshot_hash`),
    allowed_targets: asStringList(record.allowed_targets, `${field}.allowed_targets`),
    allowed_auth_slots: asStringList(record.allowed_auth_slots, `${field}.allowed_auth_slots`),
    status: asString(record.status, `${field}.status`),
    expires_at: asNullableString(record.expires_at, `${field}.expires_at`),
    used_at: asNullableString(record.used_at, `${field}.used_at`),
  };
}

export function toCredentialGrant(raw: unknown): CredentialGrant {
  return toCredentialGrantItem(raw, "用途授权");
}

export function toCredentialGrantList(raw: unknown): CredentialGrant[] {
  return mapList(raw, "用途授权列表", (item, index) => toCredentialGrantItem(item, `用途授权[${index}]`));
}

/** 秘密版本列表（轮换历史）：只有版本号与版本 id，响应里没有值。 */
export function toSecretVersionList(raw: unknown): SecretVersion[] {
  return mapList(raw, "秘密版本列表", (item, index) => toSecretVersionItem(item, `秘密版本[${index}]`));
}

function toSecretVersionItem(raw: unknown, field: string): SecretVersion {
  const record = asRecord(raw, field);
  return {
    secret_id: asString(record.secret_id, `${field}.secret_id`),
    version_id: asString(record.version_id, `${field}.version_id`),
    version: asNumber(record.version, `${field}.version`),
  };
}

function toWorkspaceMemberItem(raw: unknown, field: string): WorkspaceMember {
  const record = asRecord(raw, field);
  return {
    user_id: asString(record.user_id, `${field}.user_id`),
    username: asString(record.username, `${field}.username`),
    display_name: asString(record.display_name, `${field}.display_name`),
    role: asString(record.role, `${field}.role`),
  };
}

export function toWorkspaceMemberList(raw: unknown): WorkspaceMember[] {
  return mapList(raw, "成员列表", (item, index) => toWorkspaceMemberItem(item, `成员[${index}]`));
}

function toCredentialSetSlot(raw: unknown, field: string): CredentialSetSlot {
  const record = asRecord(raw, field);
  return {
    auth_slot: asString(record.auth_slot, `${field}.auth_slot`),
    secret_id: asString(record.secret_id, `${field}.secret_id`),
    secret_name: asString(record.secret_name, `${field}.secret_name`),
    secret_version_id: asString(record.secret_version_id, `${field}.secret_version_id`),
    secret_version: asNumber(record.secret_version, `${field}.secret_version`),
  };
}

export function toCredentialSet(raw: unknown): CredentialSet {
  const record = asRecord(raw, "凭证集合");
  return {
    profile_id: asString(record.profile_id, "凭证集合.profile_id"),
    set_id: asNullableString(record.set_id, "凭证集合.set_id"),
    epoch: asNumber(record.epoch, "凭证集合.epoch"),
    status: asNullableString(record.status, "凭证集合.status"),
    expires_at: asNullableString(record.expires_at, "凭证集合.expires_at"),
    slots: mapList(record.slots, "凭证集合.slots", (item, index) =>
      toCredentialSetSlot(item, `凭证集合.slots[${index}]`),
    ),
  };
}
