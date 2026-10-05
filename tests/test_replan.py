"""POST /dispatch/replan 行为测试：pytest tests/ -q"""

import copy
import json

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# 4 个半小时时段、效率 0.9、衰减费 0.2 元/kWh 的调度输入（与结算测试同口径）。
DISPATCH = dict(
    prices=[0.0, 1.0, 1.0, 0.0],
    base_load_kw=[0.2, 0.2, 0.2, 0.2],
    pv_kw=[0.0, 0.0, 0.0, 0.0],
    capacity_kwh=0.4,
    max_charge_kw=1.0,
    max_discharge_kw=1.0,
    charge_efficiency=0.9,
    discharge_efficiency=0.9,
    initial_soc_kwh=0.0,
    min_terminal_soc_kwh=0.0,
    degradation_cost_per_kwh=0.2,
)

# 与原计划自洽的完整 4 段实测记录（计划：充0.2、放0.1、放0.1、静置）。
RECORDS = [
    dict(
        base_load_kw=0.2, pv_kw=0.0,
        charge_ac_energy_kwh=round(0.2 / 0.9, 6), discharge_ac_energy_kwh=0.0,
        grid_purchase_energy_kwh=round(0.1 + 0.2 / 0.9, 6),
        soc_start_kwh=0.0, soc_end_kwh=0.2,
    ),
    dict(
        base_load_kw=0.2, pv_kw=0.0,
        charge_ac_energy_kwh=0.0, discharge_ac_energy_kwh=0.09,
        grid_purchase_energy_kwh=0.01,
        soc_start_kwh=0.2, soc_end_kwh=0.1,
    ),
    dict(
        base_load_kw=0.2, pv_kw=0.0,
        charge_ac_energy_kwh=0.0, discharge_ac_energy_kwh=0.09,
        grid_purchase_energy_kwh=0.01,
        soc_start_kwh=0.1, soc_end_kwh=0.0,
    ),
    dict(
        base_load_kw=0.2, pv_kw=0.0,
        charge_ac_energy_kwh=0.0, discharge_ac_energy_kwh=0.0,
        grid_purchase_energy_kwh=0.1,
        soc_start_kwh=0.0, soc_end_kwh=0.0,
    ),
]

TOL = 0.01


def _plan(dispatch=None):
    return client.post("/dispatch", json=dispatch or DISPATCH).json()


def _replan(records, rem_prices, rem_load, rem_pv, tol=TOL, plan=None, dispatch=None):
    return client.post(
        "/dispatch/replan",
        json={
            "dispatch": dispatch or DISPATCH,
            "plan_snapshot": plan or _plan(),
            "audit_tolerance_kwh": tol,
            "executed_records": records,
            "remaining_prices": rem_prices,
            "remaining_base_load_kw": rem_load,
            "remaining_pv_kw": rem_pv,
        },
    )


