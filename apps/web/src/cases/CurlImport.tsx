/**
 * cURL 导入：只解析文本，不发送请求。
 *
 * 放在地址行的工具栏里，而不是排在响应与样例长表单后面——导入是新请求的第一步，用户
 * 找不到它就等于没有这个能力。展开后就地编辑，不弹独立窗口。
 *
 * 三条不变的行为约束：
 *
 * 1. **不自动发送**。解析成功只填入编辑器，是否发出去由用户点「发送」决定（INV-06）。
 * 2. **失败保留原文**。解析报错时输入框内容原样留着，用户可以改一处再试，不用重新粘贴。
 * 3. **不可等价的命令不写入草稿**。含不支持选项时明确拒绝导入，避免生成一条看起来能发
 *    送、实际语义不同的请求。
 */
import { useId, useState } from "react";
import { Button, Collapse, Input } from "antd";

import { ErrorText, Hint } from "../components/Feedback";
import { useLeaveReport } from "../hooks/leaveGuard";

export function CurlImport({
  onImport,
  disabled,
  loading,
  idPrefix = "",
  pendingKey,
}: {
  /** 解析并填入编辑器；返回错误信息表示未能导入。 */
  onImport: (text: string) => Promise<{ error: string | null; warnings: string[] }>;
  disabled: boolean;
  loading: boolean;
  idPrefix?: string;
  pendingKey?: string;
}) {
  const localId = useId();
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [imported, setImported] = useState(false);
  // 网络忙碌由 CaseEditor 的主登记负责；这里只登记未应用原文，避免保存完成帧的重复 busy。
  useLeaveReport(pendingKey ?? `curl:${localId}`, { dirty: text !== "" && !imported, busy: false });

  async function run() {
    setError(null);
    setWarnings([]);
    setImported(false);
    const result = await onImport(text);
    if (result.error !== null) {
      // 失败时**不动** text：用户改一个字符再试，不用重新粘贴整条命令。
      setError(result.error);
      setWarnings(result.warnings);
      return;
    }
    setWarnings(result.warnings);
    setImported(true);
    // 成功即收起，把注意力交还请求区；原文保留在下面的展开区里以便再次导入。
    setOpen(false);
  }

  return (
    <div className="curl-import">
      <Collapse
        activeKey={open ? ["curl"] : []}
        onChange={(keys) => setOpen(keys.includes("curl"))}
        items={[{ key: "curl", label: "导入 cURL", collapsible: disabled ? "disabled" : undefined, children: (
        <div className="curl-panel">
          <label htmlFor={`${idPrefix ? `${idPrefix}-` : ""}curl-text`}>粘贴 cURL 命令（只解析文本，不发送请求）</label>
          <Input.TextArea
            id={`${idPrefix ? `${idPrefix}-` : ""}curl-text`}
            rows={3}
            value={text}
            readOnly={disabled}
            placeholder="curl -X POST 'https://example.test/orders?tag=a&tag=b' -H 'Content-Type: application/json' -d '{...}'"
            onChange={(event) => { setText(event.target.value); setImported(false); }}
          />
          <div className="actions">
            <Button htmlType="button" type="primary" onClick={() => void run()} disabled={disabled || loading}>
              {loading ? "解析中…" : "解析并填入编辑器"}
            </Button>
          </div>
          {error ? <ErrorText message={error} /> : null}
          {imported ? <Hint>已填入编辑器；导入过程不访问目标，也未执行任何命令。</Hint> : null}
          {warnings.length > 0 ? (
            <ul className="caption">
              {warnings.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) }]} />
    </div>
  );
}
