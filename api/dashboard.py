from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from api.dependencies import get_world_service
from simulation.dashboard import (
    DASHBOARD_GROUPS,
    NPC_SECTIONS,
    DashboardNPCNotFound,
)
from simulation.world import WorldService


router = APIRouter(prefix="/api/dashboard", tags=["dashboard-snapshots"])
WorldDependency = Annotated[WorldService, Depends(get_world_service)]


def _selection(
    raw: str | None,
    *,
    default: tuple[str, ...],
    allowed: tuple[str, ...],
    parameter: str,
) -> tuple[str, ...]:
    if raw is None:
        return default
    values = tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    invalid = [value for value in values if value not in allowed]
    if not values or invalid:
        raise HTTPException(
            status_code=422,
            detail={
                "code": f"invalid_{parameter}",
                "message": f"{parameter} must be a comma-separated subset of the allowed values",
                "allowed": list(allowed),
                "invalid": invalid,
            },
        )
    return values


@router.get("/snapshot")
async def get_dashboard_snapshot(
    service: WorldDependency,
    groups: Annotated[
        str | None,
        Query(description="Comma-separated dashboard groups"),
    ] = None,
):
    selected = _selection(
        groups,
        default=DASHBOARD_GROUPS,
        allowed=DASHBOARD_GROUPS,
        parameter="groups",
    )
    return await service.dashboard_snapshot(selected)


@router.get("/npcs/{npc_id}/snapshot")
async def get_npc_dashboard_snapshot(
    npc_id: int,
    service: WorldDependency,
    sections: Annotated[
        str | None,
        Query(description="Comma-separated NPC snapshot sections"),
    ] = None,
):
    selected = _selection(
        sections,
        default=NPC_SECTIONS,
        allowed=NPC_SECTIONS,
        parameter="sections",
    )
    try:
        return await service.dashboard_npc_snapshot(npc_id, selected)
    except DashboardNPCNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "npc_not_found", "message": "NPC not found"},
        ) from exc
