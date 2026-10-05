"""满期会话逐版结算凭证（可追溯、幂等、并发安全）。

规则（沿用会话、计量表更正与现有结算的同一套口径）：
1. 调用方提交会话 ID 与期望修订号：修订号 0 使用**最初上报记录**（
   session_records，证据永不被更正改写）；修订号 >0 使用 session_revisions
   中**对应修订号**落盘的更正台账；
2. 出单只针对**当前修订**：期望号不等于当前号（过期或超前）一律 HTTP 409
   `voucher_revision_conflict` 并附当前号；**未出单的过期修订号同样 409**，
   允许跳过未出单的中间修订（差额只相对上一张已出凭证）；
3. 针对该版完整记录，用会话原计划（dispatch + plan_snapshot 重算防篡改）
   与会话审计容差，沿用现有 execute_settlement 核算：必须 `status=settled`
   全额结算；若只能结算有效前缀（partially_settled）或首条即不可结算
   （no_valid_interval），**整次拒绝且不留下任何凭证数据**；
4. 凭证保存：凭证 ID、修订号、出单次序、结算摘要、实测总费用、出单时间，
   以及相对上一张凭证的应补 / 应退差额（首张记全额 full，之后按差额符号记
   supplement / refund / no_change，并引用上一张凭证）；
5. **同一修订号重试返回原凭证**（即使后来发生了新的更正）：幂等判定先于
   当前修订号核对，且返回该凭证首次出单时的全部冻结值；
6. 「核对当前修订号 — 取该版台账 — 全额结算 — 写入凭证」整段在同一个
   SQLite IMMEDIATE 写事务内完成，与计量表更正互斥：并发更正与出单不会
   让凭证引用过期台账；任一步失败整体回滚，不留部分数据；
7. 凭证落在 SQLite（命名卷），进程重启后按出单次序仍可查询，并能看到
   当前修订是否还有待出单凭证。

HTTP 错误码：
- session_not_found：会话不存在（404）；
- voucher_not_complete：会话未满期，不允许出单 / 查询凭证（422）；
- voucher_revision_conflict：期望修订号不是当前号（过期或超前，409），
  附 current_revision；未出单的过期修订号同样返回本错误；
- voucher_partial_settlement：该版台账只能结算有效前缀，整次拒绝（422）；
- voucher_no_valid_interval：首条记录即异常 / SOC 断点，无可结算区间（422）；
- plan_snapshot_mismatch / infeasible：沿用结算重算口径（422）。
"""

from __future__ import annotations

import json
import uuid
from typing import List, Optional

from app.models import TelemetryRecord
from app.sessions import _load_session, _status
from app.settlement import SettlementError, _round6, execute_settlement
from app.storage import SessionStore, get_store

# 复核与更正在同一处定义时间格式，保证凭证与修订时间串口径一致。
from app.revisions import _utc_now_iso


class VoucherError(Exception):
    """凭证出单 / 查询失败：code 决定错误体 error 字段，status_code 决定状态码。"""

    def __init__(
        self,
        code: str,
        reason: str,
        status_code: int = 422,
        extra: Optional[dict] = None,
    ):
        super().__init__(reason)
        self.code = code
        self.status_code = status_code
        # 附加到错误响应体顶层的结构化字段（如 current_revision）
        self.extra = extra or {}


def _ledger_for_revision(
    conn,
    store: SessionStore,
    session_id: str,
    revision_no: int,
    num_periods: int,
) -> List[TelemetryRecord]:
    """在写事务内取指定修订号的台账：0=最初上报记录，其余取对应更正台账。"""
    if revision_no == 0:
        rows = list(
            conn.execute(
                "SELECT record_json FROM session_records WHERE session_id = ?"
                " ORDER BY idx",
                (session_id,),
            ).fetchall()
        )
        if len(rows) != num_periods:
            raise VoucherError(
                "voucher_not_complete",
                f"最初上报记录数 {len(rows)} 与计划时段数 {num_periods} 不一致，"
                "会话尚未满期，不能出单",
            )
        return [TelemetryRecord.model_validate(json.loads(r["record_json"])) for r in rows]

    row = store.get_revision_row(conn, session_id, revision_no)
    if row is None:
        # 与 current 核对后不应发生（修订号只单调增加、历史行不删除），
        # 兜底按冲突处理，绝不临时拼装台账。
        raise VoucherError(
            "voucher_revision_conflict",
            f"修订号 {revision_no} 的更正台账不存在，无法按该版出单",
            409,
        )
    return [TelemetryRecord.model_validate(r) for r in json.loads(row["ledger_json"])]


