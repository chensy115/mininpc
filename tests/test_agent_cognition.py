from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select

from api.agent import router as agent_router
from api.dependencies import configure_world_service
from database.database import V15_TABLE_NAMES, create_database
from database.models import (
    AgentCognitionState,
    AgentConversation,
    AgentConversationTask,
    AgentConversationTurn,
    AgentDecisionJob,
    AgentPlan,
    AgentReflection,
    AgentReflectionSource,
    AgentReflectionTask,
    AgentSubjectiveBelief,
    AgentTakeoverTurn,
    Event,
    Memory,
    NPC,
    Relationship,
    WorldState,
)
from simulation.agent_brain import AgentDecisionGenerator, AgentSettings, build_perception_snapshot
from simulation.agent_cognition import (
    CognitionSettings,
    ReflectionGenerator,
    cancel_reflection_task,
    cognition_context_snapshot,
    enqueue_reflection,
    ensure_cognition_states,
    evaluate_plan_progress,
    process_reflection_tasks,
    recover_reflection_tasks,
)
from simulation.agent_conversation import (
    ConversationGenerator,
    ConversationSettings,
    build_turn_context,
    enqueue_social_conversation,
)
from simulation.clock import ClockSnapshot
from simulation.decision import ACTION_DURATIONS
from simulation.events import add_event
from simulation.random_service import RandomService
from simulation.world import WorldService


class ReflectionProvider:
    name = "fake-reflection"

    def __init__(self, mode: str = "ok", delay: float = 0.0, plan_action: str = "Work") -> None:
        self.mode = mode
        self.delay = delay
        self.plan_action = plan_action
        self.contexts: list[dict] = []
        self.active = 0
        self.max_active = 0

    async def generate(self, context: dict) -> str:
        self.contexts.append(context)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.mode == "error":
                raise RuntimeError("provider secret must not be persisted")
            if self.mode == "invalid_json":
                return "not-json"
            goal = context["own_goals"][0]
            evidence = goal["source_id"]
            payload = {
                "day_summary": f"reflection::{context['self']['name']}",
                "emotion_summary": f"emotion::{context['self']['name']}",
                "lessons": [f"lesson::{context['self']['name']}"],
                "goal_focus": goal["goal_key"],
                "belief_updates": [{
                    "target": f"goal:{goal['goal_key']}",
                    "belief": f"belief::{context['self']['name']}",
                    "evidence_ids": [evidence],
                    "confidence": 0.72,
                }],
                "plan_steps": [{
                    "goal_key": goal["goal_key"], "action_category": self.plan_action, "target": None,
                    "description": f"plan::{context['self']['name']}",
                    "start_in_days": 0, "end_in_days": 3, "evidence_ids": [evidence],
                }],
                "plan_adjustments": [],
                "reason_summary": f"reason::{context['self']['name']}",
            }
            if self.mode == "unknown_goal":
                payload["goal_focus"] = "invented-goal"
            elif self.mode == "unknown_action":
                payload["plan_steps"][0]["action_category"] = "BecomeKing"
            elif self.mode == "bad_target":
                payload["plan_steps"][0].update({"action_category": "Socialize", "target": "npc:999"})
            elif self.mode == "fictional_evidence":
                payload["belief_updates"][0]["evidence_ids"] = ["database:everything"]
            elif self.mode == "extra_authority":
                payload["money"] = 1_000_000
            elif self.mode == "control":
                payload["day_summary"] = "bad\x00control"
            return json.dumps(payload, ensure_ascii=False)
        finally:
            self.active -= 1


def _settings(provider=None, *, timeout: float = 0.2, concurrency: int = 3, queue: int = 15):
    agent = AgentSettings(
        api_key="fake-key" if provider is not None else None,
        base_url="https://example.invalid/v1",
        model="fake-v15-model" if provider is not None else "",
        timeout_seconds=timeout,
        max_attempts=1,
    )
    return ReflectionGenerator(
        agent_settings=agent,
        provider=provider,
        settings=CognitionSettings(
            timeout_seconds=timeout, max_concurrency=concurrency, queue_limit=queue,
            lease_seconds=max(0.2, timeout + 0.1), max_reflections_per_day=2,
        ),
    )


