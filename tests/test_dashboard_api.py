from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from api.agent import router as agent_router
from api.dashboard import router as dashboard_router
from api.dependencies import configure_world_service
from api.npc import router as npc_router
from api.runtime import router as runtime_router
from api.world import router as world_router
from database.models import DecisionLog, Event, ModelRuntimeState, WorldState
from simulation import dashboard


@pytest.fixture
def dashboard_api(world_service):
    configure_world_service(world_service)
    app = FastAPI()
    app.include_router(world_router)
    app.include_router(npc_router)
    app.include_router(agent_router)
    app.include_router(runtime_router)
    app.include_router(dashboard_router)
    with TestClient(app) as client:
        yield client, world_service


def _private_keys(value):
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in dashboard._PRIVATE_KEYS:
                found.add(key.lower())
            found.update(_private_keys(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_private_keys(item))
    return found


def test_dashboard_snapshot_contract_matches_existing_read_services(dashboard_api):
    client, _service = dashboard_api
    response = client.get(
        "/api/dashboard/snapshot?groups=runtime,world,npcs,pulse"
    )
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "schema_version", "snapshot_id", "captured_at", "world_minute", "modules"
    }
    assert payload["schema_version"] == "1.0"
    assert datetime.fromisoformat(payload["captured_at"]).utcoffset().total_seconds() == 0
    assert list(payload["modules"]) == ["runtime", "world", "npcs", "pulse"]
    assert all(module["status"] == "ok" for module in payload["modules"].values())

    modules = payload["modules"]
    assert modules["world"]["data"] == client.get("/api/world").json()
    assert modules["npcs"]["data"]["items"] == client.get("/api/npcs").json()
    assert modules["npcs"]["data"]["agents"] == client.get("/api/agents/takeover").json()
    assert modules["pulse"]["data"]["events"] == client.get("/api/events?limit=40").json()
    assert modules["pulse"]["data"]["narrative_status"] == client.get(
        "/api/narrative/status"
    ).json()
    assert modules["pulse"]["data"]["narratives"] == client.get(
        "/api/narratives/events?limit=80"
    ).json()


def test_world_scoped_modules_share_snapshot_boundary(dashboard_api):
    client, _service = dashboard_api
    first = client.get("/api/dashboard/snapshot?groups=world,npcs,pulse,runtime").json()
    for name in ("world", "npcs", "pulse"):
        module = first["modules"][name]
        assert module["snapshot_id"] == first["snapshot_id"]
        assert module["version"] == first["snapshot_id"]
        assert module["world_minute"] == first["world_minute"]
    runtime = first["modules"]["runtime"]
    assert runtime["snapshot_id"] == first["snapshot_id"]
    assert runtime["world_minute"] == first["world_minute"]
    assert runtime["generation"] == runtime["data"]["generation"]
    assert runtime["version"] == f"generation:{runtime['generation']}"
    assert datetime.fromisoformat(runtime["observed_at"]).utcoffset().total_seconds() == 0

    second = client.get("/api/dashboard/snapshot?groups=world").json()
    assert second["world_minute"] == first["world_minute"]
    assert int(second["snapshot_id"].split(":")[1]) > int(
        first["snapshot_id"].split(":")[1]
    )


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("/api/dashboard/snapshot?groups=", "invalid_groups"),
        ("/api/dashboard/snapshot?groups=world,unknown", "invalid_groups"),
        ("/api/dashboard/npcs/1/snapshot?sections=", "invalid_sections"),
        (
            "/api/dashboard/npcs/1/snapshot?sections=overview,secrets",
            "invalid_sections",
        ),
    ],
)
def test_snapshot_query_validation(path, code, dashboard_api):
    client, _service = dashboard_api
    response = client.get(path)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == code


def test_snapshot_selection_is_ordered_and_deduplicated(dashboard_api):
    client, _service = dashboard_api
    payload = client.get(
        "/api/dashboard/snapshot?groups=pulse,world,pulse"
    ).json()
    assert list(payload["modules"]) == ["pulse", "world"]
    npc = client.get(
        "/api/dashboard/npcs/1/snapshot?sections=decision,overview,decision"
    ).json()
    assert list(npc["modules"]) == ["decision", "overview"]


