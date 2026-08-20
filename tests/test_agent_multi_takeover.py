from __future__ import annotations

import asyncio
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from itertools import chain, combinations
from pathlib import Path

import pytest
from sqlalchemy import func, inspect, select

from database.database import create_database
from database.models import (
    AgentDecisionJob,
    AgentTakeoverTurn,
    DecisionLog,
    Memory,
    NPC,
    Relationship,
    WorldState,
)
from simulation.agent_brain import AgentDecisionGenerator, AgentSettings
from simulation.decision import ACTION_DURATIONS
from simulation.world import WorldService


NPC_IDS = frozenset(range(1, 6))


def _legal_choice(perception: dict, *, longest: bool = False) -> tuple[str, str | None]:
    options = perception["available_actions"]
    if longest:
        option = max(
            options,
            key=lambda item: (ACTION_DURATIONS.get(item["action"], 0), item["action"]),
        )
    else:
        option = next((item for item in options if item["action"] == "Idle"), options[0])
    targets = option.get("allowed_targets", [])
    return option["action"], targets[0] if targets else None


def _response(perception: dict, action: str, target: str | None, call: int) -> str:
    npc_id = int(perception["self"]["id"])
    return json.dumps(
        {
            "emotion": f"emotion::{npc_id}",
            "intention": f"intention::{npc_id}::{call}",
            "action": action,
            "target": target,
            "dialogue": f"dialogue::{npc_id}" if action == "Socialize" else None,
            "plan": [f"plan::{npc_id}::{call}"],
            "reason_summary": f"reason::{npc_id}::{call}",
        },
        ensure_ascii=False,
    )


class CanaryProvider:
    """Deterministic five-brain provider. It never performs network I/O."""

    name = "fake-five-agent"

    def __init__(self, *, longest: bool = False, retain_perceptions: bool = True) -> None:
        self.longest = longest
        self.retain_perceptions = retain_perceptions
        self.calls: dict[int, int] = defaultdict(int)
        self.perceptions: dict[int, list[dict]] = defaultdict(list)
        self.call_order: list[int] = []

    async def generate(self, perception: dict) -> str:
        npc_id = int(perception["self"]["id"])
        self.calls[npc_id] += 1
        self.call_order.append(npc_id)
        if self.retain_perceptions:
            self.perceptions[npc_id].append(perception)
        action, target = _legal_choice(perception, longest=self.longest)
        return _response(perception, action, target, self.calls[npc_id])


class MoveProvider(CanaryProvider):
    async def generate(self, perception: dict) -> str:
        npc_id = int(perception["self"]["id"])
        self.calls[npc_id] += 1
        self.call_order.append(npc_id)
        if self.retain_perceptions:
            self.perceptions[npc_id].append(perception)
        option = next(
            item
            for item in perception["available_actions"]
            if item["action"].startswith("Go") and item.get("allowed_targets")
        )
        return _response(
            perception,
            option["action"],
            option["allowed_targets"][0],
            self.calls[npc_id],
        )


def _generator(
    provider,
    *,
    attempts: int = 1,
    timeout: float = 0.5,
) -> AgentDecisionGenerator:
    return AgentDecisionGenerator(
        settings=AgentSettings(
            api_key="fake-key",
            base_url="https://example.invalid/v1",
            model="fake-v13-model",
            timeout_seconds=timeout,
            max_attempts=attempts,
        ),
        provider=provider,
    )


def _service(
    path: Path,
    *,
    enabled_ids=(),
    provider=None,
    attempts: int = 1,
    timeout: float = 0.5,
    concurrency: int = 5,
):
    engine, sessions = create_database(path)
    fake = provider or CanaryProvider()
    service = WorldService(
        sessions,
        economy_enabled=True,
        career_budget_enabled=True,
        community_enabled=True,
        social_life_enabled=True,
        life_story_enabled=True,
        product_enabled=True,
        agent_enabled=False,
        agent_takeover_enabled=False,
        agent_takeover_npc_ids=set(enabled_ids),
        agent_worker_concurrency=concurrency,
        agent_generator=_generator(fake, attempts=attempts, timeout=timeout),
    )
    service.initialize()
    return engine, sessions, service, fake


