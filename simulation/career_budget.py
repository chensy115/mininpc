from __future__ import annotations

import json
from math import ceil
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database.models import (
    CareerDevelopment,
    CareerTransition,
    EconomicTransaction,
    EmploymentProfile,
    Housing,
    ItemDefinition,
    NPC,
    NPCSkill,
    PerformanceReview,
    PersonalBudget,
    WeeklyEconomicReport,
)
from simulation.clock import ClockSnapshot
from simulation.economy import PROFESSIONS, WEEK_MINUTES, add_transaction, profession_definition
from simulation.events import add_event
from simulation.memory import add_memory
from simulation.npc import clamp, clamp_npc_state
from simulation.random_service import RandomService


CAREER_LEVEL_LABELS = {1: "初级", 2: "资深", 3: "高级", 4: "专家", 5: "首席"}
BUDGET_CATEGORIES = ("food", "housing", "learning", "entertainment", "savings")


def _week_start(world_minute: int) -> int:
    return (world_minute // WEEK_MINUTES) * WEEK_MINUTES


def _budget_amounts(npc: NPC, employment: EmploymentProfile, housing: Housing | None) -> dict[str, float]:
    expected_income = employment.base_wage * (10 + npc.discipline * 5)
    rent = housing.weekly_rent if housing else 23.0
    return {
        "food_budget": round(42.0 + npc.id * 2.0, 2),
        "housing_budget": round(rent, 2),
        "learning_budget": round(10.0 + npc.ambition * 12.0, 2),
        "entertainment_budget": round(8.0 + npc.extroversion * 10.0, 2),
        "savings_budget": round(max(8.0, expected_income * (0.10 + npc.discipline * 0.08)), 2),
    }


def ensure_career_budget_data(session: Session, npcs: Iterable[NPC], created_minute: int) -> dict[str, int]:
    """Idempotent V0.6 initialization. Existing V0.1-V0.5 facts are never rewritten."""
    counts = {"careers": 0, "budgets": 0}
    next_review = ((_week_start(created_minute) // WEEK_MINUTES) + 1) * WEEK_MINUTES
    for npc in npcs:
        employment = session.scalar(select(EmploymentProfile).where(EmploymentProfile.npc_id == npc.id))
        if employment is None:
            continue
        career = session.scalar(select(CareerDevelopment).where(CareerDevelopment.npc_id == npc.id))
        if career is None:
            session.add(CareerDevelopment(
                npc_id=npc.id,
                employment_status="employed",
                career_level=1,
                last_review_minute=created_minute,
                next_review_minute=next_review,
                last_transition_reason="从既有职业资料增量建立 V0.6 职业档案",
            ))
            counts["careers"] += 1
        budget = session.scalar(select(PersonalBudget).where(PersonalBudget.npc_id == npc.id))
        if budget is None:
            housing = session.scalar(select(Housing).where(Housing.npc_id == npc.id))
            session.add(PersonalBudget(
                npc_id=npc.id,
                period_start_minute=_week_start(created_minute),
                updated_minute=created_minute,
                **_budget_amounts(npc, employment, housing),
            ))
            counts["budgets"] += 1
    session.flush()
    return counts


def _transaction_totals(session: Session, npc_id: int, start: int, end: int) -> dict[str, float]:
    totals = {"income": 0.0, "food": 0.0, "housing": 0.0, "learning": 0.0, "entertainment": 0.0}
    rows = session.execute(
        select(EconomicTransaction, ItemDefinition)
        .outerjoin(ItemDefinition, ItemDefinition.id == EconomicTransaction.item_id)
        .where(
            EconomicTransaction.npc_id == npc_id,
            EconomicTransaction.world_minute >= start,
            EconomicTransaction.world_minute < end,
        )
        .order_by(EconomicTransaction.id)
    ).all()
    for tx, item in rows:
        if tx.amount > 0:
            totals["income"] += tx.amount
        elif tx.kind == "rent":
            totals["housing"] += -tx.amount
        elif tx.kind == "housing_upgrade":
            totals["housing"] += -tx.amount
        elif tx.kind == "training":
            totals["learning"] += -tx.amount
        elif tx.kind == "purchase":
            category = item.category if item else "food"
            key = category if category in {"food", "learning"} else "housing" if category == "housing" else "entertainment"
            totals[key] += -tx.amount
    return {key: round(value, 2) for key, value in totals.items()}


def _financial_metrics(
    session: Session,
    npc: NPC,
    budget: PersonalBudget,
    career: CareerDevelopment,
    end: int,
) -> dict[str, Any]:
    totals = _transaction_totals(session, npc.id, budget.period_start_minute, end)
    spent = sum(totals[key] for key in ("food", "housing", "learning", "entertainment"))
    saved = round(max(0.0, totals["income"] - spent), 2)
    disposable = round(
        totals["income"] - totals["food"] - totals["housing"] - totals["learning"] - totals["entertainment"], 2
    )
    housing = session.scalar(select(Housing).where(Housing.npc_id == npc.id))
    reasons: list[str] = []
    pressure = 0.0
    if career.employment_status != "employed":
        pressure += 35
        reasons.append("当前处于待业状态")
    if housing and housing.arrears > 0:
        pressure += min(40.0, 15.0 + housing.arrears)
        reasons.append(f"住房欠费 ${housing.arrears:.2f}")
    if totals["food"] > budget.food_budget:
        pressure += min(15.0, (totals["food"] - budget.food_budget) / max(1.0, budget.food_budget) * 30)
        reasons.append("食物支出超过周预算")
    if disposable < 0:
        pressure += min(20.0, -disposable / 3)
        reasons.append("本周可支配收入为负")
    essential_target = budget.food_budget + budget.housing_budget
    if npc.money < essential_target * 0.5:
        pressure += min(20.0, (essential_target * 0.5 - npc.money) / max(1.0, essential_target) * 40)
        reasons.append("现金不足以覆盖半周基本预算")
    if not reasons:
        reasons.append("收入与基本支出目前保持平衡")
    return {
        **totals,
        "saved": saved,
        "disposable_income": disposable,
        "economic_pressure": round(clamp(pressure), 2),
        "reasons": reasons,
    }


def _create_weekly_report(
    session: Session, npc: NPC, budget: PersonalBudget, career: CareerDevelopment, period_end: int
) -> WeeklyEconomicReport:
    existing = session.scalar(select(WeeklyEconomicReport).where(
        WeeklyEconomicReport.npc_id == npc.id,
        WeeklyEconomicReport.period_start_minute == budget.period_start_minute,
    ))
    if existing is not None:
        return existing
    metrics = _financial_metrics(session, npc, budget, career, period_end)
    report = WeeklyEconomicReport(
        npc_id=npc.id,
        period_start_minute=budget.period_start_minute,
        period_end_minute=period_end,
        income=metrics["income"],
        food_spent=metrics["food"],
        housing_spent=metrics["housing"],
        learning_spent=metrics["learning"],
        entertainment_spent=metrics["entertainment"],
        saved=metrics["saved"],
        disposable_income=metrics["disposable_income"],
        economic_pressure=metrics["economic_pressure"],
        reasons_json=json.dumps(metrics["reasons"], ensure_ascii=False),
    )
    session.add(report)
    add_event(
        session, ClockSnapshot(period_end), "ECONOMIC_REPORT",
        f"{npc.name} 的周经济报告：收入 ${metrics['income']:.2f}，结余 ${metrics['saved']:.2f}，经济压力 {metrics['economic_pressure']:.1f}",
        npc_id=npc.id,
        metadata={"income": metrics["income"], "saved": metrics["saved"], "economic_pressure": metrics["economic_pressure"]},
    )
    return report


def _run_review(
    session: Session, npc: NPC, employment: EmploymentProfile, career: CareerDevelopment,
    clock: ClockSnapshot, random_service: RandomService,
) -> PerformanceReview:
    period_start = career.last_review_minute
    wages = session.scalar(select(func.count()).select_from(EconomicTransaction).where(
        EconomicTransaction.npc_id == npc.id,
        EconomicTransaction.kind == "wage",
        EconomicTransaction.world_minute >= period_start,
        EconomicTransaction.world_minute < career.next_review_minute,
    )) or 0
    skill = session.scalar(select(NPCSkill).where(
        NPCSkill.npc_id == npc.id,
        NPCSkill.skill_key == profession_definition(employment.profession_key)["skill"],
    ))
    skill_level = skill.level if skill else 1
    score = round(clamp(
        employment.performance * 0.58 + skill_level * 6 + npc.work_satisfaction * 0.18 + min(12, wages * 1.5)
    ), 2)
    reasons = [
        f"持续工作表现 {employment.performance:.1f}",
        f"本职技能等级 {skill_level}",
        f"周期内完成 {wages} 次有薪工作",
        f"职业满意度 {npc.work_satisfaction:.1f}",
    ]
    old_wage, old_level = employment.base_wage, career.career_level
    outcome = "maintained"
    if score >= 80:
        career.strong_reviews += 1
        career.weak_reviews = 0
        if career.strong_reviews >= 2 and career.career_level < 5:
            career.career_level += 1
            employment.base_wage = round(employment.base_wage * 1.10, 2)
            career.strong_reviews = 0
            outcome = "promotion"
            reasons.append("连续两次优秀评估，达到晋升门槛")
        else:
            employment.base_wage = round(employment.base_wage * 1.03, 2)
            outcome = "raise"
            reasons.append("优秀评估触发 3% 绩效加薪")
    elif score >= 68:
        career.weak_reviews = 0
        employment.base_wage = round(employment.base_wage * 1.015, 2)
        outcome = "raise"
        reasons.append("良好评估触发 1.5% 绩效加薪")
    elif score < 35:
        career.weak_reviews += 1
        career.strong_reviews = 0
        employed_count = session.scalar(select(func.count()).select_from(CareerDevelopment).where(
            CareerDevelopment.employment_status == "employed"
        )) or 0
        total_count = session.scalar(select(func.count()).select_from(CareerDevelopment)) or 1
        safe_floor = max(3, ceil(total_count * 0.6))
        risk = min(0.18, 0.04 + (35 - score) * 0.006)
        reasons.append(f"低表现失业风险 {risk * 100:.1f}%（安全就业下限 {safe_floor} 人）")
        if career.weak_reviews >= 2 and employed_count > safe_floor and random_service.uniform(0, 1) < risk:
            career.employment_status = "unemployed"
            career.unemployment_since_minute = clock.total_minutes
            career.last_transition_reason = "连续低绩效评估后触发有限失业风险"
            outcome = "unemployment"
            session.add(CareerTransition(
                npc_id=npc.id, world_minute=clock.total_minutes, transition_type="unemployment",
                from_profession=employment.profession_key, to_profession=None,
                reason=career.last_transition_reason,
            ))
    else:
        career.strong_reviews = 0
        career.weak_reviews = 0
        reasons.append("评估处于观察区间，工资与职级不变")
    career.reviews_completed += 1
    review = PerformanceReview(
        npc_id=npc.id, world_minute=clock.total_minutes,
        period_start_minute=period_start, period_end_minute=career.next_review_minute,
        score=score, outcome=outcome, wage_before=old_wage, wage_after=employment.base_wage,
        career_level_before=old_level, career_level_after=career.career_level,
        reasons_json=json.dumps(reasons, ensure_ascii=False),
    )
    session.add(review)
    description = f"{npc.name} 完成周期绩效评估：{score:.1f}，结果为 {outcome}"
    add_event(session, clock, "CAREER_REVIEW", description, npc_id=npc.id,
              metadata={"score": score, "outcome": outcome, "wage_before": old_wage, "wage_after": employment.base_wage})
    add_memory(session, clock, npc.id, description.replace(npc.name, "我", 1), importance=5,
               emotion="negative" if outcome == "unemployment" else "positive" if outcome in {"raise", "promotion"} else "neutral")
    return review


def process_career_budget_cycles(
    session: Session, npcs: Iterable[NPC], clock: ClockSnapshot, random_service: RandomService
) -> dict[str, int]:
    counts = {"reports": 0, "reviews": 0}
    for npc in npcs:
        employment = session.scalar(select(EmploymentProfile).where(EmploymentProfile.npc_id == npc.id))
        career = session.scalar(select(CareerDevelopment).where(CareerDevelopment.npc_id == npc.id))
        budget = session.scalar(select(PersonalBudget).where(PersonalBudget.npc_id == npc.id))
        if employment is None or career is None or budget is None:
            continue
        while budget.period_start_minute + WEEK_MINUTES <= clock.total_minutes:
            period_end = budget.period_start_minute + WEEK_MINUTES
            _create_weekly_report(session, npc, budget, career, period_end)
            counts["reports"] += 1
            budget.period_start_minute = period_end
            budget.updated_minute = clock.total_minutes
            housing = session.scalar(select(Housing).where(Housing.npc_id == npc.id))
            for key, value in _budget_amounts(npc, employment, housing).items():
                setattr(budget, key, value)
        while career.next_review_minute <= clock.total_minutes:
            _run_review(session, npc, employment, career, clock, random_service)
            counts["reviews"] += 1
            career.last_review_minute = career.next_review_minute
            career.next_review_minute += WEEK_MINUTES
    return counts


def career_budget_context(session: Session, npc: NPC, world_minute: int) -> dict[str, Any]:
    employment = session.scalar(select(EmploymentProfile).where(EmploymentProfile.npc_id == npc.id))
    career = session.scalar(select(CareerDevelopment).where(CareerDevelopment.npc_id == npc.id))
    budget = session.scalar(select(PersonalBudget).where(PersonalBudget.npc_id == npc.id))
    if employment is None or career is None or budget is None:
        return {"enabled": False}
    metrics = _financial_metrics(session, npc, budget, career, world_minute + 1)
    remaining = {
        "food": round(budget.food_budget - metrics["food"], 2),
        "housing": round(budget.housing_budget - metrics["housing"], 2),
        "learning": round(budget.learning_budget - metrics["learning"], 2),
        "entertainment": round(budget.entertainment_budget - metrics["entertainment"], 2),
        "savings": round(budget.savings_budget - metrics["saved"], 2),
    }
    return {
        "enabled": True,
        "employment_status": career.employment_status,
        "career_level": career.career_level,
        "economic_pressure": metrics["economic_pressure"],
        "disposable_income": metrics["disposable_income"],
        "budget_remaining": remaining,
        "job_search_needed": career.employment_status != "employed" or npc.work_satisfaction < 42,
        "job_search_reason": "当前待业，需要在既有职业中求职" if career.employment_status != "employed" else "职业满意度偏低，可考虑既有职业内转职",
    }


def complete_job_search(
    session: Session, npc: NPC, clock: ClockSnapshot, random_service: RandomService,
    preferred_profession_key: str | None = None,
) -> dict[str, Any] | None:
    employment = session.scalar(select(EmploymentProfile).where(EmploymentProfile.npc_id == npc.id))
    career = session.scalar(select(CareerDevelopment).where(CareerDevelopment.npc_id == npc.id))
    if employment is None or career is None:
        return None
    career.applications_submitted += 1
    unemployed = career.employment_status != "employed"
    success_chance = min(0.88, 0.38 + npc.discipline * 0.25 + npc.ambition * 0.18 + (0.12 if unemployed else -0.18))
    if random_service.uniform(0, 1) >= success_chance:
        return {"success": False, "reason": f"本次申请未通过（成功机会 {success_chance * 100:.1f}%）"}
    alternatives = [(key, definition) for key, definition in PROFESSIONS.items() if key != employment.profession_key]
    if preferred_profession_key is not None:
        alternatives = [row for row in alternatives if row[0] == preferred_profession_key]
    if not alternatives:
        return {"success": False, "reason": "没有可用的既有职业选项"}
    alternatives.sort(key=lambda row: (
        abs(row[1]["base_wage"] - employment.base_wage) - npc.ambition * row[1]["base_wage"], row[0]
    ))
    top = alternatives[:1] if preferred_profession_key is not None else alternatives[:2]
    selected_key, definition = (
        top[0] if preferred_profession_key is not None
        else top[random_service.randint(0, len(top) - 1)]
    )
    previous = employment.profession_key
    transition_type = "reemployment" if unemployed else "career_change"
    reason = f"{'待业后重新求职' if unemployed else '因职业满意度偏低主动转职'}；仅从既有职业集合选择"
    npc.job = selected_key
    employment.profession_key = selected_key
    employment.employer = definition["employer"]
    employment.base_wage = definition["base_wage"]
    employment.performance = round(clamp(50 + npc.discipline * 18 + npc.ambition * 8), 2)
    career.employment_status = "employed"
    career.unemployment_since_minute = None
    career.career_level = 1
    career.strong_reviews = 0
    career.weak_reviews = 0
    career.last_transition_reason = reason
    skill = session.scalar(select(NPCSkill).where(
        NPCSkill.npc_id == npc.id, NPCSkill.skill_key == definition["skill"]
    ))
    if skill is None:
        session.add(NPCSkill(npc_id=npc.id, skill_key=definition["skill"], level=1, experience=0.0))
    session.add(CareerTransition(
        npc_id=npc.id, world_minute=clock.total_minutes, transition_type=transition_type,
        from_profession=previous, to_profession=selected_key, reason=reason,
    ))
    add_event(session, clock, "CAREER_TRANSITION", f"{npc.name} 从 {previous} 转到 {selected_key}",
              npc_id=npc.id, metadata={"from": previous, "to": selected_key, "reason": reason})
    add_memory(session, clock, npc.id, f"我从 {previous} 转到 {selected_key}", importance=6, emotion="positive")
    return {"success": True, "from_profession": previous, "to_profession": selected_key, "reason": reason}


def career_snapshot(session: Session, npc: NPC, world_minute: int) -> dict[str, Any] | None:
    employment = session.scalar(select(EmploymentProfile).where(EmploymentProfile.npc_id == npc.id))
    career = session.scalar(select(CareerDevelopment).where(CareerDevelopment.npc_id == npc.id))
    budget = session.scalar(select(PersonalBudget).where(PersonalBudget.npc_id == npc.id))
    if employment is None or career is None or budget is None:
        return None
    context = career_budget_context(session, npc, world_minute)
    reviews = list(session.scalars(select(PerformanceReview).where(
        PerformanceReview.npc_id == npc.id
    ).order_by(PerformanceReview.id.desc()).limit(12)))
    transitions = list(session.scalars(select(CareerTransition).where(
        CareerTransition.npc_id == npc.id
    ).order_by(CareerTransition.id.desc()).limit(12)))
    return {
        "npc_id": npc.id,
        "npc_name": npc.name,
        "employment_status": career.employment_status,
        "career_level": career.career_level,
        "career_level_label": CAREER_LEVEL_LABELS.get(career.career_level, str(career.career_level)),
        "reviews_completed": career.reviews_completed,
        "next_review_minute": career.next_review_minute,
        "applications_submitted": career.applications_submitted,
        "last_transition_reason": career.last_transition_reason,
        "current_metrics": {"disposable_income": context["disposable_income"], "economic_pressure": context["economic_pressure"]},
        "reviews": [{
            "id": row.id, "world_minute": row.world_minute, "score": round(row.score, 2),
            "outcome": row.outcome, "wage_before": round(row.wage_before, 2), "wage_after": round(row.wage_after, 2),
            "career_level_before": row.career_level_before, "career_level_after": row.career_level_after,
            "reasons": json.loads(row.reasons_json),
        } for row in reviews],
        "transitions": [{
            "id": row.id, "world_minute": row.world_minute, "type": row.transition_type,
            "from_profession": row.from_profession, "to_profession": row.to_profession, "reason": row.reason,
        } for row in transitions],
    }


def budget_snapshot(session: Session, npc: NPC, world_minute: int) -> dict[str, Any] | None:
    career = session.scalar(select(CareerDevelopment).where(CareerDevelopment.npc_id == npc.id))
    budget = session.scalar(select(PersonalBudget).where(PersonalBudget.npc_id == npc.id))
    if career is None or budget is None:
        return None
    metrics = _financial_metrics(session, npc, budget, career, world_minute + 1)
    allocations = {key: round(getattr(budget, f"{key}_budget"), 2) for key in BUDGET_CATEGORIES}
    actual = {"food": metrics["food"], "housing": metrics["housing"], "learning": metrics["learning"],
              "entertainment": metrics["entertainment"], "savings": metrics["saved"]}
    return {
        "npc_id": npc.id, "npc_name": npc.name,
        "period_start_minute": budget.period_start_minute,
        "period_end_minute": budget.period_start_minute + WEEK_MINUTES,
        "allocations": allocations,
        "actual": actual,
        "remaining": {key: round(allocations[key] - actual[key], 2) for key in BUDGET_CATEGORIES},
        "income": metrics["income"], "disposable_income": metrics["disposable_income"],
        "economic_pressure": metrics["economic_pressure"], "pressure_reasons": metrics["reasons"],
    }


def report_snapshots(session: Session, npc_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
    query = select(WeeklyEconomicReport).order_by(WeeklyEconomicReport.period_end_minute.desc(), WeeklyEconomicReport.npc_id)
    if npc_id is not None:
        query = query.where(WeeklyEconomicReport.npc_id == npc_id)
    names = {npc.id: npc.name for npc in session.scalars(select(NPC))}
    rows = list(session.scalars(query.limit(limit)))
    return [{
        "id": row.id, "npc_id": row.npc_id, "npc_name": names.get(row.npc_id),
        "period_start_minute": row.period_start_minute, "period_end_minute": row.period_end_minute,
        "income": round(row.income, 2),
        "spending": {"food": round(row.food_spent, 2), "housing": round(row.housing_spent, 2),
                     "learning": round(row.learning_spent, 2), "entertainment": round(row.entertainment_spent, 2)},
        "saved": round(row.saved, 2), "disposable_income": round(row.disposable_income, 2),
        "economic_pressure": round(row.economic_pressure, 2), "reasons": json.loads(row.reasons_json),
    } for row in rows]
