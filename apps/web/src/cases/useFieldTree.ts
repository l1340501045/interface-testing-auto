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
): FieldTreeState {
  const [state, setState] = useState<FieldTreeState>({ tree: null, error: null, loading: false });
  const latest = useRef(0);

  useEffect(() => {
    if (!workspaceId || !projectId || !text.trim()) {
      setState({ tree: null, error: null, loading: false });
      return;
    }
    const ticket = latest.current + 1;
    latest.current = ticket;
    const controller = new AbortController();
    setState((previous) => ({ tree: previous.tree, error: null, loading: true }));

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
            setState({ tree, error: null, loading: false });
          }
        } catch (cause) {
          if (latest.current !== ticket || controller.signal.aborted) return;
          if (cause instanceof DOMException && cause.name === "AbortError") return;
          setState({
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
  }, [workspaceId, projectId, text]);

  return state;
}
