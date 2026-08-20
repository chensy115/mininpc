from __future__ import annotations

from database.models import NPC, Relationship


def clamp_relationship(value: int | float) -> int:
    return max(-100, min(100, round(value)))


def social_delta(actor: NPC, existing_score: int, variation: float) -> int:
    quality = actor.kindness * 3.5 + actor.mood / 45 + max(-1.0, existing_score / 100)
    if actor.mood < 25 or actor.kindness < 0.25:
        quality -= 4.0
    return max(-3, min(5, round(quality + variation - 1.5)))


def update_relationship(relationship: Relationship, delta: int) -> int:
    before = relationship.score
    relationship.score = clamp_relationship(before + delta)
    return relationship.score - before

