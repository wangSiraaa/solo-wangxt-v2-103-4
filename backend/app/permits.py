"""临时旁路许可服务：生命周期、条件校验、审计链与关联方案状态迁移。

设计约定（全部为培训模拟，不输出真实控制指令）：

1. 许可状态机：draft -> active -> expired/revoked；draft 超过窗口同样 expired。
   终态（expired/revoked）不可再迁移；重复激活/撤销是确定性的幂等或拒绝，
   且每次尝试都会追加审计事件，原许可状态不受损。
2. 事件顺序的唯一权威是服务器追加的 seq（许可内严格递增）；请求自带的业务
   时刻 at（演练模拟时钟）只参与窗口校验，迟到的/乱序的事件不会重排历史。
3. 每条事件带哈希链（prev_hash -> hash），任何事后篡改都会使校验失败。
4. 许可到期时间恒等于窗口结束时刻（与何时被发现无关），保证确定结果。
5. 许可只影响求解器视图（把许可旁路阀视为临时投用且不可切断），绝不改动
   阀门表中的真实状态。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import isolation
from .models import (
    PERMIT_TERMINAL,
    BypassPermit,
    IsolationPlan,
    PermitEvent,
    Valve,
)

DISCLAIMER = (
    "临时旁路许可与关联方案仅为培训模拟：不连接真实控制系统、不输出任何真实控制指令；"
    "许可到期、撤销或拓扑/锁阀条件变化后，旧方案不得作为真实检修隔离依据。"
)

GENESIS_HASH = "0" * 64


class PermitError(RuntimeError):
    """许可操作被拒绝（确定性结果，HTTP 409）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------- 时间与序列化工具 ----------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_dt(dt: datetime) -> datetime:
    """统一为 UTC naive 存储（SQLite 不保留时区）。"""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(microsecond=0)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ---------------- 拓扑快照与条件匹配 ----------------

def topology_snapshot(db: Session) -> dict[str, Any]:
    """当前拓扑 + 阀门状态快照：结构或任一阀门状态变化都会改变哈希。"""
    payload = isolation.topology_payload(db)
    return {
        "nodes": sorted(
            ({"id": n["id"], "kind": n["kind"], "essential": n["essential"]} for n in payload["nodes"]),
            key=lambda n: n["id"],
        ),
        "segments": sorted(
            (
                {
                    "id": s["id"],
                    "source": s["source"],
                    "target": s["target"],
                    "kind": s["kind"],
                    "is_bypass": s["is_bypass"],
                    "valve_id": s["valve_id"],
                }
                for s in payload["segments"]
            ),
            key=lambda s: s["id"],
        ),
        "valves": sorted(
            (
                {
                    "id": v["id"],
                    "segment_id": v["segment_id"],
                    "is_open": v["is_open"],
                    "locked": v["locked"],
                    "operable": v["operable"],
                }
                for v in payload["valves"]
            ),
            key=lambda v: v["id"],
        ),
    }


