"""POST /dispatch/settlement 行为测试：pytest tests/ -q"""

import copy
import json

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# 4 个半小时时段、效率 0.9、衰减费 0.2 元/kWh 的调度输入。
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

# 与计划完全自洽的实测记录：
# t0 谷充 0.2 电芯（交流 0.2/0.9≈0.222222），购电 0.1+0.222222；
# t1/t2 各放 0.09 交流（电芯 0.1），购电各 0.01；t3 静置，购电 0.1。
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


def _plan():
    return client.post("/dispatch", json=DISPATCH).json()


def _settle(records, tol=TOL, plan=None, dispatch=None):
    return client.post(
        "/dispatch/settlement",
        json={
            "dispatch": dispatch or DISPATCH,
            "plan_snapshot": plan or _plan(),
            "settlement_tolerance_kwh": tol,
            "records": records,
        },
    )


def test_full_settlement_matching_execution():
    """实测与计划完全一致：全部已结算，差额全为 0。"""
    r = _settle(copy.deepcopy(RECORDS))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "settled"
    assert data["settled_periods"] == 4
    assert data["pending_periods"] == 0
    assert data["first_anomaly_index"] is None
    assert data["anomalous_records"] == []
    assert data["soc_breakpoint_records"] == []
    assert [p["status"] for p in data["periods"]] == ["settled"] * 4

    summary = data["summary"]
    # 计划总费用 = 能量费 0.02 + 衰减费 0.08（吞吐量 0.4×0.2）
    assert summary["actual_total_cost_yuan"] == 0.1
    assert summary["planned_total_cost_yuan"] == 0.1
    assert summary["total_cost_delta_yuan"] == 0.0
    assert summary["actual_battery_throughput_kwh"] == 0.4
    assert summary["grid_purchase_delta_kwh"] == 0.0

    # t0：谷时充电，能量费 0，吞吐量 0.2 电芯
    p0 = data["periods"][0]
    assert p0["plan"]["action"] == "charge"
    assert p0["actual_battery_throughput_kwh"] == 0.2
    assert p0["actual_energy_cost_yuan"] == 0.0
    assert p0["actual_degradation_cost_yuan"] == 0.04
    assert p0["energy_cost_delta_yuan"] == 0.0


def test_within_tolerance_meter_deviation_still_settles():
    """购电表小偏差在容差内：正常结算，偏差照实计入费用差额。"""
    records = copy.deepcopy(RECORDS)
    records[2]["grid_purchase_energy_kwh"] = 0.015  # 残差 0.005 ≤ 0.01
    r = _settle(records)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "settled"
    p2 = data["periods"][2]
    assert p2["grid_purchase_delta_kwh"] == 0.005
    assert p2["energy_cost_delta_yuan"] == 0.005  # 电价 1.0
    assert data["summary"]["actual_energy_cost_yuan"] == 0.025
    assert data["summary"]["energy_cost_delta_yuan"] == 0.005


def test_audit_anomaly_freezes_that_and_later_records():
    """t2 购电量残差超容差：t0/t1 已结算，t2 起（含）全部待结算。"""
    records = copy.deepcopy(RECORDS)
    records[2]["grid_purchase_energy_kwh"] = 0.11  # 期望 0.01，残差 0.1
    r = _settle(records)
    assert r.status_code == 200, r.text
    data = r.json()

    assert data["status"] == "partially_settled"
    assert data["first_anomaly_index"] == 2
    assert data["anomalous_records"] == [2]
    assert data["settled_periods"] == 2
    assert data["pending_periods"] == 2

    statuses = [p["status"] for p in data["periods"]]
    assert statuses == ["settled", "settled", "pending", "pending"]

    p1, p2, p3 = data["periods"][1:]
    assert p1["actual_total_cost_yuan"] == 0.03  # 0.01 能量 + 0.02 衰减
    assert p2["actual_energy_cost_yuan"] is None
    assert p2["total_cost_delta_yuan"] is None
    # 待结算仍回显计划口径
    assert p2["planned_total_cost_yuan"] == 0.03
    assert p3["actual_total_cost_yuan"] is None
    assert any("购电量残差" in x for x in p2["reasons"])
    assert any("冻结" in x for x in p3["reasons"])

    # 汇总只覆盖有效前缀：t0 衰减 0.04 + t1 费用 0.03 = 0.07
    summary = data["summary"]
    assert summary["settled_periods"] == 2
    assert summary["actual_total_cost_yuan"] == 0.07
    assert summary["planned_total_cost_yuan"] == 0.07
    assert summary["total_cost_delta_yuan"] == 0.0


