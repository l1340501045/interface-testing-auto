/** 项目服务目录与所选环境映射；服务身份和执行池始终继承环境。 */
import { useMemo, useRef, useState } from "react";
import { Alert, Button, Card, Collapse, Form, Input, Select, Space, Table, Typography } from "antd";

import { ApiError, apiSend, projectPath } from "../api/client";
import { toEnvironmentServiceConfig, toProjectServiceResponse, toServiceCatalog } from "../api/guards";
import type { EnvironmentServiceConfig, ProjectService, ServiceCatalog } from "../api/types";
import { ErrorText, Hint, Loading } from "../components/Feedback";
import { useLeaveReport } from "../hooks/leaveGuard";
import { useResource } from "../hooks/useResource";
import { validateEnvironmentUrl } from "./environmentUrl";

function availabilityLabel(value: EnvironmentServiceConfig["items"][number]["availability"]): string {
  return ({
    ready: "映射已配置",
    environment_archived: "环境已停用",
    service_archived: "服务已停用",
    mapping_disabled: "映射已暂停",
    mapping_missing: "尚未配置映射",
    config_inconsistent: "配置不一致",
  })[value];
}

function identityLabel(config: EnvironmentServiceConfig): string {
  const state = config.inheritance.identity.state;
  return ({ unchecked: "当前权限仅可查看继承关系", none: "未配置环境身份", ready: "环境身份已配置", unavailable: "环境身份不可用", ambiguous: "环境身份存在歧义" })[state];
}

function poolLabel(config: EnvironmentServiceConfig): string {
  const pool = config.inheritance.pool;
  if (pool.state === "missing") return "未解析到执行池";
  return `${pool.name}（${({ ready: "可用", disabled: "已停用", not_granted: "项目未获授权" })[pool.state]}）`;
}

function trimServiceName(raw: string): string { return raw.replace(/^ +| +$/g, ""); }

function serviceNameIssue(raw: string): string | null {
  const name = trimServiceName(raw);
  if (name === "") return "服务名称不能为空。";
  if ([...name].length > 200) return "服务名称最多 200 个字符。";
  return null;
}

