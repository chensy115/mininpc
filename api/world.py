from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from api.dependencies import get_world_service
from simulation.world import WorldService
from simulation.productization import (
    CreateSaveRequest,
    ImportSaveRequest,
    OnboardingRequest,
    PRESETS,
)


router = APIRouter(prefix="/api", tags=["world"])
WorldDependency = Annotated[WorldService, Depends(get_world_service)]


class SpeedRequest(BaseModel):
    speed: int


class StabilityRequest(BaseModel):
    days: Literal[30, 90, 365]
    commit_interval: int = 144


@router.get("/world")
async def get_world(service: WorldDependency):
    return await service.world_snapshot()


@router.get("/events")
async def get_events(service: WorldDependency, limit: int = Query(100, ge=1, le=500)):
    return await service.list_events(limit)


@router.get("/relationships")
async def get_relationships(service: WorldDependency):
    return await service.list_relationships()


@router.get("/goals")
async def get_goals(service: WorldDependency):
    return await service.list_goals()


@router.get("/narrative/status")
async def get_narrative_status(service: WorldDependency):
    return await service.narrative_status()


@router.get("/narratives/events")
async def get_event_narratives(
    service: WorldDependency,
    limit: int = Query(50, ge=1, le=100),
):
    return await service.list_narratives("event_explanation", limit=limit)


@router.get("/economy")
async def get_economy_status(service: WorldDependency):
    return await service.economy_status()


@router.get("/professions")
async def get_professions(service: WorldDependency):
    return await service.list_professions()


@router.get("/stores")
async def get_stores(service: WorldDependency):
    return await service.list_stores()


@router.get("/career-budget")
async def get_career_budget_status(service: WorldDependency):
    return await service.career_budget_status()


@router.get("/economic-reports")
async def get_economic_reports(
    service: WorldDependency,
    limit: int = Query(50, ge=1, le=200),
):
    return await service.list_weekly_reports(limit=limit)


@router.get("/community-rhythm")
async def get_community_rhythm_status(service: WorldDependency):
    return await service.community_status()


@router.get("/institutions")
async def get_institutions(service: WorldDependency):
    return await service.list_institutions()


@router.get("/store-stock")
async def get_store_stock(service: WorldDependency):
    return await service.list_stock()


@router.get("/social-life")
async def get_social_life_status(service: WorldDependency):
    return await service.social_life_status()


@router.get("/social-bonds")
async def get_social_bonds(service: WorldDependency):
    return await service.list_social_bonds()


@router.get("/friend-circles")
async def get_friend_circles(service: WorldDependency):
    return await service.list_friend_circles()


@router.get("/commitments")
async def get_commitments(service: WorldDependency):
    return await service.list_commitments()


@router.get("/cohousing")
async def get_cohousing(service: WorldDependency):
    return await service.list_households()


@router.get("/life-story")
async def get_life_story_status(service: WorldDependency):
    return await service.life_story_status()


@router.get("/milestones")
async def get_milestones(
    service: WorldDependency,
    npc_id: int | None = Query(None, ge=1),
    milestone_type: str | None = None,
    limit: int = Query(100, ge=1, le=500),
):
    return await service.list_milestones(npc_id, milestone_type, limit)


@router.get("/milestones/{milestone_id}/causal-chain")
async def get_milestone_causal_chain(milestone_id: int, service: WorldDependency):
    chain = await service.get_causal_chain(milestone_id)
    if chain is None:
        raise HTTPException(status_code=404, detail="未找到该人生里程碑")
    return chain


@router.get("/story-summaries")
async def get_story_summaries(
    service: WorldDependency,
    period_type: Literal["week", "month"] | None = None,
    limit: int = Query(50, ge=1, le=200),
):
    return await service.list_story_summaries(period_type, limit)


@router.get("/story-replay")
async def get_story_replay(
    service: WorldDependency,
    start_minute: int | None = Query(None, ge=0),
    end_minute: int | None = Query(None, ge=1),
    seed: int | None = None,
):
    try:
        return await service.replay_life_story(start_minute, end_minute, seed)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/product")
async def get_product_status(service: WorldDependency):
    return await service.productization_status()


@router.get("/world-presets")
async def get_world_presets():
    return [{"key": key, **value} for key, value in PRESETS.items()]


@router.get("/world-statistics")
async def get_world_statistics(service: WorldDependency):
    return await service.world_statistics()


@router.get("/balance")
async def get_balance(service: WorldDependency):
    return await service.balance_status()


@router.get("/upgrade-reports")
async def get_upgrade_reports(service: WorldDependency):
    return await service.list_upgrade_reports()


@router.get("/onboarding")
async def get_onboarding(service: WorldDependency):
    return await service.get_onboarding()


@router.put("/onboarding")
async def put_onboarding(payload: OnboardingRequest, service: WorldDependency):
    try:
        return await service.set_onboarding(payload)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/saves")
async def get_save_slots(service: WorldDependency):
    return await service.list_save_slots()


@router.post("/saves", status_code=201)
async def create_save_slot(payload: CreateSaveRequest, service: WorldDependency):
    try:
        return await service.create_save_slot(payload)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/saves/{slot_id}/export", status_code=201)
async def export_save_slot(slot_id: str, service: WorldDependency):
    try:
        return await service.export_save_slot(slot_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/exports/{export_id}")
async def download_export(export_id: str, service: WorldDependency):
    if not service.product_enabled or service.save_manager is None:
        raise HTTPException(status_code=404, detail="V1.0 数据导出未启用")
    try:
        path = service.save_manager.export_path(export_id)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, media_type="application/vnd.miniworld.save+zip", filename=f"{export_id}.mworld")


@router.post("/saves/import", status_code=201)
async def import_save(payload: ImportSaveRequest, service: WorldDependency):
    try:
        return await service.import_save_slot(payload)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/stability/run")
async def run_stability(payload: StabilityRequest, service: WorldDependency):
    if not service.product_enabled:
        raise HTTPException(status_code=409, detail="V1.0 稳定性验证未启用")
    ticks = payload.days * 144
    return {"days": payload.days, **await service.run_ticks(ticks, commit_interval=payload.commit_interval)}


@router.post("/world/pause")
async def pause_world(service: WorldDependency):
    return await service.set_paused(True)


@router.post("/world/resume")
async def resume_world(service: WorldDependency):
    return await service.set_paused(False)


@router.post("/world/speed")
async def change_speed(payload: SpeedRequest, service: WorldDependency):
    try:
        return await service.set_speed(payload.speed)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/world/reset")
async def reset_world(service: WorldDependency):
    return await service.reset()
