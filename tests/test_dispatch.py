"""端到端与求解器行为测试：pytest tests/ -q"""

from fastapi.testclient import TestClient

from app.main import app
from app.solver import InfeasibleError, solve_dispatch

client = TestClient(app)


def _post(payload):
    return client.post("/dispatch", json=payload)


BASE_BATTERY = dict(
    capacity_kwh=0.2,
    max_charge_kw=1.0,
    max_discharge_kw=1.0,
    charge_efficiency=1.0,
    discharge_efficiency=1.0,
    initial_soc_kwh=0.0,
    min_terminal_soc_kwh=0.0,
    degradation_cost_per_kwh=0.0,
)


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_price_arbitrage_charge_at_low_discharge_at_high():
    """4 个半小时时段：负荷恒为 0.1kWh，谷时(0元)充、峰时(1元)放。"""
    payload = dict(
        BASE_BATTERY,
        prices=[1.0, 0.0, 1.0, 0.0],
        base_load_kw=[0.2] * 4,  # 每时段 0.1kWh
        pv_kw=[0.0] * 4,
    )
    r = _post(payload)
    assert r.status_code == 200, r.text
    data = r.json()
    periods = data["periods"]
    names = [p["action"] for p in periods]

    assert names[1] == "charge"
    assert names[2] == "discharge"
    assert periods[1]["charge_ac_energy_kwh"] == 0.1
    assert periods[2]["discharge_ac_energy_kwh"] == 0.1

    # SOC 轨迹正确闭合
    assert [p["soc_end_kwh"] for p in periods] == [0.0, 0.1, 0.0, 0.0]

    # 无电池时费用 0.2 元；套利后峰时段自供 0.1kWh，节省 0.1 元
    assert data["summary"]["total_cost_yuan"] == 0.1
    assert data["summary"]["total_energy_cost_yuan"] == 0.1
    assert data["summary"]["total_degradation_cost_yuan"] == 0.0
    # t1 购电量 0.2kWh 为全周期峰值
    assert data["summary"]["peak_grid_purchase_energy_kwh"] == 0.2


def test_efficiency_losses_and_degradation_cost():
    """eta<1 时充 0.1 电芯电量需 0.125 交流电量；吞吐量衰减费单列。"""
    payload = dict(
        BASE_BATTERY,
        capacity_kwh=0.2,
        max_charge_kw=1.0,
        charge_efficiency=0.8,
        discharge_efficiency=0.8,
        degradation_cost_per_kwh=0.5,
        prices=[0.0, 10.0, 0.0],
        base_load_kw=[0.0, 0.2, 0.0],  # t1 需 0.1kWh
        pv_kw=[0.0, 0.0, 0.0],
    )
    r = _post(payload)
    assert r.status_code == 200, r.text
    periods = r.json()["periods"]
    assert periods[0]["action"] == "charge"
    assert periods[0]["charge_ac_energy_kwh"] == round(0.1 / 0.8, 6)
    # 放电仅 0.08 交流电量，t1 仍需补购 0.02
    assert periods[1]["discharge_ac_energy_kwh"] == 0.08
    assert periods[1]["grid_purchase_energy_kwh"] == 0.02
    summary = r.json()["summary"]
    # 能量费 = 10 * 0.02 = 0.2；衰减费 = 吞吐量 0.2kWh * 0.5 = 0.1
    assert summary["total_energy_cost_yuan"] == 0.2
    assert summary["total_degradation_cost_yuan"] == 0.1
    assert summary["total_cost_yuan"] == 0.3
    assert summary["battery_throughput_kwh"] == 0.2


def test_terminal_soc_constraint_enforced():
    """最低末端 SOC=0.2 时，期末必须充满；初始 0.1、容量 0.2。"""
    payload = dict(
        BASE_BATTERY,
        capacity_kwh=0.2,
        initial_soc_kwh=0.1,
        min_terminal_soc_kwh=0.2,
        prices=[0.0, 0.0],
        base_load_kw=[0.0, 0.0],
        pv_kw=[0.0, 0.0],
    )
    r = _post(payload)
    assert r.status_code == 200, r.text
    periods = r.json()["periods"]
    assert periods[-1]["soc_end_kwh"] == 0.2
    assert sum(p["action_energy_kwh"] for p in periods) == 0.1


def test_infeasible_terminal_soc_returns_clear_reason():
    """零充电功率且初始 SOC 低于末端要求 → 无解，需说明原因。"""
    payload = dict(
        BASE_BATTERY,
        max_charge_kw=0.0,
        initial_soc_kwh=0.0,
        min_terminal_soc_kwh=0.1,
        prices=[0.0, 0.0],
        base_load_kw=[0.0, 0.0],
        pv_kw=[0.0, 0.0],
    )
    r = _post(payload)
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "infeasible"
    assert "末端 SOC" in body["reason"]
    assert "0.1" in body["reason"]


def test_infeasible_due_to_insufficient_horizon():
    """每时段最多充 0.1（0.2kW*0.5h），单时段却要求从 0 充到 0.2 → 无解。"""
    payload = dict(
        BASE_BATTERY,
        capacity_kwh=0.2,
        max_charge_kw=0.2,
        min_terminal_soc_kwh=0.2,
        prices=[0.0],
        base_load_kw=[0.0],
        pv_kw=[0.0],
    )
    r = _post(payload)
    assert r.status_code == 422
    assert r.json()["error"] == "infeasible"


