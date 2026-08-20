from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import func, inspect, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.models import (
    AgentCognitionState,
    AgentConversationParticipantResult,
    AgentPlan,
    AgentReflection,
    AgentReflectionSource,
    AgentReflectionTask,
    AgentSubjectiveBelief,
    DecisionLog,
    Event,
    LifeMilestone,
    LongTermGoal,
    Memory,
    NPC,
)
from simulation.agent_brain import AgentSettings
from simulation.clock import ClockSnapshot
from simulation.goals import goal_snapshots


V15_TABLE_NAMES = {
    "agent_cognition_states",
    "agent_reflection_tasks",
    "agent_reflection_sources",
    "agent_reflections",
    "agent_subjective_beliefs",
    "agent_plans",
}
SUPPORTED_NPC_IDS = frozenset({1, 2, 3, 4, 5})
PLAN_ACTIONS = frozenset({
    "GoHome", "GoOffice", "GoCafe", "GoPark", "Sleep", "Eat", "Work", "Relax",
    "Socialize", "Shop", "UseItem", "JobSearch", "UseFacility", "Train", "UpgradeHome",
})
ACTION_EVENT_TYPES = {
    "GoHome": "MOVE", "GoOffice": "MOVE", "GoCafe": "MOVE", "GoPark": "MOVE",
    "Sleep": "SLEEP", "Eat": "EAT", "Work": "WORK", "Relax": "RELAX",
    "Socialize": "SOCIAL", "Shop": "SHOP", "UseItem": "ITEM",
    "JobSearch": "CAREER_SEARCH", "UseFacility": "FACILITY", "Train": "TRAINING",
    "UpgradeHome": "HOUSING_UPGRADE",
}
MOVE_TARGETS = {
    "GoHome": "Home", "GoOffice": "Office", "GoCafe": "Cafe", "GoPark": "Park",
}
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAX_SOURCES = 80


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() not in {"0", "false", "no", "off", ""}


def cognition_enabled_from_env() -> bool:
    return _env_bool("MINIWORLD_AGENT_COGNITION_ENABLED", False)


def cognition_npc_ids_from_env() -> set[int]:
    if _env_bool("MINIWORLD_AGENT_COGNITION_ALL_ENABLED", False):
        return set(SUPPORTED_NPC_IDS)
    names = {"alice": 1, "bob": 2, "charlie": 3, "diana": 4, "eric": 5}
    result: set[int] = set()
    for token in (part.strip().lower() for part in os.getenv("MINIWORLD_AGENT_COGNITION_NPCS", "").split(",")):
        if not token:
            continue
        try:
            npc_id = int(token)
        except ValueError:
            npc_id = names.get(token, 0)
        if npc_id in SUPPORTED_NPC_IDS:
            result.add(npc_id)
    if cognition_enabled_from_env():
        result.update(SUPPORTED_NPC_IDS)
    return result


