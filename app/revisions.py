"""计量表更正：保留最初上报证据，叠加提供修订后的实测台账。

规则（与会话、遥测审计、结算同一套口径）：
1. 仅 **满期（status=complete）** 的已完成会话允许更正；最初分批上报的
   session_records（证据）与 session_batches（批次边界、首次回执）永不被
   更正改写——原会话查询、批次重试判定与首收回执继续使用最初上报记录；
2. 通过 HTTP 按会话提交「期望修订号」+ 至少一个「时段下标 + 完整替换记录」：
   完成会话从修订号 0 开始；服务端把本次变更在**当前版本台账**上叠加，
   未指定的时段保持原值，随后对**整段**修订记录重新核对；
3. 用会话的容量、充/放效率、初始 SOC 与审计容差重核整段台账：首条首态
   SOC 须在容差内接原初始 SOC，逐段核对电芯能量守恒、购电量平衡与相邻
   记录 SOC 衔接（沿用 audit_telemetry）；下标重复 / 越界、任一时段能量
   失衡或相邻 SOC 断点均**整次拒绝**，已有修订不变，错误原因点名下标；
4. 期望修订号与服务端当前号不符（过期）→ HTTP 409 冲突，响应给出当前号；
5. 核对通过后在同一 SQLite 写事务内**原子保存**新修订号、本次原始变更与
   叠加后的整段修订台账（修订号 +1）；
6. 查询返回最初上报记录与当前修订记录、差异下标、历次更正历史，以及可
   直接整体提交给现有 /dispatch/settlement 的完整请求数据；
7. 修订落在 SQLite（命名卷），重启后仍可查询并在当前版本上继续更正。

HTTP 错误码：
- session_not_found：会话不存在（404）；
- revision_conflict：期望修订号过期（409），响应附 current_revision；
- revision_not_complete：会话尚未满期，不能更正（422）；
- revision_invalid_index：下标越界（422）；
- revision_duplicate_index：同一次提交内下标重复（422）；
- revision_anchor_gap：修订台账首条首态未在容差内接原初始 SOC（422）；
- revision_audit_failed：整段重核出现能量失衡 / 购电失衡 / SOC 断点（422）；
- invalid_input：替换记录测量值非法（负 / 非有限 / SOC 超容量）等（422）。
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import List, Optional, Sequence, Set

from app.models import TelemetryRecord
from app.sessions import _audit_range, _load_session, _status
from app.settlement import _round6
from app.storage import SessionStore, get_store


class RevisionError(Exception):
    """计量表更正失败：code 决定 HTTP 错误体 error 字段，status_code 决定状态码。"""

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
        # 附加到错误响应体顶层的结构化字段（如修订号冲突时的 current_revision）
        self.extra = extra or {}


def _utc_now_iso() -> str:
    # 与 sessions._utc_now_iso 同口径：UTC、秒精度、带 Z。
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical(rec: TelemetryRecord) -> dict:
    """转成 JSON 往返后的规范化 dict，供台账叠加与逐字段差异比较。"""
    return json.loads(rec.model_dump_json())


def _validate_change_record(rec: TelemetryRecord, capacity_kwh: float, index: int) -> None:
    """替换记录的有限/非负/SOC 容量校验，错误原因点名时段下标。"""
    for name, label in (
        ("base_load_kw", "基础负荷"),
        ("pv_kw", "光伏功率"),
        ("charge_ac_energy_kwh", "交流充电量"),
        ("discharge_ac_energy_kwh", "交流放电量"),
        ("grid_purchase_energy_kwh", "电网购电量"),
        ("soc_start_kwh", "首态 SOC"),
        ("soc_end_kwh", "末态 SOC"),
    ):
        v = getattr(rec, name)
        if not math.isfinite(v):
            raise RevisionError(
                "invalid_input",
                f"下标 {index} 的替换记录的{label}不是有限数值：{v}，整次更正拒绝，"
                "已有修订不变",
            )
        if v < 0:
            raise RevisionError(
                "invalid_input",
                f"下标 {index} 的替换记录的{label}不允许为负值：{v}，整次更正拒绝，"
                "已有修订不变",
            )
    if rec.soc_start_kwh > capacity_kwh or rec.soc_end_kwh > capacity_kwh:
        raise RevisionError(
            "invalid_input",
            f"下标 {index} 的替换记录 SOC（首态 {rec.soc_start_kwh}kWh / "
            f"末态 {rec.soc_end_kwh}kWh）超过电池容量 {capacity_kwh}kWh，"
            "整次更正拒绝，已有修订不变",
        )


def submit_revision(
    session_id: str,
    expected_revision: int,
    raw_changes: Sequence[dict],
    store: Optional[SessionStore] = None,
) -> dict:
    """提交一次计量表更正：核对修订号 → 在当前版本上叠加 → 整段重核 → 原子落盘。"""
    store = store or get_store()
    # 会话不存在由 _load_session 抛 session_not_found（404）
    session = _load_session(store, session_id)
    dispatch = session["dispatch"]
    tol = session["audit_tolerance_kwh"]
    eta_c = dispatch["charge_efficiency"]
    eta_d = dispatch["discharge_efficiency"]
    capacity = dispatch["capacity_kwh"]
    num_periods = session["num_periods"]

    # 仅满期会话允许更正：整段记录齐全后才能「重新核对整段修订记录」
    confirmed = store.count_records_readonly(session_id)
    if confirmed < num_periods:
        raise RevisionError(
            "revision_not_complete",
            f"会话当前仅确认 {confirmed}/{num_periods} 个时段（status="
            f"{_status(confirmed, num_periods)}），计量表更正在会话满期（complete）后才允许："
            "未完成时段请先按批次追加，整次更正拒绝，已有修订不变",
        )

    # 解析本次提交的完整替换记录（不做字段级 patch：缺字段按模型缺省值处理，
    # 因此调用方必须给出完整记录）
    changes: List[dict] = []
    for item in raw_changes:
        idx = item["index"]
        rec = TelemetryRecord.model_validate(item["record"])
        _validate_change_record(rec, capacity, idx)
        changes.append({"index": idx, "record": rec})

    # 下标越界：整次拒绝并点名下标
    for ch in changes:
        if ch["index"] < 0 or ch["index"] >= num_periods:
            raise RevisionError(
                "revision_invalid_index",
                f"下标 {ch['index']} 越界：合法范围为 [0, {num_periods})，"
                "整次更正拒绝，已有修订不变",
            )

    # 同一次提交内下标重复：整次拒绝并点名下标
    seen: Set[int] = set()
    for ch in changes:
        if ch["index"] in seen:
            raise RevisionError(
                "revision_duplicate_index",
                f"本次提交中时段下标 {ch['index']} 重复出现：每个下标至多一条完整替换记录，"
                "整次更正拒绝，已有修订不变",
            )
        seen.add(ch["index"])
    created_at = _utc_now_iso()
    changes_payload = [
        {"index": ch["index"], "record": _canonical(ch["record"])} for ch in changes
    ]

    with store.write_transaction() as conn:
        # 乐观并发：以持久化的最新修订号为准，过期整次拒绝并给出当前号
        latest = store.get_latest_revision(conn, session_id)
        current_revision = int(latest["revision_no"]) if latest is not None else 0
        if expected_revision != current_revision:
            raise RevisionError(
                "revision_conflict",
                f"期望修订号 {expected_revision} 已过期：会话 {session_id} 当前修订号为 "
                f"{current_revision}；请先查询最新修订台账，在修订号 {current_revision} "
                "上重新叠加更正后提交。本次更正未写入，已有修订不变",
                409,
                extra={"current_revision": current_revision},
            )

        # 基线台账：修订号 0 为最初上报记录；之后在最新整段台账上叠加
        if latest is not None:
            ledger = [TelemetryRecord.model_validate(r) for r in json.loads(latest["ledger_json"])]
        else:
            original_rows = store.get_all_records(session_id)
            ledger = [
                TelemetryRecord.model_validate(json.loads(row["record_json"]))
                for row in sorted(original_rows, key=lambda r: r["idx"])
            ]
        if len(ledger) != num_periods:  # 不变量兜底：满期检查后应恒等
            raise RevisionError(
                "revision_not_complete",
                f"已保存记录数 {len(ledger)} 与计划时段数 {num_periods} 不一致，拒绝更正",
            )

        # 在当前版本上叠加替换；未指定时段保持原值
        for ch in changes:
            ledger[ch["index"]] = ch["record"]

        # 整段重核：首条首态 SOC 在容差内接原初始 SOC（修订可能改到第 0 条）
        anchor_gap = ledger[0].soc_start_kwh - dispatch["initial_soc_kwh"]
        if abs(anchor_gap) > tol:
            raise RevisionError(
                "revision_anchor_gap",
                f"修订台账下标 0 的首态 SOC {ledger[0].soc_start_kwh}kWh 与原初始 SOC "
                f"{dispatch['initial_soc_kwh']}kWh 相差 {anchor_gap:+.6f}kWh，"
                f"超审计容差 {tol}kWh，整次更正拒绝，已有修订不变",
            )

        # 整段遥测审计：任一时段能量失衡 / 购电失衡 / 相邻 SOC 断点都整次拒绝
        audit_items = _audit_range(ledger, 0, eta_c, eta_d, tol)
        bad = [a for a in audit_items if a["anomalous"]]
        if bad:
            first_bad = bad[0]
            detail = "；".join(first_bad["reasons"])
            raise RevisionError(
                "revision_audit_failed",
                f"修订后台账下标 {first_bad['index']} 未通过遥测审计，整次更正拒绝，"
                f"已有修订不变：{detail}",
            )

        # 核对通过：新修订号、原始变更与整段修订台账在同一事务原子落盘
        new_revision_no = current_revision + 1
        ledger_payload = [_canonical(r) for r in ledger]
        store.insert_revision(
            conn,
            session_id,
            new_revision_no,
            json.dumps(changes_payload, ensure_ascii=False),
            json.dumps(ledger_payload, ensure_ascii=False),
            created_at,
        )

    return get_revision(session_id, store=store)


def get_revision(session_id: str, store: Optional[SessionStore] = None) -> dict:
    """查询计量表修订：原始台账、当前修订台账、差异下标、历史与结算请求数据。"""
    store = store or get_store()
    session = _load_session(store, session_id)
    dispatch = session["dispatch"]
    plan_snapshot = session["plan_snapshot"]
    tol = session["audit_tolerance_kwh"]
    eta_c = dispatch["charge_efficiency"]
    eta_d = dispatch["discharge_efficiency"]
    num_periods = session["num_periods"]

    original_rows = store.get_all_records(session_id)
    confirmed = len(original_rows)
    if confirmed < num_periods:
        raise RevisionError(
            "revision_not_complete",
            f"会话当前仅确认 {confirmed}/{num_periods} 个时段，计量表修订查询在会话满期"
            "（complete）后才可用",
        )

    original_rows = sorted(original_rows, key=lambda r: r["idx"])
    original_payload = [json.loads(row["record_json"]) for row in original_rows]
    original_models = [TelemetryRecord.model_validate(p) for p in original_payload]

    revision_rows = store.list_revision_rows(session_id)
    history: List[dict] = []
    for row in revision_rows:
        changes = json.loads(row["changes_json"])
        history.append(
            {
                "revision_no": int(row["revision_no"]),
                "changes": changes,
                "changed_indices": sorted({int(c["index"]) for c in changes}),
                "created_at": row["created_at"],
            }
        )

    # 当前修订台账：无修订（修订号 0）即最初上报记录
    if revision_rows:
        latest = revision_rows[-1]
        current_revision = int(latest["revision_no"])
        revised_payload = json.loads(latest["ledger_json"])
    else:
        current_revision = 0
        revised_payload = original_payload
    revised_models = [TelemetryRecord.model_validate(p) for p in revised_payload]

    # 差异下标：当前台账与最初上报记录逐字段比较（JSON 规范化口径）
    revised_canonical = [_canonical(r) for r in revised_models]
    changed_indices = [
        i for i in range(num_periods) if revised_canonical[i] != original_payload[i]
    ]

    revised_audit = _audit_range(revised_models, 0, eta_c, eta_d, tol)

    return {
        "session_id": session_id,
        "num_periods": num_periods,
        "confirmed_periods": confirmed,
        "status": "complete",
        "current_revision": current_revision,
        "audit_tolerance_kwh": tol,
        "original_records": [
            {
                "index": i,
                "record": original_models[i],
                "stored_at": original_rows[i]["stored_at"],
            }
            for i in range(num_periods)
        ],
        "revised_records": [
            {"index": i, "record": revised_models[i]} for i in range(num_periods)
        ],
        "changed_indices": changed_indices,
        "revisions": history,
        "revised_audit": revised_audit,
        "initial_soc_kwh": dispatch["initial_soc_kwh"],
        "final_soc_kwh": _round6(revised_models[-1].soc_end_kwh),
        "settlement_request": {
            "dispatch": dispatch,
            "plan_snapshot": plan_snapshot,
            "settlement_tolerance_kwh": tol,
            "records": revised_models,
        },
    }
