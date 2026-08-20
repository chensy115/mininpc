from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from statistics import median


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.database import create_database
from scripts.stability import fact_digest
from simulation.productization import NewWorldConfig, validate_database
from simulation.world import WorldService
from simulation.dashboard import DASHBOARD_GROUPS, NPC_SECTIONS


async def build(path: Path) -> tuple[object, WorldService]:
    engine, factory = create_database(path)
    service = WorldService(factory, world_config=NewWorldConfig(world_name="性能基准", seed=42))
    service.initialize()
    return engine, service


def latency_summary(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    p95_index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))
    return {
        "minimum_ms": round(ordered[0], 3),
        "median_ms": round(median(ordered), 3),
        "p95_ms": round(ordered[p95_index], 3),
        "maximum_ms": round(ordered[-1], 3),
    }


async def snapshot_benchmark(service: WorldService, iterations: int) -> dict:
    homepage_latencies: list[float] = []
    npc_latencies: list[float] = []
    homepage_size = 0
    npc_size = 0
    for _ in range(iterations):
        started = time.perf_counter()
        homepage = await service.dashboard_snapshot(DASHBOARD_GROUPS)
        homepage_latencies.append((time.perf_counter() - started) * 1000)
        homepage_size = len(json.dumps(homepage, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

        started = time.perf_counter()
        npc = await service.dashboard_npc_snapshot(1, NPC_SECTIONS)
        npc_latencies.append((time.perf_counter() - started) * 1000)
        npc_size = len(json.dumps(npc, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    return {
        "iterations": iterations,
        "provider_calls": 0,
        "homepage": {
            "latency": latency_summary(homepage_latencies),
            "response_bytes": homepage_size,
            "legacy_equivalent_gets": 6,
            "snapshot_gets": 1,
            "theoretical_request_reduction_percent": 83.333,
            "minimum_group_reduction_percent": 75.0,
        },
        "npc_overview_and_decision": {
            "latency": latency_summary(npc_latencies),
            "response_bytes": npc_size,
            "legacy_equivalent_gets": 8,
            "snapshot_gets": 1,
            "theoretical_request_reduction_percent": 87.5,
        },
    }


async def main_async(ticks: int, dashboard_iterations: int = 25) -> dict:
    with tempfile.TemporaryDirectory(prefix="miniworld-v10-benchmark-") as directory:
        root = Path(directory)
        legacy_path = root / "per-tick.db"
        batch_path = root / "batch.db"
        legacy_engine, legacy = await build(legacy_path)
        started = time.perf_counter()
        for _ in range(ticks):
            if not await legacy.tick():
                raise RuntimeError("逐 Tick 基准意外停止")
        legacy_elapsed = time.perf_counter() - started
        legacy_engine.dispose()

        batch_engine, batch = await build(batch_path)
        started = time.perf_counter()
        batch_result = await batch.run_ticks(ticks, commit_interval=144)
        batch_elapsed = time.perf_counter() - started
        dashboard_performance = await snapshot_benchmark(batch, dashboard_iterations)
        batch_engine.dispose()

        legacy_digest = fact_digest(legacy_path)
        batch_digest = fact_digest(batch_path)
        legacy_validation = validate_database(legacy_path, require_v10=True)
        batch_validation = validate_database(batch_path, require_v10=True)
        legacy_rate = ticks / legacy_elapsed
        batch_rate = ticks / batch_elapsed
        checks = {
            "same_full_fact_digest": legacy_digest == batch_digest,
            "same_tick_count": batch_result["ticks"] == ticks,
            "legacy_integrity": legacy_validation["valid"],
            "batch_integrity": batch_validation["valid"],
            "batch_not_slower": batch_rate >= legacy_rate,
        }
        return {
            "version": "1.0.0",
            "ticks": ticks,
            "simulated_minutes": ticks * 10,
            "before": {"strategy": "commit every tick", "elapsed_seconds": round(legacy_elapsed, 6), "ticks_per_second": round(legacy_rate, 3)},
            "after": {"strategy": "same Engine facts, commit every 144 ticks", "elapsed_seconds": round(batch_elapsed, 6), "ticks_per_second": round(batch_rate, 3)},
            "speedup": round(batch_rate / legacy_rate, 3),
            "fact_digest": batch_digest,
            "dashboard_snapshots": dashboard_performance,
            "checks": checks,
            "passed": all(checks.values()),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="MiniWorld V1.0 full-fact commit batching benchmark")
    parser.add_argument("--ticks", type=int, default=720)
    parser.add_argument("--dashboard-iterations", type=int, default=25)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.ticks < 144 or args.ticks > 5000:
        parser.error("--ticks must be between 144 and 5000")
    if args.dashboard_iterations < 1 or args.dashboard_iterations > 1000:
        parser.error("--dashboard-iterations must be between 1 and 1000")
    result = asyncio.run(main_async(args.ticks, args.dashboard_iterations))
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
