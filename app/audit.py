"""遥测审计：核对半小时实测记录的能量守恒与购电量一致性。

只读校验——不修正读数、不生成调度计划：
- 电芯侧能量：SOC 变化应等于 交流充电量×充电效率 − 交流放电量÷放电效率；
- 购电量：应等于 max(0, (基础负荷 − 光伏)×0.5 + 交流充电量 − 交流放电量)；
- 相邻记录的首末 SOC 应衔接（首态 SOC 与上一条末态 SOC 之差绝对值不超容差）。

任一残差绝对值超过能量容差即标记该条记录异常，并逐条返回分项残差。
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from app.models import TelemetryRecord
from app.solver import PERIOD_HOURS


def _round6(value: float) -> float:
    """保留 6 位小数；+0.0 把 -0.0 归一为 0.0，避免响应里出现负零。"""
    return round(value, 6) + 0.0


def audit_telemetry(
    charge_efficiency: float,
    discharge_efficiency: float,
    energy_tolerance_kwh: float,
    records: Sequence[TelemetryRecord],
) -> dict:
    """逐条核算实测记录，返回审计报告（分项残差 + 异常标记）。"""
    tol = energy_tolerance_kwh
    report: List[dict] = []
    anomalous_records: List[int] = []
    prev_soc_end: Optional[float] = None

    for i, rec in enumerate(records):
        # 电芯侧：期望末态 SOC = 首态 + 充入电芯能量 − 电芯放出能量
        expected_soc_end = (
            rec.soc_start_kwh
            + rec.charge_ac_energy_kwh * charge_efficiency
            - rec.discharge_ac_energy_kwh / discharge_efficiency
        )
        soc_residual = rec.soc_end_kwh - expected_soc_end

        # 交流侧：期望购电量（不允许倒送，盈余截断为 0）
        expected_grid = max(
            0.0,
            (rec.base_load_kw - rec.pv_kw) * PERIOD_HOURS
            + rec.charge_ac_energy_kwh
            - rec.discharge_ac_energy_kwh,
        )
        grid_residual = rec.grid_purchase_energy_kwh - expected_grid

        # 与上一条记录的 SOC 衔接差（首条记 0）
        continuity_gap = 0.0 if prev_soc_end is None else rec.soc_start_kwh - prev_soc_end

        reasons: List[str] = []
        if abs(soc_residual) > tol:
            reasons.append(
                f"电芯能量残差 {soc_residual:+.6f}kWh 超容差 {tol}kWh"
                f"（实测末态 SOC {rec.soc_end_kwh}kWh，核算值 {_round6(expected_soc_end)}kWh）"
            )
        if abs(grid_residual) > tol:
            reasons.append(
                f"购电量残差 {grid_residual:+.6f}kWh 超容差 {tol}kWh"
                f"（实测 {rec.grid_purchase_energy_kwh}kWh，核算值 {_round6(expected_grid)}kWh）"
            )
        if prev_soc_end is not None and abs(continuity_gap) > tol:
            reasons.append(
                f"与上一条记录 SOC 不衔接：首态 {rec.soc_start_kwh}kWh "
                f"与上条末态 {prev_soc_end}kWh 相差 {continuity_gap:+.6f}kWh，超容差 {tol}kWh"
            )

        anomalous = bool(reasons)
        if anomalous:
            anomalous_records.append(i)

        report.append(
            {
                "index": i,
                "expected_soc_end_kwh": _round6(expected_soc_end),
                "soc_residual_kwh": _round6(soc_residual),
                "expected_grid_purchase_energy_kwh": _round6(expected_grid),
                "grid_residual_kwh": _round6(grid_residual),
                "soc_continuity_gap_kwh": _round6(continuity_gap),
                "anomalous": anomalous,
                "reasons": reasons,
            }
        )
        prev_soc_end = rec.soc_end_kwh

    return {
        "num_records": len(records),
        "period_hours": PERIOD_HOURS,
        "energy_tolerance_kwh": energy_tolerance_kwh,
        "anomalous": bool(anomalous_records),
        "anomalous_records": anomalous_records,
        "records": report,
    }
