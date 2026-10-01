"""隔离候选集合计算（NetworkX）。

模型约定（仅适用于本演示的固定拓扑与阀门模型）：

1. 图为无向“物理连通图”——隔离边界阀门无论装在设备哪一侧，关闭后该物理
   连通即被切断。管段上保存的 up/down 方向用于前端箭头与来源/下游解释。
2. 阀门与其所在管段 1:1。阀门打开 => 该边连通；关闭 => 该边移除；
   锁定阀门不可被本算法选入关闭集合（保持其当前状态）。
3. 有效隔离集合必须同时满足：
   a. 目标设备与所有介质来源之间不存在连通路径；
   b. 每个必要供给点（essential 节点）仍与至少一个介质来源连通。
4. 在所有有效集合中取阀门数最少者；并列时按阀门 id 排序取字典序最小，
   并可返回若干等价方案。
5. 不存在有效集合时，返回当前仍可从来源到达目标设备的一条残余路径
   以及断供/无法隔离的诊断信息。
"""
from __future__ import annotations

from itertools import combinations
from typing import Any

import networkx as nx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Node, Segment, Valve

MAX_ENUMERATE = 200_000
MAX_ALTERNATIVES = 5


class TopologyError(RuntimeError):
    pass


def _load(db: Session) -> tuple[list[Node], dict[str, dict[str, Any]]]:
    nodes = list(db.scalars(select(Node)).all())
    valves = {v.segment_id: v for v in db.scalars(select(Valve)).all()}
    segments = list(db.scalars(select(Segment)).all())

    edges: dict[str, dict[str, Any]] = {}
    for seg in segments:
        v = valves.get(seg.id)
        edges[seg.id] = {
            "id": seg.id,
            "u": seg.upstream_id,
            "v": seg.downstream_id,
            "direction": f"{seg.upstream_id}->{seg.downstream_id}",
            "kind": seg.kind,
            "is_bypass": seg.is_bypass,
            "valve_id": v.id if v else None,
            "valve_name": v.name if v else None,
            "is_open": v.is_open if v else True,
            "locked": v.locked if v else False,
            "operable": v.operable if v else True,
        }
    return nodes, edges


def build_graph(edges: dict[str, dict[str, Any]], closed: set[str]) -> nx.Graph:
    """构建在给定关闭集合下的物理连通无向图。

    初始即关闭、或被方案关闭的阀门对应边都不放入图；
    锁定阀门保持其当前状态、不可被选入 closed。
    """
    g = nx.Graph()
    for e in edges.values():
        g.add_node(e["u"])
        g.add_node(e["v"])
        if not e["is_open"]:
            continue  # 初始已关闭
        if e["valve_id"] in closed:
            continue  # 本方案关闭
        g.add_edge(e["u"], e["v"], edge_id=e["id"], valve_id=e["valve_id"])
    return g


def _reachable_sources(g: nx.Graph, node: str, sources: list[str]) -> list[str]:
    return [s for s in sources if s in g and node in g and nx.has_path(g, s, node)]


def _shortest_source_path(g: nx.Graph, target: str, sources: list[str]) -> list[str] | None:
    best: list[str] | None = None
    for s in sources:
        if s in g and target in g and nx.has_path(g, s, target):
            p = nx.shortest_path(g, s, target)
            if best is None or len(p) < len(best):
                best = p
    return best


