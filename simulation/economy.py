from __future__ import annotations

import json
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database.models import (
    EconomicTransaction,
    EmploymentProfile,
    Housing,
    InventoryItem,
    ItemDefinition,
    NPC,
    NPCSkill,
    Store,
    StoreListing,
)
from simulation.clock import ClockSnapshot
from simulation.events import add_event
from simulation.memory import add_memory
from simulation.npc import clamp, clamp_npc_state


PROFESSIONS: dict[str, dict[str, Any]] = {
    "Designer": {"label": "设计师", "employer": "城市设计工作室", "base_wage": 14.0, "skill": "design"},
    "Developer": {"label": "开发工程师", "employer": "微光科技", "base_wage": 16.0, "skill": "programming"},
    "Manager": {"label": "经理", "employer": "社区服务中心", "base_wage": 18.0, "skill": "management"},
    "Writer": {"label": "作家", "employer": "自由职业", "base_wage": 13.0, "skill": "writing"},
    "Accountant": {"label": "会计师", "employer": "城市账务所", "base_wage": 15.0, "skill": "accounting"},
}

ITEM_CATALOG: tuple[dict[str, Any], ...] = (
    {"id": 1, "item_key": "prepared_meal", "name": "便当", "category": "food", "price": 8.0,
     "effect": {"hunger": -45, "mood": 3}, "description": "可在下次用餐时消耗。"},
    {"id": 2, "item_key": "coffee", "name": "咖啡", "category": "consumable", "price": 5.0,
     "effect": {"energy": 9, "mood": 2, "hunger": 2}, "description": "使用后短暂恢复精力。"},
    {"id": 3, "item_key": "professional_guide", "name": "职业进阶手册", "category": "learning", "price": 18.0,
     "effect": {"skill_experience": 36}, "description": "使用后提升本职技能经验。"},
    {"id": 4, "item_key": "home_decor", "name": "家居装饰", "category": "housing", "price": 28.0,
     "effect": {"comfort": 8, "mood": 4}, "description": "在家使用可改善住房舒适度。"},
)

WEEK_MINUTES = 7 * 24 * 60


def profession_definition(job: str) -> dict[str, Any]:
    return PROFESSIONS.get(
        job,
        {"label": job, "employer": "社区雇主", "base_wage": 15.0, "skill": "professional"},
    )


