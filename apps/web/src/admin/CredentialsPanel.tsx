/**
 * 人工凭证：秘密、身份配置与固定版本的用途授权。
 *
 * 秘密只进不出：请求可以带明文，响应永远不返回明文，也不返回密文；界面只显示名称
 * 与版本号，输入框在保存后立刻清空，避免值停留在屏幕上或历史里。身份配置按不可变
 * 版本新增，当前生效的凭证集合按槽位绑定秘密版本；用途授权把“谁能用这组凭证”固定
 * 到某一条用例版本（或一次调试快照），而不是笼统地放开给整个项目。
 *
 * 三条“不让用户拼内部结构”的边界，都是本任务修过的真实缺陷：
 * - **认证方案用表单，不写 JSON**。认证位置、值前缀与失效判据由选择项生成（见
 *   authScheme.ts）。让管理员手写配置 JSON，拼错一个键不会报错，只会让配置静静地
 *   缺一半，直到运行时以“凭证没生效”暴露。
 * - **主体与版本用选择器，不抄 UUID**。主体按可见成员的中文姓名选，默认当前账号；
 *   用例版本按“用例名 / 版本号 / 时间”选，从当前打开的用例进入时预选那一版。
 * - **绑定提交的是整份集合**。服务端每次整份新建并激活集合，只提交一个槽位等于把
 *   其他槽位删掉，而界面上看不出发生过什么。整份集合是“槽位 → 版本”的映射，所以
 *   同一个槽位出现两行时后一行会覆盖前一行：重复槽位在选择器里就被挡住，提交前再
 *   拒绝一次，绝不让“保存了两行”的成功提示建立在只生效一行之上。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ComponentRef } from "react";
import { Button, Checkbox, Collapse, Flex, Input, Select, Typography } from "antd";

import { ApiError, apiDelete, apiSend, projectPath } from "../api/client";
import {
  toCaseSummaryList,
  toCaseVersion,
  toCredentialGrant,
  toCredentialGrantList,
  toCredentialProfile,
  toCredentialProfileList,
  toCredentialSet,
  toSecret,
  toSecretList,
  toSecretVersion,
  toSecretVersionList,
  toWorkspaceMemberList,
} from "../api/guards";
import type {
  CaseSummary,
  CaseVersion,
  CredentialGrant,
  CredentialProfile,
  CredentialSet,
  Environment,
  Secret,
  SecretVersion,
  WorkspaceMember,
} from "../api/types";
import { Empty, ErrorText, Hint, Loading, Notice } from "../components/Feedback";
import { useLeaveReport } from "../hooks/leaveGuard";
import { useResource } from "../hooks/useResource";
import {
  AUTH_SCHEMES,
  EMPTY_AUTH,
  INVALIDATION_CODES,
  applyScheme,
  authConfig,
  authLocationError,
  authSlot,
  authSlots,
  invalidationSummary,
  type AuthDeclaration,
  type AuthSchemeId,
} from "./authScheme";

const ROLE_LABEL: Record<string, string> = { admin: "管理员", editor: "编辑者", viewer: "查看者" };

/** 槽位声明与绑定用同一套格式（与执行内核一致：header.X 或 query.x）。 */
function slotFormatError(slots: string[]): string | null {
  const bad = slots.find((slot) => {
    const [kind, name] = slot.split(".", 2);
    return (kind !== "header" && kind !== "query") || !name;
  });
  return bad === undefined ? null : `认证槽位格式无效：${bad}（应为 header.X 或 query.x）。`;
}

function splitList(text: string): string[] {
  return text
    .split(/[,，\n]/)
    .map((item) => item.trim())
    .filter((item) => item !== "");
}

/** 一行绑定草稿：槽位、秘密与秘密版本；服务端读回来的绑定也用它承载。 */
interface BindingRow {
  key: string;
  slot: string;
  secretId: string;
  versionId: string;
  /** 服务端当前绑定的版本号，用于在版本列表未到时不把选择框显示成别的值。 */
  knownVersion: number | null;
}

let rowSeq = 0;
function nextRowKey(): string {
  rowSeq += 1;
  return `row-${rowSeq}`;
}

function bindingKey(slot: string, versionId: string): string {
  return `${slot}::${versionId}`;
}

/** 一组绑定的比较用指纹：与服务端整份集合对得上就是没改过。顺序不算改动。 */
function keysOf(rows: { slot: string; versionId: string }[]): string[] {
  return rows.map((row) => bindingKey(row.slot, row.versionId)).sort();
}

function sameKeys(left: string[], right: string[]): boolean {
  return left.length === right.length && left.every((item, index) => item === right[index]);
}

/**
 * 出现两次以上的槽位。
 *
 * 整份集合是“槽位 → 秘密版本”的映射，同一个槽位出现两行时后一行会覆盖前一行，
 * 而成功提示仍按行数报告，界面上看不出少生效了一行。这里先把它找出来：选择器据此
 * 挡住重复选择，提交前再拒绝一次，两处都不依赖“用户会自己发现”。
 */
function duplicateSlots(rows: BindingRow[]): string[] {
  const seen = new Set<string>();
  const duplicated = new Set<string>();
  for (const row of rows) {
    const slot = row.slot.trim();
    if (slot === "") continue;
    if (seen.has(slot)) duplicated.add(slot);
    seen.add(slot);
  }
  return [...duplicated];
}

function setToRows(set: CredentialSet | null): BindingRow[] {
  if (set === null) return [];
  return set.slots.map((item) => ({
    key: nextRowKey(),
    slot: item.auth_slot,
    secretId: item.secret_id,
    versionId: item.secret_version_id,
    knownVersion: item.secret_version,
  }));
}

/**
 * 秘密版本选择：只列版本号，没有任何值。
 *
 * 版本列表没到之前不显示成“已选中别的版本”：选择框的值仍然是要提交的那个 id，
 * 只是把当前值单独作为一个选项列出来，避免出现“显示第一项、提交另一个值”。
 */