def _preferred_source_path(
    g: nx.Graph, target: str, sources: list[str], prefer_edges: set[tuple[str, str]]
) -> list[str] | None:
    """返回来源到目标的供给路径；在最短长度 +2 跳内优先经过 prefer_edges（如开启的旁路）。

    两条供给路径都真实存在时，优先展示经旁路的那一条，便于培训页直观呈现
    “旁路绕回”。代价是允许比最短路多至多两跳；无旁路可用时退回最短路。
    """
    best: list[str] | None = None
    for s in sources:
        if s not in g or target not in g or not nx.has_path(g, s, target):
            continue
        min_len = nx.shortest_path_length(g, s, target)
        chosen: list[str] | None = None
        checked = 0
        for p in nx.shortest_simple_paths(g, s, target):
            if len(p) - 1 > min_len + 2 or checked > 200:
                break
            checked += 1
            if any((a, b) in prefer_edges or (b, a) in prefer_edges for a, b in zip(p, p[1:])):
                chosen = p
                break
        if chosen is None:
            chosen = nx.shortest_path(g, s, target)
        if best is None or len(chosen) < len(best):
            best = chosen
    return best


def _path_valves(g_or_edges: nx.Graph | dict, path: list[str]) -> list[str]:
    """把节点路径翻译成经过的阀门 id 列表。"""
    if isinstance(g_or_edges, nx.Graph):
        g = g_or_edges
        out: list[str] = []
        for a, b in zip(path, path[1:]):
            data = g.get_edge_data(a, b)
            if data and data.get("valve_id"):
                out.append(data["valve_id"])
        return out
    # dict 模式：用于解析经过已移除边的残余路径
    edges = g_or_edges
    pair_to_valve = {(e["u"], e["v"]): e["valve_id"] for e in edges.values()}
    pair_to_valve.update({(e["v"], e["u"]): e["valve_id"] for e in edges.values()})
    return [pair_to_valve[(a, b)] for a, b in zip(path, path[1:]) if (a, b) in pair_to_valve]


