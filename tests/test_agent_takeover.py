from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select

from api.agent import router as agent_router
from api.dependencies import configure_world_service
from database.database import V12_TABLES, create_database
from database.models import (
    AgentDecisionJob,
    AgentTakeoverTurn,
    CareerDevelopment,
    DecisionLog,
    InventoryItem,
    ItemDefinition,
    Memory,
    NPC,
    PersonalBudget,
    WorldState,
)
from simulation.agent_brain import AgentDecisionGenerator, AgentSettings
from simulation.world import WorldService
from simulation.agent_takeover import build_action_options, validate_action_selection
from simulation.clock import ClockSnapshot


class ChoosingProvider:
    """Deterministic V1.2 provider; it never performs network I/O."""

    name = "fake-takeover"

    def __init__(
        self,
        action: str | None = None,
        target: str | None = None,
        chooser: Callable[[dict], tuple[str, str | None]] | None = None,
    ) -> None:
        self.action = action
        self.target = target
        self.chooser = chooser
        self.calls = 0
        self.perceptions: list[dict] = []

    async def generate(self, perception: dict) -> str:
        self.calls += 1
        self.perceptions.append(perception)
        if self.chooser is not None:
            action, target = self.chooser(perception)
        elif self.action is not None:
            action, target = self.action, self.target
        else:
            candidate = perception["available_actions"][0]
            action = candidate["action"]
            targets = candidate.get("allowed_targets", [])
            target = targets[0] if targets else None
        return json.dumps(
            {
                "emotion": "专注",
                "intention": "执行一个经过 Engine 验证的下一步行动",
                "action": action,
                "target": target,
                "dialogue": "一起聊聊吧" if action == "Socialize" else None,
                "plan": ["完成当前行动", "随后根据最新可见事实重新评估"],
                "reason_summary": "只依据受限感知和 Engine 给出的候选集合选择。",
            },
            ensure_ascii=False,
        )


class BlockingProvider(ChoosingProvider):
    name = "fake-blocking-takeover"

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(self, perception: dict) -> str:
        self.started.set()
        await self.release.wait()
        return await super().generate(perception)


def _generator(provider=None, *, api_key: str | None = "fake-key", timeout: float = 0.2):
    return AgentDecisionGenerator(
        AgentSettings(
            api_key=api_key,
            base_url="https://example.invalid",
            model="fake-model",
            timeout_seconds=timeout,
            max_attempts=1,
        ),
        provider=provider,
    )


def _service(path, *, takeover: bool, provider=None, api_key: str | None = "fake-key", timeout=0.2):
    engine, sessions = create_database(path)
    service = WorldService(
        sessions,
        agent_enabled=True,
        agent_takeover_enabled=takeover,
        agent_generator=_generator(provider, api_key=api_key, timeout=timeout),
    )
    service.initialize()
    return engine, sessions, service


def _legacy_snapshot(sessions):
    """Only V1.0-and-earlier facts: V1.2 audit rows are intentionally excluded."""
    with sessions() as session:
        state = session.get(WorldState, 1)
        return {
            "world": (
                state.total_minutes,
                state.paused,
                state.speed,
                state.seed,
                state.random_counter,
            ),
            "npcs": [
                (
                    row.id,
                    row.current_location,
                    row.current_action,
                    row.action_end_minute,
                    row.pending_location,
                    row.last_move_minute,
                    row.money,
                    row.energy,
                    row.hunger,
                    row.mood,
                    row.social_need,
                    row.work_satisfaction,
                )
                for row in session.scalars(select(NPC).order_by(NPC.id))
            ],
            "decisions": [
                (
                    row.id,
                    row.npc_id,
                    row.world_day,
                    row.world_time,
                    row.chosen_action,
                    row.candidates_json,
                    row.reason_json,
                )
                for row in session.scalars(select(DecisionLog).order_by(DecisionLog.id))
            ],
        }


