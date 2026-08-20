from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import wraps

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text

from database.database import V16_TABLE_NAMES, create_database
from database.models import AgentTakeoverTurn, ModelCallAudit, ModelRuntimeState
from api.dependencies import configure_world_service
from api.runtime import router as runtime_router
from simulation.runtime_v16 import (
    FairCallScheduler,
    ModelRuntime,
    RuntimeProviderError,
    RuntimeSettings,
)
from simulation.agent_brain import AgentDecisionGenerator, AgentSettings
from simulation.world import WorldService


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def settings(**changes) -> RuntimeSettings:
    base = RuntimeSettings.from_env()
    values = {
        "api_key": "fake-v16-key-never-persist", "base_url": "https://example.invalid/v1",
        "model": "fake-v16-model", "timeout_seconds": 1.0, "max_output_tokens": 128,
        "max_attempts": 2, "retry_base_seconds": 0.0, "max_concurrency": 2,
        "per_task_concurrency": 1, "queue_limit": 10, "calls_per_minute": 100,
        "calls_per_hour": 100, "calls_per_day": 100, "calls_per_npc_hour": 100,
        "calls_per_npc_day": 100, "calls_per_task_hour": 100, "calls_per_task_day": 100,
        "input_tokens_per_day": 1_000_000, "output_tokens_per_day": 1_000_000,
        "total_tokens_per_day": 1_000_000, "tokens_per_npc_day": 1_000_000,
        "tokens_per_task_day": 1_000_000,
    }
    values.update(changes)
    return replace(base, **values)


def make_runtime(tmp_path, *, transport=None, clock=None, sleeper=None, config=None):
    db_path = tmp_path / "runtime-v16.db"
    engine, sessions = create_database(db_path)
    runtime = ModelRuntime(
        sessions,
        config or settings(),
        transport=transport,
        now=clock,
        sleeper=sleeper,
    )
    service = WorldService(sessions, model_runtime=runtime)
    service.initialize()
    return engine, sessions, runtime, db_path


def context(npc_id: int = 1, version: str = "1.3") -> dict:
    return {"schema_version": version, "self": {"id": npc_id, "name": f"NPC-{npc_id}"}, "available_actions": []}


@async_test
async def test_v16_no_key_and_key_without_start_make_zero_http_calls(tmp_path):
    calls = 0

    async def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    no_key = settings(api_key=None)
    engine, _sessions, runtime, _path = make_runtime(
        tmp_path, transport=httpx.MockTransport(handler), config=no_key
    )
    assert runtime.status()["configured"] is False
    with pytest.raises(ValueError, match="provider_not_configured"):
        await runtime.transition("start")
    with pytest.raises(RuntimeProviderError, match="runtime_not_online"):
        await runtime.generate("decision", 1, context())
    assert calls == 0
    engine.dispose()


@async_test
async def test_v16_success_usage_audit_and_secret_never_persisted_or_returned(tmp_path):
    async def handler(request):
        assert request.headers["Authorization"] == "Bearer fake-v16-key-never-persist"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"ok":true}'}}],
            "usage": {"prompt_tokens": 31, "completion_tokens": 7, "total_tokens": 38},
        })

    engine, sessions, runtime, path = make_runtime(tmp_path, transport=httpx.MockTransport(handler))
    await runtime.transition("start", npc_ids={1})
    assert await runtime.generate("decision", 1, context()) == '{"ok":true}'
    status = runtime.status()
    assert status["provider"]["key"] == {"configured": True}
    assert "fake-v16-key" not in str(status)
    assert status["recent_calls"][0]["total_tokens"] == 38
    assert status["recent_calls"][0]["usage_reported"] is True
    with sessions() as session:
        row = session.scalar(select(ModelCallAudit))
        assert row is not None and row.task_type == "decision" and row.npc_id == 1
    engine.dispose()
    raw = path.read_bytes()
    assert b"fake-v16-key-never-persist" not in raw
    assert b"Authorization" not in raw and b"Bearer" not in raw


