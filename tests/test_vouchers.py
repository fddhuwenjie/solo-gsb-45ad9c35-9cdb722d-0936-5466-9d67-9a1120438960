"""满期执行会话的逐版结算凭证行为测试。

覆盖：
- 仅满期会话可出单 / 查询（未满期 422、未知会话 404）；
- 修订号 0 对最初上报记录、n≥1 对对应更正台账，沿用现有结算规则核算；
- 首张凭证记全额，其后每张列相对上一张凭证的应补/应退/无差额（可跳过未出单
  的中间修订）；
- 只能结算有效前缀时整次拒绝（voucher_partial_settled），不留任何凭证；
- 同一修订号重试原样返回冻结凭证（result=existing），即使后来已有更新修订/凭证；
- 未出单的过期修订号 / 超前修订号 → 409 voucher_revision_conflict 及当前号；
- 凭证冻结该版台账与完整结算报告，后续更正不影响已出凭证；
- 按出单次序列表 + 当前修订待出单状态；
- 重启后凭证仍可查；同修订并发出单只有一份落盘；出单与更正互斥不引用过期台账；
- 旧库（无凭证表）打开自动补建；原会话、修订与结算接口行为不变。
"""

import copy
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.revisions import submit_revision
from app.storage import configure_store
from app.vouchers import issue_voucher

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

# 与原计划自洽的完整 4 段实测（计划：充 0.2、放 0.1、放 0.1、静置）。
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
ORIG_TOTAL = 0.1  # 最初上报记录口径的实测总费用（t1/t2 各 0.01 能量费 + 0.08 衰减）


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    configure_store(str(tmp_path / "vouchers.db"))
    yield
    configure_store(None)


def _plan():
    return client.post("/dispatch", json=DISPATCH).json()


def _create_complete(tol=TOL):
    """创建会话并把 RECORDS 分两批追加成满期会话，返回 sid。"""
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
    return sid


def _rev(sid, expected, changes):
    return client.post(
        f"/dispatch/sessions/{sid}/revisions",
        json={"expected_revision": expected, "changes": changes},
    )


def _issue(sid, revision):
    return client.post(
        f"/dispatch/sessions/{sid}/vouchers",
        json={"expected_revision": revision},
    )


def _list(sid):
    return client.get(f"/dispatch/sessions/{sid}/vouchers")


def _ch(index, record):
    return {"index": index, "record": record}


# ---------------------------------------------------------------------------
# 首张凭证：修订号 0，最初上报记录，全额计费
# ---------------------------------------------------------------------------


def test_first_voucher_revision_zero_full_amount():
    sid = _create_complete()
    r = _issue(sid, 0)
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["result"] == "issued"
    assert v["voucher_no"] == 1
    assert v["revision_no"] == 0
    assert v["current_revision"] == 0
    assert v["settlement_status"] == "settled"
    assert v["settled_periods"] == 4
    assert v["num_periods"] == 4
    assert v["billing"] == {
        "type": "full",
        "amount_yuan": pytest.approx(ORIG_TOTAL, abs=1e-9),
        "relative_to_revision": None,
        "relative_to_voucher_no": None,
    }
    assert v["total_cost_yuan"] == pytest.approx(ORIG_TOTAL, abs=1e-9)
    assert v["issued_at"].endswith("Z")
    # 冻结的台账就是最初上报记录
    assert [x["record"] for x in v["records"]] == RECORDS
    # 冻结的完整结算报告口径与现有结算接口一致
    assert v["settlement"]["status"] == "settled"
    assert v["settlement"]["summary"]["actual_total_cost_yuan"] == pytest.approx(
        ORIG_TOTAL, abs=1e-9
    )
    assert len(v["settlement"]["periods"]) == 4
    assert v["voucher_id"]


def test_voucher_settlement_matches_existing_settlement_endpoint():
    """凭证冻结的结算报告与直接调用 /dispatch/settlement 的结果一致。"""
    sid = _create_complete()
    v = _issue(sid, 0).json()

    detail = client.get(f"/dispatch/sessions/{sid}").json()
    direct = client.post(
        "/dispatch/settlement",
        json={
            "dispatch": detail["dispatch"],
            "plan_snapshot": detail["plan_snapshot"],
            "settlement_tolerance_kwh": TOL,
            "records": [r["record"] for r in detail["confirmed_records"]],
        },
    ).json()
    assert v["settlement"] == direct


# ---------------------------------------------------------------------------
# 补 / 退差额：相对上一张凭证
# ---------------------------------------------------------------------------