def _latest_turn(sessions) -> AgentTakeoverTurn:
    with sessions() as session:
        turn = session.scalar(select(AgentTakeoverTurn).order_by(AgentTakeoverTurn.id.desc()))
        assert turn is not None
        session.expunge(turn)
        return turn


async def _make_and_resolve_turn(service: WorldService) -> None:
    assert await service.tick()
    assert await service.process_agent_decision_jobs(limit=5) >= 1
    # The worker may validate a response, but only a later Engine tick may start it.
    assert await service.tick()


def test_takeover_default_off_preserves_v11_facts_and_random_order(tmp_path):
    implicit_engine, implicit_sessions = create_database(tmp_path / "implicit.db")
    explicit_engine, explicit_sessions = create_database(tmp_path / "explicit.db")
    implicit = WorldService(implicit_sessions, agent_enabled=True, agent_generator=_generator(ChoosingProvider()))
    explicit = WorldService(
        explicit_sessions,
        agent_enabled=True,
        agent_takeover_enabled=False,
        agent_generator=_generator(ChoosingProvider()),
    )
    implicit.initialize()
    explicit.initialize()
    try:
        for _ in range(36):
            assert asyncio.run(implicit.tick())
            assert asyncio.run(explicit.tick())
        assert _legacy_snapshot(implicit_sessions) == _legacy_snapshot(explicit_sessions)
        assert asyncio.run(implicit.agent_takeover_status())["enabled"] is False
        with implicit_sessions() as session:
            assert session.scalar(select(func.count()).select_from(AgentTakeoverTurn)) == 0
    finally:
        implicit_engine.dispose()
        explicit_engine.dispose()


def test_v12_migration_is_additive_idempotent_and_preserves_old_sql(tmp_path):
    path = tmp_path / "v12-upgrade.db"
    engine, sessions = create_database(path)
    WorldService(sessions, agent_enabled=False).initialize()
    for table in reversed(V12_TABLES):
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
    upgraded, _ = create_database(path)
    try:
        assert set(inspect(upgraded).get_table_names()) - old_tables == {
            "agent_takeover_turns"
        }
        with upgraded.connect() as connection:
            for name, sql in old_sql.items():
                assert connection.exec_driver_sql(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
                ).scalar_one() == sql
        # Re-running create_database is a no-op for schema and facts.
        upgraded.dispose()
        reopened, _ = create_database(path)
        assert set(inspect(reopened).get_table_names()) == old_tables | {
            "agent_takeover_turns"
        }
        reopened.dispose()
    finally:
        upgraded.dispose()


def test_engine_options_cover_and_validate_all_parameterized_actions(tmp_path):
    engine, sessions, service = _service(
        tmp_path / "all-params.db", takeover=True, provider=ChoosingProvider()
    )
    try:
        with sessions() as session:
            alice = session.get(NPC, 1)
            bob = session.get(NPC, 2)
            alice.money = 5000
            coffee = session.scalar(
                select(ItemDefinition).where(ItemDefinition.item_key == "coffee")
            )
            inventory = session.scalar(
                select(InventoryItem).where(
                    InventoryItem.npc_id == 1, InventoryItem.item_id == coffee.id
                )
            )
            if inventory is None:
                session.add(InventoryItem(npc_id=1, item_id=coffee.id, quantity=2))
            else:
                inventory.quantity = 2
            session.flush()

            def options_at(location: str, actions: list[str], minute: int = 10 * 60):
                alice.current_location = location
                bob.current_location = location if "Socialize" in actions else "Home"
                session.flush()
                candidates = [
                    {
                        "action": action,
                        "available": True,
                        "target_location": {
                            "GoHome": "Home", "GoOffice": "Office",
                            "GoCafe": "Cafe", "GoPark": "Park",
                        }.get(action),
                        "explanation": action,
                    }
                    for action in actions
                ]
                return build_action_options(
                    session, alice, ClockSnapshot(minute), candidates
                )

            all_options = []
            all_options += options_at("Cafe", ["Socialize", "Shop", "JobSearch", "GoPark"])
            all_options += options_at("Home", ["UseItem", "UpgradeHome", "GoOffice"])
            all_options += options_at("Park", ["UseFacility", "GoCafe"], 18 * 60)
            all_options += options_at("Office", ["Train", "GoHome"], 18 * 60)
            by_action = {}
            for option in all_options:
                by_action.setdefault(option["action"], []).append(option)

            expected = {
                "Socialize": "target_npc_id",
                "Shop": "item_key",
                "UseItem": "item_key",
                "JobSearch": "profession_key",
                "UseFacility": "institution_id",
                "Train": "skill_key",
                "UpgradeHome": "tier_after",
                "GoHome": "target_location",
                "GoOffice": "target_location",
                "GoCafe": "target_location",
                "GoPark": "target_location",
            }
            assert expected.keys() <= by_action.keys()
            for action, parameter in expected.items():
                option = by_action[action][0]
                assert option["target"] is not None
                assert parameter in option["params"]
                assert validate_action_selection(
                    action, option["target"], all_options,
                    dialogue="你好" if action == "Socialize" else None,
                )["legal"] is True
                assert validate_action_selection(
                    action, "forged-target", all_options
                )["legal"] is False
    finally:
        engine.dispose()


