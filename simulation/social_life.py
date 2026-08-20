from __future__ import annotations

import json
from collections import defaultdict, deque
from typing import Any, Iterable

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from database.models import (
    CohousingHousehold,
    EconomicTransaction,
    Event,
    FriendCircle,
    Housing,
    JointActivity,
    NPC,
    Relationship,
    SharedExpense,
    SocialAudit,
    SocialBond,
    SocialCommitment,
    SocialInvitation,
    SocialProfile,
)
from simulation.clock import ClockSnapshot
from simulation.economy import add_transaction
from simulation.events import add_event
from simulation.memory import add_memory
from simulation.relationships import update_relationship


DAY_MINUTES = 1440
WEEK_MINUTES = 7 * DAY_MINUTES
INVITATION_COOLDOWN = DAY_MINUTES
COMMITMENT_WINDOW = 180
MAX_CIRCLE_SIZE = 4
MAX_ACTIVE_HOUSEHOLDS = 1
HOUSEHOLD_SHARED_COST = 12.0
STAGE_ORDER = ("hostile", "strained", "distant", "acquaintance", "friend", "close_friend", "trusted")
STAGE_LABELS = {
    "hostile": "敌对", "strained": "紧张", "distant": "疏远", "acquaintance": "熟人",
    "friend": "朋友", "close_friend": "亲密朋友", "trusted": "信赖伙伴",
}


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return round(max(low, min(high, value)), 2)


def _pair(first_id: int, second_id: int) -> tuple[int, int]:
    return (first_id, second_id) if first_id < second_id else (second_id, first_id)


def _relationship(session: Session, source_id: int, target_id: int) -> Relationship | None:
    return session.scalar(
        select(Relationship).where(
            Relationship.from_npc_id == source_id,
            Relationship.to_npc_id == target_id,
        )
    )


def _bond(session: Session, first_id: int, second_id: int) -> SocialBond | None:
    low, high = _pair(first_id, second_id)
    return session.scalar(
        select(SocialBond).where(SocialBond.npc_low_id == low, SocialBond.npc_high_id == high)
    )


def _scores(session: Session, bond: SocialBond) -> tuple[Relationship, Relationship] | None:
    low_to_high = _relationship(session, bond.npc_low_id, bond.npc_high_id)
    high_to_low = _relationship(session, bond.npc_high_id, bond.npc_low_id)
    if low_to_high is None or high_to_low is None:
        return None
    return low_to_high, high_to_low


def _stage(low_to_high: int, high_to_low: int, trust: float) -> str:
    mutual = (low_to_high + high_to_low) / 2
    asymmetry = abs(low_to_high - high_to_low)
    if min(low_to_high, high_to_low) <= -25:
        return "hostile"
    if mutual < 0 or asymmetry >= 40:
        return "strained"
    if mutual < 15:
        return "distant"
    if mutual < 30:
        return "acquaintance"
    if mutual < 55:
        return "friend"
    if mutual < 75 or trust < 75:
        return "close_friend"
    return "trusted"


def _refresh_bond(session: Session, bond: SocialBond, now: int) -> bool:
    rows = _scores(session, bond)
    if rows is None:
        return False
    low_to_high, high_to_low = rows
    mutual = (low_to_high.score + high_to_low.score) / 2
    asymmetry = abs(low_to_high.score - high_to_low.score)
    evidence = min(15.0, bond.positive_interactions * 1.5)
    risk = min(30.0, bond.negative_interactions * 3.0 + bond.decay_count)
    repair = min(12.0, bond.repair_count * 2.0)
    calculated_trust = _clamp(50 + mutual * 0.45 - asymmetry * 0.20 + evidence - risk + repair)
    # Trust is a derived, explainable indicator. Smooth it to avoid a single interaction
    # causing an implausibly large discontinuity while remaining fully deterministic.
    bond.trust = _clamp(bond.trust * 0.35 + calculated_trust * 0.65)
    bond.stage = _stage(low_to_high.score, high_to_low.score, bond.trust)
    bond.reasons_json = json.dumps([
        f"双向关系 {low_to_high.score}/{high_to_low.score}，均值 {mutual:.1f}",
        f"方向差 {asymmetry}，互动 {bond.interaction_count} 次",
        f"积极/消极 {bond.positive_interactions}/{bond.negative_interactions}，衰减/修复 {bond.decay_count}/{bond.repair_count}",
    ], ensure_ascii=False)
    bond.updated_minute = now
    return True


