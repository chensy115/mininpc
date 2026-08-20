from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select

from api.agent import router as agent_router
from api.dependencies import configure_world_service
from database.database import V11_TABLES, create_database
from database.models import (
    AgentDecisionArtifact,
    AgentDecisionJob,
    DecisionLog,
    Event,
    Memory,
    NPC,
    Relationship,
    WorldState,
)
from simulation.agent_brain import (
    AgentDecisionGenerator,
    AgentGenerationError,
    AgentSettings,
    V11_TABLE_NAMES,
)
from simulation.world import WorldService


class DynamicProvider:
    name = "fake-agent"

    def __init__(self, *, illegal: bool = False, extra: bool = False) -> None:
        self.illegal = illegal
        self.extra = extra
        self.calls = 0
        self.last_perception = None

    async def generate(self, perception):
        self.calls += 1
        self.last_perception = perception
        candidate = perception["available_actions"][-1]
        action = "Teleport" if self.illegal else candidate["action"]
        targets = candidate["allowed_targets"]
        payload = {
            "emotion": "专注",
            "intention": "选择一个当前可执行的行动",
            "action": action,
            "target": targets[0] if targets else None,
            "dialogue": "一起聊聊吧" if action == "Socialize" else None,
            "plan": ["先执行当前建议", "稍后根据可见状态重新评估"],
            "reason_summary": "依据当前地点、自身状态、目标和相关记忆给出简短建议。",
        }
        if self.extra:
            payload["database_write"] = {"money": 999999}
        return json.dumps(payload, ensure_ascii=False)


def _generator(provider=None, *, attempts=1, timeout=0.2):
    return AgentDecisionGenerator(
        AgentSettings(
            api_key="fake-key",
            base_url="https://example.invalid",
            model="fake-model",
            timeout_seconds=timeout,
            max_attempts=attempts,
        ),
        provider=provider,
    )


def _service(path, *, enabled, provider=None, attempts=1, timeout=0.2):
    engine, sessions = create_database(path)
    service = WorldService(
        sessions,
        agent_enabled=enabled,
        agent_generator=_generator(provider, attempts=attempts, timeout=timeout),
    )
    service.initialize()
    return engine, sessions, service


def _legacy_snapshot(sessions):
    with sessions() as session:
        state = session.get(WorldState, 1)
        return {
            "world": (
                state.total_minutes, state.paused, state.speed, state.seed, state.random_counter
            ),
            "npcs": [
                (
                    row.id, row.current_location, row.current_action, row.action_end_minute,
                    row.pending_location, row.last_move_minute, row.money, row.energy, row.hunger,
                    row.mood, row.social_need, row.work_satisfaction,
                )
                for row in session.scalars(select(NPC).order_by(NPC.id))
            ],
            "relationships": [
                (row.id, row.from_npc_id, row.to_npc_id, row.score)
                for row in session.scalars(select(Relationship).order_by(Relationship.id))
            ],
            "decisions": [
                (
                    row.id, row.npc_id, row.world_day, row.world_time, row.chosen_action,
                    row.candidates_json, row.reason_json,
                )
                for row in session.scalars(select(DecisionLog).order_by(DecisionLog.id))
            ],
            "events": [
                (
                    row.id, row.world_day, row.world_time, row.event_type, row.npc_id,
                    row.target_npc_id, row.location, row.description, row.metadata_json,
                )
                for row in session.scalars(select(Event).order_by(Event.id))
            ],
            "memories": [
                (
                    row.id, row.npc_id, row.content, row.importance, row.emotion,
                    row.timestamp, row.related_npc_id,
                )
                for row in session.scalars(select(Memory).order_by(Memory.id))
            ],
        }


