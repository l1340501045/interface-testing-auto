/**
 * 通用反馈组件：加载、空结果、失败与状态文字。
 *
 * 颜色之外必须有文字：色觉差异或灰度打印下，仅靠红绿无法判断结果，
 * 因此每个状态都带明确中文标签。
 */
import type { ReactNode } from "react";

export function Loading({ label = "正在加载…" }: { label?: string }) {
  return (
    <p className="hint" role="status">
      {label}
    </p>
  );
}

export function Empty({ label }: { label: string }) {
  return <p className="hint">{label}</p>;
}

export function ErrorText({ message }: { message: string }) {
  return (
    <p className="error" role="alert">
      {message}
    </p>
  );
}

export function Hint({ children }: { children: ReactNode }) {
  return <p className="hint">{children}</p>;
}

export function Notice({
  tone,
  title,
  children,
}: {
  tone: "info" | "warning";
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className={tone === "warning" ? "notice notice-warning" : "notice"} role="status">
      <strong>{title}</strong>
      {children ? <div>{children}</div> : null}
    </div>
  );
}

/** 运行与断言结果的状态标签：文字优先，颜色其次。 */
export function StatusTag({ status }: { status: string }) {
  const label =
    status === "passed"
      ? "通过"
      : status === "failed"
        ? "失败"
        : status === "error"
          ? "配置或执行错误"
          : status === "skipped"
            ? "未执行"
            : status;
  const tone =
    status === "passed" ? "tag-ok" : status === "failed" ? "tag-bad" : status === "skipped" ? "tag-idle" : "tag-warn";
  return <span className={`tag ${tone}`}>{label}</span>;
}
