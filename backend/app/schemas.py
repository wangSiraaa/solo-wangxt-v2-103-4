"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ValveLockIn(BaseModel):
    locked: bool = Field(..., description="True=锁定该阀门（保持现状不可操作），False=解锁")


class ValveStateIn(BaseModel):
    is_open: bool = Field(..., description="True=阀门打开（投用），False=关闭（停用，模拟检修）")


class IsolationIn(BaseModel):
    target_id: str = Field("T", description="待隔离目标设备节点 id")
    locks: dict[str, bool] | None = Field(
        None, description="可选：一次性提交的阀门锁定状态 {valve_id: locked}"
    )
    permit_id: int | None = Field(
        None, description="可选：本次计算应用的临时旁路许可 id（许可必须已生效且条件匹配）"
    )
    at: datetime | None = Field(None, description="可选：演练模拟时钟（缺省为服务器当前时间）")


class NodeOut(BaseModel):
    id: str
    name: str
    kind: str
    x: float
    y: float
    essential: bool


class ValveOut(BaseModel):
    id: str
    name: str
    segment_id: str
    endpoints: list[str]
    is_open: bool
    locked: bool
    operable: bool
    is_bypass: bool


class SegmentOut(BaseModel):
    id: str
    source: str
    target: str
    direction: str
    kind: str
    is_bypass: bool
    valve_id: str | None


class TopologyOut(BaseModel):
    nodes: list[NodeOut]
    segments: list[SegmentOut]
    valves: list[ValveOut]


class IsolationSolution(BaseModel):
    close_valves: list[str]
    size: int
    alternative_rank: int
    closes_bypass_valves: list[str] = []
    supply_paths: dict[str, list[str] | None] = {}


class ResidualPath(BaseModel):
    nodes: list[str]
    valves: list[str | None]
    locked_valves_on_path: list[str]
    uses_bypass: bool


class UnconstrainedBest(BaseModel):
    close_valves: list[str]
    size: int
    disconnects_essentials: list[str]
    unavoidable_essentials: list[str] = []


class IsolationOut(BaseModel):
    feasible: bool
    target_id: str
    sources: list[str]
    essentials: list[str]
    candidate_valves: list[ValveOut]
    examined_combinations: int = 0
    solutions: list[dict[str, Any]] = []
    best_solution: list[str] = []
    residual_path: dict[str, Any] | None = None
    locked_witness_path: dict[str, Any] | None = None
    infeasible_reason: str | None = None
    unconstrained_best: dict[str, Any] | None = None
    permit_valves: list[str] = []
    permit: dict[str, Any] | None = None
    plan_id: int | None = None
    disclaimer: str = ""


# ---------------- 临时旁路许可 ----------------


class PermitCreateIn(BaseModel):
    target_id: str = Field("T", description="维护窗口内的隔离目标设备")
    valve_ids: list[str] = Field(..., description="许可覆盖的旁路阀门 id 列表")
    purpose: str = Field("", description="许可目的（维护窗口说明）")
    start_at: datetime = Field(..., description="窗口开始时刻")
    end_at: datetime = Field(..., description="窗口结束时刻")
    at: datetime | None = Field(None, description="可选：演练模拟时钟")


class PermitActionIn(BaseModel):
    at: datetime | None = Field(None, description="可选：演练模拟时钟（缺省为服务器当前时间）")
    reason: str = Field("", description="撤销原因（仅撤销使用）")


class PermitEventOut(BaseModel):
    seq: int
    event_type: str
    at: str | None
    recorded_at: str | None
    detail: dict[str, Any]
    prev_hash: str
    hash: str


class PermitOut(BaseModel):
    id: int
    target_id: str
    valve_ids: list[str]
    segment_ids: list[str]
    purpose: str
    start_at: str | None
    end_at: str | None
    status: str
    topo_hash: str
    precondition_locks: dict[str, bool]
    conditions_match: bool
    condition_mismatches: list[str]
    created_at: str | None
    activated_at: str | None
    revoked_at: str | None
    expired_at: str | None
    revoke_reason: str | None
    disclaimer: str = ""


class PermitHistoryOut(BaseModel):
    permit: PermitOut
    events: list[PermitEventOut]
    chain_valid: bool
    disclaimer: str = ""


class PlanOut(BaseModel):
    id: int
    target_id: str
    permit_id: int | None
    status: str
    status_reason: str | None
    feasible: bool
    created_at: str | None
    status_at: str | None
    best_solution: list[str]
    supply_paths: dict[str, list[str] | None]
    result: dict[str, Any]
    disclaimer: str = ""