def test_replan_basic_timeline_and_handoff():
    """前 2 段按计划执行；余段 t2 电价暴涨到 5 元：余段应继续放电、费用口径正确，
    并产出可直接交给 /dispatch/settlement 的余段输入与快照。"""
    r = _replan(
        copy.deepcopy(RECORDS[:2]),
        rem_prices=[5.0, 0.0],
        rem_load=[0.2, 0.2],
        rem_pv=[0.0, 0.0],
    )
    assert r.status_code == 200, r.text
    data = r.json()

    assert data["num_periods"] == 4
    assert data["period_hours"] == 0.5
    assert data["audit_tolerance_kwh"] == TOL
    assert data["executed_periods"] == 2
    assert data["remaining_periods"] == 2

    statuses = [p["status"] for p in data["periods"]]
    assert statuses == ["executed", "executed", "planned", "planned"]
    assert [p["index"] for p in data["periods"]] == [0, 1, 2, 3]

    # 已执行明细：实测口径 + 原计划回显 + 审计无异常
    p0 = data["periods"][0]
    assert p0["plan"]["action"] == "charge"
    assert p0["actual_battery_throughput_kwh"] == 0.2
    assert p0["actual_energy_cost_yuan"] == 0.0
    assert p0["actual_degradation_cost_yuan"] == 0.04
    assert p0["soc_gap_end_kwh"] == 0.0
    assert p0["audit"]["anomalous"] is False
    assert p0["reasons"] == []
    # 原始记录原样保留
    assert p0["record"]["soc_end_kwh"] == 0.2

    p1 = data["periods"][1]
    assert p1["actual_total_cost_yuan"] == 0.03  # 0.01 能量 + 0.02 衰减
    assert p1["grid_purchase_delta_kwh"] == 0.0

    # 余段：t2 高价应继续放电（0.1→0.0），t3 静置
    p2, p3 = data["periods"][2:]
    assert p2["action"] == "discharge"
    assert p2["remaining_index"] == 0
    assert p2["soc_start_kwh"] == 0.1
    assert p2["soc_end_kwh"] == 0.0
    assert p3["remaining_index"] == 1
    assert p3["soc_start_kwh"] == 0.0

    # 前缀实测费用 0.07（t0 衰减 0.04 + t1 合计 0.03）；余段预测 0.07（放电省购电 + 衰减）
    es = data["executed_summary"]
    assert es["executed_periods"] == 2
    assert es["final_soc_kwh"] == 0.1
    assert es["actual_total_cost_yuan"] == 0.07
    assert es["planned_total_cost_yuan"] == 0.07
    assert es["total_cost_delta_yuan"] == 0.0
    assert data["actual_prefix_total_cost_yuan"] == 0.07
    assert data["remaining_forecast_total_cost_yuan"] == 0.07
    assert data["total_cost_yuan"] == 0.14

    # 余段调度输入：初始 SOC 接前缀末态、数组只覆盖余段、末端约束沿用原值
    rem_dispatch = data["remaining_dispatch"]
    assert rem_dispatch["initial_soc_kwh"] == 0.1
    assert rem_dispatch["prices"] == [5.0, 0.0]
    assert rem_dispatch["base_load_kw"] == [0.2, 0.2]
    assert rem_dispatch["min_terminal_soc_kwh"] == 0.0
    assert rem_dispatch["capacity_kwh"] == 0.4
    assert len(rem_dispatch["prices"]) == 2
    assert data["remaining_plan_snapshot"]["summary"]["num_periods"] == 2
    assert data["remaining_summary"]["terminal_soc_kwh"] == 0.0

    # 端到端：余段输入+快照可直接结算，快照能通过重算核对
    rem_records = []
    for pp in data["remaining_plan_snapshot"]["periods"]:
        rem_records.append(
            dict(
                base_load_kw=0.2, pv_kw=0.0,
                charge_ac_energy_kwh=pp["charge_ac_energy_kwh"],
                discharge_ac_energy_kwh=pp["discharge_ac_energy_kwh"],
                grid_purchase_energy_kwh=pp["grid_purchase_energy_kwh"],
                soc_start_kwh=pp["soc_start_kwh"],
                soc_end_kwh=pp["soc_end_kwh"],
            )
        )
    sr = client.post(
        "/dispatch/settlement",
        json={
            "dispatch": rem_dispatch,
            "plan_snapshot": data["remaining_plan_snapshot"],
            "settlement_tolerance_kwh": TOL,
            "records": rem_records,
        },
    )
    assert sr.status_code == 200, sr.text
    sbody = sr.json()
    assert sbody["status"] == "settled"
    assert sbody["settled_periods"] == 2
    assert (
        sbody["summary"]["actual_total_cost_yuan"]
        == data["remaining_forecast_total_cost_yuan"]
    )


