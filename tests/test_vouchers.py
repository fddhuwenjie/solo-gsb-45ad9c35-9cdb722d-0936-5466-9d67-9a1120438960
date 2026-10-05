"""逐版结算凭证（vouchers）行为测试。

覆盖：
- 修订号 0 用最初上报记录出首张全额凭证，凭证含修订号、结算摘要、实测总费用、
  出单时间与完整结算报告；
- 更正后对当前修订出第二张凭证：应补 / 应退 / 持平差额相对上一张已出凭证，
  差额口径等于两版直接提交 /dispatch/settlement 的实测总费用之差；
- 允许跳过未出单的中间修订：序号按出单次序连续，差额只相对上一张已出凭证；
- 同一修订号重试幂等返回原凭证（voucher_id / 出单时间 / 结算结果全部冻结），
  即使后来发生更正；后续更正后重试旧修订号仍返回原凭证而不是冲突；
- 未出单的过期修订号、超前修订号 → 409 voucher_revision_conflict 及当前号；
- 该版台账只能结算有效前缀（SOC 断点 / 审计异常）→ 422
  voucher_partial_settlement，首条即不可结算 → voucher_no_valid_interval，
  整次拒绝且不留下凭证（待出单状态保持 true）；
- 未满期 → 422 voucher_not_complete，未知会话 → 404，期望修订号为负 → 422；
- 按出单次序查询凭证与当前修订待出单状态，更正后待出单重新置为 true；
- 凭证持久化在 SQLite，按同一路径重建存储模拟进程重启后仍可查询、幂等重试；
- 并发同修订号出单只有一份生效（其余 deduped，同 voucher_id / 序号）；
  并发更正与出单不会产生引用过期台账的凭证，失败不留部分数据；
- 原会话、修订、结算等既有接口行为不变。
"""

import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.storage import configure_store

client = TestClient(app)


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

# 与原计划自洽的完整 4 段实测（计划：t0 充 0.2、t1/t2 各放 0.1、t3 静置）。
# 实测总费用：t1/t2 各 0.01 元能量费 + 0.02 元衰减费，t3 能量费 0.1×0=0，
# 充放电吞吐量 0.4 × 0.2 = 0.08 元；合计 0.10 元。
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


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    configure_store(str(tmp_path / "vouchers.db"))
    yield
    configure_store(None)


def _plan():
    return client.post("/dispatch", json=DISPATCH).json()


def _create_complete(tol=TOL):
    """创建会话并把 RECORDS 分两批追加成满期会话，返回 (sid, detail)。"""
    sid = client.post(
        "/dispatch/sessions",
        json={"dispatch": DISPATCH, "plan_snapshot": _plan(), "audit_tolerance_kwh": tol},
    ).json()["session_id"]
    assert client.post(
        f"/dispatch/sessions/{sid}/batches",
        json={"start_index": 0, "records": RECORDS[:2]},
    ).status_code == 200
    assert client.post(
        f"/dispatch/sessions/{sid}/batches",
        json={"start_index": 2, "records": RECORDS[2:]},
    ).status_code == 200
    detail = client.get(f"/dispatch/sessions/{sid}").json()
    assert detail["status"] == "complete"
    return sid, detail


def _issue(sid, expected_revision):
    return client.post(
        f"/dispatch/sessions/{sid}/vouchers",
        json={"expected_revision": expected_revision},
    )


def _list(sid):
    return client.get(f"/dispatch/sessions/{sid}/vouchers")


def _submit_revision(sid, expected_revision, changes):
    return client.post(
        f"/dispatch/sessions/{sid}/revisions",
        json={"expected_revision": expected_revision, "changes": changes},
    )