def _service(tmp_path, provider=None, *, ids=(1, 2, 3, 4, 5), filename="v15.db", generator=None):
    engine, sessions = create_database(tmp_path / filename)
    service = WorldService(
        sessions,
        agent_enabled=False,
        agent_takeover_enabled=False,
        agent_takeover_npc_ids=set(),
        agent_conversations_enabled=False,
        agent_cognition_npc_ids=set(ids),
        reflection_generator=generator or _settings(provider or ReflectionProvider()),
    )
    service.initialize()
    return engine, sessions, service


def _to_day_boundary(sessions, service):
    with sessions() as session:
        session.get(WorldState, 1).total_minutes = 1430
        session.commit()
    assert asyncio.run(service.tick()) is True


def _facts(sessions):
    with sessions() as session:
        return {
            "npcs": [(row.id, row.money, row.current_location, row.job, row.energy, row.mood)
                     for row in session.scalars(select(NPC).order_by(NPC.id))],
            "relationships": [(row.from_npc_id, row.to_npc_id, row.score)
                              for row in session.scalars(select(Relationship).order_by(Relationship.id))],
        }


def test_v15_default_off_exact_v14_compatibility(monkeypatch, tmp_path):
    monkeypatch.delenv("MINIWORLD_AGENT_COGNITION_ENABLED", raising=False)
    monkeypatch.delenv("MINIWORLD_AGENT_COGNITION_ALL_ENABLED", raising=False)
    monkeypatch.delenv("MINIWORLD_AGENT_COGNITION_NPCS", raising=False)
    engine, sessions = create_database(tmp_path / "default-off.db")
    service = WorldService(sessions)
    service.initialize()
    try:
        assert service.agent_cognition_enabled is False
        before = asyncio.run(service.world_snapshot())
        npc_before = asyncio.run(service.get_npc(1))
        with sessions() as session:
            session.get(WorldState, 1).total_minutes = 1430
            session.commit()
        asyncio.run(service.tick())
        with sessions() as session:
            assert session.scalar(select(func.count()).select_from(AgentCognitionState)) == 0
            assert session.scalar(select(func.count()).select_from(AgentReflectionTask)) == 0
        assert set(before) == set(asyncio.run(service.world_snapshot()))
        assert set(npc_before) == set(asyncio.run(service.get_npc(1)))
    finally:
        engine.dispose()


def test_five_daily_reflections_are_isolated_persistent_and_auditable(tmp_path):
    provider = ReflectionProvider()
    engine, sessions, service = _service(tmp_path, provider)
    try:
        with sessions() as session:
            for npc_id in range(1, 6):
                session.add(Memory(
                    npc_id=npc_id, content=f"PRIVATE-CANARY-{npc_id}", importance=10,
                    emotion="neutral", timestamp=1000,
                ))
            session.commit()
        _to_day_boundary(sessions, service)
        assert asyncio.run(service.process_agent_reflection_jobs(limit=5)) == 5
        with sessions() as session:
            rows = list(session.scalars(select(AgentReflection).order_by(AgentReflection.npc_id)))
            assert [row.npc_id for row in rows] == [1, 2, 3, 4, 5]
            assert len(set(row.day_summary for row in rows)) == 5
            assert session.scalar(select(func.count()).select_from(AgentSubjectiveBelief)) == 5
            assert session.scalar(select(func.count()).select_from(AgentPlan)) == 5
            for row in rows:
                source_owners = set(session.scalars(select(AgentReflectionSource.npc_id).where(
                    AgentReflectionSource.task_id == row.task_id
                )))
                assert source_owners == {row.npc_id}
        assert len(provider.contexts) == 5
        for context in provider.contexts:
            owner = context["self"]["id"]
            serialized = json.dumps(context, ensure_ascii=False)
            assert context["security"]["private_context_owner_npc_id"] == owner
            for npc_id in range(1, 6):
                assert (f"PRIVATE-CANARY-{npc_id}" in serialized) is (npc_id == owner)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("unknown_goal", "unknown_goal"),
        ("unknown_action", "unknown_action"),
        ("bad_target", "invalid_plan_target"),
        ("fictional_evidence", "fictional_evidence"),
        ("extra_authority", "schema_validation_failed"),
        ("control", "schema_validation_failed"),
        ("invalid_json", "invalid_json"),
        ("error", "provider_error"),
    ],
)
def test_strict_validation_falls_back_without_fact_authority(tmp_path, mode, reason):
    provider = ReflectionProvider(mode)
    engine, sessions, service = _service(tmp_path, provider, ids=(1,), filename=f"{mode}.db")
    try:
        _to_day_boundary(sessions, service)
        facts = _facts(sessions)
        assert asyncio.run(service.process_agent_reflection_jobs(limit=1)) == 1
        assert _facts(sessions) == facts
        with sessions() as session:
            reflection = session.scalar(select(AgentReflection))
            assert reflection.fallback_used is True
            assert reflection.failure_reason == reason
            assert reflection.provider == "deterministic-personality"
            assert "secret" not in json.dumps(asyncio.run(service.get_agent_cognition(1)), ensure_ascii=False)
    finally:
        engine.dispose()


