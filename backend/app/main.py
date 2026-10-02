"""FastAPI 入口：拓扑查询、阀门锁定/模拟开闭、隔离方案计算、临时旁路许可。

临时旁路许可（纯培训模拟）：许可只在有效且条件匹配时被隔离计算纳入；
时限到达、拓扑/锁阀变化或许可撤销后，关联方案转为待复核/已过期，
不会静默沿用旧计算。全部输出不构成真实控制指令。
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import isolation, permits
from .config import settings
from .database import Base, SessionLocal, engine, get_db
from .models import BypassPermit, IsolationPlan, PermitEvent, Valve
from .schemas import (
    ClockIn,
    ClockOut,
    IsolationIn,
    IsolationOut,
    PermitActionIn,
    PermitEventOut,
    PermitEventsOut,
    PermitIn,
    PermitOut,
    PlanDetailOut,
    PlanOut,
    RevalidateOut,
    TopologyOut,
    ValveLockIn,
    ValveStateIn,
)
from .seed import reset_database, seed_database


@asynccontextmanager
async def lifespan(app: FastAPI):  # pragma: no cover
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        seed_database(db)
    finally:
        db.close()
    yield


app = FastAPI(
    title="管网隔离方案演示 API（培训用）",
    description="基于固定拓扑与阀门模型的检修隔离候选集合计算，不连接真实控制系统。",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _permit_error(exc: permits.PermitError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}
    )


def _get_permit_or_404(db: Session, permit_id: int) -> BypassPermit:
    permit = db.get(BypassPermit, permit_id)
    if permit is None:
        raise HTTPException(status_code=404, detail=f"许可不存在: {permit_id}")
    return permit


def _permit_out(db: Session, permit: BypassPermit, now) -> PermitOut:
    checks = permits.permit_checks(db, permit, now)
    applicable = permit.status == "active" and checks["conditions_ok"]
    linked = [
        plan.id
        for plan in db.scalars(select(IsolationPlan)).all()
        if permit.id in (plan.permit_ids or [])
    ]
    return PermitOut(
        id=permit.id,
        target_id=permit.target_id,
        bypass_valve_ids=list(permit.bypass_valve_ids),
        purpose=permit.purpose,
        starts_at=permits.as_utc(permit.starts_at),
        ends_at=permits.as_utc(permit.ends_at),
        precondition_locks=dict(permit.precondition_locks),
        topology_hash=permit.topology_hash,
        state_hash=permit.state_hash,
        status=permit.status,
        created_at=permits.as_utc(permit.created_at),
        closed_at=permits.as_utc(permit.closed_at) if permit.closed_at else None,
        applicable_now=applicable,
        linked_plan_ids=sorted(linked),
    )


def _plan_out(plan: IsolationPlan) -> PlanOut:
    return PlanOut(
        id=plan.id,
        target_id=plan.target_id,
        permit_ids=list(plan.permit_ids),
        close_valves=list(plan.close_valves),
        status=plan.status,
        created_at=permits.as_utc(plan.created_at),
        status_changed_at=permits.as_utc(plan.status_changed_at) if plan.status_changed_at else None,
    )


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/topology", response_model=TopologyOut)
def get_topology(db: Session = Depends(get_db)) -> TopologyOut:
    seed_database(db)
    return TopologyOut(**isolation.topology_payload(db))


@app.post("/api/valves/{valve_id}/lock")
def set_valve_lock(valve_id: str, body: ValveLockIn, db: Session = Depends(get_db)) -> dict[str, object]:
    valve = db.scalar(select(Valve).where(Valve.id == valve_id))
    if valve is None:
        raise HTTPException(status_code=404, detail=f"阀门不存在: {valve_id}")
    valve.locked = body.locked
    db.commit()
    # 锁阀变化后：快照失配的方案转为待复核，不再静默沿用
    permits.refresh_permits_and_plans(db, permits.current_now(db))
    return {"id": valve.id, "locked": valve.locked}


@app.post("/api/valves/{valve_id}/state")
def set_valve_state(valve_id: str, body: ValveStateIn, db: Session = Depends(get_db)) -> dict[str, object]:
    """模拟阀门开/关（培训演示状态，不输出真实控制指令）。锁定阀门保持现状。"""
    valve = db.scalar(select(Valve).where(Valve.id == valve_id))
    if valve is None:
        raise HTTPException(status_code=404, detail=f"阀门不存在: {valve_id}")
    if valve.locked:
        raise HTTPException(status_code=409, detail=f"阀门 {valve_id} 已锁定，保持现状不可操作")
    valve.is_open = body.is_open
    db.commit()
    permits.refresh_permits_and_plans(db, permits.current_now(db))
    return {"id": valve.id, "is_open": valve.is_open}


@app.post("/api/reset")
def reset(db: Session = Depends(get_db)) -> dict[str, str]:
    now = permits.current_now(db)
    reset_database(db)
    # 未终止许可一律撤销留痕，关联方案转为已过期；演示时钟恢复真实时间
    permits.reset_permits_and_plans(db, now)
    permits.set_clock(db, None)
    return {"status": "reset"}


@app.post("/api/isolation", response_model=IsolationOut)
def calc_isolation(body: IsolationIn, db: Session = Depends(get_db)) -> IsolationOut:
    seed_database(db)
    now = permits.current_now(db)
    if body.locks:
        known = {v.id for v in db.scalars(select(Valve)).all()}
        unknown = [vid for vid in body.locks if vid not in known]
        if unknown:
            raise HTTPException(status_code=400, detail=f"未知阀门: {', '.join(unknown)}")
        for vid, locked in body.locks.items():
            valve = db.get(Valve, vid)
            if valve is not None:
                valve.locked = locked
        db.commit()

    # 时限/快照检查先行：到期许可与失配方案在此落态，绝不静默沿用
    permits.refresh_permits_and_plans(db, now)

    applied: list[BypassPermit] = []
    if body.permit_id is not None:
        permit = _get_permit_or_404(db, body.permit_id)
        checks = permits.permit_checks(db, permit, now)
        if permit.status != "active" or not checks["conditions_ok"]:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "PERMIT_NOT_APPLICABLE",
                    "message": f"许可 #{permit.id} 当前不可应用（状态 {permit.status}，条件检查 {checks}）",
                },
            )
        applied = [permit]
    else:
        applied = permits.applicable_permits(db, body.target_id, now)

    permit_valves = {vid for p in applied for vid in p.bypass_valve_ids}
    try:
        payload = isolation.compute_isolation(db, body.target_id, permit_valves=permit_valves)
    except isolation.TopologyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    payload["applied_permits"] = [
        {
            "id": p.id,
            "bypass_valve_ids": list(p.bypass_valve_ids),
            "status": p.status,
            "purpose": p.purpose,
        }
        for p in applied
    ]
    payload["permit_note"] = (
        "本计算应用了临时旁路许可（培训模拟，不输出真实控制指令）："
        + "、".join(f"#{p.id}" for p in applied)
        if applied
        else None
    )
    if applied:
        permits.persist_plan(db, body.target_id, applied, payload, now)
    return IsolationOut(**payload)


# ---------------------------------------------------------------- 临时旁路许可

@app.post("/api/permits", response_model=PermitOut, status_code=201)
def create_permit(body: PermitIn, db: Session = Depends(get_db)) -> PermitOut:
    now = permits.current_now(db)
    try:
        permit = permits.create_permit(
            db,
            target_id=body.target_id,
            bypass_valve_ids=body.bypass_valve_ids,
            starts_at=body.starts_at,
            ends_at=body.ends_at,
            purpose=body.purpose,
            now=now,
        )
    except permits.PermitError as exc:
        raise _permit_error(exc) from exc
    return _permit_out(db, permit, now)


@app.get("/api/permits", response_model=list[PermitOut])
def list_permits(db: Session = Depends(get_db)) -> list[PermitOut]:
    now = permits.current_now(db)
    permits.refresh_permits_and_plans(db, now)
    rows = db.scalars(select(BypassPermit).order_by(BypassPermit.id)).all()
    return [_permit_out(db, p, now) for p in rows]


@app.get("/api/permits/{permit_id}", response_model=PermitOut)
def get_permit(permit_id: int, db: Session = Depends(get_db)) -> PermitOut:
    now = permits.current_now(db)
    permits.refresh_permits_and_plans(db, now)
    return _permit_out(db, _get_permit_or_404(db, permit_id), now)


@app.post("/api/permits/{permit_id}/activate", response_model=PermitOut)
def activate_permit(permit_id: int, body: PermitActionIn, db: Session = Depends(get_db)) -> PermitOut:
    now = permits.current_now(db)
    permit = _get_permit_or_404(db, permit_id)
    occurred_at = body.occurred_at or now
    try:
        permits.activate_permit(db, permit, occurred_at, now)
    except permits.PermitError as exc:
        raise _permit_error(exc) from exc
    return _permit_out(db, permit, now)


@app.post("/api/permits/{permit_id}/revoke", response_model=PermitOut)
def revoke_permit(permit_id: int, body: PermitActionIn, db: Session = Depends(get_db)) -> PermitOut:
    now = permits.current_now(db)
    permit = _get_permit_or_404(db, permit_id)
    occurred_at = body.occurred_at or now
    try:
        permits.revoke_permit(db, permit, occurred_at, now)
    except permits.PermitError as exc:
        raise _permit_error(exc) from exc
    return _permit_out(db, permit, now)


@app.post("/api/permits/{permit_id}/revalidate", response_model=RevalidateOut)
def revalidate_permit(permit_id: int, db: Session = Depends(get_db)) -> RevalidateOut:
    """执行中复核：窗口/快照/锁阀限制 + 启用许可后目标隔离与必要供给复核。"""
    now = permits.current_now(db)
    permits.refresh_permits_and_plans(db, now)
    permit = _get_permit_or_404(db, permit_id)
    report = permits.validate_permit(db, permit, now)
    return RevalidateOut(**report)


@app.get("/api/permits/{permit_id}/events", response_model=PermitEventsOut)
def permit_events(permit_id: int, db: Session = Depends(get_db)) -> PermitEventsOut:
    _get_permit_or_404(db, permit_id)
    events = list(
        db.scalars(
            select(PermitEvent).where(PermitEvent.permit_id == permit_id).order_by(PermitEvent.seq)
        ).all()
    )
    return PermitEventsOut(
        events=[
            PermitEventOut(
                id=ev.id,
                permit_id=ev.permit_id,
                seq=ev.seq,
                event_type=ev.event_type,
                occurred_at=permits.as_utc(ev.occurred_at),
                recorded_at=permits.as_utc(ev.recorded_at),
                detail=dict(ev.detail),
                prev_hash=ev.prev_hash,
                hash=ev.hash,
            )
            for ev in events
        ],
        chain_valid=permits.verify_chain(events),
    )


# ---------------------------------------------------------------- 关联方案

@app.get("/api/plans", response_model=list[PlanOut])
def list_plans(db: Session = Depends(get_db)) -> list[PlanOut]:
    now = permits.current_now(db)
    permits.refresh_permits_and_plans(db, now)
    plans = db.scalars(select(IsolationPlan).order_by(IsolationPlan.id.desc())).all()
    return [_plan_out(p) for p in plans]


@app.get("/api/plans/{plan_id}", response_model=PlanDetailOut)
def get_plan(plan_id: int, db: Session = Depends(get_db)) -> PlanDetailOut:
    now = permits.current_now(db)
    permits.refresh_permits_and_plans(db, now)
    plan = db.get(IsolationPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"方案不存在: {plan_id}")
    return PlanDetailOut(**_plan_out(plan).model_dump(), result=dict(plan.result))


# ---------------------------------------------------------------- 演示时钟

@app.get("/api/clock", response_model=ClockOut)
def get_clock(db: Session = Depends(get_db)) -> ClockOut:
    return ClockOut(now=permits.current_now(db), simulated=permits.clock_simulated(db))


@app.post("/api/clock", response_model=ClockOut)
def set_clock(body: ClockIn, db: Session = Depends(get_db)) -> ClockOut:
    permits.set_clock(db, body.now)
    # 时钟变化后立即按新时刻推进许可/方案状态（如时限到达）
    permits.refresh_permits_and_plans(db, permits.current_now(db))
    return ClockOut(now=permits.current_now(db), simulated=permits.clock_simulated(db))


# 前端静态资源（ng build 产物）挂在根路径。挂载放在所有 /api 路由之后，
# 因此 API 请求不会被静态应用截获；html=True 同时提供 SPA 回退。
if settings.frontend_dist.exists():
    app.mount(
        "/",
        StaticFiles(directory=settings.frontend_dist, html=True),
        name="frontend",
    )
