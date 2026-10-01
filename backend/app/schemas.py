"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ValveLockIn(BaseModel):
    locked: bool = Field(..., description="True=锁定该阀门（保持现状不可操作），False=解锁")


class IsolationIn(BaseModel):
    target_id: str = Field("T", description="待隔离目标设备节点 id")
    locks: dict[str, bool] | None = Field(
        None, description="可选：一次性提交的阀门锁定状态 {valve_id: locked}"
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