export function ServiceConfigPanel({ workspaceId, projectId, environmentId, canEdit, onChanged, active = true, initialOpen = false }: {
  workspaceId: string;
  projectId: string;
  environmentId: string | null;
  canEdit: boolean;
  onChanged: () => void;
  active?: boolean;
  initialOpen?: boolean;
}) {
  const [open, setOpen] = useState(initialOpen);
  const catalog = useResource<ServiceCatalog>(active && open ? `${workspaceId}/${projectId}/services` : null, (signal) =>
    apiSend(projectPath(workspaceId, projectId, "/services"), "GET", undefined, toServiceCatalog, { signal }));
  const config = useResource<EnvironmentServiceConfig>(active && open && environmentId !== null ? `${workspaceId}/${projectId}/${environmentId}/service-config` : null, (signal) =>
    apiSend(projectPath(workspaceId, projectId, `/environments/${environmentId ?? ""}/service-config`), "GET", undefined, toEnvironmentServiceConfig, { signal }));

  const [newName, setNewName] = useState("");
  const [editingService, setEditingService] = useState<ProjectService | null>(null);
  const [serviceName, setServiceName] = useState("");
  const [serviceStatus, setServiceStatus] = useState<"active" | "archived">("active");
  const [editingMapping, setEditingMapping] = useState<EnvironmentServiceConfig["items"][number] | null>(null);
  const [mappingEnvironmentId, setMappingEnvironmentId] = useState<string | null>(null);
  const [mappingEnvironmentRev, setMappingEnvironmentRev] = useState<number | null>(null);
  const [mappingUrl, setMappingUrl] = useState("");
  const [mappingStatus, setMappingStatus] = useState<"active" | "disabled">("active");
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [message, setMessage] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  const dirty = newName.trim() !== ""
    || (editingService !== null && (serviceName !== editingService.name || serviceStatus !== editingService.status))
    || (editingMapping !== null && (mappingUrl !== (editingMapping.mapping?.base_url ?? "") || mappingStatus !== (editingMapping.mapping?.status === "disabled" ? "disabled" : "active")));
  useLeaveReport(`service-config:${workspaceId}/${projectId}`, { dirty, busy });

  const currentMappingRow = useMemo(() => config.data?.items.find((item) => item.service_key === editingMapping?.service_key) ?? null, [config.data, editingMapping]);
  const currentService = useMemo(() => catalog.data?.items.find((item) => item.id === editingService?.id) ?? null, [catalog.data, editingService]);
  const staleService = editingService !== null && currentService !== null && editingService.rev !== currentService.rev;
  const staleMapping = editingMapping !== null && mappingEnvironmentRev !== null && config.data !== null
    && mappingEnvironmentId === environmentId
    && (config.data.environment.rev !== mappingEnvironmentRev || currentMappingRow?.mapping?.rev !== editingMapping.mapping?.rev);

  async function reloadAll() {
    catalog.reload();
    config.reload();
    onChanged();
  }

  async function createService() {
    if (busyRef.current) return;
    const nameIssue = serviceNameIssue(newName);
    if (nameIssue) { setFailure(nameIssue); return; }
    busyRef.current = true; setBusy(true); setFailure(null); setMessage(null);
    try {
      await apiSend(projectPath(workspaceId, projectId, "/services"), "POST", { name: trimServiceName(newName) }, toProjectServiceResponse);
      setNewName(""); setMessage("服务已创建；请为需要使用它的环境分别配置地址。"); await reloadAll();
    } catch (cause) { setFailure(cause instanceof Error ? cause.message : "创建服务失败"); }
    finally { busyRef.current = false; setBusy(false); }
  }

  async function saveService() {
    if (busyRef.current || editingService === null) return;
    const nameIssue = serviceNameIssue(serviceName);
    if (nameIssue) { setFailure(nameIssue); return; }
    if (trimServiceName(serviceName) === editingService.name && (editingService.is_default || serviceStatus === editingService.status)) {
      setFailure("服务名称或状态没有变化。");
      return;
    }
    busyRef.current = true; setBusy(true); setFailure(null); setMessage(null);
    try {
      await apiSend(projectPath(workspaceId, projectId, `/services/${editingService.id}`), "PATCH", {
        ...(trimServiceName(serviceName) !== editingService.name ? { name: trimServiceName(serviceName) } : {}),
        ...(!editingService.is_default && serviceStatus !== editingService.status ? { status: serviceStatus } : {}),
      }, toProjectServiceResponse, { headers: { "If-Match": String(editingService.rev) } });
      setEditingService(null); setMessage("服务目录已保存。"); await reloadAll();
    } catch (cause) {
      setFailure(cause instanceof ApiError && cause.code.startsWith("config_revision_")
        ? `${cause.message}；当前名称和状态草稿已保留，请刷新目录后再处理。`
        : cause instanceof Error ? cause.message : "保存服务失败");
      if (cause instanceof ApiError && cause.code.startsWith("config_revision_")) catalog.reload();
    } finally { busyRef.current = false; setBusy(false); }
  }

  function beginMapping(row: EnvironmentServiceConfig["items"][number]) {
    if (busyRef.current || config.data === null) return;
    setEditingMapping(row); setMappingEnvironmentId(config.data.environment.id); setMappingEnvironmentRev(config.data.environment.rev);
    setMappingUrl(row.mapping?.base_url ?? ""); setMappingStatus(row.mapping?.status === "disabled" ? "disabled" : "active");
    setFailure(null); setMessage(null);
  }

  async function saveMapping() {
    if (busyRef.current || editingMapping === null || mappingEnvironmentRev === null || environmentId === null) return;
    if (mappingEnvironmentId !== environmentId) { setFailure("当前草稿属于另一个环境，请切回原环境后再保存或取消草稿。"); return; }
    if (editingMapping.is_default && editingMapping.mapping === null) { setFailure("默认服务配置不一致，不能从当前页面猜测修复依据。"); return; }
    if (editingMapping.service_status === "archived") { setFailure("请先恢复该服务，再新建或启用环境映射。"); return; }
    const urlIssue = validateEnvironmentUrl(mappingUrl);
    if (urlIssue !== null) { setFailure(urlIssue); return; }
    busyRef.current = true; setBusy(true); setFailure(null); setMessage(null);
    const existing = editingMapping.mapping;
    const item = editingMapping.is_default
      ? { service_key: "default", base_url: mappingUrl.trim(), expected_mapping_rev: existing?.rev }
      : {
          service_key: editingMapping.service_key,
          base_url: mappingUrl.trim(), status: mappingStatus,
          ...(existing === null ? {} : { expected_mapping_rev: existing.rev }),
        };
    try {
      await apiSend(projectPath(workspaceId, projectId, `/environments/${environmentId}/service-config`), "PATCH", { items: [item] }, toEnvironmentServiceConfig, { headers: { "If-Match": String(mappingEnvironmentRev) } });
      setEditingMapping(null); setMappingEnvironmentId(null); setMappingEnvironmentRev(null); setMessage("环境服务映射已保存。"); await reloadAll();
    } catch (cause) {
      setFailure(cause instanceof ApiError && cause.code.startsWith("config_revision_")
        ? `${cause.message}；当前地址和状态草稿已保留，请读取最新配置后再决定。`
        : cause instanceof Error ? cause.message : "保存映射失败");
      if (cause instanceof ApiError && cause.code.startsWith("config_revision_")) config.reload();
    } finally { busyRef.current = false; setBusy(false); }
  }

  return <div className="block" id="service-config-panel" tabIndex={-1}>
    <Collapse activeKey={open ? ["services"] : []} onChange={(keys) => setOpen(Array.isArray(keys) ? keys.includes("services") : keys === "services")} items={[{ key: "services", label: "服务目录与环境映射", children: <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
      <Alert type="info" showIcon title="所有服务共用所选环境的身份和执行池；服务行不单独选择账号或网络出口。" />
      {catalog.loading ? <Loading label="正在加载服务目录…" /> : null}
      {catalog.error ? <ErrorText message={catalog.error.message} /> : null}
      <Table size="small" pagination={false} scroll={{ x: 620 }} rowKey="id" dataSource={catalog.data?.items ?? []} columns={[
        { title: "服务", dataIndex: "name" }, { title: "稳定标识", dataIndex: "service_key", render: (value: string) => <code>{value}</code> },
        { title: "状态", render: (_: unknown, row: ProjectService) => row.status === "active" ? "启用" : "已停用" },
        { title: "操作", render: (_: unknown, row: ProjectService) => canEdit ? <Button htmlType="button" size="small" disabled={busy || editingService !== null} onClick={() => { setEditingService(row); setServiceName(row.name); setServiceStatus(row.status); }}>编辑</Button> : null },
      ]} />
      {editingService ? <Card size="small" title={`编辑服务：${editingService.name}`}><Form layout="vertical">
        {staleService ? <Alert type="warning" showIcon title={`服务端目录已从修订 ${editingService.rev} 更新到 ${currentService?.rev}；当前草稿仍基于旧修订。`} action={currentService ? <Button htmlType="button" onClick={() => setEditingService(currentService)}>保留输入并采用修订 {currentService.rev}</Button> : undefined} /> : null}
        <Form.Item label="服务名称"><Input aria-label="服务名称" value={serviceName} disabled={busy} onChange={(event) => setServiceName(event.target.value)} /></Form.Item>
        {editingService.is_default ? <Hint>默认服务不能停用，稳定标识不可修改。</Hint> : <Form.Item label="服务状态"><Select aria-label="服务状态" value={serviceStatus} disabled={busy} options={[{ value: "active", label: "启用" }, { value: "archived", label: "停用" }]} onChange={setServiceStatus} /></Form.Item>}
        <Space><Button type="primary" htmlType="button" loading={busy} onClick={() => void saveService()}>保存服务</Button><Button htmlType="button" disabled={busy} onClick={() => setEditingService(null)}>取消</Button></Space>
      </Form></Card> : null}
      {canEdit ? <Card size="small" title="新增命名服务"><Space><Input aria-label="新服务名称" value={newName} disabled={busy} onChange={(event) => setNewName(event.target.value)} /><Button type="primary" htmlType="button" loading={busy} onClick={() => void createService()}>创建服务</Button></Space></Card> : null}
      {environmentId === null ? <Hint>请选择上方一个环境，再配置该环境的服务地址。</Hint> : config.loading ? <Loading label="正在加载环境服务映射…" /> : config.error ? <ErrorText message={config.error.message} /> : config.data ? <>
        <Typography.Text>当前环境：{config.data.environment.name} · 身份：{identityLabel(config.data)} · 执行池：{poolLabel(config.data)}</Typography.Text>
        <Table size="small" pagination={false} scroll={{ x: 660 }} rowKey="service_key" dataSource={config.data.items} columns={[
          { title: "服务", dataIndex: "service_name" },
          { title: "环境地址", render: (_: unknown, row: EnvironmentServiceConfig["items"][number]) => row.mapping?.base_url ?? "—" },
          { title: "映射状态", render: (_: unknown, row: EnvironmentServiceConfig["items"][number]) => `${availabilityLabel(row.availability)}${row.mapping?.status === "disabled" ? "（暂停）" : ""}` },
          { title: "操作", render: (_: unknown, row: EnvironmentServiceConfig["items"][number]) => canEdit ? <Button htmlType="button" size="small" disabled={busy || editingMapping !== null || row.service_status === "archived" || row.availability === "config_inconsistent"} onClick={() => beginMapping(row)}>配置</Button> : null },
        ]} />
      </> : null}
      {editingMapping ? <Card size="small" title={`配置映射：${editingMapping.service_name}`}>
        {mappingEnvironmentId !== environmentId ? <Alert type="warning" showIcon title="当前映射草稿属于先前选择的环境，已保留但不能写入当前环境。请切回原环境或取消草稿。" /> : null}
        {staleMapping ? <Alert type="warning" showIcon title="服务端配置已更新；当前地址草稿仍基于旧修订。" action={currentMappingRow ? <Button htmlType="button" onClick={() => { setEditingMapping(currentMappingRow); setMappingEnvironmentRev(config.data?.environment.rev ?? null); }}>保留输入并采用最新修订</Button> : undefined} /> : null}
        <Form layout="vertical"><Form.Item label="环境中的服务地址"><Input aria-label="服务映射地址" value={mappingUrl} disabled={busy} onChange={(event) => setMappingUrl(event.target.value)} /></Form.Item>
          {!editingMapping.is_default ? <Form.Item label="映射状态"><Select aria-label="映射状态" value={mappingStatus} disabled={busy || editingMapping.service_status === "archived"} options={[{ value: "active", label: "启用" }, { value: "disabled", label: "暂停" }]} onChange={setMappingStatus} /></Form.Item> : <Hint>默认服务状态跟随环境，不能单独暂停。</Hint>}
          <Space><Button type="primary" htmlType="button" loading={busy} disabled={mappingEnvironmentId !== environmentId} onClick={() => void saveMapping()}>保存映射</Button><Button htmlType="button" disabled={busy} onClick={() => { setEditingMapping(null); setMappingEnvironmentId(null); setMappingEnvironmentRev(null); }}>取消</Button></Space>
        </Form>
      </Card> : null}
      {message ? <Hint>{message}</Hint> : null}{failure ? <ErrorText message={failure} /> : null}
    </Space> }]} />
  </div>;
}