def _bounded_int(name: str, default: int, low: int, high: int) -> int:
    try:
        return min(high, max(low, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


def _bounded_float(name: str, default: float, low: float, high: float) -> float:
    try:
        return min(high, max(low, float(os.getenv(name, str(default)))))
    except ValueError:
        return default


@dataclass(frozen=True)
class CognitionSettings:
    timeout_seconds: float = 8.0
    max_concurrency: int = 3
    queue_limit: int = 15
    lease_seconds: float = 12.0
    max_reflections_per_day: int = 2

    @classmethod
    def from_env(cls, agent: AgentSettings | None = None) -> "CognitionSettings":
        shared = agent or AgentSettings.from_env()
        timeout = _bounded_float("MINIWORLD_COGNITION_TIMEOUT", shared.timeout_seconds, 0.1, 60.0)
        return cls(
            timeout_seconds=timeout,
            max_concurrency=_bounded_int("MINIWORLD_COGNITION_MAX_CONCURRENCY", 3, 1, 5),
            queue_limit=_bounded_int("MINIWORLD_COGNITION_QUEUE_LIMIT", 15, 5, 25),
            lease_seconds=max(timeout + 2.0, _bounded_float("MINIWORLD_COGNITION_LEASE_SECONDS", 12.0, 2.0, 90.0)),
            max_reflections_per_day=_bounded_int("MINIWORLD_COGNITION_DAILY_LIMIT", 2, 1, 3),
        )


def _clean_text(value: str, maximum: int) -> str:
    cleaned = _CONTROL_RE.sub("", value).strip()
    if cleaned != value.strip():
        raise ValueError("control characters are not accepted")
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"text must contain 1-{maximum} characters")
    return cleaned


class BeliefUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    target: str = Field(min_length=1, max_length=100)
    belief: str = Field(min_length=1, max_length=280)
    evidence_ids: list[str] = Field(min_length=1, max_length=4)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("target")
    @classmethod
    def clean_target(cls, value: str) -> str:
        return _clean_text(value, 100)

    @field_validator("belief")
    @classmethod
    def clean_belief(cls, value: str) -> str:
        return _clean_text(value, 280)

    @field_validator("evidence_ids")
    @classmethod
    def clean_evidence(cls, value: list[str]) -> list[str]:
        return [_clean_text(item, 120) for item in value]


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    goal_key: str = Field(min_length=1, max_length=80)
    action_category: str = Field(min_length=1, max_length=40)
    target: str | None = Field(default=None, max_length=100)
    description: str = Field(min_length=1, max_length=240)
    start_in_days: int = Field(ge=0, le=7)
    end_in_days: int = Field(ge=1, le=30)
    evidence_ids: list[str] = Field(min_length=1, max_length=4)

    @field_validator("goal_key")
    @classmethod
    def clean_goal(cls, value: str) -> str:
        return _clean_text(value, 80)

    @field_validator("action_category")
    @classmethod
    def clean_action(cls, value: str) -> str:
        return _clean_text(value, 40)

    @field_validator("target")
    @classmethod
    def clean_target(cls, value: str | None) -> str | None:
        return None if value is None else _clean_text(value, 100)

    @field_validator("description")
    @classmethod
    def clean_description(cls, value: str) -> str:
        return _clean_text(value, 240)

    @field_validator("evidence_ids")
    @classmethod
    def clean_evidence(cls, value: list[str]) -> list[str]:
        return [_clean_text(item, 120) for item in value]

    @model_validator(mode="after")
    def ordered_window(self) -> "PlanStep":
        if self.end_in_days < self.start_in_days:
            raise ValueError("plan window is reversed")
        return self


class PlanAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    plan_id: int = Field(gt=0)
    operation: Literal["cancel", "extend"]
    extend_days: int = Field(default=0, ge=0, le=14)
    reason: str = Field(min_length=1, max_length=200)
    evidence_ids: list[str] = Field(min_length=1, max_length=4)

    @field_validator("reason")
    @classmethod
    def clean_reason(cls, value: str) -> str:
        return _clean_text(value, 200)

    @field_validator("evidence_ids")
    @classmethod
    def clean_evidence(cls, value: list[str]) -> list[str]:
        return [_clean_text(item, 120) for item in value]

    @model_validator(mode="after")
    def valid_extension(self) -> "PlanAdjustment":
        if (self.operation == "extend") != (self.extend_days > 0):
            raise ValueError("extend_days must be positive only for extend")
        return self


class ReflectionOutput(BaseModel):
    """Only short, auditable conclusions; hidden reasoning is never requested or stored."""

    model_config = ConfigDict(extra="forbid", strict=True)
    day_summary: str = Field(min_length=1, max_length=500)
    emotion_summary: str = Field(min_length=1, max_length=160)
    lessons: list[str] = Field(min_length=1, max_length=4)
    goal_focus: str = Field(min_length=1, max_length=80)
    belief_updates: list[BeliefUpdate] = Field(default_factory=list, max_length=3)
    plan_steps: list[PlanStep] = Field(min_length=1, max_length=3)
    plan_adjustments: list[PlanAdjustment] = Field(default_factory=list, max_length=3)
    reason_summary: str = Field(min_length=1, max_length=500)

    @field_validator("day_summary", "reason_summary")
    @classmethod
    def clean_long_text(cls, value: str) -> str:
        return _clean_text(value, 500)

    @field_validator("emotion_summary")
    @classmethod
    def clean_emotion(cls, value: str) -> str:
        return _clean_text(value, 160)

    @field_validator("goal_focus")
    @classmethod
    def clean_goal_focus(cls, value: str) -> str:
        return _clean_text(value, 80)

    @field_validator("lessons")
    @classmethod
    def clean_lessons(cls, value: list[str]) -> list[str]:
        return [_clean_text(item, 180) for item in value]


class ReflectionProvider(Protocol):
    name: str

    async def generate(self, context: dict[str, Any]) -> str: ...


class OpenAICompatibleReflectionProvider:
    name = "openai-compatible"

    def __init__(self, settings: AgentSettings) -> None:
        self.settings = settings

    async def generate(self, context: dict[str, Any]) -> str:
        name = context["self"]["name"]
        system = (
            f"You produce {name}'s bounded daily reflection for MiniWorld V1.5. Return one JSON object only. "
            "Use only supplied first-person sources and stable evidence IDs. Beliefs are subjective, plans are "
            "non-executable intentions, and goal_focus must be an offered goal_key. Do not invent facts, expose "
            "hidden chain-of-thought, request tools, or modify world state. Keys must be exactly day_summary, "
            "emotion_summary, lessons, goal_focus, belief_updates, plan_steps, plan_adjustments, reason_summary."
        )
        payload = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": "Return strict JSON from this bounded context:\n" + json.dumps(context, ensure_ascii=False, separators=(",", ":"))},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": 1100,
        }
        try:
            async with httpx.AsyncClient(timeout=self.settings.timeout_seconds) as client:
                response = await client.post(
                    f"{self.settings.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.settings.api_key}"},
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
            content = body["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty_response")
            return content
        except httpx.TimeoutException as exc:
            raise TimeoutError from exc


@dataclass(frozen=True)
class ReflectionGenerationResult:
    output: ReflectionOutput
    provider: str
    model: str | None
    fallback_used: bool
    failure_reason: str | None


