from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, TYPE_CHECKING

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from database.models import (
    DecisionLog,
    Event,
    NPC,
    NarrativeArtifact,
    NarrativeJob,
    Relationship,
    WorldState,
)
from simulation.agent_brain import agent_shadow_snapshot
from simulation.clock import ClockSnapshot
from simulation.community import npc_rhythm_snapshot
from simulation.decision import LOCATIONS
from simulation.goals import goal_snapshots
from simulation.narrative import artifact_to_dict
from simulation.npc import npc_to_dict
from simulation.social_life import npc_social_snapshot

if TYPE_CHECKING:
    from simulation.world import WorldService


SCHEMA_VERSION = "1.0"
DASHBOARD_GROUPS = ("runtime", "world", "npcs", "pulse")
NPC_SECTIONS = ("overview", "decision")

_PRIVATE_KEYS = {
    "api_key",
    "chain_of_thought",
    "hidden_reasoning",
    "prompt",
    "raw_prompt",
    "raw_response",
    "reasoning_content",
    "response",
}


class DashboardNPCNotFound(LookupError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _public_data(value: Any) -> Any:
    """Copy a snapshot while enforcing the dashboard's stricter privacy boundary."""

    if isinstance(value, dict):
        return {
            key: _public_data(item)
            for key, item in value.items()
            if str(key).lower() not in _PRIVATE_KEYS
        }
    if isinstance(value, list):
        return [_public_data(item) for item in value]
    if isinstance(value, tuple):
        return [_public_data(item) for item in value]
    return value


def world_data(
    session: Session,
    *,
    state: WorldState | None = None,
    npcs: list[NPC] | None = None,
) -> dict[str, Any]:
    state = state or session.get(WorldState, 1)
    if state is None:
        raise RuntimeError("world_unavailable")
    clock = ClockSnapshot(state.total_minutes)
    people = npcs if npcs is not None else list(session.scalars(select(NPC).order_by(NPC.id)))
    return {
        "day": clock.day,
        "weekday": clock.weekday,
        "time": clock.time_text,
        "label": clock.label,
        "total_minutes": state.total_minutes,
        "paused": state.paused,
        "speed": state.speed,
        "locations": {
            location: [npc_to_dict(npc) for npc in people if npc.current_location == location]
            for location in LOCATIONS
        },
    }


def npc_core_data(session: Session, npc: NPC) -> dict[str, Any]:
    data = npc_to_dict(npc)
    names = {person.id: person.name for person in session.scalars(select(NPC))}
    relationships = list(
        session.scalars(
            select(Relationship)
            .where(Relationship.from_npc_id == npc.id)
            .order_by(Relationship.to_npc_id)
        )
    )
    data["relationships"] = [
        {
            "npc_id": item.to_npc_id,
            "name": names.get(item.to_npc_id),
            "score": item.score,
        }
        for item in relationships
    ]
    return data


def decision_data(session: Session, npc_id: int) -> dict[str, Any] | None:
    record = session.scalar(
        select(DecisionLog)
        .where(DecisionLog.npc_id == npc_id)
        .order_by(DecisionLog.id.desc())
        .limit(1)
    )
    if record is None:
        return None
    return {
        "id": record.id,
        "npc_id": record.npc_id,
        "world_day": record.world_day,
        "world_time": record.world_time,
        "chosen_action": record.chosen_action,
        "candidates": json.loads(record.candidates_json),
        "reason": json.loads(record.reason_json),
    }


def events_data(session: Session, limit: int = 100) -> list[dict[str, Any]]:
    records = list(
        session.scalars(
            select(Event).order_by(Event.id.desc()).limit(min(max(limit, 1), 500))
        )
    )
    names = {npc.id: npc.name for npc in session.scalars(select(NPC))}
    return [
        {
            "id": record.id,
            "world_day": record.world_day,
            "world_time": record.world_time,
            "event_type": record.event_type,
            "npc_id": record.npc_id,
            "npc_name": names.get(record.npc_id),
            "target_npc_id": record.target_npc_id,
            "target_npc_name": names.get(record.target_npc_id),
            "location": record.location,
            "description": record.description,
            "metadata": json.loads(record.metadata_json),
        }
        for record in records
    ]


def narratives_data(
    session: Session,
    kind: str,
    *,
    npc_id: int | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    query = select(NarrativeArtifact).where(NarrativeArtifact.kind == kind)
    if npc_id is not None:
        if kind == "dialogue":
            query = query.where(
                or_(
                    NarrativeArtifact.npc_id == npc_id,
                    NarrativeArtifact.related_npc_id == npc_id,
                )
            )
        else:
            query = query.where(NarrativeArtifact.npc_id == npc_id)
    records = list(
        session.scalars(
            query.order_by(NarrativeArtifact.id.desc()).limit(min(max(limit, 1), 100))
        )
    )
    names = {npc.id: npc.name for npc in session.scalars(select(NPC))}
    return [artifact_to_dict(record, names) for record in records]


def narrative_status_data(service: WorldService, session: Session) -> dict[str, Any]:
    counts = {
        status: session.scalar(
            select(func.count())
            .select_from(NarrativeJob)
            .where(NarrativeJob.status == status)
        )
        or 0
        for status in ("pending", "processing", "completed")
    }
    fallback_count = session.scalar(
        select(func.count())
        .select_from(NarrativeArtifact)
        .where(NarrativeArtifact.fallback_used.is_(True))
    ) or 0
    return {
        **service.narrative_generator.status(),
        "jobs": counts,
        "fallback_artifacts": fallback_count,
    }


def _rhythm_data(
    service: WorldService,
    session: Session,
    npc: NPC,
    state: WorldState,
) -> dict[str, Any]:
    if not service.community_enabled:
        return {
            "enabled": False,
            "mode": "v0.6-compatible",
            "npc_id": npc.id,
            "npc_name": npc.name,
        }
    snapshot = npc_rhythm_snapshot(session, npc, ClockSnapshot(state.total_minutes))
    return {
        "enabled": snapshot is not None,
        "mode": "v0.7" if snapshot else "v0.6-compatible",
        **(snapshot or {"npc_id": npc.id, "npc_name": npc.name}),
    }


def _social_data(service: WorldService, session: Session, npc: NPC) -> dict[str, Any]:
    if not service.social_life_enabled:
        return {
            "enabled": False,
            "mode": "v0.7-compatible",
            "npc_id": npc.id,
            "npc_name": npc.name,
        }
    snapshot = npc_social_snapshot(session, npc)
    return {
        "enabled": snapshot is not None,
        "mode": "v0.8" if snapshot else "v0.7-compatible",
        **(snapshot or {"npc_id": npc.id, "npc_name": npc.name}),
    }


def _group_world(
    service: WorldService,
    session: Session,
    state: WorldState,
    npcs: list[NPC],
) -> dict[str, Any]:
    return world_data(session, state=state, npcs=npcs)


def _group_npcs(
    service: WorldService,
    session: Session,
    state: WorldState,
    npcs: list[NPC],
) -> dict[str, Any]:
    return {
        "items": [npc_to_dict(npc) for npc in npcs],
        "agents": service._agent_takeover_overview_snapshot(session, npcs=npcs),
    }


def _group_pulse(
    service: WorldService,
    session: Session,
    state: WorldState,
    npcs: list[NPC],
) -> dict[str, Any]:
    return {
        "events": events_data(session, limit=40),
        "narrative_status": narrative_status_data(service, session),
        "narratives": narratives_data(session, "event_explanation", limit=80),
    }


_GROUP_BUILDERS: dict[
    str, Callable[[WorldService, Session, WorldState, list[NPC]], dict[str, Any]]
] = {
    "world": _group_world,
    "npcs": _group_npcs,
    "pulse": _group_pulse,
}


def _section_overview(
    service: WorldService,
    session: Session,
    state: WorldState,
    npc: NPC,
) -> dict[str, Any]:
    return {
        "npc": npc_core_data(session, npc),
        "goals": [
            {"npc_id": npc.id, "npc_name": npc.name, **snapshot}
            for snapshot in goal_snapshots(session, npc)
        ],
        "goal_narratives": narratives_data(
            session, "goal_narrative", npc_id=npc.id, limit=100
        ),
        "rhythm": _rhythm_data(service, session, npc, state),
        "social": _social_data(service, session, npc),
    }


def _section_decision(
    service: WorldService,
    session: Session,
    state: WorldState,
    npc: NPC,
) -> dict[str, Any]:
    latest = decision_data(session, npc.id)
    return {
        "decision": latest
        or {
            "npc_id": npc.id,
            "chosen_action": None,
            "candidates": [],
            "reason": {"summary": "正在等待第一次决策 Tick"},
        },
        "shadow": agent_shadow_snapshot(
            session, npc.id, service.agent_enabled, service.agent_generator
        ),
        "control": service._agent_control_snapshot(session, npc.id),
    }


_SECTION_BUILDERS: dict[
    str, Callable[[WorldService, Session, WorldState, NPC], dict[str, Any]]
] = {
    "overview": _section_overview,
    "decision": _section_decision,
}


def _ok_module(
    *,
    data: Any,
    snapshot_id: str,
    world_minute: int,
    version: str | None = None,
    **sampling: Any,
) -> dict[str, Any]:
    return {
        "status": "ok",
        "version": version or snapshot_id,
        "snapshot_id": snapshot_id,
        "world_minute": world_minute,
        **sampling,
        "data": _public_data(data),
    }


def _error_module(
    name: str,
    *,
    snapshot_id: str,
    world_minute: int,
    version: str | None = None,
    **sampling: Any,
) -> dict[str, Any]:
    return {
        "status": "error",
        "version": version or snapshot_id,
        "snapshot_id": snapshot_id,
        "world_minute": world_minute,
        **sampling,
        "error": {
            "code": f"{name}_snapshot_unavailable",
            "message": f"{name} snapshot is temporarily unavailable",
            "retryable": True,
        },
    }


def _metadata(service: WorldService, world_minute: int) -> tuple[str, str]:
    service._dashboard_snapshot_sequence += 1
    snapshot_id = f"{world_minute}:{service._dashboard_snapshot_sequence}"
    return snapshot_id, _utc_now()


async def build_dashboard_snapshot(
    service: WorldService,
    groups: Iterable[str],
) -> dict[str, Any]:
    requested = tuple(groups)
    modules: dict[str, dict[str, Any]] = {}
    async with service.lock:
        with service.session_factory() as session:
            state = session.get(WorldState, 1)
            if state is None:
                raise RuntimeError("world_unavailable")
            world_minute = int(state.total_minutes)
            snapshot_id, captured_at = _metadata(service, world_minute)
            npcs = list(session.scalars(select(NPC).order_by(NPC.id)))
            for group in requested:
                if group == "runtime":
                    continue
                try:
                    data = _GROUP_BUILDERS[group](service, session, state, npcs)
                    modules[group] = _ok_module(
                        data=data,
                        snapshot_id=snapshot_id,
                        world_minute=world_minute,
                    )
                except Exception:
                    modules[group] = _error_module(
                        group,
                        snapshot_id=snapshot_id,
                        world_minute=world_minute,
                    )

    if "runtime" in requested:
        try:
            runtime = await service.runtime_status()
            observed_at = _utc_now()
            generation = runtime.get("generation")
            modules["runtime"] = _ok_module(
                data=runtime,
                snapshot_id=snapshot_id,
                world_minute=world_minute,
                version=f"generation:{generation}" if generation is not None else "generation:unknown",
                generation=generation,
                observed_at=observed_at,
            )
        except Exception:
            modules["runtime"] = _error_module(
                "runtime",
                snapshot_id=snapshot_id,
                world_minute=world_minute,
                version="generation:unknown",
                generation=None,
                observed_at=_utc_now(),
            )

    return {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "captured_at": captured_at,
        "world_minute": world_minute,
        "modules": {name: modules[name] for name in requested},
    }


async def build_npc_dashboard_snapshot(
    service: WorldService,
    npc_id: int,
    sections: Iterable[str],
) -> dict[str, Any]:
    requested = tuple(sections)
    modules: dict[str, dict[str, Any]] = {}
    async with service.lock:
        with service.session_factory() as session:
            state = session.get(WorldState, 1)
            if state is None:
                raise RuntimeError("world_unavailable")
            npc = session.get(NPC, npc_id)
            if npc is None:
                raise DashboardNPCNotFound(npc_id)
            world_minute = int(state.total_minutes)
            snapshot_id, captured_at = _metadata(service, world_minute)
            for section in requested:
                try:
                    data = _SECTION_BUILDERS[section](service, session, state, npc)
                    modules[section] = _ok_module(
                        data=data,
                        snapshot_id=snapshot_id,
                        world_minute=world_minute,
                    )
                except Exception:
                    modules[section] = _error_module(
                        section,
                        snapshot_id=snapshot_id,
                        world_minute=world_minute,
                    )

    return {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "captured_at": captured_at,
        "world_minute": world_minute,
        "modules": {name: modules[name] for name in requested},
    }