def ensure_economy_data(
    session: Session,
    npcs: Iterable[NPC],
    created_minute: int,
) -> dict[str, int]:
    """Idempotently populate only V0.5 tables, including upgrades from every old version."""
    counts = {"stores": 0, "items": 0, "listings": 0, "employment": 0, "skills": 0, "housing": 0}
    store = session.get(Store, 1)
    if store is None:
        store = Store(id=1, name="社区生活商店", location="Cafe", revenue=0.0)
        session.add(store)
        counts["stores"] += 1

    for spec in ITEM_CATALOG:
        item = session.scalar(select(ItemDefinition).where(ItemDefinition.item_key == spec["item_key"]))
        if item is None:
            item = ItemDefinition(
                id=spec["id"],
                item_key=spec["item_key"],
                name=spec["name"],
                category=spec["category"],
                base_price=spec["price"],
                effect_json=json.dumps(spec["effect"], ensure_ascii=False),
            )
            session.add(item)
            counts["items"] += 1
        session.flush()
        listing = session.scalar(
            select(StoreListing).where(StoreListing.store_id == store.id, StoreListing.item_id == item.id)
        )
        if listing is None:
            session.add(StoreListing(store_id=store.id, item_id=item.id, price=spec["price"], enabled=True))
            counts["listings"] += 1

    next_rent = ((created_minute // WEEK_MINUTES) + 1) * WEEK_MINUTES
    for npc in npcs:
        definition = profession_definition(npc.job)
        employment = session.scalar(select(EmploymentProfile).where(EmploymentProfile.npc_id == npc.id))
        if employment is None:
            initial_performance = round(clamp(45 + npc.discipline * 20 + npc.ambition * 10), 2)
            session.add(
                EmploymentProfile(
                    npc_id=npc.id,
                    profession_key=npc.job,
                    employer=definition["employer"],
                    base_wage=definition["base_wage"],
                    performance=initial_performance,
                )
            )
            counts["employment"] += 1
        skill = session.scalar(
            select(NPCSkill).where(NPCSkill.npc_id == npc.id, NPCSkill.skill_key == definition["skill"])
        )
        if skill is None:
            level = max(1, min(5, 1 + int((npc.discipline + npc.ambition) * 1.5)))
            session.add(NPCSkill(npc_id=npc.id, skill_key=definition["skill"], level=level, experience=0.0))
            counts["skills"] += 1
        housing = session.scalar(select(Housing).where(Housing.npc_id == npc.id))
        if housing is None:
            weekly_rent = 20.0 + (npc.id % 3) * 3.0
            comfort = 58.0 + npc.id * 3.0
            session.add(
                Housing(
                    npc_id=npc.id,
                    name=f"{npc.name} 的公寓",
                    tier="standard",
                    weekly_rent=weekly_rent,
                    comfort=comfort,
                    next_rent_minute=next_rent,
                    arrears=0.0,
                )
            )
            counts["housing"] += 1
    session.flush()
    return counts


def _job_skill(session: Session, npc: NPC) -> NPCSkill | None:
    skill_key = profession_definition(npc.job)["skill"]
    return session.scalar(
        select(NPCSkill).where(NPCSkill.npc_id == npc.id, NPCSkill.skill_key == skill_key)
    )


def add_skill_experience(skill: NPCSkill, amount: float) -> bool:
    skill.experience += amount
    leveled = False
    while skill.level < 10 and skill.experience >= skill.level * 60:
        skill.experience -= skill.level * 60
        skill.level += 1
        leveled = True
    return leveled


def add_transaction(
    session: Session,
    npc: NPC,
    clock: ClockSnapshot,
    kind: str,
    amount: float,
    description: str,
    item_id: int | None = None,
) -> EconomicTransaction:
    transaction = EconomicTransaction(
        npc_id=npc.id,
        world_minute=clock.total_minutes,
        kind=kind,
        amount=round(amount, 2),
        balance_after=round(npc.money, 2),
        item_id=item_id,
        description=description,
    )
    session.add(transaction)
    return transaction


def complete_work_economy(
    session: Session,
    npc: NPC,
    clock: ClockSnapshot,
    satisfaction_change: float,
) -> dict[str, Any] | None:
    employment = session.scalar(select(EmploymentProfile).where(EmploymentProfile.npc_id == npc.id))
    skill = _job_skill(session, npc)
    if employment is None or skill is None:
        return None
    wage_multiplier = max(0.85, min(1.30, 0.82 + employment.performance / 500 + skill.level * 0.025))
    pay = round(employment.base_wage * wage_multiplier, 2)
    performance_change = round(
        (npc.discipline - 0.5) * 1.2 + (npc.ambition - 0.5) * 0.8 + satisfaction_change * 0.18,
        2,
    )
    employment.performance = round(clamp(employment.performance + performance_change), 2)
    employment.experience += 1.0
    employment.shifts_completed += 1
    employment.total_earnings = round(employment.total_earnings + pay, 2)
    leveled = add_skill_experience(skill, 12 + npc.ambition * 4)
    npc.money = round(npc.money + pay, 2)
    add_transaction(session, npc, clock, "wage", pay, f"{npc.job} 工作工资")
    return {
        "pay": pay,
        "performance_change": performance_change,
        "performance": employment.performance,
        "skill_key": skill.skill_key,
        "skill_level": skill.level,
        "skill_leveled": leveled,
    }


def _inventory_record(session: Session, npc_id: int, item_id: int) -> InventoryItem | None:
    return session.scalar(
        select(InventoryItem).where(InventoryItem.npc_id == npc_id, InventoryItem.item_id == item_id)
    )


def inventory_quantities(session: Session, npc_id: int) -> dict[str, int]:
    rows = session.execute(
        select(ItemDefinition.item_key, InventoryItem.quantity)
        .join(InventoryItem, InventoryItem.item_id == ItemDefinition.id)
        .where(InventoryItem.npc_id == npc_id, InventoryItem.quantity > 0)
    )
    return {key: quantity for key, quantity in rows}


def consume_prepared_meal(session: Session, npc: NPC, clock: ClockSnapshot) -> bool:
    item = session.scalar(select(ItemDefinition).where(ItemDefinition.item_key == "prepared_meal"))
    if item is None:
        return False
    inventory = _inventory_record(session, npc.id, item.id)
    if inventory is None or inventory.quantity <= 0:
        return False
    inventory.quantity -= 1
    add_transaction(session, npc, clock, "consume", 0.0, "食用了库存中的便当", item.id)
    return True


def record_direct_meal_purchase(session: Session, npc: NPC, clock: ClockSnapshot, amount: float) -> None:
    item = session.scalar(select(ItemDefinition).where(ItemDefinition.item_key == "prepared_meal"))
    add_transaction(
        session,
        npc,
        clock,
        "purchase",
        -amount,
        "即时购买并食用便当",
        item.id if item else None,
    )
    store = session.get(Store, 1)
    if store is not None:
        store.revenue = round(store.revenue + amount, 2)


def _preferred_purchase_key(session: Session, npc: NPC) -> str:
    quantities = inventory_quantities(session, npc.id)
    housing = session.scalar(select(Housing).where(Housing.npc_id == npc.id))
    skill = _job_skill(session, npc)
    if npc.hunger >= 35 and quantities.get("prepared_meal", 0) < 2:
        return "prepared_meal"
    if npc.energy < 45 and quantities.get("coffee", 0) < 1:
        return "coffee"
    if skill is not None and skill.level < 7 and quantities.get("professional_guide", 0) < 1:
        return "professional_guide"
    if housing is not None and housing.comfort < 82 and quantities.get("home_decor", 0) < 1:
        return "home_decor"
    return "prepared_meal" if quantities.get("prepared_meal", 0) < 2 else "coffee"


def complete_shopping(
    session: Session,
    npc: NPC,
    clock: ClockSnapshot,
    stocked_listings: set[int] | None = None,
    preferred_item_key: str | None = None,
    preferred_listing_id: int | None = None,
) -> dict[str, Any] | None:
    preferred = preferred_item_key or _preferred_purchase_key(session, npc)
    rows = session.execute(
        select(ItemDefinition, StoreListing, Store)
        .join(StoreListing, StoreListing.item_id == ItemDefinition.id)
        .join(Store, Store.id == StoreListing.store_id)
        .where(StoreListing.enabled.is_(True), Store.location == npc.current_location)
        .order_by(StoreListing.id)
    ).all()
    affordable = [
        row for row in rows
        if row[1].price <= npc.money
        and (stocked_listings is None or row[1].id in stocked_listings)
    ]
    if not affordable:
        return None
    selected = next(
        (
            row for row in affordable
            if row[0].item_key == preferred
            and (preferred_listing_id is None or row[1].id == preferred_listing_id)
        ),
        None,
    )
    if selected is None and preferred_item_key is not None:
        return None
    selected = selected or affordable[0]
    item, listing, store = selected
    npc.money = round(npc.money - listing.price, 2)
    store.revenue = round(store.revenue + listing.price, 2)
    inventory = _inventory_record(session, npc.id, item.id)
    if inventory is None:
        inventory = InventoryItem(npc_id=npc.id, item_id=item.id, quantity=0)
        session.add(inventory)
    inventory.quantity += 1
    add_transaction(session, npc, clock, "purchase", -listing.price, f"在{store.name}购买{item.name}", item.id)
    return {
        "item_id": item.id, "item_key": item.item_key, "item_name": item.name,
        "price": listing.price, "listing_id": listing.id,
    }


def complete_item_use(
    session: Session,
    npc: NPC,
    clock: ClockSnapshot,
    preferred_item_key: str | None = None,
    preferred_item_id: int | None = None,
) -> dict[str, Any] | None:
    quantities = inventory_quantities(session, npc.id)
    preferred = preferred_item_key or (
        "coffee" if npc.energy < 65 and quantities.get("coffee", 0) else
        "professional_guide" if quantities.get("professional_guide", 0) else
        "home_decor" if npc.current_location == "Home" and quantities.get("home_decor", 0) else
        None
    )
    if preferred is None:
        return None
    if preferred not in {"coffee", "professional_guide", "home_decor"}:
        return None
    if preferred == "home_decor" and npc.current_location != "Home":
        return None
    item = session.scalar(
        select(ItemDefinition).where(
            ItemDefinition.item_key == preferred,
            *([ItemDefinition.id == preferred_item_id] if preferred_item_id is not None else []),
        )
    )
    if item is None:
        return None
    inventory = _inventory_record(session, npc.id, item.id)
    if inventory is None or inventory.quantity <= 0:
        return None
    inventory.quantity -= 1
    effect = json.loads(item.effect_json)
    leveled = False
    if preferred == "coffee":
        npc.energy += effect.get("energy", 0)
        npc.mood += effect.get("mood", 0)
        npc.hunger += effect.get("hunger", 0)
    elif preferred == "professional_guide":
        skill = _job_skill(session, npc)
        if skill is not None:
            leveled = add_skill_experience(skill, float(effect.get("skill_experience", 0)))
    elif preferred == "home_decor":
        housing = session.scalar(select(Housing).where(Housing.npc_id == npc.id))
        if housing is not None:
            housing.comfort = clamp(housing.comfort + effect.get("comfort", 0))
        npc.mood += effect.get("mood", 0)
    clamp_npc_state(npc)
    add_transaction(session, npc, clock, "consume", 0.0, f"使用了{item.name}", item.id)
    return {"item_id": item.id, "item_key": item.item_key, "item_name": item.name, "skill_leveled": leveled}


def process_housing_costs(session: Session, npcs: Iterable[NPC], clock: ClockSnapshot) -> int:
    processed = 0
    for npc in npcs:
        housing = session.scalar(select(Housing).where(Housing.npc_id == npc.id))
        if housing is None:
            continue
        while housing.next_rent_minute <= clock.total_minutes:
            due = round(housing.weekly_rent + housing.arrears, 2)
            paid = round(min(npc.money, due), 2)
            npc.money = round(npc.money - paid, 2)
            housing.arrears = round(due - paid, 2)
            housing.next_rent_minute += WEEK_MINUTES
            if paid:
                add_transaction(session, npc, clock, "rent", -paid, f"支付{housing.name}住房费用")
            if housing.arrears:
                npc.mood -= 5
                description = f"{npc.name} 支付了 ${paid:.2f} 住房费用，仍欠 ${housing.arrears:.2f}"
                emotion = "negative"
            else:
                npc.mood += 1
                description = f"{npc.name} 支付了 ${paid:.2f} 周住房费用"
                emotion = "neutral"
            add_event(
                session, clock, "HOUSING", description, npc_id=npc.id, location="Home",
                metadata={"rent_paid": paid, "arrears": housing.arrears},
            )
            add_memory(session, clock, npc.id, description.replace(npc.name, "我", 1), importance=4, emotion=emotion)
            processed += 1
        clamp_npc_state(npc)
    return processed


def build_economy_context(session: Session, npc: NPC) -> dict[str, Any]:
    employment = session.scalar(select(EmploymentProfile).where(EmploymentProfile.npc_id == npc.id))
    skill = _job_skill(session, npc)
    housing = session.scalar(select(Housing).where(Housing.npc_id == npc.id))
    inventory = inventory_quantities(session, npc.id)
    cheapest = session.scalar(select(func.min(StoreListing.price)).where(StoreListing.enabled.is_(True)))
    return {
        "enabled": employment is not None,
        "performance": employment.performance if employment else 0.0,
        "base_wage": employment.base_wage if employment else profession_definition(npc.job)["base_wage"],
        "skill_level": skill.level if skill else 1,
        "housing_comfort": housing.comfort if housing else 50.0,
        "housing_arrears": housing.arrears if housing else 0.0,
        "inventory": inventory,
        "can_shop": cheapest is not None and npc.money >= cheapest,
        "has_usable_item": (
            inventory.get("coffee", 0) > 0
            or inventory.get("professional_guide", 0) > 0
            or (npc.current_location == "Home" and inventory.get("home_decor", 0) > 0)
        ),
    }


def employment_to_dict(session: Session, npc: NPC) -> dict[str, Any] | None:
    employment = session.scalar(select(EmploymentProfile).where(EmploymentProfile.npc_id == npc.id))
    if employment is None:
        return None
    definition = profession_definition(employment.profession_key)
    skill = _job_skill(session, npc)
    return {
        "profession_key": employment.profession_key,
        "profession_label": definition["label"],
        "employer": employment.employer,
        "base_wage": round(employment.base_wage, 2),
        "performance": round(employment.performance, 2),
        "experience": round(employment.experience, 2),
        "shifts_completed": employment.shifts_completed,
        "total_earnings": round(employment.total_earnings, 2),
        "primary_skill": skill.skill_key if skill else definition["skill"],
        "skill_level": skill.level if skill else 1,
    }


def npc_economy_snapshot(session: Session, npc: NPC) -> dict[str, Any]:
    housing = session.scalar(select(Housing).where(Housing.npc_id == npc.id))
    inventory_rows = session.execute(
        select(InventoryItem, ItemDefinition)
        .join(ItemDefinition, ItemDefinition.id == InventoryItem.item_id)
        .where(InventoryItem.npc_id == npc.id, InventoryItem.quantity > 0)
        .order_by(ItemDefinition.id)
    ).all()
    skills = list(session.scalars(select(NPCSkill).where(NPCSkill.npc_id == npc.id).order_by(NPCSkill.skill_key)))
    transactions = list(
        session.scalars(
            select(EconomicTransaction)
            .where(EconomicTransaction.npc_id == npc.id)
            .order_by(EconomicTransaction.id.desc())
            .limit(30)
        )
    )
    return {
        "npc_id": npc.id,
        "npc_name": npc.name,
        "balance": round(npc.money, 2),
        "employment": employment_to_dict(session, npc),
        "skills": [
            {"skill_key": skill.skill_key, "level": skill.level, "experience": round(skill.experience, 2),
             "next_level_experience": skill.level * 60 if skill.level < 10 else None}
            for skill in skills
        ],
        "housing": None if housing is None else {
            "name": housing.name, "tier": housing.tier, "weekly_rent": round(housing.weekly_rent, 2),
            "comfort": round(housing.comfort, 2), "next_rent_minute": housing.next_rent_minute,
            "arrears": round(housing.arrears, 2),
        },
        "inventory": [
            {"item_id": item.id, "item_key": item.item_key, "name": item.name,
             "category": item.category, "quantity": inventory.quantity}
            for inventory, item in inventory_rows
        ],
        "transactions": [
            {"id": tx.id, "world_minute": tx.world_minute, "kind": tx.kind,
             "amount": round(tx.amount, 2), "balance_after": round(tx.balance_after, 2),
             "item_id": tx.item_id, "description": tx.description}
            for tx in transactions
        ],
    }


def store_catalog_snapshot(session: Session) -> list[dict[str, Any]]:
    stores = list(session.scalars(select(Store).order_by(Store.id)))
    result: list[dict[str, Any]] = []
    for store in stores:
        rows = session.execute(
            select(StoreListing, ItemDefinition)
            .join(ItemDefinition, ItemDefinition.id == StoreListing.item_id)
            .where(StoreListing.store_id == store.id, StoreListing.enabled.is_(True))
            .order_by(StoreListing.id)
        ).all()
        result.append({
            "id": store.id,
            "name": store.name,
            "location": store.location,
            "revenue": round(store.revenue, 2),
            "items": [
                {"id": item.id, "item_key": item.item_key, "name": item.name, "category": item.category,
                 "price": round(listing.price, 2), "effects": json.loads(item.effect_json),
                 "description": next(spec["description"] for spec in ITEM_CATALOG if spec["item_key"] == item.item_key)}
                for listing, item in rows
            ],
        })
    return result
