"""单体谱系台账。

台账只追加不改写：每颗单体先登记出厂批次，再按时间记录它进入、离开哪一簇。
换包、检修调拨都会产生跨簇移动，"这只异常单体进过哪些簇、属于哪个批次"只能
靠这份流水回答，靠人工翻巡检记录既慢又容易漏。流水落盘为 JSONL 段，重启后
按原顺序回放重建；所有查询都接受 ``as_of`` 时间点，能还原"当时知道的谱系"，
这是按时间点回放追溯口径的基础。

一致性约束在写入侧拦住：批次是出厂属性，登记后不能改写；同一时刻一颗单体
只能待在一簇里，跨簇移动必须先离簇再进簇。放过任何一条，后面算出来的隔离
范围就是错的。
"""

from __future__ import annotations

import threading

from ..errors import TraceError
from ..store.journal import JsonlJournal

CELL_REGISTERED = "cell_registered"
CELL_INSTALLED = "cell_installed"
CELL_REMOVED = "cell_removed"


def _fold(records: list[dict]) -> tuple[dict[str, str], dict[str, dict], list[dict]]:
    """把一段记录折叠成批次表、在架表与全部装机区间。"""

    batches: dict[str, str] = {}
    open_stays: dict[str, dict] = {}
    history: list[dict] = []
    for record in records:
        kind = record["type"]
        if kind == CELL_REGISTERED:
            batches[record["cell_id"]] = record["batch_id"]
        elif kind == CELL_INSTALLED:
            stay = {
                "cell_id": record["cell_id"],
                "rack_id": record["rack_id"],
                "installed_at": record["ts"],
                "removed_at": None,
            }
            open_stays[record["cell_id"]] = stay
            history.append(stay)
        elif kind == CELL_REMOVED:
            stay = open_stays.pop(record["cell_id"], None)
            if stay is not None:
                stay["removed_at"] = record["ts"]
    return batches, open_stays, history


class CellLedger:
    """只追加的单体批次与跨簇移动台账。"""

    def __init__(self, journal: JsonlJournal) -> None:
        self._journal = journal
        self._records: list[dict] = []
        self._batches: dict[str, str] = {}
        self._open: dict[str, str] = {}
        self._lock = threading.RLock()
        self._restore()

    def _restore(self) -> None:
        """回放落盘流水，重建当前批次表与在架表。"""

        for record in self._journal.read_all():
            self._records.append(record)
            if record["type"] == CELL_REGISTERED:
                self._batches[record["cell_id"]] = record["batch_id"]
            elif record["type"] == CELL_INSTALLED:
                self._open[record["cell_id"]] = record["rack_id"]
            elif record["type"] == CELL_REMOVED:
                self._open.pop(record["cell_id"], None)

    def _append(self, record: dict) -> dict:
        self._journal.append(record)
        self._records.append(record)
        return dict(record)

    def register_cell(self, cell_id: str, batch_id: str, now: float) -> dict:
        """登记一颗单体的出厂批次；同批次重复登记按幂等放过（重启补种）。"""

        if not cell_id or not batch_id:
            raise TraceError("单体与批次都不能为空", component="trace")
        with self._lock:
            known = self._batches.get(cell_id)
            if known is not None:
                if known != batch_id:
                    raise TraceError(f"单体 {cell_id} 已属批次 {known}，批次不能改写", component="trace")
                return {"type": CELL_REGISTERED, "cell_id": cell_id, "batch_id": known, "ts": None}
            self._batches[cell_id] = batch_id
            return self._append(
                {"type": CELL_REGISTERED, "cell_id": cell_id, "batch_id": batch_id, "ts": float(now)}
            )

    def install(self, cell_id: str, rack_id: str, now: float) -> dict:
        """记录一颗单体进入某簇；已在该簇的重复进簇按幂等放过。"""

        with self._lock:
            if cell_id not in self._batches:
                raise TraceError(f"单体 {cell_id} 未登记批次，不能进簇", component="trace")
            current = self._open.get(cell_id)
            if current is not None:
                if current == rack_id:
                    return {"type": CELL_INSTALLED, "cell_id": cell_id, "rack_id": rack_id, "ts": None}
                raise TraceError(f"单体 {cell_id} 仍在 {current}，跨簇移动必须先离簇", component="trace")
            self._open[cell_id] = rack_id
            return self._append({"type": CELL_INSTALLED, "cell_id": cell_id, "rack_id": rack_id, "ts": float(now)})

    def remove(self, cell_id: str, rack_id: str, now: float) -> dict:
        """记录一颗单体离开某簇。"""

        with self._lock:
            current = self._open.get(cell_id)
            if current is None or current != rack_id:
                raise TraceError(f"单体 {cell_id} 不在 {rack_id} 在架，离簇记录不成立", component="trace")
            del self._open[cell_id]
            return self._append({"type": CELL_REMOVED, "cell_id": cell_id, "rack_id": rack_id, "ts": float(now)})

    def known(self, cell_id: str, as_of: float | None = None) -> bool:
        """单体是否已登记批次；``as_of`` 用于回答"当时是否已登记"。"""

        with self._lock:
            if as_of is None:
                return cell_id in self._batches
            batches, _open, _history = _fold([r for r in self._records if r["ts"] <= as_of])
            return cell_id in batches

    def batch_of(self, cell_id: str, as_of: float | None = None) -> str:
        """单体所属批次；未知单体报错，因为静默放过会让隔离范围漏圈。"""

        with self._lock:
            if as_of is None:
                batch_id = self._batches.get(cell_id)
            else:
                batches, _open, _history = _fold([r for r in self._records if r["ts"] <= as_of])
                batch_id = batches.get(cell_id)
        if batch_id is None:
            raise TraceError(f"未知单体: {cell_id}", component="trace")
        return batch_id

    def cells_of_batch(self, batch_id: str, as_of: float | None = None) -> list[str]:
        """某批次登记过的全部单体。"""

        with self._lock:
            if as_of is None:
                batches = dict(self._batches)
            else:
                batches, _open, _history = _fold([r for r in self._records if r["ts"] <= as_of])
        return sorted(cell_id for cell_id, batch in batches.items() if batch == batch_id)

    def installed_rack(self, cell_id: str) -> str | None:
        """单体当前所在簇，不在架时返回 ``None``。"""

        with self._lock:
            return self._open.get(cell_id)

    def residencies(self, cell_id: str | None = None, as_of: float | None = None) -> list[dict]:
        """装机区间清单（含已离簇的），可按单体与时间点过滤。"""

        with self._lock:
            records = self._records if as_of is None else [r for r in self._records if r["ts"] <= as_of]
            _batches, _open, history = _fold(list(records))
        stays = [dict(stay) for stay in history if cell_id is None or stay["cell_id"] == cell_id]
        return sorted(stays, key=lambda stay: (stay["installed_at"], stay["cell_id"]))

    def racks_of_cell(self, cell_id: str, as_of: float | None = None) -> list[str]:
        """单体进过的全部簇。"""

        return sorted({stay["rack_id"] for stay in self.residencies(cell_id, as_of=as_of)})

    def summary(self) -> dict:
        """台账计数，供快照与控制台核对。"""

        with self._lock:
            batches = set(self._batches.values())
            residencies = len(self.residencies())
            return {
                "cells": len(self._batches),
                "batches": len(batches),
                "batch_ids": sorted(batches),
                "residencies": residencies,
                "installed": len(self._open),
                "journal_count": self._journal.count(),
            }