function SecretVersionSelect({
  id,
  workspaceId,
  projectId,
  secretId,
  value,
  knownVersion,
  disabled,
  onChange,
}: {
  id: string;
  workspaceId: string;
  projectId: string;
  secretId: string;
  value: string;
  knownVersion: number | null;
  disabled: boolean;
  onChange: (versionId: string) => void;
}) {
  const selectRef = useRef<ComponentRef<typeof Select>>(null);
  const versions = useResource<SecretVersion[]>(
    secretId ? `${workspaceId}/${projectId}/secrets/${secretId}/versions` : null,
    (signal) =>
      apiSend(
        projectPath(workspaceId, projectId, `/credentials/secrets/${secretId}/versions`),
        "GET",
        undefined,
        toSecretVersionList,
        { signal },
      ),
  );
  const list = versions.data ?? [];
  const loading = versions.loading && versions.data === null;
  const missing = !loading && value !== "" && !list.some((item) => item.version_id === value);
  const options = [
    ...(loading ? [{ value, label: "正在读取版本…" }] : []),
    ...(!loading && value === "" ? [{ value: "", label: "请选择版本" }] : []),
    ...(missing
      ? [{
          value,
          label: knownVersion === null ? "当前绑定版本" : `当前绑定第 ${knownVersion} 版`,
        }]
      : []),
    ...list.map((item) => ({ value: item.version_id, label: `第 ${item.version} 版` })),
  ];

  return (
    <>
      <label htmlFor={id} onClick={() => selectRef.current?.focus()}>版本</label>
      <Select
        ref={selectRef}
        id={id}
        aria-label="秘密版本"
        data-selected-value={value}
        value={value}
        disabled={disabled || loading}
        options={options}
        onChange={onChange}
      />
    </>
  );
}

/**
 * 一个身份配置的当前凭证集合：整份展示、整份编辑、整份提交。
 *
 * 槽位集合必须先读出来才能改：服务端的“切换当前凭证集合”是整份替换，界面如果只
 * 知道用户这一次填的那一个槽位，另一次绑定就会被连同旧集合一起丢掉。
 */
