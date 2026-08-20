from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from database.models import NPC
from simulation.clock import ClockSnapshot
from simulation.random_service import RandomService


LOCATIONS = ("Home", "Office", "Cafe", "Park")
ACTION_DURATIONS = {
    "Sleep": 60,
    "Eat": 30,
    "Work": 60,
    "Relax": 30,
    "Socialize": 20,
    "Shop": 20,
    "UseItem": 20,
    "JobSearch": 60,
    "UseFacility": 60,
    "Train": 90,
    "UpgradeHome": 30,
    "GoHome": 10,
    "GoOffice": 10,
    "GoCafe": 10,
    "GoPark": 10,
    "Idle": 10,
}
MOVE_ACTIONS = {"Home": "GoHome", "Office": "GoOffice", "Cafe": "GoCafe", "Park": "GoPark"}
LOCATION_NAMES_ZH = {"Home": "家", "Office": "办公室", "Cafe": "咖啡馆", "Park": "公园"}
ACTION_NAMES_ZH = {
    "Sleep": "睡觉", "Eat": "吃饭", "Work": "工作", "Relax": "放松",
    "Socialize": "社交", "Shop": "购物", "UseItem": "使用物品", "JobSearch": "求职/转职",
    "UseFacility": "使用设施", "Train": "职业培训", "UpgradeHome": "升级住房", "Idle": "发呆",
}


@dataclass
class Candidate:
    action: str
    score: float
    raw_score: float
    available: bool
    contributions: dict[str, float]
    explanation: str
    target_location: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["score"] = round(self.score, 2)
        data["raw_score"] = round(self.raw_score, 2)
        data["contributions"] = {key: round(value, 2) for key, value in self.contributions.items()}
        return data


@dataclass
class Decision:
    chosen_action: str
    candidates: list[Candidate]
    reason: dict[str, Any]


def _score(contributions: dict[str, float]) -> float:
    return sum(contributions.values())


def _at_work_time(clock: ClockSnapshot) -> bool:
    return clock.weekday not in {"星期六", "星期日"} and 9 <= clock.hour < 18


def _goal_utility(
    context: dict[str, dict[str, Any]] | None,
    goal_type: str,
    maximum: float,
) -> float:
    if not context or goal_type not in context:
        return 0.0
    goal = context[goal_type]
    return round(float(goal["need_score"]) / 100.0 * float(goal["priority"]) * maximum, 2)


def _base_desires(
    npc: NPC,
    clock: ClockSnapshot,
    goal_context: dict[str, dict[str, Any]] | None = None,
) -> dict[str, tuple[float, dict[str, float], str]]:
    night = clock.hour >= 22 or clock.hour < 7
    sleep_parts = {
        "能量不足": (100 - npc.energy) * 1.2,
        "夜间加成": 25.0 if night else 0.0,
        "自律性": npc.discipline * 5,
    }
    eat_parts = {
        "饥饿程度": npc.hunger * 1.3,
        "餐费承受能力": 5.0 if npc.money >= 8 else -20.0,
        "用餐时段": 8.0 if clock.hour in {7, 8, 12, 13, 18, 19} else 0.0,
        "长期目标：储蓄预算": -_goal_utility(goal_context, "savings", 8.0)
        if npc.hunger < 60 else 0.0,
    }
    work_parts = {
        "进取心": npc.ambition * 40,
        "自律性": npc.discipline * 30,
        "工作满意度": npc.work_satisfaction * 0.3,
        "工作时段": 35.0 if _at_work_time(clock) else -30.0,
        "低能量惩罚": -20.0 if npc.energy < 25 else 0.0,
        "高饥饿惩罚": -18.0 if npc.hunger > 75 else 0.0,
        "长期目标：建立储蓄": _goal_utility(goal_context, "savings", 34.0),
        "长期目标：职业满意度": _goal_utility(goal_context, "career_satisfaction", 26.0),
    }
    social_parts = {
        "社交需求": npc.social_need * 0.8,
        "外向程度": npc.extroversion * 40,
        "当前心情": npc.mood * 0.2,
        "长期目标：结交朋友": _goal_utility(goal_context, "friendship", 34.0),
    }
    relax_parts = {
        "能量不足": (100 - npc.energy) * 0.3,
        "心情低落": (100 - npc.mood) * 0.5,
        "风险偏好": npc.risk_tolerance * 4,
    }
    idle_parts = {"基础分": 8.0, "满足感": max(0.0, (npc.mood - 70) * 0.15)}
    return {
        "Sleep": (_score(sleep_parts), sleep_parts, "在家睡觉以恢复能量"),
        "Eat": (_score(eat_parts), eat_parts, "在家或咖啡馆用餐以降低饥饿"),
        "Work": (_score(work_parts), work_parts, "前往办公室工作并获得收入"),
        "Socialize": (_score(social_parts), social_parts, "与当前位置的其他 NPC 互动"),
        "Relax": (_score(relax_parts), relax_parts, "放松以恢复心情和部分能量"),
        "Idle": (_score(idle_parts), idle_parts, "短暂等待后重新考虑行动"),
    }


