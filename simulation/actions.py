from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import CareerDevelopment, LongTermGoal, NPC, Relationship
from simulation.career_budget import complete_job_search
from simulation.community import (
    complete_facility_service,
    complete_housing_upgrade,
    complete_training,
    consume_stock,
    record_work_attendance,
    stocked_listing_ids,
)
from simulation.clock import ClockSnapshot
from simulation.decision import ACTION_DURATIONS, MOVE_ACTIONS
from simulation.economy import (
    complete_item_use,
    complete_shopping,
    complete_work_economy,
    consume_prepared_meal,
    record_direct_meal_purchase,
)
from simulation.events import add_event
from simulation.memory import add_memory
from simulation.npc import clamp_npc_state
from simulation.random_service import RandomService
from simulation.relationships import social_delta, update_relationship


JOB_PAY = {"Designer": 14, "Developer": 16, "Manager": 18, "Writer": 13, "Accountant": 15}
EVENT_TYPES = {
    "Sleep": "SLEEP",
    "Eat": "EAT",
    "Work": "WORK",
    "Relax": "RELAX",
    "Socialize": "SOCIAL",
    "Idle": "SYSTEM",
}
LOCATION_NAMES_ZH = {"Home": "家", "Office": "办公室", "Cafe": "咖啡馆", "Park": "公园"}


def apply_passive_drift(npc: NPC) -> None:
    npc.hunger += 0.8
    npc.social_need += 0.25
    if npc.current_action != "Sleep":
        npc.energy -= 0.2
    clamp_npc_state(npc)


def start_action(npc: NPC, action: str, clock: ClockSnapshot) -> None:
    npc.current_action = action
    npc.action_end_minute = clock.total_minutes + ACTION_DURATIONS[action]
    target_by_move = {move: location for location, move in MOVE_ACTIONS.items()}
    npc.pending_location = target_by_move.get(action)


def _social_target(
    session: Session,
    npc: NPC,
    random_service: RandomService,
    target_npc_id: int | None = None,
) -> tuple[NPC, Relationship] | None:
    others = list(
        session.scalars(
            select(NPC).where(NPC.current_location == npc.current_location, NPC.id != npc.id).order_by(NPC.id)
        )
    )
    if not others:
        return None
    if target_npc_id is not None:
        target = next((other for other in others if other.id == target_npc_id), None)
        if target is None:
            return None
        relationship = session.scalar(
            select(Relationship).where(
                Relationship.from_npc_id == npc.id,
                Relationship.to_npc_id == target.id,
            )
        )
        return (target, relationship) if relationship is not None else None
    relationship_goal = session.scalar(
        select(LongTermGoal).where(
            LongTermGoal.npc_id == npc.id,
            LongTermGoal.goal_type == "relationship",
        )
    )
    choices: list[tuple[float, NPC, Relationship]] = []
    for other in others:
        relationship = session.scalar(
            select(Relationship).where(
                Relationship.from_npc_id == npc.id, Relationship.to_npc_id == other.id
            )
        )
        if relationship is None:
            continue
        goal_bonus = 0.0
        if relationship_goal is not None and relationship_goal.target_npc_id == other.id:
            remaining = max(0.0, relationship_goal.target_value - relationship.score)
            need_ratio = min(1.0, remaining / max(1.0, relationship_goal.target_value))
            goal_bonus = 40.0 * relationship_goal.priority * need_ratio
        affinity = relationship.score + goal_bonus + random_service.uniform(-5, 5)
        choices.append((affinity, other, relationship))
    if not choices:
        return None
    _, target, relationship = max(choices, key=lambda item: item[0])
    return target, relationship


