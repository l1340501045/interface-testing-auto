import type { KeyboardEvent as ReactKeyboardEvent } from "react";

/**
 * 补足 Ant Modal 焦点锁在浏览器末端 Tab 不触发 focusin 时的缺口。
 *
 * 只处理首尾两个端点；中间顺序、Escape、遮罩和关闭后的回焦仍由 Modal 负责。
 */
export function trapModalTabEndpoints(event: ReactKeyboardEvent<HTMLDivElement>): void {
  if (event.key !== "Tab") return;
  const dialog = event.currentTarget.querySelector<HTMLElement>('[role="dialog"]');
  if (dialog === null) return;
  const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(
    'button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]',
  )).filter((item) => item.getAttribute("aria-hidden") !== "true" && item.closest('[hidden], [style*="display: none"]') === null);
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (first === undefined || last === undefined) return;
  if ((!event.shiftKey && document.activeElement === last) || (event.shiftKey && document.activeElement === first)) {
    event.preventDefault();
    (event.shiftKey ? last : first).focus();
  }
}
