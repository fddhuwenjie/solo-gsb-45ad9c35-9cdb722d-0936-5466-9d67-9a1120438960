"""计划执行中途的实测状态续排。

流程（只读核算：不修正任何读数、不改写已执行记录）：
1. 用现有求解器对原始 `/dispatch` 输入重算，逐字段比对计划快照（复用结算的
   `_verify_snapshot`），确认快照未被篡改；
2. 按现有遥测审计规则（audit_telemetry）核对连续实测前缀：电芯能量守恒、购电量
   功率平衡、相邻记录 SOC 衔接；并额外要求首条记录的首态 SOC 在审计容差内接原初值；
   已执行动作即使偏离旧计划，只要实测自洽即保留原记录（不做 SOC 断点判定）；
3. 要求前缀末态 SOC 精确落在 0.1kWh 网格上（离散求解器的初始状态必须是整格）；
4. 检查原末端 SOC 约束从实测末态起仍可满足——对余段新预测重算，无解（infeasible）
   即整体拒绝；
5. 从实测末态起安排余段，返回原时间轴上已执行/待执行明细、实测前缀费用（按现有
   结算口径）加余段预测费用，以及可直接交给 /dispatch/settlement 的余段调度输入与
   计划快照（余段首态 SOC 与前缀末态衔接）。

整体拒绝（HTTP 422）的情形：
- plan_snapshot_mismatch：快照与重算结果不一致（重算无解为 infeasible）；
- invalid_input：长度不符、容差/测量值非法、已执行段数不在 [1, n) 等（请求模型层拦截）；
- prefix_audit_failed：首条 SOC 未在容差内接原初值，或前缀中存在遥测审计异常；
- prefix_soc_off_grid：前缀末态 SOC 不落在 0.1kWh 网格上；
- infeasible：从实测末态出发，原末端 SOC 约束在余段新预测下已无法满足。
"""

from __future__ import annotations

from typing import Dict, List, Sequence

from app.audit import audit_telemetry
from app.models import TelemetryRecord
from app.settlement import _round6, _verify_snapshot
from app.solver import (
    PERIOD_HOURS,
    SOC_STEP_KWH,
    _snap_steps,
    solve_dispatch,
)


class ReplanError(Exception):
    """续排整体拒绝：code 决定 HTTP 错误体中的 error 字段。"""

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code


