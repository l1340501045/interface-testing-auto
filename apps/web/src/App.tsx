/**
 * 应用外壳：会话门禁、工作空间／项目／环境选择、用例目录与编辑器。
 *
 * 范围（工作空间、项目、环境）只保存在组件状态里，任何一次范围切换都会清空
 * 下游选择，避免把上一个项目的用例或环境带到当前项目显示。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ApiError, apiSend } from "./api/client";
import type { SessionInfo } from "./api/types";
import { AdminPanel } from "./admin/AdminPanel";
import { CaseBrowser } from "./cases/CaseBrowser";
import { CaseEditor } from "./cases/CaseEditor";
import { useFolders } from "./cases/useCases";
import { Empty, ErrorText, Hint, Loading } from "./components/Feedback";
import { LeaveGuardProvider, useLeaveAggregate, useLeaveReport } from "./hooks/leaveGuard";
import { EnvironmentPanel } from "./projects/EnvironmentPanel";
import { useEnvironments, useProjects } from "./projects/useProjects";
import { LoginPage } from "./session/LoginPage";
import { useSession } from "./session/useSession";

/** 与后端 permissions.py 的中文角色名保持一致的展示文案。 */
const ROLE_LABEL: Record<string, string> = { admin: "管理员", editor: "编辑者", viewer: "查看者" };

function roleLabel(role: string): string {
  return ROLE_LABEL[role] ?? role;
}

function canEdit(role: string | null): boolean {
  return role === "admin" || role === "editor";
}

/**
 * 当前打开的编辑器：id 为 null 表示正在新建一条尚未保存的用例。
 *
 * nonce 变化才重建编辑器组件，用于在“切换用例”时清掉上一条的草稿状态；
 * 新建用例保存成功后只更新选中高亮与列表，不改变编辑器身份，避免刚保存的
 * 草稿被重新拉取覆盖。
 *
 * `folderId` 只在**新建**时有意义：它是「从哪个目录点的＋新建用例」，编辑器挂载时
 * 拿它当初始归属。已有用例的归属由详情带来，与这里无关——所以选中已有用例时它恒为
 * null，不会把上一次新建用的目录串到另一条用例上。
 */
interface EditorTarget {
  id: string | null;
  folderId: string | null;
  nonce: number;
}

/**
 * 新建项目表单。
 *
 * 空态（当前工作空间一个项目都没有）和“已经有项目”两种情形共用同一份字段与同一个
 * 提交入口：接口只有 `POST /workspaces/{id}/projects` 一个，拆成两套表单迟早会各自
 * 漂移（元素 id 重复、校验只改了一处）。这里仍然只做到“把项目建出来”，不提供项目列表
 * 管理、重命名或删除。
 *
 * 创建过程计为忙碌：请求在飞的时候切走工作空间，用户就看不到创建结果了，因此这时也要
 * 走一次确认（离开保护的 busy 分支）；忙碌期间输入与按钮一并锁住，避免重复提交。
 */
function ProjectCreateForm({
  idPrefix,
  projectKey,
  projectName,
  busy,
  error,
  onKeyChange,
  onNameChange,
  onSubmit,
  onCancel,
}: {
  idPrefix: string;
  projectKey: string;
  projectName: string;
  busy: boolean;
  error: string | null;
  onKeyChange: (value: string) => void;
  onNameChange: (value: string) => void;
  onSubmit: () => void;
  onCancel: (() => void) | null;
}) {
  return (
    <div className="project-create">
      <label htmlFor={`${idPrefix}-key`}>项目键（字母、数字、下划线或短横线）</label>
      <input
        id={`${idPrefix}-key`}
        value={projectKey}
        disabled={busy}
        onChange={(event) => onKeyChange(event.target.value)}
      />
      <label htmlFor={`${idPrefix}-name`}>项目名称</label>
      <input
        id={`${idPrefix}-name`}
        value={projectName}
        disabled={busy}
        onChange={(event) => onNameChange(event.target.value)}
      />
      <div className="actions">
        <button type="button" onClick={onSubmit} disabled={busy}>
          {busy ? "创建中…" : "创建项目"}
        </button>
        {onCancel ? (
          <button type="button" onClick={onCancel} disabled={busy}>
            取消
          </button>
        ) : null}
      </div>
      {error ? <ErrorText message={error} /> : null}
    </div>
  );
}

