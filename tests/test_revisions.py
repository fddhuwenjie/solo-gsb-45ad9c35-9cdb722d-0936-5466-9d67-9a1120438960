"""计量表更正接口行为测试。

覆盖：
- 仅满期会话可更正 / 查询（未满期 422、未知会话 404）；
- 修订号从 0 起、在当前版本上叠加、未指定时段保持原值；
- 整段重核（容量/效率/初始 SOC/审计容差）：能量失衡、购电失衡、相邻 SOC
  断点、首态未接原初始 SOC 均整次拒绝且已有修订不变，原因点名下标；
- 下标重复 / 越界整次拒绝（点名下标）；
- 期望修订号过期 → 409 冲突及当前号；
- 原子落盘与重启后可查询、可继续更正；
- 最初上报记录、批次重试判定与首收回执不受更正改写；
- 查询返回原始 / 当前修订记录、差异下标、历史与可提交结算接口的完整请求数据；
- 并发同期望修订号提交只有一份生效。
"""

import copy
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.revisions import submit_revision
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
    configure_store(str(tmp_path / "revisions.db"))
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


def _submit(sid, expected_revision, changes):
    return client.post(
        f"/dispatch/sessions/{sid}/revisions",
        json={"expected_revision": expected_revision, "changes": changes},
    )


def _get(sid):
    return client.get(f"/dispatch/sessions/{sid}/revisions")


def _ch(index, record):
    return {"index": index, "record": record}


def test_revision_zero_query_returns_original_ledger():
    """无更正时：修订号 0、修订台账即最初上报、差异为空、结算请求可直接用。"""
    sid, _ = _create_complete()
    r = _get(sid)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["current_revision"] == 0
    assert b["changed_indices"] == []
    assert b["revisions"] == []
    assert [x["index"] for x in b["original_records"]] == [0, 1, 2, 3]
    assert [x["index"] for x in b["revised_records"]] == [0, 1, 2, 3]
    for i, rec in enumerate(RECORDS):
        assert b["original_records"][i]["record"] == rec
        assert b["revised_records"][i]["record"] == rec
    assert b["original_records"][0]["stored_at"].endswith("Z")
    assert b["final_soc_kwh"] == 0.0
    assert all(not a["anomalous"] for a in b["revised_audit"])

    settlement_request = b["settlement_request"]
    assert settlement_request["dispatch"]["prices"] == DISPATCH["prices"]
    assert settlement_request["plan_snapshot"]["summary"]["num_periods"] == 4
    assert settlement_request["settlement_tolerance_kwh"] == TOL
    assert settlement_request["records"] == RECORDS


def test_first_revision_full_ledger_recheck_and_atomic_persist():
    """首次更正（期望修订号 0）：自洽替换通过，修订号变 1，原始证据保留。"""
    sid, _ = _create_complete()
    # t1 实际少放：AC 0.045 → 电芯 0.05，SOC 0.2→0.15，购电 0.1-0.045=0.055
    new1 = dict(RECORDS[1])
    new1.update(
        discharge_ac_energy_kwh=0.045, grid_purchase_energy_kwh=0.055,
        soc_start_kwh=0.2, soc_end_kwh=0.15,
    )
    # t2 自洽承接 0.15：放 0.09 AC → 电芯 0.1 → 末态 0.05
    new2 = dict(RECORDS[2])
    new2.update(soc_start_kwh=0.15, soc_end_kwh=0.05)
    # t3 自洽承接 0.05：静置，首尾都 0.05
    new3 = dict(RECORDS[3])
    new3.update(soc_start_kwh=0.05, soc_end_kwh=0.05)

    r = _submit(sid, 0, [_ch(1, new1), _ch(3, new3), _ch(2, new2)])  # 乱序提交也按下标落位
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["current_revision"] == 1
    assert b["changed_indices"] == [1, 2, 3]
    assert b["revised_records"][1]["record"] == new1
    assert b["revised_records"][2]["record"] == new2
    assert b["revised_records"][3]["record"] == new3
    # 未指定下标 0 保持原值
    assert b["revised_records"][0]["record"] == RECORDS[0]
    # 最初上报证据原样保留
    for i, rec in enumerate(RECORDS):
        assert b["original_records"][i]["record"] == rec
    assert b["final_soc_kwh"] == 0.05
    assert all(not a["anomalous"] for a in b["revised_audit"])
    assert len(b["revisions"]) == 1
    h = b["revisions"][0]
    assert h["revision_no"] == 1
    assert h["changed_indices"] == [1, 2, 3]
    assert h["created_at"].endswith("Z")

    # GET 与 POST 返回一致
    assert _get(sid).json()["current_revision"] == 1