def execute_replan(
    dispatch_input: dict,
    plan_snapshot: dict,
    audit_tolerance_kwh: float,
    executed_records: Sequence[TelemetryRecord],
    remaining_prices: Sequence[float],
    remaining_base_load_kw: Sequence[float],
    remaining_pv_kw: Sequence[float],
) -> dict:
    """执行中途续排，返回原时间轴明细、费用汇总与余段结算入参（见模块文档字符串）。"""
    # 1. 重算并核对原计划快照（长度不符/篡改照结算口径拒绝）
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

    n = len(plan_snapshot["periods"])
    k = len(executed_records)
    if k < 1 or k >= n or n - k != len(remaining_prices):
        # 请求模型层通常已拦截，服务层再守一道
        raise ReplanError(
            "invalid_input",
            f"已执行记录 {k} 段、余段预测 {len(remaining_prices)} 段，"
            f"与原计划 {n} 段对不上：前缀须至少一段且留有待执行时段",
        )

    prices = dispatch_input["prices"]
    eta_c = dispatch_input["charge_efficiency"]
    eta_d = dispatch_input["discharge_efficiency"]
    degradation_cost = dispatch_input["degradation_cost_per_kwh"]
    tol = audit_tolerance_kwh

    # 2a. 首条 SOC 须在容差内接原初值（telemetry 审计不感知原计划，需在续排侧补一道）
    initial_gap = executed_records[0].soc_start_kwh - dispatch_input["initial_soc_kwh"]
    if abs(initial_gap) > tol:
        raise ReplanError(
            "prefix_audit_failed",
            f"首条实测记录首态 SOC {executed_records[0].soc_start_kwh}kWh 与原初始 SOC "
            f"{dispatch_input['initial_soc_kwh']}kWh 相差 {initial_gap:+.6f}kWh，"
            f"超审计容差 {tol}kWh，前缀不是原计划时间轴上的连续执行段",
        )

    # 2b. 复用现有遥测审计核对整条前缀
    audit_report = audit_telemetry(
        charge_efficiency=eta_c,
        discharge_efficiency=eta_d,
        energy_tolerance_kwh=tol,
        records=executed_records,
    )
    if audit_report["anomalous"]:
        first_bad = audit_report["anomalous_records"][0]
        detail = "；".join(audit_report["records"][first_bad]["reasons"])
        raise ReplanError(
            "prefix_audit_failed",
            f"连续实测前缀第 {first_bad} 条记录未通过遥测审计，整体拒绝续排：{detail}",
        )
    audit_by_index: Dict[int, dict] = {r["index"]: r for r in audit_report["records"]}

    # 3. 前缀末态须落在 0.1kWh 网格（余段 DP 的初始状态必须是整格）
    final_soc = executed_records[-1].soc_end_kwh
    try:
        final_soc = _snap_steps(final_soc, "前缀末态 SOC") * SOC_STEP_KWH
    except ValueError as exc:
        raise ReplanError("prefix_soc_off_grid", str(exc)) from exc
    if final_soc > dispatch_input["capacity_kwh"]:
        # 请求模型层已按容量拦过记录，此处再守一道
        raise ReplanError(
            "prefix_soc_off_grid",
            f"前缀末态 SOC {final_soc}kWh 超过电池容量 {dispatch_input['capacity_kwh']}kWh",
        )

    # 4 + 5. 从实测末态起对余段新预测重排；原末端 SOC 约束无法满足时 solve_dispatch 抛
    # InfeasibleError，由 main.py 映射为 error=infeasible。
    remaining_dispatch = dict(dispatch_input)
    remaining_dispatch["initial_soc_kwh"] = _round6(final_soc)
    remaining_dispatch["prices"] = list(remaining_prices)
    remaining_dispatch["base_load_kw"] = list(remaining_base_load_kw)
    remaining_dispatch["pv_kw"] = list(remaining_pv_kw)

    remaining_plan = solve_dispatch(
        prices=remaining_prices,
        base_load_kw=remaining_base_load_kw,
        pv_kw=remaining_pv_kw,
        capacity_kwh=dispatch_input["capacity_kwh"],
        max_charge_kw=dispatch_input["max_charge_kw"],
        max_discharge_kw=dispatch_input["max_discharge_kw"],
        charge_efficiency=eta_c,
        discharge_efficiency=eta_d,
        initial_soc_kwh=final_soc,
        min_terminal_soc_kwh=dispatch_input["min_terminal_soc_kwh"],
        degradation_cost=degradation_cost,
    )
    # 衔接自检：余段首个时段的首态 SOC 必须就是前缀末态
    assert abs(
        remaining_plan["periods"][0]["soc_start_kwh"] - final_soc
    ) < 1e-9, "余段首态 SOC 与前缀末态未衔接"

    planned_old = plan_snapshot["periods"]

    # ---- 已执行前缀明细：沿用 /dispatch/settlement 的计费口径 ----
    timeline: List[dict] = []
    total_actual_energy_cost = 0.0
    total_actual_deg_cost = 0.0
    total_actual_grid = 0.0
    total_actual_throughput = 0.0
    total_planned_energy_cost = 0.0
    total_planned_deg_cost = 0.0
    total_planned_grid = 0.0
    total_planned_throughput = 0.0

    for i, rec in enumerate(executed_records):
        plan = planned_old[i]
        actual_throughput = (
            rec.charge_ac_energy_kwh * eta_c + rec.discharge_ac_energy_kwh / eta_d
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
        total_actual_deg_cost += actual_deg_cost
        total_actual_grid += actual_grid
        total_actual_throughput += actual_throughput
        total_planned_energy_cost += planned_energy_cost
        total_planned_deg_cost += planned_deg_cost
        total_planned_grid += planned_grid
        total_planned_throughput += planned_throughput

        timeline.append(
            {
                "index": i,
                "status": "executed",
                "record": rec,
                "plan": {
                    "action": plan["action"],
                    "grid_purchase_energy_kwh": planned_grid,
                    "battery_throughput_kwh": _round6(planned_throughput),
                    "energy_cost_yuan": planned_energy_cost,
                    "degradation_cost_yuan": planned_deg_cost,
                    "soc_start_kwh": float(plan["soc_start_kwh"]),
                    "soc_end_kwh": float(plan["soc_end_kwh"]),
                },
                "soc_gap_start_kwh": _round6(
                    rec.soc_start_kwh - float(plan["soc_start_kwh"])
                ),
                "soc_gap_end_kwh": _round6(rec.soc_end_kwh - float(plan["soc_end_kwh"])),
                "actual_grid_purchase_energy_kwh": _round6(actual_grid),
                "actual_battery_throughput_kwh": _round6(actual_throughput),
                "actual_energy_cost_yuan": _round6(actual_energy_cost),
                "actual_degradation_cost_yuan": _round6(actual_deg_cost),
                "actual_total_cost_yuan": _round6(actual_total_cost),
                "planned_energy_cost_yuan": _round6(planned_energy_cost),
                "planned_degradation_cost_yuan": _round6(planned_deg_cost),
                "planned_total_cost_yuan": _round6(planned_total_cost),
                "energy_cost_delta_yuan": _round6(actual_energy_cost - planned_energy_cost),
                "degradation_cost_delta_yuan": _round6(actual_deg_cost - planned_deg_cost),
                "total_cost_delta_yuan": _round6(actual_total_cost - planned_total_cost),
                "grid_purchase_delta_kwh": _round6(actual_grid - planned_grid),
                "throughput_delta_kwh": _round6(actual_throughput - planned_throughput),
                "audit": audit_by_index[i],
                "reasons": [],
            }
        )

    actual_prefix_total = total_actual_energy_cost + total_actual_deg_cost
    planned_prefix_total = total_planned_energy_cost + total_planned_deg_cost

    # ---- 余段待执行明细：下标接回原时间轴 ----
    for j, period in enumerate(remaining_plan["periods"]):
        entry = dict(period)
        entry["status"] = "planned"
        entry["remaining_index"] = j
        entry["index"] = k + j
        timeline.append(entry)

    remaining_total = float(remaining_plan["summary"]["total_cost_yuan"])

    return {
        "num_periods": n,
        "period_hours": PERIOD_HOURS,
        "audit_tolerance_kwh": tol,
        "executed_periods": k,
        "remaining_periods": n - k,
        "periods": timeline,
        "executed_summary": {
            "executed_periods": k,
            "final_soc_kwh": _round6(final_soc),
            "actual_grid_purchase_kwh": _round6(total_actual_grid),
            "planned_grid_purchase_kwh": _round6(total_planned_grid),
            "grid_purchase_delta_kwh": _round6(total_actual_grid - total_planned_grid),
            "actual_battery_throughput_kwh": _round6(total_actual_throughput),
            "planned_battery_throughput_kwh": _round6(total_planned_throughput),
            "throughput_delta_kwh": _round6(
                total_actual_throughput - total_planned_throughput
            ),
            "actual_energy_cost_yuan": _round6(total_actual_energy_cost),
            "planned_energy_cost_yuan": _round6(total_planned_energy_cost),
            "energy_cost_delta_yuan": _round6(
                total_actual_energy_cost - total_planned_energy_cost
            ),
            "actual_degradation_cost_yuan": _round6(total_actual_deg_cost),
            "planned_degradation_cost_yuan": _round6(total_planned_deg_cost),
            "degradation_cost_delta_yuan": _round6(
                total_actual_deg_cost - total_planned_deg_cost
            ),
            "actual_total_cost_yuan": _round6(actual_prefix_total),
            "planned_total_cost_yuan": _round6(planned_prefix_total),
            "total_cost_delta_yuan": _round6(actual_prefix_total - planned_prefix_total),
        },
        "remaining_summary": remaining_plan["summary"],
        "actual_prefix_total_cost_yuan": _round6(actual_prefix_total),
        "remaining_forecast_total_cost_yuan": _round6(remaining_total),
        "total_cost_yuan": _round6(actual_prefix_total + remaining_total),
        "remaining_dispatch": remaining_dispatch,
        "remaining_plan_snapshot": remaining_plan,
    }
