/**
 * 用例编辑：请求编辑、字段断言、cURL 导入、保存与发布、调试发送与响应。
 *
 * 草稿保存在服务端并用 ETag 做乐观锁：他人已修改时提示刷新，不静默覆盖。
 * 只有保存成功后才允许发布；发布产生不可变版本，执行固定在该版本上。
 *
 * 调试与保存／发布是**两条分开的路**：地址行旁的「发送」提交当前编辑内容的临时快照，
 * 不落用例、不产生版本，也不要求先保存；「保存并执行」仍是次级操作，固定已发布版本。
 *
 * 这个组件是**草稿与保存编排层**：请求工具栏、标签、响应、断言编辑与发送生命周期
 * 都由独立组件／Hook 承担，这里只负责把它们接起来（design 第 5 节的组件边界）。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ApiError, apiSend, apiSendWithMeta, projectPath } from "../api/client";
import { toAssertionTypes, toCaseDetail, toCaseVersion, toCurlPreview } from "../api/guards";
import type {
  AssertionResult,
  AssertionType,
  CaseAssertion,
  CaseDetail,
  CaseVersion,
  Environment,
  Folder,
  RunReport,
} from "../api/types";
import { ErrorText, Hint, Loading, Notice } from "../components/Feedback";
import { useLeaveReport } from "../hooks/leaveGuard";
import type { LeaveState } from "../hooks/leaveGuard";
import { useResource } from "../hooks/useResource";
import { ResponsePanel } from "../runs/ResponsePanel";
import { AuthTab, SendBar, type SendStage } from "../runs/SendBar";
import { isTerminal } from "../runs/useRuns";
import { submissionKey, useDebugRun, type DebugSubmission } from "../runs/useDebugRun";
import { AssertionTab } from "./AssertionTab";
import { AssertionColumn } from "./AssertionColumn";
import { removeAssertion, upsertAssertion } from "./assertionGroups";
import { CurlImport } from "./CurlImport";
import { CaseHeading } from "./CaseHeading";
import { unknownFolderLabel } from "./folderLabels";
import { BodyEditor, KeyValueRows } from "./RequestParts";
import { RequestTabs } from "./RequestTabs";
import { ResizableWorkbench } from "./ResizableWorkbench";
import { ResponseFieldPanel } from "./ResponseFieldPanel";
import { RunPanel, type RunProvenance } from "../runs/RunPanel";
import { emptyRequest, newRequestRow, rawToSpec, requestToRaw, sameRequest, upgradeRequestV2, type RawKeyValue, type RawRequest } from "./requestDraft";
import { useFieldTree } from "./useFieldTree";

/** 正文类型的短标签，用在请求体标签上；不写“有”这种没有信息量的词。 */
const BODY_TYPE_LABEL: Record<string, string> = {
  json: "JSON",
  text: "文本",
  form: "表单",
};

/**
 * 当前查看的运行：来源 + run_id。
 *
 * 同一个 run_id 在不同来源下代表不同的证据链（调试快照 vs 已发布版本），因此来源必须
 * 与 id 一起记录，不能只记 id 再回头猜它属于哪一类。
 */
interface RunSelection {
  /**
   * 读取入口：本次调试记录，还是项目／环境历史。
   *
   * 按**入口**而不是按执行目标类型命名：历史列表里既可能有版本运行，也可能有别的
   * 调试运行。真正的执行目标类型仍以 `report.run.target_type` 为准。
   */
  source: "debug" | "history";
  runId: string;
}

