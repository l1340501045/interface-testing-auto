/**
 * 项目与环境读取：范围由工作空间／项目决定，切换即取消旧请求。
 */
import { apiGet, projectPath } from "../api/client";
import { toEnvironmentList, toProjectList } from "../api/guards";
import type { Environment, Project } from "../api/types";
import { useResource } from "../hooks/useResource";

export function useProjects(workspaceId: string | null) {
  return useResource<Project[]>(workspaceId, (signal) =>
    apiGet(`/workspaces/${workspaceId ?? ""}/projects`, toProjectList, signal),
  );
}

export function useEnvironments(workspaceId: string | null, projectId: string | null) {
  const key = workspaceId && projectId ? `${workspaceId}/${projectId}` : null;
  return useResource<Environment[]>(key, (signal) =>
    apiGet(projectPath(workspaceId ?? "", projectId ?? "", "/environments"), toEnvironmentList, signal),
  );
}
