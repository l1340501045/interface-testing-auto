/**
 * 用例编辑：请求编辑、字段断言、cURL 导入、保存与发布、发起执行。
 *
 * 草稿保存在服务端并用 ETag 做乐观锁：他人已修改时提示刷新，不静默覆盖。
 * 只有保存成功后才允许发布；发布产生不可变版本，执行固定在该版本上。
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
import { useResource } from "../hooks/useResource";
import { AssertionColumn } from "./AssertionColumn";
import { fieldAssertions, groupByField, removeAssertion, upsertAssertion } from "./assertionGroups";
import { FieldTreePanel } from "./FieldTreePanel";
import { BodyEditor, KeyValueRows, MethodAndPath } from "./RequestParts";
import { ResponseFieldPanel } from "./ResponseFieldPanel";
import { RunPanel } from "../runs/RunPanel";
import { emptyRequest, rawToSpec, requestToRaw, sameRequest, type RawRequest } from "./requestDraft";
import { useFieldTree } from "./useFieldTree";

/** 固定的响应断言行：状态码与耗时。 */
const RESPONSE_FIELDS = [
  { label: "状态码", target: "response.status" as const, selector: [], fieldType: "integer" },
  { label: "耗时（毫秒）", target: "response.elapsed" as const, selector: [], fieldType: "integer" },
];

/** 新建用例尚未保存时的基线：只要用户动过任何一处，就算有未保存修改。 */
const BLANK_DRAFT = {
  name: "",
  request: emptyRequest(),
  assertions: JSON.stringify([]),
};

/** 未分组在界面上的文案：它不是一个目录，而是“不属于任何目录”这个明确状态。 */
const UNFILED_LABEL = "未分组";

/**
 * 当前值指向的目录不在可选清单里时，选择器里那条占位选项的文案。
 *
 * 不能把这个状态显示成「未分组」：那等于在界面上宣布一个用户没做过的改动，
 * 用户一保存就真的被移出原目录。目录被归档、被删、或属于别的项目都会走到这里。
 */
