from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select

from api.dependencies import configure_world_service
from api.npc import router as npc_router
from api.world import router as world_router
from database.database import create_database
from database.models import (
    CommunityInstitution,
    FacilityUsage,
    Housing,
    HousingUpgradeRecord,
    NPC,
    RestockEvent,
    StoreStock,
    TrainingRecord,
    WorkAttendance,
    WorkSchedule,
    WorldState,
)
from simulation.actions import complete_action
from simulation.clock import ClockSnapshot
from simulation.community import (
    DAY_MINUTES,
    TRAINING_FEE,
    complete_facility_service,
    complete_housing_upgrade,
    complete_training,
    community_context,
    process_restocking,
)
from simulation.decision import decide
from simulation.narrative import NarrativeGenerator, NarrativeSettings
from simulation.random_service import RandomService
from simulation.world import WorldService


V07_MODELS_IN_DROP_ORDER = (
    HousingUpgradeRecord,
    TrainingRecord,
    FacilityUsage,
    RestockEvent,
    StoreStock,
    WorkAttendance,
    WorkSchedule,
    CommunityInstitution,
)


def test_v07_initializes_only_fixed_institutions_schedules_and_stock(world_service):
    with world_service.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(CommunityInstitution)) == 4
        assert session.scalar(select(func.count()).select_from(WorkSchedule)) == 5
        assert session.scalar(select(func.count()).select_from(StoreStock)) == 4
        assert session.scalar(select(func.count()).select_from(WorkAttendance)) == 0
        locations = set(session.scalars(select(CommunityInstitution.location)))
        assert locations <= {"Home", "Office", "Cafe", "Park"}
        assert all(stock.quantity == stock.capacity for stock in session.scalars(select(StoreStock)))


def test_business_hours_and_weekend_rhythm_gate_utility_actions(world_service):
    with world_service.session_factory() as session:
        npc = session.get(NPC, 1)
        npc.current_location = "Cafe"
        monday_closed = community_context(session, npc, ClockSnapshot(6 * 60))
        monday_open = community_context(session, npc, ClockSnapshot(8 * 60))
        assert monday_closed["store_open"] is False
        assert monday_open["store_open"] is True

        weekend_clock = ClockSnapshot(5 * DAY_MINUTES + 10 * 60)
        npc.current_location = "Office"
        rhythm = community_context(session, npc, weekend_clock)
        occupants = {key: [] for key in ("Home", "Office", "Cafe", "Park")}
        occupants["Office"] = [npc]
        decision = decide(npc, weekend_clock, occupants, RandomService(7), community_context=rhythm)
        candidates = {candidate.action: candidate for candidate in decision.candidates}
        assert rhythm["is_weekend"] is True
        assert candidates["Work"].available is False
        assert candidates["Train"].available is True
        assert candidates["Relax"].contributions["周末休闲节奏"] == 16


def test_work_schedule_records_lateness_and_completed_shift(world_service):
    with world_service.session_factory() as session:
        npc = session.get(NPC, 1)
        npc.current_location = "Office"
        npc.current_action = "Work"
        complete_action(session, npc, ClockSnapshot(10 * 60), RandomService(2), community_enabled=True)
        npc.current_action = "Work"
        complete_action(session, npc, ClockSnapshot(11 * 60), RandomService(3), community_enabled=True)
        session.commit()
        attendance = session.scalar(select(WorkAttendance).where(WorkAttendance.npc_id == 1))
        schedule = session.scalar(select(WorkSchedule).where(WorkSchedule.npc_id == 1))
        assert attendance.status == "late"
        assert attendance.minutes_late == 60
        assert attendance.worked_minutes == 120
        assert schedule.late_days == 1 and schedule.shifts_completed == 1


def test_stock_is_consumed_and_restocked_on_fixed_daily_cycle(world_service):
    with world_service.session_factory() as session:
        npc = session.get(NPC, 1)
        npc.current_location = "Cafe"
        npc.current_action = "Shop"
        npc.hunger = 80
        stocks = list(session.scalars(select(StoreStock).order_by(StoreStock.id)))
        for stock in stocks:
            stock.quantity = 0
        stocks[0].quantity = 1
        due = stocks[0].next_restock_minute
        before_money = npc.money
        complete_action(session, npc, ClockSnapshot(12 * 60), RandomService(4), community_enabled=True)
        assert stocks[0].quantity == 0 and npc.money < before_money
        process_restocking(session, ClockSnapshot(due))
        session.commit()
        assert stocks[0].quantity == stocks[0].restock_amount
        event = session.scalar(select(RestockEvent).where(RestockEvent.stock_id == stocks[0].id))
        assert event.world_minute == due and event.quantity_added == stocks[0].restock_amount


