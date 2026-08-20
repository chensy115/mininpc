from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database.models import (
    Event,
    LongTermGoal,
    Memory,
    NPC,
    NarrativeArtifact,
    NarrativeJob,
    Relationship,
)
from simulation.clock import ClockSnapshot
from simulation.goals import GOAL_LABELS, goal_snapshots


NARRATIVE_KINDS = {"dialogue", "event_explanation", "goal_narrative", "memory_summary", "story_summary"}
IMPORTANT_EVENT_TYPES = {"WORK", "SOCIAL", "RELATIONSHIP"}
SUMMARY_BATCH_SIZE = 5


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_timeout(name: str, default: float) -> float:
    try:
        return max(0.2, float(os.getenv(name, str(default))))
    except ValueError:
        return default


@dataclass(frozen=True)
class NarrativeSettings:
    enabled: bool
    api_key: str | None
    base_url: str
    model: str
    timeout_seconds: float

    @classmethod
    def from_env(cls) -> "NarrativeSettings":
        api_key = os.getenv("MINIWORLD_LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
        return cls(
            enabled=_env_bool("MINIWORLD_LLM_ENABLED", bool(api_key)),
            api_key=api_key,
            base_url=os.getenv("MINIWORLD_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            model=os.getenv("MINIWORLD_LLM_MODEL", "gpt-4.1-mini"),
            timeout_seconds=_env_timeout("MINIWORLD_LLM_TIMEOUT", 8.0),
        )


class TextProvider(Protocol):
    name: str

    async def generate(self, kind: str, context: dict[str, Any]) -> str: ...


class OpenAICompatibleProvider:
    def __init__(self, settings: NarrativeSettings) -> None:
        self.settings = settings
        self.name = f"openai-compatible:{settings.model}"

    async def generate(self, kind: str, context: dict[str, Any]) -> str:
        schemas = {
            "dialogue": '{"lines":[{"speaker":"人物名","text":"一句话"}]}',
            "event_explanation": '{"text":"不超过120字的事实解释"}',
            "goal_narrative": '{"title":"不超过20字","motivation":"不超过100字"}',
            "memory_summary": '{"text":"不超过180字的第一人称总结"}',
            "story_summary": '{"text":"不超过240字的事实润色；不得输出或修改facts"}',
        }
        system = (
            "你是 MiniWorld 的只读叙事层。只能依据提供的已提交事实生成文字，不得发明或修改金钱、"
            "关系分、状态、地点、行为、时间、目标数值等世界事实，不得提出数据库操作或行动决策。"
            "人生故事任务中的 facts 已由 Engine 锁定；只能生成 text，不得生成、增删、重排或修改 facts。"
            "严格输出一个 JSON 对象，不要 Markdown，不要额外字段。"
        )
        user = f"任务：{kind}\n输出格式：{schemas[kind]}\n事实快照：{json.dumps(context, ensure_ascii=False)}"
        headers = {"Authorization": f"Bearer {self.settings.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.settings.model,
            "temperature": 0.2,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        async with httpx.AsyncClient(timeout=self.settings.timeout_seconds) as client:
            response = await client.post(f"{self.settings.base_url}/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        return str(data["choices"][0]["message"]["content"])


@dataclass(frozen=True)
class NarrativeResult:
    content: dict[str, Any]
    provider: str
    fallback_used: bool
    error: str | None = None


def _clean_text(value: Any, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError("narrative text must be a string")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value)).strip()
    if not text:
        raise ValueError("empty narrative text")
    return text[:maximum]


def _extract_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("narrative response must be an object")
    return value


def _validate(kind: str, value: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if kind == "dialogue":
        lines = value.get("lines")
        allowed = {str(context["actor_name"]), str(context["target_name"])}
        if not isinstance(lines, list) or not 2 <= len(lines) <= 4:
            raise ValueError("dialogue must contain 2-4 lines")
        cleaned = []
        for line in lines:
            if not isinstance(line, dict) or line.get("speaker") not in allowed:
                raise ValueError("dialogue speaker is not grounded")
            cleaned.append({"speaker": line["speaker"], "text": _clean_text(line.get("text"), 120)})
        return {"lines": cleaned}
    if kind == "goal_narrative":
        return {"title": _clean_text(value.get("title"), 30), "motivation": _clean_text(value.get("motivation"), 160)}
    maximum = 260 if kind == "story_summary" else 220 if kind == "memory_summary" else 160
    return {"text": _clean_text(value.get("text"), maximum)}


def fallback_content(kind: str, context: dict[str, Any]) -> dict[str, Any]:
    if kind == "dialogue":
        actor = context["actor_name"]
        target = context["target_name"]
        location = context.get("location_name", context.get("location", "这里"))
        return {"lines": [
            {"speaker": actor, "text": f"在{location}遇到你真巧，最近过得怎么样？"},
            {"speaker": target, "text": "还不错，谢谢你来聊聊。"},
        ]}
    if kind == "event_explanation":
        event_type = context.get("event_type")
        reason = {
            "WORK": "它推进了既有的工作与储蓄目标",
            "SOCIAL": "它形成了一次真实的面对面互动",
            "RELATIONSHIP": "它记录了互动后由模拟引擎计算的关系变化",
        }.get(event_type, "它是世界中已经发生并提交的重要事实")
        return {"text": f"{context['description']}。这件事值得关注，因为{reason}。"}
    if kind == "goal_narrative":
        label = context.get("label") or GOAL_LABELS.get(context.get("type"), "长期目标")
        target = context.get("target_npc_name")
        suffix = f"，并把与 {target} 的相处放在心上" if target else ""
        return {"title": str(label), "motivation": f"我想稳步推进这个目标{suffix}，用每天真实发生的行动积累进展。"}
    if kind == "story_summary":
        facts = context.get("facts", [])
        milestones = [item for item in facts if item.get("fact_type") == "milestone"]
        period = "本月" if context.get("period_type") == "month" else "本周"
        if not milestones:
            return {"text": f"{period}没有形成新的人生里程碑；结构化事实清单保持为空。"}
        titles = "；".join(str(item.get("title", "已记录里程碑")) for item in milestones)
        return {"text": f"{period}已固化 {len(milestones)} 项人生里程碑：{titles}。所有事实和顺序均以 Engine 清单为准。"}
    memories = context.get("memories", [])
    selected = sorted(memories, key=lambda item: (-int(item["importance"]), -int(item["id"])))[:3]
    details = "；".join(str(item["content"]) for item in selected)
    return {"text": f"最近让我印象较深的是：{details}。这些都是我亲历事件留下的记录。"}


class NarrativeGenerator:
    def __init__(self, settings: NarrativeSettings | None = None, provider: TextProvider | None = None) -> None:
        self.settings = settings or NarrativeSettings.from_env()
        self.provider = provider
        if self.provider is None and self.settings.enabled and self.settings.api_key:
            self.provider = OpenAICompatibleProvider(self.settings)

    def status(self) -> dict[str, Any]:
        if self.provider is not None:
            return {"mode": "llm", "available": True, "provider": self.provider.name, "model": self.settings.model}
        reason = "disabled" if not self.settings.enabled else "missing_api_key"
        return {"mode": "fallback", "available": True, "provider": "deterministic-template", "model": None, "reason": reason}

    async def generate(self, kind: str, context: dict[str, Any]) -> NarrativeResult:
        if kind not in NARRATIVE_KINDS:
            raise ValueError(f"unsupported narrative kind: {kind}")
        fallback = _validate(kind, fallback_content(kind, context), context)
        if self.provider is None:
            return NarrativeResult(fallback, "deterministic-template", True, self.status().get("reason"))
        try:
            raw = await self.provider.generate(kind, context)
            return NarrativeResult(_validate(kind, _extract_json(raw), context), self.provider.name, False)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:500]
            return NarrativeResult(fallback, "deterministic-template", True, error)


def _add_job(session: Session, **values: Any) -> bool:
    dedupe_key = values["dedupe_key"]
    if session.scalar(select(NarrativeJob.id).where(NarrativeJob.dedupe_key == dedupe_key)) is not None:
        return False
    session.add(NarrativeJob(**values))
    return True


def ensure_goal_narrative_jobs(session: Session, npcs: list[NPC], created_minute: int) -> int:
    created = 0
    for npc in npcs:
        for goal in goal_snapshots(session, npc):
            context = {
                "npc_name": npc.name,
                "label": goal["label"],
                "type": goal["type"],
                "target_value": goal["target_value"],
                "priority": goal["priority"],
                "current_value": goal["current_value"],
                "target_npc_name": goal["target_npc_name"],
            }
            created += _add_job(
                session,
                kind="goal_narrative",
                dedupe_key=f"goal_narrative:{goal['id']}",
                npc_id=npc.id,
                goal_id=goal["id"],
                context_json=json.dumps(context, ensure_ascii=False),
                created_minute=created_minute,
            )
    return created


def enqueue_event_jobs(
    session: Session,
    after_event_id: int,
    created_minute: int,
    *,
    suppress_dialogue_event_ids: set[int] | None = None,
) -> int:
    events = list(session.scalars(select(Event).where(Event.id > after_event_id).order_by(Event.id)))
    if not events:
        return 0
    names = {npc.id: npc.name for npc in session.scalars(select(NPC))}
    relationship_changes = {
        (event.npc_id, event.target_npc_id, event.world_day, event.world_time): json.loads(event.metadata_json).get("change")
        for event in events if event.event_type == "RELATIONSHIP"
    }
    created = 0
    for event in events:
        context = {
            "event_id": event.id,
            "event_type": event.event_type,
            "description": event.description,
            "world_day": event.world_day,
            "world_time": event.world_time,
            "actor_name": names.get(event.npc_id),
            "target_name": names.get(event.target_npc_id),
            "location": event.location,
        }
        if event.event_type in IMPORTANT_EVENT_TYPES:
            created += _add_job(
                session,
                kind="event_explanation",
                dedupe_key=f"event_explanation:{event.id}",
                npc_id=event.npc_id,
                related_npc_id=event.target_npc_id,
                event_id=event.id,
                context_json=json.dumps(context, ensure_ascii=False),
                created_minute=created_minute,
            )
        if (
            event.event_type == "SOCIAL"
            and event.target_npc_id is not None
            and event.id not in (suppress_dialogue_event_ids or set())
        ):
            context["location_name"] = {"Home": "家", "Office": "办公室", "Cafe": "咖啡馆", "Park": "公园"}.get(event.location, event.location)
            context["relationship_change"] = relationship_changes.get(
                (event.npc_id, event.target_npc_id, event.world_day, event.world_time)
            )
            created += _add_job(
                session,
                kind="dialogue",
                dedupe_key=f"dialogue:{event.id}",
                npc_id=event.npc_id,
                related_npc_id=event.target_npc_id,
                event_id=event.id,
                context_json=json.dumps(context, ensure_ascii=False),
                created_minute=created_minute,
            )
    return created


def enqueue_memory_summary_jobs(session: Session, created_minute: int) -> int:
    created = 0
    for npc in session.scalars(select(NPC).order_by(NPC.id)):
        latest_end = session.scalar(
            select(func.max(NarrativeArtifact.source_memory_end_id)).where(
                NarrativeArtifact.kind == "memory_summary", NarrativeArtifact.npc_id == npc.id
            )
        ) or 0
        memories = list(session.scalars(
            select(Memory).where(Memory.npc_id == npc.id, Memory.id > latest_end).order_by(Memory.id).limit(20)
        ))
        if len(memories) < SUMMARY_BATCH_SIZE:
            continue
        start_id, end_id = memories[0].id, memories[-1].id
        context = {
            "npc_name": npc.name,
            "memories": [
                {"id": item.id, "content": item.content, "importance": item.importance, "emotion": item.emotion, "timestamp": item.timestamp}
                for item in memories
            ],
        }
        created += _add_job(
            session,
            kind="memory_summary",
            dedupe_key=f"memory_summary:{npc.id}:{end_id}",
            npc_id=npc.id,
            source_memory_start_id=start_id,
            source_memory_end_id=end_id,
            context_json=json.dumps(context, ensure_ascii=False),
            created_minute=created_minute,
        )
    return created


def artifact_to_dict(artifact: NarrativeArtifact, names: dict[int, str]) -> dict[str, Any]:
    return {
        "id": artifact.id,
        "kind": artifact.kind,
        "npc_id": artifact.npc_id,
        "npc_name": names.get(artifact.npc_id),
        "related_npc_id": artifact.related_npc_id,
        "related_npc_name": names.get(artifact.related_npc_id),
        "event_id": artifact.event_id,
        "goal_id": artifact.goal_id,
        "source_memory_start_id": artifact.source_memory_start_id,
        "source_memory_end_id": artifact.source_memory_end_id,
        "content": json.loads(artifact.content_json),
        "provider": artifact.provider,
        "fallback_used": artifact.fallback_used,
        "error": artifact.error,
        "created_minute": artifact.created_minute,
        "time_label": ClockSnapshot(artifact.created_minute).label,
    }


async def process_jobs(session_factory, generator: NarrativeGenerator, limit: int = 10) -> int:
    processed = 0
    for _ in range(max(0, limit)):
        with session_factory() as session:
            job = session.scalar(select(NarrativeJob).where(NarrativeJob.status == "pending").order_by(NarrativeJob.id).limit(1))
            if job is None:
                break
            job.status = "processing"
            job.attempts += 1
            job_id = job.id
            kind = job.kind
            context = json.loads(job.context_json)
            session.commit()
        result = await generator.generate(kind, context)
        with session_factory() as session:
            job = session.get(NarrativeJob, job_id)
            if job is None:
                continue
            if session.scalar(select(NarrativeArtifact.id).where(NarrativeArtifact.job_id == job.id)) is None:
                session.add(NarrativeArtifact(
                    job_id=job.id,
                    kind=job.kind,
                    npc_id=job.npc_id,
                    related_npc_id=job.related_npc_id,
                    event_id=job.event_id,
                    goal_id=job.goal_id,
                    source_memory_start_id=job.source_memory_start_id,
                    source_memory_end_id=job.source_memory_end_id,
                    content_json=json.dumps(result.content, ensure_ascii=False),
                    provider=result.provider,
                    fallback_used=result.fallback_used,
                    error=result.error,
                    created_minute=job.created_minute,
                ))
            job.status = "completed"
            job.last_error = result.error
            job.completed_at = datetime.now(timezone.utc)
            session.commit()
            processed += 1
    return processed


def reset_interrupted_jobs(session: Session) -> int:
    jobs = list(session.scalars(select(NarrativeJob).where(NarrativeJob.status == "processing")))
    for job in jobs:
        job.status = "pending"
        job.last_error = "recovered_after_restart"
    return len(jobs)
