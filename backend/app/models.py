"""数据库模型：节点（设备/供给点）、管段、阀门、临时旁路许可及其审计/方案。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # source/equipment/consumer/junction
    x: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    y: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    essential: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Valve(Base):
    """阀门与其所在管段一一对应（demo 模型）。

    is_open:       阀门当前实际开闭状态；锁定后用户不可再改变其操作状态。
    locked:        用户锁定标记，锁定阀门不参与候选关闭集合。
                   初始状态全部为打开、未锁定；当前演示中可操作的动作是“关闭”，
                   因此锁定等价于“禁止关闭”（保持打开）。
    operable:      工艺上是否允许操作（个别阀门检修/铅封，恒不可用）。
    """

    __tablename__ = "valves"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    segment_id: Mapped[str] = mapped_column(ForeignKey("segments.id"), nullable=False)
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    operable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    segment: Mapped["Segment"] = relationship(back_populates="valve")


class Segment(Base):
    """有向管段：upstream -> downstream 表示介质名义流向。

    隔离计算按“物理连通”使用无向图（介质可被两侧隔离边界切断，
    且检修隔离关注连通性而非流向）；direction 用于前端箭头标注、
    来源/下游识别与结果解释。旁路是与主管并联的一对管段。
    """

    __tablename__ = "segments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    upstream_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), nullable=False)
    downstream_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="main")  # main/bypass/branch
    is_bypass: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    valve: Mapped["Valve | None"] = relationship(back_populates="segment", uselist=False)

    __table_args__ = (UniqueConstraint("upstream_id", "downstream_id", name="uq_segment_endpoints"),)


class BypassPermit(Base):
    """临时旁路许可（培训模拟）：在指定维护窗口内临时启用一条旁路维持供给。

    - 创建时保存适用拓扑快照哈希（topology_hash）与阀门状态快照哈希
      （state_hash，含开闭/锁定/可操作性）以及显式的前置锁定状态表；
      任一快照与当前不一致时许可不再被应用，关联方案转为待复核。
    - 状态机：draft(草稿) -> active(已生效) -> expired(已过期)/revoked(已撤销)；
      草稿超过窗口结束时刻同样按过期处理。状态只前进、不回退。
    """

    __tablename__ = "bypass_permits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_id: Mapped[str] = mapped_column(String(32), nullable=False)
    bypass_valve_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    purpose: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    precondition_locks: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    topology_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    events: Mapped[list["PermitEvent"]] = relationship(
        back_populates="permit", order_by="PermitEvent.seq"
    )


class PermitEvent(Base):
    """许可审计事件：仅追加、按到达顺序编号，SHA-256 链式哈希防篡改。

    occurred_at 为事件自带时钟（允许乱序），seq/recorded_at 为服务端到达
    顺序；状态机永远按到达顺序裁决，保证乱序/重复事件结果确定。
    """

    __tablename__ = "permit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    permit_id: Mapped[int] = mapped_column(ForeignKey("bypass_permits.id"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)

    permit: Mapped[BypassPermit] = relationship(back_populates="events")


class IsolationPlan(Base):
    """许可下计算并持久化的隔离方案快照。

    result 保存计算时刻的完整结果（含供给路径），之后不再修改——
    查看历史快照时展示的就是这组旧路径。状态：current(有效) ->
    stale(待复核，拓扑/锁阀已变化) / expired(已过期，许可终止)。
    """

    __tablename__ = "isolation_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_id: Mapped[str] = mapped_column(String(32), nullable=False)
    permit_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    close_valves: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    result: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    topology_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="current")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SimClock(Base):
    """演示时钟（单行）：设置后所有许可/方案判定使用该时刻，保证演练可重复。

    now 为 NULL 时使用真实系统时间。仅用于培训模拟，不对应任何真实时钟源。
    """

    __tablename__ = "sim_clock"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    now: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
