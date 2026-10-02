"""临时旁路许可：生命周期、审计链与关联方案状态联动（纯培训模拟）。

确定性约定
----------
- 事件按到达顺序追加（permit 内 seq 严格递增），事件自带的 occurred_at
  允许乱序；状态机永远按到达顺序裁决，因此重复激活/撤销、迟到的激活
  事件都有唯一确定结果，且被拒绝的尝试同样留痕。
- 许可状态机：draft -> active -> expired/revoked；draft 超过窗口结束
  时刻也按 expired 处理。状态只前进不回退。
- 关联方案：许可终止（过期/撤销）=> 方案 expired；拓扑/阀门状态快照
  与方案记录不一致 => 方案 stale（待复核）。方案内容（含旧供给路径）
  一旦写入不再修改。
- 审计事件使用 SHA-256 链式哈希（prev_hash 串联），任何对历史事件的
  篡改都会破坏链条，可由 verify_chain 检出。
- 时间判定统一使用演示时钟（SimClock），未设置时退回真实系统时间。

本模块只做培训模拟，不输出任何真实控制指令。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import isolation
from .models import BypassPermit, IsolationPlan, Node, PermitEvent, SimClock, Valve

TERMINAL_STATUSES = {"expired", "revoked"}
GENESIS_HASH = "0" * 64


class PermitError(Exception):
    """许可操作被确定性拒绝。code 供前端/测试断言，status_code 映射 HTTP。"""

    def __init__(self, code: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


# ---------------------------------------------------------------- 基础工具

def as_utc(dt: datetime) -> datetime:
    """把任意时间规范化为带时区的 UTC（SQLite 读出的朴素时间按 UTC 解释）。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _real_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return as_utc(dt).isoformat()


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(obj: Any) -> str:
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


def fingerprints(db: Session) -> tuple[str, str]:
    """(拓扑结构快照哈希, 阀门状态快照哈希)。

    拓扑：节点 id/类型/是否必要供给点 + 管段端点/类型/是否旁路/对应阀门。
    状态：每只阀门的 开闭/锁定/可操作性。任一变化都会使许可前置条件
    与方案快照失配，从而触发“待复核/不再应用许可”的确定性行为。
    """
    nodes, edges = isolation._load(db)
    topo = {
        "nodes": sorted([n.id, n.kind, n.essential] for n in nodes),
        "segments": sorted(
            [e["id"], e["u"], e["v"], e["kind"], e["is_bypass"], e["valve_id"]]
            for e in edges.values()
        ),
    }
    state = sorted(
        [e["valve_id"], e["is_open"], e["locked"], e["operable"]]
        for e in edges.values()
        if e["valve_id"]
    )
    return _sha(topo), _sha(state)


# ---------------------------------------------------------------- 演示时钟

def current_now(db: Session) -> datetime:
    row = db.get(SimClock, 1)
    if row is not None and row.now is not None:
        return as_utc(row.now)
    return _real_now()


def set_clock(db: Session, now: datetime | None) -> None:
    row = db.get(SimClock, 1)
    if row is None:
        row = SimClock(id=1)
        db.add(row)
    row.now = as_utc(now) if now is not None else None
    db.commit()


def clock_simulated(db: Session) -> bool:
    row = db.get(SimClock, 1)
    return bool(row is not None and row.now is not None)


# ---------------------------------------------------------------- 审计链

def _event_hash(
    permit_id: int, seq: int, event_type: str, occurred_at: datetime, detail: dict, prev_hash: str
) -> str:
    return _sha(
        {
            "permit_id": permit_id,
            "seq": seq,
            "type": event_type,
            "occurred_at": _iso(occurred_at),
            "detail": detail,
            "prev": prev_hash,
        }
    )


def _add_event(
    db: Session,
    permit: BypassPermit,
    event_type: str,
    occurred_at: datetime,
    detail: dict | None = None,
) -> PermitEvent:
    detail = detail or {}
    last = db.scalar(
        select(PermitEvent)
        .where(PermitEvent.permit_id == permit.id)
        .order_by(PermitEvent.seq.desc())
        .limit(1)
    )
    seq = (last.seq if last else 0) + 1
    prev_hash = last.hash if last else GENESIS_HASH
    occurred_at = as_utc(occurred_at)
    ev = PermitEvent(
        permit_id=permit.id,
        seq=seq,
        event_type=event_type,
        occurred_at=occurred_at,
        recorded_at=_real_now(),
        detail=detail,
        prev_hash=prev_hash,
        hash=_event_hash(permit.id, seq, event_type, occurred_at, detail, prev_hash),
    )
    db.add(ev)
    return ev


