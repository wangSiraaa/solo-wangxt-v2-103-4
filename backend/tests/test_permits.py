"""临时旁路许可验收测试。

场景布置：旁路阀 V_BP_IN/V_BP_OUT 与环网联络阀 V_LK2 均停用（关闭）后，
不启用旁路许可时任何隔离 T 的切法都会断供 P2（无可行方案）；有效许可把
旁路临时纳入可用路径后，关 V_TIN+V_TOUT 即可隔离 T 且 P2 经旁路保供。

验收覆盖：
1. 有效许可让指定旁路用于保供且目标仍被隔离；
2. 许可到期/刷新后关联方案明确过期并给出重新计算入口；
3. 重叠许可、锁定阀条件改变后激活被拒绝且原许可不受损；
4. 重复撤销、迟到激活、历史快照查看后的状态/旧路径/审计顺序。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import PermitEvent
from app.seed import seed_database

T0 = "2099-01-01T09:55:00Z"
T_START = "2099-01-01T10:00:00Z"
T_IN = "2099-01-01T10:05:00Z"
T_IN2 = "2099-01-01T10:10:00Z"
T_END = "2099-01-01T12:00:00Z"
T_LATE = "2099-01-01T12:30:00Z"

BYPASS_VALVES = ["V_BP_IN", "V_BP_OUT"]


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


def _take_bypass_and_ring_out_of_service(client: TestClient) -> None:
    """模拟旁路停用 + 环网联络管检修：此时无许可则无可行隔离方案。"""
    for vid in ("V_BP_IN", "V_BP_OUT", "V_LK2"):
        r = client.post(f"/api/valves/{vid}/state", json={"is_open": False})
        assert r.status_code == 200, r.text


def _create_permit(client: TestClient, start: str = T_START, end: str = T_END, at: str = T0) -> dict:
    r = client.post(
        "/api/permits",
        json={
            "target_id": "T",
            "valve_ids": BYPASS_VALVES,
            "purpose": "维护窗口内临时启用旁路保供",
            "start_at": start,
            "end_at": end,
            "at": at,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _active_permit_setup(client: TestClient) -> tuple[dict, dict]:
    """停用旁路+环网，创建并激活许可，返回 (permit, 计算结果)。"""
    _take_bypass_and_ring_out_of_service(client)
    permit = _create_permit(client)
    r = client.post(f"/api/permits/{permit['id']}/activate", json={"at": T_IN})
    assert r.status_code == 200, r.text
    permit = r.json()
    assert permit["status"] == "active"
    r = client.post(
        "/api/isolation", json={"target_id": "T", "permit_id": permit["id"], "at": T_IN2}
    )
    assert r.status_code == 200, r.text
    return permit, r.json()


# ---------------- 验收 1：有效许可让指定旁路用于保供且目标仍被隔离 ----------------

def test_valid_permit_enables_bypass_supply_and_target_isolated(client: TestClient):
    _take_bypass_and_ring_out_of_service(client)

    # 无许可：旁路与环网均不可用，任何切法都断供 P2 -> 不可行
    r = client.post("/api/isolation", json={"target_id": "T", "at": T_IN2})
    assert r.status_code == 200
    assert r.json()["feasible"] is False

    permit = _create_permit(client)
    assert permit["status"] == "draft"
    assert permit["valve_ids"] == BYPASS_VALVES
    assert permit["precondition_locks"] == {v["id"]: False for v in client.get("/api/topology").json()["valves"]}

    # 草稿状态不能用于计算
    r = client.post("/api/isolation", json={"target_id": "T", "permit_id": permit["id"], "at": T_IN2})
    assert r.status_code == 409

    r = client.post(f"/api/permits/{permit['id']}/activate", json={"at": T_IN})
    assert r.status_code == 200, r.text
    permit = r.json()
    assert permit["status"] == "active"
    assert permit["activated_at"].startswith("2099-01-01T10:05")

    # 应用许可计算：旁路临时纳入可用路径
    r = client.post("/api/isolation", json={"target_id": "T", "permit_id": permit["id"], "at": T_IN2})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["feasible"] is True
    # 目标仍被隔离：方案关闭设备两侧阀门，且不切旁路
    assert res["best_solution"] == ["V_TIN", "V_TOUT"]
    sol = res["solutions"][0]
    assert sol["closes_bypass_valves"] == []
    # P2 经许可旁路保供
    assert "BP" in sol["supply_paths"]["P2"]
    assert sol["permit_bypass_in_supply"] == BYPASS_VALVES
    # 响应携带许可与方案信息及免责声明
    assert res["permit"]["id"] == permit["id"]
    assert res["permit"]["status"] == "active"
    assert res["plan_id"] is not None
    assert "培训模拟" in res["disclaimer"]

    # 许可只是模拟层授权：阀门真实状态未被改动（不输出真实控制指令）
    topo = client.get("/api/topology").json()
    states = {v["id"]: v["is_open"] for v in topo["valves"]}
    assert states["V_BP_IN"] is False and states["V_BP_OUT"] is False

    # 方案已持久化且当前有效
    plans = client.get("/api/plans", params={"at": T_IN2}).json()
    plan = next(p for p in plans if p["id"] == res["plan_id"])
    assert plan["status"] == "valid"
    assert plan["permit_id"] == permit["id"]


def test_activation_rejected_when_infeasible_even_with_permit(client: TestClient):
    """激活前预演：应用许可后仍无法同时隔离目标并保供，则拒绝激活。"""
    _take_bypass_and_ring_out_of_service(client)
    # 再停用 P2 支路阀：P2 无论如何都断供
    client.post("/api/valves/V_P2/state", json={"is_open": False})
    permit = _create_permit(client)
    r = client.post(f"/api/permits/{permit['id']}/activate", json={"at": T_IN})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "infeasible"
    assert client.get(f"/api/permits/{permit['id']}").json()["status"] == "draft"


# ---------------- 验收 2：许可到期后关联方案明确过期 + 重新计算入口 ----------------

def test_expiry_marks_plan_expired_and_recompute_entry(client: TestClient):
    permit, res = _active_permit_setup(client)
    plan_id = res["plan_id"]

    # 窗口内刷新：方案仍有效
    plans = client.get("/api/plans", params={"at": "2099-01-01T11:00:00Z"}).json()
    assert next(p for p in plans if p["id"] == plan_id)["status"] == "valid"

    # 时限到达后刷新：许可与关联方案明确过期，过期时刻恒为窗口结束时刻
    permit_after = client.get(f"/api/permits/{permit['id']}", params={"at": T_LATE}).json()
    assert permit_after["status"] == "expired"
    assert permit_after["expired_at"].startswith("2099-01-01T12:00")
    plans = client.get("/api/plans", params={"at": T_LATE}).json()
    plan = next(p for p in plans if p["id"] == plan_id)
    assert plan["status"] == "expired"
    assert plan["status_reason"] == "permit_expired"

    # 到期事件已持久化进审计链（后续任意读取均可见，顺序确定）
    history = client.get(f"/api/permits/{permit['id']}/history").json()
    assert [e["event_type"] for e in history["events"]] == ["created", "activated", "expired"]
    assert history["events"][-1]["at"].startswith("2099-01-01T12:00")
    assert history["chain_valid"] is True

    # 过期许可不得再用于计算
    r = client.post("/api/isolation", json={"target_id": "T", "permit_id": permit["id"], "at": T_LATE})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "permit_not_active"

    # 重新计算入口：按当前条件（许可已失效 -> 不再纳入旁路）生成新方案
    r = client.post(f"/api/plans/{plan_id}/recompute", json={"at": T_LATE})
    assert r.status_code == 200, r.text
    new_plan = r.json()
    assert new_plan["id"] != plan_id
    assert new_plan["permit_id"] is None
    assert new_plan["feasible"] is False  # 旁路仍停用，回到无可行方案

    # 旧方案快照（含旧路径）原样保留
    old = client.get(f"/api/plans/{plan_id}", params={"at": T_LATE}).json()
    assert old["status"] == "expired"
    assert old["best_solution"] == ["V_TIN", "V_TOUT"]
    assert "BP" in old["supply_paths"]["P2"]


# ---------------- 验收 3：重叠许可 / 锁阀条件改变后激活被拒绝，原许可不受损 ----------------

def test_overlapping_permit_rejected_and_existing_untouched(client: TestClient):
    permit_a = _create_permit(client, start=T_START, end=T_END)

    # 同一旁路、窗口重叠 -> 拒绝创建
    r = client.post(
        "/api/permits",
        json={
            "target_id": "T",
            "valve_ids": BYPASS_VALVES,
            "purpose": "重叠窗口",
            "start_at": "2099-01-01T11:00:00Z",
            "end_at": "2099-01-01T13:00:00Z",
            "at": T0,
        },
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "overlap"

    # 部分共享阀门同样算重叠
    r = client.post(
        "/api/permits",
        json={
            "target_id": "T",
            "valve_ids": ["V_BP_IN"],
            "purpose": "部分重叠",
            "start_at": "2099-01-01T11:00:00Z",
            "end_at": "2099-01-01T13:00:00Z",
            "at": T0,
        },
    )
    assert r.status_code == 409

    # 原许可不受损：仍为草稿，历史只有创建事件
    a = client.get(f"/api/permits/{permit_a['id']}").json()
    assert a["status"] == "draft"
    history = client.get(f"/api/permits/{permit_a['id']}/history").json()
    assert [e["event_type"] for e in history["events"]] == ["created"]

    # 首尾相接的窗口不算重叠，可以创建
    permit_c = _create_permit(client, start=T_END, end="2099-01-01T14:00:00Z")
    assert permit_c["status"] == "draft"


def test_activate_after_lock_change_rejected_original_permit_intact(client: TestClient):
    _take_bypass_and_ring_out_of_service(client)
    permit_a = _create_permit(client)                                    # [10:00, 12:00]
    permit_c = _create_permit(client, start=T_END, end="2099-01-01T14:00:00Z")

    # A 在条件匹配时正常激活并计算
    r = client.post(f"/api/permits/{permit_a['id']}/activate", json={"at": T_IN})
    assert r.status_code == 200
    r = client.post("/api/isolation", json={"target_id": "T", "permit_id": permit_a["id"], "at": T_IN2})
    plan_id = r.json()["plan_id"]

    # 锁定阀条件改变（锁定入口阀），此时仍在 A 的窗口内
    client.post("/api/valves/V_TIN/lock", json={"locked": True})

    # 原许可 A 本身状态不受损（仍生效），但条件标记为不匹配、方案转待复核
    a = client.get(f"/api/permits/{permit_a['id']}").json()
    assert a["status"] == "active"
    assert a["conditions_match"] is False
    assert any("V_TIN" in m for m in a["condition_mismatches"])
    plan = client.get(f"/api/plans/{plan_id}").json()
    assert plan["status"] == "stale"
    assert plan["status_reason"] == "conditions_changed"

    # 条件不匹配后 A 也不得再用于计算
    r = client.post("/api/isolation", json={"target_id": "T", "permit_id": permit_a["id"]})
    assert r.status_code == 409

    # 在改变后的条件下激活 C -> 拒绝；C 保持草稿，拒绝被记录
    r = client.post(f"/api/permits/{permit_c['id']}/activate", json={"at": "2099-01-01T12:05:00Z"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "conditions_changed"
    c = client.get(f"/api/permits/{permit_c['id']}").json()
    assert c["status"] == "draft"
    c_hist = client.get(f"/api/permits/{permit_c['id']}/history").json()
    assert [e["event_type"] for e in c_hist["events"]] == ["created", "activate_rejected"]

    # C 的被拒不影响 A：A 的审计链只有自身生命周期事件（窗口结束自然过期）
    a_hist = client.get(f"/api/permits/{permit_a['id']}/history").json()
    assert [e["event_type"] for e in a_hist["events"]] == ["created", "activated", "expired"]
    assert a_hist["chain_valid"] is True


def test_activate_rejected_when_permit_valve_locked(client: TestClient):
    """锁阀限制：许可旁路阀被锁定（保持现状）时禁止临时投用。"""
    _take_bypass_and_ring_out_of_service(client)
    client.post("/api/valves/V_BP_IN/lock", json={"locked": True})
    permit = _create_permit(client)
    r = client.post(f"/api/permits/{permit['id']}/activate", json={"at": T_IN})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "locked_valves"
    assert client.get(f"/api/permits/{permit['id']}").json()["status"] == "draft"


def test_duplicate_activate_is_idempotent(client: TestClient):
    _take_bypass_and_ring_out_of_service(client)
    permit = _create_permit(client)
    client.post(f"/api/permits/{permit['id']}/activate", json={"at": T_IN})
    r = client.post(f"/api/permits/{permit['id']}/activate", json={"at": "2099-01-01T10:20:00Z"})
    assert r.status_code == 200
    permit = r.json()
    assert permit["status"] == "active"
    # 首次激活时刻不被覆盖
    assert permit["activated_at"].startswith("2099-01-01T10:05")
    history = client.get(f"/api/permits/{permit['id']}/history").json()
    assert [e["event_type"] for e in history["events"]] == ["created", "activated", "activate_duplicate"]


# ---------------- 验收 4：重复撤销 / 迟到激活 / 历史快照 / 审计顺序 ----------------

def test_duplicate_revoke_late_activate_and_history_snapshot(client: TestClient):
    permit, res = _active_permit_setup(client)
    plan_id = res["plan_id"]
    pid = permit["id"]

    # 撤销 + 重复撤销：幂等，首次撤销时刻不变
    r = client.post(f"/api/permits/{pid}/revoke", json={"at": "2099-01-01T10:30:00Z", "reason": "窗口提前结束"})
    assert r.status_code == 200
    assert r.json()["status"] == "revoked"
    r = client.post(f"/api/permits/{pid}/revoke", json={"at": "2099-01-01T10:31:00Z"})
    assert r.status_code == 200
    permit_now = r.json()
    assert permit_now["status"] == "revoked"
    assert permit_now["revoked_at"].startswith("2099-01-01T10:30")

    # 迟到的激活事件（业务时刻在窗口内，但到达时许可已撤销）：确定性拒绝
    r = client.post(f"/api/permits/{pid}/activate", json={"at": "2099-01-01T10:20:00Z"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "terminal_state"
    permit_now = client.get(f"/api/permits/{pid}").json()
    assert permit_now["status"] == "revoked"
    assert permit_now["revoked_at"].startswith("2099-01-01T10:30")

    # 关联方案转为待复核，但历史快照（旧路径）原样可查
    plan = client.get(f"/api/plans/{plan_id}").json()
    assert plan["status"] == "stale"
    assert plan["status_reason"] == "permit_revoked"
    assert plan["best_solution"] == ["V_TIN", "V_TOUT"]
    assert "BP" in plan["supply_paths"]["P2"]
    assert plan["result"]["solutions"][0]["permit_bypass_in_supply"] == BYPASS_VALVES

    # 审计顺序：created -> activated -> revoked -> revoke_duplicate -> activate_rejected
    history = client.get(f"/api/permits/{pid}/history").json()
    types = [e["event_type"] for e in history["events"]]
    assert types == ["created", "activated", "revoked", "revoke_duplicate", "activate_rejected"]
    assert [e["seq"] for e in history["events"]] == [1, 2, 3, 4, 5]
    assert history["chain_valid"] is True
    # 哈希链首尾衔接
    events = history["events"]
    assert events[0]["prev_hash"] == "0" * 64
    for prev, cur in zip(events, events[1:]):
        assert cur["prev_hash"] == prev["hash"]


def test_out_of_order_clock_events_deterministic(client: TestClient):
    _take_bypass_and_ring_out_of_service(client)
    permit = _create_permit(client)
    pid = permit["id"]

    # 窗口尚未开始的激活：拒绝且许可保持草稿
    r = client.post(f"/api/permits/{pid}/activate", json={"at": "2099-01-01T09:00:00Z"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "window_not_started"
    assert client.get(f"/api/permits/{pid}").json()["status"] == "draft"

    # 迟到的激活（参考时刻已越过窗口）：许可先被确定性过期，再拒绝激活
    r = client.post(f"/api/permits/{pid}/activate", json={"at": T_LATE})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "terminal_state"
    permit = client.get(f"/api/permits/{pid}").json()
    assert permit["status"] == "expired"
    assert permit["expired_at"].startswith("2099-01-01T12:00")

    # 过期后撤销同样确定性拒绝，历史顺序完整
    r = client.post(f"/api/permits/{pid}/revoke", json={"at": T_LATE})
    assert r.status_code == 409
    history = client.get(f"/api/permits/{pid}/history").json()
    types = [e["event_type"] for e in history["events"]]
    assert types == ["created", "activate_rejected", "expired", "activate_rejected", "revoke_rejected"]
    assert history["chain_valid"] is True


def test_solver_level_permit_bypass_isolation():
    """求解器层验证：许可旁路临时投用后，方案关闭集合在图上确实隔离目标且保供。"""
    import networkx as nx

    from app.isolation import _load, build_graph, compute_isolation
    from app.models import Valve

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        seed_database(db)
        for vid in ("V_BP_IN", "V_BP_OUT", "V_LK2"):
            db.get(Valve, vid).is_open = False
        db.commit()

        # 无许可：不可行
        assert compute_isolation(db, "T")["feasible"] is False

        # 有许可：可行且目标在图层面被隔离
        res = compute_isolation(db, "T", permit_valves={"V_BP_IN", "V_BP_OUT"})
        assert res["feasible"] is True
        assert res["best_solution"] == ["V_TIN", "V_TOUT"]
        _, edges = _load(db)
        for e in edges.values():
            if e["valve_id"] in ("V_BP_IN", "V_BP_OUT"):
                e["is_open"] = True  # 与求解视图一致的许可投用
        g = build_graph(edges, set(res["best_solution"]))
        assert not nx.has_path(g, "SRC", "T")
        assert nx.has_path(g, "SRC", "P1") and nx.has_path(g, "SRC", "P2")
        assert any("BP" in p for p in nx.all_simple_paths(g, "SRC", "P2"))

        # 许可阀门未被写入阀门表
        assert db.get(Valve, "V_BP_IN").is_open is False
    finally:
        db.close()


def test_audit_chain_detects_tampering(client: TestClient):
    permit, _ = _active_permit_setup(client)
    pid = permit["id"]
    assert client.get(f"/api/permits/{pid}/history").json()["chain_valid"] is True

    # 直接篡改数据库中的事件内容 -> 哈希链校验失败
    db = SessionLocal()
    try:
        ev = db.scalar(select(PermitEvent).where(PermitEvent.permit_id == pid).limit(1))
        ev.detail_json = json.dumps({"forged": True})
        db.commit()
    finally:
        db.close()
    assert client.get(f"/api/permits/{pid}/history").json()["chain_valid"] is False
