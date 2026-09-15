/**
 * 后端字段树读取：把 JSON 原文交给服务端投影为带定位路径的字段树。
 *
 * 前端不自行解析正文：`JSON.parse` 会让长整数失真，而字段树正是用户点选字段、
 * 生成断言定位路径的入口。因此这里只传原文，由后端用无损解析生成树。
 * 输入变化做短延迟合并，避免每敲一个字符就发一次请求。
 */
import { useEffect, useRef, useState } from "react";

import { ApiError, apiSend, projectPath } from "../api/client";
import { toFieldTree } from "../api/guards";
import type { FieldTree } from "../api/types";

const DEBOUNCE_MS = 400;

export interface FieldTreeState {
  tree: FieldTree | null;
  error: string | null;
  loading: boolean;
}

export function useFieldTree(
  workspaceId: string | null,
  projectId: string | null,
  text: string,
  /**
   * 这份正文的来源标识（哪条运行／哪份样例）。
   *
   * 与正文一起构成“这棵树属于哪份数据”。换来源时旧树必须**立即**停止可交互：只保留旧树
   * 并置 loading，用户在等待期间仍能点到上一份来源的节点，而那棵树的类型与值与当前完全
   * 无关——用它配出来的条件是照着另一份响应写的。
   */
  sourceKey: string,
): FieldTreeState {
  const inputKey = `${sourceKey}\u0000${text}`;
  /**
   * 树与**产生它的输入**一起保存。
   *
   * 只存 `{tree, loading}` 时，“这份树属于哪个输入”只能靠外部猜测；结果就是上面那种
   * 跨来源使用。把 key 和结果绑在一起，过期与否是一个直接的比较。
   */
  const [state, setState] = useState<{ key: string; tree: FieldTree | null; error: string | null; loading: boolean }>(
    () => ({
      key: inputKey,
      tree: null,
      error: null,
      // 有正文时初始即“加载中”：否则第一帧会短暂显示“正文为空或不是合法 JSON”。
      loading: text.trim().length > 0,
    }),
  );
  const latest = useRef(0);

  useEffect(() => {
    if (!workspaceId || !projectId || !text.trim()) {
      setState({ key: inputKey, tree: null, error: null, loading: false });
      return;
    }
    const ticket = latest.current + 1;
    latest.current = ticket;
    const controller = new AbortController();
    // 换输入时**不保留旧树**：它属于另一个输入，留着就还有被点到的机会（见上面的说明）。
    setState({ key: inputKey, tree: null, error: null, loading: true });

    const timer = window.setTimeout(() => {
      void (async () => {
        try {
          const tree = await apiSend(
            projectPath(workspaceId, projectId, "/field-tree"),
            "POST",
            { text },
            toFieldTree,
            { signal: controller.signal },
          );
          // 请求发出后正文可能又被改动：只接受最后一次请求的结果。
          if (latest.current === ticket && !controller.signal.aborted) {
            setState({ key: inputKey, tree, error: null, loading: false });
          }
        } catch (cause) {
          if (latest.current !== ticket || controller.signal.aborted) return;
          if (cause instanceof DOMException && cause.name === "AbortError") return;
          setState({
            key: inputKey,
            tree: null,
            error: cause instanceof ApiError ? cause.message : "字段树展开失败，请检查正文格式",
            loading: false,
          });
        }
      })();
    }, DEBOUNCE_MS);

    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [workspaceId, projectId, text, sourceKey, inputKey]);

  // 门控：只有与当前输入匹配的那棵树可以交互。key 对不上时返回加载态，而不是把上一份
  // 来源的树交出去——那正是“在 r2 未到时点 r1 节点试算”的入口。
  if (state.key !== inputKey) {
    return { tree: null, error: null, loading: true };
  }
  return { tree: state.tree, error: state.error, loading: state.loading };
}
