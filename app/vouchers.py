"""满期执行会话的逐版结算凭证（可追溯、幂等、与并发更正互斥）。

规则（沿用现有会话、修订与结算口径）：
1. 仅 **满期（status=complete）** 会话允许出单与查询凭证；
2. 调用方提交会话 ID 与期望修订号：修订号 **0 对最初上报记录**结算，
   修订号 n≥1 对**对应修订号的更正台账**结算（修订台账在每次更正成功时
   整段冻结落盘，凭证取的就是该冻结快照）；
3. 出单核算完全沿用现有结算规则：原计划快照重算核对 → 遥测审计 →
   SOC 轨迹核对 → 计费，复用 `execute_settlement`。**只能结算有效前缀**
   （no_valid_interval / partially_settled）时整次拒绝，不留下任何凭证；
4. 凭证内容：修订号、结算摘要、实测总费用、出单时间，另冻结出单时该版
   完整台账与完整结算报告，保证后续更正不影响已出凭证的可追溯性；
5. 计费栏：本会话**首张凭证记全额**；其后每张列出**相对上一张凭证**的
   应补（supplement）/ 应退（refund）差额，允许**跳过未出单的中间修订**
   （差额总是相对最近一张已出凭证，而不是相邻修订号）；
6. **同一修订号重试原样返回已落盘凭证**（result=existing），即使后来又有
   更新的修订/凭证——冻结的台账、结算结果、计费栏与出单时间都不变；
7. **未出单的过期修订号**（请求号不是当前修订号且无凭证，含小于当前号的
   漏开版本与大于当前号的未来号）返回 **HTTP 409 冲突及当前号**，不写入；
8. 出单与更正共用同一把 SQLite 写事务锁（RLock + BEGIN IMMEDIATE）：
   「取当前修订号 → 取该版台账 → 核算 → 落盘凭证」整段在一个写事务临界区内，
   并发更正不可能插到中间使凭证引用过期台账；核算失败（含只能结算前缀）
   一律回滚，不留下部分数据；
9. 凭证落在 SQLite（命名卷），重启后按出单次序仍可查询。

HTTP 错误码：
- session_not_found：会话不存在（404）；
- voucher_not_complete：会话尚未满期，不能出单/查询凭证（422）；
- voucher_revision_conflict：请求修订号未出单且不是当前修订号（409），
  错误体附 current_revision；
- voucher_partial_settled：该版台账只能结算有效前缀（422），附
  first_anomaly_index 与 settled_periods，整次拒绝、不留凭证；
- no_valid_interval 等：沿用 /dispatch/settlement 的整体拒绝错误码（422），
  同样不留下凭证。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from app.models import TelemetryRecord
from app.sessions import _load_session, _status
from app.settlement import SettlementError, _round6, execute_settlement
from app.storage import SessionStore, get_store


class VoucherError(Exception):
    """凭证操作失败：code 决定 HTTP 错误体 error 字段，status_code 决定状态码。"""

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
        # 附加到错误响应体顶层的结构化字段（如冲突时的 current_revision）
        self.extra = extra or {}


def _utc_now_iso() -> str:
    # 与 sessions._utc_now_iso 同口径：UTC、秒精度、带 Z。
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ledger_for_revision(conn, store: SessionStore, session_id: str, revision_no: int):
    """在写事务内取指定修订号冻结的整段台账（JSON 规范化 dict 列表）。

    修订号 0 取最初上报的 session_records；n≥1 取该次更正落盘的 ledger_json。
    调用方须先确认修订号存在（≤ 当前修订号）。
    """
    if revision_no == 0:
        rows = store.get_all_records_in_txn(conn, session_id)
        return [json.loads(row["record_json"]) for row in rows]
    row = store.get_revision_row(conn, session_id, revision_no)
    if row is None:  # 不变量兜底：调用方已按当前号核对
        raise VoucherError(
            "voucher_revision_conflict",
            f"修订号 {revision_no} 的更正台账不存在，无法对其出单",
            409,
        )
    return json.loads(row["ledger_json"])


def _billing(total: float, voucher_count: int, prev_row) -> dict:
    """首张全额；其后与最近一张已出凭证比应补/应退（可跳过未出单的中间修订）。"""
    if voucher_count == 0 or prev_row is None:
        return {
            "type": "full",
            "amount_yuan": _round6(total),
            "relative_to_revision": None,
            "relative_to_voucher_no": None,
        }
    prev_total = float(prev_row["total_cost_yuan"])
    delta = _round6(total - prev_total)
    if delta > 0:
        btype = "supplement"
        amount = delta
    elif delta < 0:
        btype = "refund"
        amount = _round6(abs(delta))
    else:
        btype = "no_adjustment"
        amount = 0.0
    return {
        "type": btype,
        "amount_yuan": amount,
        "relative_to_revision": int(prev_row["revision_no"]),
        "relative_to_voucher_no": int(prev_row["voucher_no"]),
    }


def issue_voucher(
    session_id: str,
    expected_revision: int,
    store: Optional[SessionStore] = None,
) -> dict:
    """对指定修订版出结算凭证；幂等返回已落盘凭证，冲突/前缀结算均不留数据。"""
    store = store or get_store()
    # 会话不存在由 _load_session 抛 session_not_found（404）
    session = _load_session(store, session_id)
    dispatch = session["dispatch"]
    plan_snapshot = session["plan_snapshot"]
    tol = session["audit_tolerance_kwh"]
    num_periods = session["num_periods"]

    confirmed = store.count_records_readonly(session_id)
    if confirmed < num_periods:
        raise VoucherError(
            "voucher_not_complete",
            f"会话当前仅确认 {confirmed}/{num_periods} 个时段（status="
            f"{_status(confirmed, num_periods)}），结算凭证仅在会话满期（complete）后"
            "才能出具：请先按批次追加全部实测记录，本次不出单",
        )

    with store.write_transaction() as conn:
        # 1) 幂等优先：该修订号已有凭证 → 无论当前号走到哪，原样返回冻结内容
        existing = store.get_voucher(conn, session_id, expected_revision)
        if existing is not None:
            return _voucher_response(session_id, existing, result="existing")

        # 2) 取当前修订号；未出单的修订号必须正好是当前修订号，否则冲突
        latest = store.get_latest_revision(conn, session_id)
        current_revision = int(latest["revision_no"]) if latest is not None else 0
        if expected_revision != current_revision:
            if expected_revision > current_revision:
                detail = (
                    f"请求修订号 {expected_revision} 超前于服务端当前修订号 "
                    f"{current_revision}，该版本台账尚不存在"
                )
            else:
                detail = (
                    f"请求修订号 {expected_revision} 已过期且未出过凭证："
                    f"会话 {session_id} 当前修订号为 {current_revision}，"
                    "未出单的中间修订不能补开"
                )
            raise VoucherError(
                "voucher_revision_conflict",
                f"{detail}；本次未写入任何凭证，请对当前修订号 {current_revision} 出单"
                "（已出单的修订号可凭原修订号取回原凭证）",
                409,
                extra={"current_revision": current_revision},
            )

        # 3) 取该修订冻结台账（0=最初上报记录；n≥1=对应更正台账）并解析
        ledger_payload = _ledger_for_revision(conn, store, session_id, current_revision)
        records = [TelemetryRecord.model_validate(r) for r in ledger_payload]

        # 4) 沿用现有结算规则核算（快照重算 → 审计 → SOC 轨迹 → 计费）。
        #    execute_settlement 抛 SettlementError（no_valid_interval 等）时异常
        #    传出 with，事务自动回滚，不留下凭证。
        settlement = execute_settlement(
            dispatch_input=dispatch,
            plan_snapshot=plan_snapshot,
            settlement_tolerance_kwh=tol,
            records=records,
        )
        if settlement["status"] != "settled":
            # 只能结算有效前缀：整次拒绝、不留下凭证
            raise VoucherError(
                "voucher_partial_settled",
                f"修订号 {current_revision} 的台账只能结算前 "
                f"{settlement['settled_periods']} 个连续有效时段（首条异常/SOC 断点"
                f"下标 {settlement['first_anomaly_index']}），凭证要求整段全额可结算："
                "请先以计量表更正修复台账后再出单，本次不出单、不留下凭证",
                422,
                extra={
                    "first_anomaly_index": settlement["first_anomaly_index"],
                    "settled_periods": settlement["settled_periods"],
                },
            )

        total = float(settlement["summary"]["actual_total_cost_yuan"])

        # 5) 计费栏：首张全额，其后相对最近一张已出凭证补/退（允许跳号）
        voucher_count = store.count_vouchers(conn, session_id)
        prev_row = store.get_latest_voucher(conn, session_id)
        billing = _billing(total, voucher_count, prev_row)

        # 6) 与上面所有核对/核算在同一事务原子落盘
        voucher_no = voucher_count + 1
        issued_at = _utc_now_iso()
        voucher_id = str(uuid.uuid4())
        store.insert_voucher(
            conn,
            voucher_id=voucher_id,
            session_id=session_id,
            voucher_no=voucher_no,
            revision_no=current_revision,
            current_revision_at_issue=current_revision,
            ledger_json=json.dumps(ledger_payload, ensure_ascii=False),
            settlement_json=json.dumps(settlement, ensure_ascii=False),
            total_cost_yuan=total,
            billing_type=billing["type"],
            amount_yuan=billing["amount_yuan"],
            relative_to_revision=billing["relative_to_revision"],
            relative_to_voucher_no=billing["relative_to_voucher_no"],
            issued_at=issued_at,
        )

        row = store.get_voucher(conn, session_id, current_revision)
        return _voucher_response(session_id, row, result="issued")


def _voucher_response(session_id: str, row, result: str) -> dict:
    """把落盘凭证行组装成响应；内容全部取冻结值。"""
    ledger_payload = json.loads(row["ledger_json"])
    settlement = json.loads(row["settlement_json"])
    revision_no = int(row["revision_no"])
    billing = {
        "type": row["billing_type"],
        "amount_yuan": float(row["amount_yuan"]),
        "relative_to_revision": (
            None
            if row["relative_to_revision"] is None
            else int(row["relative_to_revision"])
        ),
        "relative_to_voucher_no": (
            None
            if row["relative_to_voucher_no"] is None
            else int(row["relative_to_voucher_no"])
        ),
    }
    return {
        "voucher_id": row["voucher_id"],
        "session_id": session_id,
        "voucher_no": int(row["voucher_no"]),
        "result": result,
        "revision_no": revision_no,
        "current_revision": int(row["current_revision_at_issue"]),
        "total_cost_yuan": float(row["total_cost_yuan"]),
        "settlement_status": settlement["status"],
        "settled_periods": int(settlement["settled_periods"]),
        "num_periods": int(settlement["num_records"]),
        "billing": billing,
        "issued_at": row["issued_at"],
        "records": [
            {"index": i, "record": ledger_payload[i]} for i in range(len(ledger_payload))
        ],
        "settlement": settlement,
    }


def list_vouchers(
    session_id: str,
    store: Optional[SessionStore] = None,
) -> dict:
    """按出单次序列出全部凭证，并给出当前修订待出单状态。"""
    store = store or get_store()
    session = _load_session(store, session_id)
    num_periods = session["num_periods"]

    confirmed = store.count_records_readonly(session_id)
    if confirmed < num_periods:
        raise VoucherError(
            "voucher_not_complete",
            f"会话当前仅确认 {confirmed}/{num_periods} 个时段，结算凭证在会话满期"
            "（complete）后才会产生",
        )

    current_revision = store.get_current_revision_no(session_id)
    rows = store.list_voucher_rows(session_id)
    vouchers: List[dict] = [
        _voucher_response(session_id, row, result="issued") for row in rows
    ]
    issued_revisions = [int(row["revision_no"]) for row in rows]

    return {
        "session_id": session_id,
        "num_periods": num_periods,
        "status": "complete",
        "current_revision": current_revision,
        "pending": {
            "current_revision": current_revision,
            "voucher_revisions": issued_revisions,
            "next_revision_settled": current_revision in issued_revisions,
        },
        "vouchers": vouchers,
    }
