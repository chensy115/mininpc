from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.dependencies import get_world_service
from simulation.world import WorldService


router = APIRouter(tags=["agent-shadow"])
WorldDependency = Annotated[WorldService, Depends(get_world_service)]


class TakeoverRequest(BaseModel):
    enabled: bool


class ConversationRequest(BaseModel):
    enabled: bool


class CognitionRequest(BaseModel):
    enabled: bool


@router.get("/api/agent/status")
async def get_agent_status(service: WorldDependency):
    return await service.agent_status()


@router.get("/api/npcs/{npc_id}/agent-shadow")
async def get_agent_shadow(npc_id: int, service: WorldDependency):
    if await service.get_npc(npc_id) is None:
        raise HTTPException(status_code=404, detail="未找到该 NPC")
    return await service.latest_agent_shadow(npc_id)


@router.get("/api/agent/takeover")
async def get_agent_takeover(service: WorldDependency):
    return await service.agent_takeover_status()


@router.put("/api/agent/takeover")
async def update_agent_takeover(request: TakeoverRequest, service: WorldDependency):
    return await service.set_agent_takeover(request.enabled)


@router.get("/api/agents/takeover")
async def get_all_agent_takeovers(service: WorldDependency):
    return await service.agent_takeover_overview()


@router.put("/api/agents/takeover")
async def update_all_agent_takeovers(request: TakeoverRequest, service: WorldDependency):
    return await service.set_all_agent_takeovers(request.enabled)


@router.get("/api/agents/{npc_id}/control")
async def get_v13_agent_control(npc_id: int, service: WorldDependency):
    if await service.get_npc(npc_id) is None:
        raise HTTPException(status_code=404, detail="未找到该 NPC")
    return await service.latest_agent_control_v13(npc_id)


@router.put("/api/agents/{npc_id}/control")
async def update_v13_agent_control(
    npc_id: int, request: TakeoverRequest, service: WorldDependency
):
    if await service.get_npc(npc_id) is None:
        raise HTTPException(status_code=404, detail="未找到该 NPC")
    return await service.set_npc_agent_takeover(npc_id, request.enabled)


@router.get("/api/npcs/{npc_id}/agent-control")
async def get_agent_control(npc_id: int, service: WorldDependency):
    if await service.get_npc(npc_id) is None:
        raise HTTPException(status_code=404, detail="未找到该 NPC")
    return await service.latest_agent_control(npc_id)


@router.get("/api/npcs/{npc_id}/agent-audits")
async def get_agent_audits(npc_id: int, service: WorldDependency, limit: int = 50):
    if await service.get_npc(npc_id) is None:
        raise HTTPException(status_code=404, detail="未找到该 NPC")
    return await service.agent_audits(npc_id, min(max(limit, 1), 200))


@router.get("/api/agent-conversations/status")
async def get_agent_conversation_status(service: WorldDependency):
    return await service.agent_conversation_status()


@router.put("/api/agent-conversations/status")
async def update_agent_conversation_status(
    request: ConversationRequest, service: WorldDependency
):
    return await service.set_agent_conversations(request.enabled)


@router.get("/api/agent-conversations/check")
async def check_agent_conversations(service: WorldDependency):
    return await service.agent_conversation_safety_check()


@router.get("/api/conversations")
async def list_agent_conversations(
    service: WorldDependency,
    npc_id: int | None = Query(None, ge=1),
    status: Literal[
        "active", "ready_for_settlement", "completed", "failed", "cancelled", "expired"
    ] | None = None,
    limit: int = Query(50, ge=1, le=100),
):
    return await service.list_agent_conversations(npc_id=npc_id, status=status, limit=limit)


@router.get("/api/conversations/{conversation_id}")
async def get_agent_conversation(conversation_id: int, service: WorldDependency):
    conversation = await service.get_agent_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="未找到该会话")
    return conversation


@router.post("/api/conversations/{conversation_id}/cancel")
async def cancel_agent_conversation(conversation_id: int, service: WorldDependency):
    conversation = await service.cancel_agent_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="未找到该会话")
    return conversation


@router.get("/api/npcs/{npc_id}/conversations")
async def get_npc_agent_conversations(
    npc_id: int, service: WorldDependency, limit: int = Query(20, ge=1, le=100)
):
    if await service.get_npc(npc_id) is None:
        raise HTTPException(status_code=404, detail="未找到该 NPC")
    return await service.list_agent_conversations(npc_id=npc_id, limit=limit)


@router.get("/api/agent-cognition/status")
async def get_agent_cognition_status(service: WorldDependency):
    return await service.agent_cognition_status()


@router.put("/api/agent-cognition/status")
async def update_agent_cognition_status(request: CognitionRequest, service: WorldDependency):
    return await service.set_agent_cognition(request.enabled)


@router.get("/api/agent-cognition/check")
async def check_agent_cognition(service: WorldDependency):
    return await service.agent_cognition_safety_check()


@router.get("/api/agents/{npc_id}/cognition")
async def get_agent_cognition(npc_id: int, service: WorldDependency):
    result = await service.get_agent_cognition(npc_id)
    if result is None:
        raise HTTPException(status_code=404, detail="未找到该 NPC")
    return result


@router.put("/api/agents/{npc_id}/cognition")
async def update_agent_cognition(
    npc_id: int, request: CognitionRequest, service: WorldDependency
):
    if await service.get_npc(npc_id) is None:
        raise HTTPException(status_code=404, detail="未找到该 NPC")
    try:
        return await service.set_npc_agent_cognition(npc_id, request.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/npcs/{npc_id}/reflections")
async def get_agent_reflections(
    npc_id: int, service: WorldDependency, limit: int = Query(30, ge=1, le=100)
):
    if await service.get_npc(npc_id) is None:
        raise HTTPException(status_code=404, detail="未找到该 NPC")
    return await service.list_agent_reflections(npc_id, limit)


@router.get("/api/npcs/{npc_id}/beliefs")
async def get_agent_beliefs(npc_id: int, service: WorldDependency):
    result = await service.get_agent_cognition(npc_id)
    if result is None:
        raise HTTPException(status_code=404, detail="未找到该 NPC")
    return result["subjective_beliefs"]


@router.get("/api/npcs/{npc_id}/plans")
async def get_agent_plans(
    npc_id: int, service: WorldDependency, limit: int = Query(100, ge=1, le=200)
):
    if await service.get_npc(npc_id) is None:
        raise HTTPException(status_code=404, detail="未找到该 NPC")
    return await service.list_agent_plans(npc_id, limit)


@router.post("/api/reflection-tasks/{task_id}/cancel")
async def cancel_agent_reflection(task_id: int, service: WorldDependency):
    task = await service.cancel_agent_reflection_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="未找到该反思任务")
    return task
