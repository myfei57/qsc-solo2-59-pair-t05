"""批次追溯与隔离处置。

覆盖五条主线：谱系台账的一致性约束与时间点回放、两种范围口径的分歧、
隔离战役的并发执行与失败续作、重算范围的单调合并（已隔离不撤回）、
以及跨重启的台账与战役恢复。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest
import support
from batterystore.app import build_platform
from batterystore.clock import ManualClock
from batterystore.config import default_config
from batterystore.console.server import ConsoleServer
from batterystore.errors import TraceError
from batterystore.store.journal import JsonlJournal
from batterystore.trace.ledger import CellLedger
from batterystore.trace.service import TraceService


def _post(base_url: str, path: str, payload: dict):
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _get(base_url: str, path: str):
    with urllib.request.urlopen(f"{base_url}{path}", timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


@pytest.fixture()
def ledger(tmp_path):
    return CellLedger(JsonlJournal(str(tmp_path), "trace"))


def test_ledger_enforces_movement_invariants(ledger):
    ledger.register_cell("c1", "lot-a", 100.0)
    ledger.install("c1", "rk-01", 110.0)
    with pytest.raises(TraceError):
        ledger.install("c1", "rk-02", 120.0)  # 未离簇不能跨簇
    with pytest.raises(TraceError):
        ledger.register_cell("c1", "lot-b", 130.0)  # 批次不能改写
    with pytest.raises(TraceError):
        ledger.install("c2", "rk-01", 140.0)  # 未登记批次不能进簇
    with pytest.raises(TraceError):
        ledger.remove("c1", "rk-02", 150.0)  # 不在该簇不能离簇
    ledger.remove("c1", "rk-01", 160.0)
    ledger.install("c1", "rk-02", 170.0)
    assert ledger.installed_rack("c1") == "rk-02"
    assert ledger.racks_of_cell("c1") == ["rk-01", "rk-02"]


def test_ledger_replays_history_as_of(ledger):
    ledger.register_cell("c1", "lot-a", 100.0)
    ledger.install("c1", "rk-01", 110.0)
    ledger.remove("c1", "rk-01", 120.0)
    ledger.install("c1", "rk-02", 130.0)
    early = ledger.residencies("c1", as_of=115.0)
    assert [(stay["rack_id"], stay["removed_at"]) for stay in early] == [("rk-01", None)]
    assert ledger.racks_of_cell("c1", as_of=115.0) == ["rk-01"]
    with pytest.raises(TraceError):
        ledger.batch_of("c1", as_of=50.0)  # 登记之前查批次，属于未知单体
    assert ledger.batch_of("c1", as_of=200.0) == "lot-a"


def test_ledger_restore_rebuilds_state(tmp_path):
    journal = JsonlJournal(str(tmp_path), "trace")
    first = CellLedger(journal)
    first.register_cell("c1", "lot-a", 100.0)
    first.install("c1", "rk-01", 110.0)
    second = CellLedger(JsonlJournal(str(tmp_path), "trace"))
    assert second.batch_of("c1") == "lot-a"
    assert second.installed_rack("c1") == "rk-01"
    # 幂等补种：同批次重复登记与同簇重复进簇不再追加流水
    second.register_cell("c1", "lot-a", 120.0)
    second.install("c1", "rk-01", 120.0)
    assert second.summary()["journal_count"] == 2


def test_window_caliber_converges_to_overlap(tmp_path):
    clock = ManualClock(start=0.0)
    service = TraceService(str(tmp_path), clock)
    clock.advance(50.0)
    service.register_cell("c2", "lot-x")
    service.install_cell("c2", "rk-02")
    clock.advance(40.0)  # t=90：c2 在异常单体进簇前已撤出 rk-02
    service.remove_cell("c2", "rk-02")
    clock.advance(10.0)  # t=100：异常单体 c1 进 rk-01，暴露窗口从这里打开
    service.register_cell("c1", "lot-x")
    service.install_cell("c1", "rk-01")
    clock.advance(300.0)  # t=400：c3 进 rk-03，窗口内在架
    service.register_cell("c3", "lot-x")
    service.install_cell("c3", "rk-03")
    clock.advance(100.0)  # t=500：检出

    wide = service.open_campaign(["c1"], "batch")
    assert sorted(wide["scope"]["targets"]) == ["rk-01", "rk-02", "rk-03"]
    narrow = service.recalculate(wide["campaign"]["campaign_id"], caliber="window")
    assert sorted(narrow["scope"]["targets"]) == ["rk-01", "rk-03"]
    assert narrow["scope"]["exposure_window"] == {"start": 100.0, "end": 500.0}


def test_open_campaign_rejects_empty_scope(tmp_path):
    service = TraceService(str(tmp_path), ManualClock())
    service.register_cell("c1", "lot-x")  # 只登记批次，没有任何装机记录
    with pytest.raises(TraceError):
        service.open_campaign(["c1"], "batch")
    with pytest.raises(TraceError):
        service.open_campaign(["ghost"], "batch")
    with pytest.raises(TraceError):
        service.open_campaign(["c1"], "bogus")


def test_platform_seeds_ledger_and_traces_cell(platform):
    report = platform.trace_cell("rk-01-p1c1")
    assert report["batch_id"] == "lot-1"
    assert report["racks"] == ["rk-01"]
    assert report["current_rack"] == "rk-01"
    assert platform.trace_cell("rk-02-p1c1")["batch_id"] == "lot-2"
    with pytest.raises(TraceError):
        platform.trace_cell("ghost-cell")


def test_replace_pack_rotates_genealogy(platform):
    rotated = platform.replace_pack_rating(pack_index=0, rating_v=3.35, rack_id="rk-02", batch_id="lot-1")
    assert rotated["rotation"] == {"pack_id": "rk-02-p1", "batch_id": "lot-1", "rotated": 12}
    report = platform.trace_cell("rk-02-p1-e1c1")
    assert report["batch_id"] == "lot-1"
    assert report["current_rack"] == "rk-02"
    # 换出去的旧单体已离簇，谱系里保留完整区间
    old = platform.trace_cell("rk-02-p1c1")
    assert old["current_rack"] is None
    assert old["residencies"][0]["removed_at"] is not None


def test_isolation_executes_concurrently_and_latches(platform):
    platform.replace_pack_rating(pack_index=0, rating_v=3.35, rack_id="rk-02", batch_id="lot-1")
    opened = platform.open_isolation(cell_id="rk-01-p1c1")
    assert sorted(opened["scope"]["targets"]) == ["rk-01", "rk-02"]
    campaign_id = opened["campaign"]["campaign_id"]
    executed = platform.execute_isolation(campaign_id)
    assert executed["ok"] and executed["complete"]
    assert executed["isolated"] == ["rk-01", "rk-02"]
    assert platform.bms.latched("rk-01") and platform.bms.latched("rk-02")
    # 闩锁挡住就绪判据：被隔离的簇不能再并网
    readiness = platform.readiness()["racks"]
    assert readiness["rk-01"]["ready"] is False
    assert readiness["rk-02"]["ready"] is False
    snapshot = platform.isolation_snapshot(campaign_id)
    assert snapshot["counts"]["isolated"] == 2


def test_failed_isolation_resumes_without_repeating(platform):
    platform.replace_pack_rating(pack_index=0, rating_v=3.35, rack_id="rk-02", batch_id="lot-1")
    campaign_id = platform.open_isolation(cell_id="rk-01-p1c1")["campaign"]["campaign_id"]
    platform.block_isolation("rk-02")
    first = platform.execute_isolation(campaign_id)
    assert first["ok"] is False and first["complete"] is False
    assert first["isolated"] == ["rk-01"]
    assert list(first["failed"]) == ["rk-02"]
    assert platform.isolation_snapshot(campaign_id)["tasks"]["rk-02"]["state"] == "failed"

    platform.block_isolation("rk-02", blocked=False)
    resumed = platform.resume_isolation(campaign_id)
    assert resumed["ok"] and resumed["complete"]
    # 已隔离的 rk-01 不重复下落：闩锁代际仍是第一次咬合的 1
    assert platform.bms.latch.generation("rk-01") == 1
    assert platform.bms.latch.generation("rk-02") == 1


def test_recalculated_scope_never_revokes_isolated(platform, clock):
    campaign_id = platform.open_isolation(cell_id="rk-01-p1c1")["campaign"]["campaign_id"]
    platform.execute_isolation(campaign_id)
    assert platform.isolation_snapshot(campaign_id)["complete"] is True

    # 换更窄的口径重算：已隔离的簇不撤回
    narrowed = platform.recalculate_isolation(campaign_id, caliber="window")
    assert narrowed["merged"]["added"] == []
    assert platform.isolation_snapshot(campaign_id)["tasks"]["rk-01"]["state"] == "isolated"

    # 台账补录新移动后重算：只把新簇并进来，旧状态一律保留
    clock.advance(60.0)
    platform.replace_pack_rating(pack_index=0, rating_v=3.35, rack_id="rk-02", batch_id="lot-1")
    widened = platform.recalculate_isolation(campaign_id)
    assert widened["merged"]["added"] == ["rk-02"]
    tasks = platform.isolation_snapshot(campaign_id)["tasks"]
    assert tasks["rk-01"]["state"] == "isolated"
    assert tasks["rk-02"]["state"] == "pending"
    assert platform.isolation_snapshot(campaign_id)["complete"] is False


def test_campaign_state_survives_restart(tmp_path):
    storage = str(tmp_path / "state")
    clock = ManualClock()
    first = build_platform(default_config(), clock=clock, storage_root=storage, rack_count=2)
    first.replace_pack_rating(pack_index=0, rating_v=3.35, rack_id="rk-02", batch_id="lot-1")
    campaign_id = first.open_isolation(cell_id="rk-01-p1c1")["campaign"]["campaign_id"]
    first.block_isolation("rk-02")
    assert first.execute_isolation(campaign_id)["ok"] is False

    # 重启：战役从隔离流水折叠回来，已隔离的保持隔离，失败的保持失败
    second = build_platform(default_config(), clock=clock, storage_root=storage, rack_count=2)
    tasks = second.isolation_snapshot(campaign_id)["tasks"]
    assert tasks["rk-01"]["state"] == "isolated"
    assert tasks["rk-02"]["state"] == "failed"
    assert second.trace_cell("rk-02-p1-e1c1")["batch_id"] == "lot-1"
    # 已隔离簇的闩锁在重启后重新咬合，不能偷偷恢复并网
    assert second.bms.latched("rk-01") is True
    resumed = second.resume_isolation(campaign_id)  # 不可达标记是运行期状态，重启后消失
    assert resumed["ok"] and resumed["complete"] is True


def test_trace_replay_shows_scope_at_past_moment(platform, clock):
    campaign_id = platform.open_isolation(cell_id="rk-01-p1c1")["campaign"]["campaign_id"]
    clock.advance(100.0)
    platform.replace_pack_rating(pack_index=0, rating_v=3.35, rack_id="rk-02", batch_id="lot-1")
    platform.recalculate_isolation(campaign_id)
    replayed = platform.trace_replay(campaign_id, as_of=1758000050.0)
    assert replayed["then_targets"] == ["rk-01"]
    assert replayed["current_targets"] == ["rk-01", "rk-02"]
    assert replayed["added_since"] == ["rk-02"]


def test_trace_replay_before_seed_registered(platform):
    platform.record_cell_move("cell-later", "rk-02", batch_id="lot-2")
    campaign_id = platform.open_isolation(cell_id="cell-later")["campaign"]["campaign_id"]
    # 回放到单体登记之前：当时追不到它，按空范围回答而不是报错
    replayed = platform.trace_replay(campaign_id, as_of=1758000000.0 - 1.0)
    assert replayed["then_targets"] == []
    assert replayed["unknown_seeds"] == ["cell-later"]
    assert replayed["current_targets"] == ["rk-02"]


def test_record_cell_move_enters_maintenance_record(platform):
    moved = platform.record_cell_move("rk-01-p1c1", "rk-02")
    assert moved["from_rack"] == "rk-01" and moved["to_rack"] == "rk-02"
    report = platform.trace_cell("rk-01-p1c1")
    assert report["racks"] == ["rk-01", "rk-02"]
    assert report["current_rack"] == "rk-02"
    with pytest.raises(TraceError):
        platform.record_cell_move("rk-01-p1c1", "rk-02")  # 已在目标簇，调拨不成立


def test_console_api_drives_isolation_workflow(platform):
    support.bring_online(platform)
    server = ConsoleServer(platform, port=0)
    base_url = server.start()
    try:
        code, opened = _post(base_url, "/api/isolation/open", {"cell_id": "rk-01-p1c1", "caliber": "batch"})
        assert code == 200
        campaign_id = opened["campaign"]["campaign_id"]
        code, executed = _post(base_url, "/api/isolation/execute", {"campaign_id": campaign_id})
        assert code == 200 and executed["complete"] is True
        code, detail = _get(base_url, f"/api/isolation/{campaign_id}")
        assert code == 200 and detail["counts"]["isolated"] == 1
        code, traced = _get(base_url, "/api/trace/rk-01-p1c1")
        assert code == 200 and traced["batch_id"] == "lot-1"
        code, replayed = _get(base_url, f"/api/isolation/replay/{campaign_id}?as_of=1758000000.0")
        assert code == 200 and replayed["then_targets"] == ["rk-01"]
        code, overview = _get(base_url, "/api/trace")
        assert code == 200 and overview["ledger"]["cells"] > 0
        try:
            _get(base_url, "/api/trace/ghost-cell")
        except urllib.error.HTTPError as error:
            assert error.code == 409
        else:  # pragma: no cover - 明确失败路径
            raise AssertionError("未知单体应当返回 409")
    finally:
        server.stop()
