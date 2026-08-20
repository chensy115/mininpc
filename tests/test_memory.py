from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from api.dependencies import configure_world_service
from api.npc import router as npc_router
from api.world import router as world_router
from database.models import Memory, NPC
from database.models import WorldState
from database.database import create_database
from simulation.actions import complete_action
from simulation.clock import ClockSnapshot
from simulation.memory import add_memory, clamp_importance
from simulation.random_service import RandomService


def test_memory_validates_emotion_and_clamps_importance(world_service):
    with world_service.session_factory() as session:
        memory = add_memory(
            session,
            ClockSnapshot(600),
            1,
            "测试记忆",
            importance=99,
            emotion="neutral",
        )
        session.commit()
        assert memory.importance == 10
        assert memory.timestamp == 600
        assert clamp_importance(-3) == 1


def test_completed_action_creates_persistent_memory(world_service):
    with world_service.session_factory() as session:
        npc = session.get(NPC, 1)
        npc.current_action = "Work"
        npc.current_location = "Office"
        complete_action(session, npc, ClockSnapshot(600), RandomService(42))
        session.commit()

        memory = session.scalar(select(Memory).where(Memory.npc_id == npc.id))
        assert memory is not None
        assert memory.importance == 4
        assert memory.emotion in {"positive", "negative"}
        assert memory.related_npc_id is None


def test_socializing_creates_related_memories_for_both_npcs(world_service):
    with world_service.session_factory() as session:
        alice = session.get(NPC, 1)
        bob = session.get(NPC, 2)
        alice.current_action = "Socialize"
        alice.current_location = "Cafe"
        bob.current_location = "Cafe"
        complete_action(session, alice, ClockSnapshot(720), RandomService(42))
        session.commit()

        alice_memory = session.scalar(select(Memory).where(Memory.npc_id == alice.id))
        bob_memory = session.scalar(select(Memory).where(Memory.npc_id == bob.id))
        assert alice_memory is not None and alice_memory.related_npc_id == bob.id
        assert bob_memory is not None and bob_memory.related_npc_id == alice.id


def test_reset_removes_memories(world_service):
    with world_service.session_factory() as session:
        add_memory(session, ClockSnapshot(600), 1, "将被重置", importance=5, emotion="neutral")
        session.commit()

    asyncio.run(world_service.reset())

    with world_service.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Memory)) == 0


def test_memory_api_filters_and_preserves_v01_contract(world_service):
    with world_service.session_factory() as session:
        add_memory(session, ClockSnapshot(600), 1, "普通记忆", importance=3, emotion="neutral")
        add_memory(
            session,
            ClockSnapshot(610),
            1,
            "重要社交记忆",
            importance=8,
            emotion="positive",
            related_npc_id=2,
        )
        session.commit()

    configure_world_service(world_service)
    api = FastAPI()
    api.include_router(world_router)
    api.include_router(npc_router)
    with TestClient(api) as client:
        response = client.get("/api/npcs/1/memories?min_importance=5&emotion=positive")
        assert response.status_code == 200
        assert response.json() == [
            {
                "id": 2,
                "npc_id": 1,
                "npc_name": "Alice",
                "content": "重要社交记忆",
                "importance": 8,
                "emotion": "positive",
                "timestamp": 610,
                "world_day": 1,
                "world_time": "10:10",
                "time_label": "第 1 天 · 星期一 · 10:10",
                "related_npc_id": 2,
                "related_npc_name": "Bob",
            }
        ]
        assert client.get("/api/npcs/999/memories").status_code == 404

        world = client.get("/api/world").json()
        assert set(world) == {
            "day", "weekday", "time", "label", "total_minutes", "paused", "speed", "locations"
        }
        npc = client.get("/api/npcs/1").json()
        assert set(npc) == {
            "id", "name", "age", "job", "current_location", "current_action",
            "action_end_minute", "money", "states", "personality", "relationships",
        }


def test_existing_v01_database_is_upgraded_additively(tmp_path):
    path = tmp_path / "legacy-world.db"
    engine, sessions = create_database(path)
    Memory.__table__.drop(engine)
    with sessions() as session:
        session.add(WorldState(id=1, total_minutes=777, paused=True, speed=5, seed=42, random_counter=9))
        session.commit()
    engine.dispose()

    upgraded_engine, upgraded_sessions = create_database(path)
    try:
        with upgraded_sessions() as session:
            assert session.get(WorldState, 1).total_minutes == 777
            assert session.scalar(select(func.count()).select_from(Memory)) == 0
    finally:
        upgraded_engine.dispose()