def _turns_by_npc(sessions) -> dict[int, list[AgentTakeoverTurn]]:
    with sessions() as session:
        rows = list(session.scalars(select(AgentTakeoverTurn).order_by(AgentTakeoverTurn.id)))
        grouped: dict[int, list[AgentTakeoverTurn]] = defaultdict(list)
        for row in rows:
            session.expunge(row)
            grouped[row.npc_id].append(row)
        return grouped


async def _create_process_resolve(service: WorldService, *, limit: int = 5) -> None:
    assert await service.tick()
    assert await service.process_agent_decision_jobs(limit=limit) >= 1
    assert await service.tick()


def _powerset(values: frozenset[int]):
    ordered = sorted(values)
    return chain.from_iterable(combinations(ordered, size) for size in range(len(ordered) + 1))


def test_default_off_and_v12_alice_switch_remain_compatible(monkeypatch, tmp_path):
    for name in (
        "MINIWORLD_AGENT_SHADOW_ENABLED",
        "MINIWORLD_AGENT_TAKEOVER_ENABLED",
        "MINIWORLD_AGENT_TAKEOVER_ALL_ENABLED",
        "MINIWORLD_AGENT_TAKEOVER_NPCS",
    ):
        monkeypatch.delenv(name, raising=False)
    engine, sessions = create_database(tmp_path / "default-off.db")
    service = WorldService(
        sessions,
        agent_enabled=None,
        agent_takeover_enabled=None,
        agent_generator=_generator(CanaryProvider()),
    )
    service.initialize()
    try:
        for _ in range(24):
            assert asyncio.run(service.tick())
        with sessions() as session:
            assert session.scalar(select(func.count()).select_from(AgentTakeoverTurn)) == 0
            assert session.scalar(select(func.count()).select_from(AgentDecisionJob)) == 0
        status = asyncio.run(service.agent_takeover_status())
        assert status["enabled"] is False
        assert status["target_npc_id"] == 1
        assert status["target_npc_name"] == "Alice"
        assert status["enabled_npc_ids"] == []

        enabled = asyncio.run(service.set_agent_takeover(True))
        assert enabled["enabled"] is True
        assert enabled["enabled_npc_ids"] == [1]
        assert service.agent_takeover_npc_ids == {1}
        disabled = asyncio.run(service.set_agent_takeover(False))
        assert disabled["enabled"] is False
        assert service.agent_takeover_npc_ids == set()
    finally:
        engine.dispose()


def test_each_of_five_npcs_can_take_over_legally_and_audit_its_own_turn(tmp_path):
    engine, sessions, service, provider = _service(tmp_path / "individual.db")

    async def scenario() -> None:
        for npc_id in sorted(NPC_IDS):
            with sessions() as session:
                state = session.get(WorldState, 1)
                npc = session.get(NPC, npc_id)
                npc.current_action = "Idle"
                npc.pending_location = None
                npc.action_end_minute = state.total_minutes
                session.commit()
            control = await service.set_npc_agent_takeover(npc_id, True)
            assert control["enabled"] is True
            assert await service.tick()
            assert await service.process_agent_decision_jobs(limit=5) == 1
            assert await service.tick()
            turn = (await service.agent_audits(npc_id, limit=10))[0]
            assert turn["npc_id"] == npc_id
            assert turn["final"]["source"] == "agent"
            assert turn["agent"]["emotion"] == f"emotion::{npc_id}"
            assert turn["agent"]["plan"] == [f"plan::{npc_id}::1"]
            await service.set_npc_agent_takeover(npc_id, False)
            assert await service.tick()  # complete the ten-minute Idle action

    try:
        asyncio.run(scenario())
        grouped = _turns_by_npc(sessions)
        assert set(grouped) == NPC_IDS
        assert all(len(grouped[npc_id]) == 1 for npc_id in NPC_IDS)
        assert provider.calls == {npc_id: 1 for npc_id in NPC_IDS}
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "enabled_ids",
    [frozenset({2, 4}), frozenset({1, 3, 5}), NPC_IDS],
    ids=["partial-even", "partial-odd", "all-five"],
)
def test_partial_and_all_takeover_create_turns_only_for_selected_npcs(tmp_path, enabled_ids):
    engine, sessions, service, provider = _service(
        tmp_path / ("scope-" + "-".join(map(str, sorted(enabled_ids))) + ".db"),
        enabled_ids=enabled_ids,
    )
    try:
        asyncio.run(_create_process_resolve(service))
        grouped = _turns_by_npc(sessions)
        assert set(grouped) == set(enabled_ids)
        assert set(provider.calls) == set(enabled_ids)
        assert all(grouped[npc_id][0].final_source == "agent" for npc_id in enabled_ids)
        overview = asyncio.run(service.agent_takeover_overview())
        assert overview["enabled_npc_ids"] == sorted(enabled_ids)
        assert overview["global_enabled"] is (enabled_ids == NPC_IDS)
        assert overview["worker"]["queue_limit"] == 5
        assert overview["worker"]["bounded"] is True
    finally:
        engine.dispose()


