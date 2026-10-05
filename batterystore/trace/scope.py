"""隔离范围口径。

两种口径共用同一份谱系台账，算出来的都只是"建议范围"——已经隔离的簇不会
因为重算口径被撤回（单调合并见 :mod:`batterystore.trace.campaign`）。

``batch``（保守，默认）
    异常单体所属批次的全部单体，凡是进过的簇一律圈入。批次性问题的失效
    机理是潜伏的，共址记录一旦有缺口，漏圈的代价是热失控，多圈的代价
    只是停产面——两种损失不对称，所以默认取保守口径。

``window``（收敛）
    只圈暴露窗口内真正装有涉事批次单体的簇：暴露窗口取异常单体首次进簇
    到检出时刻，簇内批次单体的装机区间与窗口有重叠才圈入。批次单体在
    窗口打开前就已撤出的簇不圈，停产面收敛到真正重叠的部分。代价是
    口径依赖台账完整性：移动记录漏登一条，收敛就多砍一刀。
"""

from __future__ import annotations

from ..errors import TraceError

CALIBERS = ("batch", "window")

REASONS = {
    "batch": "涉事批次单体进驻过该簇",
    "window": "涉事批次单体在暴露窗口内装机",
}


def trace_cell(ledger, cell_id: str, as_of: float | None = None) -> dict:
    """从一只单体反查它的批次与进过的簇，返回谱系报告。"""

    batch_id = ledger.batch_of(cell_id, as_of=as_of)
    residencies = ledger.residencies(cell_id, as_of=as_of)
    current = next((stay["rack_id"] for stay in residencies if stay["removed_at"] is None), None)
    return {
        "cell_id": cell_id,
        "batch_id": batch_id,
        "as_of": as_of,
        "racks": ledger.racks_of_cell(cell_id, as_of=as_of),
        "current_rack": current,
        "residencies": residencies,
    }


def exposure_window(ledger, seed_cells: list[str], end: float) -> dict:
    """暴露窗口：异常单体首次进簇到检出时刻。"""

    starts: list[float] = []
    for cell_id in seed_cells:
        stays = ledger.residencies(cell_id, as_of=end)
        if stays:
            starts.append(min(stay["installed_at"] for stay in stays))
    return {"start": min(starts) if starts else float(end), "end": float(end)}


def _overlaps(stay: dict, window: dict) -> bool:
    """装机区间与暴露窗口是否真正重叠。"""

    if stay["installed_at"] > window["end"]:
        return False
    return stay["removed_at"] is None or stay["removed_at"] >= window["start"]


def compute_scope(ledger, seed_cells: list[str], caliber: str, now: float, as_of: float | None = None) -> dict:
    """按口径算隔离建议范围；``as_of`` 用于按时间点回放当时的口径结果。"""

    if caliber not in CALIBERS:
        raise TraceError(f"未知范围口径: {caliber}（可选 {list(CALIBERS)}）", component="trace")
    seeds = list(dict.fromkeys(seed_cells))
    if not seeds:
        raise TraceError("异常单体清单不能为空", component="trace")
    end = float(as_of) if as_of is not None else float(now)
    batches = sorted({ledger.batch_of(cell_id, as_of=end) for cell_id in seeds})
    window = exposure_window(ledger, seeds, end)
    targets: dict[str, dict] = {}
    for batch_id in batches:
        for member in ledger.cells_of_batch(batch_id, as_of=end):
            for stay in ledger.residencies(member, as_of=end):
                if caliber == "window" and not _overlaps(stay, window):
                    continue
                slot = targets.setdefault(
                    stay["rack_id"], {"cells": set(), "batches": set(), "reason": REASONS[caliber]}
                )
                slot["cells"].add(member)
                slot["batches"].add(batch_id)
    normalized = {
        rack_id: {
            "cells": sorted(slot["cells"]),
            "batches": sorted(slot["batches"]),
            "reason": slot["reason"],
        }
        for rack_id, slot in sorted(targets.items())
    }
    return {
        "caliber": caliber,
        "seed_cells": seeds,
        "batches": batches,
        "exposure_window": window,
        "targets": normalized,
        "as_of": end,
        "computed_at": float(now),
    }
