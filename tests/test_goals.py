from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select

from api.dependencies import configure_world_service
from api.npc import router as npc_router
from api.world import router as world_router
from database.database import create_database
from database.models import Event, LongTermGoal, NPC, Relationship, WorldState
from simulation.actions import complete_action
from simulation.clock import ClockSnapshot
from simulation.decision import decide
from simulation.goals import build_goal_context, goal_snapshots
from simulation.random_service import RandomService
from simulation.world import WorldService


def test_default_world_has_four_long_term_goals_per_npc(world_service):
    with world_service.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(LongTermGoal)) == 20
        for npc in session.scalars(select(NPC).order_by(NPC.id)):
            goals = goal_snapshots(session, npc)
            assert {goal["type"] for goal in goals} == {
                "savings", "friendship", "career_satisfaction", "relationship"
            }
            assert all(0 <= goal["progress"] <= 100 for goal in goals)
            assert all(0 <= goal["need_score"] <= 100 for goal in goals)


def test_goal_progress_is_derived_from_existing_v02_state(world_service):
    with world_service.session_factory() as session:
        alice = session.get(NPC, 1)
        alice.money = 300
        alice.work_satisfaction = 84
        snapshots = {goal["type"]: goal for goal in goal_snapshots(session, alice)}
        assert snapshots["savings"]["status"] == "completed"
        assert snapshots["savings"]["progress"] == 100
        assert snapshots["career_satisfaction"]["status"] == "completed"


def test_existing_v02_database_adds_only_the_goal_table_and_preserves_state(tmp_path):
    path = tmp_path / "v02-world.db"
    engine, sessions = create_database(path)
    service = WorldService(sessions)
    service.initialize()
    with sessions() as session:
        state = session.get(WorldState, 1)
        state.total_minutes = 987
        alice = session.get(NPC, 1)
        alice.money = 222.5
        session.execute(LongTermGoal.__table__.delete())
        session.commit()
    LongTermGoal.__table__.drop(engine)
    engine.dispose()

    upgraded_engine, upgraded_sessions = create_database(path)
    try:
        upgraded = WorldService(upgraded_sessions)
        upgraded.initialize()
        assert "long_term_goals" in inspect(upgraded_engine).get_table_names()
        with upgraded_sessions() as session:
            assert session.get(WorldState, 1).total_minutes == 987
            assert session.get(NPC, 1).money == 222.5
            assert session.scalar(select(func.count()).select_from(LongTermGoal)) == 20
    finally:
        upgraded_engine.dispose()


def _candidate(decision, action):
    return next(item for item in decision.candidates if item.action == action)


def test_unsatisfied_long_term_goals_raise_relevant_utility_scores(world_service):
    with world_service.session_factory() as session:
        alice = session.get(NPC, 1)
        alice.current_location = "Office"
        context = build_goal_context(session, alice)
        occupants = {"Home": [], "Office": [alice], "Cafe": [], "Park": []}
        with_goals = decide(
            alice, ClockSnapshot(600), occupants, RandomService(42), context
        )
        without_goals = decide(alice, ClockSnapshot(600), occupants, RandomService(42))

        work = _candidate(with_goals, "Work")
        assert work.raw_score > _candidate(without_goals, "Work").raw_score
        assert work.contributions["长期目标：建立储蓄"] > 0
        assert work.contributions["长期目标：职业满意度"] > 0


def test_relationship_goal_only_boosts_socializing_with_target_present(world_service):
    with world_service.session_factory() as session:
        alice = session.get(NPC, 1)
        bob = session.get(NPC, 2)
        diana = session.get(NPC, 4)
        alice.current_location = bob.current_location = diana.current_location = "Cafe"
        context = build_goal_context(session, alice)
        with_target = {"Home": [], "Office": [], "Cafe": [alice, bob], "Park": []}
        without_target = {"Home": [], "Office": [], "Cafe": [alice, diana], "Park": []}

        target_score = _candidate(
            decide(alice, ClockSnapshot(720), with_target, RandomService(7), context),
            "Socialize",
        )
        other_score = _candidate(
            decide(alice, ClockSnapshot(720), without_target, RandomService(7), context),
            "Socialize",
        )
        assert target_score.contributions["长期目标：建设重要关系"] > 0
        assert other_score.contributions["长期目标：建设重要关系"] == 0
        assert target_score.raw_score > other_score.raw_score


