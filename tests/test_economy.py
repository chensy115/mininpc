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
    EconomicTransaction,
    EmploymentProfile,
    Housing,
    InventoryItem,
    ItemDefinition,
    NPC,
    NPCSkill,
    Store,
    StoreListing,
    WorldState,
)
from simulation.actions import JOB_PAY, complete_action
from simulation.clock import ClockSnapshot
from simulation.economy import process_housing_costs
from simulation.narrative import NarrativeGenerator, NarrativeSettings
from simulation.random_service import RandomService
from simulation.world import WorldService


V05_MODELS_IN_DROP_ORDER = (
    EconomicTransaction,
    InventoryItem,
    StoreListing,
    NPCSkill,
    EmploymentProfile,
    Housing,
    ItemDefinition,
    Store,
)


def test_default_world_initializes_complete_economy_additively(world_service):
    with world_service.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(EmploymentProfile)) == 5
        assert session.scalar(select(func.count()).select_from(NPCSkill)) == 5
        assert session.scalar(select(func.count()).select_from(Housing)) == 5
        assert session.scalar(select(func.count()).select_from(Store)) == 1
        assert session.scalar(select(func.count()).select_from(ItemDefinition)) == 4
        assert session.scalar(select(func.count()).select_from(StoreListing)) == 4
        employment = session.scalar(select(EmploymentProfile).where(EmploymentProfile.npc_id == 1))
        assert employment.profession_key == "Designer"
        assert employment.base_wage == JOB_PAY["Designer"]


def test_work_pays_wage_and_advances_performance_and_skill(world_service):
    with world_service.session_factory() as session:
        npc = session.get(NPC, 1)
        employment = session.scalar(select(EmploymentProfile).where(EmploymentProfile.npc_id == npc.id))
        skill = session.scalar(select(NPCSkill).where(NPCSkill.npc_id == npc.id))
        before = (npc.money, employment.performance, employment.shifts_completed, skill.experience)
        npc.current_action = "Work"
        npc.current_location = "Office"
        complete_action(session, npc, ClockSnapshot(600), RandomService(42))
        session.commit()
        transaction = session.scalar(
            select(EconomicTransaction).where(
                EconomicTransaction.npc_id == npc.id,
                EconomicTransaction.kind == "wage",
            )
        )
        assert npc.money > before[0]
        assert employment.performance != before[1]
        assert employment.shifts_completed == before[2] + 1
        assert skill.experience > before[3]
        assert transaction.amount > 0 and transaction.balance_after == npc.money


def test_shop_inventory_and_item_use_form_a_persistent_loop(world_service):
    with world_service.session_factory() as session:
        npc = session.get(NPC, 1)
        npc.current_location = "Cafe"
        npc.current_action = "Shop"
        npc.hunger = 20
        before_money = npc.money
        complete_action(session, npc, ClockSnapshot(620), RandomService(42))
        session.flush()
        inventory = session.scalar(
            select(InventoryItem).where(InventoryItem.npc_id == npc.id, InventoryItem.quantity > 0)
        )
        assert inventory is not None and npc.money < before_money
        skill = session.scalar(select(NPCSkill).where(NPCSkill.npc_id == npc.id))
        before_experience = skill.experience
        npc.current_action = "UseItem"
        complete_action(session, npc, ClockSnapshot(640), RandomService(42))
        session.commit()
        assert inventory.quantity == 0
        assert skill.experience > before_experience
        assert session.scalar(
            select(func.count()).select_from(EconomicTransaction).where(
                EconomicTransaction.npc_id == npc.id
            )
        ) == 2


def test_eat_consumes_owned_meal_without_charging_twice(world_service):
    with world_service.session_factory() as session:
        npc = session.get(NPC, 1)
        meal = session.scalar(select(ItemDefinition).where(ItemDefinition.item_key == "prepared_meal"))
        inventory = InventoryItem(npc_id=npc.id, item_id=meal.id, quantity=1)
        session.add(inventory)
        session.flush()
        before_money = npc.money
        npc.current_action = "Eat"
        npc.current_location = "Home"
        complete_action(session, npc, ClockSnapshot(660), RandomService(42))
        session.commit()
        assert inventory.quantity == 0
        assert npc.money == before_money
        transaction = session.scalar(
            select(EconomicTransaction).where(EconomicTransaction.npc_id == npc.id)
        )
        assert transaction.kind == "consume" and transaction.amount == 0