def ensure_social_life_data(
    session: Session,
    npcs: Iterable[NPC],
    current_minute: int,
) -> dict[str, int]:
    """Create only current V0.8 derived state; never invent historical activities."""
    people = list(npcs)
    created = {"bonds": 0, "profiles": 0}
    for index, first in enumerate(people):
        for second in people[index + 1:]:
            if _relationship(session, first.id, second.id) is None or _relationship(session, second.id, first.id) is None:
                continue
            bond = _bond(session, first.id, second.id)
            if bond is None:
                bond = SocialBond(
                    npc_low_id=first.id,
                    npc_high_id=second.id,
                    stage="distant",
                    trust=50.0,
                    last_interaction_minute=current_minute,
                    last_decay_minute=current_minute,
                    updated_minute=current_minute,
                )
                session.add(bond)
                session.flush()
                _refresh_bond(session, bond, current_minute)
                created["bonds"] += 1
    for npc in people:
        profile = session.scalar(select(SocialProfile).where(SocialProfile.npc_id == npc.id))
        if profile is None:
            session.add(SocialProfile(npc_id=npc.id, belonging=20.0, trust_index=50.0, updated_minute=current_minute))
            created["profiles"] += 1
    session.flush()
    _refresh_profiles(session, people, current_minute)
    return created


def _audit(
    session: Session,
    bond: SocialBond,
    now: int,
    kind: str,
    trust_before: float,
    stage_before: str,
    delta_low_to_high: int,
    delta_high_to_low: int,
    reasons: list[str],
) -> None:
    session.add(SocialAudit(
        npc_low_id=bond.npc_low_id,
        npc_high_id=bond.npc_high_id,
        world_minute=now,
        kind=kind,
        delta_low_to_high=delta_low_to_high,
        delta_high_to_low=delta_high_to_low,
        trust_before=trust_before,
        trust_after=bond.trust,
        stage_before=stage_before,
        stage_after=bond.stage,
        reasons_json=json.dumps(reasons, ensure_ascii=False),
    ))


def _active_commitment(session: Session, low: int, high: int) -> SocialCommitment | None:
    return session.scalar(
        select(SocialCommitment).where(
            SocialCommitment.npc_low_id == low,
            SocialCommitment.npc_high_id == high,
            SocialCommitment.status == "planned",
        ).order_by(SocialCommitment.id.desc())
    )


def _maybe_invite(
    session: Session,
    actor_id: int,
    target_id: int,
    location: str,
    now: int,
    bond: SocialBond,
) -> SocialInvitation | None:
    if STAGE_ORDER.index(bond.stage) < STAGE_ORDER.index("acquaintance"):
        return None
    low, high = _pair(actor_id, target_id)
    if _active_commitment(session, low, high) is not None:
        return None
    last = session.scalar(
        select(SocialInvitation).where(
            or_(
                (SocialInvitation.inviter_id == actor_id) & (SocialInvitation.invitee_id == target_id),
                (SocialInvitation.inviter_id == target_id) & (SocialInvitation.invitee_id == actor_id),
            )
        ).order_by(SocialInvitation.created_minute.desc()).limit(1)
    )
    if last is not None and now - last.created_minute < INVITATION_COOLDOWN:
        return None
    rows = _scores(session, bond)
    if rows is None:
        return None
    accepted = min(rows[0].score, rows[1].score) >= 10 and bond.trust >= 45
    invitation = SocialInvitation(
        inviter_id=actor_id,
        invitee_id=target_id,
        location=location,
        created_minute=now,
        scheduled_minute=now + 60,
        status="accepted" if accepted else "declined",
        reason=(
            f"双向关系达到{STAGE_LABELS[bond.stage]}且信任 {bond.trust:.1f}，接受共同活动"
            if accepted else f"双向关系或信任尚不足，信任 {bond.trust:.1f}"
        ),
    )
    session.add(invitation)
    session.flush()
    if accepted:
        session.add(SocialCommitment(
            invitation_id=invitation.id,
            npc_low_id=low,
            npc_high_id=high,
            activity_key="shared_time",
            location=location,
            scheduled_minute=invitation.scheduled_minute,
            expires_minute=invitation.scheduled_minute + COMMITMENT_WINDOW,
            status="planned",
        ))
        add_event(
            session, ClockSnapshot(now), "SOCIAL_COMMITMENT",
            "一项共同活动邀请已被接受并形成承诺",
            npc_id=actor_id, target_npc_id=target_id, location=location,
            metadata={"invitation_id": invitation.id, "scheduled_minute": invitation.scheduled_minute},
        )
    return invitation


