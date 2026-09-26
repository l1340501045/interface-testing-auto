/**
 * 应用外壳：会话门禁、工作空间／项目／环境选择、用例目录与编辑器。
 *
 * 范围（工作空间、项目、环境）只保存在组件状态里，任何一次范围切换都会清空
 * 下游选择，避免把上一个项目的用例或环境带到当前项目显示。
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";

import { ApiError, apiSend, invalidateClientSession } from "./api/client";
import type { SessionInfo } from "./api/types";
import { AdminPanel } from "./admin/AdminPanel";
import { CaseBrowser } from "./cases/CaseBrowser";
import { CaseEditor } from "./cases/CaseEditor";
import { useFolders } from "./cases/useCases";
import { Empty, ErrorText, Hint, Loading } from "./components/Feedback";
import { LeaveGuardProvider, useLeaveAggregate, useLeaveReport } from "./hooks/leaveGuard";
import type { LeaveState } from "./hooks/leaveGuard";
import { PAGE_LABEL, pageFromHash, pageHash, type AppPage } from "./navigation";
import { EnvironmentPanel } from "./projects/EnvironmentPanel";
import { useEnvironments, useProjects } from "./projects/useProjects";
import { RunCenter } from "./runs/RunCenter";
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
  tabId: string;
  id: string | null;
  folderId: string | null;
  nonce: number;
  environmentId: string | null;
  name: string;
  method: string;
  dirty: boolean;
  busy: boolean;
}

function newTabId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
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
  const [page, setPage] = useState<AppPage>(() => pageFromHash(window.location.hash));
  const [workspaceId, setWorkspaceId] = useState<string | null>(session.workspaces[0]?.id ?? null);
  const [projectId, setProjectId] = useState<string | null>(null);
  const [environmentId, setEnvironmentId] = useState<string | null>(null);
  /** 配置页正在浏览的环境；与工作台实际发送使用的环境严格分离。 */
  const [settingsEnvironmentId, setSettingsEnvironmentId] = useState<string | null>(null);
  const [editors, setEditors] = useState<EditorTarget[]>([]);
  const [activeEditorId, setActiveEditorId] = useState<string | null>(null);
  const editor = editors.find((item) => item.tabId === activeEditorId) ?? null;
  const liveEditors = useRef(editors);
  const liveActiveEditorId = useRef(activeEditorId);
  liveEditors.current = editors;
  liveActiveEditorId.current = activeEditorId;
  const closeControllers = useRef(new Map<string, { save: () => Promise<boolean>; state: () => LeaveState }>());
  const [closeDialog, setCloseDialog] = useState<{ tabIds: string[]; attemptId: number; saving: boolean } | null>(null);
  const closeAttemptRef = useRef(0);
  const closeSavingAttemptRef = useRef<number | null>(null);
  const workspaceTabRefs = useRef(new Map<string, HTMLButtonElement>());
  const workspaceTabScrollRef = useRef<HTMLDivElement | null>(null);
  const newRequestButtonRef = useRef<HTMLButtonElement | null>(null);
  const closeDialogCancelRef = useRef<HTMLButtonElement | null>(null);
  const closeDialogReturnFocusRef = useRef<HTMLElement | null>(null);
  const closeDialogWasOpenRef = useRef(false);
  const [selectedCaseId, setSelectedCaseId] = useState<string | null>(null);
  const [reportRequest, setReportRequest] = useState<{
    runId: string;
    token: number;
    workspaceId: string;
    projectId: string;
  } | null>(null);
  const reportRequestToken = useRef(0);

  const revealActiveTab = useCallback(() => {
    if (activeEditorId === null) return;
    const scroller = workspaceTabScrollRef.current;
    const tab = workspaceTabRefs.current.get(activeEditorId);
    if (scroller === null || tab === undefined) return;
    const viewport = scroller.getBoundingClientRect();
    const item = tab.getBoundingClientRect();
    if (item.left < viewport.left) scroller.scrollLeft -= viewport.left - item.left;
    else if (item.right > viewport.right) scroller.scrollLeft += item.right - viewport.right;
  }, [activeEditorId]);

  useLayoutEffect(() => {
    revealActiveTab();
  }, [revealActiveTab, editor?.name, editor?.method, editor?.dirty, editor?.busy]);

  useEffect(() => {
    const scroller = workspaceTabScrollRef.current;
    if (scroller === null) return;
    const handleResize = () => revealActiveTab();
    window.addEventListener("resize", handleResize);
    if (typeof ResizeObserver === "undefined") return () => window.removeEventListener("resize", handleResize);
    const observer = new ResizeObserver(handleResize);
    observer.observe(scroller);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", handleResize);
    };
  }, [revealActiveTab]);

  useEffect(() => {
    if (closeDialog !== null) {
      closeDialogWasOpenRef.current = true;
      closeDialogCancelRef.current?.focus();
      return;
    }
    if (!closeDialogWasOpenRef.current) return;
    closeDialogWasOpenRef.current = false;
    const previous = closeDialogReturnFocusRef.current;
    closeDialogReturnFocusRef.current = null;
    if (previous?.isConnected && !(previous instanceof HTMLButtonElement && previous.disabled)) previous.focus();
    else newRequestButtonRef.current?.focus();
  }, [closeDialog]);

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

  /** 工作台的纠错动作进入独立配置页，并聚焦真实的环境或身份配置块。 */
  const navigate = useCallback((next: AppPage) => {
    setPage(next);
    const hash = pageHash(next);
    if (window.location.hash === hash) {
      setPage(next);
      return;
    }
    window.location.hash = hash;
  }, []);

  useEffect(() => {
    const syncPage = () => setPage(pageFromHash(window.location.hash));
    window.addEventListener("hashchange", syncPage);
    if (window.location.hash !== pageHash(pageFromHash(window.location.hash))) {
      window.history.replaceState(null, "", pageHash(pageFromHash(window.location.hash)));
    }
    return () => window.removeEventListener("hashchange", syncPage);
  }, []);
  const [settingsFocusTarget, setSettingsFocusTarget] = useState<string | null>(null);
  const [settingsSourceTabId, setSettingsSourceTabId] = useState<string | null>(null);

  /** 环境与身份是两个配置块，入口必须精确定位，不能只切页后让用户继续寻找。 */
  const revealAdminBlock = useCallback((anchorId: string) => {
    setSettingsFocusTarget(anchorId);
    navigate("environments");
  }, [navigate]);
  const [credentialsOpen, setCredentialsOpen] = useState(false);
  const openAdminPanel = useCallback((sourceTabId?: string) => {
    setSettingsSourceTabId(sourceTabId ?? null);
    setCredentialsOpen(true);
    revealAdminBlock("credentials-panel");
  }, [revealAdminBlock]);
  /** 环境面板的折叠状态由外壳持有，纠错入口可直接展开；无环境时同样强制展开。 */
  const [environmentOpen, setEnvironmentOpen] = useState(false);
  /** 打开环境设置时同步展开环境块，保证地址编辑入口立即可见。 */
  const openEnvironmentPanel = useCallback((sourceTabId?: string, sourceEnvironmentId?: string | null) => {
    setSettingsSourceTabId(sourceTabId ?? null);
    setEnvironmentOpen(true);
    setSettingsEnvironmentId(sourceEnvironmentId ?? environmentId);
    revealAdminBlock("environment-panel");
  }, [environmentId, revealAdminBlock]);

  useEffect(() => {
    if (page !== "environments" || settingsFocusTarget === null) return;
    const panel = document.getElementById(settingsFocusTarget);
    if (panel === null) return;
    if (typeof panel.scrollIntoView === "function") panel.scrollIntoView({ block: "start" });
    panel.focus();
    setSettingsFocusTarget(null);
  }, [page, settingsFocusTarget, credentialsOpen, environmentOpen]);

  // 切换工作空间会作废项目及其下游选择；不保留上一个工作空间的 id。
  // 编辑器上报的当前用例也一并清空：编辑器卸载时会上报一次 null，但那时范围已经
  // 换成新的，晚到的上报会被范围守卫丢弃——留着它，新项目的凭证授权表单就会预选
  // 上一个项目的用例版本。
  useEffect(() => {
    setProjectId(null);
    setEnvironmentId(null);
    setSettingsEnvironmentId(null);
    setEditors([]);
    setActiveEditorId(null);
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
    setSettingsEnvironmentId(null);
    setEditors([]);
    setActiveEditorId(null);
    setSelectedCaseId(null);
    setEditorVersion(null);
    setReportRequest(null);
  }, [projectId]);

  // 默认选中第一个项目与第一个环境，减少无谓点击；用户改动后不再覆盖。
  useEffect(() => {
    if (projectId === null && projectList.length > 0) setProjectId(projectList[0].id);
  }, [projectList, projectId]);

  useEffect(() => {
    if (environmentId === null && environmentList.length > 0) setEnvironmentId(environmentList[0].id);
  }, [environmentList, environmentId]);

  useEffect(() => {
    if (settingsEnvironmentId === null && environmentList.length > 0) {
      setSettingsEnvironmentId(environmentId ?? environmentList[0].id);
    }
  }, [environmentId, environmentList, settingsEnvironmentId]);

  const openCase = useCallback((caseId: string | null, folderId: string | null = null) => {
    const existing = caseId === null ? undefined : editors.find((item) => item.id === caseId);
    if (existing) {
      setActiveEditorId(existing.tabId);
      setSelectedCaseId(existing.id);
      return;
    }
    if (editors.length >= 20) {
      window.alert("当前项目最多打开 20 个请求，请先关闭一个标签。");
      return;
    }
    const tabId = newTabId();
    const target: EditorTarget = {
      tabId,
      id: caseId,
      folderId,
      nonce: Date.now(),
      environmentId,
      name: caseId === null ? "新请求" : `用例 ${caseId.slice(0, 8)}…`,
      method: "GET",
      dirty: false,
      busy: false,
    };
    setEditors((current) => [...current, target]);
    setActiveEditorId(tabId);
    setSelectedCaseId(caseId);
  }, [editors, environmentId]);

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
      if (state.busy) {
        window.alert("当前有保存、发布或受理结果待确认；普通切换不能丢弃该操作，请先回到标签处理。只有明确退出登录可清除本地确认依据。");
        return false;
      }
      return window.confirm("当前有未保存的修改（表单或用例），离开后这些修改会丢失。确定离开吗？");
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
      // 选中已有用例时不带目录：它的归属由详情决定。只有新建才继承当前目录。
      openCase(caseId, null);
    },
    [openCase],
  );

  /**
   * 从左侧列表的「＋新建用例」进入新建：把**当前选中的目录**带进编辑器当初始归属。
   *
   * 不带的话，用户在 A 目录里点新建，用例落进未分组——而左侧列表正按 A 过滤，
   * 刚建出来的那一条当场看不见，看起来像“创建失败”。
   */
  const createCase = useCallback(
    (folderId: string | null) => {
      openCase(null, folderId);
    },
    [openCase],
  );

  const removeClosedTargets = useCallback((tabIds: readonly string[]) => {
    const latestEditors = liveEditors.current;
    const remaining = latestEditors.filter((item) => !tabIds.includes(item.tabId));
    setEditors((current) => current.filter((item) => !tabIds.includes(item.tabId)));
    const latestActive = liveActiveEditorId.current;
    if (latestActive !== null && tabIds.includes(latestActive)) {
      const next = remaining.at(-1) ?? null;
      setActiveEditorId(next?.tabId ?? null);
      setSelectedCaseId(next?.id ?? null);
    }
  }, []);

  const finishCloseEditors = useCallback(async (tabIds: readonly string[], mode: "save" | "discard", attemptId: number) => {
    if (closeAttemptRef.current !== attemptId) return;
    const targets = liveEditors.current.filter((item) => tabIds.includes(item.tabId));
    if (mode === "discard") {
      const state = liveLeaveState.current.matching(targets.map((item) => `case-tab:${item.tabId}`));
      if (closeSavingAttemptRef.current === attemptId || state.busy || targets.some((item) => closeControllers.current.get(item.tabId)?.state().busy)) {
        window.alert("保存仍在进行，不能放弃并关闭；可以取消本次关闭，保存结果仍会保留。");
        return;
      }
    }
    if (mode === "save") {
        closeSavingAttemptRef.current = attemptId;
        setCloseDialog((current) => current?.attemptId === attemptId ? { ...current, saving: true } : current);
        for (const target of targets) {
          const controller = closeControllers.current.get(target.tabId);
          if (controller === undefined) {
            closeSavingAttemptRef.current = null;
            setCloseDialog((current) => current?.attemptId === attemptId ? { ...current, saving: false } : current);
            window.alert("保存未完成或保存期间出现了新输入，目标标签均保留；已经成功的服务端保存不会回滚。");
            return;
          }
          const saved = await controller.save();
          if (closeAttemptRef.current !== attemptId) {
            if (closeSavingAttemptRef.current === attemptId) closeSavingAttemptRef.current = null;
            return;
          }
          if (!saved) {
            closeSavingAttemptRef.current = null;
            setCloseDialog((current) => current?.attemptId === attemptId ? { ...current, saving: false } : current);
            window.alert("保存未完成或保存期间出现了新输入，目标标签均保留；已经成功的服务端保存不会回滚。");
            return;
          }
          const root = controller.state();
          const child = liveLeaveState.current.descendants(`case-tab:${target.tabId}`);
          if (root.busy || root.dirty || child.busy || child.dirty) {
            closeSavingAttemptRef.current = null;
            setCloseDialog((current) => current?.attemptId === attemptId ? { ...current, saving: false } : current);
            window.alert("保存完成后又出现新输入或新操作，目标标签均保留；已经成功的服务端保存不会回滚。");
            return;
          }
        }
        if (closeAttemptRef.current !== attemptId) return;
        const latestTargetState = targets.reduce(
          (state, item) => {
            const root = closeControllers.current.get(item.tabId)?.state() ?? { dirty: true, busy: true };
            const child = liveLeaveState.current.descendants(`case-tab:${item.tabId}`);
            return { dirty: state.dirty || root.dirty || child.dirty, busy: state.busy || root.busy || child.busy };
          },
          { dirty: false, busy: false },
        );
        if (latestTargetState.busy || latestTargetState.dirty) {
          closeSavingAttemptRef.current = null;
          setCloseDialog((current) => current?.attemptId === attemptId ? { ...current, saving: false } : current);
          window.alert("仍有尚未应用的 cURL、断言或批量输入，请先回到对应标签应用或明确放弃。");
          return;
        }
        closeSavingAttemptRef.current = null;
    }
    if (closeAttemptRef.current !== attemptId) return;
    setCloseDialog(null);
    removeClosedTargets(tabIds);
  }, [removeClosedTargets]);

  const cancelCloseDialog = useCallback(() => {
    closeAttemptRef.current += 1;
    setCloseDialog(null);
  }, []);

  const requestCloseEditors = useCallback((tabIds: readonly string[]) => {
    const targets = liveEditors.current.filter((item) => tabIds.includes(item.tabId));
    const state = liveLeaveState.current.matching(targets.map((item) => `case-tab:${item.tabId}`));
    if (state.busy || targets.some((item) => closeControllers.current.get(item.tabId)?.state().busy)) {
      window.alert("目标标签仍有保存、发布或受理结果待确认，请等待或回到标签处理后再关闭。");
      return;
    }
    if (!state.dirty && targets.every((item) => !closeControllers.current.get(item.tabId)?.state().dirty)) {
      removeClosedTargets(tabIds);
      return;
    }
    closeDialogReturnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeAttemptRef.current += 1;
    setCloseDialog({ tabIds: [...tabIds], attemptId: closeAttemptRef.current, saving: false });
  }, [removeClosedTargets]);

  const closeEditor = useCallback((tabId = activeEditorId) => {
    if (tabId !== null) requestCloseEditors([tabId]);
  }, [activeEditorId, requestCloseEditors]);

  const handleCloseDialogKeyDown = useCallback((event: KeyboardEvent<HTMLElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      cancelCloseDialog();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = Array.from(
      event.currentTarget.querySelectorAll<HTMLElement>('button:not([disabled]), select:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])'),
    );
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }, [cancelCloseDialog]);

  const requestLogout = useCallback(() => {
    const current = liveLeaveState.current;
    if (current.busy && !window.confirm("仍有操作的受理结果未知。退出会清除本地标签，但不会取消远端运行；之后需要重新登录并到任务中心核对。仍要退出吗？")) return;
    if (!current.busy && current.dirty && !window.confirm("仍有未保存修改，退出会清除这些本地内容。仍要退出吗？")) return;
    invalidateClientSession();
    onLogout();
  }, [onLogout]);

  /**
   * 保存成功后的选中：只认发起保存时所在的范围。
   *
   * 保存请求可能在用户切走之后才返回。此时按返回的用例 id 去选中，新项目里就会
   * 高亮一条属于旧项目的用例；失败提示同理，会挂在一个无关的项目上。下面几个
   * 回调都把发起时的 scope 关进闭包，回来时与当前范围对不上就直接丢弃。
   */
  const onCaseSaved = useCallback(
    (tabId: string, caseId?: string) => {
      if (liveScope.current !== scope) return;
      setCaseRefresh((value) => value + 1);
      if (caseId) {
        setEditors((current) => current.map((item) => item.tabId === tabId ? { ...item, id: caseId } : item));
        if (liveActiveEditorId.current === tabId) setSelectedCaseId(caseId);
      }
    },
    [scope, activeEditorId],
  );

  /**
   * 执行配置时钟：环境、项目变量或身份配置**成功变更**时推进。
   *
   * 它回答的是“屏幕上的请求将以什么身份、发到哪里”——这三类修改都会改变结论，所以已经
   * 算出的预检、以及基于旧配置的“当前通过”都要立即失效（R3 §3）。
   *
   * 时钟按**范围 + 主体**记账：切项目或换登录主体后是另一套配置，旧范围的完成回调不许
   * 推进新范围的时钟（否则新范围会凭空作废一份刚算好的结论）。
   *
   * `ref` 与 `state` 表示同一个时钟：异步回调在 `await` 之后、下一次渲染之前就要能读到
   * 已经成功提交的变化，只读 state 会读到旧值，于是失效来得比用户看到的晚一拍。
   */
  const configOwner = `${scope}/${session.user.user_id}`;
  const configClock = useRef({ owner: configOwner, epoch: 0 });
  if (configClock.current.owner !== configOwner) {
    configClock.current = { owner: configOwner, epoch: 0 };
  }
  const [configStamp, setConfigStamp] = useState(configClock.current);
  const configEpoch = configStamp.owner === configOwner ? configStamp.epoch : 0;

  const onExecutionConfigChanged = useCallback(() => {
    if (configClock.current.owner !== configOwner) return; // 旧范围的完成回调
    const next = { owner: configOwner, epoch: configClock.current.epoch + 1 };
    configClock.current = next;
    setConfigStamp(next);
  }, [configOwner]);

  const getConfigEpoch = useCallback(
    () => (configClock.current.owner === configOwner ? configClock.current.epoch : null),
    [configOwner],
  );

  const reloadEnvironments = environments.reload;
  const onEnvironmentsChanged = useCallback(() => {
    if (liveScope.current !== scope) return;
    // 先失效，再重拉：等列表读回来才让旧结论过期，中间那段时间用户会看到基于旧配置的
    // “可以发送／已通过”。
    onExecutionConfigChanged();
    reloadEnvironments();
  }, [scope, onExecutionConfigChanged, reloadEnvironments]);

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
          <span className="eyebrow">接口测试与巡检平台</span>
          <h1>接口工作台</h1>
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

      {projectList.length > 0 ? (
        <nav className="primary-nav" aria-label="主要功能">
          {(Object.keys(PAGE_LABEL) as AppPage[]).map((item) => (
            <button
              key={item}
              type="button"
              className={page === item ? "nav-item nav-item-active" : "nav-item"}
              aria-current={page === item ? "page" : undefined}
              onClick={() => {
                if (item === "environments") setSettingsSourceTabId(null);
                navigate(item);
              }}
            >
              {PAGE_LABEL[item]}
            </button>
          ))}
        </nav>
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
        <div className="app-pages">
          <section className="workspace" hidden={page !== "workbench"} aria-label="接口工作台">
            <aside className="sidebar">
            {/*
              左侧以**目录与用例**为主：这是日常动线。环境、变量、执行池与凭证是配置
              类操作，收进下面明确的次级入口，不再常驻占满侧栏——它们把用例列表挤到
              需要滚动才能看见，而列表才是每次都要用的那一个。

              全部原功能仍然可达（同一个组件、同一个权限判断），只是默认折叠。
            */}
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

              <button type="button" className="sidebar-config-link" onClick={() => { setSettingsSourceTabId(null); navigate("environments"); }}>
                环境与身份配置
              </button>
            </aside>

            <div className="main-pane">
              <div className="workspace-tabs">
                <div ref={workspaceTabScrollRef} className="workspace-tab-scroll" role="tablist" aria-label="已打开的请求">
                  {editors.map((item) => (
                    <button
                      key={item.tabId}
                      type="button"
                      role="tab"
                      aria-selected={item.tabId === activeEditorId}
                      aria-label={`${item.method} ${item.name}`}
                      className={item.tabId === activeEditorId ? "workspace-tab workspace-tab-active" : "workspace-tab"}
                      title={`${item.method} ${item.name}`}
                      ref={(node) => {
                        if (node === null) workspaceTabRefs.current.delete(item.tabId);
                        else workspaceTabRefs.current.set(item.tabId, node);
                      }}
                      onClick={() => {
                        setActiveEditorId(item.tabId);
                        setSelectedCaseId(item.id);
                      }}
                    >
                      <span className="workspace-tab-method">{item.method}</span>
                      <span className="workspace-tab-name">{item.name}</span>
                      {item.dirty ? <span aria-label="有未保存修改">●</span> : null}
                      {item.busy ? <span aria-label="操作进行中">处理中</span> : null}
                    </button>
                  ))}
                </div>
                <div className="workspace-tab-actions" aria-label="请求标签操作">
                  <span className={editors.length >= 20 ? "tab-limit tab-limit-reached" : "tab-limit"}>
                    已打开 {editors.length}/20{editors.length >= 20 ? "，已达上限" : ""}
                  </span>
                  <button ref={newRequestButtonRef} type="button" title={editors.length >= 20 ? "已达到 20 个标签上限，请先关闭标签" : undefined} onClick={() => createCase(null)} disabled={editors.length >= 20 || !canEdit(currentProject?.role ?? null)}>＋新请求</button>
                  {editor ? <button type="button" onClick={() => closeEditor()}>关闭当前</button> : null}
                  {editors.length > 1 ? <button type="button" onClick={() => requestCloseEditors(editors.filter((item) => item.tabId !== activeEditorId).map((item) => item.tabId))}>关闭其他</button> : null}
                  {editors.length > 0 ? <button type="button" onClick={() => requestCloseEditors(editors.map((item) => item.tabId))}>关闭全部</button> : null}
                </div>
              </div>
              {editors.length === 0 ? <Empty label="从左侧选择一条用例开始编辑，或点击“＋新请求”。" /> : null}
              {editors.map((item) => {
                const active = item.tabId === activeEditorId;
                return (
                  <div key={item.tabId} hidden={!active} aria-hidden={!active} className="workspace-editor">
                    <CaseEditor
                      key={item.nonce}
                      workspaceId={workspaceId}
                      projectId={projectId ?? ""}
                      caseSummaryId={item.id}
                      environments={environmentList}
                      selectedEnvironmentId={item.environmentId}
                      onSelectEnvironment={(next) => setEditors((current) => current.map((tab) => tab.tabId === item.tabId ? { ...tab, environmentId: next } : tab))}
                      onSaved={(caseId) => onCaseSaved(item.tabId, caseId)}
                      onVersionsChanged={onCaseVersionsChanged}
                      onClose={() => closeEditor(item.tabId)}
                      onCurrentVersion={active ? onEditorVersion : undefined}
                      folders={folders.data ?? []}
                      foldersLoading={folders.loading}
                      initialFolderId={item.folderId}
                      projectRole={currentProject?.role ?? null}
                      currentUserId={session.user.user_id}
                      onOpenAdmin={() => openAdminPanel(item.tabId)}
                      onOpenEnvironment={() => openEnvironmentPanel(item.tabId, item.environmentId)}
                      configEpoch={configEpoch}
                      getConfigEpoch={getConfigEpoch}
                      separateHistory
                      active={active && page === "workbench"}
                      leaveKey={`case-tab:${item.tabId}`}
                      onTabMeta={(meta) => setEditors((current) => current.map((tab) => tab.tabId === item.tabId && (tab.name !== meta.name || tab.method !== meta.method || tab.dirty !== meta.dirty || tab.busy !== meta.busy) ? { ...tab, ...meta } : tab))}
                      domIdPrefix={`tab-${item.tabId}`}
                      onRegisterCloseSave={(action) => {
                        if (action === null) closeControllers.current.delete(item.tabId);
                        else closeControllers.current.set(item.tabId, action);
                      }}
                    />
                  </div>
                );
              })}
            </div>
          </section>

          <section className="content-page" hidden={page !== "environments"} aria-label="环境配置">
            <header className="page-title-row">
              <div>
                <span className="eyebrow">当前项目</span>
                <h2>环境配置</h2>
                <p className="caption">维护请求目标、普通变量、访问规则和身份凭证。</p>
              </div>
              <button type="button" onClick={() => {
                const source = editors.find((item) => item.tabId === settingsSourceTabId);
                if (source) {
                  setActiveEditorId(source.tabId);
                  setSelectedCaseId(source.id);
                }
                setSettingsSourceTabId(null);
                navigate("workbench");
              }}>返回接口工作台</button>
            </header>
            <div className="settings-grid">
              <EnvironmentPanel
                key={`environment:${scope}`}
                workspaceId={workspaceId}
                projectId={projectId ?? ""}
                environments={environmentList}
                loading={environments.loading}
                error={environments.error ? environments.error.message : null}
                selectedId={settingsEnvironmentId}
                onSelect={setSettingsEnvironmentId}
                canEdit={canEdit(currentProject?.role ?? null)}
                onChanged={onEnvironmentsChanged}
                open={environmentOpen || environmentList.length === 0}
                onOpenChange={setEnvironmentOpen}
              />
              <AdminPanel
                key={`admin:${scope}`}
                onExecutionConfigChanged={onExecutionConfigChanged}
                workspaceId={workspaceId}
                projectId={projectId ?? ""}
                role={currentProject?.role ?? null}
                environments={environmentList}
                currentUser={{
                  user_id: session.user.user_id,
                  display_name: session.user.display_name,
                }}
                currentCase={editorVersion}
                anchorId="admin-panel"
                credentialsAnchorId="credentials-panel"
                credentialsOpen={credentialsOpen}
                onCredentialsOpenChange={setCredentialsOpen}
              />
            </div>
          </section>

          <section hidden={page !== "tasks"} aria-label="任务中心">
            <RunCenter
              key={`tasks:${scope}`}
              workspaceId={workspaceId}
              projectId={projectId ?? ""}
              environments={environmentList}
              canCancel={canEdit(currentProject?.role ?? null)}
              mode="tasks"
              active={page === "tasks"}
              onOpenReport={(runId) => {
                if (projectId === null) return;
                reportRequestToken.current += 1;
                setReportRequest({
                  runId,
                  token: reportRequestToken.current,
                  workspaceId,
                  projectId,
                });
                navigate("reports");
              }}
            />
          </section>

          <section hidden={page !== "reports"} aria-label="测试报告">
            <RunCenter
              key={`reports:${scope}`}
              workspaceId={workspaceId}
              projectId={projectId ?? ""}
              environments={environmentList}
              canCancel={false}
              mode="reports"
              active={page === "reports"}
              reportRequest={
                reportRequest?.workspaceId === workspaceId && reportRequest.projectId === projectId
                  ? reportRequest
                  : null
              }
            />
          </section>
        </div>
      )}
      {closeDialog ? (
        <div className="dialog-backdrop">
          <section className="close-dialog" role="dialog" aria-modal="true" aria-labelledby="close-dialog-title" onKeyDown={handleCloseDialogKeyDown}>
            <h2 id="close-dialog-title">关闭请求标签</h2>
            <p>以下标签包含尚未保存或尚未应用的内容，请选择一种处理方式。</p>
            <ul className="close-dialog-list">
              {liveEditors.current.filter((item) => closeDialog.tabIds.includes(item.tabId)).map((item) => {
                const root = closeControllers.current.get(item.tabId)?.state() ?? { dirty: item.dirty, busy: item.busy };
                const child = liveLeaveState.current.descendants(`case-tab:${item.tabId}`);
                const status = root.busy || child.busy
                  ? "操作进行中"
                  : child.dirty
                    ? "有尚未应用的输入"
                    : root.dirty
                      ? "有未保存修改"
                      : "可以关闭";
                return <li key={item.tabId}><strong>{item.method} {item.name}</strong><span>{status}</span></li>;
              })}
            </ul>
            <div className="actions">
              <button type="button" className="primary" disabled={closeDialog.saving} onClick={() => void finishCloseEditors(closeDialog.tabIds, "save", closeDialog.attemptId)}>{closeDialog.saving ? "正在保存…" : "保存并关闭"}</button>
              <button type="button" className="danger" disabled={closeDialog.saving} onClick={() => void finishCloseEditors(closeDialog.tabIds, "discard", closeDialog.attemptId)}>放弃修改并关闭</button>
              <button ref={closeDialogCancelRef} type="button" onClick={cancelCloseDialog}>取消</button>
            </div>
          </section>
        </div>
      ) : null}
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