def test_legal_agent_move_is_started_by_engine_and_audited(tmp_path):
    engine, sessions, service = _service(
        tmp_path / "legal-move.db", takeover=True, provider=ChoosingProvider("GoPark", "Park")
    )
    try:
        asyncio.run(_make_and_resolve_turn(service))
        turn = _latest_turn(sessions)
        with sessions() as session:
            alice = session.get(NPC, 1)
            assert alice.current_action == "GoPark"
            assert alice.pending_location == "Park"
        assert turn.final_source == "agent"
        assert turn.final_action == "GoPark"
        assert turn.final_target == "Park"
        assert json.loads(turn.final_params_json)["target_location"] == "Park"
        assert json.loads(turn.execution_validation_json)["legal"] is True
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("action", "target", "location", "start_minute", "param_key"),
    [
        ("Shop", "coffee", "Cafe", 8 * 60, "listing_id"),
        ("UseItem", "professional_guide", "Home", 8 * 60, "item_id"),
        ("JobSearch", "Writer", "Home", 8 * 60, "profession_key"),
        ("UseFacility", "park_wellness", "Park", 17 * 60, "institution_id"),
        ("Train", "career_center", "Office", 18 * 60, "skill_key"),
        ("UpgradeHome", "improved", "Home", 8 * 60, "tier_after"),
    ],
)
def test_agent_starts_every_major_parameter_action(
    tmp_path, action, target, location, start_minute, param_key
):
    engine, sessions, service = _service(
        tmp_path / f"param-{action}.db",
        takeover=True,
        provider=ChoosingProvider(action, target),
    )
    try:
        with sessions() as session:
            state = session.get(WorldState, 1)
            state.total_minutes = start_minute - 10
            alice = session.get(NPC, 1)
            alice.current_location = location
            alice.current_action = "Idle"
            alice.action_end_minute = state.total_minutes
            alice.money = 5000
            if action == "UseItem":
                item = session.scalar(
                    select(ItemDefinition).where(
                        ItemDefinition.item_key == "professional_guide"
                    )
                )
                session.add(InventoryItem(npc_id=1, item_id=item.id, quantity=1))
            if action == "JobSearch":
                career = session.scalar(
                    select(CareerDevelopment).where(CareerDevelopment.npc_id == 1)
                )
                career.employment_status = "unemployed"
                career.unemployment_since_minute = state.total_minutes - 60
            if action == "Train":
                budget = session.scalar(
                    select(PersonalBudget).where(PersonalBudget.npc_id == 1)
                )
                budget.learning_budget = 1000
            session.commit()
        asyncio.run(_make_and_resolve_turn(service))
        turn = _latest_turn(sessions)
        assert turn.final_source == "agent"
        assert turn.final_action == action
        assert turn.final_target == target
        assert param_key in json.loads(turn.final_params_json)
        with sessions() as session:
            assert session.get(NPC, 1).current_action == action
    finally:
        engine.dispose()


