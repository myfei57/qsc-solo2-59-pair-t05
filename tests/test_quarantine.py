"""隔离令：并发执行、失败续作、重算单调合并、重启恢复与联锁。"""

from __future__ import annotations

import threading

import pytest

from batterystore.errors import InterlockError, StoreError


def _seed_lineage(platform) -> None:
    platform.record_cell_batch("c1", "B1", 1000.0)
    platform.record_cell_batch("c2", "B2", 1000.0)
    platform.record_batch_relation("B1", "B2", 1000.0, relation="same_furnace")
    platform.install_cell("c1", "rk-01", 1000.0)
    platform.install_cell("c2", "rk-02", 1200.0)


def test_open_order_isolates_all_targets(platform):
    _seed_lineage(platform)
    order = platform.open_quarantine("c1", policy="batch_wide")
    assert order["state"] == "completed"
    assert order["progress"] == {"total": 4, "isolated": 4, "failed": 0, "pending": 0, "complete": True}
    assert platform.isolation_board.isolated_racks() == ["rk-01", "rk-02"]
    assert platform.isolation_board.frozen_batches() == ["B1", "B2"]


def test_unknown_cell_cannot_open_order(platform):
    platform.record_cell_batch("c1", "B1", 1000.0)
    with pytest.raises(StoreError):
        platform.open_quarantine("ghost", policy="batch_wide")


def test_failed_target_resumes_and_keeps_done_targets(platform):
    _seed_lineage(platform)
    platform.arm_quarantine_failure(rack_id="rk-02")
    order = platform.open_quarantine("c1", policy="batch_wide")
    oid = order["order_id"]
    assert order["progress"]["failed"] == 1
    assert order["state"] == "partial"
    assert platform.isolation_board.is_rack_isolated("rk-02") is False

    attempts_before = platform.isolation_board.attempts
    resumed = platform.resume_quarantine(oid)
    assert resumed["state"] == "completed"
    assert platform.isolation_board.is_rack_isolated("rk-02") is True
    # 只重投失败的 rk-02 一个目标；rk-01、B1、B2 不重复动作
    assert platform.isolation_board.attempts - attempts_before == 1


def test_recompute_only_adds_targets_never_removes(platform):
    _seed_lineage(platform)
    order = platform.open_quarantine("c1", policy="batch_wide")
    oid = order["order_id"]
    assert order["state"] == "completed"

    # 用窗口口径重算，范围即便算出来更小，已隔离目标也不能被撤回
    narrow = platform.recompute_quarantine(oid, policy="overlap_window", mechanism="m")
    assert narrow["state"] == "completed"
    assert platform.isolation_board.is_rack_isolated("rk-01")
    assert platform.isolation_board.is_rack_isolated("rk-02")
    assert set(narrow["targets"]) == {"rack:rk-01", "rack:rk-02", "batch:B1", "batch:B2"}

    # 补录相关批次新单体装到新认知，重算只并集扩张
    platform.record_cell_batch("c3", "B2", 5000.0)
    platform.install_cell("c3", "rk-01", 5000.0)
    grown = platform.recompute_quarantine(oid)
    events = [item["event"] for item in grown["history"]]
    assert "recomputed" in events
    assert grown["progress"]["complete"] is True


def test_recompute_rejects_foreign_seed(platform):
    _seed_lineage(platform)
    order = platform.open_quarantine("c1", policy="batch_wide")
    platform.record_cell_batch("z1", "B9", 1000.0)
    # 未知单体的范围预览为空（不抛错），但开令会拒绝；这里验证跨种子灌范围必拒
    scope = platform.compute_quarantine_scope("c1")
    scope["seed_cell_id"] = "z1"
    with pytest.raises(StoreError):
        platform.quarantine.recompute(order["order_id"], scope, platform.clock.now())


def test_isolated_racks_are_interlocked(platform):
    _seed_lineage(platform)
    platform.open_quarantine("c1", policy="batch_wide")
    with pytest.raises(InterlockError):
        platform.charge("rk-01")
    with pytest.raises(InterlockError):
        platform.discharge(rack_id="rk-01")
    with pytest.raises(InterlockError):
        platform.balance("rk-01")


def test_orders_survive_restart_and_board_rehydrates(tmp_path, clock, config):
    from batterystore.app import build_platform

    storage = str(tmp_path / "state")
    platform = build_platform(config, clock=clock, storage_root=storage, rack_count=2)
    _seed_lineage(platform)
    platform.arm_quarantine_failure(rack_id="rk-02")
    order = platform.open_quarantine("c1", policy="batch_wide")
    oid = order["order_id"]
    assert order["state"] == "partial"

    restored = build_platform(config, clock=clock, storage_root=storage, rack_count=2)
    assert restored.lineage.count() == 5
    assert restored.quarantine.get(oid)["progress"]["failed"] == 1
    # 已完成的 rk-01 物理态按令重建，联锁重启后仍成立
    assert restored.isolation_board.is_rack_isolated("rk-01")
    with pytest.raises(InterlockError):
        restored.charge("rk-01")
    # failed 的 rk-02 续作成功，已完成目标不重发
    before = restored.isolation_board.attempts
    assert restored.resume_quarantine(oid)["state"] == "completed"
    assert restored.isolation_board.attempts - before == 1


def test_concurrent_isolation_orders_keep_independent_state(platform):
    _seed_lineage(platform)
    # 两张不同种子的隔离令并发打开，目标状态互不串扰
    platform.record_cell_batch("d1", "B3", 1000.0)
    platform.install_cell("d1", "rk-02", 1000.0)
    results: list[dict] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def worker(cell_id: str) -> None:
        try:
            barrier.wait(timeout=5)
            results.append(platform.open_quarantine(cell_id, policy="batch_wide"))
        except Exception as error:  # noqa: BLE001 - 测试要捕获线程内异常
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(cell,)) for cell in ("c1", "d1")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors, errors
    assert all(order["state"] == "completed" for order in results)
    listing = platform.quarantine_orders()
    assert {order["seed_cell_id"] for order in listing["orders"]} == {"c1", "d1"}


def test_snapshot_reports_lineage_and_board(platform):
    _seed_lineage(platform)
    platform.open_quarantine("c1", policy="batch_wide")
    snapshot = platform.quarantine_snapshot()
    assert snapshot["lineage_events"] == 5
    assert snapshot["held_racks"] == ["rk-01", "rk-02"]
    assert snapshot["frozen_batches"] == ["B1", "B2"]
    assert snapshot["orders"]
