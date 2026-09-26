/**
 * 地址行与发送入口：方法、路径、环境、地址预览与发送主按钮。
 *
 * 这一行是本轮的核心改动——发送不再藏在页面底部、也不要求先保存或发布。但“能点”不等于
 * “能发”：按钮旁边必须给出**具体原因与对应动作**，而不是一个不可点击的禁用按钮。
 *
 * 发送入口按**操作阶段**呈现，而不是一个“忙/闲”布尔值：
 *
 * - 预检、授权、提交：显示“停止等待”，点击真的停止（见 stopWaiting 的语义）；
 * - 等待授权确认：不显示普通发送，改由确认面板上的「授权并发送」／「取消」承担；
 * - 受理结果不明：不显示普通发送，只显示「确认受理结果」——它复用原幂等键，因此
 *   不会变成第二次业务请求。
 *
 * 地址预览只是预览：变量未解析、认证未注入，它可能不等于最终实际地址，因此含变量时
 * 明确标注，不冒充最终目标。
 */
import type { ReactNode } from "react";

import type { DebugPreflight, Environment, PreflightIssue } from "../api/types";
import type { AuthorizationView } from "./useDebugRun";
import { Hint } from "../components/Feedback";
import { METHODS } from "../cases/RequestParts";
import { ltrimPath, type RawRequest } from "../cases/requestDraft";

/** 变量占位 `{{name}}`；存在它时地址预览与最终地址可能不同。 */
const VARIABLE_PATTERN = /\{\{[^}]+\}\}/;

/**
 * 发送入口的呈现阶段；由一次发送的生命周期推导，不是独立的布尔标志。
 *
 * `pending` 与 `running` 必须分开：前者是“本次发送还没拿到运行”（预检／授权／提交），
 * 后者是“运行已受理、正在等服务端结果”。把两者都写成“运行中”，用户会在请求还没发出去
 * 的时候就以为它已经在跑了。
 */
export type SendStage =
  | "idle"
  | "pending"
  | "running"
  | "awaiting_authorization"
  | "acceptance_unknown";

/**
 * 地址预览：环境地址 + 路径 +（可重复的）查询参数。
 *
 * 只显示路径会让用户以为查询参数被丢掉了——而它们其实会随请求发出去。参数**原样列出**
 * （保留顺序与重复键），不折叠成对象、不做编码：这里只是预览，真正的编码在服务端准备
 * 请求时按协议完成，预览替它编码反而会与最终地址不一致。
 */
export function addressPreview(baseUrl: string | null, request: RawRequest): string {
  const base = (baseUrl ?? "").replace(/\/+$/, "");
  const path = request.path.trim() === "" ? "/" : ltrimPath(request.path.trim());
  const query = request.query_params
    .filter((row) => row.name.trim() !== "")
    .map((row) => `${row.name.trim()}=${row.value}`)
    .join("&");
  return query === "" ? `${base}${path}` : `${base}${path}?${query}`;
}

/** 预检问题对应的操作入口文案；每个动作都要有用户能点的下一步。 */
function actionLabel(action: PreflightIssue["action"]): string {
  switch (action) {
    case "edit_request":
      return "修改请求内容";
    case "select_environment":
      return "选择执行环境";
    case "manage_credentials":
      return "前往凭证管理";
    case "authorize":
      return "授权并发送";
    case "contact_admin":
      return "联系身份管理员";
    case "configure_environment":
      return "前往环境设置";
    default:
      return "处理后再试";
  }
}