function ProfileBindings({
  workspaceId,
  projectId,
  profile,
  secrets,
  busy,
  onBusy,
  onProfileChanged,
}: {
  workspaceId: string;
  projectId: string;
  profile: CredentialProfile;
  secrets: Secret[];
  busy: boolean;
  onBusy: (value: boolean) => void;
  /**
   * 这份集合被整份切换后通知上层重拉身份列表。
   *
   * 身份列表里的 `epoch N` 是列表被读回时的取值。绑定保存只刷新了下面这一份集合，
   * 身份列表没人动过，于是同一个身份上“当前集合：epoch 1”和“epoch 0”同时显示——
   * 管理员据此无法判断这次绑定到底生效没有。
   */
  onProfileChanged: () => void;
}) {
  const scope = `${workspaceId}/${projectId}`;
  const current = useResource<CredentialSet>(`${scope}/profiles/${profile.id}/set`, (signal) =>
    apiSend(
      projectPath(workspaceId, projectId, `/credentials/profiles/${profile.id}/set`),
      "GET",
      undefined,
      toCredentialSet,
      { signal },
    ),
  );

  const [rows, setRows] = useState<BindingRow[] | null>(null);
  const [baseline, setBaseline] = useState<string[] | null>(null);
  const [expectedEpoch, setExpectedEpoch] = useState<number | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  /**
   * 基线只建立一次，之后不再被服务端数据覆盖。
   *
   * 服务端的集合可能因为别人切换而变（epoch 前进）。若每次读到新集合就重建草稿，
   * 用户改到一半的绑定会被静默替换成别人的集合，而屏幕上看起来只是“刷新了一下”。
   * 因此刷新只用来发现“服务端已经不是我们基于的那一版”，草稿仍然属于用户；要改用
   * 服务端那一版必须由用户点按钮。
   */
  const seeded = useRef(false);
  useEffect(() => {
    if (seeded.current || current.data === null) return;
    seeded.current = true;
    setRows(setToRows(current.data));
    setBaseline(keysOf(current.data.slots.map((item) => ({ slot: item.auth_slot, versionId: item.secret_version_id }))));
    setExpectedEpoch(current.data.epoch);
  }, [current.data]);

  const serverSet = current.data;
  const drafts = rows ?? [];
  const dirty = rows !== null && (baseline === null || !sameKeys(keysOf(rows), baseline));
  const ready = rows !== null;
  const stale = expectedEpoch !== null && serverSet !== null && expectedEpoch !== serverSet.epoch;
  /** 同一个槽位被两行选中：提交前会拒绝，这里先把它显示出来。 */
  const duplicated = duplicateSlots(drafts);

  useLeaveReport(`credential-bindings:${scope}:${profile.id}`, { dirty, busy });

  /** 采用服务端当前的集合：只有用户明确选择时才丢掉自己的草稿。 */
  function adoptServerSet() {
    if (serverSet === null) return;
    setMessage(null);
    setFailure(null);
    setRows(setToRows(serverSet));
    setBaseline(
      keysOf(serverSet.slots.map((item) => ({ slot: item.auth_slot, versionId: item.secret_version_id }))),
    );
    setExpectedEpoch(serverSet.epoch);
  }

  function updateRow(key: string, patch: Partial<BindingRow>) {
    setMessage(null);
    setFailure(null);
    setRows((items) => (items ?? []).map((item) => (item.key === key ? { ...item, ...patch } : item)));
  }

  function addRow() {
    setMessage(null);
    setFailure(null);
    const used = new Set(drafts.map((row) => row.slot));
    const free = profile.allowed_auth_slots.find((slot) => !used.has(slot)) ?? "";
    setRows([
      ...drafts,
      { key: nextRowKey(), slot: free, secretId: "", versionId: "", knownVersion: null },
    ]);
  }

  async function saveAll() {
    setMessage(null);
    setFailure(null);
    if (drafts.length === 0) {
      setFailure("凭证集合至少要有一个槽位；整份清空不是收窄范围。");
      return;
    }
    const slots: Record<string, string> = {};
    const used = new Set<string>();
    for (const row of drafts) {
      const slot = row.slot.trim();
      if (!slot || !row.versionId) {
        setFailure("每一行都要选好槽位与秘密版本。");
        return;
      }
      const bad = slotFormatError([slot]);
      if (bad !== null) {
        setFailure(bad);
        return;
      }
      if (!profile.allowed_auth_slots.includes(slot)) {
        setFailure(
          `槽位 ${slot} 不在「${profile.name}」声明的允许槽位里（${profile.allowed_auth_slots.join("、") || "空"}）。`,
        );
        return;
      }
      // 组装成对象之前先拒绝重复槽位：映射里后一行会覆盖前一行，而成功提示仍按
      // 行数报告，用户以为两个版本都保存了。此时不发写请求。
      if (used.has(slot)) {
        setFailure(
          `槽位 ${slot} 重复：一个槽位只能绑定一个秘密版本，重复的那一行会覆盖另一行。请把重复行改成别的槽位，或移除它。`,
        );
        return;
      }
      used.add(slot);
      slots[slot] = row.versionId;
    }
    onBusy(true);
    try {
      const saved = await apiSend(
        projectPath(workspaceId, projectId, `/credentials/profiles/${profile.id}/set`),
        "PUT",
        { slots, expected_epoch: expectedEpoch },
        toCredentialProfile,
      );
      // 基线按服务端确认的结果原子前进：不等列表刷新，保存后立刻就不是“未保存”了。
      setBaseline(keysOf(drafts));
      setExpectedEpoch(saved.current_epoch);
      setMessage(`「${profile.name}」已整份切换凭证集合，共 ${drafts.length} 个槽位。`);
      current.reload();
      onProfileChanged();
    } catch (cause) {
      setFailure(cause instanceof ApiError ? cause.message : "切换凭证集合失败");
      // 冲突说明服务端已经换过一版：把新版本读回来提示用户，但不覆盖草稿。
      if (cause instanceof ApiError && cause.isConflict) current.reload();
    } finally {
      onBusy(false);
    }
  }

  return (
    <div className="credential-bindings">
      <span className="caption">
        当前集合：epoch {serverSet?.epoch ?? profile.current_epoch}
        {serverSet?.set_id ? ` · 集合 ${serverSet.set_id.slice(0, 8)}…` : " · 尚未绑定任何槽位"}
        {serverSet?.status ? ` · ${serverSet.status}` : ""}
      </span>
      {current.loading && serverSet === null ? <Loading label="正在读取当前凭证集合…" /> : null}
      {current.error ? <ErrorText message={current.error.message} /> : null}
      {stale ? (
        <Notice
          tone="warning"
          title={`当前集合已被别人切换到 epoch ${serverSet?.epoch}，你的表单基于 epoch ${expectedEpoch}；原样提交会被拒绝。`}
        >
          <div className="actions">
            <Button htmlType="button" onClick={adoptServerSet}>
              改用服务端当前集合
            </Button>
          </div>
        </Notice>
      ) : null}
      {ready ? (
        <>
          {drafts.length === 0 ? (
            <Hint>这个身份还没有绑定任何槽位；执行时不会注入凭证。</Hint>
          ) : null}
          {duplicated.length > 0 ? (
            <Notice
              tone="warning"
              title={`槽位重复：${duplicated.join("、")}。一个槽位只能绑定一个秘密版本，重复行会互相覆盖，保存会被拒绝。`}
            />
          ) : null}
          {drafts.map((row) => {
            const available = profile.allowed_auth_slots.includes(row.slot);
            return (
              <div className="binding-row" key={row.key}>
                <span className="param">
                  <label htmlFor={`binding-slot-${row.key}`}>槽位</label>
                  <Select
                    id={`binding-slot-${row.key}`}
                    data-selected-value={row.slot}
                    value={row.slot}
                    disabled={busy}
                    options={[
                      ...(row.slot === "" ? [{ value: "", label: "请选择槽位" }] : []),
                      ...(!available ? [{ value: row.slot, label: `${row.slot}（不在声明范围内）` }] : []),
                      ...profile.allowed_auth_slots.map((slot) => {
                      // 已被别行占用的槽位不可再选：同一个槽位绑两个版本时，后一行
                      // 会在组装成映射时覆盖前一行，而提示仍按行数报告。
                      const takenByOther = drafts.some(
                        (item) => item.key !== row.key && item.slot.trim() === slot,
                      );
                        return {
                          value: slot,
                          label: `${slot}${takenByOther ? "（已被另一行使用）" : ""}`,
                          disabled: takenByOther,
                        };
                      }),
                    ]}
                    onChange={(value) => updateRow(row.key, { slot: value })}
                  />
                </span>
                <span className="param">
                  <label htmlFor={`binding-secret-${row.key}`}>秘密</label>
                  <Select
                    id={`binding-secret-${row.key}`}
                    data-selected-value={row.secretId}
                    value={row.secretId}
                    disabled={busy}
                    options={[
                      { value: "", label: "请选择秘密" },
                      ...secrets.map((secret) => ({
                        value: secret.id,
                        label: `${secret.name}（最新第 ${secret.latest_version} 版）`,
                      })),
                    ]}
                    onChange={(value) =>
                      updateRow(row.key, { secretId: value, versionId: "", knownVersion: null })
                    }
                  />
                </span>
                <span className="param">
                  <SecretVersionSelect
                    id={`binding-version-${row.key}`}
                    workspaceId={workspaceId}
                    projectId={projectId}
                    secretId={row.secretId}
                    value={row.versionId}
                    knownVersion={row.knownVersion}
                    disabled={busy}
                    onChange={(versionId) => updateRow(row.key, { versionId })}
                  />
                </span>
                <Button
                  htmlType="button"
                  disabled={busy || drafts.length <= 1}
                  onClick={() => setRows(drafts.filter((item) => item.key !== row.key))}
                >
                  移除这行
                </Button>
              </div>
            );
          })}
          <div className="actions">
            <Button
              htmlType="button"
              onClick={addRow}
              disabled={busy || profile.allowed_auth_slots.length === 0}
            >
              ＋添加槽位
            </Button>
            <Button type="primary" htmlType="button" onClick={() => void saveAll()} disabled={busy || !dirty || drafts.length === 0}>
              {busy ? "保存中…" : "保存整份绑定集合"}
            </Button>
          </div>
          <Hint>
            提交的是整份集合：这里列出的每个槽位都会一起生效，没列出的槽位不保留。
            集合内容变化会推进 epoch，已发出的运行仍按签发时的那一版集合执行。
          </Hint>
        </>
      ) : null}
      {message ? <Notice tone="info" title={message} /> : null}
      {failure ? <ErrorText message={failure} /> : null}
    </div>
  );
}