def test_supplement_voucher_after_upward_revision():
    """修订后总费用升高：第二张凭证为应补，差额 = 新版 − 上一张。"""
    sid = _create_complete()
    _issue(sid, 0)

    # t1 实际负荷 0.22，期望购电 0.02（SOC/充放电轨迹不变），多 0.01kWh × 1 元
    new1 = dict(RECORDS[1])
    new1.update(base_load_kw=0.22, grid_purchase_energy_kwh=0.02)
    assert _rev(sid, 0, [_ch(1, new1)]).status_code == 200

    v = _issue(sid, 1).json()
    assert v["result"] == "issued"
    assert v["voucher_no"] == 2
    assert v["revision_no"] == 1
    assert v["current_revision"] == 1
    assert v["total_cost_yuan"] == pytest.approx(0.11, abs=1e-9)
    assert v["billing"] == {
        "type": "supplement",
        "amount_yuan": pytest.approx(0.01, abs=1e-9),
        "relative_to_revision": 0,
        "relative_to_voucher_no": 1,
    }
    # 冻结的是修订 1 的台账
    assert v["records"][1]["record"] == new1
    assert v["records"][0]["record"] == RECORDS[0]


def test_refund_voucher_after_downward_revision():
    """修订后总费用降低：应退，金额为差额绝对值。"""
    sid = _create_complete()
    _issue(sid, 0)

    # t1 实际负荷 0.18：期望购电 max(0, 0.09 − 0.09) = 0，少 0.01 元
    new1 = dict(RECORDS[1])
    new1.update(base_load_kw=0.18, grid_purchase_energy_kwh=0.0)
    assert _rev(sid, 0, [_ch(1, new1)]).status_code == 200

    v = _issue(sid, 1).json()
    assert v["total_cost_yuan"] == pytest.approx(0.09, abs=1e-9)
    assert v["billing"]["type"] == "refund"
    assert v["billing"]["amount_yuan"] == pytest.approx(0.01, abs=1e-9)
    assert v["billing"]["relative_to_revision"] == 0
    assert v["billing"]["relative_to_voucher_no"] == 1


def test_no_adjustment_when_total_unchanged():
    """总费用不变（如改 t3 谷时购电量）：no_adjustment，差额 0。"""
    sid = _create_complete()
    _issue(sid, 0)

    new3 = dict(RECORDS[3])
    new3.update(base_load_kw=0.22, grid_purchase_energy_kwh=0.11)
    assert _rev(sid, 0, [_ch(3, new3)]).status_code == 200

    v = _issue(sid, 1).json()
    assert v["total_cost_yuan"] == pytest.approx(ORIG_TOTAL, abs=1e-9)
    assert v["billing"]["type"] == "no_adjustment"
    assert v["billing"]["amount_yuan"] == 0.0
    assert v["billing"]["relative_to_revision"] == 0


def test_delta_relative_to_last_voucher_allows_skipping_revisions():
    """跳过未出单的中间修订：差额总是相对最近一张已出凭证。"""
    sid = _create_complete()
    v0 = _issue(sid, 0).json()

    # rev1：t1 +0.01（应补基线）
    up1 = dict(RECORDS[1]); up1.update(base_load_kw=0.22, grid_purchase_energy_kwh=0.02)
    assert _rev(sid, 0, [_ch(1, up1)]).status_code == 200
    v1 = _issue(sid, 1).json()
    assert v1["billing"]["type"] == "supplement"
    assert v1["billing"]["amount_yuan"] == pytest.approx(0.01, abs=1e-9)

    # rev2（不出单）：t1 再改到 +0.02
    up2 = dict(RECORDS[1]); up2.update(base_load_kw=0.24, grid_purchase_energy_kwh=0.03)
    assert _rev(sid, 1, [_ch(1, up2)]).status_code == 200

    # rev3（也不出单）：把 t1 改回 0.18 → 期望购电 0，总费用 0.09
    down = dict(RECORDS[1]); down.update(base_load_kw=0.18, grid_purchase_energy_kwh=0.0)
    assert _rev(sid, 2, [_ch(1, down)]).status_code == 200

    # 对当前修订 3 出单：跳过 rev2，差额相对 rev1 的凭证（0.11 → 0.09 = 退 0.02）
    v3 = _issue(sid, 3).json()
    assert v3["voucher_no"] == 3
    assert v3["revision_no"] == 3
    assert v3["records"][1]["record"] == down
    assert v3["billing"] == {
        "type": "refund",
        "amount_yuan": pytest.approx(0.02, abs=1e-9),
        "relative_to_revision": 1,
        "relative_to_voucher_no": 2,
    }
    assert v0["billing"]["type"] == "full"  # 已出凭证不受影响