def _fallback_output(context: dict[str, Any], reason: str | None) -> ReflectionOutput:
    person = context["self"]
    goals = context["own_goals"]
    goal = max(goals, key=lambda item: (item["priority"], item["goal_key"]))
    sources = context["sources"]
    evidence = [sources[-1]["source_id"]] if sources else [goal["source_id"]]
    goal_type = goal["type"]
    action = "Work" if goal_type in {"savings", "career_satisfaction"} else "Socialize"
    target = None
    if action == "Socialize":
        allowed = context["allowed_plan_targets"].get("Socialize", [])
        if allowed:
            target = allowed[0]
        else:
            action = "Relax"
    traits = person["personality"]
    tone = "稳健" if traits["discipline"] >= 0.7 else "灵活"
    observed = sources[-1]["summary"] if sources else "今天没有出现需要特别记录的新事实"
    belief_target = target or f"goal:{goal['goal_key']}"
    return ReflectionOutput(
        day_summary=f"我回顾了今天亲历或被告知的记录：{observed}"[:500],
        emotion_summary=f"我保持{tone}，情绪状态为{person['emotion']}。",
        lessons=["只依据自己可见且已提交的事实调整下一步。"],
        goal_focus=goal["goal_key"],
        belief_updates=[BeliefUpdate(
            target=belief_target,
            belief=f"我主观认为继续关注“{goal['label']}”符合当前需要。",
            evidence_ids=evidence,
            confidence=round(0.55 + float(traits["discipline"]) * 0.2, 2),
        )],
        plan_steps=[PlanStep(
            goal_key=goal["goal_key"], action_category=action, target=target,
            description=f"用一次真实的{action}行动推进“{goal['label']}”。",
            start_in_days=0, end_in_days=3, evidence_ids=evidence,
        )],
        plan_adjustments=[],
        reason_summary=f"人格化安全回退；原因：{reason or 'provider_unavailable'}。",
    )


class ReflectionGenerator:
    def __init__(
        self,
        agent_settings: AgentSettings | None = None,
        provider: ReflectionProvider | None = None,
        settings: CognitionSettings | None = None,
    ) -> None:
        self.agent_settings = agent_settings or AgentSettings.from_env()
        self.settings = settings or CognitionSettings.from_env(self.agent_settings)
        self.provider = provider
        if self.provider is None and self.agent_settings.api_key and self.agent_settings.model:
            self.provider = OpenAICompatibleReflectionProvider(self.agent_settings)

    def status(self) -> dict[str, Any]:
        if self.provider is None:
            reason = "missing_api_key" if not self.agent_settings.api_key else "missing_model"
            return {"available": False, "provider": None, "model": self.agent_settings.model, "reason": reason}
        return {"available": True, "provider": self.provider.name, "model": self.agent_settings.model, "reason": None}

    async def generate(self, context: dict[str, Any]) -> ReflectionGenerationResult:
        if self.provider is None:
            reason = self.status()["reason"]
            return ReflectionGenerationResult(_fallback_output(context, reason), "deterministic-personality", None, True, reason)
        try:
            if getattr(self.provider, "manages_timeout", False):
                raw = await self.provider.generate(context)
            else:
                raw = await asyncio.wait_for(
                    self.provider.generate(context), timeout=self.settings.timeout_seconds + 0.25
                )
            value = json.loads(raw)
            output = ReflectionOutput.model_validate(value)
            return ReflectionGenerationResult(output, self.provider.name, self.agent_settings.model, False, None)
        except TimeoutError:
            reason = "timeout"
        except json.JSONDecodeError:
            reason = "invalid_json"
        except ValidationError:
            reason = "schema_validation_failed"
        except Exception:
            reason = "provider_error"
        return ReflectionGenerationResult(_fallback_output(context, reason), "deterministic-personality", None, True, reason)


def _safe(value: Any, maximum: int) -> str:
    return _CONTROL_RE.sub("", str(value)).strip()[:maximum]


def _digest(value: Any) -> tuple[str, str]:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def ensure_cognition_states(session: Session, npc_ids: Iterable[int], now_minute: int) -> int:
    existing = set(session.scalars(select(AgentCognitionState.npc_id)))
    valid = set(session.scalars(select(NPC.id).where(NPC.id.in_(set(npc_ids)))))
    created = 0
    for npc_id in sorted(valid & set(SUPPORTED_NPC_IDS)):
        if npc_id not in existing:
            session.add(AgentCognitionState(npc_id=npc_id, updated_minute=now_minute))
            created += 1
    return created


def _source_payloads(
    session: Session, npc: NPC, reflection_day: int, start: int, end: int,
    trigger_type: str, trigger_source_id: int | None,
) -> tuple[list[dict[str, Any]], set[int]]:
    payloads: list[dict[str, Any]] = []
    known_npcs: set[int] = set()

    def add(source_key: str, source_type: str, row_id: int | None, summary: str) -> None:
        if len(payloads) >= MAX_SOURCES:
            return
        payloads.append({
            "source_id": source_key, "source_type": source_type, "source_row_id": row_id,
            "summary": _safe(summary, 320), "range_start_minute": start, "range_end_minute": end,
        })

    for row in session.scalars(select(Event).where(
        or_(Event.npc_id == npc.id, Event.target_npc_id == npc.id),
        Event.world_day == reflection_day,
    ).order_by(Event.id).limit(35)):
        other_id = row.target_npc_id if row.npc_id == npc.id else row.npc_id
        if other_id is not None and other_id != npc.id:
            known_npcs.add(other_id)
        add(f"event:{row.id}", "event", row.id, row.description)
    for row in session.scalars(select(Memory).where(
        Memory.npc_id == npc.id, Memory.timestamp >= start, Memory.timestamp < end,
    ).order_by(Memory.timestamp, Memory.id).limit(20)):
        if row.related_npc_id is not None:
            known_npcs.add(row.related_npc_id)
        add(f"memory:{row.id}", "private_memory", row.id, row.content)
    for row in session.scalars(select(AgentConversationParticipantResult).where(
        AgentConversationParticipantResult.npc_id == npc.id,
        AgentConversationParticipantResult.settled_minute >= start,
        AgentConversationParticipantResult.settled_minute < end,
    ).order_by(AgentConversationParticipantResult.id).limit(10)):
        known_npcs.add(row.related_npc_id)
        add(f"conversation:{row.conversation_id}", "own_conversation", row.conversation_id, row.subjective_summary)
    for row in session.scalars(select(DecisionLog).where(
        DecisionLog.npc_id == npc.id, DecisionLog.world_day == reflection_day,
    ).order_by(DecisionLog.id).limit(10)):
        add(f"decision:{row.id}", "own_decision", row.id, f"我在 {row.world_time} 决定进行 {row.chosen_action}")
    for goal in goal_snapshots(session, npc):
        if goal.get("target_npc_id") is not None:
            known_npcs.add(int(goal["target_npc_id"]))
        add(
            f"goal:{goal['id']}", "own_goal", int(goal["id"]),
            f"{goal['label']}：当前 {goal['current_value']} / 目标 {goal['target_value']}，状态 {goal['status']}",
        )
    for plan in session.scalars(select(AgentPlan).where(
        AgentPlan.npc_id == npc.id, AgentPlan.status.in_(("pending", "in_progress")),
    ).order_by(AgentPlan.id).limit(8)):
        add(f"plan:{plan.id}", "own_plan", plan.id, f"{plan.description}；状态 {plan.status}；截止第 {plan.window_end_day} 天")
    if trigger_type == "milestone" and trigger_source_id is not None:
        milestone = session.get(LifeMilestone, trigger_source_id)
        if milestone is not None and milestone.npc_id == npc.id:
            add(f"milestone:{milestone.id}", "own_milestone", milestone.id, milestone.title)
    return payloads, known_npcs