def test_every_one_of_32_takeover_combinations_is_selectable(tmp_path):
    engine, _sessions, service, _provider = _service(tmp_path / "all-combinations.db")

    async def scenario() -> None:
        for subset_tuple in _powerset(NPC_IDS):
            expected = set(subset_tuple)
            await service.set_all_agent_takeovers(False)
            for npc_id in expected:
                await service.set_npc_agent_takeover(npc_id, True)
            overview = await service.agent_takeover_overview()
            assert overview["enabled_npc_ids"] == sorted(expected)
            assert overview["enabled"] is bool(expected)
            assert overview["global_enabled"] is (expected == set(NPC_IDS))

    try:
        asyncio.run(scenario())
    finally:
        engine.dispose()


def test_memory_relationship_plan_and_emotion_canaries_never_cross_npcs(tmp_path):
    provider = CanaryProvider()
    engine, sessions, service, _ = _service(
        tmp_path / "isolation.db", enabled_ids=NPC_IDS, provider=provider
    )
    expected_relationships: dict[int, dict[int, int]] = {}
    try:
        with sessions() as session:
            state = session.get(WorldState, 1)
            npcs = list(session.scalars(select(NPC).order_by(NPC.id)))
            for npc in npcs:
                npc.current_location = "Cafe"
                npc.current_action = "Idle"
                npc.action_end_minute = state.total_minutes
                session.add(
                    Memory(
                        npc_id=npc.id,
                        content=f"private-memory::{npc.id}",
                        importance=10,
                        emotion=f"private-emotion::{npc.id}",
                        timestamp=state.total_minutes,
                    )
                )
            for relation in session.scalars(select(Relationship)):
                relation.score = relation.from_npc_id * 10 + relation.to_npc_id
            session.commit()
            for npc_id in NPC_IDS:
                expected_relationships[npc_id] = {
                    row.to_npc_id: row.score
                    for row in session.scalars(
                        select(Relationship).where(Relationship.from_npc_id == npc_id)
                    )
                }

        async def scenario() -> None:
            await _create_process_resolve(service)
            # Idle lasts ten minutes. This completes turn one and queues turn two,
            # whose perception must contain only this NPC's previous Agent plan.
            assert await service.tick()
            assert await service.process_agent_decision_jobs(limit=5) == 5

        asyncio.run(scenario())

        for npc_id in NPC_IDS:
            first, second = provider.perceptions[npc_id][:2]
            assert first["self"]["id"] == npc_id
            memory_canaries = {
                item["content"]
                for item in first["relevant_memories"]
                if item["content"].startswith("private-memory::")
            }
            assert memory_canaries == {f"private-memory::{npc_id}"}
            observed_relationships = {
                item["npc_id"]: item["score"] for item in first["relevant_relationships"]
            }
            assert observed_relationships == expected_relationships[npc_id]
            carried_plans = [item for item in second["plans"] if item["kind"] == "agent_plan"]
            assert len(carried_plans) == 1
            assert carried_plans[0]["items"] == [f"plan::{npc_id}::1"]
            assert f"::{npc_id}::" in carried_plans[0]["intention"]

            audits = asyncio.run(service.agent_audits(npc_id, limit=10))
            completed = next(item for item in audits if item["state"] == "completed")
            assert completed["agent"]["emotion"] == f"emotion::{npc_id}"
            assert completed["agent"]["plan"] == [f"plan::{npc_id}::1"]
            assert all(item["npc_id"] == npc_id for item in audits)
    finally:
        engine.dispose()


