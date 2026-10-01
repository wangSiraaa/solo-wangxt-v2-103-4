"""FastAPI 端到端测试：锁定 -> 重算 -> 残余路径 -> 重置。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.database import Base, engine
from app.main import app
from app.seed import seed_database
from app.database import SessionLocal


def _reset_seed():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()
    seed_database(db)
    db.close()


def test_health_topology_and_isolation_flow():
    _reset_seed()
    with TestClient(app) as client:
        assert client.get("/api/health").json() == {"status": "ok"}

        topo = client.get("/api/topology").json()
        assert len(topo["nodes"]) == 9
        assert len(topo["valves"]) == 10

        # 样例 1：无锁定
        r1 = client.post("/api/isolation", json={"target_id": "T"}).json()
        assert r1["feasible"] is True
        assert r1["best_solution"] == ["V_TIN", "V_TOUT"]

        # 样例 2：锁定入口阀后重算
        r2 = client.post(
            "/api/isolation",
            json={"target_id": "T", "locks": {"V_TIN": True}},
        ).json()
        assert r2["feasible"] is True
        assert "V_TIN" not in r2["best_solution"]
        assert len(r2["best_solution"]) == 3

        # 锁定状态已持久化
        locked = {v["id"]: v["locked"] for v in client.get("/api/topology").json()["valves"]}
        assert locked["V_TIN"] is True

        # 单独的锁定接口 + 样例 3：仅锁定出口阀（先清除样例 2 的锁定）
        client.post("/api/reset")
        client.post("/api/valves/V_TOUT/lock", json={"locked": True})
        r3 = client.post("/api/isolation", json={"target_id": "T"}).json()
        assert r3["feasible"] is False
        assert r3["residual_path"]["nodes"][0] == "SRC"
        assert r3["residual_path"]["nodes"][-1] == "T"
        witness_valves = r3["locked_witness_path"]["valves"]
        assert "V_TOUT" in witness_valves

        # 重置恢复
        client.post("/api/reset")
        r4 = client.post("/api/isolation", json={"target_id": "T"}).json()
        assert r4["best_solution"] == ["V_TIN", "V_TOUT"]


def test_unknown_valve_rejected():
    _reset_seed()
    with TestClient(app) as client:
        r = client.post("/api/isolation", json={"target_id": "T", "locks": {"NOPE": True}})
        assert r.status_code == 400