def _build_context(
    session: Session, task: AgentReflectionTask, npc: NPC, payloads: list[dict[str, Any]], known_npcs: set[int],
) -> dict[str, Any]:
    goals = goal_snapshots(session, npc)
    active_plans = list(session.scalars(select(AgentPlan).where(
        AgentPlan.npc_id == npc.id, AgentPlan.status.in_(("pending", "in_progress")),
    ).order_by(AgentPlan.id).limit(8)))
    allowed_people = list(session.scalars(select(NPC).where(NPC.id.in_(known_npcs)).order_by(NPC.id))) if known_npcs else []
    source_ids = {item["source_id"] for item in payloads}
    own_goals = []
    for goal in goals:
        source_id = f"goal:{goal['id']}"
        if source_id not in source_ids:
            continue
        own_goals.append({
            "source_id": source_id, "goal_key": goal["goal_key"], "type": goal["type"],
            "label": goal["label"], "priority": goal["priority"], "status": goal["status"],
            "current_value": goal["current_value"], "target_value": goal["target_value"],
            "target_npc_id": goal.get("target_npc_id"),
        })
    return {
        "schema_version": "1.5",
        "security": {
            "private_context_owner_npc_id": npc.id,
            "sources_are_committed_visible_facts_only": True,
            "beliefs_are_subjective": True,
            "plans_are_non_executable": True,
            "engine_fact_authority": True,
            "hidden_reasoning_requested": False,
        },
        "reflection": {
            "task_id": task.id, "day": task.reflection_day, "trigger_type": task.trigger_type,
            "range_start_minute": max(0, (task.reflection_day - 1) * 1440),
            "range_end_minute": task.reflection_day * 1440,
        },
        "self": {
            "id": npc.id, "name": npc.name, "job": npc.job,
            "emotion": "愉快" if npc.mood >= 70 else "低落" if npc.mood <= 35 else "平静",
            "personality": {
                "extroversion": npc.extroversion, "kindness": npc.kindness,
                "ambition": npc.ambition, "risk_tolerance": npc.risk_tolerance,
                "discipline": npc.discipline,
            },
        },
        "own_goals": own_goals,
        "active_own_plans": [
            {"plan_id": row.id, "goal_key": row.goal_key, "action_category": row.action_category,
             "target": row.target, "description": row.description, "status": row.status,
             "window_end_day": row.window_end_day}
            for row in active_plans
        ],
        "allowed_plan_actions": sorted(PLAN_ACTIONS),
        "allowed_plan_targets": {
            "Socialize": [f"npc:{row.id}" for row in allowed_people if row.id != npc.id],
        },
        "sources": payloads,
    }


def enqueue_reflection(
    session: Session,
    npc_id: int,
    reflection_day: int,
    created_minute: int,
    settings: CognitionSettings,
    *,
    trigger_type: Literal["daily", "milestone"] = "daily",
    trigger_source_id: int | None = None,
) -> AgentReflectionTask | None:
    if npc_id not in SUPPORTED_NPC_IDS or reflection_day < 1:
        return None
    npc = session.get(NPC, npc_id)
    state = session.scalar(select(AgentCognitionState).where(AgentCognitionState.npc_id == npc_id))
    if npc is None or state is None:
        return None
    dedupe_key = f"{trigger_type}:{npc_id}:{reflection_day}:{trigger_source_id or 0}"
    existing = session.scalar(select(AgentReflectionTask).where(AgentReflectionTask.dedupe_key == dedupe_key))
    if existing is not None:
        return existing
    per_day = session.scalar(select(func.count()).select_from(AgentReflectionTask).where(
        AgentReflectionTask.npc_id == npc_id, AgentReflectionTask.reflection_day == reflection_day,
        AgentReflectionTask.status != "cancelled",
    )) or 0
    active_npc = session.scalar(select(AgentReflectionTask.id).where(
        AgentReflectionTask.npc_id == npc_id,
        AgentReflectionTask.status.in_(("pending", "processing")),
    ).limit(1))
    active_global = session.scalar(select(func.count()).select_from(AgentReflectionTask).where(
        AgentReflectionTask.status.in_(("pending", "processing")),
    )) or 0
    if per_day >= settings.max_reflections_per_day or active_npc is not None or active_global >= settings.queue_limit:
        return None
    start, end = max(0, (reflection_day - 1) * 1440), reflection_day * 1440
    payloads, known_npcs = _source_payloads(session, npc, reflection_day, start, end, trigger_type, trigger_source_id)
    now = _utcnow()
    task = AgentReflectionTask(
        dedupe_key=dedupe_key, npc_id=npc_id, reflection_day=reflection_day,
        trigger_type=trigger_type, trigger_source_id=trigger_source_id,
        context_json="{}", context_digest="", status="pending",
        response_deadline_at=now + timedelta(seconds=settings.timeout_seconds + 1.0),
        created_minute=created_minute,
    )
    session.add(task)
    session.flush([task])
    for item in payloads:
        session.add(AgentReflectionSource(
            task_id=task.id, npc_id=npc_id, source_key=item["source_id"],
            source_type=item["source_type"], source_row_id=item["source_row_id"],
            summary=item["summary"], range_start_minute=start, range_end_minute=end,
        ))
    context = _build_context(session, task, npc, payloads, known_npcs)
    task.context_json, task.context_digest = _digest(context)
    if trigger_type == "daily":
        state.last_daily_enqueued_day = max(state.last_daily_enqueued_day, reflection_day)
    elif trigger_source_id is not None:
        state.last_milestone_id = max(state.last_milestone_id, trigger_source_id)
    state.updated_minute = created_minute
    return task


