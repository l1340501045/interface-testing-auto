/**
 * 字段树面板：展开请求或响应正文，每个字段行旁直接添加断言。
 *
 * 正文由后端无损投影为字段树（见 useFieldTree），节点自带与执行内核一致的
 * 定位路径；这里只负责渲染与选择，不自行解析 JSON，也不改写定位路径。
 */
import { useEffect, useMemo, useState } from "react";
import { Tag, Tree, type TreeDataNode } from "antd";

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

/**
 * 按定位路径在当前字段树里找出对应节点。
 *
 * 图表里存**路径**而不是节点对象：树会因为来源变化而重建，同一条路径在新树里可能是
 * 不同的类型与值。留着旧对象继续用它试算，就会拿上一份响应的样例去算当前配置的条件——
 * 用户看到的是一个从未存在过的匹配结果。
 */
export function findSelector(node: FieldNode, key: string): FieldNode | null {
  if (JSON.stringify(node.selector) === key) return node;
  for (const child of node.children) {
    const found = findSelector(child, key);
    if (found !== null) return found;
  }
  return null;
}

function toTreeNode(node: FieldNode): TreeDataNode {
  const key = JSON.stringify(node.selector);
  return {
    key,
    title: (
      <span className="field-row">
        <span className="field-name">{node.label}</span>
        <Tag className="field-type">{node.type}</Tag>
        <span className="field-value">{node.text}</span>
        {node.truncated ? <span className="field-warn">{node.truncated}</span> : null}
      </span>
    ),
    children: node.children.map(toTreeNode),
  };
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
  sourceKey,
  pendingPrefix,
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
  /**
   * 这棵树的数据来源标识（哪条运行／哪份样例）。
   *
   * 换代时清掉选中节点：路径可能碰巧还在，但它指向的是另一份数据，继续用它试算等于
   * 拿上一份响应算当前条件。
   */
  sourceKey: string;
  pendingPrefix?: string;
}) {
  /**
   * 当前选中的字段：只记**定位路径**，节点对象每次从当前树里重新解析。
   *
   * 记对象会留下上一棵树的类型与值：同一路径在新响应里变成另一种类型时，详情区仍按旧
   * 节点渲染，试算用的也是旧样例。路径是稳定的身份，节点内容是每次现取的。
   */
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const groups = useMemo(() => groupByField(assertions), [assertions]);

  const root = tree.tree?.root ?? null;
  const active = useMemo(
    () => (activeKey === null || root === null ? null : findSelector(root, activeKey)),
    [root, activeKey],
  );
  const treeData = useMemo(() => root === null ? [] : [toTreeNode(root)], [root]);

  // 路径在新树里已不存在（例如换了一份结构不同的响应）时收起详情，
  // 避免继续对一棵旧树的路径新增断言。
  useEffect(() => {
    if (activeKey === null || root === null) return;
    if (!containsSelector(root, activeKey)) setActiveKey(null);
  }, [root, activeKey]);

  // 来源换代（换运行／换样例）时清掉选中：即使路径碰巧还在，它指向的也是另一份数据。
  useEffect(() => {
    setActiveKey(null);
  }, [sourceKey]);

  if (tree.error) return <ErrorText message={tree.error} />;
  if (tree.loading && tree.tree === null) return <Loading label="正在展开字段树…" />;
  if (tree.tree === null) return <Empty label={emptyHint} />;

  return (
    <div className="field-tree">
      <div className="field-tree-head">
        <strong>{title}</strong>
        <span className="caption">共 {tree.tree.node_count} 个节点</span>
      </div>
      <Tree
        aria-label={title}
        className="tree"
        treeData={treeData}
        selectedKeys={activeKey === null ? [] : [activeKey]}
        defaultExpandedKeys={[JSON.stringify(tree.tree.root.selector)]}
        onSelect={(keys) => {
          // rc-tree 再次点击当前节点时会以空 keys 表示“取消选择”。对字段编辑器而言，
          // 这不是丢弃未应用断言的业务动作：保持当前 selector，直到用户切换字段、
          // 来源变化或新树里已经不存在该路径。
          const key = keys[0];
          if (key === undefined) return;
          setActiveKey(String(key));
        }}
      />

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
            pendingKey={pendingPrefix ? `${pendingPrefix}:assertion-${targetSource}-${activeKey ?? "field"}` : undefined}
          />
        </div>
      ) : (
        <Hint>点击字段名即可在该字段旁配置断言。字段类型为对象或数组时，请展开到具体子字段。</Hint>
      )}
    </div>
  );
}
