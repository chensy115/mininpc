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

from database.database import (
    V11_TABLES,
    V12_TABLES,
    V14_TABLES,
    V15_TABLES,
    V16_TABLES,
    create_database,
)


EXPECTED_ADDITIVE_TABLE_NAMES = {
    table.name
    for table in (*V11_TABLES, *V12_TABLES, *V14_TABLES, *V15_TABLES, *V16_TABLES)
}


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _capture(path: Path) -> dict[str, object]:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=60)) as connection:
        connection.execute("PRAGMA busy_timeout=60000")
        schemas = {
            str(name): str(sql)
            for name, sql in connection.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        }
        fixed_npcs = connection.execute(
            "SELECT id, name, age, job FROM npcs ORDER BY id"
        ).fetchall()
        world = connection.execute(
            "SELECT total_minutes, paused, speed, seed, random_counter "
            "FROM world_state WHERE id=1"
        ).fetchone()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        new_counts = {
            name: connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            for name in sorted(EXPECTED_ADDITIVE_TABLE_NAMES)
            if name in schemas
        }
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "size_bytes": path.stat().st_size,
        "table_count": len(schemas),
        "tables": sorted(schemas),
        "schemas": schemas,
        "schema_digest": hashlib.sha256(_canonical(schemas).encode()).hexdigest(),
        "fixed_npcs": fixed_npcs,
        "world_observation": world,
        "integrity_check": integrity,
        "foreign_key_errors": foreign_keys,
        "new_table_counts": new_counts,
    }


def _backup(source: Path, destination: Path) -> None:
    if destination.exists():
        raise RuntimeError(f"refusing to overwrite existing backup: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source)) as source_connection:
        with closing(sqlite3.connect(destination)) as destination_connection:
            source_connection.backup(destination_connection)


def _public_capture(capture: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in capture.items() if key != "schemas"}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Protected MiniWorld V1.6 formal-database additive upgrade validator"
    )
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "world.db")
    parser.add_argument(
        "--backup", type=Path, default=ROOT / "logs" / "v16-world-pre-upgrade.db"
    )
    parser.add_argument(
        "--output", type=Path, default=ROOT / "logs" / "v16-upgrade-validation.json"
    )
    args = parser.parse_args()
    db_path = args.db.resolve()
    backup_path = args.backup.resolve()
    current = _capture(db_path)
    expected_new = EXPECTED_ADDITIVE_TABLE_NAMES
    current_names = set(current["tables"])
    if not (current_names & expected_new):
        before = current
        _backup(db_path, backup_path)
        engine, _factory = create_database(db_path)
        engine.dispose()
        after_first = _capture(db_path)
    elif expected_new <= current_names and backup_path.is_file():
        before = _capture(backup_path)
        if set(before["tables"]) & expected_new:
            raise RuntimeError("upgrade backup is not a clean pre-V1.6 formal database")
        after_first = current
    else:
        raise RuntimeError(
            "formal database has a partial/unknown additive schema or no protected backup"
        )
    before_names = set(before["tables"])
    engine, _factory = create_database(db_path)
    engine.dispose()
    after_second = _capture(db_path)

    first_names = set(after_first["tables"])
    added = sorted(first_names - before_names)
    changed_old_sql = sorted(
        name
        for name in before_names
        if before["schemas"][name] != after_first["schemas"].get(name)
    )
    checks = {
        "started_from_v10_50_tables": before["table_count"] == 50,
        "added_exact_v11_through_v16_tables": set(added) == expected_new,
        "after_has_69_tables": after_first["table_count"] == 69,
        "all_old_table_sql_byte_equal": not changed_old_sql,
        "fixed_npc_facts_equal": before["fixed_npcs"] == after_first["fixed_npcs"],
        "world_observation_equal": before["world_observation"] == after_first["world_observation"],
        "new_tables_start_empty": all(
            count == 0 for count in after_first["new_table_counts"].values()
        ),
        "integrity_before_ok": before["integrity_check"] == "ok",
        "integrity_after_ok": after_first["integrity_check"] == "ok",
        "foreign_keys_before_ok": not before["foreign_key_errors"],
        "foreign_keys_after_ok": not after_first["foreign_key_errors"],
        "second_upgrade_schema_identical": (
            after_first["schema_digest"] == after_second["schema_digest"]
        ),
        "second_upgrade_rows_identical": (
            after_first["new_table_counts"] == after_second["new_table_counts"]
        ),
        "backup_created": backup_path.is_file() and backup_path.stat().st_size > 0,
    }
    result = {
        "version": "1.6.0",
        "database": str(db_path),
        "backup": str(backup_path),
        "before": _public_capture(before),
        "after_first": _public_capture(after_first),
        "after_second": _public_capture(after_second),
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