function unknownFolderLabel(folderId: string): string {
  return `已失效或已归档的目录（${folderId.slice(0, 8)}…）`;
}

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
  const [assertions, setAssertions] = useState<CaseAssertion[]>([]);
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
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [curlText, setCurlText] = useState("");
  const [curlError, setCurlError] = useState<string | null>(null);
  const [curlWarnings, setCurlWarnings] = useState<string[]>([]);
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
  const [report, setReport] = useState<RunReport | null>(null);
  const [creating, setCreating] = useState(false);

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
    setReport(null);
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

  const dirty = useMemo(() => {
    return (
      effectiveBaseline.name !== name ||
      !sameRequest(effectiveBaseline.request, request) ||
      effectiveBaseline.assertions !== JSON.stringify(assertions) ||
      effectiveBaseline.folderId !== folderId
    );
  }, [effectiveBaseline, name, request, assertions, folderId]);

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

  // 上报离开状态：范围切换与关闭由外壳统一拦一次，避免每个入口各写一份判断。
  useLeaveReport(`case:${workspaceId}/${projectId}/${currentId ?? "new"}`, { dirty, busy });

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
    onCurrentVersion?.({ caseId: currentId, versionId: currentVersionId });
    // 编辑器卸载（切换用例、关闭、切换范围）时撤销上报：留着它会让授权表单按一条
    // 已经不在屏幕上的用例预选。
    return () => onCurrentVersion?.(null);
  }, [currentId, currentVersionId, onCurrentVersion]);

  const bodyText = request.body_type === "json" ? request.body : "";
  const bodyTree = useFieldTree(workspaceId, projectId, bodyText);

  const groups = useMemo(() => groupByField(assertions), [assertions]);

  /**
   * 运行结果能不能贴回字段行。
   *
   * 结果按断言标识回填，而修改一条断言不会换标识：改完不重新执行，字段行旁边仍然挂着
   * 上一轮的通过，用户会以为新条件也通过了。所以必须核对运行快照——本次运行的目标环境
   * 就是当前环境，且它执行的版本快照与屏幕上这份内容一致。
   *
   * 有未保存修改时（`dirty`）屏幕内容已经不等于那个摘要，一律按未执行处理。版本列表
   * 还没加载出来时同样不猜：宁可让用户重新执行一次，也不能把别的条件的结论显示成
   * 这条条件的结论。
   */
  const reportedVersion = useMemo(
    () => versions.find((item) => item.id === report?.run.case_version_id) ?? null,
    [versions, report],
  );
  const resultsMatchRun =
    report !== null &&
    report.run.environment_id === selectedEnvironmentId &&
    !dirty &&
    draftSnapshotHash !== null &&
    reportedVersion !== null &&
    reportedVersion.snapshot_hash === draftSnapshotHash;

  const results = useMemo(() => {
    const map = new Map<string, AssertionResult>();
    for (const item of report?.assertions ?? []) {
      // 与本次执行对不上的旧结论统一标成未执行，并且不保留期望／实际：那是上一轮的
      // 取值，不是当前条件的证据。原始报告仍在“执行与历史”里完整可查。
      map.set(
        item.assertion_id,
        resultsMatchRun
          ? item
          : { ...item, status: "skipped", expected: null, actual: null, reason_code: "stale_run" },
      );
    }
    return map;
  }, [report, resultsMatchRun]);

  async function save(): Promise<CaseDetail | null> {
    setError(null);
    setNotice(null);
    let spec;
    try {
      spec = rawToSpec(request);
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
        setBaseline({
          name: created.data.name,
          request: requestToRaw(created.data.request),
          assertions: JSON.stringify(created.data.assertions),
          folderId: created.data.folder_id,
        });
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
      setEtag(updated.etag);
      setDraftRev(updated.data.rev);
      setAppliedStamp({ key: detailKey ?? "", rev: updated.data.rev });
      setLocalDetail({ key: detailKey ?? "", detail: updated.data });
      setDraftSnapshotHash(updated.data.snapshot_hash);
      // 同新建路径：等待期间用户改过目录就保留他的选择，只把基线推进到服务端那一边。
      if (folderIdRef.current === submittedFolderId) setFolderId(updated.data.folder_id);
      setBaseline({
        name: updated.data.name,
        request: requestToRaw(updated.data.request),
        assertions: JSON.stringify(updated.data.assertions),
        folderId: updated.data.folder_id,
      });
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
  async function saveThenPublish(): Promise<CaseVersion | null> {
    setError(null);
    // 用例 id 必须取自 save() 的返回值：新建时 setCurrentId 只是排队了一次
    // 状态更新，本函数闭包里的 currentId 仍是 null，用它当目标会漏掉发布。
    // 修订号同理：save() 返回的就是刚刚写入的那一版，闭包里的 draftRev 可能还是旧的。
    // 摘要同理：刚保存完的那一份摘要才是屏幕上这份内容的判据。
    let targetId = currentId;
    let targetRev = draftRev;
    let targetHash = draftSnapshotHash;
    if (dirty || currentId === null) {
      const saved = await save();
      if (saved === null) return null;
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

  async function importCurl() {
    setCurlError(null);
    setCurlWarnings([]);
    if (!curlText.trim()) {
      setCurlError("请先粘贴 cURL 命令文本。");
      return;
    }
    setBusy(true);
    try {
      const preview = await apiSend(
        projectPath(workspaceId, projectId, "/imports/curl/preview"),
        "POST",
        { text: curlText },
        toCurlPreview,
      );
      if (!preview.sendable) {
        // 无法保证等价导入的命令不写入草稿，避免产生一条看起来能发送的错误请求。
        setCurlError("这条命令含有暂不支持的能力，已拒绝导入；请手工编辑请求。");
        setCurlWarnings([...preview.warnings, ...preview.unsupported]);
        return;
      }
      setRequest((current) => ({ ...requestToRaw(preview.draft), method: preview.draft.method || current.method }));
      setCurlWarnings(preview.warnings);
      setNotice("已导入到编辑器；导入过程不访问目标，也未执行任何命令。");
    } catch (cause) {
      setCurlError(cause instanceof Error ? cause.message : "导入失败");
    } finally {
      setBusy(false);
    }
  }

  function patchRequest(patch: Partial<RawRequest>) {
    setRequest((current) => ({ ...current, ...patch }));
  }

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
    <section className="pane">
      <header className="pane-head">
        <span className="param grow">
          <label htmlFor="case-name">用例名称</label>
          <input id="case-name" value={name} onChange={(event) => setName(event.target.value)} />
        </span>
        <span className="param">
          <label htmlFor="case-folder">所属目录</label>
          {/*
            按**名称**选择，不让用户去填目录 UUID：目录清单与左侧目录树是同一份，
            选项里不会出现别的项目或已归档的目录，跨项目目录因此还有后端那一层拒绝兜底。
          */}
          <select
            id="case-folder"
            value={folderId ?? ""}
            onChange={(event) => setFolderId(event.target.value === "" ? null : event.target.value)}
          >
            <option value="">{UNFILED_LABEL}</option>
            {/*
              当前归属不在可选清单里时补一条占位选项，只为把真实状态显示出来。
              没有它，`value` 匹配不到任何选项，浏览器会显示第一项「未分组」——正好是
              这个改动最不该造成的误解：用户没动过目录，界面却看起来已经改成未分组了。
            */}
            {folderUnavailable && folderId !== null ? (
              <option value={folderId}>{unknownFolderLabel(folderId)}</option>
            ) : null}
            {folders.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
        </span>
        <span className="param">
          <label htmlFor="case-environment">执行环境</label>
          <select
            id="case-environment"
            value={selectedEnvironmentId ?? ""}
            onChange={(event) => onSelectEnvironment(event.target.value)}
          >
            <option value="">请选择环境</option>
            {environments.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}（{item.kind === "production" ? "生产" : "测试"}）
              </option>
            ))}
          </select>
        </span>
        {dirty ? <span className="tag tag-warn">有未保存修改</span> : null}
      </header>

      {folderUnavailable ? (
        <Hint>
          这条用例原本所属的目录已归档或已不可见，不再出现在可选目录里。
          <strong>不动目录直接保存不会改变它的归属</strong>
          ；若要改成别的目录或未分组，请在这里明确选择后再保存。
        </Hint>
      ) : null}
      {notice ? <Notice tone="info" title={notice} /> : null}
      {error ? <ErrorText message={error} /> : null}

      <details className="block">
        <summary>导入 cURL（只解析文本，不发送请求）</summary>
        <textarea
          rows={3}
          value={curlText}
          placeholder="curl -X POST 'https://example.test/orders?tag=a&tag=b' -H 'Content-Type: application/json' -d '{...}'"
          onChange={(event) => setCurlText(event.target.value)}
        />
        <div className="actions">
          <button type="button" onClick={() => void importCurl()} disabled={busy}>
            解析并填入编辑器
          </button>
        </div>
        {curlError ? <ErrorText message={curlError} /> : null}
        {curlWarnings.length > 0 ? (
          <ul className="caption">
            {curlWarnings.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        ) : null}
      </details>

      <div className="block">
        <h2>请求</h2>
        <MethodAndPath request={request} readOnly={false} onChange={patchRequest} />
        {request.imported_origin ? (
          <Hint>
            导入来源为 {request.imported_origin}，但实际目标由所选环境决定；请确认环境地址与预期一致。
          </Hint>
        ) : null}
        <div className="split">
          <div>
            <h3>查询参数（可重复）</h3>
            <KeyValueRows
              rows={request.query_params}
              label="查询参数"
              addLabel="＋添加查询参数"
              readOnly={false}
              onChange={(rows) => patchRequest({ query_params: rows })}
            />
            <h3>请求头（可重复）</h3>
            <KeyValueRows
              rows={request.headers}
              label="请求头"
              addLabel="＋添加请求头"
              readOnly={false}
              onChange={(rows) => patchRequest({ headers: rows })}
            />
          </div>
          <div>
            <h3>正文</h3>
            <BodyEditor request={request} readOnly={false} onChange={(body) => patchRequest({ body })} />
          </div>
        </div>
      </div>

      {report && !resultsMatchRun ? (
        <Hint>
          字段行旁显示的是最近一次运行的结论，但当前内容或执行环境已与那次运行不同，因此统一标为“未执行”；
          那次运行的完整报告仍在下方“执行与历史”里。
        </Hint>
      ) : null}

      <div className="block">
        <h2>固定响应断言</h2>
        <p className="caption">状态码与耗时无需样例即可配置，执行时按本次响应核对。</p>
        {RESPONSE_FIELDS.map((field) => (
          <div className="response-field" key={field.target}>
            <div className="response-field-head">
              <strong>{field.label}</strong>
              <span className="field-type">{field.fieldType}</span>
            </div>
            <AssertionColumn
              types={types.data ?? []}
              typesError={types.error ? types.error.message : null}
              workspaceId={workspaceId}
              projectId={projectId}
              field={{ targetSource: field.target, selector: field.selector, fieldType: field.fieldType }}
              own={fieldAssertions(groups, field.target, field.selector)}
              sample={null}
              results={results}
              readOnly={false}
              onUpsert={(next) => setAssertions(upsertAssertion(assertions, next))}
              onRemove={(id) => setAssertions(removeAssertion(assertions, id))}
            />
          </div>
        ))}
      </div>

      <ResponseFieldPanel
        workspaceId={workspaceId}
        projectId={projectId}
        report={report}
        types={types.data ?? []}
        typesError={types.error ? types.error.message : null}
        assertions={assertions}
        results={results}
        readOnly={false}
        onChange={setAssertions}
      />

      <div className="block">
        <h2>请求字段断言</h2>
        <p className="caption">发送前检查；失败时不会发出 HTTP 请求。</p>
        <FieldTreePanel
          title="请求正文字段"
          tree={bodyTree}
          targetSource="request.body"
          types={types.data ?? []}
          typesError={types.error ? types.error.message : null}
          workspaceId={workspaceId}
          projectId={projectId}
          assertions={assertions}
          results={results}
          readOnly={false}
          onChange={setAssertions}
          emptyHint={
            request.body_type === "json"
              ? "正文为空或不是合法 JSON，暂时无法展开字段。"
              : "选择 JSON 正文类型后，可在这里按字段配置断言。"
          }
        />
      </div>

      <div className="block">
        <h2>保存与发布</h2>
        <div className="actions">
          <button type="button" onClick={() => void save()} disabled={busy}>
            {busy ? "处理中…" : currentId === null ? "创建用例" : "保存草稿"}
          </button>
          <button type="button" onClick={() => void saveThenPublish()} disabled={busy}>
            保存并发布
          </button>
          <button type="button" onClick={onClose}>
            关闭
          </button>
        </div>
        {creating ? <Hint>用例已创建；列表稍后会刷新，可继续编辑或发布。</Hint> : null}
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
      </div>

      <RunPanel
        workspaceId={workspaceId}
        projectId={projectId}
        environmentId={selectedEnvironmentId}
        caseId={currentId}
        needsVersion={mustEnsureVersion}
        publishedVersion={matchingVersion}
        onEnsureVersion={saveThenPublish}
        onReport={setReport}
      />
    </section>
  );
}