# ---------------------------------------------------------------------------
# 幂等重试：同一修订号原样返回，即使后来已有更新修订
# ---------------------------------------------------------------------------


def test_retry_same_revision_returns_original_frozen_voucher():
    sid = _create_complete()
    v = _issue(sid, 0).json()

    # rev1 出单
    up1 = dict(RECORDS[1]); up1.update(base_load_kw=0.22, grid_purchase_energy_kwh=0.02)
    _rev(sid, 0, [_ch(1, up1)])
    v1 = _issue(sid, 1).json()

    # rev2 再更正（rev0/1 都已出单）
    up2 = dict(RECORDS[1]); up2.update(base_load_kw=0.24, grid_purchase_energy_kwh=0.03)
    assert _rev(sid, 1, [_ch(1, up2)]).status_code == 200

    # 重试修订号 0 → 原凭证冻结内容（不是 409！），result=existing
    again0 = _issue(sid, 0)
    assert again0.status_code == 200, again0.text
    a = again0.json()
    assert a["result"] == "existing"
    assert a["voucher_id"] == v["voucher_id"]
    assert a["issued_at"] == v["issued_at"]
    assert a["total_cost_yuan"] == v["total_cost_yuan"]
    assert a["billing"] == v["billing"]
    assert a["current_revision"] == 0  # 出单当时的当前号，冻结
    assert a["records"][1]["record"] == RECORDS[1]  # 不是 rev1/rev2 的台账

    # 重试修订号 1 同样原样返回
    again1 = _issue(sid, 1).json()
    assert again1["result"] == "existing"
    assert again1["voucher_id"] == v1["voucher_id"]
    assert again1["issued_at"] == v1["issued_at"]
    assert again1["billing"] == v1["billing"]
    assert again1["records"][1]["record"] == up1

    # 列表中每个修订号只有一张凭证，次序不乱
    lst = _list(sid).json()
    assert [x["revision_no"] for x in lst["vouchers"]] == [0, 1]
    assert [x["voucher_no"] for x in lst["vouchers"]] == [1, 2]


# ---------------------------------------------------------------------------
# 冲突：未出单的过期修订号 / 超前修订号
# ---------------------------------------------------------------------------


def test_unissued_stale_revision_conflicts_with_current_no():
    sid = _create_complete()
    _issue(sid, 0)
    up1 = dict(RECORDS[3]); up1.update(base_load_kw=0.22, grid_purchase_energy_kwh=0.11)
    assert _rev(sid, 0, [_ch(3, up1)]).status_code == 200
    # rev1 不出单，直接推进到 rev2
    up2 = dict(RECORDS[3]); up2.update(base_load_kw=0.24, grid_purchase_energy_kwh=0.12)
    assert _rev(sid, 1, [_ch(3, up2)]).status_code == 200

    r = _issue(sid, 1)  # 过期且未出单
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "voucher_revision_conflict"
    assert body["current_revision"] == 2
    assert "未出过凭证" in body["reason"]
    # 冲突不留凭证
    assert [x["revision_no"] for x in _list(sid).json()["vouchers"]] == [0]


def test_future_revision_conflicts_with_current_no():
    sid = _create_complete()
    r = _issue(sid, 1)  # 当前号为 0，超前
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "voucher_revision_conflict"
    assert body["current_revision"] == 0
    assert _list(sid).json()["vouchers"] == []


# ---------------------------------------------------------------------------
# 只能结算有效前缀：整次拒绝，不留凭证
# ---------------------------------------------------------------------------


def _deviating_chain_revision(sid):
    """提交一条遥测自洽但偏离计划 SOC 轨迹的修订链（t1 起 SOC 不同），返回修订号。"""
    n1 = dict(RECORDS[1])
    n1.update(
        discharge_ac_energy_kwh=0.045, grid_purchase_energy_kwh=0.055,
        soc_start_kwh=0.2, soc_end_kwh=0.15,
    )
    n2 = dict(RECORDS[2]); n2.update(soc_start_kwh=0.15, soc_end_kwh=0.05)
    n3 = dict(RECORDS[3]); n3.update(soc_start_kwh=0.05, soc_end_kwh=0.05)
    r = _rev(sid, 0, [_ch(1, n1), _ch(2, n2), _ch(3, n3)])
    assert r.status_code == 200, r.text