def test_revisions_stack_on_current_version_unspecified_kept():
    """后续更正在当前版本上叠加；未指定时段保持当前值；差异以原始记录为基准。"""
    sid, _ = _create_complete()
    new1 = dict(RECORDS[1])
    new1.update(
        discharge_ac_energy_kwh=0.045, grid_purchase_energy_kwh=0.055,
        soc_end_kwh=0.15,
    )
    new2 = dict(RECORDS[2])
    new2.update(soc_start_kwh=0.15, soc_end_kwh=0.05)
    new3 = dict(RECORDS[3])
    new3.update(soc_start_kwh=0.05, soc_end_kwh=0.05)
    assert _submit(sid, 0, [_ch(1, new1), _ch(2, new2), _ch(3, new3)]).status_code == 200

    # 修订 2：只把 t3 改成另一个与 0.05 自洽的静置读数（购电微改），
    # t1/t2 保持修订 1 的值（不回退原始）
    new3_v2 = dict(new3)
    new3_v2["base_load_kw"] = 0.22
    new3_v2["grid_purchase_energy_kwh"] = 0.11
    r = _submit(sid, 1, [_ch(3, new3_v2)])
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["current_revision"] == 2
    assert b["revised_records"][1]["record"] == new1
    assert b["revised_records"][2]["record"] == new2
    assert b["revised_records"][3]["record"] == new3_v2
    assert b["revised_records"][0]["record"] == RECORDS[0]
    assert b["changed_indices"] == [1, 2, 3]
    assert [h["revision_no"] for h in b["revisions"]] == [1, 2]
    assert b["revisions"][1]["changed_indices"] == [3]

    # 修订 3：把 t1/t2/t3 整链改回原始值 → 差异下标清空（修订号仍叠加）
    r = _submit(
        sid, 2,
        [_ch(1, RECORDS[1]), _ch(2, RECORDS[2]), _ch(3, RECORDS[3])],
    )
    assert r.status_code == 200
    b = r.json()
    assert b["current_revision"] == 3
    assert b["changed_indices"] == []
    for i, rec in enumerate(RECORDS):
        assert b["revised_records"][i]["record"] == rec
    assert len(b["revisions"]) == 3


def test_stale_expected_revision_conflict_with_current_no():
    """期望修订号过期：409 revision_conflict，错误体给出当前号，且不写入。"""
    sid, _ = _create_complete()
    new3 = dict(RECORDS[3])  # t3 单独替换：与原值自洽（静置 0→0），只改读数
    new3["base_load_kw"] = 0.22
    new3["grid_purchase_energy_kwh"] = 0.11
    assert _submit(sid, 0, [_ch(3, new3)]).status_code == 200

    r = _submit(sid, 0, [_ch(3, RECORDS[3])])
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "revision_conflict"
    assert body["current_revision"] == 1
    assert "当前修订号为 1" in body["reason"]

    # 冲突没有写入：当前号仍为 1
    assert _get(sid).json()["current_revision"] == 1
    # 用当前号重试即成功
    assert _submit(sid, 1, [_ch(3, RECORDS[3])]).status_code == 200
    assert _get(sid).json()["current_revision"] == 2