export function CredentialsPanel({
  workspaceId,
  projectId,
  environments,
  canAdmin,
  currentUser,
  currentCase,
  onExecutionConfigChanged,
  anchorId,
  open,
  onOpenChange,
}: {
  workspaceId: string;
  projectId: string;
  environments: Environment[];
  canAdmin: boolean;
  currentUser: { user_id: string; display_name: string };
  currentCase: { caseId: string; versionId: string | null } | null;
  /** 身份配置、秘密与用途授权成功变更后通知：它们决定请求以谁的身份发出。 */
  onExecutionConfigChanged: () => void;
  anchorId?: string;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}) {
  const scope = `${workspaceId}/${projectId}`;
  const secrets = useResource<Secret[]>(canAdmin ? `${scope}/secrets` : null, (signal) =>
    apiSend(projectPath(workspaceId, projectId, "/credentials/secrets"), "GET", undefined, toSecretList, {
      signal,
    }),
  );
  const profiles = useResource<CredentialProfile[]>(canAdmin ? `${scope}/profiles` : null, (signal) =>
    apiSend(projectPath(workspaceId, projectId, "/credentials/profiles"), "GET", undefined, toCredentialProfileList, {
      signal,
    }),
  );
  const grants = useResource<CredentialGrant[]>(canAdmin ? `${scope}/grants` : null, (signal) =>
    apiSend(projectPath(workspaceId, projectId, "/credentials/grants"), "GET", undefined, toCredentialGrantList, {
      signal,
    }),
  );

  /**
   * 绑定集合保存后的统一处理。
   *
   * 它不经过 `submit`（那是表单动作的包装），因此需要自己的成功出口：整份集合切换会改变
   * 请求实际注入的凭证，与身份配置同等重要。子级不再另发第二次通知——一次成功只通知一次。
   */
  const reloadProfiles = profiles.reload;
  const onProfileChanged = useCallback(() => {
    onExecutionConfigChanged();
    reloadProfiles();
  }, [onExecutionConfigChanged, reloadProfiles]);
  // 主体只能从可见成员里选：手抄用户 id 最容易把授权签发给一个不相干的人。
  const members = useResource<WorkspaceMember[]>(canAdmin ? `${scope}/members` : null, (signal) =>
    apiSend(`/workspaces/${workspaceId}/members`, "GET", undefined, toWorkspaceMemberList, { signal }),
  );
  const cases = useResource<CaseSummary[]>(canAdmin ? `${scope}/cases` : null, (signal) =>
    apiSend(projectPath(workspaceId, projectId, "/cases"), "GET", undefined, toCaseSummaryList, { signal }),
  );

  const [message, setMessage] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  /** 某一个身份正在提交整份绑定集合；与列表上的其他动作互不干扰。 */
  const [bindingBusy, setBindingBusy] = useState<string | null>(null);

  const [secretName, setSecretName] = useState("");
  const [secretValue, setSecretValue] = useState("");
  const [rotateValue, setRotateValue] = useState<Record<string, string>>({});

  /** 身份配置创建表单：环境、名称、允许的目标与认证方案声明。 */
  const [identityEnvironmentId, setIdentityEnvironmentId] = useState("");
  const [identityName, setIdentityName] = useState("");
  const [identityTargets, setIdentityTargets] = useState("");
  const [auth, setAuth] = useState<AuthDeclaration>(EMPTY_AUTH);
  const [identityTouched, setIdentityTouched] = useState(false);

  /** 用途授权表单：身份、主体、类型、用例版本与允许槽位。 */
  const [grantProfileId, setGrantProfileId] = useState("");
  const [grantPrincipalId, setGrantPrincipalId] = useState(currentUser.user_id);
  const [grantType, setGrantType] = useState<"case_version" | "debug_snapshot">("case_version");
  const [grantCaseId, setGrantCaseId] = useState("");
  const [grantCaseVersionId, setGrantCaseVersionId] = useState("");
  const [grantSnapshotHash, setGrantSnapshotHash] = useState("");
  const [grantSlots, setGrantSlots] = useState<string[]>([]);
  const [grantTouched, setGrantTouched] = useState(false);

  useEffect(() => {
    // 以当前项目的环境列表为准对齐选择。切换项目后，先前选中的环境 id 已经不属于
    // 新列表：下拉会因为“值不在选项里”回退显示第一个环境，提交的却仍是那个旧 id，
    // 服务端只能报“环境不存在或不属于本项目”，而界面上看不出哪里不对。
    if (environments.length === 0) return;
    if (!environments.some((item) => item.id === identityEnvironmentId)) {
      setIdentityEnvironmentId(environments[0].id);
    }
  }, [environments, identityEnvironmentId]);

  /**
   * 从当前打开的用例进入授权时预选它的版本。
   *
   * 只在用户还没动过这块表单时生效：用户已经选好的用例不该被编辑器后来打开的另一条
   * 用例顶掉。
   */
  useEffect(() => {
    if (currentCase === null || grantTouched) return;
    setGrantCaseId(currentCase.caseId);
    setGrantCaseVersionId(currentCase.versionId ?? "");
  }, [currentCase, grantTouched]);

  /**
   * 列表里没有“当前打开的用例”时补读一次。
   *
   * 授权表单只能从这里选到用例，而这份列表是进入管理面板时读一次的：刚在编辑器里新建
   * 并发布的用例不在里面，管理员根本选不到它。编辑器报上来的那条用例是确切已知存在的，
   * 看见它不在列表里就说明列表过期了——补读一次。同一个 id 只补读一次，已被归档而确实
   * 不在列表里的用例不会让这里反复重试。
   */
  const reloadedFor = useRef<string | null>(null);
  const reloadCases = cases.reload;
  useEffect(() => {
    const caseId = currentCase?.caseId ?? null;
    if (caseId === null || cases.data === null) return;
    if (cases.data.some((item) => item.id === caseId)) return;
    if (reloadedFor.current === caseId) return;
    reloadedFor.current = caseId;
    reloadCases();
  }, [currentCase?.caseId, cases.data, reloadCases]);

  const secretList = useMemo(() => secrets.data ?? [], [secrets.data]);
  const profileList = useMemo(() => profiles.data ?? [], [profiles.data]);
  const grantList = useMemo(() => grants.data ?? [], [grants.data]);
  const memberList = useMemo(() => members.data ?? [], [members.data]);
  const caseList = useMemo(() => cases.data ?? [], [cases.data]);

  const grantVersions = useResource<CaseVersion[]>(
    canAdmin && grantCaseId ? `${scope}/cases/${grantCaseId}/versions` : null,
    (signal) =>
      apiSend(
        projectPath(workspaceId, projectId, `/cases/${grantCaseId}/versions`),
        "GET",
        undefined,
        (raw) => (Array.isArray(raw) ? raw.map(toCaseVersion) : []),
        { signal },
      ),
  );

  /**
   * 版本列表里缺少“编辑器刚确认的那一版”时补读一次。
   *
   * 这份列表按 `${用例}` 读取，而给同一条用例发布新版本并不会改变这个键：新用例发布后
   * 下拉里仍是发布**之前**读到的那一份，于是编辑器已经显示“已发布 v1”、授权选择器却
   * 还是“这条用例还没有已发布版本”，管理员从这里签不出授权。编辑器报上来的那一版是
   * 服务端刚确认存在的，看见它不在列表里就说明列表过期了。与上面的用例列表同理，
   * 同一个版本只补读一次，避免与“选了别的用例”这类正常情形反复互推。
   *
   * 用例列表一起重读：版本选项与用例选项上的名字都取自它。只重读版本，重命名过的那条
   * 用例会在这里继续显示旧名字，而左侧列表已经是新名字——同一个屏幕上两份名字。
   */
  const reloadedVersionFor = useRef<string | null>(null);
  const reloadGrantVersions = grantVersions.reload;
  useEffect(() => {
    const versionId = currentCase?.versionId ?? null;
    if (versionId === null || grantVersions.data === null) return;
    if (grantVersions.data.some((item) => item.id === versionId)) return;
    if (reloadedVersionFor.current === versionId) return;
    reloadedVersionFor.current = versionId;
    reloadGrantVersions();
    reloadCases();
  }, [currentCase?.versionId, grantVersions.data, reloadGrantVersions, reloadCases]);

  function environmentName(environmentId: string): string {
    return environments.find((item) => item.id === environmentId)?.name ?? environmentId;
  }

  function memberName(userId: string): string {
    return memberList.find((item) => item.user_id === userId)?.display_name ?? `${userId.slice(0, 8)}…`;
  }

  const selectedProfile = profileList.find((item) => item.id === grantProfileId) ?? null;

  /**
   * 用例名必须能认出来：列表里还没有这条用例时退回可辨认的 id 前缀，而不是印通用词。
   *
   * 版本选项要回答的是“这一版属于哪条用例”。名字解析不到时印“用例 · 第 N 版”，读
   * 起来像一条真名字，管理员据此签发的授权就落到一条他以为认识的用例上。
   */
  function caseLabel(caseId: string): string {
    const found = caseList.find((item) => item.id === caseId);
    if (found !== undefined) return found.name;
    return caseId === currentCase?.caseId
      ? `当前打开的用例 ${caseId.slice(0, 8)}…`
      : `用例 ${caseId.slice(0, 8)}…`;
  }

  /** 未保存的表单内容：秘密输入框里还有值、或身份／授权表单已经动过。 */
  const formDirty =
    secretName.trim() !== "" ||
    secretValue !== "" ||
    Object.values(rotateValue).some((value) => value !== "") ||
    identityName.trim() !== "" ||
    identityTargets.trim() !== "" ||
    identityTouched ||
    grantProfileId !== "" ||
    grantSlots.length > 0 ||
    grantSnapshotHash.trim() !== "" ||
    grantTouched;
  useLeaveReport(`credentials:${scope}`, { dirty: formDirty, busy });

  /**
   * 统一的提交包装：成功提示、失败转述、忙碌状态只写一次。
   *
   * 成功时通知一次执行配置变更：秘密、身份配置与用途授权都会改变“这次请求以什么身份
   * 发出、是否被允许”，工作台里基于旧配置的预检与“当前通过”必须立即失效。
   * **失败不通知**——没有发生变更，作废结论只会让用户白等一次重新检查。
   */
  async function submit(action: () => Promise<string>) {
    setMessage(null);
    setFailure(null);
    setBusy(true);
    try {
      setMessage(await action());
      onExecutionConfigChanged();
    } catch (cause) {
      setFailure(cause instanceof ApiError ? cause.message : "操作失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  }

  async function createSecret() {
    if (!secretName.trim() || !secretValue) {
      setFailure("请填写秘密名称与值。");
      return;
    }
    await submit(async () => {
      const created = await apiSend(
        projectPath(workspaceId, projectId, "/credentials/secrets"),
        "POST",
        { name: secretName.trim(), value: secretValue },
        toSecret,
      );
      setSecretName("");
      setSecretValue("");
      secrets.reload();
      return `已保存秘密「${created.name}」；值不会再次显示。`;
    });
  }

  async function rotate(secret: Secret) {
    const value = rotateValue[secret.id] ?? "";
    if (!value) {
      setFailure(`请填写「${secret.name}」的新值。`);
      return;
    }
    await submit(async () => {
      const version = await apiSend(
        projectPath(workspaceId, projectId, `/credentials/secrets/${secret.id}/versions`),
        "POST",
        { value },
        toSecretVersion,
      );
      setRotateValue((current) => ({ ...current, [secret.id]: "" }));
      secrets.reload();
      return `「${secret.name}」已轮换到第 ${version.version} 版；旧版本仍被已发出的运行引用。`;
    });
  }

  function patchAuth(patch: Partial<AuthDeclaration>) {
    setIdentityTouched(true);
    setAuth((current) => ({ ...current, ...patch }));
  }

  function setAuthLocation(index: number, patch: Partial<AuthDeclaration["locations"][number]>) {
    setIdentityTouched(true);
    setAuth((current) => ({
      ...current,
      locations: current.locations.map((item, position) =>
        position === index ? { ...item, ...patch } : item,
      ),
    }));
  }

  async function createProfile() {
    setMessage(null);
    setFailure(null);
    const declaration = authLocationError(auth);
    if (declaration !== null) {
      setFailure(declaration);
      return;
    }
    if (!identityEnvironmentId || !identityName.trim()) {
      setFailure("请选择环境并填写身份名称。");
      return;
    }
    const slots = authSlots(auth);
    const badSlot = slotFormatError(slots);
    if (badSlot !== null) {
      setFailure(badSlot);
      return;
    }
    await submit(async () => {
      await apiSend(
        projectPath(workspaceId, projectId, "/credentials/profiles"),
        "POST",
        {
          environment_id: identityEnvironmentId,
          name: identityName.trim(),
          allowed_targets: splitList(identityTargets),
          allowed_auth_slots: slots,
          config: authConfig(auth),
        },
        toCredentialProfile,
      );
      setIdentityName("");
      setIdentityTargets("");
      setIdentityTouched(false);
      setAuth(EMPTY_AUTH);
      profiles.reload();
      return "身份配置已创建，认证位置与失效判据一并保存为首个版本；接着绑定秘密即可。";
    });
  }

  async function createGrant() {
    setMessage(null);
    setFailure(null);
    const profile = selectedProfile;
    if (!profile) {
      setFailure("请选择身份配置。");
      return;
    }
    if (!grantPrincipalId) {
      setFailure("请选择被授权主体。");
      return;
    }
    if (!memberList.some((item) => item.user_id === grantPrincipalId)) {
      setFailure("被授权主体不在当前工作空间的成员名单里，请重新选择。");
      return;
    }
    if (grantType === "case_version" && !grantCaseVersionId) {
      setFailure("固定版本授权必须选择一条用例版本。");
      return;
    }
    if (grantType === "debug_snapshot" && !grantSnapshotHash.trim()) {
      setFailure("调试快照授权必须填写快照摘要。");
      return;
    }
    await submit(async () => {
      await apiSend(
        projectPath(workspaceId, projectId, "/credentials/grants"),
        "POST",
        {
          profile_id: profile.id,
          grant_type: grantType,
          principal_id: grantPrincipalId,
          case_version_id: grantType === "case_version" ? grantCaseVersionId : null,
          debug_snapshot_hash: grantType === "debug_snapshot" ? grantSnapshotHash.trim() : null,
          allowed_targets: [],
          allowed_auth_slots: grantSlots,
          allowed_inputs: {},
        },
        toCredentialGrant,
      );
      setGrantSnapshotHash("");
      setGrantTouched(false);
      grants.reload();
      return "用途授权已签发；只对声明的这一份内容与当前变量输入生效，变量变化后需重新签发。";
    });
  }

  async function revoke(grant: CredentialGrant) {
    await submit(async () => {
      await apiDelete(projectPath(workspaceId, projectId, `/credentials/grants/${grant.id}`));
      grants.reload();
      return "授权已撤销。";
    });
  }

  if (!canAdmin) {
    return (
      <div className="block" id={anchorId} tabIndex={anchorId ? -1 : undefined}>
        <Collapse
          activeKey={open === undefined ? undefined : open ? ["credentials"] : []}
          defaultActiveKey={["credentials"]}
          onChange={(keys) => onOpenChange?.(Array.isArray(keys) ? keys.includes("credentials") : keys === "credentials")}
          items={[{
            key: "credentials",
            label: "人工凭证",
            children: <Hint>秘密、身份配置与用途授权都需要管理员权限；当前角色只能查看环境与用例。</Hint>,
          }]}
        />
      </div>
    );
  }

  return (
    <div className="block" id={anchorId} tabIndex={anchorId ? -1 : undefined}>
      <Collapse
        activeKey={open === undefined ? undefined : open ? ["credentials"] : []}
        defaultActiveKey={["credentials"]}
        onChange={(keys) => onOpenChange?.(Array.isArray(keys) ? keys.includes("credentials") : keys === "credentials")}
        items={[{
          key: "credentials",
          label: "人工凭证",
          children: (
            <Flex vertical gap="middle">
      <Typography.Paragraph type="secondary">
        秘密只保存不显示；身份配置按版本新增，当前集合按槽位绑定秘密版本；用途授权固定到某一条用例版本，而不是整个项目，并按签发时的普通变量冻结输入。
      </Typography.Paragraph>
      {message ? <Notice tone="info" title={message} /> : null}
      {failure ? <ErrorText message={failure} /> : null}

      <h3>秘密</h3>
      {secrets.loading ? <Loading label="正在加载秘密…" /> : null}
      {secrets.error ? <ErrorText message={secrets.error.message} /> : null}
      {secretList.length === 0 && !secrets.loading ? (
        <Empty label="还没有人工登记的秘密。" />
      ) : (
        <ul className="credential-list">
          {secretList.map((secret) => (
            <li key={secret.id}>
              <span>
                {secret.name}（{secret.kind} · 最新第 {secret.latest_version} 版）
              </span>
              <label htmlFor={`rotate-${secret.id}`}>新值</label>
              <Input.Password
                id={`rotate-${secret.id}`}
                value={rotateValue[secret.id] ?? ""}
                disabled={busy}
                onChange={(event) =>
                  setRotateValue((current) => ({ ...current, [secret.id]: event.target.value }))
                }
              />
              <Button htmlType="button" onClick={() => void rotate(secret)} disabled={busy}>
                轮换
              </Button>
            </li>
          ))}
        </ul>
      )}
      <div className="credential-create">
        <label htmlFor="secret-name">秘密名称</label>
        <Input id="secret-name" value={secretName} onChange={(event) => setSecretName(event.target.value)} />
        <label htmlFor="secret-value">秘密值（保存后不再显示）</label>
        <Input.Password
          id="secret-value"
          value={secretValue}
          onChange={(event) => setSecretValue(event.target.value)}
        />
        <Button type="primary" htmlType="button" onClick={() => void createSecret()} disabled={busy}>
          保存秘密
        </Button>
      </div>

      <h3>身份配置</h3>
      {profiles.loading && profiles.data === null ? <Loading label="正在加载身份配置…" /> : null}
      {profiles.error ? <ErrorText message={profiles.error.message} /> : null}
      {profileList.length === 0 && !profiles.loading ? (
        <Empty label="还没有身份配置；测试环境需要认证时再创建。" />
      ) : (
        <ul className="credential-list">
          {profileList.map((profile) => (
            <li key={profile.id}>
              <span>
                {profile.name} · 环境 {environmentName(profile.environment_id)} · 槽位 {profile.slot_count} 个 ·
                epoch {profile.current_epoch}
              </span>
              <span className="caption">
                声明的允许槽位：{profile.allowed_auth_slots.join("、") || "（空，不允许任何槽位）"}
              </span>
              <ProfileBindings
                workspaceId={workspaceId}
                projectId={projectId}
                profile={profile}
                secrets={secretList}
                busy={bindingBusy === profile.id}
                onBusy={(value) => setBindingBusy(value ? profile.id : null)}
                onProfileChanged={onProfileChanged}
              />
            </li>
          ))}
        </ul>
      )}
      <div className="credential-create">
        <label htmlFor="profile-env">环境</label>
        <Select
          id="profile-env"
          data-selected-value={identityEnvironmentId}
          value={identityEnvironmentId}
          options={environments.map((item) => ({ value: item.id, label: item.name }))}
          onChange={(value) => {
            setIdentityTouched(true);
            setIdentityEnvironmentId(value);
          }}
        />
        <label htmlFor="profile-name">身份名称</label>
        <Input
          id="profile-name"
          value={identityName}
          onChange={(event) => {
            setIdentityTouched(true);
            setIdentityName(event.target.value);
          }}
        />
        <fieldset className="auth-declaration">
          <legend>认证方案（选择即可，不需要填写配置 JSON）</legend>
          {auth.locations.map((location, index) => (
            <div className="auth-location" key={`auth-${index}`}>
              <span className="param">
                <label htmlFor={`auth-scheme-${index}`}>认证方式</label>
                <Select
                  id={`auth-scheme-${index}`}
                  data-selected-value={location.scheme}
                  value={location.scheme}
                  options={AUTH_SCHEMES.map((item) => ({ value: item.id, label: item.label }))}
                  onChange={(value) =>
                    setAuthLocation(index, applyScheme(location, value as AuthSchemeId))
                  }
                />
              </span>
              <span className="param">
                <label htmlFor={`auth-name-${index}`}>
                  {location.scheme === "query" ? "参数名" : "请求头名"}
                </label>
                <Input
                  id={`auth-name-${index}`}
                  value={location.name}
                  placeholder={location.scheme === "query" ? "access_token" : "Authorization"}
                  onChange={(event) => setAuthLocation(index, { name: event.target.value })}
                />
              </span>
              <span className="param">
                <label htmlFor={`auth-prefix-${index}`}>值前缀</label>
                <Input
                  id={`auth-prefix-${index}`}
                  value={location.prefix}
                  placeholder="Bearer "
                  onChange={(event) => setAuthLocation(index, { prefix: event.target.value })}
                />
              </span>
              <span className="caption">将注入到 {authSlot(location) || "（请填写名称）"}</span>
              <Button
                htmlType="button"
                disabled={auth.locations.length <= 1}
                onClick={() =>
                  patchAuth({ locations: auth.locations.filter((_, position) => position !== index) })
                }
              >
                移除
              </Button>
            </div>
          ))}
          <div className="actions">
            <Button
              htmlType="button"
              onClick={() =>
                patchAuth({
                  locations: [
                    ...auth.locations,
                    { scheme: "raw_header", name: "", prefix: "" },
                  ],
                })
              }
            >
              ＋添加认证位置
            </Button>
          </div>
          <div className="invalid-rules">
            <span className="caption">失效判据：</span>
            {INVALIDATION_CODES.map((code) => (
              <label key={code} htmlFor={`invalid-${code}`}>
                <Checkbox
                  id={`invalid-${code}`}
                  checked={auth.invalidation.statusCodes.includes(code)}
                  onChange={(event) =>
                    patchAuth({
                      invalidation: {
                        ...auth.invalidation,
                        statusCodes: event.target.checked
                          ? [...auth.invalidation.statusCodes, code]
                          : auth.invalidation.statusCodes.filter((item) => item !== code),
                      },
                    })
                  }
                />
                响应 {code} 视为凭证失效
              </label>
            ))}
            <label htmlFor="invalid-redirect">
              <Checkbox
                id="invalid-redirect"
                checked={auth.invalidation.redirectToLogin}
                onChange={(event) =>
                  patchAuth({
                    invalidation: { ...auth.invalidation, redirectToLogin: event.target.checked },
                  })
                }
              />
              被重定向到登录页视为失效
            </label>
          </div>
          <p className="caption">
            将保存的认证位置：{authSlots(auth).join("、") || "（尚未填写）"}；判据：
            {invalidationSummary(auth.invalidation)}。
          </p>
        </fieldset>
        <label htmlFor="profile-targets">允许的目标（逗号分隔，可留空表示不限）</label>
        <Input
          id="profile-targets"
          value={identityTargets}
          onChange={(event) => {
            setIdentityTouched(true);
            setIdentityTargets(event.target.value);
          }}
        />
        <Button type="primary" htmlType="button" onClick={() => void createProfile()} disabled={busy}>
          创建身份配置
        </Button>
      </div>

      <h3>用途授权</h3>
      <Hint>
        用途授权只对签发的这一份内容生效，并同时冻结当时生效的项目／环境普通变量。
        请求里的占位内容由变量解析，改了变量就等于换了要发的请求：原授权会被拒绝执行，
        需要在新的变量下重新签发授权。一次性调试授权用掉即止，不能复用给下一次执行。
      </Hint>
      {grants.loading ? <Loading label="正在加载用途授权…" /> : null}
      {grants.error ? <ErrorText message={grants.error.message} /> : null}
      {grantList.length === 0 && !grants.loading ? (
        <Empty label="还没有用途授权；没有授权时执行不会带上凭证。" />
      ) : (
        <ul className="credential-list">
          {grantList.map((grant) => (
            <li key={grant.id}>
              <span>
                {grant.grant_type === "case_version" ? "固定用例版本" : "调试快照"} ·{" "}
                {grant.case_version_id ? `版本 ${grant.case_version_id.slice(0, 8)}…` : grant.debug_snapshot_hash} ·{" "}
                主体 {memberName(grant.principal_id)} ·{" "}
                {profileList.find((item) => item.id === grant.profile_id)?.name ?? "身份已删除"} ·{" "}
                {grant.status}
              </span>
              <span className="caption">
                允许槽位：{grant.allowed_auth_slots.join("、") || "全部声明槽位"} ·{" "}
                {grant.expires_at ? `到期 ${grant.expires_at}` : "长期有效"}
                {grant.used_at ? ` · 已使用 ${grant.used_at}` : ""}
              </span>
              <Button danger htmlType="button" onClick={() => void revoke(grant)} disabled={busy}>
                撤销授权
              </Button>
            </li>
          ))}
        </ul>
      )}
      <div className="credential-create">
        <label htmlFor="grant-profile">身份配置</label>
        <Select
          id="grant-profile"
          data-selected-value={grantProfileId}
          value={grantProfileId}
          options={[
            { value: "", label: "请选择身份" },
            ...profileList.map((profile) => ({
              value: profile.id,
              label: `${profile.name}（${environmentName(profile.environment_id)}）`,
            })),
          ]}
          onChange={(value) => {
            setGrantTouched(true);
            setGrantProfileId(value);
            setGrantSlots([]);
          }}
        />
        <label htmlFor="grant-principal">被授权主体（当前工作空间成员）</label>
        {members.error ? <ErrorText message={members.error.message} /> : null}
        <Select
          id="grant-principal"
          data-selected-value={grantPrincipalId}
          value={grantPrincipalId}
          disabled={members.loading && members.data === null}
          options={[
            ...(members.loading && members.data === null
              ? [{ value: grantPrincipalId, label: "正在读取成员…" }]
              : []),
            ...memberList.map((member) => ({
              value: member.user_id,
              label: `${member.display_name}${member.user_id === currentUser.user_id ? "（我）" : ""} · ${ROLE_LABEL[member.role] ?? member.role}`,
            })),
          ]}
          onChange={(value) => {
            setGrantTouched(true);
            setGrantPrincipalId(value);
          }}
        />
        <span className="caption">
          授权只在签发时声明的环境上生效；来源环境：{selectedProfile ? environmentName(selectedProfile.environment_id) : "（先选身份）"}
        </span>
        <label htmlFor="grant-type">授权类型</label>
        <Select
          id="grant-type"
          data-selected-value={grantType}
          value={grantType}
          options={[
            { value: "case_version", label: "固定用例版本" },
            { value: "debug_snapshot", label: "调试快照" },
          ]}
          onChange={(value) => {
            setGrantTouched(true);
            setGrantType(value as typeof grantType);
          }}
        />
        {grantType === "case_version" ? (
          <>
            <label htmlFor="grant-case">用例</label>
            {cases.error ? <ErrorText message={cases.error.message} /> : null}
            <Select
              id="grant-case"
              data-selected-value={grantCaseId}
              value={grantCaseId}
              options={[
                { value: "", label: "请选择用例" },
                ...(grantCaseId !== "" && !caseList.some((item) => item.id === grantCaseId)
                  ? [{ value: grantCaseId, label: `${caseLabel(grantCaseId)}（尚未读入列表）` }]
                  : []),
                ...caseList.map((item) => ({
                  value: item.id,
                  label: `${item.name}${item.id === currentCase?.caseId ? "（当前打开的用例）" : ""}`,
                })),
              ]}
              onChange={(value) => {
                setGrantTouched(true);
                setGrantCaseId(value);
                setGrantCaseVersionId("");
              }}
            />
            <label htmlFor="grant-version">已发布版本</label>
            {grantVersions.error ? <ErrorText message={grantVersions.error.message} /> : null}
            <Select
              id="grant-version"
              data-selected-value={grantCaseVersionId}
              value={grantCaseVersionId}
              disabled={!grantCaseId || (grantVersions.loading && grantVersions.data === null)}
              options={[
                {
                  value: "",
                  label: !grantCaseId
                    ? "请先选择用例"
                    : (grantVersions.data ?? []).length === 0 && !grantVersions.loading
                      ? "这条用例还没有已发布版本"
                      : "请选择版本",
                },
                ...((grantVersions.data ?? []).length > 0 &&
                grantCaseVersionId !== "" &&
                !(grantVersions.data ?? []).some((item) => item.id === grantCaseVersionId)
                  ? [{ value: grantCaseVersionId, label: "已选版本（不在本用例的版本列表中）" }]
                  : []),
                ...(grantVersions.data ?? []).map((item) => ({
                  value: item.id,
                  label: `${caseLabel(grantCaseId)} · 第 ${item.version} 版 · ${item.created_at}`,
                })),
              ]}
              onChange={(value) => {
                setGrantTouched(true);
                setGrantCaseVersionId(value);
              }}
            />
            <span className="caption">
              {grantCaseVersionId && currentCase?.versionId === grantCaseVersionId
                ? "已预选当前打开用例对应的那一版；如需授权别的版本请在上面改选。"
                : "授权只对这一版内容生效；内容改动后需要重新发布并重新签发。"}
            </span>
          </>
        ) : (
          <>
            <label htmlFor="grant-snapshot">调试快照摘要</label>
            <Input
              id="grant-snapshot"
              value={grantSnapshotHash}
              onChange={(event) => {
                setGrantTouched(true);
                setGrantSnapshotHash(event.target.value);
              }}
            />
          </>
        )}
        <label htmlFor="grant-slots">
          允许的认证槽位（不勾选表示该身份声明的全部槽位）
        </label>
        {selectedProfile === null ? (
          <span className="caption">先选择身份配置，槽位来自它的声明。</span>
        ) : selectedProfile.allowed_auth_slots.length === 0 ? (
          <span className="caption">该身份没有声明任何槽位，无法签发授权。</span>
        ) : (
          <div className="grant-slots">
            {selectedProfile.allowed_auth_slots.map((slot) => (
              <label key={slot} htmlFor={`grant-slot-${slot}`}>
                <Checkbox
                  id={`grant-slot-${slot}`}
                  checked={grantSlots.includes(slot)}
                  onChange={(event) => {
                    setGrantTouched(true);
                    setGrantSlots(
                      event.target.checked
                        ? [...grantSlots, slot]
                        : grantSlots.filter((item) => item !== slot),
                    );
                  }}
                />
                {slot}
              </label>
            ))}
          </div>
        )}
        <Button type="primary" htmlType="button" onClick={() => void createGrant()} disabled={busy}>
          签发用途授权
        </Button>
      </div>
            </Flex>
          ),
        }]}
      />
    </div>
  );
}
