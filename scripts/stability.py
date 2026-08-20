from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from contextlib import closing


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.database import create_database
from simulation.productization import NewWorldConfig, validate_database
from simulation.world import WorldService


VOLATILE_COLUMNS = {"created_at", "updated_at"}


def fact_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with closing(sqlite3.connect(path)) as connection:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        for table in tables:
            columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
            included = [column for column in columns if column not in VOLATILE_COLUMNS]
            digest.update(table.encode())
            query = f'SELECT {", ".join(f"\"{column}\"" for column in included)} FROM "{table}" ORDER BY rowid'
            for row in connection.execute(query):
                digest.update(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str).encode())
    return digest.hexdigest()


async def run_case(path: Path, days: int, seed: int) -> dict[str, Any]:
    engine, factory = create_database(path)
    service = WorldService(
        factory,
        world_config=NewWorldConfig(world_name=f"稳定性 {days} 日", preset="balanced", seed=seed),
    )
    service.initialize()
    chunks: list[dict[str, Any]] = []
    remaining = days
    started = time.perf_counter()
    while remaining:
        chunk_days = min(30, remaining)
        size_before = path.stat().st_size
        chunk = await service.run_ticks(chunk_days * 144, commit_interval=144)
        size_after = path.stat().st_size
        chunks.append({
            "days": chunk_days,
            "ticks": chunk["ticks"],
            "elapsed_seconds": chunk["elapsed_seconds"],
            "ticks_per_second": chunk["ticks_per_second"],
            "database_growth_bytes": size_after - size_before,
        })
        remaining -= chunk_days
    elapsed = time.perf_counter() - started
    world = await service.world_snapshot()
    npc = await service.get_npc(1)
    stats = await service.world_statistics()
    balance = await service.balance_status()
    product = await service.productization_status()
    validation = validate_database(path, require_v10=True)
    digest = fact_digest(path)
    with closing(sqlite3.connect(path)) as connection:
        random_counter = connection.execute("SELECT random_counter FROM world_state WHERE id=1").fetchone()[0]
        table_counts = {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in ("decisions", "events", "life_milestones", "story_summaries", "world_statistics", "balance_audits")
        }
    engine.dispose()
    expected_ticks = days * 144
    total_ticks = sum(item["ticks"] for item in chunks)
    performance_stable = (
        not chunks
        or min(item["ticks_per_second"] for item in chunks) >= max(0.1, chunks[0]["ticks_per_second"] * 0.20)
    )
    growth = [item["database_growth_bytes"] / max(item["days"], 1) for item in chunks]
    resource_stable = not growth or max(growth) <= max(1_000_000, min(growth) * 6)
    checks = {
        "all_real_ticks_executed": total_ticks == expected_ticks,
        "world_minutes_exact": world["total_minutes"] == 480 + expected_ticks * 10,
        "integrity_ok": validation["integrity_check"] == "ok",
        "foreign_keys_ok": validation["foreign_key_errors"] == 0,
        "numeric_guards_ok": balance["status"] != "critical",
        "performance_trend_bounded": performance_stable,
        "resource_trend_bounded": resource_stable,
        "old_world_shape_exact": set(world) == {"day", "weekday", "time", "label", "total_minutes", "paused", "speed", "locations"},
        "old_npc_shape_exact": set(npc or {}) == {
            "id", "name", "age", "job", "current_location", "current_action",
            "action_end_minute", "money", "states", "personality", "relationships",
        },
    }
    return {
        "days": days,
        "seed": seed,
        "ticks": total_ticks,
        "elapsed_seconds": round(elapsed, 6),
        "ticks_per_second": round(total_ticks / elapsed, 3) if elapsed else 0.0,
        "database_size_bytes": path.stat().st_size,
        "random_counter": random_counter,
        "fact_digest": digest,
        "table_counts": table_counts,
        "chunks": chunks,
        "checks": checks,
        "passed": all(checks.values()),
        "product": product,
        "statistics_digest": stats.get("facts_digest"),
        "balance_status": balance["status"],
    }


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="miniworld-v10-stability-") as directory:
        root = Path(directory)
        for days in args.days:
            repetitions = []
            for repetition in range(args.repeat):
                case_path = root / f"d{days}-r{repetition}.db"
                repetitions.append(await run_case(case_path, days, args.seed))
                # The JSON result contains all evidence; remove only this verified
                # temp-case database before the next run to cap disk usage.
                if case_path.parent == root and case_path.exists():
                    case_path.unlink()
            reproducible = len({item["fact_digest"] for item in repetitions}) == 1
            counter_reproducible = len({item["random_counter"] for item in repetitions}) == 1
            results.append({
                "days": days,
                "reproducible": reproducible,
                "random_counter_reproducible": counter_reproducible,
                "runs": repetitions,
                "passed": reproducible and counter_reproducible and all(item["passed"] for item in repetitions),
            })
    return {
        "version": "1.0.0",
        "tick_minutes": 10,
        "days": args.days,
        "repeat": args.repeat,
        "results": results,
        "passed": all(item["passed"] for item in results),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MiniWorld V1.0 full-tick stability validator")
    parser.add_argument("--days", nargs="+", type=int, choices=(30, 90, 365), default=[30, 90, 365])
    parser.add_argument("--repeat", type=int, choices=(1, 2), default=2)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = asyncio.run(main_async(args))
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