def test_duplicate_index_rejected_whole_request():
    """同一次提交下标重复：整次拒绝、点名下标，已有修订不变。"""
    sid, _ = _create_complete()
    rec = dict(RECORDS[3])
    r = _submit(sid, 0, [_ch(3, rec), _ch(3, rec)])
    assert r.status_code == 422
    assert r.json()["error"] == "revision_duplicate_index"
    assert "下标 3" in r.json()["reason"]
    assert _get(sid).json()["current_revision"] == 0


def test_out_of_range_index_rejected():
    """下标越界（上/下界）：整次拒绝、点名下标。"""
    sid, _ = _create_complete()
    r = _submit(sid, 0, [_ch(4, RECORDS[0])])
    assert r.status_code == 422
    assert r.json()["error"] == "revision_invalid_index"
    assert "下标 4" in r.json()["reason"]
    assert "[0, 4)" in r.json()["reason"]
    assert _get(sid).json()["current_revision"] == 0


def test_energy_imbalance_rejected_keeps_existing_revision():
    """整段重核：替换记录电芯能量失衡 → revision_audit_failed，已有修订不变。"""
    sid, _ = _create_complete()
    bad3 = dict(RECORDS[3])
    bad3["soc_end_kwh"] = 0.1  # 静置但末态跳 0.1：电芯能量残差 0.1
    r = _submit(sid, 0, [_ch(3, bad3)])
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "revision_audit_failed"
    assert "下标 3" in body["reason"]
    assert "电芯能量残差" in body["reason"]
    assert _get(sid).json()["current_revision"] == 0

    # 已有修订 1 后再提交坏修订：拒绝且修订号仍停在 1
    good3 = dict(RECORDS[3])
    good3["base_load_kw"] = 0.22
    good3["grid_purchase_energy_kwh"] = 0.11
    assert _submit(sid, 0, [_ch(3, good3)]).status_code == 200
    r = _submit(sid, 1, [_ch(3, bad3)])
    assert r.status_code == 422
    assert r.json()["error"] == "revision_audit_failed"
    b = _get(sid).json()
    assert b["current_revision"] == 1
    assert b["revised_records"][3]["record"] == good3  # 现有版本未被污染


def test_grid_imbalance_rejected():
    """购电量能量失衡整次拒绝并点名问题下标。"""
    sid, _ = _create_complete()
    bad3 = dict(RECORDS[3])
    bad3["grid_purchase_energy_kwh"] = 0.5  # 期望 0.1，残差 0.4
    r = _submit(sid, 0, [_ch(3, bad3)])
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "revision_audit_failed"
    assert "下标 3" in body["reason"]
    assert "购电量残差" in body["reason"]
    assert _get(sid).json()["current_revision"] == 0


def test_soc_breakpoint_rejected_and_must_fix_full_chain():
    """相邻 SOC 断点整次拒绝；只改中间一条而不修衔接链时同样拒绝。"""
    sid, _ = _create_complete()
    # 只改 t2 首态为 0.15，但 t1 末态仍是 0.1 → 1→2 衔接断裂
    bad2 = dict(RECORDS[2])
    bad2.update(soc_start_kwh=0.15, soc_end_kwh=0.05)
    r = _submit(sid, 0, [_ch(2, bad2)])
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "revision_audit_failed"
    assert "下标 2" in body["reason"]
    assert "SOC 不衔接" in body["reason"]
    assert _get(sid).json()["current_revision"] == 0

    # 改成完整衔接链即通过
    new1 = dict(RECORDS[1])
    new1.update(
        discharge_ac_energy_kwh=0.045, grid_purchase_energy_kwh=0.055, soc_end_kwh=0.15
    )
    new3 = dict(RECORDS[3])
    new3.update(soc_start_kwh=0.05, soc_end_kwh=0.05)
    r = _submit(sid, 0, [_ch(1, new1), _ch(2, bad2), _ch(3, new3)])
    assert r.status_code == 200
    assert r.json()["changed_indices"] == [1, 2, 3]


