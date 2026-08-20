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
    CohousingHousehold,
    DecisionLog,
    Event,
    FriendCircle,
    JointActivity,
    NPC,
    Relationship,
    SharedExpense,
    SocialAudit,
    SocialBond,
    SocialCommitment,
    SocialInvitation,
    SocialProfile,
    WorldState,
)
from simulation.clock import ClockSnapshot
from simulation.narrative import NarrativeGenerator, NarrativeSettings
from simulation.social_life import (
    DAY_MINUTES,
    WEEK_MINUTES,
    _refresh_bond,
    _refresh_friend_circles,
    bond_snapshots,
    process_social_life_cycles,
    record_social_interaction,
    social_life_context,
)
from simulation.world import WorldService


V08_MODELS_IN_DROP_ORDER = (
    SharedExpense,
    JointActivity,
    SocialCommitment,
    SocialInvitation,
    SocialAudit,
    CohousingHousehold,
    FriendCircle,
    SocialProfile,
    SocialBond,
)


def _set_pair(session, first_id: int, second_id: int, score: int, now: int = 480):
    first = session.scalar(select(Relationship).where(
        Relationship.from_npc_id == first_id, Relationship.to_npc_id == second_id
    ))
    second = session.scalar(select(Relationship).where(
        Relationship.from_npc_id == second_id, Relationship.to_npc_id == first_id
    ))
    first.score = second.score = score
    bond = session.scalar(select(SocialBond).where(
        SocialBond.npc_low_id == min(first_id, second_id), SocialBond.npc_high_id == max(first_id, second_id)
    ))
    _refresh_bond(session, bond, now)
    return bond


def _record_social(session, actor_id: int, target_id: int, now: int, delta: int = 3, location: str = "Cafe"):
    relationship = session.scalar(select(Relationship).where(
        Relationship.from_npc_id == actor_id, Relationship.to_npc_id == target_id
    ))
    relationship.score += delta
    clock = ClockSnapshot(now)
    social = Event(
        world_day=clock.day, world_time=clock.time_text, event_type="SOCIAL",
        npc_id=actor_id, target_npc_id=target_id, location=location,
        description="test social", metadata_json="{}",
    )
    session.add(social)
    session.flush()
    session.add(Event(
        world_day=clock.day, world_time=clock.time_text, event_type="RELATIONSHIP",
        npc_id=actor_id, target_npc_id=target_id, location=location,
        description="test relationship", metadata_json=json.dumps({"change": delta}),
    ))
    session.flush()
    assert record_social_interaction(session, social, now)


def test_v08_initializes_current_bonds_and_profiles_without_fake_history(world_service):
    with world_service.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(SocialBond)) == 10
        assert session.scalar(select(func.count()).select_from(SocialProfile)) == 5
        assert session.scalar(select(func.count()).select_from(SocialInvitation)) == 0
        assert session.scalar(select(func.count()).select_from(SocialCommitment)) == 0
        assert session.scalar(select(func.count()).select_from(JointActivity)) == 0
        assert session.scalar(select(func.count()).select_from(CohousingHousehold)) == 0
        assert session.scalar(select(func.count()).select_from(SharedExpense)) == 0


def test_relationship_stage_uses_both_directions_and_is_explainable(world_service):
    with world_service.session_factory() as session:
        low_to_high = session.scalar(select(Relationship).where(
            Relationship.from_npc_id == 1, Relationship.to_npc_id == 2
        ))
        high_to_low = session.scalar(select(Relationship).where(
            Relationship.from_npc_id == 2, Relationship.to_npc_id == 1
        ))
        low_to_high.score, high_to_low.score = 70, 5
        bond = session.scalar(select(SocialBond).where(SocialBond.npc_low_id == 1, SocialBond.npc_high_id == 2))
        _refresh_bond(session, bond, 600)
        snapshot = next(item for item in bond_snapshots(session) if item["npc_low_id"] == 1 and item["npc_high_id"] == 2)
        assert snapshot["stage"] == "strained"
        assert snapshot["asymmetry"] == 65
        assert any("方向差" in reason for reason in snapshot["reasons"])


def test_invitation_becomes_commitment_and_joint_activity(world_service):
    with world_service.session_factory() as session:
        bond = _set_pair(session, 1, 2, 40)
        assert bond.stage == "friend"
        _record_social(session, 1, 2, 600)
        commitment = session.scalar(select(SocialCommitment))
        invitation = session.scalar(select(SocialInvitation))
        assert invitation.status == "accepted" and commitment.status == "planned"
        _record_social(session, 2, 1, commitment.scheduled_minute, location="Cafe")
        assert commitment.status == "completed"
        assert session.scalar(select(func.count()).select_from(JointActivity)) == 1
        assert session.scalar(select(func.count()).select_from(SocialAudit).where(SocialAudit.kind == "joint_activity")) == 1


