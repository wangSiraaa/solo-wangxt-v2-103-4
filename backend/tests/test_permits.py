"""临时旁路许可验收测试（API 端到端 + 求解器单元）。

覆盖验收标准：
1) 有效许可让指定旁路用于保供且目标仍被隔离；
2) 许可到期后关联方案明确过期，历史快照保留旧路径，重新计算可得当前结论；
3) 重叠许可创建被拒绝、锁阀条件改变后激活被拒绝，且原许可不受损；
4) 重复撤销、迟到的激活事件、乱序时钟事件结果确定，审计顺序与哈希链正确，
   篡改历史可被检出。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.isolation import compute_isolation
from app.main import app
from app.models import PermitEvent, Valve
from app.seed import seed_database

# 演示时间线（UTC）
T_PRE = "2026-03-01T07:00:00Z"   # 窗口前
T0 = "2026-03-01T08:00:00Z"     # 窗口开始
T_IN = "2026-03-01T08:30:00Z"   # 窗口内
T1 = "2026-03-01T12:00:00Z"     # 窗口结束
T_LATE = "2026-03-01T13:00:00Z"  # 窗口后

BYPASS = ["V_BP_IN", "V_BP_OUT"]


def _reset_seed():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()
    seed_database(db)
    db.close()


@pytest.fixture()
def client():
    _reset_seed()
    with TestClient(app) as c:
        yield c


def _set_clock(client: TestClient, iso: str | None):
    r = client.post("/api/clock", json={"now": iso})
    assert r.status_code == 200, r.text
    return r.json()


def _close_valves(client: TestClient, *valve_ids: str):
    for vid in valve_ids:
        r = client.post(f"/api/valves/{vid}/state", json={"is_open": False})
        assert r.status_code == 200, r.text


def _make_permit(client: TestClient, starts=T0, ends=T1, valves=BYPASS, purpose="演练：旁路保供"):
    r = client.post(
        "/api/permits",
        json={
            "target_id": "T",
            "bypass_valve_ids": valves,
            "starts_at": starts,
            "ends_at": ends,
            "purpose": purpose,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _setup_permit_scenario(client: TestClient):
    """环网阀与旁路阀关闭：无许可时隔离 T 必然断供 P2。"""
    _set_clock(client, T_PRE)
    _close_valves(client, "V_LK1", "V_BP_IN", "V_BP_OUT")


# ---------------- 验收 1：有效许可让指定旁路用于保供且目标仍被隔离 ----------------

def test_permit_enables_bypass_supply_while_target_isolated(client):
    _setup_permit_scenario(client)

    # 无许可：关 V_TIN+V_TOUT 会断供 P2（环网与旁路均不可用）-> 无可行方案
    r = client.post("/api/isolation", json={"target_id": "T"}).json()
    assert r["feasible"] is False
    assert r["applied_permits"] == []

    permit = _make_permit(client)
    assert permit["status"] == "draft"
    assert permit["precondition_locks"] == {v: False for v in [
        "V0", "V1", "V_BP_IN", "V_BP_OUT", "V_LK1", "V_LK2", "V_P1", "V_P2", "V_TIN", "V_TOUT",
    ]}
    assert permit["topology_hash"] and permit["state_hash"]

    _set_clock(client, T_IN)
    activated = client.post(f"/api/permits/{permit['id']}/activate", json={}).json()
    assert activated["status"] == "active"
    assert activated["applicable_now"] is True

    # 许可生效：旁路被纳入可用路径，P2 经旁路保供，目标仍被隔离
    r = client.post("/api/isolation", json={"target_id": "T"}).json()
    assert r["feasible"] is True
    assert r["best_solution"] == ["V_TIN", "V_TOUT"]
    assert r["residual_path"] is None                      # 目标确已隔离
    assert [p["id"] for p in r["applied_permits"]] == [permit["id"]]
    assert r["permit_note"]
    sol = r["solutions"][0]
    assert sol["uses_permit"] is True
    assert sorted(sol["permit_valves_used"]) == BYPASS
    p2_path = sol["supply_paths"]["P2"]
    assert "BP" in p2_path                                 # 旁路用于保供
    assert sol["supply_uses_permit"]["P2"] is True
    assert sol["supply_uses_permit"]["P1"] is False
    # 许可阀门标记且不参与候选关闭
    cand = {v["id"]: v for v in r["candidate_valves"]}
    assert cand["V_BP_IN"]["permit_enabled"] is True
    assert cand["V_BP_OUT"]["permit_enabled"] is True

    # 方案被持久化并关联许可
    plans = client.get("/api/plans").json()
    assert len(plans) == 1
    assert plans[0]["status"] == "current"
    assert plans[0]["permit_ids"] == [permit["id"]]
    assert plans[0]["close_valves"] == ["V_TIN", "V_TOUT"]


def test_solver_permit_valves_unit():
    """求解器层：许可阀门临时可用且不可关闭；无许可则不可行。"""
    _reset_seed()
    db = SessionLocal()
    try:
        for vid in ("V_LK1", "V_BP_IN", "V_BP_OUT"):
            db.get(Valve, vid).is_open = False
        db.commit()

        r0 = compute_isolation(db, "T")
        assert r0["feasible"] is False

        r1 = compute_isolation(db, "T", permit_valves={"V_BP_IN", "V_BP_OUT"})
        assert r1["feasible"] is True
        assert r1["best_solution"] == ["V_TIN", "V_TOUT"]
        assert r1["solutions"][0]["uses_permit"] is True
        assert "BP" in r1["solutions"][0]["supply_paths"]["P2"]
        # 许可阀门仍在视图中但被标记为许可启用，且不会被方案关闭
        all_ids = {v["id"] for v in r1["candidate_valves"]}
        assert {"V_BP_IN", "V_BP_OUT"} <= all_ids
        enabled = {v["id"] for v in r1["candidate_valves"] if v["permit_enabled"]}
        assert enabled == {"V_BP_IN", "V_BP_OUT"}
        for sol in r1["solutions"]:
            assert not {"V_BP_IN", "V_BP_OUT"} & set(sol["close_valves"])
    finally:
        db.close()


# ---------------- 验收 2：到期后关联方案明确过期、旧路径保留、可重新计算 ----------------

def test_permit_expiry_marks_plan_expired_and_keeps_snapshot(client):
    _setup_permit_scenario(client)
    permit = _make_permit(client)
    _set_clock(client, T_IN)
    client.post(f"/api/permits/{permit['id']}/activate", json={})
    r = client.post("/api/isolation", json={"target_id": "T"}).json()
    assert r["feasible"] is True
    plan_id = client.get("/api/plans").json()[0]["id"]

    # 时钟越过窗口结束时刻（模拟“刷新后”再查看）
    _set_clock(client, T_LATE)
    permit_after = client.get(f"/api/permits/{permit['id']}").json()
    assert permit_after["status"] == "expired"
    assert permit_after["applicable_now"] is False

    plans = client.get("/api/plans").json()
    assert plans[0]["status"] == "expired"
    assert plans[0]["status_changed_at"] is not None

    # 历史快照：旧路径（经旁路保供 P2）原样保留
    snap = client.get(f"/api/plans/{plan_id}").json()
    assert snap["status"] == "expired"
    snap_sol = snap["result"]["solutions"][0]
    assert "BP" in snap_sol["supply_paths"]["P2"]
    assert snap_sol["uses_permit"] is True
    assert snap["result"]["applied_permits"][0]["id"] == permit["id"]

    # 审计中出现 expired 事件
    events = client.get(f"/api/permits/{permit['id']}/events").json()
    assert events["chain_valid"] is True
    assert [e["event_type"] for e in events["events"]] == ["created", "activated", "expired"]
    expired_ev = events["events"][-1]
    assert expired_ev["occurred_at"].startswith("2026-03-01T12:00")  # 到期时刻=窗口结束

    # 重新计算入口：当前状态下许可不再应用，结论回到不可行（不静默沿用旧计算）
    r2 = client.post("/api/isolation", json={"target_id": "T"}).json()
    assert r2["feasible"] is False
    assert r2["applied_permits"] == []
    # 显式引用已过期许可 -> 确定性拒绝
    r3 = client.post("/api/isolation", json={"target_id": "T", "permit_id": permit["id"]})
    assert r3.status_code == 409
    assert r3.json()["detail"]["code"] == "PERMIT_NOT_APPLICABLE"


def test_plan_becomes_stale_when_locks_change(client):
    """拓扑/锁阀改变 -> 关联方案转为待复核而不是静默继续。"""
    _setup_permit_scenario(client)
    permit = _make_permit(client)
    _set_clock(client, T_IN)
    client.post(f"/api/permits/{permit['id']}/activate", json={})
    client.post("/api/isolation", json={"target_id": "T"})
    assert client.get("/api/plans").json()[0]["status"] == "current"

    client.post("/api/valves/V_TIN/lock", json={"locked": True})
    plans = client.get("/api/plans").json()
    assert plans[0]["status"] == "stale"

    # 许可本身仍在窗口内但条件不再匹配 -> 不再被应用
    permit_now = client.get(f"/api/permits/{permit['id']}").json()
    assert permit_now["status"] == "active"
    assert permit_now["applicable_now"] is False
    r = client.post("/api/isolation", json={"target_id": "T"}).json()
    assert r["applied_permits"] == []


# ---------------- 验收 3：重叠创建 / 条件变化后激活被拒绝，原许可不受损 ----------------

def test_overlapping_permit_rejected_and_original_intact(client):
    _set_clock(client, T_PRE)
    a = _make_permit(client, starts=T0, ends=T1)

    # 同一旁路（子集也算重叠）+ 重叠窗口 -> 拒绝
    r = client.post(
        "/api/permits",
        json={
            "target_id": "T",
            "bypass_valve_ids": ["V_BP_OUT"],
            "starts_at": "2026-03-01T09:00:00Z",
            "ends_at": "2026-03-01T13:00:00Z",
            "purpose": "重叠窗口",
        },
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "PERMIT_OVERLAP"

    # 原许可不受损：仍为草稿，且窗口错开后可正常创建
    a_now = client.get(f"/api/permits/{a['id']}").json()
    assert a_now["status"] == "draft"
    ok = client.post(
        "/api/permits",
        json={
            "target_id": "T",
            "bypass_valve_ids": ["V_BP_OUT"],
            "starts_at": "2026-03-01T13:00:00Z",
            "ends_at": "2026-03-01T15:00:00Z",
            "purpose": "窗口错开",
        },
    )
    assert ok.status_code == 201, ok.text

    # 非旁路阀门 / 非法窗口 / 未知阀门 -> 400
    bad1 = client.post("/api/permits", json={
        "target_id": "T", "bypass_valve_ids": ["V1"],
        "starts_at": "2026-03-02T08:00:00Z", "ends_at": "2026-03-02T09:00:00Z", "purpose": "",
    })
    assert bad1.status_code == 400 and bad1.json()["detail"]["code"] == "NOT_BYPASS"
    bad2 = client.post("/api/permits", json={
        "target_id": "T", "bypass_valve_ids": BYPASS,
        "starts_at": "2026-03-02T09:00:00Z", "ends_at": "2026-03-02T08:00:00Z", "purpose": "",
    })
    assert bad2.status_code == 400 and bad2.json()["detail"]["code"] == "INVALID_WINDOW"


def test_activation_rejected_after_lock_change_and_permit_unharmed(client):
    _set_clock(client, T_PRE)
    permit = _make_permit(client)
    _set_clock(client, T_IN)

    # 前置锁定状态被改变 -> 激活拒绝，许可保持草稿
    client.post("/api/valves/V_TIN/lock", json={"locked": True})
    r = client.post(f"/api/permits/{permit['id']}/activate", json={})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "CONDITIONS_CHANGED"
    p = client.get(f"/api/permits/{permit['id']}").json()
    assert p["status"] == "draft"

    # 拒绝已留痕；恢复锁定状态后仍可激活（原许可不受损）
    events = client.get(f"/api/permits/{permit['id']}/events").json()["events"]
    assert [e["event_type"] for e in events] == ["created", "activate_rejected"]
    assert events[1]["detail"]["code"] == "CONDITIONS_CHANGED"

    client.post("/api/valves/V_TIN/lock", json={"locked": False})
    ok = client.post(f"/api/permits/{permit['id']}/activate", json={})
    assert ok.status_code == 200
    assert ok.json()["status"] == "active"


def test_activation_rejected_when_bypass_valve_locked(client):
    _set_clock(client, T_PRE)
    _close_valves(client, "V_BP_IN")  # 先关闭再锁定（锁定=保持现状）
    client.post("/api/valves/V_BP_IN/lock", json={"locked": True})
    # 锁定阀门不能纳入许可（创建即拒绝）
    r = client.post("/api/permits", json={
        "target_id": "T", "bypass_valve_ids": BYPASS,
        "starts_at": T0, "ends_at": T1, "purpose": "",
    })
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "VALVE_LOCKED"


def test_activation_rejected_when_validation_fails(client):
    """启用旁路后仍无可行方案（目标隔离/必要供给不满足）-> 激活拒绝。"""
    _set_clock(client, T_PRE)
    # 旁路关闭 + 环网关闭 + 出口阀锁定：许可启用旁路后 P2 仍必断供
    _close_valves(client, "V_LK1", "V_BP_IN", "V_BP_OUT")
    client.post("/api/valves/V_TOUT/lock", json={"locked": True})
    permit = _make_permit(client)
    _set_clock(client, T_IN)
    r = client.post(f"/api/permits/{permit['id']}/activate", json={})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "VALIDATION_FAILED"
    assert client.get(f"/api/permits/{permit['id']}").json()["status"] == "draft"


# ---------------- 验收 4：重复/迟到/乱序事件确定结果 + 审计与快照 ----------------

def test_duplicate_revoke_and_late_activate_are_deterministic(client):
    _setup_permit_scenario(client)
    permit = _make_permit(client)
    pid = permit["id"]
    _set_clock(client, T_IN)
    client.post(f"/api/permits/{pid}/activate", json={"occurred_at": "2026-03-01T09:00:00Z"})
    client.post("/api/isolation", json={"target_id": "T"})

    # 撤销（事件时钟故意填得比激活早 —— 乱序；状态机按到达顺序裁决，仍应撤销成功）
    r = client.post(f"/api/permits/{pid}/revoke", json={"occurred_at": "2026-03-01T08:30:00Z"})
    assert r.status_code == 200
    assert r.json()["status"] == "revoked"

    # 重复撤销 -> 确定性拒绝并留痕
    r2 = client.post(f"/api/permits/{pid}/revoke", json={})
    assert r2.status_code == 409
    assert r2.json()["detail"]["code"] == "ALREADY_CLOSED"
    assert client.get(f"/api/permits/{pid}").json()["status"] == "revoked"

    # 迟到的激活事件 -> 确定性拒绝并留痕
    r3 = client.post(f"/api/permits/{pid}/activate", json={"occurred_at": "2026-03-01T09:30:00Z"})
    assert r3.status_code == 409
    assert r3.json()["detail"]["code"] == "NOT_DRAFT"

    # 审计顺序：按到达顺序 seq 严格递增
    ev = client.get(f"/api/permits/{pid}/events").json()
    assert ev["chain_valid"] is True
    seqs = [e["seq"] for e in ev["events"]]
    assert seqs == sorted(seqs) == [1, 2, 3, 4, 5]
    assert [e["event_type"] for e in ev["events"]] == [
        "created", "activated", "revoked", "revoke_rejected", "activate_rejected",
    ]
    # 乱序的 occurred_at 不改变到达顺序
    assert ev["events"][2]["occurred_at"].startswith("2026-03-01T08:30")
    assert ev["events"][1]["occurred_at"].startswith("2026-03-01T09:00")

    # 关联方案：许可撤销 -> 已过期；历史快照中的旧路径保持不变
    plans = client.get("/api/plans").json()
    assert plans[0]["status"] == "expired"
    snap = client.get(f"/api/plans/{plans[0]['id']}").json()
    assert "BP" in snap["result"]["solutions"][0]["supply_paths"]["P2"]


def test_activate_outside_window_rejected(client):
    _set_clock(client, T_PRE)
    permit = _make_permit(client)
    r = client.post(
        f"/api/permits/{permit['id']}/activate",
        json={"occurred_at": T_LATE},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "NOT_IN_WINDOW"
    assert client.get(f"/api/permits/{permit['id']}").json()["status"] == "draft"


def test_audit_chain_detects_tampering(client):
    _set_clock(client, T_PRE)
    permit = _make_permit(client)
    pid = permit["id"]
    _set_clock(client, T_IN)
    client.post(f"/api/permits/{pid}/activate", json={})
    assert client.get(f"/api/permits/{pid}/events").json()["chain_valid"] is True

    # 直接篡改数据库中的历史事件 -> 哈希链校验失败
    db = SessionLocal()
    try:
        ev = db.query(PermitEvent).filter_by(permit_id=pid, seq=1).one()
        ev.detail = {**ev.detail, "purpose": "被篡改"}
        db.commit()
    finally:
        db.close()
    assert client.get(f"/api/permits/{pid}/events").json()["chain_valid"] is False


def test_reset_revokes_permits_but_keeps_history(client):
    _setup_permit_scenario(client)
    permit = _make_permit(client)
    pid = permit["id"]
    _set_clock(client, T_IN)
    client.post(f"/api/permits/{pid}/activate", json={})
    client.post("/api/isolation", json={"target_id": "T"})

    client.post("/api/reset")
    p = client.get(f"/api/permits/{pid}").json()
    assert p["status"] == "revoked"
    plans = client.get("/api/plans").json()
    assert plans[0]["status"] == "expired"
    events = client.get(f"/api/permits/{pid}/events").json()
    assert events["chain_valid"] is True
    assert events["events"][-1]["detail"]["reason"] == "系统重置"
    # 重置后演示时钟恢复真实时间
    assert client.get("/api/clock").json()["simulated"] is False


def test_revalidate_endpoint_reports_checks(client):
    _setup_permit_scenario(client)
    permit = _make_permit(client)
    pid = permit["id"]
    _set_clock(client, T_IN)
    client.post(f"/api/permits/{pid}/activate", json={})

    report = client.post(f"/api/permits/{pid}/revalidate").json()
    assert report["status"] == "active"
    checks = report["checks"]
    assert checks["window_ok"] and checks["topology_ok"] and checks["state_ok"]
    assert checks["valves_ok"] and checks["solver_feasible"]
    assert checks["target_isolated"] and checks["essentials_supplied"]
    assert checks["applicable_now"] is True

    # 锁阀限制被破坏后：执行中复核立即反映
    client.post("/api/valves/V_BP_IN/lock", json={"locked": True})
    report2 = client.post(f"/api/permits/{pid}/revalidate").json()
    assert report2["checks"]["valves_ok"] is False
    assert report2["checks"]["state_ok"] is False
    assert report2["checks"]["applicable_now"] is False
