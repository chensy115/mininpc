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
    Event,
    LongTermGoal,
    Memory,
    NPC,
    NarrativeArtifact,
    NarrativeJob,
    Relationship,
    WorldState,
)
from simulation.clock import ClockSnapshot
from simulation.events import add_event
from simulation.memory import add_memory
from simulation.narrative import (
    NarrativeGenerator,
    NarrativeSettings,
    enqueue_event_jobs,
    enqueue_memory_summary_jobs,
)
from simulation.world import WorldService


def fallback_generator() -> NarrativeGenerator:
    return NarrativeGenerator(
        NarrativeSettings(False, None, "https://example.invalid/v1", "test-model", 0.2)
    )


class FailingProvider:
    name = "failing-test-provider"

    async def generate(self, kind, context):
        raise TimeoutError("simulated provider outage")


class SuccessfulProvider:
    name = "successful-test-provider"

    async def generate(self, kind, context):
        if kind == "goal_narrative":
            return json.dumps({"title": "我的目标", "motivation": "按事实稳步推进。"}, ensure_ascii=False)
        if kind == "dialogue":
            return json.dumps(
                {"lines": [
                    {"speaker": context["actor_name"], "text": "今天见到你很高兴。"},
                    {"speaker": context["target_name"], "text": "我也是。"},
                ]},
                ensure_ascii=False,
            )
        if kind == "event_explanation":
            return json.dumps({"text": "这是已提交事实的简短解释。"}, ensure_ascii=False)
        return json.dumps({"text": "这是基于真实记忆的总结。"}, ensure_ascii=False)


class OverreachingProvider:
    name = "overreaching-test-provider"

    async def generate(self, kind, context):
        return json.dumps({
            "title": "建立储蓄",
            "motivation": "继续努力。",
            "money": 999999,
            "relationship": 100,
            "location": "Moon",
            "action": "Teleport",
        }, ensure_ascii=False)


def make_service(tmp_path, generator=None, filename="narrative.db"):
    engine, sessions = create_database(tmp_path / filename)
    service = WorldService(sessions, generator or fallback_generator())
    service.initialize()
    return engine, service


def fact_snapshot(service: WorldService):
    with service.session_factory() as session:
        state = session.get(WorldState, 1)
        npcs = [
            (npc.id, npc.current_location, npc.current_action, npc.money, npc.energy, npc.hunger,
             npc.mood, npc.social_need, npc.work_satisfaction)
            for npc in session.scalars(select(NPC).order_by(NPC.id))
        ]
        relationships = [
            (item.from_npc_id, item.to_npc_id, item.score)
            for item in session.scalars(select(Relationship).order_by(Relationship.id))
        ]
        goals = [
            (goal.id, goal.npc_id, goal.goal_key, goal.target_value, goal.priority, goal.target_npc_id)
            for goal in session.scalars(select(LongTermGoal).order_by(LongTermGoal.id))
        ]
        return (state.total_minutes, state.seed, state.random_counter, npcs, relationships, goals)


def test_no_key_mode_generates_safe_fallback_without_touching_facts(tmp_path):
    engine, service = make_service(tmp_path)
    try:
        before = fact_snapshot(service)
        assert asyncio.run(service.process_narrative_jobs(limit=100)) == 20
        after = fact_snapshot(service)
        assert after == before

        status = asyncio.run(service.narrative_status())
        assert status["mode"] == "fallback"
        assert status["reason"] == "disabled"
        assert status["jobs"] == {"pending": 0, "processing": 0, "completed": 20}
        assert status["fallback_artifacts"] == 20
    finally:
        engine.dispose()


def test_provider_failure_and_invalid_authority_fall_back_to_text_only_artifact(tmp_path):
    settings = NarrativeSettings(True, "not-a-real-key", "https://example.invalid/v1", "test", 0.2)
    generator = NarrativeGenerator(settings, FailingProvider())
    engine, service = make_service(tmp_path, generator)
    try:
        before = fact_snapshot(service)
        asyncio.run(service.process_narrative_jobs(limit=1))
        assert fact_snapshot(service) == before
        with service.session_factory() as session:
            artifact = session.scalar(select(NarrativeArtifact).order_by(NarrativeArtifact.id))
            assert artifact.fallback_used is True
            assert artifact.provider == "deterministic-template"
            assert "TimeoutError" in artifact.error
            assert set(json.loads(artifact.content_json)) == {"title", "motivation"}
    finally:
        engine.dispose()


def test_llm_extra_fact_mutations_are_discarded_and_cannot_change_world(tmp_path):
    settings = NarrativeSettings(True, "fake", "https://example.invalid/v1", "test", 0.2)
    engine, service = make_service(
        tmp_path, NarrativeGenerator(settings, OverreachingProvider())
    )
    try:
        before = fact_snapshot(service)
        asyncio.run(service.process_narrative_jobs(limit=1))
        assert fact_snapshot(service) == before
        with service.session_factory() as session:
            artifact = session.scalar(select(NarrativeArtifact).order_by(NarrativeArtifact.id))
            assert json.loads(artifact.content_json) == {
                "title": "建立储蓄",
                "motivation": "继续努力。",
            }
    finally:
        engine.dispose()