def _matching_commitment(
    session: Session,
    first_id: int,
    second_id: int,
    location: str,
    now: int,
) -> SocialCommitment | None:
    low, high = _pair(first_id, second_id)
    return session.scalar(
        select(SocialCommitment).where(
            SocialCommitment.npc_low_id == low,
            SocialCommitment.npc_high_id == high,
            SocialCommitment.location == location,
            SocialCommitment.status == "planned",
            SocialCommitment.scheduled_minute - 30 <= now,
            SocialCommitment.expires_minute >= now,
        ).order_by(SocialCommitment.scheduled_minute, SocialCommitment.id)
    )


def _circle_for_pair(session: Session, first_id: int, second_id: int) -> FriendCircle | None:
    for circle in session.scalars(select(FriendCircle).where(FriendCircle.active.is_(True)).order_by(FriendCircle.id)):
        members = json.loads(circle.member_ids_json)
        if first_id in members and second_id in members:
            return circle
    return None


def _complete_commitment(
    session: Session,
    commitment: SocialCommitment,
    actor: NPC,
    target: NPC,
    bond: SocialBond,
    now: int,
) -> None:
    commitment.status = "completed"
    commitment.completed_minute = now
    cost = 8.0 if commitment.location == "Cafe" else 0.0
    paid: dict[int, float] = {}
    for person in (actor, target):
        share = min(person.money, cost / 2)
        person.money = round(person.money - share, 2)
        paid[person.id] = round(share, 2)
        if share:
            add_transaction(session, person, ClockSnapshot(now), "joint_activity", -share, "共同活动分摊")
        person.mood = _clamp(person.mood + 3)
        person.social_need = _clamp(person.social_need - 10)
    low_to_high, high_to_low = _scores(session, bond) or (None, None)
    delta_low = delta_high = 0
    if low_to_high is not None and high_to_low is not None:
        delta_low = update_relationship(low_to_high, 2)
        delta_high = update_relationship(high_to_low, 2)
    trust_before, stage_before = bond.trust, bond.stage
    bond.positive_interactions += 1
    bond.interaction_count += 1
    bond.last_interaction_minute = now
    _refresh_bond(session, bond, now)
    circle = _circle_for_pair(session, actor.id, target.id)
    activity = JointActivity(
        commitment_id=commitment.id,
        circle_id=circle.id if circle else None,
        activity_key=commitment.activity_key,
        location=commitment.location,
        start_minute=now,
        end_minute=now + 20,
        participant_ids_json=json.dumps(sorted((actor.id, target.id))),
        shared_cost=round(sum(paid.values()), 2),
        outcome_json=json.dumps({"paid": paid, "relationship_change": 2, "trust": bond.trust}, ensure_ascii=False),
    )
    session.add(activity)
    _audit(session, bond, now, "joint_activity", trust_before, stage_before, delta_low, delta_high,
           ["双方履行了承诺", f"共同地点 {commitment.location}", f"实际共同支出 ${sum(paid.values()):.2f}"])
    add_event(
        session, ClockSnapshot(now), "JOINT_ACTIVITY",
        f"{actor.name} 与 {target.name} 履行了承诺并完成共同活动",
        npc_id=actor.id, target_npc_id=target.id, location=commitment.location,
        metadata={"commitment_id": commitment.id, "shared_cost": round(sum(paid.values()), 2)},
    )
    for person, other in ((actor, target), (target, actor)):
        add_memory(session, ClockSnapshot(now), person.id, f"我和 {other.name} 履行了承诺，一起度过了一段时间",
                   importance=6, emotion="positive", related_npc_id=other.id)