def verify_chain(events: list[PermitEvent]) -> bool:
    """重放哈希链：seq 连续、prev 衔接、哈希可重算，任一不符即被篡改。"""
    prev = GENESIS_HASH
    for expect_seq, ev in enumerate(sorted(events, key=lambda e: e.seq), start=1):
        if ev.seq != expect_seq or ev.prev_hash != prev:
            return False
        if (
            _event_hash(ev.permit_id, ev.seq, ev.event_type, ev.occurred_at, ev.detail, ev.prev_hash)
            != ev.hash
        ):
            return False
        prev = ev.hash
    return True


# ---------------------------------------------------------------- 状态刷新

def refresh_permits_and_plans(db: Session, now: datetime) -> None:
    """惰性推进：到期的许可落为 expired，关联方案联动 stale/expired。

    在所有读/写端点入口调用，保证“时限到达后不会静默继续”。
    """
    now = as_utc(now)
    changed = False
    permits = list(db.scalars(select(BypassPermit)).all())
    for p in permits:
        if p.status in ("draft", "active") and now > as_utc(p.ends_at):
            was_status = p.status
            p.status = "expired"
            p.closed_at = now
            _add_event(
                db,
                p,
                "expired",
                as_utc(p.ends_at),
                {"reason": "维护窗口结束", "was_status": was_status},
            )
            changed = True
    if changed:
        db.flush()

    topo_h, state_h = fingerprints(db)
    by_id = {p.id: p for p in permits}
    for plan in db.scalars(select(IsolationPlan)).all():
        if plan.status == "expired":
            continue
        linked_terminal = any(
            by_id[pid].status in TERMINAL_STATUSES for pid in plan.permit_ids if pid in by_id
        )
        if linked_terminal:
            if plan.status != "expired":
                plan.status = "expired"
                plan.status_changed_at = now
                changed = True
        elif plan.status == "current" and (
            plan.topology_hash != topo_h or plan.state_hash != state_h
        ):
            plan.status = "stale"
            plan.status_changed_at = now
            changed = True
    if changed:
        db.commit()


# ---------------------------------------------------------------- 条件校验

def permit_checks(db: Session, permit: BypassPermit, now: datetime) -> dict[str, bool]:
    """许可“条件匹配”检查（与求解可行性无关的部分）。"""
    now = as_utc(now)
    topo_h, state_h = fingerprints(db)
    valves = {v.id: v for v in db.scalars(select(Valve)).all()}
    permit_valves = [valves.get(vid) for vid in permit.bypass_valve_ids]
    checks = {
        "window_ok": as_utc(permit.starts_at) <= now <= as_utc(permit.ends_at),
        "topology_ok": permit.topology_hash == topo_h,
        "state_ok": permit.state_hash == state_h,
        "valves_ok": all(v is not None and v.operable and not v.locked for v in permit_valves),
    }
    checks["conditions_ok"] = all(checks.values())
    return checks


def validate_permit(db: Session, permit: BypassPermit, now: datetime) -> dict[str, Any]:
    """激活前/执行中的完整校验：条件匹配 + 启用许可后的求解复核。

    求解复核确认：目标设备确实可被隔离、每个必要供给点仍可达
    （在许可旁路启用、许可阀门不可关闭的模型下）。
    """
    checks = permit_checks(db, permit, now)
    solver = isolation.compute_isolation(
        db, permit.target_id, permit_valves=set(permit.bypass_valve_ids)
    )
    checks["solver_feasible"] = bool(solver["feasible"])
    checks["target_isolated"] = bool(solver["feasible"])
    checks["essentials_supplied"] = bool(solver["feasible"])
    checks["applicable_now"] = bool(
        permit.status == "active" and checks["conditions_ok"]
    )
    checks["activatable_now"] = bool(
        permit.status == "draft" and checks["conditions_ok"] and checks["solver_feasible"]
    )
    return {"permit_id": permit.id, "status": permit.status, "checks": checks}


