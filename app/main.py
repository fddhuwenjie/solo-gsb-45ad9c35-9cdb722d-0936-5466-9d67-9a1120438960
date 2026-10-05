"""FastAPI 应用：家庭储能时段调度。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.audit import audit_telemetry
from app.models import (
    BatchAppendRequest,
    BatchAppendResponse,
    DispatchRequest,
    DispatchResponse,
    ErrorResponse,
    ReplanRequest,
    ReplanResponse,
    RevisionCreateRequest,
    RevisionDetailResponse,
    SessionCreateRequest,
    SessionCreateResponse,
    SessionDetailResponse,
    SessionListResponse,
    SettlementRequest,
    SettlementResponse,
    SettlementVoucherResponse,
    TelemetryAuditRequest,
    TelemetryAuditResponse,
    VoucherIssueRequest,
    VoucherListResponse,
)
from app.replan import ReplanError, execute_replan
from app.revisions import RevisionError, get_revision, submit_revision
from app.sessions import SessionError, append_batch, create_session, get_session, list_sessions
from app.settlement import SettlementError, execute_settlement
from app.solver import InfeasibleError, solve_dispatch
from app.vouchers import VoucherError, issue_voucher, list_vouchers

app = FastAPI(
    title="家庭储能时段调度 API",
    description=(
        "输入半小时电价/基础负荷/光伏预测与电池参数，"
        "按 0.1kWh 离散 SOC 做动态规划，求满足功率、SOC 与末端约束的最低总成本计划；"
        "另提供 /telemetry/audit 对半小时实测记录做电芯能量守恒与购电量核对"
        "（只审计标记异常，不修正读数、不生成调度计划）；"
        "/dispatch/settlement 对已下发计划做执行结算：重算校验计划快照、"
        "复用遥测审计并核对 SOC 轨迹，仅对首个异常/断点之前的连续有效区间计费；"
        "/dispatch/replan 支持计划执行中途依据连续实测前缀续排：核对原计划与前缀审计、"
        "从实测末态重排余段，并给出可直接交给 /dispatch/settlement 的余段输入与快照；"
        "/dispatch/sessions 提供可持久接续的执行会话：创建时提交原 /dispatch 输入、"
        "完整计划快照与审计容差（先重算确认快照一致），之后按时段下标成批追加半小时"
        "实测记录（沿用遥测审计、整批通过才入库、与已确认整批完全一致的重试幂等返回"
        "首收回执、其余重叠上报返回冲突），"
        "记录持久化在 SQLite 中，进程重启后仍可查询、续报、续排与结算；"
        "满期会话可经 /dispatch/sessions/{id}/revisions 提交计量表更正："
        "按期望修订号叠加「下标 + 完整替换记录」，整段台账重核通过后原子落盘，"
        "最初上报证据与批次首收回执永不改写，查询返回原始/修订台账、差异下标"
        "与可直接提交结算接口的完整请求数据；"
        "满期会话还可经 /dispatch/sessions/{id}/vouchers 按修订号出具可追溯的"
        "逐版结算凭证：修订号 0 对最初上报记录、其余版本对对应更正台账，"
        "沿用现有结算规则核算（只能结算有效前缀时整次拒绝且不留凭证），"
        "首张凭证记全额、其后每张列相对上一张的应补/应退差额（可跳过未出单"
        "中间修订），同一修订号重试原样返回冻结凭证、未出单的过期修订号返回 "
        "409 及当前号；凭证与出单次序持久化在 SQLite 中，重启后仍可查。"
    ),
    version="1.7.0",
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """把 Pydantic 校验错误整理成一条可读的中文原因。"""
    reasons = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p not in ("body",))
        msg = err.get("msg", "参数不合法")
        reasons.append(f"{loc + ': ' if loc else ''}{msg}")
    reason = "；".join(reasons) if reasons else "请求参数不合法"
    return JSONResponse(
        status_code=422,
        content={"error": "invalid_input", "reason": reason},
    )


@app.exception_handler(InfeasibleError)
async def infeasible_exception_handler(request: Request, exc: InfeasibleError):
    return JSONResponse(
        status_code=422,
        content={"error": "infeasible", "reason": str(exc)},
    )


@app.exception_handler(SettlementError)
async def settlement_error_handler(request: Request, exc: SettlementError):
    return JSONResponse(
        status_code=422,
        content={"error": exc.code, "reason": str(exc)},
    )


@app.exception_handler(ReplanError)
async def replan_error_handler(request: Request, exc: ReplanError):
    return JSONResponse(
        status_code=422,
        content={"error": exc.code, "reason": str(exc)},
    )


@app.exception_handler(SessionError)
async def session_error_handler(request: Request, exc: SessionError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "reason": str(exc)},
    )


@app.exception_handler(RevisionError)
async def revision_error_handler(request: Request, exc: RevisionError):
    body = {"error": exc.code, "reason": str(exc)}
    # 附加字段（如修订号冲突时的 current_revision）随错误体顶层返回
    body.update(exc.extra)
    return JSONResponse(
        status_code=exc.status_code,
        content=body,
    )


@app.exception_handler(VoucherError)
async def voucher_error_handler(request: Request, exc: VoucherError):
    body = {"error": exc.code, "reason": str(exc)}
    # 附加字段（如冲突时的 current_revision、前缀结算时的 first_anomaly_index）
    # 随错误体顶层返回
    body.update(exc.extra)
    return JSONResponse(
        status_code=exc.status_code,
        content=body,
    )


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    return JSONResponse(
        status_code=422,
        content={"error": "invalid_input", "reason": str(exc)},
    )


@app.get("/healthz", tags=["meta"])
async def healthz():
    return {"status": "ok"}


@app.post(
    "/dispatch",
    response_model=DispatchResponse,
    responses={422: {"model": ErrorResponse, "description": "非法输入或问题无解"}},
    tags=["dispatch"],
)
async def dispatch(req: DispatchRequest) -> DispatchResponse:
    result = solve_dispatch(
        prices=req.prices,
        base_load_kw=req.base_load_kw,
        pv_kw=req.pv_kw,
        capacity_kwh=req.capacity_kwh,
        max_charge_kw=req.max_charge_kw,
        max_discharge_kw=req.max_discharge_kw,
        charge_efficiency=req.charge_efficiency,
        discharge_efficiency=req.discharge_efficiency,
        initial_soc_kwh=req.initial_soc_kwh,
        min_terminal_soc_kwh=req.min_terminal_soc_kwh,
        degradation_cost=req.degradation_cost_per_kwh,
    )
    return DispatchResponse.model_validate(result)


@app.post(
    "/telemetry/audit",
    response_model=TelemetryAuditResponse,
    responses={422: {"model": ErrorResponse, "description": "非法输入"}},
    tags=["telemetry"],
)
async def telemetry_audit(req: TelemetryAuditRequest) -> TelemetryAuditResponse:
    """核对实测记录的能量守恒与购电量一致性；异常记录照常返回 200 与分项残差。"""
    result = audit_telemetry(
        charge_efficiency=req.charge_efficiency,
        discharge_efficiency=req.discharge_efficiency,
        energy_tolerance_kwh=req.energy_tolerance_kwh,
        records=req.records,
    )
    return TelemetryAuditResponse.model_validate(result)


@app.post(
    "/dispatch/settlement",
    response_model=SettlementResponse,
    responses={422: {"model": ErrorResponse, "description": "非法输入或结算整体拒绝"}},
    tags=["settlement"],
)
async def dispatch_settlement(req: SettlementRequest) -> SettlementResponse:
    """计划执行结算：重算校验快照 → 遥测审计 → SOC 轨迹核对 → 有效前缀计费。

    - 计划快照与重算不一致（plan_snapshot_mismatch）、记录长度不符
      （settlement_length_mismatch）、无有效区间（no_valid_interval）均整体拒绝；
    - 存在异常/SOC 断点时不报错，返回 status=partially_settled 与首个异常下标。
    """
    result = execute_settlement(
        dispatch_input=req.dispatch.model_dump(),
        plan_snapshot=req.plan_snapshot.model_dump(),
        settlement_tolerance_kwh=req.settlement_tolerance_kwh,
        records=req.records,
    )
    return SettlementResponse.model_validate(result)


@app.post(
    "/dispatch/replan",
    response_model=ReplanResponse,
    responses={422: {"model": ErrorResponse, "description": "非法输入、快照不符、前缀异常或余段无解"}},
    tags=["replan"],
)
async def dispatch_replan(req: ReplanRequest) -> ReplanResponse:
    """计划执行中途按实测状态续排。

    - 先重算并核对原计划快照，再按现有遥测审计规则核对连续实测前缀；
      首条记录首态 SOC 须在审计容差内接原初值；
    - 已执行动作即使偏离旧计划，只要实测自洽即保留原记录，从实测末态重排余段；
    - 前缀异常（prefix_audit_failed）、末态不在 0.1kWh 网格（prefix_soc_off_grid）、
      原末端 SOC 约束从实测末态已无法满足（infeasible）时整体拒绝。
    """
    result = execute_replan(
        dispatch_input=req.dispatch.model_dump(),
        plan_snapshot=req.plan_snapshot.model_dump(),
        audit_tolerance_kwh=req.audit_tolerance_kwh,
        executed_records=req.executed_records,
        remaining_prices=req.remaining_prices,
        remaining_base_load_kw=req.remaining_base_load_kw,
        remaining_pv_kw=req.remaining_pv_kw,
    )
    return ReplanResponse.model_validate(result)


@app.post(
    "/dispatch/sessions",
    response_model=SessionCreateResponse,
    responses={422: {"model": ErrorResponse, "description": "非法输入、快照不符或重算无解"}},
    tags=["sessions"],
)
async def create_dispatch_session(req: SessionCreateRequest) -> SessionCreateResponse:
    """创建可持久接续的执行会话。

    - 提交原 /dispatch 输入、完整计划快照与审计容差；服务端先重算并逐字段核对快照，
      不一致（plan_snapshot_mismatch）或重算无解（infeasible）时不创建会话；
    - 会话与后续已确认记录持久化到 SQLite，进程重启后仍在。
    """
    result = create_session(
        dispatch=req.dispatch.model_dump(),
        plan_snapshot=req.plan_snapshot.model_dump(),
        audit_tolerance_kwh=req.audit_tolerance_kwh,
    )
    return SessionCreateResponse.model_validate(result)


@app.post(
    "/dispatch/sessions/{session_id}/batches",
    response_model=BatchAppendResponse,
    responses={
        404: {"model": ErrorResponse, "description": "会话不存在"},
        409: {"model": ErrorResponse, "description": "未与已确认整批完全一致的重叠/并发上报冲突"},
        422: {"model": ErrorResponse, "description": "非法输入、下标跳跃、越界、首态不接或批内审计异常"},
    },
    tags=["sessions"],
)
async def append_session_batch(
    session_id: str, req: BatchAppendRequest
) -> BatchAppendResponse:
    """按时段下标成批追加半小时实测记录。

    - start_index 必须等于会话 next_index（首批为 0），不得跳跃、不得越出计划范围；
    - 首批首态 SOC 须在审计容差内接原初始 SOC，后续批次首态接已保存末态；
      批内沿用遥测审计逐条核对，动作偏离计划但自洽时仍接受；
    - 任一记录异常、下标跳跃或越界时整批不入库；
    - 起点、长度、内容均与某已确认整批一致的原样重试 → result=deduped，
      返回该批首次成功时冻结的回执（进度与末态不随后续批次漂移）；
      同起点短批、内容变更、跨批拼接等重叠上报 → 409 conflict，
      已保存记录与进度不变。
    """
    result = append_batch(
        session_id=session_id,
        start_index=req.start_index,
        raw_records=[rec.model_dump() for rec in req.records],
    )
    return BatchAppendResponse.model_validate(result)


@app.get(
    "/dispatch/sessions",
    response_model=SessionListResponse,
    tags=["sessions"],
)
async def list_dispatch_sessions() -> SessionListResponse:
    """列出全部执行会话概览（创建时间升序）。"""
    return SessionListResponse.model_validate(list_sessions())


@app.post(
    "/dispatch/sessions/{session_id}/revisions",
    response_model=RevisionDetailResponse,
    responses={
        404: {"model": ErrorResponse, "description": "会话不存在"},
        409: {"model": ErrorResponse, "description": "期望修订号过期（附当前修订号）"},
        422: {"model": ErrorResponse, "description": "未满期、下标重复/越界、首态断点或整段重核失败"},
    },
    tags=["sessions"],
)
async def submit_session_revision(
    session_id: str, req: RevisionCreateRequest
) -> RevisionDetailResponse:
    """满期会话的计量表更正（乐观修订号）。

    - 提交期望修订号（完成会话从 0 开始）与至少一个「时段下标 + 完整替换记录」，
      本次变更在当前版本台账上叠加（未指定时段保持原值），随后用会话的容量、
      效率、初始 SOC 与审计容差重新核对整段台账：
    - 下标重复 / 越界、任一时段能量失衡或相邻 SOC 断点均整次拒绝，已有修订不变，
      错误原因点名下标；期望修订号过期返回 409 及当前号；
    - 核对通过后原子保存新修订号、原始变更与整段修订台账；
      最初上报记录、批次边界与首收回执永不改写。
    """
    result = submit_revision(
        session_id=session_id,
        expected_revision=req.expected_revision,
        raw_changes=[
            {"index": ch.index, "record": ch.record.model_dump()} for ch in req.changes
        ],
    )
    return RevisionDetailResponse.model_validate(result)


@app.get(
    "/dispatch/sessions/{session_id}/revisions",
    response_model=RevisionDetailResponse,
    responses={404: {"model": ErrorResponse, "description": "会话不存在"}},
    tags=["sessions"],
)
async def get_session_revision(session_id: str) -> RevisionDetailResponse:
    """查询计量表修订：最初上报记录（原始证据）、当前修订记录、差异下标、
    历次更正历史，以及可直接整体 POST 给 /dispatch/settlement 的完整请求数据。
    无更正时当前修订号为 0、修订台账即最初上报记录。"""
    result = get_revision(session_id)
    return RevisionDetailResponse.model_validate(result)


@app.post(
    "/dispatch/sessions/{session_id}/vouchers",
    response_model=SettlementVoucherResponse,
    responses={
        404: {"model": ErrorResponse, "description": "会话不存在"},
        409: {"model": ErrorResponse, "description": "请求修订号未出单且不是当前修订号（附当前号）"},
        422: {"model": ErrorResponse, "description": "未满期或该版只能结算有效前缀（整次拒绝不留凭证）"},
    },
    tags=["sessions"],
)
async def issue_session_voucher(
    session_id: str, req: VoucherIssueRequest
) -> SettlementVoucherResponse:
    """为满期会话按修订号出具逐版结算凭证。

    - 修订号 0 对最初上报记录结算，n≥1 对修订号 n 的更正台账结算；
      核算完全沿用现有结算规则（原计划重算核对、遥测审计、SOC 轨迹核对、计费），
      只能结算有效前缀时整体拒绝（voucher_partial_settled）且不留下凭证；
    - 首张凭证计费栏记全额，其后每张列相对上一张凭证的应补/应退差额，
      允许跳过未出单的中间修订；
    - 同一修订号重试返回落盘时冻结的原凭证（result=existing），即使后来已有更新修订；
    - 请求修订号未出单且不等于当前修订号（漏开的过期版本或超前版本）
      → 409 voucher_revision_conflict，错误体附 current_revision；
    - 出单与计量表更正在同一 SQLite 写事务锁下串行，凭证不会引用过期台账，
      失败回滚不留下部分数据。
    """
    result = issue_voucher(
        session_id=session_id,
        expected_revision=req.expected_revision,
    )
    return SettlementVoucherResponse.model_validate(result)


@app.get(
    "/dispatch/sessions/{session_id}/vouchers",
    response_model=VoucherListResponse,
    responses={404: {"model": ErrorResponse, "description": "会话不存在"}},
    tags=["sessions"],
)
async def list_session_vouchers(session_id: str) -> VoucherListResponse:
    """按出单次序（voucher_no 升序）查询全部已出凭证，并给出当前修订待出单状态：

    - vouchers[*] 均为落盘时冻结的内容（台账、结算报告、计费栏、出单时间）；
    - pending.current_revision 为当前修订号，pending.next_revision_settled
      表示当前修订是否已有凭证（false 即当前修订尚待出单），
      pending.voucher_revisions 为已出单修订号（按出单次序，未必连续）。
    """
    result = list_vouchers(session_id)
    return VoucherListResponse.model_validate(result)


@app.get(
    "/dispatch/sessions/{session_id}",
    response_model=SessionDetailResponse,
    responses={404: {"model": ErrorResponse, "description": "会话不存在"}},
    tags=["sessions"],
)
async def get_dispatch_session(session_id: str) -> SessionDetailResponse:
    """查询会话：原计划信息（dispatch + plan_snapshot）、连续已确认记录（含逐条审计）
    与下一下标。未完成前缀可直接取 confirmed_records 用于 /dispatch/replan；
    满期（status=complete）记录可用于 /dispatch/settlement。"""
    result = get_session(session_id)
    return SessionDetailResponse.model_validate(result)
