"""谱系事件的时点物化物。

:class:`LineageIndex` 是一组事件在"某个截止时点之前"的纯函数视图：只保留
``ts <= as_of`` 的事件，按发生顺序重放，得到单体→批次、批次关联图、
单体在簇驻留区间三种关系。范围计算与反查都只读这个物化结果，因此同一个
``as_of`` 无论重算多少遍，结论完全一致——这是追溯可回放的落点。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .events import BATCH_RELATION, CELL_BATCH, INSTALL, REMOVE, LineageEvent


@dataclass(frozen=True)
class DwellInterval:
    """单体在某簇的一段驻留区间，``ended_ts`` 为 ``None`` 表示至今未拆除。"""

    rack_id: str
    started_ts: float
    ended_ts: float | None

    def as_dict(self) -> dict:
        return {
            "rack_id": self.rack_id,
            "started_ts": round(self.started_ts, 3),
            "ended_ts": round(self.ended_ts, 3) if self.ended_ts is not None else None,
        }


@dataclass
class LineageIndex:
    """事件按截止时点重放后的谱系视图。"""

    as_of: float
    cell_batch: dict[str, str] = field(default_factory=dict)
    batch_edges: dict[str, set[str]] = field(default_factory=dict)
    dwell: dict[str, list[DwellInterval]] = field(default_factory=dict)
    seen_batches: set[str] = field(default_factory=set)

    def related_batches(self, seed: str) -> set[str]:
        """沿批次关联双向图做闭包传递，返回与种子批次连通的全部批次。"""

        reached: set[str] = set()
        if seed not in self.seen_batches:
            return reached
        pending = [seed]
        while pending:
            current = pending.pop()
            if current in reached:
                continue
            reached.add(current)
            pending.extend(self.batch_edges.get(current, ()))
        return reached

    def cells_of_batch(self, batch_id: str) -> list[str]:
        """返回归属于某批次的全部单体。"""

        return sorted(cell_id for cell_id, owner in self.cell_batch.items() if owner == batch_id)

    def dwell_of(self, cell_id: str) -> list[DwellInterval]:
        """返回单体在各簇的驻留区间（按装机时间升序）。"""

        return list(self.dwell.get(cell_id, ()))

    def as_dict(self) -> dict:
        return {
            "as_of": round(self.as_of, 3),
            "cell_count": len(self.cell_batch),
            "batch_count": len(self.seen_batches),
        }


def build_index(events: list[LineageEvent], as_of: float) -> LineageIndex:
    """按业务时间顺序重放截止时点前的事件，物化成谱系视图。"""

    index = LineageIndex(as_of=as_of)
    for event in sorted((item for item in events if item.ts <= as_of), key=lambda item: (item.ts, item.seq)):
        if event.kind == CELL_BATCH:
            if event.cell_id and event.batch_id:
                index.cell_batch[event.cell_id] = event.batch_id
                index.seen_batches.add(event.batch_id)
        elif event.kind == BATCH_RELATION:
            if event.batch_id and event.related_batch_id:
                index.batch_edges.setdefault(event.batch_id, set()).add(event.related_batch_id)
                index.batch_edges.setdefault(event.related_batch_id, set()).add(event.batch_id)
                index.seen_batches.update((event.batch_id, event.related_batch_id))
        elif event.kind == INSTALL:
            if event.cell_id and event.rack_id:
                intervals = index.dwell.setdefault(event.cell_id, [])
                if intervals and intervals[-1].ended_ts is None:
                    # 重复装机视为补录同一动作，不重复开区间。
                    if intervals[-1].rack_id != event.rack_id:
                        intervals[-1] = DwellInterval(intervals[-1].rack_id, intervals[-1].started_ts, event.ts)
                        intervals.append(DwellInterval(event.rack_id, event.ts, None))
                else:
                    intervals.append(DwellInterval(event.rack_id, event.ts, None))
        elif event.kind == REMOVE:
            if event.cell_id and event.rack_id:
                intervals = index.dwell.setdefault(event.cell_id, [])
                for position in range(len(intervals) - 1, -1, -1):
                    interval = intervals[position]
                    if interval.rack_id == event.rack_id and interval.ended_ts is None:
                        intervals[position] = DwellInterval(interval.rack_id, interval.started_ts, event.ts)
                        break
    for intervals in index.dwell.values():
        intervals.sort(key=lambda interval: interval.started_ts)
    return index
