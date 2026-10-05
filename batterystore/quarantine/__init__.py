"""批次性异常隔离：隔离令、跨簇并发执行、失败续作与范围单调合并。"""

from __future__ import annotations

from .board import RackIsolationBoard
from .manager import QuarantineManager
from .orders import QuarantineOrder, TargetState, merge_targets

__all__ = [
    "QuarantineManager",
    "QuarantineOrder",
    "RackIsolationBoard",
    "TargetState",
    "merge_targets",
]
