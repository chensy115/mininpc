from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.database import create_database
from database.models import WorldState
from simulation.productization import NewWorldConfig, V10_TABLE_NAMES, ensure_product_data


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def capture(path: Path) -> dict:
    with closing(sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=30)) as connection:
        connection.execute("PRAGMA busy_timeout=30000")
        schemas = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        }
        fixed_npcs = connection.execute(
            "SELECT id, name, age, job FROM npcs ORDER BY id"
        ).fetchall()
        world = connection.execute(
            "SELECT total_minutes, paused, speed, seed, random_counter FROM world_state WHERE id=1"
        ).fetchone()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        counts = {
            name: connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            for name in V10_TABLE_NAMES if name in schemas
        }
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "size_bytes": path.stat().st_size,
        "table_count": len(schemas),
        "tables": sorted(schemas),
        "schemas": schemas,
        "schema_digest": hashlib.sha256(canonical(schemas).encode()).hexdigest(),
        "fixed_npcs": fixed_npcs,
        "world_observation": world,
        "integrity_check": integrity,
        "foreign_key_errors": foreign_keys,
        "v10_counts": counts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Protected MiniWorld V1.0 real-database additive validator")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "world.db")
    parser.add_argument("--output", type=Path, default=ROOT / "logs" / "v10-validation.json")
    args = parser.parse_args()
    db_path = args.db.resolve()
    before = capture(db_path)
    if set(before["tables"]) & V10_TABLE_NAMES:
        raise RuntimeError("真实库已含 V1.0 表；此脚本只允许执行一次首轮保护性升级")

    engine, factory = create_database(db_path)
    try:
        with factory() as session:
            state = session.get(WorldState, 1)
            if state is None:
                raise RuntimeError("真实库缺少 world_state，拒绝升级")
            ensure_product_data(
                session,
                state,
                NewWorldConfig(),
                getattr(engine, "_miniworld_upgrade_context", {}),
            )
            session.commit()
    finally:
        engine.dispose()

    after = capture(db_path)
    old_names = set(before["tables"])
    added = sorted(set(after["tables"]) - old_names)
    changed_old_sql = sorted(
        name for name in old_names if before["schemas"][name] != after["schemas"].get(name)
    )
    checks = {
        "started_from_v09_44_tables": before["table_count"] == 44,
        "added_exact_v10_tables": set(added) == V10_TABLE_NAMES,
        "all_old_table_sql_byte_equal": not changed_old_sql,
        "fixed_npc_facts_equal": before["fixed_npcs"] == after["fixed_npcs"],
        "integrity_before_ok": before["integrity_check"] == "ok",
        "integrity_after_ok": after["integrity_check"] == "ok",
        "foreign_keys_before_ok": not before["foreign_key_errors"],
        "foreign_keys_after_ok": not after["foreign_key_errors"],
        "v10_no_fake_history": after["v10_counts"].get("world_statistics") == 0
        and after["v10_counts"].get("balance_audits") == 0
        and after["v10_counts"].get("data_transfer_audits") == 0,
        "v10_single_baselines": after["v10_counts"].get("product_state") == 1
        and after["v10_counts"].get("onboarding_progress") == 1
        and after["v10_counts"].get("upgrade_reports") == 1,
    }
    result = {
        "version": "1.0.0",
        "database": str(db_path),
        "before": before,
        "after": after,
        "added_tables": added,
        "changed_old_sql": changed_old_sql,
        "checks": checks,
        "passed": all(checks.values()),
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
