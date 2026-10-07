/**
 * 请求草稿的本地形态与提交形态。
 *
 * 界面需要“空行”表示尚未填写的参数，而后端校验要求名称非空；因此本地用原始行
 * 保存，提交前统一丢弃空行。名称的前后空白在提交时去掉，值保持原样，避免悄悄
 * 改变用户填写的查询参数。
 */
import { toRequestSpec } from "../api/guards";
import type { CaseAssertion, RequestRowV2, RequestSpec } from "../api/types";

export interface RawKeyValue {
  name: string;
  value: string;
  row_id?: string;
  enabled?: boolean;
  description?: string;
}

export interface RawRequest {
  schema_version?: 2;
  method: string;
  path: string;
  query_params: RawKeyValue[];
  headers: RawKeyValue[];
  body_type: RequestSpec["body_type"];
  body: string;
  imported_origin?: string;
  /** 必须使用当前环境登录态；见 RequestSpec.auth_required。缺省即“跟随环境”。 */
  auth_required?: boolean;
  service_contract?: 1;
  service_key?: string;
}

export function ltrimPath(value: string): string {
  return value.startsWith("/") ? value : `/${value}`;
}

export function emptyRequest(): RawRequest {
  return {
    method: "GET",
    path: "/",
    query_params: [],
    headers: [],
    body_type: "none",
    body: "",
  };
}

export function requestToRaw(spec: RequestSpec): RawRequest {
  const raw: RawRequest = {
    ...(spec.schema_version === 2 ? { schema_version: 2 as const } : {}),
    method: spec.method,
    path: spec.path,
    query_params: spec.query_params.map((item) => ({ ...item })),
    headers: spec.headers.map((item) => ({ ...item })),
    body_type: spec.body_type,
    body: spec.body,
  };
  if (spec.imported_origin) raw.imported_origin = spec.imported_origin;
  // 只在为 true 时带上：写一个 false 会让服务端摘要与既有版本、既有授权都不同。
  if (spec.auth_required === true) raw.auth_required = true;
  if (spec.service_contract === 1 && spec.service_key) {
    raw.service_contract = 1;
    raw.service_key = spec.service_key;
  }
  return raw;
}

/** cURL 不携带项目服务语义；应用到已有标签时只保留该标签明确选择的服务。 */
export function preserveServiceTarget(current: RawRequest, imported: RawRequest): RawRequest {
  if (current.service_contract === 1 && current.service_key) {
    return { ...imported, service_contract: 1, service_key: current.service_key };
  }
  const { service_contract: _contract, service_key: _key, ...defaultRequest } = imported;
  return defaultRequest;
}

function cleanV1Rows(rows: RawKeyValue[]): { name: string; value: string }[] {
  return rows
    .filter((row) => row.name.trim().length > 0)
    .map((row) => ({ name: row.name.trim(), value: row.value }));
}

export function newRequestRow(values: Pick<RawKeyValue, "name" | "value"> & Partial<RawKeyValue> = { name: "", value: "" }): RequestRowV2 {
  return {
    row_id: values.row_id ?? newRowId(),
    name: values.name,
    value: values.value,
    enabled: values.enabled ?? true,
    description: values.description ?? "",
  };
}

function newRowId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = new Uint8Array(16);
  if (typeof crypto !== "undefined" && typeof crypto.getRandomValues === "function") crypto.getRandomValues(bytes);
  else for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.floor(Math.random() * 256);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function cleanV2Rows(rows: RawKeyValue[], kind: "Query" | "Header", protectedRowIds: ReadonlySet<string>): RequestRowV2[] {
  return rows
    .filter((row) => row.name !== "" || row.value !== "" || (row.description ?? "") !== "" || (row.row_id !== undefined && protectedRowIds.has(row.row_id)))
    .map((row, index) => {
      if (!row.row_id) throw new Error(`${kind} 第 ${index + 1} 行缺少稳定标识，请重新添加该行。`);
      if (row.name === "") throw new Error(`${kind} 第 ${index + 1} 行名称不能为空。`);
      if ([...(row.description ?? "")].length > 1024) throw new Error(`${kind} 第 ${index + 1} 行说明最多 1024 个字符。`);
      if (kind === "Header" && !/^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/.test(row.name)) {
        throw new Error(`Header 第 ${index + 1} 行名称不是合法的请求头名称。`);
      }
      return {
        row_id: row.row_id,
        name: row.name,
        value: row.value,
        enabled: row.enabled === true,
        description: row.description ?? "",
      };
    });
}