def record_social_interaction(session: Session, social_event: Event, now: int) -> bool:
    """Consume a committed legacy SOCIAL fact and add V0.8 facts in the same Engine tick."""
    if social_event.npc_id is None or social_event.target_npc_id is None or social_event.location is None:
        return False
    actor = session.get(NPC, social_event.npc_id)
    target = session.get(NPC, social_event.target_npc_id)
    bond = _bond(session, social_event.npc_id, social_event.target_npc_id)
    if actor is None or target is None or bond is None:
        return False
    rows = _scores(session, bond)
    if rows is None:
        return False
    relationship_event = session.scalar(
        select(Event).where(
            Event.id > social_event.id,
            Event.event_type == "RELATIONSHIP",
            Event.npc_id == actor.id,
            Event.target_npc_id == target.id,
        ).order_by(Event.id).limit(1)
    )
    actor_delta = int(json.loads(relationship_event.metadata_json).get("change", 0)) if relationship_event else 0
    low_to_high, high_to_low = rows
    reciprocal = high_to_low if actor.id == bond.npc_low_id else low_to_high
    reciprocal_delta = update_relationship(reciprocal, 1 if actor_delta > 0 else -1 if actor_delta < 0 else 0)
    delta_low = actor_delta if actor.id == bond.npc_low_id else reciprocal_delta
    delta_high = reciprocal_delta if actor.id == bond.npc_low_id else actor_delta
    trust_before, stage_before = bond.trust, bond.stage
    bond.interaction_count += 1
    bond.last_interaction_minute = now
    if actor_delta > 0:
        bond.positive_interactions += 1
    elif actor_delta < 0:
        bond.negative_interactions += 1
    repair_applied = 0
    if actor_delta > 0 and bond.decay_count > bond.repair_count:
        repair_applied = 1
        update_relationship(low_to_high, 1)
        update_relationship(high_to_low, 1)
        delta_low += 1
        delta_high += 1
        bond.repair_count += 1
    _refresh_bond(session, bond, now)
    reasons = ["由已完成的 Socialize 事实触发", "同时判断两条有向关系", f"互动结果 {actor_delta:+d}"]
    if repair_applied:
        reasons.append("积极互动修复了 1 点已衰减关系")
    _audit(session, bond, now, "repair" if repair_applied else "interaction", trust_before, stage_before,
           delta_low, delta_high, reasons)
    commitment = _matching_commitment(session, actor.id, target.id, social_event.location, now)
    if commitment is not None:
        _complete_commitment(session, commitment, actor, target, bond, now)
    elif actor_delta > 0:
        _maybe_invite(session, actor.id, target.id, social_event.location, now, bond)
    _refresh_friend_circles(session, now)
    _refresh_profiles(session, list(session.scalars(select(NPC).order_by(NPC.id))), now)
    return True