def enqueue_due_reflections(
    session: Session, clock: ClockSnapshot, enabled_npc_ids: Iterable[int], settings: CognitionSettings,
) -> int:
    enabled = sorted(set(enabled_npc_ids) & set(SUPPORTED_NPC_IDS))
    ensure_cognition_states(session, enabled, clock.total_minutes)
    created = 0
    if clock.total_minutes % 1440 == 0:
        reflection_day = clock.day - 1
        for npc_id in enabled:
            state = session.scalar(select(AgentCognitionState).where(AgentCognitionState.npc_id == npc_id))
            if state is not None and state.last_daily_enqueued_day < reflection_day:
                created += enqueue_reflection(
                    session, npc_id, reflection_day, clock.total_minutes, settings,
                ) is not None
    # At most one owned milestone extra per NPC/day. A blocked one is retried later.
    for npc_id in enabled:
        state = session.scalar(select(AgentCognitionState).where(AgentCognitionState.npc_id == npc_id))
        if state is None:
            continue
        milestone = session.scalar(select(LifeMilestone).where(
            LifeMilestone.npc_id == npc_id, LifeMilestone.id > state.last_milestone_id,
        ).order_by(LifeMilestone.id).limit(1))
        if milestone is not None:
            created += enqueue_reflection(
                session, npc_id, clock.day, clock.total_minutes, settings,
                trigger_type="milestone", trigger_source_id=milestone.id,
            ) is not None
    return created


def _validate_output(session: Session, task: AgentReflectionTask, output: ReflectionOutput) -> str | None:
    context = json.loads(task.context_json)
    if context.get("self", {}).get("id") != task.npc_id or context.get("security", {}).get("private_context_owner_npc_id") != task.npc_id:
        return "wrong_npc"
    source_ids = {row.source_key for row in session.scalars(select(AgentReflectionSource).where(
        AgentReflectionSource.task_id == task.id, AgentReflectionSource.npc_id == task.npc_id,
    ))}
    goal_keys = set(session.scalars(select(LongTermGoal.goal_key).where(LongTermGoal.npc_id == task.npc_id)))
    if output.goal_focus not in goal_keys:
        return "unknown_goal"
    allowed_social = set(context.get("allowed_plan_targets", {}).get("Socialize", []))
    allowed_belief_targets = {"self"} | {f"goal:{key}" for key in goal_keys} | allowed_social

    def evidence_ok(items: list[str]) -> bool:
        return bool(items) and set(items).issubset(source_ids)

    for belief in output.belief_updates:
        if belief.target not in allowed_belief_targets:
            return "invalid_belief_target"
        if not evidence_ok(belief.evidence_ids):
            return "fictional_evidence"
    for plan in output.plan_steps:
        if plan.goal_key not in goal_keys:
            return "unknown_plan_goal"
        if plan.action_category not in PLAN_ACTIONS:
            return "unknown_action"
        if plan.action_category == "Socialize":
            if plan.target not in allowed_social:
                return "invalid_plan_target"
        elif plan.target is not None:
            return "invalid_plan_target"
        if not evidence_ok(plan.evidence_ids):
            return "fictional_evidence"
    for adjustment in output.plan_adjustments:
        plan = session.get(AgentPlan, adjustment.plan_id)
        if plan is None or plan.npc_id != task.npc_id or plan.status not in {"pending", "in_progress"}:
            return "invalid_plan_adjustment"
        if not evidence_ok(adjustment.evidence_ids):
            return "fictional_evidence"
    return None


