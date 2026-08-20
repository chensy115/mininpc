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
    CareerTransition,
    CausalLink,
    CommunityInstitution,
    Housing,
    HousingUpgradeRecord,
    LifeMilestone,
    LongTermGoal,
    NPC,
    NPCSkill,
    NarrativeArtifact,
    PerformanceReview,
    ReplayCheckpoint,
    SocialBond,
    StoryState,
    StorySummary,
    TrainingRecord,
    WorldState,
)
from simulation.clock import ClockSnapshot
from simulation.life_story import (
    MONTH_MINUTES,
    WEEK_MINUTES,
    causal_chain_snapshot,
    process_life_story_cycles,
    replay_story,
)
from simulation.narrative import NarrativeGenerator, NarrativeSettings
from simulation.world import WorldService


V09_MODELS_IN_DROP_ORDER = (
    CausalLink,
    ReplayCheckpoint,
    StorySummary,
    LifeMilestone,
    StoryState,
)


def _process(service: WorldService, now: int):
    with service.session_factory() as session:
        state = session.get(WorldState, 1)
        state.total_minutes = now
        npcs = list(session.scalars(select(NPC).order_by(NPC.id)))
        result = process_life_story_cycles(
            session, npcs, ClockSnapshot(now), seed=state.seed,
            random_counter=state.random_counter,
        )
        session.commit()
        return result


def test_v09_initializes_baseline_without_fake_history(world_service):
    with world_service.session_factory() as session:
        story = session.get(StoryState, 1)
        assert story.initialized_minute == 480
        observations = json.loads(story.observations_json)
        assert set(observations) == {"1", "2", "3", "4", "5"}
        assert session.scalar(select(func.count()).select_from(LifeMilestone)) == 0
        assert session.scalar(select(func.count()).select_from(StorySummary)) == 0
        assert session.scalar(select(func.count()).select_from(CausalLink)) == 0
        assert session.scalar(select(func.count()).select_from(ReplayCheckpoint)) == 0


def test_committed_facts_create_all_bounded_milestone_types(world_service):
    with world_service.session_factory() as session:
        npc = session.get(NPC, 1)
        skill = session.scalar(select(NPCSkill).where(NPCSkill.npc_id == 1))
        institution = session.scalar(select(CommunityInstitution).where(
            CommunityInstitution.institution_key == "career_center"
        ))
        housing = session.scalar(select(Housing).where(Housing.npc_id == 1))
        bond = session.scalar(select(SocialBond).where(
            SocialBond.npc_low_id == 1, SocialBond.npc_high_id == 2
        ))
        npc.money = 300.0
        skill.level = 2
        bond.stage = "trusted"
        bond.trust = 82
        housing.arrears = 10
        session.add(PerformanceReview(
            npc_id=1, world_minute=600, period_start_minute=480, period_end_minute=600,
            score=88, outcome="promotion", wage_before=10, wage_after=11,
            career_level_before=1, career_level_after=2, reasons_json='["优秀"]',
        ))
        for index, kind in enumerate(("unemployment", "reemployment", "career_change")):
            session.add(CareerTransition(
                npc_id=1, world_minute=601 + index, transition_type=kind,
                from_profession="Designer", to_profession=None if kind == "unemployment" else "Writer",
                reason=f"已提交的 {kind} 原因",
            ))
        session.add(HousingUpgradeRecord(
            npc_id=1, world_minute=604, tier_before="standard", tier_after="improved",
            cost=80, weekly_rent_before=22, weekly_rent_after=28,
            comfort_before=50, comfort_after=65,
        ))
        session.add(TrainingRecord(
            npc_id=1, institution_id=institution.id, world_minute=605,
            week_start_minute=0, profession_key="Designer", skill_key=skill.skill_key,
            fee=18, skill_experience=30, leveled_up=True,
        ))
        session.commit()

    first = _process(world_service, 660)
    assert first["milestones"] == 9
    second = _process(world_service, 660 + WEEK_MINUTES)
    assert second["milestones"] == 1
    with world_service.session_factory() as session:
        types = set(session.scalars(select(LifeMilestone.milestone_type)))
        assert types == {
            "promotion", "unemployment", "reemployment", "career_change",
            "housing_change", "skill_upgrade", "savings_achieved",
            "important_friendship", "persistent_arrears",
        }


def test_causal_chain_is_ordered_and_fact_backed(world_service):
    with world_service.session_factory() as session:
        goal = session.scalar(select(LongTermGoal).where(
            LongTermGoal.npc_id == 1, LongTermGoal.goal_type == "savings"
        ))
        session.get(NPC, 1).money = goal.target_value + 1
        session.commit()
    _process(world_service, 600)
    with world_service.session_factory() as session:
        milestone = session.scalar(select(LifeMilestone).where(
            LifeMilestone.milestone_type == "savings_achieved"
        ))
        chain = causal_chain_snapshot(session, milestone.id)
        assert chain["milestone"]["fact_digest"] == milestone.fact_digest
        assert [row["sequence"] for row in chain["causes"]] == [1]
        assert chain["causes"][0]["fact"]["current_balance"] == goal.target_value + 1