def test_housing_rent_is_due_deterministically_and_tracks_arrears(world_service):
    with world_service.session_factory() as session:
        npc = session.get(NPC, 1)
        housing = session.scalar(select(Housing).where(Housing.npc_id == npc.id))
        housing.next_rent_minute = 700
        npc.money = 5.0
        processed = process_housing_costs(session, [npc], ClockSnapshot(700))
        session.commit()
        assert processed == 1
        assert npc.money == 0
        assert housing.arrears == housing.weekly_rent - 5
        assert housing.next_rent_minute == 700 + 7 * 24 * 60
        rent = session.scalar(select(EconomicTransaction).where(EconomicTransaction.kind == "rent"))
        assert rent.amount == -5


def test_v05_apis_are_additive_and_preserve_all_old_exact_shapes(world_service):
    configure_world_service(world_service)
    api = FastAPI()
    api.include_router(world_router)
    api.include_router(npc_router)
    with TestClient(api) as client:
        assert client.get("/api/economy").json()["mode"] == "v0.5"
        assert len(client.get("/api/professions").json()) == 5
        assert len(client.get("/api/stores").json()[0]["items"]) == 4
        economy = client.get("/api/npcs/1/economy")
        assert economy.status_code == 200
        assert economy.json()["employment"]["profession_key"] == "Designer"
        assert client.get("/api/npcs/999/economy").status_code == 404
        assert set(client.get("/api/world").json()) == {
            "day", "weekday", "time", "label", "total_minutes", "paused", "speed", "locations"
        }
        assert set(client.get("/api/npcs/1").json()) == {
            "id", "name", "age", "job", "current_location", "current_action",
            "action_end_minute", "money", "states", "personality", "relationships",
        }


def test_existing_v04_database_gets_only_new_tables_and_preserves_facts(tmp_path):
    path = tmp_path / "v04-world.db"
    engine, sessions = create_database(path)
    service = WorldService(sessions)
    service.initialize()
    with sessions() as session:
        session.get(WorldState, 1).total_minutes = 4321
        session.get(NPC, 1).money = 321.5
        session.commit()
    for model in V05_MODELS_IN_DROP_ORDER:
        model.__table__.drop(engine)
    old_tables = set(inspect(engine).get_table_names())
    engine.dispose()

    upgraded_engine, upgraded_sessions = create_database(path)
    try:
        upgraded = WorldService(upgraded_sessions)
        upgraded.initialize()
        new_tables = set(inspect(upgraded_engine).get_table_names())
        assert new_tables - old_tables == {model.__tablename__ for model in V05_MODELS_IN_DROP_ORDER}
        with upgraded_sessions() as session:
            assert session.get(WorldState, 1).total_minutes == 4321
            assert session.get(NPC, 1).money == 321.5
            assert session.scalar(select(func.count()).select_from(EmploymentProfile)) == 5
    finally:
        upgraded_engine.dispose()


def test_economy_can_be_disabled_with_exact_legacy_work_fallback(tmp_path):
    engine, sessions = create_database(tmp_path / "disabled.db")
    try:
        service = WorldService(sessions, economy_enabled=False)
        service.initialize()
        with sessions() as session:
            assert session.scalar(select(func.count()).select_from(EmploymentProfile)) == 0
            npc = session.get(NPC, 1)
            npc.current_action = "Work"
            npc.current_location = "Office"
            before = npc.money
            complete_action(session, npc, ClockSnapshot(600), RandomService(42), economy_enabled=False)
            assert npc.money - before == JOB_PAY[npc.job]
            assert session.scalar(select(func.count()).select_from(EconomicTransaction)) == 0
        assert asyncio.run(service.economy_status()) == {
            "enabled": False, "mode": "legacy", "stores": 0, "items": 0, "transactions": 0
        }
    finally:
        engine.dispose()


