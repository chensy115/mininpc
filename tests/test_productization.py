from __future__ import annotations

import asyncio
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, inspect, select

from api.dependencies import configure_world_service
from api.npc import router as npc_router
from api.world import router as world_router
from database.database import V10_TABLES, create_database
from database.models import (
    BalanceAudit,
    DecisionLog,
    NPC,
    OnboardingProgress,
    ProductState,
    UpgradeReport,
    WorldState,
    WorldStatistic,
)
from simulation.productization import (
    CreateSaveRequest,
    ImportSaveRequest,
    NewWorldConfig,
    OnboardingRequest,
    SaveManager,
    SaveOwnership,
    SaveOwnershipError,
    V10_TABLE_NAMES,
    validate_database,
)
from simulation.world import WorldService
from simulation.narrative import NarrativeGenerator, NarrativeSettings


def _service(path: Path, **kwargs):
    engine, sessions = create_database(path)
    service = WorldService(sessions, **kwargs)
    service.initialize()
    return engine, sessions, service


def _old_fact_snapshot(sessions):
    with sessions() as session:
        state = session.get(WorldState, 1)
        npcs = list(session.scalars(select(NPC).order_by(NPC.id)))
        decisions = list(session.scalars(select(DecisionLog).order_by(DecisionLog.id)))
        return {
            "state": (state.total_minutes, state.paused, state.speed, state.seed, state.random_counter),
            "npcs": [
                (
                    row.id, row.name, row.age, row.job, row.current_location, row.current_action,
                    row.action_end_minute, row.pending_location, row.last_move_minute, row.money,
                    row.energy, row.hunger, row.mood, row.social_need, row.work_satisfaction,
                    row.extroversion, row.kindness, row.ambition, row.risk_tolerance, row.discipline,
                )
                for row in npcs
            ],
            "decisions": [
                (row.npc_id, row.world_day, row.world_time, row.chosen_action, row.candidates_json, row.reason_json)
                for row in decisions
            ],
        }


def test_v10_initializes_additive_product_records(world_service):
    status = asyncio.run(world_service.productization_status())
    assert status["enabled"] is True
    assert status["mode"] == "v1.0"
    assert status["version"] == "1.0.0"
    with world_service.session_factory() as session:
        assert V10_TABLE_NAMES.issubset(set(inspect(session.get_bind()).get_table_names()))
        assert session.get(ProductState, 1).preset_key == "balanced"
        assert session.get(OnboardingProgress, 1) is not None
        assert session.scalar(select(func.count()).select_from(WorldStatistic)) == 0
        assert session.scalar(select(func.count()).select_from(BalanceAudit)) == 0


def test_v10_upgrade_report_is_idempotent_across_restarts(tmp_path):
    path = tmp_path / "restart.db"
    first_engine, first_sessions, first = _service(path)
    asyncio.run(first.tick())
    with first_sessions() as session:
        assert session.scalar(select(func.count()).select_from(UpgradeReport)) == 1
    first_engine.dispose()

    second_engine, second_sessions = create_database(path)
    try:
        WorldService(second_sessions).initialize()
        with second_sessions() as session:
            assert session.scalar(select(func.count()).select_from(UpgradeReport)) == 1
    finally:
        second_engine.dispose()


def test_new_world_config_is_strict_finite_and_reproducible(tmp_path):
    with pytest.raises(ValidationError):
        NewWorldConfig.model_validate({"preset": "arbitrary", "seed": 42})
    with pytest.raises(ValidationError):
        NewWorldConfig.model_validate({"preset": "balanced", "unknown": True})
    config = NewWorldConfig(world_name="Career", preset="career_focus", seed=991, speed=5)
    left_engine, left_sessions, _left = _service(tmp_path / "left.db", world_config=config)
    right_engine, right_sessions, _right = _service(tmp_path / "right.db", world_config=config)
    try:
        assert _old_fact_snapshot(left_sessions) == _old_fact_snapshot(right_sessions)
        with left_sessions() as session:
            state = session.get(WorldState, 1)
            alice = session.get(NPC, 1)
            product = session.get(ProductState, 1)
            assert (state.seed, state.speed) == (991, 5)
            assert alice.money == 132.0
            assert alice.ambition == 0.67
            assert json.loads(product.config_json) == config.model_dump()
    finally:
        left_engine.dispose(); right_engine.dispose()