def _store_result(
    session: Session, task: AgentReflectionTask, result: ReflectionGenerationResult, now_minute: int,
) -> AgentReflection:
    output = result.output
    reflection = AgentReflection(
        task_id=task.id, npc_id=task.npc_id, reflection_day=task.reflection_day,
        trigger_type=task.trigger_type, day_summary=output.day_summary,
        emotion_summary=output.emotion_summary,
        lessons_json=json.dumps(output.lessons, ensure_ascii=False, separators=(",", ":")),
        goal_focus=output.goal_focus, reason_summary=output.reason_summary,
        plan_adjustments_json=json.dumps(
            [item.model_dump() for item in output.plan_adjustments], ensure_ascii=False, separators=(",", ":")
        ),
        provider=result.provider, model=result.model, fallback_used=result.fallback_used,
        failure_reason=result.failure_reason,
        fact_boundary_json=json.dumps({
            "authority": "simulation_engine", "beliefs_are_subjective": True,
            "plans_are_non_executable": True,
            "forbidden_fact_writes": [
                "goal_value", "relationship", "commitment", "money", "inventory", "career",
                "location", "state", "action",
            ],
        }, ensure_ascii=False, separators=(",", ":")),
        created_minute=now_minute,
    )
    session.add(reflection)
    session.flush([reflection])
    for item in output.belief_updates:
        session.add(AgentSubjectiveBelief(
            npc_id=task.npc_id, reflection_id=reflection.id, target=item.target,
            belief_text=item.belief, confidence=item.confidence,
            evidence_json=json.dumps(item.evidence_ids, ensure_ascii=False),
            created_minute=now_minute,
        ))
    for sequence, item in enumerate(output.plan_steps, start=1):
        session.add(AgentPlan(
            npc_id=task.npc_id, reflection_id=reflection.id, sequence=sequence,
            goal_key=item.goal_key, action_category=item.action_category, target=item.target,
            description=item.description,
            evidence_json=json.dumps(item.evidence_ids, ensure_ascii=False),
            window_start_day=task.reflection_day + item.start_in_days,
            window_end_day=task.reflection_day + item.end_in_days,
            status="pending", created_minute=now_minute, updated_minute=now_minute,
        ))
    for item in output.plan_adjustments:
        plan = session.get(AgentPlan, item.plan_id)
        if plan is None or plan.npc_id != task.npc_id:
            continue
        if item.operation == "cancel":
            plan.status = "cancelled"
            plan.progress_reason = _safe(item.reason, 240)
        else:
            plan.window_end_day += item.extend_days
            plan.progress_reason = _safe(item.reason, 240)
        plan.progress_source_type = "reflection"
        plan.progress_source_id = reflection.id
        plan.updated_minute = now_minute
    state = session.scalar(select(AgentCognitionState).where(AgentCognitionState.npc_id == task.npc_id))
    if state is not None:
        state.current_goal_key = output.goal_focus
        state.last_reflected_day = max(state.last_reflected_day, task.reflection_day)
        state.updated_minute = now_minute
    task.status = "completed"
    task.completed_at = _utcnow()
    task.lease_token = None
    task.lease_expires_at = None
    task.last_error_code = result.failure_reason
    return reflection


async def process_reflection_tasks(session_factory, generator: ReflectionGenerator, limit: int = 5) -> int:
    with session_factory() as session:
        if not V15_TABLE_NAMES.issubset(set(inspect(session.get_bind()).get_table_names())):
            return 0
    claimed: list[tuple[int, str, dict[str, Any]]] = []
    for _ in range(min(max(limit, 1), generator.settings.queue_limit)):
        token = uuid.uuid4().hex
        now = _utcnow()
        with session_factory() as session:
            # One active row per NPC makes oldest-first ordering fair across NPCs.
            task = session.scalar(select(AgentReflectionTask).where(
                AgentReflectionTask.status == "pending",
            ).order_by(AgentReflectionTask.created_minute, AgentReflectionTask.npc_id, AgentReflectionTask.id).limit(1))
            if task is None:
                break
            context = json.loads(task.context_json)
            raw, digest = _digest(context)
            if digest != task.context_digest or context.get("self", {}).get("id") != task.npc_id:
                task.status = "discarded"
                task.last_error_code = "context_identity_mismatch"
                task.completed_at = now
                session.commit()
                continue
            task.status = "processing"
            task.attempts += 1
            task.lease_token = token
            task.started_at = now
            task.lease_expires_at = now + timedelta(seconds=generator.settings.lease_seconds)
            task_id = task.id
            session.commit()
        claimed.append((task_id, token, context))

    semaphore = asyncio.Semaphore(generator.settings.max_concurrency)

    async def process_one(task_id: int, token: str, context: dict[str, Any]) -> None:
        async with semaphore:
            result = await generator.generate(context)
        with session_factory() as session:
            task = session.get(AgentReflectionTask, task_id)
            if task is None or task.status != "processing" or task.lease_token != token:
                return
            now = _utcnow()
            state = session.get(NPC, task.npc_id)
            context_raw, context_digest = _digest(context)
            failure = _validate_output(session, task, result.output)
            if state is None or context_digest != task.context_digest:
                failure = "late_or_identity_mismatch"
            if _as_utc(task.response_deadline_at) < now and not result.fallback_used:
                failure = "late_response"
            if failure is not None:
                result = ReflectionGenerationResult(
                    _fallback_output(context, failure), "deterministic-personality", None, True, failure
                )
                fallback_failure = _validate_output(session, task, result.output)
                if fallback_failure is not None:
                    task.status = "discarded"
                    task.last_error_code = fallback_failure
                    task.completed_at = now
                    session.commit()
                    return
            existing = session.scalar(select(AgentReflection).where(AgentReflection.task_id == task.id))
            if existing is None:
                _store_result(session, task, result, task.created_minute)
            else:
                task.status = "completed"
                task.completed_at = now
            session.commit()

    await asyncio.gather(*(process_one(*item) for item in claimed), return_exceptions=True)
    return len(claimed)