def test_missing_employment_profile_falls_back_without_stopping_simulation(world_service):
    with world_service.session_factory() as session:
        profile = session.scalar(select(EmploymentProfile).where(EmploymentProfile.npc_id == 1))
        session.delete(profile)
        session.commit()
        npc = session.get(NPC, 1)
        before = npc.money
        npc.current_action = "Work"
        npc.current_location = "Office"
        complete_action(session, npc, ClockSnapshot(600), RandomService(42))
        session.commit()
        assert npc.money - before == JOB_PAY[npc.job]
        assert session.scalar(
            select(func.count()).select_from(EconomicTransaction).where(EconomicTransaction.npc_id == npc.id)
        ) == 0


def _economy_fact_snapshot(service: WorldService):
    with service.session_factory() as session:
        state = session.get(WorldState, 1)
        npcs = [(npc.id, npc.current_action, npc.current_location, npc.money) for npc in session.scalars(select(NPC).order_by(NPC.id))]
        employment = [
            (item.npc_id, item.performance, item.experience, item.shifts_completed, item.total_earnings)
            for item in session.scalars(select(EmploymentProfile).order_by(EmploymentProfile.npc_id))
        ]
        skills = [(item.npc_id, item.skill_key, item.level, item.experience) for item in session.scalars(select(NPCSkill).order_by(NPCSkill.id))]
        inventory = [(item.npc_id, item.item_id, item.quantity) for item in session.scalars(select(InventoryItem).order_by(InventoryItem.id))]
        housing = [(item.npc_id, item.comfort, item.arrears, item.next_rent_minute) for item in session.scalars(select(Housing).order_by(Housing.npc_id))]
        transactions = [(item.npc_id, item.world_minute, item.kind, item.amount, item.balance_after, item.item_id) for item in session.scalars(select(EconomicTransaction).order_by(EconomicTransaction.id))]
        return state.total_minutes, state.random_counter, npcs, employment, skills, inventory, housing, transactions


def test_narrative_processing_cannot_mutate_economic_facts(tmp_path):
    class OverreachingProvider:
        name = "overreaching-economy-provider"

        async def generate(self, kind, context):
            return json.dumps({
                "title": "职业目标", "motivation": "继续努力。", "money": 999999,
                "wage": 9999, "performance": 100, "inventory": ["everything"], "rent": 0,
            }, ensure_ascii=False)

    generator = NarrativeGenerator(
        NarrativeSettings(True, "test", "https://example.invalid/v1", "test", 0.2),
        OverreachingProvider(),
    )
    engine, sessions = create_database(tmp_path / "isolation.db")
    try:
        service = WorldService(sessions, generator)
        service.initialize()
        before = _economy_fact_snapshot(service)
        asyncio.run(service.process_narrative_jobs(limit=100))
        assert _economy_fact_snapshot(service) == before
    finally:
        engine.dispose()


def test_economy_is_randomly_reproducible_with_and_without_narrative_worker(tmp_path):
    engine_a, sessions_a = create_database(tmp_path / "a.db")
    engine_b, sessions_b = create_database(tmp_path / "b.db")
    service_a, service_b = WorldService(sessions_a), WorldService(sessions_b)
    service_a.initialize()
    service_b.initialize()
    try:
        async def run():
            for tick in range(60):
                assert await service_a.tick()
                assert await service_b.tick()
                if tick % 15 == 0:
                    await service_a.process_narrative_jobs(limit=100)

        asyncio.run(run())
        assert _economy_fact_snapshot(service_a) == _economy_fact_snapshot(service_b)
    finally:
        engine_a.dispose()
        engine_b.dispose()


def test_two_day_simulation_closes_income_consumption_and_inventory_loop(world_service):
    async def run_two_days():
        for _ in range(2 * 24 * 6):
            assert await world_service.tick()

    asyncio.run(run_two_days())
    with world_service.session_factory() as session:
        kinds = set(session.scalars(select(EconomicTransaction.kind)))
        assert "wage" in kinds
        assert "purchase" in kinds
        assert "consume" in kinds
        assert session.scalar(select(func.sum(EmploymentProfile.shifts_completed))) > 0
        assert session.scalar(select(func.count()).select_from(EconomicTransaction)) > 20