def _valve_view(edges: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for e in edges.values():
        vid = e["valve_id"]
        if not vid or vid in seen:
            continue
        seen.add(vid)
        out.append(
            {
                "id": vid,
                "name": e["valve_name"],
                "segment_id": e["id"],
                "endpoints": [e["u"], e["v"]],
                "is_open": e["is_open"],
                "locked": e["locked"],
                "operable": e["operable"],
                "is_bypass": e["is_bypass"],
            }
        )
    return sorted(out, key=lambda x: x["id"])


def compute_isolation(db: Session, target_id: str) -> dict[str, Any]:
    nodes, edges = _load(db)
    node_ids = {n.id for n in nodes}
    if target_id not in node_ids:
        raise TopologyError(f"目标节点不存在: {target_id}")

    sources = sorted(n.id for n in nodes if n.kind == "source")
    essentials = sorted(n.id for n in nodes if n.essential)
    if not sources:
        raise TopologyError("拓扑中没有介质来源节点")

    full_open = nx.Graph()
    full_open.add_nodes_from(node_ids)
    for e in edges.values():
        if e["is_open"]:
            full_open.add_edge(e["u"], e["v"], edge_id=e["id"], valve_id=e["valve_id"])

    # 可被本算法关闭的阀门 = 工艺可操作 + 当前打开 + 未锁定
    candidates = sorted(
        e["valve_id"]
        for e in edges.values()
        if e["valve_id"] and e["operable"] and e["is_open"] and not e["locked"]
    )

    base_graph = build_graph(edges, set())

    # 初始状态下目标已无来源路径：无需操作
    if not _reachable_sources(base_graph, target_id, sources):
        return {
            "feasible": True,
            "target_id": target_id,
            "sources": sources,
            "essentials": essentials,
            "candidate_valves": _valve_view(edges),
            "solutions": [{"close_valves": [], "size": 0, "alternative_rank": 0}],
            "best_solution": [],
            "residual_path": None,
            "locked_witness_path": None,
            "infeasible_reason": None,
            "unconstrained_best": None,
        }

    def valid(closed: set[str]) -> bool:
        g = build_graph(edges, closed)
        if _reachable_sources(g, target_id, sources):
            return False
        for p in essentials:
            if not _reachable_sources(g, p, sources):
                return False
        return True

    solutions: list[set[str]] = []
    found_size: int | None = None
    examined = 0
    for size in range(0, len(candidates) + 1):
        for combo in combinations(candidates, size):
            examined += 1
            if examined > MAX_ENUMERATE:
                break
            closed = set(combo)
            if valid(closed):
                solutions.append(closed)
        if examined > MAX_ENUMERATE:
            break
        if solutions:
            found_size = size
            break

    def enrich(closed: set[str], rank: int) -> dict[str, Any]:
        g = build_graph(edges, closed)
        open_bypass_edges: set[tuple[str, str]] = {
            (e["u"], e["v"])
            for e in edges.values()
            if e["is_bypass"] and e["valve_id"] not in closed and e["is_open"]
        }
        supply_paths = {
            p: _preferred_source_path(g, p, sources, open_bypass_edges)
            for p in essentials
        }
        return {
            "close_valves": sorted(closed),
            "size": len(closed),
            "alternative_rank": rank,
            "closes_bypass_valves": sorted(v for v in closed if any(e["valve_id"] == v and e["is_bypass"] for e in edges.values())),
            "supply_paths": supply_paths,
        }

    result: dict[str, Any] = {
        "feasible": bool(solutions),
        "target_id": target_id,
        "sources": sources,
        "essentials": essentials,
        "candidate_valves": _valve_view(edges),
        "examined_combinations": examined,
        "solutions": [],
        "best_solution": [],
        "residual_path": None,
        "infeasible_reason": None,
        "unconstrained_best": None,
    }

    if solutions:
        solutions.sort(key=lambda s: sorted(s))
        alts = [enrich(s, i) for i, s in enumerate(solutions[:MAX_ALTERNATIVES])]
        result["solutions"] = alts
        result["best_solution"] = alts[0]["close_valves"]
        return result

    # ---------- 不可行：诊断 ----------
    # 1) 枚举所有能切断目标来源的切集（候选数受 MAX_ENUMERATE 约束），
    #    判断哪些必要供给点“在任何切法下都无法保住”，并给出一只断供
    #    供给点最少的见证切集。
    all_cuts: list[set[str]] = []
    examined2 = 0
    for size in range(0, len(candidates) + 1):
        stop = False
        for combo in combinations(candidates, size):
            examined2 += 1
            if examined2 > MAX_ENUMERATE:
                stop = True
                break
            g = build_graph(edges, set(combo))
            if not _reachable_sources(g, target_id, sources):
                all_cuts.append(set(combo))
        if stop:
            break

    unconstrained = None
    if all_cuts:
        # 对每个必要供给点：存在某个切集仍保住它 => 并非不可避免
        ever_supplied: set[str] = set()
        essential_set = set(essentials)
        cuts_with_lost: list[tuple[set[str], set[str]]] = []
        for cut in all_cuts:
            g = build_graph(edges, cut)
            cut_p = {p for p in essentials if not _reachable_sources(g, p, sources)}
            for p in essential_set - cut_p:
                ever_supplied.add(p)
            cuts_with_lost.append((cut, cut_p))
        unavoidable = sorted(essential_set - ever_supplied)
        unavoidable_set = set(unavoidable)

        # 见证切集：在“只断供不可避免点”的切集中选阀门最少、字典序最小者
        clean_witness = [
            (cut, lost)
            for cut, lost in cuts_with_lost
            if lost == unavoidable_set
        ]
        if clean_witness:
            clean_witness.sort(key=lambda t: (len(t[0]), sorted(t[0])))
            witness_cut, witness_lost = clean_witness[0]
        else:
            cuts_with_lost.sort(key=lambda t: (len(t[1]), len(t[0]), sorted(t[0])))
            witness_cut, witness_lost = cuts_with_lost[0]

        unconstrained = {
            "close_valves": sorted(witness_cut),
            "size": len(witness_cut),
            "disconnects_essentials": sorted(witness_lost),
            "unavoidable_essentials": unavoidable,
        }

    # 2) 残余路径：当前状态（锁定下）仍连通来源与目标的路径
    def _describe_path(node_path: list[str]) -> dict[str, Any]:
        # 跨边拼接时 h1 的邻点 h2 会在两段中重复出现，压缩相邻重复
        cleaned: list[str] = []
        for n in node_path:
            if not cleaned or cleaned[-1] != n:
                cleaned.append(n)
        node_path = cleaned
        valve_seq: list[str | None] = []
        edge_seq: list[str] = []
        for a, b in zip(node_path, node_path[1:]):
            data = full_open.get_edge_data(a, b)
            edge_seq.append(data["edge_id"] if data else "")
            valve_seq.append(data["valve_id"] if data else None)
        return {
            "nodes": node_path,
            "valves": valve_seq,
            "locked_valves_on_path": sorted(
                edges[eid]["valve_id"] for eid in edge_seq if eid and edges[eid]["locked"]
            ),
            "uses_bypass": any(eid and edges[eid]["is_bypass"] for eid in edge_seq),
        }

    residual_nodes = _shortest_source_path(base_graph, target_id, sources)
    locked_witness: dict[str, Any] | None = None
    if residual_nodes:
        residual = _describe_path(residual_nodes)
        # 若最短残余路径没有经过锁定阀，再找一条经过锁定阀的见证路径，
        # 以直观展示“是哪只锁死的阀门让来源仍能回到目标”。
        if not residual["locked_valves_on_path"]:
            locked_edge_ids = {
                e["id"] for e in edges.values() if e["locked"] and e["is_open"]
            }
            witness_nodes: list[str] | None = None
            for src in sources:
                for a, b, data in base_graph.edges(data=True):
                    if data.get("edge_id") in locked_edge_ids:
                        for h1, h2 in ((a, b), (b, a)):
                            if nx.has_path(base_graph, src, h1) and nx.has_path(
                                base_graph, h2, target_id
                            ):
                                # 跨过锁定边 h1-h2：到 h1 的路径 + 从 h2 到目标的路径
                                cand = (
                                    nx.shortest_path(base_graph, src, h1)
                                    + nx.shortest_path(base_graph, h2, target_id)
                                )
                                if witness_nodes is None or len(cand) < len(witness_nodes):
                                    witness_nodes = cand
            if witness_nodes is not None:
                locked_witness = _describe_path(witness_nodes)
        result["residual_path"] = residual
        result["locked_witness_path"] = locked_witness
    else:
        result["locked_witness_path"] = None

    unavoidable = unconstrained["unavoidable_essentials"] if unconstrained else []
    if unavoidable:
        reason = (
            "在当前阀门模型下，任何能切断目标设备来源的切法都不可避免地断供必要供给点 "
            f"{', '.join(unavoidable)}（其余必要供给点可同时保住）；"
            "目标仍与来源连通的残余路径见上。"
        )
    elif locked_witness is not None or (
        residual_nodes
        and any(
            edges[full_open.get_edge_data(a, b)["edge_id"]]["locked"]
            for a, b in zip(residual_nodes, residual_nodes[1:])
        )
    ):
        reason = "目标设备仍经锁定阀门与来源连通，该阀门不可关闭，无法形成隔离边界。"
    else:
        reason = "在当前锁定约束下找不到满足条件的隔离集合。"

    result["unconstrained_best"] = unconstrained
    result["infeasible_reason"] = reason
    return result


def topology_payload(db: Session) -> dict[str, Any]:
    nodes, edges = _load(db)
    return {
        "nodes": [
            {"id": n.id, "name": n.name, "kind": n.kind, "x": n.x, "y": n.y, "essential": n.essential}
            for n in nodes
        ],
        "segments": [
            {
                "id": e["id"],
                "source": e["u"],
                "target": e["v"],
                "direction": e["direction"],
                "kind": e["kind"],
                "is_bypass": e["is_bypass"],
                "valve_id": e["valve_id"],
            }
            for e in edges.values()
        ],
        "valves": _valve_view(edges),
    }
