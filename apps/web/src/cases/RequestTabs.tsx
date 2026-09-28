/**
 * 请求编辑标签：参数、认证、请求头、请求体、断言。
 *
 * 用 `hidden` 隐藏非当前标签而不是卸载它们：卸载会连输入框一起销毁，光标位置、选中的
 * 文本、正在编辑但还没提交的断言表单全部丢失。用户切一次标签看一眼请求头，回来发现
 * 请求体里的排版被重置，会以为内容丢了。因此五个面板都保持在 DOM 里，只切换可见性。
 *
 * 键盘可用是硬要求：标签栏按 WAI-ARIA 的 tabs 模式实现左右方向键与 Home／End，
 * 焦点只在标签之间移动，面板本身不参与 Tab 序列的额外跳转。
 */
import { Tabs } from "antd";
import { useEffect, useRef, type ReactNode } from "react";

export interface RequestTab {
  id: string;
  label: string;
  /** 该标签上需要注意的状态（例如认证缺配置），用文字而不只是颜色表达。 */
  badge?: string | null;
  /**
   * 已配置条数。只写数字，不写“有”这类没有信息量的词：用户想知道的是“配了几条”，
   * 而不是“这里不是空的”。
   */
  summary?: number | null;
  content: ReactNode;
}

export function RequestTabs({
  tabs,
  activeId,
  onChange,
  idPrefix = "request",
}: {
  tabs: RequestTab[];
  activeId: string;
  onChange: (id: string) => void;
  idPrefix?: string;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    rootRef.current?.querySelector<HTMLElement>('[role="tablist"]')?.setAttribute("aria-label", "请求编辑");
  }, [tabs.length]);

  return (
    <div ref={rootRef} className="request-tabs">
      <Tabs
        id={idPrefix}
        aria-label="请求编辑"
        activeKey={activeId}
        onChange={onChange}
        destroyOnHidden={false}
        items={tabs.map((tab) => ({
          key: tab.id,
          forceRender: true,
          label: (
            <span>
              {tab.label}
              {tab.summary ? <span className="tab-badge">{tab.summary}</span> : null}
              {tab.badge ? <span className="tab-badge">{tab.badge}</span> : null}
            </span>
          ),
          children: <div className="tab-panel">{tab.content}</div>,
        }))}
      />
    </div>
  );
}