/** 编辑实例标识：随机、与用例 id 无关，挂载时生成一次。 */
function newEditorInstance(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/** 与后端 `permissions.py` 的 `edit` 动作一致：管理员与编辑者可以改，查看者不行。 */
function canEditRole(role: string): boolean {
  return role === "admin" || role === "editor";
}

/**
 * 断言标签上的提示。
 *
 * 只在“有需要注意的状态”时出现文字，不用颜色单独表达：认证缺配置不写出来，用户就
 * 得逐个标签点开才发现，而发送失败时也说不清是内容问题还是凭证问题。
 */
function authBadge(
  request: RawRequest,
  preflight: { auth: { required: boolean; state: string } } | null,
): string | null {
  if (request.auth_required === true) return "必须认证";
  if (preflight === null) return null;
  if (preflight.auth.state === "needs_authorization") return "待授权";
  if (preflight.auth.state === "ambiguous") return "身份歧义";
  if (preflight.auth.state === "unavailable") return "身份不可用";
  return null;
}

/** 新建用例尚未保存时的基线：只要用户动过任何一处，就算有未保存修改。 */
const BLANK_DRAFT = {
  name: "",
  request: emptyRequest(),
  assertions: JSON.stringify([]),
};

export function CaseEditor({
  workspaceId,
  projectId,
  caseSummaryId,
  environments,
  selectedEnvironmentId,
  onSelectEnvironment,
  onSaved,
  onClose,
  onCurrentVersion,
  onVersionsChanged,
  folders = [],
  foldersLoading = false,
  initialFolderId = null,
  projectRole = null,
  currentUserId = null,
  onOpenAdmin,
  onOpenEnvironment,
  configEpoch = 0,
  getConfigEpoch,
  separateHistory = false,
  active = true,
  leaveKey,
  onTabMeta,
  domIdPrefix,
  onRegisterCloseSave,
}: {
  workspaceId: string;
  projectId: string;
  caseSummaryId: string | null;
  environments: Environment[];
  selectedEnvironmentId: string | null;
  onSelectEnvironment: (environmentId: string) => void;
  onSaved: (caseId?: string) => void;
  onClose: () => void;
  /**
   * 当前打开的用例与它对应的已发布版本。
   *
   * 签发凭证用途授权时要选“哪条用例的哪一版”。让管理员自己去找 id，最容易选中的
   * 恰恰不是屏幕上这一份内容。这里把编辑器认定的那一版报上去，授权表单据此预选，
   * 管理员仍然可以改成别的用例或版本。
   */
  onCurrentVersion?: (target: { caseId: string; versionId: string | null } | null) => void;
  /**
   * 某条用例的已发布版本发生了变化（新固化一版，或复用了内容一致的已发布版本）。
   *
   * 左侧列表的“已发布 vN”来自用例列表接口，而那次拉取发生在**保存草稿**时：发布在保存
   * 之后，那次读到的还是没有新版本的列表。不在这里报一次，左侧就会一直停在“未发布”或
   * 落后一版（改到 v2～v5 时尤其明显），直到用户再保存一次才追上。
   */
  onVersionsChanged?: (caseId: string) => void;
  /** 当前项目的可选目录（与左侧目录树同一份清单，只含未归档目录）。 */
  folders?: Folder[];
  /** 目录清单是否仍在加载。加载中不能断言“这个目录已失效”——那会在界面上说错话。 */
  foldersLoading?: boolean;
  /** 新建用例时要落进的目录：从哪个目录点的「＋新建用例」就继承哪一个。 */
  initialFolderId?: string | null;
  /**
   * 当前项目内的角色。
   *
   * 只读角色（查看者）不获得新建、保存、授权或发送能力。前端据此收起入口并说明原因，
   * 但它不是权限边界——服务端仍按角色独立拒绝；这里只是不让用户对着必然失败的按钮。
   */
  projectRole?: string | null;
  /** 当前登录用户 id：就地授权要指定被授权人，只能是自己。 */
  currentUserId?: string | null;
  /** 打开独立环境配置页并定位身份配置；界面整理不改变原有能力。 */
  onOpenAdmin?: () => void;
  /**
   * 打开独立配置页里**环境**那一段的入口。
   *
   * 与 `onOpenAdmin` 分开，是因为两者要去的地方不同：地址类问题（例如环境地址缺协议）
   * 要送到环境编辑，凭证类问题要送到凭证列表。不传时退回 `onOpenAdmin`，老调用点不受影响。
   */
  onOpenEnvironment?: () => void;
  /**
   * 配置世代：环境、项目变量或身份配置成功变更时由外壳递增。
   *
   * 这些变更都会改变“这份内容将以什么身份、发到哪里”，因此已经算出的预检结论与正在
   * 进行的发送都不再有效。只覆盖本页面能观察到的变更，不臆测外部改动。
   */
  configEpoch?: number;
  /**
   * 读取**当前**配置世代。
   *
   * 供异步回调在 `await` 之后、下一次渲染之前核对"配置是否已经被改过"。只读 props 里的
   * `configEpoch` 会读到发起时的旧值，于是失效比用户看到的晚一拍。
   */
  getConfigEpoch?: () => number | null;
  /** App 已提供独立任务／报告页时，编辑器只保留版本执行动作。 */
  separateHistory?: boolean;
  /** 多标签宿主只允许活动实例响应全局快捷操作与上报活动授权目标。 */
  active?: boolean;
  /** 外壳可识别的离开登记键，用于按目标标签汇总关闭。 */
  leaveKey?: string;
  onTabMeta?: (meta: { name: string; method: string; dirty: boolean; busy: boolean }) => void;
  domIdPrefix?: string;
  onRegisterCloseSave?: (controller: { save: () => Promise<boolean>; state: () => LeaveState } | null) => void;
}) {
  // 新建的用例在保存后才有 id。这里自己记住它，避免“创建成功但再保存又建一条”。
  const [currentId, setCurrentId] = useState<string | null>(caseSummaryId);
  useEffect(() => setCurrentId(caseSummaryId), [caseSummaryId]);

  const detail = useResource<CaseDetail>(
    currentId ? `${workspaceId}/${projectId}/${currentId}` : null,
    (signal) =>
      apiSend(projectPath(workspaceId, projectId, `/cases/${currentId ?? ""}`), "GET", undefined, toCaseDetail, {
        signal,
      }),
  );
  const types = useResource<AssertionType[]>(`${workspaceId}/assertion-types`, (signal) =>
    apiSend("/assertion-types", "GET", undefined, toAssertionTypes, { signal }),
  );

  const [name, setName] = useState("");
  const [request, setRequest] = useState<RawRequest>(emptyRequest);
  const legacyRowIdsRef = useRef(new WeakMap<object, string>());
  const [assertions, setAssertions] = useState<CaseAssertion[]>([]);
  const nameRef = useRef(name);
  const requestRef = useRef(request);
  const assertionsRef = useRef(assertions);
  nameRef.current = name;
  requestRef.current = request;
  assertionsRef.current = assertions;
  /**
   * 所属目录。`null` 是**未分组**这个明确取值，不是“还没加载”。
   *
   * 新建时初值是外壳传来的 `initialFolderId`（从哪个目录点的新建就落进哪个目录）；
   * 已有用例则由详情回填。它与 `name`／`request`／`assertions` 一起参与基线与脏状态：
   * 只改目录也是一次未保存修改，必须同样被离开保护与 `If-Match` 冲突保护覆盖。
   */
  const [folderId, setFolderId] = useState<string | null>(initialFolderId);
  /**
   * 最近一次渲染里的目录选择。
   *
   * 保存请求在飞的时候用户还能继续改目录：响应回来时不能拿服务端回显把**更新**的选择
   * 盖掉——那是用户没做过的改动，界面上会跳回去且不留痕迹。推进已经提交的那一份基线，
   * 把新的选择留在表单里（于是照常显示“有未保存修改”，下次保存再提交它）。
   */
  const folderIdRef = useRef(folderId);
  folderIdRef.current = folderId;
  const [etag, setEtag] = useState<string | null>(null);
  // 当前草稿在服务端的修订号；发布时必须声明固化的是这一版。
  const [draftRev, setDraftRev] = useState<number | null>(null);
  const [baseline, setBaseline] = useState<{
    name: string;
    request: RawRequest;
    assertions: string;
    folderId: string | null;
  } | null>(null);
  const baselineSyncRef = useRef(baseline);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [versions, setVersions] = useState<CaseVersion[]>([]);
  /**
   * 版本列表的读取世代。
   *
   * 每一次列表读取与每一次**服务端确认的版本变化**都推进它。读取结果只有带着发起时的
   * 世代回来才被接纳：发布之前发出的那次读取读到的是还没有这一版的那一份，它晚到一步
   * 就会把界面打回“尚未发布任何版本”，而数据库里版本确实已经存在。只做“最后一次读取
   * 生效”是不够的——发布不是读取，它同样必须能作废在飞的读取。
   */
  const versionsEpochRef = useRef(0);
  /** 服务端算出的当前草稿快照摘要；“已发布版本是否仍代表屏幕内容”以它为准。 */
  const [draftSnapshotHash, setDraftSnapshotHash] = useState<string | null>(null);
  /** 已发布版本运行的报告；调试报告由 useDebugRun 持有。 */
  const [versionReport, setVersionReport] = useState<RunReport | null>(null);
  /**
   * 当前查看的运行：**来源 + run_id**，正文、字段结论、断言与取消都由它驱动。
   *
   * 用一个显式选择而不是“最近到达的报告”：报告是异步到达的，任何一次迟到都可能把
   * 界面从一种来源切到另一种，出现“正文是调试 B、字段区却是版本 A”这种自相矛盾的画面。
   */
  const [selection, setSelection] = useState<RunSelection | null>(null);
  /**
   * 版本运行的**执行配置依据**：受理那一刻的 run_id、环境与配置世代。
   *
   * 历史列表里的报告只能说明“这条运行跑过”，不能证明它按**当前**配置跑过。因此“当前
   * 字段结论”必须要求这份依据存在且与当前配置一致；仅凭点选历史不能凭空造出它。
   *
   * 由 `onRunSubmitted`（真实受理）写入，`handleHistoryReport` 绝不写入——后者只知道
   * “收到了一份旧报告”，不知道它当时用的是哪份配置。
   */
  const [versionProvenance, setVersionProvenance] = useState<{
    runId: string;
    environmentId: string | null;
    configEpoch: number;
  } | null>(null);
  const [creating, setCreating] = useState(false);
  const [versionOperationActive, setVersionOperationActive] = useState(false);
  /** 请求标签；默认停在参数上，首屏即可看到地址、环境与发送。 */
  const [activeTab, setActiveTab] = useState("params");
  const [sendError, setSendError] = useState<string | null>(null);

  /**
   * 只读由**明确的角色**决定，不由“没传角色”推断。
   *
   * 缺省当成无权会让所有未传角色的调用方（测试、以及将来别的宿主）看到一个按钮齐全
   * 却全部失效的界面，而真正该收起入口的是服务端已经判定为查看者的那一种。未知角色
   * 不在这里收紧：权限判据在服务端，前端只能如实呈现它拿到的角色。
   */
  const readOnly = projectRole !== null && projectRole !== undefined && !canEditRole(projectRole);
  const readOnlyReason = readOnly
    ? "当前项目角色是查看者：可以查看请求、断言与历史报告，但不能新建、保存、授权或发送。"
    : null;

  /**
   * 编辑实例标识：**挂载时生成一次**，切换用例由外壳按 nonce 重挂载来换值。
   *
   * 刻意不用 `currentId` 参与：新建用例首次保存会让它从 `null` 变成 id，但那是**同一次
   * 编辑、同一份内容**——换标识会把本次调试历史与正在受理的运行一起清空，而用户什么都
   * 没有切换。真正该失效的只有换用例／换项目／换主体／卸载。
   */
  const [editorKey] = useState(() => newEditorInstance());
  const editRevisionRef = useRef(0);
  const saveInFlightRef = useRef<Promise<CaseDetail | null> | null>(null);
  const editorWriteGateRef = useRef(false);
  const shortcutLockRef = useRef(false);
  const executionGateRef = useRef<"debug" | "version" | null>(null);
  const onTabMetaRef = useRef(onTabMeta);
  onTabMetaRef.current = onTabMeta;
  const principalRef = useRef(currentUserId);
  const environmentRef = useRef(selectedEnvironmentId);
  principalRef.current = currentUserId;
  environmentRef.current = selectedEnvironmentId;
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => { aliveRef.current = false; };
  }, []);
  const debugReadOnly = readOnly;

  function currentExecutionEpoch(): number | null {
    return getConfigEpoch === undefined ? configEpoch : getConfigEpoch();
  }

  function ownsEditor(owner: { workspaceId: string; projectId: string; editorKey: string; principalId: string | null }): boolean {
    return aliveRef.current && owner.workspaceId === workspaceId && owner.projectId === projectId && owner.editorKey === editorKey && owner.principalId === principalRef.current;
  }

  /**
   * 刚刚由本页写入的那一份详情。
   *
   * 新建成功后服务端会重新拉一次这条用例，而重新拉取有一小段时间是空的：不能因为
   * “当前这一版还没到”就把表单退回未就绪状态，那样刚建好的用例会闪一下、并在
   * 用户继续输入时被随后的回填覆盖。以服务端返回的那一份为准，直到服务端版本更高。
   */
  const detailKey = currentId ? `${workspaceId}/${projectId}/${currentId}` : null;
  const [localDetail, setLocalDetail] = useState<{ key: string; detail: CaseDetail } | null>(null);
  const effectiveDetail = useMemo<CaseDetail | null>(() => {
    const server = detail.data;
    const local = localDetail !== null && localDetail.key === detailKey ? localDetail.detail : null;
    if (local === null) return server;
    if (server === null) return local;
    return server.rev >= local.rev ? server : local;
  }, [detail.data, localDetail, detailKey]);

  /**
   * 表单当前反映的是**哪一条用例的哪一版**。
   *
   * 它同时是两件事的判据：
   *
   * 1. 同一版重复到达时不再回填。只记修订号不够：两条不同的用例可以有相同的修订号，
   *    于是“切换到另一条用例后新详情到达”会被误判成“同一版重复到达”，表单留着上一条
   *    用例的名称与断言，保存时却写到新那一条上——数据被贴上了别人的标签。这一版属于
   *    谁，必须和修订号一起记。
   * 2. 表单什么时候算就绪（见下面的 ready）。它必须是 state 而不能只是 ref：就绪与否
   *    要参与渲染判断，而“详情非空”与“内容已应用”之间隔着一次提交。
   */
  const [appliedStamp, setAppliedStamp] = useState<{ key: string; rev: number } | null>(null);

  /**
   * 载入某一用例时重置全部编辑状态，避免上一条用例的草稿串到这一条。
   *
   * 回填只在**真的换了一版内容**时发生：同一次保存触发的重新拉取会带回同一修订号，
   * 那一次到达不该把用户在这之间敲进去的内容重新盖掉。用户还没见到已有用例的内容
   * 之前，表单也不开放编辑（见下面的 ready）。
   */
  useEffect(() => {
    if (currentId === null) return; // 新建草稿：空白表单就是它的初始状态
    const data = effectiveDetail;
    if (data === null) return;
    const stamp = { key: detailKey ?? "", rev: data.rev };
    if (appliedStamp !== null && appliedStamp.key === stamp.key && appliedStamp.rev === stamp.rev) {
      return;
    }
    setAppliedStamp(stamp);
    setName(data.name);
    const raw = requestToRaw(data.request);
    setRequest(raw);
    setAssertions(data.assertions);
    // 目录随详情一起回填：重新打开用例必须回到它保存时所在的目录，而不是停在
    // 上一次编辑留下的选择上。
    setFolderId(data.folder_id);
    setEtag(`"${data.rev}"`);
    setDraftRev(data.rev);
    setBaseline({
      name: data.name,
      request: raw,
      assertions: JSON.stringify(data.assertions),
      folderId: data.folder_id,
    });
    setNotice(null);
    setError(null);
    setDraftSnapshotHash(data.snapshot_hash);
    setVersionReport(null);
    setSelection(null);
  }, [effectiveDetail, currentId, detailKey, appliedStamp]);

  /**
   * 读一次这条用例的版本列表。
   *
   * 返回而不是只写 state：判断“要不要固化新版本”用的是**读到的那一份**。新用例刚保存
   * 完的一瞬间 state 里还是空的，只按它判断会把“已经有同内容的版本”误判成“必须发布”。
   */
  const fetchVersions = useCallback(
    async (caseId: string): Promise<CaseVersion[]> =>
      apiSend(
        projectPath(workspaceId, projectId, `/cases/${caseId}/versions`),
        "GET",
        undefined,
        (raw) => (Array.isArray(raw) ? raw.map(toCaseVersion) : []),
      ),
    [workspaceId, projectId],
  );

  /**
   * 读一次这条用例的版本列表，并按世代接纳。
   *
   * 返回读到的那一份而不是只写 state：判断“要不要固化新版本”用的是**读到的那一份**。
   * 新用例刚保存完的一瞬间 state 里还是空的，只按它判断会把“已经有同内容的版本”误判成
   * “必须发布”。读取失败返回 `null`（不静默当成空列表，那会让发布流程以为没有任何版本
   * 可复用而多固化一版），并只在世代仍有效时才把失败落到界面。
   */
  const loadVersions = useCallback(
    async (caseId: string): Promise<CaseVersion[] | null> => {
      const epoch = ++versionsEpochRef.current;
      try {
        const loaded = await fetchVersions(caseId);
        // 世代对不上说明期间有发布确认、或又发起了更新的读取：那一份更新，这一份作废。
        if (versionsEpochRef.current === epoch) setVersions(loaded);
        return loaded;
      } catch (cause) {
        if (versionsEpochRef.current === epoch) {
          setVersions([]);
          setError(cause instanceof Error ? cause.message : "版本列表加载失败");
        }
        return null;
      }
    },
    [fetchVersions],
  );

  /**
   * 原子接纳服务端刚确认的一版。
   *
   * 发布或复用成功时，服务端返回的那一版就是事实：直接并进列表，不等随后再读一次。
   * 这样“提示已发布 v1”与版本区、授权选择器看到的是同一份数据，中间不存在一个
   * “已经发布但界面还不知道”的窗口。
   *
   * 同时推进读取世代，作废发布之前发出的那次列表读取；并入时只保留同一条用例的版本，
   * 列表里可能还留着切走之前那条用例的内容。
   */
  const acceptVersion = useCallback((caseId: string, version: CaseVersion) => {
    versionsEpochRef.current += 1;
    setVersions((previous) => {
      const sameCase = previous.filter(
        (item) => item.case_id === caseId && item.id !== version.id,
      );
      return [version, ...sameCase].sort((left, right) => right.version - left.version);
    });
  }, []);

  useEffect(() => {
    if (currentId === null) {
      // 新建草稿还没有服务端身份：清掉上一条用例的版本，并推进世代让它在飞的读取也失效。
      versionsEpochRef.current += 1;
      setVersions([]);
      return;
    }
    void loadVersions(currentId);
  }, [loadVersions, currentId]);

  // 新建的用例没有服务端基线。此时基线就是"空白草稿"，不能因为 baseline 为 null
  // 就当作没有改动：那会让用户在新用例里敲进去的内容被切换项目／关闭编辑器静默丢弃，
  // 离开保护也就形同虚设。目录同样要进基线：新用例的初值来自“从哪个目录点的新建”，
  // 基线取同一个值，才不会一打开就被算成改过目录。
  const effectiveBaseline = baseline ?? { ...BLANK_DRAFT, folderId: initialFolderId };
  baselineSyncRef.current = effectiveBaseline;

  const dirty = useMemo(() => {
    return (
      effectiveBaseline.name !== name ||
      !sameRequest(effectiveBaseline.request, request) ||
      effectiveBaseline.assertions !== JSON.stringify(assertions) ||
      effectiveBaseline.folderId !== folderId
    );
  }, [effectiveBaseline, name, request, assertions, folderId]);

  /** v1 只为呈现生成稳定行视图；不写回 state，因此纯读取不 dirty、不改 hash。 */
  const presentedUpgrade = useMemo(() => {
    if (request.schema_version === 2) return upgradeRequestV2(request, assertions);
    const stableId = (row: RawKeyValue): string => {
      const existing = legacyRowIdsRef.current.get(row);
      if (existing !== undefined) return existing;
      const created = newRequestRow(row).row_id;
      legacyRowIdsRef.current.set(row, created);
      return created;
    };
    return upgradeRequestV2(request, assertions, {
      query_params: request.query_params.map(stableId),
      headers: request.headers.map(stableId),
    });
  }, [request, assertions]);
  const presentedRequest = request.schema_version === 2 ? request : presentedUpgrade.request;
  const presentedAssertions = request.schema_version === 2 ? assertions : presentedUpgrade.assertions;

  /**
   * 当前值指向的目录是否已经不在可选清单里（被归档、被删、或不属于这个项目）。
   *
   * 单独算出来是为了**明确处理**，而不是把它当成未分组：把它显示成「未分组」等于在
   * 界面上宣布一个用户没做过的改动，保存时还可能真的把用例移出原目录。
   * 清单还在加载时不下这个结论——那时 `folders` 是空的，结论只会是错的。
   */
  const folderUnavailable =
    !foldersLoading && folderId !== null && !folders.some((item) => item.id === folderId);
  /** 用户是否真的动过目录选择；只有动过才在保存时提交 `folder_id`（见 save）。 */
  const folderChanged = effectiveBaseline.folderId !== folderId;

  /**
   * 可编辑的条件是**这一条用例的这一版内容已经应用到表单上**，而不只是“详情到达了”。
   *
   * 详情到达与回填之间隔着一次提交：只按“详情非空”开放编辑，表单会先按上一个范围的
   * 残留内容（切换用例时是空的）画出来，再靠随后的副作用改成服务端内容。用户在这个
   * 中间态里打进去的字会被那次回填覆盖，而界面上看不出发生过什么——这是可复现的中间
   * 提交，不是理论担忧（CaseEditor.loading.test.tsx 里用 MutationObserver 盯住了它）。
   *
   * setAppliedStamp 与回填的其余 setState 在同一次副作用里排队，会被合并进同一次提交，
   * 因此表单第一次带着输入框出现时，里面就是服务端那一版内容。
   *
   * 新建的用例是另一种状态：它没有“已有内容”要等，空白草稿本身就是初始状态，因此
   * 从一开始就可编辑。
   */
  const ready =
    currentId === null ||
    (effectiveDetail !== null &&
      appliedStamp !== null &&
      appliedStamp.key === detailKey &&
      appliedStamp.rev === effectiveDetail.rev);

  /**
   * 屏幕上这条用例对应的已发布版本：摘要与服务端草稿摘要相同的那一版。
   *
   * 摘要由服务端对 `{name, request, assertions}` 规范化后算出，发布时用的是同一个
   * 函数，所以“摘要相同”就是“发布的就是屏幕上这一份内容”。前端不再自己拼一份签名：
   * 那样只在“本次会话里点过发布”时才有值，重新打开页面就会把内容没变的用例当成需要
   * 重新发布，于是每次执行都固化出一个新版本——固定到某一条用例版本的用途授权因此
   * 永远匹配不上执行的那一版，而界面上一切正常。
   *
   * 版本列表按版本号倒序，取到的是最新一版同内容的版本。`case_id` 必须一起核对：
   * 切换用例的一瞬间列表可能还是上一条用例的。
   */
  const matchingVersion = useMemo(
    () =>
      draftSnapshotHash === null
        ? null
        : (versions.find(
            (item) => item.snapshot_hash === draftSnapshotHash && item.case_id === currentId,
          ) ?? null),
    [versions, draftSnapshotHash, currentId],
  );

  /**
   * 执行前是否必须先走一次“保存并确保版本”（`saveThenPublish`）。
   *
   * 有未保存改动时要走：那些改动必须先按 ETag 落库。只看“是否有未保存修改”不够——
   * 先保存草稿而不发布，草稿摘要就与任何已发布版本都不同，同样得走一遍。
   *
   * 走这一步**不等于**会新增版本：目录这类不属于执行快照的改动，摘要与已有版本相同，
   * 保存后就复用那一版（见 `saveThenPublish`）。
   */
  const mustEnsureVersion = dirty || matchingVersion === null;

  // 把“当前用例 + 它对应的已发布版本”报给外壳，供授权表单预选。
  const currentVersionId = matchingVersion?.id ?? null;
  useEffect(() => {
    if (currentId === null) return;
    if (!active) return;
    onCurrentVersion?.({ caseId: currentId, versionId: currentVersionId });
    // 编辑器卸载（切换用例、关闭、切换范围）时撤销上报：留着它会让授权表单按一条
    // 已经不在屏幕上的用例预选。
    return () => onCurrentVersion?.(null);
  }, [active, currentId, currentVersionId, onCurrentVersion]);

  const bodyText = request.body_type === "json" ? request.body : "";
  // 请求正文树的来源标识就是正文本身：正文一变就是另一份数据。
  const bodyTree = useFieldTree(workspaceId, projectId, bodyText, bodyText, active);

  /**
   * 当前表单对应的请求定义；不合法时给出原因而不抛到渲染里。
   *
   * 发送与预检都必须基于**同一份**规范化结果：预检按它算摘要，发送按它提交快照，
   * 两边各拼一次就会出现“预检说可以、发送说内容变了”。
   */
  const currentSpec = useMemo(() => {
    try {
      return { spec: rawToSpec(request, assertions), error: null as string | null };
    } catch (cause) {
      return {
        spec: null,
        error: cause instanceof Error ? cause.message : "请求定义不合法",
      };
    }
  }, [request, assertions]);

  const currentSubmission = useMemo<DebugSubmission | null>(() => {
    if (selectedEnvironmentId === null || currentSpec.spec === null || debugReadOnly) return null;
    return {
      environmentId: selectedEnvironmentId,
      request: currentSpec.spec,
      assertions,
    };
  }, [selectedEnvironmentId, currentSpec.spec, assertions, debugReadOnly]);

  /**
   * 当前内容的稳定键。
   *
   * 它与预检结论记录的键同源（都是 `submissionKey`），因此“两个键相等”就等于“那份
   * 结论针对的正是现在屏幕上这份内容”。用户改一个字符，这个键立即变化，旧结论当场失去
   * 效力——不用等 400ms 防抖重检，也就不会读到一段假的“通过”。
   */
  const currentSnapshotKey = currentSubmission === null ? null : submissionKey(currentSubmission);

  /**
   * 受理一条运行成功时切到它的来源。
   *
   * 这是**允许改变查看选择**的两个事件之一（另一个是用户点选历史记录）。稳定引用：
   * Hook 把它用在已发出的提交闭包里，每次渲染换新函数没有意义。
   */
  const onDebugRunAccepted = useCallback((runId: string) => {
    setSelection({ source: "debug", runId });
  }, []);

  /**
   * 一次发送的完整生命周期。
   *
   * 传入当前内容键：Hook 据此判断“预检结论／未提交操作是否还对应屏幕上的内容”，
   * 因此内容一变旧提示当场失效，不用等防抖重检。
   */
  const debug = useDebugRun(
    workspaceId,
    projectId,
    editorKey,
    configEpoch,
    currentUserId ?? "",
    currentSnapshotKey,
    onDebugRunAccepted,
    getConfigEpoch,
  );
  const debugAcceptancePending = debug.phase !== "idle" && debug.phase !== "running";
  const closeOperationPendingRef = useRef(false);
  closeOperationPendingRef.current = debugAcceptancePending || versionOperationActive;

  // 上报离开状态：范围切换与关闭由外壳统一拦一次，避免每个入口各写一份判断。
  // **未结束的调试操作同样计入 busy**：离开发送中的链路会让用户既看不到受理结果，也
  // 不会知道它是否已经产生副作用；unknown 也属于未结束，不能因为阶段名里没有“运行”
  // 就当成空闲。
  useLeaveReport(leaveKey ?? `case:${workspaceId}/${projectId}/${editorKey}`, {
    dirty,
    busy: busy || debugAcceptancePending || versionOperationActive,
  });
  useEffect(() => {
    onTabMetaRef.current?.({ name: name.trim() || "新请求", method: request.method, dirty, busy: busy || debugAcceptancePending || versionOperationActive });
  }, [name, request.method, dirty, busy, debugAcceptancePending, versionOperationActive]);

  /**
   * 内容或环境变化后自动预检一次（防抖）。
   *
   * 预检是只读的：不解密秘密、不消费授权、不创建运行。它存在的意义是让按钮旁边能写出
   * “缺什么、该做什么”，而不是只给一个不可点击的按钮。真正发送时还会再检查一次，
   * 因此这里的结论过期不会导致越权发送。
   */
  useEffect(() => {
    if (!active || currentSubmission === null) return;
    const timer = window.setTimeout(() => {
      void debug.runPreflight(currentSubmission);
    }, 400);
    return () => window.clearTimeout(timer);
    // debug.runPreflight 是稳定回调；把它放进依赖会让每次渲染都重排一次防抖。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, currentSnapshotKey, editorKey, configEpoch]);

  /**
   * 发送当前编辑内容。
   *
   * 整条链（预检 → 可能的授权 → 提交受理）由 `debug.start` 按**一个**操作上下文管理：
   * 内容在这里冻结，锁在点击的那一帧就锁住。因此双击不会建出两条链，授权期间的编辑也
   * 不会改变已经提交的内容。
   */
  async function sendDebug() {
    setSendError(null);
    if (versionOperationActive || executionGateRef.current !== null) {
      setSendError("同一标签已有版本运行正在确认，请先处理完成。");
      return;
    }
    if (selectedEnvironmentId === null) {
      setSendError("请先选择执行环境。");
      return;
    }
    if (currentSubmission === null) {
      setSendError(currentSpec.error ?? "请求定义不合法");
      return;
    }
    const environment = environments.find((item) => item.id === selectedEnvironmentId) ?? null;
    executionGateRef.current = "debug";
    try {
      await debug.start(currentSubmission, environment?.name ?? selectedEnvironmentId);
    } finally {
      executionGateRef.current = null;
    }
  }

  /** 用户确认授权并发送；摘要与签发都由服务端完成，用户不接触内部摘要。 */
  async function confirmAuthorization() {
    setSendError(null);
    await debug.confirmAuthorization();
  }

  /** 取消授权确认：只丢弃本地操作，不创建运行。 */
  function cancelAuthorization() {
    setSendError(null);
    debug.cancelOperation();
  }

  /**
   * 这份预检结论是不是针对**当前内容**算的。
   *
   * 编辑会让 `currentSnapshotKey` 立即变化，而 `debug.preflightFor` 仍是上一次预检的键，
   * 两者不等即当场失效——不等防抖重检，也就不会在那 400ms 里把旧结论当成新内容的通过。
   */
  const preflightIsCurrent =
    debug.preflightFor !== null && debug.preflightFor === currentSnapshotKey;

  /**
   * 当前查看的运行对应的报告。
   *
   * **只按 `(source, run_id)` 取**：先按当前选择的来源确定候选来源，再要求候选报告的
   * `run.id` 与所选 id 完全一致。不匹配时返回 null——旧报告可以留在缓存里供以后查看，
   * 但绝不作为当前正文、状态或终态判据。绕开这道校验的旁路（直接拿“最近一份报告”）
   * 正是“正文显示调试 B、字段区却是版本 A”以及“r2 报告读失败却显示 r1 的 200”的来源。
   */
  const displayedReport: RunReport | null = useMemo(() => {
    if (selection === null) return null;
    const candidate = selection.source === "debug" ? debug.reports[selection.runId] : versionReport;
    if (candidate === undefined || candidate === null) return null;
    return candidate.run.id === selection.runId ? candidate : null;
  }, [selection, debug.reports, versionReport]);

  const displayedReportError =
    selection === null
      ? null
      : selection.source === "debug"
        ? debug.reportErrors[selection.runId] ?? null
        : null;

  /**
   * 这份报告与当前编辑内容是否同源。
   *
   * 判据来自**服务端**：预检按当前内容算出 context，报告带着生成时冻结的 context。
   * 两个标记相等，说明“这份报告就是按现在屏幕上这份内容产生的”。前端没有能力自己算这个
   * 标记（也不该有——那会变成又一个可以离线比对的内容摘要入口）。
   *
   * 另外两项：报告必须属于当前选中的那条运行；调试运行还要求环境一致——切了环境之后旧
   * 报告仍可查看，但不把它的通过贴到别的内容上。
   */
  /**
   * 当前查看的那条调试运行**当时**提交到的环境。
   *
   * 按选择的 run_id 查，而不是读“活动运行”的登记：运行到达终态后活动锁会释放，
   * 那时的活动运行是空的。用它做判据会让一份刚刚真正跑完、环境也没变的报告在结束的
   * 瞬间被判成过期——而这与用户看到的事实相反。环境是**那条运行**的属性。
   */
  const selectedDebugRecord = useMemo(
    () => debug.records.find((item) => item.runId === selection?.runId) ?? null,
    [debug.records, selection],
  );

  const debugMatchesCurrent =
    displayedReport !== null &&
    selection?.source === "debug" &&
    displayedReport.run.target_type === "debug_snapshot" &&
    selectedDebugRecord?.environmentId === selectedEnvironmentId &&
    displayedReport.context !== null &&
    preflightIsCurrent &&
    debug.preflight?.context != null &&
    // 两个标记都要比：snapshot 覆盖请求与断言，input 覆盖普通变量；环境除了 id 还要比
    // 地址——同一条环境记录被改了 base_url，运行就不再是打到同一个目标。
    displayedReport.context.snapshot_fingerprint === debug.preflight.context.snapshot_fingerprint &&
    displayedReport.context.input_fingerprint === debug.preflight.context.input_fingerprint &&
    displayedReport.context.environment.id === debug.preflight.context.environment.id &&
    displayedReport.context.environment.base_url === debug.preflight.context.environment.base_url;

  /**
   * 已发布版本运行的匹配判断。
   *
   * 要求：报告确实属于当前选择的那条运行、同环境、无未保存改动，且它执行的版本快照与
   * 当前草稿摘要一致。名称与目录不属于执行内容，因此只改目录不会被算成“内容变了”。
   */
  const reportedVersion = useMemo(
    () => versions.find((item) => item.id === versionReport?.run.case_version_id) ?? null,
    [versions, versionReport],
  );
  const versionProvenanceMatches =
    versionProvenance !== null &&
    versionProvenance.runId === displayedReport?.run.id &&
    versionProvenance.environmentId === selectedEnvironmentId &&
    // 配置世代一致：环境、项目变量或身份配置被改过之后，旧运行就不再能代表当前配置。
    versionProvenance.configEpoch === configEpoch;

  const versionRunMatches =
    displayedReport !== null &&
    selection?.source === "history" &&
    displayedReport.run.target_type === "case_version" &&
    displayedReport.run.environment_id === selectedEnvironmentId &&
    versionProvenanceMatches &&
    !dirty &&
    draftSnapshotHash !== null &&
    reportedVersion !== null &&
    reportedVersion.snapshot_hash === draftSnapshotHash;

  /**
   * 项目／环境历史上报的运行报告。
   *
   * 它**只更新缓存**，不改变查看选择：选择只由显式事件驱动（受理成功、用户点选），因此
   * 后台报告到达不会把界面从一种来源切到另一种。
   *
   * 两件事写在这里：
   *
   * 1. **稳定引用。** RunPanel 的上报 effect 依赖这个回调（见 `RunPanel` 的
   *    `[report.data, onReport]`）；每次渲染换一个新函数会让它在上报条件成立时重新执行。
   * 2. **更新必须幂等。** 值没变时必须原样返回 `current`：状态更新函数返回新对象会被 React
   *    当作“有变化”，从而继续重渲染；若上报方在 effect 里依赖这次渲染的结果，就成了
   *    “上报 → 新对象 → 重渲染 → 上报”的闭环，界面会卡死。
   */
  const handleHistoryReport = useCallback((next: RunReport) => {
    setVersionReport(next);
  }, []);

  /**
   * 捕获本次版本提交的执行配置依据。
   *
   * 由 RunPanel 在**发起 POST 之前**调用，因此读到的是“这次提交实际用的配置”。受理响应
   * 要等一次网络往返，期间配置可能已被改（管理面板刚保存成功）：等响应回来再读时钟，就会
   * 把一条按旧配置跑的运行记成按新配置跑的，旧结论于是又匹配上当前。
   *
   * 同步时钟不可用时返回 null——那说明范围已经失效，调用方据此不发起写请求。
   *
   * **两种“没有值”必须分开**：getter 不存在（外层没接时钟）时回退到 props 里的世代；
   * getter 存在但返回 `null`（范围已失效）时必须**原样传出 null**。用 `?.() ?? configEpoch`
   * 会把后者换成旧世代，于是一条已经失效的提交照样发出运行请求，并被登记成“当前配置的
   * 依据”——正是这条分支要防的事。
   */
  const captureRunProvenance = useCallback((): RunProvenance | null => {
    const epoch = getConfigEpoch === undefined ? configEpoch : getConfigEpoch();
    if (epoch === null) return null;
    if (selectedEnvironmentId === null) return null;
    return { environmentId: selectedEnvironmentId, configEpoch: epoch };
  }, [getConfigEpoch, configEpoch, selectedEnvironmentId]);

  /**
   * 版本运行被受理：原样登记提交前捕获的那份依据。
   *
   * **不在此刻读取时钟**：到这里时配置可能已经变了，而这次运行用的仍是提交时的配置。
   */
  const handleRunSubmitted = useCallback((runId: string, provenance: RunProvenance) => {
    setVersionProvenance({ runId, ...provenance });
  }, []);

  /** 用户点选项目／环境历史里的某条运行：这是**显式**的来源切换。 */
  const selectHistoryRun = useCallback((runId: string) => {
    setSelection((current) =>
      current !== null && current.source === "history" && current.runId === runId
        ? current
        : { source: "history", runId },
    );
  }, []);

  /**
   * 字段行旁的结论能不能贴回当前条件。
   *
   * 调试运行与版本运行走**两套**匹配判据，不能共用：调试没有 `case_version_id`，而
   * “有未保存改动”恰恰是调试的常态——把调试也算进 `dirty` 判断，刚真跑通过的结果会
   * 立刻被标成未执行；反过来直接去掉 `dirty` 判断，改一条断言就会借用旧通过。
   */
  const activeMatches = selection?.source === "history" ? versionRunMatches : debugMatchesCurrent;

  /**
   * 发送入口呈现哪个阶段。
   *
   * 由权威阶段推导。需要撤掉普通发送入口的是三个阶段：**等待授权确认**（改由确认面板
   * 承担）、**受理结果不明**（只能确认旧受理）、以及**运行中**（锁要持有到该运行的终态，
   * 否则排队期间又能用新键发一份新内容）。
   */
  const sendStage: SendStage =
    debug.phase === "acceptance_unknown"
      ? "acceptance_unknown"
      : debug.phase === "awaiting_authorization"
        ? "awaiting_authorization"
        : debug.phase === "idle"
          ? "idle"
          : debug.phase === "running"
            ? "running"
            : "pending";

  const results = useMemo(() => {
    const map = new Map<string, AssertionResult>();
    for (const item of displayedReport?.assertions ?? []) {
      // 与本次执行对不上的旧结论统一标成未执行，并且不保留期望／实际：那是上一轮的
      // 取值，不是当前条件的证据。原始报告仍在“本次记录”与“项目／环境历史”里完整可查。
      map.set(
        item.assertion_id,
        activeMatches
          ? item
          : { ...item, status: "skipped", expected: null, actual: null, reason_code: "stale_run" },
      );
    }
    return map;
  }, [displayedReport, activeMatches]);

  async function performSave(): Promise<CaseDetail | null> {
    const owner = { workspaceId, projectId, editorKey, principalId: principalRef.current };
    setError(null);
    setNotice(null);
    let spec;
    try {
      spec = rawToSpec(request, assertions);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "请求定义不合法");
      return null;
    }
    setBusy(true);
    /**
     * 这次请求提交的目录。
     *
     * 请求在飞的时候目录选择仍可改（保存不该把整个表单冻住）；响应回来时只有用户没再
     * 动过它，才接受服务端回显的那一份，否则会盖掉用户刚做的选择。
     */
    const submittedFolderId = folderId;
    try {
      if (currentId === null) {
        const created = await apiSendWithMeta(
          projectPath(workspaceId, projectId, "/cases"),
          "POST",
          // 目录来自外壳（从哪个目录点的新建就落进哪个目录）。恒定传 `null` 会让
          // 「在 A 目录里新建」的用例落进未分组，而左侧列表正按 A 过滤——刚建出来的
          // 那一条当场看不见。
          { name, folder_id: folderId, request: spec, assertions },
          toCaseDetail,
        );
        if (!ownsEditor(owner)) return null;
        // 刚写入的这一版直接成为新基线：不等随后的重新拉取，用户接着编辑的内容
        // 不会在那次拉取回来时被旧内容盖掉。就绪标记要跟着一起更新，否则表单会退回
        // 载入态，正在输入的表单被卸载重挂（焦点与选择都会丢）。
        setAppliedStamp({ key: `${workspaceId}/${projectId}/${created.data.id}`, rev: created.data.rev });
        setLocalDetail({ key: `${workspaceId}/${projectId}/${created.data.id}`, detail: created.data });
        setEtag(created.etag);
        setDraftRev(created.data.rev);
        setCurrentId(created.data.id);
        setCreating(true);
        // 草稿摘要由服务端给出，保存后立刻对齐：它决定“执行固定在哪一版”。
        setDraftSnapshotHash(created.data.snapshot_hash);
        // 基线取服务端回显的归属，而不是本地的 `folderId`：万一服务端存的是别的值，
        // 基线必须跟着服务端走，否则界面会显示成“没有未保存修改”。表单里显示的那一份
        // 只在用户没在等待期间改过时才跟着回显走（见 submittedFolderId）。
        if (folderIdRef.current === submittedFolderId) setFolderId(created.data.folder_id);
        const nextBaseline = {
          name: created.data.name,
          request: requestToRaw(created.data.request),
          assertions: JSON.stringify(created.data.assertions),
          folderId: created.data.folder_id,
        };
        baselineSyncRef.current = nextBaseline;
        setBaseline(nextBaseline);
        setNotice("用例已创建，继续编辑后仍可保存。");
        onSaved(created.data.id);
        return created.data;
      }
      /**
       * 只有用户真的动过目录，才在请求里带上 `folder_id`。
       *
       * 服务端把 `folder_id` 分成三种含义（见 `CaseUpdate` 的 docstring）：**字段缺席**
       * 是不改目录、**显式 null** 是移到未分组、**给定 id** 是移到该目录。恒定带上
       * `folder_id: folderId` 看起来更简单，但用例所属目录已失效、用户只改了名称时，
       * 发出去的 null 会把这条用例悄悄移出原目录——用户没做过这个改动，历史执行记录
       * 还按旧目录被检索。未改动就让整个字段不出现。
       */
      const payload: Record<string, unknown> = { name, request: spec, assertions };
      if (folderChanged) payload.folder_id = folderId;

      const updated = await apiSendWithMeta(
        projectPath(workspaceId, projectId, `/cases/${currentId}`),
        "PATCH",
        payload,
        toCaseDetail,
        { headers: etag ? { "If-Match": etag } : {} },
      );
      if (!ownsEditor(owner)) return null;
      setEtag(updated.etag);
      setDraftRev(updated.data.rev);
      setAppliedStamp({ key: detailKey ?? "", rev: updated.data.rev });
      setLocalDetail({ key: detailKey ?? "", detail: updated.data });
      setDraftSnapshotHash(updated.data.snapshot_hash);
      // 同新建路径：等待期间用户改过目录就保留他的选择，只把基线推进到服务端那一边。
      if (folderIdRef.current === submittedFolderId) setFolderId(updated.data.folder_id);
      const nextBaseline = {
        name: updated.data.name,
        request: requestToRaw(updated.data.request),
        assertions: JSON.stringify(updated.data.assertions),
        folderId: updated.data.folder_id,
      };
      baselineSyncRef.current = nextBaseline;
      setBaseline(nextBaseline);
      setNotice("已保存到草稿。");
      onSaved(updated.data.id);
      return updated.data;
    } catch (cause) {
      if (cause instanceof ApiError && cause.isConflict) {
        setError(`${cause.message}（草稿未被覆盖，请刷新后重新编辑）`);
      } else if (cause instanceof ApiError && cause.status === 428) {
        setError(cause.message);
      } else {
        setError(cause instanceof Error ? cause.message : "保存失败");
      }
      return null;
    } finally {
      setBusy(false);
    }
  }

  /** 所有保存入口共享责任层同步锁；首次 POST 也不会因按钮＋快捷键重复创建。 */
  function save(): Promise<CaseDetail | null> {
    const inFlight = saveInFlightRef.current;
    if (inFlight !== null) return inFlight;
    if (editorWriteGateRef.current) return Promise.resolve(null);
    editorWriteGateRef.current = true;
    const operation = performSave().finally(() => {
      if (saveInFlightRef.current === operation) saveInFlightRef.current = null;
      editorWriteGateRef.current = false;
    });
    saveInFlightRef.current = operation;
    return operation;
  }

  useEffect(() => {
    if (onRegisterCloseSave === undefined) return;
    const state = (): LeaveState => {
      const base = baselineSyncRef.current ?? { ...BLANK_DRAFT, folderId: initialFolderId };
      return {
        dirty:
          base.name !== nameRef.current ||
          !sameRequest(base.request, requestRef.current) ||
          base.assertions !== JSON.stringify(assertionsRef.current) ||
          base.folderId !== folderIdRef.current,
        busy: editorWriteGateRef.current || closeOperationPendingRef.current,
      };
    };
    const action = async () => {
      const submitted = { name: nameRef.current, request: requestRef.current, assertions: assertionsRef.current, folderId: folderIdRef.current };
      const saved = await save();
      return saved !== null && !closeOperationPendingRef.current && nameRef.current === submitted.name && sameRequest(requestRef.current, submitted.request) && JSON.stringify(assertionsRef.current) === JSON.stringify(submitted.assertions) && folderIdRef.current === submitted.folderId;
    };
    onRegisterCloseSave({ save: action, state });
    return () => onRegisterCloseSave(null);
  }, [onRegisterCloseSave, save]);

  /**
   * 保存草稿，并确保有一版固定了屏幕上的执行内容；返回执行要固定到的那一版。
   *
   * 顺序是**先按 ETag 保存必要编辑，再用服务端返回的执行内容摘要匹配已有版本**。
   * 只看 `dirty` 就发布是不行的：所属目录不属于执行快照（摘要是对 `{name, request,
   * assertions}` 算的），只改一次目录，摘要与已有版本完全相同，发布却会固化出一条
   * 内容相同、id 不同的新版本——按“固定用例版本”签发的用途授权绑定的正是版本 id，
   * 于是改目录这一步会把原本匹配的授权打断，直到执行前才暴露，界面上没有任何异常。
   * 只有执行快照内容真的变了（摘要与任何已发布版本都不同）才固化新版本。
   */
  async function performSaveThenPublish(): Promise<CaseVersion | null> {
    const operationOwner = {
      workspaceId,
      projectId,
      editorKey,
      principalId: principalRef.current,
      environmentId: environmentRef.current,
      configEpoch: currentExecutionEpoch(),
    };
    const executionStillOwned = () =>
      ownsEditor(operationOwner) &&
      operationOwner.environmentId === environmentRef.current &&
      operationOwner.configEpoch !== null &&
      currentExecutionEpoch() === operationOwner.configEpoch;
    if (!executionStillOwned()) {
      setError("当前主体、环境或执行配置已变化，未开始版本准备。");
      return null;
    }
    setError(null);
    // 用例 id 必须取自 save() 的返回值：新建时 setCurrentId 只是排队了一次
    // 状态更新，本函数闭包里的 currentId 仍是 null，用它当目标会漏掉发布。
    // 修订号同理：save() 返回的就是刚刚写入的那一版，闭包里的 draftRev 可能还是旧的。
    // 摘要同理：刚保存完的那一份摘要才是屏幕上这份内容的判据。
    let targetId = currentId;
    let targetRev = draftRev;
    let targetHash = draftSnapshotHash;
    if (dirty || currentId === null) {
      const saved = await performSave();
      if (saved === null) return null;
      if (!executionStillOwned()) {
        setError("保存完成，但主体、环境或执行配置已变化；未继续发布或运行。");
        return null;
      }
      targetId = saved.id;
      targetRev = saved.rev;
      targetHash = saved.snapshot_hash;
    }
    if (targetId === null || targetRev === null) {
      setError("请先填写用例名称并保存。");
      return null;
    }
    setBusy(true);
    try {
      // 读不到版本时并没有固化任何版本，这句提示不能与“发布失败”混为一谈：
      // `loadVersions` 自己吞掉读取失败并返回 null，所以它不会走到下面的 catch。
      const existing = await loadVersions(targetId);
      if (!executionStillOwned()) {
        setError("版本读取完成，但主体、环境或执行配置已变化；未继续发布或运行。");
        return null;
      }
      if (existing === null) {
        setError("版本列表读取失败，未提交执行");
        return null;
      }
      const reusable =
        existing.find((item) => item.snapshot_hash === targetHash && item.case_id === targetId) ?? null;
      if (reusable !== null) {
        // 复用也是服务端确认的一版：同样原子接纳并上报，左侧的“已发布 vN”不会滞后。
        acceptVersion(targetId, reusable);
        setDraftSnapshotHash(reusable.snapshot_hash);
        setNotice(`已保存；执行复用内容一致的已发布版本 v${reusable.version}，未新增版本。`);
        onVersionsChanged?.(targetId);
        return reusable;
      }
      const version = await apiSend(
        projectPath(workspaceId, projectId, `/cases/${targetId}/publish`),
        "POST",
        // 副作用未知时不当作只读：未知副作用禁止自动重放，本阶段生产统一拒绝。
        // draft_rev 声明固化的正是屏幕上这一版：服务端在同一行锁内比对，期间
        // 若有别的保存把草稿推进，这里会收到冲突而不是悄悄固化另一份内容。
        { side_effect: "unknown", draft_rev: targetRev },
        toCaseVersion,
      );
      if (!executionStillOwned()) return null;
      // 发布返回的那一版就是刚刚固化的内容，草稿摘要与它一致：接着执行会固定在这一版。
      // 先接纳它再提示，界面上的版本区、左侧列表与授权选择器同时拿到这一版。
      acceptVersion(targetId, version);
      setDraftSnapshotHash(version.snapshot_hash);
      setNotice(`已发布版本 v${version.version}，执行将固定在该版本上。`);
      onVersionsChanged?.(targetId);
      return version;
    } catch (cause) {
      // 能走到这里的只有发布本身：读版本失败已经在上面按“未提交执行”返回了。
      if (cause instanceof ApiError && cause.code === "draft_rev_conflict") {
        setError(`${cause.message}（发布已取消，未固化任何版本）`);
      } else {
        setError(cause instanceof Error ? cause.message : "发布失败");
      }
      return null;
    } finally {
      setBusy(false);
    }
  }

  function saveThenPublish(): Promise<CaseVersion | null> {
    if (editorWriteGateRef.current) return Promise.resolve(null);
    editorWriteGateRef.current = true;
    return performSaveThenPublish().finally(() => {
      editorWriteGateRef.current = false;
    });
  }

  /**
   * 解析一条 cURL 并填入编辑器。
   *
   * 返回错误与警告给导入组件自己展示：它就在工具栏里，就地给出反馈比让用户去别处找
   * 提示更直接。**不发送任何被测请求**，也不在失败时改动草稿（INV-06）。
   */
  async function importCurl(text: string): Promise<{ error: string | null; warnings: string[] }> {
    if (!text.trim()) {
      return { error: "请先粘贴 cURL 命令文本。", warnings: [] };
    }
    const startedRevision = editRevisionRef.current;
    setBusy(true);
    try {
      const preview = await apiSend(
        projectPath(workspaceId, projectId, "/imports/curl/preview"),
        "POST",
        { text },
        toCurlPreview,
      );
      if (!preview.sendable) {
        // 无法保证等价导入的命令不写入草稿，避免产生一条看起来能发送的错误请求。
        return {
          error: "这条命令含有暂不支持的能力，已拒绝导入；请手工编辑请求。",
          warnings: [...preview.warnings, ...preview.unsupported],
        };
      }
      if (!aliveRef.current) return { error: "编辑器已关闭，解析结果没有应用。", warnings: preview.warnings };
      const changedWhileParsing = editRevisionRef.current !== startedRevision;
      const currentRequest = requestRef.current;
      const currentAssertions = assertionsRef.current;
      const hasAssertions = currentAssertions.length > 0;
      if ((changedWhileParsing || currentRequest.schema_version === 2 || hasAssertions) && !window.confirm(
        `${changedWhileParsing ? "解析期间请求已被修改。" : ""}应用会整份替换请求；原行断言会保留为“字段已删除”，历史位置条件不会自动绑定新行。仍要应用吗？`,
      )) {
        return { error: "已保留解析预览，当前请求没有被覆盖。", warnings: preview.warnings };
      }
      let next = requestToRaw(preview.draft);
      let nextAssertions = currentAssertions;
      if (currentRequest.schema_version === 2 || hasAssertions) {
        const upgraded = upgradeRequestV2(currentRequest, currentAssertions);
        nextAssertions = upgraded.assertions;
        next = {
          ...next,
          schema_version: 2,
          query_params: next.query_params.map((row) => newRequestRow(row)),
          headers: next.headers.map((row) => newRequestRow(row)),
        };
      }
      editRevisionRef.current += 1;
      setRequest({ ...next, method: preview.draft.method || currentRequest.method });
      setAssertions(nextAssertions);
      return { error: null, warnings: preview.warnings };
    } catch (cause) {
      return {
        error: cause instanceof Error ? cause.message : "导入失败",
        warnings: [],
      };
    } finally {
      setBusy(false);
    }
  }

  function patchRequest(patch: Partial<RawRequest>) {
    editRevisionRef.current += 1;
    setRequest((current) => ({ ...current, ...patch }));
  }

  function changeAssertions(next: CaseAssertion[] | ((current: CaseAssertion[]) => CaseAssertion[])) {
    editRevisionRef.current += 1;
    setAssertions(next);
  }

  function changeRows(field: "query_params" | "headers", rows: RawKeyValue[]) {
    const upgraded = request.schema_version === 2
      ? { request, assertions, migrated: 0, historical: 0 }
      : presentedUpgrade;
    if (request.schema_version !== 2 && (upgraded.migrated > 0 || upgraded.historical > 0)) {
      const accepted = window.confirm(
        `首次编辑参数会启用稳定行定位：可迁移 ${upgraded.migrated} 条断言，${upgraded.historical} 条历史位置条件保持原语义。继续吗？`,
      );
      if (!accepted) return;
    }
    const originalRows = request[field];
    const upgradedRows = upgraded.request[field];
    const used = new Set<string>();
    const stableRows = rows.map((row, submittedIndex) => {
      if (row.row_id) {
        used.add(row.row_id);
        return row;
      }
      const originalIndex = originalRows.findIndex((item) => item === row);
      const candidate = upgradedRows[originalIndex >= 0 ? originalIndex : submittedIndex];
      if (candidate?.row_id && !used.has(candidate.row_id)) {
        used.add(candidate.row_id);
        return { ...candidate, ...row, row_id: candidate.row_id, enabled: candidate.enabled, description: row.description ?? candidate.description };
      }
      return newRequestRow(row);
    });
    const otherField = field === "query_params" ? "headers" : "query_params";
    if (stableRows.length + upgraded.request[otherField].length > 500) {
      setError("启用新版参数功能后 Query 和 Header 合计最多 500 行；请先删除多余行。");
      return;
    }
    editRevisionRef.current += 1;
    setAssertions(upgraded.assertions);
    setRequest({ ...upgraded.request, [field]: stableRows });
  }

  function changeRowAssertions(next: CaseAssertion[] | ((current: CaseAssertion[]) => CaseAssertion[])) {
    if (request.schema_version === 2) {
      changeAssertions(next);
      return;
    }
    editRevisionRef.current += 1;
    const upgradedAssertions = typeof next === "function" ? next(presentedUpgrade.assertions) : next;
    setRequest(presentedUpgrade.request);
    setAssertions(upgradedAssertions);
  }

  useEffect(() => {
    if (!active) return;
    const handle = (event: KeyboardEvent) => {
      if (event.repeat || event.isComposing || (!event.metaKey && !event.ctrlKey)) return;
      const target = event.target instanceof Element ? event.target : null;
      if (target?.closest('[role="dialog"]')) return;
      const saveShortcut = event.key.toLowerCase() === "s";
      const sendShortcut = event.key === "Enter";
      if (!saveShortcut && !sendShortcut) return;
      event.preventDefault();
      if (shortcutLockRef.current || readOnly) return;
      shortcutLockRef.current = true;
      void (saveShortcut ? save() : sendDebug()).finally(() => {
        shortcutLockRef.current = false;
      });
    };
    window.addEventListener("keydown", handle);
    return () => window.removeEventListener("keydown", handle);
  }, [active, readOnly, save, sendDebug]);

  if (detail.error) {
    return (
      <section className="pane">
        <ErrorText message={detail.error.message} />
      </section>
    );
  }

  // 已有用例的内容还没到时只显示进度：此时表单里的空白并不是这条用例的内容，
  // 开放编辑只会让用户白打一遍字，然后被回填覆盖。
  if (!ready) {
    return (
      <section className="pane">
        <Loading label="正在载入用例内容…" />
      </section>
    );
  }

  return (
    <section className="pane workbench">
      <CaseHeading
        idPrefix={domIdPrefix ? `${domIdPrefix}-case` : "case"}
        name={name}
        onNameChange={setName}
        folderId={folderId}
        onFolderChange={setFolderId}
        folders={folders}
        folderUnavailable={folderUnavailable}
        folderPlaceholder={
          foldersLoading ? "正在加载目录…" : unknownFolderLabel(folderId ?? "")
        }
        dirty={dirty}
        busy={busy}
        readOnly={readOnly}
        creating={creating}
        isNew={currentId === null}
        versionCount={versions.length}
        onSave={() => void save()}
        onPublish={() => void saveThenPublish()}
        onClose={onClose}
      />

      {folderUnavailable ? (
        <Hint>
          这条用例原本所属的目录已归档或已不可见，不再出现在可选目录里。
          <strong>不动目录直接保存不会改变它的归属</strong>
          ；若要改成别的目录或未分组，请在这里明确选择后再保存。
        </Hint>
      ) : null}
      {notice ? <Notice tone="info" title={notice} /> : null}
      {error ? <ErrorText message={error} /> : null}

      {/*
        地址行：唯一一组方法／路径／环境输入，加上常驻的蓝色「发送」。
        首屏第一眼就能找到它——这是本轮的核心改动，不再藏在长表单底部。
      */}
      <ResizableWorkbench
        controls={(
          <>
            <SendBar
              idPrefix={domIdPrefix ? `${domIdPrefix}-send` : undefined}
              request={request}
              environments={environments}
              selectedEnvironmentId={selectedEnvironmentId}
              onSelectEnvironment={onSelectEnvironment}
              onPatch={patchRequest}
              onSend={() => void sendDebug()}
              onStopWaiting={debug.stopWaiting}
              onRetryAcceptance={() => void debug.retryAcceptance()}
              onResumeWaiting={debug.resumeWaiting}
              paused={debug.paused}
              stage={sendStage}
              readOnly={readOnly}
              readOnlyReason={readOnlyReason}
              preflight={debug.preflight}
              preflightError={debug.preflightError}
              preflighting={debug.preflighting}
              onOpenAdmin={() => onOpenAdmin?.()}
              onOpenEnvironment={onOpenEnvironment ? () => onOpenEnvironment() : undefined}
              canAuthorize={debug.preflight?.can_authorize ?? false}
              onSubmitAuthorization={() => void confirmAuthorization()}
              onCancelAuthorization={cancelAuthorization}
              tools={<CurlImport idPrefix={domIdPrefix ? `${domIdPrefix}-request` : ""} pendingKey={leaveKey ? `${leaveKey}:curl` : undefined} onImport={importCurl} disabled={readOnly} loading={busy} />}
              authorization={debug.authorizationView}
            />
            {sendError ? <ErrorText message={sendError} /> : null}
            {debug.error ? <ErrorText message={debug.error} /> : null}
            {debug.notice ? <Notice tone="info" title={debug.notice} /> : null}
          </>
        )}
        request={(
          <>
      <div className="block">
        {/*
          标签只切换可见性，不卸载面板：切走再回来时输入框、光标与未提交的编辑都还在。
          断言、样例与预期字段都在标签内，不再常驻在请求区外面把响应挤到首屏之外。
        */}
        <RequestTabs
          idPrefix={domIdPrefix ? `${domIdPrefix}-request` : "request"}
          activeId={activeTab}
          onChange={setActiveTab}
          tabs={[
            {
              id: "params",
              label: "参数",
              summary: request.query_params.filter((row) => row.name.trim()).length || null,
              content: (
                <>
                  <h3>查询参数（可重复）</h3>
                  <p className="caption">重复键按原样保留顺序与出现次数。</p>
                  <KeyValueRows
                    rows={presentedRequest.query_params}
                    label="查询参数"
                    addLabel="＋添加查询参数"
                    readOnly={readOnly}
                    idPrefix={`${editorKey}-query`}
                    kind="query"
                    version={2}
                    pendingPrefix={leaveKey}
                    otherRows={presentedRequest.headers}
                    ownerRevision={`${editRevisionRef.current}:${JSON.stringify(assertions)}`}
                    relatedAssertionCount={presentedAssertions.filter((item) => {
                      const first = item.selector[0];
                      return item.target_source === "request.query" && first?.kind === "row" && presentedRequest.query_params.some((row) => row.row_id === first.row_id);
                    }).length}
                    assertionSlot={(row) => row.row_id ? (
                      <AssertionColumn
                        types={types.data ?? []}
                        typesError={types.error?.message ?? null}
                        workspaceId={workspaceId}
                        projectId={projectId}
                        field={{ targetSource: "request.query", selector: [{ kind: "row", row_id: row.row_id }, { kind: "key", key: "value" }], fieldType: "string" }}
                        own={presentedAssertions.filter((item) => item.target_source === "request.query" && item.selector[0]?.kind === "row" && item.selector[0].row_id === row.row_id)}
                        sample={row.enabled === false ? null : { type: "string", text: row.value }}
                        results={results}
                        readOnly={readOnly}
                        onUpsert={(next) => changeRowAssertions((current) => upsertAssertion(current, next))}
                        onRemove={(id) => changeRowAssertions((current) => removeAssertion(current, id))}
                        pendingKey={leaveKey ? `${leaveKey}:assertion-query-${row.row_id}` : undefined}
                      />
                    ) : null}
                    onChange={(rows) => changeRows("query_params", rows)}
                  />
                </>
              ),
            },
            {
              id: "auth",
              label: "认证",
              badge: authBadge(request, debug.preflight),
              content: (
                <AuthTab
                  preflight={debug.preflight}
                  preflightError={debug.preflightError}
                  environmentName={
                    environments.find((item) => item.id === selectedEnvironmentId)?.name ?? null
                  }
                  authRequired={request.auth_required === true}
                  readOnly={readOnly}
                  onToggleRequired={(next) => patchRequest({ auth_required: next ? true : undefined })}
                  onOpenAdmin={() => onOpenAdmin?.()}
                />
              ),
            },
            {
              id: "headers",
              label: "请求头",
              summary: request.headers.filter((row) => row.name.trim()).length || null,
              content: (
                <>
                  <h3>请求头（可重复）</h3>
                  <p className="caption">
                    认证头请通过环境的身份配置注入，不要写在这里：写在请求头里的凭证会被
                    当作普通内容保存与展示。
                  </p>
                  <KeyValueRows
                    rows={presentedRequest.headers}
                    label="请求头"
                    addLabel="＋添加请求头"
                    readOnly={readOnly}
                    idPrefix={`${editorKey}-header`}
                    kind="header"
                    version={2}
                    pendingPrefix={leaveKey}
                    otherRows={presentedRequest.query_params}
                    ownerRevision={`${editRevisionRef.current}:${JSON.stringify(assertions)}`}
                    relatedAssertionCount={presentedAssertions.filter((item) => {
                      const first = item.selector[0];
                      return item.target_source === "request.header" && first?.kind === "row" && presentedRequest.headers.some((row) => row.row_id === first.row_id);
                    }).length}
                    assertionSlot={(row) => row.row_id ? (
                      <AssertionColumn
                        types={types.data ?? []}
                        typesError={types.error?.message ?? null}
                        workspaceId={workspaceId}
                        projectId={projectId}
                        field={{ targetSource: "request.header", selector: [{ kind: "row", row_id: row.row_id }, { kind: "key", key: "value" }], fieldType: "string" }}
                        own={presentedAssertions.filter((item) => item.target_source === "request.header" && item.selector[0]?.kind === "row" && item.selector[0].row_id === row.row_id)}
                        sample={row.enabled === false ? null : { type: "string", text: row.value }}
                        results={results}
                        readOnly={readOnly}
                        onUpsert={(next) => changeRowAssertions((current) => upsertAssertion(current, next))}
                        onRemove={(id) => changeRowAssertions((current) => removeAssertion(current, id))}
                        pendingKey={leaveKey ? `${leaveKey}:assertion-header-${row.row_id}` : undefined}
                      />
                    ) : null}
                    onChange={(rows) => changeRows("headers", rows)}
                  />
                </>
              ),
            },
            {
              id: "body",
              label: "请求体",
              badge: request.body_type === "none" ? null : BODY_TYPE_LABEL[request.body_type],
              content: (
                <>
                  <BodyEditor
                    idPrefix={domIdPrefix ? `${domIdPrefix}-request` : "request"}
                    request={request}
                    readOnly={readOnly}
                    onChange={(body) => patchRequest({ body })}
                    onTypeChange={(body_type) => patchRequest({ body_type })}
                  />
                  <p className="caption">正文字段断言在「断言」标签里按字段配置。</p>
                </>
              ),
            },
            {
              id: "assertions",
              label: "断言",
              summary: assertions.length || null,
              content: (
                <AssertionTab
                  workspaceId={workspaceId}
                  projectId={projectId}
                  types={types.data ?? []}
                  typesError={types.error ? types.error.message : null}
                  assertions={assertions}
                  results={results}
                  readOnly={readOnly}
                  onChange={changeAssertions}
                  bodyTree={bodyTree}
                  bodySourceKey={bodyText}
                  bodyHint={
                    request.body_type === "json"
                      ? "正文为空或不是合法 JSON，暂时无法展开字段。"
                      : "选择 JSON 正文类型后，可在这里按字段配置断言。"
                  }
                  pendingPrefix={leaveKey}
                  request={presentedRequest}
                />
              ),
            },
          ]}
        />
      </div>

          </>
        )}
        response={(
          <>

      {/*
        只在**确有终态结果**且与当前输入不同源时提示“字段行标为未执行”。
        排队中的运行还没有结论：那时说“字段行旁显示的是最近一次运行的结论”是一句空话，
        真实情况是“还没有可展示的结果”。来源未确定不等于已有旧结果。
      */}
      {displayedReport !== null && isTerminal(displayedReport.run) && !activeMatches ? (
        <Hint>
          字段行旁显示的是最近一次运行的结论，但当前内容或执行环境已与那次运行不同，因此统一标为“未执行”；
          {separateHistory
            ? "那次运行的完整报告仍可在“本次记录”或顶部“测试报告”中查看。"
            : "那次运行的完整报告仍可在“本次记录”或本页“项目／环境历史”中查看。"}
        </Hint>
      ) : null}

      {/*
        响应区紧跟在请求区下方：标题常驻，1366×768 首屏即可看到。出参样例与预期字段
        随响应一起，因为它们要对着**本次真实响应**配置。
      */}
      <ResponsePanel
        report={displayedReport}
        reportError={displayedReportError}
        loading={debug.phase === "submitting"}
        matchesCurrent={activeMatches}
        selectedRunId={selection?.runId ?? null}
        onCancel={(runId) => void debug.cancel(runId)}
        canCancel={!readOnly}
        fieldsTab={
          <ResponseFieldPanel
            workspaceId={workspaceId}
            projectId={projectId}
            report={displayedReport}
            types={types.data ?? []}
            typesError={types.error ? types.error.message : null}
            assertions={assertions}
            results={results}
            readOnly={readOnly}
            onChange={changeAssertions}
            pendingPrefix={leaveKey}
            idPrefix={domIdPrefix ? `${domIdPrefix}-response` : "response"}
          />
        }
      />

      <details className="block">
        <summary>
          本次记录{debug.records.length > 0 ? `（${debug.records.length}）` : ""}
        </summary>
        <p className="caption">
          只列<strong>本次打开这个编辑器</strong>期间受理的运行。关闭或刷新后，
          {separateHistory
            ? "请到顶部“任务中心”或“测试报告”按项目／环境查看。"
            : "请到本页“项目／环境历史”按项目／环境查看。"}
        </p>
        {debug.records.length === 0 ? (
          <Hint>还没有本次调试记录。点击地址行右侧的「发送」即可调试当前编辑内容。</Hint>
        ) : (
          <ul className="record-list">
            {debug.records.map((item) => (
              <li key={item.runId}>
                <button
                  type="button"
                  className={item.runId === debug.selectedRunId ? "row-active" : undefined}
                  onClick={() => setSelection({ source: "debug", runId: item.runId })}
                >
                  {item.runId.slice(0, 8)}
                </button>
                <span className="caption">
                  {new Date(item.submittedAt).toLocaleTimeString()} · 环境{" "}
                  {environments.find((env) => env.id === item.environmentId)?.name ??
                    item.environmentId.slice(0, 8)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </details>

          </>
        )}
      />


      {/*
        已发布版本的执行与项目／环境历史。它是次级操作：这里的「保存并执行」会先按
        ETag 保存、再固定一个已发布版本，与上面的调试发送不是同一条路。保留原有用例
        执行的既有约束（必须有版本、必须先保存）不变。
      */}
      <details className="block">
        <summary>版本执行与发布记录</summary>
        <RunPanel
          workspaceId={workspaceId}
          projectId={projectId}
          environmentId={selectedEnvironmentId}
          caseId={currentId}
          needsVersion={mustEnsureVersion}
          publishedVersion={matchingVersion}
          onEnsureVersion={saveThenPublish}
          selectedRunId={selection?.source === "history" ? selection.runId : null}
          onSelectRun={selectHistoryRun}
          onReport={handleHistoryReport}
          onRunSubmitted={handleRunSubmitted}
          captureProvenance={captureRunProvenance}
          canCancel={!readOnly}
          readOnly={readOnly}
          showHistory={!separateHistory}
          operationBlocked={debug.operationActive}
          onOperationActive={setVersionOperationActive}
          tryAcquireOperation={() => {
            if (executionGateRef.current !== null) return false;
            executionGateRef.current = "version";
            return true;
          }}
          releaseOperation={() => {
            if (executionGateRef.current === "version") executionGateRef.current = null;
          }}
        />
        <h3>已发布版本</h3>
        {versions.length > 0 ? (
          <ul className="caption">
            {versions.map((item) => (
              <li key={item.id}>
                v{item.version} · 副作用 {item.side_effect} · {item.created_at}
              </li>
            ))}
          </ul>
        ) : (
          <Hint>尚未发布任何版本。</Hint>
        )}
        {creating ? <Hint>用例已创建；列表稍后会刷新，可继续编辑或发布。</Hint> : null}
      </details>
    </section>
  );
}
