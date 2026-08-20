from __future__ import annotations

import json
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database.models import (
    CommunityInstitution,
    EconomicTransaction,
    EmploymentProfile,
    FacilityUsage,
    Housing,
    HousingUpgradeRecord,
    ItemDefinition,
    NPC,
    NPCSkill,
    RestockEvent,
    StoreListing,
    StoreStock,
    TrainingRecord,
    WorkAttendance,
    WorkSchedule,
)
from simulation.clock import ClockSnapshot
from simulation.economy import add_skill_experience, add_transaction, profession_definition
from simulation.events import add_event
from simulation.memory import add_memory
from simulation.npc import clamp, clamp_npc_state


DAY_MINUTES = 24 * 60
WEEK_MINUTES = 7 * DAY_MINUTES
WORK_BLOCK_MINUTES = 60
SHIFT_MINUTES = 120
TRAINING_FEE = 18.0
TRAINING_XP = 30.0
TRAINING_WEEKLY_LIMIT = 2

INSTITUTIONS: tuple[dict[str, Any], ...] = (
    {
        "id": 1, "institution_key": "community_store", "name": "社区生活商店",
        "institution_type": "store", "location": "Cafe",
        "weekday_open_minute": 7 * 60, "weekday_close_minute": 21 * 60,
        "weekend_open_minute": 9 * 60, "weekend_close_minute": 22 * 60,
        "service_key": None, "daily_capacity": None,
    },
    {
        "id": 2, "institution_key": "park_wellness", "name": "公园身心驿站",
        "institution_type": "facility", "location": "Park",
        "weekday_open_minute": 17 * 60, "weekday_close_minute": 21 * 60,
        "weekend_open_minute": 8 * 60, "weekend_close_minute": 20 * 60,
        "service_key": "wellness_session", "daily_capacity": 3,
    },
    {
        "id": 3, "institution_key": "career_center", "name": "社区职业培训中心",
        "institution_type": "training", "location": "Office",
        "weekday_open_minute": 18 * 60, "weekday_close_minute": 21 * 60,
        "weekend_open_minute": 10 * 60, "weekend_close_minute": 18 * 60,
        "service_key": "career_training", "daily_capacity": 5,
    },
    {
        "id": 4, "institution_key": "housing_desk", "name": "社区住房服务台",
        "institution_type": "housing", "location": "Home",
        "weekday_open_minute": 8 * 60, "weekday_close_minute": 20 * 60,
        "weekend_open_minute": 10 * 60, "weekend_close_minute": 18 * 60,
        "service_key": "housing_upgrade", "daily_capacity": None,
    },
)

STOCK_SPECS = {
    "prepared_meal": (8, 6),
    "coffee": (6, 4),
    "professional_guide": (3, 2),
    "home_decor": (3, 1),
}

HOUSING_UPGRADES = {
    "standard": {"tier": "improved", "cost": 160.0, "weekly_rent": 34.0, "comfort": 76.0},
    "improved": {"tier": "premium", "cost": 360.0, "weekly_rent": 48.0, "comfort": 90.0},
}


def _minute_of_day(total_minutes: int) -> int:
    return total_minutes % DAY_MINUTES


