"""POST /telemetry/audit 行为测试：pytest tests/ -q"""

import json

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _post(payload):
    return client.post("/telemetry/audit", json=payload)


def _post_raw(payload):
    """用宽松 JSON 序列化发送（httpx 的 json= 拒绝 NaN/Inf，而服务端必须能拦截它们）。"""
    return client.post(
        "/telemetry/audit",
        content=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )


BASE_AUDIT = dict(
    capacity_kwh=10.0,
    charge_efficiency=0.9,
    discharge_efficiency=0.9,
    energy_tolerance_kwh=0.05,
)

# 两条完全自洽的记录：
# t0 净负荷 (1.0-0.4)*0.5=0.3，充电 0.5 → 购电 0.8；电芯充入 0.5*0.9=0.45 → SOC 2.0→2.45
# t1 净负荷 1.0，放电 0.45 → 购电 0.55；电芯放出 0.45/0.9=0.5 → SOC 2.45→1.95
CLEAN_RECORDS = [
    dict(
        base_load_kw=1.0, pv_kw=0.4,
        charge_ac_energy_kwh=0.5, discharge_ac_energy_kwh=0.0,
        grid_purchase_energy_kwh=0.8,
        soc_start_kwh=2.0, soc_end_kwh=2.45,
    ),
    dict(
        base_load_kw=2.0, pv_kw=0.0,
        charge_ac_energy_kwh=0.0, discharge_ac_energy_kwh=0.45,
        grid_purchase_energy_kwh=0.55,
        soc_start_kwh=2.45, soc_end_kwh=1.95,
    ),
]


def test_clean_records_pass():
    r = _post(dict(BASE_AUDIT, records=CLEAN_RECORDS))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["num_records"] == 2
    assert data["period_hours"] == 0.5
    assert data["anomalous"] is False
    assert data["anomalous_records"] == []

    rec0, rec1 = data["records"]
    # 效率核算：充 0.5 交流 → 0.45 电芯；放 0.45 交流 → 0.5 电芯
    assert rec0["expected_soc_end_kwh"] == 2.45
    assert rec1["expected_soc_end_kwh"] == 1.95
    assert rec0["expected_grid_purchase_energy_kwh"] == 0.8
    assert rec1["expected_grid_purchase_energy_kwh"] == 0.55
    for rec in (rec0, rec1):
        assert abs(rec["soc_residual_kwh"]) < 1e-6
        assert abs(rec["grid_residual_kwh"]) < 1e-6
        assert abs(rec["soc_continuity_gap_kwh"]) < 1e-6
        assert rec["anomalous"] is False
        assert rec["reasons"] == []


def test_soc_residual_flagged():
    """末态 SOC 与充放电核算不符 → 电芯能量残差异常，仍返回 200。"""
    records = [dict(CLEAN_RECORDS[0]), dict(CLEAN_RECORDS[1], soc_end_kwh=2.1)]
    r = _post(dict(BASE_AUDIT, records=records))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["anomalous"] is True
    assert data["anomalous_records"] == [1]
    rec1 = data["records"][1]
    assert rec1["soc_residual_kwh"] == 0.15
    assert rec1["anomalous"] is True
    assert any("电芯能量残差" in reason for reason in rec1["reasons"])
    # 审计不修正读数：响应只含残差与核算值，无改写后的记录
    assert "soc_end_kwh" not in rec1


def test_grid_residual_flagged():
    """购电量与功率平衡不符 → 购电量残差异常。"""
    records = [dict(CLEAN_RECORDS[0], grid_purchase_energy_kwh=0.9), CLEAN_RECORDS[1]]
    r = _post(dict(BASE_AUDIT, records=records))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["anomalous_records"] == [0]
    rec0 = data["records"][0]
    assert rec0["grid_residual_kwh"] == 0.1
    assert any("购电量残差" in reason for reason in rec0["reasons"])


def test_soc_continuity_gap_flagged():
    """相邻记录首末 SOC 不衔接 → 异常并给出衔接差。"""
    records = [CLEAN_RECORDS[0], dict(CLEAN_RECORDS[1], soc_start_kwh=2.6)]
    r = _post(dict(BASE_AUDIT, records=records))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["anomalous_records"] == [1]
    rec1 = data["records"][1]
    assert rec1["soc_continuity_gap_kwh"] == 0.15
    assert any("不衔接" in reason for reason in rec1["reasons"])