def _refresh_friend_circles(session: Session, now: int) -> None:
    adjacency: dict[int, set[int]] = defaultdict(set)
    for bond in session.scalars(select(SocialBond).order_by(SocialBond.npc_low_id, SocialBond.npc_high_id)):
        if STAGE_ORDER.index(bond.stage) >= STAGE_ORDER.index("friend"):
            adjacency[bond.npc_low_id].add(bond.npc_high_id)
            adjacency[bond.npc_high_id].add(bond.npc_low_id)
    desired: dict[str, list[int]] = {}
    visited: set[int] = set()
    for start in sorted(adjacency):
        if start in visited:
            continue
        queue = deque([start])
        component: list[int] = []
        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            component.append(current)
            queue.extend(sorted(adjacency[current] - visited))
        members = sorted(component)[:MAX_CIRCLE_SIZE]
        if len(members) >= 3:
            desired["circle-" + "-".join(map(str, members))] = members
    active_keys = set(desired)
    for circle in session.scalars(select(FriendCircle).where(FriendCircle.active.is_(True))):
        if circle.circle_key not in active_keys:
            circle.active = False
            circle.ended_minute = now
            circle.updated_minute = now
    for key, members in desired.items():
        circle = session.scalar(select(FriendCircle).where(FriendCircle.circle_key == key))
        reasons = ["成员之间至少由朋友阶段的双向关系连通", f"圈子人数限制为 {MAX_CIRCLE_SIZE}"]
        if circle is None:
            circle = FriendCircle(
                circle_key=key, name=f"朋友圈 {key.removeprefix('circle-')}",
                member_ids_json=json.dumps(members), active=True,
                created_minute=now, updated_minute=now,
                reasons_json=json.dumps(reasons, ensure_ascii=False),
            )
            session.add(circle)
        else:
            circle.member_ids_json = json.dumps(members)
            circle.active = True
            circle.ended_minute = None
            circle.updated_minute = now
            circle.reasons_json = json.dumps(reasons, ensure_ascii=False)


def _refresh_profiles(session: Session, npcs: list[NPC], now: int) -> None:
    week_start = now - WEEK_MINUTES
    circles = list(session.scalars(select(FriendCircle).where(FriendCircle.active.is_(True))))
    households = list(session.scalars(select(CohousingHousehold).where(CohousingHousehold.active.is_(True))))
    for npc in npcs:
        profile = session.scalar(select(SocialProfile).where(SocialProfile.npc_id == npc.id))
        if profile is None:
            continue
        bonds = list(session.scalars(select(SocialBond).where(
            or_(SocialBond.npc_low_id == npc.id, SocialBond.npc_high_id == npc.id)
        )))
        if not bonds:
            continue
        trust_index = sum(bond.trust for bond in bonds) / len(bonds)
        circle_size = max((len(json.loads(circle.member_ids_json)) for circle in circles
                           if npc.id in json.loads(circle.member_ids_json)), default=0)
        activities = session.scalar(
            select(func.count()).select_from(JointActivity).where(
                JointActivity.start_minute >= week_start,
                JointActivity.participant_ids_json.like(f"%{npc.id}%"),
            )
        ) or 0
        commitments = session.scalar(
            select(func.count()).select_from(SocialCommitment).where(
                or_(SocialCommitment.npc_low_id == npc.id, SocialCommitment.npc_high_id == npc.id),
                SocialCommitment.status == "planned",
            )
        ) or 0
        cohabiting = any(npc.id in json.loads(row.resident_ids_json) for row in households)
        belonging = _clamp(20 + circle_size * 9 + min(activities, 4) * 6 + min(commitments, 2) * 3 + (15 if cohabiting else 0))
        profile.trust_index = _clamp(trust_index)
        profile.belonging = belonging
        profile.reasons_json = json.dumps([
            f"活跃朋友圈规模 {circle_size}", f"近 7 天共同活动 {activities} 次",
            f"待履行承诺 {commitments} 项", "处于合住家庭" if cohabiting else "未合住",
            f"全部双向关系平均信任 {trust_index:.1f}",
        ], ensure_ascii=False)
        profile.updated_minute = now


