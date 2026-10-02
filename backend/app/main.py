"""FastAPI 入口：拓扑查询、阀门锁定/投用、隔离方案计算、临时旁路许可。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import isolation, permits
from .config import settings
from .database import Base, SessionLocal, engine, get_db
from .models import BypassPermit, IsolationPlan, Valve
from .schemas import (
    IsolationIn,
    IsolationOut,
    PermitActionIn,
    PermitCreateIn,
    PermitHistoryOut,
    PermitOut,
    PlanOut,
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


def _ref_time(at: datetime | None) -> datetime:
    """请求参考时刻：演练模拟时钟或服务器当前时间（UTC naive）。"""
    return permits.normalize_dt(at) if at is not None else permits.utcnow()


def _get_permit(db: Session, permit_id: int) -> BypassPermit:
    permit = db.get(BypassPermit, permit_id)
    if permit is None:
        raise HTTPException(status_code=404, detail=f"许可不存在: {permit_id}")
    return permit


def _permit_error(exc: permits.PermitError) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": exc.code, "message": exc.message})


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
    return {"id": valve.id, "locked": valve.locked}


@app.post("/api/valves/{valve_id}/state")
def set_valve_state(valve_id: str, body: ValveStateIn, db: Session = Depends(get_db)) -> dict[str, object]:
    """设置阀门开闭（投用/停用），用于演练中模拟旁路或管段退出运行。"""
    valve = db.scalar(select(Valve).where(Valve.id == valve_id))
    if valve is None:
        raise HTTPException(status_code=404, detail=f"阀门不存在: {valve_id}")
    valve.is_open = body.is_open
    db.commit()
    return {"id": valve.id, "is_open": valve.is_open}


@app.post("/api/reset")
def reset(db: Session = Depends(get_db)) -> dict[str, str]:
    reset_database(db)
    return {"status": "reset"}


@app.post("/api/isolation", response_model=IsolationOut)
def calc_isolation(body: IsolationIn, db: Session = Depends(get_db)) -> IsolationOut:
    seed_database(db)
    now = _ref_time(body.at)
    permits.sweep_expired(db, now)
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

    permit: BypassPermit | None = None
    permit_valves: set[str] = set()
    if body.permit_id is not None:
        permit = _get_permit(db, body.permit_id)
        if permit.status != "active":
            raise HTTPException(
                status_code=409,
                detail={"code": "permit_not_active", "message": f"许可 #{permit.id} 当前状态为 {permit.status}，不可用于计算"},
            )
        match, reasons = permits.conditions_match(permit, db)
        if not match:
            raise HTTPException(
                status_code=409,
                detail={"code": "conditions_changed", "message": "许可条件已不匹配: " + "; ".join(reasons)},
            )
        permit_valves = set(permits.permit_valves(permit))

    try:
        payload = isolation.compute_isolation(db, body.target_id, permit_valves=permit_valves)
    except isolation.TopologyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    plan = permits.record_plan(db, target_id=body.target_id, permit=permit, result=payload, now=now)
    payload["permit"] = permits.permit_view(db, permit) if permit else None
    payload["plan_id"] = plan.id
    payload["disclaimer"] = permits.DISCLAIMER
    return IsolationOut(**payload)


# ---------------- 临时旁路许可 ----------------


@app.post("/api/permits", response_model=PermitOut, status_code=201)
def create_permit(body: PermitCreateIn, db: Session = Depends(get_db)) -> PermitOut:
    seed_database(db)
    now = _ref_time(body.at)
    permits.sweep_expired(db, now)
    try:
        permit = permits.create_permit(
            db,
            target_id=body.target_id,
            valve_ids=body.valve_ids,
            purpose=body.purpose,
            start_at=body.start_at,
            end_at=body.end_at,
            now=now,
        )
    except permits.PermitError as exc:
        raise _permit_error(exc) from exc
    return PermitOut(**permits.permit_view(db, permit))


@app.get("/api/permits", response_model=list[PermitOut])
def list_permits(at: datetime | None = Query(None), db: Session = Depends(get_db)) -> list[PermitOut]:
    now = _ref_time(at)
    permits.sweep_expired(db, now)
    db.commit()
    rows = db.scalars(select(BypassPermit).order_by(BypassPermit.id)).all()
    return [PermitOut(**permits.permit_view(db, p)) for p in rows]


@app.get("/api/permits/{permit_id}", response_model=PermitOut)
def get_permit(permit_id: int, at: datetime | None = Query(None), db: Session = Depends(get_db)) -> PermitOut:
    now = _ref_time(at)
    permits.sweep_expired(db, now)
    db.commit()
    return PermitOut(**permits.permit_view(db, _get_permit(db, permit_id)))


@app.post("/api/permits/{permit_id}/activate", response_model=PermitOut)
def activate_permit(permit_id: int, body: PermitActionIn, db: Session = Depends(get_db)) -> PermitOut:
    permit = _get_permit(db, permit_id)
    try:
        permits.activate_permit(db, permit, _ref_time(body.at))
    except permits.PermitError as exc:
        raise _permit_error(exc) from exc
    return PermitOut(**permits.permit_view(db, permit))


@app.post("/api/permits/{permit_id}/revoke", response_model=PermitOut)
def revoke_permit(permit_id: int, body: PermitActionIn, db: Session = Depends(get_db)) -> PermitOut:
    permit = _get_permit(db, permit_id)
    try:
        permits.revoke_permit(db, permit, _ref_time(body.at), reason=body.reason)
    except permits.PermitError as exc:
        raise _permit_error(exc) from exc
    return PermitOut(**permits.permit_view(db, permit))


@app.get("/api/permits/{permit_id}/history", response_model=PermitHistoryOut)
def permit_history(permit_id: int, at: datetime | None = Query(None), db: Session = Depends(get_db)) -> PermitHistoryOut:
    now = _ref_time(at)
    permits.sweep_expired(db, now)
    db.commit()
    permit = _get_permit(db, permit_id)
    events = sorted(permit.events, key=lambda e: e.seq)
    return PermitHistoryOut(
        permit=PermitOut(**permits.permit_view(db, permit)),
        events=[permits.event_view(e) for e in events],
        chain_valid=permits.verify_chain(events),
        disclaimer=permits.DISCLAIMER,
    )


# ---------------- 隔离方案记录 ----------------


@app.get("/api/plans", response_model=list[PlanOut])
def list_plans(at: datetime | None = Query(None), db: Session = Depends(get_db)) -> list[PlanOut]:
    now = _ref_time(at)
    permits.refresh_plan_states(db, now)
    db.commit()
    rows = db.scalars(select(IsolationPlan).order_by(IsolationPlan.id)).all()
    return [PlanOut(**permits.plan_view(p)) for p in rows]


@app.get("/api/plans/{plan_id}", response_model=PlanOut)
def get_plan(plan_id: int, at: datetime | None = Query(None), db: Session = Depends(get_db)) -> PlanOut:
    now = _ref_time(at)
    permits.refresh_plan_states(db, now)
    db.commit()
    plan = db.get(IsolationPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"方案不存在: {plan_id}")
    return PlanOut(**permits.plan_view(plan))


@app.post("/api/plans/{plan_id}/recompute", response_model=PlanOut)
def recompute_plan(plan_id: int, body: PermitActionIn, db: Session = Depends(get_db)) -> PlanOut:
    """以当前拓扑/锁阀/许可条件重新计算，生成新方案（旧方案快照保留不动）。"""
    now = _ref_time(body.at)
    permits.refresh_plan_states(db, now)
    db.commit()
    plan = db.get(IsolationPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"方案不存在: {plan_id}")

    permit_valves: set[str] = set()
    permit: BypassPermit | None = None
    if plan.permit_id is not None:
        candidate = db.get(BypassPermit, plan.permit_id)
        if candidate is not None and candidate.status == "active":
            match, _ = permits.conditions_match(candidate, db)
            if match:
                permit = candidate
                permit_valves = set(permits.permit_valves(candidate))

    try:
        payload = isolation.compute_isolation(db, plan.target_id, permit_valves=permit_valves)
    except isolation.TopologyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    new_plan = permits.record_plan(db, target_id=plan.target_id, permit=permit, result=payload, now=now)
    return PlanOut(**permits.plan_view(new_plan))


# 前端静态资源（ng build 产物）挂在根路径。挂载放在所有 /api 路由之后，
# 因此 API 请求不会被静态应用截获；html=True 同时提供 SPA 回退。
if settings.frontend_dist.exists():
    app.mount(
        "/",
        StaticFiles(directory=settings.frontend_dist, html=True),
        name="frontend",
    )