export function SendBar({
  request,
  environments,
  selectedEnvironmentId,
  onSelectEnvironment,
  onPatch,
  onSend,
  onStopWaiting,
  onRetryAcceptance,
  onResumeWaiting,
  paused,
  stage,
  readOnly,
  readOnlyReason,
  preflight,
  preflightError,
  preflighting,
  onOpenAdmin,
  onOpenEnvironment,
  canAuthorize,
  onSubmitAuthorization,
  onCancelAuthorization,
  authorization,
  tools,
}: {
  request: RawRequest;
  environments: Environment[];
  selectedEnvironmentId: string | null;
  onSelectEnvironment: (environmentId: string) => void;
  /** 方法与路径就地编辑：地址行是它们**唯一**的编辑入口，不做第二份副本。 */
  onPatch: (patch: Partial<RawRequest>) => void;
  onSend: () => void;
  onStopWaiting: () => void;
  onRetryAcceptance: () => void;
  onResumeWaiting: () => void;
  /** 运行中且已暂停本地轮询：提供恢复等待，不创建新运行。 */
  paused: boolean;
  stage: SendStage;
  readOnly: boolean;
  readOnlyReason: string | null;
  preflight: DebugPreflight | null;
  preflightError: string | null;
  preflighting: boolean;
  onOpenAdmin: () => void;
  /**
   * 打开环境设置。
   *
   * 地址类问题（`configure_environment`）的下一步是去改环境地址，凭证类问题的下一步是
   * 去凭证列表——两者不是同一个地方。缺省时退回 `onOpenAdmin`，因此老调用点不需要改动。
   */
  onOpenEnvironment?: () => void;
  canAuthorize: boolean;
  /** 等待用户确认本次授权（此时普通发送入口不出现）。 */
  onSubmitAuthorization: () => void;
  onCancelAuthorization: () => void;
  /**
   * 待确认授权的**完整冻结摘要**（环境、方法／路径、身份、被授权人、有效期）。
   *
   * 全部字段来自同一个有效操作：确认面板显示的，必须就是将要签发授权并提交出去的那份
   * 内容。用当前选择的环境名去包装一个冻结的旧操作，会让用户以为授权的是眼前这一份。
   */
  authorization: AuthorizationView | null;
  /**
   * 导入 cURL 等工具入口。
   *
   * 由调用方提供具体组件：导入是草稿编辑的事，地址行只负责给它一个**首屏可见**的位置。
   * 作为插槽放在这里而不是排在响应与样例之后——导入是新请求的第一步，藏在长表单底下
   * 等于没有这个能力。
   */
  tools?: ReactNode;
}) {
  const environment = environments.find((item) => item.id === selectedEnvironmentId) ?? null;
  const preview = environment === null ? null : addressPreview(environment.base_url, request);
  const methods = METHODS.includes(request.method) ? METHODS : [request.method, ...METHODS];
  const hasVariables =
    VARIABLE_PATTERN.test(request.path) ||
    request.query_params.some((row) => VARIABLE_PATTERN.test(row.value));
  const awaitingAuthorization = stage === "awaiting_authorization";
  const blocked = preflight !== null && !preflight.ready;
  const needsAuthorize =
    blocked && preflight !== null && preflight.auth.state === "needs_authorization";

  /**
   * 把“授权类”问题与“内容／环境类”问题分开显示。
   *
   * 一次性授权被正常消费之后，下一次预检会报“需要重新授权”。若把它和“请求写错了”一起
   * 放进红色问题块，用户会以为**刚才那次执行**失败了——而它可能刚刚通过。授权到期或已用
   * 只影响**下一次发送**，因此用紧凑说明呈现，并放回认证相关的位置。
   */
  const authorizationActions = new Set(["authorize", "contact_admin", "manage_credentials"]);
  const contentIssues = preflight !== null && !preflight.ready
    ? preflight.issues.filter((issue) => !authorizationActions.has(issue.action))
    : [];
  const authorizationIssues = preflight !== null && !preflight.ready
    ? preflight.issues.filter((issue) => authorizationActions.has(issue.action))
    : [];

  /**
   * 环境地址本身不合法时，地址预览**不能**再以“实际目标”的口吻展示。
   *
   * 预览是把环境地址与路径拼起来的字符串，地址缺协议时这个拼接结果根本不是一个能发出的
   * 目标。此时仍写着“实际目标：echo:8080/orders”，等于给用户一个不存在的结论。
   */
  const invalidEnvironment =
    preflight?.issues.some((issue) => issue.code === "environment_url_invalid") ?? false;
  const openEnvironment = onOpenEnvironment ?? onOpenAdmin;

  return (
    <div className="send-bar">
      <div className="send-line">
        <span className="param method-cell">
          <label htmlFor="request-method">方法</label>
          <select
            id="request-method"
            value={request.method}
            disabled={readOnly}
            onChange={(event) => onPatch({ method: event.target.value })}
          >
            {methods.map((method) => (
              <option key={method} value={method}>
                {method}
              </option>
            ))}
          </select>
        </span>
        <span className="param grow">
          <label htmlFor="request-path">路径</label>
          <input
            id="request-path"
            value={request.path}
            readOnly={readOnly}
            placeholder="/orders"
            onChange={(event) => onPatch({ path: ltrimPath(event.target.value) })}
          />
        </span>
        <span className="param">
          <label htmlFor="send-environment">执行环境</label>
          <select
            id="send-environment"
            value={selectedEnvironmentId ?? ""}
            disabled={readOnly}
            onChange={(event) => onSelectEnvironment(event.target.value)}
          >
            <option value="">请选择环境</option>
            {environments.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}（{item.kind === "production" ? "生产" : "测试"}）
              </option>
            ))}
          </select>
        </span>
        <SendAction
          stage={stage}
          readOnly={readOnly}
          hasEnvironment={selectedEnvironmentId !== null}
          paused={paused}
          onSend={onSend}
          onStopWaiting={onStopWaiting}
          onRetryAcceptance={onRetryAcceptance}
          onResumeWaiting={onResumeWaiting}
        />
      </div>

      <p className="caption address-preview">
        {invalidEnvironment
          ? "实际目标：环境地址不合法，暂时无法确定（请先在环境设置里修正该环境的地址）。"
          : `实际目标：${preview === null ? "尚未选择执行环境" : preview}${
              hasVariables ? "（含 {{变量}}，解析后的地址可能不同，这里只是预览）" : ""
            }`}
      </p>
      {request.imported_origin && environment !== null ? (
        <Hint>
          导入来源为 {request.imported_origin}，与会选择的环境地址不一定相同；实际目标
          始终由所选环境决定。
        </Hint>
      ) : null}

      {readOnly ? <Hint>{readOnlyReason ?? "当前角色没有发送权限。"}</Hint> : null}
      {preflighting ? <p className="caption">正在检查当前环境与凭证状态…</p> : null}
      {preflightError && stage === "idle" ? (
        <Hint>预检未能完成（{preflightError}）；发送时服务端仍会按权威规则重新检查。</Hint>
      ) : null}

      {contentIssues.length > 0 && !awaitingAuthorization ? (
        <ul className="issue-list">
          {contentIssues.map((issue) => (
            <li key={issue.code}>
              <span>{issue.message}</span>
              {issue.action === "configure_environment" ? (
                /*
                  环境类问题给**真能点的下一步**：只写一句“建议：前往环境设置”，用户还得自己
                  去侧栏里找那一段。按钮复用外壳已有的展开入口，不新建第二套管理界面。
                */
                <button type="button" className="link" onClick={openEnvironment}>
                  {actionLabel(issue.action)}
                </button>
              ) : (
                <span className="caption">建议：{actionLabel(issue.action)}</span>
              )}
            </li>
          ))}
        </ul>
      ) : null}

      {authorizationIssues.length > 0 && !awaitingAuthorization ? (
        <p className="caption auth-note">
          下次发送前需要重新确认授权：{authorizationIssues.map((issue) => issue.message).join("；")}
          （本次结果不受影响，见下方响应区。）
        </p>
      ) : null}

      {awaitingAuthorization && authorization !== null ? (
        <AuthorizationPrompt
          authorization={authorization}
          onSubmit={onSubmitAuthorization}
          onCancel={onCancelAuthorization}
        />
      ) : null}

      {needsAuthorize && !canAuthorize && !awaitingAuthorization ? (
        <Hint>本次请求需要凭证授权，请联系身份管理员；管理员可以只针对这份内容授权。</Hint>
      ) : null}

      {/* 工具与管理入口同排：管理入口留在地址行里可及，但不为它单独占一整行。 */}
      <div className="toolbar-row">
        {tools}
        <button type="button" className="link" onClick={onOpenAdmin}>
          环境与凭证管理
        </button>
      </div>
    </div>
  );
}

