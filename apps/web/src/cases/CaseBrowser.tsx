/**
 * 用例目录与列表：左侧选择目录，右侧列出用例。
 *
 * 目录与用例都属于当前项目；切换项目会重新拉取，不使用其他项目的缓存。
 * 只展示草稿用例（列表接口按草稿过滤），已归档用例不在编辑列表中。
 *
 * 选中的目录是**当前范围**的状态，由外壳按工作空间／项目重挂载本组件来重置。
 * 它在组件内留着的话，在 A 项目选过目录再切到 B 项目会继续用它过滤 B 的列表
 * （看起来像“B 项目没有用例”），新建也会把这个属于 A 的目录 id 提交给 B。
 *
 * 目录清单由外壳统一持有并下传：编辑器的「所属目录」选择器要用**同一份**清单。
 * 各取一份的话，在浏览器里归档目录之后编辑器那份不会刷新，已归档的目录仍然可选，
 * 而选中它保存会被服务端按“目录不存在”拒绝——界面看起来正常，保存却失败。
 */
import { useState } from "react";

import { ApiError, apiDelete, apiSend, projectPath } from "../api/client";
import type { Folder } from "../api/types";
import { useLeaveReport } from "../hooks/leaveGuard";
import { useCaseList } from "./useCases";
import { ErrorText, Hint, Loading } from "../components/Feedback";

export function CaseBrowser({
  workspaceId,
  projectId,
  canEdit,
  selectedCaseId,
  onSelect,
  onCreate,
  folders,
  foldersLoading,
  foldersError,
  onFoldersChanged,
  refreshToken,
}: {
  workspaceId: string;
  projectId: string;
  canEdit: boolean;
  selectedCaseId: string | null;
  onSelect: (caseId: string) => void;
  /**
   * 新建用例。带上当前选中的目录，让新用例直接落进这个目录——不传的话用户在 A 目录里
   * 点新建，用例却进了未分组，当前列表（按 A 过滤）看不到刚建出来的那一条。
   */
  onCreate: (folderId: string | null) => void;
  folders: Folder[];
  foldersLoading: boolean;
  foldersError: string | null;
  onFoldersChanged: () => void;
  refreshToken: number;
}) {
  const [folderId, setFolderId] = useState<string | null>(null);
  const [newFolder, setNewFolder] = useState("");
  const [error, setError] = useState<string | null>(null);

  const list = useCaseList(workspaceId, projectId, folderId, refreshToken);

  // 目录名是这里唯一的草稿：填到一半切项目，输入框会连同组件一起被重置。
  useLeaveReport(`browser:${workspaceId}/${projectId}`, { dirty: newFolder.trim() !== "", busy: false });

  async function createFolder() {
    setError(null);
    if (!newFolder.trim()) {
      setError("请输入目录名称。");
      return;
    }
    try {
      await apiSend(projectPath(workspaceId, projectId, "/folders"), "POST", { name: newFolder.trim() }, (raw) => raw);
      setNewFolder("");
      onFoldersChanged();
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "创建目录失败");
    }
  }

  async function archiveFolder(id: string) {
    setError(null);
    try {
      await apiDelete(projectPath(workspaceId, projectId, `/folders/${id}`));
      if (folderId === id) setFolderId(null);
      onFoldersChanged();
      list.reload();
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "归档目录失败");
    }
  }

  return (
    <aside className="browser" aria-label="用例目录">
      <div className="browser-head">
        <h2>用例</h2>
        <button type="button" onClick={() => onCreate(folderId)} disabled={!canEdit}>
          ＋新建用例
        </button>
      </div>
      {error ? <ErrorText message={error} /> : null}

      <section>
        <h3>目录</h3>
        {foldersLoading ? <Loading label="正在加载目录…" /> : null}
        {foldersError ? <ErrorText message={foldersError} /> : null}
        <ul className="folder-list">
          <li>
            <button
              type="button"
              className={folderId === null ? "active" : undefined}
              onClick={() => setFolderId(null)}
            >
              全部用例
            </button>
          </li>
          {folders.map((folder) => (
            <li key={folder.id}>
              <span className="folder-item">
                <button
                  type="button"
                  className={folderId === folder.id ? "active" : undefined}
                  onClick={() => setFolderId(folder.id)}
                >
                  {folder.name}
                </button>
                {canEdit ? (
                  <button type="button" className="link" onClick={() => void archiveFolder(folder.id)}>
                    归档
                  </button>
                ) : null}
              </span>
            </li>
          ))}
        </ul>
        {canEdit ? (
          <div className="folder-create">
            <label htmlFor="new-folder">新目录名称</label>
            <input id="new-folder" value={newFolder} onChange={(event) => setNewFolder(event.target.value)} />
            <button type="button" onClick={() => void createFolder()}>
              添加目录
            </button>
          </div>
        ) : (
          <Hint>当前角色为只读，不能编辑目录与用例。</Hint>
        )}
      </section>

      <section>
        <h3>用例列表</h3>
        {list.loading ? <Loading label="正在加载用例…" /> : null}
        {list.error ? <ErrorText message={list.error.message} /> : null}
        {list.data && list.data.length === 0 ? <Hint>当前范围内没有用例。</Hint> : null}
        <ul className="case-list">
          {(list.data ?? []).map((item) => (
            <li key={item.id}>
              <button
                type="button"
                className={selectedCaseId === item.id ? "active" : undefined}
                onClick={() => onSelect(item.id)}
              >
                <span className="case-method">{item.method}</span>
                <span className="case-name">{item.name}</span>
                <span className="caption">
                  {item.latest_version === null ? "未发布" : `已发布 v${item.latest_version}`}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </section>

      <Hint>这里只会列出草稿用例；归档用例不参与编辑与执行。</Hint>
    </aside>
  );
}