export interface UpgradeResult {
  request: RawRequest;
  assertions: CaseAssertion[];
  migrated: number;
  historical: number;
}

/** 第一次参数行操作时一次性分配稳定 ID，并迁移能确定归属的旧位置定位。 */
export function upgradeRequestV2(
  raw: RawRequest,
  assertions: CaseAssertion[],
  stableIds?: { query_params: string[]; headers: string[] },
): UpgradeResult {
  if (raw.schema_version === 2) return { request: raw, assertions, migrated: 0, historical: 0 };
  const query = raw.query_params.map((row, index) => newRequestRow({ ...row, row_id: stableIds?.query_params[index] }));
  const headers = raw.headers.map((row, index) => newRequestRow({ ...row, row_id: stableIds?.headers[index] }));
  let migrated = 0;
  let historical = 0;
  const nextAssertions = assertions.map((assertion) => {
    if (assertion.target_source !== "request.query" && assertion.target_source !== "request.header") return assertion;
    const first = assertion.selector[0];
    if (!first) return assertion;
    const rows = assertion.target_source === "request.query" ? query : headers;
    let position: number | null = null;
    if (first.kind === "index") position = first.index;
    if (first.kind === "repeat_key") {
      let occurrence = -1;
      position = rows.findIndex((row) => {
        if (row.name !== first.key) return false;
        occurrence += 1;
        return occurrence === first.occurrence;
      });
    }
    if (position === null || position < 0 || position >= rows.length) {
      historical += 1;
      return assertion;
    }
    migrated += 1;
    const tail = assertion.selector.slice(1);
    return {
      ...assertion,
      selector: first.kind === "repeat_key"
        ? [{ kind: "row" as const, row_id: rows[position].row_id }, { kind: "key" as const, key: "value" }, ...tail]
        : [{ kind: "row" as const, row_id: rows[position].row_id }, ...tail],
    };
  });
  return {
    request: { ...raw, schema_version: 2, query_params: query, headers },
    assertions: nextAssertions,
    migrated,
    historical,
  };
}

/** 转成后端可校验的请求定义；路径为空时退回根路径。 */
export function rawToSpec(raw: RawRequest, assertions: CaseAssertion[] = []): RequestSpec {
  const protectedRowIds = new Set(
    assertions.flatMap((assertion) => assertion.selector[0]?.kind === "row" ? [assertion.selector[0].row_id] : []),
  );
  const queryParams = raw.schema_version === 2 ? cleanV2Rows(raw.query_params, "Query", protectedRowIds) : cleanV1Rows(raw.query_params);
  const headers = raw.schema_version === 2 ? cleanV2Rows(raw.headers, "Header", protectedRowIds) : cleanV1Rows(raw.headers);
  if (raw.schema_version === 2 && queryParams.length + headers.length > 500) {
    throw new Error("Query 和 Header 合计最多 500 行。");
  }
  const spec = {
    ...(raw.schema_version === 2 ? { schema_version: 2 as const } : {}),
    method: raw.method,
    path: raw.path.trim() === "" ? "/" : raw.path.trim(),
    query_params: queryParams,
    headers,
    body_type: raw.body_type,
    body: raw.body_type === "none" ? "" : raw.body,
    ...(raw.imported_origin ? { imported_origin: raw.imported_origin } : {}),
    ...(raw.auth_required === true ? { auth_required: true } : {}),
    ...(raw.service_contract === 1 && raw.service_key ? { service_contract: 1 as const, service_key: raw.service_key } : {}),
  };
  // 复用与响应相同的运行时校验，避免界面拼出后端不接受的结构。
  return toRequestSpec(spec);
}

export function sameRequest(left: RawRequest, right: RawRequest): boolean {
  if (JSON.stringify(left) === JSON.stringify(right)) return true;
  try {
    return JSON.stringify(rawToSpec(left)) === JSON.stringify(rawToSpec(right));
  } catch {
    // 编辑中的空名称、非法 Header 等是正常中间态：它们应算 dirty，但不能穿出 render。
    return false;
  }
}