def test_deviation_from_old_plan_kept_and_replanned():
    """实测自洽但动作偏离旧计划：不做 SOC 断点拒绝，原记录保留，从实测末态重排。"""
    # t0 原计划充 0.2；实际只充 0.1（末态 0.1），能量/购电自洽
    records = [
        dict(
            base_load_kw=0.2, pv_kw=0.0,
            charge_ac_energy_kwh=round(0.1 / 0.9, 6), discharge_ac_energy_kwh=0.0,
            grid_purchase_energy_kwh=round(0.1 + 0.1 / 0.9, 6),
            soc_start_kwh=0.0, soc_end_kwh=0.1,
        )
    ]
    r = _replan(
        records,
        rem_prices=[1.0, 5.0, 0.0],
        rem_load=[0.2, 0.2, 0.2],
        rem_pv=[0.0, 0.0, 0.0],
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["executed_periods"] == 1
    assert data["remaining_periods"] == 3

    p0 = data["periods"][0]
    # 对旧计划的偏离照实展示，但不是异常
    assert p0["plan"]["action"] == "charge"
    assert p0["soc_gap_end_kwh"] == -0.1
    assert p0["audit"]["anomalous"] is False
    assert p0["record"]["soc_end_kwh"] == 0.1
    assert p0["actual_battery_throughput_kwh"] == 0.1
    assert p0["planned_energy_cost_yuan"] == 0.0
    assert p0["actual_energy_cost_yuan"] == 0.0  # t0 电价 0

    # 余段首态接 0.1；t2 高价 5 元时放电
    planned = data["periods"][1:]
    assert planned[0]["soc_start_kwh"] == 0.1
    assert [p["action"] for p in planned] == ["idle", "discharge", "idle"]
    assert data["executed_summary"]["final_soc_kwh"] == 0.1
    assert data["remaining_dispatch"]["initial_soc_kwh"] == 0.1


def test_meter_deviation_within_tolerance_accepted():
    """购电表小偏差在容差内：前缀通过，实测费用照实（同结算口径）。"""
    records = copy.deepcopy(RECORDS[:2])
    records[1]["grid_purchase_energy_kwh"] = 0.015  # 残差 0.005 ≤ 0.01
    r = _replan(records, rem_prices=[1.0, 0.0], rem_load=[0.2, 0.2], rem_pv=[0.0, 0.0])
    assert r.status_code == 200, r.text
    data = r.json()
    p1 = data["periods"][1]
    assert p1["grid_purchase_delta_kwh"] == 0.005
    assert p1["energy_cost_delta_yuan"] == 0.005
    assert data["executed_summary"]["actual_energy_cost_yuan"] == 0.015
    assert data["actual_prefix_total_cost_yuan"] == 0.075


def test_first_soc_not_at_original_initial_rejected():
    """首条记录首态 SOC 偏离原初值超容差 → prefix_audit_failed；等于容差不算超差。"""
    records = copy.deepcopy(RECORDS[:2])
    records[0]["soc_start_kwh"] = 0.1
    r = _replan(records, rem_prices=[1.0, 0.0], rem_load=[0.2, 0.2], rem_pv=[0.0, 0.0])
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "prefix_audit_failed"
    assert "原初始 SOC" in body["reason"]

    # 边界：静置前缀，首态偏离恰好等于容差 0.01 → 接受（严格大于才判超差）
    idle_records = [
        dict(
            base_load_kw=0.2, pv_kw=0.0,
            charge_ac_energy_kwh=0.0, discharge_ac_energy_kwh=0.0,
            grid_purchase_energy_kwh=0.1,
            soc_start_kwh=0.01, soc_end_kwh=0.01,
        ),
        dict(
            base_load_kw=0.2, pv_kw=0.0,
            charge_ac_energy_kwh=0.0, discharge_ac_energy_kwh=0.0,
            grid_purchase_energy_kwh=0.1,
            soc_start_kwh=0.01, soc_end_kwh=0.01,
        ),
    ]
    r = _replan(
        idle_records, rem_prices=[1.0, 0.0], rem_load=[0.2, 0.2], rem_pv=[0.0, 0.0]
    )
    # 首态边界通过，但末态 0.01 不在 0.1 网格——说明审计边界已过（错误码不是审计失败）
    assert r.status_code == 422
    assert r.json()["error"] == "prefix_soc_off_grid"

    # 超出容差一点点即按首态问题拒绝
    idle_records[0]["soc_start_kwh"] = 0.011
    r = _replan(
        idle_records, rem_prices=[1.0, 0.0], rem_load=[0.2, 0.2], rem_pv=[0.0, 0.0]
    )
    assert r.status_code == 422
    assert r.json()["error"] == "prefix_audit_failed"


def test_prefix_grid_residual_anomaly_rejected():
    """前缀存在购电量残差异常 → prefix_audit_failed 整体拒绝。"""
    records = copy.deepcopy(RECORDS[:2])
    records[1]["grid_purchase_energy_kwh"] = 0.11  # 期望 0.01，残差 0.1
    r = _replan(records, rem_prices=[1.0, 0.0], rem_load=[0.2, 0.2], rem_pv=[0.0, 0.0])
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "prefix_audit_failed"
    assert "第 1 条" in body["reason"]
    assert "购电量残差" in body["reason"]


def test_prefix_soc_continuity_gap_rejected():
    """前缀内部 SOC 衔接断裂 → prefix_audit_failed。"""
    records = copy.deepcopy(RECORDS[:2])
    records[1]["soc_start_kwh"] = 0.25
    r = _replan(records, rem_prices=[1.0, 0.0], rem_load=[0.2, 0.2], rem_pv=[0.0, 0.0])
    assert r.status_code == 422
    assert r.json()["error"] == "prefix_audit_failed"


def test_prefix_soc_energy_residual_anomaly_rejected():
    """前缀末态与电芯能量核算不符 → prefix_audit_failed。"""
    records = copy.deepcopy(RECORDS[:2])
    records[1]["soc_end_kwh"] = 0.05
    r = _replan(records, rem_prices=[1.0, 0.0], rem_load=[0.2, 0.2], rem_pv=[0.0, 0.0])
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "prefix_audit_failed"
    assert "电芯能量残差" in body["reason"]


def test_off_grid_final_soc_rejected():
    """前缀遥测自洽但末态不在 0.1kWh 网格 → prefix_soc_off_grid。"""
    dispatch = dict(DISPATCH, charge_efficiency=1.0, discharge_efficiency=1.0)
    plan = _plan(dispatch)
    records = [
        dict(
            base_load_kw=0.2, pv_kw=0.0,
            charge_ac_energy_kwh=0.05, discharge_ac_energy_kwh=0.0,
            grid_purchase_energy_kwh=0.15,
            soc_start_kwh=0.0, soc_end_kwh=0.05,
        ),
        dict(
            base_load_kw=0.2, pv_kw=0.0,
            charge_ac_energy_kwh=0.0, discharge_ac_energy_kwh=0.0,
            grid_purchase_energy_kwh=0.1,
            soc_start_kwh=0.05, soc_end_kwh=0.05,
        ),
    ]
    r = _replan(
        records, rem_prices=[1.0, 0.0], rem_load=[0.2, 0.2], rem_pv=[0.0, 0.0],
        plan=plan, dispatch=dispatch,
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "prefix_soc_off_grid"
    assert "0.1" in body["reason"]


def test_terminal_soc_unreachable_from_prefix_state_rejected():
    """原末端约束从实测末态起在余段已无法满足 → infeasible 整体拒绝。

    全周期 4 段可行（3 充 1 放即可到 0.3），但前缀静置到 0.0、余段只剩 2 段、
    每段最多充 0.1（0.2kW×0.5h），最高只到 0.2 < 0.3。"""
    dispatch = dict(
        DISPATCH,
        max_charge_kw=0.2,
        charge_efficiency=1.0,
        discharge_efficiency=1.0,
        min_terminal_soc_kwh=0.3,
    )
    plan = _plan(dispatch)
    records = copy.deepcopy(RECORDS[:2])
    for rec in records:
        rec["charge_ac_energy_kwh"] = 0.0
        rec["discharge_ac_energy_kwh"] = 0.0
        rec["grid_purchase_energy_kwh"] = 0.1
        rec["soc_start_kwh"] = 0.0
        rec["soc_end_kwh"] = 0.0
    r = _replan(
        records, rem_prices=[1.0, 0.0], rem_load=[0.2, 0.2], rem_pv=[0.0, 0.0],
        plan=plan, dispatch=dispatch,
    )
    assert r.status_code == 422
    assert r.json()["error"] == "infeasible"


def test_tampered_snapshot_rejected():
    """原计划快照被篡改 → plan_snapshot_mismatch。"""
    tampered = copy.deepcopy(_plan())
    tampered["periods"][0]["energy_cost_yuan"] = 9.9
    r = _replan(
        copy.deepcopy(RECORDS[:2]),
        rem_prices=[1.0, 0.0], rem_load=[0.2, 0.2], rem_pv=[0.0, 0.0],
        plan=tampered,
    )
    assert r.status_code == 422
    assert r.json()["error"] == "plan_snapshot_mismatch"


def test_remaining_length_mismatch_rejected():
    """余段预测长度不等于（原时段数 − 前缀段数）→ invalid_input。"""
    r = _replan(
        copy.deepcopy(RECORDS[:2]),
        rem_prices=[1.0, 0.0, 1.0], rem_load=[0.2, 0.2, 0.2], rem_pv=[0.0, 0.0, 0.0],
    )
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_input"
    assert "余段预测长度" in r.json()["reason"]

    # 三组余段预测彼此不等长同样拒绝
    r = _replan(
        copy.deepcopy(RECORDS[:2]),
        rem_prices=[1.0, 0.0], rem_load=[0.2], rem_pv=[0.0, 0.0],
    )
    assert r.status_code == 422


def test_prefix_must_be_nonempty_and_leave_remaining():
    """空前缀（min_length=1）与前缀覆盖全周期（无待执行时段）均拒绝。"""
    r = client.post(
        "/dispatch/replan",
        json={
            "dispatch": DISPATCH,
            "plan_snapshot": _plan(),
            "audit_tolerance_kwh": TOL,
            "executed_records": [],
            "remaining_prices": [1.0] * 4,
            "remaining_base_load_kw": [0.2] * 4,
            "remaining_pv_kw": [0.0] * 4,
        },
    )
    assert r.status_code == 422

    # 前缀已覆盖全部 4 段：即便余段数组非空也在模型校验阶段被拒
    r = client.post(
        "/dispatch/replan",
        json={
            "dispatch": DISPATCH,
            "plan_snapshot": _plan(),
            "audit_tolerance_kwh": TOL,
            "executed_records": copy.deepcopy(RECORDS),
            "remaining_prices": [1.0],
            "remaining_base_load_kw": [0.2],
            "remaining_pv_kw": [0.0],
        },
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "invalid_input"
    assert "少于原计划时段数" in body["reason"]
    assert "待执行时段" in body["reason"]


def test_invalid_scalars_and_measurements_rejected():
    """容差非法、测量值为负/非有限、SOC 超容量沿用既有校验 → invalid_input。"""
    assert (
        _replan(
            copy.deepcopy(RECORDS[:2]),
            rem_prices=[1.0, 0.0], rem_load=[0.2, 0.2], rem_pv=[0.0, 0.0],
            tol=-0.01,
        ).status_code
        == 422
    )

    raw = json.dumps({
        "dispatch": DISPATCH,
        "plan_snapshot": _plan(),
        "audit_tolerance_kwh": float("nan"),
        "executed_records": RECORDS[:2],
        "remaining_prices": [1.0, 0.0],
        "remaining_base_load_kw": [0.2, 0.2],
        "remaining_pv_kw": [0.0, 0.0],
    })
    r = client.post(
        "/dispatch/replan",
        content=raw,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422

    records = copy.deepcopy(RECORDS[:2])
    records[0]["base_load_kw"] = -0.1
    assert (
        _replan(
            records, rem_prices=[1.0, 0.0], rem_load=[0.2, 0.2], rem_pv=[0.0, 0.0]
        ).status_code
        == 422
    )

    records = copy.deepcopy(RECORDS[:2])
    records[0]["soc_end_kwh"] = 0.5  # 超容量 0.4
    assert (
        _replan(
            records, rem_prices=[1.0, 0.0], rem_load=[0.2, 0.2], rem_pv=[0.0, 0.0]
        ).status_code
        == 422
    )

    # 余段预测出现负值/非有限同样拒绝
    assert (
        _replan(
            copy.deepcopy(RECORDS[:2]),
            rem_prices=[-1.0, 0.0], rem_load=[0.2, 0.2], rem_pv=[0.0, 0.0],
        ).status_code
        == 422
    )


def test_recomputed_infeasible_original_input_rejected():
    """原 dispatch 输入本身无解 → infeasible，不进入续排。"""
    bad_dispatch = copy.deepcopy(DISPATCH)
    bad_dispatch["max_charge_kw"] = 0.0
    bad_dispatch["min_terminal_soc_kwh"] = 0.4
    r = client.post(
        "/dispatch/replan",
        json={
            "dispatch": bad_dispatch,
            "plan_snapshot": _plan(),
            "audit_tolerance_kwh": TOL,
            "executed_records": copy.deepcopy(RECORDS[:2]),
            "remaining_prices": [1.0, 0.0],
            "remaining_base_load_kw": [0.2, 0.2],
            "remaining_pv_kw": [0.0, 0.0],
        },
    )
    assert r.status_code == 422
    assert r.json()["error"] == "infeasible"


def test_single_period_remaining_handoff():
    """前缀 3 段、余段仅 1 段也可续排，且余段快照可结算。"""
    r = _replan(
        copy.deepcopy(RECORDS[:3]),
        rem_prices=[0.0], rem_load=[0.2], rem_pv=[0.0],
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["executed_periods"] == 3
    assert data["remaining_periods"] == 1
    assert [p["status"] for p in data["periods"]] == ["executed"] * 3 + ["planned"]
    last = data["periods"][-1]
    assert last["index"] == 3
    assert last["remaining_index"] == 0
    assert last["soc_start_kwh"] == 0.0
    assert len(data["remaining_dispatch"]["prices"]) == 1

    sr = client.post(
        "/dispatch/settlement",
        json={
            "dispatch": data["remaining_dispatch"],
            "plan_snapshot": data["remaining_plan_snapshot"],
            "settlement_tolerance_kwh": TOL,
            "records": [
                dict(
                    base_load_kw=0.2, pv_kw=0.0,
                    charge_ac_energy_kwh=last["charge_ac_energy_kwh"],
                    discharge_ac_energy_kwh=last["discharge_ac_energy_kwh"],
                    grid_purchase_energy_kwh=last["grid_purchase_energy_kwh"],
                    soc_start_kwh=last["soc_start_kwh"],
                    soc_end_kwh=last["soc_end_kwh"],
                )
            ],
        },
    )
    assert sr.status_code == 200, sr.text
    assert sr.json()["status"] == "settled"


def test_existing_endpoints_unchanged():
    """新增续排接口不影响 /dispatch、/telemetry/audit、/dispatch/settlement、/healthz。"""
    assert client.get("/healthz").json() == {"status": "ok"}

    r = client.post("/dispatch", json=DISPATCH)
    assert r.status_code == 200
    assert len(r.json()["periods"]) == 4

    r = client.post(
        "/telemetry/audit",
        json={
            "capacity_kwh": DISPATCH["capacity_kwh"],
            "charge_efficiency": 0.9,
            "discharge_efficiency": 0.9,
            "energy_tolerance_kwh": TOL,
            "records": RECORDS,
        },
    )
    assert r.status_code == 200
    assert r.json()["anomalous"] is False

    r = client.post(
        "/dispatch/settlement",
        json={
            "dispatch": DISPATCH,
            "plan_snapshot": _plan(),
            "settlement_tolerance_kwh": TOL,
            "records": copy.deepcopy(RECORDS),
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "settled"
