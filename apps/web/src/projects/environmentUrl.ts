/**
 * 环境基础地址的语法校验：就地早反馈，服务端仍然是权威。
 *
 * 规则与服务端 `app/kernel/environment_url.py` 一一对应，包括**提示文案**——同一份输入
 * 无论被哪一层拦下，用户看到的都是同一句话，不会出现“前端说少协议、后端说别的”。
 * 两边都在，是因为它们解决不同问题：前端让人在离开输入框之前就知道写错了，后端保证绕过
 * 页面的调用（脚本、直接请求）也不能把坏地址存进库。因此这里**不承担**准入职责，也不做
 * DNS／HTTP 探测。
 *
 * 基础地址会拼到用例路径前面，所以它必须是一个完整服务地址：`https://service.example`
 * 或带基础路径的 `https://service.example/api/v1`。裸主机名与“主机:端口”看起来像
 * 地址，却没有任何协议信息，拼出来的目标无法解析——过去它们能存进库，直到执行时才失败，
 * 并且被报成“目标不在白名单”，把用户引向去修改本来正确的用例路径。
 *
 * 只 trim 首尾**普通空格**，不补协议、不改大小写、不重写路径与百分号编码：基础路径是
 * 用户配置的一部分，被“规范化”改掉会让请求打到别的地方。
 */

const SEPARATOR = "://";
const ALLOWED_SCHEMES = ["http", "https"];
const MIN_PORT = 1;
const MAX_PORT = 65535;
const MAX_HOST_LENGTH = 253;
const MAX_LABEL_LENGTH = 63;

/**
 * 主机标签：字母、数字、下划线，内部可含连字符。
 *
 * 用 `\p{L}`／`\p{N}` 而不是 `[A-Za-z0-9_]`：国际化域名的标签是非 ASCII 字母，ASCII-only
 * 规则会把合法域名先拒掉，而服务端与目标策略本来就接受这些来源。前端是早反馈，但把合法
 * 地址先拦住同样是缺陷。下划线在真实内网名字里很常见。
 */
const LABEL_PATTERN = /^[\p{L}\p{N}_](?:[\p{L}\p{N}_-]*[\p{L}\p{N}_])?$/u;
/** 方括号里的 IPv6 字面量：前端只做形态检查，权威解析在服务端。 */
const IPV6_PATTERN = /^[0-9A-Fa-f:.]+(%[0-9A-Za-z]+)?$/;

/**
 * 示例地址用保留域名 `service.example`（RFC 2606），不是任何真实环境。
 *
 * 两个示例都要给出：日常调试以域名为主，但**IP＋端口同样必须支持**——内网直连时它才是
 * 常见的写法。地址用 TEST-NET-1（RFC 5737 的 `192.0.2.0/24`），同样不会指向真实主机。
 */
const EXAMPLE = "https://service.example";
const IP_EXAMPLE = "http://192.0.2.10:8080";

/** 地址示例：表单里给用户照着填的那一个，与错误文案里的保持一致。 */
export const ENVIRONMENT_URL_EXAMPLE = EXAMPLE;

/** IP＋端口的地址示例；说明文案用它表明这种写法同样受支持。 */
export const ENVIRONMENT_URL_IP_EXAMPLE = IP_EXAMPLE;

export const CONTROL_CHARACTER_MESSAGE =
  "环境地址不能包含空格、制表符、换行或其他控制字符。";

/** 去掉首尾普通空格；`trim()` 会连制表符与换行一起吃掉，与服务端不一致。 */
function trimSpaces(raw: string): string {
  return raw.replace(/^ +/, "").replace(/ +$/, "");
}

function hasControlOrSpace(text: string): boolean {
  for (const char of text) {
    // \s 覆盖空格、制表符、换行与 Unicode 空白；Cc／Cf 是控制与格式字符（含零宽空格）。
    if (/\s/u.test(char) || /[\p{Cc}\p{Cf}]/u.test(char)) return true;
  }
  return false;
}

/** 四段全 ASCII 数字的 IPv4 外形；只看外形，越界与前导零交给严格判断。 */
function looksLikeIpv4(host: string): boolean {
  const parts = host.split(".");
  return parts.length === 4 && parts.every((part) => /^[0-9]+$/.test(part));
}

/** 严格 IPv4：每段 0–255，且不接受前导零（与真实 HTTP 客户端一致）。 */
function isStrictIpv4(host: string): boolean {
  return host.split(".").every(
    (part) =>
      /^[0-9]{1,3}$/.test(part) &&
      !(part.length > 1 && part.startsWith("0")) &&
      Number(part) <= 255,
  );
}

