"""执行会话接口行为测试：创建、分批追加、重试幂等、并发冲突、查询、持久化与续排/结算衔接。"""

import copy
import json
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.sessions import append_batch
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


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    """每个测试一个独立 SQLite 文件，避免用例间相互可见。"""
    configure_store(str(tmp_path / "sessions.db"))
    yield
    configure_store(None)


def _plan():
    return client.post("/dispatch", json=DISPATCH).json()


def _create(tol=TOL, plan=None, dispatch=None):
    r = client.post(
        "/dispatch/sessions",
        json={
            "dispatch": dispatch or DISPATCH,
            "plan_snapshot": plan or _plan(),
            "audit_tolerance_kwh": tol,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _append(sid, start_index, records):
    return client.post(
        f"/dispatch/sessions/{sid}/batches",
        json={"start_index": start_index, "records": records},
    )


def test_create_session_recomputes_snapshot():
    """创建会话回显原输入与快照；快照被篡改/重算无解时不创建。"""
    data = _create()
    assert len(data["session_id"]) == 36
    assert data["num_periods"] == 4
    assert data["confirmed_periods"] == 0
    assert data["next_index"] == 0
    assert data["status"] == "awaiting_records"
    assert data["dispatch"]["prices"] == DISPATCH["prices"]
    assert data["plan_snapshot"]["summary"]["num_periods"] == 4
    assert data["audit_tolerance_kwh"] == TOL
    assert data["created_at"].endswith("Z")

    tampered = copy.deepcopy(_plan())
    tampered["periods"][1]["action"] = "idle"
    r = client.post(
        "/dispatch/sessions",
        json={"dispatch": DISPATCH, "plan_snapshot": tampered, "audit_tolerance_kwh": TOL},
    )
    assert r.status_code == 422
    assert r.json()["error"] == "plan_snapshot_mismatch"

    bad_dispatch = copy.deepcopy(DISPATCH)
    bad_dispatch["max_charge_kw"] = 0.0
    bad_dispatch["min_terminal_soc_kwh"] = 0.4
    r = client.post(
        "/dispatch/sessions",
        json={"dispatch": bad_dispatch, "plan_snapshot": _plan(), "audit_tolerance_kwh": TOL},
    )
    assert r.status_code == 422
    assert r.json()["error"] == "infeasible"


def test_batched_append_continuity_and_status():
    """分批追加：首批接原初始 SOC、后续批接已保存末态；状态随确认数推进。"""
    sid = _create()["session_id"]

    r = _append(sid, 0, RECORDS[:2])
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["result"] == "accepted"
    assert b["start_index"] == 0
    assert b["batch_size"] == 2
    assert b["confirmed_periods"] == 2
    assert b["next_index"] == 2
    assert b["status"] == "in_progress"
    assert b["final_soc_kwh"] == 0.1
    assert b["first_record_soc_gap_kwh"] == 0.0
    assert [a["index"] for a in b["batch_audit"]] == [0, 1]
    assert all(not a["anomalous"] for a in b["batch_audit"])

    # 第二批首态须接上批末态 0.1
    r = _append(sid, 2, RECORDS[2:])
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["result"] == "accepted"
    assert b["next_index"] == 4
    assert b["status"] == "complete"
    assert b["first_record_soc_gap_kwh"] == 0.0
    assert b["final_soc_kwh"] == 0.0
    assert [a["index"] for a in b["batch_audit"]] == [2, 3]


def test_action_deviation_from_plan_but_self_consistent_accepted():
    """实测动作偏离旧计划但记录自洽：不做 SOC 轨迹核对，照常接受入库。"""
    sid = _create()["session_id"]
    # t0 计划充 0.2，实际只充 0.1（能量/购电自洽、首态接原初值）
    deviated = [
        dict(
            base_load_kw=0.2, pv_kw=0.0,
            charge_ac_energy_kwh=round(0.1 / 0.9, 6), discharge_ac_energy_kwh=0.0,
            grid_purchase_energy_kwh=round(0.1 + 0.1 / 0.9, 6),
            soc_start_kwh=0.0, soc_end_kwh=0.1,
        )
    ]
    r = _append(sid, 0, deviated)
    assert r.status_code == 200, r.text
    assert r.json()["batch_audit"][0]["anomalous"] is False
    d = client.get(f"/dispatch/sessions/{sid}").json()
    assert d["confirmed_records"][0]["record"]["soc_end_kwh"] == 0.1
    assert d["final_soc_kwh"] == 0.1


def test_index_gap_whole_batch_rejected_and_not_stored():
    """下标跳跃整批拒绝且不入库：next_index 不变。"""
    sid = _create()["session_id"]
    r = _append(sid, 1, RECORDS[1:2])
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "batch_index_gap"
    assert "期望 0" in body["reason"]

    d = client.get(f"/dispatch/sessions/{sid}").json()
    assert d["confirmed_periods"] == 0
    assert d["next_index"] == 0

    # 补齐后再跳：2 已确认，报 3 → 拒绝
    assert _append(sid, 0, RECORDS[:2]).status_code == 200
    r = _append(sid, 3, RECORDS[3:])
    assert r.status_code == 422
    assert r.json()["error"] == "batch_index_gap"
    d = client.get(f"/dispatch/sessions/{sid}").json()
    assert d["next_index"] == 2


def test_out_of_range_whole_batch_rejected():
    """超出计划时段范围整批拒绝：起点越界与批尾越界都覆盖。"""
    sid = _create()["session_id"]
    r = _append(sid, 4, RECORDS[:1])
    assert r.status_code == 422
    assert r.json()["error"] == "batch_out_of_range"

    assert _append(sid, 0, RECORDS[:3]).status_code == 200
    r = _append(sid, 3, RECORDS[2:])  # 区间 [3,5) 越界
    assert r.status_code == 422
    assert r.json()["error"] == "batch_out_of_range"
    assert client.get(f"/dispatch/sessions/{sid}").json()["next_index"] == 3


def test_batch_audit_anomaly_whole_batch_rejected():
    """批内任一记录遥测审计异常 → batch_audit_failed 整批不入库。"""
    sid = _create()["session_id"]
    bad = copy.deepcopy(RECORDS[:2])
    bad[1]["grid_purchase_energy_kwh"] = 0.11  # 期望 0.01，残差 0.1
    r = _append(sid, 0, bad)
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "batch_audit_failed"
    assert "下标 1" in body["reason"]
    assert "购电量残差" in body["reason"]
    assert client.get(f"/dispatch/sessions/{sid}").json()["confirmed_periods"] == 0

    # 批内 SOC 衔接断裂同样整批拒绝
    bad2 = copy.deepcopy(RECORDS[:2])
    bad2[1]["soc_start_kwh"] = 0.25
    r = _append(sid, 0, bad2)
    assert r.status_code == 422
    assert r.json()["error"] == "batch_audit_failed"
    assert client.get(f"/dispatch/sessions/{sid}").json()["confirmed_periods"] == 0


def test_first_batch_must_meet_original_initial_soc():
    """首批首态 SOC 未在容差内接原初始 SOC → batch_boundary_gap；等于容差通过。"""
    sid = _create()["session_id"]
    recs = copy.deepcopy(RECORDS[:2])

    recs[0]["soc_start_kwh"] = 0.011
    r = _append(sid, 0, recs)
    assert r.status_code == 422
    assert r.json()["error"] == "batch_boundary_gap"
    assert "原初始 SOC" in r.json()["reason"]

    # 恰好等于容差：边界接受（严格大于才判超差）；末态 0.21 等后续自洽构造
    recs2 = [
        dict(
            base_load_kw=0.2, pv_kw=0.0,
            charge_ac_energy_kwh=0.0, discharge_ac_energy_kwh=0.0,
            grid_purchase_energy_kwh=0.1,
            soc_start_kwh=0.01, soc_end_kwh=0.01,
        )
    ]
    r = _append(sid, 0, recs2)
    assert r.status_code == 200, r.text
    assert r.json()["first_record_soc_gap_kwh"] == 0.01


def test_later_batch_must_meet_saved_end_soc():
    """后续批次首态须接已保存末态，不接 → batch_boundary_gap 整批不入库。"""
    sid = _create()["session_id"]
    assert _append(sid, 0, RECORDS[:2]).status_code == 200
    recs = copy.deepcopy(RECORDS[2:])
    recs[0]["soc_start_kwh"] = 0.15  # 已保存末态为 0.1
    r = _append(sid, 2, recs)
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "batch_boundary_gap"
    assert "已保存末态" in body["reason"]
    assert client.get(f"/dispatch/sessions/{sid}").json()["next_index"] == 2


def test_identical_retry_returns_existing_result():
    """同起点已保存整批原样重试：deduped 返回既有结果，不新增记录。"""
    sid = _create()["session_id"]
    first = _append(sid, 0, RECORDS[:2]).json()
    again = _append(sid, 0, copy.deepcopy(RECORDS[:2])).json()
    assert again["result"] == "deduped"
    assert again["confirmed_periods"] == first["confirmed_periods"] == 2
    assert again["batch_audit"] == first["batch_audit"]
    assert again["first_record_soc_gap_kwh"] == first["first_record_soc_gap_kwh"]
    assert again["final_soc_kwh"] == first["final_soc_kwh"]

    d = client.get(f"/dispatch/sessions/{sid}").json()
    assert d["confirmed_periods"] == 2
    assert {r["index"] for r in d["confirmed_records"]} == {0, 1}

    # 满期后对最后一批原样重试也幂等
    last = _append(sid, 2, RECORDS[2:]).json()
    assert last["result"] == "accepted"
    again_last = _append(sid, 2, copy.deepcopy(RECORDS[2:])).json()
    assert again_last["result"] == "deduped"
    assert again_last["status"] == "complete"


def test_short_prefix_retry_is_conflict_not_deduped():
    """同起点短批（只重发已确认整批的前几条）→ 409 conflict，不得误判 deduped。"""
    sid = _create()["session_id"]
    first = _append(sid, 0, RECORDS[:2]).json()
    assert first["result"] == "accepted"

    # 只重发第一条：起点相同但长度不符，不是已确认整批
    r = _append(sid, 0, RECORDS[:1])
    assert r.status_code == 409
    assert r.json()["error"] == "conflict"
    assert "next_index=2" in r.json()["reason"]

    # 同起点更长的批（含未确认下标）同样冲突
    r = _append(sid, 0, RECORDS[:3])
    assert r.status_code == 409
    assert r.json()["error"] == "conflict"

    # 记录与进度不变；完整首批原样重试仍幂等
    d = client.get(f"/dispatch/sessions/{sid}").json()
    assert d["confirmed_periods"] == 2
    assert d["next_index"] == 2
    again = _append(sid, 0, copy.deepcopy(RECORDS[:2])).json()
    assert again["result"] == "deduped"


def test_retry_after_later_batches_returns_frozen_receipt():
    """后续批次入库后重发完整首批：deduped 且回执保持该批首次成功时的值。"""
    sid = _create()["session_id"]
    first = _append(sid, 0, RECORDS[:2]).json()
    assert first["confirmed_periods"] == 2
    assert first["status"] == "in_progress"
    assert first["final_soc_kwh"] == 0.1

    # 追加第二批至满期，会话进度与末态已推进
    last = _append(sid, 2, RECORDS[2:]).json()
    assert last["status"] == "complete"

    # 重发完整首批：除 result 外全部字段与首收回执一致，不漂移到最新进度
    again = _append(sid, 0, copy.deepcopy(RECORDS[:2])).json()
    assert again["result"] == "deduped"
    for key in (
        "session_id", "start_index", "batch_size", "confirmed_periods",
        "next_index", "status", "batch_audit", "first_record_soc_gap_kwh",
        "final_soc_kwh",
    ):
        assert again[key] == first[key], key

    # 第二批重发同样冻结在其首收回执（complete）
    again_last = _append(sid, 2, copy.deepcopy(RECORDS[2:])).json()
    assert again_last["result"] == "deduped"
    for key in ("confirmed_periods", "next_index", "status", "batch_audit", "final_soc_kwh"):
        assert again_last[key] == last[key], key


def test_cross_batch_concatenation_conflict():
    """跨批拼接（覆盖多个已确认批次）→ 409 conflict，记录与进度不变。"""
    sid = _create()["session_id"]
    assert _append(sid, 0, RECORDS[:2]).status_code == 200
    assert _append(sid, 2, RECORDS[2:]).status_code == 200

    # [0,4) 拼接两个已确认批次：起点 0 的已确认批次长度为 2，不等于 4
    r = _append(sid, 0, RECORDS)
    assert r.status_code == 409
    assert r.json()["error"] == "conflict"

    # [0,3) 跨批短拼接同样冲突
    r = _append(sid, 0, RECORDS[:3])
    assert r.status_code == 409

    # 从批中起点（非批次边界）上报也冲突
    r = _append(sid, 1, RECORDS[1:3])
    assert r.status_code == 409

    d = client.get(f"/dispatch/sessions/{sid}").json()
    assert d["confirmed_periods"] == 4
    assert d["next_index"] == 4
    assert d["status"] == "complete"

    # 两个已确认整批各自原样重试仍幂等
    assert _append(sid, 0, copy.deepcopy(RECORDS[:2])).json()["result"] == "deduped"
    assert _append(sid, 2, copy.deepcopy(RECORDS[2:])).json()["result"] == "deduped"


def test_same_start_different_content_conflict():
    """同起点内容不同（含部分重叠）→ 409 conflict，已保存一份继续生效。"""
    sid = _create()["session_id"]
    assert _append(sid, 0, RECORDS[:2]).status_code == 200

    diff = copy.deepcopy(RECORDS[:2])
    diff[1]["grid_purchase_energy_kwh"] = 0.012  # 容差内但内容不同
    r = _append(sid, 0, diff)
    assert r.status_code == 409
    assert r.json()["error"] == "conflict"
    assert "next_index=2" in r.json()["reason"]

    # 部分重叠（[0,2) 已存，再报 [1,3)）也冲突
    r = _append(sid, 1, RECORDS[1:3])
    assert r.status_code == 409
    assert r.json()["error"] == "conflict"

    # 已保存内容原样仍可幂等取回，冲突上报没有污染数据
    again = _append(sid, 0, copy.deepcopy(RECORDS[:2])).json()
    assert again["result"] == "deduped"


def test_concurrent_same_start_only_one_wins():
    """并发同起点上报：内容相同都拿到同一份（accepted/deduped）；
    内容不同只有一份生效，其余 409。"""
    sid = _create()["session_id"]
    outcomes = []

    def worker(records, tag):
        try:
            r = append_batch(sid, 0, copy.deepcopy(records))
            outcomes.append((tag, r["result"]))
        except Exception as exc:  # noqa: BLE001
            outcomes.append((tag, getattr(exc, "code", "error")))

    t1 = threading.Thread(target=worker, args=(RECORDS[:2], "same"))
    t2 = threading.Thread(target=worker, args=(RECORDS[:2], "same"))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(tag for tag, _ in outcomes) == ["same", "same"]
    assert sorted(res for _, res in outcomes) == ["accepted", "deduped"]

    # 内容不同的并发上报：恰好一个 accepted，其余 conflict
    sid2 = _create()["session_id"]
    alt = copy.deepcopy(RECORDS[:2])
    alt[0]["soc_end_kwh"] = 0.2  # 相同基线占位
    outcomes2 = []

    def worker2(records):
        try:
            r = append_batch(sid2, 0, records)
            outcomes2.append(r["result"])
        except Exception as exc:  # noqa: BLE001
            outcomes2.append(getattr(exc, "code", "error"))

    alt_a = copy.deepcopy(RECORDS[:2]); alt_a[1]["grid_purchase_energy_kwh"] = 0.012
    alt_b = copy.deepcopy(RECORDS[:2]); alt_b[1]["grid_purchase_energy_kwh"] = 0.011
    alt_c = copy.deepcopy(RECORDS[:2]); alt_c[1]["grid_purchase_energy_kwh"] = 0.009
    threads = [
        threading.Thread(target=worker2, args=(copy.deepcopy(alt_a),)),
        threading.Thread(target=worker2, args=(copy.deepcopy(alt_b),)),
        threading.Thread(target=worker2, args=(copy.deepcopy(alt_c),)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes2.count("accepted") == 1
    assert outcomes2.count("conflict") == 2
    assert client.get(f"/dispatch/sessions/{sid2}").json()["confirmed_periods"] == 2


def test_get_session_returns_plan_continuous_records_and_next_index():
    """查询返回原计划信息、连续已确认记录与下一下标。"""
    sid = _create()["session_id"]
    assert _append(sid, 0, RECORDS[:1]).status_code == 200

    d = client.get(f"/dispatch/sessions/{sid}").json()
    assert d["session_id"] == sid
    assert d["num_periods"] == 4
    assert d["confirmed_periods"] == 1
    assert d["next_index"] == 1
    assert d["status"] == "in_progress"
    assert d["initial_soc_kwh"] == 0.0
    assert d["final_soc_kwh"] == 0.2
    assert d["dispatch"]["capacity_kwh"] == 0.4
    assert d["plan_snapshot"]["periods"][0]["action"] == "charge"
    assert [r["index"] for r in d["confirmed_records"]] == [0]
    assert d["confirmed_records"][0]["stored_at"].endswith("Z")
    assert d["confirmed_audit"][0]["index"] == 0
    assert d["confirmed_audit"][0]["anomalous"] is False
    assert d["updated_at"] == d["confirmed_records"][0]["stored_at"]

    # 空会话也可查
    sid_empty = _create()["session_id"]
    d0 = client.get(f"/dispatch/sessions/{sid_empty}").json()
    assert d0["status"] == "awaiting_records"
    assert d0["confirmed_records"] == []
    assert d0["confirmed_audit"] == []
    assert d0["final_soc_kwh"] is None
    assert d0["updated_at"] is None


def test_persistence_across_restart(tmp_path):
    """进程重启（同路径重新打开存储）后会话与已保存记录仍在，且可继续追加。"""
    db_path = str(tmp_path / "persist.db")
    configure_store(db_path)
    plan = _plan()
    r = client.post(
        "/dispatch/sessions",
        json={"dispatch": DISPATCH, "plan_snapshot": plan, "audit_tolerance_kwh": TOL},
    )
    sid = r.json()["session_id"]
    assert _append(sid, 0, RECORDS[:2]).status_code == 200

    # 模拟重启：丢弃全局单例、按同一路径重新打开
    configure_store(db_path)

    d = client.get(f"/dispatch/sessions/{sid}").json()
    assert d["confirmed_periods"] == 2
    assert d["next_index"] == 2
    assert d["status"] == "in_progress"
    assert d["plan_snapshot"] == plan
    assert [r["record"]["soc_end_kwh"] for r in d["confirmed_records"]] == [0.2, 0.1]

    # 已保存批次原样重试仍幂等，并能接着报余段至满期
    assert _append(sid, 0, copy.deepcopy(RECORDS[:2])).json()["result"] == "deduped"
    tail = _append(sid, 2, RECORDS[2:])
    assert tail.status_code == 200
    assert tail.json()["status"] == "complete"


def test_frozen_receipt_survives_restart_and_later_batches(tmp_path):
    """重启与后续批次入库后重试：仍返回该批首次成功时冻结的回执。"""
    db_path = str(tmp_path / "receipts.db")
    configure_store(db_path)
    sid = _create()["session_id"]
    first = _append(sid, 0, RECORDS[:2]).json()

    configure_store(db_path)  # 模拟重启
    assert _append(sid, 2, RECORDS[2:]).json()["status"] == "complete"

    configure_store(db_path)  # 再次重启
    again = _append(sid, 0, copy.deepcopy(RECORDS[:2])).json()
    assert again["result"] == "deduped"
    for key in (
        "confirmed_periods", "next_index", "status",
        "batch_audit", "first_record_soc_gap_kwh", "final_soc_kwh",
    ):
        assert again[key] == first[key], key
    # 会话实际进度仍是满期（回执冻结不等于进度回退）
    d = client.get(f"/dispatch/sessions/{sid}").json()
    assert d["confirmed_periods"] == 4
    assert d["status"] == "complete"


def test_legacy_db_without_batch_boundaries(tmp_path):
    """旧库只有逐段记录（无批次边界表数据）：查询与从 next_index 续报保留；
    无法确认历史批次边界时不按前缀猜测去重，重叠上报一律 409。"""
    db_path = str(tmp_path / "legacy.db")
    plan = _plan()

    # 手工构造旧版库：只有 sessions / session_records 两张表与逐段记录
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
    sid = "legacy-session-id"
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
        (sid, json.dumps(DISPATCH), json.dumps(plan), TOL, 4, "2026-01-01T00:00:00Z"),
    )
    for idx, rec in enumerate(RECORDS[:2]):
        conn.execute(
            "INSERT INTO session_records VALUES (?, ?, ?, ?)",
            (sid, idx, json.dumps(rec), "2026-01-01T00:00:01Z"),
        )
    conn.commit()
    conn.close()

    configure_store(db_path)  # 新版代码打开旧库：批次表自动补建为空表

    # 查询能力保留：原计划、连续记录、下一下标都在
    d = client.get(f"/dispatch/sessions/{sid}").json()
    assert d["confirmed_periods"] == 2
    assert d["next_index"] == 2
    assert d["status"] == "in_progress"
    assert [r["index"] for r in d["confirmed_records"]] == [0, 1]

    # 历史批次边界无法确认：即使内容与已保存记录逐条相同也不去重 → 409
    r = _append(sid, 0, copy.deepcopy(RECORDS[:2]))
    assert r.status_code == 409
    assert r.json()["error"] == "conflict"
    r = _append(sid, 0, RECORDS[:1])
    assert r.status_code == 409

    # 从 next_index 续报能力保留：新批次正常入库并登记边界
    r = _append(sid, 2, RECORDS[2:])
    assert r.status_code == 200, r.text
    assert r.json()["result"] == "accepted"
    assert r.json()["status"] == "complete"

    # 新批次的边界已确认：原样重试幂等，回执冻结
    tail = r.json()
    again = _append(sid, 2, copy.deepcopy(RECORDS[2:])).json()
    assert again["result"] == "deduped"
    assert again["confirmed_periods"] == tail["confirmed_periods"] == 4
    assert client.get(f"/dispatch/sessions/{sid}").json()["confirmed_periods"] == 4


def test_cross_process_concurrent_append(tmp_path):
    """跨进程并发同起点上报：内容相同 → accepted+deduped；内容不同 → 仅一份生效。"""
    import multiprocessing

    def worker(db_path, sid, records, queue):
        from app.sessions import append_batch as _append_batch
        from app.storage import configure_store as _configure

        _configure(db_path)
        try:
            r = _append_batch(sid, 0, records)
            queue.put(r["result"])
        except Exception as exc:  # noqa: BLE001
            queue.put(getattr(exc, "code", "error"))

    def run_race(records_list):
        db_path = str(tmp_path / f"xproc-{len(records_list)}-{run_race.n}.db")
        run_race.n += 1
        configure_store(db_path)
        sid = _create()["session_id"]
        ctx = multiprocessing.get_context("fork")
        queue = ctx.Queue()
        procs = [
            ctx.Process(target=worker, args=(db_path, sid, copy.deepcopy(recs), queue))
            for recs in records_list
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(30)
            assert p.exitcode == 0
        outcomes = sorted(queue.get() for _ in procs)
        configure_store(db_path)  # 主进程重新打开，核对落库结果
        return outcomes, sid

    run_race.n = 0

    # 同内容并发：恰好一个 accepted，其余 deduped（同一冻结回执）
    outcomes, sid = run_race([RECORDS[:2], RECORDS[:2]])
    assert outcomes == ["accepted", "deduped"]
    assert client.get(f"/dispatch/sessions/{sid}").json()["confirmed_periods"] == 2

    # 不同内容并发：恰好一个 accepted，其余 conflict，记录与进度不变
    alt = copy.deepcopy(RECORDS[:2])
    alt[1]["grid_purchase_energy_kwh"] = 0.012
    outcomes, sid = run_race([RECORDS[:2], alt])
    assert outcomes == ["accepted", "conflict"]
    d = client.get(f"/dispatch/sessions/{sid}").json()
    assert d["confirmed_periods"] == 2
    saved = d["confirmed_records"][1]["record"]["grid_purchase_energy_kwh"]
    assert saved in (0.01, 0.012)  # 只有一份生效，且与 accepted 的那份一致


def test_unfinished_prefix_feeds_replan():
    """未完成前缀可直接交给现有 /dispatch/replan 续排。"""
    sid = _create()["session_id"]
    assert _append(sid, 0, RECORDS[:2]).status_code == 200
    d = client.get(f"/dispatch/sessions/{sid}").json()

    rr = client.post(
        "/dispatch/replan",
        json={
            "dispatch": d["dispatch"],
            "plan_snapshot": d["plan_snapshot"],
            "audit_tolerance_kwh": d["audit_tolerance_kwh"],
            "executed_records": [r["record"] for r in d["confirmed_records"]],
            "remaining_prices": [5.0, 0.0],
            "remaining_base_load_kw": [0.2, 0.2],
            "remaining_pv_kw": [0.0, 0.0],
        },
    )
    assert rr.status_code == 200, rr.text
    body = rr.json()
    assert body["executed_periods"] == 2
    assert body["remaining_periods"] == 2
    assert body["remaining_dispatch"]["initial_soc_kwh"] == 0.1


def test_complete_records_feed_settlement():
    """满期记录可直接交给现有 /dispatch/settlement 结算。"""
    sid = _create()["session_id"]
    assert _append(sid, 0, RECORDS[:3]).status_code == 200
    assert _append(sid, 3, RECORDS[3:]).status_code == 200
    d = client.get(f"/dispatch/sessions/{sid}").json()
    assert d["status"] == "complete"

    sr = client.post(
        "/dispatch/settlement",
        json={
            "dispatch": d["dispatch"],
            "plan_snapshot": d["plan_snapshot"],
            "settlement_tolerance_kwh": TOL,
            "records": [r["record"] for r in d["confirmed_records"]],
        },
    )
    assert sr.status_code == 200, sr.text
    assert sr.json()["status"] == "settled"
    assert sr.json()["settled_periods"] == 4


def test_list_sessions_and_404():
    """会话概览列表按创建时间返回；未知会话 404。"""
    s1 = _create()["session_id"]
    s2 = _create()["session_id"]
    assert _append(s1, 0, RECORDS[:1]).status_code == 200

    lst = client.get("/dispatch/sessions").json()["sessions"]
    ids = [s["session_id"] for s in lst]
    assert s1 in ids and s2 in ids
    by_id = {s["session_id"]: s for s in lst}
    assert by_id[s1]["confirmed_periods"] == 1
    assert by_id[s1]["next_index"] == 1
    assert by_id[s1]["status"] == "in_progress"
    assert by_id[s1]["final_soc_kwh"] == 0.2
    assert by_id[s1]["initial_soc_kwh"] == 0.0
    assert by_id[s2]["status"] == "awaiting_records"
    assert by_id[s2]["final_soc_kwh"] is None
    assert by_id[s2]["updated_at"] is None

    assert client.get("/dispatch/sessions/nope").status_code == 404
    assert client.post("/dispatch/sessions/nope/batches", json={"start_index": 0, "records": RECORDS[:1]}).status_code == 404


def test_invalid_batch_inputs_rejected():
    """空批、SOC 超容量、负值等非法输入 → 422 invalid_input，且不影响会话。"""
    sid = _create()["session_id"]

    r = client.post(
        f"/dispatch/sessions/{sid}/batches",
        json={"start_index": 0, "records": []},
    )
    assert r.status_code == 422

    bad = copy.deepcopy(RECORDS[:1])
    bad[0]["soc_end_kwh"] = 0.5  # 超容量 0.4
    r = _append(sid, 0, bad)
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_input"

    bad = copy.deepcopy(RECORDS[:1])
    bad[0]["base_load_kw"] = -0.1
    assert _append(sid, 0, bad).status_code == 422

    r = client.post(
        f"/dispatch/sessions/{sid}/batches",
        json={"start_index": -1, "records": RECORDS[:1]},
    )
    assert r.status_code == 422

    raw = json.dumps({"start_index": 0, "records": RECORDS[:1]})
    assert client.get(f"/dispatch/sessions/{sid}").json()["confirmed_periods"] == 0
    assert raw is not None  # 占位：确认无副作用


def test_existing_endpoints_unchanged():
    """新增会话能力不影响既有四个接口。"""
    assert client.get("/healthz").json() == {"status": "ok"}
    r = client.post("/dispatch", json=DISPATCH)
    assert r.status_code == 200 and len(r.json()["periods"]) == 4
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
    assert r.status_code == 200 and r.json()["anomalous"] is False