def test_cross_week_and_month_summaries_are_structured_and_immutable(world_service):
    result = _process(world_service, MONTH_MINUTES)
    assert result == {"milestones": 0, "summaries": 5, "checkpoints": 30}
    with world_service.session_factory() as session:
        rows = list(session.scalars(select(StorySummary).order_by(StorySummary.id)))
        assert [row.period_type for row in rows].count("week") == 4
        assert [row.period_type for row in rows].count("month") == 1
        for row in rows:
            facts = json.loads(row.facts_json)
            assert facts[0]["fact_type"] == "period"
            assert row.fact_digest


class OverreachingStoryProvider:
    name = "overreaching-story-provider"

    async def generate(self, kind, context):
        return json.dumps({
            "text": "仅润色文本。", "facts": [{"invented": True}],
            "milestones": [999], "money": 999999, "location": "Moon",
        }, ensure_ascii=False)


def test_llm_story_text_cannot_mutate_fact_lists_or_world(world_service):
    _process(world_service, WEEK_MINUTES)
    with world_service.session_factory() as session:
        before = [(row.id, row.facts_json, row.fact_digest) for row in session.scalars(select(StorySummary))]
        money = session.get(NPC, 1).money
    settings = NarrativeSettings(True, "fake", "https://example.invalid/v1", "test", 0.2)
    world_service.narrative_generator = NarrativeGenerator(settings, OverreachingStoryProvider())
    asyncio.run(world_service.process_narrative_jobs(limit=100))
    with world_service.session_factory() as session:
        after = [(row.id, row.facts_json, row.fact_digest) for row in session.scalars(select(StorySummary))]
        assert after == before
        assert session.get(NPC, 1).money == money
        artifact = session.scalar(select(NarrativeArtifact).where(NarrativeArtifact.kind == "story_summary"))
        assert json.loads(artifact.content_json) == {"text": "仅润色文本。"}


def test_narrative_failure_uses_text_fallback_without_story_fact_changes(tmp_path):
    class FailingProvider:
        name = "failing"
        async def generate(self, kind, context):
            raise TimeoutError("story provider unavailable")

    settings = NarrativeSettings(True, "fake", "https://example.invalid/v1", "test", 0.2)
    engine, sessions = create_database(tmp_path / "narrative-fault.db")
    service = WorldService(sessions, NarrativeGenerator(settings, FailingProvider()))
    try:
        service.initialize()
        _process(service, WEEK_MINUTES)
        with sessions() as session:
            before = session.scalar(select(StorySummary.facts_json))
        asyncio.run(service.process_narrative_jobs(limit=100))
        with sessions() as session:
            assert session.scalar(select(StorySummary.facts_json)) == before
            artifact = session.scalar(select(NarrativeArtifact).where(NarrativeArtifact.kind == "story_summary"))
            assert artifact.fallback_used is True
            assert json.loads(artifact.content_json)["text"]
    finally:
        engine.dispose()


def test_fixed_seed_replay_is_identical_across_days_weeks_and_month(world_service):
    _process(world_service, MONTH_MINUTES + WEEK_MINUTES + DAY_MINUTES)
    with world_service.session_factory() as session:
        first = replay_story(session, 480, MONTH_MINUTES + WEEK_MINUTES + 1, 42)
        second = replay_story(session, 480, MONTH_MINUTES + WEEK_MINUTES + 1, 42)
        assert first == second
        assert len(first["checkpoints"]) >= 30
        assert any(row["period_type"] == "week" for row in first["summaries"])
        assert any(row["period_type"] == "month" for row in first["summaries"])


DAY_MINUTES = 1440


def test_v09_apis_are_additive_and_old_exact_shapes_remain_unchanged(world_service):
    configure_world_service(world_service)
    app = FastAPI()
    app.include_router(world_router)
    app.include_router(npc_router)
    with TestClient(app) as client:
        assert client.get("/api/life-story").json()["mode"] == "v0.9"
        assert client.get("/api/milestones").json() == []
        assert client.get("/api/story-summaries").json() == []
        assert client.get("/api/npcs/1/timeline").json()["mode"] == "v0.9"
        assert client.get("/api/npcs/999/timeline").status_code == 404
        assert client.get("/api/story-replay?seed=999").status_code == 422
        assert set(client.get("/api/world").json()) == {
            "day", "weekday", "time", "label", "total_minutes", "paused", "speed", "locations"
        }
        assert set(client.get("/api/npcs/1").json()) == {
            "id", "name", "age", "job", "current_location", "current_action",
            "action_end_minute", "money", "states", "personality", "relationships",
        }
        assert client.get("/api/economy").json()["mode"] == "v0.5"
        assert client.get("/api/career-budget").json()["mode"] == "v0.6"
        assert client.get("/api/community-rhythm").json()["mode"] == "v0.7"
        assert client.get("/api/social-life").json()["mode"] == "v0.8"