def _settlement_summary(report: dict) -> dict:
    """从现有结算报告提取凭证上的结算摘要（状态 + 汇总口径）。"""
    return {
        "status": report["status"],
        "settled_periods": report["settled_periods"],
        "pending_periods": report["pending_periods"],
        "first_anomaly_index": report["first_anomaly_index"],
        "anomalous_records": report["anomalous_records"],
        "soc_breakpoint_records": report["soc_breakpoint_records"],
        **report["summary"],
    }


def _billing(actual_total: float, prev_row) -> dict:
    """首张记全额；之后每张列相对上一张已出凭证的应补 / 应退差额。"""
    if prev_row is None:
        return {
            "type": "full",
            "amount_yuan": _round6(actual_total),
            "delta_yuan": None,
            "previous_voucher_id": None,
            "previous_revision_no": None,
            "previous_sequence_no": None,
            "description": f"首张凭证记全额：实测总费用 {_round6(actual_total)} 元",
        }

    prev_payload = json.loads(prev_row["voucher_json"])
    prev_total = float(prev_payload["actual_total_cost_yuan"])
    delta = _round6(actual_total - prev_total)
    if delta > 0:
        btype = "supplement"
        desc = f"相对上一张凭证（序号 {prev_payload['sequence_no']}，修订号 " \
               f"{prev_payload['revision_no']}）应补 {delta} 元"
    elif delta < 0:
        btype = "refund"
        desc = f"相对上一张凭证（序号 {prev_payload['sequence_no']}，修订号 " \
               f"{prev_payload['revision_no']}）应退 {-delta} 元"
    else:
        btype = "no_change"
        desc = f"相对上一张凭证（序号 {prev_payload['sequence_no']}，修订号 " \
               f"{prev_payload['revision_no']}）费用持平，无补退"
    return {
        "type": btype,
        "amount_yuan": _round6(abs(delta)),
        "delta_yuan": delta,
        "previous_voucher_id": prev_payload["voucher_id"],
        "previous_revision_no": prev_payload["revision_no"],
        "previous_sequence_no": prev_payload["sequence_no"],
        "description": desc,
    }


def _voucher_payload(
    *,
    voucher_id: str,
    session_id: str,
    revision_no: int,
    sequence_no: int,
    issued_at: str,
    report: dict,
    billing: dict,
) -> dict:
    """组装入库与响应共用的完整凭证（冻结快照，重试原样返回）。"""
    actual_total = float(report["summary"]["actual_total_cost_yuan"])
    return {
        "result": "issued",
        "voucher_id": voucher_id,
        "session_id": session_id,
        "revision_no": revision_no,
        "sequence_no": sequence_no,
        "issued_at": issued_at,
        "settlement_summary": _settlement_summary(report),
        "actual_total_cost_yuan": _round6(actual_total),
        "billing": billing,
        "settlement": report,
    }