def test_bounded_batches_are_fifo_fair_across_all_five_npcs(tmp_path):
    provider = CanaryProvider()
    engine, sessions, service, _ = _service(
        tmp_path / "fairness.db",
        enabled_ids=NPC_IDS,
        provider=provider,
        concurrency=2,
    )
    try:
        assert asyncio.run(service.tick())
        with sessions() as session:
            depth = session.scalar(
                select(func.count()).select_from(AgentDecisionJob).where(
                    AgentDecisionJob.status.in_(("pending", "processing"))
                )
            )
            assert depth == 5
        assert asyncio.run(service.process_agent_decision_jobs(limit=2)) == 2
        assert provider.call_order == [1, 2]
        assert asyncio.run(service.process_agent_decision_jobs(limit=2)) == 2
        assert provider.call_order == [1, 2, 3, 4]
        assert asyncio.run(service.process_agent_decision_jobs(limit=2)) == 1
        assert provider.call_order == [1, 2, 3, 4, 5]
        assert set(provider.calls) == NPC_IDS
    finally:
        engine.dispose()


def test_one_slow_and_one_failed_npc_do_not_block_world_or_siblings(tmp_path):
    class SlowFailProvider(CanaryProvider):
        def __init__(self) -> None:
            super().__init__()
            self.slow_started = asyncio.Event()
            self.release_slow = asyncio.Event()

        async def generate(self, perception: dict) -> str:
            npc_id = int(perception["self"]["id"])
            if npc_id == 1:
                self.slow_started.set()
                await self.release_slow.wait()
            if npc_id == 2:
                raise RuntimeError("private fake failure detail")
            return await super().generate(perception)

    async def scenario() -> None:
        provider = SlowFailProvider()
        engine, sessions, service, _ = _service(
            tmp_path / "slow-fail.db",
            enabled_ids=NPC_IDS,
            provider=provider,
            attempts=1,
            timeout=1.0,
            concurrency=5,
        )
        try:
            assert await service.tick()
            with sessions() as session:
                before = session.get(WorldState, 1).total_minutes
            worker = asyncio.create_task(service.process_agent_decision_jobs(limit=5))
            await asyncio.wait_for(provider.slow_started.wait(), timeout=0.5)
            for _ in range(50):
                with sessions() as session:
                    states = {
                        turn.npc_id: (turn.state, turn.worker_state)
                        for turn in session.scalars(select(AgentTakeoverTurn))
                    }
                if states.get(2) == ("waiting", "failed") and all(
                    states.get(npc_id) == ("ready", "completed") for npc_id in (3, 4, 5)
                ):
                    break
                await asyncio.sleep(0.01)
            assert not worker.done()
            assert states[1] == ("waiting", "processing")
            assert states[2] == ("waiting", "failed")
            assert all(states[npc_id] == ("ready", "completed") for npc_id in (3, 4, 5))

            assert await asyncio.wait_for(service.tick(), timeout=0.8)
            with sessions() as session:
                assert session.get(WorldState, 1).total_minutes == before + 10
                turns = {turn.npc_id: turn for turn in session.scalars(select(AgentTakeoverTurn))}
                assert turns[1].state == "waiting"
                assert turns[2].final_source == "utility_fallback"
                assert turns[2].fallback_reason_code == "provider_error"
                assert all(turns[npc_id].final_source == "agent" for npc_id in (3, 4, 5))

            provider.release_slow.set()
            assert await asyncio.wait_for(worker, timeout=0.8) == 5
            assert await service.tick()
            # NPCs 2-5 finish their ten-minute first action on this tick and may
            # already own a second waiting turn. Inspect the first audit instead
            # of accidentally overwriting it with that newer row.
            first_turns = {npc_id: rows[0] for npc_id, rows in _turns_by_npc(sessions).items()}
            assert first_turns[1].final_source == "agent"
            assert first_turns[2].final_source == "utility_fallback"
            assert all(
                first_turns[npc_id].final_source == "agent" for npc_id in (1, 3, 4, 5)
            )
        finally:
            engine.dispose()

    asyncio.run(scenario())


