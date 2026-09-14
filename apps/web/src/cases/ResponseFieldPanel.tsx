/**
 * 出参（响应正文字段）断言面板。
 *
 * 响应正文的字段路径来自一棵字段树，页面会明确显示当前用的是哪个来源：
 * - 默认用最近一次运行的真实响应正文，即“本次实际收到的正文”；
 * - 没有运行记录时用用户粘贴的响应样例，仅用于配置与试算，不代表真实接口。
 *
 * 两种来源都只把正文原文交给后端字段树接口做无损投影，前端不解析 JSON，
 * 因此长整数与小数不会在点选字段时失真。
 *
 * 树里没有的已有出参断言不会被隐藏：样例只帮助配置，真实核对发生在执行时，
 * 若把树当成唯一真相，用户保存过的条件会在页面上消失，而它其实仍在执行。
 * 这类条件仍逐条显示并可修改删除，页面文案标明“按实际运行结果核对”。
 */
import { useMemo, useState } from "react";

import type {
  AssertionResult,
  AssertionType,
  CaseAssertion,
  LocatorStep,
  RunReport,
} from "../api/types";
import { Hint } from "../components/Feedback";
import { AssertionColumn, fieldKey } from "./AssertionColumn";
import { literalTypeOf } from "./assertionModel";
import {
  formatSelector,
  groupByField,
  newAssertionId,
  nextSortOrder,
  parsePathInput,
  removeAssertion,
  upsertAssertion,
} from "./assertionGroups";
import { containsSelector, FieldTreePanel } from "./FieldTreePanel";
import { useFieldTree } from "./useFieldTree";

/** 字段类型选项，与后端字段树投影出的类型同名。 */
const FIELD_TYPES = ["string", "number", "integer", "boolean", "object", "array", "null"];

/** 只能用于数值字段的检查类型；其余默认按字符串呈现。 */
const NUMBER_ONLY_TYPES = new Set([
  "greater_than",
  "greater_or_equal",
  "less_than",
  "less_or_equal",
  "range",
  "not_range",
]);

/** 已保存字面量类型 → 界面里的字段类型名。 */
const LITERAL_FIELD_TYPES: Record<string, string> = {
  number: "number",
  boolean: "boolean",
  null: "null",
  json: "object",
  string: "string",
};

/**
 * 字段类型只能从样例树推断，而“不在树里的字段”没有样例可用。
 *
 * 这时**已保存的字面量是唯一的类型证据**：一条 `{type:"boolean"}` 的期望值说明这个
 * 字段是布尔字段。若一律退回“字符串”，用户看到的是错的字段类型，可选检查类型的范围
 * 也跟着错（数值区间不会出现在列表里），且界面上没有任何迹象说明类型是猜的。
 * 证据不唯一时（同一字段上既有数字又有文本条件）才退回原先的保守判断。
 */
function inferFieldType(items: CaseAssertion[]): string {
  const kinds = new Set<string>();
  for (const item of items) {
    const params = item.parameters;
    if (typeof params !== "object" || params === null) continue;
    for (const value of Object.values(params as Record<string, unknown>)) {
      for (const raw of Array.isArray(value) ? value : [value]) {
        const type = literalTypeOf(raw);
        if (type !== null) kinds.add(LITERAL_FIELD_TYPES[type] ?? "string");
      }
    }
  }
  if (kinds.size === 1) return [...kinds][0];
  return items.some((item) => NUMBER_ONLY_TYPES.has(item.type)) ? "number" : "string";
}

/** 从最近一次运行报告中取出响应正文原文；没有可用的正文时返回 null。 */
function latestResponseBody(report: RunReport | null): string | null {
  const body = report?.response?.body;
  return typeof body === "string" && body.trim() !== "" ? body : null;
}

/**
 * 不在样例树中的出参断言，按字段聚成一行。
 *
 * 同一个字段上可以有多条条件（存在、非空、开头…），必须一起显示、一起增删改：
 * 若每个条件各占一行、各自触发整字段替换，删除其中一条就会把同字段的其他条件
 * 一起丢掉。字段类型无从推断，因此由用户显式选择；选错只影响可选检查类型的范围，
 * 不会改写已保存的条件本身。
 */