def test_limited_facility_service_enforces_daily_capacity(world_service):
    with world_service.session_factory() as session:
        institution = session.scalar(
            select(CommunityInstitution).where(CommunityInstitution.institution_key == "park_wellness")
        )
        institution.daily_capacity = 1
        first = session.get(NPC, 1)
        second = session.get(NPC, 2)
        first.current_location = second.current_location = "Park"
        clock = ClockSnapshot(18 * 60)
        assert complete_facility_service(session, first, clock) is not None
        assert complete_facility_service(session, first, clock) is None
        assert complete_facility_service(session, second, clock) is None
        session.commit()
        assert session.scalar(select(func.count()).select_from(FacilityUsage)) == 1


def test_training_is_bounded_audited_and_advances_engine_facts(world_service):
    with world_service.session_factory() as session:
        npc = session.get(NPC, 1)
        npc.current_location = "Office"
        before_money = npc.money
        result = complete_training(session, npc, ClockSnapshot(19 * 60 + 30))
        session.commit()
        assert result is not None and result["skill_experience"] == 30
        assert npc.money == before_money - TRAINING_FEE
        record = session.scalar(select(TrainingRecord).where(TrainingRecord.npc_id == 1))
        assert record.fee == TRAINING_FEE and record.profession_key == npc.job


def test_housing_upgrade_changes_only_owned_housing_and_is_audited(world_service):
    with world_service.session_factory() as session:
        npc = session.get(NPC, 1)
        npc.current_location = "Home"
        npc.money = 1000
        housing = session.scalar(select(Housing).where(Housing.npc_id == 1))
        result = complete_housing_upgrade(session, npc, ClockSnapshot(10 * 60 + 30))
        session.commit()
        assert result == {"tier_before": "standard", "tier_after": "improved", "cost": 160.0}
        assert housing.tier == "improved" and housing.comfort >= 76
        assert session.scalar(select(func.count()).select_from(HousingUpgradeRecord)) == 1


def test_v07_apis_are_additive_and_old_exact_shapes_remain_unchanged(world_service):
    configure_world_service(world_service)
    api = FastAPI()
    api.include_router(world_router)
    api.include_router(npc_router)
    with TestClient(api) as client:
        assert client.get("/api/economy").json()["mode"] == "v0.5"
        assert client.get("/api/career-budget").json()["mode"] == "v0.6"
        assert client.get("/api/community-rhythm").json() == {
            "enabled": True, "mode": "v0.7", "institutions": 4,
            "schedules": 5, "stock_items": 4, "training_records": 0,
            "housing_upgrades": 0,
        }
        assert len(client.get("/api/institutions").json()) == 4
        assert len(client.get("/api/store-stock").json()) == 4
        assert client.get("/api/npcs/1/rhythm").json()["mode"] == "v0.7"
        assert client.get("/api/npcs/999/rhythm").status_code == 404
        assert set(client.get("/api/world").json()) == {
            "day", "weekday", "time", "label", "total_minutes", "paused", "speed", "locations"
        }
        assert set(client.get("/api/npcs/1").json()) == {
            "id", "name", "age", "job", "current_location", "current_action",
            "action_end_minute", "money", "states", "personality", "relationships",
        }


def test_existing_v06_database_adds_only_v07_tables_and_preserves_facts(tmp_path):
    path = tmp_path / "v06-world.db"
    engine, sessions = create_database(path)
    service = WorldService(sessions)
    service.initialize()
    with sessions() as session:
        session.get(WorldState, 1).total_minutes = 54321
        session.get(NPC, 1).money = 654.3
        session.commit()
    for model in V07_MODELS_IN_DROP_ORDER:
        model.__table__.drop(engine)
    old_tables = set(inspect(engine).get_table_names())
    engine.dispose()

    upgraded_engine, upgraded_sessions = create_database(path)
    try:
        upgraded = WorldService(upgraded_sessions)
        upgraded.initialize()
        assert set(inspect(upgraded_engine).get_table_names()) - old_tables == {
            model.__tablename__ for model in V07_MODELS_IN_DROP_ORDER
        }
        with upgraded_sessions() as session:
            assert session.get(WorldState, 1).total_minutes == 54321
            assert session.get(NPC, 1).money == 654.3
            assert session.scalar(select(func.count()).select_from(WorkAttendance)) == 0
            assert session.scalar(select(func.count()).select_from(TrainingRecord)) == 0
            assert session.scalar(select(func.count()).select_from(HousingUpgradeRecord)) == 0
    finally:
        upgraded_engine.dispose()


