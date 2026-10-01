"""数据库模型：节点（设备/供给点）、管段、阀门。"""
from __future__ import annotations

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, UniqueConstraint
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
