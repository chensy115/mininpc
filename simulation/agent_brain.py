from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.models import (
    AgentDecisionArtifact,
    AgentDecisionJob,
    AgentTakeoverTurn,
    DecisionLog,
    LongTermGoal,
    Memory,
    NPC,
    Relationship,
    SocialCommitment,
    WorldState,
)
from simulation.clock import ClockSnapshot
from simulation.decision import Candidate, Decision
from simulation.goals import goal_snapshots
from simulation.agent_takeover import (
    SUPPORTED_NPC_IDS,
    claim_takeover_turn,
    deadline_expired,
    mark_turn_ready,
    mark_turn_worker_failed,
    recover_takeover_leases,
    validate_action_selection,
)


TARGET_NPC_ID = 1
V11_TABLE_NAMES = {"agent_decision_jobs", "agent_decision_artifacts"}
MOVE_ACTIONS = {"GoHome", "GoOffice", "GoCafe", "GoPark"}


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


def agent_shadow_enabled_from_env() -> bool:
    return _env_bool("MINIWORLD_AGENT_SHADOW_ENABLED", False)


def agent_takeover_enabled_from_env() -> bool:
    return _env_bool("MINIWORLD_AGENT_TAKEOVER_ENABLED", False)


def agent_takeover_npc_ids_from_env() -> set[int]:
    """Resolve V1.3 scope while keeping the V1.2 flag Alice-only."""

    if _env_bool("MINIWORLD_AGENT_TAKEOVER_ALL_ENABLED", False):
        return set(SUPPORTED_NPC_IDS)
    raw = os.getenv("MINIWORLD_AGENT_TAKEOVER_NPCS", "")
    names = {"alice": 1, "bob": 2, "charlie": 3, "diana": 4, "eric": 5}
    selected: set[int] = set()
    for token in (part.strip().lower() for part in raw.split(",")):
        if not token:
            continue
        try:
            npc_id = int(token)
        except ValueError:
            npc_id = names.get(token, 0)
        if npc_id in SUPPORTED_NPC_IDS:
            selected.add(npc_id)
    if agent_takeover_enabled_from_env():
        selected.add(TARGET_NPC_ID)
    return selected


def _timeout_from_env(name: str, default: float) -> float:
    try:
        return min(60.0, max(0.1, float(os.getenv(name, str(default)))))
    except ValueError:
        return default


def _attempts_from_env() -> int:
    try:
        return min(5, max(1, int(os.getenv("MINIWORLD_AGENT_MAX_ATTEMPTS", "2"))))
    except ValueError:
        return 2


@dataclass(frozen=True)
class AgentSettings:
    api_key: str | None
    base_url: str
    model: str
    timeout_seconds: float
    max_attempts: int

    @classmethod
    def from_env(cls) -> "AgentSettings":
        return cls(
            api_key=(
                os.getenv("MINIWORLD_AGENT_API_KEY")
                or os.getenv("MINIWORLD_LLM_API_KEY")
                or os.getenv("OPENAI_API_KEY")
            ),
            base_url=(
                os.getenv("MINIWORLD_AGENT_BASE_URL")
                or os.getenv("MINIWORLD_LLM_BASE_URL")
                or "https://api.openai.com/v1"
            ).rstrip("/"),
            model=(
                os.getenv("MINIWORLD_AGENT_MODEL")
                or os.getenv("MINIWORLD_LLM_MODEL")
                or ""
            ),
            timeout_seconds=_timeout_from_env(
                "MINIWORLD_AGENT_TIMEOUT",
                _timeout_from_env("MINIWORLD_LLM_TIMEOUT", 8.0),
            ),
            max_attempts=_attempts_from_env(),
        )


