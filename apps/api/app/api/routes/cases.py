"""用例草稿、发布版本、cURL 导入预览与断言试算。

草稿用修订号做乐观锁，写请求必须带 If-Match；发布产生不可变快照，草稿后续
修改不影响已发布版本。导入 cURL 只解析文本，不执行命令也不发送请求。
"""
from __future__ import annotations

import hashlib
import json
import uuid

from fastapi import APIRouter, Depends, Header, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db import get_db
from ...kernel import assertion_catalog, request_spec
from ...kernel.assertion_inputs import normalize_for_compare
from ...kernel.assertion_spec import AssertionSpecError, validate_assertions
from ...kernel.assertions import (
    AssertionConfigError,
    AssertionContext,
    AssertionPolicyError,
    evaluate,
)
from ...kernel.curl_parser import CurlParseError
from ...kernel.curl_parser import parse as parse_curl
from ...kernel.field_tree import FieldTreeError, build_field_tree
from ...kernel.lossless_json import LosslessJSONError, loads
from ...kernel.target_policy import TargetPolicyError, normalize_origin
from ...kernel.valueliteral import ValueLiteral, ValueLiteralError
from ...models import Case, CaseAssertion, CaseVersion, Folder
from ...services import asset_lifecycle
from ...services.folder_graph import blocked_folder_ids, invalid_folder_ids
from .. import deps
from ..errors import ApiError, bad_request, conflict, not_found
from ..request_contract import is_v2, require_v2_capability
from ..schemas import (
    AssertionPreviewOut,
    AssertionPreviewRequest,
    AssertionTypeOut,
    CaseCreate,
    CaseOut,
    CasePublish,
    CaseSummaryOut,
    CaseUpdate,
    CaseVersionOut,
    CurlPreviewOut,
    CurlPreviewRequest,
    FieldNodeOut,
    FieldTreeOut,
    FieldTreeRequest,
)

router = APIRouter(tags=["用例"])

_VIEW_SCOPE = Depends(deps.view_scope)
_EDIT_SCOPE = Depends(deps.edit_scope)


def _gate(session: Session, scope: deps.ProjectScope) -> deps.ProjectScope:
    try:
        return asset_lifecycle.lock_project_and_reauthorize(session, scope, "edit")
    except asset_lifecycle.AssetLifecycleError as error:
        raise ApiError(error.status_code, error.code, error.message) from error


def _ensure_source_folder_available(
    session: Session, scope: deps.ProjectScope, folder_id: uuid.UUID | None
) -> None:
    try:
        asset_lifecycle.resolve_available_folder(session, scope, folder_id)
    except asset_lifecycle.AssetLifecycleError as error:
        raise conflict("asset_unavailable", error.message) from error


def _etag(rev: int) -> str:
    return f'"{rev}"'


def _check_precondition(if_match: str | None, rev: int) -> None:
    if if_match is None:
        raise ApiError(
            428,
            "precondition_required",
            "请先读取最新用例版本再保存，避免覆盖他人的修改。",
        )
    if if_match.strip() not in ('*', _etag(rev)):
        raise conflict(
            "revision_conflict",
            "该用例已被其他修改更新，请刷新后重新编辑。",
        )


def _get_case(session: Session, scope: deps.ProjectScope, case_id: uuid.UUID) -> Case:
    case = session.scalar(select(Case).where(
        Case.workspace_id == scope.workspace_id,
        Case.id == case_id,
        Case.project_id == scope.project_id,
    ))
    if case is None:
        raise not_found("用例不存在")
    return case


