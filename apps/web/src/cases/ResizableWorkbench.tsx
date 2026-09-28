import { useEffect, useRef, useState, type CSSProperties, type PointerEvent, type ReactNode } from "react";
import { Segmented } from "antd";

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
    const axisLength = direction === "horizontal" ? bounds.width : bounds.height;
    if (axisLength <= 0) return;
    const offset = direction === "horizontal" ? event.clientX - bounds.left : event.clientY - bounds.top;
    const next = Math.round((offset / axisLength) * 100);
    setRatio(Math.min(75, Math.max(25, next)));
  }

  return (
    <div className="workbench-layout">
      {controls ? <div className="workbench-controls">{controls}</div> : null}
      <div className="layout-toolbar" aria-label="工作区布局">
        <span className="caption">请求与响应布局</span>
        <Segmented<Direction>
          aria-label="请求与响应布局"
          value={direction}
          options={[{ label: "上下", value: "vertical" }, { label: "左右", value: "horizontal" }]}
          onChange={setDirection}
        />
      </div>
      <div
        ref={frame}
        className={`workbench-split-frame workbench-split workbench-${direction}`}
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
