"""批次追溯与隔离处置入口。

``TraceService`` 把谱系台账、范围口径与隔离战役串起来：登记单体批次与跨簇
移动、从异常单体反查谱系、按口径算隔离范围、开立战役并跟踪执行。台账与
战役各用一条 JSONL 流水落盘，重启即回放，追溯口径与执行进度都不因重启
断档。
"""

from __future__ import annotations

import threading

from ..clock import Clock
from ..errors import TraceError
from ..ids import IdFactory
from ..store.journal import JsonlJournal
from .campaign import IsolationCampaign
from .ledger import CellLedger
from .scope import compute_scope, trace_cell


class TraceService:
    """谱系台账 + 隔离战役的处置门面。"""

    def __init__(self, root: str, clock: Clock) -> None:
        self._clock = clock
        self._ledger = CellLedger(JsonlJournal(root, "trace"))
        self._journal = JsonlJournal(root, "isolation")
        self._campaigns = IsolationCampaign.restore_all(self._journal)
        self._ids = IdFactory("iso")
        self._lock = threading.RLock()

    def register_cell(self, cell_id: str, batch_id: str) -> dict:
        """登记一颗单体的出厂批次。"""

        with self._lock:
            return self._ledger.register_cell(cell_id, batch_id, self._clock.now())

    def install_cell(self, cell_id: str, rack_id: str) -> dict:
        """记录一颗单体进入某簇。"""

        with self._lock:
            return self._ledger.install(cell_id, rack_id, self._clock.now())

    def remove_cell(self, cell_id: str, rack_id: str) -> dict:
        """记录一颗单体离开某簇。"""

        with self._lock:
            return self._ledger.remove(cell_id, rack_id, self._clock.now())

    def move_cell(self, cell_id: str, to_rack_id: str, batch_id: str | None = None) -> dict:
        """登记一次跨簇调拨：先离原簇再进目标簇，新单体可同时补登批次。"""

        with self._lock:
            now = self._clock.now()
            if batch_id is not None and not self._ledger.known(cell_id):
                self._ledger.register_cell(cell_id, batch_id, now)
            from_rack = self._ledger.installed_rack(cell_id)
            if from_rack == to_rack_id:
                raise TraceError(f"单体 {cell_id} 已在 {to_rack_id}，调拨不成立", component="trace")
            if from_rack is not None:
                self._ledger.remove(cell_id, from_rack, now)
            self._ledger.install(cell_id, to_rack_id, now)
            return {
                "cell_id": cell_id,
                "from_rack": from_rack,
                "to_rack": to_rack_id,
                "batch_id": self._ledger.batch_of(cell_id),
                "moved_at": now,
            }

    def trace(self, cell_id: str, as_of: float | None = None) -> dict:
        """从一只单体反查批次与进过的簇。"""

        with self._lock:
            return trace_cell(self._ledger, cell_id, as_of=as_of)

    def campaign(self, campaign_id: str) -> IsolationCampaign:
        """按标识取战役，未知标识报错。"""

        with self._lock:
            found = self._campaigns.get(campaign_id)
        if found is None:
            raise TraceError(f"未知隔离战役: {campaign_id}", component="trace")
        return found

    def open_campaign(self, seed_cells: list[str], caliber: str) -> dict:
        """算口径范围并开立战役；范围为空说明台账缺记录，直接报错。"""

        with self._lock:
            now = self._clock.now()
            report = compute_scope(self._ledger, seed_cells, caliber, now)
            if not report["targets"]:
                raise TraceError("台账中没有涉事批次的装机记录，圈定范围为空", component="trace")
            campaign = IsolationCampaign(self._ids.next(), report["seed_cells"], caliber, now, self._journal)
            self._campaigns[campaign.campaign_id] = campaign
            merged = campaign.merge_scope(sorted(report["targets"]), caliber, now)
            return {"campaign": campaign.snapshot(), "scope": report, "merged": merged}

    def recalculate(self, campaign_id: str, caliber: str | None = None) -> dict:
        """按当前台账重算范围并单调合并：只加不撤。"""

        with self._lock:
            now = self._clock.now()
            campaign = self.campaign(campaign_id)
            use_caliber = caliber or campaign.caliber
            report = compute_scope(self._ledger, campaign.seed_cells, use_caliber, now)
            merged = campaign.merge_scope(sorted(report["targets"]), use_caliber, now)
            return {"scope": report, "merged": merged, "campaign": campaign.snapshot()}

    def replay(self, campaign_id: str, as_of: float) -> dict:
        """按时间点回放：当时的台账会圈出哪些簇，与现在的范围差多少。

        回放时刻异常单体还没登记是合法答案（说明当时根本追不到它），
        按空范围返回并标注，不当错误抛给控制台。
        """

        with self._lock:
            campaign = self.campaign(campaign_id)
            known_seeds = [cell_id for cell_id in campaign.seed_cells if self._ledger.known(cell_id, as_of=as_of)]
            unknown_seeds = sorted(set(campaign.seed_cells) - set(known_seeds))
            if known_seeds:
                report = compute_scope(self._ledger, known_seeds, campaign.caliber, as_of, as_of=as_of)
            else:
                report = {
                    "caliber": campaign.caliber,
                    "seed_cells": [],
                    "batches": [],
                    "exposure_window": {"start": float(as_of), "end": float(as_of)},
                    "targets": {},
                    "as_of": float(as_of),
                    "computed_at": float(as_of),
                }
            current = sorted(campaign.tasks())
            then = sorted(report["targets"])
            return {
                "campaign_id": campaign_id,
                "as_of": float(as_of),
                "caliber": campaign.caliber,
                "scope": report,
                "unknown_seeds": unknown_seeds,
                "then_targets": then,
                "current_targets": current,
                "added_since": sorted(set(current) - set(then)),
            }

    def snapshot(self) -> dict:
        """追溯与处置总览。"""

        with self._lock:
            campaigns = [self._campaigns[key].snapshot() for key in sorted(self._campaigns)]
        return {
            "ledger": self._ledger.summary(),
            "campaigns": campaigns,
            "isolation_journal_count": self._journal.count(),
        }
