/**
 * 字段树面板：展开请求或响应正文，每个字段行旁直接添加断言。
 *
 * 正文由后端无损投影为字段树（见 useFieldTree），节点自带与执行内核一致的
 * 定位路径；这里只负责渲染与选择，不自行解析 JSON，也不改写定位路径。
 */
import { useEffect, useMemo, useState } from "react";

import type { AssertionResult, AssertionType, CaseAssertion, FieldNode, ValueLiteral } from "../api/types";
import { Empty, ErrorText, Hint, Loading } from "../components/Feedback";
import { AssertionColumn } from "./AssertionColumn";
import { fieldAssertions, formatSelector, groupByField, removeAssertion, upsertAssertion } from "./assertionGroups";
import type { FieldTreeState } from "./useFieldTree";

/** 把字段树的文本按节点类型转成试算样例；数字保持十进制文本。 */
function sampleOf(node: FieldNode): ValueLiteral | null {
  switch (node.type) {
    case "string":
      return { type: "string", text: node.text };
    case "number":
    case "integer":
      return { type: "number", text: node.text };
    case "boolean":
      return { type: "boolean", value: node.text === "true" };
    case "null":
      return { type: "null" };
    default:
      return null;
  }
}

/** 该定位路径（JSON 文本）是否出现在这棵树里。 */
export function containsSelector(node: FieldNode, key: string): boolean {
  if (JSON.stringify(node.selector) === key) return true;
  return node.children.some((child) => containsSelector(child, key));
}

function FieldRow({
  node,
  depth,
  activeKey,
  onSelect,
}: {
  node: FieldNode;
  depth: number;
  activeKey: string | null;
  onSelect: (node: FieldNode) => void;
}) {
  const key = JSON.stringify(node.selector);
  const hasChildren = node.children.length > 0;
  const [open, setOpen] = useState(depth < 1);
  return (
    <li>
      <div className={activeKey === key ? "field-row field-row-active" : "field-row"}>
        {hasChildren ? (
          <button
            type="button"
            className="field-toggle"
            aria-expanded={open}
            aria-label={`${open ? "收起" : "展开"}${node.label}`}
            onClick={() => setOpen((value) => !value)}
          >
            {open ? "▾" : "▸"}
          </button>
        ) : (
          <span className="field-toggle" aria-hidden="true" />
        )}
        <button type="button" className="field-name" onClick={() => onSelect(node)}>
          {node.label}
        </button>
        <span className="field-type">{node.type}</span>
        <span className="field-value">{node.text}</span>
        {node.truncated ? <span className="field-warn">{node.truncated}</span> : null}
      </div>
      {hasChildren && open ? (
        <ul>
          {node.children.map((child, index) => (
            <FieldRow
              key={`${child.label}-${index}`}
              node={child}
              depth={depth + 1}
              activeKey={activeKey}
              onSelect={onSelect}
            />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

export function FieldTreePanel({
  title,
  tree,
  targetSource,
  types,
  typesError,
  workspaceId,
  projectId,
  assertions,
  results,
  readOnly,
  onChange,
  emptyHint,
}: {
  title: string;
  tree: FieldTreeState;
  /** 该字段树对应的检查来源：请求正文为 request.body，响应正文为 response.body。 */
  targetSource: CaseAssertion["target_source"];
  types: AssertionType[];
  typesError: string | null;
  workspaceId: string;
  projectId: string;
  assertions: CaseAssertion[];
  results: Map<string, AssertionResult>;
  readOnly: boolean;
  onChange: (next: CaseAssertion[]) => void;
  emptyHint: string;
}) {
  const [active, setActive] = useState<FieldNode | null>(null);
  const groups = useMemo(() => groupByField(assertions), [assertions]);

  // 正文变化后字段树会重建，之前选中的节点可能已不存在；此时收起详情，
  // 避免继续对一棵旧树的路径新增断言。
  const activeKey = active ? JSON.stringify(active.selector) : null;
  const root = tree.tree?.root ?? null;
  useEffect(() => {
    if (activeKey === null || root === null) return;
    if (!containsSelector(root, activeKey)) setActive(null);
  }, [root, activeKey]);

  if (tree.error) return <ErrorText message={tree.error} />;
  if (tree.loading && tree.tree === null) return <Loading label="正在展开字段树…" />;
  if (tree.tree === null) return <Empty label={emptyHint} />;

  return (
    <div className="field-tree">
      <div className="field-tree-head">
        <strong>{title}</strong>
        <span className="caption">共 {tree.tree.node_count} 个节点</span>
      </div>
      <ul className="tree">
        <FieldRow node={tree.tree.root} depth={0} activeKey={activeKey} onSelect={setActive} />
      </ul>

      {active ? (
        <div className="field-detail">
          <p className="caption">
            当前字段：{active.label}（{active.type}）· 定位路径 {formatSelector(active.selector)}
          </p>
          <AssertionColumn
            types={types}
            typesError={typesError}
            workspaceId={workspaceId}
            projectId={projectId}
            field={{ targetSource, selector: active.selector, fieldType: active.type }}
            own={fieldAssertions(groups, targetSource, active.selector)}
            sample={sampleOf(active)}
            results={results}
            readOnly={readOnly}
            onUpsert={(next) => onChange(upsertAssertion(assertions, next))}
            onRemove={(id) => onChange(removeAssertion(assertions, id))}
          />
        </div>
      ) : (
        <Hint>点击字段名即可在该字段旁配置断言。字段类型为对象或数组时，请展开到具体子字段。</Hint>
      )}
    </div>
  );
}
