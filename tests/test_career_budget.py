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
    CareerDevelopment,
    CareerTransition,
    EconomicTransaction,
    EmploymentProfile,
    NPC,
    PerformanceReview,
    PersonalBudget,
    WeeklyEconomicReport,
    WorldState,
)
from simulation.career_budget import (
    complete_job_search,
    process_career_budget_cycles,
)
from simulation.clock import ClockSnapshot
from simulation.decision import decide
from simulation.economy import PROFESSIONS, WEEK_MINUTES, add_transaction
from simulation.narrative import NarrativeGenerator, NarrativeSettings
from simulation.random_service import RandomService
from simulation.world import WorldService


V06_MODELS_IN_DROP_ORDER = (
    WeeklyEconomicReport,
    PerformanceReview,
    CareerTransition,
    PersonalBudget,
    CareerDevelopment,
)


class AlwaysLowRandom:
    counter = 0

    def uniform(self, _low, _high):
        self.counter += 1
        return 0.0

    def randint(self, low, _high):
        self.counter += 1
        return low


def test_v06_initializes_additive_career_and_budget_records(world_service):
    with world_service.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(CareerDevelopment)) == 5
        assert session.scalar(select(func.count()).select_from(PersonalBudget)) == 5
        career = session.scalar(select(CareerDevelopment).where(CareerDevelopment.npc_id == 1))
        budget = session.scalar(select(PersonalBudget).where(PersonalBudget.npc_id == 1))
        assert career.employment_status == "employed"
        assert career.next_review_minute == WEEK_MINUTES
        assert all(getattr(budget, f"{key}_budget") > 0 for key in (
            "food", "housing", "learning", "entertainment", "savings"
        ))


def test_two_strong_periodic_reviews_explain_raise_then_promotion(world_service):
    with world_service.session_factory() as session:
        npc = session.get(NPC, 1)
        employment = session.scalar(select(EmploymentProfile).where(EmploymentProfile.npc_id == 1))
        career = session.scalar(select(CareerDevelopment).where(CareerDevelopment.npc_id == 1))
        employment.performance = 100
        npc.work_satisfaction = 100
        wage_before = employment.base_wage
        process_career_budget_cycles(session, [npc], ClockSnapshot(WEEK_MINUTES), RandomService(42))
        assert career.career_level == 1
        assert employment.base_wage > wage_before
        first = session.scalar(select(PerformanceReview).where(PerformanceReview.npc_id == 1))
        assert first.outcome == "raise"
        assert len(json.loads(first.reasons_json)) >= 4
        process_career_budget_cycles(session, [npc], ClockSnapshot(2 * WEEK_MINUTES), RandomService(42, 3))
        session.commit()
        reviews = list(session.scalars(select(PerformanceReview).where(
            PerformanceReview.npc_id == 1
        ).order_by(PerformanceReview.id)))
        assert [row.outcome for row in reviews] == ["raise", "promotion"]
        assert career.career_level == 2
        assert any("连续两次" in reason for reason in json.loads(reviews[-1].reasons_json))


def test_unemployment_risk_has_reasons_probability_and_safe_worker_floor(world_service):
    with world_service.session_factory() as session:
        for npc_id in (1, 2):
            npc = session.get(NPC, npc_id)
            npc.work_satisfaction = 0
            employment = session.scalar(select(EmploymentProfile).where(EmploymentProfile.npc_id == npc_id))
            career = session.scalar(select(CareerDevelopment).where(CareerDevelopment.npc_id == npc_id))
            employment.performance = 0
            career.weak_reviews = 1
            process_career_budget_cycles(session, [npc], ClockSnapshot(WEEK_MINUTES), AlwaysLowRandom())
            assert career.employment_status == "unemployed"
        npc = session.get(NPC, 3)
        npc.work_satisfaction = 0
        employment = session.scalar(select(EmploymentProfile).where(EmploymentProfile.npc_id == 3))
        career = session.scalar(select(CareerDevelopment).where(CareerDevelopment.npc_id == 3))
        employment.performance = 0
        career.weak_reviews = 1
        process_career_budget_cycles(session, [npc], ClockSnapshot(WEEK_MINUTES), AlwaysLowRandom())
        review = session.scalar(select(PerformanceReview).where(PerformanceReview.npc_id == 3))
        session.commit()
        assert career.employment_status == "employed"
        assert "安全就业下限 3 人" in " ".join(json.loads(review.reasons_json))
        assert session.scalar(select(func.count()).select_from(CareerDevelopment).where(
            CareerDevelopment.employment_status == "employed"
        )) == 3