def _form_household(session: Session, now: int) -> bool:
    active_count = session.scalar(
        select(func.count()).select_from(CohousingHousehold).where(CohousingHousehold.active.is_(True))
    ) or 0
    if active_count >= MAX_ACTIVE_HOUSEHOLDS:
        return False
    for bond in session.scalars(select(SocialBond).order_by(SocialBond.trust.desc(), SocialBond.id)):
        if STAGE_ORDER.index(bond.stage) < STAGE_ORDER.index("close_friend") or bond.trust < 65:
            continue
        activities = session.scalar(
            select(func.count()).select_from(JointActivity).where(
                JointActivity.participant_ids_json == json.dumps([bond.npc_low_id, bond.npc_high_id])
            )
        ) or 0
        if activities < 2:
            continue
        existing = list(session.scalars(select(CohousingHousehold).where(CohousingHousehold.active.is_(True))))
        occupied = {member for row in existing for member in json.loads(row.resident_ids_json)}
        if bond.npc_low_id in occupied or bond.npc_high_id in occupied:
            continue
        housings = {
            housing.npc_id: housing for housing in session.scalars(
                select(Housing).where(Housing.npc_id.in_((bond.npc_low_id, bond.npc_high_id)))
            )
        }
        if len(housings) != 2 or any(row.arrears > 0 for row in housings.values()):
            continue
        host = min(housings.values(), key=lambda row: (-row.comfort, row.npc_id))
        session.add(CohousingHousehold(
            host_housing_id=host.id,
            resident_ids_json=json.dumps([bond.npc_low_id, bond.npc_high_id]),
            started_minute=now,
            active=True,
            weekly_shared_cost=HOUSEHOLD_SHARED_COST,
            next_expense_minute=now + WEEK_MINUTES,
            trust_at_start=bond.trust,
            reasons_json=json.dumps([
                f"关系阶段 {STAGE_LABELS[bond.stage]}", f"信任 {bond.trust:.1f}",
                f"共同活动 {activities} 次", "双方住房均无欠费", "每户最多 2 人且全世界最多 1 个活跃合住家庭",
            ], ensure_ascii=False),
        ))
        add_event(session, ClockSnapshot(now), "COHOUSING", "两位居民建立了有限合住安排",
                  npc_id=bond.npc_low_id, target_npc_id=bond.npc_high_id, location="Home",
                  metadata={"host_housing_id": host.id, "weekly_shared_cost": HOUSEHOLD_SHARED_COST})
        return True
    return False


def _process_shared_expenses(session: Session, now: int) -> int:
    processed = 0
    for household in session.scalars(select(CohousingHousehold).where(CohousingHousehold.active.is_(True))):
        while household.next_expense_minute <= now:
            members = json.loads(household.resident_ids_json)
            share_due = round(household.weekly_shared_cost / len(members), 2)
            split: dict[int, dict[str, float]] = {}
            for npc_id in members:
                npc = session.get(NPC, npc_id)
                if npc is None:
                    continue
                paid = round(min(npc.money, share_due), 2)
                npc.money = round(npc.money - paid, 2)
                split[npc_id] = {"due": share_due, "paid": paid}
                if paid:
                    add_transaction(session, npc, ClockSnapshot(household.next_expense_minute),
                                    "shared_expense", -paid, "合住家庭共同生活支出")
            session.add(SharedExpense(
                household_id=household.id, world_minute=household.next_expense_minute,
                kind="household_utilities", amount=round(sum(item["paid"] for item in split.values()), 2),
                split_json=json.dumps(split, ensure_ascii=False), description="每周固定共同生活支出，按居民人数均分",
            ))
            add_event(session, ClockSnapshot(household.next_expense_minute), "SHARED_EXPENSE",
                      "合住家庭结算了每周共同生活支出", location="Home",
                      metadata={"household_id": household.id, "split": split})
            household.next_expense_minute += WEEK_MINUTES
            processed += 1
    return processed