def test_unequal_lengths_rejected():
    payload = dict(
        BASE_BATTERY,
        prices=[0.0, 0.0],
        base_load_kw=[0.0],
        pv_kw=[0.0, 0.0],
    )
    r = _post(payload)
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "invalid_input"
    assert "等长" in body["reason"]


def test_non_grid_snap_soc_rejected():
    payload = dict(
        BASE_BATTERY,
        capacity_kwh=0.25,
        prices=[0.0],
        base_load_kw=[0.0],
        pv_kw=[0.0],
    )
    r = _post(payload)
    assert r.status_code == 422
    assert "0.1" in r.json()["reason"]


def test_negative_price_and_bad_efficiency_rejected():
    payload = dict(
        BASE_BATTERY,
        prices=[-1.0],
        base_load_kw=[0.0],
        pv_kw=[0.0],
    )
    r = _post(payload)
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_input"

    payload = dict(
        BASE_BATTERY,
        charge_efficiency=1.2,
        prices=[0.0],
        base_load_kw=[0.0],
        pv_kw=[0.0],
    )
    r = _post(payload)
    assert r.status_code == 422


def test_no_feed_in_discharge_limited_by_load():
    """光伏已覆盖负荷时，放电无处可去（禁止倒送），电池必须静置。"""
    payload = dict(
        BASE_BATTERY,
        initial_soc_kwh=0.2,
        prices=[5.0],
        base_load_kw=[0.0],
        pv_kw=[0.4],  # 盈余 0.2kWh
    )
    r = _post(payload)
    assert r.status_code == 200, r.text
    p = r.json()["periods"][0]
    assert p["action"] == "idle"
    assert p["grid_purchase_energy_kwh"] == 0.0
    # 电池未吸收盈余，全部弃光
    assert p["pv_curtailed_energy_kwh"] == 0.2


def test_pv_surplus_charges_battery_for_free():
    """光伏盈余可充电池：两时段各有 0.1 盈余，要求末端 0.1，购电量恒为 0。"""
    payload = dict(
        BASE_BATTERY,
        min_terminal_soc_kwh=0.1,
        prices=[0.0, 0.0],
        base_load_kw=[0.0, 0.0],
        pv_kw=[0.2, 0.2],
    )
    r = _post(payload)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["summary"]["total_grid_purchase_kwh"] == 0.0
    assert data["summary"]["terminal_soc_kwh"] == 0.1


def test_lexicographic_tie_break_prefers_idle_early():
    """费用与峰值完全相同（光伏免费充电）时，按动作字典序选“先静置后充电”。

    两时段各有 0.1kWh 光伏盈余，容量 0.2，末端至少 0.1：
    (charge,idle) 与 (idle,charge) 费用/峰值相同，动作整数序 (0,+1) < (+1,0)。
    """
    payload = dict(
        BASE_BATTERY,
        capacity_kwh=0.2,
        min_terminal_soc_kwh=0.1,
        prices=[0.0, 0.0],
        base_load_kw=[0.0, 0.0],
        pv_kw=[0.2, 0.2],
    )
    r = _post(payload)
    assert r.status_code == 200, r.text
    actions = [p["action"] for p in r.json()["periods"]]
    assert actions == ["idle", "charge"]


def test_peak_purchase_used_as_second_tiebreak():
    """两方案能量费相同时，选峰值购电量更小者（放电削峰）。

    构造：t1 高价有负荷，t0 低价。方案 A 峰时放电会改变费用，
    这里直接用 solver 验证 summary 的峰值统计与最优性：
    两时段同价且无衰减时，“先充后放”与“不动作”费用相同则应选峰值更小。
    """
    # 价格相同 → 套利无收益；充放电会抬高 t0 购电峰值，故最优是不动作
    payload = dict(
        BASE_BATTERY,
        prices=[1.0, 1.0],
        base_load_kw=[0.2, 0.2],
        pv_kw=[0.0, 0.0],
    )
    r = _post(payload)
    assert r.status_code == 200, r.text
    actions = [p["action"] for p in r.json()["periods"]]
    assert actions == ["idle", "idle"]


def test_solver_rejects_non_snapped_values_directly():
    import pytest

    with pytest.raises(ValueError):
        solve_dispatch(
            prices=[0.0],
            base_load_kw=[0.0],
            pv_kw=[0.0],
            capacity_kwh=0.13,
            max_charge_kw=1.0,
            max_discharge_kw=1.0,
            charge_efficiency=1.0,
            discharge_efficiency=1.0,
            initial_soc_kwh=0.0,
            min_terminal_soc_kwh=0.0,
            degradation_cost=0.0,
        )


def test_solver_raises_infeasible():
    import pytest

    with pytest.raises(InfeasibleError):
        solve_dispatch(
            prices=[0.0],
            base_load_kw=[0.0],
            pv_kw=[0.0],
            capacity_kwh=0.2,
            max_charge_kw=0.0,
            max_discharge_kw=0.0,
            charge_efficiency=1.0,
            discharge_efficiency=1.0,
            initial_soc_kwh=0.0,
            min_terminal_soc_kwh=0.2,
            degradation_cost=0.0,
        )
