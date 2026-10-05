"""隔离令数据模型与单调合并。

一张隔离令对应"从一只异常单体出发的一次处置"。范围会被反复重算（补录
装机记录、换口径、推进截止时点都会重算），但已经隔离下去的动作绝不能被
重算撤回。因此重算只做**目标集合的并集单调扩张**：

* 新算出来的目标补进隔离令，状态从 ``pending`` 起步；
* 已经 ``isolated`` / ``failed`` 的目标原样保留，任何目标都不会因重算消失；
* 隔离令的完成态同样单调：曾经全部完成就记 ``completed``，重算扩张后转
  ``partial`` 续作，全部目标再次完成才重新 ``completed``。
"""

from __future__ import annotations

from dataclasses import dataclass, field

STATE_PENDING = "pending"
STATE_ISOLATED = "isolated"
STATE_FAILED = "failed"
TARGET_STATES = (STATE_PENDING, STATE_ISOLATED, STATE_FAILED)

ORDER_PLANNED = "planned"
ORDER_EXECUTING = "executing"
ORDER_PARTIAL = "partial"
ORDER_COMPLETED = "completed"


@dataclass
class TargetState:
    """隔离令中单个对象的执行状态。"""

    kind: str
    target_id: str
    state: str = STATE_PENDING
    attempts: int = 0
    last_error: str = ""
    history: list[dict] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.target_id}"

    @classmethod
    def from_dict(cls, raw: dict) -> "TargetState":
        return cls(
            kind=str(raw.get("kind", "")),
            target_id=str(raw.get("target_id", "")),
            state=str(raw.get("state", STATE_PENDING)),
            attempts=int(raw.get("attempts", 0)),
            last_error=str(raw.get("last_error", "")),
            history=list(raw.get("history", [])),
        )

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "target_id": self.target_id,
            "state": self.state,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "history": list(self.history),
        }


@dataclass
class QuarantineOrder:
    """一张可重算、可续作的隔离令。"""

    order_id: str
    seed_cell_id: str
    created_ts: float
    updated_ts: float
    policy: str
    as_of: float
    window_padding_s: float
    rationale: str
    state: str = ORDER_PLANNED
    targets: dict[str, TargetState] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)
    run_count: int = 0
    last_scope: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict) -> "QuarantineOrder":
        targets = {
            key: TargetState.from_dict(value)
            for key, value in raw.get("targets", {}).items()
        }
        return cls(
            order_id=str(raw.get("order_id", "")),
            seed_cell_id=str(raw.get("seed_cell_id", "")),
            created_ts=float(raw.get("created_ts", 0.0)),
            updated_ts=float(raw.get("updated_ts", 0.0)),
            policy=str(raw.get("policy", "")),
            as_of=float(raw.get("as_of", 0.0)),
            window_padding_s=float(raw.get("window_padding_s", 0.0)),
            rationale=str(raw.get("rationale", "")),
            state=str(raw.get("state", ORDER_PLANNED)),
            targets=targets,
            history=list(raw.get("history", [])),
            run_count=int(raw.get("run_count", 0)),
            last_scope=dict(raw.get("last_scope", {})),
        )

    def as_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "seed_cell_id": self.seed_cell_id,
            "created_ts": round(self.created_ts, 3),
            "updated_ts": round(self.updated_ts, 3),
            "policy": self.policy,
            "as_of": round(self.as_of, 3),
            "window_padding_s": round(self.window_padding_s, 3),
            "rationale": self.rationale,
            "state": self.state,
            "run_count": self.run_count,
            "targets": {key: target.as_dict() for key, target in sorted(self.targets.items())},
            "history": list(self.history),
            "last_scope": dict(self.last_scope),
            "progress": self.progress(),
        }

    def progress(self) -> dict:
        """按状态汇总目标计数。"""

        counts = {STATE_PENDING: 0, STATE_ISOLATED: 0, STATE_FAILED: 0}
        for target in self.targets.values():
            counts[target.state] = counts.get(target.state, 0) + 1
        total = len(self.targets)
        done = counts[STATE_ISOLATED]
        return {
            "total": total,
            "isolated": done,
            "failed": counts[STATE_FAILED],
            "pending": counts[STATE_PENDING],
            "complete": total > 0 and done == total,
        }


def merge_targets(order: QuarantineOrder, scope: dict, ts: float) -> list[str]:
    """把重算范围并入隔离令，返回本次新增的目标键（只增不删）。"""

    added: list[str] = []
    for raw_target in scope.get("targets", []):
        key = f"{raw_target['kind']}:{raw_target['target_id']}"
        if key not in order.targets:
            order.targets[key] = TargetState(kind=raw_target["kind"], target_id=raw_target["target_id"])
            added.append(key)
            order.history.append(
                {"ts": round(ts, 3), "event": "target_added", "target": key, "reasons": raw_target.get("reasons", [])}
            )
    order.as_of = float(scope.get("as_of", order.as_of))
    order.rationale = str(scope.get("rationale", order.rationale))
    order.window_padding_s = float(scope.get("window_padding_s", order.window_padding_s))
    order.last_scope = dict(scope)
    order.updated_ts = ts
    return sorted(added)