def test_default_off_even_with_existing_llm_key_and_old_behavior(monkeypatch, tmp_path):
    monkeypatch.delenv("MINIWORLD_AGENT_SHADOW_ENABLED", raising=False)
    monkeypatch.setenv("MINIWORLD_LLM_API_KEY", "present-but-must-not-enable-agent")
    engine, sessions = create_database(tmp_path / "default-off.db")
    service = WorldService(sessions)
    service.initialize()
    try:
        assert service.agent_enabled is False
        assert asyncio.run(service.tick()) is True
        with sessions() as session:
            assert list(session.scalars(select(AgentDecisionJob))) == []
        status = asyncio.run(service.agent_status())
        assert status["mode"] == "disabled"
        assert status["authority"] == "advisory_only"
    finally:
        engine.dispose()


def test_enabled_without_key_is_unavailable_and_does_not_queue_or_call_network(tmp_path):
    engine, sessions = create_database(tmp_path / "no-key.db")
    generator = AgentDecisionGenerator(
        AgentSettings(None, "https://api.deepseek.com", "deepseek-v4-flash", 0.1, 1)
    )
    service = WorldService(sessions, agent_enabled=True, agent_generator=generator)
    service.initialize()
    try:
        assert asyncio.run(service.tick()) is True
        with sessions() as session:
            assert session.scalar(select(AgentDecisionJob)) is None
        shadow = asyncio.run(service.latest_agent_shadow(1))
        assert shadow["status"] == "unavailable"
        assert shadow["error_code"] == "missing_api_key"
    finally:
        engine.dispose()


