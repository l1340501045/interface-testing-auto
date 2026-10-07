/**
 * 同源 API 客户端：CSRF 回填、错误信封解析、请求取消。
 *
 * 所有写请求都带上可读 Cookie 中的 CSRF 令牌，与服务端 httpOnly 会话形成双提交；
 * 令牌不落 localStorage，只在内存与 Cookie 之间传递。401 统一转为会话失效，
 * 由上层清理界面状态并回到登录页，不在组件里各自判断状态码。
 */

const CSRF_COOKIE = "interface_csrf";
const CSRF_HEADER = "X-CSRF-Token";
const API_PREFIX = "/api/v1";
export const REQUEST_CONTRACT_HEADERS = { "X-Request-Contract": "2" } as const;
/** 新前端统一声明理解多服务目标；行协议与服务协议是两条独立能力。 */
export const SERVICE_CONTRACT_HEADERS = { "X-Service-Contract": "1" } as const;

/** v2 兼容错误统一转成用户可以直接采取行动的文案。 */
function compatibilityMessage(code: string, fallback: string): string {
  if (code === "client_contract_required") return "当前页面版本无法完整读取这份请求，请刷新页面后重试。";
  if (code === "request_contract_downgrade") return "这份请求已使用新版参数格式，不能用旧格式覆盖；请刷新页面后继续编辑。";
  return fallback;
}

/** 后端错误信封：稳定 code、中文 message、可关联日志的 trace_id。 */
export interface ApiErrorBody {
  code: string;
  message: string;
  trace_id: string | null;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly traceId: string | null;

  constructor(status: number, code: string, message: string, traceId: string | null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.traceId = traceId;
  }

  /** 会话失效：上层据此回到登录页。 */
  get isUnauthenticated(): boolean {
    return this.status === 401;
  }

  /** 版本冲突：提示用户刷新，不静默覆盖他人修改。 */
  get isConflict(): boolean {
    return this.status === 409;
  }
}

/**
 * 会话失效广播：任何请求收到 401 都通知一次，由会话层清理身份并回到登录页。
 *
 * 放在网络边界而不是各个组件里，是因为“什么时候会话失效”只有服务端知道；
 * 让每个页面自己判断状态码会漏掉后台请求（例如运行轮询）触发的失效。
 */
type UnauthorizedListener = () => void;
const unauthorizedListeners = new Set<UnauthorizedListener>();
let sessionGeneration = 0;

/** 同步作废当前主体已发出的旧请求结果；不会声称取消已经到达服务端的写操作。 */
export function invalidateClientSession(): void {
  sessionGeneration += 1;
}

export function onUnauthorized(listener: UnauthorizedListener): () => void {
  unauthorizedListeners.add(listener);
  return () => {
    unauthorizedListeners.delete(listener);
  };
}

function notifyUnauthorized(): void {
  invalidateClientSession();
  for (const listener of [...unauthorizedListeners]) listener();
}

function readCookie(name: string): string {
  const prefix = `${name}=`;
  for (const part of document.cookie.split(";")) {
    const trimmed = part.trim();
    if (trimmed.startsWith(prefix)) return decodeURIComponent(trimmed.slice(prefix.length));
  }
  return "";
}

/** 网络层失败：目标不可达、DNS、连接中断等，与业务错误区分。 */
export class NetworkError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "NetworkError";
  }
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  signal?: AbortSignal;
  headers?: Record<string, string>;
}

interface RawResponse {
  status: number;
  headers: Headers;
  body: unknown;
}

function toErrorBody(payload: unknown, status: number): ApiErrorBody {
  if (typeof payload === "object" && payload !== null) {
    const record = payload as Record<string, unknown>;
    const code = typeof record.code === "string" ? record.code : "unknown_error";
    const message = typeof record.message === "string" ? record.message : "请求失败";
    const traceId = typeof record.trace_id === "string" ? record.trace_id : null;
    return { code, message, trace_id: traceId };
  }
  return { code: "unknown_error", message: `请求失败（HTTP ${status}）`, trace_id: null };
}

async function send(path: string, options: RequestOptions): Promise<RawResponse> {
  const generation = sessionGeneration;
  const method = options.method ?? "GET";
  // 新客户端始终声明完整理解 RequestSpec v2；后端只在相关资源上使用该能力声明。
  const headers: Record<string, string> = {
    Accept: "application/json",
    ...REQUEST_CONTRACT_HEADERS,
    ...SERVICE_CONTRACT_HEADERS,
    ...options.headers,
  };
  if (method !== "GET" && method !== "HEAD") {
    headers[CSRF_HEADER] = readCookie(CSRF_COOKIE);
  }
  let body: string | undefined;
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }

  let response: Response;
  try {
    response = await fetch(path, { method, headers, body, credentials: "same-origin", signal: options.signal });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new NetworkError("无法连接服务，请检查网络后重试");
  }
  if (generation !== sessionGeneration) throw new DOMException("旧会话请求已作废", "AbortError");

  const text = await response.text();
  if (generation !== sessionGeneration) throw new DOMException("旧会话请求已作废", "AbortError");
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      // 代理返回 HTML 错误页等情况：保留状态码，正文不当作 JSON 解析结果使用。
      payload = null;
    }
  }
  if (!response.ok) {
    if (generation !== sessionGeneration) throw new DOMException("旧会话请求已作废", "AbortError");
    const error = toErrorBody(payload, response.status);
    if (response.status === 401) notifyUnauthorized();
    throw new ApiError(
      response.status,
      error.code,
      compatibilityMessage(error.code, error.message),
      error.trace_id,
    );
  }
  if (generation !== sessionGeneration) throw new DOMException("旧会话请求已作废", "AbortError");
  return { status: response.status, headers: response.headers, body: payload };
}

export async function apiGet<T>(path: string, parse: (raw: unknown) => T, signal?: AbortSignal): Promise<T> {
  const result = await send(`${API_PREFIX}${path}`, { signal });
  return parse(result.body);
}

export async function apiSend<T>(
  path: string,
  method: string,
  body: unknown,
  parse: (raw: unknown) => T,
  options?: { headers?: Record<string, string>; signal?: AbortSignal },
): Promise<T> {
  const result = await send(`${API_PREFIX}${path}`, {
    method,
    body,
    headers: options?.headers,
    signal: options?.signal,
  });
  return parse(result.body);
}

/** 保存用例需要读回 ETag 做乐观锁，因此单独暴露响应头。 */
export async function apiSendWithMeta<T>(
  path: string,
  method: string,
  body: unknown,
  parse: (raw: unknown) => T,
  options?: { headers?: Record<string, string>; signal?: AbortSignal },
): Promise<{ data: T; etag: string | null }> {
  const result = await send(`${API_PREFIX}${path}`, {
    method,
    body,
    headers: options?.headers,
    signal: options?.signal,
  });
  return { data: parse(result.body), etag: result.headers.get("ETag") };
}

export async function apiDelete(path: string, options?: { headers?: Record<string, string>; signal?: AbortSignal }): Promise<void> {
  await send(`${API_PREFIX}${path}`, { method: "DELETE", headers: options?.headers, signal: options?.signal });
}

export function projectPath(workspaceId: string, projectId: string, suffix: string): string {
  return `/workspaces/${workspaceId}/projects/${projectId}${suffix}`;
}
