/**
 * 应用管理面板：项目普通变量、执行池与目标白名单、人工凭证。
 *
 * 三块都按“先看现状、再改配置”的顺序排布：普通变量按不可变版本新增，白名单与凭证
 * 只写配置、不访问目标。权限在服务端判定，这里只负责不给出越权的入口。
 *
 * 当前账号与当前用例往下传：凭证授权要指定“被授权主体”和“哪条用例的哪一版”，
 * 让管理员手抄 id 是最容易出错的一步，默认值应当来自他此刻看到的东西。
 */
import type { Environment } from "../api/types";
import { CredentialsPanel } from "./CredentialsPanel";
import { PoolTargetsPanel } from "./PoolTargetsPanel";
import { VariablesPanel } from "./VariablesPanel";

export function AdminPanel({
  workspaceId,
  projectId,
  role,
  environments,
  currentUser,
  currentCase,
}: {
  workspaceId: string;
  projectId: string;
  role: string | null;
  environments: Environment[];
  currentUser: { user_id: string; display_name: string };
  currentCase: { caseId: string; versionId: string | null } | null;
}) {
  const canEdit = role === "admin" || role === "editor";
  const canAdmin = role === "admin";

  return (
    <div className="admin-stack">
      <VariablesPanel workspaceId={workspaceId} projectId={projectId} canEdit={canEdit} />
      <PoolTargetsPanel workspaceId={workspaceId} projectId={projectId} canAdmin={canAdmin} />
      <CredentialsPanel
        workspaceId={workspaceId}
        projectId={projectId}
        environments={environments}
        canAdmin={canAdmin}
        currentUser={currentUser}
        currentCase={currentCase}
      />
    </div>
  );
}