def snapshot_hash(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(_canon(snapshot).encode("utf-8")).hexdigest()


def current_locks(db: Session) -> dict[str, bool]:
    return {v.id: bool(v.locked) for v in db.scalars(select(Valve)).all()}


def permit_valves(permit: BypassPermit) -> list[str]:
    return list(json.loads(permit.valve_ids_json))


def conditions_match(permit: BypassPermit, db: Session) -> tuple[bool, list[str]]:
    """许可条件是否仍与当前拓扑/锁阀一致；返回 (是否匹配, 失配原因列表)。"""
    reasons: list[str] = []
    snap = topology_snapshot(db)
    if snapshot_hash(snap) != permit.topo_hash:
        cur_valves = {v["id"]: v for v in snap["valves"]}
        old_valves = {v["id"]: v for v in json.loads(permit.topo_snapshot_json)["valves"]}
        for vid in sorted(set(cur_valves) | set(old_valves)):
            cur, old = cur_valves.get(vid), old_valves.get(vid)
            if cur is None or old is None:
                reasons.append(f"阀门集合变化: {vid}")
            elif cur["locked"] != old["locked"]:
                reasons.append(f"锁定状态变化: {vid}")
            elif cur["is_open"] != old["is_open"]:
                reasons.append(f"开闭状态变化: {vid}")
            elif cur != old:
                reasons.append(f"阀门属性变化: {vid}")
        if {n["id"] for n in snap["nodes"]} != {
            n["id"] for n in json.loads(permit.topo_snapshot_json)["nodes"]
        }:
            reasons.append("节点集合变化")
        if not reasons:
            reasons.append("拓扑快照哈希不匹配")
    pre = json.loads(permit.precondition_locks_json)
    now_locks = current_locks(db)
    for vid in sorted(pre):
        if vid in now_locks and now_locks[vid] != pre[vid]:
            if not any(vid in r for r in reasons):
                reasons.append(f"前置锁定状态变化: {vid}")
    return (not reasons, reasons)


# ---------------- 审计事件（哈希链） ----------------

def _event_hash(permit_id: int, seq: int, event_type: str, at: datetime, detail: dict, prev: str) -> str:
    body = _canon(
        {
            "permit_id": permit_id,
            "seq": seq,
            "event_type": event_type,
            "at": at.isoformat(),
            "detail": detail,
            "prev_hash": prev,
        }
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def append_event(
    db: Session,
    permit: BypassPermit,
    event_type: str,
    at: datetime,
    detail: dict[str, Any] | None = None,
) -> PermitEvent:
    last = db.scalar(
        select(PermitEvent)
        .where(PermitEvent.permit_id == permit.id)
        .order_by(PermitEvent.seq.desc())
        .limit(1)
    )
    seq = (last.seq + 1) if last else 1
    prev = last.hash if last else GENESIS_HASH
    detail = detail or {}
    ev = PermitEvent(
        permit_id=permit.id,
        seq=seq,
        event_type=event_type,
        at=normalize_dt(at),
        recorded_at=utcnow(),
        detail_json=_canon(detail),
        prev_hash=prev,
        hash=_event_hash(permit.id, seq, event_type, normalize_dt(at), detail, prev),
    )
    db.add(ev)
    return ev


def verify_chain(events: list[PermitEvent]) -> bool:
    prev = GENESIS_HASH
    for ev in events:
        if ev.prev_hash != prev:
            return False
        expect = _event_hash(
            ev.permit_id, ev.seq, ev.event_type, ev.at, json.loads(ev.detail_json), ev.prev_hash
        )
        if ev.hash != expect:
            return False
        prev = ev.hash
    return True


# ---------------- 生命周期 ----------------

def sweep_expired(db: Session, now: datetime) -> list[BypassPermit]:
    """惰性到期扫描：窗口已过的 draft/active 许可转为 expired。

    expired_at 恒等于窗口结束时刻，与扫描发生时间无关，保证确定结果。
    关联的 valid 方案同步转为 expired。
    """
    now = normalize_dt(now)
    changed: list[BypassPermit] = []
    permits = db.scalars(select(BypassPermit).where(BypassPermit.status.in_(("draft", "active")))).all()
    for p in permits:
        if p.end_at < now:
            was = p.status
            p.status = "expired"
            p.expired_at = p.end_at
            append_event(db, p, "expired", p.end_at, {"from": was, "reason": "window_elapsed"})
            for plan in db.scalars(
                select(IsolationPlan).where(
                    IsolationPlan.permit_id == p.id, IsolationPlan.status == "valid"
                )
            ).all():
                plan.status = "expired"
                plan.status_reason = "permit_expired"
                plan.status_at = now
            changed.append(p)
    if changed:
        db.flush()
    return changed


def _window_overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    # 半开区间 [start, end)：首尾相接不算重叠
    return a_start < b_end and b_start < a_end


def _assert_no_overlap(db: Session, permit: BypassPermit, exclude_id: int | None = None) -> None:
    others = db.scalars(
        select(BypassPermit).where(BypassPermit.status.in_(("draft", "active")))
    ).all()
    mine = set(permit_valves(permit))
    for other in others:
        if exclude_id is not None and other.id == exclude_id:
            continue
        if other.id == permit.id:
            continue
        if not mine & set(permit_valves(other)):
            continue
        if _window_overlaps(permit.start_at, permit.end_at, other.start_at, other.end_at):
            raise PermitError(
                "overlap",
                f"与许可 #{other.id} 的旁路/窗口重叠（{other.start_at.isoformat()}~{other.end_at.isoformat()}），"
                "同一旁路同一时刻只允许一张有效许可",
            )


def create_permit(
    db: Session,
    *,
    target_id: str,
    valve_ids: list[str],
    purpose: str,
    start_at: datetime,
    end_at: datetime,
    now: datetime,
) -> BypassPermit:
    now = normalize_dt(now)
    start_at = normalize_dt(start_at)
    end_at = normalize_dt(end_at)
    if end_at <= start_at:
        raise PermitError("bad_window", "结束时刻必须晚于开始时刻")

    nodes, edges = isolation._load(db)
    node_ids = {n.id for n in nodes}
    if target_id not in node_ids:
        raise PermitError("bad_target", f"目标节点不存在: {target_id}")

    by_valve = {e["valve_id"]: e for e in edges.values() if e["valve_id"]}
    unknown = [v for v in valve_ids if v not in by_valve]
    if unknown:
        raise PermitError("bad_valves", f"未知阀门: {', '.join(sorted(unknown))}")
    not_bypass = [v for v in valve_ids if not by_valve[v]["is_bypass"]]
    if not_bypass:
        raise PermitError("bad_valves", f"许可只能覆盖旁路阀门: {', '.join(sorted(not_bypass))}")
    if not valve_ids:
        raise PermitError("bad_valves", "许可必须至少覆盖一只旁路阀门")

    valve_ids = sorted(set(valve_ids))
    segment_ids = sorted(by_valve[v]["id"] for v in valve_ids)
    snapshot = topology_snapshot(db)

    permit = BypassPermit(
        target_id=target_id,
        bypass_key=",".join(valve_ids),
        valve_ids_json=_canon(valve_ids),
        segment_ids_json=_canon(segment_ids),
        purpose=purpose,
        start_at=start_at,
        end_at=end_at,
        status="draft",
        topo_hash=snapshot_hash(snapshot),
        topo_snapshot_json=_canon(snapshot),
        precondition_locks_json=_canon(current_locks(db)),
        created_at=now,
    )
    db.add(permit)
    db.flush()  # 分配 id 供事件与冲突检测使用
    _assert_no_overlap(db, permit)
    append_event(db, permit, "created", now, {"purpose": purpose, "valves": valve_ids})
    db.commit()
    return permit


def _activation_checks(db: Session, permit: BypassPermit, at: datetime) -> None:
    """激活前重新校验：窗口、重叠、拓扑/锁阀条件、锁阀限制与可行隔离方案。"""
    if at < permit.start_at:
        raise PermitError("window_not_started", f"窗口尚未开始（{permit.start_at.isoformat()} 起）")
    if at > permit.end_at:
        raise PermitError("window_elapsed", "窗口已结束，许可应已过期")
    _assert_no_overlap(db, permit, exclude_id=permit.id)

    match, reasons = conditions_match(permit, db)
    if not match:
        raise PermitError("conditions_changed", "拓扑/锁阀条件已变化: " + "; ".join(reasons))

    # 锁阀限制：许可旁路阀当前不得被锁定（锁定=保持现状，与临时投用冲突）
    _, edges = isolation._load(db)
    locked_permit_valves = sorted(
        v for v in permit_valves(permit) if any(e["valve_id"] == v and e["locked"] for e in edges.values())
    )
    if locked_permit_valves:
        raise PermitError(
            "locked_valves", f"许可旁路阀被锁定，禁止临时投用: {', '.join(locked_permit_valves)}"
        )

    # 可行性预演：应用许可后目标必须确实可隔离且必要供给点仍可达
    result = isolation.compute_isolation(db, permit.target_id, permit_valves=set(permit_valves(permit)))
    if not result["feasible"]:
        raise PermitError(
            "infeasible",
            "应用许可后仍无可行隔离方案（目标无法隔离或必要供给点断供），拒绝激活",
        )


def activate_permit(db: Session, permit: BypassPermit, at: datetime) -> BypassPermit:
    at = normalize_dt(at)
    sweep_expired(db, at)
    db.refresh(permit)
    if permit.status == "active":
        # 重复激活：幂等，状态不变，仅记录审计事件
        append_event(db, permit, "activate_duplicate", at, {"note": "许可已处于已生效状态"})
        db.commit()
        return permit
    if permit.status in PERMIT_TERMINAL:
        append_event(db, permit, "activate_rejected", at, {"reason": f"permit_{permit.status}"})
        db.commit()
        raise PermitError("terminal_state", f"许可已{ '过期' if permit.status=='expired' else '撤销' }，不可激活")
    try:
        _activation_checks(db, permit, at)
    except PermitError as exc:
        append_event(db, permit, "activate_rejected", at, {"reason": exc.code, "message": exc.message})
        db.commit()
        raise
    permit.status = "active"
    permit.activated_at = at
    append_event(db, permit, "activated", at, {"valves": permit_valves(permit)})
    db.commit()
    return permit


def revoke_permit(db: Session, permit: BypassPermit, at: datetime, reason: str = "") -> BypassPermit:
    at = normalize_dt(at)
    sweep_expired(db, at)
    db.refresh(permit)
    if permit.status == "revoked":
        # 重复撤销：幂等，保留首次撤销时刻，仅记录审计事件
        append_event(db, permit, "revoke_duplicate", at, {"note": "许可已撤销，本次为重复操作"})
        db.commit()
        return permit
    if permit.status == "expired":
        append_event(db, permit, "revoke_rejected", at, {"reason": "permit_expired"})
        db.commit()
        raise PermitError("terminal_state", "许可已过期，不可撤销")
    permit.status = "revoked"
    permit.revoked_at = at
    permit.revoke_reason = reason or None
    append_event(db, permit, "revoked", at, {"reason": reason})
    # 撤销后关联方案立即转为待复核，不得静默继续
    for plan in db.scalars(
        select(IsolationPlan).where(IsolationPlan.permit_id == permit.id, IsolationPlan.status == "valid")
    ).all():
        plan.status = "stale"
        plan.status_reason = "permit_revoked"
        plan.status_at = at
    db.commit()
    return permit


# ---------------- 方案（关联计算结果） ----------------

def record_plan(
    db: Session,
    *,
    target_id: str,
    permit: BypassPermit | None,
    result: dict[str, Any],
    now: datetime,
) -> IsolationPlan:
    now = normalize_dt(now)
    plan = IsolationPlan(
        target_id=target_id,
        permit_id=permit.id if permit else None,
        status="valid",
        status_reason=None,
        feasible=bool(result["feasible"]),
        result_json=_canon(result),
        topo_hash=snapshot_hash(topology_snapshot(db)),
        locks_json=_canon(current_locks(db)),
        created_at=now,
        status_at=now,
    )
    db.add(plan)
    db.commit()
    return plan


def refresh_plan_states(db: Session, now: datetime) -> None:
    """惰性复核：许可/拓扑/锁阀条件变化后，valid 方案转为 stale/expired。

    快照（result_json）永不变更，仅状态迁移；查看历史快照仍得到当时的旧路径。
    """
    now = normalize_dt(now)
    sweep_expired(db, now)
    plans = db.scalars(select(IsolationPlan).where(IsolationPlan.status == "valid")).all()
    cur_hash = snapshot_hash(topology_snapshot(db))
    for plan in plans:
        reason: str | None = None
        if plan.permit_id is not None:
            permit = db.get(BypassPermit, plan.permit_id)
            if permit is None:
                reason = "permit_revoked"
            elif permit.status == "revoked":
                reason = "permit_revoked"
            elif permit.status == "expired":
                plan.status = "expired"
                plan.status_reason = "permit_expired"
                plan.status_at = now
                continue
            else:
                match, _ = conditions_match(permit, db)
                if not match:
                    reason = "conditions_changed"
        if reason is None and plan.topo_hash != cur_hash:
            reason = "conditions_changed"
        if reason is not None:
            plan.status = "stale"
            plan.status_reason = reason
            plan.status_at = now
    db.flush()


# ---------------- 视图 ----------------

def permit_view(db: Session, permit: BypassPermit) -> dict[str, Any]:
    match, reasons = conditions_match(permit, db)
    return {
        "id": permit.id,
        "target_id": permit.target_id,
        "valve_ids": permit_valves(permit),
        "segment_ids": json.loads(permit.segment_ids_json),
        "purpose": permit.purpose,
        "start_at": _iso(permit.start_at),
        "end_at": _iso(permit.end_at),
        "status": permit.status,
        "topo_hash": permit.topo_hash,
        "precondition_locks": json.loads(permit.precondition_locks_json),
        "conditions_match": match,
        "condition_mismatches": reasons,
        "created_at": _iso(permit.created_at),
        "activated_at": _iso(permit.activated_at),
        "revoked_at": _iso(permit.revoked_at),
        "expired_at": _iso(permit.expired_at),
        "revoke_reason": permit.revoke_reason,
        "disclaimer": DISCLAIMER,
    }


def event_view(ev: PermitEvent) -> dict[str, Any]:
    return {
        "seq": ev.seq,
        "event_type": ev.event_type,
        "at": _iso(ev.at),
        "recorded_at": _iso(ev.recorded_at),
        "detail": json.loads(ev.detail_json),
        "prev_hash": ev.prev_hash,
        "hash": ev.hash,
    }


def plan_view(plan: IsolationPlan) -> dict[str, Any]:
    result = json.loads(plan.result_json)
    return {
        "id": plan.id,
        "target_id": plan.target_id,
        "permit_id": plan.permit_id,
        "status": plan.status,
        "status_reason": plan.status_reason,
        "feasible": plan.feasible,
        "created_at": _iso(plan.created_at),
        "status_at": _iso(plan.status_at),
        "best_solution": result.get("best_solution", []),
        "supply_paths": (result.get("solutions") or [{}])[0].get("supply_paths", {})
        if plan.feasible
        else {},
        "result": result,
        "disclaimer": DISCLAIMER,
    }
