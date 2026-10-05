"""反查与两种隔离口径的范围计算。"""

from __future__ import annotations

import pytest

from batterystore.lineage.events import LineageEvent
from batterystore.lineage.scope import (
    POLICY_BATCH_WIDE,
    POLICY_OVERLAP_WINDOW,
    compute_scope,
    scope_fingerprint,
)
from batterystore.lineage.trace import trace_cell


def _events() -> list[LineageEvent]:
    raw = [
        (0, "cell_batch", 1000.0, {"cell_id": "c1", "batch_id": "B1"}),
        (1, "cell_batch", 1000.0, {"cell_id": "c2", "batch_id": "B1"}),
        (2, "cell_batch", 1000.0, {"cell_id": "c3", "batch_id": "B2"}),
        (3, "cell_batch", 1000.0, {"cell_id": "c9", "batch_id": "B3"}),
        (4, "batch_relation", 1000.0, {"batch_id": "B1", "related_batch_id": "B2"}),
        (5, "install", 1000.0, {"cell_id": "c1", "rack_id": "rk-01"}),
        (6, "remove", 2000.0, {"cell_id": "c1", "rack_id": "rk-01"}),
        (7, "install", 3000.0, {"cell_id": "c1", "rack_id": "rk-02"}),
        (8, "install", 1500.0, {"cell_id": "c2", "rack_id": "rk-01"}),
        (9, "install", 4000.0, {"cell_id": "c3", "rack_id": "rk-01"}),
        (10, "install", 3500.0, {"cell_id": "c3", "rack_id": "rk-02"}),
        (11, "install", 1000.0, {"cell_id": "c9", "rack_id": "rk-01"}),
    ]
    return [LineageEvent(seq=seq, kind=kind, ts=ts, **fields) for seq, kind, ts, fields in raw]


def test_trace_finds_batches_relations_and_racks():
    trace = trace_cell(_events(), "c1")
    assert trace["found"] is True
    assert trace["seed_batch_id"] == "B1"
    assert trace["batch_ids"] == ["B1", "B2"]
    assert trace["racks_visited"] == ["rk-01", "rk-02"]
    assert len(trace["batch_relations"]) == 1
    seed_path = next(item for item in trace["cell_paths"] if item["is_seed"])
    assert [dwell["rack_id"] for dwell in seed_path["dwell"]] == ["rk-01", "rk-02"]


def test_trace_unknown_cell_is_empty():
    trace = trace_cell(_events(), "nope")
    assert trace["found"] is False
    assert trace["batch_ids"] == []
    assert trace["racks_visited"] == []


def test_trace_as_of_replays_history_only():
    past = trace_cell(_events(), "c1", as_of=1900.0)
    paths = {item["cell_id"]: item["dwell"] for item in past["cell_paths"]}
    assert [dwell["rack_id"] for dwell in paths["c1"]] == ["rk-01"]
    # c3 此时虽然已归属，但还没装进任何簇
    assert paths["c3"] == []


def test_batch_wide_scope_covers_every_related_batch_rack():
    scope = compute_scope(_events(), "c1", policy=POLICY_BATCH_WIDE)
    payload = scope.as_dict()
    assert payload["rack_ids"] == ["rk-01", "rk-02"]
    assert payload["frozen_batches"] == ["B1", "B2"]
    assert payload["target_count"] == 4
    assert "B3" not in payload["batch_ids"]
    assert scope.rationale


def test_overlap_scope_excludes_non_overlapping_witness():
    # c1 在 rk-01 的窗口 [1000,2000)；c3 4000 才进 rk-01，不重叠 -> rk-01
    # 仍由 c2(1500 装入) 圈入，但证据见证单体不能出现 c3。
    scope = compute_scope(_events(), "c1", policy=POLICY_OVERLAP_WINDOW, mechanism="共簇耦合")
    payload = scope.as_dict()
    rack_evidence = {target.target_id: target.evidence for target in scope.targets if target.kind == "rack"}
    assert "c3" not in rack_evidence["rk-01"]["witnesses"]
    assert "c2" in rack_evidence["rk-01"]["witnesses"]
    # rk-02：c1[3000,∞) 与 c3[3500,∞) 重叠
    assert "c3" in rack_evidence["rk-02"]["witnesses"]
    # 批次冻结不受窗口影响，相关批次仍全部停用
    assert payload["frozen_batches"] == ["B1", "B2"]


def test_overlap_scope_converges_when_seed_never_shared_rack():
    raw = [
        LineageEvent(seq=0, kind="cell_batch", ts=1000.0, cell_id="x1", batch_id="B1"),
        LineageEvent(seq=1, kind="cell_batch", ts=1000.0, cell_id="x2", batch_id="B1"),
        LineageEvent(seq=2, kind="install", ts=1000.0, cell_id="x1", rack_id="rk-01"),
        LineageEvent(seq=3, kind="remove", ts=2000.0, cell_id="x1", rack_id="rk-01"),
        LineageEvent(seq=4, kind="install", ts=3000.0, cell_id="x2", rack_id="rk-02"),
    ]
    wide = compute_scope(raw, "x1", policy=POLICY_BATCH_WIDE)
    narrow = compute_scope(raw, "x1", policy=POLICY_OVERLAP_WINDOW, mechanism="m")
    assert wide.as_dict()["rack_ids"] == ["rk-01", "rk-02"]
    assert narrow.as_dict()["rack_ids"] == ["rk-01"]
    # padding 外扩也无法跨越 1000s 的鸿沟
    padded = compute_scope(raw, "x1", policy=POLICY_OVERLAP_WINDOW, window_padding_s=100.0, mechanism="m")
    assert padded.as_dict()["rack_ids"] == ["rk-01"]


def test_overlap_scope_requires_mechanism():
    with pytest.raises(ValueError):
        compute_scope(_events(), "c1", policy=POLICY_OVERLAP_WINDOW)


def test_fingerprint_is_deterministic_but_distinguishes_policies():
    wide = compute_scope(_events(), "c1", policy=POLICY_BATCH_WIDE)
    wide_again = compute_scope(_events(), "c1", policy=POLICY_BATCH_WIDE)
    narrow = compute_scope(_events(), "c1", policy=POLICY_OVERLAP_WINDOW, mechanism="m")
    assert scope_fingerprint(wide) == scope_fingerprint(wide_again)
    assert scope_fingerprint(wide) != scope_fingerprint(narrow)