def test_soc_breakpoint_with_self_consistent_record():
    """遥测本身自洽，但末态 SOC 偏离计划轨迹：判 SOC 断点并冻结。"""
    records = copy.deepcopy(RECORDS)
    # t3 计划静置（0→0），实际谷时多充 0.1 电芯，能量守恒且购电平衡
    records[3] = dict(
        base_load_kw=0.2, pv_kw=0.0,
        charge_ac_energy_kwh=round(0.1 / 0.9, 6), discharge_ac_energy_kwh=0.0,
        grid_purchase_energy_kwh=round(0.1 + 0.1 / 0.9, 6),
        soc_start_kwh=0.0, soc_end_kwh=0.1,
    )
    r = _settle(records)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "partially_settled"
    assert data["first_anomaly_index"] == 3
    assert data["soc_breakpoint_records"] == [3]
    # 遥测审计本身无异常（衔接、守恒、购电量都自洽）
    assert data["anomalous_records"] == []
    assert data["settled_periods"] == 3
    p3 = data["periods"][3]
    assert p3["status"] == "pending"
    assert p3["soc_gap_end_kwh"] == 0.1
    assert p3["audit"]["soc_breakpoint"] is True
    assert any("末态 SOC 偏离计划" in x for x in p3["reasons"])


def test_soc_breakpoint_midstream_also_flags_continuity():
    """t2 首态被挪走：既是 SOC 断点，也令 t2/t3 遥测衔接异常。"""
    records = copy.deepcopy(RECORDS)
    records[2] = dict(records[2], soc_start_kwh=0.15, soc_end_kwh=0.05)
    r = _settle(records)
    data = r.json()
    assert data["status"] == "partially_settled"
    assert data["first_anomaly_index"] == 2
    assert data["soc_breakpoint_records"] == [2]
    assert data["anomalous_records"] == [2, 3]
    assert data["periods"][0]["status"] == "settled"
    assert data["periods"][2]["status"] == "pending"


def test_actual_load_deviation_settles_with_cost_delta():
    """预测偏差（实际负荷更高）但执行轨迹不变：全额结算，费用走实际购电量。"""
    records = copy.deepcopy(RECORDS)
    records[3] = dict(
        base_load_kw=0.4, pv_kw=0.0,
        charge_ac_energy_kwh=0.0, discharge_ac_energy_kwh=0.0,
        grid_purchase_energy_kwh=0.2,
        soc_start_kwh=0.0, soc_end_kwh=0.0,
    )
    r = _settle(records)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "settled"
    p3 = data["periods"][3]
    assert p3["grid_purchase_delta_kwh"] == 0.1
    # t3 谷电电价为 0：多购电不多花钱
    assert p3["energy_cost_delta_yuan"] == 0.0
    assert p3["audit"]["anomalous"] is False


def test_degradation_cost_on_actual_throughput():
    """衰减费按实测电芯吞吐量计：容差内少放 0.005kWh，衰减费与吞吐量同步下降。"""
    dispatch = dict(
        prices=[0.0, 1.0, 0.0],
        base_load_kw=[0.2, 0.2, 0.2],
        pv_kw=[0.0, 0.0, 0.0],
        capacity_kwh=0.2,
        max_charge_kw=1.0,
        max_discharge_kw=1.0,
        charge_efficiency=1.0,
        discharge_efficiency=1.0,
        initial_soc_kwh=0.0,
        min_terminal_soc_kwh=0.0,
        degradation_cost_per_kwh=0.2,
    )
    plan = client.post("/dispatch", json=dispatch).json()
    # 计划：t0 充 0.1（0→0.1），t1 放 0.1（0.1→0），t2 静置。
    assert [p["action"] for p in plan["periods"]] == ["charge", "discharge", "idle"]

    # 实际 t1 只放 0.095：末态 SOC=0.005（偏离计划 0.005 ≤ 容差 0.01），
    # 购电 0.005 与功率平衡一致；t2 首/末态 0.005 同样在容差内 → 全部可结算。
    records = [
        dict(base_load_kw=0.2, pv_kw=0.0,
             charge_ac_energy_kwh=0.1, discharge_ac_energy_kwh=0.0,
             grid_purchase_energy_kwh=0.2,
             soc_start_kwh=0.0, soc_end_kwh=0.1),
        dict(base_load_kw=0.2, pv_kw=0.0,
             charge_ac_energy_kwh=0.0, discharge_ac_energy_kwh=0.095,
             grid_purchase_energy_kwh=0.005,
             soc_start_kwh=0.1, soc_end_kwh=0.005),
        dict(base_load_kw=0.2, pv_kw=0.0,
             charge_ac_energy_kwh=0.0, discharge_ac_energy_kwh=0.0,
             grid_purchase_energy_kwh=0.1,
             soc_start_kwh=0.005, soc_end_kwh=0.005),
    ]
    r = _settle(records, tol=0.01, plan=plan, dispatch=dispatch)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "settled"
    p1 = data["periods"][1]
    assert p1["actual_battery_throughput_kwh"] == 0.095
    assert p1["actual_degradation_cost_yuan"] == 0.019
    assert p1["degradation_cost_delta_yuan"] == -0.001
    assert p1["energy_cost_delta_yuan"] == 0.005  # 多购 0.005kWh × 电价 1
    summary = data["summary"]
    assert summary["actual_battery_throughput_kwh"] == 0.195
    assert summary["throughput_delta_kwh"] == -0.005
    assert summary["actual_degradation_cost_yuan"] == 0.039