def test_no_key_timeout_late_response_and_each_personality_fallback(tmp_path):
    no_key = _settings(None)
    engine, sessions, service = _service(tmp_path, None, ids=(1, 2), generator=no_key)
    try:
        _to_day_boundary(sessions, service)
        assert asyncio.run(service.process_agent_reflection_jobs(limit=2)) == 2
        with sessions() as session:
            rows = list(session.scalars(select(AgentReflection).order_by(AgentReflection.npc_id)))
            assert all(row.failure_reason == "missing_api_key" for row in rows)
            assert rows[0].day_summary != rows[1].day_summary
    finally:
        engine.dispose()

    slow = ReflectionProvider(delay=0.4)
    generator = _settings(slow, timeout=0.01)
    engine, sessions, service = _service(tmp_path, slow, ids=(1,), filename="timeout.db", generator=generator)
    try:
        _to_day_boundary(sessions, service)
        asyncio.run(service.process_agent_reflection_jobs(limit=1))
        with sessions() as session:
            assert session.scalar(select(AgentReflection)).failure_reason == "timeout"
    finally:
        engine.dispose()


def test_dedupe_restart_cancel_queue_bound_and_fair_concurrency(tmp_path):
    provider = ReflectionProvider(delay=0.02)
    generator = _settings(provider, concurrency=2, queue=5)
    engine, sessions, service = _service(tmp_path, provider, generator=generator)
    try:
        with sessions() as session:
            state = session.get(WorldState, 1)
            first = enqueue_reflection(session, 1, 1, state.total_minutes, generator.settings)
            same = enqueue_reflection(session, 1, 1, state.total_minutes, generator.settings)
            assert first.id == same.id
            first.status = "processing"
            first.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            first.lease_token = "dead-lease"
            session.commit()
        with sessions() as session:
            assert recover_reflection_tasks(session) == 1
            session.commit()
        assert asyncio.run(service.process_agent_reflection_jobs(limit=5)) == 1
        with sessions() as session:
            state = session.get(WorldState, 1)
            for npc_id in range(1, 6):
                enqueue_reflection(session, npc_id, 2, state.total_minutes, generator.settings)
            assert session.scalar(select(func.count()).select_from(AgentReflectionTask).where(
                AgentReflectionTask.status.in_(("pending", "processing"))
            )) == 5
            task = session.scalar(select(AgentReflectionTask).where(
                AgentReflectionTask.npc_id == 5, AgentReflectionTask.status == "pending"
            ))
            assert cancel_reflection_task(session, task.id) is True
            session.commit()
        assert asyncio.run(service.process_agent_reflection_jobs(limit=5)) == 4
        assert provider.max_active <= 2
        with sessions() as session:
            completed_owners = set(session.scalars(select(AgentReflection.npc_id)))
            assert {1, 2, 3, 4}.issubset(completed_owners)
            assert session.scalar(select(func.count()).select_from(AgentReflectionTask).where(
                AgentReflectionTask.status.in_(("pending", "processing"))
            )) == 0
    finally:
        engine.dispose()


