/**
 * 用例目录与列表读取。目录与用例分开缓存，切换目录只重新拉取列表。
 *
 * 缓存键与请求路径是两件事：缓存键可以带刷新序号与目录，请求路径只由
 * 工作空间／项目／目录的真实 id 组成。把缓存键当成 id 会拼出带 `#` 的非法路径。
 */
import { apiGet, projectPath } from "../api/client";
import { toCaseSummaryList, toFolderList } from "../api/guards";
import type { CaseSummary, Folder } from "../api/types";
import { useResource } from "../hooks/useResource";

function scopeKey(workspaceId: string | null, projectId: string | null): string | null {
  return workspaceId && projectId ? `${workspaceId}/${projectId}` : null;
}

export function useFolders(workspaceId: string | null, projectId: string | null, refreshToken: number) {
  const key = scopeKey(workspaceId, projectId);
  return useResource<Folder[]>(key === null ? null : `${key}#folders#${refreshToken}`, (signal) =>
    apiGet(projectPath(workspaceId ?? "", projectId ?? "", "/folders"), toFolderList, signal),
  );
}

export function useCaseList(
  workspaceId: string | null,
  projectId: string | null,
  folderId: string | null,
  refreshToken: number,
) {
  const key = scopeKey(workspaceId, projectId);
  return useResource<CaseSummary[]>(key === null ? null : `${key}#${folderId ?? "all"}#${refreshToken}`, (signal) => {
    const suffix = folderId ? `/cases?folder_id=${encodeURIComponent(folderId)}` : "/cases";
    return apiGet(projectPath(workspaceId ?? "", projectId ?? "", suffix), toCaseSummaryList, signal);
  });
}