def test_friend_circles_are_rule_derived_and_bounded_to_four(world_service):
    with world_service.session_factory() as session:
        for first, second in ((1, 2), (2, 3), (3, 4), (4, 5)):
            _set_pair(session, first, second, 50)
        _refresh_friend_circles(session, 700)
        circle = session.scalar(select(FriendCircle).where(FriendCircle.active.is_(True)))
        assert circle is not None
        assert json.loads(circle.member_ids_json) == [1, 2, 3, 4]
        assert len(json.loads(circle.member_ids_json)) <= 4


def test_limited_cohousing_and_weekly_shared_expense_are_audited(world_service):
    with world_service.session_factory() as session:
        bond = _set_pair(session, 1, 2, 80)
        assert bond.stage in {"close_friend", "trusted"}
        for offset in (0, 100):
            session.add(JointActivity(
                activity_key="shared_time", location="Park", start_minute=600 + offset,
                end_minute=620 + offset, participant_ids_json=json.dumps([1, 2]),
                shared_cost=0.0, outcome_json="{}",
            ))
        session.flush()
        process_social_life_cycles(session, list(session.scalars(select(NPC).order_by(NPC.id))), ClockSnapshot(800))
        household = session.scalar(select(CohousingHousehold).where(CohousingHousehold.active.is_(True)))
        assert household is not None and json.loads(household.resident_ids_json) == [1, 2]
        before = {item.id: item.money for item in session.scalars(select(NPC).where(NPC.id.in_((1, 2))))}
        process_social_life_cycles(session, list(session.scalars(select(NPC).order_by(NPC.id))), ClockSnapshot(household.next_expense_minute))
        expense = session.scalar(select(SharedExpense))
        assert expense.amount == 12.0 and len(json.loads(expense.split_json)) == 2
        assert all(session.get(NPC, item).money == before[item] - 6 for item in before)


def test_inactive_relationship_decays_daily_then_positive_contact_repairs(world_service):
    now = 5 * DAY_MINUTES
    with world_service.session_factory() as session:
        bond = _set_pair(session, 1, 2, 20)
        bond.last_interaction_minute = 0
        bond.last_decay_minute = 0
        people = list(session.scalars(select(NPC).order_by(NPC.id)))
        result = process_social_life_cycles(session, people, ClockSnapshot(now))
        assert result["decays"] >= 1 and bond.decay_count == 1
        scores_after_decay = [
            session.scalar(select(Relationship.score).where(Relationship.from_npc_id == a, Relationship.to_npc_id == b))
            for a, b in ((1, 2), (2, 1))
        ]
        assert scores_after_decay == [19, 19]
        _record_social(session, 1, 2, now + 10)
        assert bond.repair_count == 1
        assert session.scalar(select(func.count()).select_from(SocialAudit).where(SocialAudit.kind == "repair")) == 1


def test_belonging_and_trust_are_derived_with_reasons(world_service):
    with world_service.session_factory() as session:
        npc = session.get(NPC, 1)
        context = social_life_context(session, npc, 480)
        profile = session.scalar(select(SocialProfile).where(SocialProfile.npc_id == 1))
        assert context["enabled"] is True
        assert 0 <= context["belonging"] <= 100 and 0 <= context["trust_index"] <= 100
        assert len(json.loads(profile.reasons_json)) == 5


def test_v08_apis_are_additive_and_old_exact_shapes_remain_unchanged(world_service):
    configure_world_service(world_service)
    api = FastAPI()
    api.include_router(world_router)
    api.include_router(npc_router)
    with TestClient(api) as client:
        assert client.get("/api/economy").json()["mode"] == "v0.5"
        assert client.get("/api/career-budget").json()["mode"] == "v0.6"
        assert client.get("/api/community-rhythm").json()["mode"] == "v0.7"
        assert client.get("/api/social-life").json() == {
            "enabled": True, "mode": "v0.8", "bonds": 10, "active_circles": 0,
            "planned_commitments": 0, "joint_activities": 0,
            "active_households": 0, "shared_expenses": 0,
        }
        assert len(client.get("/api/social-bonds").json()) == 10
        assert client.get("/api/npcs/1/social-life").json()["mode"] == "v0.8"
        assert client.get("/api/npcs/999/social-life").status_code == 404
        assert set(client.get("/api/world").json()) == {
            "day", "weekday", "time", "label", "total_minutes", "paused", "speed", "locations"
        }
        assert set(client.get("/api/npcs/1").json()) == {
            "id", "name", "age", "job", "current_location", "current_action",
            "action_end_minute", "money", "states", "personality", "relationships",
        }


