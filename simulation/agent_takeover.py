from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from database.models import (
    AgentDecisionArtifact,
    AgentDecisionJob,
    AgentTakeoverTurn,
    CareerDevelopment,
    CommunityInstitution,
    EmploymentProfile,
    FacilityUsage,
    Housing,
    InventoryItem,
    ItemDefinition,
    NPC,
    NPCSkill,
    Relationship,
    Store,
    StoreListing,
    StoreStock,
    TrainingRecord,
)
from simulation.clock import ClockSnapshot
from simulation.community import (
    DAY_MINUTES,
    HOUSING_UPGRADES,
    TRAINING_FEE,
    TRAINING_WEEKLY_LIMIT,
    WEEK_MINUTES,
    institution_is_open,
)
from simulation.decision import Decision
from simulation.economy import PROFESSIONS, profession_definition


SUPPORTED_NPC_IDS = frozenset(range(1, 6))
TARGET_NPC_ID = 1  # V1.2-compatible default for callers that omit npc_id.
V12_TABLE_NAMES = {"agent_takeover_turns"}
MOVE_ACTIONS = {"GoHome", "GoOffice", "GoCafe", "GoPark"}
NO_TARGET_ACTIONS = {"Sleep", "Eat", "Work", "Relax", "Idle"}
PARAMETER_ACTIONS = {
    "Socialize",
    "Shop",
    "UseItem",
    "JobSearch",
    "UseFacility",
    "Train",
    "UpgradeHome",
} | MOVE_ACTIONS
ACTIVE_STATES = {"waiting", "ready", "agent_executing", "fallback_executing"}
EXECUTING_STATES = {"agent_executing", "fallback_executing"}


@dataclass(frozen=True)
class ActionOption:
    """One exact Engine-authored action/target pair exposed to the Agent."""

    action: str
    target: str | None
    params: dict[str, Any]
    description: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _candidate_value(candidate: Any, key: str, default: Any = None) -> Any:
    if isinstance(candidate, Mapping):
        return candidate.get(key, default)
    return getattr(candidate, key, default)


def _available_candidates(decision: Decision | Sequence[Any]) -> list[Any]:
    candidates = decision.candidates if isinstance(decision, Decision) else decision
    return [candidate for candidate in candidates if bool(_candidate_value(candidate, "available"))]


def _plain_option(action: str, description: str) -> ActionOption:
    return ActionOption(action=action, target=None, params={}, description=description)


def _social_options(
    session: Session,
    npc: NPC,
    description: str,
    social_context: Mapping[str, Any] | None,
) -> list[ActionOption]:
    people = list(
        session.scalars(
            select(NPC).where(
                NPC.current_location == npc.current_location,
                NPC.id != npc.id,
            ).order_by(NPC.id)
        )
    )
    partner_id = None
    if social_context and social_context.get("commitment_due"):
        partner_id = social_context.get("commitment_partner_id")
    result: list[ActionOption] = []
    for person in people:
        if partner_id is not None and person.id != partner_id:
            continue
        relationship = session.scalar(
            select(Relationship.id).where(
                Relationship.from_npc_id == npc.id,
                Relationship.to_npc_id == person.id,
            )
        )
        if relationship is None:
            continue
        result.append(
            ActionOption(
                action="Socialize",
                target=person.name,
                params={"target_npc_id": person.id, "target_npc_name": person.name},
                description=f"{description}：{person.name}",
            )
        )
    return result


def _shop_options(
    session: Session, npc: NPC, clock: ClockSnapshot, description: str
) -> list[ActionOption]:
    community_store = session.scalar(
        select(CommunityInstitution).where(
            CommunityInstitution.institution_key == "community_store"
        )
    )
    if community_store is not None and not institution_is_open(community_store, clock.total_minutes):
        return []
    any_stock_rows = session.scalar(select(func.count()).select_from(StoreStock)) or 0
    rows = session.execute(
        select(StoreListing, ItemDefinition, Store, StoreStock)
        .join(ItemDefinition, ItemDefinition.id == StoreListing.item_id)
        .join(Store, Store.id == StoreListing.store_id)
        .outerjoin(StoreStock, StoreStock.listing_id == StoreListing.id)
        .where(
            StoreListing.enabled.is_(True),
            Store.location == npc.current_location,
            StoreListing.price <= npc.money,
        )
        .order_by(StoreListing.id)
    ).all()
    result: list[ActionOption] = []
    for listing, item, store, stock in rows:
        if any_stock_rows and (stock is None or stock.quantity <= 0):
            continue
        result.append(
            ActionOption(
                action="Shop",
                target=item.item_key,
                params={
                    "listing_id": listing.id,
                    "item_id": item.id,
                    "item_key": item.item_key,
                    "store_id": store.id,
                    "price": round(float(listing.price), 2),
                },
                description=f"{description}：{item.name}",
            )
        )
    return result