def test_tampered_period_snapshot_rejected():
    """篡改快照任一时段字段 → plan_snapshot_mismatch。"""
    plan = _plan()
    tampered = copy.deepcopy(plan)
    tampered["periods"][0]["grid_purchase_energy_kwh"] = 0.99
    r = _settle(copy.deepcopy(RECORDS), plan=tampered)
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "plan_snapshot_mismatch"
    assert "grid_purchase_energy_kwh" in body["reason"]

    tampered = copy.deepcopy(plan)
    tampered["periods"][1]["action"] = "charge"
    r = _settle(copy.deepcopy(RECORDS), plan=tampered)
    assert r.status_code == 422
    assert r.json()["error"] == "plan_snapshot_mismatch"


def test_tampered_summary_snapshot_rejected():
    """篡改快照 summary → plan_snapshot_mismatch。"""
    plan = _plan()
    for field, value in (
        ("total_cost_yuan", 9.9),
        ("battery_throughput_kwh", 1.7),
        ("num_periods", 3),
    ):
        tampered = copy.deepcopy(plan)
        tampered["summary"][field] = value
        r = _settle(copy.deepcopy(RECORDS), plan=tampered)
        assert r.status_code == 422, (field, r.text)
        assert r.json()["error"] == "plan_snapshot_mismatch"


def test_snapshot_verified_against_dispatched_input_not_records():
    """快照与 dispatch 输入（而非实测记录）重算比对：改 dispatch 使重算不同即拒绝。"""
    plan = _plan()
    other_dispatch = copy.deepcopy(DISPATCH)
    other_dispatch["prices"] = [5.0, 5.0, 5.0, 5.0]
    r = _settle(copy.deepcopy(RECORDS), plan=plan, dispatch=other_dispatch)
    assert r.status_code == 422
    assert r.json()["error"] == "plan_snapshot_mismatch"


def test_length_mismatch_rejected():
    """实测记录长度与计划时段数不符 → 整体拒绝。"""
    r = _settle(copy.deepcopy(RECORDS[:3]))
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "invalid_input"
    assert "不符" in body["reason"]

    r = _settle(copy.deepcopy(RECORDS) + copy.deepcopy(RECORDS))
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_input"


def test_no_valid_interval_rejected():
    """首条记录即异常 → 无有效区间，整体拒绝 no_valid_interval。"""
    records = copy.deepcopy(RECORDS)
    records[0]["soc_end_kwh"] = 0.05
    r = _settle(records)
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "no_valid_interval"
    assert "下标 0" in body["reason"]

    # 首条就是 SOC 断点（首态与计划初始 SOC 不符）同样拒绝
    records = copy.deepcopy(RECORDS)
    records[0]["soc_start_kwh"] = 0.3
    r = _settle(records)
    assert r.status_code == 422
    assert r.json()["error"] == "no_valid_interval"


def test_recomputed_infeasible_input_rejected():
    """重算时发现输入无解 → infeasible，不进入结算。"""
    bad_dispatch = copy.deepcopy(DISPATCH)
    bad_dispatch["max_charge_kw"] = 0.0
    bad_dispatch["min_terminal_soc_kwh"] = 0.4
    r = _settle(copy.deepcopy(RECORDS), plan=_plan(), dispatch=bad_dispatch)
    assert r.status_code == 422
    assert r.json()["error"] == "infeasible"


def test_invalid_scalars_and_measurements_rejected():
    """容差非法、测量值非法等沿用既有校验 → 422 invalid_input。"""
    assert _settle(copy.deepcopy(RECORDS), tol=-0.1).status_code == 422

    raw = json.dumps({
        "dispatch": DISPATCH,
        "plan_snapshot": _plan(),
        "settlement_tolerance_kwh": float("nan"),
        "records": RECORDS,
    })
    r = client.post(
        "/dispatch/settlement",
        content=raw,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422

    records = copy.deepcopy(RECORDS)
    records[1]["grid_purchase_energy_kwh"] = -0.01
    assert _settle(records).status_code == 422

    bad_dispatch = copy.deepcopy(DISPATCH)
    bad_dispatch["prices"] = [0.0, 0.0, 0.0]  # dispatch 内部不等长
    assert _settle(copy.deepcopy(RECORDS), dispatch=bad_dispatch).status_code == 422


def test_existing_endpoints_unchanged():
    """新增结算接口不影响 /dispatch、/telemetry/audit 与 /healthz。"""
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