def test_partial_settlement_rejected_without_voucher():
    sid = _create_complete()
    _deviating_chain_revision(sid)

    r = _issue(sid, 1)
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "voucher_partial_settled"
    assert body["first_anomaly_index"] == 1
    assert body["settled_periods"] == 1

    # 整次拒绝、不留凭证；当前修订仍未出单
    lst = _list(sid).json()
    assert lst["vouchers"] == []
    assert lst["pending"] == {
        "current_revision": 1,
        "voucher_revisions": [],
        "next_revision_settled": False,
    }


def test_no_valid_interval_rejected_without_voucher():
    """首条即 SOC 断点：沿用 no_valid_interval 错误码（结算规则原样核算），不留凭证。"""
    sid = _create_complete()
    # t0 只充 0.1kWh（计划为 0.2），遥测自洽但末态 SOC 0.1 偏离计划 0.2 → 首条断点；
    # 后续链条从 0.1 自洽承接：t1 放 0.1、t2/t3 静置
    n0 = dict(RECORDS[0])
    n0.update(
        charge_ac_energy_kwh=round(0.1 / 0.9, 6),
        grid_purchase_energy_kwh=round(0.1 + 0.1 / 0.9, 6),
        soc_end_kwh=0.1,
    )
    n1 = dict(RECORDS[1]); n1.update(
        discharge_ac_energy_kwh=0.09, grid_purchase_energy_kwh=0.01,
        soc_start_kwh=0.1, soc_end_kwh=0.0,
    )
    n2 = dict(RECORDS[2]); n2.update(
        charge_ac_energy_kwh=0.0, discharge_ac_energy_kwh=0.0,
        grid_purchase_energy_kwh=0.1, soc_start_kwh=0.0, soc_end_kwh=0.0,
    )
    n3 = dict(RECORDS[3]); n3.update(soc_start_kwh=0.0, soc_end_kwh=0.0)
    r = _rev(sid, 0, [_ch(0, n0), _ch(1, n1), _ch(2, n2), _ch(3, n3)])
    assert r.status_code == 200, r.text

    r = _issue(sid, 1)
    assert r.status_code == 422
    assert r.json()["error"] == "no_valid_interval"
    assert _list(sid).json()["vouchers"] == []


def test_failed_issue_leaves_no_partial_data_and_retry_after_fix():
    """前缀拒绝后不影响对后续已修复修订出单。"""
    sid = _create_complete()
    _deviating_chain_revision(sid)
    assert _issue(sid, 1).status_code == 422

    # 修订 2 把台账改回与计划一致 → 对当前号 2 可正常出单（本会话首张，全额）
    r = _rev(
        sid, 1,
        [_ch(1, RECORDS[1]), _ch(2, RECORDS[2]), _ch(3, RECORDS[3])],
    )
    assert r.status_code == 200
    v = _issue(sid, 2).json()
    assert v["billing"]["type"] == "full"
    assert v["total_cost_yuan"] == pytest.approx(ORIG_TOTAL, abs=1e-9)


# ---------------------------------------------------------------------------
# 前置条件
# ---------------------------------------------------------------------------


def test_only_complete_session_can_issue_or_list():
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

    # 空会话同样拒绝
    sid_empty = client.post(
        "/dispatch/sessions",
        json={"dispatch": DISPATCH, "plan_snapshot": _plan(), "audit_tolerance_kwh": TOL},
    ).json()["session_id"]
    assert _issue(sid_empty, 0).status_code == 422
    assert _list(sid_empty).status_code == 422


def test_unknown_session_returns_404():
    r = _issue("does-not-exist", 0)
    assert r.status_code == 404
    assert r.json()["error"] == "session_not_found"
    assert _list("does-not-exist").status_code == 404


def test_negative_revision_invalid_input():
    sid = _create_complete()
    r = client.post(
        f"/dispatch/sessions/{sid}/vouchers", json={"expected_revision": -1}
    )
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_input"


# ---------------------------------------------------------------------------
# 列表与待出单状态
# ---------------------------------------------------------------------------


