"""可持久接续的执行会话：分批追加半小时实测记录。

规则（与现有遥测审计、结算、续排同一套口径）：
1. 创建会话时提交原 /dispatch 输入、完整计划快照与审计容差；服务端先重算
   确认快照一致（复用结算的 _verify_snapshot），不一致按 plan_snapshot_mismatch
   整体拒绝，不创建会话；
2. 之后按 start_index 成批追加记录，下标必须接在已保存末态之后（next_index），
   不得跳跃、不得越出 [0, num_periods)；
3. 每批沿用 audit_telemetry 逐条核对（残差绝对值严格大于审计容差即异常）；
   首批首态 SOC 须在容差内接原初始 SOC，后续批次首态须接已保存最后一条末态；
   批内记录动作偏离计划但自洽时仍接受（不做计划 SOC 轨迹核对）；
4. 任一条记录异常、下标跳跃或超出计划范围时整批不入库；
5. 重试判定以持久化的批次边界为准：只有起点、长度、内容三者都与某个
   已确认整批一致才算原样重试 → 返回该批首次成功时冻结的回执（仅 result
   改为 deduped，confirmed_periods/next_index/status/final_soc_kwh 等
   不随后续批次入库而漂移）；同起点短批、内容变更、跨批拼接或落在已确认
   区间内却没有匹配批次边界的上报 → 409 conflict，已保存记录与进度不变；
6. 批次边界、首次回执与逐段记录在同一 SQLite 写事务内确认，跨进程并发、
   进程重启、后续批次入库后重试都遵守同一套规则；旧库只有逐段记录、无法
   确认历史批次边界时，保留查询与从 next_index 续报能力，但不按前缀猜测
   去重（重叠上报一律 409）。

HTTP 错误码：
- session_not_found：会话不存在（404）；
- plan_snapshot_mismatch / infeasible：创建会话时快照核对或重算失败（422）；
- invalid_input：容差/记录值非法等（422）；
- batch_index_gap：start_index 大于 next_index（下标跳跃）（422）；
- batch_out_of_range：本批超出计划时段范围（422）；
- batch_boundary_gap：本批首态未在容差内接原初始 SOC / 已保存末态（422）；
- batch_audit_failed：批内存在遥测审计异常（422）；
- conflict：起点、长度、内容未与已确认整批完全一致的重叠上报（409）。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Sequence

from app.audit import audit_telemetry
from app.models import TelemetryRecord, _validate_telemetry_records
from app.settlement import _round6, _verify_snapshot
from app.solver import solve_dispatch
from app.storage import SessionStore, get_store


class SessionError(Exception):
    """会话操作失败：code 决定 HTTP 错误体 error 字段，status_code 决定状态码。"""

    def __init__(self, code: str, reason: str, status_code: int = 422):
        super().__init__(reason)
        self.code = code
        self.status_code = status_code


def _utc_now_iso() -> str:
    # 统一 UTC、秒精度、带 Z，入库与响应口径一致。
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _recalculate(dispatch_input: dict) -> dict:
    return solve_dispatch(
        prices=dispatch_input["prices"],
        base_load_kw=dispatch_input["base_load_kw"],
        pv_kw=dispatch_input["pv_kw"],
        capacity_kwh=dispatch_input["capacity_kwh"],
        max_charge_kw=dispatch_input["max_charge_kw"],
        max_discharge_kw=dispatch_input["max_discharge_kw"],
        charge_efficiency=dispatch_input["charge_efficiency"],
        discharge_efficiency=dispatch_input["discharge_efficiency"],
        initial_soc_kwh=dispatch_input["initial_soc_kwh"],
        min_terminal_soc_kwh=dispatch_input["min_terminal_soc_kwh"],
        degradation_cost=dispatch_input["degradation_cost_per_kwh"],
    )


def _status(confirmed: int, num_periods: int) -> str:
    if confirmed == 0:
        return "awaiting_records"
    if confirmed >= num_periods:
        return "complete"
    return "in_progress"


def _records_to_models(rows: Sequence) -> List[TelemetryRecord]:
    return [TelemetryRecord.model_validate(json.loads(row["record_json"])) for row in rows]


def _audit_range(
    records: List[TelemetryRecord],
    global_offset: int,
    eta_c: float,
    eta_d: float,
    tol: float,
) -> List[dict]:
    """对一段记录跑现有遥测审计，并把 index 改成原时间轴全局下标。"""
    report = audit_telemetry(
        charge_efficiency=eta_c,
        discharge_efficiency=eta_d,
        energy_tolerance_kwh=tol,
        records=records,
    )
    return [
        dict(item, index=item["index"] + global_offset) for item in report["records"]
    ]


def create_session(
    dispatch: dict,
    plan_snapshot: dict,
    audit_tolerance_kwh: float,
    store: Optional[SessionStore] = None,
) -> dict:
    """创建会话：重算核对快照，通过后持久化原输入、快照与容差。"""
    store = store or get_store()
    # 重算无解时抛出 InfeasibleError（由 main.py 映射为 infeasible）；
    # 快照不符时 _verify_snapshot 抛 SettlementError（plan_snapshot_mismatch）。
    recalculated = _recalculate(dispatch)
    _verify_snapshot(recalculated, plan_snapshot)

    num_periods = len(plan_snapshot["periods"])
    session_id = str(uuid.uuid4())
    created_at = _utc_now_iso()
    store.insert_session(
        session_id=session_id,
        dispatch_json=json.dumps(dispatch, ensure_ascii=False),
        plan_snapshot_json=json.dumps(plan_snapshot, ensure_ascii=False),
        audit_tolerance_kwh=audit_tolerance_kwh,
        num_periods=num_periods,
        created_at=created_at,
    )

    return {
        "session_id": session_id,
        "created_at": created_at,
        "audit_tolerance_kwh": audit_tolerance_kwh,
        "num_periods": num_periods,
        "confirmed_periods": 0,
        "next_index": 0,
        "status": "awaiting_records",
        "dispatch": dispatch,
        "plan_snapshot": plan_snapshot,
    }


def _load_session(store: SessionStore, session_id: str) -> dict:
    row = store.get_session_row(session_id)
    if row is None:
        raise SessionError(
            "session_not_found", f"会话 {session_id} 不存在（可能从未创建或已删除）", 404
        )
    return {
        "session_id": row["session_id"],
        "dispatch": json.loads(row["dispatch_json"]),
        "plan_snapshot": json.loads(row["plan_snapshot_json"]),
        "audit_tolerance_kwh": float(row["audit_tolerance_kwh"]),
        "num_periods": int(row["num_periods"]),
        "created_at": row["created_at"],
    }


def append_batch(
    session_id: str,
    start_index: int,
    raw_records: List[dict],
    store: Optional[SessionStore] = None,
) -> dict:
    """成批追加记录；查重、边界核对、审计、入库在同一写事务临界区内完成。

    重试语义：只有起点、长度、内容都与某个已确认整批完全一致才返回该批
    首次成功时冻结的回执（result=deduped）；同起点短批、内容变更、跨批
    拼接或旧库无法确认批次边界的重叠上报一律 409，记录与进度不变。
    """
    store = store or get_store()
    session = _load_session(store, session_id)
    dispatch = session["dispatch"]
    tol = session["audit_tolerance_kwh"]
    eta_c = dispatch["charge_efficiency"]
    eta_d = dispatch["discharge_efficiency"]
    num_periods = session["num_periods"]

    # 入事务前先把本批记录解析为模型（有限/非负/SOC 不超容量，非法即 invalid_input）
    records = [TelemetryRecord.model_validate(r) for r in raw_records]
    _validate_telemetry_records(records, dispatch["capacity_kwh"])
    batch_size = len(records)

    # 越界检查先于下标跳跃：明确告知计划范围
    if start_index >= num_periods or start_index + batch_size > num_periods:
        raise SessionError(
            "batch_out_of_range",
            f"本批下标区间 [{start_index}, {start_index + batch_size}) 超出计划范围 "
            f"[0, {num_periods})：计划共 {num_periods} 个半小时时段，整批不入库",
        )

    stored_at = _utc_now_iso()
    canonical = [json.loads(r.model_dump_json()) for r in records]

    with store.write_transaction() as conn:
        confirmed = store.count_records(conn, session_id)
        next_index = confirmed  # 记录自 0 连续保存，已确认条数即下一下标

        # 以持久化的批次边界判定重试：起点、长度、内容三者都一致才算原样重试
        batch_row = store.get_batch(conn, session_id, start_index)
        if batch_row is not None:
            saved_size = int(batch_row["batch_size"])
            same_content = False
            if saved_size == batch_size:
                saved_rows = store.get_records_from(conn, session_id, start_index)
                saved_payload = [
                    json.loads(row["record_json"]) for row in saved_rows[:batch_size]
                ]
                same_content = (
                    len(saved_payload) == batch_size and saved_payload == canonical
                )
            if saved_size == batch_size and same_content:
                # 原样重试：不重复入库，返回该批首次成功时冻结的回执，
                # 仅 result 改为 deduped，其余字段保持首收时的值
                receipt = json.loads(batch_row["receipt_json"])
                receipt["result"] = "deduped"
                return receipt
            detail = (
                f"已确认批次长度为 {saved_size} 条，本次为 {batch_size} 条，"
                "短批/长批/跨批拼接都不等同于已确认整批"
                if saved_size != batch_size
                else "同起点已确认批次的记录内容与本次上报不同"
            )
            raise SessionError(
                "conflict",
                f"下标 {start_index} 起已有确认批次（当前 next_index={next_index}）："
                f"{detail}；已保存的一份继续生效，记录与进度不变，"
                f"请以下标 {next_index} 续报",
                409,
            )

        # 无批次边界记录：start_index 必须严格等于下一下标才允许入库
        if start_index != next_index:
            if start_index < next_index:
                # 落在已确认区间内却没有匹配的批次边界（批中起点、跨批拼接，
                # 或旧库只有逐段记录）：无法确认历史批次边界，不按前缀猜测去重
                raise SessionError(
                    "conflict",
                    f"下标 {start_index} 落在已确认区间 [0, {next_index}) 内，"
                    "但没有起点相同的已确认批次可逐条核对（当前 "
                    f"next_index={next_index}）：不按前缀猜测去重，"
                    f"已保存记录与进度不变，请以下标 {next_index} 续报",
                    409,
                )
            raise SessionError(
                "batch_index_gap",
                f"起始下标 {start_index} 与已确认末态不衔接：期望 {next_index}，"
                f"下标不允许跳跃，整批不入库",
            )

        # 边界衔接：首批接原初始 SOC，后续批接已保存最后一条末态
        if next_index == 0:
            expected_start = dispatch["initial_soc_kwh"]
            anchor_desc = f"原初始 SOC {expected_start}kWh"
        else:
            prev_rows = store.get_records_from(conn, session_id, next_index - 1)
            prev_rec = TelemetryRecord.model_validate(
                json.loads(prev_rows[0]["record_json"])
            )
            expected_start = prev_rec.soc_end_kwh
            anchor_desc = f"已保存末态（下标 {next_index - 1} 的 soc_end）{expected_start}kWh"

        start_gap = records[0].soc_start_kwh - expected_start
        if abs(start_gap) > tol:
            raise SessionError(
                "batch_boundary_gap",
                f"本批首条记录首态 SOC {records[0].soc_start_kwh}kWh 与{anchor_desc} "
                f"相差 {start_gap:+.6f}kWh，超审计容差 {tol}kWh，整批不入库",
            )

        # 沿用现有遥测审计核对整批（含批内相邻记录 SOC 衔接）
        batch_audit = _audit_range(records, start_index, eta_c, eta_d, tol)
        bad = [a for a in batch_audit if a["anomalous"]]
        if bad:
            first_bad = bad[0]
            detail = "；".join(first_bad["reasons"])
            raise SessionError(
                "batch_audit_failed",
                f"本批下标 {start_index} 起的记录中，下标 {first_bad['index']} "
                f"未通过遥测审计，整批不入库：{detail}",
            )

        # 全部通过：整批记录、批次边界与首次回执在同一事务内确认，
        # 任一条写入失败即整体回滚
        for offset, payload in enumerate(canonical):
            store.insert_record(
                conn,
                session_id,
                start_index + offset,
                json.dumps(payload, ensure_ascii=False),
                stored_at,
            )
        confirmed += batch_size

        # 本批结束即最新末态：首收回执在此冻结，后续批次入库不影响它
        receipt = {
            "session_id": session_id,
            "start_index": start_index,
            "batch_size": batch_size,
            "result": "accepted",
            "confirmed_periods": confirmed,
            "next_index": confirmed,
            "status": _status(confirmed, num_periods),
            "batch_audit": batch_audit,
            "first_record_soc_gap_kwh": _round6(start_gap),
            "final_soc_kwh": _round6(records[-1].soc_end_kwh),
        }
        store.insert_batch(
            conn,
            session_id,
            start_index,
            batch_size,
            json.dumps(receipt, ensure_ascii=False),
            stored_at,
        )
        return receipt


def get_session(session_id: str, store: Optional[SessionStore] = None) -> dict:
    """查询会话：原计划信息 + 连续已确认记录（含逐条审计）+ 下一下标。"""
    store = store or get_store()
    session = _load_session(store, session_id)
    dispatch = session["dispatch"]
    tol = session["audit_tolerance_kwh"]
    num_periods = session["num_periods"]

    rows = store.get_all_records(session_id)
    records = _records_to_models(rows)
    confirmed = len(records)

    # 已确认记录自下标 0 连续保存（不变量），审计按 [0, confirmed) 整段重算
    audit_items = (
        _audit_range(records, 0, dispatch["charge_efficiency"], dispatch["discharge_efficiency"], tol)
        if records
        else []
    )

    updated_at = rows[-1]["stored_at"] if rows else None
    final_soc = records[-1].soc_end_kwh if records else None

    # 当前计量表修订号（仅用于查询展示）；更正记录在独立的 session_revisions 表，
    # 不影响 session_records / session_batches 与批次重试、首收回执。
    current_revision = store.get_current_revision_no(session_id)

    return {
        "session_id": session_id,
        "created_at": session["created_at"],
        "updated_at": updated_at,
        "audit_tolerance_kwh": tol,
        "num_periods": num_periods,
        "confirmed_periods": confirmed,
        "next_index": confirmed,
        "current_revision": current_revision,
        "status": _status(confirmed, num_periods),
        "dispatch": dispatch,
        "plan_snapshot": session["plan_snapshot"],
        "confirmed_records": [
            {"index": i, "record": records[i], "stored_at": rows[i]["stored_at"]}
            for i in range(confirmed)
        ],
        "confirmed_audit": audit_items,
        "initial_soc_kwh": dispatch["initial_soc_kwh"],
        "final_soc_kwh": None if final_soc is None else _round6(final_soc),
    }


def list_sessions(store: Optional[SessionStore] = None) -> dict:
    """会话概览列表（创建时间升序）。"""
    store = store or get_store()
    items: List[dict] = []
    for row in store.list_session_rows():
        dispatch = json.loads(row["dispatch_json"])
        confirmed = int(row["confirmed_periods"])
        num_periods = int(row["num_periods"])
        final_soc = None
        if row["last_record_json"]:
            final_soc = TelemetryRecord.model_validate(
                json.loads(row["last_record_json"])
            ).soc_end_kwh
        items.append(
            {
                "session_id": row["session_id"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "audit_tolerance_kwh": float(row["audit_tolerance_kwh"]),
                "num_periods": num_periods,
                "confirmed_periods": confirmed,
                "next_index": confirmed,
                "status": _status(confirmed, num_periods),
                "initial_soc_kwh": dispatch["initial_soc_kwh"],
                "final_soc_kwh": None if final_soc is None else _round6(final_soc),
            }
        )
    return {"sessions": items}
