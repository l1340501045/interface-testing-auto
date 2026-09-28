/**
 * 环境管理：创建测试环境、修改地址与普通变量、停用不再使用的环境。
 *
 * 本阶段不启用生产环境执行，因此界面只提供测试环境；地址由管理员填写，
 * 目标是否可达由执行池的受控目标白名单在服务端决定，前端不做放行判断。
 * 环境级普通变量与项目级同源：只收字面量，口令等秘密必须走身份凭证配置。
 *
 * 地址与变量是别人依赖的配置，因此地址在原位做语法校验：写错了立刻在输入框旁标出来，
 * 不提交、也不清空已经输入的内容。服务端仍会独立校验一次——绕过页面的调用同样要挡住。
 */
import { useRef, useState } from "react";
import { Alert, Button, Card, Collapse, Flex, Form, Input, Select, Space, Typography } from "antd";

import { ApiError, apiSend, projectPath } from "../api/client";
import { toEnvironment } from "../api/guards";
import type { Environment, VariableItem } from "../api/types";
import { ErrorText, Hint, Loading } from "../components/Feedback";
import { useLeaveReport } from "../hooks/leaveGuard";
import { VariableRowsEditor } from "../admin/VariableRowsEditor";
import {
  describeLiteralText,
  sameLiteral,
  toLiteral,
  toPayload,
  toRows,
  validateRows,
  type VariableRow,
} from "../admin/variableRows";
import {
  ENVIRONMENT_URL_EXAMPLE,
  ENVIRONMENT_URL_IP_EXAMPLE,
  validateEnvironmentUrl,
} from "./environmentUrl";

/**
 * 地址说明：域名与 IP＋端口两种写法都要讲清楚。
 *
 * 日常调试以域名为主，但 IP＋端口同样受支持（内网直连时它才是常见写法）；只给域名示例，
 * 用户会以为这里必须写域名。创建与编辑共用同一句，避免两处说明逐渐分叉。
 */
const URL_HINT = `完整的服务地址，可带基础路径，例如 ${ENVIRONMENT_URL_EXAMPLE}/api/v1；也支持 IP 与端口，例如 ${ENVIRONMENT_URL_IP_EXAMPLE}。`;

/** 一行环境变量的显示摘要：名称 = 值，让人一眼看出这条环境带了什么。 */
function summarize(variables: Record<string, unknown>): string {
  const names = Object.keys(variables);
  if (names.length === 0) return "无普通变量";
  return names.map((name) => `${name}=${describeLiteralText(variables[name])}`).join("，");
}

function variableRows(variables: Record<string, unknown>): VariableRow[] {
  return toRows(Object.entries(variables).map(([name, value]): VariableItem => ({ name, value })));
}

/**
 * 草稿是否与原环境不同：比较**将要提交的字面量**与已保存的那一份。
 *
 * 这里曾经比的是显示文本（`describeLiteralText`），并且拿 `row.original` 当草稿侧
 * 的取值。两个问题都会让保护失效：显示文本把数字 1 与字符串 "1" 一律写成 "1"，
 * 只改类型算不出差别；`row.original` 从载入起就不再变化，只把已有变量的值从 1 改成
 * 2、其他字段不动时，脏状态仍然是“干净”的——切换项目或退出时不会提示，输入直接丢。
 * 类型与值的差别只能由字面量本身回答。
 */
function edited(environment: Environment, rows: VariableRow[]): boolean {
  if (rows.length !== Object.keys(environment.variables).length) return true;
  for (const row of rows) {
    const key = row.name.trim();
    if (!(key in environment.variables)) return true;
    if (!sameLiteral(toLiteral(row), environment.variables[key])) return true;
  }
  return false;
}

