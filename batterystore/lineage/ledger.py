"""谱系流水账本：事件只追加落盘，重放得到任意时点的真相。

账本复用平台的 :class:`~batterystore.store.journal.JsonlJournal` 分段 JSONL，
与审计流水同构：录入即 ``fsync`` 落盘，进程重启后按段读回。录入按业务时间
校验基本顺序约束（拆除必须先装机、重复归属以最新记录为准），但不拒绝事后
补录——补录正是追溯回放要处理的常态。
"""

from __future__ import annotations

import threading

from ..errors import StoreError
from ..store.journal import JsonlJournal
from .events import BATCH_RELATION, CELL_BATCH, EVENT_KINDS, INSTALL, REMOVE, LineageEvent
from .index import LineageIndex, build_index


class LineageLedger:
    """只追加的单体谱系流水。"""

    def __init__(self, journal: JsonlJournal) -> None:
        self._journal = journal
        self._events: list[LineageEvent] = []
        self._lock = threading.RLock()
        for raw in journal.read_all():
            self._events.append(LineageEvent.from_dict(raw))

    @property
    def events(self) -> list[LineageEvent]:
        """返回全部已录入事件（按录入顺序）。"""

        with self._lock:
            return list(self._events)

    def count(self) -> int:
        """返回已录入事件条数。"""

        with self._lock:
            return len(self._events)

    def _append(self, kind: str, ts: float, recorded_ts: float, **fields: str) -> LineageEvent:
        if kind not in EVENT_KINDS:
            raise StoreError(f"未知谱系事件类型: {kind}", component="trace")
        event = LineageEvent(
            seq=len(self._events),
            kind=kind,
            ts=float(ts),
            recorded_ts=float(recorded_ts),
            **fields,
        )
        self._journal.append(event.as_dict())
        self._events.append(event)
        return event

    def record_cell_batch(self, cell_id: str, batch_id: str, ts: float, *, source: str = "") -> dict:
        """登记单体归属批次（同一单体可改登记，以业务时间最新一条为准）。"""

        if not cell_id or not batch_id:
            raise StoreError("单体标识与批次标识都不能为空", component="trace")
        with self._lock:
            return self._append(
                CELL_BATCH, ts, ts, cell_id=cell_id, batch_id=batch_id, source=source
            ).as_dict()

    def record_batch_relation(
        self, batch_id: str, related_batch_id: str, ts: float, *, relation: str = "related", source: str = ""
    ) -> dict:
        """登记批次关联，追溯时沿关联图双向闭包传递。"""

        if not batch_id or not related_batch_id:
            raise StoreError("关联两侧的批次标识都不能为空", component="trace")
        if batch_id == related_batch_id:
            raise StoreError("批次不能与自身建立关联", component="trace")
        with self._lock:
            return self._append(
                BATCH_RELATION,
                ts,
                ts,
                batch_id=batch_id,
                related_batch_id=related_batch_id,
                relation=relation,
                source=source,
            ).as_dict()

    def record_install(self, cell_id: str, rack_id: str, ts: float, *, batch_id: str = "", source: str = "") -> dict:
        """登记单体在某时刻装入某簇。"""

        if not cell_id or not rack_id:
            raise StoreError("单体标识与簇标识都不能为空", component="trace")
        with self._lock:
            owner = batch_id
            if not owner:
                owner = self._latest_batch(cell_id)
            if not owner:
                raise StoreError(f"装机前必须先登记单体 {cell_id} 的批次归属", component="trace")
            return self._append(
                INSTALL, ts, ts, cell_id=cell_id, rack_id=rack_id, batch_id=owner, source=source
            ).as_dict()

    def record_remove(self, cell_id: str, rack_id: str, ts: float, *, source: str = "") -> dict:
        """登记单体在某时刻从某簇拆除。"""

        if not cell_id or not rack_id:
            raise StoreError("单体标识与簇标识都不能为空", component="trace")
        with self._lock:
            index = build_index(self._events, max((event.ts for event in self._events), default=ts))
            if not any(
                interval.rack_id == rack_id and interval.ended_ts is None
                for interval in index.dwell_of(cell_id)
            ):
                raise StoreError(f"单体 {cell_id} 当前没有装在簇 {rack_id}，拆除事件无对应装机", component="trace")
            return self._append(REMOVE, ts, ts, cell_id=cell_id, rack_id=rack_id, source=source).as_dict()

    def _latest_batch(self, cell_id: str) -> str:
        owner = ""
        owner_ts = float("-inf")
        for event in self._events:
            if event.kind == CELL_BATCH and event.cell_id == cell_id and event.ts >= owner_ts:
                owner = event.batch_id
                owner_ts = event.ts
        return owner

    def index(self, as_of: float | None = None) -> LineageIndex:
        """按截止时点物化谱系视图，缺省取最晚业务时间。"""

        with self._lock:
            events = list(self._events)
        cutoff = max((event.ts for event in events), default=0.0) if as_of is None else float(as_of)
        return build_index(events, cutoff)

    def replay(self, *, as_of: float | None = None, kind: str | None = None) -> list[dict]:
        """按业务时间升序回放事件，可按时点与类型过滤。"""

        with self._lock:
            events = list(self._events)
        cutoff = max((event.ts for event in events), default=0.0) if as_of is None else float(as_of)
        selected = [
            event
            for event in events
            if event.ts <= cutoff and (kind is None or event.kind == kind)
        ]
        selected.sort(key=lambda event: (event.ts, event.seq))
        return [event.as_dict() for event in selected]
