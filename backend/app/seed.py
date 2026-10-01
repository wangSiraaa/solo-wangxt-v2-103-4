"""演示拓扑种子数据（固定管网，仅用于工艺培训演示）。

拓扑示意（箭头为介质名义流向）:

  SRC ─V0─ N1 ─V1─ N2 ─V_TIN─ [T] ─V_TOUT─ N3
           │              ╲  旁路: N2─VBPIN─BP─VBPOUT─N3  ╱
          V_P1            环网联络: N1─VLK1─N5─VLK2─N3
           ↓
          P1                N3 ─V_P2─ P2

- 旁路（bypass）与目标设备 [T] 并联：T 隔离后介质仍经 BP 绕回 N3 供 P2。
- 环网联络管 N1-N5-N3 提供 N3 的第二路来源（锁定阀门场景使用）。
- P1/P2 为必要供给点（essential=True），任何方案都不得断供。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Node, Segment, Valve

NODES: list[dict] = [
    {"id": "SRC", "name": "介质来源", "kind": "source", "x": 60, "y": 200, "essential": False},
    {"id": "N1", "name": "节点N1", "kind": "junction", "x": 250, "y": 200, "essential": False},
    {"id": "N2", "name": "节点N2", "kind": "junction", "x": 480, "y": 200, "essential": False},
    {"id": "T", "name": "目标设备T", "kind": "equipment", "x": 680, "y": 200, "essential": False},
    {"id": "N3", "name": "节点N3", "kind": "junction", "x": 880, "y": 200, "essential": False},
    {"id": "BP", "name": "旁路桥点", "kind": "junction", "x": 680, "y": 60, "essential": False},
    {"id": "N5", "name": "环网桥点N5", "kind": "junction", "x": 560, "y": 430, "essential": False},
    {"id": "P1", "name": "支路用户P1", "kind": "consumer", "x": 250, "y": 430, "essential": True},
    {"id": "P2", "name": "支路用户P2", "kind": "consumer", "x": 880, "y": 430, "essential": True},
]

# (segment_id, upstream, downstream, valve_id, valve_name, kind, is_bypass)
SEGMENTS: list[tuple[str, str, str, str, str, str, bool]] = [
    ("E0",    "SRC", "N1", "V0",      "来源总阀",       "main",   False),
    ("E1",    "N1",  "N2", "V1",      "N1-N2隔断阀",    "main",   False),
    ("E2",    "N2",  "T",  "V_TIN",   "设备T入口阀",    "main",   False),
    ("E3",    "T",   "N3", "V_TOUT",  "设备T出口阀",    "main",   False),
    ("EB1",   "N2",  "BP", "V_BP_IN", "旁路入口阀",     "bypass", True),
    ("EB2",   "BP",  "N3", "V_BP_OUT","旁路出口阀",     "bypass", True),
    ("EL1",   "N1",  "N5", "V_LK1",   "环网联络阀1",    "main",   False),
    ("EL2",   "N5",  "N3", "V_LK2",   "环网联络阀2",    "main",   False),
    ("EP1",   "N1",  "P1", "V_P1",    "P1支路阀",       "branch", False),
    ("EP2",   "N3",  "P2", "V_P2",    "P2支路阀",       "branch", False),
]


def seed_database(db: Session) -> None:
    """幂等写入演示拓扑。已存在数据时不覆盖用户的锁定状态。"""
    existing = db.scalar(select(Node).limit(1))
    if existing is not None:
        return

    db.add_all([Node(**n) for n in NODES])
    for seg_id, up, down, valve_id, valve_name, kind, is_bypass in SEGMENTS:
        seg = Segment(id=seg_id, upstream_id=up, downstream_id=down, kind=kind, is_bypass=is_bypass)
        seg.valve = Valve(id=valve_id, name=valve_name, is_open=True, locked=False, operable=True)
        db.add(seg)
    db.commit()


def reset_database(db: Session) -> None:
    """恢复演示初始状态：全部阀门打开、未锁定。"""
    db.query(Valve).update({Valve.locked: False, Valve.is_open: True, Valve.operable: True})
    db.commit()