def _get_case_locked(session: Session, scope: deps.ProjectScope, case_id: uuid.UUID) -> Case:
    """以行锁读取用例，供“读修订号 → 校验 → 写入”这一整段使用。

    先读后写的写路径必须锁住同一行：不锁的话两个并发请求会各自读到 rev=1，
    都通过 If-Match 校验，随后分别写 rev=2——第二个提交覆盖第一个，而它携带的
    前提条件早已失效。锁把校验和写入并进同一个临界区。
    """
    case = session.scalar(
        select(Case)
        .where(
            Case.workspace_id == scope.workspace_id,
            Case.id == case_id,
            Case.project_id == scope.project_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if case is None:
        raise not_found("用例不存在")
    return case


def _latest_version(session: Session, case_id: uuid.UUID) -> int | None:
    return session.scalar(
        select(func.max(CaseVersion.version)).where(CaseVersion.case_id == case_id)
    )


def _resolve_folder(session: Session, scope: deps.ProjectScope, folder_id: uuid.UUID) -> Folder:
    """把请求里的目录 id 解析成本项目里一个**可用**的目录。

    两种情况必须分开报：别的项目的目录按“不存在”处理，不泄露它是否存在；本项目的
    已归档目录则明确说已归档——它已经不在可选清单里，把用例放进去只会得到一条挂在
    失效目录上的用例，按目录过滤时哪一边都不出现。
    """
    folder = session.scalar(
        select(Folder).where(
            Folder.workspace_id == scope.workspace_id,
            Folder.id == folder_id,
            Folder.project_id == scope.project_id,
        )
    )
    if folder is None:
        raise not_found("目录不存在")
    if folder.archived_at is not None:
        raise bad_request("folder_archived", "该目录已归档，不能再把用例放进去。")
    blocked = blocked_folder_ids(scope)
    invalid = invalid_folder_ids(scope)
    if session.scalar(
        select(Folder.id).where(
            Folder.id == folder.id,
            (Folder.id.in_(select(blocked.c.id)) | Folder.id.in_(select(invalid.c.id))),
        )
    ):
        raise conflict("folder_unavailable", "该目录祖先不可用，请先整理目录。")
    return folder


def _case_out(session: Session, case: Case) -> CaseOut:
    return CaseOut(
        id=case.id,
        folder_id=case.folder_id,
        name=case.name,
        request=case.request,
        assertions=case.assertions,
        rev=case.rev,
        status=case.status,
        latest_version=_latest_version(session, case.id),
        updated_at=case.updated_at,
        # 与发布写入版本时用同一个函数：前端拿它和运行报告里的版本摘要比对，
        # 才能判断“这一轮结果还算不算当前配置的结果”。
        snapshot_hash=_snapshot_hash(case),
    )


# —— 用例列表与草稿 ——


@router.get(
    "/workspaces/{workspace_id}/projects/{project_id}/cases",
    response_model=list[CaseSummaryOut],
)
def list_cases(
    folder_id: uuid.UUID | None = None,
    scope: deps.ProjectScope = _VIEW_SCOPE,
    session: Session = Depends(get_db),
) -> list[CaseSummaryOut]:
    query = select(Case).where(Case.project_id == scope.project_id, Case.status == "draft")
    if folder_id is not None:
        query = query.where(Case.folder_id == folder_id)
    items = session.scalars(query.order_by(Case.name))
    versions = dict(
        session.execute(
            select(CaseVersion.case_id, func.max(CaseVersion.version)).group_by(CaseVersion.case_id)
        ).all()
    )
    return [
        CaseSummaryOut(
            id=item.id,
            folder_id=item.folder_id,
            name=item.name,
            method=str(item.request.get("method", "GET")),
            status=item.status,
            rev=item.rev,
            latest_version=versions.get(item.id),
        )
        for item in items
    ]


@router.post(
    "/workspaces/{workspace_id}/projects/{project_id}/cases",
    response_model=CaseOut,
    status_code=201,
)
def create_case(
    payload: CaseCreate,
    response: Response,
    request_contract: str | None = Header(default=None, alias="X-Request-Contract"),
    scope: deps.ProjectScope = _EDIT_SCOPE,
    session: Session = Depends(get_db),
) -> CaseOut:
    scope = _gate(session, scope)
    require_v2_capability(request_contract, needed=is_v2(payload.request))
    if payload.folder_id is not None:
        _resolve_folder(session, scope, payload.folder_id)
    try:
        spec = request_spec.validate_request(payload.request)
        assertions = validate_assertions(payload.assertions, spec)
    except (request_spec.RequestSpecError, AssertionSpecError) as error:
        raise bad_request("invalid_case", str(error)) from error

    case = Case(
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        folder_id=payload.folder_id,
        name=payload.name,
        request=spec,
        assertions=assertions,
    )
    session.add(case)
    deps.commit(session)
    response.headers["ETag"] = _etag(case.rev)
    return _case_out(session, case)


@router.get(
    "/workspaces/{workspace_id}/projects/{project_id}/cases/{case_id}", response_model=CaseOut
)
def get_case(
    case_id: uuid.UUID,
    response: Response,
    request_contract: str | None = Header(default=None, alias="X-Request-Contract"),
    scope: deps.ProjectScope = _VIEW_SCOPE,
    session: Session = Depends(get_db),
) -> CaseOut:
    case = _get_case(session, scope, case_id)
    require_v2_capability(request_contract, needed=is_v2(case.request))
    response.headers["ETag"] = _etag(case.rev)
    return _case_out(session, case)


@router.patch(
    "/workspaces/{workspace_id}/projects/{project_id}/cases/{case_id}", response_model=CaseOut
)
def update_case(
    case_id: uuid.UUID,
    payload: CaseUpdate,
    response: Response,
    if_match: str | None = Header(default=None, alias="If-Match"),
    request_contract: str | None = Header(default=None, alias="X-Request-Contract"),
    scope: deps.ProjectScope = _EDIT_SCOPE,
    session: Session = Depends(get_db),
) -> CaseOut:
    scope = _gate(session, scope)
    case = _get_case_locked(session, scope, case_id)
    if case.status == "archived":
        raise conflict("asset_unavailable", "归档用例必须恢复后才能编辑。")
    _ensure_source_folder_available(session, scope, case.folder_id)
    _check_precondition(if_match, case.rev)
    existing_v2 = is_v2(case.request)
    submitted_v2 = payload.request is not None and is_v2(payload.request)
    require_v2_capability(request_contract, needed=existing_v2 or submitted_v2)
    if existing_v2 and payload.request is not None and not submitted_v2:
        raise conflict(
            "request_contract_downgrade",
            "已升级的请求不能覆盖为 v1；请保留 schema_version: 2 及完整行字段。",
        )

    try:
        next_request = (
            request_spec.validate_request(payload.request)
            if payload.request is not None
            else request_spec.validate_request(case.request)
        )
        next_assertions = (
            validate_assertions(payload.assertions, next_request)
            if payload.assertions is not None
            else validate_assertions(case.assertions, next_request)
        )
    except request_spec.RequestSpecError as error:
        raise bad_request("invalid_case", str(error)) from error
    except AssertionSpecError as error:
        raise bad_request("invalid_assertion", str(error)) from error

    if payload.name is not None:
        case.name = payload.name
    # 目录按**字段是否出现**判断，而不是按值是否为 None：
    # `{"folder_id": null}` 是「移到未分组」这一明确意图，与「本次不改目录」（字段缺席）
    # 是两件事。只看 `is not None` 会让「改成未分组」静默无效——界面显示改好了，重新
    # 打开又回到原来的目录，而重新分组恰恰是这个功能的主要用途之一。
    if "folder_id" in payload.model_fields_set:
        if payload.folder_id is None:
            case.folder_id = None
        else:
            case.folder_id = _resolve_folder(session, scope, payload.folder_id).id
    if payload.request is not None:
        case.request = next_request
    if payload.assertions is not None:
        case.assertions = next_assertions

    case.rev += 1
    deps.commit(session)
    response.headers["ETag"] = _etag(case.rev)
    return _case_out(session, case)


@router.delete(
    "/workspaces/{workspace_id}/projects/{project_id}/cases/{case_id}", status_code=204
)
def archive_case(
    case_id: uuid.UUID,
    scope: deps.ProjectScope = _EDIT_SCOPE,
    session: Session = Depends(get_db),
) -> Response:
    raise conflict(
        "asset_operation_required",
        "归档已迁移到资产操作，请先预览并通过 asset-operations 确认。",
    )


# —— 发布与版本 ——


def _snapshot_hash(case: Case) -> str:
    payload = json.dumps(
        {"name": case.name, "request": case.request, "assertions": case.assertions},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@router.post(
    "/workspaces/{workspace_id}/projects/{project_id}/cases/{case_id}/publish",
    response_model=CaseVersionOut,
    status_code=201,
)
def publish_case(
    case_id: uuid.UUID,
    payload: CasePublish,
    request_contract: str | None = Header(default=None, alias="X-Request-Contract"),
    scope: deps.ProjectScope = _EDIT_SCOPE,
    session: Session = Depends(get_db),
) -> CaseVersionOut:
    # 发布会把草稿固化成不可变快照，执行从此固定在该快照上。因此发布必须声明
    # 它固化的是哪个草稿修订：否则“用户看到草稿 v3、点发布”与“另一个保存把草稿
    # 改成 v4”之间的窗口会让 v4 被悄悄发布出去，而页面上显示的是 v3 的内容。
    # 条件更新与快照写入在同一行锁内完成，两个窗口各有一条测试。
    scope = _gate(session, scope)
    case = _get_case_locked(session, scope, case_id)
    if case.status == "archived":
        raise conflict("asset_unavailable", "归档用例必须恢复后才能发布。")
    _ensure_source_folder_available(session, scope, case.folder_id)
    require_v2_capability(request_contract, needed=is_v2(case.request))
    if case.rev != payload.draft_rev:
        raise conflict(
            "draft_rev_conflict",
            "草稿在发布前已被修改，请刷新后确认内容再发布。",
        )
    # 发布前重新校验，避免历史草稿带着已失效的配置被固化。
    try:
        spec = request_spec.validate_request(case.request)
        assertions = validate_assertions(case.assertions, spec)
    except (request_spec.RequestSpecError, AssertionSpecError) as error:
        raise bad_request("invalid_case", str(error)) from error

    version = (_latest_version(session, case.id) or 0) + 1
    snapshot = CaseVersion(
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        case_id=case.id,
        version=version,
        request=spec,
        schema_version=2 if is_v2(spec) else 1,
        side_effect=payload.side_effect,
        snapshot_hash=_snapshot_hash(case),
        created_by=scope.principal.user_id,
    )
    session.add(snapshot)
    session.flush()
    for item in assertions:
        session.add(
            CaseAssertion(
                workspace_id=scope.workspace_id,
                project_id=scope.project_id,
                case_version_id=snapshot.id,
                assertion_id=item["id"],
                target_source=item["target_source"],
                selector=item["selector"],
                type=item["type"],
                parameters=item["parameters"],
                compare_as=item["compare_as"],
                severity=item["severity"],
                enabled=item["enabled"],
                sort_order=item["sort_order"],
            )
        )
    deps.commit(session)
    return CaseVersionOut(
        id=snapshot.id,
        case_id=snapshot.case_id,
        version=snapshot.version,
        schema_version=snapshot.schema_version,
        side_effect=snapshot.side_effect,
        snapshot_hash=snapshot.snapshot_hash,
        created_by=snapshot.created_by,
        created_at=snapshot.created_at,
    )


@router.get(
    "/workspaces/{workspace_id}/projects/{project_id}/cases/{case_id}/versions",
    response_model=list[CaseVersionOut],
)
def list_versions(
    case_id: uuid.UUID,
    scope: deps.ProjectScope = _VIEW_SCOPE,
    session: Session = Depends(get_db),
) -> list[CaseVersionOut]:
    _get_case(session, scope, case_id)
    items = session.scalars(
        select(CaseVersion).where(CaseVersion.case_id == case_id).order_by(CaseVersion.version.desc())
    )
    return [
        CaseVersionOut(
            id=item.id,
            case_id=item.case_id,
            version=item.version,
            schema_version=item.schema_version,
            side_effect=item.side_effect,
            snapshot_hash=item.snapshot_hash,
            created_by=item.created_by,
            created_at=item.created_at,
        )
        for item in items
    ]


# —— cURL 导入预览 ——


@router.post(
    "/workspaces/{workspace_id}/projects/{project_id}/imports/curl/preview",
    response_model=CurlPreviewOut,
)
def curl_preview(
    payload: CurlPreviewRequest,
    scope: deps.ProjectScope = _EDIT_SCOPE,
) -> CurlPreviewOut:
    try:
        draft = parse_curl(payload.text)
    except CurlParseError as error:
        raise bad_request("curl_parse_failed", str(error)) from error

    data = draft.to_dict()
    request = {
        "method": data["method"],
        "path": draft.path or "/",
        "query_params": data["query_params"],
        "headers": data["headers"],
        "body_type": data["body_type"],
        "body": data["body"],
    }
    if draft.host:
        try:
            request["imported_origin"] = normalize_origin(f"{draft.scheme}://{draft.host}")
        except TargetPolicyError:
            pass
    if draft.auth_hint is not None:
        # 导入时识别到认证头或 Cookie：这份请求确实需要登录态，而值已经被丢弃、不会
        # 进入请求定义。把“必须使用环境身份”写成请求自己的约束并随草稿保存，执行时
        # 才拦得住“身份后来没了就退回匿名发送”。提示（auth_hint）本身不进入请求。
        request["auth_required"] = True
    try:
        request = request_spec.validate_request(request)
    except request_spec.RequestSpecError as error:
        raise bad_request("curl_parse_failed", str(error)) from error

    return CurlPreviewOut(
        draft=request,
        sendable=draft.sendable,
        warnings=draft.warnings,
        unsupported=draft.unsupported,
        auth_hint=draft.auth_hint,
    )


# —— 断言目录与试算 ——


@router.get("/assertion-types", response_model=list[AssertionTypeOut])
def assertion_types() -> list[AssertionTypeOut]:
    assertion_catalog.assert_registry_complete()
    return [AssertionTypeOut(**item) for item in assertion_catalog.catalog()]


@router.post(
    "/workspaces/{workspace_id}/projects/{project_id}/assertion-previews",
    response_model=AssertionPreviewOut,
)
def assertion_preview(
    payload: AssertionPreviewRequest,
    scope: deps.ProjectScope = _EDIT_SCOPE,
) -> AssertionPreviewOut:
    if payload.type not in {item["id"] for item in assertion_catalog.catalog()}:
        raise bad_request("unknown_assertion_type", f"未实现的断言类型：{payload.type}")
    # 试算与保存草稿、执行必须走同一份参数校验，否则前端试算能过而保存失败，
    # 或者坏参数在求值阶段变成 500。
    try:
        assertion_catalog.validate_parameters(payload.type, payload.parameters)
    except assertion_catalog.AssertionCatalogError as error:
        raise bad_request("invalid_assertion_config", str(error)) from error

    value = None
    found = payload.found
    if payload.value is not None:
        try:
            literal = ValueLiteral.from_dict(payload.value)
        except ValueLiteralError as error:
            raise bad_request("invalid_value", str(error)) from error
        if literal.type == "json":
            try:
                value = loads(literal.text or "")
            except LosslessJSONError as error:
                raise bad_request("invalid_value", str(error)) from error
        else:
            value = literal
    elif not found:
        value = None

    # 试算必须套用与执行完全相同的取值规则：执行时先按 compare_as 把文本值
    # 转成数字再比较，试算不转就会对同一个断言给出相反结论。
    value = normalize_for_compare(value, payload.compare_as)

    try:
        outcome = evaluate(
            payload.type,
            1,
            value,
            found,
            payload.parameters,
            path=(),
            ctx=AssertionContext(),
        )
    except AssertionConfigError as error:
        raise bad_request("invalid_assertion_config", str(error)) from error
    except AssertionPolicyError as error:
        raise bad_request("assertion_policy_rejected", str(error)) from error

    return AssertionPreviewOut(
        status=outcome.status,
        reason_code=outcome.reason_code,
        message=outcome.message,
        expected=outcome.expected,
        actual=outcome.actual,
    )


@router.post(
    "/workspaces/{workspace_id}/projects/{project_id}/field-tree",
    response_model=FieldTreeOut,
)
def field_tree(
    payload: FieldTreeRequest,
    scope: deps.ProjectScope = _VIEW_SCOPE,
) -> FieldTreeOut:
    """把 JSON 原文投影为字段树，供用户在字段行旁直接选字段加断言。

    前端不自行 `JSON.parse`：那会把长整数变成相邻的浮点数，用户选中的字段值
    与执行内核看到的已经不是一回事。这里返回的每个节点都带定位路径，点选即有
    稳定 selector。
    """
    try:
        root = build_field_tree(payload.text)
    except FieldTreeError as error:
        raise bad_request("invalid_json_body", str(error)) from error
    if root is None:
        raise bad_request("empty_body", "正文为空，无法展开字段树")

    def count(node: dict) -> int:
        return 1 + sum(count(child) for child in node["children"])

    return FieldTreeOut(root=FieldNodeOut(**root), node_count=count(root))
