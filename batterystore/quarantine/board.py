"""簇隔离执行板。

现场隔离是"下发分断 → 接触器回采确认"两步，跨簇并发执行，任何一簇都可能
通信超时或拒绝分断。执行板把这层物理动作收敛成幂等接口：

* :meth:`isolate_rack` 对同一簇重复下发是安全的，已咬合的簇直接回成功，
  不会因为重算范围或续作把已隔离的簇"撤回来"；
* :meth:`fail_once` 注入一次下发失败（按簇或按批次冻结），用于验证续作；
* 真实执行器替换本类时只需保持同样的返回结构，管理器与编排无需改动。
"""

from __future__ import annotations

import threading

from ..errors import InterlockError
from .targets import TARGET_BATCH, TARGET_RACK


class RackIsolationBoard:
    """模拟跨簇接触器隔离板的执行器（可注入失败）。"""

    def __init__(self) -> None:
        self._latched: set[str] = set()
        self._frozen_batches: set[str] = set()
        self._fail_next_racks: set[str] = set()
        self._fail_next_batches: set[str] = set()
        self._attempts = 0
        self._lock = threading.RLock()

    @property
    def attempts(self) -> int:
        """返回累计下发次数，用于核对续作没有重复动已完成的对象。"""

        with self._lock:
            return self._attempts

    def fail_once(self, *, rack_id: str = "", batch_id: str = "") -> dict:
        """让指定簇或批次的下一次下发失败一次（验证失败续作）。"""

        with self._lock:
            if rack_id:
                self._fail_next_racks.add(rack_id)
            if batch_id:
                self._fail_next_batches.add(batch_id)
            return {"rack_id": rack_id, "batch_id": batch_id, "armed": True}

    def isolated_racks(self) -> list[str]:
        """返回已咬合隔离的簇。"""

        with self._lock:
            return sorted(self._latched)

    def rehydrate(self, *, racks: list[str], batches: list[str]) -> dict:
        """按隔离令重放已完成的物理态（仅回采重建，不重新下发动作）。

        进程重启后执行板内存清空，但现场接触器仍在分断位、批次仍在停用态。
        用隔离令里 ``isolated`` 的目标把执行板状态重建回来，保证联锁与去重
        判断在重启后依然成立；这不产生新的下发尝试，``attempts`` 不变。
        """

        with self._lock:
            self._latched.update(racks)
            self._frozen_batches.update(batches)
            return self.snapshot()

    def frozen_batches(self) -> list[str]:
        """返回已停用冻结的批次。"""

        with self._lock:
            return sorted(self._frozen_batches)

    def is_rack_isolated(self, rack_id: str) -> bool:
        """某簇是否已隔离。"""

        with self._lock:
            return rack_id in self._latched

    def isolate_rack(self, rack_id: str, reason: str) -> dict:
        """对一簇下发隔离并回采确认，返回尝试记录。"""

        with self._lock:
            self._attempts += 1
            if rack_id in self._latched:
                return {"kind": TARGET_RACK, "target_id": rack_id, "isolated": True, "noop": True, "reason": reason}
            if rack_id in self._fail_next_racks:
                self._fail_next_racks.discard(rack_id)
                raise InterlockError(f"簇 {rack_id} 隔离接触器下发超时", component="quarantine")
            self._latched.add(rack_id)
            return {"kind": TARGET_RACK, "target_id": rack_id, "isolated": True, "noop": False, "reason": reason}

    def freeze_batch(self, batch_id: str, reason: str) -> dict:
        """停用冻结一个批次（拦截后续装机/放行），返回尝试记录。"""

        with self._lock:
            self._attempts += 1
            if batch_id in self._frozen_batches:
                return {"kind": TARGET_BATCH, "target_id": batch_id, "frozen": True, "noop": True, "reason": reason}
            if batch_id in self._fail_next_batches:
                self._fail_next_batches.discard(batch_id)
                raise InterlockError(f"批次 {batch_id} 冻结指令未获确认", component="quarantine")
            self._frozen_batches.add(batch_id)
            return {"kind": TARGET_BATCH, "target_id": batch_id, "frozen": True, "noop": False, "reason": reason}

    def snapshot(self) -> dict:
        """返回执行板当前状态。"""

        with self._lock:
            return {
                "isolated_racks": sorted(self._latched),
                "frozen_batches": sorted(self._frozen_batches),
                "attempts": self._attempts,
            }