function hostMessage(host: string): string | null {
  if (host.startsWith("[") && host.endsWith("]")) {
    const inner = host.slice(1, -1);
    return inner !== "" && IPV6_PATTERN.test(inner)
      ? null
      : "环境地址的主机名不合法：只能写域名（含内网短名与国际化域名）或 IP 地址。";
  }
  if (looksLikeIpv4(host)) {
    // 外形一旦成立，这个主机只可能是 IPv4，不能退回域名分支：那样纯数字标签会通过标签
    // 规则，把客户端发不出去的地址说成合法。
    return isStrictIpv4(host)
      ? null
      : "环境地址的 IPv4 地址不合法：每段必须是 0 到 255，且不能有前导零。";
  }
  // 尾部单个点是根域写法，不占主机名长度；先去掉再判断长度与标签（与服务端同一口径）。
  const name = host.endsWith(".") ? host.slice(0, -1) : host;
  if (name === "") return "环境地址缺少主机名。";
  if (name.length > MAX_HOST_LENGTH) return "环境地址的主机名过长。";
  for (const label of name.split(".")) {
    if (label === "" || label.length > MAX_LABEL_LENGTH || !LABEL_PATTERN.test(label)) {
      return "环境地址的主机名不合法：只能写域名（含内网短名与国际化域名）或 IP 地址。";
    }
  }
  return null;
}

/** 把 authority 拆成主机与端口文本；无法拆分时返回错误文案。 */
function splitAuthority(authority: string): { host: string; port: string } | string {
  if (authority.startsWith("[")) {
    const close = authority.indexOf("]");
    if (close === -1) return "环境地址格式不合法，无法解析。";
    const rest = authority.slice(close + 1);
    if (rest !== "" && !rest.startsWith(":")) return "环境地址格式不合法，无法解析。";
    return { host: authority.slice(0, close + 1), port: rest.slice(1) };
  }
  // 按**第一个**冒号切分，与标准库的 authority 解析口径一致：`h:80:90` 里的后半段是
  // 端口文本（非法），而不是“主机 h:80”。主机名本身不允许含冒号，因此这样切没有歧义。
  const colon = authority.indexOf(":");
  if (colon === -1) return { host: authority, port: "" };
  return { host: authority.slice(0, colon), port: authority.slice(colon + 1) };
}

/**
 * 校验环境基础地址；合法返回 `null`，不合法返回可直接展示的中文原因。
 *
 * 返回值只描述问题，**不复述输入**：地址常常是从浏览器或抓包工具整段粘过来的，原文里
 * 可能带着凭证或签名，把它回显到界面上就等于把秘密显示出来。
 */
export function validateEnvironmentUrl(raw: string): string | null {
  const text = trimSpaces(raw);
  if (!text.includes(SEPARATOR)) {
    return `环境地址必须以 http:// 或 https:// 开头，不能只写主机名或“主机:端口”。例如 ${EXAMPLE}。`;
  }
  if (hasControlOrSpace(text)) return CONTROL_CHARACTER_MESSAGE;
  if (text.includes("\\")) return "环境地址不能包含反斜杠；路径分隔符请使用正斜杠。";
  if (text.includes("?")) return "环境地址不能带查询参数，请把查询参数写在用例的请求参数里。";
  if (text.includes("#")) return "环境地址不能带 # 片段。";

  const separatorAt = text.indexOf(SEPARATOR);
  const scheme = text.slice(0, separatorAt).toLowerCase();
  if (!ALLOWED_SCHEMES.includes(scheme)) {
    return "环境地址只支持 http 或 https 协议，其他协议不会被发送。";
  }

  const rest = text.slice(separatorAt + SEPARATOR.length);
  const slashAt = rest.indexOf("/");
  const authority = slashAt === -1 ? rest : rest.slice(0, slashAt);
  if (authority === "") return `环境地址缺少主机名，例如 ${EXAMPLE}。`;
  if (authority.includes("@")) {
    return "环境地址不能包含账号信息（账号:口令@主机），凭证请在身份配置里维护。";
  }
  if (authority.endsWith(":")) {
    return "环境地址的端口号不完整：冒号后面缺少数字。";
  }

  const split = splitAuthority(authority);
  if (typeof split === "string") return split;
  if (split.host === "") return `环境地址缺少主机名，例如 ${EXAMPLE}。`;
  if (split.port !== "") {
    if (!/^[0-9]+$/.test(split.port)) {
      return "环境地址的端口号必须是 1 到 65535 之间的数字。";
    }
    const port = Number(split.port);
    if (port < MIN_PORT || port > MAX_PORT) {
      return "环境地址的端口号必须是 1 到 65535 之间的数字。";
    }
  }
  return hostMessage(split.host);
}
