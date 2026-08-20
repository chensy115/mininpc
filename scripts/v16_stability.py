from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy import func, select

from database.database import create_database
from database.models import (
    AgentConversation,
    AgentConversationTask,
    AgentConversationTurn,
    AgentDecisionJob,
    AgentPlan,
    AgentReflection,
    AgentReflectionSource,
    AgentReflectionTask,
    AgentTakeoverTurn,
    ModelCallAudit,
    NPC,
    Relationship,
    WorldState,
)
from simulation.agent_brain import AgentDecisionGenerator, AgentSettings
from simulation.agent_cognition import CognitionSettings, ReflectionGenerator
from simulation.agent_conversation import ConversationGenerator, ConversationSettings
from simulation.decision import ACTION_DURATIONS
from simulation.runtime_v16 import ModelRuntime, RuntimeProviderError, RuntimeSettings
from simulation.world import WorldService


class ActionProvider:
    name = "fake-v16-action"

    def __init__(self) -> None:
        self.calls = {npc_id: 0 for npc_id in range(1, 6)}

    async def generate(self, perception: dict) -> str:
        npc_id = int(perception["self"]["id"])
        self.calls[npc_id] += 1
        options = perception["available_actions"]
        social = next((row for row in options if row["action"] == "Socialize" and row["allowed_targets"]), None)
        non_move = [row for row in options if not row["action"].startswith("Go")]
        longest = max(non_move or options, key=lambda row: (ACTION_DURATIONS.get(row["action"], 0), row["action"]))
        option = social if social is not None and self.calls[npc_id] % 6 == 1 else longest
        target = option["allowed_targets"][0] if option.get("allowed_targets") else None
        return json.dumps({
            "emotion": f"stable::{npc_id}", "intention": "保持跨日连续性",
            "action": option["action"], "target": target,
            "dialogue": "聊聊今天的计划" if option["action"] == "Socialize" else None,
            "plan": ["依据最新世界候选继续"],
            "reason_summary": "fake provider 只选 Engine 当前候选",
        }, ensure_ascii=False)


class DialogueProvider:
    name = "fake-v16-dialogue"

    async def generate(self, context: dict) -> str:
        name = context["self"]["name"]
        return json.dumps({
            "speaker": name,
            "utterance": f"{name} 正在按自己的反思继续今天的生活。",
            "emotion_summary": "平静", "intent_summary": "分享当前安排",
            "conversation_act": "share",
        }, ensure_ascii=False)


class ReflectionProvider:
    name = "fake-v16-reflection"

    async def generate(self, context: dict) -> str:
        goal = context["own_goals"][0]
        evidence = goal["source_id"]
        name = context["self"]["name"]
        return json.dumps({
            "day_summary": f"reflection::{name}", "emotion_summary": f"emotion::{name}",
            "lessons": [f"lesson::{name}"], "goal_focus": goal["goal_key"],
            "belief_updates": [{"target": f"goal:{goal['goal_key']}", "belief": f"belief::{name}",
                                "evidence_ids": [evidence], "confidence": 0.72}],
            "plan_steps": [{"goal_key": goal["goal_key"], "action_category": "Relax",
                            "target": None, "description": f"plan::{name}",
                            "start_in_days": 0, "end_in_days": 3, "evidence_ids": [evidence]}],
            "plan_adjustments": [], "reason_summary": f"reason::{name}",
        }, ensure_ascii=False)


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 19, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


class FaultTransport:
    def __init__(self) -> None:
        self.attempts = 0
        self.injected = {"429": 0, "timeout": 0, "5xx": 0}

    async def handler(self, _request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        if self.attempts == 5:
            self.injected["429"] += 1
            return httpx.Response(429, headers={"Retry-After": "0"})
        if self.attempts == 12:
            self.injected["timeout"] += 1
            raise httpx.ReadTimeout("injected")
        if self.attempts == 20:
            self.injected["5xx"] += 1
            return httpx.Response(503)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22},
        })


def _agent_settings(model: str) -> AgentSettings:
    return AgentSettings(
        api_key="offline-fake", base_url="https://example.invalid/v1", model=model,
        timeout_seconds=1.0, max_attempts=1,
    )