def test_existing_v07_database_adds_only_v08_tables_and_preserves_facts(tmp_path):
    path = tmp_path / "v07-world.db"
    engine, sessions = create_database(path)
    service = WorldService(sessions)
    service.initialize()
    with sessions() as session:
        session.get(WorldState, 1).total_minutes = 54321
        session.get(NPC, 1).money = 654.3
        original_relationships = list(session.execute(select(
            Relationship.from_npc_id, Relationship.to_npc_id, Relationship.score
        ).order_by(Relationship.id)))
        session.commit()
    for model in V08_MODELS_IN_DROP_ORDER:
        model.__table__.drop(engine)
    old_tables = set(inspect(engine).get_table_names())
    engine.dispose()

    upgraded_engine, upgraded_sessions = create_database(path)
    try:
        upgraded = WorldService(upgraded_sessions)
        upgraded.initialize()
        assert set(inspect(upgraded_engine).get_table_names()) - old_tables == {
            model.__tablename__ for model in V08_MODELS_IN_DROP_ORDER
        }
        with upgraded_sessions() as session:
            assert session.get(WorldState, 1).total_minutes == 54321
            assert session.get(NPC, 1).money == 654.3
            assert list(session.execute(select(
                Relationship.from_npc_id, Relationship.to_npc_id, Relationship.score
            ).order_by(Relationship.id))) == original_relationships
            assert session.scalar(select(func.count()).select_from(SocialBond)) == 10
            assert session.scalar(select(func.count()).select_from(JointActivity)) == 0
    finally:
        upgraded_engine.dispose()


def test_v08_disabled_missing_data_and_fault_use_v07_fallback(tmp_path, monkeypatch):
    engine, sessions = create_database(tmp_path / "disabled.db")
    try:
        service = WorldService(sessions, social_life_enabled=False)
        service.initialize()
        assert asyncio.run(service.social_life_status())["mode"] == "v0.7-compatible"
        with sessions() as session:
            assert session.scalar(select(func.count()).select_from(SocialBond)) == 0
        assert asyncio.run(service.tick())
    finally:
        engine.dispose()

    engine, sessions = create_database(tmp_path / "missing.db")
    try:
        service = WorldService(sessions)
        service.initialize()
        with sessions() as session:
            session.delete(session.scalar(select(SocialBond).where(SocialBond.npc_low_id == 1)))
            session.commit()
        with sessions() as session:
            assert social_life_context(session, session.get(NPC, 1), 480) is None
        assert asyncio.run(service.tick())
    finally:
        engine.dispose()

    def old_fact_snapshot(service):
        with service.session_factory() as session:
            state = session.get(WorldState, 1)
            return (
                (state.total_minutes, state.random_counter),
                [(row.id, row.current_location, row.current_action, row.action_end_minute,
                  row.pending_location, row.last_move_minute, row.money, row.energy, row.hunger,
                  row.mood, row.social_need, row.work_satisfaction)
                 for row in session.scalars(select(NPC).order_by(NPC.id))],
                [(row.from_npc_id, row.to_npc_id, row.score)
                 for row in session.scalars(select(Relationship).order_by(Relationship.id))],
                [(row.npc_id, row.chosen_action, row.candidates_json, row.reason_json)
                 for row in session.scalars(select(DecisionLog).order_by(DecisionLog.id))],
            )

    control_engine, control_sessions = create_database(tmp_path / "control.db")
    fault_engine, fault_sessions = create_database(tmp_path / "fault.db")
    try:
        control = WorldService(control_sessions, social_life_enabled=False)
        fault = WorldService(fault_sessions)
        control.initialize()
        fault.initialize()
        for sessions_for_case in (control_sessions, fault_sessions):
            with sessions_for_case() as session:
                session.get(WorldState, 1).total_minutes = 530
                session.commit()
        fail = lambda *_args: (_ for _ in ()).throw(RuntimeError("injected-v08-fault"))
        monkeypatch.setattr("simulation.world.process_social_life_cycles", fail)
        monkeypatch.setattr("simulation.world.social_life_context", fail)
        assert asyncio.run(control.tick()) and asyncio.run(fault.tick())
        assert old_fact_snapshot(fault) == old_fact_snapshot(control)
    finally:
        control_engine.dispose()
        fault_engine.dispose()