def _item_options(session: Session, npc: NPC, description: str) -> list[ActionOption]:
    rows = session.execute(
        select(InventoryItem, ItemDefinition)
        .join(ItemDefinition, ItemDefinition.id == InventoryItem.item_id)
        .where(InventoryItem.npc_id == npc.id, InventoryItem.quantity > 0)
        .order_by(ItemDefinition.id)
    ).all()
    result: list[ActionOption] = []
    for inventory, item in rows:
        if item.item_key == "home_decor" and npc.current_location != "Home":
            continue
        result.append(
            ActionOption(
                action="UseItem",
                target=item.item_key,
                params={
                    "inventory_id": inventory.id,
                    "item_id": item.id,
                    "item_key": item.item_key,
                },
                description=f"{description}：{item.name}",
            )
        )
    return result


def _job_options(session: Session, npc: NPC, description: str) -> list[ActionOption]:
    employment = session.scalar(
        select(EmploymentProfile).where(EmploymentProfile.npc_id == npc.id)
    )
    career = session.scalar(
        select(CareerDevelopment).where(CareerDevelopment.npc_id == npc.id)
    )
    if employment is None or career is None:
        return []
    return [
        ActionOption(
            action="JobSearch",
            target=profession_key,
            params={"profession_key": profession_key},
            description=f"{description}：{definition['label']}",
        )
        for profession_key, definition in sorted(PROFESSIONS.items())
        if profession_key != employment.profession_key
    ]


def _daily_facility_usage(session: Session, institution_id: int, day: int) -> int:
    return session.scalar(
        select(func.count()).select_from(FacilityUsage).where(
            FacilityUsage.institution_id == institution_id,
            FacilityUsage.world_day == day,
        )
    ) or 0


def _facility_options(
    session: Session, npc: NPC, clock: ClockSnapshot, description: str
) -> list[ActionOption]:
    institution = session.scalar(
        select(CommunityInstitution).where(
            CommunityInstitution.institution_key == "park_wellness"
        )
    )
    if (
        institution is None
        or npc.current_location != institution.location
        or not institution_is_open(institution, clock.total_minutes)
    ):
        return []
    already_used = session.scalar(
        select(FacilityUsage.id).where(
            FacilityUsage.npc_id == npc.id,
            FacilityUsage.institution_id == institution.id,
            FacilityUsage.world_day == clock.day,
        )
    )
    used = _daily_facility_usage(session, institution.id, clock.day)
    if already_used is not None or (
        institution.daily_capacity is not None and used >= institution.daily_capacity
    ):
        return []
    return [
        ActionOption(
            action="UseFacility",
            target=institution.institution_key,
            params={
                "institution_id": institution.id,
                "institution_key": institution.institution_key,
                "service_key": institution.service_key,
            },
            description=f"{description}：{institution.name}",
        )
    ]