/**
 * 发送区的动作按钮。
 *
 * 「发送」**始终留在原位**，忙时改为禁用而不是换成别的按钮。这不是外观问题：若把它换成
 * 「停止等待」，双击的第二下就会落在同一位置的新按钮上，把用户自己那条链路停掉——用户
 * 以为自己发了一次，实际什么都没发。留成禁用的同一颗按钮，重复点击自然无效。
 *
 * 「停止等待」与「确认受理结果」因此作为独立动作出现在旁边：前者明确表示“不再等待这次
 * 受理”，后者明确表示“用原键核对那次到底受理了没有”。两者都与普通发送分开，用户不会
 * 在不知情的情况下产生第二次业务请求。
 */
function SendAction({
  stage,
  readOnly,
  hasEnvironment,
  paused,
  onSend,
  onStopWaiting,
  onRetryAcceptance,
  onResumeWaiting,
}: {
  stage: SendStage;
  readOnly: boolean;
  hasEnvironment: boolean;
  paused: boolean;
  onSend: () => void;
  onStopWaiting: () => void;
  onRetryAcceptance: () => void;
  onResumeWaiting: () => void;
}) {
  const busy = stage !== "idle";
  return (
    <>
      <button
        type="button"
        className="primary"
        onClick={onSend}
        disabled={readOnly || !hasEnvironment || busy}
        title={busy ? "上一次发送尚未结束；同一份内容不会重复提交。" : undefined}
      >
        {stage === "running" ? "运行中" : "发送"}
      </button>
      {stage === "pending" ? (
        <button type="button" className="danger" onClick={onStopWaiting}>
          停止等待
        </button>
      ) : null}
      {stage === "running" && !paused ? (
        <button type="button" className="danger" onClick={onStopWaiting}>
          停止等待
        </button>
      ) : null}
      {stage === "running" && paused ? (
        <button type="button" onClick={onResumeWaiting}>
          恢复等待
        </button>
      ) : null}
      {stage === "acceptance_unknown" ? (
        <button type="button" className="primary" onClick={onRetryAcceptance}>
          确认受理结果
        </button>
      ) : null}
    </>
  );
}