def test_all_five_processing_leases_recover_after_restart_without_duplicates(tmp_path):
    path = tmp_path / "restart-five.db"
    first_engine, sessions, first, _ = _service(path, enabled_ids=NPC_IDS)
    assert asyncio.run(first.tick())
    with sessions() as session:
        turns = list(session.scalars(select(AgentTakeoverTurn).order_by(AgentTakeoverTurn.id)))
        assert len(turns) == 5
        for turn in turns:
            job = session.get(AgentDecisionJob, turn.job_id)
            turn.worker_state = "processing"
            turn.lease_token = f"crashed-worker::{turn.npc_id}"
            turn.lease_expires_at = None
            job.status = "processing"
        session.commit()
    first_engine.dispose()

    second_engine, second_sessions, second, provider = _service(
        path, enabled_ids=NPC_IDS, provider=CanaryProvider()
    )
    try:
        assert asyncio.run(second.recover_agent_decision_jobs()) >= 5
        assert asyncio.run(second.process_agent_decision_jobs(limit=5)) == 5
        assert asyncio.run(second.tick())
        grouped = _turns_by_npc(second_sessions)
        assert set(grouped) == NPC_IDS
        assert all(len(grouped[npc_id]) == 1 for npc_id in NPC_IDS)
        assert all(grouped[npc_id][0].final_source == "agent" for npc_id in NPC_IDS)
        assert provider.calls == {npc_id: 1 for npc_id in NPC_IDS}
    finally:
        second_engine.dispose()


def test_identity_tampering_falls_back_for_each_npc_without_calling_provider(tmp_path):
    provider = CanaryProvider()
    engine, sessions, service, _ = _service(
        tmp_path / "identity-tamper.db", enabled_ids=NPC_IDS, provider=provider
    )
    try:
        assert asyncio.run(service.tick())
        with sessions() as session:
            for job in session.scalars(select(AgentDecisionJob).order_by(AgentDecisionJob.npc_id)):
                perception = json.loads(job.perception_json)
                perception["self"]["id"] = job.npc_id % 5 + 1
                job.perception_json = json.dumps(perception, ensure_ascii=False)
            session.commit()
        # Identity validation rejects each job before provider.generate.
        assert asyncio.run(service.process_agent_decision_jobs(limit=5)) == 0
        assert provider.calls == {}
        assert asyncio.run(service.tick())
        grouped = _turns_by_npc(sessions)
        for npc_id in NPC_IDS:
            turn = grouped[npc_id][0]
            assert turn.final_source == "utility_fallback"
            assert turn.fallback_reason_code == "context_npc_mismatch"
            assert turn.last_error_code == "context_npc_mismatch"
    finally:
        engine.dispose()


def test_agent_target_parameter_tampering_is_caught_by_engine_for_each_npc(tmp_path):
    provider = MoveProvider()
    engine, sessions, service, _ = _service(
        tmp_path / "parameter-tamper.db", enabled_ids=NPC_IDS, provider=provider
    )
    try:
        with sessions() as session:
            state = session.get(WorldState, 1)
            for npc in session.scalars(select(NPC)):
                npc.current_location = "Home"
                npc.current_action = "Idle"
                npc.action_end_minute = state.total_minutes
            session.commit()
        assert asyncio.run(service.tick())
        assert asyncio.run(service.process_agent_decision_jobs(limit=5)) == 5
        with sessions() as session:
            for turn in session.scalars(select(AgentTakeoverTurn)):
                assert turn.state == "ready"
                advice = json.loads(turn.agent_decision_json)
                assert advice["action"].startswith("Go")
                advice["target"] = f"forged-target::{turn.npc_id}"
                turn.agent_decision_json = json.dumps(advice, ensure_ascii=False)
                turn.agent_target = advice["target"]
            session.commit()
        assert asyncio.run(service.tick())
        grouped = _turns_by_npc(sessions)
        for npc_id in NPC_IDS:
            turn = grouped[npc_id][0]
            assert turn.final_source == "utility_fallback"
            assert turn.fallback_reason_code in {
                "invalid_move_target",
                "snapshot_invalid_move_target",
            }
            assert turn.final_target is None
    finally:
        engine.dispose()