def issue_voucher(
    session_id: str,
    expected_revision: int,
    store: Optional[SessionStore] = None,
) -> dict:
    """按期望修订号出单；同一修订号重试幂等返回原凭证。

    关键顺序（整段在写事务临界区内）：
    已出凭证幂等判定 → 当前修订号核对 → 取该版台账 → 重算快照 + 全额结算
    → 计算与上一张凭证的补 / 退差额 → 同事务原子落盘。任何一步失败回滚，
    不留部分数据。
    """
    store = store or get_store()
    # 会话不存在由 _load_session 抛 session_not_found（404）
    session = _load_session(store, session_id)
    dispatch = session["dispatch"]
    plan_snapshot = session["plan_snapshot"]
    tol = session["audit_tolerance_kwh"]
    num_periods = session["num_periods"]

    with store.write_transaction() as conn:
        confirmed = store.count_records(conn, session_id)
        if confirmed < num_periods:
            raise VoucherError(
                "voucher_not_complete",
                f"会话当前仅确认 {confirmed}/{num_periods} 个时段（status="
                f"{_status(confirmed, num_periods)}），结算凭证仅对满期（complete）"
                "会话出具：未完成时段请先按批次追加，本次未出单、未写入凭证",
            )

        # 1) 幂等：同一修订号已有凭证 → 无论当前号是否已前进，都返回原凭证，
        #    且全部字段保持首次出单时的冻结值（仅 result 标为 deduped）。
        existing = store.get_voucher(conn, session_id, expected_revision)
        if existing is not None:
            payload = json.loads(existing["voucher_json"])
            payload["result"] = "deduped"
            return payload

        # 2) 只对当前修订出新凭证：过期（未出单）与超前都冲突并给出当前号
        latest = store.get_latest_revision(conn, session_id)
        current_revision = int(latest["revision_no"]) if latest is not None else 0
        if expected_revision != current_revision:
            if expected_revision < current_revision:
                detail = (
                    f"期望修订号 {expected_revision} 已过期且该修订号尚未出过凭证："
                    f"当前修订号为 {current_revision}；凭证只针对当前修订出具，"
                    "允许跳过未出单的中间修订（差额相对上一张已出凭证）"
                )
            else:
                detail = (
                    f"期望修订号 {expected_revision} 超前于当前修订号 "
                    f"{current_revision}：请先完成对应更正或改在当前修订号上出单"
                )
            raise VoucherError(
                "voucher_revision_conflict",
                f"{detail}。本次未出单、未写入凭证。",
                409,
                extra={"current_revision": current_revision},
            )

        # 3) 取该版台账（修订号 0=最初上报；其余=对应更正台账）。与上面的
        #    当前号读取在同一 IMMEDIATE 事务内，并发更正无法在期间提交，
        #    凭证不会引用过期台账。
        ledger = _ledger_for_revision(
            conn, store, session_id, expected_revision, num_periods
        )

        # 4) 沿用现有结算规则：重算防篡改 + 遥测审计 + SOC 轨迹核对。
        #    plan_snapshot_mismatch / infeasible 直接向上抛（main 已映射），
        #    异常会回滚整个事务，不留凭证。
        try:
            report = execute_settlement(
                dispatch_input=dispatch,
                plan_snapshot=plan_snapshot,
                settlement_tolerance_kwh=tol,
                records=ledger,
            )
        except SettlementError as exc:
            if exc.code == "no_valid_interval":
                raise VoucherError(
                    "voucher_no_valid_interval",
                    f"修订号 {expected_revision} 的台账首条记录即异常或 SOC 断点，"
                    f"不存在可结算区间，整次拒绝、不出凭证：{exc}",
                )
            raise

        # 5) 只能结算有效前缀 → 整次拒绝，不留凭证
        if report["status"] != "settled":
            first = report["first_anomaly_index"]
            reasons: List[str] = []
            if first is not None and first < len(report["periods"]):
                reasons = list(report["periods"][first].get("reasons", []))
            detail = "；".join(reasons) if reasons else "存在遥测审计异常或 SOC 断点"
            raise VoucherError(
                "voucher_partial_settlement",
                f"修订号 {expected_revision} 的完整台账只能结算前 "
                f"{report['settled_periods']}/{num_periods} 个有效时段（首个异常 / "
                f"SOC 断点下标 {first}），凭证要求按该版完整记录全额结算，"
                f"整次拒绝、不留下凭证：{detail}",
                extra={
                    "first_anomaly_index": first,
                    "settled_periods": report["settled_periods"],
                    "pending_periods": report["pending_periods"],
                },
            )

        # 6) 差额相对会话内上一张已出凭证（允许跳过未出单的中间修订）
        prev_row = store.get_latest_voucher(conn, session_id)
        billing = _billing(
            float(report["summary"]["actual_total_cost_yuan"]), prev_row
        )

        sequence_no = store.count_vouchers(conn, session_id) + 1
        voucher_id = str(uuid.uuid4())
        issued_at = _utc_now_iso()
        payload = _voucher_payload(
            voucher_id=voucher_id,
            session_id=session_id,
            revision_no=expected_revision,
            sequence_no=sequence_no,
            issued_at=issued_at,
            report=report,
            billing=billing,
        )

        # 7) 凭证与序号在同一事务原子落盘；插入失败（如并发抢占序号 / 修订号）
        #    整体回滚，不存在半张凭证。
        store.insert_voucher(
            conn,
            session_id,
            expected_revision,
            sequence_no,
            json.dumps(payload, ensure_ascii=False),
            issued_at,
        )
        return payload


def list_vouchers(
    session_id: str,
    store: Optional[SessionStore] = None,
) -> dict:
    """按出单次序查询凭证，并给出当前修订的待出单状态。"""
    store = store or get_store()
    session = _load_session(store, session_id)
    num_periods = session["num_periods"]

    confirmed = store.count_records_readonly(session_id)
    if confirmed < num_periods:
        raise VoucherError(
            "voucher_not_complete",
            f"会话当前仅确认 {confirmed}/{num_periods} 个时段，结算凭证查询在会话"
            "满期（complete）后才可用",
        )

    rows = store.list_voucher_rows(session_id)
    vouchers = [json.loads(row["voucher_json"]) for row in rows]

    current_revision = store.get_current_revision_no(session_id)
    current_row = next(
        (json.loads(r["voucher_json"]) for r in rows if int(r["revision_no"]) == current_revision),
        None,
    )
    current_voucher = None
    if current_row is not None:
        current_voucher = {
            "revision_no": current_row["revision_no"],
            "sequence_no": current_row["sequence_no"],
            "voucher_id": current_row["voucher_id"],
            "issued_at": current_row["issued_at"],
        }

    return {
        "session_id": session_id,
        "status": "complete",
        "num_periods": num_periods,
        "current_revision": current_revision,
        "voucher_count": len(vouchers),
        "current_revision_voucher": current_voucher,
        "current_revision_pending_issuance": current_row is None,
        "vouchers": vouchers,
    }