def decide(
    npc: NPC,
    clock: ClockSnapshot,
    occupants: dict[str, list[NPC]],
    random_service: RandomService,
    goal_context: dict[str, dict[str, Any]] | None = None,
    economy_context: dict[str, Any] | None = None,
    career_budget_context: dict[str, Any] | None = None,
    community_context: dict[str, Any] | None = None,
    social_life_context: dict[str, Any] | None = None,
) -> Decision:
    desires = _base_desires(npc, clock, goal_context)
    economy_enabled = bool(economy_context and economy_context.get("enabled"))
    career_budget_enabled = bool(career_budget_context and career_budget_context.get("enabled"))
    community_enabled = bool(community_context and community_context.get("enabled"))
    social_life_enabled = bool(social_life_context and social_life_context.get("enabled"))
    employed = not career_budget_enabled or career_budget_context.get("employment_status") == "employed"
    if career_budget_enabled:
        pressure = float(career_budget_context.get("economic_pressure", 0.0))
        desires["Work"][1]["经济压力下的收入需求"] = round(pressure * 0.32, 2)
        if not employed:
            desires["Work"][1]["待业状态不可工作"] = -200.0
        job_search_parts = {
            "求职需要": 85.0 if not employed else 32.0 if career_budget_context.get("job_search_needed") else -80.0,
            "经济压力": pressure * 0.35,
            "进取心": npc.ambition * 24,
            "转职审慎约束": -18.0 if employed else 0.0,
        }
        desires["JobSearch"] = (
            _score(job_search_parts), job_search_parts,
            str(career_budget_context.get("job_search_reason", "在既有职业集合内寻找机会")),
        )
    if economy_enabled:
        inventory = economy_context.get("inventory", {})
        budget_remaining = (career_budget_context or {}).get("budget_remaining", {})
        budget_allows_shop = not career_budget_enabled or any(
            float(budget_remaining.get(key, 0.0)) > 0 for key in ("food", "learning", "housing")
        )
        shop_parts = {
            "生活补给缺口": 38.0 if inventory.get("prepared_meal", 0) == 0 else 8.0,
            "消费需求": npc.hunger * 0.28 + (100 - npc.energy) * 0.10,
            "技能成长意愿": npc.ambition * 12,
            "住房改善需求": max(0.0, 75 - float(economy_context.get("housing_comfort", 50))) * 0.25,
            "预算约束": 5.0 if economy_context.get("can_shop") else -100.0,
            "已有储备": -8.0 if inventory.get("prepared_meal", 0) >= 2 else 0.0,
            "个人预算余量": 8.0 if budget_allows_shop else -100.0,
        }
        use_parts = {
            "可用物品": 25.0 if economy_context.get("has_usable_item") else -100.0,
            "能量不足": max(0.0, 60 - npc.energy) * 0.45,
            "技能成长意愿": npc.ambition * 14,
            "住房改善需求": max(0.0, 80 - float(economy_context.get("housing_comfort", 50))) * 0.30,
        }
        desires["Shop"] = (_score(shop_parts), shop_parts, "在咖啡馆的社区商店购买需要的物品")
        desires["UseItem"] = (_score(use_parts), use_parts, "使用库存物品改善生活或提升技能")
    if community_enabled:
        weekend = bool(community_context.get("is_weekend"))
        training_budget_allows = not career_budget_enabled or float(
            career_budget_context.get("budget_remaining", {}).get("learning", 0.0)
        ) >= float(community_context.get("training_fee", 0.0))
        desires["Work"][1]["排班时段"] = 62.0 if community_context.get("work_available") else -120.0
        desires["Work"][1]["迟到后的到岗压力"] = 24.0 if community_context.get("is_late") else 0.0
        desires["Work"][1]["当日班次已完成"] = -100.0 if community_context.get("work_completed_today") else 0.0
        desires["Socialize"][1]["周末社交节奏"] = 14.0 if weekend else 0.0
        desires["Relax"][1]["周末休闲节奏"] = 16.0 if weekend else 0.0
        if economy_enabled:
            desires["Shop"][1]["商店营业状态"] = 10.0 if community_context.get("store_open") else -120.0
            desires["Shop"][1]["商品库存"] = 6.0 if community_context.get("stock_available") else -120.0
        facility_parts = {
            "设施开放与名额": 32.0 if community_context.get("facility_available") else -100.0,
            "恢复需要": (100 - npc.energy) * 0.35 + (100 - npc.mood) * 0.30,
            "周末生活节奏": 12.0 if weekend else 0.0,
        }
        training_parts = {
            "培训开放与名额": 34.0 if community_context.get("training_available") else -100.0,
            "进取心": npc.ambition * 34,
            "职业满意度改善": max(0.0, 72 - npc.work_satisfaction) * 0.25,
            "培训费用": -8.0,
            "周末学习时间": 10.0 if weekend else 0.0,
            "个人学习预算": 8.0 if training_budget_allows else -100.0,
        }
        upgrade = community_context.get("next_housing_upgrade") or {}
        upgrade_parts = {
            "住房升级资格": 38.0 if community_context.get("housing_upgrade_available") else -100.0,
            "住房改善意愿": npc.ambition * 12,
            "升级成本约束": -min(30.0, float(upgrade.get("cost", 0.0)) / max(1.0, npc.money) * 18.0) if upgrade else -60.0,
        }
        desires["UseFacility"] = (_score(facility_parts), facility_parts, "在公园使用每日名额有限的社区设施")
        desires["Train"] = (_score(training_parts), training_parts, "在职业培训中心参加本职培训")
        desires["UpgradeHome"] = (_score(upgrade_parts), upgrade_parts, "通过住房服务台升级现有住房")
    if social_life_enabled:
        belonging = float(social_life_context.get("belonging", 50.0))
        desires["Socialize"][1]["归属感缺口"] = round(max(0.0, 60.0 - belonging) * 0.35, 2)
        desires["Socialize"][1]["关系修复需要"] = 18.0 if social_life_context.get("repair_needed") else 0.0
        desires["Socialize"][1]["共同活动承诺"] = 45.0 if social_life_context.get("commitment_due") else 0.0
    relationship_goal = (goal_context or {}).get("relationship")
    relationship_target = relationship_goal.get("target_npc_id") if relationship_goal else None
    relationship_pressure = _goal_utility(goal_context, "relationship", 40.0)
    allowed_locations = {
        "Sleep": {"Home"},
        "Eat": {"Home", "Cafe"},
        "Work": {"Office"},
        "Socialize": {"Cafe", "Office", "Park"},
        "Relax": {"Home", "Cafe", "Park"},
        "Shop": {"Cafe"},
        "UseItem": {"Home", "Cafe"},
        "JobSearch": {"Home", "Cafe"},
        "UseFacility": {"Park"},
        "Train": {"Office"},
        "UpgradeHome": {"Home"},
        "Idle": set(LOCATIONS),
    }
    candidates: list[Candidate] = []

    direct_actions = ["Sleep", "Eat", "Work", "Socialize", "Relax"]
    if economy_enabled:
        direct_actions.extend(("Shop", "UseItem"))
    if career_budget_enabled:
        direct_actions.append("JobSearch")
    if community_enabled:
        direct_actions.extend(("UseFacility", "Train", "UpgradeHome"))
    direct_actions.append("Idle")
    for action in direct_actions:
        raw, parts, explanation = desires[action]
        available = npc.current_location in allowed_locations[action]
        if action == "Socialize":
            available = available and any(other.id != npc.id for other in occupants[npc.current_location])
            if social_life_enabled and social_life_context.get("commitment_due"):
                partner_id = social_life_context.get("commitment_partner_id")
                available = available and any(other.id == partner_id for other in occupants[npc.current_location])
        elif action == "Shop":
            available = available and bool(economy_context.get("can_shop")) and budget_allows_shop
            if community_enabled:
                available = available and bool(community_context.get("store_open")) and bool(community_context.get("stock_available"))
        elif action == "UseItem":
            available = available and bool(economy_context.get("has_usable_item"))
        elif action == "Work":
            available = available and employed
            if community_enabled:
                available = available and bool(community_context.get("work_available"))
        elif action == "JobSearch":
            available = available and bool(career_budget_context.get("job_search_needed"))
        elif action == "UseFacility":
            available = available and bool(community_context.get("facility_available"))
        elif action == "Train":
            available = available and bool(community_context.get("training_available")) and training_budget_allows
        elif action == "UpgradeHome":
            available = available and bool(community_context.get("housing_upgrade_available"))
        direct_parts = dict(parts)
        if action == "Socialize" and relationship_target is not None:
            target_is_here = any(
                other.id == relationship_target for other in occupants[npc.current_location]
            )
            direct_parts["长期目标：建设重要关系"] = (
                relationship_pressure if target_is_here else 0.0
            )
        if available and action != "Idle":
            direct_parts["地点条件满足"] = 12.0
        if action == npc.current_action and available and action != "Idle":
            direct_parts["行动连续性"] = 5.0
        direct_raw = _score(direct_parts)
        jitter = random_service.uniform(-0.05, 0.05) if available else 0.0
        adjusted = direct_raw * (1 + jitter) if available else -1000.0
        candidates.append(
            Candidate(action, adjusted, direct_raw, available, direct_parts, explanation)
        )

    target_options = {
        "Home": ("Sleep", "Eat", "Relax", *(("UseItem",) if economy_enabled else ()), *(("JobSearch",) if career_budget_enabled else ()), *(("UpgradeHome",) if community_enabled else ())),
        "Office": ("Work", "Socialize", *(("Train",) if community_enabled else ())),
        "Cafe": ("Eat", "Socialize", "Relax", *(("Shop", "UseItem") if economy_enabled else ()), *(("JobSearch",) if career_budget_enabled else ())),
        "Park": ("Socialize", "Relax", *(("UseFacility",) if community_enabled else ())),
    }
    for target, supported_actions in target_options.items():
        action = MOVE_ACTIONS[target]
        available = target != npc.current_location and npc.energy >= 5
        viable: list[tuple[str, float]] = []
        for supported in supported_actions:
            if supported == "Socialize" and not any(other.id != npc.id for other in occupants[target]):
                commitment_target = social_life_enabled and social_life_context.get("commitment_due") and social_life_context.get("commitment_location") == target
                if not commitment_target:
                    continue
            if community_enabled and supported == "Work" and not community_context.get("work_available"):
                continue
            if community_enabled and supported == "Shop" and not (
                community_context.get("store_open") and community_context.get("stock_available")
            ):
                continue
            if community_enabled and supported == "UseFacility" and not community_context.get("facility_available"):
                continue
            if community_enabled and supported == "Train" and not (
                community_context.get("training_available") and training_budget_allows
            ):
                continue
            if community_enabled and supported == "UpgradeHome" and not community_context.get("housing_upgrade_available"):
                continue
            supported_score = desires[supported][0]
            if supported == "Socialize" and relationship_target is not None:
                if any(other.id == relationship_target for other in occupants[target]):
                    supported_score += relationship_pressure
            if supported == "Socialize" and social_life_enabled and social_life_context.get("commitment_location") == target:
                supported_score += 45.0
            viable.append((supported, supported_score))
        enabled_action, enabled_score = max(viable, key=lambda item: item[1]) if viable else ("Idle", 0.0)
        parts = {f"目标行为需求（{ACTION_NAMES_ZH[enabled_action]}）": enabled_score, "移动成本": -12.0}
        if npc.last_move_minute is not None and clock.total_minutes - npc.last_move_minute < 30:
            parts["近期移动冷却"] = -35.0
        if npc.energy < 15:
            parts["低能量移动惩罚"] = -15.0
        raw = _score(parts)
        jitter = random_service.uniform(-0.05, 0.05) if available else 0.0
        adjusted = raw * (1 + jitter) if available else -1000.0
        candidates.append(
            Candidate(
                action,
                adjusted,
                raw,
                available,
                parts,
                f"前往{LOCATION_NAMES_ZH[target]}，以便{ACTION_NAMES_ZH[enabled_action]}",
                target,
            )
        )

    candidates.sort(key=lambda candidate: candidate.score, reverse=True)
    chosen = candidates[0]
    return Decision(
        chosen_action=chosen.action,
        candidates=candidates,
        reason={
            "summary": chosen.explanation,
            "top_contributions": chosen.contributions,
            "raw_score": round(chosen.raw_score, 2),
            "final_score": round(chosen.score, 2),
            "randomness": "确定性效用分数，最多加入 ±5% 的微小扰动",
        },
    )
