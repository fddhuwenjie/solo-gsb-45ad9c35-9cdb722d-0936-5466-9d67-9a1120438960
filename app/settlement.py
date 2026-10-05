"""储能计划执行结算。

流程（只读核算，不修正任何读数）：
1. 用现有求解器对原始 `/dispatch` 输入重算，逐字段比对计划快照，确认快照未被篡改；
2. 复用遥测审计（audit_telemetry）逐条核对实测记录的电芯能量守恒、购电量与 SOC 衔接；
3. 额外核对每条记录的首/末 SOC 是否落在计划 SOC 轨迹上（结算容差内）——即「SOC 断点」；
4. 从首条异常（审计不过）或 SOC 断点起，该条及其后所有记录一律标为「待结算」；
5. 对此前连续有效区间，按实测购电量与实测吞吐量计算实际费用，并给出与计划费用的
   差额及逐时段偏差。

整体拒绝（HTTP 422）的情形：
- 计划快照与重算结果不一致（plan_snapshot_mismatch）；
- 实测记录长度与计划时段数不符（settlement_length_mismatch，请求模型层拦截）；
- 不存在任何有效区间，即首条记录就无法结算（no_valid_interval）。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from app.audit import audit_telemetry
from app.models import TelemetryRecord
from app.solver import solve_dispatch

# 快照与重算结果均按 6 位小数给出，留一位余量吸纳 JSON 往返噪声；
# 真实篡改（改 0.1kWh 动作格、改费用等）远大于该阈值。
_SNAPSHOT_EPS = 1e-7

# 计划快照周期字段中的浮点字段（index/action 另行精确比较）。
_PERIOD_FLOAT_FIELDS = (
    "action_energy_kwh",
    "charge_ac_energy_kwh",
    "discharge_ac_energy_kwh",
    "battery_power_kw",
    "grid_purchase_energy_kwh",
    "pv_curtailed_energy_kwh",
    "soc_start_kwh",
    "soc_end_kwh",
    "energy_cost_yuan",
    "degradation_cost_yuan",
)
_SUMMARY_FLOAT_FIELDS = (
    "total_energy_cost_yuan",
    "total_degradation_cost_yuan",
    "total_cost_yuan",
    "total_grid_purchase_kwh",
    "peak_grid_purchase_energy_kwh",
    "peak_grid_purchase_power_kw",
    "battery_throughput_kwh",
    "terminal_soc_kwh",
)


class SettlementError(Exception):
    """结算整体拒绝：code 决定 HTTP 错误体中的 error 字段。"""

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code


def _round6(value: float) -> float:
    """保留 6 位小数；+0.0 把 -0.0 归一为 0.0。"""
    return round(value, 6) + 0.0


def _as_float(value: object) -> Optional[float]:
    if isinstance(value, bool):  # bool 是 int 的子类，明确排除
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _verify_snapshot(recalculated: dict, snapshot: dict) -> None:
    """逐字段比对快照与求解器重算结果；任一处不一致即按篡改拒绝。"""
    snap_periods = snapshot.get("periods")
    calc_periods = recalculated["periods"]
    if not isinstance(snap_periods, list) or not isinstance(snapshot.get("summary"), dict):
        raise SettlementError(
            "plan_snapshot_mismatch",
            "计划快照结构不完整：缺少 periods 列表或 summary 对象，无法核对",
        )

    if len(snap_periods) != len(calc_periods):
        raise SettlementError(
            "plan_snapshot_mismatch",
            f"计划快照时段数 {len(snap_periods)} 与重算结果 {len(calc_periods)} 不一致，"
            "快照可能已被篡改",
        )

    for i, (snap, calc) in enumerate(zip(snap_periods, calc_periods)):
        if not isinstance(snap, dict):
            raise SettlementError(
                "plan_snapshot_mismatch", f"计划快照第 {i} 个时段不是对象，快照可能已被篡改"
            )
        index = snap.get("index")
        if not isinstance(index, int) or index != calc["index"]:
            raise SettlementError(
                "plan_snapshot_mismatch",
                f"计划快照第 {i} 个时段下标 {index!r} 与重算结果 {calc['index']} 不一致",
            )
        if snap.get("action") != calc["action"]:
            raise SettlementError(
                "plan_snapshot_mismatch",
                f"计划快照第 {i} 个时段动作 {snap.get('action')!r} 与重算结果 "
                f"{calc['action']!r} 不一致，快照可能已被篡改",
            )
        for name in _PERIOD_FLOAT_FIELDS:
            sv = _as_float(snap.get(name))
            cv = float(calc[name])
            if sv is None or abs(sv - cv) > _SNAPSHOT_EPS:
                raise SettlementError(
                    "plan_snapshot_mismatch",
                    f"计划快照第 {i} 个时段的 {name}={snap.get(name)!r} 与重算结果 "
                    f"{calc[name]} 不一致，快照可能已被篡改",
                )

    snap_summary = snapshot["summary"]
    calc_summary = recalculated["summary"]
    for name in ("num_periods",):
        sv = snap_summary.get(name)
        cv = calc_summary[name]
        if not isinstance(sv, int) or sv != cv:
            raise SettlementError(
                "plan_snapshot_mismatch",
                f"计划快照 summary.{name}={sv!r} 与重算结果 {cv} 不一致",
            )
    for name in _SUMMARY_FLOAT_FIELDS:
        sv = _as_float(snap_summary.get(name))
        cv = float(calc_summary[name])
        if sv is None or abs(sv - cv) > _SNAPSHOT_EPS:
            raise SettlementError(
                "plan_snapshot_mismatch",
                f"计划快照 summary.{name}={snap_summary.get(name)!r} 与重算结果 "
                f"{calc_summary[name]} 不一致，快照可能已被篡改",
            )
    for name in ("period_hours", "soc_resolution_kwh"):
        sv = _as_float(snap_summary.get(name))
        cv = float(calc_summary[name])
        if sv is None or abs(sv - cv) > _SNAPSHOT_EPS:
            raise SettlementError(
                "plan_snapshot_mismatch",
                f"计划快照 summary.{name}={snap_summary.get(name)!r} 与重算结果 {cv} 不一致",
            )


def execute_settlement(
    dispatch_input: dict,
    plan_snapshot: dict,
    settlement_tolerance_kwh: float,
    records: Sequence[TelemetryRecord],
) -> dict:
    """执行计划结算，返回结算报告（见模块文档字符串）。"""
    # 1. 重算并核对快照
    recalculated = solve_dispatch(
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
    _verify_snapshot(recalculated, plan_snapshot)

    n = len(records)
    planned = plan_snapshot["periods"]
    prices = dispatch_input["prices"]
    eta_c = dispatch_input["charge_efficiency"]
    eta_d = dispatch_input["discharge_efficiency"]
    degradation_cost = dispatch_input["degradation_cost_per_kwh"]
    tol = settlement_tolerance_kwh

    # 长度不符（请求模型层通常已拦截，服务层再守一道）
    if n != len(planned):
        raise SettlementError(
            "settlement_length_mismatch",
            f"实测记录长度 {n} 与计划时段数 {len(planned)} 不符，无法逐时段结算",
        )

    # 2. 复用遥测审计
    audit_report = audit_telemetry(
        charge_efficiency=eta_c,
        discharge_efficiency=eta_d,
        energy_tolerance_kwh=tol,
        records=records,
    )
    audit_by_index: Dict[int, dict] = {r["index"]: r for r in audit_report["records"]}

    # 3 + 4. 找首条「审计异常或 SOC 断点」：该条起一律待结算
    first_hold_index: Optional[int] = None
    soc_break_records: List[int] = []
    soc_gap_start: List[Optional[float]] = [None] * n
    soc_gap_end: List[Optional[float]] = [None] * n

    for i, rec in enumerate(records):
        plan = planned[i]
        gap_start = rec.soc_start_kwh - float(plan["soc_start_kwh"])
        gap_end = rec.soc_end_kwh - float(plan["soc_end_kwh"])
        soc_gap_start[i] = gap_start
        soc_gap_end[i] = gap_end

        breakpoint_reasons: List[str] = []
        if abs(gap_start) > tol:
            breakpoint_reasons.append(
                f"首态 SOC 偏离计划：实测 {rec.soc_start_kwh}kWh，计划 "
                f"{plan['soc_start_kwh']}kWh，偏差 {gap_start:+.6f}kWh 超容差 {tol}kWh"
            )
        if abs(gap_end) > tol:
            breakpoint_reasons.append(
                f"末态 SOC 偏离计划：实测 {rec.soc_end_kwh}kWh，计划 "
                f"{plan['soc_end_kwh']}kWh，偏差 {gap_end:+.6f}kWh 超容差 {tol}kWh"
            )

        audit_rec = audit_by_index[i]
        is_break = bool(breakpoint_reasons)
        if is_break:
            soc_break_records.append(i)
        if first_hold_index is None and (audit_rec["anomalous"] or is_break):
            first_hold_index = i
        # 记录断点原因（挂在审计报告之外，供该条结算明细展示）
        audit_rec["soc_breakpoint"] = is_break
        audit_rec["soc_breakpoint_reasons"] = breakpoint_reasons

    settled_count = n if first_hold_index is None else first_hold_index

    # 5. 没有任何有效区间 → 整体拒绝
    if settled_count == 0:
        first = audit_by_index[0]
        reasons = list(first["reasons"]) + list(first["soc_breakpoint_reasons"])
        detail = "；".join(reasons) if reasons else "首条记录即无法通过审计"
        raise SettlementError(
            "no_valid_interval",
            f"首条实测记录（下标 0）即异常或 SOC 断点，不存在可结算的连续有效区间：{detail}",
        )

    # 6. 对有效前缀 [0, settled_count) 逐时段结算
    period_results: List[dict] = []
    total_actual_energy_cost = 0.0
    total_actual_degradation_cost = 0.0
    total_actual_grid = 0.0
    total_actual_throughput = 0.0
    total_planned_energy_cost = 0.0
    total_planned_degradation_cost = 0.0
    total_planned_grid = 0.0
    total_planned_throughput = 0.0

    for i, rec in enumerate(records):
        plan = planned[i]
        audit_rec = audit_by_index[i]
        is_held = first_hold_index is not None and i >= first_hold_index

        entry: dict = {
            "index": i,
            "status": "pending" if is_held else "settled",
            "plan": {
                "action": plan["action"],
                "grid_purchase_energy_kwh": float(plan["grid_purchase_energy_kwh"]),
                "battery_throughput_kwh": _round6(
                    abs(float(plan["action_energy_kwh"]))
                ),
                "energy_cost_yuan": float(plan["energy_cost_yuan"]),
                "degradation_cost_yuan": float(plan["degradation_cost_yuan"]),
                "soc_start_kwh": float(plan["soc_start_kwh"]),
                "soc_end_kwh": float(plan["soc_end_kwh"]),
            },
            "soc_gap_start_kwh": _round6(soc_gap_start[i]),
            "soc_gap_end_kwh": _round6(soc_gap_end[i]),
            "audit": audit_rec,
        }

        if is_held:
            entry["reasons"] = (
                list(audit_rec["reasons"]) + list(audit_rec["soc_breakpoint_reasons"])
                if i == first_hold_index
                else [
                    f"前序第 {first_hold_index} 条记录已出现异常/SOC 断点，"
                    "后续记录冻结为待结算"
                ]
            )
            entry.update(
                actual_grid_purchase_energy_kwh=None,
                actual_battery_throughput_kwh=None,
                actual_energy_cost_yuan=None,
                actual_degradation_cost_yuan=None,
                actual_total_cost_yuan=None,
                planned_energy_cost_yuan=float(plan["energy_cost_yuan"]),
                planned_degradation_cost_yuan=float(plan["degradation_cost_yuan"]),
                planned_total_cost_yuan=_round6(
                    float(plan["energy_cost_yuan"]) + float(plan["degradation_cost_yuan"])
                ),
                energy_cost_delta_yuan=None,
                degradation_cost_delta_yuan=None,
                total_cost_delta_yuan=None,
                grid_purchase_delta_kwh=None,
                throughput_delta_kwh=None,
            )
            period_results.append(entry)
            continue

        # 实测吞吐量（电芯侧）= 充入电芯 + 电芯放出
        actual_throughput = (
            rec.charge_ac_energy_kwh * eta_c
            + rec.discharge_ac_energy_kwh / eta_d
        )
        actual_grid = rec.grid_purchase_energy_kwh
        actual_energy_cost = prices[i] * actual_grid
        actual_deg_cost = degradation_cost * actual_throughput
        actual_total_cost = actual_energy_cost + actual_deg_cost

        planned_grid = float(plan["grid_purchase_energy_kwh"])
        planned_throughput = abs(float(plan["action_energy_kwh"]))
        planned_energy_cost = float(plan["energy_cost_yuan"])
        planned_deg_cost = float(plan["degradation_cost_yuan"])
        planned_total_cost = planned_energy_cost + planned_deg_cost

        total_actual_energy_cost += actual_energy_cost
        total_actual_degradation_cost += actual_deg_cost
        total_actual_grid += actual_grid
        total_actual_throughput += actual_throughput
        total_planned_energy_cost += planned_energy_cost
        total_planned_degradation_cost += planned_deg_cost
        total_planned_grid += planned_grid
        total_planned_throughput += planned_throughput

        entry["reasons"] = []
        entry.update(
            actual_grid_purchase_energy_kwh=_round6(actual_grid),
            actual_battery_throughput_kwh=_round6(actual_throughput),
            actual_energy_cost_yuan=_round6(actual_energy_cost),
            actual_degradation_cost_yuan=_round6(actual_deg_cost),
            actual_total_cost_yuan=_round6(actual_total_cost),
            planned_energy_cost_yuan=_round6(planned_energy_cost),
            planned_degradation_cost_yuan=_round6(planned_deg_cost),
            planned_total_cost_yuan=_round6(planned_total_cost),
            energy_cost_delta_yuan=_round6(actual_energy_cost - planned_energy_cost),
            degradation_cost_delta_yuan=_round6(actual_deg_cost - planned_deg_cost),
            total_cost_delta_yuan=_round6(actual_total_cost - planned_total_cost),
            grid_purchase_delta_kwh=_round6(actual_grid - planned_grid),
            throughput_delta_kwh=_round6(actual_throughput - planned_throughput),
        )
        period_results.append(entry)

    status = "settled" if first_hold_index is None else "partially_settled"
    actual_total = total_actual_energy_cost + total_actual_degradation_cost
    planned_total = total_planned_energy_cost + total_planned_degradation_cost

    return {
        "num_records": n,
        "period_hours": recalculated["summary"]["period_hours"],
        "settlement_tolerance_kwh": tol,
        "status": status,
        "settled_periods": settled_count,
        "pending_periods": n - settled_count,
        "first_anomaly_index": first_hold_index,
        "anomalous_records": list(audit_report["anomalous_records"]),
        "soc_breakpoint_records": soc_break_records,
        "periods": period_results,
        "summary": {
            "settled_periods": settled_count,
            "actual_grid_purchase_kwh": _round6(total_actual_grid),
            "planned_grid_purchase_kwh": _round6(total_planned_grid),
            "grid_purchase_delta_kwh": _round6(total_actual_grid - total_planned_grid),
            "actual_battery_throughput_kwh": _round6(total_actual_throughput),
            "planned_battery_throughput_kwh": _round6(total_planned_throughput),
            "throughput_delta_kwh": _round6(total_actual_throughput - total_planned_throughput),
            "actual_energy_cost_yuan": _round6(total_actual_energy_cost),
            "planned_energy_cost_yuan": _round6(total_planned_energy_cost),
            "energy_cost_delta_yuan": _round6(
                total_actual_energy_cost - total_planned_energy_cost
            ),
            "actual_degradation_cost_yuan": _round6(total_actual_degradation_cost),
            "planned_degradation_cost_yuan": _round6(total_planned_degradation_cost),
            "degradation_cost_delta_yuan": _round6(
                total_actual_degradation_cost - total_planned_degradation_cost
            ),
            "actual_total_cost_yuan": _round6(actual_total),
            "planned_total_cost_yuan": _round6(planned_total),
            "total_cost_delta_yuan": _round6(actual_total - planned_total),
        },
        "audit_summary": {
            "anomalous": audit_report["anomalous"],
            "anomalous_records": list(audit_report["anomalous_records"]),
        },
    }
