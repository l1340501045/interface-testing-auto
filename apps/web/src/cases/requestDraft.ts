/**
 * 请求草稿的本地形态与提交形态。
 *
 * 界面需要“空行”表示尚未填写的参数，而后端校验要求名称非空；因此本地用原始行
 * 保存，提交前统一丢弃空行。名称的前后空白在提交时去掉，值保持原样，避免悄悄
 * 改变用户填写的查询参数。
 */
import { toRequestSpec } from "../api/guards";
import type { RequestSpec } from "../api/types";

export interface RawKeyValue {
  name: string;
  value: string;
}

export interface RawRequest {
  method: string;
  path: string;
  query_params: RawKeyValue[];
  headers: RawKeyValue[];
  body_type: RequestSpec["body_type"];
  body: string;
  imported_origin?: string;
  /** 必须使用当前环境登录态；见 RequestSpec.auth_required。缺省即“跟随环境”。 */
  auth_required?: boolean;
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
  return raw;
}

function cleanRows(rows: RawKeyValue[]): { name: string; value: string }[] {
  return rows
    .filter((row) => row.name.trim().length > 0)
    .map((row) => ({ name: row.name.trim(), value: row.value }));
}

/** 转成后端可校验的请求定义；路径为空时退回根路径。 */
export function rawToSpec(raw: RawRequest): RequestSpec {
  const spec = {
    method: raw.method,
    path: raw.path.trim() === "" ? "/" : raw.path.trim(),
    query_params: cleanRows(raw.query_params),
    headers: cleanRows(raw.headers),
    body_type: raw.body_type,
    body: raw.body_type === "none" ? "" : raw.body,
    ...(raw.imported_origin ? { imported_origin: raw.imported_origin } : {}),
    ...(raw.auth_required === true ? { auth_required: true } : {}),
  };
  // 复用与响应相同的运行时校验，避免界面拼出后端不接受的结构。
  return toRequestSpec(spec);
}

export function sameRequest(left: RawRequest, right: RawRequest): boolean {
  return JSON.stringify(rawToSpec(left)) === JSON.stringify(rawToSpec(right));
}