def test_revision_anchor_gap_when_period_zero_changed():
    """修订改到第 0 条且首态偏离原初始 SOC 超容差：revision_anchor_gap。"""
    sid, _ = _create_complete()
    bad0 = dict(RECORDS[0])
    bad0["soc_start_kwh"] = 0.05  # 原初始 SOC 0.0，偏差 0.05 > 0.01
    r = _submit(sid, 0, [_ch(0, bad0)])
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "revision_anchor_gap"
    assert "下标 0" in body["reason"]
    assert "原初始 SOC" in body["reason"]
    assert _get(sid).json()["current_revision"] == 0

    # 容差边界：首态偏差恰好等于容差允许（严格大于才判超差）；
    # 用算术干净的全静置链（首态 0.01、首尾 SOC 一致、购电恒为 0.1）
    idle = lambda: dict(
        base_load_kw=0.2, pv_kw=0.0, charge_ac_energy_kwh=0.0,
        discharge_ac_energy_kwh=0.0, grid_purchase_energy_kwh=0.1,
        soc_start_kwh=TOL, soc_end_kwh=TOL,
    )
    r = _submit(sid, 0, [_ch(i, idle()) for i in range(4)])
    assert r.status_code == 200, r.text
    assert r.json()["current_revision"] == 1
    assert r.json()["changed_indices"] == [0, 1, 2, 3]


def test_invalid_replacement_record_rejected():
    """替换记录负值 / SOC 超容量：invalid_input 且点名下标。"""
    sid, _ = _create_complete()
    bad = dict(RECORDS[2])
    bad["grid_purchase_energy_kwh"] = -0.01
    r = _submit(sid, 0, [_ch(2, bad)])
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_input"
    assert "下标 2" in r.json()["reason"]

    bad = dict(RECORDS[2])
    bad["soc_end_kwh"] = 0.5  # 容量 0.4
    r = _submit(sid, 0, [_ch(2, bad)])
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_input"
    assert "下标 2" in r.json()["reason"]
    assert _get(sid).json()["current_revision"] == 0

    # 空 changes / 结构错误由请求模型层拦截为 422 invalid_input
    r = client.post(
        f"/dispatch/sessions/{sid}/revisions",
        json={"expected_revision": 0, "changes": []},
    )
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_input"

    r = client.post(
        f"/dispatch/sessions/{sid}/revisions",
        json={"expected_revision": -1, "changes": [_ch(3, RECORDS[3])]},
    )
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_input"


def test_only_complete_session_can_be_revised():
    """未满期会话更正与查询均 422 revision_not_complete。"""
    sid = client.post(
        "/dispatch/sessions",
        json={"dispatch": DISPATCH, "plan_snapshot": _plan(), "audit_tolerance_kwh": TOL},
    ).json()["session_id"]
    assert client.post(
        f"/dispatch/sessions/{sid}/batches",
        json={"start_index": 0, "records": RECORDS[:2]},
    ).status_code == 200

    r = _get(sid)
    assert r.status_code == 422
    assert r.json()["error"] == "revision_not_complete"

    r = _submit(sid, 0, [_ch(0, RECORDS[0])])
    assert r.status_code == 422
    assert r.json()["error"] == "revision_not_complete"

    # 空会话同样不允许
    sid_empty = client.post(
        "/dispatch/sessions",
        json={"dispatch": DISPATCH, "plan_snapshot": _plan(), "audit_tolerance_kwh": TOL},
    ).json()["session_id"]
    assert _get(sid_empty).status_code == 422


def test_unknown_session_returns_404():
    assert _get("does-not-exist").status_code == 404
    r = _submit("does-not-exist", 0, [_ch(0, RECORDS[0])])
    assert r.status_code == 404
    assert r.json()["error"] == "session_not_found"