def process_social_life_cycles(session: Session, npcs: list[NPC], clock: ClockSnapshot) -> dict[str, int]:
    now = clock.total_minutes
    result = {"decays": 0, "expired_commitments": 0, "households": 0, "shared_expenses": 0}
    for commitment in session.scalars(select(SocialCommitment).where(
        SocialCommitment.status == "planned", SocialCommitment.expires_minute < now
    )):
        commitment.status = "expired"
        result["expired_commitments"] += 1
    for bond in session.scalars(select(SocialBond).order_by(SocialBond.id)):
        if now - bond.last_interaction_minute < 3 * DAY_MINUTES or now - bond.last_decay_minute < DAY_MINUTES:
            continue
        rows = _scores(session, bond)
        if rows is None:
            continue
        low_to_high, high_to_low = rows
        trust_before, stage_before = bond.trust, bond.stage
        delta_low = update_relationship(low_to_high, -1 if low_to_high.score > 0 else 1 if low_to_high.score < 0 else 0)
        delta_high = update_relationship(high_to_low, -1 if high_to_low.score > 0 else 1 if high_to_low.score < 0 else 0)
        bond.decay_count += 1
        bond.last_decay_minute = now
        _refresh_bond(session, bond, now)
        _audit(session, bond, now, "decay", trust_before, stage_before, delta_low, delta_high,
               ["连续 3 天没有互动", "双向关系各向中性值衰减 1 点", "每个自然日至多执行一次"])
        result["decays"] += 1
    _refresh_friend_circles(session, now)
    if _form_household(session, now):
        result["households"] += 1
    result["shared_expenses"] = _process_shared_expenses(session, now)
    _refresh_profiles(session, npcs, now)
    return result


def social_life_context(session: Session, npc: NPC, now: int) -> dict[str, Any] | None:
    profile = session.scalar(select(SocialProfile).where(SocialProfile.npc_id == npc.id))
    expected = max(0, (session.scalar(select(func.count()).select_from(NPC)) or 0) - 1)
    bonds = list(session.scalars(select(SocialBond).where(
        or_(SocialBond.npc_low_id == npc.id, SocialBond.npc_high_id == npc.id)
    )))
    if profile is None or len(bonds) != expected:
        return None
    commitment = session.scalar(
        select(SocialCommitment).where(
            or_(SocialCommitment.npc_low_id == npc.id, SocialCommitment.npc_high_id == npc.id),
            SocialCommitment.status == "planned",
            SocialCommitment.scheduled_minute - 60 <= now,
            SocialCommitment.expires_minute >= now,
        ).order_by(SocialCommitment.scheduled_minute, SocialCommitment.id)
    )
    repair_needed = any(bond.stage in {"hostile", "strained"} or bond.decay_count > bond.repair_count for bond in bonds)
    partner_id = None
    if commitment is not None:
        partner_id = commitment.npc_high_id if commitment.npc_low_id == npc.id else commitment.npc_low_id
    return {
        "enabled": True,
        "belonging": profile.belonging,
        "trust_index": profile.trust_index,
        "repair_needed": repair_needed,
        "commitment_due": commitment is not None,
        "commitment_location": commitment.location if commitment else None,
        "commitment_partner_id": partner_id,
    }


def bond_snapshots(session: Session) -> list[dict[str, Any]]:
    names = {npc.id: npc.name for npc in session.scalars(select(NPC))}
    result: list[dict[str, Any]] = []
    for bond in session.scalars(select(SocialBond).order_by(SocialBond.npc_low_id, SocialBond.npc_high_id)):
        rows = _scores(session, bond)
        if rows is None:
            continue
        result.append({
            "id": bond.id, "npc_low_id": bond.npc_low_id, "npc_low_name": names.get(bond.npc_low_id),
            "npc_high_id": bond.npc_high_id, "npc_high_name": names.get(bond.npc_high_id),
            "low_to_high": rows[0].score, "high_to_low": rows[1].score,
            "mutual_score": round((rows[0].score + rows[1].score) / 2, 1),
            "asymmetry": abs(rows[0].score - rows[1].score),
            "stage": bond.stage, "stage_label": STAGE_LABELS[bond.stage], "trust": round(bond.trust, 1),
            "interaction_count": bond.interaction_count, "decay_count": bond.decay_count,
            "repair_count": bond.repair_count, "last_interaction_minute": bond.last_interaction_minute,
            "reasons": json.loads(bond.reasons_json),
        })
    return result


