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
}) {
  return (
    <header className="case-head">
      <label className="visually-hidden" htmlFor="case-name">
        用例名称
      </label>
      <input
        id="case-name"
        className="case-name-input"
        value={name}
        readOnly={readOnly}
        placeholder="未命名用例"
        onChange={(event) => onNameChange(event.target.value)}
      />
      <label className="visually-hidden" htmlFor="case-folder">
        所属目录
      </label>
      <select
        id="case-folder"
        className="case-folder-select"
        value={folderId ?? ""}
        disabled={readOnly}
        onChange={(event) => onFolderChange(event.target.value === "" ? null : event.target.value)}
      >
        <option value="">{UNFILED_LABEL}</option>
        {/*
          当前归属不在可选清单里时补一条占位选项，只为把真实状态显示出来。没有它，
          `value` 匹配不到任何选项，浏览器会显示第一项「未分组」——正好是这个改动
          最不该造成的误解：用户没动过目录，界面却看起来已经改成未分组了。
        */}
        {folderUnavailable && folderId !== null ? (
          <option value={folderId}>{folderPlaceholder}</option>
        ) : null}
        {folders.map((item) => (
          <option key={item.id} value={item.id}>
            {item.name}
          </option>
        ))}
      </select>

      {dirty ? <span className="tag tag-warn">有未保存修改</span> : null}
      {creating ? <span className="tag">新用例</span> : null}
      {versionCount > 0 ? <span className="tag">已发布 {versionCount} 版</span> : null}

      <div className="head-actions">
        <button type="button" onClick={onSave} disabled={busy || readOnly}>
          {busy ? "处理中…" : isNew ? "创建用例" : "保存草稿"}
        </button>
        <button type="button" onClick={onPublish} disabled={busy || readOnly}>
          保存并发布
        </button>
        <button type="button" onClick={onClose}>
          关闭
        </button>
      </div>
    </header>
  );
}