def test_social_action_prefers_the_relationship_goal_target(world_service):
    with world_service.session_factory() as session:
        alice = session.get(NPC, 1)
        bob = session.get(NPC, 2)
        diana = session.get(NPC, 4)
        alice.current_location = bob.current_location = diana.current_location = "Cafe"
        alice.current_action = "Socialize"
        before = session.scalar(
            select(Relationship).where(
                Relationship.from_npc_id == alice.id,
                Relationship.to_npc_id == bob.id,
            )
        ).score

        complete_action(session, alice, ClockSnapshot(720), RandomService(42))
        session.commit()

        social_event = session.scalar(
            select(Event)
            .where(Event.event_type == "SOCIAL", Event.npc_id == alice.id)
            .order_by(Event.id.desc())
        )
        after = session.scalar(
            select(Relationship).where(
                Relationship.from_npc_id == alice.id,
                Relationship.to_npc_id == bob.id,
            )
        ).score
        assert social_event.target_npc_id == bob.id
        assert after >= before


def test_career_goal_adds_a_small_positive_work_satisfaction_pull(world_service):
    with world_service.session_factory() as session:
        alice = session.get(NPC, 1)
        alice.current_location = "Office"
        alice.current_action = "Work"
        alice.work_satisfaction = 50
        before = alice.work_satisfaction
        baseline_random_change = RandomService(42).uniform(-1.5, 1.5)

        complete_action(session, alice, ClockSnapshot(600), RandomService(42))

        assert alice.work_satisfaction - before > baseline_random_change


def test_goals_have_additive_apis_without_changing_existing_contracts(world_service):
    configure_world_service(world_service)
    api = FastAPI()
    api.include_router(world_router)
    api.include_router(npc_router)
    with TestClient(api) as client:
        all_goals = client.get("/api/goals")
        alice_goals = client.get("/api/npcs/1/goals")
        assert all_goals.status_code == 200 and len(all_goals.json()) == 20
        assert alice_goals.status_code == 200 and len(alice_goals.json()) == 4
        assert {goal["type"] for goal in alice_goals.json()} == {
            "savings", "friendship", "career_satisfaction", "relationship"
        }
        assert client.get("/api/npcs/999/goals").status_code == 404

        # V0.1/V0.2 clients can continue strict field validation unchanged.
        assert set(client.get("/api/world").json()) == {
            "day", "weekday", "time", "label", "total_minutes", "paused", "speed", "locations"
        }
        assert set(client.get("/api/npcs/1").json()) == {
            "id", "name", "age", "job", "current_location", "current_action",
            "action_end_minute", "money", "states", "personality", "relationships",
        }


def test_three_day_simulation_advances_persistent_long_term_goal_progress(world_service):
    def snapshot_values():
        with world_service.session_factory() as session:
            return {
                (npc.id, goal["goal_key"]): goal["current_value"]
                for npc in session.scalars(select(NPC).order_by(NPC.id))
                for goal in goal_snapshots(session, npc)
            }

    before = snapshot_values()

    async def run_three_days():
        for _ in range(3 * 24 * 6):
            assert await world_service.tick() is True

    asyncio.run(run_three_days())
    after = snapshot_values()

    changed_types = {
        key[1].split(":", 1)[0]
        for key, value in after.items()
        if value != before[key]
    }
    assert "savings" in changed_types
    assert "career_satisfaction" in changed_types
    assert "friendship" in changed_types or "relationship" in changed_types
    with world_service.session_factory() as session:
        assert session.get(WorldState, 1).total_minutes == 480 + 3 * 1440