def recover_reflection_tasks(session: Session, now: datetime | None = None) -> int:
    now = now or _utcnow()
    recovered = 0
    for task in session.scalars(select(AgentReflectionTask).where(AgentReflectionTask.status == "processing")):
        if task.lease_expires_at is None or _as_utc(task.lease_expires_at) <= now:
            task.status = "pending"
            task.lease_token = None
            task.lease_expires_at = None
            task.started_at = None
            task.last_error_code = "recovered_after_restart"
            recovered += 1
    return recovered


def cancel_reflection_task(session: Session, task_id: int) -> bool:
    task = session.get(AgentReflectionTask, task_id)
    if task is None or task.status not in {"pending", "processing"}:
        return False
    task.status = "cancelled"
    task.last_error_code = "cancelled_by_operator"
    task.completed_at = _utcnow()
    task.lease_token = None
    task.lease_expires_at = None
    return True


def evaluate_plan_progress(session: Session, clock: ClockSnapshot) -> int:
    changed = 0
    for plan in session.scalars(select(AgentPlan).where(
        AgentPlan.status.in_(("pending", "in_progress")),
    ).order_by(AgentPlan.id)):
        if clock.day > plan.window_end_day:
            plan.status = "expired"
            plan.progress_reason = "Engine 未在计划时间窗内找到该 NPC 的真实完成事实"
            plan.updated_minute = clock.total_minutes
            changed += 1
            continue
        if clock.day < plan.window_start_day:
            continue
        event_type = ACTION_EVENT_TYPES[plan.action_category]
        earliest_day = plan.created_minute // 1440 + 1
        query = select(Event).where(
            Event.npc_id == plan.npc_id, Event.event_type == event_type,
            Event.world_day >= earliest_day,
        ).order_by(Event.id)
        event = None
        for candidate in session.scalars(query):
            try:
                hours, minutes = candidate.world_time.split(":", 1)
                candidate_minute = max(0, candidate.world_day - 1) * 1440 + int(hours) * 60 + int(minutes)
            except (ValueError, AttributeError):
                continue
            if candidate_minute < plan.created_minute:
                continue
            if plan.action_category == "Socialize" and plan.target != f"npc:{candidate.target_npc_id}":
                continue
            if plan.action_category in MOVE_TARGETS and candidate.location != MOVE_TARGETS[plan.action_category]:
                continue
            event = candidate
            break
        if event is not None:
            failed = any(token in event.description for token in ("未能", "没有可", "暂未满足", "安全回退"))
            plan.status = "failed" if failed else "completed"
            plan.progress_reason = _safe(event.description, 240)
            plan.progress_source_type = "event"
            plan.progress_source_id = event.id
            plan.updated_minute = clock.total_minutes
            changed += 1
            continue
        decision = next((row for row in session.scalars(select(DecisionLog).where(
            DecisionLog.npc_id == plan.npc_id,
            DecisionLog.chosen_action == plan.action_category,
            DecisionLog.world_day >= earliest_day,
        ).order_by(DecisionLog.id)) if (
            max(0, row.world_day - 1) * 1440
            + int(row.world_time[:2]) * 60 + int(row.world_time[3:5])
        ) >= plan.created_minute), None)
        if decision is not None and plan.status == "pending":
            plan.status = "in_progress"
            plan.progress_reason = "Engine 已记录匹配的候选决策，等待真实行动结果"
            plan.progress_source_type = "decision"
            plan.progress_source_id = decision.id
            plan.updated_minute = clock.total_minutes
            changed += 1
    return changed


def cognition_context_snapshot(
    session: Session, npc_id: int, *, related_npc_id: int | None = None,
) -> dict[str, Any] | None:
    state = session.scalar(select(AgentCognitionState).where(AgentCognitionState.npc_id == npc_id))
    if state is None:
        return None
    reflection = session.scalar(select(AgentReflection).where(
        AgentReflection.npc_id == npc_id,
    ).order_by(AgentReflection.id.desc()).limit(1))
    if reflection is None:
        return None
    belief_query = select(AgentSubjectiveBelief).where(
        AgentSubjectiveBelief.npc_id == npc_id, AgentSubjectiveBelief.active.is_(True),
    )
    allowed_targets = {"self", f"goal:{state.current_goal_key}"}
    if related_npc_id is not None:
        allowed_targets.add(f"npc:{related_npc_id}")
    beliefs = list(session.scalars(
        belief_query.where(AgentSubjectiveBelief.target.in_(allowed_targets)).order_by(AgentSubjectiveBelief.id.desc()).limit(6)
    ))
    plans = list(session.scalars(select(AgentPlan).where(
        AgentPlan.npc_id == npc_id, AgentPlan.status.in_(("pending", "in_progress")),
    ).order_by(AgentPlan.window_end_day, AgentPlan.id).limit(6)))
    return {
        "current_goal_focus": state.current_goal_key,
        "latest_reflection": {
            "reflection_id": reflection.id, "day": reflection.reflection_day,
            "day_summary": reflection.day_summary, "emotion_summary": reflection.emotion_summary,
            "lessons": json.loads(reflection.lessons_json), "reason_summary": reflection.reason_summary,
        },
        "subjective_beliefs": [
            {"belief_id": row.id, "target": row.target, "belief": row.belief_text,
             "confidence": row.confidence, "evidence_ids": json.loads(row.evidence_json),
             "subjective_not_fact": True}
            for row in beliefs
        ],
        "unfinished_plans": [
            {"plan_id": row.id, "goal_key": row.goal_key, "action_category": row.action_category,
             "target": row.target, "description": row.description, "status": row.status,
             "window_start_day": row.window_start_day, "window_end_day": row.window_end_day,
             "non_executable": True}
            for row in plans
        ],
    }