def _runtime_settings() -> RuntimeSettings:
    return replace(
        RuntimeSettings.from_env(),
        api_key="offline-fake-runtime", base_url="https://example.invalid/v1",
        model="fake-v16-runtime", timeout_seconds=1.0, max_output_tokens=128,
        max_attempts=2, retry_base_seconds=0.0, max_concurrency=2,
        per_task_concurrency=1, queue_limit=20, calls_per_minute=100,
        calls_per_hour=1000, calls_per_day=1000, calls_per_npc_hour=1000,
        calls_per_npc_day=1000, calls_per_task_hour=1000, calls_per_task_day=1000,
        input_tokens_per_day=10_000_000, output_tokens_per_day=10_000_000,
        total_tokens_per_day=10_000_000, tokens_per_npc_day=10_000_000,
        tokens_per_task_day=10_000_000, circuit_failure_threshold=3,
        circuit_cooldown_seconds=0.01, timezone_name="Asia/Shanghai",
        input_price_per_million=1.0, output_price_per_million=2.0,
        estimated_cost_per_day=100.0, currency="CNY",
    )


async def run_stability(db_path: Path, *, days: int = 30) -> dict:
    engine, sessions = create_database(db_path)
    action_provider = ActionProvider()
    action_generator = AgentDecisionGenerator(_agent_settings("fake-v16-action"), action_provider)
    conversation_generator = ConversationGenerator(
        agent_settings=_agent_settings("fake-v16-dialogue"), provider=DialogueProvider(),
        settings=ConversationSettings(timeout_seconds=1.0, expiry_seconds=900.0,
                                      max_concurrency=5, max_active_conversations=10),
    )
    reflection_generator = ReflectionGenerator(
        agent_settings=_agent_settings("fake-v16-reflection"), provider=ReflectionProvider(),
        settings=CognitionSettings(timeout_seconds=1.0, max_concurrency=5, queue_limit=15,
                                   lease_seconds=3.0, max_reflections_per_day=2),
    )
    clock = MutableClock()
    faults = FaultTransport()
    transport = httpx.MockTransport(faults.handler)
    runtime_config = _runtime_settings()
    runtime = ModelRuntime(sessions, runtime_config, transport=transport, now=clock,
                           sleeper=lambda _delay: asyncio.sleep(0))

    def build_service(active_runtime: ModelRuntime) -> WorldService:
        return WorldService(
            sessions, agent_enabled=False, agent_takeover_npc_ids={1, 2, 3, 4, 5},
            agent_worker_concurrency=5, agent_generator=action_generator,
            agent_conversations_enabled=True, conversation_generator=conversation_generator,
            agent_cognition_npc_ids={1, 2, 3, 4, 5}, reflection_generator=reflection_generator,
            model_runtime=active_runtime,
        )

    service = build_service(runtime)
    service.initialize()
    await runtime.transition("start", npc_ids={1, 2, 3, 4, 5})
    with sessions() as session:
        state = session.get(WorldState, 1)
        for npc in session.scalars(select(NPC).order_by(NPC.id)):
            npc.current_location = "Cafe"
            npc.current_action = "Idle"
            npc.action_end_minute = state.total_minutes
        session.commit()

    formal_ticks = days * 24 * 6
    queue_peaks = {"agent_decisions": 0, "reflections": 0, "conversations": 0}
    paused_refusals = 0
    cold_restarts = 0
    emergency_stops = 0
    explicit_budget_resets = 0
    shutdown_ticks = 0
    try:
        for index in range(formal_ticks):
            assert await service.tick()
            await service.process_agent_decision_jobs(limit=5)
            await service.process_agent_conversation_jobs(limit=5)
            await service.process_agent_reflection_jobs(limit=5)

            if index % 72 == 0 or index == formal_ticks - 1:
                with sessions() as session:
                    queue_peaks["agent_decisions"] = max(queue_peaks["agent_decisions"], session.scalar(
                        select(func.count()).select_from(AgentDecisionJob).where(AgentDecisionJob.status.in_(("pending", "processing")))
                    ) or 0)
                    queue_peaks["reflections"] = max(queue_peaks["reflections"], session.scalar(
                        select(func.count()).select_from(AgentReflectionTask).where(AgentReflectionTask.status.in_(("pending", "processing")))
                    ) or 0)
                    queue_peaks["conversations"] = max(queue_peaks["conversations"], session.scalar(
                        select(func.count()).select_from(AgentConversationTask).where(AgentConversationTask.status.in_(("pending", "processing")))
                    ) or 0)

            if (index + 1) % 144 == 0:
                simulated_day = (index + 1) // 144
                clock.value += timedelta(days=1)
                task_type = ("decision", "conversation", "reflection")[(simulated_day - 1) % 3]
                npc_id = 1 + ((simulated_day - 1) % 5)
                await runtime.generate(task_type, npc_id, {
                    "schema_version": {"decision": "1.3", "conversation": "1.4", "reflection": "1.5"}[task_type],
                    "self": {"id": npc_id, "name": f"NPC-{npc_id}"}, "available_actions": [],
                })
                if simulated_day == 10:
                    await service.pause_runtime()
                    try:
                        await runtime.generate("decision", 1, {"schema_version": "1.3", "self": {"id": 1, "name": "Alice"}})
                    except RuntimeProviderError as exc:
                        assert exc.code == "runtime_not_online"
                        paused_refusals += 1
                    await service.resume_runtime()
                if simulated_day == 15:
                    runtime = ModelRuntime(sessions, runtime_config, transport=transport, now=clock,
                                           sleeper=lambda _delay: asyncio.sleep(0))
                    service = build_service(runtime)
                    service.initialize()
                    await service.recover_agent_decision_jobs()
                    await service.recover_agent_conversation_jobs()
                    await service.recover_agent_reflection_jobs()
                    cold_restarts += 1
                if simulated_day == 20:
                    await service.stop_runtime(emergency=True, reason="injected_stability_stop")
                    emergency_stops += 1
                    await service.start_runtime(set(range(1, 6)))
                if simulated_day == 25:
                    await service.reset_runtime_budget()
                    explicit_budget_resets += 1

        await service.process_agent_decision_jobs(limit=5)
        for _ in range(30):
            reflected = await service.process_agent_reflection_jobs(limit=5)
            conversed = await service.process_agent_conversation_jobs(limit=5)
            if reflected == 0 and conversed == 0:
                break
        service.agent_cognition_enabled = False
        service.agent_cognition_npc_ids.clear()
        service.agent_conversations_enabled = False
        await service.set_all_agent_takeovers(False)
        for _ in range(30):
            assert await service.tick()
            shutdown_ticks += 1
            await service.process_agent_conversation_jobs(limit=5)
            with sessions() as session:
                active = sum((
                    session.scalar(select(func.count()).select_from(AgentDecisionJob).where(AgentDecisionJob.status.in_(("pending", "processing")))) or 0,
                    session.scalar(select(func.count()).select_from(AgentTakeoverTurn).where(AgentTakeoverTurn.state.in_(("waiting", "ready", "agent_executing", "fallback_executing")))) or 0,
                    session.scalar(select(func.count()).select_from(AgentConversation).where(AgentConversation.status.in_(("active", "ready_for_settlement")))) or 0,
                    session.scalar(select(func.count()).select_from(AgentConversationTask).where(AgentConversationTask.status.in_(("pending", "processing")))) or 0,
                ))
            if active == 0:
                break

        with sessions() as session:
            world = session.get(WorldState, 1)
            reflections = list(session.scalars(select(AgentReflection).where(AgentReflection.trigger_type == "daily")))
            plans = list(session.scalars(select(AgentPlan)))
            conversations = list(session.scalars(select(AgentConversation)))
            turns = list(session.scalars(select(AgentConversationTurn)))
            npcs = list(session.scalars(select(NPC).order_by(NPC.id)))
            relationships = list(session.scalars(select(Relationship)))
            audits = list(session.scalars(select(ModelCallAudit).order_by(ModelCallAudit.id)))
            sources_owned = all(
                source.npc_id == session.get(AgentReflectionTask, source.task_id).npc_id
                for source in session.scalars(select(AgentReflectionSource))
            )
            final_queues = {
                "agent_decisions": session.scalar(select(func.count()).select_from(AgentDecisionJob).where(AgentDecisionJob.status.in_(("pending", "processing")))) or 0,
                "reflections": session.scalar(select(func.count()).select_from(AgentReflectionTask).where(AgentReflectionTask.status.in_(("pending", "processing")))) or 0,
                "conversations": session.scalar(select(func.count()).select_from(AgentConversationTask).where(AgentConversationTask.status.in_(("pending", "processing")))) or 0,
            }
            assert world.total_minutes == 480 + formal_ticks * 10 + shutdown_ticks * 10
            assert len(reflections) == days * 5
            assert len({(row.npc_id, row.reflection_day) for row in reflections}) == len(reflections)
            assert all(action_provider.calls[npc_id] > 0 for npc_id in range(1, 6))
            assert sources_owned
            assert conversations and turns
            assert len({(row.conversation_id, row.turn_index) for row in turns}) == len(turns)
            assert {row.actor_npc_id for row in conversations} == set(range(1, 6))
            assert all(0 <= value <= 100 for npc in npcs for value in (
                npc.energy, npc.hunger, npc.mood, npc.social_need, npc.work_satisfaction,
            ))
            assert all(-10_000 <= npc.money <= 1_000_000 for npc in npcs)
            assert all(-100 <= row.score <= 100 for row in relationships)
            assert final_queues == {"agent_decisions": 0, "reflections": 0, "conversations": 0}
            result = {
                "version": "1.6.0", "network_access": False, "real_api_key_used": False,
                "simulation": {"agents": 5, "simulated_days": days, "formal_ticks": formal_ticks,
                               "tick_minutes": 10, "shutdown_ticks": shutdown_ticks,
                               "daily_reflections": len(reflections), "plans": len(plans),
                               "completed_event_plans": sum(row.status == "completed" and row.progress_source_type == "event" for row in plans),
                               "conversations": len(conversations), "conversation_turns": len(turns)},
                "runtime": {"mock_calls": len(audits), "transport_attempts": faults.attempts,
                            "faults_injected": faults.injected, "retried_calls": sum(row.retry_count > 0 for row in audits),
                            "paused_refusals": paused_refusals, "cold_restarts": cold_restarts,
                            "emergency_stops": emergency_stops, "explicit_budget_resets": explicit_budget_resets,
                            "npc_owners": sorted({row.npc_id for row in audits}),
                            "task_types": sorted({row.task_type for row in audits}),
                            "usage_reported": all(row.usage_reported for row in audits)},
                "queues": {"peaks": queue_peaks, "final": final_queues},
                "safety": {"reflection_source_owner_isolation": sources_owned,
                           "unique_conversation_turns": True,
                           "npc_state_ranges": True, "money_range": [min(row.money for row in npcs), max(row.money for row in npcs)],
                           "relationship_range": [min(row.score for row in relationships), max(row.score for row in relationships)]},
            }
        with engine.connect() as connection:
            result["sqlite"] = {
                "integrity_check": connection.exec_driver_sql("PRAGMA integrity_check").scalar_one(),
                "foreign_key_errors": len(connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()),
            }
        assert result["sqlite"] == {"integrity_check": "ok", "foreign_key_errors": 0}
        assert queue_peaks["agent_decisions"] <= 5 and queue_peaks["reflections"] <= 15 and queue_peaks["conversations"] <= 10
        if days >= 30:
            assert faults.injected == {"429": 1, "timeout": 1, "5xx": 1}
            assert result["runtime"]["retried_calls"] == 3
            assert result["runtime"]["npc_owners"] == [1, 2, 3, 4, 5]
            assert result["runtime"]["task_types"] == ["conversation", "decision", "reflection"]
        return result
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    result = asyncio.run(run_stability(args.db_path, days=args.days))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("V16_STABILITY=" + json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
