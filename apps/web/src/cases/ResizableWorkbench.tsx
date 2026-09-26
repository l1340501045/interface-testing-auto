import { useEffect, useRef, useState, type CSSProperties, type PointerEvent, type ReactNode } from "react";

type Direction = "vertical" | "horizontal";

const DIRECTION_KEY = "interface-workbench-direction";
const RATIO_KEY = "interface-workbench-ratio";

function initialDirection(): Direction {
  return window.localStorage.getItem(DIRECTION_KEY) === "horizontal" ? "horizontal" : "vertical";
}

function initialRatio(): number {
  const value = Number(window.localStorage.getItem(RATIO_KEY));
  return Number.isFinite(value) && value >= 25 && value <= 75 ? value : 50;
}

export function ResizableWorkbench({
  controls,
  request,
  response,
}: {
  /** 地址、环境和发送等关键操作；始终位于可调分栏之外，避免被较小的请求区裁掉。 */
  controls?: ReactNode;
  request: ReactNode;
  response: ReactNode;
}) {
  const [direction, setDirection] = useState<Direction>(initialDirection);
  const [ratio, setRatio] = useState(initialRatio);
  const frame = useRef<HTMLDivElement>(null);

  useEffect(() => window.localStorage.setItem(DIRECTION_KEY, direction), [direction]);
  useEffect(() => window.localStorage.setItem(RATIO_KEY, String(ratio)), [ratio]);

  function updateFromPointer(event: PointerEvent<HTMLDivElement>) {
    const bounds = frame.current?.getBoundingClientRect();
    if (!bounds) return;
    const raw = direction === "horizontal"
      ? ((event.clientX - bounds.left) / bounds.width) * 100
      : ((event.clientY - bounds.top) / bounds.height) * 100;
    setRatio(Math.min(75, Math.max(25, Math.round(raw))));
  }

  return (
    <div className="workbench-layout">
      {controls ? <div className="workbench-controls">{controls}</div> : null}
      <div className="layout-toolbar" aria-label="工作区布局">
        <span className="caption">请求与响应布局</span>
        <button
          type="button"
          className={direction === "vertical" ? "layout-choice active" : "layout-choice"}
          aria-pressed={direction === "vertical"}
          onClick={() => setDirection("vertical")}
        >上下</button>
        <button
          type="button"
          className={direction === "horizontal" ? "layout-choice active" : "layout-choice"}
          aria-pressed={direction === "horizontal"}
          onClick={() => setDirection("horizontal")}
        >左右</button>
      </div>
      <div
        ref={frame}
        className={`workbench-split workbench-${direction}`}
        style={{ "--split-ratio": `${ratio}%` } as CSSProperties}
      >
        <div className="workbench-request">{request}</div>
        <div
          className="workbench-divider"
          role="separator"
          tabIndex={0}
          aria-label="调整请求与响应区域大小"
          aria-orientation={direction === "horizontal" ? "vertical" : "horizontal"}
          aria-valuemin={25}
          aria-valuemax={75}
          aria-valuenow={ratio}
          onDoubleClick={() => setRatio(50)}
          onPointerDown={(event) => {
            event.currentTarget.setPointerCapture(event.pointerId);
            updateFromPointer(event);
          }}
          onPointerMove={(event) => {
            if (event.currentTarget.hasPointerCapture(event.pointerId)) updateFromPointer(event);
          }}
          onKeyDown={(event) => {
            const backward = event.key === "ArrowLeft" || event.key === "ArrowUp";
            const forward = event.key === "ArrowRight" || event.key === "ArrowDown";
            if (!backward && !forward && event.key !== "Home") return;
            event.preventDefault();
            setRatio((current) => event.key === "Home" ? 50 : Math.min(75, Math.max(25, current + (forward ? 5 : -5))));
          }}
        />
        <div className="workbench-response">{response}</div>
      </div>
    </div>
  );
}