def test_reset_removes_v08_history_and_restores_profiles(world_service):
    with world_service.session_factory() as session:
        _set_pair(session, 1, 2, 40)
        _record_social(session, 1, 2, 600)
        session.commit()
        assert session.scalar(select(func.count()).select_from(SocialInvitation)) == 1
    asyncio.run(world_service.reset())
    with world_service.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(SocialBond)) == 10
        assert session.scalar(select(func.count()).select_from(SocialProfile)) == 5
        assert session.scalar(select(func.count()).select_from(SocialInvitation)) == 0
        assert session.scalar(select(func.count()).select_from(SocialAudit)) == 0


def _v08_fact_snapshot(service: WorldService):
    with service.session_factory() as session:
        return (
            [(row.npc_low_id, row.npc_high_id, row.stage, row.trust, row.interaction_count,
              row.decay_count, row.repair_count) for row in session.scalars(select(SocialBond).order_by(SocialBond.id))],
            [(row.npc_id, row.belonging, row.trust_index) for row in session.scalars(select(SocialProfile).order_by(SocialProfile.npc_id))],
            session.scalar(select(func.count()).select_from(SocialInvitation)),
            session.scalar(select(func.count()).select_from(JointActivity)),
            session.scalar(select(func.count()).select_from(CohousingHousehold)),
            session.scalar(select(func.count()).select_from(SharedExpense)),
        )


def test_llm_text_cannot_mutate_v08_facts(tmp_path):
    class OverreachingProvider:
        name = "overreaching-v08-provider"

        async def generate(self, kind, context):
            return json.dumps({
                "title": "我们立即成为挚友并合住", "motivation": "信任 100",
                "trust": 100, "belonging": 100, "stage": "trusted",
                "household": {"residents": [1, 2]}, "shared_expense": 999,
            }, ensure_ascii=False)

    generator = NarrativeGenerator(
        NarrativeSettings(True, "test", "https://example.invalid/v1", "test", 0.2),
        OverreachingProvider(),
    )
    engine, sessions = create_database(tmp_path / "isolation.db")
    try:
        service = WorldService(sessions, generator)
        service.initialize()
        before = _v08_fact_snapshot(service)
        asyncio.run(service.process_narrative_jobs(limit=100))
        assert _v08_fact_snapshot(service) == before
    finally:
        engine.dispose()


def test_v08_cycles_are_seed_independent_and_reproducible(tmp_path):
    engines, services = [], []
    for name in ("a.db", "b.db"):
        engine, sessions = create_database(tmp_path / name)
        service = WorldService(sessions)
        service.initialize()
        engines.append(engine)
        services.append(service)
        with sessions() as session:
            bond = _set_pair(session, 1, 2, 20)
            bond.last_interaction_minute = 0
            bond.last_decay_minute = 0
            session.commit()
    try:
        for service in services:
            with service.session_factory() as session:
                process_social_life_cycles(session, list(session.scalars(select(NPC).order_by(NPC.id))), ClockSnapshot(5 * DAY_MINUTES))
                session.commit()
        assert _v08_fact_snapshot(services[0]) == _v08_fact_snapshot(services[1])
    finally:
        for engine in engines:
            engine.dispose()


def test_multiweek_shared_living_is_bounded_and_idempotent(world_service):
    with world_service.session_factory() as session:
        household = CohousingHousehold(
            host_housing_id=1, resident_ids_json=json.dumps([1, 2]), started_minute=480,
            active=True, weekly_shared_cost=12.0, next_expense_minute=480 + WEEK_MINUTES,
            trust_at_start=80.0, reasons_json="[]",
        )
        session.add(household)
        session.flush()
        target = 480 + 3 * WEEK_MINUTES
        people = list(session.scalars(select(NPC).order_by(NPC.id)))
        process_social_life_cycles(session, people, ClockSnapshot(target))
        first_count = session.scalar(select(func.count()).select_from(SharedExpense))
        process_social_life_cycles(session, people, ClockSnapshot(target))
        assert first_count == 3
        assert session.scalar(select(func.count()).select_from(SharedExpense)) == first_count
        assert session.scalar(select(func.count()).select_from(CohousingHousehold).where(CohousingHousehold.active.is_(True))) == 1