def _training_options(
    session: Session, npc: NPC, clock: ClockSnapshot, description: str
) -> list[ActionOption]:
    institution = session.scalar(
        select(CommunityInstitution).where(
            CommunityInstitution.institution_key == "career_center"
        )
    )
    employment = session.scalar(
        select(EmploymentProfile).where(EmploymentProfile.npc_id == npc.id)
    )
    if (
        institution is None
        or employment is None
        or npc.current_location != institution.location
        or npc.money < TRAINING_FEE
        or not institution_is_open(institution, clock.total_minutes)
    ):
        return []
    definition = profession_definition(employment.profession_key)
    skill = session.scalar(
        select(NPCSkill).where(
            NPCSkill.npc_id == npc.id,
            NPCSkill.skill_key == definition["skill"],
        )
    )
    if skill is None:
        return []
    week_start = (clock.total_minutes // WEEK_MINUTES) * WEEK_MINUTES
    weekly_count = session.scalar(
        select(func.count()).select_from(TrainingRecord).where(
            TrainingRecord.npc_id == npc.id,
            TrainingRecord.week_start_minute == week_start,
        )
    ) or 0
    day_start = (clock.day - 1) * DAY_MINUTES
    daily_count = session.scalar(
        select(func.count()).select_from(TrainingRecord).where(
            TrainingRecord.institution_id == institution.id,
            TrainingRecord.world_minute >= day_start,
            TrainingRecord.world_minute < day_start + DAY_MINUTES,
        )
    ) or 0
    if weekly_count >= TRAINING_WEEKLY_LIMIT or (
        institution.daily_capacity is not None and daily_count >= institution.daily_capacity
    ):
        return []
    return [
        ActionOption(
            action="Train",
            target=institution.institution_key,
            params={
                "institution_id": institution.id,
                "institution_key": institution.institution_key,
                "profession_key": employment.profession_key,
                "skill_key": skill.skill_key,
                "fee": TRAINING_FEE,
            },
            description=f"{description}：{institution.name}",
        )
    ]


def _upgrade_options(
    session: Session, npc: NPC, clock: ClockSnapshot, description: str
) -> list[ActionOption]:
    institution = session.scalar(
        select(CommunityInstitution).where(
            CommunityInstitution.institution_key == "housing_desk"
        )
    )
    housing = session.scalar(select(Housing).where(Housing.npc_id == npc.id))
    if (
        institution is None
        or housing is None
        or npc.current_location != "Home"
        or housing.arrears > 0
        or not institution_is_open(institution, clock.total_minutes)
    ):
        return []
    upgrade = HOUSING_UPGRADES.get(housing.tier)
    if upgrade is None or npc.money < float(upgrade["cost"]):
        return []
    return [
        ActionOption(
            action="UpgradeHome",
            target=str(upgrade["tier"]),
            params={
                "institution_id": institution.id,
                "institution_key": institution.institution_key,
                "housing_id": housing.id,
                "tier_before": housing.tier,
                "tier_after": upgrade["tier"],
                "cost": round(float(upgrade["cost"]), 2),
            },
            description=f"{description}：{upgrade['tier']}",
        )
    ]


def build_action_options(
    session: Session,
    npc: NPC,
    clock: ClockSnapshot,
    decision: Decision | Sequence[Any],
    *,
    social_context: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Expand available Utility candidates into exact, parameter-safe options.

    The function is read-only and consumes no RandomService values. Calling it
    once for the queued snapshot and again immediately before dispatch provides
    the two Engine-owned candidate sets required by takeover validation.
    """

    if npc.id not in SUPPORTED_NPC_IDS:
        return []
    options: list[ActionOption] = []
    for candidate in _available_candidates(decision):
        action = str(_candidate_value(candidate, "action", ""))
        description = str(_candidate_value(candidate, "explanation", action))
        if action in MOVE_ACTIONS:
            target = _candidate_value(candidate, "target_location")
            if isinstance(target, str) and target:
                options.append(
                    ActionOption(
                        action=action,
                        target=target,
                        params={"target_location": target},
                        description=description,
                    )
                )
        elif action == "Socialize":
            options.extend(_social_options(session, npc, description, social_context))
        elif action == "Shop":
            options.extend(_shop_options(session, npc, clock, description))
        elif action == "UseItem":
            options.extend(_item_options(session, npc, description))
        elif action == "JobSearch":
            options.extend(_job_options(session, npc, description))
        elif action == "UseFacility":
            options.extend(_facility_options(session, npc, clock, description))
        elif action == "Train":
            options.extend(_training_options(session, npc, clock, description))
        elif action == "UpgradeHome":
            options.extend(_upgrade_options(session, npc, clock, description))
        elif action in NO_TARGET_ACTIONS:
            options.append(_plain_option(action, description))
    return [option.to_dict() for option in options]


def _target_reason(action: str) -> str:
    return {
        **{move: "invalid_move_target" for move in MOVE_ACTIONS},
        "Socialize": "invalid_social_target",
        "Shop": "invalid_shop_target",
        "UseItem": "invalid_item_target",
        "JobSearch": "invalid_profession_target",
        "UseFacility": "invalid_facility_target",
        "Train": "invalid_training_target",
        "UpgradeHome": "invalid_upgrade_target",
    }.get(action, "invalid_target")


def validate_action_selection(
    action: str,
    target: str | None,
    options: Iterable[Mapping[str, Any]],
    *,
    dialogue: str | None = None,
) -> dict[str, Any]:
    """Validate only model-authored fields against an Engine option snapshot."""

    materialized = list(options)
    offered = [option for option in materialized if option.get("action") == action]
    action_offered = bool(offered)
    target_required = any(option.get("target") is not None for option in offered)
    matched = next((option for option in offered if option.get("target") == target), None)
    target_valid = matched is not None
    dialogue_valid = action == "Socialize" or dialogue is None
    if not action_offered:
        reason_code = "action_not_offered"
    elif target_required and target is None:
        reason_code = "missing_target"
    elif not target_valid:
        reason_code = "unexpected_target" if not target_required else _target_reason(action)
    elif not dialogue_valid:
        reason_code = "unexpected_dialogue"
    else:
        reason_code = "ok"
    legal = action_offered and target_valid and dialogue_valid
    return {
        "legal": legal,
        "action_offered": action_offered,
        "target_required": target_required,
        "target_valid": target_valid,
        "dialogue_valid": dialogue_valid,
        "reason_code": reason_code,
        "params": dict(matched.get("params") or {}) if matched is not None else None,
    }


def validate_latest_action(
    session: Session,
    npc: NPC,
    clock: ClockSnapshot,
    decision: Decision | Sequence[Any],
    *,
    action: str,
    target: str | None,
    snapshot_options: Iterable[Mapping[str, Any]],
    dialogue: str | None = None,
    social_context: Mapping[str, Any] | None = None,
    valid_until_minute: int | None = None,
) -> dict[str, Any]:
    """Perform snapshot membership and latest-world validation in one result."""

    snapshot = validate_action_selection(
        action, target, snapshot_options, dialogue=dialogue
    )
    current_options = build_action_options(
        session, npc, clock, decision, social_context=social_context
    )
    current = validate_action_selection(action, target, current_options, dialogue=dialogue)
    expired = valid_until_minute is not None and clock.total_minutes > valid_until_minute
    if expired:
        reason_code = "decision_expired"
    elif not snapshot["legal"]:
        reason_code = f"snapshot_{snapshot['reason_code']}"
    elif not current["legal"]:
        reason_code = f"latest_{current['reason_code']}"
    else:
        reason_code = "ok"
    return {
        "legal": bool(not expired and snapshot["legal"] and current["legal"]),
        "reason_code": reason_code,
        "expired": expired,
        "snapshot": snapshot,
        "latest": current,
        "params": current["params"] if current["legal"] else None,
    }


def create_waiting_turn(
    session: Session,
    *,
    decision_id: int,
    npc_id: int,
    created_minute: int,
    valid_until_minute: int,
    response_deadline_at: datetime,
    options: Iterable[Mapping[str, Any]],
    utility_action: str,
    utility_target: str | None,
    utility_reason: Mapping[str, Any],
    job_id: int | None = None,
    fallback_reason_code: str | None = None,
) -> AgentTakeoverTurn:
    if npc_id not in SUPPORTED_NPC_IDS:
        raise ValueError("V1.3 takeover supports only the five built-in NPCs")
    turn = AgentTakeoverTurn(
        decision_id=decision_id,
        job_id=job_id,
        npc_id=npc_id,
        state="waiting",
        worker_state="pending" if job_id is not None else "not_queued",
        created_minute=created_minute,
        valid_until_minute=valid_until_minute,
        response_deadline_at=_as_utc(response_deadline_at),
        options_json=json.dumps(list(options), ensure_ascii=False, separators=(",", ":")),
        utility_action=utility_action,
        utility_target=utility_target,
        utility_reason_json=json.dumps(utility_reason, ensure_ascii=False, separators=(",", ":")),
        fallback_reason_code=fallback_reason_code,
    )
    session.add(turn)
    session.flush([turn])
    return turn


def mark_turn_ready(
    turn: AgentTakeoverTurn,
    *,
    agent_decision: Mapping[str, Any],
    snapshot_validation: Mapping[str, Any],
) -> None:
    if turn.state != "waiting":
        raise ValueError(f"turn {turn.id} is not waiting")
    turn.state = "ready"
    turn.worker_state = "completed"
    turn.agent_decision_json = json.dumps(
        agent_decision, ensure_ascii=False, separators=(",", ":")
    )
    turn.agent_action = str(agent_decision.get("action") or "") or None
    target = agent_decision.get("target")
    turn.agent_target = str(target) if target is not None else None
    turn.snapshot_validation_json = json.dumps(
        snapshot_validation, ensure_ascii=False, separators=(",", ":")
    )
    turn.lease_token = None
    turn.lease_expires_at = None
    turn.last_error_code = None


def mark_turn_worker_failed(turn: AgentTakeoverTurn, reason_code: str) -> None:
    if turn.state not in {"waiting", "ready"}:
        return
    turn.worker_state = "failed"
    turn.last_error_code = reason_code
    turn.lease_token = None
    turn.lease_expires_at = None


def mark_turn_executing(
    turn: AgentTakeoverTurn,
    *,
    source: str,
    action: str,
    target: str | None,
    params: Mapping[str, Any],
    started_minute: int,
    end_minute: int,
    execution_validation: Mapping[str, Any] | None = None,
    fallback_reason_code: str | None = None,
) -> None:
    if turn.state not in {"waiting", "ready"}:
        raise ValueError(f"turn {turn.id} cannot start from {turn.state}")
    if source not in {"agent", "utility_fallback"}:
        raise ValueError("source must be agent or utility_fallback")
    turn.state = "agent_executing" if source == "agent" else "fallback_executing"
    turn.final_source = source
    turn.final_action = action
    turn.final_target = target
    turn.final_params_json = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
    turn.execution_validation_json = (
        json.dumps(execution_validation, ensure_ascii=False, separators=(",", ":"))
        if execution_validation is not None else None
    )
    turn.fallback_reason_code = fallback_reason_code
    turn.action_started_minute = started_minute
    turn.action_end_minute = end_minute
    turn.lease_token = None
    turn.lease_expires_at = None


def mark_turn_completed(
    turn: AgentTakeoverTurn,
    completed_minute: int,
    *,
    completion: Mapping[str, Any] | None = None,
) -> None:
    if turn.state not in EXECUTING_STATES:
        raise ValueError(f"turn {turn.id} is not executing")
    turn.state = "completed"
    turn.action_completed_minute = completed_minute
    turn.completion_json = json.dumps(
        completion or {"status": "completed"},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def deadline_expired(turn: AgentTakeoverTurn, now: datetime, world_minute: int) -> bool:
    return (
        world_minute > turn.valid_until_minute
        or _as_utc(now) >= _as_utc(turn.response_deadline_at)
    )


def claim_takeover_turn(
    session: Session,
    *,
    lease_token: str,
    now: datetime,
    lease_expires_at: datetime,
    world_minute: int,
    eligible_npc_ids: Iterable[int] | None = None,
) -> AgentTakeoverTurn | None:
    """Conditionally claim one pending turn; caller commits before network I/O."""

    query = select(AgentTakeoverTurn.id).where(
        AgentTakeoverTurn.state == "waiting",
        AgentTakeoverTurn.worker_state == "pending",
        AgentTakeoverTurn.valid_until_minute >= world_minute,
        AgentTakeoverTurn.response_deadline_at > _as_utc(now),
    )
    if eligible_npc_ids is not None:
        allowed = sorted(set(eligible_npc_ids) & SUPPORTED_NPC_IDS)
        if not allowed:
            return None
        query = query.where(AgentTakeoverTurn.npc_id.in_(allowed))
    candidate_id = session.scalar(
        query.order_by(
            AgentTakeoverTurn.created_minute,
            AgentTakeoverTurn.id,
        ).limit(1)
    )
    if candidate_id is None:
        return None
    result = session.execute(
        update(AgentTakeoverTurn)
        .where(
            AgentTakeoverTurn.id == candidate_id,
            AgentTakeoverTurn.state == "waiting",
            AgentTakeoverTurn.worker_state == "pending",
        )
        .values(
            worker_state="processing",
            attempts=AgentTakeoverTurn.attempts + 1,
            lease_token=lease_token,
            lease_expires_at=_as_utc(lease_expires_at),
            last_error_code=None,
        )
    )
    if result.rowcount != 1:
        return None
    turn = session.get(AgentTakeoverTurn, candidate_id)
    if turn is None:
        return None
    session.refresh(turn)
    if turn.job_id is not None:
        job = session.get(AgentDecisionJob, turn.job_id)
        if job is not None and job.status == "pending":
            job.status = "processing"
            job.attempts += 1
            job.started_at = _as_utc(now)
    return turn


def recover_takeover_leases(
    session: Session,
    *,
    now: datetime,
    world_minute: int,
) -> int:
    """Recover only interrupted waiting work; executing actions are untouched."""

    recovered = 0
    rows = list(
        session.scalars(
            select(AgentTakeoverTurn).where(
                AgentTakeoverTurn.state == "waiting",
                AgentTakeoverTurn.worker_state == "processing",
            ).order_by(AgentTakeoverTurn.id)
        )
    )
    for turn in rows:
        lease_expired = turn.lease_expires_at is None or _as_utc(turn.lease_expires_at) <= _as_utc(now)
        if not lease_expired:
            continue
        expired = deadline_expired(turn, now, world_minute)
        turn.worker_state = "failed" if expired else "pending"
        turn.last_error_code = "restart_expired" if expired else "recovered_after_restart"
        turn.lease_token = None
        turn.lease_expires_at = None
        if turn.job_id is not None:
            job = session.get(AgentDecisionJob, turn.job_id)
            if job is not None and job.status == "processing":
                job.status = "failed" if expired else "pending"
                job.last_error_code = turn.last_error_code
                job.started_at = None
        recovered += 1
    return recovered


def _loads(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def takeover_turn_snapshot(session: Session, turn: AgentTakeoverTurn) -> dict[str, Any]:
    artifact = None
    if turn.job_id is not None:
        artifact = session.scalar(
            select(AgentDecisionArtifact).where(AgentDecisionArtifact.job_id == turn.job_id)
        )
    agent = _loads(turn.agent_decision_json, None)
    if agent is None and artifact is not None:
        agent = _loads(artifact.decision_json, None)
    return {
        "id": turn.id,
        "npc_id": turn.npc_id,
        "state": turn.state,
        "worker_state": turn.worker_state,
        "job": {
            "id": turn.job_id,
            "decision_id": turn.decision_id,
            "attempts": turn.attempts,
            "leased": turn.lease_token is not None,
            "lease_expires_at": turn.lease_expires_at.isoformat()
            if turn.lease_expires_at else None,
            "error_code": turn.last_error_code,
        },
        "timing": {
            "created_minute": turn.created_minute,
            "valid_until_minute": turn.valid_until_minute,
            "response_deadline_at": turn.response_deadline_at.isoformat(),
            "action_started_minute": turn.action_started_minute,
            "action_end_minute": turn.action_end_minute,
            "action_completed_minute": turn.action_completed_minute,
        },
        "utility": {
            "action": turn.utility_action,
            "target": turn.utility_target,
            "reason": _loads(turn.utility_reason_json, {}),
        },
        "agent": agent,
        "validation": {
            "snapshot": _loads(turn.snapshot_validation_json, None),
            "execution": _loads(turn.execution_validation_json, None),
        },
        "final": None if turn.final_source is None else {
            "source": turn.final_source,
            "action": turn.final_action,
            "target": turn.final_target,
            "params": _loads(turn.final_params_json, {}),
            "fallback_reason_code": turn.fallback_reason_code,
            "completion": _loads(turn.completion_json, None),
        },
        "provider": None if artifact is None else {
            "provider": artifact.provider,
            "model": artifact.model,
        },
    }


def latest_takeover_snapshot(session: Session, npc_id: int) -> dict[str, Any] | None:
    turn = session.scalar(
        select(AgentTakeoverTurn).where(
            AgentTakeoverTurn.npc_id == npc_id
        ).order_by(AgentTakeoverTurn.id.desc()).limit(1)
    )
    return takeover_turn_snapshot(session, turn) if turn is not None else None


def takeover_audit_snapshots(
    session: Session, npc_id: int, limit: int = 50
) -> list[dict[str, Any]]:
    rows = list(
        session.scalars(
            select(AgentTakeoverTurn).where(
                AgentTakeoverTurn.npc_id == npc_id
            ).order_by(AgentTakeoverTurn.id.desc()).limit(min(max(limit, 1), 200))
        )
    )
    return [takeover_turn_snapshot(session, turn) for turn in rows]


def active_takeover_turn(session: Session, npc_id: int = TARGET_NPC_ID) -> AgentTakeoverTurn | None:
    return session.scalar(
        select(AgentTakeoverTurn).where(
            AgentTakeoverTurn.npc_id == npc_id,
            AgentTakeoverTurn.state.in_(ACTIVE_STATES),
        ).order_by(AgentTakeoverTurn.id.desc()).limit(1)
    )