def test_list_vouchers_in_issue_order_and_pending_status():
    sid = _create_complete()
    empty = _list(sid).json()
    assert empty["current_revision"] == 0
    assert empty["status"] == "complete"
    assert empty["pending"] == {
        "current_revision": 0,
        "voucher_revisions": [],
        "next_revision_settled": False,
    }

    _issue(sid, 0)
    after0 = _list(sid).json()
    assert after0["pending"]["next_revision_settled"] is True
    assert after0["pending"]["voucher_revisions"] == [0]

    up1 = dict(RECORDS[3]); up1.update(base_load_kw=0.22, grid_purchase_energy_kwh=0.11)
    _rev(sid, 0, [_ch(3, up1)])
    after_rev1 = _list(sid).json()
    # 新修订出现：当前号 1 尚未出单
    assert after_rev1["current_revision"] == 1
    assert after_rev1["pending"] == {
        "current_revision": 1,
        "voucher_revisions": [0],
        "next_revision_settled": False,
    }

    _issue(sid, 1)
    done = _list(sid).json()
    assert [x["voucher_no"] for x in done["vouchers"]] == [1, 2]
    assert [x["revision_no"] for x in done["vouchers"]] == [0, 1]
    assert done["pending"]["next_revision_settled"] is True
    assert done["vouchers"][1]["billing"]["relative_to_revision"] == 0
    # 列表里的凭证 result 恒为 issued（幂等标记只在 POST 重试响应中出现）
    assert all(x["result"] == "issued" for x in done["vouchers"])


# ---------------------------------------------------------------------------
# 持久化：重启后凭证仍可查、仍幂等
# ---------------------------------------------------------------------------


def test_vouchers_persist_across_restart(tmp_path):
    db_path = str(tmp_path / "persist-voucher.db")
    configure_store(db_path)
    sid = _create_complete()
    v0 = _issue(sid, 0).json()
    up1 = dict(RECORDS[1]); up1.update(base_load_kw=0.22, grid_purchase_energy_kwh=0.02)
    _rev(sid, 0, [_ch(1, up1)])
    v1 = _issue(sid, 1).json()

    configure_store(db_path)  # 模拟进程重启
    lst = _list(sid).json()
    assert [x["revision_no"] for x in lst["vouchers"]] == [0, 1]
    assert lst["vouchers"][0]["voucher_id"] == v0["voucher_id"]
    assert lst["vouchers"][1]["billing"] == v1["billing"]
    assert lst["pending"]["next_revision_settled"] is True

    # 重启后重试仍返回原凭证
    again = _issue(sid, 0).json()
    assert again["result"] == "existing"
    assert again["voucher_id"] == v0["voucher_id"]
    assert again["issued_at"] == v0["issued_at"]

    # 重启后继续推进修订并出单，序号接 3
    up2 = dict(RECORDS[1]); up2.update(base_load_kw=0.24, grid_purchase_energy_kwh=0.03)
    assert _rev(sid, 1, [_ch(1, up2)]).status_code == 200
    v2 = _issue(sid, 2).json()
    assert v2["voucher_no"] == 3
    assert v2["billing"]["relative_to_revision"] == 1