def test_job_search_changes_only_to_existing_profession_and_is_audited(world_service):
    with world_service.session_factory() as session:
        npc = session.get(NPC, 1)
        career = session.scalar(select(CareerDevelopment).where(CareerDevelopment.npc_id == 1))
        career.employment_status = "unemployed"
        career.unemployment_since_minute = 900
        before = npc.job
        result = complete_job_search(session, npc, ClockSnapshot(1000), AlwaysLowRandom())
        session.commit()
        transition = session.scalar(select(CareerTransition).where(CareerTransition.npc_id == 1))
        assert result["success"] is True
        assert npc.job in PROFESSIONS and npc.job != before
        assert transition.from_profession == before and transition.to_profession == npc.job
        assert "既有职业集合" in transition.reason
        assert career.employment_status == "employed"


def test_weekly_budget_report_tracks_categories_disposable_income_and_pressure(world_service):
    with world_service.session_factory() as session:
        npc = session.get(NPC, 1)
        npc.money += 80
        add_transaction(session, npc, ClockSnapshot(600), "wage", 80, "测试工资")
        npc.money -= 8
        add_transaction(session, npc, ClockSnapshot(700), "purchase", -8, "测试餐费", item_id=1)
        process_career_budget_cycles(session, [npc], ClockSnapshot(WEEK_MINUTES), RandomService(7))
        session.commit()
        report = session.scalar(select(WeeklyEconomicReport).where(WeeklyEconomicReport.npc_id == 1))
        assert report.income == 80
        assert report.food_spent == 8
        assert report.disposable_income == 72
        assert report.saved == 72
        assert 0 <= report.economic_pressure <= 100
        assert json.loads(report.reasons_json)


def test_budget_and_career_facts_enter_utility_ai_only_when_available(world_service):
    with world_service.session_factory() as session:
        npc = session.get(NPC, 1)
        occupants = {key: [] for key in ("Home", "Office", "Cafe", "Park")}
        occupants[npc.current_location] = [npc]
        context = {
            "enabled": True, "employment_status": "unemployed", "economic_pressure": 70,
            "disposable_income": -20, "budget_remaining": {"food": 20, "learning": 0, "housing": 0},
            "job_search_needed": True, "job_search_reason": "当前待业，需要在既有职业中求职",
        }
        decision = decide(npc, ClockSnapshot(600), occupants, RandomService(42), None, None, context)
        candidates = {row.action: row for row in decision.candidates}
        assert candidates["Work"].available is False
        assert candidates["JobSearch"].available is True
        assert "经济压力" in candidates["JobSearch"].contributions
        legacy = decide(npc, ClockSnapshot(600), occupants, RandomService(42))
        assert "JobSearch" not in {row.action for row in legacy.candidates}


def test_v06_apis_are_additive_and_old_exact_shapes_remain_unchanged(world_service):
    configure_world_service(world_service)
    api = FastAPI()
    api.include_router(world_router)
    api.include_router(npc_router)
    with TestClient(api) as client:
        assert client.get("/api/economy").json()["mode"] == "v0.5"
        assert client.get("/api/career-budget").json() == {
            "enabled": True, "mode": "v0.6", "careers": 5, "budgets": 5, "reports": 0
        }
        career = client.get("/api/npcs/1/career")
        budget = client.get("/api/npcs/1/budget")
        assert career.status_code == 200 and career.json()["mode"] == "v0.6"
        assert set(budget.json()["allocations"]) == {"food", "housing", "learning", "entertainment", "savings"}
        assert client.get("/api/npcs/999/career").status_code == 404
        assert client.get("/api/npcs/999/budget").status_code == 404
        assert set(client.get("/api/world").json()) == {
            "day", "weekday", "time", "label", "total_minutes", "paused", "speed", "locations"
        }
        assert set(client.get("/api/npcs/1").json()) == {
            "id", "name", "age", "job", "current_location", "current_action",
            "action_end_minute", "money", "states", "personality", "relationships",
        }


def test_existing_v05_database_adds_only_v06_tables_and_preserves_facts(tmp_path):
    path = tmp_path / "v05-world.db"
    engine, sessions = create_database(path)
    service = WorldService(sessions)
    service.initialize()
    with sessions() as session:
        session.get(WorldState, 1).total_minutes = 5432
        session.get(NPC, 1).money = 432.1
        session.commit()
    for model in V06_MODELS_IN_DROP_ORDER:
        model.__table__.drop(engine)
    old_tables = set(inspect(engine).get_table_names())
    engine.dispose()
    upgraded_engine, upgraded_sessions = create_database(path)
    try:
        upgraded = WorldService(upgraded_sessions)
        upgraded.initialize()
        assert set(inspect(upgraded_engine).get_table_names()) - old_tables == {
            model.__tablename__ for model in V06_MODELS_IN_DROP_ORDER
        }
        with upgraded_sessions() as session:
            assert session.get(WorldState, 1).total_minutes == 5432
            assert session.get(NPC, 1).money == 432.1
            assert session.scalar(select(func.count()).select_from(CareerDevelopment)) == 5
    finally:
        upgraded_engine.dispose()


