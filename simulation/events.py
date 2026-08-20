from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from database.models import Event
from simulation.clock import ClockSnapshot


def add_event(
    session: Session,
    clock: ClockSnapshot,
    event_type: str,
    description: str,
    *,
    npc_id: int | None = None,
    target_npc_id: int | None = None,
    location: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Event:
    record = Event(
        world_day=clock.day,
        world_time=clock.time_text,
        event_type=event_type,
        npc_id=npc_id,
        target_npc_id=target_npc_id,
        location=location,
        description=description,
        metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
    )
    session.add(record)
    return record