/**
 * 本次授权确认。
 *
 * 需要授权时**不自动签发**：授权是一次真实的凭证使用决定，用户必须看到这次授的是什么
 * 再确认。展示的每一项都来自**冻结的那个操作**——包括环境名称。若用当前选择的环境名去
 * 展示一个旧操作，用户会以为授权的是眼前这份配置，而实际签发并提交的是另一份。
 *
 * 摘要在服务端算，界面不出现手工摘要输入，也不出现可编辑的身份选择器——身份按环境继承，
 * 不在这里另选。确认期间内容发生变化时，上层会撤销这次待确认（避免用户在不知情下授权
 * 一份已经改变的请求）。
 */
function AuthorizationPrompt({
  authorization,
  onSubmit,
  onCancel,
}: {
  authorization: AuthorizationView;
  onSubmit: () => void;
  onCancel: () => void;
}) {
  return (
    <section className="auth-prompt" role="region" aria-label="本次授权确认">
      <h3>本次授权确认</h3>
      <ul className="caption">
        <li>
          请求：<code>{authorization.method} {authorization.path}</code>
        </li>
        <li>环境：{authorization.environmentLabel}</li>
        <li>身份：当前环境登录态（按该环境配置的身份注入，不在此另选）</li>
        <li>被授权人：当前登录账号本人</li>
        <li>有效期：{authorization.ttlMinutes} 分钟；授权不等于保存或发布用例</li>
      </ul>
      {/*
        内部坐标收在详细信息里：用户需要判断的是“这次授的是什么”，不是一串 UUID。
        但坐标也不能省——需要核对身份的管理员得能看到授权到底绑定在哪一份身份上。
      */}
      <details>
        <summary>详细信息</summary>
        <ul className="caption">
          <li>环境 ID：{authorization.environmentId}</li>
          <li>身份 ID：{authorization.profileId ?? "（服务端未返回可授权的身份）"}</li>
          <li>被授权人 ID：{authorization.principalId}</li>
        </ul>
      </details>
      <p className="caption">
        确认后由服务端按<strong>这份冻结内容</strong>计算摘要并签发一次性授权，随后用同一份
        内容发送；期间继续编辑不会改变已提交的内容。
      </p>
      <div className="actions">
        <button type="button" className="primary" onClick={onSubmit}>
          授权并发送
        </button>
        <button type="button" onClick={onCancel}>
          取消
        </button>
      </div>
    </section>
  );
}

