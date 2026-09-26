/**
 * 断言标签的内容：固定响应断言行、全部条件汇总，以及请求正文字段树。
 *
 * 从 CaseEditor 里拆出来的原因与布局直接相关：这些是**面板内部**的编辑控件，原先
 * 常驻在请求区外面、把响应推到长表单末尾。它们只接收断言与结果、通过回调提交变更，
 * 不持有草稿状态——草稿与保存编排仍留在 CaseEditor。
 *
 * 字段旁的配置入口是主路径（DW-04／design 2.3）：响应字段与请求正文字段各自带一列
 * 断言编辑，这里的“全部条件”只是汇总视图，不是要求用户先建公共规则。
 */
import type { AssertionResult, AssertionType, CaseAssertion } from "../api/types";
import { Hint } from "../components/Feedback";
import { AssertionColumn } from "./AssertionColumn";
import { FieldTreePanel } from "./FieldTreePanel";
import { fieldAssertions, groupByField, removeAssertion, upsertAssertion } from "./assertionGroups";
import type { FieldTreeState } from "./useFieldTree";
import type { RawRequest } from "./requestDraft";

/** 固定的响应断言行：状态码与耗时无需样例即可配置。 */
export const RESPONSE_FIELDS = [
  { label: "状态码", target: "response.status" as const, selector: [], fieldType: "integer" },
  { label: "耗时（毫秒）", target: "response.elapsed" as const, selector: [], fieldType: "integer" },
];

export function AssertionTab({
  workspaceId,
  projectId,
  types,
  typesError,
  assertions,
  results,
  readOnly,
  onChange,
  bodyTree,
  bodySourceKey,
  bodyHint,
  pendingPrefix,
  request,
}: {
  workspaceId: string;
  projectId: string;
  types: AssertionType[];
  typesError: string | null;
  assertions: CaseAssertion[];
  results: Map<string, AssertionResult>;
  readOnly: boolean;
  onChange: (next: CaseAssertion[]) => void;
  /** 请求正文字段树状态；正文不是 JSON 或为空时 tree 为 null。 */
  bodyTree: FieldTreeState;
  /** 请求正文字段树的来源标识（即正文本身）；换代时清掉选中节点。 */
  bodySourceKey: string;
  bodyHint: string;
  pendingPrefix?: string;
  request: RawRequest;
}) {
  // 按字段分组复用既有实现：它同时负责同字段条件按 sort_order 排序，自己再滤一遍
  // 会得到第二份顺序规则，删改之后两边就可能不一致。
  const groups = groupByField(assertions);

  return (
    <>
      <h3>固定响应断言</h3>
      <p className="caption">状态码与耗时无需样例即可配置，执行时按本次响应核对。</p>
      {RESPONSE_FIELDS.map((field) => (
        <div className="response-field" key={field.target}>
          <div className="response-field-head">
            <strong>{field.label}</strong>
            <span className="field-type">{field.fieldType}</span>
          </div>
          <AssertionColumn
            types={types}
            typesError={typesError}
            workspaceId={workspaceId}
            projectId={projectId}
            field={{
              targetSource: field.target,
              selector: field.selector,
              fieldType: field.fieldType,
            }}
            own={fieldAssertions(groups, field.target, field.selector)}
            sample={null}
            results={results}
            readOnly={readOnly}
            onUpsert={(next) => onChange(upsertAssertion(assertions, next))}
            onRemove={(id) => onChange(removeAssertion(assertions, id))}
            pendingKey={pendingPrefix ? `${pendingPrefix}:assertion-${field.target}` : undefined}
          />
        </div>
      ))}

      <h3>请求正文字段断言</h3>
      <p className="caption">发送前检查；失败时不会发出 HTTP 请求。</p>
      <FieldTreePanel
        title="请求正文字段"
        tree={bodyTree}
        // 请求正文一变，这棵树就是另一份数据：选中节点随之清掉。
        sourceKey={bodySourceKey}
        targetSource="request.body"
        types={types}
        typesError={typesError}
        workspaceId={workspaceId}
        projectId={projectId}
        assertions={assertions}
        results={results}
        readOnly={readOnly}
        onChange={onChange}
        emptyHint={bodyHint}
        pendingPrefix={pendingPrefix}
      />

      <h3>全部条件</h3>
      <p className="caption">条件属于当前用例；字段旁的配置入口是主路径，这里是汇总视图。</p>
      <AssertionSummary assertions={assertions} results={results} request={request} readOnly={readOnly} onChange={onChange} />
    </>
  );
}