export function EnvironmentPanel({
  workspaceId,
  projectId,
  environments,
  loading,
  error,
  selectedId,
  onSelect,
  canEdit,
  onChanged,
  open,
  onOpenChange,
}: {
  workspaceId: string;
  projectId: string;
  environments: Environment[];
  loading: boolean;
  error: string | null;
  selectedId: string | null;
  onSelect: (environmentId: string) => void;
  canEdit: boolean;
  onChanged: () => void;
  /**
   * 面板自身的折叠状态，由外壳持有。
   *
   * 这一层折叠必须能被**别的入口**打开：调试报“环境地址不合法”时，用户点「前往环境设置」
   * 期望直接看到环境编辑，而不是停在一个仍折叠的“环境（N）”标题上。状态若留在本组件内部，
   * 外壳就只能展开外层 `details`，内层依旧关着——那正是要修的缺陷。
   */
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  /** 新建表单与编辑表单各自的地址错误：错误必须贴在**它对应的那个输入框**上。 */
  const [createUrlError, setCreateUrlError] = useState<string | null>(null);
  const [draftUrlError, setDraftUrlError] = useState<string | null>(null);

  /** 正在编辑的环境与它的草稿：名称、地址、状态与变量行。 */
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftName, setDraftName] = useState("");
  const [draftUrl, setDraftUrl] = useState("");
  const [draftStatus, setDraftStatus] = useState("active");
  const [draftRows, setDraftRows] = useState<VariableRow[]>([]);
  const [busy, setBusy] = useState(false);
  // 同步锁覆盖 busy 尚未渲染的同帧操作，保存返回前不能切换或取消编辑目标。
  const busyRef = useRef(false);

  // 未保存的环境编辑也是草稿：地址与变量都是别人依赖的配置，切换范围前必须问一次。
  const editingTarget = environments.find((item) => item.id === editingId) ?? null;
  const formDirty =
    (editingTarget !== null &&
      (draftName !== editingTarget.name ||
        draftUrl !== editingTarget.base_url ||
        draftStatus !== editingTarget.status ||
        edited(editingTarget, draftRows))) ||
    name.trim() !== "" ||
    baseUrl.trim() !== "";
  useLeaveReport(`environment:${workspaceId}/${projectId}`, { dirty: formDirty, busy });

  async function create() {
    if (busyRef.current) return;
    setMessage(null);
    setFailure(null);
    if (!name.trim() || !baseUrl.trim()) {
      setCreateUrlError(validateEnvironmentUrl(baseUrl));
      setFailure("请填写环境名称和地址。");
      return;
    }
    const urlIssue = validateEnvironmentUrl(baseUrl);
    setCreateUrlError(urlIssue);
    if (urlIssue !== null) {
      // 本地能看出的地址错误不必往返一次服务端；服务端仍会按同一套规则复查。
      setFailure("环境地址不合法，请按提示修正后再创建。");
      return;
    }
    busyRef.current = true;
    setBusy(true);
    try {
      await apiSend(
        projectPath(workspaceId, projectId, "/environments"),
        "POST",
        { name: name.trim(), kind: "test", base_url: baseUrl.trim(), variables: {} },
        toEnvironment,
      );
      setName("");
      setBaseUrl("");
      setMessage("环境已创建，可在用例编辑页顶部的“执行环境”中选择。");
      onChanged();
    } catch (cause) {
      if (cause instanceof ApiError && cause.code === "environment_url_invalid") {
        // 服务端是权威：前端只做形态检查，两边不可能完全一致（例如 `http://[1]/` 前端放行、
        // 服务端判定它不是可用地址）。这类错误必须落到**地址输入框**上，否则用户只看到一句
        // 泛化提示，不知道该改哪个字段。
        setCreateUrlError(cause.message);
      } else {
        setFailure(cause instanceof ApiError ? cause.message : "创建环境失败");
      }
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }

  function startEdit(environment: Environment) {
    if (busyRef.current) return;
    setMessage(null);
    setFailure(null);
    setDraftUrlError(null);
    setEditingId(environment.id);
    setDraftName(environment.name);
    setDraftUrl(environment.base_url);
    setDraftStatus(environment.status);
    setDraftRows(variableRows(environment.variables));
  }

  async function saveEdit(environment: Environment) {
    if (busyRef.current) return;
    setMessage(null);
    setFailure(null);
    if (!draftName.trim() || !draftUrl.trim()) {
      setDraftUrlError(validateEnvironmentUrl(draftUrl));
      setFailure("环境名称和地址都不能为空。");
      return;
    }
    const urlIssue = validateEnvironmentUrl(draftUrl);
    setDraftUrlError(urlIssue);
    if (urlIssue !== null) {
      // 先挡在本地：整条 PATCH 都不发，服务端不会留下“地址没改、变量改了”的半截记录。
      setFailure("环境地址不合法，请按提示修正后再保存。");
      return;
    }
    const invalid = validateRows(draftRows);
    if (invalid !== null) {
      setFailure(invalid);
      return;
    }
    busyRef.current = true;
    setBusy(true);
    try {
      await apiSend(
        projectPath(workspaceId, projectId, `/environments/${environment.id}`),
        "PATCH",
        {
          name: draftName.trim(),
          base_url: draftUrl.trim(),
          status: draftStatus,
          variables: Object.fromEntries(toPayload(draftRows).map((item) => [item.name, item.value])),
        },
        toEnvironment,
      );
      setEditingId(null);
      setMessage(`环境「${draftName.trim()}」已保存。`);
      onChanged();
    } catch (cause) {
      if (cause instanceof ApiError && cause.code === "environment_url_invalid") {
        // 保存期间地址输入与编辑目标都被锁定，服务端错误只归实际提交的那份草稿。
        setDraftUrlError(cause.message);
      } else {
        setFailure(cause instanceof ApiError ? cause.message : "保存环境失败");
      }
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }

  const content = (
    <Flex vertical gap="middle">
      {loading ? <Loading label="正在加载环境…" /> : null}
      {error ? <ErrorText message={error} /> : null}
      {environments.length === 0 && !loading ? (
        <Hint>当前项目还没有环境。执行前至少需要一个测试环境，其地址由执行池白名单放行。</Hint>
      ) : (
        <Flex className="env-list" vertical gap="small">
          {environments.map((item) => (
            <Card key={item.id} size="small">
              <Space wrap>
                <Button
                  htmlType="button"
                  type={selectedId === item.id ? "primary" : "default"}
                  onClick={() => onSelect(item.id)}
                >
                  {item.name}
                </Button>
              <Typography.Text type="secondary">
                {item.kind === "production" ? "生产" : "测试"} · {item.base_url}
                {item.status === "archived" ? " · 已停用" : ""}
                {item.pool_id ? "" : " · 未绑定执行池"}
              </Typography.Text>
              <Typography.Text type="secondary">{summarize(item.variables)}</Typography.Text>
              {canEdit ? (
                <Button htmlType="button" onClick={() => startEdit(item)} disabled={busy}>
                  编辑
                </Button>
              ) : null}
              </Space>
              {editingId === item.id ? (
                <Form className="env-edit" layout="vertical">
                  <Form.Item label="环境名称" htmlFor="env-edit-name">
                    <Input
                      id="env-edit-name"
                      value={draftName}
                      disabled={busy}
                      onChange={(event) => setDraftName(event.target.value)}
                    />
                  </Form.Item>
                  <Form.Item label="服务地址" htmlFor="env-edit-url">
                    <Input
                      id="env-edit-url"
                      value={draftUrl}
                    /* 保存期间锁定地址：不锁定的话，请求返回时输入可能已经被改成另一个值，
                       迟到的地址错误就会标在用户刚写的地址上。锁定后这份错误必然属于它校验
                       的那一份输入。同表单的变量编辑器同样在 busy 时禁用。 */
                      disabled={busy}
                      aria-invalid={draftUrlError !== null}
                      aria-describedby={draftUrlError !== null ? "env-edit-url-error" : undefined}
                      placeholder={ENVIRONMENT_URL_EXAMPLE}
                      onChange={(event) => {
                        setDraftUrl(event.target.value);
                        setDraftUrlError(null);
                      }}
                    />
                  </Form.Item>
                  {draftUrlError !== null ? (
                    <p className="error" id="env-edit-url-error">
                      {draftUrlError}
                    </p>
                  ) : (
                    <Typography.Text type="secondary">{URL_HINT}</Typography.Text>
                  )}
                  <Form.Item label="状态" htmlFor="env-edit-status">
                    <Select
                      id="env-edit-status"
                      data-selected-value={draftStatus}
                      value={draftStatus}
                      disabled={busy}
                      options={[{ value: "active", label: "启用" }, { value: "archived", label: "停用" }]}
                      onChange={setDraftStatus}
                    />
                  </Form.Item>
                  <h4>环境普通变量</h4>
                  <VariableRowsEditor
                    rows={draftRows}
                    disabled={busy}
                    onChange={setDraftRows}
                    emptyHint="该环境还没有普通变量。"
                  />
                  <Space className="actions">
                    <Button type="primary" htmlType="button" onClick={() => void saveEdit(item)} disabled={busy}>
                      {busy ? "保存中…" : "保存环境"}
                    </Button>
                    <Button
                      htmlType="button"
                      onClick={() => {
                        if (!busyRef.current) setEditingId(null);
                      }}
                      disabled={busy}
                    >
                      取消
                    </Button>
                  </Space>
                </Form>
              ) : null}
            </Card>
          ))}
        </Flex>
      )}
      {canEdit ? (
        <Card size="small" title="创建测试环境">
          <Form className="env-create" layout="vertical">
            <Form.Item label="新环境名称" htmlFor="env-name">
              <Input id="env-name" value={name} disabled={busy} onChange={(event) => setName(event.target.value)} />
            </Form.Item>
            <Form.Item label="新环境地址" htmlFor="env-url">
              <Input
                id="env-url"
                value={baseUrl}
            /* 与编辑表单同一条规则：保存期间锁定地址，迟到的地址错误只属于它校验的那一份输入。 */
                disabled={busy}
                aria-invalid={createUrlError !== null}
                aria-describedby={createUrlError !== null ? "env-url-error" : undefined}
                placeholder={ENVIRONMENT_URL_EXAMPLE}
                onChange={(event) => {
                  setBaseUrl(event.target.value);
                  setCreateUrlError(null);
                }}
              />
            </Form.Item>
          {createUrlError !== null ? (
            <p className="error" id="env-url-error">
              {createUrlError}
            </p>
          ) : (
            <Typography.Text type="secondary">{URL_HINT}</Typography.Text>
          )}
            <Form.Item>
              <Button type="primary" htmlType="button" onClick={() => void create()} disabled={busy}>
                创建测试环境
              </Button>
            </Form.Item>
            <Alert type="info" showIcon title="本阶段不启用生产环境；创建生产环境会被服务端拒绝。" />
          </Form>
        </Card>
      ) : null}
      {message ? <Hint>{message}</Hint> : null}
      {failure ? <ErrorText message={failure} /> : null}
    </Flex>
  );

  return (
    <div className="block" id="environment-panel" tabIndex={-1}>
      <Collapse
        activeKey={open ? ["environment"] : []}
        onChange={(keys) => onOpenChange(Array.isArray(keys) ? keys.includes("environment") : keys === "environment")}
        items={[{ key: "environment", label: `环境（${environments.length}）`, children: content }]}
      />
    </div>
  );
}