def test_existing_v08_database_adds_only_v09_tables_and_preserves_facts(tmp_path):
    path = tmp_path / "v08-world.db"
    engine, sessions = create_database(path)
    service = WorldService(sessions)
    service.initialize()
    with sessions() as session:
        session.get(WorldState, 1).total_minutes = 54321
        session.get(NPC, 1).money = 654.3
        session.commit()
    for model in V09_MODELS_IN_DROP_ORDER:
        model.__table__.drop(engine)
    old_tables = set(inspect(engine).get_table_names())
    engine.dispose()

    upgraded_engine, upgraded_sessions = create_database(path)
    try:
        upgraded = WorldService(upgraded_sessions)
        upgraded.initialize()
        assert set(inspect(upgraded_engine).get_table_names()) - old_tables == {
            model.__tablename__ for model in V09_MODELS_IN_DROP_ORDER
        }
        with upgraded_sessions() as session:
            assert session.get(WorldState, 1).total_minutes == 54321
            assert session.get(NPC, 1).money == 654.3
            assert session.get(StoryState, 1).initialized_minute == 54321
            assert session.scalar(select(func.count()).select_from(LifeMilestone)) == 0
    finally:
        upgraded_engine.dispose()


def _v08_snapshot(service: WorldService):
    with service.session_factory() as session:
        state = session.get(WorldState, 1)
        return (
            state.total_minutes, state.random_counter,
            [(row.id, row.job, row.current_location, row.current_action, row.action_end_minute,
              row.money, row.energy, row.hunger, row.mood, row.social_need, row.work_satisfaction)
             for row in session.scalars(select(NPC).order_by(NPC.id))],
        )


def test_disabled_missing_state_and_periodic_fault_fall_back_to_exact_v08(tmp_path, monkeypatch):
    disabled_engine, disabled_sessions = create_database(tmp_path / "disabled.db")
    missing_engine, missing_sessions = create_database(tmp_path / "missing.db")
    control_engine, control_sessions = create_database(tmp_path / "control.db")
    fault_engine, fault_sessions = create_database(tmp_path / "fault.db")
    try:
        disabled = WorldService(disabled_sessions, life_story_enabled=False)
        disabled.initialize()
        assert asyncio.run(disabled.life_story_status())["mode"] == "v0.8-compatible"
        with disabled_sessions() as session:
            assert session.get(StoryState, 1) is None

        missing = WorldService(missing_sessions)
        missing.initialize()
        with missing_sessions() as session:
            session.delete(session.get(StoryState, 1))
            session.commit()
        assert asyncio.run(missing.tick())
        assert asyncio.run(missing.life_story_status())["mode"] == "v0.8-compatible"

        control = WorldService(control_sessions, life_story_enabled=False)
        fault = WorldService(fault_sessions)
        control.initialize()
        fault.initialize()
        for sf in (control_sessions, fault_sessions):
            with sf() as session:
                session.get(WorldState, 1).total_minutes = 530
                session.commit()
        monkeypatch.setattr(
            "simulation.world.process_life_story_cycles",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected-v09-fault")),
        )
        assert asyncio.run(control.tick()) and asyncio.run(fault.tick())
        assert _v08_snapshot(fault) == _v08_snapshot(control)
    finally:
        disabled_engine.dispose(); missing_engine.dispose()
        control_engine.dispose(); fault_engine.dispose()


def test_initialization_fault_keeps_v08_world_usable(tmp_path, monkeypatch):
    engine, sessions = create_database(tmp_path / "init-fault.db")
    monkeypatch.setattr(
        "simulation.world.ensure_life_story_data",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("injected-migration-fault")),
    )
    try:
        service = WorldService(sessions)
        service.initialize()
        assert asyncio.run(service.life_story_status())["mode"] == "v0.8-compatible"
        assert asyncio.run(service.social_life_status())["mode"] == "v0.8"
        assert asyncio.run(service.tick())
    finally:
        engine.dispose()


def test_v09_observation_consumes_no_random_values(world_service):
    with world_service.session_factory() as session:
        state = session.get(WorldState, 1)
        before = state.random_counter
        npcs = list(session.scalars(select(NPC).order_by(NPC.id)))
        process_life_story_cycles(
            session, npcs, ClockSnapshot(600), seed=state.seed,
            random_counter=state.random_counter,
        )
        assert state.random_counter == before


def test_reset_removes_v09_history_and_restores_baseline(world_service):
    with world_service.session_factory() as session:
        goal = session.scalar(select(LongTermGoal).where(
            LongTermGoal.npc_id == 1, LongTermGoal.goal_type == "savings"
        ))
        session.get(NPC, 1).money = goal.target_value + 1
        session.commit()
    _process(world_service, 600)
    assert asyncio.run(world_service.life_story_status())["milestones"] == 1
    asyncio.run(world_service.reset())
    with world_service.session_factory() as session:
        assert session.get(StoryState, 1).initialized_minute == 480
        assert session.scalar(select(func.count()).select_from(LifeMilestone)) == 0
        assert session.scalar(select(func.count()).select_from(StorySummary)) == 0
        assert session.scalar(select(func.count()).select_from(ReplayCheckpoint)) == 0
