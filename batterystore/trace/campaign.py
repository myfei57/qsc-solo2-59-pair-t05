"""隔离执行跟踪。

一次批次处置对应一个战役（campaign）：范围算出来后落成逐簇任务，任务状态机
为 ``pending → running → isolated | failed``。两条硬规则：

1. **单调合并**：重算范围（换口径、补录移动记录）只能往战役里加簇，已在
   战役里的簇永远保留——已经隔离的簇不能因为重算范围被撤回来，安全侧
   状态只进不退。
2. **失败可续**：执行中途失败的簇留在 ``failed`` 并记下原因，续作只重跑
   ``pending`` 与 ``failed``，已隔离的簇不重复下落。

全部状态迁移都追加到隔离流水，重启后按流水折叠重建，跨重启也能续作。
"""

from __future__ import annotations

import threading

from ..errors import TraceError
from ..store.journal import JsonlJournal
from .scope import CALIBERS

TASK_STATES = ("pending", "running", "isolated", "failed")

CAMPAIGN_OPENED = "campaign_opened"
SCOPE_MERGED = "scope_merged"
TASK_STATE = "task_state"


class IsolationCampaign:
    """一个批次处置战役的逐簇任务台账。"""

    def __init__(self, campaign_id: str, seed_cells: list[str], caliber: str, created_at: float, journal: JsonlJournal) -> None:
        if not seed_cells:
            raise TraceError("战役至少需要一个异常单体", component="trace")
        if caliber not in CALIBERS:
            raise TraceError(f"未知范围口径: {caliber}", component="trace")
        self._campaign_id = campaign_id
        self._seed_cells = list(seed_cells)
        self._caliber = caliber
        self._created_at = float(created_at)
        self._journal = journal
        self._tasks: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._journal.append(
            {
                "type": CAMPAIGN_OPENED,
                "campaign_id": campaign_id,
                "seed_cells": list(seed_cells),
                "caliber": caliber,
                "ts": self._created_at,
            }
        )

    @property
    def campaign_id(self) -> str:
        return self._campaign_id

    @property
    def seed_cells(self) -> list[str]:
        return list(self._seed_cells)

    @property
    def caliber(self) -> str:
        return self._caliber

    def state_of(self, rack_id: str) -> str | None:
        """某簇当前任务状态，不在战役里返回 ``None``。"""

        with self._lock:
            task = self._tasks.get(rack_id)
            return task["state"] if task else None

    def tasks(self) -> dict:
        """全部任务状态（按簇排序的副本）。"""

        with self._lock:
            return {rack_id: dict(task) for rack_id, task in sorted(self._tasks.items())}

    def pending_or_failed(self) -> list[str]:
        """待下落的簇：还没执行的与失败待续作的。"""

        with self._lock:
            return sorted(rack_id for rack_id, task in self._tasks.items() if task["state"] in ("pending", "failed"))

    def is_complete(self) -> bool:
        """战役是否执行完：范围内每簇都已隔离。"""

        with self._lock:
            return bool(self._tasks) and all(task["state"] == "isolated" for task in self._tasks.values())

    def merge_scope(self, rack_ids: list[str], caliber: str, now: float) -> dict:
        """把重算的范围并进来：只加新簇，已有任务状态一律保留。"""

        if caliber not in CALIBERS:
            raise TraceError(f"未知范围口径: {caliber}", component="trace")
        with self._lock:
            self._caliber = caliber
            added = [rack_id for rack_id in rack_ids if rack_id not in self._tasks]
            for rack_id in added:
                self._tasks[rack_id] = {"state": "pending", "error": "", "updated_at": float(now)}
            self._journal.append(
                {
                    "type": SCOPE_MERGED,
                    "campaign_id": self._campaign_id,
                    "added": list(added),
                    "caliber": caliber,
                    "ts": float(now),
                }
            )
            return {"added": added, "targets": sorted(self._tasks)}

    def _transition(self, rack_id: str, expected: tuple[str, ...], state: str, now: float, error: str = "") -> dict:
        with self._lock:
            task = self._tasks.get(rack_id)
            if task is None:
                raise TraceError(f"簇 {rack_id} 不在战役 {self._campaign_id} 的隔离范围", component="trace")
            if task["state"] not in expected:
                raise TraceError(
                    f"簇 {rack_id} 状态为 {task['state']}，不能迁到 {state}", component="trace"
                )
            task["state"] = state
            task["error"] = error
            task["updated_at"] = float(now)
            self._journal.append(
                {
                    "type": TASK_STATE,
                    "campaign_id": self._campaign_id,
                    "rack_id": rack_id,
                    "state": state,
                    "error": error,
                    "ts": float(now),
                }
            )
            return dict(task)

    def mark_running(self, rack_id: str, now: float) -> dict:
        """任务开始下落；失败任务的续作也经由此迁移。"""

        return self._transition(rack_id, ("pending", "failed"), "running", now)

    def mark_isolated(self, rack_id: str, now: float) -> dict:
        """隔离确认落停。"""

        return self._transition(rack_id, ("running",), "isolated", now)

    def mark_failed(self, rack_id: str, error: str, now: float) -> dict:
        """下落失败，记下原因等续作。"""

        return self._transition(rack_id, ("running",), "failed", now, error=error)

    def snapshot(self) -> dict:
        """战役跟踪视图：逐簇状态、计数与是否执行完。"""

        with self._lock:
            tasks = {rack_id: dict(task) for rack_id, task in sorted(self._tasks.items())}
            counts = {state: 0 for state in TASK_STATES}
            for task in tasks.values():
                counts[task["state"]] += 1
            return {
                "campaign_id": self._campaign_id,
                "seed_cells": list(self._seed_cells),
                "caliber": self._caliber,
                "created_at": self._created_at,
                "tasks": tasks,
                "counts": counts,
                "complete": self.is_complete(),
            }

    @classmethod
    def restore_all(cls, journal: JsonlJournal) -> dict[str, "IsolationCampaign"]:
        """按隔离流水折叠重建全部战役，供重启后续作。"""

        campaigns: dict[str, IsolationCampaign] = {}
        for record in journal.read_all():
            kind = record["type"]
            if kind == CAMPAIGN_OPENED:
                campaign = cls.__new__(cls)
                campaign._campaign_id = record["campaign_id"]
                campaign._seed_cells = list(record["seed_cells"])
                campaign._caliber = record["caliber"]
                campaign._created_at = record["ts"]
                campaign._journal = journal
                campaign._tasks = {}
                campaign._lock = threading.RLock()
                campaigns[record["campaign_id"]] = campaign
            elif kind == SCOPE_MERGED:
                campaign = campaigns[record["campaign_id"]]
                campaign._caliber = record["caliber"]
                for rack_id in record["added"]:
                    campaign._tasks.setdefault(
                        rack_id, {"state": "pending", "error": "", "updated_at": record["ts"]}
                    )
            elif kind == TASK_STATE:
                campaign = campaigns[record["campaign_id"]]
                campaign._tasks[record["rack_id"]] = {
                    "state": record["state"],
                    "error": record.get("error", ""),
                    "updated_at": record["ts"],
                }
        return campaigns