def test_socialize_target_is_resolved_to_visible_npc_and_persisted(tmp_path):
    engine, sessions, service = _service(
        tmp_path / "social-target.db",
        takeover=True,
        provider=ChoosingProvider("Socialize", "Bob"),
    )
    try:
        with sessions() as session:
            alice = session.get(NPC, 1)
            bob = session.get(NPC, 2)
            alice.current_location = bob.current_location = "Cafe"
            alice.current_action = bob.current_action = "Idle"
            alice.action_end_minute = bob.action_end_minute = 480
            session.commit()
        asyncio.run(_make_and_resolve_turn(service))
        turn = _latest_turn(sessions)
        params = json.loads(turn.final_params_json)
        assert turn.final_source == "agent"
        assert turn.final_action == "Socialize"
        assert turn.final_target == "Bob"
        assert params["target_npc_id"] == 2
        with sessions() as session:
            assert session.get(NPC, 1).current_action == "Socialize"
    finally:
        engine.dispose()


def test_illegal_agent_action_uses_auditable_utility_fallback(tmp_path):
    engine, sessions, service = _service(
        tmp_path / "illegal.db", takeover=True, provider=ChoosingProvider("Teleport", None)
    )
    try:
        asyncio.run(_make_and_resolve_turn(service))
        turn = _latest_turn(sessions)
        assert turn.final_source == "utility_fallback"
        assert turn.final_action == turn.utility_action
        assert turn.final_action != "Teleport"
        assert turn.fallback_reason_code in {"action_not_offered", "snapshot_invalid", "illegal_action"}
        assert json.loads(turn.snapshot_validation_json)["legal"] is False
        with sessions() as session:
            assert session.get(NPC, 1).current_action == turn.final_action
    finally:
        engine.dispose()


def test_missing_key_and_timeout_fall_back_without_network(tmp_path):
    no_key_engine, no_key_sessions, no_key = _service(
        tmp_path / "no-key.db", takeover=True, provider=None, api_key=None
    )

    class SlowProvider:
        name = "fake-slow-takeover"

        async def generate(self, _perception):
            await asyncio.sleep(2)
            raise AssertionError("wait_for should have cancelled this fake provider")

    timeout_engine, timeout_sessions, timeout_service = _service(
        tmp_path / "timeout.db", takeover=True, provider=SlowProvider(), timeout=0.01
    )
    try:
        assert asyncio.run(no_key.tick())
        no_key_turn = _latest_turn(no_key_sessions)
        assert no_key_turn.final_source == "utility_fallback"
        assert no_key_turn.fallback_reason_code == "missing_api_key"
        with no_key_sessions() as session:
            assert session.scalar(select(AgentDecisionJob)) is None

        asyncio.run(_make_and_resolve_turn(timeout_service))
        timeout_turn = _latest_turn(timeout_sessions)
        assert timeout_turn.final_source == "utility_fallback"
        assert timeout_turn.fallback_reason_code == "timeout"
    finally:
        no_key_engine.dispose()
        timeout_engine.dispose()


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("exception", "provider_error"),
        ("invalid_json", "invalid_json"),
        ("extra_field", "schema_validation_failed"),
    ],
)
def test_takeover_provider_and_schema_failures_are_auditable_fallbacks(
    tmp_path, mode, reason
):
    class BrokenProvider:
        name = "fake-broken"

        async def generate(self, perception):
            if mode == "exception":
                raise RuntimeError("secret provider detail")
            if mode == "invalid_json":
                return "not-json"
            candidate = perception["available_actions"][0]
            return json.dumps({
                "emotion": "平静", "intention": "尝试", "action": candidate["action"],
                "target": candidate["allowed_targets"][0] if candidate["allowed_targets"] else None,
                "dialogue": None, "plan": ["尝试"], "reason_summary": "简短理由",
                "database_write": {"money": 999999},
            }, ensure_ascii=False)

    engine, sessions, service = _service(
        tmp_path / f"broken-{mode}.db", takeover=True, provider=BrokenProvider()
    )
    try:
        asyncio.run(_make_and_resolve_turn(service))
        turn = _latest_turn(sessions)
        assert turn.final_source == "utility_fallback"
        assert turn.fallback_reason_code == reason
        assert "secret" not in (turn.last_error_code or "")
        assert "database_write" not in (turn.agent_decision_json or "")
    finally:
        engine.dispose()


