/**
 * 通用反馈组件：加载、空结果、失败与状态文字。
 *
 * 颜色之外必须有文字：色觉差异或灰度打印下，仅靠红绿无法判断结果，
 * 因此每个状态都带明确中文标签。
 */
import { Alert, Empty as AntEmpty, Spin, Tag } from "antd";
import type { ReactNode } from "react";

export function Loading({ label = "正在加载…" }: { label?: string }) {
  return (
    <div className="feedback-loading" role="status">
      <Spin size="small" />
      <span>{label}</span>
    </div>
  );
}

export function Empty({ label }: { label: string }) {
  return <AntEmpty image={AntEmpty.PRESENTED_IMAGE_SIMPLE} description={label} />;
}

export function ErrorText({ message }: { message: string }) {
  return <Alert className="feedback-alert" type="error" title={message} showIcon role="alert" />;
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
  return <Alert className="feedback-alert" type={tone} title={title} description={children} showIcon role="status" />;
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
  const color = status === "passed" ? "success" : status === "failed" ? "error" : status === "skipped" ? "default" : "warning";
  return <Tag color={color}>{label}</Tag>;
}
