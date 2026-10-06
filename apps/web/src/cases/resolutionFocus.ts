export function scheduleEditorFieldFocus({
  root,
  elementId,
  ariaLabel,
  span,
  isCurrent,
  schedule = requestAnimationFrame,
}: {
  root: HTMLElement;
  elementId: string | null;
  ariaLabel: string | null;
  span?: { start: number; end: number };
  isCurrent: () => boolean;
  schedule?: (callback: FrameRequestCallback) => number;
}) {
  schedule(() => {
    if (!isCurrent() || !root.isConnected) return;
    const target = elementId
      ? root.querySelector<HTMLElement>(`[id="${elementId}"]`)
      : ariaLabel
        ? root.querySelector<HTMLElement>(`[aria-label="${ariaLabel}"], [aria-label="${ariaLabel}转义文本"]`)
        : null;
    if (!(target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement)) return;
    target.focus();
    if (span) target.setSelectionRange(span.start, span.end);
  });
}