@async_test
async def test_v16_missing_usage_is_explicit_and_conservatively_counted(tmp_path):
    transport = httpx.MockTransport(lambda _request: httpx.Response(
        200, json={"choices": [{"message": {"content": "ok"}}]}
    ))
    engine, _sessions, runtime, _path = make_runtime(tmp_path, transport=transport)
    await runtime.transition("start", npc_ids={2})
    await runtime.generate("conversation", 2, context(2, "1.4"))
    call = runtime.status()["recent_calls"][0]
    assert call["usage_reported"] is False
    assert call["prompt_tokens"] > 0 and call["completion_tokens"] == 0
    engine.dispose()


@async_test
async def test_v16_429_retry_after_then_5xx_backoff_then_success(tmp_path):
    responses = iter([
        httpx.Response(429, headers={"Retry-After": "0.25"}),
        httpx.Response(503),
        httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}),
    ])
    delays = []

    async def sleep(delay):
        delays.append(delay)

    config = settings(max_attempts=3, retry_base_seconds=0.5)
    engine, _sessions, runtime, _path = make_runtime(
        tmp_path, transport=httpx.MockTransport(lambda _request: next(responses)), sleeper=sleep,
        config=config,
    )
    await runtime.transition("start", npc_ids={1})
    assert await runtime.generate("decision", 1, context()) == "ok"
    assert delays == [0.25, 1.0]
    assert runtime.status()["recent_calls"][0]["retry_count"] == 2
    engine.dispose()


@async_test
@pytest.mark.parametrize(
    ("exception", "error"),
    [(httpx.ConnectError("offline"), "connection"), (httpx.ReadTimeout("slow"), "timeout")],
)
async def test_v16_connection_and_timeout_are_bounded_and_classified(tmp_path, exception, error):
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        raise exception

    engine, _sessions, runtime, _path = make_runtime(
        tmp_path, transport=httpx.MockTransport(handler), sleeper=lambda _delay: asyncio.sleep(0)
    )
    await runtime.transition("start", npc_ids={1})
    with pytest.raises(RuntimeProviderError, match=error):
        await runtime.generate("decision", 1, context())
    assert attempts == 2
    assert runtime.status()["recent_calls"][0]["error_class"] == error
    engine.dispose()


@async_test
async def test_v16_authentication_opens_circuit_and_half_open_success_recovers(tmp_path):
    clock = MutableClock(datetime(2026, 8, 19, tzinfo=timezone.utc))
    outcome = {"authorized": False}

    def handler(_request):
        if not outcome["authorized"]:
            return httpx.Response(401)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    config = settings(max_attempts=1, circuit_failure_threshold=1, circuit_cooldown_seconds=1.0)
    engine, _sessions, runtime, _path = make_runtime(
        tmp_path, transport=httpx.MockTransport(handler), clock=clock, config=config
    )
    await runtime.transition("start", npc_ids={1})
    with pytest.raises(RuntimeProviderError, match="authentication"):
        await runtime.generate("decision", 1, context())
    assert any(row["state"] == "open" for row in runtime.status()["circuits"])
    with pytest.raises(RuntimeProviderError, match="circuit_open"):
        await runtime.generate("decision", 1, context())
    clock.value += timedelta(seconds=2)
    outcome["authorized"] = True
    assert await runtime.generate("decision", 1, context()) == "ok"
    assert all(row["state"] == "closed" for row in runtime.status()["circuits"])
    engine.dispose()


@async_test
async def test_v16_daily_budget_reset_and_timezone_boundary(tmp_path):
    clock = MutableClock(datetime(2026, 8, 19, 15, 59, tzinfo=timezone.utc))  # 23:59 Shanghai
    config = settings(calls_per_day=1, calls_per_npc_day=1, calls_per_task_day=1)
    transport = httpx.MockTransport(lambda _request: httpx.Response(
        200, json={"choices": [{"message": {"content": "ok"}}]}
    ))
    engine, _sessions, runtime, _path = make_runtime(
        tmp_path, transport=transport, clock=clock, config=config
    )
    await runtime.transition("start", npc_ids={1})
    await runtime.generate("decision", 1, context())
    with pytest.raises(RuntimeProviderError, match="budget_calls_day"):
        await runtime.generate("decision", 1, context())
    clock.value += timedelta(minutes=2)
    await runtime.generate("decision", 1, context())
    assert runtime.status()["budget"]["local_day"] == "2026-08-20"
    engine.dispose()


