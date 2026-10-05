"""隔离令管理器：持久化、跨簇并发执行、失败续作、重算单调扩张。

管理器持有全部隔离令（原子 JSON 文档落盘，重启恢复），驱动执行板完成物理
隔离，并对平台事件总线发布每一步动作，审计因此能回答四个问题：

1. **算了什么**：每张令的口径、时点、范围指纹与证据随令保存，可回放；
2. **隔了没有**：目标状态只有 ``pending / isolated / failed``，失败带原因；
3. **中途失败怎么办**：``resume`` 只重投 ``pending`` 与 ``failed``，已
   ``isolated`` 的目标由执行板短路，绝不重复动作、更不会撤回；
4. **重算会不会撤隔离**：重算只与现存目标做并集，物理态与令状态都单调。

并发执行对簇目标开线程池（真实现场每簇一路通道），批次冻结是台账动作直接
落；单目标失败只记该目标失败，不阻塞其他簇。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from ..errors import StoreError
from ..ids import IdFactory
from ..store.jsonfile import JsonStore
from .board import RackIsolationBoard
from .orders import (
    ORDER_COMPLETED,
    ORDER_EXECUTING,
    ORDER_PARTIAL,
    ORDER_PLANNED,
    QuarantineOrder,
    STATE_FAILED,
    STATE_ISOLATED,
    STATE_PENDING,
    merge_targets,
)
from .targets import TARGET_BATCH, TARGET_RACK

DOC_ID = "quarantine_orders"

Publisher = Callable[[str, str, str, dict], None]


class QuarantineManager:
    """隔离令的创建、重算、执行、续作与状态查询。"""

    def __init__(
        self,
        store: JsonStore,
        board: RackIsolationBoard,
        *,
        publisher: Publisher | None = None,
        id_factory: IdFactory | None = None,
        max_workers: int = 8,
    ) -> None:
        self._store = store
        self._board = board
        self._publish = publisher or (lambda topic, action, severity, payload: None)
        self._ids = id_factory or IdFactory("q")
        self._max_workers = max(1, max_workers)
        self._orders: dict[str, QuarantineOrder] = {}
        self._lock = threading.RLock()
        self._restore()

    def _restore(self) -> None:
        raw = self._store.read(DOC_ID)
        if not isinstance(raw, dict):
            return
        held_racks: list[str] = []
        frozen_batches: list[str] = []
        for order_id, order_raw in raw.get("orders", {}).items():
            order = QuarantineOrder.from_dict(order_raw)
            self._orders[order_id] = order
            for target in order.targets.values():
                if target.state != STATE_ISOLATED:
                    continue
                if target.kind == TARGET_RACK:
                    held_racks.append(target.target_id)
                elif target.kind == TARGET_BATCH:
                    frozen_batches.append(target.target_id)
        self._board.rehydrate(racks=sorted(set(held_racks)), batches=sorted(set(frozen_batches)))

    def _persist(self) -> None:
        payload = {"orders": {order_id: order.as_dict() for order_id, order in self._orders.items()}}
        self._store.write(DOC_ID, payload)

    def orders(self) -> list[dict]:
        """返回全部隔离令（按创建顺序）。"""

        with self._lock:
            return [self._orders[key].as_dict() for key in sorted(self._orders, key=lambda k: self._orders[k].created_ts)]

    def get(self, order_id: str) -> dict:
        """取一张隔离令。"""

        with self._lock:
            return self._require(order_id).as_dict()

    def _require(self, order_id: str) -> QuarantineOrder:
        order = self._orders.get(order_id)
        if order is None:
            raise StoreError(f"未知隔离令: {order_id}", component="quarantine")
        return order

    def board_snapshot(self) -> dict:
        """返回执行板物理状态。"""

        return self._board.snapshot()

    def held_racks(self) -> list[str]:
        """返回当前物理上处于隔离态的簇，供联锁拦截充放电。"""

        return self._board.isolated_racks()

    def is_rack_held(self, rack_id: str) -> bool:
        """某簇是否已被隔离（重算与续作都不会解除它）。"""

        return self._board.is_rack_isolated(rack_id)

    def frozen_batches(self) -> list[str]:
        """返回已停用的批次。"""

        return self._board.frozen_batches()

    def open_order(self, scope: dict, now: float) -> dict:
        """按一份范围结果创建隔离令并立刻执行一轮。"""

        with self._lock:
            order_id = self._ids.next()
            order = QuarantineOrder(
                order_id=order_id,
                seed_cell_id=str(scope.get("seed_cell_id", "")),
                created_ts=now,
                updated_ts=now,
                policy=str(scope.get("policy", "")),
                as_of=float(scope.get("as_of", now)),
                window_padding_s=float(scope.get("window_padding_s", 0.0)),
                rationale=str(scope.get("rationale", "")),
                state=ORDER_PLANNED,
            )
            self._orders[order_id] = order
            merge_targets(order, scope, now)
            order.history.append({"ts": round(now, 3), "event": "opened", "fingerprint": scope.get("fingerprint", "")})
            self._persist()
            self._publish("quarantine", "opened", "alarm", {"order_id": order_id, **self._scope_brief(scope)})
            return self._drive(order)

    def recompute(self, order_id: str, scope: dict, now: float) -> dict:
        """用新范围重算隔离令：只并集扩张，并对新增目标续作一轮。"""

        with self._lock:
            order = self._require(order_id)
            if scope.get("seed_cell_id") != order.seed_cell_id:
                raise StoreError("重算范围的异常单体必须与隔离令一致", component="quarantine")
            previous_fingerprint = order.last_scope.get("fingerprint", "")
            added = merge_targets(order, scope, now)
            order.history.append(
                {
                    "ts": round(now, 3),
                    "event": "recomputed",
                    "fingerprint": scope.get("fingerprint", ""),
                    "previous_fingerprint": previous_fingerprint,
                    "added": added,
                }
            )
            self._persist()
            self._publish(
                "quarantine",
                "scope_recomputed",
                "warning" if added else "notice",
                {"order_id": order_id, "added": added, "fingerprint": scope.get("fingerprint", "")},
            )
            if not added:
                return order.as_dict()
            return self._drive(order)

    def resume(self, order_id: str, now: float) -> dict:
        """续作一张未完成的隔离令，只重投 pending/failed 目标。"""

        with self._lock:
            return self._drive(self._require(order_id), now_override=now)

    def resume_all(self, now: float) -> dict:
        """跨重启或批量续作：把所有未完成的隔离令续作一遍。"""

        with self._lock:
            touched: list[dict] = []
            for order_id in sorted(self._orders):
                order = self._orders[order_id]
                if not order.progress()["complete"]:
                    touched.append(self._drive(order, now_override=now))
            return {"resumed": touched, "count": len(touched)}

    def fail_once(self, *, rack_id: str = "", batch_id: str = "") -> dict:
        """给执行板挂一次失败（演练与自检用）。"""

        return self._board.fail_once(rack_id=rack_id, batch_id=batch_id)

    def _drive(self, order: QuarantineOrder, now_override: float | None = None) -> dict:
        now = order.updated_ts if now_override is None else now_override
        pending = [
            target
            for target in order.targets.values()
            if target.state in (STATE_PENDING, STATE_FAILED)
        ]
        if not pending:
            order.state = ORDER_COMPLETED
            self._persist()
            return order.as_dict()

        order.state = ORDER_EXECUTING
        order.run_count += 1
        run_id = order.run_count
        order.history.append(
            {"ts": round(now, 3), "event": "run_started", "run": run_id, "targets": [target.key for target in pending]}
        )
        self._persist()
        self._publish(
            "quarantine",
            "run_started",
            "warning",
            {"order_id": order.order_id, "run": run_id, "targets": [target.key for target in pending]},
        )

        rack_targets = [target for target in pending if target.kind == TARGET_RACK]
        batch_targets = [target for target in pending if target.kind == TARGET_BATCH]

        def act(target) -> None:
            reason = f"{order.order_id}:{order.seed_cell_id}"
            target.attempts += 1
            try:
                if target.kind == TARGET_RACK:
                    result = self._board.isolate_rack(target.target_id, reason)
                else:
                    result = self._board.freeze_batch(target.target_id, reason)
                target.state = STATE_ISOLATED
                target.last_error = ""
                target.history.append({"ts": round(now, 3), "run": run_id, "ok": True, "result": result})
                self._publish(
                    "quarantine",
                    "target_isolated" if target.kind == TARGET_RACK else "batch_frozen",
                    "alarm",
                    {"order_id": order.order_id, "run": run_id, "target": target.key, "result": result},
                )
            except StoreError as error:
                target.state = STATE_FAILED
                target.last_error = error.message
                target.history.append(
                    {"ts": round(now, 3), "run": run_id, "ok": False, "error": error.as_record()}
                )
                self._publish(
                    "quarantine",
                    "target_failed",
                    "alarm",
                    {
                        "order_id": order.order_id,
                        "run": run_id,
                        "target": target.key,
                        "error": error.as_record(),
                    },
                )

        for target in batch_targets:
            act(target)
        if rack_targets:
            with ThreadPoolExecutor(max_workers=min(self._max_workers, len(rack_targets))) as pool:
                futures = {pool.submit(act, target): target for target in rack_targets}
                for future in as_completed(futures):
                    future.result()

        progress = order.progress()
        if progress["complete"]:
            order.state = ORDER_COMPLETED
        elif progress["isolated"] > 0:
            order.state = ORDER_PARTIAL
        else:
            order.state = ORDER_EXECUTING
        order.updated_ts = now
        order.history.append(
            {
                "ts": round(now, 3),
                "event": "run_finished",
                "run": run_id,
                "state": order.state,
                "progress": progress,
            }
        )
        self._persist()
        self._publish(
            "quarantine",
            "run_finished",
            "notice" if progress["complete"] else "warning",
            {"order_id": order.order_id, "run": run_id, "state": order.state, "progress": progress},
        )
        return order.as_dict()

    def _scope_brief(self, scope: dict) -> dict:
        return {
            "seed_cell_id": scope.get("seed_cell_id", ""),
            "policy": scope.get("policy", ""),
            "as_of": scope.get("as_of", 0.0),
            "rack_ids": scope.get("rack_ids", []),
            "frozen_batches": scope.get("frozen_batches", []),
            "fingerprint": scope.get("fingerprint", ""),
        }