def _rewrite_as_v12_alice_only(path: Path, current_sql: str) -> None:
    old_sql = current_sql.replace(
        "CONSTRAINT ck_agent_takeover_state",
        "CONSTRAINT ck_agent_takeover_alice_only CHECK (npc_id = 1), "
        "CONSTRAINT ck_agent_takeover_state",
        1,
    )
    assert old_sql != current_sql
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP INDEX IF EXISTS uq_agent_takeover_active_npc")
        connection.execute(
            'ALTER TABLE "agent_takeover_turns" RENAME TO "agent_takeover_turns_v12_seed"'
        )
        connection.execute(old_sql)
        columns = [
            row[1]
            for row in connection.execute('PRAGMA table_info("agent_takeover_turns")')
        ]
        column_sql = ", ".join(f'"{column}"' for column in columns)
        connection.execute(
            f'INSERT INTO "agent_takeover_turns" ({column_sql}) '
            f'SELECT {column_sql} FROM "agent_takeover_turns_v12_seed"'
        )
        connection.execute('DROP TABLE "agent_takeover_turns_v12_seed"')
        connection.execute(
            "CREATE UNIQUE INDEX uq_agent_takeover_active_npc "
            "ON agent_takeover_turns (npc_id) "
            "WHERE state IN ('waiting', 'ready', 'agent_executing', 'fallback_executing')"
        )
        connection.commit()
    finally:
        connection.close()


def test_v12_to_v13_takeover_migration_is_idempotent_and_preserves_audit(tmp_path):
    path = tmp_path / "v12-to-v13.db"
    engine, sessions, service, _ = _service(path)
    with sessions() as session:
        state = session.get(WorldState, 1)
        decision = DecisionLog(
            npc_id=1,
            world_day=1,
            world_time="08:00",
            chosen_action="Idle",
            candidates_json="[]",
            reason_json='{"summary":"legacy-v12"}',
        )
        session.add(decision)
        session.flush()
        session.add(
            AgentTakeoverTurn(
                decision_id=decision.id,
                npc_id=1,
                state="completed",
                worker_state="not_queued",
                response_deadline_at=datetime.now(timezone.utc),
                created_minute=state.total_minutes,
                valid_until_minute=state.total_minutes + 30,
                options_json="[]",
                utility_action="Idle",
                utility_reason_json='{"summary":"legacy-v12"}',
                final_source="utility_fallback",
                final_action="Idle",
                final_params_json="{}",
                fallback_reason_code="legacy-v12",
                action_started_minute=state.total_minutes,
                action_end_minute=state.total_minutes + 10,
                action_completed_minute=state.total_minutes + 10,
                completion_json='{"status":"completed"}',
            )
        )
        session.commit()
    current_sql = inspect(engine).get_table_names()
    assert "agent_takeover_turns" in current_sql
    with engine.connect() as connection:
        table_sql = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='agent_takeover_turns'"
        ).scalar_one()
    engine.dispose()

    _rewrite_as_v12_alice_only(path, table_sql)
    upgraded, upgraded_sessions = create_database(path)
    try:
        with upgraded.connect() as connection:
            first_sql = connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='agent_takeover_turns'"
            ).scalar_one()
            assert "ck_agent_takeover_alice_only" not in first_sql
            assert "npc_id = 1" not in first_sql
            assert connection.exec_driver_sql(
                "SELECT COUNT(*) FROM agent_takeover_turns WHERE fallback_reason_code='legacy-v12'"
            ).scalar_one() == 1
            assert connection.exec_driver_sql(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'agent_takeover_turns_v12_%'"
            ).scalar_one() == 0
        upgraded.dispose()
        reopened, _ = create_database(path)
        with reopened.connect() as connection:
            second_sql = connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='agent_takeover_turns'"
            ).scalar_one()
            assert second_sql == first_sql
            assert connection.exec_driver_sql(
                "SELECT COUNT(*) FROM agent_takeover_turns WHERE fallback_reason_code='legacy-v12'"
            ).scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
        reopened.dispose()
    finally:
        upgraded.dispose()