def circle_snapshots(session: Session) -> list[dict[str, Any]]:
    names = {npc.id: npc.name for npc in session.scalars(select(NPC))}
    return [{
        "id": row.id, "circle_key": row.circle_key, "name": row.name, "active": row.active,
        "members": [{"npc_id": item, "npc_name": names.get(item)} for item in json.loads(row.member_ids_json)],
        "created_minute": row.created_minute, "updated_minute": row.updated_minute,
        "ended_minute": row.ended_minute, "reasons": json.loads(row.reasons_json),
    } for row in session.scalars(select(FriendCircle).order_by(FriendCircle.active.desc(), FriendCircle.id))]


def commitment_snapshots(session: Session, npc_id: int | None = None) -> list[dict[str, Any]]:
    query = select(SocialCommitment).order_by(SocialCommitment.id.desc())
    if npc_id is not None:
        query = query.where(or_(SocialCommitment.npc_low_id == npc_id, SocialCommitment.npc_high_id == npc_id))
    names = {npc.id: npc.name for npc in session.scalars(select(NPC))}
    return [{
        "id": row.id, "invitation_id": row.invitation_id, "npc_low_id": row.npc_low_id,
        "npc_low_name": names.get(row.npc_low_id), "npc_high_id": row.npc_high_id,
        "npc_high_name": names.get(row.npc_high_id), "activity_key": row.activity_key,
        "location": row.location, "scheduled_minute": row.scheduled_minute,
        "expires_minute": row.expires_minute, "status": row.status,
        "completed_minute": row.completed_minute,
    } for row in session.scalars(query)]


def household_snapshots(session: Session) -> list[dict[str, Any]]:
    names = {npc.id: npc.name for npc in session.scalars(select(NPC))}
    result = []
    for row in session.scalars(select(CohousingHousehold).order_by(CohousingHousehold.id.desc())):
        members = json.loads(row.resident_ids_json)
        expenses = list(session.scalars(select(SharedExpense).where(
            SharedExpense.household_id == row.id
        ).order_by(SharedExpense.id.desc()).limit(10)))
        result.append({
            "id": row.id, "active": row.active, "host_housing_id": row.host_housing_id,
            "residents": [{"npc_id": item, "npc_name": names.get(item)} for item in members],
            "started_minute": row.started_minute, "weekly_shared_cost": row.weekly_shared_cost,
            "next_expense_minute": row.next_expense_minute, "trust_at_start": row.trust_at_start,
            "reasons": json.loads(row.reasons_json),
            "expenses": [{"id": item.id, "world_minute": item.world_minute, "kind": item.kind,
                          "amount": item.amount, "split": json.loads(item.split_json),
                          "description": item.description} for item in expenses],
        })
    return result


def npc_social_snapshot(session: Session, npc: NPC) -> dict[str, Any] | None:
    profile = session.scalar(select(SocialProfile).where(SocialProfile.npc_id == npc.id))
    if profile is None:
        return None
    bonds = [item for item in bond_snapshots(session) if npc.id in (item["npc_low_id"], item["npc_high_id"])]
    circles = [item for item in circle_snapshots(session)
               if any(member["npc_id"] == npc.id for member in item["members"])]
    households = [item for item in household_snapshots(session)
                  if any(member["npc_id"] == npc.id for member in item["residents"])]
    activities = list(session.scalars(select(JointActivity).where(
        JointActivity.participant_ids_json.like(f"%{npc.id}%")
    ).order_by(JointActivity.id.desc()).limit(10)))
    return {
        "npc_id": npc.id, "npc_name": npc.name, "belonging": round(profile.belonging, 1),
        "trust_index": round(profile.trust_index, 1), "indicator_reasons": json.loads(profile.reasons_json),
        "bonds": bonds, "circles": circles, "commitments": commitment_snapshots(session, npc.id),
        "households": households,
        "recent_activities": [{
            "id": row.id, "commitment_id": row.commitment_id, "activity_key": row.activity_key,
            "location": row.location, "start_minute": row.start_minute, "end_minute": row.end_minute,
            "participant_ids": json.loads(row.participant_ids_json), "shared_cost": row.shared_cost,
            "outcome": json.loads(row.outcome_json),
        } for row in activities],
    }