def _weekday_index(total_minutes: int) -> int:
    return (total_minutes // DAY_MINUTES) % 7


def _week_start(total_minutes: int) -> int:
    return (total_minutes // WEEK_MINUTES) * WEEK_MINUTES


def _is_weekend(total_minutes: int) -> bool:
    return _weekday_index(total_minutes) >= 5


def institution_is_open(institution: CommunityInstitution, total_minutes: int) -> bool:
    minute = _minute_of_day(total_minutes)
    if _is_weekend(total_minutes):
        return institution.weekend_open_minute <= minute < institution.weekend_close_minute
    return institution.weekday_open_minute <= minute < institution.weekday_close_minute


def _hours(institution: CommunityInstitution, total_minutes: int) -> tuple[int, int]:
    if _is_weekend(total_minutes):
        return institution.weekend_open_minute, institution.weekend_close_minute
    return institution.weekday_open_minute, institution.weekday_close_minute


def _format_minute(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def ensure_community_data(
    session: Session,
    npcs: Iterable[NPC],
    created_minute: int,
) -> dict[str, int]:
    """Idempotently create only V0.7 rows; never infer or backfill old history."""
    counts = {"institutions": 0, "schedules": 0, "stock": 0}
    for spec in INSTITUTIONS:
        if session.scalar(
            select(CommunityInstitution).where(
                CommunityInstitution.institution_key == spec["institution_key"]
            )
        ) is None:
            session.add(CommunityInstitution(**spec))
            counts["institutions"] += 1
    session.flush()

    for npc in npcs:
        schedule = session.scalar(select(WorkSchedule).where(WorkSchedule.npc_id == npc.id))
        if schedule is None:
            start = 8 * 60 + ((npc.id - 1) % 3) * 30
            session.add(
                WorkSchedule(
                    npc_id=npc.id,
                    workdays_json="[0, 1, 2, 3, 4]",
                    start_minute=start,
                    end_minute=start + 8 * 60,
                    grace_minutes=15,
                )
            )
            counts["schedules"] += 1

    next_restock = (created_minute // DAY_MINUTES) * DAY_MINUTES + 6 * 60
    if next_restock <= created_minute:
        next_restock += DAY_MINUTES
    listings = session.execute(
        select(StoreListing, ItemDefinition)
        .join(ItemDefinition, ItemDefinition.id == StoreListing.item_id)
        .order_by(StoreListing.id)
    ).all()
    for listing, item in listings:
        stock = session.scalar(select(StoreStock).where(StoreStock.listing_id == listing.id))
        if stock is None:
            capacity, amount = STOCK_SPECS.get(item.item_key, (4, 2))
            session.add(
                StoreStock(
                    listing_id=listing.id,
                    quantity=capacity,
                    capacity=capacity,
                    restock_amount=amount,
                    restock_interval_minutes=DAY_MINUTES,
                    last_restock_minute=created_minute,
                    next_restock_minute=next_restock,
                )
            )
            counts["stock"] += 1
    session.flush()
    return counts


def process_restocking(session: Session, clock: ClockSnapshot) -> int:
    processed = 0
    rows = list(
        session.scalars(
            select(StoreStock)
            .where(StoreStock.next_restock_minute <= clock.total_minutes)
            .order_by(StoreStock.id)
        )
    )
    for stock in rows:
        while stock.next_restock_minute <= clock.total_minutes:
            before = stock.quantity
            stock.quantity = min(stock.capacity, stock.quantity + stock.restock_amount)
            added = stock.quantity - before
            due_minute = stock.next_restock_minute
            stock.last_restock_minute = due_minute
            stock.next_restock_minute += stock.restock_interval_minutes
            session.add(
                RestockEvent(
                    stock_id=stock.id,
                    world_minute=due_minute,
                    quantity_before=before,
                    quantity_added=added,
                    quantity_after=stock.quantity,
                )
            )
            processed += 1
            if added:
                add_event(
                    session, ClockSnapshot(due_minute), "RESTOCK",
                    f"社区商店完成固定周期补货，库存增加 {added}",
                    location="Cafe",
                    metadata={"stock_id": stock.id, "quantity_added": added, "quantity_after": stock.quantity},
                )
    return processed


def stocked_listing_ids(session: Session) -> set[int]:
    return set(
        session.scalars(
            select(StoreStock.listing_id).where(StoreStock.quantity > 0).order_by(StoreStock.listing_id)
        )
    )


def consume_stock(session: Session, listing_id: int) -> bool:
    stock = session.scalar(select(StoreStock).where(StoreStock.listing_id == listing_id))
    if stock is None or stock.quantity <= 0:
        return False
    stock.quantity -= 1
    return True


def record_work_attendance(session: Session, npc: NPC, clock: ClockSnapshot) -> dict[str, Any] | None:
    schedule = session.scalar(select(WorkSchedule).where(WorkSchedule.npc_id == npc.id))
    if schedule is None:
        return None
    start_total = clock.total_minutes - WORK_BLOCK_MINUTES
    day = start_total // DAY_MINUTES + 1
    day_start = (day - 1) * DAY_MINUTES
    workdays = set(json.loads(schedule.workdays_json))
    if (day - 1) % 7 not in workdays:
        return None
    attendance = session.scalar(
        select(WorkAttendance).where(
            WorkAttendance.npc_id == npc.id,
            WorkAttendance.world_day == day,
        )
    )
    first = attendance is None
    if attendance is None:
        scheduled = day_start + schedule.start_minute
        minutes_late = max(0, start_total - scheduled)
        status = "late" if minutes_late > schedule.grace_minutes else "on_time"
        attendance = WorkAttendance(
            npc_id=npc.id,
            world_day=day,
            scheduled_start_minute=scheduled,
            first_arrival_minute=start_total,
            minutes_late=minutes_late,
            status=status,
            worked_minutes=0,
        )
        session.add(attendance)
        if status == "late":
            schedule.late_days += 1
            employment = session.scalar(
                select(EmploymentProfile).where(EmploymentProfile.npc_id == npc.id)
            )
            if employment is not None:
                employment.performance = round(clamp(employment.performance - min(2.0, minutes_late / 30)), 2)
        else:
            schedule.on_time_days += 1
    before = attendance.worked_minutes
    attendance.worked_minutes += WORK_BLOCK_MINUTES
    completed = before < SHIFT_MINUTES <= attendance.worked_minutes
    if completed:
        schedule.shifts_completed += 1
    if first:
        label = "准时到岗" if attendance.status == "on_time" else f"迟到 {attendance.minutes_late} 分钟"
        add_event(
            session, clock, "ATTENDANCE", f"{npc.name} {label}", npc_id=npc.id, location="Office",
            metadata={"status": attendance.status, "minutes_late": attendance.minutes_late},
        )
    if completed:
        add_event(
            session, clock, "SHIFT", f"{npc.name} 完成了当日排班", npc_id=npc.id, location="Office",
            metadata={"worked_minutes": attendance.worked_minutes},
        )
    return {
        "status": attendance.status,
        "minutes_late": attendance.minutes_late,
        "worked_minutes": attendance.worked_minutes,
        "shift_completed": completed,
    }


def _daily_usage_count(session: Session, institution_id: int, world_day: int) -> int:
    return session.scalar(
        select(func.count()).select_from(FacilityUsage).where(
            FacilityUsage.institution_id == institution_id,
            FacilityUsage.world_day == world_day,
        )
    ) or 0


def complete_facility_service(session: Session, npc: NPC, clock: ClockSnapshot) -> dict[str, Any] | None:
    institution = session.scalar(
        select(CommunityInstitution).where(CommunityInstitution.institution_key == "park_wellness")
    )
    action_start = clock.total_minutes - 60
    if institution is None or npc.current_location != institution.location or not institution_is_open(institution, action_start):
        return None
    day = action_start // DAY_MINUTES + 1
    existing = session.scalar(
        select(FacilityUsage).where(
            FacilityUsage.npc_id == npc.id,
            FacilityUsage.institution_id == institution.id,
            FacilityUsage.world_day == day,
        )
    )
    used = _daily_usage_count(session, institution.id, day)
    if existing is not None or (institution.daily_capacity is not None and used >= institution.daily_capacity):
        return None
    outcome = {"energy": 6, "mood": 10, "social_need": -5}
    npc.energy += outcome["energy"]
    npc.mood += outcome["mood"]
    npc.social_need += outcome["social_need"]
    clamp_npc_state(npc)
    session.add(
        FacilityUsage(
            npc_id=npc.id, institution_id=institution.id, world_day=day,
            world_minute=clock.total_minutes, service_key="wellness_session",
            outcome_json=json.dumps(outcome, ensure_ascii=False),
        )
    )
    add_event(session, clock, "FACILITY", f"{npc.name} 使用了公园身心驿站服务", npc_id=npc.id, location="Park", metadata=outcome)
    add_memory(session, clock, npc.id, "我参加了公园身心驿站的活动，精神放松了不少", importance=3, emotion="positive")
    return outcome


def complete_training(session: Session, npc: NPC, clock: ClockSnapshot) -> dict[str, Any] | None:
    institution = session.scalar(
        select(CommunityInstitution).where(CommunityInstitution.institution_key == "career_center")
    )
    action_start = clock.total_minutes - 90
    if institution is None or npc.current_location != institution.location or not institution_is_open(institution, action_start):
        return None
    employment = session.scalar(select(EmploymentProfile).where(EmploymentProfile.npc_id == npc.id))
    if employment is None or npc.money < TRAINING_FEE:
        return None
    definition = profession_definition(npc.job)
    skill = session.scalar(
        select(NPCSkill).where(NPCSkill.npc_id == npc.id, NPCSkill.skill_key == definition["skill"])
    )
    if skill is None:
        return None
    week_start = _week_start(action_start)
    weekly_count = session.scalar(
        select(func.count()).select_from(TrainingRecord).where(
            TrainingRecord.npc_id == npc.id,
            TrainingRecord.week_start_minute == week_start,
        )
    ) or 0
    day = action_start // DAY_MINUTES + 1
    daily_count = session.scalar(
        select(func.count()).select_from(TrainingRecord).where(
            TrainingRecord.institution_id == institution.id,
            TrainingRecord.world_minute >= (day - 1) * DAY_MINUTES,
            TrainingRecord.world_minute < day * DAY_MINUTES,
        )
    ) or 0
    if weekly_count >= TRAINING_WEEKLY_LIMIT or (institution.daily_capacity is not None and daily_count >= institution.daily_capacity):
        return None
    npc.money = round(npc.money - TRAINING_FEE, 2)
    npc.energy -= 5
    npc.mood += 3
    leveled = add_skill_experience(skill, TRAINING_XP)
    employment.performance = round(clamp(employment.performance + 1.0), 2)
    clamp_npc_state(npc)
    add_transaction(session, npc, clock, "training", -TRAINING_FEE, f"参加{institution.name}")
    session.add(
        TrainingRecord(
            npc_id=npc.id, institution_id=institution.id, world_minute=clock.total_minutes,
            week_start_minute=week_start, profession_key=npc.job, skill_key=skill.skill_key,
            fee=TRAINING_FEE, skill_experience=TRAINING_XP, leveled_up=leveled,
        )
    )
    add_event(
        session, clock, "TRAINING", f"{npc.name} 完成了职业培训",
        npc_id=npc.id, location="Office",
        metadata={"fee": TRAINING_FEE, "skill_key": skill.skill_key, "skill_experience": TRAINING_XP, "leveled_up": leveled},
    )
    add_memory(session, clock, npc.id, "我完成了一次职业培训，对本职工作更熟练了", importance=5, emotion="positive")
    return {"fee": TRAINING_FEE, "skill_key": skill.skill_key, "skill_experience": TRAINING_XP, "leveled_up": leveled}


def complete_housing_upgrade(session: Session, npc: NPC, clock: ClockSnapshot) -> dict[str, Any] | None:
    institution = session.scalar(
        select(CommunityInstitution).where(CommunityInstitution.institution_key == "housing_desk")
    )
    action_start = clock.total_minutes - 30
    housing = session.scalar(select(Housing).where(Housing.npc_id == npc.id))
    if (
        institution is None or housing is None or npc.current_location != "Home"
        or not institution_is_open(institution, action_start) or housing.arrears > 0
    ):
        return None
    upgrade = HOUSING_UPGRADES.get(housing.tier)
    if upgrade is None or npc.money < upgrade["cost"]:
        return None
    before = {
        "tier": housing.tier, "rent": housing.weekly_rent, "comfort": housing.comfort,
    }
    npc.money = round(npc.money - upgrade["cost"], 2)
    housing.tier = upgrade["tier"]
    housing.weekly_rent = upgrade["weekly_rent"]
    housing.comfort = max(housing.comfort, upgrade["comfort"])
    npc.mood += 6
    clamp_npc_state(npc)
    add_transaction(session, npc, clock, "housing_upgrade", -upgrade["cost"], f"住房升级至{housing.tier}")
    session.add(
        HousingUpgradeRecord(
            npc_id=npc.id, world_minute=clock.total_minutes,
            tier_before=before["tier"], tier_after=housing.tier, cost=upgrade["cost"],
            weekly_rent_before=before["rent"], weekly_rent_after=housing.weekly_rent,
            comfort_before=before["comfort"], comfort_after=housing.comfort,
        )
    )
    add_event(
        session, clock, "HOUSING_UPGRADE", f"{npc.name} 将住房升级为 {housing.tier}",
        npc_id=npc.id, location="Home",
        metadata={"tier_before": before["tier"], "tier_after": housing.tier, "cost": upgrade["cost"]},
    )
    add_memory(session, clock, npc.id, f"我把住房升级成了 {housing.tier}，居住环境更舒适了", importance=6, emotion="positive")
    return {"tier_before": before["tier"], "tier_after": housing.tier, "cost": upgrade["cost"]}


def community_context(session: Session, npc: NPC, clock: ClockSnapshot) -> dict[str, Any]:
    schedule = session.scalar(select(WorkSchedule).where(WorkSchedule.npc_id == npc.id))
    housing = session.scalar(select(Housing).where(Housing.npc_id == npc.id))
    employment = session.scalar(select(EmploymentProfile).where(EmploymentProfile.npc_id == npc.id))
    if schedule is None or housing is None or employment is None:
        return {"enabled": False}
    institutions = {
        row.institution_key: row
        for row in session.scalars(select(CommunityInstitution).order_by(CommunityInstitution.id))
    }
    required = {"community_store", "park_wellness", "career_center", "housing_desk"}
    if not required.issubset(institutions):
        return {"enabled": False}
    workdays = set(json.loads(schedule.workdays_json))
    weekday_index = _weekday_index(clock.total_minutes)
    minute = _minute_of_day(clock.total_minutes)
    today_attendance = session.scalar(
        select(WorkAttendance).where(
            WorkAttendance.npc_id == npc.id,
            WorkAttendance.world_day == clock.day,
        )
    )
    on_workday = weekday_index in workdays
    work_window = on_workday and schedule.start_minute - 60 <= minute < schedule.end_minute
    shift_complete = bool(today_attendance and today_attendance.worked_minutes >= SHIFT_MINUTES)
    week_start = _week_start(clock.total_minutes)
    training_count = session.scalar(
        select(func.count()).select_from(TrainingRecord).where(
            TrainingRecord.npc_id == npc.id,
            TrainingRecord.week_start_minute == week_start,
        )
    ) or 0
    training_center = institutions["career_center"]
    training_day_start = (clock.day - 1) * DAY_MINUTES
    training_daily_count = session.scalar(
        select(func.count()).select_from(TrainingRecord).where(
            TrainingRecord.institution_id == training_center.id,
            TrainingRecord.world_minute >= training_day_start,
            TrainingRecord.world_minute < training_day_start + DAY_MINUTES,
        )
    ) or 0
    wellness = institutions["park_wellness"]
    used_wellness = session.scalar(
        select(FacilityUsage.id).where(
            FacilityUsage.npc_id == npc.id,
            FacilityUsage.institution_id == wellness.id,
            FacilityUsage.world_day == clock.day,
        )
    ) is not None
    wellness_capacity_used = _daily_usage_count(session, wellness.id, clock.day)
    upgrade = HOUSING_UPGRADES.get(housing.tier)
    stock_total = session.scalar(select(func.sum(StoreStock.quantity))) or 0
    return {
        "enabled": True,
        "is_weekend": _is_weekend(clock.total_minutes),
        "schedule": {
            "workdays": sorted(workdays), "start_minute": schedule.start_minute,
            "end_minute": schedule.end_minute, "grace_minutes": schedule.grace_minutes,
        },
        "on_workday": on_workday,
        "work_available": work_window and not shift_complete,
        "work_completed_today": shift_complete,
        "is_late": on_workday and minute > schedule.start_minute + schedule.grace_minutes and not today_attendance,
        "minutes_after_start": max(0, minute - schedule.start_minute),
        "store_open": institution_is_open(institutions["community_store"], clock.total_minutes),
        "stock_available": stock_total > 0,
        "facility_open": institution_is_open(wellness, clock.total_minutes),
        "facility_available": (
            institution_is_open(wellness, clock.total_minutes)
            and not used_wellness
            and (wellness.daily_capacity is None or wellness_capacity_used < wellness.daily_capacity)
        ),
        "training_open": institution_is_open(training_center, clock.total_minutes),
        "training_available": (
            institution_is_open(training_center, clock.total_minutes)
            and training_count < TRAINING_WEEKLY_LIMIT
            and (training_center.daily_capacity is None or training_daily_count < training_center.daily_capacity)
            and npc.money >= TRAINING_FEE
        ),
        "training_count": training_count,
        "training_fee": TRAINING_FEE,
        "housing_service_open": institution_is_open(institutions["housing_desk"], clock.total_minutes),
        "housing_upgrade_available": bool(
            institution_is_open(institutions["housing_desk"], clock.total_minutes)
            and upgrade and housing.arrears <= 0 and npc.money >= upgrade["cost"]
        ),
        "next_housing_upgrade": upgrade,
    }


def institution_snapshots(session: Session, clock: ClockSnapshot) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for institution in session.scalars(select(CommunityInstitution).order_by(CommunityInstitution.id)):
        opened, closed = _hours(institution, clock.total_minutes)
        used = _daily_usage_count(session, institution.id, clock.day) if institution.daily_capacity else 0
        result.append({
            "id": institution.id, "key": institution.institution_key, "name": institution.name,
            "type": institution.institution_type, "location": institution.location,
            "open": institution_is_open(institution, clock.total_minutes),
            "today_hours": f"{_format_minute(opened)}–{_format_minute(closed)}",
            "weekday_hours": f"{_format_minute(institution.weekday_open_minute)}–{_format_minute(institution.weekday_close_minute)}",
            "weekend_hours": f"{_format_minute(institution.weekend_open_minute)}–{_format_minute(institution.weekend_close_minute)}",
            "service_key": institution.service_key,
            "daily_capacity": institution.daily_capacity,
            "capacity_used_today": used,
        })
    return result


def stock_snapshots(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(
        select(StoreStock, StoreListing, ItemDefinition)
        .join(StoreListing, StoreListing.id == StoreStock.listing_id)
        .join(ItemDefinition, ItemDefinition.id == StoreListing.item_id)
        .order_by(StoreStock.id)
    ).all()
    return [
        {
            "stock_id": stock.id, "listing_id": listing.id, "item_id": item.id,
            "item_key": item.item_key, "item_name": item.name,
            "quantity": stock.quantity, "capacity": stock.capacity,
            "restock_amount": stock.restock_amount,
            "restock_interval_minutes": stock.restock_interval_minutes,
            "last_restock_minute": stock.last_restock_minute,
            "next_restock_minute": stock.next_restock_minute,
        }
        for stock, listing, item in rows
    ]


def npc_rhythm_snapshot(session: Session, npc: NPC, clock: ClockSnapshot) -> dict[str, Any] | None:
    context = community_context(session, npc, clock)
    if not context.get("enabled"):
        return None
    schedule = session.scalar(select(WorkSchedule).where(WorkSchedule.npc_id == npc.id))
    attendance = list(
        session.scalars(
            select(WorkAttendance).where(WorkAttendance.npc_id == npc.id)
            .order_by(WorkAttendance.world_day.desc()).limit(7)
        )
    )
    training = list(
        session.scalars(
            select(TrainingRecord).where(TrainingRecord.npc_id == npc.id)
            .order_by(TrainingRecord.id.desc()).limit(5)
        )
    )
    upgrades = list(
        session.scalars(
            select(HousingUpgradeRecord).where(HousingUpgradeRecord.npc_id == npc.id)
            .order_by(HousingUpgradeRecord.id.desc()).limit(5)
        )
    )
    housing = session.scalar(select(Housing).where(Housing.npc_id == npc.id))
    return {
        "npc_id": npc.id, "npc_name": npc.name,
        "is_weekend": context["is_weekend"],
        "schedule": {
            **context["schedule"],
            "start": _format_minute(schedule.start_minute), "end": _format_minute(schedule.end_minute),
            "on_time_days": schedule.on_time_days, "late_days": schedule.late_days,
            "shifts_completed": schedule.shifts_completed,
        },
        "today": {
            "on_workday": context["on_workday"], "work_available": context["work_available"],
            "work_completed": context["work_completed_today"], "store_open": context["store_open"],
            "facility_available": context["facility_available"], "training_available": context["training_available"],
        },
        "attendance": [
            {"world_day": row.world_day, "status": row.status, "minutes_late": row.minutes_late, "worked_minutes": row.worked_minutes}
            for row in attendance
        ],
        "training": [
            {"world_minute": row.world_minute, "profession_key": row.profession_key, "skill_key": row.skill_key,
             "fee": row.fee, "skill_experience": row.skill_experience, "leveled_up": row.leveled_up}
            for row in training
        ],
        "housing": {
            "tier": housing.tier, "comfort": round(housing.comfort, 2), "weekly_rent": round(housing.weekly_rent, 2),
            "next_upgrade": context["next_housing_upgrade"],
            "upgrades": [
                {"world_minute": row.world_minute, "tier_before": row.tier_before, "tier_after": row.tier_after, "cost": row.cost}
                for row in upgrades
            ],
        },
    }