def test_waiting_for_agent_does_not_block_world_or_create_duplicate_turn(tmp_path):
    async def scenario():
        provider = BlockingProvider()
        engine, sessions, service = _service(
            tmp_path / "nonblocking.db", takeover=True, provider=provider, timeout=2.0
        )
        try:
            assert await service.tick()
            with sessions() as session:
                before_time = session.get(WorldState, 1).total_minutes
                before_bob_action = session.get(NPC, 2).current_action
            worker = asyncio.create_task(service.process_agent_decision_jobs(limit=1))
            await asyncio.wait_for(provider.started.wait(), timeout=0.5)
            assert await asyncio.wait_for(service.tick(), timeout=0.8)
            assert await asyncio.wait_for(service.tick(), timeout=0.8)
            with sessions() as session:
                assert session.get(WorldState, 1).total_minutes == before_time + 20
                assert session.get(NPC, 2).current_action == before_bob_action
                assert session.scalar(select(func.count()).select_from(AgentTakeoverTurn)) == 1
                assert session.get(NPC, 1).current_action != "Teleport"
            provider.release.set()
            assert await asyncio.wait_for(worker, timeout=0.8) == 1
            assert await service.tick()
            with sessions() as session:
                assert session.scalar(select(func.count()).select_from(AgentTakeoverTurn)) == 1
        finally:
            engine.dispose()

    asyncio.run(scenario())


def test_restart_recovers_processing_lease_without_duplicate_execution(tmp_path):
    path = tmp_path / "restart.db"
    first_engine, sessions, first = _service(
        path, takeover=True, provider=ChoosingProvider("GoPark", "Park")
    )
    assert asyncio.run(first.tick())
    with sessions() as session:
        job = session.scalar(select(AgentDecisionJob))
        turn = session.scalar(select(AgentTakeoverTurn))
        job.status = "processing"
        turn.worker_state = "processing"
        turn.lease_token = "interrupted-worker"
        session.commit()
    first_engine.dispose()

    second_engine, second_sessions = create_database(path)
    second = WorldService(
        second_sessions,
        agent_enabled=True,
        agent_takeover_enabled=True,
        agent_generator=_generator(ChoosingProvider("GoPark", "Park")),
    )
    second.initialize()
    try:
        assert asyncio.run(second.recover_agent_decision_jobs()) >= 1
        assert asyncio.run(second.process_agent_decision_jobs(limit=5)) >= 1
        assert asyncio.run(second.tick())
        with second_sessions() as session:
            turns = list(session.scalars(select(AgentTakeoverTurn)))
            assert len(turns) == 1
            assert turns[0].final_source == "agent"
            assert turns[0].final_action == "GoPark"
            assert session.get(NPC, 1).current_action == "GoPark"
    finally:
        second_engine.dispose()


def test_takeover_api_is_additive_and_bob_remains_utility_only(tmp_path):
    engine, _sessions, service = _service(
        tmp_path / "api.db", takeover=False, provider=ChoosingProvider()
    )
    configure_world_service(service)
    app = FastAPI()
    app.include_router(agent_router)
    try:
        with TestClient(app) as client:
            old_status = client.get("/api/agent/status")
            assert old_status.status_code == 200
            assert {
                "enabled", "mode", "target_npc_id", "target_npc_name",
                "provider", "jobs", "authority",
            }.issubset(old_status.json())

            takeover = client.get("/api/agent/takeover")
            assert takeover.status_code == 200
            assert takeover.json()["enabled"] is False
            enabled = client.put("/api/agent/takeover", json={"enabled": True})
            assert enabled.status_code == 200
            assert enabled.json()["enabled"] is True

            bob = client.get("/api/npcs/2/agent-control")
            assert bob.status_code == 200
            assert bob.json()["supported"] is False
            assert bob.json()["status"] == "unsupported"
            audits = client.get("/api/npcs/1/agent-audits?limit=10")
            assert audits.status_code == 200
            assert isinstance(audits.json(), list)
            assert client.get("/api/npcs/999/agent-control").status_code == 404
    finally:
        engine.dispose()