def complete_action(
    session: Session,
    npc: NPC,
    clock: ClockSnapshot,
    random_service: RandomService,
    economy_enabled: bool = True,
    career_budget_enabled: bool = True,
    community_enabled: bool = False,
    action_params: dict | None = None,
) -> None:
    action = npc.current_action
    if action.startswith("Go") and npc.pending_location:
        previous = npc.current_location
        destination = npc.pending_location
        npc.current_location = destination
        npc.pending_location = None
        npc.last_move_minute = clock.total_minutes
        npc.energy -= 2
        add_event(
            session,
            clock,
            "MOVE",
            f"{npc.name} 从{LOCATION_NAMES_ZH[previous]}移动到了{LOCATION_NAMES_ZH[destination]}",
            npc_id=npc.id,
            location=destination,
            metadata={"from": previous, "to": destination, "energy_change": -2},
        )
        add_memory(
            session,
            clock,
            npc.id,
            f"我从{LOCATION_NAMES_ZH[previous]}来到了{LOCATION_NAMES_ZH[destination]}",
            importance=2,
            emotion="neutral",
        )
    elif action == "Sleep":
        npc.energy += 15
        npc.hunger += 4
        add_event(session, clock, "SLEEP", f"{npc.name} 睡醒了，精力有所恢复", npc_id=npc.id, location=npc.current_location, metadata={"energy_change": 15, "hunger_change": 4})
        add_memory(session, clock, npc.id, "我好好睡了一觉，醒来后精力恢复了", importance=3, emotion="positive")
    elif action == "Eat":
        npc.hunger -= 45
        used_inventory = economy_enabled and consume_prepared_meal(session, npc, clock)
        meal_cost = 0.0
        if not used_inventory:
            meal_cost = min(8.0, npc.money)
            npc.money = round(max(0.0, npc.money - 8), 2)
            if economy_enabled:
                record_direct_meal_purchase(session, npc, clock, meal_cost)
        npc.mood += 3
        add_event(session, clock, "EAT", f"{npc.name} 吃完了一顿饭", npc_id=npc.id, location=npc.current_location, metadata={"hunger_change": -45, "money_change": -meal_cost, "mood_change": 3, "inventory_used": used_inventory})
        add_memory(session, clock, npc.id, "我吃完一顿饭，感觉没那么饿了", importance=2, emotion="positive")
    elif action == "Work":
        pay = JOB_PAY.get(npc.job, 15)
        npc.energy -= 8
        npc.hunger += 7
        satisfaction_change = random_service.uniform(-1.5, 1.5)
        career_goal = session.scalar(
            select(LongTermGoal).where(
                LongTermGoal.npc_id == npc.id,
                LongTermGoal.goal_type == "career_satisfaction",
            )
        )
        if career_goal is not None and npc.work_satisfaction < career_goal.target_value:
            remaining_ratio = min(
                1.0,
                (career_goal.target_value - npc.work_satisfaction) / max(1.0, career_goal.target_value),
            )
            satisfaction_change += 1.5 * career_goal.priority * remaining_ratio
        npc.work_satisfaction += satisfaction_change
        career = session.scalar(select(CareerDevelopment).where(CareerDevelopment.npc_id == npc.id))
        unemployed = career_budget_enabled and career is not None and career.employment_status != "employed"
        economy_result = None if unemployed else (
            complete_work_economy(session, npc, clock, satisfaction_change)
            if economy_enabled else None
        )
        if unemployed:
            pay = 0.0
            satisfaction_change = min(satisfaction_change, 0.0)
        elif economy_result is None:
            npc.money += pay
        else:
            pay = economy_result["pay"]
        metadata = {"energy_change": -8, "hunger_change": 7, "money_change": pay, "work_satisfaction_change": round(satisfaction_change, 2)}
        if economy_result is not None:
            metadata.update({
                "performance_change": economy_result["performance_change"],
                "performance": economy_result["performance"],
                "skill_key": economy_result["skill_key"],
                "skill_level": economy_result["skill_level"],
            })
        attendance = record_work_attendance(session, npc, clock) if community_enabled else None
        if attendance is not None:
            metadata["attendance"] = attendance
        add_event(session, clock, "WORK", f"{npc.name} 完成了一段工作，获得 ${pay:.2f}", npc_id=npc.id, location=npc.current_location, metadata=metadata)
        add_memory(
            session,
            clock,
            npc.id,
            f"我完成了一段工作并获得 ${pay:.2f}",
            importance=4,
            emotion="positive" if satisfaction_change >= 0 else "negative",
        )
    elif action == "JobSearch":
        result = complete_job_search(
            session, npc, clock, random_service,
            preferred_profession_key=(action_params or {}).get("profession_key"),
        ) if career_budget_enabled else None
        npc.energy -= 3
        if result is None:
            add_event(session, clock, "CAREER_SEARCH", f"{npc.name} 的职业资料暂不可用，求职安全回退为等待", npc_id=npc.id)
        elif result["success"]:
            npc.mood += 5
        else:
            npc.mood -= 2
            add_event(session, clock, "CAREER_SEARCH", f"{npc.name} 完成一次求职申请，但未成功", npc_id=npc.id,
                      metadata={"success": False, "reason": result["reason"]})
            add_memory(session, clock, npc.id, f"我完成一次求职申请，但未成功：{result['reason']}", importance=4, emotion="negative")
    elif action == "Relax":
        npc.energy += 5
        npc.mood += 8
        npc.hunger += 3
        add_event(session, clock, "RELAX", f"{npc.name} 结束了放松休息", npc_id=npc.id, location=npc.current_location, metadata={"energy_change": 5, "mood_change": 8, "hunger_change": 3})
        add_memory(session, clock, npc.id, "我放松休息了一会儿，心情变好了", importance=2, emotion="positive")
    elif action == "Socialize":
        result = _social_target(
            session, npc, random_service,
            int(action_params["target_npc_id"])
            if action_params and action_params.get("target_npc_id") is not None else None,
        )
        npc.energy -= 3
        npc.social_need -= 30
        npc.mood += 5
        if result is None:
            add_event(session, clock, "SOCIAL", f"{npc.name} 没有找到可以聊天的人", npc_id=npc.id, location=npc.current_location)
            add_memory(session, clock, npc.id, "我想找人聊天，但没有遇到合适的人", importance=2, emotion="negative")
        else:
            target, relationship = result
            delta = social_delta(npc, relationship.score, random_service.uniform(-2, 2))
            applied_delta = update_relationship(relationship, delta)
            add_event(session, clock, "SOCIAL", f"{npc.name} 与 {target.name} 聊了聊天", npc_id=npc.id, target_npc_id=target.id, location=npc.current_location)
            sign = "+" if applied_delta >= 0 else ""
            add_event(session, clock, "RELATIONSHIP", f"{npc.name} → {target.name} 的关系值变化 {sign}{applied_delta}", npc_id=npc.id, target_npc_id=target.id, location=npc.current_location, metadata={"change": applied_delta, "new_score": relationship.score})
            emotion = "positive" if applied_delta > 0 else "negative" if applied_delta < 0 else "neutral"
            importance = 4 + min(3, abs(applied_delta))
            add_memory(
                session,
                clock,
                npc.id,
                f"我和 {target.name} 聊了聊天，我们的关系变化了 {sign}{applied_delta}",
                importance=importance,
                emotion=emotion,
                related_npc_id=target.id,
            )
            add_memory(
                session,
                clock,
                target.id,
                f"{npc.name} 和我聊了聊天",
                importance=max(3, importance - 1),
                emotion=emotion,
                related_npc_id=npc.id,
            )
    elif action == "Shop":
        available_stock = stocked_listing_ids(session) if community_enabled else None
        result = complete_shopping(
            session, npc, clock, available_stock,
            preferred_item_key=(action_params or {}).get("item_key"),
            preferred_listing_id=(action_params or {}).get("listing_id"),
        ) if economy_enabled else None
        if result is not None and community_enabled and not consume_stock(session, result["listing_id"]):
            # A single Simulation Engine transaction owns both purchase and stock;
            # this guard is only reachable with inconsistent/missing V0.7 data.
            result = None
        if result is None:
            add_event(session, clock, "SHOP", f"{npc.name} 逛了商店，但没有购买物品", npc_id=npc.id, location=npc.current_location)
            add_memory(session, clock, npc.id, "我逛了商店，但没有买东西", importance=2, emotion="neutral")
        else:
            add_event(
                session, clock, "SHOP", f"{npc.name} 购买了{result['item_name']}，花费 ${result['price']:.2f}",
                npc_id=npc.id, location=npc.current_location,
                metadata={"item_id": result["item_id"], "item_key": result["item_key"], "money_change": -result["price"]},
            )
            add_memory(session, clock, npc.id, f"我购买了{result['item_name']}，花费 ${result['price']:.2f}", importance=3, emotion="neutral")
    elif action == "UseItem":
        result = complete_item_use(
            session, npc, clock,
            preferred_item_key=(action_params or {}).get("item_key"),
            preferred_item_id=(action_params or {}).get("item_id"),
        ) if economy_enabled else None
        if result is None:
            add_event(session, clock, "ITEM", f"{npc.name} 没有可使用的合适物品", npc_id=npc.id, location=npc.current_location)
        else:
            description = f"{npc.name} 使用了{result['item_name']}"
            if result.get("skill_leveled"):
                description += "，本职技能提升了"
            add_event(
                session, clock, "ITEM", description, npc_id=npc.id, location=npc.current_location,
                metadata={"item_id": result["item_id"], "item_key": result["item_key"], "skill_leveled": result.get("skill_leveled", False)},
            )
            add_memory(session, clock, npc.id, description.replace(npc.name, "我", 1), importance=4, emotion="positive")
    elif action == "UseFacility":
        result = complete_facility_service(session, npc, clock) if community_enabled else None
        if result is None:
            add_event(session, clock, "FACILITY", f"{npc.name} 未能使用社区设施，安全回退为等待", npc_id=npc.id, location=npc.current_location)
    elif action == "Train":
        result = complete_training(session, npc, clock) if community_enabled else None
        if result is None:
            add_event(session, clock, "TRAINING", f"{npc.name} 未能参加职业培训，安全回退为等待", npc_id=npc.id, location=npc.current_location)
    elif action == "UpgradeHome":
        result = complete_housing_upgrade(session, npc, clock) if community_enabled else None
        if result is None:
            add_event(session, clock, "HOUSING_UPGRADE", f"{npc.name} 暂未满足住房升级条件", npc_id=npc.id, location=npc.current_location)
    elif action == "Idle":
        add_event(
            session,
            clock,
            "SYSTEM",
            f"{npc.name} 安静地发了一会儿呆",
            npc_id=npc.id,
            location=npc.current_location,
        )
    clamp_npc_state(npc)