@async_test
async def test_v16_cost_guard_rejects_without_stopping_world_runtime(tmp_path):
    config = settings(
        input_price_per_million=10.0,
        output_price_per_million=10.0,
        estimated_cost_per_day=0.000001,
    )
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    engine, _sessions, runtime, _path = make_runtime(
        tmp_path, transport=httpx.MockTransport(handler), config=config
    )
    await runtime.transition("start", npc_ids={1})
    with pytest.raises(RuntimeProviderError, match="budget_estimated_cost"):
        await runtime.generate("decision", 1, context())
    assert calls == 0 and runtime.status()["mode"] == "online"
    engine.dispose()


@async_test
async def test_v16_pause_resume_and_late_generation_result(tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(_request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"choices": [{"message": {"content": "late"}}]})

    engine, _sessions, runtime, _path = make_runtime(tmp_path, transport=httpx.MockTransport(handler))
    await runtime.transition("start", npc_ids={1, 2})
    await runtime.transition("pause")
    with pytest.raises(RuntimeProviderError, match="runtime_not_online"):
        await runtime.generate("decision", 1, context())
    await runtime.transition("resume")
    task = asyncio.create_task(runtime.generate("decision", 1, context()))
    await entered.wait()
    await runtime.set_npc(2, False)  # any scope change advances generation
    release.set()
    with pytest.raises(RuntimeProviderError, match="generation_changed"):
        await task
    assert runtime.status()["recent_calls"][0]["late"] is True
    engine.dispose()


@async_test
async def test_v16_emergency_stop_cancels_inflight_and_is_idempotent(tmp_path):
    entered = asyncio.Event()

    async def handler(_request):
        entered.set()
        await asyncio.Event().wait()

    engine, _sessions, runtime, _path = make_runtime(tmp_path, transport=httpx.MockTransport(handler))
    await runtime.transition("start", npc_ids={1})
    task = asyncio.create_task(runtime.generate("decision", 1, context()))
    await entered.wait()
    first = await runtime.transition("emergency_stop", reason="test")
    second = await runtime.transition("emergency_stop", reason="test")
    with pytest.raises(asyncio.CancelledError):
        await task
    assert first["mode"] == second["mode"] == "emergency_stop"
    call = runtime.status()["recent_calls"][0]
    assert call["status"] == "cancelled_emergency_stop"
    assert call["cancelled"] is True
    assert runtime.consistency()["active_audits"] == 0
    engine.dispose()


@async_test
async def test_v16_emergency_stop_reconciles_orphaned_started_audits(tmp_path):
    engine, sessions, runtime, _path = make_runtime(tmp_path)
    await runtime.transition("start", npc_ids={1})
    with sessions() as session:
        state = session.get(ModelRuntimeState, 1)
        session.add(ModelCallAudit(
            request_id="orphaned-started-call",
            generation=state.generation,
            budget_epoch=0,
            provider=runtime.provider_name,
            model=runtime.settings.model,
            task_type="reflection",
            npc_id=1,
            local_day="2026-08-20",
            started_at=datetime.now(timezone.utc),
            status="started",
            prompt_tokens=10,
            total_tokens=10,
            currency="CNY",
        ))
        session.commit()
    await runtime.transition("emergency_stop", reason="operator_emergency_stop")
    with sessions() as session:
        row = session.scalar(select(ModelCallAudit).where(
            ModelCallAudit.request_id == "orphaned-started-call"
        ))
        assert row.status == "cancelled_emergency_stop"
        assert row.completed_at is not None and row.cancelled and row.late and row.fallback
    assert runtime.consistency()["active_audits"] == 0
    engine.dispose()


@async_test
async def test_v16_service_emergency_stop_cancels_waiting_turn_without_starting_world_action(tmp_path):
    engine, sessions = create_database(tmp_path / "service-emergency.db")
    runtime = ModelRuntime(sessions, settings(max_attempts=1))
    service = WorldService(sessions, model_runtime=runtime)
    service.initialize()
    await service.start_runtime({1})

    control = await service.latest_agent_control(1)
    for _ in range(24):
        if control.get("status") == "waiting":
            break
        await service.tick()
        control = await service.latest_agent_control(1)
    assert control["status"] == "waiting"
    turn_id = control["turn"]["id"]

    result = await service.stop_runtime(emergency=True, reason="operator_emergency_stop")
    assert result["mode"] == "emergency_stop"
    with sessions() as session:
        turn = session.get(AgentTakeoverTurn, turn_id)
        assert turn.state == "completed"
        assert turn.worker_state == "failed"
        assert turn.action_started_minute is None and turn.action_end_minute is None
        completion = json.loads(turn.completion_json)
        assert completion == {
            "status": "cancelled_before_execution",
            "reason_code": "emergency_stop",
        }
    assert runtime.consistency()["active_audits"] == 0
    engine.dispose()


