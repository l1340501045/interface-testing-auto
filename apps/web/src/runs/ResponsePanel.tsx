/**
 * 本次调试的响应区：状态、耗时、正文、响应头、字段与断言结论。
 *
 * 三条不算“顺手美化”的约束：
 *
 * 1. **有证据才显示。** 没有响应时不补造 200 或 0 毫秒；正文被省略、被截断、目标是
 *    纯文本，各自按服务端给的原因说明。只读响应正文时永远当文本，不执行其中的 HTML。
 * 2. **`size_bytes` 是正文大小**，不是网络流量，标签照实写。
 * 3. **旧报告保留但不得冒充当前结论。** 报告带来源标记（环境 + 两个不透明指纹）；
 *    当前内容／环境对不上时，字段行旁不贴旧的通过，只在“上次响应”里保留原文供继续
 *    配置断言。
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Button, Tabs, Tag } from "antd";

import type { RunReport } from "../api/types";
import { Hint } from "../components/Feedback";
import { isTerminal, runOutcomeLabel, runReasonLabel, runStateLabel, uncheckedReasonLabel } from "./useRuns";

/** 报告里的响应证据；只在服务端确实给了响应时才存在。 */
export interface ResponseEvidence {
  status: number;
  elapsed_ms: number;
  headers: { name: string; value: string }[];
  body: string;
  body_format: string;
  body_truncated: boolean;
  body_omitted_reason: string | null;
  size_bytes: number;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

/**
 * 从报告里取出响应证据。
 *
 * 形状不对时返回 `null` 而不是补默认值：报告是外部输入，靠猜填出来的 200／0 毫秒
 * 会被当成真实结论展示。
 */
export function responseEvidence(report: RunReport | null): ResponseEvidence | null {
  const raw = asRecord(report?.response ?? null);
  if (raw === null) return null;
  const status = raw.status;
  if (typeof status !== "number") return null;
  const headers = Array.isArray(raw.headers)
    ? raw.headers.flatMap((item) => {
        const entry = asRecord(item);
        if (entry === null) return [];
        const name = entry.name;
        const value = entry.value;
        if (typeof name !== "string" || typeof value !== "string") return [];
        return [{ name, value }];
      })
    : [];
  return {
    status,
    elapsed_ms: typeof raw.elapsed_ms === "number" ? raw.elapsed_ms : 0,
    headers,
    body: typeof raw.body === "string" ? raw.body : "",
    body_format: typeof raw.body_format === "string" ? raw.body_format : "unknown",
    body_truncated: raw.body_truncated === true,
    body_omitted_reason: typeof raw.body_omitted_reason === "string" ? raw.body_omitted_reason : null,
    size_bytes: typeof raw.size_bytes === "number" ? raw.size_bytes : 0,
  };
}

const OMITTED_REASONS: Record<string, string> = {
  unparsable_json_body: "正文声明为 JSON 但无法安全解析，为避免展示残缺内容已省略。",
  incomplete_redaction: "正文脱敏后仍可能读出凭证，已省略。",
};

function omittedReasonLabel(reason: string): string {
  return OMITTED_REASONS[reason] ?? `正文已省略（${reason}）。`;
}

export function ResponsePanel({
  report,
  reportError,
  loading,
  /** 当前内容与这份报告是否同源；false 时只作为“上次响应”保留。 */
  matchesCurrent,
  evidencePending = false,
  selectedRunId,
  onCancel,
  canCancel,
  fieldsTab,
}: {
  report: RunReport | null;
  reportError: string | null;
  loading: boolean;
  matchesCurrent: boolean;
  evidencePending?: boolean;
  /**
   * 当前明确选中的运行 id。
   *
   * 取消入口由它决定，**不由报告是否读成功决定**：报告读失败、还没读回来，都不影响
   * “取消这一个已知运行”。反过来，没有 run_id 时也不能假装服务端执行已停止。
   */
  selectedRunId: string | null;
  onCancel: (runId: string) => void;
  /**
   * 当前角色是否可以取消运行。
   *
   * 查看者能读未结束的历史运行，但没有取消能力。守卫要在**这里**就把按钮去掉，而不是
   * 让用户点下去收一个 403——那是把一个权限事实伪装成一次失败操作。
   */
  canCancel: boolean;
  /**
   * 「字段与断言」标签里的内容（出参样例、预期字段与字段旁的条件入口）。
   *
   * 作为插槽而不是在这里写死：面板只负责“这份响应是什么”，字段断言的编辑组件由编辑器
   * 提供，两边的职责不混在一起。放进标签而不是常驻在面板底下：那块表单很长，会把它上面
   * 的正文、以及它下面的“本次记录”一起推出视野，而多数时候用户只是先看响应。
   */
  fieldsTab?: ReactNode | ((active: boolean) => ReactNode);
}) {
  const evidence = useMemo(() => responseEvidence(report), [report]);
  /** 响应区自己的标签；空状态下也显示，用户才能看出这里会有哪些内容。 */
  const [tab, setTab] = useState<"body" | "fields" | "headers">("body");
  const tabsRootRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    tabsRootRef.current?.querySelector<HTMLElement>('[role="tablist"]')?.setAttribute("aria-label", "响应");
  }, []);

  return (
    <section className="response-pane" aria-label="响应">
      <header className="pane-head response-head">
        <h2>响应</h2>
        {selectedRunId === null ? null : (
          <span className="caption">运行 {selectedRunId.slice(0, 8)}</span>
        )}
        {report !== null ? (
          <>
            <Tag>{runStateLabel(report.run)}</Tag>
            <Tag>{runOutcomeLabel(report.run)}</Tag>
            {runReasonLabel(report.run) ? (
              <Tag color="warning">{runReasonLabel(report.run)}</Tag>
            ) : null}
          </>
        ) : null}
        {canCancel && selectedRunId !== null && !(report !== null && isTerminal(report.run)) ? (
          <Button htmlType="button" danger onClick={() => onCancel(selectedRunId)}>
            取消本次执行
          </Button>
        ) : null}
        {!canCancel && selectedRunId !== null && !(report !== null && isTerminal(report.run)) ? (
          <span className="caption">当前角色只能查看，不能取消运行</span>
        ) : null}
      </header>

      {/*
        标签常驻：还没有响应时也让用户看到这里会有正文、字段与断言、响应头。
        没有响应时只有一个空态说明，因此不需要像请求区那样限制面板高度。
      */}
      {selectedRunId === null ? (
        <Hint>还没有本次调试记录。点击地址栏右侧的「发送」即可调试当前编辑内容，无需先保存用例。</Hint>
      ) : null}
      {reportError ? <Hint>报告读取失败：{reportError}</Hint> : null}
      {loading && report === null ? <Hint>正在读取本次报告…</Hint> : null}

      {report !== null ? (
        <>
          {report.run.outcome === "completed_unchecked" ? (
            <Hint>{uncheckedReasonLabel(report.steps.at(-1)?.error_code ?? null)}</Hint>
          ) : null}
          {/*
            两条互斥的说明，判据都是“这条运行有没有结果可谈”。
            `isTerminal` 是服务端状态：排队／执行中的运行还没有结论，不能说“上一次发送的
            结果”——**来源尚未确定不等于已有旧结果**，那会让用户在刚点完发送时就以为
            屏幕上显示的是上一轮的东西。
          */}
          {!isTerminal(report.run) ? (
            <Hint>
              该运行尚未结束（{runStateLabel(report.run)}），结果出来后这里会自动刷新。
            </Hint>
          ) : null}
          {isTerminal(report.run) && !matchesCurrent ? (
            <Hint>
              {evidencePending ? (
                <>当前依据暂未确认；下面保留该运行的历史结果，确认完成前字段行旁不会贴用这次结论。</>
              ) : (
                <>下面显示的是<strong>上一次发送</strong>的结果，当前编辑内容或执行环境已与它不同，
                  因此字段行旁不会贴用这次结论。继续编辑不受影响。</>
              )}
            </Hint>
          ) : null}

          <p className="metrics-line">
            {evidence !== null ? (
              <>
                <span className="metric-inline">
                  状态 <strong>{evidence.status}</strong>
                </span>
                <span className="metric-inline">
                  耗时 <strong>{evidence.elapsed_ms}</strong> 毫秒
                </span>
                <span className="metric-inline">
                  正文 <strong>{evidence.size_bytes}</strong> 字节
                </span>
              </>
            ) : (
              <span className="caption">本次没有收到响应。</span>
            )}
          </p>

          {report.response !== null && evidence === null ? (
            // 服务端给了 response，但里面只有错误说明（网络失败等），没有任何状态码。
            <Hint>本次请求未收到响应</Hint>
          ) : null}
        </>
      ) : null}

      <div ref={tabsRootRef}>
      <Tabs
        aria-label="响应"
        activeKey={tab}
        onChange={(value) => setTab(value as "body" | "fields" | "headers")}
        destroyOnHidden={false}
        items={[
          {
            key: "body",
            label: "正文",
            forceRender: true,
            children: <div className="response-tab-body">
            {report === null ? (
              <Hint>发送后，这里显示服务端返回的真实状态码、耗时与正文。</Hint>
            ) : evidence === null ? (
              <Hint>本次没有响应正文可显示。</Hint>
            ) : evidence.body_omitted_reason ? (
              <Hint>{omittedReasonLabel(evidence.body_omitted_reason)}</Hint>
            ) : evidence.body === "" ? (
              <Hint>响应正文为空。</Hint>
            ) : (
              <>
                {evidence.body_truncated ? (
                  <Hint>正文过长，下面显示的是截断后的内容。</Hint>
                ) : null}
                {/* 一律当文本渲染：响应 HTML 不执行，也不注入。 */}
                <pre className="evidence">{evidence.body}</pre>
              </>
            )}
            </div>,
          },
          {
            key: "fields",
            label: (
              <span>
                字段与断言
                {report !== null && report.assertions.length > 0 ? <span className="tab-badge">{report.assertions.length}</span> : null}
              </span>
            ),
            forceRender: true,
            children: <div className="response-tab-body">
            {report === null ? (
              <Hint>发送后，字段树对着**本次真实响应**展开，条件也就近配置在字段旁。</Hint>
            ) : null}
            {typeof fieldsTab === "function" ? fieldsTab(tab === "fields") : fieldsTab}
            </div>,
          },
          {
            key: "headers",
            label: "响应头",
            forceRender: true,
            children: <div className="response-tab-body">{
          evidence === null ? (
            <Hint>本次没有响应头可显示。</Hint>
          ) : evidence.headers.length === 0 ? (
            <Hint>目标没有返回响应头。</Hint>
          ) : (
            <ul className="caption">
              {evidence.headers.map((item, index) => (
                <li key={`${item.name}-${index}`}>
                  {item.name}: {item.value}
                </li>
              ))}
            </ul>
          )
            }</div>,
          },
        ]}
      />
      </div>
    </section>
  );
}