function OutsideTreeRow({
  targetSource,
  selector,
  items,
  types,
  typesError,
  workspaceId,
  projectId,
  results,
  readOnly,
  onUpsert,
  onRemove,
}: {
  targetSource: CaseAssertion["target_source"];
  selector: LocatorStep[];
  items: CaseAssertion[];
  types: AssertionType[];
  typesError: string | null;
  workspaceId: string;
  projectId: string;
  results: Map<string, AssertionResult>;
  readOnly: boolean;
  onUpsert: (next: CaseAssertion) => void;
  onRemove: (id: string) => void;
}) {
  const [fieldType, setFieldType] = useState(() => inferFieldType(items));
  const typeSelectId = `outside-type-${fieldKey(targetSource, selector)}`;

  return (
    <div className="response-field">
      <div className="response-field-head">
        <strong>响应正文 {formatSelector(selector)}</strong>
        <span className="param">
          <label htmlFor={typeSelectId}>字段类型</label>
          <select
            id={typeSelectId}
            value={fieldType}
            disabled={readOnly}
            onChange={(event) => setFieldType(event.target.value)}
          >
            {FIELD_TYPES.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </span>
      </div>
      <AssertionColumn
        types={types}
        typesError={typesError}
        workspaceId={workspaceId}
        projectId={projectId}
        field={{ targetSource, selector, fieldType }}
        own={items}
        sample={null}
        results={results}
        readOnly={readOnly}
        onUpsert={onUpsert}
        onRemove={onRemove}
      />
    </div>
  );
}

export function ResponseFieldPanel({
  workspaceId,
  projectId,
  report,
  types,
  typesError,
  assertions,
  results,
  readOnly,
  onChange,
}: {
  workspaceId: string;
  projectId: string;
  /** 最近一次运行报告；有真实响应时优先于粘贴样例。 */
  report: RunReport | null;
  types: AssertionType[];
  typesError: string | null;
  assertions: CaseAssertion[];
  results: Map<string, AssertionResult>;
  readOnly: boolean;
  onChange: (next: CaseAssertion[]) => void;
}) {
  const [sample, setSample] = useState("");
  const [manualPath, setManualPath] = useState("");

  const runBody = latestResponseBody(report);
  const usingRun = runBody !== null;
  const tree = useFieldTree(workspaceId, projectId, usingRun ? runBody : sample);
  const root = tree.tree?.root ?? null;

  const manualSelector = manualPath.trim() === "" ? [] : parsePathInput(manualPath);
  const manualInvalid = manualSelector === null;

  // 不在当前字段树中的出参断言：没有样例时它就是全部出参断言。按字段聚合，
  // 同一字段上的多条条件在同一行内增删改，删除一条不会牵连同字段的其他条件。
  const outsideTree = useMemo(() => {
    const items = assertions.filter((item) => item.target_source === "response.body");
    const missing =
      root === null
        ? items
        : items.filter((item) => !containsSelector(root, JSON.stringify(item.selector)));
    const groups = groupByField(missing);
    return [...groups.entries()]
      .map(([key, entries]) => ({ key, entries, head: entries[0] }))
      .sort((a, b) => a.head.sort_order - b.head.sort_order);
  }, [assertions, root]);

  function addExpectedField() {
    if (manualSelector === null) return;
    onChange(
      upsertAssertion(assertions, {
        id: newAssertionId(),
        target_source: "response.body",
        selector: manualSelector,
        // 先建出一条存在性条件占位，随后在下方该字段行改成需要的检查。
        type: "exists",
        parameters: {},
        compare_as: null,
        severity: "error",
        enabled: true,
        sort_order: nextSortOrder(assertions),
      }),
    );
    setManualPath("");
  }

  return (
    <div className="block">
      <h2>出参字段断言</h2>
      <p className="caption">
        {usingRun
          ? "字段来自最近一次运行的真实响应正文；这里配置的条件在收到本次响应后检查。"
          : "先粘贴一份响应样例以展开字段；样例只用于配置和试算，执行时仍按本次真实响应核对。"}
      </p>

      {usingRun ? (
        <Hint>已使用最近一次运行的响应正文；没有运行记录时才使用粘贴样例。</Hint>
      ) : (
        <div className="body-editor">
          <label htmlFor="response-sample">响应样例</label>
          <textarea
            id="response-sample"
            rows={5}
            value={sample}
            placeholder='例如 {"code": 0, "data": {"name": "abc"}}'
            onChange={(event) => setSample(event.target.value)}
          />
          <p className="caption">
            样例按原文交给后端展开字段树，长整数与小数不会经过前端解析而失真。
          </p>
        </div>
      )}

      <FieldTreePanel
        title="响应正文字段"
        tree={tree}
        targetSource="response.body"
        types={types}
        typesError={typesError}
        workspaceId={workspaceId}
        projectId={projectId}
        assertions={assertions}
        results={results}
        readOnly={readOnly}
        onChange={onChange}
        emptyHint={
          usingRun
            ? "本次响应正文为空或不是 JSON，无法展开字段；可用下方“添加预期字段”按路径指定。"
            : "粘贴响应样例后，可在这里按字段配置断言；也可以用下方的“添加预期字段”。"
        }
      />

      <div className="kv-block">
        <h3>添加预期字段</h3>
        <p className="caption">
          没有响应样例时按路径逐级填写，待实际运行后核对；不自动调用接口补样例。
        </p>
        <div className="actions">
          <span className="param grow">
            <label htmlFor="manual-field-path">字段路径</label>
            <input
              id="manual-field-path"
              value={manualPath}
              placeholder="例如 data.name 或 items[0].price"
              onChange={(event) => setManualPath(event.target.value)}
            />
          </span>
          <button
            type="button"
            disabled={readOnly || manualInvalid}
            onClick={addExpectedField}
          >
            添加预期字段
          </button>
        </div>
        {manualInvalid ? (
          <p className="caption">
            路径无法完整解析，请写成 data.name 或 items[0].price 这样的形式；留空表示响应正文根。
          </p>
        ) : null}
      </div>

      {outsideTree.length > 0 ? (
        <div className="field-tree">
          <div className="field-tree-head">
            <strong>不在样例树中的条件</strong>
            <span className="caption">共 {outsideTree.length} 条</span>
          </div>
          <p className="caption">
            这些条件不在上面的字段树里，仍会按实际运行结果核对；可以在这里修改或删除。
          </p>
          {outsideTree.map((group) => (
            <OutsideTreeRow
              key={group.key}
              targetSource={group.head.target_source}
              selector={group.head.selector}
              items={group.entries}
              types={types}
              typesError={typesError}
              workspaceId={workspaceId}
              projectId={projectId}
              results={results}
              readOnly={readOnly}
              onUpsert={(next) => onChange(upsertAssertion(assertions, next))}
              onRemove={(id) => onChange(removeAssertion(assertions, id))}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}
