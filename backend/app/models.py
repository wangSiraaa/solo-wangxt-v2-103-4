"""数据库模型：节点（设备/供给点）、管段、阀门、临时旁路许可、隔离方案。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
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


# ---------------- 临时旁路许可（培训模拟） ----------------

#: 许可状态机：draft -> active -> expired/revoked；draft 超窗同样 -> expired。
PERMIT_STATUSES = ("draft", "active", "expired", "revoked")
PERMIT_TERMINAL = ("expired", "revoked")

#: 方案状态：valid=当前条件有效；stale=待复核（许可被撤销或拓扑/锁阀条件已变化）；
#: expired=许可时限到达。
PLAN_STATUSES = ("valid", "stale", "expired")


class BypassPermit(Base):
    """临时旁路许可：在指定维护窗口内允许把一条（平时停用的）旁路纳入可用路径。

    许可只是培训模拟层的授权记录，绝不改动阀门真实状态（不输出真实控制指令）。
    创建时抓取拓扑快照与前置锁定状态；之后拓扑/锁阀一旦变化，许可条件即不再匹配，
    隔离计算不得再使用该许可，关联方案转为待复核/过期。
    """

    __tablename__ = "bypass_permits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_id: Mapped[str] = mapped_column(String(32), nullable=False)
    bypass_key: Mapped[str] = mapped_column(String(255), nullable=False)  # 排序后阀门 id 逗号连接
    valve_ids_json: Mapped[str] = mapped_column(Text, nullable=False)     # JSON list[str]
    segment_ids_json: Mapped[str] = mapped_column(Text, nullable=False)   # JSON list[str]
    purpose: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    start_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    topo_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    topo_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    precondition_locks_json: Mapped[str] = mapped_column(Text, nullable=False)  # JSON {valve_id: bool}
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    events: Mapped[list["PermitEvent"]] = relationship(
        back_populates="permit", order_by="PermitEvent.seq", cascade="all, delete-orphan"
    )
    plans: Mapped[list["IsolationPlan"]] = relationship(back_populates="permit")


class PermitEvent(Base):
    """许可事件审计日志（仅追加，哈希链防篡改）。

    seq 为许可内严格递增序号，是事件顺序的唯一权威依据；客户端自带的业务时刻
    （at，演练模拟时钟）只影响窗口校验，不会重排历史。hash 链接前一条事件，
    任何事后篡改都会使校验失败。
    """

    __tablename__ = "permit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    permit_id: Mapped[int] = mapped_column(ForeignKey("bypass_permits.id"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime, nullable=False)          # 业务时刻（可为模拟时钟）
    recorded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)  # 服务器落库时刻
    detail_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)

    permit: Mapped[BypassPermit] = relationship(back_populates="events")

    __table_args__ = (UniqueConstraint("permit_id", "seq", name="uq_permit_event_seq"),)


class IsolationPlan(Base):
    """一次隔离计算的持久化方案快照。

    result_json 保存计算时刻的完整结果（含供给路径），之后不再修改——
    查看历史快照时看到的就是当时的旧路径。状态（valid/stale/expired）
    随许可生命周期与拓扑/锁阀条件变化而迁移，但快照本身不可变。
    """

    __tablename__ = "isolation_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_id: Mapped[str] = mapped_column(String(32), nullable=False)
    permit_id: Mapped[int | None] = mapped_column(ForeignKey("bypass_permits.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="valid")
    status_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    feasible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    result_json: Mapped[str] = mapped_column(Text, nullable=False)
    topo_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    locks_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    permit: Mapped[BypassPermit | None] = relationship(back_populates="plans")