def applicable_permits(db: Session, target_id: str, now: datetime) -> list[BypassPermit]:
    """当前可被隔离计算应用的许可：已生效 + 窗口内 + 快照条件匹配。"""
    now = as_utc(now)
    out = []
    for p in db.scalars(select(BypassPermit)).all():
        if p.status != "active" or p.target_id != target_id:
            continue
        if permit_checks(db, p, now)["conditions_ok"]:
            out.append(p)
    return sorted(out, key=lambda p: p.id)


# ---------------------------------------------------------------- 生命周期

def _windows_overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def create_permit(
    db: Session,
    *,
    target_id: str,
    bypass_valve_ids: list[str],
    starts_at: datetime,
    ends_at: datetime,
    purpose: str,
    now: datetime,
) -> BypassPermit:
    now = as_utc(now)
    starts_at = as_utc(starts_at)
    ends_at = as_utc(ends_at)

    if ends_at <= starts_at:
        raise PermitError("INVALID_WINDOW", "结束时刻必须晚于开始时刻", 400)
    if db.get(Node, target_id) is None:
        raise PermitError("UNKNOWN_TARGET", f"目标节点不存在: {target_id}", 400)

    valve_ids = list(dict.fromkeys(bypass_valve_ids))
    if not valve_ids:
        raise PermitError("EMPTY_BYPASS", "至少选择一只旁路阀门", 400)
    valves = {v.id: v for v in db.scalars(select(Valve)).all()}
    unknown = [vid for vid in valve_ids if vid not in valves]
    if unknown:
        raise PermitError("UNKNOWN_VALVES", f"未知阀门: {', '.join(unknown)}", 400)
    not_bypass = [vid for vid in valve_ids if not valves[vid].segment.is_bypass]
    if not_bypass:
        raise PermitError("NOT_BYPASS", f"许可仅适用于旁路阀门: {', '.join(not_bypass)}", 400)
    blocked = [vid for vid in valve_ids if valves[vid].locked or not valves[vid].operable]
    if blocked:
        raise PermitError(
            "VALVE_LOCKED", f"阀门被锁定或不可操作，不能纳入许可: {', '.join(blocked)}"
        )

    # 先推进到期，再用最新状态做重叠判定（确定结果：同旁路重叠窗口拒绝新建）
    refresh_permits_and_plans(db, now)
    for other in db.scalars(select(BypassPermit)).all():
        if other.status in TERMINAL_STATUSES:
            continue
        if not set(other.bypass_valve_ids) & set(valve_ids):
            continue
        if _windows_overlap(starts_at, ends_at, as_utc(other.starts_at), as_utc(other.ends_at)):
            raise PermitError(
                "PERMIT_OVERLAP",
                f"与现有许可 #{other.id}（{other.status}）的旁路与窗口重叠，创建被拒绝",
            )

    topo_h, state_h = fingerprints(db)
    permit = BypassPermit(
        target_id=target_id,
        bypass_valve_ids=valve_ids,
        purpose=purpose,
        starts_at=starts_at,
        ends_at=ends_at,
        precondition_locks={vid: v.locked for vid, v in sorted(valves.items())},
        topology_hash=topo_h,
        state_hash=state_h,
        status="draft",
        created_at=now,
    )
    db.add(permit)
    db.flush()
    _add_event(
        db,
        permit,
        "created",
        now,
        {
            "target_id": target_id,
            "bypass_valve_ids": valve_ids,
            "window": [_iso(starts_at), _iso(ends_at)],
            "purpose": purpose,
        },
    )
    db.commit()
    return permit