def _settlement_total(records):
    """直接提交现有结算接口，返回实测总费用（凭证差额口径的对照）。"""
    r = client.post(
        "/dispatch/settlement",
        json={
            "dispatch": DISPATCH,
            "plan_snapshot": _plan(),
            "settlement_tolerance_kwh": TOL,
            "records": records,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["summary"]["actual_total_cost_yuan"]


def test_first_voucher_revision_zero_full_amount():
    """修订号 0 出首张凭证：全额、序号 1、含全部必备字段与完整结算报告。"""
    sid, _ = _create_complete()
    # 出单前：列表可用，0 张，当前修订 0 待出单
    lst = _list(sid).json()
    assert lst["voucher_count"] == 0
    assert lst["current_revision"] == 0
    assert lst["current_revision_pending_issuance"] is True
    assert lst["current_revision_voucher"] is None

    r = _issue(sid, 0)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["result"] == "issued"
    assert b["revision_no"] == 0
    assert b["sequence_no"] == 1
    assert b["session_id"] == sid
    assert isinstance(b["voucher_id"], str) and len(b["voucher_id"]) == 36
    assert b["issued_at"].endswith("Z")
    # 首张记全额
    billing = b["billing"]
    assert billing["type"] == "full"
    assert billing["delta_yuan"] is None
    assert billing["previous_voucher_id"] is None
    assert billing["amount_yuan"] == b["actual_total_cost_yuan"] == 0.10
    # 结算摘要与实测总费用
    s = b["settlement_summary"]
    assert s["status"] == "settled"
    assert s["settled_periods"] == 4
    assert s["pending_periods"] == 0
    assert s["first_anomaly_index"] is None
    assert s["anomalous_records"] == []
    assert s["actual_total_cost_yuan"] == 0.10
    # 完整结算报告随凭证冻结（逐时段可追溯）
    rep = b["settlement"]
    assert rep["status"] == "settled"
    assert len(rep["periods"]) == 4
    assert all(p["status"] == "settled" for p in rep["periods"])

    # 出单后：当前修订已出单，待出单标志翻为 false
    lst = _list(sid).json()
    assert lst["voucher_count"] == 1
    assert lst["current_revision_pending_issuance"] is False
    assert lst["current_revision_voucher"]["voucher_id"] == b["voucher_id"]
    assert lst["current_revision_voucher"]["sequence_no"] == 1


def test_retry_same_revision_returns_original_voucher_frozen():
    """同一修订号重试返回原凭证，全部字段冻结。"""
    sid, _ = _create_complete()
    first = _issue(sid, 0).json()

    again = _issue(sid, 0)
    assert again.status_code == 200
    b = again.json()
    assert b["result"] == "deduped"
    assert b["voucher_id"] == first["voucher_id"]
    assert b["issued_at"] == first["issued_at"]
    assert b["sequence_no"] == 1
    assert b["actual_total_cost_yuan"] == 0.10
    assert b["billing"] == first["billing"]
    assert b["settlement"] == first["settlement"]

    # 列表中只有一张
    assert _list(sid).json()["voucher_count"] == 1


def test_revision_then_voucher_supplement_delta():
    """更正使 t1 购电量 0.01→0.02：第二张凭证列应补差额。"""
    sid, _ = _create_complete()
    v0 = _issue(sid, 0).json()

    new1 = dict(RECORDS[1])
    new1["base_load_kw"] = 0.22
    new1["grid_purchase_energy_kwh"] = 0.02
    assert _submit_revision(sid, 0, [{"index": 1, "record": new1}]).status_code == 200

    # 当前修订变 1，且有新的待出单凭证
    lst = _list(sid).json()
    assert lst["current_revision"] == 1
    assert lst["current_revision_pending_issuance"] is True

    r = _issue(sid, 1)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["result"] == "issued"
    assert b["sequence_no"] == 2
    assert b["actual_total_cost_yuan"] == pytest.approx(0.11)
    billing = b["billing"]
    assert billing["type"] == "supplement"
    assert billing["delta_yuan"] == pytest.approx(0.01)
    assert billing["amount_yuan"] == pytest.approx(0.01)
    assert billing["previous_voucher_id"] == v0["voucher_id"]
    assert billing["previous_revision_no"] == 0
    assert billing["previous_sequence_no"] == 1

    # 差额与两版直接走 /dispatch/settlement 的费用差一致
    total0 = _settlement_total(RECORDS)
    revised = [dict(RECORDS[0]), new1, RECORDS[2], RECORDS[3]]
    total1 = _settlement_total(revised)
    assert total1 - total0 == pytest.approx(0.01)

    lst = _list(sid).json()
    assert [v["sequence_no"] for v in lst["vouchers"]] == [1, 2]
    assert [v["revision_no"] for v in lst["vouchers"]] == [0, 1]
    assert lst["current_revision_pending_issuance"] is False


def test_voucher_refund_delta_dedicated_session():
    """应退差额：最初上报的 t1 表计读数偏高（购电 0.02），更正核减为计划口径 0.01
    （SOC 与充放电轨迹不变，只改 base_load/grid 保持台账自洽），费用 0.11→0.10。"""
    # 原始上报：t1 基础负荷 0.22kW、购电 0.02kWh（核算期望 0.11−0.09=0.02，自洽；
    # 相对计划只是购电量偏差，照常全额计费），其余时段同 RECORDS。
    over1 = dict(RECORDS[1])
    over1.update(base_load_kw=0.22, grid_purchase_energy_kwh=0.02)
    original = [dict(RECORDS[0]), over1, dict(RECORDS[2]), dict(RECORDS[3])]

    sid = client.post(
        "/dispatch/sessions",
        json={"dispatch": dict(DISPATCH), "plan_snapshot": _plan(), "audit_tolerance_kwh": TOL},
    ).json()["session_id"]
    assert client.post(
        f"/dispatch/sessions/{sid}/batches",
        json={"start_index": 0, "records": original[:2]},
    ).status_code == 200
    assert client.post(
        f"/dispatch/sessions/{sid}/batches",
        json={"start_index": 2, "records": original[2:]},
    ).status_code == 200

    v0 = _issue(sid, 0).json()
    assert v0["actual_total_cost_yuan"] == pytest.approx(0.11)

    # 更正核减 t1 购电表读数（负荷/购电同步回到计划口径，SOC 轨迹不变）
    fixed1 = dict(RECORDS[1])
    assert _submit_revision(sid, 0, [{"index": 1, "record": fixed1}]).status_code == 200
    v1 = _issue(sid, 1).json()
    assert v1["actual_total_cost_yuan"] == pytest.approx(0.10)
    assert v1["billing"]["type"] == "refund"
    assert v1["billing"]["delta_yuan"] == pytest.approx(-0.01)
    assert v1["billing"]["amount_yuan"] == pytest.approx(0.01)
    assert v1["billing"]["previous_voucher_id"] == v0["voucher_id"]


def test_voucher_no_change_when_equal_total():
    """两版实测总费用持平时 billing=no_change，差额 0。

    用「费用中性」的表计更正：t1 同步上调负荷与光伏（净购电不变、SOC 轨迹不变），
    实测总费用不变；rev1 相对 rev0 即 no_change。
    """
    sid, _ = _create_complete()
    _issue(sid, 0)
    neutral1 = dict(RECORDS[1])
    neutral1.update(base_load_kw=0.22, pv_kw=0.02)  # 净购电仍 0.01，台账自洽
    assert _submit_revision(sid, 0, [{"index": 1, "record": neutral1}]).status_code == 200
    b = _issue(sid, 1).json()
    assert b["actual_total_cost_yuan"] == pytest.approx(0.10)
    assert b["billing"]["type"] == "no_change"
    assert b["billing"]["delta_yuan"] == 0.0
    assert b["billing"]["amount_yuan"] == 0.0
    assert b["billing"]["previous_revision_no"] == 0


def test_skip_unissued_intermediate_revision():
    """允许跳过未出单的中间修订：差额相对上一张已出凭证，序号连续。"""
    sid, _ = _create_complete()
    v0 = _issue(sid, 0).json()

    new1 = dict(RECORDS[1])
    new1["base_load_kw"] = 0.22
    new1["grid_purchase_energy_kwh"] = 0.02
    assert _submit_revision(sid, 0, [{"index": 1, "record": new1}]).status_code == 200  # rev1
    back1 = dict(RECORDS[1])
    assert _submit_revision(sid, 1, [{"index": 1, "record": back1}]).status_code == 200  # rev2

    # 未出过单的 rev1 现在已过期 → 409 + 当前号，不留凭证
    stale = _issue(sid, 1)
    assert stale.status_code == 409
    assert stale.json()["error"] == "voucher_revision_conflict"
    assert stale.json()["current_revision"] == 2
    assert _list(sid).json()["voucher_count"] == 1

    # 直接为当前修订 rev2 出单：序号 2，差额相对唯一已出的 rev0
    b = _issue(sid, 2).json()
    assert b["sequence_no"] == 2
    assert b["revision_no"] == 2
    assert b["billing"]["previous_revision_no"] == 0
    assert b["billing"]["previous_voucher_id"] == v0["voucher_id"]
    assert b["billing"]["type"] == "no_change"

    lst = _list(sid).json()
    assert [(v["sequence_no"], v["revision_no"]) for v in lst["vouchers"]] == [(1, 0), (2, 2)]


def test_stale_revision_after_correction_returns_conflict_and_current():
    """未出单的过期修订号返回 409 及当前号。"""
    sid, _ = _create_complete()
    new1 = dict(RECORDS[1])
    new1["base_load_kw"] = 0.22
    new1["grid_purchase_energy_kwh"] = 0.02
    assert _submit_revision(sid, 0, [{"index": 1, "record": new1}]).status_code == 200

    r = _issue(sid, 0)
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "voucher_revision_conflict"
    assert body["current_revision"] == 1
    assert _list(sid).json()["voucher_count"] == 0


def test_retry_old_revision_after_new_correction_returns_original():
    """出过单的修订号后来发生更正：重试返回原凭证（冻结），不是冲突。"""
    sid, _ = _create_complete()
    first = _issue(sid, 0).json()
    new1 = dict(RECORDS[1])
    new1["base_load_kw"] = 0.22
    new1["grid_purchase_energy_kwh"] = 0.02
    assert _submit_revision(sid, 0, [{"index": 1, "record": new1}]).status_code == 200

    again = _issue(sid, 0)
    assert again.status_code == 200
    b = again.json()
    assert b["result"] == "deduped"
    assert b["voucher_id"] == first["voucher_id"]
    assert b["actual_total_cost_yuan"] == first["actual_total_cost_yuan"] == 0.10
    assert b["issued_at"] == first["issued_at"]
    # 列表中 rev0 凭证仍是冻结的原凭证
    stored = [v for v in _list(sid).json()["vouchers"] if v["revision_no"] == 0][0]
    assert stored["voucher_id"] == first["voucher_id"]
    assert stored["actual_total_cost_yuan"] == 0.10


def test_future_revision_conflict():
    """超前修订号 → 409 并附当前号。"""
    sid, _ = _create_complete()
    r = _issue(sid, 3)
    assert r.status_code == 409
    assert r.json()["error"] == "voucher_revision_conflict"
    assert r.json()["current_revision"] == 0
    assert _list(sid).json()["voucher_count"] == 0


def test_partial_settlement_rejected_without_voucher():
    """修订台账自洽但偏离计划 SOC 轨迹：只能结算前缀 → 整次拒绝、不留凭证。"""
    sid, _ = _create_complete()
    # 从 t1 起 SOC 改为另一条自洽链（审计通过、SOC 断点自 t1）
    n1 = dict(RECORDS[1])
    n1.update(
        discharge_ac_energy_kwh=0.045, grid_purchase_energy_kwh=0.055,
        soc_start_kwh=0.2, soc_end_kwh=0.15,
    )
    n2 = dict(RECORDS[2])
    n2.update(soc_start_kwh=0.15, soc_end_kwh=0.05)
    n3 = dict(RECORDS[3])
    n3.update(soc_start_kwh=0.05, soc_end_kwh=0.05)
    assert _submit_revision(sid, 0, [
        {"index": 1, "record": n1},
        {"index": 2, "record": n2},
        {"index": 3, "record": n3},
    ]).status_code == 200

    r = _issue(sid, 1)
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "voucher_partial_settlement"
    assert body["first_anomaly_index"] == 1
    assert body["settled_periods"] == 1
    assert body["pending_periods"] == 3

    # 没有任何凭证落盘，当前修订仍待出单；可在修正台账后重新出单
    lst = _list(sid).json()
    assert lst["voucher_count"] == 0
    assert lst["current_revision_pending_issuance"] is True


def test_no_valid_interval_rejected_without_voucher():
    """首条记录即 SOC 断点 → voucher_no_valid_interval，整次拒绝不留凭证。"""
    sid, _ = _create_complete()
    # t0 只充到 0.1（计划 0.2），后续链接 0.1：首条即断点
    m0 = dict(RECORDS[0])
    m0.update(
        charge_ac_energy_kwh=round(0.1 / 0.9, 6),
        grid_purchase_energy_kwh=round(0.1 + 0.1 / 0.9, 6),
        soc_end_kwh=0.1,
    )
    m1 = dict(RECORDS[1])
    m1.update(soc_start_kwh=0.1, soc_end_kwh=0.0)
    m2 = dict(RECORDS[2])
    m2.update(
        charge_ac_energy_kwh=0.0, discharge_ac_energy_kwh=0.0,
        grid_purchase_energy_kwh=0.1, soc_start_kwh=0.0, soc_end_kwh=0.0,
    )
    m3 = dict(RECORDS[3])
    assert _submit_revision(sid, 0, [
        {"index": 0, "record": m0},
        {"index": 1, "record": m1},
        {"index": 2, "record": m2},
        {"index": 3, "record": m3},
    ]).status_code == 200

    r = _issue(sid, 1)
    assert r.status_code == 422
    assert r.json()["error"] == "voucher_no_valid_interval"
    assert _list(sid).json()["voucher_count"] == 0


def test_failed_issuance_then_fix_and_issue_succeeds():
    """前缀结算被拒后（无凭证），后续更正在同一修订号... 修订号只增不变，
    因此提交 rev2 修好台账后可正常出单，序号仍从 1 开始。"""
    sid, _ = _create_complete()
    n1 = dict(RECORDS[1])
    n1.update(
        discharge_ac_energy_kwh=0.045, grid_purchase_energy_kwh=0.055,
        soc_end_kwh=0.15,
    )
    n2 = dict(RECORDS[2])
    n2.update(soc_start_kwh=0.15, soc_end_kwh=0.05)
    n3 = dict(RECORDS[3])
    n3.update(soc_start_kwh=0.05, soc_end_kwh=0.05)
    assert _submit_revision(sid, 0, [
        {"index": 1, "record": n1}, {"index": 2, "record": n2}, {"index": 3, "record": n3}
    ]).status_code == 200
    assert _issue(sid, 1).status_code == 422

    # rev2 改回原计划轨迹
    assert _submit_revision(sid, 1, [
        {"index": 1, "record": dict(RECORDS[1])},
        {"index": 2, "record": dict(RECORDS[2])},
        {"index": 3, "record": dict(RECORDS[3])},
    ]).status_code == 200
    b = _issue(sid, 2).json()
    assert b["result"] == "issued"
    assert b["sequence_no"] == 1  # 之前失败未占序号
    assert b["billing"]["type"] == "full"


def test_incomplete_session_rejected():
    """未满期会话不能出单 / 查询凭证。"""
    sid = client.post(
        "/dispatch/sessions",
        json={"dispatch": DISPATCH, "plan_snapshot": _plan(), "audit_tolerance_kwh": TOL},
    ).json()["session_id"]
    assert client.post(
        f"/dispatch/sessions/{sid}/batches",
        json={"start_index": 0, "records": RECORDS[:2]},
    ).status_code == 200

    r = _issue(sid, 0)
    assert r.status_code == 422
    assert r.json()["error"] == "voucher_not_complete"

    r = _list(sid)
    assert r.status_code == 422
    assert r.json()["error"] == "voucher_not_complete"


def test_unknown_session_404():
    assert _issue("does-not-exist", 0).status_code == 404
    assert _list("does-not-exist").status_code == 404


def test_negative_expected_revision_invalid():
    sid, _ = _create_complete()
    r = _issue(sid, -1)
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_input"


def test_pending_flag_tracks_corrections_and_issuance():
    """待出单状态：出单后 false，再更正重新 true。"""
    sid, _ = _create_complete()
    assert _list(sid).json()["current_revision_pending_issuance"] is True
    _issue(sid, 0)
    assert _list(sid).json()["current_revision_pending_issuance"] is False

    new1 = dict(RECORDS[1])
    new1["base_load_kw"] = 0.22
    new1["grid_purchase_energy_kwh"] = 0.02
    _submit_revision(sid, 0, [{"index": 1, "record": new1}])
    assert _list(sid).json()["current_revision_pending_issuance"] is True
    _issue(sid, 1)
    assert _list(sid).json()["current_revision_pending_issuance"] is False


def test_vouchers_persist_across_restart():
    """同一路径重建存储模拟进程重启：凭证仍可按序查询、幂等重试。"""
    db_path = None
    import app.storage as storage_mod

    sid, _ = _create_complete()
    first = _issue(sid, 0).json()
    db_path = storage_mod.get_store().db_path

    new1 = dict(RECORDS[1])
    new1["base_load_kw"] = 0.22
    new1["grid_purchase_energy_kwh"] = 0.02
    _submit_revision(sid, 0, [{"index": 1, "record": new1}])
    second = _issue(sid, 1).json()

    configure_store(db_path)  # 模拟进程重启
    lst = _list(sid).json()
    assert lst["voucher_count"] == 2
    assert lst["current_revision"] == 1
    assert lst["current_revision_pending_issuance"] is False
    assert [v["voucher_id"] for v in lst["vouchers"]] == [
        first["voucher_id"], second["voucher_id"]
    ]
    # 完整结算报告与差额仍在
    v1 = lst["vouchers"][1]
    assert v1["billing"]["type"] == "supplement"
    assert v1["settlement"]["status"] == "settled"

    again = _issue(sid, 0).json()
    assert again["result"] == "deduped"
    assert again["voucher_id"] == first["voucher_id"]
    assert again["issued_at"] == first["issued_at"]


def test_concurrent_issuance_same_revision_single_effect():
    """并发对同一修订号出单：只有一份生效，其余 deduped，同 ID 同序号。"""
    sid, _ = _create_complete()
    results = []

    def worker():
        r = _issue(sid, 0)
        results.append((r.status_code, r.json()))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(code == 200 for code, _ in results)
    assert {b["voucher_id"] for _, b in results} == {results[0][1]["voucher_id"]}
    assert {b["sequence_no"] for _, b in results} == {1}
    assert sorted(b["result"] for _, b in results).count("issued") == 1
    assert _list(sid).json()["voucher_count"] == 1


def test_concurrent_correction_and_issuance_no_stale_voucher():
    """并发更正与出单：不存在引用过期台账的凭证；失败不留部分数据。

    两种合法结局（写锁串行化）：
    - 出单先提交：rev0 凭证落盘（0.10 元），更正随后成功为 rev1；
    - 更正先提交：期望 rev0 的新出单（无既有凭证）返回 409。
    无论哪种，rev0 若有凭证其费用必为最初口径 0.10，且修订台账与凭证一致。
    """
    sid, _ = _create_complete()
    new1 = dict(RECORDS[1])
    new1["base_load_kw"] = 0.22
    new1["grid_purchase_energy_kwh"] = 0.02

    outcomes = {}

    def do_revision():
        r = _submit_revision(sid, 0, [{"index": 1, "record": new1}])
        outcomes["revision"] = r.status_code

    def do_voucher():
        r = _issue(sid, 0)
        outcomes["voucher"] = (r.status_code, r.json())

    t1 = threading.Thread(target=do_revision)
    t2 = threading.Thread(target=do_voucher)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert outcomes["revision"] == 200
    code, body = outcomes["voucher"]
    assert code in (200, 409)
    if code == 200:
        # 出单赢：凭证引用的是最初台账，费用必为 0.10
        assert body["revision_no"] == 0
        assert body["actual_total_cost_yuan"] == pytest.approx(0.10)
    else:
        # 更正赢：新出单按过期修订号被拒，未写任何凭证
        assert body["error"] == "voucher_revision_conflict"
        assert body["current_revision"] == 1
        assert _list(sid).json()["voucher_count"] == 0

    # 库内不存在「rev0 凭证费用却是修订后 0.11」这种过期引用
    for v in _list(sid).json()["vouchers"]:
        if v["revision_no"] == 0:
            assert v["actual_total_cost_yuan"] == pytest.approx(0.10)

    # 当前修订 rev1 仍可正常出单（失败事务没有污染序号 / 状态）
    b = _issue(sid, 1).json()
    assert b["result"] == "issued"
    if code == 200:
        assert b["sequence_no"] == 2
        assert b["billing"]["type"] == "supplement"
    else:
        assert b["sequence_no"] == 1
        assert b["billing"]["type"] == "full"


def test_existing_endpoints_unchanged():
    """凭证功能不影响会话、修订、结算等既有接口。"""
    sid, detail = _create_complete()
    # 原会话查询仍是最初上报记录
    got = client.get(f"/dispatch/sessions/{sid}").json()
    assert got["confirmed_records"][1]["record"] == RECORDS[1]
    assert got["status"] == "complete"

    new1 = dict(RECORDS[1])
    new1["base_load_kw"] = 0.22
    new1["grid_purchase_energy_kwh"] = 0.02
    _submit_revision(sid, 0, [{"index": 1, "record": new1}])
    _issue(sid, 1)

    # 更正查询仍可直接提交结算；原始证据不改写；批次重试仍 deduped
    rev = client.get(f"/dispatch/sessions/{sid}/revisions").json()
    assert rev["current_revision"] == 1
    assert rev["original_records"][1]["record"] == RECORDS[1]
    sr = client.post("/dispatch/settlement", json=rev["settlement_request"])
    assert sr.status_code == 200
    assert sr.json()["summary"]["actual_total_cost_yuan"] == pytest.approx(0.11)

    dedup = client.post(
        f"/dispatch/sessions/{sid}/batches",
        json={"start_index": 0, "records": RECORDS[:2]},
    ).json()
    assert dedup["result"] == "deduped"

    # 会话列表接口口径不变
    assert any(s["session_id"] == sid for s in client.get("/dispatch/sessions").json()["sessions"])


def test_voucher_for_original_uses_original_records_not_revised():
    """修订号 0 的凭证即使在更正后补出（需在更正前未出单且当前号仍为 0 的约束下）：
    直接验证台账选择——首张凭证结算报告的 t1 实测购电量取最初上报值。"""
    sid, _ = _create_complete()
    b = _issue(sid, 0).json()
    t1 = next(p for p in b["settlement"]["periods"] if p["index"] == 1)
    assert t1["actual_grid_purchase_energy_kwh"] == 0.01

    new1 = dict(RECORDS[1])
    new1["base_load_kw"] = 0.22
    new1["grid_purchase_energy_kwh"] = 0.02
    _submit_revision(sid, 0, [{"index": 1, "record": new1}])
    b1 = _issue(sid, 1).json()
    t1b = next(p for p in b1["settlement"]["periods"] if p["index"] == 1)
    assert t1b["actual_grid_purchase_energy_kwh"] == 0.02
