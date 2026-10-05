"""跨簇并发隔离编排。

隔离是跨多簇的并发动作：每簇一条线程并行下落，单簇失败不拖住其他簇——
失败的簇由战役记成 ``failed`` 等续作，成功的簇正常落停。执行钩子由平台
注入（咬合 BMS 故障闩锁），编排只负责并发调度与状态记账，不直接碰 BMS。
"""

from __future__ import annotations

import threading

JOIN_TIMEOUT_S = 30.0


def isolate_racks(campaign, apply, rack_ids: list[str], now: float) -> dict:
    """并发下落隔离，返回带步骤与违约项的报告。"""

    steps: list[dict] = []
    violations: list[str] = []
    selected: list[str] = []
    for rack_id in dict.fromkeys(rack_ids):
        state = campaign.state_of(rack_id)
        if state in ("pending", "failed"):
            selected.append(rack_id)
        elif state is None:
            violations.append(f"{rack_id}: 不在隔离范围")
    for rack_id in selected:
        campaign.mark_running(rack_id, now)
    steps.append({"component": "trace", "action": "dispatch", "detail": {"racks": len(selected)}})

    errors: dict[str, str] = {}
    lock = threading.Lock()

    def worker(rack_id: str) -> None:
        try:
            apply(rack_id)
        except Exception as error:  # 单簇失败不拖住其他簇，记下原因等续作
            with lock:
                errors[rack_id] = f"{type(error).__name__}: {error}"

    threads = [
        threading.Thread(target=worker, args=(rack_id,), name=f"isolate-{rack_id}") for rack_id in selected
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=JOIN_TIMEOUT_S)
    for rack_id, thread in zip(selected, threads):
        if thread.is_alive():
            with lock:
                errors[rack_id] = "isolation_timeout"

    isolated: list[str] = []
    for rack_id in selected:
        if rack_id in errors:
            campaign.mark_failed(rack_id, errors[rack_id], now)
            violations.append(f"{rack_id}: {errors[rack_id]}")
        else:
            campaign.mark_isolated(rack_id, now)
            isolated.append(rack_id)
    steps.append(
        {
            "component": "trace",
            "action": "settle",
            "detail": {"isolated": len(isolated), "failed": len(errors)},
        }
    )
    return {
        "ok": not violations,
        "steps": steps,
        "violations": violations,
        "isolated": sorted(isolated),
        "failed": {rack_id: errors[rack_id] for rack_id in sorted(errors)},
        "complete": campaign.is_complete(),
    }