def test_residual_equal_to_tolerance_not_flagged():
    """残差绝对值恰好等于容差不算超差（严格大于才判异常）。"""
    payload = dict(
        BASE_AUDIT,
        charge_efficiency=1.0,
        discharge_efficiency=1.0,
        energy_tolerance_kwh=0.5,
        records=[
            dict(
                base_load_kw=0.0, pv_kw=0.0,
                charge_ac_energy_kwh=1.0, discharge_ac_energy_kwh=0.0,
                grid_purchase_energy_kwh=1.0,
                soc_start_kwh=0.0, soc_end_kwh=1.5,  # 期望 1.0，残差恰为 0.5
            )
        ],
    )
    r = _post(payload)
    assert r.status_code == 200, r.text
    assert r.json()["anomalous"] is False

    payload["records"][0]["soc_end_kwh"] = 1.6  # 残差 0.6 > 0.5
    r = _post(payload)
    assert r.status_code == 200, r.text
    assert r.json()["anomalous"] is True


def test_pv_surplus_expected_grid_clamped_to_zero():
    """光伏盈余时期望购电量截断为 0，实测为 0 即自洽。"""
    payload = dict(
        BASE_AUDIT,
        records=[
            dict(
                base_load_kw=0.2, pv_kw=2.0,  # 盈余 0.9kWh
                charge_ac_energy_kwh=0.0, discharge_ac_energy_kwh=0.0,
                grid_purchase_energy_kwh=0.0,
                soc_start_kwh=1.0, soc_end_kwh=1.0,
            )
        ],
    )
    r = _post(payload)
    assert r.status_code == 200, r.text
    rec = r.json()["records"][0]
    assert rec["expected_grid_purchase_energy_kwh"] == 0.0
    assert r.json()["anomalous"] is False


def test_invalid_scalars_rejected():
    bad_payloads = [
        dict(BASE_AUDIT, capacity_kwh=0.0, records=CLEAN_RECORDS),      # 容量须为正
        dict(BASE_AUDIT, capacity_kwh=-1.0, records=CLEAN_RECORDS),
        dict(BASE_AUDIT, capacity_kwh=float("inf"), records=CLEAN_RECORDS),
        dict(BASE_AUDIT, charge_efficiency=0.0, records=CLEAN_RECORDS),  # 效率 (0,1]
        dict(BASE_AUDIT, charge_efficiency=1.2, records=CLEAN_RECORDS),
        dict(BASE_AUDIT, discharge_efficiency=-0.5, records=CLEAN_RECORDS),
        dict(BASE_AUDIT, energy_tolerance_kwh=-0.1, records=CLEAN_RECORDS),
        dict(BASE_AUDIT, energy_tolerance_kwh=float("nan"), records=CLEAN_RECORDS),
        dict(BASE_AUDIT, records=[]),                                    # 至少一条记录
    ]
    for payload in bad_payloads:
        r = _post_raw(payload)
        assert r.status_code == 422, payload
        assert r.json()["error"] == "invalid_input"


def test_invalid_record_values_rejected():
    bad_records = [
        dict(CLEAN_RECORDS[0], base_load_kw=-0.1),                    # 负测量值
        dict(CLEAN_RECORDS[0], grid_purchase_energy_kwh=float("nan")),  # 非有限
        dict(CLEAN_RECORDS[0], pv_kw=float("inf")),
        dict(CLEAN_RECORDS[0], soc_start_kwh=10.5),                   # SOC 超容量
        dict(CLEAN_RECORDS[0], soc_end_kwh=10.5),
    ]
    for rec in bad_records:
        r = _post_raw(dict(BASE_AUDIT, records=[rec]))
        assert r.status_code == 422, rec
        body = r.json()
        assert body["error"] == "invalid_input"
        assert "第 0 条记录" in body["reason"]


def test_dispatch_endpoint_untouched():
    """新增审计接口不影响既有 /dispatch。"""
    r = client.post(
        "/dispatch",
        json=dict(
            prices=[1.0, 0.0],
            base_load_kw=[0.2, 0.2],
            pv_kw=[0.0, 0.0],
            capacity_kwh=0.2,
            max_charge_kw=1.0,
            max_discharge_kw=1.0,
            charge_efficiency=1.0,
            discharge_efficiency=1.0,
            initial_soc_kwh=0.0,
            min_terminal_soc_kwh=0.0,
            degradation_cost_per_kwh=0.0,
        ),
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["periods"]) == 2