def test_agent_settings_prefer_dedicated_deepseek_config_then_llm_fallback(monkeypatch):
    for name in ("MINIWORLD_AGENT_API_KEY", "MINIWORLD_AGENT_BASE_URL", "MINIWORLD_AGENT_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MINIWORLD_LLM_API_KEY", "shared-key")
    monkeypatch.setenv("MINIWORLD_LLM_BASE_URL", "https://shared.example/v1")
    monkeypatch.setenv("MINIWORLD_LLM_MODEL", "shared-model")
    shared = AgentSettings.from_env()
    assert (shared.api_key, shared.base_url, shared.model) == (
        "shared-key", "https://shared.example/v1", "shared-model"
    )
    monkeypatch.setenv("MINIWORLD_AGENT_API_KEY", "agent-key")
    monkeypatch.setenv("MINIWORLD_AGENT_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("MINIWORLD_AGENT_MODEL", "deepseek-v4-flash")
    dedicated = AgentSettings.from_env()
    assert (dedicated.api_key, dedicated.base_url, dedicated.model) == (
        "agent-key", "https://api.deepseek.com", "deepseek-v4-flash"
    )


def test_invalid_json_and_provider_error_retry_then_fail_safely(tmp_path):
    class BadProvider:
        name = "bad-fake"

        def __init__(self, mode):
            self.mode = mode

        async def generate(self, _perception):
            if self.mode == "error":
                raise RuntimeError("secret detail must not be stored")
            return "```json\n{}\n```"

    for mode, code in (("error", "provider_error"), ("json", "invalid_json")):
        engine, sessions, service = _service(
            tmp_path / f"bad-{mode}.db", enabled=True,
            provider=BadProvider(mode), attempts=1,
        )
        try:
            assert asyncio.run(service.tick()) is True
            assert asyncio.run(service.process_agent_decision_jobs()) == 1
            with sessions() as session:
                job = session.scalar(select(AgentDecisionJob))
                assert job.status == "failed" and job.last_error_code == code
                assert "secret detail" not in (job.last_error_code or "")
                assert session.scalar(select(AgentDecisionArtifact)) is None
        finally:
            engine.dispose()


def test_agent_timeout_fails_safely_without_delaying_world_tick(tmp_path):
    class SlowProvider:
        name = "slow-fake"

        async def generate(self, _perception):
            await asyncio.sleep(1)
            return "{}"

    engine, sessions, service = _service(
        tmp_path / "timeout.db", enabled=True, provider=SlowProvider(), attempts=1, timeout=0.05
    )
    try:
        assert asyncio.run(service.tick()) is True
        before = _legacy_snapshot(sessions)
        assert asyncio.run(service.process_agent_decision_jobs()) == 1
        with sessions() as session:
            job = session.scalar(select(AgentDecisionJob))
            assert job.status == "failed" and job.last_error_code == "timeout"
        after = _legacy_snapshot(sessions)
        assert after == before
    finally:
        engine.dispose()


def test_shadow_enqueue_uses_bounded_alice_perception_and_engine_actions(tmp_path):
    provider = DynamicProvider()
    engine, sessions, service = _service(tmp_path / "bounded.db", enabled=True, provider=provider)
    try:
        with sessions() as session:
            session.add(Memory(
                npc_id=1, content="PRIVATE-ERIC-CANARY", importance=10,
                emotion="neutral", timestamp=480, related_npc_id=5,
            ))
            session.add(Memory(
                npc_id=2, content="BOB-PRIVATE-CANARY", importance=10,
                emotion="neutral", timestamp=480, related_npc_id=None,
            ))
            session.commit()
        assert asyncio.run(service.tick()) is True
        with sessions() as session:
            jobs = list(session.scalars(select(AgentDecisionJob)))
            assert len(jobs) == 1 and jobs[0].npc_id == 1
            perception = json.loads(jobs[0].perception_json)
            actual = session.get(DecisionLog, jobs[0].decision_id)
            available = {
                item["action"] for item in json.loads(actual.candidates_json) if item["available"]
            }
        assert set(perception) == {
            "schema_version", "time", "place", "self", "people_here",
            "relevant_relationships", "goals", "plans", "relevant_memories",
            "available_actions",
        }
        serialized = json.dumps(perception, ensure_ascii=False)
        assert "PRIVATE-ERIC-CANARY" not in serialized
        assert "BOB-PRIVATE-CANARY" not in serialized
        assert "world_statistics" not in serialized and "economic_transactions" not in serialized
        assert all("score" not in item and "contributions" not in item for item in perception["available_actions"])
        assert {item["action"] for item in perception["available_actions"]} == available
        assert provider.calls == 0
    finally:
        engine.dispose()


def test_valid_and_illegal_advice_are_compared_but_never_executed(tmp_path):
    for label, provider, expected_legal in (
        ("valid", DynamicProvider(), True),
        ("illegal", DynamicProvider(illegal=True), False),
    ):
        engine, sessions, service = _service(
            tmp_path / f"{label}.db", enabled=True, provider=provider
        )
        try:
            assert asyncio.run(service.tick()) is True
            with sessions() as session:
                before = session.get(WorldState, 1).random_counter
                actual_action = session.get(NPC, 1).current_action
            assert asyncio.run(service.process_agent_decision_jobs()) == 1
            with sessions() as session:
                assert session.get(WorldState, 1).random_counter == before
                assert session.get(NPC, 1).current_action == actual_action
                artifact = session.scalar(select(AgentDecisionArtifact))
                assert artifact is not None and artifact.legal is expected_legal
            shadow = asyncio.run(service.latest_agent_shadow(1))
            assert shadow["status"] == "completed"
            assert shadow["utility"]["decision_id"] == shadow["job"]["decision_id"]
            assert shadow["validation"]["legal"] is expected_legal
            assert shadow["agent"]["action"] == ("Teleport" if not expected_legal else provider.last_perception["available_actions"][-1]["action"])
        finally:
            engine.dispose()


def test_strict_schema_rejects_extra_fields_and_marks_terminal_failure(tmp_path):
    provider = DynamicProvider(extra=True)
    engine, sessions, service = _service(tmp_path / "strict.db", enabled=True, provider=provider)
    try:
        assert asyncio.run(service.tick()) is True
        assert asyncio.run(service.process_agent_decision_jobs()) == 1
        with sessions() as session:
            job = session.scalar(select(AgentDecisionJob))
            assert job.status == "failed"
            assert job.last_error_code == "schema_validation_failed"
            assert session.scalar(select(AgentDecisionArtifact)) is None
            assert "database_write" not in job.perception_json
    finally:
        engine.dispose()


def test_processing_job_recovers_safely_after_restart(tmp_path):
    engine, sessions, service = _service(
        tmp_path / "recover.db", enabled=True, provider=DynamicProvider()
    )
    try:
        assert asyncio.run(service.tick()) is True
        with sessions() as session:
            job = session.scalar(select(AgentDecisionJob))
            job.status = "processing"
            session.commit()
        assert asyncio.run(service.recover_agent_decision_jobs()) == 1
        with sessions() as session:
            job = session.scalar(select(AgentDecisionJob))
            assert job.status == "pending"
            assert job.last_error_code == "recovered_after_restart"
    finally:
        engine.dispose()


def test_provider_wait_does_not_hold_world_lock_or_database_transaction(tmp_path):
    class BlockingProvider:
        name = "blocking-fake"

        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def generate(self, perception):
            self.started.set()
            await self.release.wait()
            candidate = perception["available_actions"][0]
            return json.dumps({
                "emotion": "平静", "intention": "等待后建议", "action": candidate["action"],
                "target": candidate["allowed_targets"][0] if candidate["allowed_targets"] else None,
                "dialogue": None, "plan": ["执行建议"], "reason_summary": "只使用受限快照。",
            }, ensure_ascii=False)

    async def scenario():
        provider = BlockingProvider()
        engine, _sessions, service = _service(
            tmp_path / "nonblocking.db", enabled=True, provider=provider, timeout=2.0
        )
        try:
            assert await service.tick()
            worker = asyncio.create_task(service.process_agent_decision_jobs())
            await asyncio.wait_for(provider.started.wait(), timeout=0.5)
            assert await asyncio.wait_for(service.tick(), timeout=0.8)
            provider.release.set()
            assert await asyncio.wait_for(worker, timeout=0.8) >= 1
        finally:
            engine.dispose()

    asyncio.run(scenario())


def test_shadow_mode_preserves_random_sequence_and_legacy_facts(tmp_path):
    off_engine, _off_sessions, off = _service(tmp_path / "off.db", enabled=False)
    on_engine, _on_sessions, on = _service(
        tmp_path / "on.db", enabled=True, provider=DynamicProvider()
    )
    try:
        for _ in range(36):
            assert asyncio.run(off.tick()) and asyncio.run(on.tick())
        assert _legacy_snapshot(off.session_factory) == _legacy_snapshot(on.session_factory)
        with on.session_factory() as session:
            assert session.scalar(select(AgentDecisionJob)) is not None
    finally:
        off_engine.dispose()
        on_engine.dispose()


def test_v10_database_adds_only_v11_tables_and_preserves_old_sql(tmp_path):
    path = tmp_path / "upgrade.db"
    engine, sessions = create_database(path)
    WorldService(sessions, agent_enabled=False).initialize()
    with sessions() as session:
        session.get(NPC, 1).money = 432.1
        session.commit()
    for table in reversed(V11_TABLES):
        table.drop(engine)
    old_tables = set(inspect(engine).get_table_names())
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
        assert set(inspect(upgraded_engine).get_table_names()) - old_tables == V11_TABLE_NAMES
        with upgraded_engine.connect() as connection:
            assert all(
                connection.exec_driver_sql(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
                ).scalar_one() == sql
                for name, sql in old_sql.items()
            )
        with upgraded_sessions() as session:
            assert session.get(NPC, 1).money == 432.1
    finally:
        upgraded_engine.dispose()


def test_agent_api_is_additive_and_keeps_unsupported_npcs_read_only(tmp_path):
    engine, _sessions, service = _service(
        tmp_path / "api.db", enabled=True, provider=DynamicProvider()
    )
    configure_world_service(service)
    app = FastAPI()
    app.include_router(agent_router)
    try:
        with TestClient(app) as client:
            status = client.get("/api/agent/status")
            assert status.status_code == 200
            assert set(status.json()) == {
                "enabled", "mode", "target_npc_id", "target_npc_name",
                "provider", "jobs", "authority",
            }
            bob = client.get("/api/npcs/2/agent-shadow").json()
            assert bob["supported"] is False and bob["status"] == "unsupported"
            assert client.get("/api/npcs/999/agent-shadow").status_code == 404
    finally:
        engine.dispose()
