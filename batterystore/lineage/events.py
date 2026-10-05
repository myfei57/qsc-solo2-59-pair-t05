"""谱系事件定义。

追溯必须能按时间点回放，因此谱系只允许四种只追加事件，任何"更正"都通过
再追加一条事件表达，历史不可改写：

``cell_batch``    记录某只单体属于某个生产批次；
``batch_relation`` 记录批次间关联（同炉、同卷绕段、来料合并等），双向闭包传递；
``install``       记录某只单体在某个时间装入某簇；
``remove``        记录某只单体在某个时间从某簇拆除，缺省视为仍在簇内。

事件带两个时间：``ts`` 是业务发生时间（装机时刻），``recorded_ts`` 是录入
系统的时间。按 ``ts`` 过滤得到"某一历史时点看到的真相"，``recorded_ts`` 只
用于落盘审计，区分"当时发生"与"事后补录"。
"""

from __future__ import annotations

from dataclasses import dataclass

CELL_BATCH = "cell_batch"
BATCH_RELATION = "batch_relation"
INSTALL = "install"
REMOVE = "remove"

EVENT_KINDS = (CELL_BATCH, BATCH_RELATION, INSTALL, REMOVE)


@dataclass(frozen=True)
class LineageEvent:
    """一条只追加的谱系流水。"""

    seq: int
    kind: str
    ts: float
    cell_id: str = ""
    batch_id: str = ""
    rack_id: str = ""
    related_batch_id: str = ""
    relation: str = ""
    source: str = ""
    recorded_ts: float = 0.0

    @classmethod
    def from_dict(cls, raw: dict) -> "LineageEvent":
        """从落盘字段还原一条事件。"""

        return cls(
            seq=int(raw.get("seq", 0)),
            kind=str(raw.get("kind", "")),
            ts=float(raw.get("ts", 0.0)),
            cell_id=str(raw.get("cell_id", "")),
            batch_id=str(raw.get("batch_id", "")),
            rack_id=str(raw.get("rack_id", "")),
            related_batch_id=str(raw.get("related_batch_id", "")),
            relation=str(raw.get("relation", "")),
            source=str(raw.get("source", "")),
            recorded_ts=float(raw.get("recorded_ts", 0.0)),
        )

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "ts": round(self.ts, 3),
            "cell_id": self.cell_id,
            "batch_id": self.batch_id,
            "rack_id": self.rack_id,
            "related_batch_id": self.related_batch_id,
            "relation": self.relation,
            "source": self.source,
            "recorded_ts": round(self.recorded_ts, 3),
        }
