/**
 * 编辑器标题行：名称、目录、保存状态与保存／发布／关闭动作。
 *
 * 从 CaseEditor 里抽出来的原因不是“文件太长”，而是这一行与本轮的工具型布局有直接
 * 冲突：标题、目录、脏状态、保存与发布原本各自成段，占掉首屏一大块纵向空间，把地址行
 * 与发送按钮挤到下面。紧凑工具界面的要求是首屏就能看到地址、环境与发送，因此标题行
 * 必须收成一条。
 *
 * 这里只负责呈现与回调，不持有草稿状态：草稿、ETag、发布规则仍然由 CaseEditor 编排。
 */
import type { Folder } from "../api/types";
import { UNFILED_LABEL } from "./folderLabels";
import { Button, Input, Select, Space, Tag } from "antd";

export function CaseHeading({
  name,
  onNameChange,
  folderId,
  onFolderChange,
  folders,
  folderUnavailable,
  folderPlaceholder,
  dirty,
  busy,
  readOnly,
  creating,
  /** 尚未保存的新用例：保存动作叫“创建用例”，与已有用例的“保存草稿”区分开。 */
  isNew,
  versionCount,
  onSave,
  onPublish,
  onClose,
  idPrefix = "case",
}: {
  name: string;
  onNameChange: (next: string) => void;
  folderId: string | null;
  onFolderChange: (next: string | null) => void;
  folders: Folder[];
  /** 当前归属已不在可选清单里（被归档、被删或不属于本项目）。 */
  folderUnavailable: boolean;
  /** 失效目录占位选项的文案；加载中时不下“已失效”的结论。 */
  folderPlaceholder: string;
  dirty: boolean;
  busy: boolean;
  readOnly: boolean;
  creating: boolean;
  isNew: boolean;
  versionCount: number;
  onSave: () => void;
  onPublish: () => void;
  onClose: () => void;
  idPrefix?: string;
}) {
  return (
    <header className="case-head">
      <label className="visually-hidden" htmlFor={`${idPrefix}-name`}>
        用例名称
      </label>
      <Input
        id={`${idPrefix}-name`}
        className="case-name-input"
        value={name}
        readOnly={readOnly}
        placeholder="未命名用例"
        onChange={(event) => onNameChange(event.target.value)}
      />
      <label className="visually-hidden" htmlFor={`${idPrefix}-folder`}>
        所属目录
      </label>
      <Select
        id={`${idPrefix}-folder`}
        className="case-folder-select"
        value={folderId ?? ""}
        disabled={readOnly}
        aria-label="所属目录"
        data-selected-value={folderId ?? ""}
        onChange={(value: string) => onFolderChange(value === "" ? null : value)}
        options={[
          { value: "", label: UNFILED_LABEL },
          ...(folderUnavailable && folderId !== null ? [{ value: folderId, label: folderPlaceholder }] : []),
          ...folders.filter((item) => item.availability === "available").map((item) => ({ value: item.id, label: item.name })),
        ]}
      />

      {dirty ? <Tag color="warning">有未保存修改</Tag> : null}
      {creating ? <Tag>新用例</Tag> : null}
      {versionCount > 0 ? <Tag color="blue">已发布 {versionCount} 版</Tag> : null}

      <Space className="head-actions" size={6}>
        <Button htmlType="button" type="primary" onClick={onSave} disabled={busy || readOnly}>
          {busy ? "处理中…" : isNew ? "创建用例" : "保存草稿"}
        </Button>
        <Button htmlType="button" onClick={onPublish} disabled={busy || readOnly}>
          保存并发布
        </Button>
        <Button htmlType="button" aria-label="关闭" onClick={onClose}>
          关闭
        </Button>
      </Space>
    </header>
  );
}