@async_test
async def test_v16_managed_provider_is_not_wrapped_in_legacy_shorter_timeout(monkeypatch):
    class ManagedProvider:
        name = "managed-test"
        manages_timeout = True

        async def generate(self, _context):
            return json.dumps({
                "emotion": "calm", "intention": "wait safely", "action": "Idle",
                "target": None, "dialogue": None, "plan": ["wait"],
                "reason_summary": "bounded managed provider",
            })

    async def forbidden_wait_for(*_args, **_kwargs):
        raise AssertionError("managed provider must own its timeout")

    monkeypatch.setattr(asyncio, "wait_for", forbidden_wait_for)
    agent_settings = replace(
        AgentSettings.from_env(), api_key="in-memory-only", model="managed-test"
    )
    decision = await AgentDecisionGenerator(agent_settings, ManagedProvider()).generate(context())
    assert decision.action == "Idle"


@async_test
async def test_v16_empty_200_response_closes_durable_audit(tmp_path):
    transport = httpx.MockTransport(lambda _request: httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": ""}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 128, "total_tokens": 148},
        },
    ))
    engine, _sessions, runtime, _path = make_runtime(
        tmp_path, transport=transport, config=settings(max_attempts=1)
    )
    await runtime.transition("start", npc_ids={1})
    with pytest.raises(RuntimeProviderError, match="empty_response"):
        await runtime.generate("decision", 1, context())
    call = runtime.status()["recent_calls"][0]
    assert call["status"] == "failed"
    assert call["http_status"] == 200
    assert call["error_class"] == "empty_response"
    assert call["usage_reported"] is True and call["total_tokens"] == 148
    assert runtime.consistency()["active_audits"] == 0
    engine.dispose()


def test_v16_task_payloads_use_strict_schema_and_configured_output_cap(tmp_path):
    config = settings(max_output_tokens=2000)
    engine, _sessions, runtime, _path = make_runtime(tmp_path, config=config)
    for task_type, version in (("decision", "1.3"), ("conversation", "1.4"), ("reflection", "1.5")):
        payload = runtime._build_payload(task_type, context(1, version))
        assert payload["max_tokens"] == 2000
        system = payload["messages"][0]["content"]
        assert "do not add keys" in system
    engine.dispose()


@async_test
async def test_v16_scheduler_is_bounded_fair_and_one_active_per_npc():
    scheduler = FairCallScheduler(global_limit=2, task_limit=1, queue_limit=3)
    await scheduler.acquire("decision", 1)
    blocked_same_npc = asyncio.create_task(scheduler.acquire("conversation", 1))
    fair_other = asyncio.create_task(scheduler.acquire("reflection", 2))
    await asyncio.wait_for(fair_other, timeout=0.2)
    assert scheduler.snapshot()["active"] == 2
    overflow = [asyncio.create_task(scheduler.acquire("decision", npc)) for npc in (3, 4, 5, 2)]
    await asyncio.sleep(0)
    assert any(task.done() and isinstance(task.exception(), RuntimeProviderError) for task in overflow)
    await scheduler.release("reflection", 2)
    await scheduler.release("decision", 1)
    await asyncio.wait_for(blocked_same_npc, timeout=0.2)
    await scheduler.release("conversation", 1)
    for task in overflow:
        if not task.done():
            task.cancel()
    await asyncio.gather(*overflow, return_exceptions=True)


def test_v16_additive_migration_is_idempotent_and_has_no_secret_fields(tmp_path):
    path = tmp_path / "upgrade.db"
    engine1, sessions1 = create_database(path)
    WorldService(sessions1).initialize()
    with engine1.connect() as connection:
        before = {row[0]: row[1] for row in connection.execute(text(
            "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
        ))}
    engine1.dispose()
    engine2, _sessions2 = create_database(path)
    with engine2.connect() as connection:
        after = {row[0]: row[1] for row in connection.execute(text(
            "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
        ))}
        assert connection.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert list(connection.exec_driver_sql("PRAGMA foreign_key_check")) == []
    assert before == after
    assert V16_TABLE_NAMES.issubset(set(inspect(engine2).get_table_names()))
    v16_sql = " ".join(after[name] for name in V16_TABLE_NAMES).lower()
    assert "api_key" not in v16_sql and "prompt_json" not in v16_sql and "response_json" not in v16_sql
    engine2.dispose()


