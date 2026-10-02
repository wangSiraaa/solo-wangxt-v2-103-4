"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ValveLockIn(BaseModel):
    locked: bool = Field(..., description="True=锁定该阀门（保持现状不可操作），False=解锁")


class ValveStateIn(BaseModel):
    is_open: bool = Field(..., description="模拟阀门开/关（仅培训模拟，不输出真实控制指令）")


class IsolationIn(BaseModel):
    target_id: str = Field("T", description="待隔离目标设备节点 id")
    locks: dict[str, bool] | None = Field(
        None, description="可选：一次性提交的阀门锁定状态 {valve_id: locked}"
    )
    permit_id: int | None = Field(
        None,
        description="可选：显式指定临时旁路许可 id；不指定时自动应用当前有效的匹配许可",
    )


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
    permit_enabled: bool = False


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


class AppliedPermit(BaseModel):
    id: int
    bypass_valve_ids: list[str]
    status: str
    purpose: str


class IsolationOut(BaseModel):
    feasible: bool
    target_id: str
    sources: list[str]
    essentials: list[str]
    candidate_valves: list[ValveOut]
    permit_valves: list[str] = []
    applied_permits: list[AppliedPermit] = []
    permit_note: str | None = None
    examined_combinations: int = 0
    solutions: list[dict[str, Any]] = []
    best_solution: list[str] = []
    residual_path: dict[str, Any] | None = None
    locked_witness_path: dict[str, Any] | None = None
    infeasible_reason: str | None = None
    unconstrained_best: dict[str, Any] | None = None


# ---------------------------------------------------------------- 临时旁路许可

class PermitIn(BaseModel):
    target_id: str = Field("T", description="许可服务的隔离目标设备")
    bypass_valve_ids: list[str] = Field(..., min_length=1, description="许可覆盖的旁路阀门")
    starts_at: datetime = Field(..., description="维护窗口开始时刻")
    ends_at: datetime = Field(..., description="维护窗口结束时刻")
    purpose: str = Field("", description="许可目的（如：演练期间维持 P2 供给）")


class PermitActionIn(BaseModel):
    occurred_at: datetime | None = Field(
        None, description="事件自带时钟（可乱序）；缺省使用当前演示时钟"
    )


class PermitOut(BaseModel):
    id: int
    target_id: str
    bypass_valve_ids: list[str]
    purpose: str
    starts_at: datetime
    ends_at: datetime
    precondition_locks: dict[str, bool]
    topology_hash: str
    state_hash: str
    status: str
    created_at: datetime
    closed_at: datetime | None
    applicable_now: bool
    linked_plan_ids: list[int] = []


class PermitEventOut(BaseModel):
    id: int
    permit_id: int
    seq: int
    event_type: str
    occurred_at: datetime
    recorded_at: datetime
    detail: dict[str, Any]
    prev_hash: str
    hash: str


class PermitEventsOut(BaseModel):
    events: list[PermitEventOut]
    chain_valid: bool


class RevalidateOut(BaseModel):
    permit_id: int
    status: str
    checks: dict[str, bool]


class PlanOut(BaseModel):
    id: int
    target_id: str
    permit_ids: list[int]
    close_valves: list[str]
    status: str
    created_at: datetime
    status_changed_at: datetime | None


class PlanDetailOut(PlanOut):
    result: dict[str, Any]


class ClockIn(BaseModel):
    now: datetime | None = Field(None, description="演示时钟时刻；null 恢复真实系统时间")


class ClockOut(BaseModel):
    now: datetime
    simulated: bool
