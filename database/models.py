from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class WorldState(Base):
    __tablename__ = "world_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    total_minutes: Mapped[int] = mapped_column(Integer, default=480, nullable=False)
    paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    speed: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    seed: Mapped[int] = mapped_column(Integer, default=42, nullable=False)
    random_counter: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class NPC(Base):
    __tablename__ = "npcs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    age: Mapped[int] = mapped_column(Integer, nullable=False)
    job: Mapped[str] = mapped_column(String(80), nullable=False)
    current_location: Mapped[str] = mapped_column(String(30), nullable=False)
    current_action: Mapped[str] = mapped_column(String(30), default="Idle", nullable=False)
    action_end_minute: Mapped[int] = mapped_column(Integer, default=480, nullable=False)
    pending_location: Mapped[str | None] = mapped_column(String(30), nullable=True)
    last_move_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    money: Mapped[float] = mapped_column(Float, default=100.0, nullable=False)

    energy: Mapped[float] = mapped_column(Float, nullable=False)
    hunger: Mapped[float] = mapped_column(Float, nullable=False)
    mood: Mapped[float] = mapped_column(Float, nullable=False)
    social_need: Mapped[float] = mapped_column(Float, nullable=False)
    work_satisfaction: Mapped[float] = mapped_column(Float, nullable=False)

    extroversion: Mapped[float] = mapped_column(Float, nullable=False)
    kindness: Mapped[float] = mapped_column(Float, nullable=False)
    ambition: Mapped[float] = mapped_column(Float, nullable=False)
    risk_tolerance: Mapped[float] = mapped_column(Float, nullable=False)
    discipline: Mapped[float] = mapped_column(Float, nullable=False)