def test_original_evidence_batch_retry_and_receipt_unchanged():
    """更正不改写：原会话查询仍是最初记录；批次重试仍 deduped 且回执冻结。"""
    sid, detail = _create_complete()
    first_batch_receipt = client.post(
        f"/dispatch/sessions/{sid}/batches",
        json={"start_index": 0, "records": RECORDS[:2]},
    ).json()
    assert first_batch_receipt["result"] == "deduped"
    assert first_batch_receipt["final_soc_kwh"] == 0.1

    new3 = dict(RECORDS[3])
    new3.update(soc_start_kwh=0.0, soc_end_kwh=0.0, base_load_kw=0.22,
                grid_purchase_energy_kwh=0.11)
    assert _submit(sid, 0, [_ch(3, new3)]).status_code == 200

    # 原会话查询：confirmed_records 仍是最初上报，current_revision 仅作展示
    d = client.get(f"/dispatch/sessions/{sid}").json()
    assert d["current_revision"] == 1
    assert d["confirmed_records"][3]["record"] == RECORDS[3]
    assert [x["record"] for x in d["confirmed_records"]] == RECORDS

    # 批次重试判定与首收回执继续使用最初上报记录，不随后续修订漂移
    retry = client.post(
        f"/dispatch/sessions/{sid}/batches",
        json={"start_index": 0, "records": RECORDS[:2]},
    ).json()
    assert retry["result"] == "deduped"
    for key in ("confirmed_periods", "next_index", "status", "final_soc_kwh"):
        assert retry[key] == first_batch_receipt[key]
    assert retry["final_soc_kwh"] == 0.1

    # 原始记录即使内容与修订台账冲突，仍按最初上报去重（不因修订而变冲突）
    retry2 = client.post(
        f"/dispatch/sessions/{sid}/batches",
        json={"start_index": 2, "records": RECORDS[2:]},
    )
    assert retry2.json()["result"] == "deduped"


def test_revised_ledger_settlement_request_accepted_by_settlement():
    """查询返回的 settlement_request 可直接提交现有结算接口并得到修订口径结果。"""
    sid, _ = _create_complete()
    # 计量表更正：t1 实际基础负荷高于原记录（0.2→0.22），购电量同步更正
    # 0.01→0.02（期望购电 = (0.22-0)×0.5 − 0.09 = 0.02）；SOC/充放电轨迹不变，
    # 因此结算接口的计划 SOC 轨迹核对仍通过，读数偏差照常全额结算。
    new1 = dict(RECORDS[1])
    new1.update(base_load_kw=0.22, grid_purchase_energy_kwh=0.02)
    b = _submit(sid, 0, [_ch(1, new1)]).json()

    sr = client.post("/dispatch/settlement", json=b["settlement_request"])
    assert sr.status_code == 200, sr.text
    settlement = sr.json()
    assert settlement["status"] == "settled"
    assert settlement["settled_periods"] == 4
    # 原始口径实际总费用 0.1（见会话测试）；修订后 t1 多购 0.01kWh × 1 元 = 0.01
    assert settlement["summary"]["actual_total_cost_yuan"] == pytest.approx(0.11, abs=1e-6)

    # 用最初上报记录结算仍是原口径，两者互不影响
    d = client.get(f"/dispatch/sessions/{sid}").json()
    sr_orig = client.post(
        "/dispatch/settlement",
        json={
            "dispatch": d["dispatch"],
            "plan_snapshot": d["plan_snapshot"],
            "settlement_tolerance_kwh": TOL,
            "records": [r["record"] for r in d["confirmed_records"]],
        },
    )
    assert sr_orig.json()["summary"]["actual_total_cost_yuan"] == pytest.approx(0.1, abs=1e-6)