def test_seven_day_takeover_stability_is_deterministic_safe_and_auditable(tmp_path):
    durations = {
        "Sleep": 120,
        "Work": 120,
        "Relax": 60,
        "Socialize": 45,
        "Eat": 30,
        "Shop": 30,
        "UseItem": 30,
        "JobSearch": 60,
        "UseFacility": 40,
        "Train": 60,
        "UpgradeHome": 60,
        "GoHome": 10,
        "GoOffice": 10,
        "GoCafe": 10,
        "GoPark": 10,
        "Idle": 10,
    }
    call_count = 0

    def stable_choice(perception: dict) -> tuple[str, str | None]:
        nonlocal call_count
        call_count += 1
        if call_count % 11 == 0:
            return "Teleport", None
        candidate = max(
            perception["available_actions"],
            key=lambda item: (durations.get(item["action"], 10), item["action"]),
        )
        targets = candidate.get("allowed_targets", [])
        return candidate["action"], targets[0] if targets else None

    path = tmp_path / "seven-days.db"
    provider = ChoosingProvider(chooser=stable_choice)
    engine, sessions, service = _service(
        path, takeover=True, provider=provider
    )

    async def simulate() -> None:
        for _ in range(7 * 24 * 6):
            assert await service.tick()
            await service.process_agent_decision_jobs(limit=5)

    try:
        asyncio.run(asyncio.wait_for(simulate(), timeout=180))
        with sessions() as session:
            state = session.get(WorldState, 1)
            turns = list(session.scalars(select(AgentTakeoverTurn).order_by(AgentTakeoverTurn.id)))
            alice = session.get(NPC, 1)
            assert state.total_minutes == 480 + 7 * 1440
            assert turns
            assert len({turn.decision_id for turn in turns}) == len(turns)
            assert sum(turn.state != "completed" for turn in turns) <= 1
            assert any(turn.final_source == "agent" for turn in turns)
            assert any(turn.final_source == "utility_fallback" for turn in turns)
            assert all(
                turn.final_action != "Teleport"
                for turn in turns
                if turn.final_action is not None
            )
            assert all(0 <= value <= 100 for value in (
                alice.energy, alice.hunger, alice.mood,
                alice.social_need, alice.work_satisfaction,
            ))
            assert -10000 <= alice.money <= 1000000
            assert session.scalar(
                select(func.count()).select_from(Memory).where(Memory.npc_id == 1)
            ) > 0
            agent_turns = [turn for turn in turns if turn.final_source == "agent"]
            assert all(json.loads(turn.agent_decision_json)["plan"] for turn in agent_turns)
            assert any(
                any(plan.get("kind") == "agent_plan" for plan in perception.get("plans", []))
                for perception in provider.perceptions[1:]
            )
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []

        # Audits, plans and memories survive a normal service restart.
        before = asyncio.run(service.agent_audits(1, limit=1000))
        engine.dispose()
        restarted_engine, restarted_sessions = create_database(path)
        restarted = WorldService(
            restarted_sessions,
            agent_enabled=True,
            agent_takeover_enabled=True,
            agent_generator=_generator(ChoosingProvider()),
        )
        restarted.initialize()
        try:
            after = asyncio.run(restarted.agent_audits(1, limit=1000))
            assert after == before
        finally:
            restarted_engine.dispose()
    finally:
        engine.dispose()