def test_v10_api_is_additive_and_old_exact_shapes_remain(world_service, tmp_path):
    manager = SaveManager(tmp_path, Path(world_service.session_factory.kw["bind"].url.database))
    world_service.save_manager = manager
    configure_world_service(world_service)
    app = FastAPI()
    app.include_router(world_router); app.include_router(npc_router)
    with TestClient(app) as client:
        assert set(client.get("/api/world").json()) == {
            "day", "weekday", "time", "label", "total_minutes", "paused", "speed", "locations",
        }
        assert set(client.get("/api/npcs/1").json()) == {
            "id", "name", "age", "job", "current_location", "current_action",
            "action_end_minute", "money", "states", "personality", "relationships",
        }
        assert client.get("/api/economy").json()["mode"] == "v0.5"
        assert client.get("/api/career-budget").json()["mode"] == "v0.6"
        assert client.get("/api/community-rhythm").json()["mode"] == "v0.7"
        assert client.get("/api/social-life").json()["mode"] == "v0.8"
        assert client.get("/api/life-story").json()["mode"] == "v0.9"
        assert client.get("/api/product").json()["mode"] == "v1.0"
        assert client.get("/api/world-statistics").json()["sources"]["tables"]["decision_mix"].startswith("decisions")
        assert client.get("/api/balance").json()["status"] == "healthy"
        assert len(client.get("/api/world-presets").json()) == 3
        assert client.get("/api/upgrade-reports").json()[0]["checks"]["old_schema_preserved"] is True


def test_existing_v09_database_adds_only_v10_tables_and_preserves_sql_and_facts(tmp_path):
    path = tmp_path / "upgrade.db"
    engine, sessions, _service_instance = _service(path)
    with sessions() as session:
        session.get(WorldState, 1).total_minutes = 98765
        session.get(NPC, 1).money = 432.1
        session.commit()
    for table in reversed(V10_TABLES):
        table.drop(engine)
    inspector = inspect(engine)
    old_tables = set(inspector.get_table_names())
    with engine.connect() as connection:
        old_sql = {
            name: connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).scalar_one()
            for name in old_tables
        }
    engine.dispose()

    upgraded_engine, upgraded_sessions = create_database(path)
    try:
        assert set(inspect(upgraded_engine).get_table_names()) - old_tables == V10_TABLE_NAMES
        with upgraded_engine.connect() as connection:
            assert all(
                connection.exec_driver_sql(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
                ).scalar_one() == sql
                for name, sql in old_sql.items()
            )
        upgraded = WorldService(upgraded_sessions)
        upgraded.initialize()
        with upgraded_sessions() as session:
            assert session.get(WorldState, 1).total_minutes == 98765
            assert session.get(NPC, 1).money == 432.1
            assert session.get(ProductState, 1).initialized_minute == 98765
    finally:
        upgraded_engine.dispose()


def test_disabled_initialization_and_periodic_fault_fall_back_to_exact_v09(tmp_path, monkeypatch):
    disabled_engine, disabled_sessions, disabled = _service(tmp_path / "disabled.db", product_enabled=False)
    fault_engine, fault_sessions, fault = _service(tmp_path / "fault.db")
    try:
        assert asyncio.run(disabled.productization_status())["mode"] == "v0.9-compatible"
        monkeypatch.setattr(
            "simulation.world.process_product_cycles",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected-v10-fault")),
        )
        for sessions in (disabled_sessions, fault_sessions):
            with sessions() as session:
                session.get(WorldState, 1).total_minutes = 1430
                session.commit()
        assert asyncio.run(disabled.tick()) and asyncio.run(fault.tick())
        assert _old_fact_snapshot(disabled_sessions) == _old_fact_snapshot(fault_sessions)
        with fault_sessions() as session:
            assert session.scalar(select(func.count()).select_from(WorldStatistic)) == 0
    finally:
        disabled_engine.dispose(); fault_engine.dispose()


