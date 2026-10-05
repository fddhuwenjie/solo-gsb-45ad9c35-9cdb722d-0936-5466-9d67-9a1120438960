"""家庭储能时段调度：0.1kWh 离散动态规划。

约定（详见 README）：
- 每个时段固定 0.5h，字段均以 kWh / kW 计；
- 动作为单时段电池电芯侧能量变化，单位 0.1kWh：正=充电，负=放电，0=静置；
- 充电：交流侧能量 = 电芯能量 / 充电效率；放电：交流侧能量 = 电芯能量 * 放电效率；
- 光伏优先供给负荷，剩余可给电池充电；不允许向电网倒送，多余光伏弃光；
- 购电量 = max(0, 基础负荷 - 光伏 + 电池交流充电量 - 电池交流放电量)；
- 衰减费按电芯侧吞吐量 sum(|充入电芯能量| + |电芯放出能量|) 计费。
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence

SOC_STEP_KWH = 0.1
PERIOD_HOURS = 0.5

# 浮点“视为相等”的容差：真实费用差异至少在 1e-3 元量级，容差远小于它，
# 只用于消除同一路径不同枚举顺序带来的舍入噪声。
_COST_EPS_ABS = 1e-8
_COST_EPS_REL = 1e-9
_PEAK_EPS_ABS = 1e-9
_PEAK_EPS_REL = 1e-9


class InfeasibleError(Exception):
    """约束可行域为空（输入本身合法，但不存在满足全部约束的计划）。"""


def _snap_steps(value: float, name: str) -> int:
    """把 kWh 量吸附到 0.1kWh 网格，非整数倍直接拒绝（静默取整会违背用户意图）。"""
    steps = int(round(value / SOC_STEP_KWH))
    if abs(value - steps * SOC_STEP_KWH) > 1e-6:
        raise ValueError(f"{name}={value}kWh 不是 {SOC_STEP_KWH}kWh 的整数倍，无法精确离散到 SOC 网格")
    return max(0, steps)


class _Node:
    """到达某 SOC 状态的最优前缀。parent 链同时保存完整动作序列，用于字典序破平局。"""

    __slots__ = ("cost", "peak", "parent", "soc", "action")

    def __init__(self, cost: float, peak: float, parent: Optional["_Node"], soc: int, action: int):
        self.cost = cost
        self.peak = peak
        self.parent = parent
        self.soc = soc          # 到达状态（时段末 SOC，单位 0.1kWh）
        self.action = action    # 到达该状态所用动作（单位 0.1kWh，正充负放）


def _cost_lt(a: float, b: float) -> bool:
    tol = _COST_EPS_ABS + _COST_EPS_REL * max(abs(a), abs(b))
    return a < b - tol


def _peak_lt(a: float, b: float) -> bool:
    tol = _PEAK_EPS_ABS + _PEAK_EPS_REL * max(abs(a), abs(b))
    return a < b - tol


def _lex_less(a: _Node, b: _Node) -> bool:
    """两条等长前缀按动作序列字典序比较：取时间上最早不同的动作。

    parent 链是倒序存储（链尾对应最后一个时段），因此一路回溯并不断
    覆盖比较结果，最终保留的是最靠近根（时间最早）的那一对比对。
    """
    result = False
    found = False
    while a.parent is not None:
        if a.action != b.action:
            result = a.action < b.action
            found = True
        a, b = a.parent, b.parent
    return result if found else False  # 序列完全相同则不算“更小”


def _better(candidate: _Node, incumbent: _Node) -> bool:
    """三级破平局：总费用 -> 峰值购电量 -> 动作字典序。"""
    if _cost_lt(candidate.cost, incumbent.cost):
        return True
    if _cost_lt(incumbent.cost, candidate.cost):
        return False
    if _peak_lt(candidate.peak, incumbent.peak):
        return True
    if _peak_lt(incumbent.peak, candidate.peak):
        return False
    return _lex_less(candidate, incumbent)


def solve_dispatch(
    prices: Sequence[float],
    base_load_kw: Sequence[float],
    pv_kw: Sequence[float],
    capacity_kwh: float,
    max_charge_kw: float,
    max_discharge_kw: float,
    charge_efficiency: float,
    discharge_efficiency: float,
    initial_soc_kwh: float,
    min_terminal_soc_kwh: float,
    degradation_cost: float,
) -> dict:
    n = len(prices)
    cap = _snap_steps(capacity_kwh, "电池容量")
    if cap <= 0:
        raise ValueError("电池容量必须为正")
    init = _snap_steps(initial_soc_kwh, "初始 SOC")
    if init > cap:
        raise ValueError("初始 SOC 不能超过电池容量")
    end_min = _snap_steps(min_terminal_soc_kwh, "最低末端 SOC")
    if end_min > cap:
        raise ValueError("最低末端 SOC 不能超过电池容量")

    dt = PERIOD_HOURS
    step = SOC_STEP_KWH

    # 功率约束换算成单时段最多动作格数（向零取整，保证不越功率上限）。
    max_charge_steps = int(math.floor(max_charge_kw * dt * charge_efficiency / step + 1e-9))
    max_discharge_steps = int(math.floor(max_discharge_kw * dt / discharge_efficiency / step + 1e-9))

    # 每个动作格数对应的购电量与费用，预先按时段算好，DP 内层只做查表。
    # 充电 ac 能量 = ci*step/eta_c；放电 ac 能量 = di*step*eta_d。
    grid0: List[float] = [0.0] * n
    ch_grid: List[List[float]] = [[0.0] * (max_charge_steps + 1) for _ in range(n)]
    ch_cost: List[List[float]] = [[0.0] * (max_charge_steps + 1) for _ in range(n)]
    dis_grid: List[List[float]] = [[0.0] * (max_discharge_steps + 1) for _ in range(n)]
    dis_cost: List[List[float]] = [[0.0] * (max_discharge_steps + 1) for _ in range(n)]
    dis_limit: List[int] = [0] * n  # 受“不许倒送”限制，每时段实际可放格数

    for t in range(n):
        residual = base_load_kw[t] * dt - pv_kw[t] * dt
        grid0[t] = max(0.0, residual)
        price = prices[t]
        for ci in range(1, max_charge_steps + 1):
            ac_energy = ci * step / charge_efficiency
            g = max(0.0, residual + ac_energy)
            ch_grid[t][ci] = g
            ch_cost[t][ci] = price * g + degradation_cost * ci * step
        # 放电交流电量不得超过净负荷，否则构成向电网倒送；净负荷为负时该时段禁止放电。
        d_by_load = int(math.floor(max(0.0, residual) / discharge_efficiency / step + 1e-9))
        dlim = min(max_discharge_steps, d_by_load)
        dis_limit[t] = dlim
        for di in range(1, dlim + 1):
            ac_energy = di * step * discharge_efficiency
            g = max(0.0, residual - ac_energy)
            dis_grid[t][di] = g
            dis_cost[t][di] = price * g + degradation_cost * di * step

    # DP：nodes[j] = 时段末处于状态 j 的最优前缀节点。
    nodes: List[Optional[_Node]] = [None] * (cap + 1)
    nodes[init] = _Node(0.0, 0.0, None, init, 0)

    for t in range(n):
        new_nodes: List[Optional[_Node]] = [None] * (cap + 1)
        g0 = grid0[t]
        c_grid = ch_grid[t]
        c_cost = ch_cost[t]
        d_grid = dis_grid[t]
        d_cost = dis_cost[t]
        dlim = dis_limit[t]
        idle_cost = prices[t] * g0

        for k, node in enumerate(nodes):
            if node is None:
                continue

            # 静置
            cand = _Node(node.cost + idle_cost, max(node.peak, g0), node, k, 0)
            incumbent = new_nodes[k]
            if incumbent is None or _better(cand, incumbent):
                new_nodes[k] = cand

            # 充电（电网或光伏盈余供电，动作 ci 越大充得越多）
            c_hi = min(max_charge_steps, cap - k)
            for ci in range(1, c_hi + 1):
                j = k + ci
                g = c_grid[ci]
                cand = _Node(node.cost + c_cost[ci], max(node.peak, g), node, j, ci)
                incumbent = new_nodes[j]
                if incumbent is None or _better(cand, incumbent):
                    new_nodes[j] = cand

            # 放电（不超过功率、SOC 与本地净负荷）
            d_hi = min(dlim, k)
            for di in range(1, d_hi + 1):
                j = k - di
                g = d_grid[di]
                cand = node.cost + d_cost[di]
                peak = node.peak if node.peak >= g else g
                cand_node = _Node(cand, peak, node, j, -di)
                incumbent = new_nodes[j]
                if incumbent is None or _better(cand_node, incumbent):
                    new_nodes[j] = cand_node

        nodes = new_nodes

    # 末端约束：SOC >= min_terminal_soc（无上限以外的末端上界）。
    best: Optional[_Node] = None
    for j in range(end_min, cap + 1):
        node = nodes[j]
        if node is not None and (best is None or _better(node, best)):
            best = node

    if best is None:
        reachable = [j for j, node in enumerate(nodes) if node is not None]
        if not reachable:
            raise InfeasibleError("无任何可行调度路径（所有时段均不可达，请检查功率与 SOC 参数）")
        max_soc = max(reachable) * step
        raise InfeasibleError(
            "无满足末端 SOC 约束的计划："
            f"调度期末最高可达 SOC 约为 {max_soc:.1f}kWh，"
            f"低于最低末端 SOC {end_min * step:.1f}kWh；"
            "原因是受时段数、最大充电功率或充电效率限制，无法在期末充到要求电量"
        )

    # 回溯，恢复每个时段的（起始 SOC 格, 动作格）。
    chain: List[_Node] = []
    cur: Optional[_Node] = best
    while cur is not None and cur.parent is not None:
        chain.append(cur)
        cur = cur.parent
    chain.reverse()

    periods = []
    total_energy_cost = 0.0
    total_deg_cost = 0.0
    total_grid = 0.0
    total_throughput = 0.0
    peak_grid = 0.0

    for t, node in enumerate(chain):
        a = node.action
        j_end = node.soc
        k_start = j_end - a
        residual = base_load_kw[t] * dt - pv_kw[t] * dt
        if a > 0:
            ch_ac = a * step / charge_efficiency
            dis_ac = 0.0
            action_name = "charge"
        elif a < 0:
            ch_ac = 0.0
            dis_ac = -a * step * discharge_efficiency
            action_name = "discharge"
        else:
            ch_ac = dis_ac = 0.0
            action_name = "idle"

        grid = max(0.0, residual + ch_ac - dis_ac)
        curtailed = max(0.0, -residual - ch_ac)  # 未被负荷/充电消纳的光伏盈余
        energy_cost = prices[t] * grid
        throughput = abs(a) * step
        deg_cost = degradation_cost * throughput

        total_energy_cost += energy_cost
        total_deg_cost += deg_cost
        total_grid += grid
        total_throughput += throughput
        peak_grid = max(peak_grid, grid)

        periods.append(
            {
                "index": t,
                "action": action_name,
                "action_energy_kwh": round(a * step, 6),
                "charge_ac_energy_kwh": round(ch_ac, 6),
                "discharge_ac_energy_kwh": round(dis_ac, 6),
                "battery_power_kw": round((ch_ac - dis_ac) / dt, 6),
                "grid_purchase_energy_kwh": round(grid, 6),
                "pv_curtailed_energy_kwh": round(curtailed, 6),
                "soc_start_kwh": round(k_start * step, 6),
                "soc_end_kwh": round(j_end * step, 6),
                "energy_cost_yuan": round(energy_cost, 6),
                "degradation_cost_yuan": round(deg_cost, 6),
            }
        )

    total_cost = total_energy_cost + total_deg_cost
    return {
        "periods": periods,
        "summary": {
            "num_periods": n,
            "period_hours": PERIOD_HOURS,
            "soc_resolution_kwh": SOC_STEP_KWH,
            "total_energy_cost_yuan": round(total_energy_cost, 6),
            "total_degradation_cost_yuan": round(total_deg_cost, 6),
            "total_cost_yuan": round(total_cost, 6),
            "total_grid_purchase_kwh": round(total_grid, 6),
            "peak_grid_purchase_energy_kwh": round(peak_grid, 6),
            "peak_grid_purchase_power_kw": round(peak_grid / dt, 6),
            "battery_throughput_kwh": round(total_throughput, 6),
            "terminal_soc_kwh": round(best.soc * step, 6),
        },
    }