def test_five_agent_seven_day_stability_has_bounded_queue_and_closed_audits(tmp_path):
    provider = CanaryProvider(longest=True, retain_perceptions=False)
    engine, sessions, service, _ = _service(
        tmp_path / "five-agent-seven-days.db",
        enabled_ids=NPC_IDS,
        provider=provider,
        concurrency=5,
        timeout=1.0,
    )
    max_queue_depth = 0
    max_active_turns = 0

    async def simulate() -> None:
        nonlocal max_queue_depth, max_active_turns
        for tick_index in range(7 * 24 * 6):
            assert await service.tick()
            await service.process_agent_decision_jobs(limit=5)
            if tick_index % 48 == 0 or tick_index == 7 * 24 * 6 - 1:
                with sessions() as session:
                    queue_depth = session.scalar(
                        select(func.count()).select_from(AgentDecisionJob).where(
                            AgentDecisionJob.status.in_(("pending", "processing"))
                        )
                    ) or 0
                    active_turns = session.scalar(
                        select(func.count()).select_from(AgentTakeoverTurn).where(
                            AgentTakeoverTurn.state.in_(
                                ("waiting", "ready", "agent_executing", "fallback_executing")
                            )
                        )
                    ) or 0
                    max_queue_depth = max(max_queue_depth, queue_depth)
                    max_active_turns = max(max_active_turns, active_turns)
                    assert queue_depth <= 5
                    assert active_turns <= 5

    try:
        # Five independent audit streams intentionally exercise substantially
        # more SQLite work than the V1.2 Alice-only run. Keep the full 1008
        # Engine ticks and allow slower Windows/CI filesystems enough wall time.
        asyncio.run(asyncio.wait_for(simulate(), timeout=360))
        with sessions() as session:
            state = session.get(WorldState, 1)
            assert state.total_minutes == 480 + 7 * 1440
            npcs = list(session.scalars(select(NPC).order_by(NPC.id)))
            assert [npc.id for npc in npcs] == sorted(NPC_IDS)
            for npc in npcs:
                assert all(
                    0 <= value <= 100
                    for value in (
                        npc.energy,
                        npc.hunger,
                        npc.mood,
                        npc.social_need,
                        npc.work_satisfaction,
                    )
                )
                assert -10000 <= npc.money <= 1_000_000
            assert all(
                -100 <= relation.score <= 100
                for relation in session.scalars(select(Relationship))
            )
            turns = list(session.scalars(select(AgentTakeoverTurn).order_by(AgentTakeoverTurn.id)))
            assert turns
            assert len({turn.decision_id for turn in turns}) == len(turns)
            assert sum(turn.state != "completed" for turn in turns) <= 5
            assert all(
                turn.final_source in {"agent", "utility_fallback"}
                for turn in turns
                if turn.state in {"agent_executing", "fallback_executing", "completed"}
            )
            for npc_id in NPC_IDS:
                owned = [turn for turn in turns if turn.npc_id == npc_id]
                assert owned
                assert any(turn.final_source == "agent" for turn in owned)
                assert provider.calls[npc_id] > 0
            queue_depth = session.scalar(
                select(func.count()).select_from(AgentDecisionJob).where(
                    AgentDecisionJob.status.in_(("pending", "processing"))
                )
            ) or 0
            assert queue_depth <= 5
        assert max_queue_depth <= 5
        assert max_active_turns <= 5
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    finally:
        engine.dispose()