def test_social_dialogue_and_important_event_explanation_are_grounded_and_additive(tmp_path):
    settings = NarrativeSettings(True, "fake", "https://example.invalid/v1", "test", 0.2)
    engine, service = make_service(tmp_path, NarrativeGenerator(settings, SuccessfulProvider()))
    try:
        asyncio.run(service.process_narrative_jobs(limit=100))
        with service.session_factory() as session:
            clock = ClockSnapshot(720)
            last_event_id = session.scalar(select(func.max(Event.id))) or 0
            event = add_event(
                session, clock, "SOCIAL", "Alice 与 Bob 聊了聊天",
                npc_id=1, target_npc_id=2, location="Cafe",
            )
            session.flush()
            event_id = event.id
            assert enqueue_event_jobs(session, last_event_id, clock.total_minutes) == 2
            session.commit()

        before = fact_snapshot(service)
        assert asyncio.run(service.process_narrative_jobs(limit=10)) == 2
        assert fact_snapshot(service) == before
        alice = asyncio.run(service.list_narratives("dialogue", npc_id=1))
        bob = asyncio.run(service.list_narratives("dialogue", npc_id=2))
        explanations = asyncio.run(service.list_narratives("event_explanation"))
        assert alice[0]["event_id"] == bob[0]["event_id"] == event_id
        assert [line["speaker"] for line in alice[0]["content"]["lines"]] == ["Alice", "Bob"]
        assert explanations[0]["event_id"] == event_id
        assert explanations[0]["fallback_used"] is False
    finally:
        engine.dispose()


def test_memory_summary_uses_only_persisted_memory_range(tmp_path):
    engine, service = make_service(tmp_path)
    try:
        asyncio.run(service.process_narrative_jobs(limit=100))
        with service.session_factory() as session:
            for index in range(5):
                add_memory(
                    session, ClockSnapshot(600 + index * 10), 1, f"真实记忆 {index}",
                    importance=index + 1, emotion="neutral",
                )
            session.flush()
            assert enqueue_memory_summary_jobs(session, 650) == 1
            first_id = session.scalar(select(func.min(Memory.id)).where(Memory.npc_id == 1))
            last_id = session.scalar(select(func.max(Memory.id)).where(Memory.npc_id == 1))
            session.commit()

        asyncio.run(service.process_narrative_jobs(limit=10))
        summaries = asyncio.run(service.list_narratives("memory_summary", npc_id=1))
        assert len(summaries) == 1
        assert summaries[0]["source_memory_start_id"] == first_id
        assert summaries[0]["source_memory_end_id"] == last_id
        assert "真实记忆" in summaries[0]["content"]["text"]
    finally:
        engine.dispose()


def test_v03_database_upgrade_adds_only_v04_tables_and_preserves_data(tmp_path):
    path = tmp_path / "v03-world.db"
    engine, sessions = create_database(path)
    service = WorldService(sessions, fallback_generator())
    service.initialize()
    with sessions() as session:
        session.get(WorldState, 1).total_minutes = 1234
        session.get(NPC, 1).money = 234.5
        session.commit()
    NarrativeArtifact.__table__.drop(engine)
    NarrativeJob.__table__.drop(engine)
    engine.dispose()

    upgraded_engine, upgraded_sessions = create_database(path)
    try:
        upgraded = WorldService(upgraded_sessions, fallback_generator())
        upgraded.initialize()
        tables = set(inspect(upgraded_engine).get_table_names())
        assert {"narrative_jobs", "narrative_artifacts"} <= tables
        with upgraded_sessions() as session:
            assert session.get(WorldState, 1).total_minutes == 1234
            assert session.get(NPC, 1).money == 234.5
            assert session.scalar(select(func.count()).select_from(LongTermGoal)) == 20
            assert session.scalar(select(func.count()).select_from(NarrativeJob)) == 20
    finally:
        upgraded_engine.dispose()


def test_v04_apis_are_additive_and_old_response_shapes_remain_exact(tmp_path):
    engine, service = make_service(tmp_path)
    try:
        asyncio.run(service.process_narrative_jobs(limit=100))
        configure_world_service(service)
        api = FastAPI()
        api.include_router(world_router)
        api.include_router(npc_router)
        with TestClient(api) as client:
            assert client.get("/api/narrative/status").status_code == 200
            assert len(client.get("/api/npcs/1/goal-narratives").json()) == 4
            assert client.get("/api/npcs/1/dialogues").json() == []
            assert client.get("/api/npcs/1/memory-summaries").json() == []
            assert client.get("/api/npcs/999/dialogues").status_code == 404
            assert set(client.get("/api/world").json()) == {
                "day", "weekday", "time", "label", "total_minutes", "paused", "speed", "locations"
            }
            assert set(client.get("/api/npcs/1").json()) == {
                "id", "name", "age", "job", "current_location", "current_action",
                "action_end_minute", "money", "states", "personality", "relationships",
            }
    finally:
        engine.dispose()


def test_narrative_processing_preserves_long_simulation_reproducibility(tmp_path):
    engine_a, service_a = make_service(tmp_path, fallback_generator(), "a.db")
    engine_b, service_b = make_service(tmp_path, fallback_generator(), "b.db")
    try:
        async def run():
            for tick in range(24 * 6):
                assert await service_a.tick() is True
                assert await service_b.tick() is True
                if tick % 24 == 0:
                    await service_a.process_narrative_jobs(limit=100)

        asyncio.run(run())
        assert fact_snapshot(service_a) == fact_snapshot(service_b)
        for npc_id in range(1, 6):
            assert asyncio.run(service_a.latest_decision(npc_id)) == asyncio.run(service_b.latest_decision(npc_id))
    finally:
        engine_a.dispose()
        engine_b.dispose()
