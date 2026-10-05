"""隔离范围计算：两种口径。

同一份时点证据可以圈出两种范围，差别只在"簇要不要圈进来"的判据：

``batch_wide``（保守口径）
    种子批次沿关联图闭包得到的全部批次，凡是装过这些批次单体的簇一律隔离，
    同时把相关批次本身标记为"停用批次"。它对"批次性内在缺陷"是安全的——
    缺陷不依赖同簇共存条件，时间窗口收敛反而会漏掉从没与种子同簇、但同属
    问题批次的单体。代价是停产面大。

``overlap_window``（装机时间窗口口径）
    批次范围不变，但簇只圈"与种子单体真实同簇共存过"的：某簇装了同批次
    单体的驻留区间，必须与种子在该簇的驻留区间（可外扩一个判定窗口）有
    时间重叠才隔离。它适用于"异常只在同簇工况耦合下才成立"的机理假设，
    停产面小，但一旦缺陷其实是批次内在的，就会漏隔——所以该口径要求显式
    带机理依据，且结果必须能和保守口径对比着看。

范围结果带指纹：同事件集 + 同 ``as_of`` + 同口径 + 同窗口必然得到同一指纹，
隔离令用它判断"重算后范围有没有实质变化"。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .events import LineageEvent
from .index import LineageIndex, build_index

POLICY_BATCH_WIDE = "batch_wide"
POLICY_OVERLAP_WINDOW = "overlap_window"
POLICIES = (POLICY_BATCH_WIDE, POLICY_OVERLAP_WINDOW)

TARGET_RACK = "rack"
TARGET_BATCH = "batch"


@dataclass(frozen=True)
class ScopeTarget:
    """一个隔离对象：整簇（连带簇内全部单体）或停用批次。"""

    kind: str
    target_id: str
    reasons: tuple[str, ...]
    evidence: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.target_id}"

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "target_id": self.target_id,
            "reasons": list(self.reasons),
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class IsolationScope:
    """一次范围计算的完整结论（可回放、可对比）。"""

    seed_cell_id: str
    policy: str
    as_of: float
    window_padding_s: float
    rationale: str
    batch_ids: tuple[str, ...]
    targets: tuple[ScopeTarget, ...]
    event_count: int

    def keys(self) -> tuple[str, ...]:
        return tuple(target.key for target in self.targets)

    def as_dict(self) -> dict:
        return {
            "seed_cell_id": self.seed_cell_id,
            "policy": self.policy,
            "as_of": round(self.as_of, 3),
            "window_padding_s": round(self.window_padding_s, 3),
            "rationale": self.rationale,
            "batch_ids": list(self.batch_ids),
            "targets": [target.as_dict() for target in self.targets],
            "rack_ids": [target.target_id for target in self.targets if target.kind == TARGET_RACK],
            "frozen_batches": [target.target_id for target in self.targets if target.kind == TARGET_BATCH],
            "target_count": len(self.targets),
            "event_count": self.event_count,
            "fingerprint": scope_fingerprint(self),
        }


def _overlaps(start_a: float, end_a: float, start_b: float, end_b: float) -> bool:
    return start_a <= end_b and start_b <= end_a


def _rack_targets(index: LineageIndex, batches: list[str], seed: str, policy: str, padding: float) -> list[ScopeTarget]:
    seed_dwell = index.dwell_of(seed)
    seed_by_rack: dict[str, list] = {}
    for interval in seed_dwell:
        seed_by_rack.setdefault(interval.rack_id, []).append(interval)

    targets: dict[str, ScopeTarget] = {}
    for batch in batches:
        for cell_id in index.cells_of_batch(batch):
            for dwell in index.dwell_of(cell_id):
                rack_id = dwell.rack_id
                if policy == POLICY_BATCH_WIDE:
                    reasons = ("batch_member_installed",)
                    evidence = {
                        "cell_id": cell_id,
                        "batch_id": batch,
                        "dwell": dwell.as_dict(),
                    }
                else:
                    windows = seed_by_rack.get(rack_id, [])
                    ended = dwell.ended_ts if dwell.ended_ts is not None else index.as_of
                    hit = None
                    for seed_window in windows:
                        seed_ended = seed_window.ended_ts if seed_window.ended_ts is not None else index.as_of
                        if _overlaps(
                            dwell.started_ts - padding,
                            ended + padding,
                            seed_window.started_ts - padding,
                            seed_ended + padding,
                        ):
                            hit = seed_window
                            break
                    if hit is None:
                        continue
                    reasons = ("co_resident_window",)
                    evidence = {
                        "cell_id": cell_id,
                        "batch_id": batch,
                        "dwell": dwell.as_dict(),
                        "seed_window": hit.as_dict(),
                        "window_padding_s": round(padding, 3),
                    }
                previous = targets.get(rack_id)
                if previous is None:
                    targets[rack_id] = ScopeTarget(
                        TARGET_RACK,
                        rack_id,
                        reasons,
                        {"witnesses": [cell_id], "batches": [batch], "first_match": evidence},
                    )
                else:
                    merged_reasons = tuple(sorted(set(previous.reasons) | set(reasons)))
                    witnesses = sorted(set(previous.evidence.get("witnesses", [])) | {cell_id})
                    batches_seen = sorted(set(previous.evidence.get("batches", [])) | {batch})
                    targets[rack_id] = ScopeTarget(
                        TARGET_RACK,
                        rack_id,
                        merged_reasons,
                        {"witnesses": witnesses, "batches": batches_seen},
                    )
    return [targets[key] for key in sorted(targets)]


def compute_scope(
    events: list[LineageEvent],
    seed_cell_id: str,
    *,
    policy: str = POLICY_BATCH_WIDE,
    as_of: float | None = None,
    window_padding_s: float = 0.0,
    mechanism: str = "",
) -> IsolationScope:
    """按口径计算必须隔离的范围。

    ``window_padding_s`` 只对时间窗口口径生效，把驻留区间两端各外扩，覆盖
    "刚拆走／刚装入即构成暴露"的保守余量。时间窗口口径必须给 ``mechanism``
    说明机理依据，防止默认拿不安全口径直接执行。
    """

    if policy not in POLICIES:
        raise ValueError(f"未知隔离口径: {policy}")
    padding = max(0.0, float(window_padding_s))
    if policy == POLICY_OVERLAP_WINDOW and not mechanism.strip():
        raise ValueError("时间窗口口径必须给出机理依据 mechanism")

    cutoff = max((event.ts for event in events), default=0.0) if as_of is None else float(as_of)
    index = build_index(events, cutoff)
    seed_batch = index.cell_batch.get(seed_cell_id, "")
    batches = sorted(index.related_batches(seed_batch)) if seed_batch else []

    rack_targets = _rack_targets(index, batches, seed_cell_id, policy, padding)
    batch_targets = [
        ScopeTarget(
            TARGET_BATCH,
            batch,
            ("defective_batch_freeze",),
            {"seed": batch == seed_batch, "member_cells": len(index.cells_of_batch(batch))},
        )
        for batch in batches
    ]
    targets = tuple(sorted([*rack_targets, *batch_targets], key=lambda item: (item.kind, item.target_id)))

    if policy == POLICY_BATCH_WIDE:
        rationale = "批次性缺陷不依赖同簇共存：凡装过相关批次单体的簇全部隔离，相关批次停用"
    else:
        rationale = f"按装机时间窗口收敛到真实同簇共存，机理依据：{mechanism.strip()}"

    return IsolationScope(
        seed_cell_id=seed_cell_id,
        policy=policy,
        as_of=cutoff,
        window_padding_s=padding,
        rationale=rationale,
        batch_ids=tuple(batches),
        targets=targets,
        event_count=len([event for event in events if event.ts <= cutoff]),
    )


def scope_fingerprint(scope: IsolationScope) -> str:
    """范围指纹只取决于口径、时点、窗口与对象集合，与证据文字无关。"""

    joined = "|".join(
        [
            scope.policy,
            f"{scope.as_of:.3f}",
            f"{scope.window_padding_s:.3f}",
            ",".join(scope.keys()),
        ]
    )
    digest = 0
    for char in joined.encode("utf-8"):
        digest = (digest * 131 + char) & 0xFFFFFFFF
    return f"fp-{digest:08x}"