def test_v06_disabled_and_missing_profile_fall_back_safely(tmp_path):
    engine, sessions = create_database(tmp_path / "disabled.db")
    try:
        service = WorldService(sessions, career_budget_enabled=False)
        service.initialize()
        assert asyncio.run(service.career_budget_status()) == {
            "enabled": False, "mode": "v0.5-compatible", "careers": 0, "budgets": 0, "reports": 0
        }
        with sessions() as session:
            assert session.scalar(select(func.count()).select_from(CareerDevelopment)) == 0
        assert asyncio.run(service.tick())
    finally:
        engine.dispose()

    engine, sessions = create_database(tmp_path / "missing.db")
    try:
        service = WorldService(sessions)
        service.initialize()
        with sessions() as session:
            session.delete(session.scalar(select(PersonalBudget).where(PersonalBudget.npc_id == 1)))
            session.commit()
        assert asyncio.run(service.tick())
        snapshot = asyncio.run(service.get_npc_budget(1))
        assert snapshot == {"enabled": False, "mode": "v0.5-compatible", "npc_id": 1, "npc_name": "Alice"}
    finally:
        engine.dispose()


def test_reset_removes_v06_history_and_restores_defaults(world_service):
    with world_service.session_factory() as session:
        npc = session.get(NPC, 1)
        process_career_budget_cycles(session, [npc], ClockSnapshot(WEEK_MINUTES), RandomService(9))
        session.commit()
        assert session.scalar(select(func.count()).select_from(PerformanceReview)) == 1
        assert session.scalar(select(func.count()).select_from(WeeklyEconomicReport)) == 1
    asyncio.run(world_service.reset())
    with world_service.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(PerformanceReview)) == 0
        assert session.scalar(select(func.count()).select_from(WeeklyEconomicReport)) == 0
        assert session.scalar(select(func.count()).select_from(CareerDevelopment)) == 5


def _v06_fact_snapshot(service: WorldService):
    with service.session_factory() as session:
        return (
            [(x.npc_id, x.employment_status, x.career_level, x.reviews_completed, x.applications_submitted)
             for x in session.scalars(select(CareerDevelopment).order_by(CareerDevelopment.npc_id))],
            [(x.npc_id, x.period_start_minute, x.food_budget, x.savings_budget)
             for x in session.scalars(select(PersonalBudget).order_by(PersonalBudget.npc_id))],
            [(x.npc_id, x.world_minute, x.score, x.outcome, x.wage_after)
             for x in session.scalars(select(PerformanceReview).order_by(PerformanceReview.id))],
            [(x.npc_id, x.period_end_minute, x.income, x.saved, x.economic_pressure)
             for x in session.scalars(select(WeeklyEconomicReport).order_by(WeeklyEconomicReport.id))],
        )


def test_llm_text_processing_cannot_mutate_v06_facts(tmp_path):
    class OverreachingProvider:
        name = "overreaching-v06-provider"

        async def generate(self, kind, context):
            return json.dumps({
                "title": "立即晋升", "motivation": "把预算清零", "career_level": 5,
                "budget": {"food": 0}, "employment_status": "unemployed", "performance": 100,
            }, ensure_ascii=False)

    generator = NarrativeGenerator(
        NarrativeSettings(True, "test", "https://example.invalid/v1", "test", 0.2),
        OverreachingProvider(),
    )
    engine, sessions = create_database(tmp_path / "isolation.db")
    try:
        service = WorldService(sessions, generator)
        service.initialize()
        before = _v06_fact_snapshot(service)
        asyncio.run(service.process_narrative_jobs(limit=100))
        assert _v06_fact_snapshot(service) == before
    finally:
        engine.dispose()


def test_three_week_career_budget_cycles_are_randomly_reproducible(tmp_path):
    engine_a, sessions_a = create_database(tmp_path / "a.db")
    engine_b, sessions_b = create_database(tmp_path / "b.db")
    services = (WorldService(sessions_a), WorldService(sessions_b))
    for service in services:
        service.initialize()
    try:
        for week in range(1, 4):
            for service in services:
                with service.session_factory() as session:
                    state = session.get(WorldState, 1)
                    state.total_minutes = week * WEEK_MINUTES
                    rng = RandomService(state.seed, state.random_counter)
                    npcs = list(session.scalars(select(NPC).order_by(NPC.id)))
                    process_career_budget_cycles(session, npcs, ClockSnapshot(state.total_minutes), rng)
                    state.random_counter = rng.counter
                    session.commit()
        assert _v06_fact_snapshot(services[0]) == _v06_fact_snapshot(services[1])
        with sessions_a() as session:
            assert session.scalar(select(func.count()).select_from(WeeklyEconomicReport)) == 15
            assert session.scalar(select(func.count()).select_from(PerformanceReview)) == 15
    finally:
        engine_a.dispose()
        engine_b.dispose()