def test_v07_disabled_and_missing_schedule_fall_back_safely(tmp_path):
    engine, sessions = create_database(tmp_path / "disabled.db")
    try:
        service = WorldService(sessions, community_enabled=False)
        service.initialize()
        assert asyncio.run(service.community_status()) == {
            "enabled": False, "mode": "v0.6-compatible", "institutions": 0,
            "schedules": 0, "stock_items": 0, "training_records": 0,
            "housing_upgrades": 0,
        }
        with sessions() as session:
            assert session.scalar(select(func.count()).select_from(CommunityInstitution)) == 0
        assert asyncio.run(service.tick())
    finally:
        engine.dispose()

    engine, sessions = create_database(tmp_path / "missing.db")
    try:
        service = WorldService(sessions)
        service.initialize()
        with sessions() as session:
            session.delete(session.scalar(select(WorkSchedule).where(WorkSchedule.npc_id == 1)))
            session.commit()
        assert asyncio.run(service.tick())
        assert asyncio.run(service.get_npc_rhythm(1)) == {
            "enabled": False, "mode": "v0.6-compatible", "npc_id": 1, "npc_name": "Alice"
        }
    finally:
        engine.dispose()


def test_reset_removes_v07_history_and_restores_defaults(world_service):
    with world_service.session_factory() as session:
        npc = session.get(NPC, 1)
        npc.current_location = "Office"
        complete_training(session, npc, ClockSnapshot(19 * 60 + 30))
        session.commit()
        assert session.scalar(select(func.count()).select_from(TrainingRecord)) == 1
    asyncio.run(world_service.reset())
    with world_service.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(TrainingRecord)) == 0
        assert session.scalar(select(func.count()).select_from(WorkAttendance)) == 0
        assert session.scalar(select(func.count()).select_from(WorkSchedule)) == 5
        assert session.scalar(select(func.count()).select_from(StoreStock)) == 4


def _v07_fact_snapshot(service: WorldService):
    with service.session_factory() as session:
        return (
            [(row.npc_id, row.on_time_days, row.late_days, row.shifts_completed)
             for row in session.scalars(select(WorkSchedule).order_by(WorkSchedule.npc_id))],
            [(row.listing_id, row.quantity, row.last_restock_minute, row.next_restock_minute)
             for row in session.scalars(select(StoreStock).order_by(StoreStock.listing_id))],
            [(row.npc_id, row.world_minute, row.skill_experience)
             for row in session.scalars(select(TrainingRecord).order_by(TrainingRecord.id))],
            [(row.npc_id, row.tier_before, row.tier_after, row.cost)
             for row in session.scalars(select(HousingUpgradeRecord).order_by(HousingUpgradeRecord.id))],
        )


def test_llm_text_cannot_mutate_v07_facts(tmp_path):
    class OverreachingProvider:
        name = "overreaching-v07-provider"

        async def generate(self, kind, context):
            return json.dumps({
                "title": "全天营业并无限补货", "motivation": "升级豪宅",
                "stock": 999, "housing_tier": "palace", "late_days": 0,
                "training": {"skill_experience": 9999},
            }, ensure_ascii=False)

    generator = NarrativeGenerator(
        NarrativeSettings(True, "test", "https://example.invalid/v1", "test", 0.2),
        OverreachingProvider(),
    )
    engine, sessions = create_database(tmp_path / "isolation.db")
    try:
        service = WorldService(sessions, generator)
        service.initialize()
        before = _v07_fact_snapshot(service)
        asyncio.run(service.process_narrative_jobs(limit=100))
        assert _v07_fact_snapshot(service) == before
    finally:
        engine.dispose()


def test_cross_week_restock_and_rhythm_are_reproducible(tmp_path):
    engine_a, sessions_a = create_database(tmp_path / "a.db")
    engine_b, sessions_b = create_database(tmp_path / "b.db")
    services = (WorldService(sessions_a), WorldService(sessions_b))
    for service in services:
        service.initialize()
    try:
        for service in services:
            with service.session_factory() as session:
                stocks = list(session.scalars(select(StoreStock).order_by(StoreStock.id)))
                for index, stock in enumerate(stocks):
                    stock.quantity = index
                state = session.get(WorldState, 1)
                state.total_minutes = 8 * DAY_MINUTES + 12 * 60
                process_restocking(session, ClockSnapshot(state.total_minutes))
                session.commit()
        assert _v07_fact_snapshot(services[0]) == _v07_fact_snapshot(services[1])
        with sessions_a() as session:
            assert session.scalar(select(func.count()).select_from(RestockEvent)) == 32
            monday = community_context(session, session.get(NPC, 1), ClockSnapshot(7 * DAY_MINUTES + 9 * 60))
            weekend = community_context(session, session.get(NPC, 1), ClockSnapshot(5 * DAY_MINUTES + 9 * 60))
            assert monday["on_workday"] is True and weekend["is_weekend"] is True
    finally:
        engine_a.dispose()
        engine_b.dispose()