def reflection_snapshot(session: Session, row: AgentReflection) -> dict[str, Any]:
    sources = list(session.scalars(select(AgentReflectionSource).where(
        AgentReflectionSource.task_id == row.task_id,
    ).order_by(AgentReflectionSource.id)))
    beliefs = list(session.scalars(select(AgentSubjectiveBelief).where(
        AgentSubjectiveBelief.reflection_id == row.id,
    ).order_by(AgentSubjectiveBelief.id)))
    plans = list(session.scalars(select(AgentPlan).where(
        AgentPlan.reflection_id == row.id,
    ).order_by(AgentPlan.sequence)))
    task = session.get(AgentReflectionTask, row.task_id)
    return {
        "id": row.id, "task_id": row.task_id, "npc_id": row.npc_id,
        "reflection_day": row.reflection_day, "trigger_type": row.trigger_type,
        "day_summary": row.day_summary, "emotion_summary": row.emotion_summary,
        "lessons": json.loads(row.lessons_json), "goal_focus": row.goal_focus,
        "reason_summary": row.reason_summary, "plan_adjustments": json.loads(row.plan_adjustments_json),
        "provider": row.provider, "model": row.model, "fallback_used": row.fallback_used,
        "failure_reason": row.failure_reason, "fact_boundary": json.loads(row.fact_boundary_json),
        "sources": [
            {"source_id": item.source_key, "type": item.source_type, "row_id": item.source_row_id,
             "summary": item.summary, "range_start_minute": item.range_start_minute,
             "range_end_minute": item.range_end_minute}
            for item in sources
        ],
        "beliefs": [
            {"id": item.id, "target": item.target, "belief": item.belief_text,
             "confidence": item.confidence, "evidence_ids": json.loads(item.evidence_json),
             "subjective_not_fact": True}
            for item in beliefs
        ],
        "plans": [plan_snapshot(item) for item in plans],
        "task": {"status": task.status, "attempts": task.attempts, "error": task.last_error_code} if task else None,
    }


def plan_snapshot(row: AgentPlan) -> dict[str, Any]:
    return {
        "id": row.id, "npc_id": row.npc_id, "reflection_id": row.reflection_id,
        "sequence": row.sequence, "goal_key": row.goal_key, "action_category": row.action_category,
        "target": row.target, "description": row.description,
        "evidence_ids": json.loads(row.evidence_json),
        "window_start_day": row.window_start_day, "window_end_day": row.window_end_day,
        "status": row.status, "progress_reason": row.progress_reason,
        "progress_evidence": {
            "source_type": row.progress_source_type, "source_id": row.progress_source_id,
        },
        "non_executable": True,
    }


def cognition_snapshot(session: Session, npc_id: int) -> dict[str, Any] | None:
    npc = session.get(NPC, npc_id)
    if npc is None:
        return None
    state = session.scalar(select(AgentCognitionState).where(AgentCognitionState.npc_id == npc_id))
    reflections = list(session.scalars(select(AgentReflection).where(
        AgentReflection.npc_id == npc_id,
    ).order_by(AgentReflection.id.desc()).limit(10)))
    beliefs = list(session.scalars(select(AgentSubjectiveBelief).where(
        AgentSubjectiveBelief.npc_id == npc_id, AgentSubjectiveBelief.active.is_(True),
    ).order_by(AgentSubjectiveBelief.id.desc()).limit(20)))
    plans = list(session.scalars(select(AgentPlan).where(
        AgentPlan.npc_id == npc_id,
    ).order_by(AgentPlan.id.desc()).limit(30)))
    return {
        "npc_id": npc_id, "npc_name": npc.name, "initialized": state is not None,
        "current_goal_focus": state.current_goal_key if state else None,
        "last_reflected_day": state.last_reflected_day if state else 0,
        "reflections": [reflection_snapshot(session, row) for row in reflections],
        "subjective_beliefs": [
            {"id": row.id, "reflection_id": row.reflection_id, "target": row.target,
             "belief": row.belief_text, "confidence": row.confidence,
             "evidence_ids": json.loads(row.evidence_json), "subjective_not_fact": True}
            for row in beliefs
        ],
        "plans": [plan_snapshot(row) for row in plans],
        "fact_boundary": {
            "beliefs_are_subjective": True, "plans_are_non_executable": True,
            "world_facts_owned_by": "simulation_engine",
        },
    }


def cognition_safety_check(session: Session, queue_limit: int) -> dict[str, Any]:
    active = list(session.scalars(select(AgentReflectionTask).where(
        AgentReflectionTask.status.in_(("pending", "processing")),
    )))
    violations: list[dict[str, Any]] = []
    seen_npcs: set[int] = set()
    for task in active:
        if task.npc_id in seen_npcs:
            violations.append({"task_id": task.id, "code": "multiple_active_for_npc"})
        seen_npcs.add(task.npc_id)
        context = json.loads(task.context_json)
        if context.get("self", {}).get("id") != task.npc_id:
            violations.append({"task_id": task.id, "code": "context_owner_mismatch"})
    for source in session.scalars(select(AgentReflectionSource)):
        task = session.get(AgentReflectionTask, source.task_id)
        if task is None or source.npc_id != task.npc_id:
            violations.append({"source_id": source.id, "code": "source_owner_mismatch"})
    return {
        "ok": not violations and len(active) <= queue_limit,
        "queue": {"active": len(active), "limit": queue_limit, "bounded": len(active) <= queue_limit},
        "violations": violations,
        "private_context_exposed_by_api": False,
        "model_fact_authority": False,
        "hidden_reasoning_requested": False,
    }
