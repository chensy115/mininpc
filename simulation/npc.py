from __future__ import annotations

from typing import Any

from database.models import NPC


STATE_FIELDS = ("energy", "hunger", "mood", "social_need", "work_satisfaction")
PERSONALITY_FIELDS = ("extroversion", "kindness", "ambition", "risk_tolerance", "discipline")


def clamp(value: float, minimum: float = 0.0, maximum: float = 100.0) -> float:
    return round(max(minimum, min(maximum, value)), 2)


def clamp_npc_state(npc: NPC) -> None:
    for field in STATE_FIELDS:
        setattr(npc, field, clamp(float(getattr(npc, field))))


def npc_to_dict(npc: NPC) -> dict[str, Any]:
    return {
        "id": npc.id,
        "name": npc.name,
        "age": npc.age,
        "job": npc.job,
        "current_location": npc.current_location,
        "current_action": npc.current_action,
        "action_end_minute": npc.action_end_minute,
        "money": round(npc.money, 2),
        "states": {field: round(float(getattr(npc, field)), 2) for field in STATE_FIELDS},
        "personality": {field: round(float(getattr(npc, field)), 2) for field in PERSONALITY_FIELDS},
    }