def test_initialization_fault_keeps_v09_world_usable(tmp_path, monkeypatch):
    engine, sessions = create_database(tmp_path / "init-fault.db")
    monkeypatch.setattr(
        "simulation.world.ensure_product_data",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected-init-fault")),
    )
    service = WorldService(sessions)
    service.initialize()
    try:
        assert service.product_enabled is False
        assert asyncio.run(service.life_story_status())["mode"] == "v0.9"
        assert asyncio.run(service.tick()) is True
    finally:
        engine.dispose()


def test_missing_product_state_falls_back_without_touching_v09(world_service):
    with world_service.session_factory() as session:
        session.delete(session.get(ProductState, 1))
        session.commit()
    before = _old_fact_snapshot(world_service.session_factory)
    assert asyncio.run(world_service.productization_status())["mode"] == "v0.9-compatible"
    assert asyncio.run(world_service.world_statistics())["mode"] == "v0.9-compatible"
    assert asyncio.run(world_service.tick()) is True
    after = _old_fact_snapshot(world_service.session_factory)
    assert after["state"][0] == before["state"][0] + 10


def test_llm_cannot_modify_product_config_statistics_saves_or_audits(tmp_path):
    class OverreachingProvider:
        name = "overreaching-v10"

        async def generate(self, _kind, _context):
            return json.dumps({
                "title": "文字标题", "motivation": "只保留文字。", "money": 999999,
                "save_slot": "hijacked", "world_name": "Hacked", "statistics": {"npc_count": 999},
                "balance": "healthy", "config": {"seed": 0}, "facts": ["invented"],
            }, ensure_ascii=False)

    settings = NarrativeSettings(True, "fake", "https://example.invalid/v1", "test", 0.2)
    generator = NarrativeGenerator(settings, OverreachingProvider())
    engine, sessions, service = _service(tmp_path / "llm-v10.db", narrative_generator=generator)
    try:
        before_facts = _old_fact_snapshot(sessions)
        before_product = asyncio.run(service.productization_status())
        before_statistics = asyncio.run(service.world_statistics())
        asyncio.run(service.process_narrative_jobs(limit=1))
        assert _old_fact_snapshot(sessions) == before_facts
        assert asyncio.run(service.productization_status()) == before_product
        assert asyncio.run(service.world_statistics()) == before_statistics
    finally:
        engine.dispose()


def test_statistics_are_traceable_bounded_and_balance_has_explicit_guards(world_service):
    initial = asyncio.run(world_service.world_statistics())
    assert initial["sources"]["method"].startswith("deterministic")
    assert initial["metrics"]["decisions"]["window"] <= 500
    asyncio.run(world_service.run_ticks(144, commit_interval=72))
    with world_service.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(WorldStatistic)) == 1
        assert session.scalar(select(func.count()).select_from(BalanceAudit)) == 1
        session.get(NPC, 1).energy = 101
        session.commit()
    balance = asyncio.run(world_service.balance_status())
    assert balance["status"] == "critical"
    assert balance["thresholds"]["needs_range"] == [0.0, 100.0]
    assert balance["policy"].startswith("observe-and-guard")


