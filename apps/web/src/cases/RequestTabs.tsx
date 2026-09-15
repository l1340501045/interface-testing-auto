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
import { useRef, type KeyboardEvent, type ReactNode } from "react";

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
}: {
  tabs: RequestTab[];
  activeId: string;
  onChange: (id: string) => void;
}) {
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const index = tabs.findIndex((tab) => tab.id === activeId);
    if (index < 0) return;
    let next = index;
    if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
    else if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = tabs.length - 1;
    else return;
    event.preventDefault();
    const target = tabs[next];
    onChange(target.id);
    refs.current[target.id]?.focus();
  }

  return (
    <div className="request-tabs">
      <div className="tab-bar" role="tablist" aria-label="请求编辑" onKeyDown={onKeyDown}>
        {tabs.map((tab) => {
          const selected = tab.id === activeId;
          return (
            <button
              key={tab.id}
              type="button"
              role="tab"
              id={`request-tab-${tab.id}`}
              aria-selected={selected}
              aria-controls={`request-panel-${tab.id}`}
              tabIndex={selected ? 0 : -1}
              className={selected ? "tab tab-active" : "tab"}
              ref={(node) => {
                refs.current[tab.id] = node;
              }}
              onClick={() => onChange(tab.id)}
            >
              {tab.label}
              {tab.summary ? <span className="tab-badge">{tab.summary}</span> : null}
              {tab.badge ? <span className="tab-badge">{tab.badge}</span> : null}
            </button>
          );
        })}
      </div>
      {tabs.map((tab) => (
        <div
          key={tab.id}
          role="tabpanel"
          id={`request-panel-${tab.id}`}
          aria-labelledby={`request-tab-${tab.id}`}
          hidden={tab.id !== activeId}
          className="tab-panel"
        >
          {tab.content}
        </div>
      ))}
    </div>
  );
}