def activate_permit(db: Session, permit: BypassPermit, occurred_at: datetime, now: datetime) -> BypassPermit:
    """激活许可。所有拒绝都先落审计事件再抛出，结果确定。"""
    occurred_at = as_utc(occurred_at)
    now = as_utc(now)
    refresh_permits_and_plans(db, now)

    def reject(code: str, message: str) -> None:
        _add_event(db, permit, "activate_rejected", occurred_at, {"code": code, "message": message})
        db.commit()
        raise PermitError(code, message)

    if permit.status != "draft":
        reject("NOT_DRAFT", f"许可当前状态为 {permit.status}，只有草稿可以激活")
    if not as_utc(permit.starts_at) <= occurred_at <= as_utc(permit.ends_at):
        reject(
            "NOT_IN_WINDOW",
            f"激活时刻 {_iso(occurred_at)} 不在许可窗口 "
            f"[{_iso(permit.starts_at)}, {_iso(permit.ends_at)}] 内",
        )
    for other in db.scalars(select(BypassPermit)).all():
        if other.id == permit.id or other.status != "active":
            continue
        if set(other.bypass_valve_ids) & set(permit.bypass_valve_ids):
            reject("PERMIT_OVERLAP", f"许可 #{other.id} 已生效且旁路重叠，不能同时生效")

    checks = permit_checks(db, permit, occurred_at)
    if not (checks["topology_ok"] and checks["state_ok"]):
        reject(
            "CONDITIONS_CHANGED",
            "拓扑或阀门状态与许可前置快照不一致（锁阀/开闭已变化），激活被拒绝；原许可保持草稿",
        )
    if not checks["valves_ok"]:
        reject("VALVE_LOCKED", "许可覆盖的旁路阀门已被锁定或不可操作，激活被拒绝")

    solver = isolation.compute_isolation(
        db, permit.target_id, permit_valves=set(permit.bypass_valve_ids)
    )
    if not solver["feasible"]:
        reject(
            "VALIDATION_FAILED",
            "启用该旁路后仍无可行隔离方案（目标隔离或必要供给点供给不满足），激活被拒绝",
        )

    permit.status = "active"
    _add_event(
        db,
        permit,
        "activated",
        occurred_at,
        {"checks": {k: v for k, v in checks.items()}, "solver_feasible": True},
    )
    db.commit()
    return permit


def revoke_permit(db: Session, permit: BypassPermit, occurred_at: datetime, now: datetime) -> BypassPermit:
    occurred_at = as_utc(occurred_at)
    now = as_utc(now)
    refresh_permits_and_plans(db, now)

    if permit.status in TERMINAL_STATUSES:
        _add_event(
            db,
            permit,
            "revoke_rejected",
            occurred_at,
            {"code": "ALREADY_CLOSED", "message": f"许可已处于终止状态 {permit.status}"},
        )
        db.commit()
        raise PermitError("ALREADY_CLOSED", f"许可已处于终止状态 {permit.status}，撤销被拒绝")

    was_status = permit.status
    permit.status = "revoked"
    permit.closed_at = occurred_at
    _add_event(db, permit, "revoked", occurred_at, {"was_status": was_status})
    db.commit()
    # 许可终止 -> 关联方案立即转为已过期
    refresh_permits_and_plans(db, now)
    return permit


# ---------------------------------------------------------------- 方案持久化

def persist_plan(
    db: Session, target_id: str, permits: list[BypassPermit], payload: dict, now: datetime
) -> IsolationPlan:
    """把许可下算得的方案落库（含完整结果快照）。已有等效当前方案时不重复记录。"""
    now = as_utc(now)
    permit_ids = sorted(p.id for p in permits)
    topo_h, state_h = fingerprints(db)
    close_valves = list(payload.get("best_solution") or [])
    for plan in db.scalars(select(IsolationPlan).where(IsolationPlan.status == "current")).all():
        if (
            plan.target_id == target_id
            and sorted(plan.permit_ids) == permit_ids
            and plan.close_valves == close_valves
            and plan.topology_hash == topo_h
            and plan.state_hash == state_h
        ):
            return plan
    plan = IsolationPlan(
        target_id=target_id,
        permit_ids=permit_ids,
        close_valves=close_valves,
        result=payload,
        topology_hash=topo_h,
        state_hash=state_h,
        status="current",
        created_at=now,
    )
    db.add(plan)
    db.commit()
    return plan


def reset_permits_and_plans(db: Session, now: datetime) -> None:
    """系统重置：未终止许可一律撤销（留痕），关联方案随之过期。历史不删除。"""
    now = as_utc(now)
    for p in db.scalars(select(BypassPermit)).all():
        if p.status not in TERMINAL_STATUSES:
            p.status = "revoked"
            p.closed_at = now
            _add_event(db, p, "revoked", now, {"reason": "系统重置"})
    db.commit()
    refresh_permits_and_plans(db, now)