/** 认证标签：按环境身份的真实状态说明本次会以谁的身份发送。 */
export function AuthTab({
  preflight,
  preflightError,
  environmentName,
  authRequired,
  onToggleRequired,
  readOnly,
  onOpenAdmin,
}: {
  preflight: DebugPreflight | null;
  preflightError: string | null;
  environmentName: string | null;
  authRequired: boolean;
  onToggleRequired: (next: boolean) => void;
  readOnly: boolean;
  onOpenAdmin: () => void;
}) {
  const state = preflight?.auth.state ?? null;
  return (
    <div className="auth-tab">
      <p className="caption">
        身份按环境继承：一次请求使用当前环境的登录态，不能在这里为单次请求另选身份。
      </p>

      <label className="checkbox-row">
        <input
          type="checkbox"
          checked={authRequired}
          disabled={readOnly}
          onChange={(event) => onToggleRequired(event.target.checked)}
        />
        必须使用环境登录态（关闭时“跟随环境”：环境配了可用身份就注入，没有就按公开请求发送）
      </label>
      {authRequired ? (
        <Hint>
          这份请求已标记为必须认证。没有可用身份时发送会被拒绝，不会退回匿名发送；
          导入时识别到认证头或 Cookie 会自动打开它，原始凭证不会被保存。
        </Hint>
      ) : null}

      <h3>当前状态</h3>
      {environmentName === null ? (
        <Hint>还没有选择执行环境。</Hint>
      ) : preflightError ? (
        <Hint>状态未能读取（{preflightError}）。</Hint>
      ) : state === null ? (
        <Hint>尚未检查；发送前会自动检查一次。</Hint>
      ) : (
        <ul className="caption">
          <li>环境：{environmentName}</li>
          <li>认证要求：{preflight?.auth.required ? "必须使用环境登录态" : "跟随环境"}</li>
          <li>
            当前状态：
            {state === "none"
              ? "该环境没有配置身份，将按公开请求发送"
              : state === "ready"
                ? "有可用身份且本次用途已获授权"
                : state === "needs_authorization"
                  ? "有可用身份，但本次内容还没有授权"
                  : state === "ambiguous"
                    ? "该环境有多份可用身份，无法确定使用哪一份"
                    : "身份不可用（未配置、已过期或已停用）"}
          </li>
        </ul>
      )}

      <div className="actions">
        <button type="button" onClick={onOpenAdmin}>
          打开凭证管理
        </button>
      </div>
    </div>
  );
}