function Shell({ session, onLogout }: { session: SessionInfo; onLogout: () => void }) {
  const [workspaceId, setWorkspaceId] = useState<string | null>(session.workspaces[0]?.id ?? null);
  const [projectId, setProjectId] = useState<string | null>(null);
  const [environmentId, setEnvironmentId] = useState<string | null>(null);
  const [editor, setEditor] = useState<EditorTarget | null>(null);
  const [selectedCaseId, setSelectedCaseId] = useState<string | null>(null);
  const [caseRefresh, setCaseRefresh] = useState(0);
  /**
   * 目录清单的刷新序号。目录与用例分属两个资源：新建／归档目录只该重拉目录，
   * 保存用例只该重拉列表。合成一个序号会让保存用例顺带重拉目录，也会让归档目录
   * 顺带把用例列表打成空（那一刻服务端已经过滤掉归档目录里的用例了）。
   */
  const [folderRefresh, setFolderRefresh] = useState(0);
  const [newProjectKey, setNewProjectKey] = useState("");
  const [newProjectName, setNewProjectName] = useState("");
  const [projectError, setProjectError] = useState<string | null>(null);
  /** 已经有项目时新建表单默认收起，避免把选择区挤走；空态则一直展开。 */
  const [projectCreateOpen, setProjectCreateOpen] = useState(false);
  /**
   * 正在创建项目的工作空间。忙碌状态按工作空间记账，而不是一个全局布尔：请求在飞的
   * 时候用户可能已经切到别的工作空间，那个范围里不该连带被锁住（它的创建是另一次操作）。
   */
  const [creatingIn, setCreatingIn] = useState<string[]>([]);
  /** 当前工作空间是不是正在创建项目；忙碌只锁发起它的那个范围。 */
  const projectBusy = workspaceId !== null && creatingIn.includes(workspaceId);
  /** 编辑器当前所在用例与它对应的已发布版本，供凭证授权表单预选。 */
  const [editorVersion, setEditorVersion] = useState<{ caseId: string; versionId: string | null } | null>(null);

  /**
   * 离开保护看的是**全部**管理表单的登记结果，而不只是用例编辑器：项目变量、
   * 执行池白名单、环境编辑和凭证表单各有草稿，凭证表单里还有已经输入的秘密值。
   * 只问用例编辑器，用户在凭证表单里填到一半切项目，秘密和草稿就一起没了。
   */
  const leaveState = useLeaveAggregate();

  /** 当前范围；切换范围后旧请求的回调必须认得出自己已经不属于这个范围。 */
  const scope = `${workspaceId ?? ""}/${projectId ?? ""}`;
  const liveScope = useRef(scope);
  liveScope.current = scope;

  /** 当前工作空间；迟到的创建结果按它判断自己是否已经属于上一个工作空间。 */
  const liveWorkspace = useRef(workspaceId);
  liveWorkspace.current = workspaceId;

  const projects = useProjects(workspaceId);
  const environments = useEnvironments(workspaceId, projectId);
  /**
   * 目录清单由外壳持有并下发给左侧目录树与编辑器。
   *
   * 各取一份的话，在浏览器里归档目录之后编辑器那份不会刷新，已归档的目录仍然可选，
   * 而选中它保存会被服务端按“目录不存在”拒绝——界面看起来正常，保存却失败。
   */
  const folders = useFolders(workspaceId, projectId, folderRefresh);

  // 新建项目表单也在离开保护的范围内：填到一半切换工作空间会连同组件一起被重置，
  // 输入的名称再也不会回来；创建请求在飞的时候离开同样看不到结果。
  useLeaveReport("project-create", {
    dirty: newProjectKey.trim() !== "" || newProjectName.trim() !== "",
    busy: projectBusy,
  });

  const currentWorkspace = session.workspaces.find((item) => item.id === workspaceId) ?? null;
  const projectList = projects.data ?? [];
  const currentProject = projectList.find((item) => item.id === projectId) ?? null;
  const environmentList = environments.data ?? [];
  /** 能不能建项目看工作空间角色：查看者连入口都不显示。 */
  const canCreateProject = canEdit(currentWorkspace?.role ?? null);

  // 切换工作空间会作废项目及其下游选择；不保留上一个工作空间的 id。
  // 编辑器上报的当前用例也一并清空：编辑器卸载时会上报一次 null，但那时范围已经
  // 换成新的，晚到的上报会被范围守卫丢弃——留着它，新项目的凭证授权表单就会预选
  // 上一个项目的用例版本。
  useEffect(() => {
    setProjectId(null);
    setEnvironmentId(null);
    setEditor(null);
    setSelectedCaseId(null);
    setEditorVersion(null);
    setProjectError(null);
    // 新建项目的草稿属于上一个工作空间：留在这里会被当成新工作空间的项目键提交。
    setNewProjectKey("");
    setNewProjectName("");
    setProjectCreateOpen(false);
  }, [workspaceId]);

  useEffect(() => {
    setEnvironmentId(null);
    setEditor(null);
    setSelectedCaseId(null);
    setEditorVersion(null);
  }, [projectId]);

  // 默认选中第一个项目与第一个环境，减少无谓点击；用户改动后不再覆盖。
  useEffect(() => {
    if (projectId === null && projectList.length > 0) setProjectId(projectList[0].id);
  }, [projectList, projectId]);

  useEffect(() => {
    if (environmentId === null && environmentList.length > 0) setEnvironmentId(environmentList[0].id);
  }, [environmentList, environmentId]);

  const selectedEnvironmentName = useMemo(
    () => environmentList.find((item) => item.id === environmentId)?.name ?? null,
    [environmentList, environmentId],
  );

  const openCase = useCallback((caseId: string | null, folderId: string | null = null) => {
    setEditor((current) => ({ id: caseId, folderId, nonce: (current?.nonce ?? 0) + 1 }));
    setSelectedCaseId(caseId);
  }, []);

  /**
   * 离开登记表的最新汇总。
   *
   * `confirmLeaveEditor` 不只被同步的事件处理函数调用，还会被**异步回调**在 `await`
   * 之后调用（新建项目成功 → 自动切到新项目）。那些回调持有的是发起时那一帧的函数，
   * 闭包里的登记表就停在提交那一刻：等待期间用户继续编辑当前用例、新产生的 dirty 它
   * 一个也看不到，于是不弹确认就切范围、把编辑器连同刚输入的内容一起卸载掉。
   * 判断离开时读的必须是“现在”的登记，所以这里与 `liveScope`／`liveWorkspace` 用同一种
   * 方式留一份最新汇总；不引入更大的状态重构。
   */
  const liveLeaveState = useRef(leaveState);
  liveLeaveState.current = leaveState;

  /**
   * 编辑器离开前的统一拦截。
   *
   * 切换工作空间／项目／用例、关闭编辑器与退出登录都会卸载下游表单并丢掉草稿；
   * 不在这里拦一次，每个入口都得各写一份判断，漏掉任何一个就是静默丢数据。正在
   * 保存或发布时同样拦截：请求会完成，但用户看不到结果，等于把结论丢掉了。
   */
  const confirmLeaveEditor = useCallback(
    (except?: string): boolean => {
      // 取调用**当下**的汇总，而不是本函数被创建那一帧的：异步回调里的判断同样要
      // 看得见等待期间新产生的草稿。
      const current = liveLeaveState.current;
      // 由某个表单自己发起的离开（见下面的新建项目）要把它自己排除掉，否则它自己的
      // 草稿会把这次切换判成“有未保存修改”，弹一个无所指的确认框。
      const state = except === undefined ? current : current.without(except);
      if (!state.dirty && !state.busy) return true;
      const message = state.busy
        ? "当前有正在进行的操作，离开会丢失尚未看到的结果。确定离开吗？"
        : "当前有未保存的修改（表单或用例），离开后这些修改会丢失。确定离开吗？";
      return window.confirm(message);
    },
    [],
  );

  // 关闭页签或窗口同样会丢掉草稿，且没有第二次机会。只在确有未保存内容或进行中
  // 的操作时挂上监听，平时不打扰正常的关闭动作。
  useEffect(() => {
    if (!leaveState.dirty && !leaveState.busy) return;
    const keep = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", keep);
    return () => window.removeEventListener("beforeunload", keep);
  }, [leaveState.dirty, leaveState.busy]);

  const changeCase = useCallback(
    (caseId: string | null) => {
      if (!confirmLeaveEditor()) return;
      // 选中已有用例时不带目录：它的归属由详情决定。只有新建才继承当前目录。
      openCase(caseId, null);
    },
    [confirmLeaveEditor, openCase],
  );

  /**
   * 从左侧列表的「＋新建用例」进入新建：把**当前选中的目录**带进编辑器当初始归属。
   *
   * 不带的话，用户在 A 目录里点新建，用例落进未分组——而左侧列表正按 A 过滤，
   * 刚建出来的那一条当场看不见，看起来像“创建失败”。
   */
  const createCase = useCallback(
    (folderId: string | null) => {
      if (!confirmLeaveEditor()) return;
      openCase(null, folderId);
    },
    [confirmLeaveEditor, openCase],
  );

  const closeEditor = useCallback(() => {
    if (!confirmLeaveEditor()) return;
    setEditor(null);
    setSelectedCaseId(null);
  }, [confirmLeaveEditor]);

  const requestLogout = useCallback(() => {
    if (!confirmLeaveEditor()) return;
    onLogout();
  }, [confirmLeaveEditor, onLogout]);

  /**
   * 保存成功后的选中：只认发起保存时所在的范围。
   *
   * 保存请求可能在用户切走之后才返回。此时按返回的用例 id 去选中，新项目里就会
   * 高亮一条属于旧项目的用例；失败提示同理，会挂在一个无关的项目上。下面几个
   * 回调都把发起时的 scope 关进闭包，回来时与当前范围对不上就直接丢弃。
   */
  const onCaseSaved = useCallback(
    (caseId?: string) => {
      if (liveScope.current !== scope) return;
      setCaseRefresh((value) => value + 1);
      if (caseId) setSelectedCaseId(caseId);
    },
    [scope],
  );

  const onEnvironmentsChanged = useCallback(() => {
    if (liveScope.current !== scope) return;
    void environments.reload();
  }, [environments, scope]);

  /**
   * 目录被新建或归档后重拉目录清单。
   *
   * 用序号而不是 `folders.reload()`：`reload` 会换掉资源对象，把它作为依赖传下去会
   * 在一次渲染里再次触发拉取。序号变化只改缓存键，语义是“这份清单过期了”。
   */
  const onFoldersChanged = useCallback(() => {
    if (liveScope.current !== scope) return;
    setFolderRefresh((value) => value + 1);
  }, [scope]);

  /**
   * 某条用例的已发布版本变了：左侧列表要重拉。
   *
   * 列表里的“已发布 vN”是随保存草稿一起读回来的，而发布在保存之后发生——不在这里
   * 再报一次，左侧会一直停在“未发布”或落后一版，与编辑器里刚确认的版本自相矛盾。
   * 与其它回调同理，只认发起时所在的范围。
   */
  const onCaseVersionsChanged = useCallback(
    (_caseId: string) => {
      if (liveScope.current !== scope) return;
      setCaseRefresh((value) => value + 1);
    },
    [scope],
  );

  /**
   * 编辑器上报的当前用例：与保存回调同理，只认上报时所在的范围。
   */
  const onEditorVersion = useCallback(
    (target: { caseId: string; versionId: string | null } | null) => {
      if (liveScope.current !== scope) return;
      setEditorVersion(target);
    },
    [scope],
  );

  async function createProject() {
    setProjectError(null);
    if (!newProjectKey.trim() || !newProjectName.trim()) {
      setProjectError("请填写项目键和项目名称。");
      return;
    }
    // 结果只属于发起时的工作空间。用户可能已经切到别的工作空间，这时既不能把新项目
    // 选成当前项目（会把用户切回旧工作空间的项目），也不能借清表单的动作抹掉新范围里
    // 刚输入的内容。
    const startedIn = workspaceId;
    if (startedIn === null) return;
    setCreatingIn((list) => [...list, startedIn]);
    try {
      const created = await apiSend(
        `/workspaces/${workspaceId ?? ""}/projects`,
        "POST",
        { key: newProjectKey.trim(), name: newProjectName.trim() },
        (raw) => raw,
      );
      if (liveWorkspace.current !== startedIn) return;
      setNewProjectKey("");
      setNewProjectName("");
      setProjectCreateOpen(false);
      await projects.reload();
      const id = (created as { id?: unknown }).id;
      // 切到新项目同样会卸载编辑器：先问一次，用户不确认就只建项目、不切范围。
      // 排除创建表单自己：它的草稿刚被清掉、结果也已经看到，把它算进“未保存内容”会弹出
      // 一个无所指的确认框（真实页面上确实弹了）。
      if (typeof id === "string" && confirmLeaveEditor("project-create")) setProjectId(id);
    } catch (cause) {
      // 失败提示同理：属于旧工作空间的失败不该挂在新工作空间的界面上。
      if (liveWorkspace.current !== startedIn) return;
      setProjectError(cause instanceof ApiError ? cause.message : "创建项目失败");
    } finally {
      setCreatingIn((list) => list.filter((item) => item !== startedIn));
    }
  }

  if (workspaceId === null) {
    return (
      <main className="boot">
        <span className="eyebrow">接口自动化测试与巡检平台</span>
        <h1>还没有可访问的工作空间</h1>
        <Hint>账号已登录，但还没有加入任何工作空间；请由管理员在本机初始化命令中创建。</Hint>
        <div className="actions">
          <button type="button" onClick={requestLogout}>
            退出登录
          </button>
        </div>
      </main>
    );
  }

  return (
    <div className="app">
      <header className="app-head">
        <div className="brand">
          <span className="eyebrow">接口自动化测试与巡检平台</span>
          <h1>单接口执行</h1>
        </div>

        <div className="scope">
          <span className="param">
            <label htmlFor="scope-workspace">工作空间</label>
            <select
              id="scope-workspace"
              value={workspaceId}
              onChange={(event) => {
                if (!confirmLeaveEditor()) return;
                setWorkspaceId(event.target.value);
              }}
            >
              {session.workspaces.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          </span>
          <span className="param">
            <label htmlFor="scope-project">项目</label>
            <select
              id="scope-project"
              value={projectId ?? ""}
              disabled={projectList.length === 0}
              onChange={(event) => {
                if (!confirmLeaveEditor()) return;
                setProjectId(event.target.value || null);
              }}
            >
              {projectList.length === 0 ? <option value="">暂无项目</option> : null}
              {projectList.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          </span>
          {/*
            新建项目的入口不能只留在空态：工作空间里已经有项目时，管理员/编辑者仍然
            需要建下一个项目。这里只多一个入口，接口与表单仍是空态那一份。
            查看者不显示这个入口，也不显示可提交的表单。
          */}
          {canCreateProject && projectList.length > 0 ? (
            <button
              type="button"
              onClick={() => setProjectCreateOpen((open) => !open)}
              disabled={projectBusy}
            >
              {projectCreateOpen ? "收起新建项目" : "＋新建项目"}
            </button>
          ) : null}
          <span className="current-env" aria-live="polite">
            执行环境：{selectedEnvironmentName ?? "未选择"}
          </span>
        </div>

        <div className="who">
          <span className="caption">
            {session.user.display_name}
            {currentWorkspace ? `（${roleLabel(currentWorkspace.role)}）` : ""}
          </span>
          <button type="button" onClick={requestLogout}>
            退出登录
          </button>
        </div>
      </header>

      {projects.error ? <ErrorText message={projects.error.message} /> : null}

      {/**
       * 已经有项目时，新建表单由头部的入口展开，位置就在选择区下方：选中的项目与
       * 正在新建的项目在同一条视线里，不会让人以为“点了没反应”。
       */}
      {projectCreateOpen && canCreateProject && projectList.length > 0 ? (
        <ProjectCreateForm
          idPrefix="project-inline"
          projectKey={newProjectKey}
          projectName={newProjectName}
          busy={projectBusy}
          error={projectError}
          onKeyChange={setNewProjectKey}
          onNameChange={setNewProjectName}
          onSubmit={() => void createProject()}
          onCancel={() => setProjectCreateOpen(false)}
        />
      ) : null}

      {projects.loading && projects.data === null ? (
        <Loading label="正在加载项目…" />
      ) : projectList.length === 0 ? (
        <main className="boot">
          <h2>当前工作空间还没有项目</h2>
          <Hint>项目决定用例、环境与执行池的归属；创建项目是管理员动作。</Hint>
          {currentWorkspace && canEdit(currentWorkspace.role) ? (
            <ProjectCreateForm
              idPrefix="project"
              projectKey={newProjectKey}
              projectName={newProjectName}
              busy={projectBusy}
              error={projectError}
              onKeyChange={setNewProjectKey}
              onNameChange={setNewProjectName}
              onSubmit={() => void createProject()}
              onCancel={null}
            />
          ) : (
            <Hint>当前角色不能创建项目，请联系工作空间管理员。</Hint>
          )}
        </main>
      ) : (
        <div className="workspace">
          <div className="sidebar">
            {/* 环境编辑表单同样按范围重挂载，否则上一个项目的编辑草稿会留在新项目里。 */}
            <EnvironmentPanel
              key={`environment:${scope}`}
              workspaceId={workspaceId}
              projectId={projectId ?? ""}
              environments={environmentList}
              loading={environments.loading}
              error={environments.error ? environments.error.message : null}
              selectedId={environmentId}
              onSelect={setEnvironmentId}
              canEdit={canEdit(currentProject?.role ?? null)}
              onChanged={onEnvironmentsChanged}
            />
            {/*
              浏览器的目录范围同样是**这个范围的**状态：它的目录过滤与「＋新建用例」的
              归属都取自已选中的目录。不按范围重挂载，在 A 项目选了目录之后再切到 B，
              B 的列表仍按 A 的目录过滤（看起来像“B 项目没有用例”），新建还会把 A 的
              目录 id 提交给 B，被服务端按“目录不存在”拒绝。
            */}
            <CaseBrowser
              key={`cases:${scope}`}
              workspaceId={workspaceId}
              projectId={projectId ?? ""}
              canEdit={canEdit(currentProject?.role ?? null)}
              selectedCaseId={selectedCaseId}
              onSelect={changeCase}
              onCreate={createCase}
              folders={folders.data ?? []}
              foldersLoading={folders.loading}
              foldersError={folders.error ? folders.error.message : null}
              onFoldersChanged={onFoldersChanged}
              refreshToken={caseRefresh}
            />
            {/*
              管理表单按范围重挂载：范围变了就是另一套资源，凭证表单里的秘密输入、
              变量草稿、白名单草稿都不能沿用上一个项目的状态——留着它，用户在 A 项目
              输入的秘密会显示在 B 项目，并且按 B 项目的路径提交出去。
            */}
            <AdminPanel
              key={`admin:${scope}`}
              workspaceId={workspaceId}
              projectId={projectId ?? ""}
              role={currentProject?.role ?? null}
              environments={environmentList}
              currentUser={{ user_id: session.user.user_id, display_name: session.user.display_name }}
              currentCase={editorVersion}
            />
          </div>

          <div className="main-pane">
            {editor === null ? (
              <Empty label="从左侧选择一条用例开始编辑，或点击“＋新建用例”。" />
            ) : (
              <CaseEditor
                key={editor.nonce}
                workspaceId={workspaceId}
                projectId={projectId ?? ""}
                caseSummaryId={editor.id}
                environments={environmentList}
                selectedEnvironmentId={environmentId}
                onSelectEnvironment={setEnvironmentId}
                onSaved={onCaseSaved}
                onVersionsChanged={onCaseVersionsChanged}
                onClose={closeEditor}
                onCurrentVersion={onEditorVersion}
                folders={folders.data ?? []}
                foldersLoading={folders.loading}
                initialFolderId={editor.folderId}
              />
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export function App() {
  const { session, loading, error, expired, login, logout } = useSession();
  const [busy, setBusy] = useState(false);

  async function handleLogin(username: string, password: string) {
    setBusy(true);
    try {
      await login(username, password);
    } catch {
      // 失败信息由 useSession 统一呈现，这里不重复提示。
    } finally {
      setBusy(false);
    }
  }

  if (loading) {
    return (
      <main className="boot">
        <Loading label="正在确认登录状态…" />
      </main>
    );
  }

  if (session === null) {
    return (
      <LoginPage
        onSubmit={(username, password) => void handleLogin(username, password)}
        error={error}
        notice={expired ? "会话已失效，请重新登录。" : null}
        busy={busy}
      />
    );
  }

  // 离开保护的登记处放在外壳之上：它只汇总“有没有未保存内容”，不持有任何表单
  // 内容，因此秘密不会因为要拦截离开而被复制到别处。
  return (
    <LeaveGuardProvider>
      <Shell session={session} onLogout={() => void logout()} />
    </LeaveGuardProvider>
  );
}