/** 断言标签里的汇总：按字段分组列出当前用例的全部条件与最近一次结论。 */
function AssertionSummary({
  assertions,
  results,
  request,
  readOnly,
  onChange,
}: {
  assertions: CaseAssertion[];
  results: Map<string, AssertionResult>;
  request: RawRequest;
  readOnly: boolean;
  onChange: (next: CaseAssertion[]) => void;
}) {
  if (assertions.length === 0) {
    return (
      <Hint>
        还没有配置断言。可在响应区的字段旁，或请求体的字段树里直接添加条件；
        这些条件属于当前用例，不需要先创建公共规则。
      </Hint>
    );
  }
  return (
    <table className="report-table">
      <thead>
        <tr>
          <th>字段</th>
          <th>类型</th>
          <th>严重级别</th>
          <th>最近结论</th>
          <th>处理</th>
        </tr>
      </thead>
      <tbody>
        {assertions.map((item) => {
          const first = item.selector[0];
          const rows = item.target_source === "request.query"
            ? request.query_params
            : item.target_source === "request.header"
              ? request.headers
              : [];
          const orphaned = first?.kind === "row" && !rows.some((row) => row.row_id === first.row_id);
          const canRebind = first?.kind === "row" && (item.target_source === "request.query" || item.target_source === "request.header");
          return (
          <tr key={item.id}>
            <td>
              {item.target_source}
              {item.selector.length > 0 ? ` · ${item.selector.length} 级定位` : ""}
              {orphaned ? <strong className="field-warn"> · 字段已删除</strong> : null}
              {first && first.kind !== "row" && (item.target_source === "request.query" || item.target_source === "request.header") ? <span className="caption"> · 历史位置条件</span> : null}
            </td>
            <td>{item.type}</td>
            <td>{item.severity === "error" ? "必需" : "提示"}</td>
            <td>{assertionResultLabel(results.get(item.id))}</td>
            <td>
              {readOnly ? null : (
                <span className="inline-actions">
                  {orphaned && canRebind ? (
                    <label>
                      重新绑定
                      <select
                        aria-label={`重新绑定条件 ${item.id}`}
                        value=""
                        onChange={(event) => {
                          const rowId = event.target.value;
                          if (rowId === "" || first?.kind !== "row") return;
                          onChange(assertions.map((assertion) => assertion.id === item.id ? { ...assertion, selector: [{ kind: "row", row_id: rowId }, ...assertion.selector.slice(1)] } : assertion));
                        }}
                      >
                        <option value="">选择当前字段…</option>
                        {rows.map((row, index) => row.row_id ? (
                          <option key={row.row_id} value={row.row_id}>
                            {index + 1}. {row.name || "未命名字段"}{row.enabled === false ? "（已停用）" : ""}
                          </option>
                        ) : null)}
                      </select>
                    </label>
                  ) : null}
                  <button type="button" onClick={() => onChange(removeAssertion(assertions, item.id))}>删除</button>
                </span>
              )}
            </td>
          </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function assertionResultLabel(result: AssertionResult | undefined): string {
  if (result === undefined) return "本次未执行";
  if (result.status === "passed") return "通过";
  if (result.status === "failed") return result.reason_code ?? "未通过";
  if (result.status === "skipped") {
    return result.reason_code === "stale_run" ? "与当前内容不符，未执行" : "未执行";
  }
  return result.reason_code ?? result.status;
}