def test_cognition_injects_only_owner_continuity_into_decision_and_dialogue(tmp_path):
    provider = ReflectionProvider()
    engine, sessions, service = _service(tmp_path, provider, ids=(1, 2))
    try:
        _to_day_boundary(sessions, service)
        asyncio.run(service.process_agent_reflection_jobs(limit=2))
        with sessions() as session:
            state = session.get(WorldState, 1)
            npcs = list(session.scalars(select(NPC).order_by(NPC.id)))
            alice = npcs[0]
            decision, occupants, _ = service._decide_for_npc(
                session, alice, npcs, ClockSnapshot(state.total_minutes), RandomService(state.seed, state.random_counter)
            )
            perception = build_perception_snapshot(
                session, alice, ClockSnapshot(state.total_minutes), occupants, decision
            )
            assert perception["cognition"]["latest_reflection"]["day_summary"] == "reflection::Alice"
            assert "belief::Bob" not in json.dumps(perception, ensure_ascii=False)
            event = add_event(
                session, ClockSnapshot(state.total_minutes), "SOCIAL", "Alice 与 Bob 聊天",
                npc_id=1, target_npc_id=2, location="Cafe",
            )
            session.flush()
            conversation = enqueue_social_conversation(
                session, event, enabled_npc_ids={1, 2},
                settings=ConversationSettings(0.2, 30.0, 2, 10),
            )
            context = build_turn_context(session, conversation, 1, 2, 0)
            assert context["own_cognition"]["latest_reflection"]["day_summary"] == "reflection::Alice"
            assert "belief::Bob" not in json.dumps(context["own_cognition"], ensure_ascii=False)
    finally:
        engine.dispose()


def test_engine_real_events_complete_fail_and_expire_plans(tmp_path):
    provider = ReflectionProvider()
    engine, sessions, service = _service(tmp_path, provider, ids=(1,))
    try:
        _to_day_boundary(sessions, service)
        asyncio.run(service.process_agent_reflection_jobs(limit=1))
        with sessions() as session:
            reflection = session.scalar(select(AgentReflection))
            work = session.scalar(select(AgentPlan))
            failed = AgentPlan(
                npc_id=1, reflection_id=reflection.id, sequence=2, goal_key=work.goal_key,
                action_category="UseFacility", target=None, description="尝试设施",
                evidence_json=work.evidence_json, window_start_day=2, window_end_day=3,
                status="pending", created_minute=1440, updated_minute=1440,
            )
            expired = AgentPlan(
                npc_id=1, reflection_id=reflection.id, sequence=3, goal_key=work.goal_key,
                action_category="Shop", target=None, description="逾期购物",
                evidence_json=work.evidence_json, window_start_day=1, window_end_day=1,
                status="pending", created_minute=1440, updated_minute=1440,
            )
            session.add_all([failed, expired])
            add_event(session, ClockSnapshot(1450), "WORK", "Alice 完成了一段工作", npc_id=1, location="Office")
            add_event(session, ClockSnapshot(1450), "FACILITY", "Alice 未能使用社区设施，安全回退为等待", npc_id=1)
            session.flush()
            assert evaluate_plan_progress(session, ClockSnapshot(1450)) == 3
            session.commit()
            assert work.status == "completed" and work.progress_source_type == "event"
            assert failed.status == "failed" and failed.progress_source_type == "event"
            assert expired.status == "expired" and expired.progress_source_type is None
    finally:
        engine.dispose()


