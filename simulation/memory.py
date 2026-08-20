from __future__ import annotations

from sqlalchemy.orm import Session

from database.models import Memory
from simulation.clock import ClockSnapshot


VALID_EMOTIONS = {"positive", "neutral", "negative"}


def clamp_importance(value: int | float) -> int:
    return max(1, min(10, round(value)))


def add_memory(
    session: Session,
    clock: ClockSnapshot,
    npc_id: int,
    content: str,
    *,
    importance: int,
    emotion: str,
    related_npc_id: int | None = None,
) -> Memory:
    if emotion not in VALID_EMOTIONS:
        raise ValueError(f"不支持的记忆情绪：{emotion}")
    record = Memory(
        npc_id=npc_id,
        content=content,
        importance=clamp_importance(importance),
        emotion=emotion,
        timestamp=clock.total_minutes,
        related_npc_id=related_npc_id,
    )
    session.add(record)
    return record
