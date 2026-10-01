"""隔离算法的三个培训样例验证。

样例 1 —— 旁路绕回：
    无锁定。方案必须同时关闭目标设备的入口/出口阀（仅关单侧时，
    介质经旁路“绕回” N3 后仍能回到目标），而旁路保持打开继续供 P2。

样例 2 —— 锁定阀门：
    锁定入口阀 V_TIN（不可关闭）。最小方案变为 3 只阀：出口阀 +
    上游来源 V1 + 旁路一只阀（旁路此时必须被切断，否则介质绕回），
    N3/P2 改由环网联络管 N1-N5-N3 继续供料。

样例 3 —— 不应断供的支路 / 残余路径：
    锁定出口阀 V_TOUT。任何切得掉 T 的集合都会断供 P2，故无可行方案；
    接口必须返回一条当前仍连通的残余路径（且经过被锁定的出口阀）。
"""
from __future__ import annotations

import pytest

from app.database import Base, SessionLocal, engine
from app.isolation import build_graph, compute_isolation
from app.models import Valve
from app.seed import reset_database, seed_database


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()
    seed_database(db)
    yield db
    db.close()


def _set_locks(db, **locks: bool) -> None:
    for vid, locked in locks.items():
        valve = db.get(Valve, vid)
        assert valve is not None, vid
        valve.locked = locked
    db.commit()


def _graph(db, closed):
    from app.isolation import _load

    _, edges = _load(db)
    return build_graph(edges, set(closed)), edges


def cand_ids(db):
    from app.isolation import _load

    _, edges = _load(db)
    return [
        e["valve_id"]
        for e in edges.values()
        if e["valve_id"] and e["operable"] and e["is_open"] and not e["locked"]
    ]


# ---------------- 基础拓扑 ----------------

def test_topology_seed_has_direction_bypass_and_essentials(fresh_db):
    from app.isolation import topology_payload

    topo = topology_payload(fresh_db)
    by_id = {s["id"]: s for s in topo["segments"]}

    # 管段方向被显式保存
    assert by_id["E2"]["direction"] == "N2->T"
    assert by_id["E3"]["direction"] == "T->N3"

    # 旁路被显式标记，且与目标设备并联
    assert by_id["EB1"]["is_bypass"] is True
    assert by_id["EB2"]["is_bypass"] is True
    assert by_id["E2"]["is_bypass"] is False

    # 必要供给点
    assert {n["id"] for n in topo["nodes"] if n["essential"]} == {"P1", "P2"}
    assert {n["id"] for n in topo["nodes"] if n["kind"] == "source"} == {"SRC"}

    # 全部阀门初始打开、未锁定
    assert all(v["is_open"] and not v["locked"] for v in topo["valves"])


# ---------------- 样例 1：旁路绕回 ----------------

def test_sample_1_bypass_wrap_around(fresh_db):
    result = compute_isolation(fresh_db, "T")

    assert result["feasible"] is True
    # 最小方案恰好是设备入口阀 + 出口阀
    assert result["best_solution"] == ["V_TIN", "V_TOUT"]
    sol = result["solutions"][0]
    assert sol["size"] == 2

    # 方案不关闭任何旁路阀 —— 旁路绕回，P2 不能断供
    assert sol["closes_bypass_valves"] == []

    g, _ = _graph(fresh_db, set(result["best_solution"]))
    import networkx as nx

    assert not nx.has_path(g, "SRC", "T")                       # T 已隔离
    assert nx.has_path(g, "SRC", "P1")                          # P1 仍可达
    assert nx.has_path(g, "SRC", "P2")                          # P2 仍可达
    # P2 的来源路径确实使用了旁路（经过 BP 桥点）
    paths = list(nx.all_simple_paths(g, "SRC", "P2"))
    assert any("BP" in p for p in paths), paths
    # 环网旁的证明：旁路绕回路径存在
    assert nx.has_path(g, "N2", "N3") and nx.has_path(g, "BP", "N3")

    # 返回的展示用供给路径优先选择仍开启的旁路（直观体现旁路绕回）
    p2_supply = sol["supply_paths"]["P2"]
    assert "BP" in p2_supply, p2_supply
    assert p2_supply == ["SRC", "N1", "N2", "BP", "N3", "P2"]


def test_sample_1_single_side_valve_does_not_isolate(fresh_db):
    """仅关入口阀时，介质仍可经旁路绕回 N3、再经出口阀回到 T。"""
    import networkx as nx

    g, _ = _graph(fresh_db, {"V_TIN"})
    # 残余来源 -> 目标路径仍存在（环网绕回）
    assert nx.has_path(g, "SRC", "T")
    # 旁路也构成一条绕回路径：N2-BP-N3-T
    assert nx.has_path(g, "N2", "T")
    bypass_path = nx.shortest_path(g, "N2", "T")
    assert bypass_path == ["N2", "BP", "N3", "T"]
    # 补上出口阀后才真正隔离
    g2, _ = _graph(fresh_db, {"V_TIN", "V_TOUT"})
    assert not nx.has_path(g2, "SRC", "T")


