/**
 * 与后端同源的接口契约类型。
 *
 * 这些类型只描述结构，不能证明网络返回合法：所有外部输入仍需在边界做运行时
 * 校验（见 guards.ts）。数值一律用十进制文本承载，避免 JavaScript Number 在
 * 9007199254740993 这类长整数上失真。
 */

export type LiteralType = "number" | "string" | "boolean" | "null" | "json";

/** 数值与字符串都以 text 承载，不经过 Number。 */
export type ValueLiteral =
  | { type: "number"; text: string }
  | { type: "string"; text: string }
  | { type: "json"; text: string }
  | { type: "boolean"; value: boolean }
  | { type: "null" };

export type LocatorStep =
  | { kind: "key"; key: string }
  | { kind: "index"; index: number }
  | { kind: "repeat_key"; key: string; occurrence: number };

export type TargetSource =
  | "request.path"
  | "request.query"
  | "request.header"
  | "request.body"
  | "request.text"
  | "response.status"
  | "response.elapsed"
  | "response.header"
  | "response.cookie"
  | "response.body"
  | "response.text";

export type AssertionSeverity = "error" | "warning";

export interface CaseAssertion {
  id: string;
  target_source: TargetSource;
  selector: LocatorStep[];
  type: string;
  parameters: Record<string, unknown>;
  compare_as: "number" | "integer" | null;
  severity: AssertionSeverity;
  enabled: boolean;
  sort_order: number;
}

export interface Principal {
  user_id: string;
  username: string;
  display_name: string;
  is_admin: boolean;
}

export interface Workspace {
  id: string;
  name: string;
  role: string;
}

export interface SessionInfo {
  user: Principal;
  workspaces: Workspace[];
}

export interface Project {
  id: string;
  workspace_id: string;
  key: string;
  name: string;
  status: string;
  role: string;
  pool_id: string | null;
}

export interface Environment {
  id: string;
  name: string;
  kind: string;
  base_url: string;
  pool_id: string | null;
  variables: Record<string, unknown>;
  status: string;
}

export interface Folder {
  id: string;
  parent_id: string | null;
  name: string;
  archived_at: string | null;
}

/** 请求定义：目标地址由环境决定，用例不携带绝对地址。 */
export interface RequestSpec {
  method: string;
  path: string;
  query_params: NameValuePair[];
  headers: NameValuePair[];
  body_type: "none" | "json" | "text" | "form";
  body: string;
  imported_origin?: string;
}

export interface NameValuePair {
  name: string;
  value: string;
}

export interface CaseDetail {
  id: string;
  folder_id: string | null;
  name: string;
  request: RequestSpec;
  assertions: CaseAssertion[];
  rev: number;
  status: string;
  latest_version: number | null;
  updated_at: string;
  /** 当前草稿内容的快照摘要；与已发布版本的摘要相同时，那次运行才代表这份内容。 */
  snapshot_hash: string;
}

export interface CaseSummary {
  id: string;
  folder_id: string | null;
  name: string;
  method: string;
  status: string;
  rev: number;
  latest_version: number | null;
}

export interface CaseVersion {
  id: string;
  case_id: string;
  version: number;
  schema_version: number;
  side_effect: string;
  snapshot_hash: string;
  created_by: string;
  created_at: string;
}

export interface CurlPreview {
  draft: RequestSpec;
  sendable: boolean;
  warnings: string[];
  unsupported: string[];
  auth_hint: Record<string, unknown> | null;
}

export interface ParamField {
  control: "value" | "value_list" | "number" | "switch" | "select";
  type: string;
  label?: string;
  default?: unknown;
  options?: string[];
}

export interface AssertionType {
  id: string;
  label: string;
  group: string;
  applies_to: string[];
  params_schema: Record<string, ParamField>;
  summary: string;
  operator_version: number;
}

export interface AssertionPreview {
  status: string;
  reason_code: string | null;
  message: string;
  expected: unknown;
  actual: unknown;
}

export interface FieldNode {
  label: string;
  type: string;
  text: string;
  selector: LocatorStep[];
  children: FieldNode[];
  truncated: string | null;
}

export interface FieldTree {
  root: FieldNode;
  node_count: number;
}

export interface RunSummary {
  id: string;
  target_type: string;
  case_version_id: string | null;
  environment_id: string;
  state: string;
  outcome: string | null;
  reason_category: string | null;
  pool_id: string | null;
  created_at: string;
}

export interface RunStep {
  step_key: string;
  attempt_no: number;
  state: string;
  outcome: string | null;
  elapsed_ms: number | null;
  error_code: string | null;
}

export interface AssertionResult {
  assertion_id: string;
  type: string;
  phase: string;
  target: Record<string, unknown>;
  status: string;
  expected: unknown;
  actual: unknown;
  reason_code: string | null;
  elapsed_ms: number | null;
}

export interface RunReport {
  run: RunSummary;
  steps: RunStep[];
  assertions: AssertionResult[];
  request: Record<string, unknown> | null;
  response: Record<string, unknown> | null;
}

// —— 管理页契约：普通变量、执行池白名单与人工凭证 ——
//
// 这里只承载“看得见的管理状态”：秘密明文与密文都不出现在任何响应里，
// 页面能拿到的最敏感信息是名称与版本号。


/** 普通变量：名称 + 字面量值；秘密不能从这条入口进来。 */
export interface VariableItem {
  name: string;
  value: unknown;
}

export interface VariablesSet {
  version: number;
  variables: VariableItem[];
}

/** 当前项目可用的执行池；allowed_targets 是出网目标白名单，不是秘密。 */
export interface RunnerPool {
  id: string;
  name: string;
  status: string;
  network_zone: string;
  allowed_targets: string[];
  grant_id: string;
  grant_status: string;
  granted_at: string;
  environment_ids: string[];
}

export interface Secret {
  id: string;
  name: string;
  kind: string;
  latest_version: number;
  latest_version_id: string;
}

export interface SecretVersion {
  secret_id: string;
  version_id: string;
  version: number;
}

export interface CredentialProfile {
  id: string;
  environment_id: string;
  name: string;
  provider: string;
  status: string;
  current_epoch: number;
  current_set_id: string | null;
  profile_version_id: string | null;
  allowed_targets: string[];
  allowed_auth_slots: string[];
  slot_count: number;
}

export interface CredentialGrant {
  id: string;
  profile_id: string;
  environment_id: string;
  grant_type: string;
  principal_id: string;
  case_version_id: string | null;
  debug_snapshot_hash: string | null;
  allowed_targets: string[];
  allowed_auth_slots: string[];
  status: string;
  expires_at: string | null;
  used_at: string | null;
}

/** 工作空间成员的可选元数据；用于按姓名选主体，不含任何凭据。 */
export interface WorkspaceMember {
  user_id: string;
  username: string;
  display_name: string;
  role: string;
}

/** 当前凭证集合中的一个槽位绑定；只有位置与秘密名称／版本号，没有秘密值。 */
export interface CredentialSetSlot {
  auth_slot: string;
  secret_id: string;
  secret_name: string;
  secret_version_id: string;
  secret_version: number;
}

/** 当前凭证集合的绑定元数据；整份集合一起提交，因此需要能整份读出来。 */
export interface CredentialSet {
  profile_id: string;
  set_id: string | null;
  epoch: number;
  status: string | null;
  expires_at: string | null;
  slots: CredentialSetSlot[];
}
