from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from api.dependencies import get_world_service
from simulation.world import WorldService


router = APIRouter(prefix="/api/npcs", tags=["npcs"])
WorldDependency = Annotated[WorldService, Depends(get_world_service)]


@router.get("")
async def list_npcs(service: WorldDependency):
    return await service.list_npcs()


@router.get("/{npc_id}")
async def get_npc(npc_id: int, service: WorldDependency):
    npc = await service.get_npc(npc_id)
    if npc is None:
        raise HTTPException(status_code=404, detail="未找到该 NPC")
    return npc


@router.get("/{npc_id}/decision")
async def get_decision(npc_id: int, service: WorldDependency):
    npc = await service.get_npc(npc_id)
    if npc is None:
        raise HTTPException(status_code=404, detail="未找到该 NPC")
    decision = await service.latest_decision(npc_id)
    return decision or {"npc_id": npc_id, "chosen_action": None, "candidates": [], "reason": {"summary": "正在等待第一次决策 Tick"}}


@router.get("/{npc_id}/memories")
async def get_memories(
    npc_id: int,
    service: WorldDependency,
    limit: int = Query(50, ge=1, le=100),
    min_importance: int = Query(1, ge=1, le=10),
    emotion: Literal["positive", "neutral", "negative"] | None = None,
):
    npc = await service.get_npc(npc_id)
    if npc is None:
        raise HTTPException(status_code=404, detail="未找到该 NPC")
    return await service.list_memories(
        npc_id,
        limit=limit,
        min_importance=min_importance,
        emotion=emotion,
    )


@router.get("/{npc_id}/goals")
async def get_goals(npc_id: int, service: WorldDependency):
    npc = await service.get_npc(npc_id)
    if npc is None:
        raise HTTPException(status_code=404, detail="未找到该 NPC")
    return await service.list_goals(npc_id)


@router.get("/{npc_id}/dialogues")
async def get_dialogues(
    npc_id: int,
    service: WorldDependency,
    limit: int = Query(20, ge=1, le=100),
):
    npc = await service.get_npc(npc_id)
    if npc is None:
        raise HTTPException(status_code=404, detail="未找到该 NPC")
    return await service.list_narratives("dialogue", npc_id=npc_id, limit=limit)


@router.get("/{npc_id}/goal-narratives")
async def get_goal_narratives(npc_id: int, service: WorldDependency):
    npc = await service.get_npc(npc_id)
    if npc is None:
        raise HTTPException(status_code=404, detail="未找到该 NPC")
    return await service.list_narratives("goal_narrative", npc_id=npc_id, limit=100)


@router.get("/{npc_id}/memory-summaries")
async def get_memory_summaries(
    npc_id: int,
    service: WorldDependency,
    limit: int = Query(10, ge=1, le=100),
):
    npc = await service.get_npc(npc_id)
    if npc is None:
        raise HTTPException(status_code=404, detail="未找到该 NPC")
    return await service.list_narratives("memory_summary", npc_id=npc_id, limit=limit)


@router.get("/{npc_id}/economy")
async def get_npc_economy(npc_id: int, service: WorldDependency):
    economy = await service.get_npc_economy(npc_id)
    if economy is None:
        raise HTTPException(status_code=404, detail="未找到该 NPC")
    return economy


@router.get("/{npc_id}/career")
async def get_npc_career(npc_id: int, service: WorldDependency):
    career = await service.get_npc_career(npc_id)
    if career is None:
        raise HTTPException(status_code=404, detail="未找到该 NPC")
    return career


@router.get("/{npc_id}/budget")
async def get_npc_budget(npc_id: int, service: WorldDependency):
    budget = await service.get_npc_budget(npc_id)
    if budget is None:
        raise HTTPException(status_code=404, detail="未找到该 NPC")
    return budget


@router.get("/{npc_id}/economic-reports")
async def get_npc_economic_reports(
    npc_id: int,
    service: WorldDependency,
    limit: int = Query(20, ge=1, le=100),
):
    npc = await service.get_npc(npc_id)
    if npc is None:
        raise HTTPException(status_code=404, detail="未找到该 NPC")
    return await service.list_weekly_reports(npc_id=npc_id, limit=limit)


@router.get("/{npc_id}/rhythm")
async def get_npc_rhythm(npc_id: int, service: WorldDependency):
    rhythm = await service.get_npc_rhythm(npc_id)
    if rhythm is None:
        raise HTTPException(status_code=404, detail="未找到该 NPC")
    return rhythm


@router.get("/{npc_id}/social-life")
async def get_npc_social_life(npc_id: int, service: WorldDependency):
    social_life = await service.get_npc_social_life(npc_id)
    if social_life is None:
        raise HTTPException(status_code=404, detail="未找到该 NPC")
    return social_life


@router.get("/{npc_id}/timeline")
async def get_npc_timeline(
    npc_id: int,
    service: WorldDependency,
    limit: int = Query(100, ge=1, le=500),
):
    timeline = await service.get_npc_timeline(npc_id, limit)
    if timeline is None:
        raise HTTPException(status_code=404, detail="未找到该 NPC")
    return timeline