def test_v15_migration_api_and_read_only_consistency(tmp_path):
    path = tmp_path / "migration.db"
    engine, sessions = create_database(path)
    engine.dispose()
    engine, sessions = create_database(path)
    service = WorldService(sessions, agent_cognition_npc_ids={1})
    service.initialize()
    configure_world_service(service)
    app = FastAPI()
    app.include_router(agent_router)
    try:
        assert V15_TABLE_NAMES.issubset(set(inspect(engine).get_table_names()))
        with TestClient(app) as client:
            status = client.get("/api/agent-cognition/status")
            assert status.status_code == 200 and status.json()["version"] == "1.5.0"
            assert client.get("/api/agent-cognition/check").json()["ok"] is True
            assert client.get("/api/agents/1/cognition").status_code == 200
            assert client.get("/api/npcs/1/reflections").json() == []
            assert client.put("/api/agents/2/cognition", json={"enabled": True}).status_code == 200
        with sessions() as session:
            assert session.execute(select(func.count()).select_from(AgentCognitionState)).scalar_one() == 2
            assert session.execute(select(func.count()).select_from(AgentReflectionTask)).scalar_one() == 0
            assert session.connection().exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
            assert session.connection().exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()


class StableActionProvider:
    name = "fake-v15-action"

    def __init__(self) -> None:
        self.calls = {npc_id: 0 for npc_id in range(1, 6)}

    async def generate(self, perception: dict) -> str:
        npc_id = int(perception["self"]["id"])
        self.calls[npc_id] += 1
        options = perception["available_actions"]
        social = next((item for item in options if item["action"] == "Socialize" and item["allowed_targets"]), None)
        non_move = [item for item in options if not item["action"].startswith("Go")]
        longest = max(non_move or options, key=lambda item: (ACTION_DURATIONS.get(item["action"], 0), item["action"]))
        option = social if social is not None and self.calls[npc_id] % 6 == 1 else longest
        target = option["allowed_targets"][0] if option.get("allowed_targets") else None
        return json.dumps({
            "emotion": f"stable::{npc_id}", "intention": "保持跨日连续性",
            "action": option["action"], "target": target,
            "dialogue": "聊聊今天的计划" if option["action"] == "Socialize" else None,
            "plan": ["依据最新世界候选继续"],
            "reason_summary": "fake provider 只选 Engine 当前候选",
        }, ensure_ascii=False)


class StableDialogueProvider:
    name = "fake-v15-dialogue"

    async def generate(self, context: dict) -> str:
        name = context["self"]["name"]
        return json.dumps({
            "speaker": name,
            "utterance": f"{name} 正在按自己的反思继续今天的生活。",
            "emotion_summary": "平静",
            "intent_summary": "分享当前安排",
            "conversation_act": "share",
        }, ensure_ascii=False)


