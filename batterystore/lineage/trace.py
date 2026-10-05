"""从一只异常单体反查批次与簇轨迹。

反查只读 :class:`~batterystore.lineage.index.LineageIndex` 的时点视图，输出
三段证据：种子单体归属、批次关联闭包（连带批次及关联依据）、每只相关单体
在哪些簇驻留过。范围口径的取舍不在这里做，这里只把证据链摊开，保证人工
翻巡检记录时依赖的每一条事实在回放里都能对上号。
"""

from __future__ import annotations

from .events import LineageEvent
from .index import build_index


def trace_cell(
    events: list[LineageEvent],
    seed_cell_id: str,
    *,
    as_of: float | None = None,
) -> dict:
    """反查一只单体进过哪些簇、属于哪些批次（可按时间点回放）。

    ``as_of`` 缺省取事件里最晚的业务时间，等价于"按现在的认知回放"；传入
    历史时间点则只重放该时点之前已经发生的事件，补录事件不影响历史结论。
    """

    cutoff = max((event.ts for event in events), default=0.0) if as_of is None else float(as_of)
    index = build_index(events, cutoff)
    seed_batch = index.cell_batch.get(seed_cell_id, "")
    related = sorted(index.related_batches(seed_batch)) if seed_batch else []

    member_cells: dict[str, list[str]] = {batch: index.cells_of_batch(batch) for batch in related}
    racks: set[str] = set()
    cell_paths: list[dict] = []
    for batch in related:
        for cell_id in member_cells[batch]:
            dwell = index.dwell_of(cell_id)
            racks.update(interval.rack_id for interval in dwell)
            cell_paths.append(
                {
                    "cell_id": cell_id,
                    "batch_id": batch,
                    "is_seed": cell_id == seed_cell_id,
                    "dwell": [interval.as_dict() for interval in dwell],
                }
            )
    cell_paths.sort(key=lambda item: (not item["is_seed"], item["batch_id"], item["cell_id"]))

    relations = [
        event.as_dict()
        for event in sorted(
            (
                event
                for event in events
                if event.kind == "batch_relation"
                and event.ts <= cutoff
                and (event.batch_id in related or event.related_batch_id in related)
            ),
            key=lambda event: (event.ts, event.seq),
        )
    ]
    return {
        "seed_cell_id": seed_cell_id,
        "as_of": round(cutoff, 3),
        "found": bool(seed_batch),
        "seed_batch_id": seed_batch,
        "batch_ids": related,
        "batch_relations": relations,
        "racks_visited": sorted(racks),
        "cell_paths": cell_paths,
        "index": index.as_dict(),
    }