class AgentDecision(BaseModel):
    """The only model-authored fields MiniWorld accepts or stores."""

    model_config = ConfigDict(extra="forbid", strict=True)

    emotion: str = Field(min_length=1, max_length=80)
    intention: str = Field(min_length=1, max_length=240)
    action: str = Field(min_length=1, max_length=40)
    target: str | None = Field(max_length=80)
    dialogue: str | None = Field(max_length=280)
    plan: list[str] = Field(min_length=1, max_length=4)
    reason_summary: str = Field(min_length=1, max_length=500)

    @field_validator("emotion", "intention", "action", "target", "dialogue", "reason_summary")
    @classmethod
    def _clean_string(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("blank strings are not accepted")
        return cleaned

    @field_validator("plan")
    @classmethod
    def _clean_plan(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item or len(item) > 180 for item in cleaned):
            raise ValueError("plan items must contain 1-180 characters")
        return cleaned


class AgentProvider(Protocol):
    name: str

    async def generate(self, perception: dict[str, Any]) -> str: ...


class AgentGenerationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class OpenAICompatibleAgentProvider:
    name = "openai-compatible"

    def __init__(self, settings: AgentSettings) -> None:
        self.settings = settings

    async def generate(self, perception: dict[str, Any]) -> str:
        takeover = perception.get("schema_version") in {"1.2", "1.3"}
        self_name = str(perception.get("self", {}).get("name") or "the NPC")
        system = (
            (f"You choose {self_name}'s proposed next action in MiniWorld V1.3 takeover mode. "
             if takeover else f"You are {self_name}'s advisory agent in MiniWorld V1.1 shadow mode. ") +
            "Return one JSON object only. Never request tools, execute actions, invent world facts, "
            "or provide hidden chain-of-thought. Use only the supplied perception and available_actions. "
            "Keep reason_summary brief. Required keys are exactly: emotion, intention, action, target, "
            "dialogue, plan, reason_summary. target and dialogue must be null when not applicable; "
            "plan must contain 1-4 short strings."
        )
        payload = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": "Choose one legal action and return strict JSON. Perception:\n"
                    + json.dumps(perception, ensure_ascii=False, separators=(",", ":")),
                },
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": 500,
        }
        headers = {"Authorization": f"Bearer {self.settings.api_key}"}
        try:
            async with httpx.AsyncClient(timeout=self.settings.timeout_seconds) as client:
                response = await client.post(
                    f"{self.settings.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
            content = body["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise AgentGenerationError("empty_response")
            return content
        except AgentGenerationError:
            raise
        except httpx.TimeoutException as exc:
            raise AgentGenerationError("timeout") from exc
        except httpx.HTTPStatusError as exc:
            raise AgentGenerationError("http_error") from exc
        except (httpx.HTTPError, KeyError, TypeError, ValueError, IndexError) as exc:
            raise AgentGenerationError("provider_error") from exc


class AgentDecisionGenerator:
    def __init__(
        self,
        settings: AgentSettings | None = None,
        provider: AgentProvider | None = None,
    ) -> None:
        self.settings = settings or AgentSettings.from_env()
        self.provider = provider
        if self.provider is None and self.settings.api_key and self.settings.model:
            self.provider = OpenAICompatibleAgentProvider(self.settings)

    def status(self) -> dict[str, Any]:
        if self.provider is None:
            return {
                "available": False,
                "provider": None,
                "model": self.settings.model,
                "reason": "missing_api_key" if not self.settings.api_key else "missing_model",
            }
        return {
            "available": True,
            "provider": self.provider.name,
            "model": self.settings.model,
            "reason": None,
        }

    async def generate(self, perception: dict[str, Any]) -> AgentDecision:
        if self.provider is None:
            raise AgentGenerationError("provider_unavailable")
        try:
            if getattr(self.provider, "manages_timeout", False):
                raw = await self.provider.generate(perception)
            else:
                raw = await asyncio.wait_for(
                    self.provider.generate(perception), timeout=self.settings.timeout_seconds + 0.5
                )
        except TimeoutError as exc:
            raise AgentGenerationError("timeout") from exc
        except AgentGenerationError:
            raise
        except Exception as exc:
            raise AgentGenerationError("provider_error") from exc
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise AgentGenerationError("invalid_json") from exc
        try:
            return AgentDecision.model_validate(value)
        except ValidationError as exc:
            raise AgentGenerationError("schema_validation_failed") from exc


def _relevant_memories(
    session: Session,
    npc_id: int,
    related_ids: set[int],
    now_minute: int,
    limit: int = 8,
) -> list[dict[str, Any]]:
    query = select(Memory).where(Memory.npc_id == npc_id)
    if related_ids:
        query = query.where(or_(Memory.related_npc_id.is_(None), Memory.related_npc_id.in_(related_ids)))
    else:
        query = query.where(Memory.related_npc_id.is_(None))
    window = list(
        session.scalars(query.order_by(Memory.timestamp.desc(), Memory.id.desc()).limit(60))
    )
    ranked = sorted(
        window,
        key=lambda item: (
            item.related_npc_id in related_ids if item.related_npc_id is not None else False,
            item.importance,
            item.timestamp,
            item.id,
        ),
        reverse=True,
    )[:limit]
    names = {
        person.id: person.name
        for person in session.scalars(select(NPC).where(NPC.id.in_(related_ids)))
    } if related_ids else {}
    return [
        {
            "content": item.content,
            "importance": item.importance,
            "emotion": item.emotion,
            "age_minutes": max(0, now_minute - item.timestamp),
            "related_person": names.get(item.related_npc_id),
        }
        for item in sorted(ranked, key=lambda item: (item.timestamp, item.id))
    ]


def _candidate_snapshot(candidate: Candidate, visible_names: list[str]) -> dict[str, Any]:
    if candidate.action in MOVE_ACTIONS:
        allowed_targets = [candidate.target_location] if candidate.target_location else []
    elif candidate.action == "Socialize":
        allowed_targets = visible_names
    else:
        allowed_targets = []
    return {
        "action": candidate.action,
        "target_location": candidate.target_location,
        "allowed_targets": allowed_targets,
        "description": candidate.explanation,
    }


def build_perception_snapshot(
    session: Session,
    npc: NPC,
    clock: ClockSnapshot,
    occupants: dict[str, list[NPC]],
    decision: Decision,
) -> dict[str, Any]:
    visible_people = [person for person in occupants[npc.current_location] if person.id != npc.id]
    goals = goal_snapshots(session, npc)
    goal_target_ids = {
        int(item["target_npc_id"])
        for item in goals
        if item.get("target_npc_id") is not None
    }
    related_ids = {person.id for person in visible_people} | goal_target_ids
    names = {
        person.id: person.name
        for person in session.scalars(select(NPC).where(NPC.id.in_(related_ids)))
    } if related_ids else {}
    relationships = list(
        session.scalars(
            select(Relationship).where(
                Relationship.from_npc_id == npc.id,
                Relationship.to_npc_id.in_(related_ids),
            ).order_by(Relationship.to_npc_id)
        )
    ) if related_ids else []
    commitments = list(
        session.scalars(
            select(SocialCommitment).where(
                SocialCommitment.status == "planned",
                or_(SocialCommitment.npc_low_id == npc.id, SocialCommitment.npc_high_id == npc.id),
            ).order_by(SocialCommitment.scheduled_minute, SocialCommitment.id).limit(5)
        )
    )
    plans = []
    for item in commitments:
        other_id = item.npc_high_id if item.npc_low_id == npc.id else item.npc_low_id
        other = session.get(NPC, other_id)
        plans.append({
            "kind": "social_commitment",
            "with": other.name if other is not None else f"NPC {other_id}",
            "location": item.location,
            "planned_minute": item.scheduled_minute,
            "expires_minute": item.expires_minute,
        })
    visible_names = [person.name for person in visible_people]
    available_actions = [
        _candidate_snapshot(candidate, visible_names)
        for candidate in decision.candidates
        if candidate.available
    ]
    snapshot = {
        "schema_version": "1.1",
        "time": {
            "day": clock.day,
            "weekday": clock.weekday,
            "time": clock.time_text,
            "total_minutes": clock.total_minutes,
        },
        "place": npc.current_location,
        "self": {
            "id": npc.id,
            "name": npc.name,
            "age": npc.age,
            "job": npc.job,
            "money": round(float(npc.money), 2),
            "states": {
                "energy": round(float(npc.energy), 2),
                "hunger": round(float(npc.hunger), 2),
                "mood": round(float(npc.mood), 2),
                "social_need": round(float(npc.social_need), 2),
                "work_satisfaction": round(float(npc.work_satisfaction), 2),
            },
            "personality": {
                "extroversion": npc.extroversion,
                "kindness": npc.kindness,
                "ambition": npc.ambition,
                "risk_tolerance": npc.risk_tolerance,
                "discipline": npc.discipline,
            },
            "previous_action": npc.current_action,
        },
        "people_here": [
            {"id": person.id, "name": person.name, "current_action": person.current_action}
            for person in visible_people
        ],
        "relevant_relationships": [
            {"npc_id": item.to_npc_id, "name": names.get(item.to_npc_id), "score": item.score}
            for item in relationships
        ],
        "goals": [
            {
                "type": item["type"],
                "label": item["label"],
                "priority": item["priority"],
                "current_value": item["current_value"],
                "target_value": item["target_value"],
                "status": item["status"],
                "target_person": item.get("target_npc_name"),
            }
            for item in goals
        ],
        "plans": plans,
        "relevant_memories": _relevant_memories(
            session, npc.id, related_ids, clock.total_minutes
        ),
        "available_actions": available_actions,
    }
    # Lazy import avoids coupling the shared provider definitions back into this
    # module. Default-off worlds have no cognition state and preserve exact V1.4 shape.
    from simulation.agent_cognition import cognition_context_snapshot
    cognition = cognition_context_snapshot(session, npc.id)
    if cognition is not None:
        snapshot["cognition"] = cognition
    return snapshot


def enqueue_agent_decision(
    session: Session,
    decision_log: DecisionLog,
    npc: NPC,
    clock: ClockSnapshot,
    occupants: dict[str, list[NPC]],
    decision: Decision,
) -> bool:
    if npc.id not in SUPPORTED_NPC_IDS:
        return False
    if decision_log.id is None:
        raise ValueError("decision_log must be flushed before enqueue")
    session.add(
        AgentDecisionJob(
            decision_id=decision_log.id,
            npc_id=npc.id,
            perception_json=json.dumps(
                build_perception_snapshot(session, npc, clock, occupants, decision),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            status="pending",
            created_minute=clock.total_minutes,
        )
    )
    return True


def _validate_legality(
    advice: AgentDecision, perception: dict[str, Any]
) -> dict[str, Any]:
    candidates = {
        item["action"]: item for item in perception.get("available_actions", [])
    }
    candidate = candidates.get(advice.action)
    action_offered = candidate is not None
    allowed_targets = candidate.get("allowed_targets", []) if candidate else []
    if not action_offered:
        target_valid = False
        reason_code = "action_not_offered"
    elif advice.action in MOVE_ACTIONS:
        target_valid = advice.target in allowed_targets
        reason_code = "ok" if target_valid else "invalid_move_target"
    elif advice.action == "Socialize":
        target_valid = advice.target in allowed_targets
        reason_code = "ok" if target_valid else "invalid_social_target"
    else:
        target_valid = advice.target is None
        reason_code = "ok" if target_valid else "unexpected_target"
    return {
        "legal": bool(action_offered and target_valid),
        "action_offered": action_offered,
        "candidate_available": action_offered,
        "target_valid": target_valid,
        "reason_code": reason_code,
    }


def _utility_target(action: str, perception: dict[str, Any]) -> str | None:
    if action not in MOVE_ACTIONS:
        return None
    candidate = next(
        (
            item for item in perception.get("available_actions", [])
            if item.get("action") == action
        ),
        None,
    )
    return candidate.get("target_location") if candidate else None


def _comparison(
    advice: AgentDecision,
    utility: DecisionLog,
    perception: dict[str, Any],
) -> dict[str, Any]:
    utility_target = _utility_target(utility.chosen_action, perception)
    same_action = advice.action == utility.chosen_action
    if advice.action == "Socialize" and same_action:
        same_target: bool | None = None
        target_comparison = "not_comparable"
    else:
        same_target = advice.target == utility_target
        target_comparison = "same" if same_target else "different"
    if same_action and (same_target is True or same_target is None):
        summary = "Agent 建议与 Utility 实际行动一致"
    else:
        summary = f"Agent 建议 {advice.action}，Utility 实际选择 {utility.chosen_action}"
    return {
        "same_action": same_action,
        "same_target": same_target,
        "target_comparison": target_comparison,
        "difference_summary": summary,
    }


async def process_agent_jobs(
    session_factory,
    generator: AgentDecisionGenerator,
    limit: int = 5,
) -> int:
    processed = 0
    for _ in range(max(1, min(limit, 20))):
        with session_factory() as session:
            job = session.scalar(
                select(AgentDecisionJob)
                .where(AgentDecisionJob.status == "pending")
                .order_by(AgentDecisionJob.id)
                .limit(1)
            )
            if job is None:
                break
            job.status = "processing"
            job.attempts += 1
            job.started_at = datetime.now(timezone.utc)
            job_id = job.id
            perception = json.loads(job.perception_json)
            attempts = job.attempts
            session.commit()
        try:
            advice = await generator.generate(perception)
        except AgentGenerationError as exc:
            with session_factory() as session:
                job = session.get(AgentDecisionJob, job_id)
                if job is not None and job.status == "processing":
                    terminal = exc.code == "provider_unavailable" or attempts >= generator.settings.max_attempts
                    job.status = "failed" if terminal else "pending"
                    job.last_error_code = exc.code
                    job.completed_at = datetime.now(timezone.utc) if terminal else None
                    session.commit()
            processed += 1
            continue
        with session_factory() as session:
            job = session.get(AgentDecisionJob, job_id)
            if job is None or job.status != "processing":
                processed += 1
                continue
            utility = session.get(DecisionLog, job.decision_id)
            if utility is None:
                job.status = "failed"
                job.last_error_code = "missing_utility_decision"
                job.completed_at = datetime.now(timezone.utc)
                session.commit()
                processed += 1
                continue
            validation = _validate_legality(advice, perception)
            comparison = _comparison(advice, utility, perception)
            if session.scalar(
                select(AgentDecisionArtifact.id).where(AgentDecisionArtifact.job_id == job.id)
            ) is None:
                session.add(
                    AgentDecisionArtifact(
                        job_id=job.id,
                        decision_json=advice.model_dump_json(),
                        provider=generator.provider.name if generator.provider is not None else "unavailable",
                        model=generator.settings.model,
                        legal=validation["legal"],
                        validation_json=json.dumps(validation, ensure_ascii=False),
                        comparison_json=json.dumps(comparison, ensure_ascii=False),
                    )
                )
            job.status = "completed"
            job.last_error_code = None
            job.completed_at = datetime.now(timezone.utc)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
            processed += 1
    return processed


async def process_takeover_jobs(
    session_factory,
    generator: AgentDecisionGenerator,
    limit: int = 5,
    *,
    max_concurrency: int = 3,
    eligible_npc_ids: Iterable[int] | None = None,
) -> int:
    """Process a fair, bounded five-NPC batch without network work in a transaction.

    At most one live turn exists per NPC, so claiming the oldest five rows gives
    every enabled NPC an opportunity before any NPC can enqueue another turn.
    Provider waits run concurrently and each result commits independently.
    """

    allowed = set(eligible_npc_ids or SUPPORTED_NPC_IDS) & SUPPORTED_NPC_IDS
    batch_size = min(max(1, limit), len(SUPPORTED_NPC_IDS), len(allowed))
    claimed: list[tuple[int, int, str, dict[str, Any], int]] = []
    with session_factory() as session:
        state = session.get(WorldState, 1)
        recover_takeover_leases(
            session,
            now=datetime.now(timezone.utc),
            world_minute=state.total_minutes if state is not None else 0,
        )
        session.commit()
    for _ in range(batch_size):
        now = datetime.now(timezone.utc)
        token = uuid.uuid4().hex
        with session_factory() as session:
            state = session.get(WorldState, 1)
            world_minute = state.total_minutes if state is not None else 0
            turn = claim_takeover_turn(
                session,
                lease_token=token,
                now=now,
                lease_expires_at=now + timedelta(seconds=generator.settings.timeout_seconds + 2.0),
                world_minute=world_minute,
                eligible_npc_ids=allowed,
            )
            if turn is None:
                break
            turn_id = turn.id
            job = session.get(AgentDecisionJob, turn.job_id) if turn.job_id else None
            if job is None:
                mark_turn_worker_failed(turn, "missing_agent_job")
                session.commit()
                continue
            perception = json.loads(job.perception_json)
            utility = session.get(DecisionLog, job.decision_id)
            perceived_id = perception.get("self", {}).get("id")
            if (
                job.npc_id != turn.npc_id
                or utility is None
                or utility.npc_id != turn.npc_id
                or perceived_id != turn.npc_id
            ):
                mark_turn_worker_failed(turn, "context_npc_mismatch")
                job.status = "failed"
                job.last_error_code = "context_npc_mismatch"
                job.completed_at = now
                session.commit()
                continue
            attempts = turn.attempts
            npc_id = turn.npc_id
            session.commit()
        claimed.append((turn_id, npc_id, token, perception, attempts))

    semaphore = asyncio.Semaphore(min(max(1, max_concurrency), len(SUPPORTED_NPC_IDS)))

    async def process_one(
        turn_id: int,
        _npc_id: int,
        token: str,
        perception: dict[str, Any],
        attempts: int,
    ) -> None:
        async with semaphore:
            try:
                advice = await generator.generate(perception)
            except AgentGenerationError as exc:
                with session_factory() as session:
                    turn = session.get(AgentTakeoverTurn, turn_id)
                    if turn is not None and turn.state == "waiting" and turn.lease_token == token:
                        job = session.get(AgentDecisionJob, turn.job_id) if turn.job_id else None
                        terminal = (
                            exc.code == "provider_unavailable"
                            or attempts >= generator.settings.max_attempts
                        )
                        if terminal:
                            mark_turn_worker_failed(turn, exc.code)
                        else:
                            turn.worker_state = "pending"
                            turn.last_error_code = exc.code
                            turn.lease_token = None
                            turn.lease_expires_at = None
                        if job is not None and job.status == "processing":
                            job.status = "failed" if terminal else "pending"
                            job.last_error_code = exc.code
                            job.completed_at = datetime.now(timezone.utc) if terminal else None
                        session.commit()
                return
            with session_factory() as session:
                turn = session.get(AgentTakeoverTurn, turn_id)
                if turn is None or turn.state != "waiting" or turn.lease_token != token:
                    return
                job = session.get(AgentDecisionJob, turn.job_id) if turn.job_id else None
                if job is None or job.status != "processing":
                    mark_turn_worker_failed(turn, "missing_agent_job")
                    session.commit()
                    return
                utility = session.get(DecisionLog, job.decision_id)
                if (
                    job.npc_id != turn.npc_id
                    or utility is None
                    or utility.npc_id != turn.npc_id
                    or perception.get("self", {}).get("id") != turn.npc_id
                ):
                    mark_turn_worker_failed(turn, "context_npc_mismatch")
                    job.status = "failed"
                    job.last_error_code = "context_npc_mismatch"
                    job.completed_at = datetime.now(timezone.utc)
                    session.commit()
                    return
                state = session.get(WorldState, 1)
                if deadline_expired(
                    turn,
                    datetime.now(timezone.utc),
                    state.total_minutes if state is not None else turn.valid_until_minute + 1,
                ):
                    mark_turn_worker_failed(turn, "late_response")
                    job.status = "failed"
                    job.last_error_code = "late_response"
                    job.completed_at = datetime.now(timezone.utc)
                    session.commit()
                    return
                options = json.loads(turn.options_json)
                validation = validate_action_selection(
                    advice.action, advice.target, options, dialogue=advice.dialogue
                )
                artifact = session.scalar(
                    select(AgentDecisionArtifact).where(AgentDecisionArtifact.job_id == job.id)
                )
                if artifact is None:
                    session.add(
                        AgentDecisionArtifact(
                            job_id=job.id,
                            decision_json=advice.model_dump_json(),
                            provider=generator.provider.name if generator.provider else "unavailable",
                            model=generator.settings.model,
                            legal=bool(validation["legal"]),
                            validation_json=json.dumps(validation, ensure_ascii=False),
                            comparison_json=json.dumps(
                                {"mode": "takeover", "execution_pending": True},
                                ensure_ascii=False,
                            ),
                        )
                    )
                mark_turn_ready(
                    turn,
                    agent_decision=advice.model_dump(),
                    snapshot_validation=validation,
                )
                job.status = "completed"
                job.last_error_code = None
                job.completed_at = datetime.now(timezone.utc)
                session.commit()

    results = await asyncio.gather(
        *(process_one(*item) for item in claimed),
        return_exceptions=True,
    )
    # An unexpected per-NPC persistence error is isolated from sibling tasks.
    # Its processing lease is recovered on the next normal recovery pass.
    return len(results)


def reset_interrupted_agent_jobs(session: Session) -> int:
    now = datetime.now(timezone.utc)
    valid_takeover_job_ids = {
        turn.job_id
        for turn in session.scalars(
            select(AgentTakeoverTurn).where(
                AgentTakeoverTurn.state == "waiting",
                AgentTakeoverTurn.worker_state == "processing",
            )
        )
        if turn.job_id is not None
        and turn.lease_expires_at is not None
        and (
            turn.lease_expires_at.replace(tzinfo=timezone.utc)
            if turn.lease_expires_at.tzinfo is None else turn.lease_expires_at
        ) > now
    }
    jobs = list(
        session.scalars(
            select(AgentDecisionJob).where(AgentDecisionJob.status == "processing")
        )
    )
    for job in jobs:
        if job.id in valid_takeover_job_ids:
            continue
        job.status = "pending"
        job.last_error_code = "recovered_after_restart"
        job.started_at = None
    return sum(job.id not in valid_takeover_job_ids for job in jobs)


def agent_status_snapshot(
    session: Session,
    enabled: bool,
    generator: AgentDecisionGenerator,
) -> dict[str, Any]:
    provider = generator.status()
    counts = {status: 0 for status in ("pending", "processing", "completed", "failed")}
    if enabled:
        for status in counts:
            counts[status] = session.scalar(
                select(func.count()).select_from(AgentDecisionJob).where(
                    AgentDecisionJob.status == status
                )
            ) or 0
    return {
        "enabled": enabled,
        "mode": "shadow" if enabled else "disabled",
        "target_npc_id": TARGET_NPC_ID,
        "target_npc_name": "Alice",
        "provider": provider,
        "jobs": counts,
        "authority": "advisory_only",
    }


def agent_shadow_snapshot(
    session: Session,
    npc_id: int,
    enabled: bool,
    generator: AgentDecisionGenerator,
) -> dict[str, Any]:
    supported = npc_id == TARGET_NPC_ID
    base: dict[str, Any] = {
        "enabled": enabled,
        "mode": "shadow" if enabled else "disabled",
        "supported": supported,
        "npc_id": npc_id,
        "status": "disabled" if not enabled else "waiting",
        "job": None,
        "utility": None,
        "agent": None,
        "validation": None,
        "comparison": None,
        "provider": generator.status(),
        "error_code": None,
    }
    if not supported:
        base["status"] = "unsupported"
        return base
    if not enabled:
        return base
    if generator.provider is None:
        base["status"] = "unavailable"
        base["error_code"] = "missing_api_key"
        return base
    job = session.scalar(
        select(AgentDecisionJob)
        .where(AgentDecisionJob.npc_id == npc_id)
        .order_by(AgentDecisionJob.id.desc())
        .limit(1)
    )
    if job is None:
        return base
    utility = session.get(DecisionLog, job.decision_id)
    perception = json.loads(job.perception_json)
    utility_reason = json.loads(utility.reason_json) if utility is not None else {}
    utility_target = _utility_target(utility.chosen_action, perception) if utility is not None else None
    base.update({
        "status": job.status,
        "job": {"id": job.id, "decision_id": job.decision_id, "attempts": job.attempts},
        "utility": None if utility is None else {
            "decision_id": utility.id,
            "world_day": utility.world_day,
            "world_time": utility.world_time,
            "action": utility.chosen_action,
            "target": utility_target,
            "reason_summary": utility_reason.get("summary"),
        },
        "error_code": job.last_error_code,
    })
    artifact = session.scalar(
        select(AgentDecisionArtifact).where(AgentDecisionArtifact.job_id == job.id)
    )
    if artifact is not None:
        base["agent"] = json.loads(artifact.decision_json)
        base["validation"] = json.loads(artifact.validation_json)
        base["comparison"] = json.loads(artifact.comparison_json)
        base["provider"] = {
            "available": True,
            "provider": artifact.provider,
            "model": artifact.model,
            "reason": None,
        }
    return base