# ---------------- 样例 2：锁定阀门 ----------------

def test_sample_2_locked_inlet_valve(fresh_db):
    _set_locks(fresh_db, V_TIN=True)
    result = compute_isolation(fresh_db, "T")

    assert result["feasible"] is True
    best = result["best_solution"]

    # 锁定阀不能被选入
    assert "V_TIN" not in best
    # 最小集合扩大到 3 只：出口阀 + 上游隔断 + 旁路一只
    assert len(best) == 3
    assert "V_TOUT" in best
    assert "V1" in best
    assert best.count("V_BP_IN") + best.count("V_BP_OUT") == 1

    g, _ = _graph(fresh_db, set(best))
    import networkx as nx

    assert not nx.has_path(g, "SRC", "T")
    assert nx.has_path(g, "SRC", "P1")
    assert nx.has_path(g, "SRC", "P2")
    # 旁路此时被封，P2 靠环网联络管 N1-N5-N3 绕回供料
    p2_paths = list(nx.all_simple_paths(g, "SRC", "P2"))
    assert all("BP" not in p for p in p2_paths)
    assert any("N5" in p for p in p2_paths), p2_paths

    # 等价最小方案至少两只（封旁路入口或出口互为等价）
    assert len(result["solutions"]) >= 2


def test_sample_2_locked_valve_excluded_but_two_valves_not_enough(fresh_db):
    """锁定入口阀后，任意 2 只阀的组合都无法既隔离 T 又保住 P2。"""
    from itertools import combinations

    _set_locks(fresh_db, V_TIN=True)
    from app.isolation import _load

    _, edges = _load(fresh_db)
    candidates = [
        e["valve_id"]
        for e in edges.values()
        if e["valve_id"] and e["operable"] and e["is_open"] and not e["locked"]
    ]
    import networkx as nx

    for combo in combinations(candidates, 2):
        g = build_graph(edges, set(combo))
        isolated = not nx.has_path(g, "SRC", "T")
        supplied = nx.has_path(g, "SRC", "P1") and nx.has_path(g, "SRC", "P2")
        assert not (isolated and supplied), combo


# ---------------- 样例 3：不应断供的支路 / 残余路径 ----------------

def test_sample_3_locked_outlet_is_infeasible_with_residual_path(fresh_db):
    _set_locks(fresh_db, V_TOUT=True)
    result = compute_isolation(fresh_db, "T")

    assert result["feasible"] is False
    assert result["best_solution"] == []
    assert result["residual_path"] is not None

    residual = result["residual_path"]
    # 残余路径从来源走到目标设备
    assert residual["nodes"][0] == "SRC"
    assert residual["nodes"][-1] == "T"

    # 最短残余路径可能走入口侧；另给出经过锁定出口阀的见证路径
    witness = result["locked_witness_path"]
    assert witness is not None
    assert witness["nodes"][0] == "SRC"
    assert witness["nodes"][-1] == "T"
    assert "V_TOUT" in witness["valves"]
    assert "V_TOUT" in witness["locked_valves_on_path"]
    # 两条残余路径之一必须显式经过被锁定的出口阀
    assert (
        "V_TOUT" in residual["valves"]
        or "V_TOUT" in witness["valves"]
    )

    # 诊断：切得掉 T 的切法不可避免地断供 P2（但可保住 P1）
    ub = result["unconstrained_best"]
    assert ub is not None
    assert ub["unavoidable_essentials"] == ["P2"]
    assert "P2" in ub["disconnects_essentials"]
    assert "P1" not in ub["disconnects_essentials"]
    # 见证切法 2 只阀即可（封旁路+环网）：出口锁死下只牺牲 P2、保住 P1
    assert set(ub["close_valves"]) <= set(cand_ids(fresh_db))
    assert ub["size"] == 2
    assert len(ub["close_valves"]) == 2
    assert result["infeasible_reason"] and "P2" in result["infeasible_reason"]


def test_sample_3_residual_path_is_actually_walkable(fresh_db):
    """返回的残余路径必须是当前锁定状态下图里真实存在的路径。"""
    import networkx as nx

    _set_locks(fresh_db, V_TOUT=True)
    result = compute_isolation(fresh_db, "T")
    g, edges = _graph(fresh_db, set())

    for path_key in ("residual_path", "locked_witness_path"):
        path_obj = result[path_key]
        if path_obj is None:
            continue
        nodes = path_obj["nodes"]
        for a, b in zip(nodes, nodes[1:]):
            assert g.has_edge(a, b), (path_key, a, b)
        assert nx.is_simple_path(g, nodes)
    assert result["locked_witness_path"] is not None
    # 同一锁定下，算法对所有候选组合的结论一致：找不到可行方案
    assert result["examined_combinations"] > 0
