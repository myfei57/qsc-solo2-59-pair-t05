"""单体谱系流水：只追加录入、时点回放与重启恢复。"""

from __future__ import annotations

import pytest

from batterystore.errors import StoreError
from batterystore.lineage.events import BATCH_RELATION, CELL_BATCH, INSTALL, REMOVE
from batterystore.lineage.index import build_index
from batterystore.lineage.ledger import LineageLedger
from batterystore.store.journal import JsonlJournal


def _ledger(tmp_path) -> LineageLedger:
    return LineageLedger(JsonlJournal(str(tmp_path), "lineage", segment_limit=64))


def _seed(ledger: LineageLedger) -> None:
    ledger.record_cell_batch("c1", "B1", 1000.0)
    ledger.record_cell_batch("c2", "B2", 1000.0)
    ledger.record_batch_relation("B1", "B2", 1000.0, relation="same_furnace")
    ledger.record_install("c1", "rk-01", 1100.0)
    ledger.record_install("c2", "rk-02", 1200.0)
    ledger.record_remove("c1", "rk-01", 1800.0)
    ledger.record_install("c1", "rk-02", 2000.0)


def test_events_append_and_replay(tmp_path):
    ledger = _ledger(tmp_path)
    _seed(ledger)
    assert ledger.count() == 7

    full = ledger.replay()
    assert [event["kind"] for event in full[:2]] == [CELL_BATCH, CELL_BATCH]
    assert full[-1]["kind"] == INSTALL

    early = ledger.replay(as_of=1500.0)
    kinds = {(event["kind"], event.get("cell_id", "")) for event in early}
    assert (REMOVE, "c1") not in kinds
    assert (INSTALL, "c1") in kinds


def test_remove_requires_prior_install(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_cell_batch("c1", "B1", 1000.0)
    with pytest.raises(StoreError):
        ledger.record_remove("c1", "rk-09", 1100.0)


def test_install_requires_batch(tmp_path):
    ledger = _ledger(tmp_path)
    with pytest.raises(StoreError):
        ledger.record_install("ghost", "rk-01", 1100.0)


def test_batch_relation_rejects_self_and_blank(tmp_path):
    ledger = _ledger(tmp_path)
    with pytest.raises(StoreError):
        ledger.record_batch_relation("B1", "B1", 1000.0)
    with pytest.raises(StoreError):
        ledger.record_batch_relation("", "B2", 1000.0)


def test_index_dwell_intervals_and_closure(tmp_path):
    ledger = _ledger(tmp_path)
    _seed(ledger)
    index = build_index(ledger.events, as_of=5000.0)
    dwell = {(interval.rack_id, interval.ended_ts) for interval in index.dwell_of("c1")}
    assert ("rk-01", 1800.0) in dwell
    assert ("rk-02", None) in dwell
    assert sorted(interval.started_ts for interval in index.dwell_of("c1")) == [1100.0, 2000.0]
    assert index.related_batches("B1") == {"B1", "B2"}
    assert index.related_batches("B9") == set()

    past = build_index(ledger.events, as_of=1900.0)
    past_dwell = past.dwell_of("c1")
    assert len(past_dwell) == 1 and past_dwell[0].rack_id == "rk-01"


def test_ledger_rebuilds_from_journal(tmp_path):
    ledger = _ledger(tmp_path)
    _seed(ledger)
    rebuilt = LineageLedger(JsonlJournal(str(tmp_path), "lineage", segment_limit=64))
    assert rebuilt.count() == 7
    assert [event.kind for event in rebuilt.events] == [
        CELL_BATCH,
        CELL_BATCH,
        BATCH_RELATION,
        INSTALL,
        INSTALL,
        REMOVE,
        INSTALL,
    ]