def test_npc_snapshot_contract_and_unknown_npc(dashboard_api):
    client, _service = dashboard_api
    response = client.get(
        "/api/dashboard/npcs/1/snapshot?sections=overview,decision"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "1.0"
    assert list(payload["modules"]) == ["overview", "decision"]
    assert payload["modules"]["overview"]["data"]["npc"] == client.get(
        "/api/npcs/1"
    ).json()
    assert payload["modules"]["decision"]["data"]["decision"] == client.get(
        "/api/npcs/1/decision"
    ).json()
    for module in payload["modules"].values():
        assert module["status"] == "ok"
        assert module["snapshot_id"] == payload["snapshot_id"]
        assert module["world_minute"] == payload["world_minute"]

    missing = client.get("/api/dashboard/npcs/999/snapshot")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "npc_not_found"


def test_group_failure_is_local_and_error_message_is_safe(monkeypatch, dashboard_api):
    client, _service = dashboard_api

    def broken(*_args, **_kwargs):
        raise RuntimeError("API_KEY=secret prompt=response")

    monkeypatch.setitem(dashboard._GROUP_BUILDERS, "pulse", broken)
    response = client.get("/api/dashboard/snapshot?groups=world,pulse,npcs")
    assert response.status_code == 200
    payload = response.json()
    assert payload["modules"]["world"]["status"] == "ok"
    assert payload["modules"]["npcs"]["status"] == "ok"
    failure = payload["modules"]["pulse"]
    assert failure["status"] == "error"
    assert failure["error"] == {
        "code": "pulse_snapshot_unavailable",
        "message": "pulse snapshot is temporarily unavailable",
        "retryable": True,
    }
    assert "secret" not in response.text


def test_runtime_failure_does_not_break_world_modules(monkeypatch, dashboard_api):
    client, service = dashboard_api

    async def broken_runtime():
        raise RuntimeError("provider internal failure")

    monkeypatch.setattr(service, "runtime_status", broken_runtime)
    response = client.get("/api/dashboard/snapshot?groups=runtime,world,npcs")
    assert response.status_code == 200
    modules = response.json()["modules"]
    assert modules["runtime"]["status"] == "error"
    assert modules["runtime"]["generation"] is None
    assert modules["world"]["status"] == "ok"
    assert modules["npcs"]["status"] == "ok"


def test_npc_section_failure_does_not_break_other_section(monkeypatch, dashboard_api):
    client, _service = dashboard_api

    def broken(*_args, **_kwargs):
        raise RuntimeError("decision unavailable")

    monkeypatch.setitem(dashboard._SECTION_BUILDERS, "decision", broken)
    response = client.get(
        "/api/dashboard/npcs/1/snapshot?sections=overview,decision"
    )
    assert response.status_code == 200
    modules = response.json()["modules"]
    assert modules["overview"]["status"] == "ok"
    assert modules["decision"]["status"] == "error"


def test_snapshot_privacy_boundary_removes_private_fields(monkeypatch, dashboard_api):
    client, service = dashboard_api
    monkeypatch.setattr(
        service.model_runtime,
        "settings",
        replace(service.model_runtime.settings, api_key="definitely-not-public"),
    )
    original = dashboard._GROUP_BUILDERS["pulse"]

    def with_private_fields(*args, **kwargs):
        data = original(*args, **kwargs)
        data["api_key"] = "definitely-not-public"
        data["nested"] = {
            "prompt": "private prompt",
            "response": "private response",
            "safe_explanation": "public",
        }
        return data

    monkeypatch.setitem(dashboard._GROUP_BUILDERS, "pulse", with_private_fields)
    response = client.get("/api/dashboard/snapshot?groups=runtime,pulse")
    assert response.status_code == 200
    payload = response.json()
    assert _private_keys(payload) == set()
    rendered = json.dumps(payload, ensure_ascii=False)
    assert "definitely-not-public" not in rendered
    assert "private prompt" not in rendered
    assert "private response" not in rendered
    assert payload["modules"]["pulse"]["data"]["nested"] == {
        "safe_explanation": "public"
    }


def test_snapshot_endpoints_are_read_only(dashboard_api):
    client, service = dashboard_api

    def facts():
        with service.session_factory() as session:
            state = session.get(WorldState, 1)
            runtime = session.get(ModelRuntimeState, 1)
            return {
                "world": (state.total_minutes, state.paused, state.speed),
                "events": session.scalar(select(func.count()).select_from(Event)),
                "decisions": session.scalar(
                    select(func.count()).select_from(DecisionLog)
                ),
                "runtime": (
                    runtime.mode,
                    runtime.generation,
                    runtime.enabled_npc_ids_json,
                    runtime.emergency_reason,
                ),
            }

    before = facts()
    for _ in range(3):
        assert client.get("/api/dashboard/snapshot").status_code == 200
        assert client.get("/api/dashboard/npcs/1/snapshot").status_code == 200
    assert facts() == before


def test_old_api_payloads_are_unchanged_by_snapshot_reads(dashboard_api):
    client, _service = dashboard_api
    paths = (
        "/api/world",
        "/api/npcs",
        "/api/npcs/1",
        "/api/npcs/1/decision",
        "/api/events?limit=40",
        "/api/narrative/status",
        "/api/narratives/events?limit=80",
        "/api/agents/takeover",
        "/api/runtime",
    )
    before = {path: client.get(path).json() for path in paths}
    client.get("/api/dashboard/snapshot")
    client.get("/api/dashboard/npcs/1/snapshot")
    after = {path: client.get(path).json() for path in paths}
    assert after == before