def test_v16_runtime_http_shapes_and_strict_validation(tmp_path):
    engine, sessions = create_database(tmp_path / "http.db")
    runtime = ModelRuntime(sessions, settings())
    service = WorldService(sessions, model_runtime=runtime)
    service.initialize()
    configure_world_service(service)
    app = FastAPI()
    app.include_router(runtime_router)
    with TestClient(app) as client:
        status = client.get("/api/runtime")
        assert status.status_code == 200
        body = status.json()
        assert body["mode"] == "safe" and body["configured"] is True
        assert body["provider"]["key"] == {"configured": True}
        assert "fake-v16-key-never-persist" not in status.text
        assert client.post("/api/runtime/start", json={"npc_ids": [1, 2, 3, 4, 5]}).status_code == 200
        assert client.post("/api/runtime/pause").json()["mode"] == "paused"
        assert client.post("/api/runtime/resume").json()["mode"] == "online"
        assert client.put("/api/runtime/npcs/2", json={"enabled": False}).status_code == 200
        assert client.put("/api/runtime/budget", json={"calls_per_day": 7, "currency": "USD"}).json()["budget"]["limits"]["calls_per_day"] == 7
        assert client.put("/api/runtime/budget", json={"calls_per_day": 0}).status_code == 400
        assert client.post("/api/runtime/start", json={"unknown": True}).status_code == 422
        assert client.get("/api/runtime/consistency").json()["ok"] is True
        assert client.post("/api/runtime/emergency-stop", json={"reason": "operator_emergency_stop"}).json()["mode"] == "emergency_stop"
    engine.dispose()


def test_runtime_provider_can_be_configured_from_local_ui_without_persisting_secret(tmp_path):
    db_path = tmp_path / "provider-ui.db"
    engine, sessions = create_database(db_path)
    runtime = ModelRuntime(sessions, settings(api_key=None, model=""))
    service = WorldService(sessions, model_runtime=runtime)
    service.initialize()
    assert runtime.configured is False
    assert service.agent_generator.provider is None
    assert service.conversation_generator.provider is None
    assert service.reflection_generator.provider is None

    configure_world_service(service)
    app = FastAPI()
    app.include_router(runtime_router)
    secret = "fake-dashboard-key-never-persist"
    with TestClient(app) as client:
        configured = client.put("/api/runtime/provider", json={
            "api_key": secret,
            "base_url": "https://provider.example/v1/",
            "model": "provider-model",
        })
        assert configured.status_code == 200
        assert configured.headers["cache-control"] == "no-store"
        assert configured.json()["configured"] is True
        assert configured.json()["provider"] == {
            "name": "deepseek-openai-compatible",
            "model": "provider-model",
            "base_url": "https://provider.example/v1",
            "healthy": True,
            "key": {"configured": True},
        }
        assert secret not in configured.text
        assert service.agent_generator.provider is not None
        assert service.conversation_generator.provider is not None
        assert service.reflection_generator.provider is not None

        assert client.post("/api/runtime/start", json={}).status_code == 200
        blocked = client.put("/api/runtime/provider", json={
            "api_key": "replacement-key",
            "base_url": "https://provider.example/v1",
            "model": "replacement-model",
        })
        assert blocked.status_code == 400
        assert blocked.json()["detail"]["code"] == "provider_configuration_requires_stopped_runtime"
        assert client.post("/api/runtime/emergency-stop", json={"reason": "operator_emergency_stop"}).status_code == 200
        insecure = client.put("/api/runtime/provider", json={
            "api_key": "replacement-key",
            "base_url": "http://provider.example/v1",
            "model": "replacement-model",
        })
        assert insecure.status_code == 400
        assert insecure.json()["detail"]["code"] == "insecure_provider_base_url"

    engine.dispose()
    assert secret.encode() not in db_path.read_bytes()
