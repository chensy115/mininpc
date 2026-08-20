from __future__ import annotations

from typing import Annotated, Literal

from ipaddress import ip_address

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from api.dependencies import get_world_service
from simulation.world import WorldService


router = APIRouter(prefix="/api/runtime", tags=["v1.6-runtime"])
WorldDependency = Annotated[WorldService, Depends(get_world_service)]


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    npc_ids: list[int] | None = None


class NPCSwitchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class EmergencyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Literal["user_requested", "dashboard_emergency_stop", "operator_emergency_stop"] = "user_requested"


class ProviderConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: SecretStr = Field(min_length=1, max_length=4096)
    base_url: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=200)


class BudgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    calls_per_minute: int | None = None
    calls_per_hour: int | None = None
    calls_per_day: int | None = None
    calls_per_npc_hour: int | None = None
    calls_per_npc_day: int | None = None
    calls_per_task_hour: int | None = None
    calls_per_task_day: int | None = None
    input_tokens_per_day: int | None = None
    output_tokens_per_day: int | None = None
    total_tokens_per_day: int | None = None
    tokens_per_npc_day: int | None = None
    tokens_per_task_day: int | None = None
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None
    estimated_cost_per_day: float | None = None
    currency: str | None = None
    timezone: str | None = None


def _bad_request(exc: ValueError) -> HTTPException:
    code = str(exc)
    return HTTPException(status_code=400, detail={"code": code, "message": code.replace("_", " ")})


@router.get("")
async def get_runtime(service: WorldDependency):
    return await service.runtime_status()


@router.get("/health")
async def get_runtime_health(service: WorldDependency):
    status = await service.runtime_status()
    return {
        "ok": status.get("mode") in {"safe", "online", "paused", "emergency_stop"},
        "mode": status.get("mode"),
        "configured": status.get("configured", False),
        "provider_healthy": status.get("provider", {}).get("healthy", False),
        "world_continues_on_provider_failure": True,
    }


@router.get("/consistency")
async def get_runtime_consistency(service: WorldDependency):
    return await service.runtime_consistency()


@router.put("/provider")
async def configure_runtime_provider(
    request: Request,
    response: Response,
    payload: ProviderConfigRequest,
    service: WorldDependency,
):
    client_host = request.client.host if request.client else ""
    try:
        local_client = client_host == "testclient" or ip_address(client_host).is_loopback
    except ValueError:
        local_client = False
    if not local_client:
        raise HTTPException(
            status_code=403,
            detail={"code": "local_configuration_only", "message": "provider configuration is local-only"},
        )
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.configure_runtime_provider(
            api_key=payload.api_key.get_secret_value(),
            base_url=payload.base_url,
            model=payload.model,
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/start")
async def start_runtime(request: StartRequest, service: WorldDependency):
    try:
        ids = None if request.npc_ids is None else set(request.npc_ids)
        return await service.start_runtime(ids)
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/pause")
async def pause_runtime(service: WorldDependency):
    return await service.pause_runtime()


@router.post("/resume")
async def resume_runtime(service: WorldDependency):
    try:
        return await service.resume_runtime()
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/stop")
async def stop_runtime(service: WorldDependency):
    return await service.stop_runtime()


@router.post("/emergency-stop")
async def emergency_stop_runtime(request: EmergencyRequest, service: WorldDependency):
    return await service.stop_runtime(emergency=True, reason=request.reason)


@router.put("/npcs/{npc_id}")
async def switch_runtime_npc(npc_id: int, request: NPCSwitchRequest, service: WorldDependency):
    if await service.get_npc(npc_id) is None:
        raise HTTPException(status_code=404, detail={"code": "npc_not_found", "message": "NPC not found"})
    try:
        return await service.set_runtime_npc(npc_id, request.enabled)
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.put("/budget")
async def update_runtime_budget(request: BudgetRequest, service: WorldDependency):
    try:
        return await service.update_runtime_budget(request.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/budget/reset")
async def reset_runtime_budget(service: WorldDependency):
    return await service.reset_runtime_budget()