def test_legacy_db_voucher_table_auto_created(tmp_path):
    """旧库（只有 sessions / session_records）打开即补建凭证表，满期可正常出单。"""
    import json
    import sqlite3

    db_path = str(tmp_path / "legacy-voucher.db")
    plan = client.post("/dispatch", json=DISPATCH).json()

    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            dispatch_json TEXT NOT NULL,
            plan_snapshot_json TEXT NOT NULL,
            audit_tolerance_kwh REAL NOT NULL,
            num_periods INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE session_records (
            session_id TEXT NOT NULL,
            idx INTEGER NOT NULL,
            record_json TEXT NOT NULL,
            stored_at TEXT NOT NULL,
            PRIMARY KEY (session_id, idx)
        );
        """
    )
    sid = "legacy-voucher-session"
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
        (sid, json.dumps(DISPATCH), json.dumps(plan), TOL, 4, "2026-01-01T00:00:00Z"),
    )
    for idx, rec in enumerate(RECORDS):
        conn.execute(
            "INSERT INTO session_records VALUES (?, ?, ?, ?)",
            (sid, idx, json.dumps(rec), "2026-01-01T00:00:01Z"),
        )
    conn.commit()
    conn.close()

    configure_store(db_path)
    lst = _list(sid)
    assert lst.status_code == 200, lst.text
    assert lst.json()["vouchers"] == []
    r = _issue(sid, 0)
    assert r.status_code == 200, r.text
    assert r.json()["billing"]["type"] == "full"


# ---------------------------------------------------------------------------
# 并发
# ---------------------------------------------------------------------------


def test_concurrent_issue_same_revision_only_one_persisted():
    sid = _create_complete()
    outcomes = []

    def worker(tag):
        try:
            r = issue_voucher(sid, 0)
            outcomes.append((tag, r["result"], r["voucher_id"]))
        except Exception as exc:  # noqa: BLE001
            outcomes.append((tag, getattr(exc, "code", "error"), None))

    threads = [threading.Thread(target=worker, args=(str(i),)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(outcomes) == 8
    assert sum(1 for _, result, _ in outcomes if result == "issued") == 1
    assert sum(1 for _, result, _ in outcomes if result == "existing") == 7
    assert {oid for _, _, oid in outcomes if oid} == {outcomes[0][2]}
    assert len(_list(sid).json()["vouchers"]) == 1


def test_concurrent_revision_and_issue_never_references_stale_ledger():
    """更正与出单并发：要么出单先落盘 rev0、要么更正先生效导致出单 409；
    不允许出现「409 但写入了 rev0 凭证」或凭证引用修订后台账的部分数据。"""
    import random

    for round_no in range(6):
        sid = _create_complete()
        errors = []

        def worker_issue():
            try:
                issue_voucher(sid, 0)
            except Exception as exc:  # noqa: BLE001
                errors.append(("issue", getattr(exc, "code", str(exc))))

        def worker_revise():
            try:
                new3 = dict(RECORDS[3])
                new3.update(base_load_kw=0.22, grid_purchase_energy_kwh=0.11)
                submit_revision(
                    sid, 0, [{"index": 3, "record": copy.deepcopy(new3)}]
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(("rev", getattr(exc, "code", str(exc))))

        workers = [worker_issue, worker_revise]
        random.shuffle(workers)
        threads = [threading.Thread(target=w) for w in workers]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        lst = _list(sid).json()
        revision_rows = client.get(f"/dispatch/sessions/{sid}/revisions").json()
        current = revision_rows["current_revision"]
        issued = [v["revision_no"] for v in lst["vouchers"]]

        # 更正必定成功（两个临界区最终都能完成其一；若出单先，更正随后在 rev0
        # 上仍成功；若更正先，出单 409）
        assert current == 1, (round_no, errors, current)
        assert set(issued) <= {0}, (round_no, issued)

        if issued:
            # 出单成功：凭证必须冻结最初上报台账（t3 购电 0.1），且没有任何冲突
            v0 = lst["vouchers"][0]
            assert v0["records"][3]["record"]["grid_purchase_energy_kwh"] == 0.1
            assert not any(code == "voucher_revision_conflict" for _, code in errors)
        else:
            # 出单失败：必须是冲突，且凭证表为空（无部分数据）
            assert ("issue", "voucher_revision_conflict") in errors


# ---------------------------------------------------------------------------
# 既有接口行为不变
# ---------------------------------------------------------------------------


def test_existing_session_revision_settlement_interfaces_unchanged():
    sid = _create_complete()
    up1 = dict(RECORDS[1]); up1.update(base_load_kw=0.22, grid_purchase_energy_kwh=0.02)
    _rev(sid, 0, [_ch(1, up1)])
    _issue(sid, 0)
    _issue(sid, 1)

    # 原会话查询仍是最初上报记录
    detail = client.get(f"/dispatch/sessions/{sid}").json()
    assert detail["current_revision"] == 1
    assert [r["record"] for r in detail["confirmed_records"]] == RECORDS

    # 批次重试仍 deduped、回执冻结
    retry = client.post(
        f"/dispatch/sessions/{sid}/batches",
        json={"start_index": 0, "records": RECORDS[:2]},
    ).json()
    assert retry["result"] == "deduped"
    assert retry["final_soc_kwh"] == 0.1

    # 修订查询照常返回可提交结算接口的请求数据
    rev = client.get(f"/dispatch/sessions/{sid}/revisions").json()
    assert rev["current_revision"] == 1
    direct = client.post("/dispatch/settlement", json=rev["settlement_request"]).json()
    assert direct["summary"]["actual_total_cost_yuan"] == pytest.approx(0.11, abs=1e-9)

    # 结算接口本身不感知凭证
    direct0 = client.post(
        "/dispatch/settlement",
        json={
            "dispatch": detail["dispatch"],
            "plan_snapshot": detail["plan_snapshot"],
            "settlement_tolerance_kwh": TOL,
            "records": [r["record"] for r in detail["confirmed_records"]],
        },
    ).json()
    assert direct0["summary"]["actual_total_cost_yuan"] == pytest.approx(
        ORIG_TOTAL, abs=1e-9
    )