class Relationship(Base):
    __tablename__ = "relationships"
    __table_args__ = (UniqueConstraint("from_npc_id", "to_npc_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_npc_id: Mapped[int] = mapped_column(ForeignKey("npcs.id", ondelete="CASCADE"), nullable=False)
    to_npc_id: Mapped[int] = mapped_column(ForeignKey("npcs.id", ondelete="CASCADE"), nullable=False)
    score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    world_day: Mapped[int] = mapped_column(Integer, nullable=False)
    world_time: Mapped[str] = mapped_column(String(5), nullable=False)
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    npc_id: Mapped[int | None] = mapped_column(ForeignKey("npcs.id", ondelete="SET NULL"), nullable=True)
    target_npc_id: Mapped[int | None] = mapped_column(
        ForeignKey("npcs.id", ondelete="SET NULL"), nullable=True
    )
    location: Mapped[str | None] = mapped_column(String(30), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[str] = mapped_column("metadata", Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class DecisionLog(Base):
    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    npc_id: Mapped[int] = mapped_column(ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False)
    world_day: Mapped[int] = mapped_column(Integer, nullable=False)
    world_time: Mapped[str] = mapped_column(String(5), nullable=False)
    chosen_action: Mapped[str] = mapped_column(String(30), nullable=False)
    candidates_json: Mapped[str] = mapped_column(Text, nullable=False)
    reason_json: Mapped[str] = mapped_column(Text, nullable=False)


class Memory(Base):
    __tablename__ = "memories"
    __table_args__ = (CheckConstraint("importance BETWEEN 1 AND 10", name="ck_memory_importance"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    importance: Mapped[int] = mapped_column(Integer, nullable=False)
    emotion: Mapped[str] = mapped_column(String(30), nullable=False)
    # Simulated-world minutes are stable across restarts and preserve exact ordering.
    timestamp: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    related_npc_id: Mapped[int | None] = mapped_column(
        ForeignKey("npcs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class LongTermGoal(Base):
    """Additive V0.3 goal state; existing V0.1/V0.2 tables remain untouched."""

    __tablename__ = "long_term_goals"
    __table_args__ = (
        UniqueConstraint("npc_id", "goal_key"),
        CheckConstraint("priority BETWEEN 0 AND 1", name="ck_goal_priority"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    goal_key: Mapped[str] = mapped_column(String(80), nullable=False)
    goal_type: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    target_value: Mapped[float] = mapped_column(Float, nullable=False)
    priority: Mapped[float] = mapped_column(Float, nullable=False)
    target_npc_id: Mapped[int | None] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), nullable=True
    )
    created_minute: Mapped[int] = mapped_column(Integer, nullable=False)


class NarrativeJob(Base):
    """Durable V0.4 work queue. Jobs contain fact snapshots, never write commands."""

    __tablename__ = "narrative_jobs"
    __table_args__ = (
        UniqueConstraint("dedupe_key"),
        CheckConstraint("status IN ('pending', 'processing', 'completed')", name="ck_narrative_job_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(120), nullable=False)
    npc_id: Mapped[int | None] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=True
    )
    related_npc_id: Mapped[int | None] = mapped_column(
        ForeignKey("npcs.id", ondelete="SET NULL"), nullable=True
    )
    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), index=True, nullable=True
    )
    goal_id: Mapped[int | None] = mapped_column(
        ForeignKey("long_term_goals.id", ondelete="CASCADE"), index=True, nullable=True
    )
    source_memory_start_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_memory_end_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NarrativeArtifact(Base):
    """Text-only LLM/fallback output, isolated from simulation-authoritative tables."""

    __tablename__ = "narrative_artifacts"
    __table_args__ = (UniqueConstraint("job_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("narrative_jobs.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    npc_id: Mapped[int | None] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=True
    )
    related_npc_id: Mapped[int | None] = mapped_column(
        ForeignKey("npcs.id", ondelete="SET NULL"), nullable=True
    )
    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), index=True, nullable=True
    )
    goal_id: Mapped[int | None] = mapped_column(
        ForeignKey("long_term_goals.id", ondelete="CASCADE"), index=True, nullable=True
    )
    source_memory_start_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_memory_end_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_json: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class AgentDecisionJob(Base):
    """Durable V1.1 shadow-decision queue containing a bounded perception snapshot."""

    __tablename__ = "agent_decision_jobs"
    __table_args__ = (
        UniqueConstraint("decision_id"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_agent_decision_job_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    decision_id: Mapped[int] = mapped_column(
        ForeignKey("decisions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    perception_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentDecisionArtifact(Base):
    """Validated V1.1 advice only; it has no authority over simulation facts."""

    __tablename__ = "agent_decision_artifacts"
    __table_args__ = (UniqueConstraint("job_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("agent_decision_jobs.id", ondelete="CASCADE"), nullable=False
    )
    decision_json: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    legal: Mapped[bool] = mapped_column(Boolean, nullable=False)
    validation_json: Mapped[str] = mapped_column(Text, nullable=False)
    comparison_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class AgentTakeoverTurn(Base):
    """Durable V1.3 ownership and audit record for one NPC action boundary.

    V1.1 jobs/artifacts remain the bounded network work queue. This row is the
    Engine-owned bridge which records waiting, the validated final authority,
    exact action parameters, and completion. It never grants a provider write
    access to simulation facts.
    """

    __tablename__ = "agent_takeover_turns"
    __table_args__ = (
        UniqueConstraint("decision_id"),
        UniqueConstraint("job_id"),
        CheckConstraint(
            "state IN ('waiting', 'ready', 'agent_executing', "
            "'fallback_executing', 'completed')",
            name="ck_agent_takeover_state",
        ),
        CheckConstraint(
            "worker_state IN ('not_queued', 'pending', 'processing', "
            "'completed', 'failed', 'discarded')",
            name="ck_agent_takeover_worker_state",
        ),
        CheckConstraint(
            "final_source IS NULL OR final_source IN ('agent', 'utility_fallback')",
            name="ck_agent_takeover_final_source",
        ),
        Index(
            "uq_agent_takeover_active_npc",
            "npc_id",
            unique=True,
            sqlite_where=text(
                "state IN ('waiting', 'ready', 'agent_executing', 'fallback_executing')"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    decision_id: Mapped[int] = mapped_column(
        ForeignKey("decisions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_decision_jobs.id", ondelete="CASCADE"), index=True, nullable=True
    )
    npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False
    )

    state: Mapped[str] = mapped_column(String(30), default="waiting", index=True, nullable=False)
    worker_state: Mapped[str] = mapped_column(
        String(20), default="pending", index=True, nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(80), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    response_deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    created_minute: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    valid_until_minute: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    options_json: Mapped[str] = mapped_column(Text, nullable=False)
    utility_action: Mapped[str] = mapped_column(String(30), nullable=False)
    utility_target: Mapped[str | None] = mapped_column(String(100), nullable=True)
    utility_reason_json: Mapped[str] = mapped_column(Text, nullable=False)

    agent_decision_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_action: Mapped[str | None] = mapped_column(String(30), nullable=True)
    agent_target: Mapped[str | None] = mapped_column(String(100), nullable=True)
    snapshot_validation_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_validation_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    final_source: Mapped[str | None] = mapped_column(String(30), nullable=True)
    final_action: Mapped[str | None] = mapped_column(String(30), nullable=True)
    final_target: Mapped[str | None] = mapped_column(String(100), nullable=True)
    final_params_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    fallback_reason_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    completion_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    action_started_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action_end_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action_completed_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc), nullable=False
    )


class AgentConversation(Base):
    """V1.4 durable dialogue state rooted in one Engine-committed SOCIAL event."""

    __tablename__ = "agent_conversations"
    __table_args__ = (
        UniqueConstraint("social_event_id"),
        CheckConstraint("target_turn_count BETWEEN 3 AND 6", name="ck_agent_conversation_turn_bound"),
        CheckConstraint(
            "status IN ('active', 'ready_for_settlement', 'completed', 'failed', "
            "'cancelled', 'expired')",
            name="ck_agent_conversation_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    social_event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )
    actor_npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    target_npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    location: Mapped[str] = mapped_column(String(30), nullable=False)
    created_minute: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    target_turn_count: Mapped[int] = mapped_column(Integer, nullable=False)
    next_turn_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enabled_npc_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    fact_boundary_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="active", index=True, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settled_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc), nullable=False
    )


class AgentConversationTask(Base):
    """One idempotent queued utterance; its private context is never returned by APIs."""

    __tablename__ = "agent_conversation_tasks"
    __table_args__ = (
        UniqueConstraint("conversation_id", "turn_index"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'discarded')",
            name="ck_agent_conversation_task_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("agent_conversations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    speaker_npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    listener_npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), nullable=False
    )
    context_json: Mapped[str] = mapped_column(Text, nullable=False)
    context_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lease_token: Mapped[str | None] = mapped_column(String(80), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    response_deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentConversationTurn(Base):
    """Strict validated text fields only; no action or fact mutation fields exist."""

    __tablename__ = "agent_conversation_turns"
    __table_args__ = (
        UniqueConstraint("conversation_id", "turn_index"),
        UniqueConstraint("task_id"),
        CheckConstraint("length(utterance) BETWEEN 1 AND 280", name="ck_agent_conversation_text_bound"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("agent_conversations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    task_id: Mapped[int] = mapped_column(
        ForeignKey("agent_conversation_tasks.id", ondelete="CASCADE"), nullable=False
    )
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    speaker_npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    listener_npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), nullable=False
    )
    utterance: Mapped[str] = mapped_column(Text, nullable=False)
    emotion_summary: Mapped[str] = mapped_column(String(80), nullable=False)
    intent_summary: Mapped[str] = mapped_column(String(120), nullable=False)
    conversation_act: Mapped[str | None] = mapped_column(String(30), nullable=True)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class AgentConversationParticipantResult(Base):
    """Engine-settled subjective result and first-person memory link for one participant."""

    __tablename__ = "agent_conversation_participant_results"
    __table_args__ = (UniqueConstraint("conversation_id", "npc_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("agent_conversations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    related_npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), nullable=False
    )
    subjective_summary: Mapped[str] = mapped_column(Text, nullable=False)
    emotion: Mapped[str] = mapped_column(String(30), nullable=False)
    importance: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_id: Mapped[int] = mapped_column(
        ForeignKey("memories.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    provider_summary: Mapped[str] = mapped_column(String(120), nullable=False)
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    settled_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class AgentConversationAudit(Base):
    """Sanitized orchestration/fact-boundary audit; never stores prompts, keys, or raw errors."""

    __tablename__ = "agent_conversation_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("agent_conversations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_conversation_tasks.id", ondelete="CASCADE"), nullable=True
    )
    code: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    details_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class AgentCognitionState(Base):
    """V1.5 per-NPC cognitive continuity, isolated from objective world facts."""

    __tablename__ = "agent_cognition_states"
    __table_args__ = (UniqueConstraint("npc_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )
    current_goal_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_daily_enqueued_day: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_reflected_day: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_milestone_id: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_minute: Mapped[int] = mapped_column(Integer, nullable=False)


class AgentReflectionTask(Base):
    """Bounded durable reflection queue; context contains only one NPC's visible sources."""

    __tablename__ = "agent_reflection_tasks"
    __table_args__ = (
        UniqueConstraint("dedupe_key"),
        CheckConstraint("trigger_type IN ('daily', 'milestone')", name="ck_reflection_trigger"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'cancelled', 'discarded')",
            name="ck_reflection_task_status",
        ),
        Index(
            "uq_agent_reflection_active_npc",
            "npc_id",
            unique=True,
            sqlite_where=text("status IN ('pending', 'processing')"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(180), nullable=False)
    npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    reflection_day: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(20), nullable=False)
    trigger_source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_json: Mapped[str] = mapped_column(Text, nullable=False)
    context_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lease_token: Mapped[str | None] = mapped_column(String(80), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    response_deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_minute: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentReflectionSource(Base):
    """Stable auditable source IDs/summaries supplied to exactly one reflection task."""

    __tablename__ = "agent_reflection_sources"
    __table_args__ = (UniqueConstraint("task_id", "source_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("agent_reflection_tasks.id", ondelete="CASCADE"), index=True, nullable=False
    )
    npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source_key: Mapped[str] = mapped_column(String(120), nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    source_row_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[str] = mapped_column(String(320), nullable=False)
    range_start_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    range_end_minute: Mapped[int] = mapped_column(Integer, nullable=False)


class AgentReflection(Base):
    """Strict validated reflective text with no columns capable of changing world facts."""

    __tablename__ = "agent_reflections"
    __table_args__ = (UniqueConstraint("task_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("agent_reflection_tasks.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    reflection_day: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(20), nullable=False)
    day_summary: Mapped[str] = mapped_column(String(500), nullable=False)
    emotion_summary: Mapped[str] = mapped_column(String(160), nullable=False)
    lessons_json: Mapped[str] = mapped_column(Text, nullable=False)
    goal_focus: Mapped[str] = mapped_column(String(80), nullable=False)
    reason_summary: Mapped[str] = mapped_column(String(500), nullable=False)
    plan_adjustments_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    fact_boundary_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class AgentSubjectiveBelief(Base):
    """Versioned subjective belief; target/evidence remain explicitly non-objective."""

    __tablename__ = "agent_subjective_beliefs"
    __table_args__ = (
        CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_belief_confidence"),
        UniqueConstraint("reflection_id", "target", "belief_text"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    reflection_id: Mapped[int] = mapped_column(
        ForeignKey("agent_reflections.id", ondelete="CASCADE"), index=True, nullable=False
    )
    target: Mapped[str] = mapped_column(String(100), nullable=False)
    belief_text: Mapped[str] = mapped_column(String(280), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_minute: Mapped[int] = mapped_column(Integer, nullable=False)


class AgentPlan(Base):
    """Engine-monitored intention. A plan is never itself an executable action."""

    __tablename__ = "agent_plans"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'in_progress', 'completed', 'failed', 'expired', 'cancelled')",
            name="ck_agent_plan_status",
        ),
        UniqueConstraint("reflection_id", "sequence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    reflection_id: Mapped[int] = mapped_column(
        ForeignKey("agent_reflections.id", ondelete="CASCADE"), index=True, nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    goal_key: Mapped[str] = mapped_column(String(80), nullable=False)
    action_category: Mapped[str] = mapped_column(String(40), nullable=False)
    target: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description: Mapped[str] = mapped_column(String(240), nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False)
    window_start_day: Mapped[int] = mapped_column(Integer, nullable=False)
    window_end_day: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True, nullable=False)
    progress_reason: Mapped[str | None] = mapped_column(String(240), nullable=True)
    progress_source_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    progress_source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_minute: Mapped[int] = mapped_column(Integer, nullable=False)


class ModelRuntimeState(Base):
    """Persisted V1.6 control plane; it never contains provider credentials."""

    __tablename__ = "model_runtime_state"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('safe', 'online', 'paused', 'emergency_stop')",
            name="ck_model_runtime_mode",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    mode: Mapped[str] = mapped_column(String(24), default="safe", nullable=False)
    generation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enabled_npc_ids_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    emergency_reason: Mapped[str | None] = mapped_column(String(160), nullable=True)
    last_transition: Mapped[str] = mapped_column(String(40), default="initialized", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc), nullable=False
    )


class ModelBudgetConfig(Base):
    """Local call/token/cost guardrails. Prices are user supplied, never baked in."""

    __tablename__ = "model_budget_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    timezone_name: Mapped[str] = mapped_column(String(80), default="Asia/Shanghai", nullable=False)
    calls_per_minute: Mapped[int] = mapped_column(Integer, default=6, nullable=False)
    calls_per_hour: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    calls_per_day: Mapped[int] = mapped_column(Integer, default=120, nullable=False)
    calls_per_npc_hour: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    calls_per_npc_day: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    calls_per_task_hour: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    calls_per_task_day: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    input_tokens_per_day: Mapped[int] = mapped_column(Integer, default=120000, nullable=False)
    output_tokens_per_day: Mapped[int] = mapped_column(Integer, default=30000, nullable=False)
    total_tokens_per_day: Mapped[int] = mapped_column(Integer, default=150000, nullable=False)
    tokens_per_npc_day: Mapped[int] = mapped_column(Integer, default=40000, nullable=False)
    tokens_per_task_day: Mapped[int] = mapped_column(Integer, default=75000, nullable=False)
    estimated_cost_per_day: Mapped[float | None] = mapped_column(Float, nullable=True)
    input_price_per_million: Mapped[float | None] = mapped_column(Float, nullable=True)
    output_price_per_million: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(12), default="CNY", nullable=False)
    budget_epoch: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc), nullable=False
    )


class ModelCircuitState(Base):
    """Durable provider and per-NPC circuit breaker state."""

    __tablename__ = "model_circuit_states"
    __table_args__ = (
        UniqueConstraint("scope", "scope_key"),
        CheckConstraint("state IN ('closed', 'open', 'half_open')", name="ck_model_circuit_state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(80), nullable=False)
    state: Mapped[str] = mapped_column(String(20), default="closed", nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    half_open_in_flight: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_error_class: Mapped[str | None] = mapped_column(String(60), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc), nullable=False
    )


class ModelCallAudit(Base):
    """Metadata-only V1.6 call audit; prompts, responses and secrets are prohibited."""

    __tablename__ = "model_call_audits"
    __table_args__ = (
        CheckConstraint(
            "task_type IN ('decision', 'conversation', 'reflection')",
            name="ck_model_call_task_type",
        ),
        Index("ix_model_call_day_task_npc", "local_day", "task_type", "npc_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    budget_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    model: Mapped[str] = mapped_column(String(160), nullable=False)
    task_type: Mapped[str] = mapped_column(String(24), nullable=False)
    npc_id: Mapped[int | None] = mapped_column(
        ForeignKey("npcs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    local_day: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(30), index=True, nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_class: Mapped[str | None] = mapped_column(String(60), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    usage_reported: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    estimated_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(12), nullable=True)
    fallback: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    late: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ModelRuntimeAudit(Base):
    """Secret-free operator audit for runtime switches and budget changes."""

    __tablename__ = "model_runtime_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    details_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class EmploymentProfile(Base):
    """Additive V0.5 career state; the legacy NPC.job field remains authoritative-compatible."""

    __tablename__ = "employment_profiles"
    __table_args__ = (UniqueConstraint("npc_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    profession_key: Mapped[str] = mapped_column(String(80), nullable=False)
    employer: Mapped[str] = mapped_column(String(100), nullable=False)
    base_wage: Mapped[float] = mapped_column(Float, nullable=False)
    performance: Mapped[float] = mapped_column(Float, default=60.0, nullable=False)
    experience: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    shifts_completed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_earnings: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)


class NPCSkill(Base):
    __tablename__ = "npc_skills"
    __table_args__ = (UniqueConstraint("npc_id", "skill_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    skill_key: Mapped[str] = mapped_column(String(80), nullable=False)
    level: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    experience: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)


class Store(Base):
    __tablename__ = "stores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    location: Mapped[str] = mapped_column(String(30), nullable=False)
    revenue: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)


class ItemDefinition(Base):
    __tablename__ = "item_definitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    base_price: Mapped[float] = mapped_column(Float, nullable=False)
    effect_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)


class StoreListing(Base):
    __tablename__ = "store_listings"
    __table_args__ = (UniqueConstraint("store_id", "item_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), index=True, nullable=False
    )
    item_id: Mapped[int] = mapped_column(
        ForeignKey("item_definitions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    price: Mapped[float] = mapped_column(Float, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class InventoryItem(Base):
    __tablename__ = "inventory_items"
    __table_args__ = (UniqueConstraint("npc_id", "item_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    item_id: Mapped[int] = mapped_column(
        ForeignKey("item_definitions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Housing(Base):
    __tablename__ = "housing"
    __table_args__ = (UniqueConstraint("npc_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    tier: Mapped[str] = mapped_column(String(30), nullable=False)
    weekly_rent: Mapped[float] = mapped_column(Float, nullable=False)
    comfort: Mapped[float] = mapped_column(Float, nullable=False)
    next_rent_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    arrears: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)


class EconomicTransaction(Base):
    __tablename__ = "economic_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    world_minute: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    balance_after: Mapped[float] = mapped_column(Float, nullable=False)
    item_id: Mapped[int | None] = mapped_column(
        ForeignKey("item_definitions.id", ondelete="SET NULL"), nullable=True
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)


class CareerDevelopment(Base):
    """Additive V0.6 career-cycle state; V0.5 employment rows remain intact."""

    __tablename__ = "career_development"
    __table_args__ = (UniqueConstraint("npc_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    employment_status: Mapped[str] = mapped_column(String(20), default="employed", nullable=False)
    career_level: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_review_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    next_review_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    reviews_completed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    strong_reviews: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    weak_reviews: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unemployment_since_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    applications_submitted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_transition_reason: Mapped[str] = mapped_column(Text, default="初始职业", nullable=False)


class PerformanceReview(Base):
    __tablename__ = "performance_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    world_minute: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    period_start_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    period_end_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    outcome: Mapped[str] = mapped_column(String(30), nullable=False)
    wage_before: Mapped[float] = mapped_column(Float, nullable=False)
    wage_after: Mapped[float] = mapped_column(Float, nullable=False)
    career_level_before: Mapped[int] = mapped_column(Integer, nullable=False)
    career_level_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reasons_json: Mapped[str] = mapped_column(Text, nullable=False)


class CareerTransition(Base):
    __tablename__ = "career_transitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    world_minute: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    transition_type: Mapped[str] = mapped_column(String(30), nullable=False)
    from_profession: Mapped[str] = mapped_column(String(80), nullable=False)
    to_profession: Mapped[str | None] = mapped_column(String(80), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)


class PersonalBudget(Base):
    __tablename__ = "personal_budgets"
    __table_args__ = (UniqueConstraint("npc_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    period_start_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    food_budget: Mapped[float] = mapped_column(Float, nullable=False)
    housing_budget: Mapped[float] = mapped_column(Float, nullable=False)
    learning_budget: Mapped[float] = mapped_column(Float, nullable=False)
    entertainment_budget: Mapped[float] = mapped_column(Float, nullable=False)
    savings_budget: Mapped[float] = mapped_column(Float, nullable=False)
    updated_minute: Mapped[int] = mapped_column(Integer, nullable=False)


class WeeklyEconomicReport(Base):
    __tablename__ = "weekly_economic_reports"
    __table_args__ = (UniqueConstraint("npc_id", "period_start_minute"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    period_start_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    period_end_minute: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    income: Mapped[float] = mapped_column(Float, nullable=False)
    food_spent: Mapped[float] = mapped_column(Float, nullable=False)
    housing_spent: Mapped[float] = mapped_column(Float, nullable=False)
    learning_spent: Mapped[float] = mapped_column(Float, nullable=False)
    entertainment_spent: Mapped[float] = mapped_column(Float, nullable=False)
    saved: Mapped[float] = mapped_column(Float, nullable=False)
    disposable_income: Mapped[float] = mapped_column(Float, nullable=False)
    economic_pressure: Mapped[float] = mapped_column(Float, nullable=False)
    reasons_json: Mapped[str] = mapped_column(Text, nullable=False)


class CommunityInstitution(Base):
    """Fixed V0.7 institution on one of the existing logical location nodes."""

    __tablename__ = "community_institutions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    institution_key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    institution_type: Mapped[str] = mapped_column(String(40), nullable=False)
    location: Mapped[str] = mapped_column(String(30), nullable=False)
    weekday_open_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    weekday_close_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    weekend_open_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    weekend_close_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    service_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    daily_capacity: Mapped[int | None] = mapped_column(Integer, nullable=True)


class WorkSchedule(Base):
    __tablename__ = "work_schedules"
    __table_args__ = (UniqueConstraint("npc_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    workdays_json: Mapped[str] = mapped_column(Text, default="[0, 1, 2, 3, 4]", nullable=False)
    start_minute: Mapped[int] = mapped_column(Integer, default=540, nullable=False)
    end_minute: Mapped[int] = mapped_column(Integer, default=1020, nullable=False)
    grace_minutes: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    on_time_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    late_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    shifts_completed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class WorkAttendance(Base):
    __tablename__ = "work_attendance"
    __table_args__ = (UniqueConstraint("npc_id", "world_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    world_day: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    scheduled_start_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    first_arrival_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    minutes_late: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    worked_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class StoreStock(Base):
    __tablename__ = "store_stock"
    __table_args__ = (UniqueConstraint("listing_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("store_listings.id", ondelete="CASCADE"), index=True, nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    restock_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    restock_interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    last_restock_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    next_restock_minute: Mapped[int] = mapped_column(Integer, index=True, nullable=False)


class RestockEvent(Base):
    __tablename__ = "restock_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stock_id: Mapped[int | None] = mapped_column(
        ForeignKey("store_stock.id", ondelete="SET NULL"), index=True, nullable=True
    )
    world_minute: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    quantity_before: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity_added: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity_after: Mapped[int] = mapped_column(Integer, nullable=False)


class FacilityUsage(Base):
    __tablename__ = "facility_usage"
    __table_args__ = (UniqueConstraint("npc_id", "institution_id", "world_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    institution_id: Mapped[int] = mapped_column(
        ForeignKey("community_institutions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    world_day: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    world_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    service_key: Mapped[str] = mapped_column(String(80), nullable=False)
    outcome_json: Mapped[str] = mapped_column(Text, nullable=False)


class TrainingRecord(Base):
    __tablename__ = "training_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    institution_id: Mapped[int] = mapped_column(
        ForeignKey("community_institutions.id", ondelete="CASCADE"), nullable=False
    )
    world_minute: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    week_start_minute: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    profession_key: Mapped[str] = mapped_column(String(80), nullable=False)
    skill_key: Mapped[str] = mapped_column(String(80), nullable=False)
    fee: Mapped[float] = mapped_column(Float, nullable=False)
    skill_experience: Mapped[float] = mapped_column(Float, nullable=False)
    leveled_up: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class HousingUpgradeRecord(Base):
    __tablename__ = "housing_upgrade_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    world_minute: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    tier_before: Mapped[str] = mapped_column(String(30), nullable=False)
    tier_after: Mapped[str] = mapped_column(String(30), nullable=False)
    cost: Mapped[float] = mapped_column(Float, nullable=False)
    weekly_rent_before: Mapped[float] = mapped_column(Float, nullable=False)
    weekly_rent_after: Mapped[float] = mapped_column(Float, nullable=False)
    comfort_before: Mapped[float] = mapped_column(Float, nullable=False)
    comfort_after: Mapped[float] = mapped_column(Float, nullable=False)


class SocialBond(Base):
    """V0.8 bidirectional view over the two legacy directed relationship rows."""

    __tablename__ = "social_bonds"
    __table_args__ = (
        UniqueConstraint("npc_low_id", "npc_high_id"),
        CheckConstraint("npc_low_id < npc_high_id", name="ck_social_bond_pair_order"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    npc_low_id: Mapped[int] = mapped_column(ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False)
    npc_high_id: Mapped[int] = mapped_column(ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False)
    stage: Mapped[str] = mapped_column(String(30), default="distant", nullable=False)
    trust: Mapped[float] = mapped_column(Float, default=50.0, nullable=False)
    last_interaction_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    last_decay_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    interaction_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    positive_interactions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    negative_interactions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    decay_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    repair_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reasons_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    updated_minute: Mapped[int] = mapped_column(Integer, nullable=False)


class SocialInvitation(Base):
    __tablename__ = "social_invitations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    inviter_id: Mapped[int] = mapped_column(ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False)
    invitee_id: Mapped[int] = mapped_column(ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False)
    location: Mapped[str] = mapped_column(String(30), nullable=False)
    created_minute: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    scheduled_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)


class SocialCommitment(Base):
    __tablename__ = "social_commitments"
    __table_args__ = (UniqueConstraint("invitation_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invitation_id: Mapped[int] = mapped_column(ForeignKey("social_invitations.id", ondelete="CASCADE"), nullable=False)
    npc_low_id: Mapped[int] = mapped_column(ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False)
    npc_high_id: Mapped[int] = mapped_column(ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False)
    activity_key: Mapped[str] = mapped_column(String(40), nullable=False)
    location: Mapped[str] = mapped_column(String(30), nullable=False)
    scheduled_minute: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    expires_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    completed_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)


class FriendCircle(Base):
    __tablename__ = "friend_circles"
    __table_args__ = (UniqueConstraint("circle_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    circle_key: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    member_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    ended_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reasons_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)


class JointActivity(Base):
    __tablename__ = "joint_activities"
    __table_args__ = (UniqueConstraint("commitment_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    commitment_id: Mapped[int | None] = mapped_column(ForeignKey("social_commitments.id", ondelete="SET NULL"), nullable=True)
    circle_id: Mapped[int | None] = mapped_column(ForeignKey("friend_circles.id", ondelete="SET NULL"), nullable=True)
    activity_key: Mapped[str] = mapped_column(String(40), nullable=False)
    location: Mapped[str] = mapped_column(String(30), nullable=False)
    start_minute: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    end_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    participant_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    shared_cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    outcome_json: Mapped[str] = mapped_column(Text, nullable=False)


class CohousingHousehold(Base):
    __tablename__ = "cohousing_households"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_housing_id: Mapped[int] = mapped_column(ForeignKey("housing.id", ondelete="CASCADE"), index=True, nullable=False)
    resident_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    started_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    weekly_shared_cost: Mapped[float] = mapped_column(Float, nullable=False)
    next_expense_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    trust_at_start: Mapped[float] = mapped_column(Float, nullable=False)
    reasons_json: Mapped[str] = mapped_column(Text, nullable=False)


class SharedExpense(Base):
    __tablename__ = "shared_expenses"
    __table_args__ = (UniqueConstraint("household_id", "world_minute", "kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("cohousing_households.id", ondelete="CASCADE"), index=True, nullable=False)
    world_minute: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    split_json: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)


class SocialAudit(Base):
    __tablename__ = "social_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    npc_low_id: Mapped[int] = mapped_column(ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False)
    npc_high_id: Mapped[int] = mapped_column(ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False)
    world_minute: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    delta_low_to_high: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    delta_high_to_low: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    trust_before: Mapped[float] = mapped_column(Float, nullable=False)
    trust_after: Mapped[float] = mapped_column(Float, nullable=False)
    stage_before: Mapped[str] = mapped_column(String(30), nullable=False)
    stage_after: Mapped[str] = mapped_column(String(30), nullable=False)
    reasons_json: Mapped[str] = mapped_column(Text, nullable=False)


class SocialProfile(Base):
    __tablename__ = "social_profiles"
    __table_args__ = (UniqueConstraint("npc_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    npc_id: Mapped[int] = mapped_column(ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False)
    belonging: Mapped[float] = mapped_column(Float, default=20.0, nullable=False)
    trust_index: Mapped[float] = mapped_column(Float, default=50.0, nullable=False)
    reasons_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    updated_minute: Mapped[int] = mapped_column(Integer, nullable=False)


class StoryState(Base):
    """V0.9 cursor and observations; it never rewrites a V0.1-V0.8 fact."""

    __tablename__ = "story_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    initialized_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    last_processed_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    next_week_summary_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    next_month_summary_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    next_replay_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    observations_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_minute: Mapped[int] = mapped_column(Integer, nullable=False)


class LifeMilestone(Base):
    """Immutable Engine-authored milestone derived only from committed facts."""

    __tablename__ = "life_milestones"
    __table_args__ = (UniqueConstraint("milestone_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    milestone_key: Mapped[str] = mapped_column(String(160), nullable=False)
    npc_id: Mapped[int] = mapped_column(
        ForeignKey("npcs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    milestone_type: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    world_minute: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    facts_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String(60), nullable=False)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rule_json: Mapped[str] = mapped_column(Text, nullable=False)
    fact_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class CausalLink(Base):
    """Ordered, immutable explanation edge for one milestone."""

    __tablename__ = "causal_links"
    __table_args__ = (UniqueConstraint("milestone_id", "sequence"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    milestone_id: Mapped[int] = mapped_column(
        ForeignKey("life_milestones.id", ondelete="CASCADE"), index=True, nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    cause_type: Mapped[str] = mapped_column(String(60), nullable=False)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    fact_json: Mapped[str] = mapped_column(Text, nullable=False)


class StorySummary(Base):
    """Weekly/monthly structured facts fixed by the Engine before prose exists."""

    __tablename__ = "story_summaries"
    __table_args__ = (UniqueConstraint("summary_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    summary_key: Mapped[str] = mapped_column(String(120), nullable=False)
    period_type: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    period_start_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    period_end_minute: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    facts_json: Mapped[str] = mapped_column(Text, nullable=False)
    milestone_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    fact_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_minute: Mapped[int] = mapped_column(Integer, nullable=False)


class ReplayCheckpoint(Base):
    """Daily immutable replay index; creation consumes no random values."""

    __tablename__ = "replay_checkpoints"
    __table_args__ = (UniqueConstraint("checkpoint_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    checkpoint_key: Mapped[str] = mapped_column(String(120), nullable=False)
    period_start_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    period_end_minute: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    random_counter: Mapped[int] = mapped_column(Integer, nullable=False)
    milestone_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    summary_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    facts_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_minute: Mapped[int] = mapped_column(Integer, nullable=False)


class ProductState(Base):
    """V1.0 product metadata and deterministic cycle cursor."""

    __tablename__ = "product_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    schema_version: Mapped[str] = mapped_column(String(20), default="1.0.0", nullable=False)
    world_name: Mapped[str] = mapped_column(String(60), nullable=False)
    preset_key: Mapped[str] = mapped_column(String(30), nullable=False)
    config_json: Mapped[str] = mapped_column(Text, nullable=False)
    initialized_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    last_statistics_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    last_balance_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_minute: Mapped[int] = mapped_column(Integer, nullable=False)


class WorldStatistic(Base):
    """Immutable, Engine-authored statistics snapshot with source provenance."""

    __tablename__ = "world_statistics"
    __table_args__ = (UniqueConstraint("snapshot_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_key: Mapped[str] = mapped_column(String(100), nullable=False)
    world_minute: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    metrics_json: Mapped[str] = mapped_column(Text, nullable=False)
    sources_json: Mapped[str] = mapped_column(Text, nullable=False)
    facts_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class BalanceAudit(Base):
    """Deterministic economic/decision guardrail result; never changes old facts."""

    __tablename__ = "balance_audits"
    __table_args__ = (UniqueConstraint("audit_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    audit_key: Mapped[str] = mapped_column(String(100), nullable=False)
    world_minute: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    metrics_json: Mapped[str] = mapped_column(Text, nullable=False)
    thresholds_json: Mapped[str] = mapped_column(Text, nullable=False)
    violations_json: Mapped[str] = mapped_column(Text, nullable=False)
    facts_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class UpgradeReport(Base):
    """Append-only report for a schema/product upgrade observation."""

    __tablename__ = "upgrade_reports"
    __table_args__ = (UniqueConstraint("report_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_key: Mapped[str] = mapped_column(String(120), nullable=False)
    from_version: Mapped[str] = mapped_column(String(20), nullable=False)
    to_version: Mapped[str] = mapped_column(String(20), nullable=False)
    world_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    added_tables_json: Mapped[str] = mapped_column(Text, nullable=False)
    preserved_tables_json: Mapped[str] = mapped_column(Text, nullable=False)
    checks_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class OnboardingProgress(Base):
    """Small product-only checklist; it has no authority over world facts."""

    __tablename__ = "onboarding_progress"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    completed_steps_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class DataTransferAudit(Base):
    """Audit record for validated exports/imports; payloads never enter this table."""

    __tablename__ = "data_transfer_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operation: Mapped[str] = mapped_column(String(20), nullable=False)
    transfer_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    target_slot: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