def test_v15_five_agent_reflection_plan_stability_for_fourteen_simulated_days(tmp_path):
    action_provider = StableActionProvider()
    action_settings = AgentSettings(
        api_key="fake-key", base_url="https://example.invalid/v1", model="fake-v15-action",
        timeout_seconds=1.0, max_attempts=1,
    )
    # The deterministic action stream repeatedly completes Relax; choosing the
    # same Engine action category gives the plan monitor real future evidence.
    reflection_provider = ReflectionProvider(plan_action="Relax")
    reflection_generator = ReflectionGenerator(
        agent_settings=AgentSettings(
            api_key="fake-key", base_url="https://example.invalid/v1", model="fake-v15-reflection",
            timeout_seconds=1.0, max_attempts=1,
        ),
        provider=reflection_provider,
        settings=CognitionSettings(
            timeout_seconds=1.0, max_concurrency=5, queue_limit=15,
            lease_seconds=3.0, max_reflections_per_day=2,
        ),
    )
    dialogue_generator = ConversationGenerator(
        agent_settings=AgentSettings(
            api_key="fake-key", base_url="https://example.invalid/v1", model="fake-v15-dialogue",
            timeout_seconds=1.0, max_attempts=1,
        ),
        provider=StableDialogueProvider(),
        settings=ConversationSettings(
            timeout_seconds=1.0, expiry_seconds=900.0, max_concurrency=5,
            max_active_conversations=10,
        ),
    )
    engine, sessions = create_database(tmp_path / "v15-fourteen-days.db")
    service = WorldService(
        sessions,
        agent_enabled=False,
        agent_takeover_npc_ids={1, 2, 3, 4, 5},
        agent_worker_concurrency=5,
        agent_generator=AgentDecisionGenerator(action_settings, action_provider),
        agent_conversations_enabled=True,
        conversation_generator=dialogue_generator,
        agent_cognition_npc_ids={1, 2, 3, 4, 5},
        reflection_generator=reflection_generator,
    )
    service.initialize()
    max_decision_queue = 0
    max_reflection_queue = 0
    max_conversation_queue = 0
    shutdown_ticks = 0

    async def simulate() -> None:
        nonlocal max_decision_queue, max_reflection_queue, max_conversation_queue, shutdown_ticks
        with sessions() as session:
            state = session.get(WorldState, 1)
            for npc in session.scalars(select(NPC).order_by(NPC.id)):
                npc.current_location = "Cafe"
                npc.current_action = "Idle"
                npc.action_end_minute = state.total_minutes
            session.commit()
        for index in range(14 * 24 * 6):
            assert await service.tick()
            await service.process_agent_decision_jobs(limit=5)
            await service.process_agent_conversation_jobs(limit=5)
            await service.process_agent_reflection_jobs(limit=5)
            if index % 72 == 0 or index == 14 * 24 * 6 - 1:
                with sessions() as session:
                    decision_queue = session.scalar(select(func.count()).select_from(AgentDecisionJob).where(
                        AgentDecisionJob.status.in_(("pending", "processing"))
                    )) or 0
                    reflection_queue = session.scalar(select(func.count()).select_from(AgentReflectionTask).where(
                        AgentReflectionTask.status.in_(("pending", "processing"))
                    )) or 0
                    conversation_queue = session.scalar(select(func.count()).select_from(AgentConversationTask).where(
                        AgentConversationTask.status.in_(("pending", "processing"))
                    )) or 0
                    max_decision_queue = max(max_decision_queue, decision_queue)
                    max_reflection_queue = max(max_reflection_queue, reflection_queue)
                    max_conversation_queue = max(max_conversation_queue, conversation_queue)
                    assert decision_queue <= 5
                    assert reflection_queue <= 15
                    assert conversation_queue <= 10
        # Drain the last authorized decision batch before closing all new Agent work.
        await service.process_agent_decision_jobs(limit=5)
        for _ in range(20):
            reflected = await service.process_agent_reflection_jobs(limit=5)
            conversed = await service.process_agent_conversation_jobs(limit=5)
            if reflected == 0 and conversed == 0:
                break
        service.agent_cognition_enabled = False
        service.agent_cognition_npc_ids.clear()
        service.agent_conversations_enabled = False
        await service.set_all_agent_takeovers(False)
        # Existing ready/executing takeover turns are still Engine-settled; no new
        # model work is created once the per-NPC set is empty.
        for _ in range(30):
            assert await service.tick()
            shutdown_ticks += 1
            await service.process_agent_conversation_jobs(limit=5)
            with sessions() as session:
                pending_decisions = session.scalar(select(func.count()).select_from(AgentDecisionJob).where(
                    AgentDecisionJob.status.in_(("pending", "processing"))
                )) or 0
                active_takeovers = session.scalar(select(func.count()).select_from(AgentTakeoverTurn).where(
                    AgentTakeoverTurn.state.in_(("waiting", "ready", "agent_executing", "fallback_executing"))
                )) or 0
                active_conversations = session.scalar(select(func.count()).select_from(AgentConversation).where(
                    AgentConversation.status.in_(("active", "ready_for_settlement"))
                )) or 0
                pending_conversation_tasks = session.scalar(select(func.count()).select_from(AgentConversationTask).where(
                    AgentConversationTask.status.in_(("pending", "processing"))
                )) or 0
            if not (pending_decisions or active_takeovers or active_conversations or pending_conversation_tasks):
                break

    try:
        asyncio.run(asyncio.wait_for(simulate(), timeout=960))
        with sessions() as session:
            state = session.get(WorldState, 1)
            assert state.total_minutes == 480 + 14 * 1440 + shutdown_ticks * 10
            assert shutdown_ticks <= 30
            daily = list(session.scalars(select(AgentReflection).where(
                AgentReflection.trigger_type == "daily"
            ).order_by(AgentReflection.npc_id, AgentReflection.reflection_day)))
            assert len(daily) == 70
            for npc_id in range(1, 6):
                owned = [row for row in daily if row.npc_id == npc_id]
                assert [row.reflection_day for row in owned] == list(range(1, 15))
                assert action_provider.calls[npc_id] > 0
            assert len({(row.npc_id, row.reflection_day) for row in daily}) == 70
            assert all(
                source.npc_id == session.get(AgentReflectionTask, source.task_id).npc_id
                for source in session.scalars(select(AgentReflectionSource))
            )
            plans = list(session.scalars(select(AgentPlan).order_by(AgentPlan.id)))
            assert len(plans) >= 70
            assert all(plan.window_end_day > session.get(AgentReflection, plan.reflection_id).reflection_day for plan in plans)
            assert any(plan.status == "completed" and plan.progress_source_type == "event" for plan in plans)
            assert any(plan.status in {"in_progress", "completed", "expired"} for plan in plans)
            assert session.scalar(select(func.count()).select_from(AgentReflectionTask).where(
                AgentReflectionTask.status.in_(("pending", "processing"))
            )) == 0
            assert session.scalar(select(func.count()).select_from(AgentConversationTask).where(
                AgentConversationTask.status.in_(("pending", "processing"))
            )) == 0
            conversations = list(session.scalars(select(AgentConversation)))
            turns = list(session.scalars(select(AgentConversationTurn)))
            assert conversations and turns
            assert {row.actor_npc_id for row in conversations} == {1, 2, 3, 4, 5}
            assert len({(row.conversation_id, row.turn_index) for row in turns}) == len(turns)
            assert all(row.status in {"completed", "failed", "expired", "cancelled"} for row in conversations)
            npcs = list(session.scalars(select(NPC).order_by(NPC.id)))
            relationships = list(session.scalars(select(Relationship)))
            assert all(0 <= value <= 100 for npc in npcs for value in (
                npc.energy, npc.hunger, npc.mood, npc.social_need, npc.work_satisfaction,
            ))
            assert all(-10_000 <= npc.money <= 1_000_000 for npc in npcs)
            assert all(-100 <= row.score <= 100 for row in relationships)
            final_decision_queue = session.scalar(select(func.count()).select_from(AgentDecisionJob).where(
                AgentDecisionJob.status.in_(("pending", "processing"))
            )) or 0
            final_reflection_queue = session.scalar(select(func.count()).select_from(AgentReflectionTask).where(
                AgentReflectionTask.status.in_(("pending", "processing"))
            )) or 0
            final_conversation_queue = session.scalar(select(func.count()).select_from(AgentConversationTask).where(
                AgentConversationTask.status.in_(("pending", "processing"))
            )) or 0
            assert final_decision_queue == final_reflection_queue == final_conversation_queue == 0
            completed_event_plans = sum(
                plan.status == "completed" and plan.progress_source_type == "event" for plan in plans
            )
            audit = {
                "formal_ticks": 14 * 24 * 6,
                "shutdown_ticks": shutdown_ticks,
                "daily_reflections": len(daily),
                "plans": len(plans),
                "completed_event_plans": completed_event_plans,
                "conversations": len(conversations),
                "conversation_turns": len(turns),
                "npc_money_range": {
                    "minimum": min(npc.money for npc in npcs),
                    "maximum": max(npc.money for npc in npcs),
                },
                "relationship_score_range": {
                    "minimum": min(row.score for row in relationships),
                    "maximum": max(row.score for row in relationships),
                },
                "queue_peaks": {
                    "agent_decisions": max_decision_queue,
                    "reflections": max_reflection_queue,
                    "conversations": max_conversation_queue,
                },
                "queue_final": {
                    "agent_decisions": final_decision_queue,
                    "reflections": final_reflection_queue,
                    "conversations": final_conversation_queue,
                },
            }
        assert max_decision_queue <= 5 and max_reflection_queue <= 15 and max_conversation_queue <= 10
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
        print("V15_STABILITY_AUDIT=" + json.dumps(audit, ensure_ascii=False, sort_keys=True))
    finally:
        engine.dispose()