def test_revisions_persist_across_restart(tmp_path):
    """重启后修订仍可查询，并能在当前版本上继续更正。"""
    db_path = str(tmp_path / "persist-rev.db")
    configure_store(db_path)
    sid, _ = _create_complete()
    new3 = dict(RECORDS[3])
    new3.update(base_load_kw=0.22, grid_purchase_energy_kwh=0.11)
    b1 = _submit(sid, 0, [_ch(3, new3)]).json()
    assert b1["current_revision"] == 1

    configure_store(db_path)  # 模拟进程重启
    g = _get(sid).json()
    assert g["current_revision"] == 1
    assert g["revised_records"][3]["record"] == new3
    assert g["changed_indices"] == [3]
    assert len(g["revisions"]) == 1
    # 原始证据也在
    assert g["original_records"][3]["record"] == RECORDS[3]

    # 旧修订号 0 已过期，必须从当前号 1 继续
    r = _submit(sid, 0, [_ch(3, RECORDS[3])])
    assert r.status_code == 409
    assert r.json()["current_revision"] == 1

    r = _submit(sid, 1, [_ch(3, RECORDS[3])])
    assert r.status_code == 200
    assert r.json()["current_revision"] == 2
    assert r.json()["changed_indices"] == []

    # 再重启一次仍可继续
    configure_store(db_path)
    assert _get(sid).json()["current_revision"] == 2
    assert _submit(sid, 2, [_ch(3, new3)]).json()["current_revision"] == 3


def test_concurrent_revisions_only_one_wins():
    """并发同期望修订号提交：恰好一份 accepted 落盘，其余 409 冲突。"""
    sid, _ = _create_complete()
    outcomes = []

    def worker(record, tag):
        try:
            r = submit_revision(sid, 0, [{"index": 3, "record": copy.deepcopy(record)}])
            outcomes.append((tag, r["current_revision"]))
        except Exception as exc:  # noqa: BLE001
            outcomes.append((tag, getattr(exc, "code", "error")))

    rec_a = dict(RECORDS[3]); rec_a.update(base_load_kw=0.22, grid_purchase_energy_kwh=0.11)
    rec_b = dict(RECORDS[3]); rec_b.update(base_load_kw=0.24, grid_purchase_energy_kwh=0.12)
    t1 = threading.Thread(target=worker, args=(rec_a, "a"))
    t2 = threading.Thread(target=worker, args=(rec_b, "b"))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(tag for tag, _ in outcomes) == ["a", "b"]
    assert {code for _, code in outcomes} == {1, "revision_conflict"}
    # 只有一份生效，修订号只前进一次
    b = _get(sid).json()
    assert b["current_revision"] == 1
    saved_grid = b["revised_records"][3]["record"]["grid_purchase_energy_kwh"]
    assert saved_grid in (0.11, 0.12)


def test_legacy_db_revision_table_auto_created(tmp_path):
    """旧库（无 session_batches / session_revisions）打开即补建：
    历史会话满期后修订查询为修订号 0，并可正常提交首次更正。"""
    import json
    import sqlite3

    from app.storage import configure_store as _configure

    db_path = str(tmp_path / "legacy-rev.db")
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
    sid = "legacy-rev-session"
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

    _configure(db_path)
    g = _get(sid)
    assert g.status_code == 200, g.text
    assert g.json()["current_revision"] == 0
    assert g.json()["revised_records"][0]["record"] == RECORDS[0]

    new3 = dict(RECORDS[3])
    new3.update(base_load_kw=0.22, grid_purchase_energy_kwh=0.11)
    r = _submit(sid, 0, [_ch(3, new3)])
    assert r.status_code == 200, r.text
    assert r.json()["current_revision"] == 1
    assert r.json()["changed_indices"] == [3]


def test_session_detail_and_list_expose_current_revision():
    """会话详情暴露 current_revision；列表接口行为不变（不含修订字段）。"""
    sid, _ = _create_complete()
    assert client.get(f"/dispatch/sessions/{sid}").json()["current_revision"] == 0

    new3 = dict(RECORDS[3])
    new3.update(base_load_kw=0.22, grid_purchase_energy_kwh=0.11)
    _submit(sid, 0, [_ch(3, new3)])
    assert client.get(f"/dispatch/sessions/{sid}").json()["current_revision"] == 1

    lst = client.get("/dispatch/sessions").json()["sessions"]
    target = next(s for s in lst if s["session_id"] == sid)
    assert target["status"] == "complete"
    assert "current_revision" not in target  # 列表概览口径保持不变
