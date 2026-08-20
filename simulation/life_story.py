from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database.models import (
    CareerDevelopment,
    CareerTransition,
    CausalLink,
    Housing,
    HousingUpgradeRecord,
    LifeMilestone,
    LongTermGoal,
    NarrativeArtifact,
    NarrativeJob,
    NPC,
    NPCSkill,
    PerformanceReview,
    ReplayCheckpoint,
    SocialBond,
    StoryState,
    StorySummary,
    TrainingRecord,
)
from simulation.clock import ClockSnapshot


DAY_MINUTES = 24 * 60
WEEK_MINUTES = 7 * DAY_MINUTES
MONTH_MINUTES = 30 * DAY_MINUTES
IMPORTANT_STAGES = {"close_friend", "trusted"}
V09_TABLE_NAMES = {
    "story_state", "life_milestones", "causal_links", "story_summaries", "replay_checkpoints"
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _next_boundary(now: int, interval: int) -> int:
    return ((now // interval) + 1) * interval


def _current_observations(session: Session, npcs: Iterable[NPC], now: int) -> dict[str, Any]:
    result: dict[str, Any] = {}
    bonds = list(session.scalars(select(SocialBond).order_by(SocialBond.id)))
    for npc in npcs:
        career = session.scalar(select(CareerDevelopment).where(CareerDevelopment.npc_id == npc.id))
        housing = session.scalar(select(Housing).where(Housing.npc_id == npc.id))
        skills = {
            row.skill_key: row.level
            for row in session.scalars(select(NPCSkill).where(NPCSkill.npc_id == npc.id).order_by(NPCSkill.skill_key))
        }
        important = {
            f"{row.npc_low_id}:{row.npc_high_id}": row.stage
            for row in bonds
            if npc.id in {row.npc_low_id, row.npc_high_id} and row.stage in IMPORTANT_STAGES
        }
        result[str(npc.id)] = {
            "money": round(npc.money, 2),
            "job": npc.job,
            "career_level": career.career_level if career else None,
            "employment_status": career.employment_status if career else None,
            "housing_tier": housing.tier if housing else None,
            "arrears": round(housing.arrears, 2) if housing else None,
            "arrears_since": now if housing and housing.arrears > 0 else None,
            "skills": skills,
            "important_bonds": important,
            "last_review_id": session.scalar(select(func.max(PerformanceReview.id)).where(PerformanceReview.npc_id == npc.id)) or 0,
            "last_transition_id": session.scalar(select(func.max(CareerTransition.id)).where(CareerTransition.npc_id == npc.id)) or 0,
            "last_housing_upgrade_id": session.scalar(select(func.max(HousingUpgradeRecord.id)).where(HousingUpgradeRecord.npc_id == npc.id)) or 0,
            "last_training_id": session.scalar(select(func.max(TrainingRecord.id)).where(TrainingRecord.npc_id == npc.id)) or 0,
        }
    return result


def ensure_life_story_data(session: Session, npcs: Iterable[NPC], created_minute: int) -> dict[str, int]:
    """Start at the current V0.8 facts without inventing any historical milestone."""
    if session.get(StoryState, 1) is not None:
        return {"states": 0}
    npc_list = list(npcs)
    session.add(
        StoryState(
            id=1,
            initialized_minute=created_minute,
            last_processed_minute=created_minute,
            next_week_summary_minute=_next_boundary(created_minute, WEEK_MINUTES),
            next_month_summary_minute=_next_boundary(created_minute, MONTH_MINUTES),
            next_replay_minute=_next_boundary(created_minute, DAY_MINUTES),
            observations_json=_json(_current_observations(session, npc_list, created_minute)),
            updated_minute=created_minute,
        )
    )
    session.flush()
    return {"states": 1}


def _fact(key: str, value: Any) -> dict[str, Any]:
    return {"key": key, "value": value}


def _add_milestone(
    session: Session,
    *,
    key: str,
    npc_id: int,
    milestone_type: str,
    world_minute: int,
    title: str,
    facts: list[dict[str, Any]],
    source_type: str,
    source_id: int | None,
    rule: dict[str, Any],
    causes: list[dict[str, Any]],
) -> LifeMilestone | None:
    if session.scalar(select(LifeMilestone).where(LifeMilestone.milestone_key == key)) is not None:
        return None
    payload = {
        "npc_id": npc_id,
        "milestone_type": milestone_type,
        "world_minute": world_minute,
        "facts": facts,
        "source_type": source_type,
        "source_id": source_id,
        "rule": rule,
    }
    milestone = LifeMilestone(
        milestone_key=key,
        npc_id=npc_id,
        milestone_type=milestone_type,
        world_minute=world_minute,
        title=title,
        facts_json=_json(facts),
        source_type=source_type,
        source_id=source_id,
        rule_json=_json(rule),
        fact_digest=_digest(payload),
    )
    session.add(milestone)
    session.flush()
    for sequence, cause in enumerate(causes, start=1):
        session.add(
            CausalLink(
                milestone_id=milestone.id,
                sequence=sequence,
                cause_type=cause["cause_type"],
                source_id=cause.get("source_id"),
                description=cause["description"],
                fact_json=_json(cause.get("fact", {})),
            )
        )
    return milestone


def _process_source_records(
    session: Session, npc: NPC, old: dict[str, Any], current: dict[str, Any]
) -> int:
    created = 0
    reviews = list(session.scalars(select(PerformanceReview).where(
        PerformanceReview.npc_id == npc.id,
        PerformanceReview.id > int(old.get("last_review_id", 0)),
    ).order_by(PerformanceReview.id)))
    for row in reviews:
        if row.outcome != "promotion":
            continue
        created += _add_milestone(
            session, key=f"promotion:{row.id}", npc_id=npc.id, milestone_type="promotion",
            world_minute=row.world_minute, title=f"{npc.name} 获得晋升",
            facts=[_fact("career_level_before", row.career_level_before), _fact("career_level_after", row.career_level_after), _fact("review_score", row.score)],
            source_type="performance_review", source_id=row.id,
            rule={"rule": "连续优秀评估并提升职业等级", "minimum_level_delta": 1},
            causes=[{"cause_type": "performance_review", "source_id": row.id, "description": "周期绩效评估达到晋升规则", "fact": {"score": row.score, "outcome": row.outcome}}],
        ) is not None

    transitions = list(session.scalars(select(CareerTransition).where(
        CareerTransition.npc_id == npc.id,
        CareerTransition.id > int(old.get("last_transition_id", 0)),
    ).order_by(CareerTransition.id)))
    transition_titles = {
        "unemployment": "进入待业阶段",
        "reemployment": "重新就业",
        "career_change": "完成职业转换",
    }
    for row in transitions:
        if row.transition_type not in transition_titles:
            continue
        facts = [_fact("from_profession", row.from_profession), _fact("to_profession", row.to_profession)]
        created += _add_milestone(
            session, key=f"career-transition:{row.id}", npc_id=npc.id,
            milestone_type=row.transition_type, world_minute=row.world_minute,
            title=f"{npc.name} {transition_titles[row.transition_type]}", facts=facts,
            source_type="career_transition", source_id=row.id,
            rule={"rule": "只接受已提交的有限职业转换记录"},
            causes=[{"cause_type": "career_transition", "source_id": row.id, "description": row.reason, "fact": {"transition_type": row.transition_type}}],
        ) is not None

    upgrades = list(session.scalars(select(HousingUpgradeRecord).where(
        HousingUpgradeRecord.npc_id == npc.id,
        HousingUpgradeRecord.id > int(old.get("last_housing_upgrade_id", 0)),
    ).order_by(HousingUpgradeRecord.id)))
    for row in upgrades:
        created += _add_milestone(
            session, key=f"housing:{row.id}", npc_id=npc.id, milestone_type="housing_change",
            world_minute=row.world_minute, title=f"{npc.name} 的住房升级为 {row.tier_after}",
            facts=[_fact("tier_before", row.tier_before), _fact("tier_after", row.tier_after), _fact("cost", row.cost)],
            source_type="housing_upgrade", source_id=row.id,
            rule={"rule": "住房等级发生已审计的有限升级"},
            causes=[{"cause_type": "housing_upgrade", "source_id": row.id, "description": "住房服务台完成升级并提交费用", "fact": {"cost": row.cost, "comfort_after": row.comfort_after}}],
        ) is not None

    trainings = list(session.scalars(select(TrainingRecord).where(
        TrainingRecord.npc_id == npc.id,
        TrainingRecord.id > int(old.get("last_training_id", 0)),
        TrainingRecord.leveled_up.is_(True),
    ).order_by(TrainingRecord.id)))
    for row in trainings:
        level = int(current.get("skills", {}).get(row.skill_key, 1))
        created += _add_milestone(
            session, key=f"skill:{npc.id}:{row.skill_key}:{level}", npc_id=npc.id,
            milestone_type="skill_upgrade", world_minute=row.world_minute,
            title=f"{npc.name} 的 {row.skill_key} 技能升至 {level} 级",
            facts=[_fact("skill_key", row.skill_key), _fact("level_after", level), _fact("training_experience", row.skill_experience)],
            source_type="training_record", source_id=row.id,
            rule={"rule": "技能经验跨越固定升级阈值"},
            causes=[{"cause_type": "training", "source_id": row.id, "description": "职业培训增加了已提交的技能经验", "fact": {"experience": row.skill_experience}}],
        ) is not None
    return created


def _process_observed_transitions(
    session: Session, npc: NPC, old: dict[str, Any], current: dict[str, Any], now: int
) -> int:
    created = 0
    for skill_key, level in sorted(current.get("skills", {}).items()):
        before = old.get("skills", {}).get(skill_key)
        if before is None or level <= before:
            continue
        created += _add_milestone(
            session, key=f"skill:{npc.id}:{skill_key}:{level}", npc_id=npc.id,
            milestone_type="skill_upgrade", world_minute=now,
            title=f"{npc.name} 的 {skill_key} 技能升至 {level} 级",
            facts=[_fact("skill_key", skill_key), _fact("level_before", before), _fact("level_after", level)],
            source_type="npc_skill", source_id=None,
            rule={"rule": "持久化技能等级相对上次 Engine 观察值上升"},
            causes=[{"cause_type": "skill_experience", "description": "工作、训练或已提交物品使用使技能经验越过固定阈值", "fact": {"level_before": before, "level_after": level}}],
        ) is not None

    goals = list(session.scalars(select(LongTermGoal).where(
        LongTermGoal.npc_id == npc.id, LongTermGoal.goal_type == "savings"
    ).order_by(LongTermGoal.id)))
    for goal in goals:
        if float(old.get("money", npc.money)) < goal.target_value <= npc.money:
            created += _add_milestone(
                session, key=f"savings:{goal.id}", npc_id=npc.id,
                milestone_type="savings_achieved", world_minute=now,
                title=f"{npc.name} 达成储蓄目标 ${goal.target_value:.2f}",
                facts=[_fact("target", goal.target_value), _fact("balance", round(npc.money, 2))],
                source_type="long_term_goal", source_id=goal.id,
                rule={"rule": "余额从目标以下跨越已提交的储蓄目标"},
                causes=[{"cause_type": "balance_crossing", "source_id": goal.id, "description": "持久余额达到目标且此前观察值低于目标", "fact": {"previous_balance": old.get("money"), "current_balance": round(npc.money, 2)}}],
            ) is not None

    old_bonds = old.get("important_bonds", {})
    for pair, stage in sorted(current.get("important_bonds", {}).items()):
        if old_bonds.get(pair) == stage:
            continue
        low, high = (int(value) for value in pair.split(":"))
        bond = session.scalar(select(SocialBond).where(
            SocialBond.npc_low_id == low, SocialBond.npc_high_id == high
        ))
        other_id = high if npc.id == low else low
        created += _add_milestone(
            session, key=f"friendship:{npc.id}:{pair}:{stage}", npc_id=npc.id,
            milestone_type="important_friendship", world_minute=now,
            title=f"{npc.name} 建立了重要友谊（{stage}）",
            facts=[_fact("other_npc_id", other_id), _fact("stage", stage), _fact("trust", round(bond.trust, 2) if bond else None)],
            source_type="social_bond", source_id=bond.id if bond else None,
            rule={"rule": "双向关系进入 close_friend 或 trusted 阶段"},
            causes=[{"cause_type": "bidirectional_relationship", "source_id": bond.id if bond else None, "description": "双向关系和信任共同达到重要友谊门槛", "fact": {"stage": stage, "trust": round(bond.trust, 2) if bond else None}}],
        ) is not None

    arrears = current.get("arrears")
    arrears_since = old.get("arrears_since")
    if arrears is not None and arrears > 0:
        current["arrears_since"] = arrears_since if arrears_since is not None else now
        since = int(current["arrears_since"])
        if now - since >= WEEK_MINUTES:
            housing = session.scalar(select(Housing).where(Housing.npc_id == npc.id))
            created += _add_milestone(
                session, key=f"persistent-arrears:{npc.id}:{since}", npc_id=npc.id,
                milestone_type="persistent_arrears", world_minute=now,
                title=f"{npc.name} 的住房欠费已持续一周",
                facts=[_fact("arrears", arrears), _fact("since_minute", since), _fact("duration_minutes", now - since)],
                source_type="housing", source_id=housing.id if housing else None,
                rule={"rule": "住房欠费连续保持至少 10080 分钟", "minimum_minutes": WEEK_MINUTES},
                causes=[{"cause_type": "housing_arrears", "source_id": housing.id if housing else None, "description": "已提交住房欠费在连续观察中未清偿", "fact": {"arrears": arrears, "duration_minutes": now - since}}],
            ) is not None
    else:
        current["arrears_since"] = None
    return created


def _queue_summary_narrative(session: Session, summary: StorySummary) -> None:
    dedupe = f"story-summary:{summary.summary_key}"
    if session.scalar(select(NarrativeJob).where(NarrativeJob.dedupe_key == dedupe)) is not None:
        return
    context = {
        "summary_id": summary.id,
        "period_type": summary.period_type,
        "period_start_minute": summary.period_start_minute,
        "period_end_minute": summary.period_end_minute,
        "facts": json.loads(summary.facts_json),
        "fact_digest": summary.fact_digest,
    }
    session.add(NarrativeJob(
        kind="story_summary", dedupe_key=dedupe, context_json=_json(context),
        status="pending", attempts=0, created_minute=summary.created_minute,
    ))


def _create_summary(session: Session, period_type: str, start: int, end: int, created_minute: int) -> StorySummary:
    key = f"{period_type}:{start}:{end}"
    existing = session.scalar(select(StorySummary).where(StorySummary.summary_key == key))
    if existing is not None:
        return existing
    milestones = list(session.scalars(select(LifeMilestone).where(
        LifeMilestone.world_minute >= start, LifeMilestone.world_minute < end
    ).order_by(LifeMilestone.world_minute, LifeMilestone.id)))
    facts: list[dict[str, Any]] = [{
        "fact_type": "period",
        "period_type": period_type,
        "period_start_minute": start,
        "period_end_minute": end,
        "milestone_count": len(milestones),
    }]
    facts.extend({
        "fact_type": "milestone", "milestone_id": row.id, "npc_id": row.npc_id,
        "milestone_type": row.milestone_type, "world_minute": row.world_minute,
        "title": row.title, "fact_digest": row.fact_digest,
    } for row in milestones)
    summary = StorySummary(
        summary_key=key, period_type=period_type, period_start_minute=start,
        period_end_minute=end, facts_json=_json(facts),
        milestone_ids_json=_json([row.id for row in milestones]),
        fact_digest=_digest(facts), created_minute=created_minute,
    )
    session.add(summary)
    session.flush()
    _queue_summary_narrative(session, summary)
    return summary


def _create_checkpoint(
    session: Session, start: int, end: int, seed: int, random_counter: int, created_minute: int
) -> ReplayCheckpoint:
    key = f"daily:{start}:{end}:seed:{seed}"
    existing = session.scalar(select(ReplayCheckpoint).where(ReplayCheckpoint.checkpoint_key == key))
    if existing is not None:
        return existing
    milestones = list(session.scalars(select(LifeMilestone).where(
        LifeMilestone.world_minute >= start, LifeMilestone.world_minute < end
    ).order_by(LifeMilestone.world_minute, LifeMilestone.id)))
    summaries = list(session.scalars(select(StorySummary).where(
        StorySummary.period_end_minute > start, StorySummary.period_end_minute <= end
    ).order_by(StorySummary.period_end_minute, StorySummary.id)))
    snapshot = {
        "seed": seed, "period_start_minute": start, "period_end_minute": end,
        "milestones": [{"id": row.id, "fact_digest": row.fact_digest} for row in milestones],
        "summaries": [{"id": row.id, "fact_digest": row.fact_digest} for row in summaries],
    }
    checkpoint = ReplayCheckpoint(
        checkpoint_key=key, period_start_minute=start, period_end_minute=end,
        seed=seed, random_counter=random_counter,
        milestone_ids_json=_json([row.id for row in milestones]),
        summary_ids_json=_json([row.id for row in summaries]),
        snapshot_json=_json(snapshot), facts_digest=_digest(snapshot), created_minute=created_minute,
    )
    session.add(checkpoint)
    session.flush()
    return checkpoint


def process_life_story_cycles(
    session: Session,
    npcs: Iterable[NPC],
    clock: ClockSnapshot,
    *,
    seed: int,
    random_counter: int,
) -> dict[str, int]:
    """Observe committed facts. This function never reads or advances RandomService."""
    state = session.get(StoryState, 1)
    if state is None:
        return {"milestones": 0, "summaries": 0, "checkpoints": 0}
    now = clock.total_minutes
    npc_list = list(npcs)
    old_all = json.loads(state.observations_json)
    current_all = _current_observations(session, npc_list, now)
    counts = {"milestones": 0, "summaries": 0, "checkpoints": 0}
    for npc in npc_list:
        old = old_all.get(str(npc.id))
        current = current_all.get(str(npc.id))
        if old is None or current is None:
            continue
        counts["milestones"] += _process_source_records(session, npc, old, current)
        counts["milestones"] += _process_observed_transitions(session, npc, old, current, now)

    while state.next_week_summary_minute <= now:
        end = state.next_week_summary_minute
        start = max(state.initialized_minute, end - WEEK_MINUTES)
        _create_summary(session, "week", start, end, now)
        state.next_week_summary_minute += WEEK_MINUTES
        counts["summaries"] += 1
    while state.next_month_summary_minute <= now:
        end = state.next_month_summary_minute
        start = max(state.initialized_minute, end - MONTH_MINUTES)
        _create_summary(session, "month", start, end, now)
        state.next_month_summary_minute += MONTH_MINUTES
        counts["summaries"] += 1
    while state.next_replay_minute <= now:
        end = state.next_replay_minute
        start = max(state.initialized_minute, end - DAY_MINUTES)
        _create_checkpoint(session, start, end, seed, random_counter, now)
        state.next_replay_minute += DAY_MINUTES
        counts["checkpoints"] += 1

    state.last_processed_minute = now
    state.observations_json = _json(current_all)
    state.updated_minute = now
    return counts


def causal_chain_snapshot(session: Session, milestone_id: int) -> dict[str, Any] | None:
    milestone = session.get(LifeMilestone, milestone_id)
    if milestone is None:
        return None
    links = list(session.scalars(select(CausalLink).where(
        CausalLink.milestone_id == milestone_id
    ).order_by(CausalLink.sequence)))
    return {
        "milestone": milestone_snapshot(milestone),
        "causes": [{
            "sequence": row.sequence, "cause_type": row.cause_type,
            "source_id": row.source_id, "description": row.description,
            "fact": json.loads(row.fact_json),
        } for row in links],
    }


def milestone_snapshot(row: LifeMilestone) -> dict[str, Any]:
    clock = ClockSnapshot(row.world_minute)
    return {
        "id": row.id, "npc_id": row.npc_id, "milestone_type": row.milestone_type,
        "world_minute": row.world_minute, "time_label": clock.label, "title": row.title,
        "facts": json.loads(row.facts_json), "source_type": row.source_type,
        "source_id": row.source_id, "rule": json.loads(row.rule_json),
        "fact_digest": row.fact_digest,
    }


def milestone_snapshots(
    session: Session, npc_id: int | None = None, milestone_type: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    query = select(LifeMilestone)
    if npc_id is not None:
        query = query.where(LifeMilestone.npc_id == npc_id)
    if milestone_type is not None:
        query = query.where(LifeMilestone.milestone_type == milestone_type)
    rows = list(session.scalars(query.order_by(LifeMilestone.world_minute.desc(), LifeMilestone.id.desc()).limit(limit)))
    return [milestone_snapshot(row) for row in rows]


def summary_snapshots(session: Session, period_type: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    query = select(StorySummary)
    if period_type is not None:
        query = query.where(StorySummary.period_type == period_type)
    rows = list(session.scalars(query.order_by(StorySummary.period_end_minute.desc(), StorySummary.id.desc()).limit(limit)))
    result = []
    for row in rows:
        dedupe = f"story-summary:{row.summary_key}"
        narrative = session.execute(
            select(NarrativeArtifact, NarrativeJob)
            .join(NarrativeJob, NarrativeArtifact.job_id == NarrativeJob.id)
            .where(NarrativeJob.dedupe_key == dedupe)
        ).first()
        text = None
        if narrative:
            content = json.loads(narrative[0].content_json)
            text = content.get("text")
        result.append({
            "id": row.id, "period_type": row.period_type,
            "period_start_minute": row.period_start_minute,
            "period_end_minute": row.period_end_minute,
            "facts": json.loads(row.facts_json),
            "milestone_ids": json.loads(row.milestone_ids_json),
            "fact_digest": row.fact_digest, "narrative_text": text,
        })
    return result


def replay_story(session: Session, start: int, end: int, seed: int) -> dict[str, Any]:
    if start < 0 or end <= start:
        raise ValueError("回放范围必须满足 0 <= start_minute < end_minute")
    milestones = list(session.scalars(select(LifeMilestone).where(
        LifeMilestone.world_minute >= start, LifeMilestone.world_minute < end
    ).order_by(LifeMilestone.world_minute, LifeMilestone.id)))
    summaries = list(session.scalars(select(StorySummary).where(
        StorySummary.period_end_minute > start, StorySummary.period_end_minute <= end
    ).order_by(StorySummary.period_end_minute, StorySummary.id)))
    checkpoints = list(session.scalars(select(ReplayCheckpoint).where(
        ReplayCheckpoint.period_end_minute > start,
        ReplayCheckpoint.period_start_minute < end,
        ReplayCheckpoint.seed == seed,
    ).order_by(ReplayCheckpoint.period_start_minute, ReplayCheckpoint.id)))
    milestone_data = []
    for row in milestones:
        chain = causal_chain_snapshot(session, row.id)
        milestone_data.append(chain)
    summary_data = [{
        "id": row.id, "period_type": row.period_type,
        "period_start_minute": row.period_start_minute,
        "period_end_minute": row.period_end_minute,
        "facts": json.loads(row.facts_json), "fact_digest": row.fact_digest,
    } for row in summaries]
    payload = {
        "seed": seed, "start_minute": start, "end_minute": end,
        "milestones": milestone_data, "summaries": summary_data,
        "checkpoint_digests": [row.facts_digest for row in checkpoints],
    }
    return {
        "mode": "v0.9", "seed": seed, "start_minute": start, "end_minute": end,
        "milestones": milestone_data, "summaries": summary_data,
        "checkpoints": [{
            "id": row.id, "period_start_minute": row.period_start_minute,
            "period_end_minute": row.period_end_minute, "facts_digest": row.facts_digest,
        } for row in checkpoints],
        "replay_digest": _digest(payload),
    }
