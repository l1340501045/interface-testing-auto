/**
 * 认证方案：把“用哪种认证方式”翻译成后端要的槽位与配置。
 *
 * 这里存在的理由是界面不该让管理员手写内部 JSON。后端要的是两样东西：
 * 允许注入的槽位（`header.Authorization` 这类）与身份版本的配置快照（认证位置、
 * 值前缀、失效判据）。让用户拼 JSON 等于把内部契约当作输入格式：拼错一个键不会
 * 报错，只会让配置静静地少一半内容，直到运行时才以“凭证没生效”暴露出来。
 *
 * 因此界面只问业务问题——用什么方案、放在哪个头/参数、值有什么前缀、什么样的响应
 * 算凭证失效——由这里生成结构。生成结果可被序列化后原样展示给管理员复核，但用户
 * 不需要、也不应该直接编辑它。
 */

/** 支持的认证方案；自定义是“方案不在列表里”的兜底，不是任意 JSON。 */
export const AUTH_SCHEMES = [
  { id: "bearer", label: "请求头 Bearer Token", slotKind: "header", defaultName: "Authorization", defaultPrefix: "Bearer " },
  { id: "cookie", label: "Cookie", slotKind: "header", defaultName: "Cookie", defaultPrefix: "" },
  { id: "raw_header", label: "请求头（原样值）", slotKind: "header", defaultName: "", defaultPrefix: "" },
  { id: "query", label: "查询参数", slotKind: "query", defaultName: "", defaultPrefix: "" },
] as const;

export type AuthSchemeId = (typeof AUTH_SCHEMES)[number]["id"];
export type SlotKind = "header" | "query";

export interface AuthLocation {
  scheme: AuthSchemeId;
  /** 头名或参数名；Bearer 固定 Authorization，Cookie 固定 Cookie。 */
  name: string;
  /** 值前缀，如 "Bearer "；原样值方案留空。 */
  prefix: string;
}

export interface InvalidRule {
  /** 视为凭证已失效的响应状态码。 */
  statusCodes: number[];
  /** 被重定向到登录页同样视为失效。 */
  redirectToLogin: boolean;
}

export interface AuthDeclaration {
  locations: AuthLocation[];
  invalidation: InvalidRule;
}

export const EMPTY_AUTH: AuthDeclaration = {
  locations: [{ scheme: "bearer", name: "Authorization", prefix: "Bearer " }],
  invalidation: { statusCodes: [401], redirectToLogin: false },
};

/** 可勾选的失效判据状态码；与产品约定的判据集合一致。 */
export const INVALIDATION_CODES = [401, 403] as const;

export function schemeLabel(id: AuthSchemeId): string {
  return AUTH_SCHEMES.find((item) => item.id === id)?.label ?? id;
}

/** 方案决定槽位种类与默认名称；切换方案时同步改掉默认值，避免留下上一个方案的残值。 */
export function applyScheme(location: AuthLocation, scheme: AuthSchemeId): AuthLocation {
  const preset = AUTH_SCHEMES.find((item) => item.id === scheme);
  if (preset === undefined) return { ...location, scheme };
  const next: AuthLocation = {
    scheme,
    name: preset.defaultName,
    prefix: preset.defaultPrefix,
  };
  return next;
}

/** 槽位字符串：与执行内核一致（header.X 或 query.x）。 */
export function authSlot(location: AuthLocation): string {
  const kind: SlotKind = AUTH_SCHEMES.find((item) => item.id === location.scheme)?.slotKind ?? "header";
  return `${kind}.${location.name.trim()}`;
}

/** 认证位置是否可提交；名称缺失或重复都不该带着一个错误声明去创建身份。 */
export function authLocationError(declaration: AuthDeclaration): string | null {
  if (declaration.locations.length === 0) return "请至少声明一个认证位置。";
  const seen = new Set<string>();
  for (const location of declaration.locations) {
    if (!location.name.trim()) return "认证位置的头名或参数名不能为空。";
    const slot = authSlot(location);
    if (slot.endsWith(".")) return `认证位置格式无效：${slot}`;
    if (seen.has(slot)) return `认证位置重复：${slot}`;
    seen.add(slot);
    if (location.scheme === "bearer" && !location.prefix) {
      return "Bearer Token 需要值前缀（通常是 “Bearer ”加一个空格）。";
    }
  }
  return null;
}

/** 声明涉及的槽位列表，直接用于身份的允许槽位。 */
export function authSlots(declaration: AuthDeclaration): string[] {
  return declaration.locations.map(authSlot);
}

/**
 * 身份版本的配置快照。
 *
 * `auth_locations` 同时给出槽位、方案与值前缀：只写方案会丢掉“放在哪个头里”，
 * 只写槽位会丢掉“值要不要加前缀”，两者都在运行时才用得上，缺一个都装不起来。
 */
export function authConfig(declaration: AuthDeclaration): Record<string, unknown> {
  return {
    auth_locations: declaration.locations.map((location) => ({
      slot: authSlot(location),
      scheme: location.scheme,
      prefix: location.prefix,
    })),
    invalidation: {
      status_codes: [...declaration.invalidation.statusCodes].sort((left, right) => left - right),
      redirect_to_login: declaration.invalidation.redirectToLogin,
    },
  };
}

/** 失效判据的可读摘要，用于列表展示与提交前复核。 */
export function invalidationSummary(rule: InvalidRule): string {
  const codes = [...rule.statusCodes].sort((left, right) => left - right).map(String);
  const parts: string[] = [];
  if (codes.length > 0) parts.push(`状态码 ${codes.join("、")}`);
  if (rule.redirectToLogin) parts.push("跳转到登录页");
  return parts.length === 0 ? "未设置失效判据" : parts.join("；");
}
