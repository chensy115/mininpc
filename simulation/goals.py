from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import LongTermGoal, NPC, Relationship
from simulation.npc import clamp


GOAL_TYPES = ("savings", "friendship", "career_satisfaction", "relationship")
GOAL_LABELS = {
    "savings": "建立储蓄",
    "friendship": "结交朋友",
    "career_satisfaction": "提升职业满意度",
    "relationship": "建设重要关系",
}

# Targets differ by character, while priorities are derived from personality below.
SAVINGS_TARGETS = {1: 300.0, 2: 350.0, 3: 450.0, 4: 250.0, 5: 400.0}
CAREER_TARGETS = {1: 84.0, 2: 86.0, 3: 90.0, 4: 82.0, 5: 88.0}
RELATIONSHIP_TARGETS = {1: 2, 2: 1, 3: 2, 4: 1, 5: 3}


def _priority(value: float) -> float:
    return round(max(0.25, min(1.0, value)), 2)


def default_goals_for(npc: NPC, created_minute: int) -> list[LongTermGoal]:
    relationship_target = RELATIONSHIP_TARGETS.get(npc.id)
    return [
        LongTermGoal(
            npc_id=npc.id,
            goal_key="savings",
            goal_type="savings",
            target_value=SAVINGS_TARGETS.get(npc.id, max(250.0, npc.money + 150.0)),
            priority=_priority(0.35 + npc.discipline * 0.45 + npc.ambition * 0.2),
            created_minute=created_minute,
        ),
        LongTermGoal(
            npc_id=npc.id,
            goal_key="friendship",
            goal_type="friendship",
            target_value=2.0,
            priority=_priority(0.3 + npc.extroversion * 0.55 + npc.kindness * 0.15),
            created_minute=created_minute,
        ),
        LongTermGoal(
            npc_id=npc.id,
            goal_key="career_satisfaction",
            goal_type="career_satisfaction",
            target_value=CAREER_TARGETS.get(npc.id, 85.0),
            priority=_priority(0.3 + npc.ambition * 0.5 + npc.discipline * 0.2),
            created_minute=created_minute,
        ),
        LongTermGoal(
            npc_id=npc.id,
            goal_key=f"relationship:{relationship_target}",
            goal_type="relationship",
            target_value=60.0,
            priority=_priority(0.25 + npc.kindness * 0.4 + npc.extroversion * 0.25),
            target_npc_id=relationship_target,
            created_minute=created_minute,
        ),
    ]


def ensure_default_goals(session: Session, npcs: Iterable[NPC], created_minute: int) -> int:
    """Backfill only missing defaults, so an existing V0.2 world upgrades in place."""
    created = 0
    existing = {
        (goal.npc_id, goal.goal_key)
        for goal in session.scalars(select(LongTermGoal))
    }
    npc_list = list(npcs)
    valid_ids = {npc.id for npc in npc_list}
    for npc in npc_list:
        for goal in default_goals_for(npc, created_minute):
            if goal.target_npc_id is not None and goal.target_npc_id not in valid_ids:
                continue
            if (goal.npc_id, goal.goal_key) in existing:
                continue
            session.add(goal)
            existing.add((goal.npc_id, goal.goal_key))
            created += 1
    return created


def _current_values(session: Session, npc: NPC) -> dict[tuple[str, int | None], float]:
    relationships = list(
        session.scalars(
            select(Relationship).where(Relationship.from_npc_id == npc.id)
        )
    )
    relationship_scores = {item.to_npc_id: float(item.score) for item in relationships}
    friend_count = float(sum(item.score >= 30 for item in relationships))
    values: dict[tuple[str, int | None], float] = {
        ("savings", None): float(npc.money),
        ("friendship", None): friend_count,
        ("career_satisfaction", None): float(npc.work_satisfaction),
    }
    for target_id, score in relationship_scores.items():
        values[("relationship", target_id)] = score
    return values


def goal_snapshots(session: Session, npc: NPC) -> list[dict[str, Any]]:
    goals = list(
        session.scalars(
            select(LongTermGoal)
            .where(LongTermGoal.npc_id == npc.id)
            .order_by(LongTermGoal.id)
        )
    )
    names = {person.id: person.name for person in session.scalars(select(NPC))}
    current_values = _current_values(session, npc)
    result: list[dict[str, Any]] = []
    for goal in goals:
        key = (goal.goal_type, goal.target_npc_id if goal.goal_type == "relationship" else None)
        current = current_values.get(key, 0.0)
        target = max(0.01, float(goal.target_value))
        progress = clamp(current / target * 100.0)
        need = clamp((target - current) / target * 100.0)
        result.append(
            {
                "id": goal.id,
                "goal_key": goal.goal_key,
                "type": goal.goal_type,
                "label": GOAL_LABELS[goal.goal_type],
                "priority": round(float(goal.priority), 2),
                "current_value": round(current, 2),
                "target_value": round(target, 2),
                "progress": progress,
                "need_score": need,
                "status": "completed" if current >= target else "active",
                "target_npc_id": goal.target_npc_id,
                "target_npc_name": names.get(goal.target_npc_id),
                "created_minute": goal.created_minute,
            }
        )
    return result


def build_goal_context(session: Session, npc: NPC) -> dict[str, dict[str, Any]]:
    """Compact, read-only goal inputs consumed by Utility AI and action outcomes."""
    return {goal["type"]: goal for goal in goal_snapshots(session, npc)}