def test_batch_ticks_use_same_fact_path_and_seed_as_individual_ticks(tmp_path):
    left_engine, left_sessions, left = _service(tmp_path / "individual.db")
    right_engine, right_sessions, right = _service(tmp_path / "batch.db")
    try:
        for _ in range(180):
            assert asyncio.run(left.tick())
        evidence = asyncio.run(right.run_ticks(180, commit_interval=60))
        assert evidence["fact_path"] == "full-engine-tick"
        assert evidence["ticks"] == 180
        assert _old_fact_snapshot(left_sessions) == _old_fact_snapshot(right_sessions)
        assert asyncio.run(left.replay_life_story()) == asyncio.run(right.replay_life_story())
    finally:
        left_engine.dispose(); right_engine.dispose()


def test_save_slots_are_isolated_and_never_overwrite_primary(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    primary = data / "world.db"
    primary_engine, _primary_sessions, _primary = _service(primary)
    primary_engine.dispose()
    before = primary.read_bytes()
    manager = SaveManager(tmp_path, primary)
    result = manager.create_slot(CreateSaveRequest(
        slot_id="career1",
        config=NewWorldConfig(world_name="职业档", preset="career_focus", seed=73),
    ))
    assert result["slot_id"] == "career1"
    assert primary.read_bytes() == before
    slot = manager.slot_path("career1")
    assert slot.exists() and slot != primary
    assert validate_database(slot, require_v10=True)["valid"]
    with pytest.raises(FileExistsError):
        manager.create_slot(CreateSaveRequest(slot_id="career1"))


def test_save_writer_ownership_rejects_two_loops(tmp_path):
    db_path = tmp_path / "owned.db"
    db_path.touch()
    first = SaveOwnership(db_path)
    second = SaveOwnership(db_path)
    first.claim()
    try:
        with pytest.raises(SaveOwnershipError):
            second.claim()
    finally:
        first.release()
    assert not first.lock_path.exists()
    second.claim(); second.release()


def test_export_import_is_validated_atomic_and_requires_new_target(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    primary = data / "world.db"
    engine, _sessions, _service_instance = _service(primary)
    engine.dispose()
    manager = SaveManager(tmp_path, primary)
    exported = manager.export_slot("primary")
    imported = manager.import_export(ImportSaveRequest(
        export_id=exported["export_id"], target_slot="restored",
    ))
    assert imported["slot_id"] == "restored"
    assert validate_database(manager.slot_path("restored"), require_v10=True)["valid"]
    with pytest.raises(FileExistsError):
        manager.import_export(ImportSaveRequest(
            export_id=exported["export_id"], target_slot="restored",
        ))
    with pytest.raises(ValidationError):
        ImportSaveRequest(export_id=exported["export_id"], target_slot="primary")


def test_malicious_or_corrupt_import_leaves_no_partial_target(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    primary = data / "world.db"
    engine, _sessions, _service_instance = _service(primary)
    engine.dispose()
    manager = SaveManager(tmp_path, primary)
    exported = manager.export_slot("primary")
    package = manager.export_path(exported["export_id"])
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("../escape", b"bad")
        archive.writestr("manifest.json", "{}")
        archive.writestr("world.db", b"not sqlite")
    with pytest.raises(ValueError):
        manager.import_export(ImportSaveRequest(
            export_id=exported["export_id"], target_slot="corrupt",
        ))
    assert not manager.slot_path("corrupt").exists()
    assert not (tmp_path / "escape").exists()


def test_onboarding_and_reset_are_product_only_and_auditable(world_service):
    before = _old_fact_snapshot(world_service.session_factory)
    request = OnboardingRequest(
        completed_steps=["manage_save", "observe", "observe"], dismissed=True,
    )
    result = asyncio.run(world_service.set_onboarding(request))
    assert result["completed_steps"] == ["observe", "manage_save"]
    assert _old_fact_snapshot(world_service.session_factory) == before
    asyncio.run(world_service.run_ticks(144, commit_interval=144))
    reset = asyncio.run(world_service.reset())
    assert reset["total_minutes"] == 480
    with world_service.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(WorldStatistic)) == 0
        assert session.scalar(select(func.count()).select_from(BalanceAudit)) == 0
        assert json.loads(session.get(OnboardingProgress, 1).completed_steps_json) == []
