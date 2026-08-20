from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import delete, func, inspect, or_, select
from sqlalchemy.orm import Session, sessionmaker

from database.models import (
    AgentDecisionArtifact,
    AgentDecisionJob,
    AgentTakeoverTurn,
    AgentConversation,
    AgentConversationAudit,
    AgentConversationParticipantResult,
    AgentConversationTask,
    AgentConversationTurn,
    AgentCognitionState,
    AgentPlan,
    AgentReflection,
    AgentReflectionSource,
    AgentReflectionTask,
    AgentSubjectiveBelief,
    BalanceAudit,
    CareerDevelopment,
    CareerTransition,
    CausalLink,
    CohousingHousehold,
    CommunityInstitution,
    DecisionLog,
    EconomicTransaction,
    EmploymentProfile,
    Event,
    FacilityUsage,
    FriendCircle,
    Housing,
    HousingUpgradeRecord,
    InventoryItem,
    ItemDefinition,
    JointActivity,
    LifeMilestone,
    LongTermGoal,
    Memory,
    NPC,
    NarrativeArtifact,
    NarrativeJob,
    NPCSkill,
    PerformanceReview,
    PersonalBudget,
    ProductState,
    Relationship,
    ReplayCheckpoint,
    RestockEvent,
    SharedExpense,
    SocialAudit,
    SocialBond,
    SocialCommitment,
    SocialInvitation,
    SocialProfile,
    StoryState,
    StorySummary,
    Store,
    StoreListing,
    StoreStock,
    TrainingRecord,
    UpgradeReport,
    WeeklyEconomicReport,
    WorkAttendance,
    WorkSchedule,
    WorldState,
    WorldStatistic,
    OnboardingProgress,
)
from simulation.actions import apply_passive_drift, complete_action, start_action
from simulation.agent_brain import (
    AgentSettings,
    AgentDecisionGenerator,
    V11_TABLE_NAMES,
    agent_shadow_enabled_from_env,
    agent_takeover_enabled_from_env,
    agent_takeover_npc_ids_from_env,
    agent_shadow_snapshot,
    agent_status_snapshot,
    enqueue_agent_decision,
    process_agent_jobs,
    process_takeover_jobs,
    reset_interrupted_agent_jobs,
)
from simulation.agent_takeover import (
    SUPPORTED_NPC_IDS,
    V12_TABLE_NAMES,
    active_takeover_turn,
    build_action_options,
    create_waiting_turn,
    deadline_expired,
    latest_takeover_snapshot,
    mark_turn_completed,
    mark_turn_executing,
    mark_turn_worker_failed,
    recover_takeover_leases,
    takeover_audit_snapshots,
    validate_latest_action,
)
from simulation.agent_conversation import (
    V14_TABLE_NAMES,
    ConversationGenerator,
    cancel_conversation,
    conversation_enabled_from_env,
    conversation_safety_check,
    conversation_snapshot,
    enqueue_social_conversation,
    process_conversation_tasks,
    recover_conversation_tasks,
    settle_ready_conversations,
)
from simulation.agent_cognition import (
    V15_TABLE_NAMES,
    ReflectionGenerator,
    cancel_reflection_task,
    cognition_context_snapshot,
    cognition_npc_ids_from_env,
    cognition_safety_check,
    cognition_snapshot,
    enqueue_due_reflections,
    ensure_cognition_states,
    evaluate_plan_progress,
    plan_snapshot,
    process_reflection_tasks,
    recover_reflection_tasks,
    reflection_snapshot,
)
from simulation.clock import ClockSnapshot, TICK_MINUTES
from simulation.decision import LOCATIONS, decide
from simulation.events import add_event
from simulation.economy import (
    PROFESSIONS,
    build_economy_context,
    ensure_economy_data,
    npc_economy_snapshot,
    process_housing_costs,
    store_catalog_snapshot,
)
from simulation.career_budget import (
    budget_snapshot,
    career_budget_context,
    career_snapshot,
    ensure_career_budget_data,
    process_career_budget_cycles,
    report_snapshots,
)
from simulation.community import (
    community_context,
    ensure_community_data,
    institution_snapshots,
    npc_rhythm_snapshot,
    process_restocking,
    stock_snapshots,
)
from simulation.social_life import (
    bond_snapshots,
    circle_snapshots,
    commitment_snapshots,
    ensure_social_life_data,
    household_snapshots,
    npc_social_snapshot,
    process_social_life_cycles,
    record_social_interaction,
    social_life_context,
)
from simulation.life_story import (
    causal_chain_snapshot,
    ensure_life_story_data,
    milestone_snapshots,
    process_life_story_cycles,
    replay_story,
    summary_snapshots,
    V09_TABLE_NAMES,
)
from simulation.goals import build_goal_context, ensure_default_goals, goal_snapshots
from simulation.narrative import (
    NarrativeGenerator,
    artifact_to_dict,
    enqueue_event_jobs,
    enqueue_memory_summary_jobs,
    ensure_goal_narrative_jobs,
    process_jobs,
    reset_interrupted_jobs,
)
from simulation.npc import npc_to_dict
from simulation.random_service import RandomService
from simulation.productization import (
    CreateSaveRequest,
    ImportSaveRequest,
    NewWorldConfig,
    OnboardingRequest,
    SaveManager,
    V10_TABLE_NAMES,
    apply_preset_to_profiles,
    ensure_product_data,
    latest_balance,
    latest_statistics,
    onboarding_snapshot,
    process_product_cycles,
    product_status,
    update_onboarding,
    upgrade_reports,
)
from simulation.runtime_v16 import ModelRuntime, RuntimeProvider


logger = logging.getLogger(__name__)
VALID_SPEEDS = {1, 5, 20}


def _economy_enabled_from_env() -> bool:
    value = os.getenv("MINIWORLD_ECONOMY_ENABLED", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _career_budget_enabled_from_env() -> bool:
    value = os.getenv("MINIWORLD_CAREER_BUDGET_ENABLED", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _community_enabled_from_env() -> bool:
    value = os.getenv("MINIWORLD_COMMUNITY_RHYTHM_ENABLED", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _social_life_enabled_from_env() -> bool:
    value = os.getenv("MINIWORLD_SOCIAL_LIFE_ENABLED", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _life_story_enabled_from_env() -> bool:
    value = os.getenv("MINIWORLD_LIFE_STORY_ENABLED", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _product_enabled_from_env() -> bool:
    value = os.getenv("MINIWORLD_PRODUCT_ENABLED", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}

NPC_PROFILES = (
    dict(id=1, name="Alice", age=29, job="Designer", current_location="Home", money=120, energy=78, hunger=24, mood=68, social_need=48, work_satisfaction=72, extroversion=0.82, kindness=0.76, ambition=0.62, risk_tolerance=0.42, discipline=0.68),
    dict(id=2, name="Bob", age=31, job="Developer", current_location="Home", money=145, energy=84, hunger=31, mood=61, social_need=28, work_satisfaction=66, extroversion=0.32, kindness=0.61, ambition=0.78, risk_tolerance=0.28, discipline=0.91),
    dict(id=3, name="Charlie", age=38, job="Manager", current_location="Office", money=185, energy=73, hunger=22, mood=72, social_need=41, work_satisfaction=78, extroversion=0.67, kindness=0.45, ambition=0.93, risk_tolerance=0.58, discipline=0.84),
    dict(id=4, name="Diana", age=27, job="Writer", current_location="Cafe", money=96, energy=69, hunger=38, mood=75, social_need=55, work_satisfaction=59, extroversion=0.74, kindness=0.88, ambition=0.48, risk_tolerance=0.65, discipline=0.52),
    dict(id=5, name="Eric", age=34, job="Accountant", current_location="Park", money=160, energy=81, hunger=27, mood=57, social_need=34, work_satisfaction=70, extroversion=0.22, kindness=0.69, ambition=0.71, risk_tolerance=0.18, discipline=0.95),
)


class WorldService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        narrative_generator: NarrativeGenerator | None = None,
        economy_enabled: bool | None = None,
        career_budget_enabled: bool | None = None,
        community_enabled: bool | None = None,
        social_life_enabled: bool | None = None,
        life_story_enabled: bool | None = None,
        product_enabled: bool | None = None,
        agent_enabled: bool | None = None,
        agent_takeover_enabled: bool | None = None,
        agent_takeover_npc_ids: Iterable[int] | None = None,
        agent_worker_concurrency: int | None = None,
        agent_generator: AgentDecisionGenerator | None = None,
        agent_conversations_enabled: bool | None = None,
        conversation_generator: ConversationGenerator | None = None,
        agent_cognition_enabled: bool | None = None,
        agent_cognition_npc_ids: Iterable[int] | None = None,
        reflection_generator: ReflectionGenerator | None = None,
        model_runtime: ModelRuntime | None = None,
        world_config: NewWorldConfig | None = None,
        save_manager: SaveManager | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.lock = asyncio.Lock()
        self.narrative_lock = asyncio.Lock()
        self.agent_lock = asyncio.Lock()
        self.conversation_lock = asyncio.Lock()
        self.cognition_lock = asyncio.Lock()
        self._dashboard_snapshot_sequence = 0
        self.narrative_generator = narrative_generator or NarrativeGenerator()
        self.economy_enabled = _economy_enabled_from_env() if economy_enabled is None else economy_enabled
        requested_v06 = _career_budget_enabled_from_env() if career_budget_enabled is None else career_budget_enabled
        self.career_budget_enabled = bool(self.economy_enabled and requested_v06)
        requested_v07 = _community_enabled_from_env() if community_enabled is None else community_enabled
        self.community_enabled = bool(self.career_budget_enabled and requested_v07)
        requested_v08 = _social_life_enabled_from_env() if social_life_enabled is None else social_life_enabled
        self.social_life_enabled = bool(self.community_enabled and requested_v08)
        requested_v09 = _life_story_enabled_from_env() if life_story_enabled is None else life_story_enabled
        self.life_story_enabled = bool(self.social_life_enabled and requested_v09)
        requested_v10 = _product_enabled_from_env() if product_enabled is None else product_enabled
        self.product_enabled = bool(self.life_story_enabled and requested_v10)
        requested_v11 = (
            agent_shadow_enabled_from_env() if agent_enabled is None else agent_enabled
        )
        self.agent_shadow_requested = bool(self.product_enabled and requested_v11)
        requested_v12 = (
            agent_takeover_enabled_from_env()
            if agent_takeover_enabled is None else agent_takeover_enabled
        )
        if agent_takeover_npc_ids is None:
            requested_ids = (
                agent_takeover_npc_ids_from_env()
                if agent_takeover_enabled is None
                else ({1} if requested_v12 else set())
            )
        else:
            requested_ids = set(agent_takeover_npc_ids)
        self.agent_takeover_npc_ids = (
            set(requested_ids) & set(SUPPORTED_NPC_IDS) if self.product_enabled else set()
        )
        self.agent_takeover_enabled = bool(self.agent_takeover_npc_ids)
        # Kept as the exact V1.2 Alice compatibility flag.
        self.agent_takeover_requested = 1 in self.agent_takeover_npc_ids
        self.agent_enabled = bool(
            self.agent_shadow_requested or self.agent_takeover_enabled
        )
        try:
            configured_concurrency = int(
                os.getenv("MINIWORLD_AGENT_MAX_CONCURRENCY", "3")
            ) if agent_worker_concurrency is None else int(agent_worker_concurrency)
        except ValueError:
            configured_concurrency = 3
        self.agent_worker_concurrency = min(5, max(1, configured_concurrency))
        self.model_runtime = model_runtime or ModelRuntime(self.session_factory)
        runtime_provider = RuntimeProvider(self.model_runtime) if self.model_runtime.configured else None
        runtime_agent_settings = AgentSettings(
            api_key=self.model_runtime.settings.api_key,
            base_url=self.model_runtime.settings.base_url,
            model=self.model_runtime.settings.model,
            timeout_seconds=self.model_runtime.settings.timeout_seconds,
            max_attempts=self.model_runtime.settings.max_attempts,
        )
        self.agent_generator = agent_generator or AgentDecisionGenerator(
            settings=runtime_agent_settings, provider=runtime_provider
        )
        requested_v14 = (
            conversation_enabled_from_env()
            if agent_conversations_enabled is None else agent_conversations_enabled
        )
        self.agent_conversations_enabled = bool(self.product_enabled and requested_v14)
        self.conversation_generator = conversation_generator or ConversationGenerator(
            agent_settings=self.agent_generator.settings, provider=runtime_provider
        )
        if agent_cognition_npc_ids is None:
            cognition_ids = cognition_npc_ids_from_env()
            if agent_cognition_enabled is True:
                cognition_ids = set(SUPPORTED_NPC_IDS)
            elif agent_cognition_enabled is False:
                cognition_ids = set()
        else:
            cognition_ids = set(agent_cognition_npc_ids)
        self.agent_cognition_npc_ids = (
            set(cognition_ids) & set(SUPPORTED_NPC_IDS) if self.product_enabled else set()
        )
        self.agent_cognition_enabled = bool(self.agent_cognition_npc_ids)
        self.cognition_schema_available = False
        self.reflection_generator = reflection_generator or ReflectionGenerator(
            agent_settings=self.agent_generator.settings, provider=runtime_provider
        )
        recovered_mode, _recovered_generation, recovered_ids = self.model_runtime._state_snapshot()
        if recovered_mode in {"online", "paused"} and recovered_ids:
            self.agent_takeover_npc_ids = set(recovered_ids)
            self.agent_takeover_enabled = True
            self.agent_takeover_requested = 1 in recovered_ids
            self.agent_enabled = True
            self.agent_conversations_enabled = True
            self.agent_cognition_npc_ids = set(recovered_ids)
            self.agent_cognition_enabled = True
        self.world_config = world_config
        self.save_manager = save_manager

    def initialize(self) -> None:
        with self.session_factory() as session:
            state = session.get(WorldState, 1)
            if state is None:
                self._create_default_world(session)
                state = session.get(WorldState, 1)
            npcs = list(session.scalars(select(NPC).order_by(NPC.id)))
            economy_created = (
                ensure_economy_data(session, npcs, state.total_minutes if state else 480)
                if self.economy_enabled else {}
            )
            career_created = (
                ensure_career_budget_data(session, npcs, state.total_minutes if state else 480)
                if self.career_budget_enabled else {}
            )
            community_created = (
                ensure_community_data(session, npcs, state.total_minutes if state else 480)
                if self.community_enabled else {}
            )
            social_created: dict[str, int] = {}
            if self.social_life_enabled:
                try:
                    with session.begin_nested():
                        social_created = ensure_social_life_data(
                            session, npcs, state.total_minutes if state else 480
                        )
                except Exception:
                    logger.exception("V0.8 initialization failed; retaining exact V0.7-compatible facts")
            story_created: dict[str, int] = {}
            if self.life_story_enabled:
                available_tables = set(inspect(session.get_bind()).get_table_names())
                if not V09_TABLE_NAMES.issubset(available_tables):
                    self.life_story_enabled = False
                    logger.error("V0.9 schema incomplete; retaining exact V0.8-compatible behavior")
            if self.life_story_enabled:
                try:
                    with session.begin_nested():
                        story_created = ensure_life_story_data(
                            session, npcs, state.total_minutes if state else 480
                        )
                except Exception:
                    self.life_story_enabled = False
                    logger.exception("V0.9 initialization failed; retaining exact V0.8-compatible facts")
            product_created: dict[str, int] = {}
            if self.product_enabled:
                available_tables = set(inspect(session.get_bind()).get_table_names())
                if not V10_TABLE_NAMES.issubset(available_tables):
                    self.product_enabled = False
                    logger.error("V1.0 schema incomplete; retaining exact V0.9-compatible behavior")
            if self.product_enabled and state is not None:
                try:
                    with session.begin_nested():
                        context = getattr(session.get_bind(), "_miniworld_upgrade_context", {})
                        product_created = ensure_product_data(
                            session, state, self.world_config, context
                        )
                except Exception:
                    self.product_enabled = False
                    logger.exception("V1.0 initialization failed; retaining exact V0.9-compatible facts")
            if self.agent_enabled:
                available_tables = set(inspect(session.get_bind()).get_table_names())
                if not V11_TABLE_NAMES.issubset(available_tables):
                    self.agent_enabled = False
                    logger.error("V1.1 schema incomplete; retaining exact V1.0 behavior")
            if self.agent_takeover_enabled:
                available_tables = set(inspect(session.get_bind()).get_table_names())
                if not V12_TABLE_NAMES.issubset(available_tables):
                    self.agent_takeover_enabled = False
                    self.agent_takeover_npc_ids.clear()
                    self.agent_takeover_requested = False
                    logger.error("V1.2 schema incomplete; retaining exact V1.1 behavior")
            if V12_TABLE_NAMES.issubset(set(inspect(session.get_bind()).get_table_names())):
                unfinished_takeover = any(
                    active_takeover_turn(session, npc_id) is not None
                    for npc_id in SUPPORTED_NPC_IDS
                )
                if unfinished_takeover:
                    self.agent_takeover_enabled = True
            available_tables = set(inspect(session.get_bind()).get_table_names())
            self.cognition_schema_available = V15_TABLE_NAMES.issubset(available_tables)
            if self.agent_conversations_enabled and not V14_TABLE_NAMES.issubset(available_tables):
                self.agent_conversations_enabled = False
                logger.error("V1.4 schema incomplete; retaining exact V1.3 behavior")
            conversation_recovered = (
                recover_conversation_tasks(session)
                if V14_TABLE_NAMES.issubset(available_tables) else 0
            )
            if self.agent_cognition_enabled and not self.cognition_schema_available:
                self.agent_cognition_enabled = False
                self.agent_cognition_npc_ids.clear()
                logger.error("V1.5 schema incomplete; retaining exact V1.4 behavior")
            cognition_created = (
                ensure_cognition_states(
                    session, self.agent_cognition_npc_ids, state.total_minutes if state else 480
                )
                if self.agent_cognition_enabled else 0
            )
            cognition_recovered = (
                recover_reflection_tasks(session)
                if self.cognition_schema_available else 0
            )
            created = ensure_default_goals(session, npcs, state.total_minutes if state else 480)
            recovered = reset_interrupted_jobs(session)
            narrative_created = ensure_goal_narrative_jobs(
                session, npcs, state.total_minutes if state else 480
            )
            narrative_created += enqueue_memory_summary_jobs(
                session, state.total_minutes if state else 480
            )
            session.commit()
            if created:
                logger.info("Initialized %s MiniWorld long-term goals", created)
            if sum(economy_created.values()):
                logger.info("Initialized MiniWorld economy records: %s", economy_created)
            if sum(career_created.values()):
                logger.info("Initialized MiniWorld V0.6 career/budget records: %s", career_created)
            if sum(community_created.values()):
                logger.info("Initialized MiniWorld V0.7 community/rhythm records: %s", community_created)
            if sum(social_created.values()):
                logger.info("Initialized MiniWorld V0.8 social-life records: %s", social_created)
            if sum(story_created.values()):
                logger.info("Initialized MiniWorld V0.9 life-story records: %s", story_created)
            if sum(product_created.values()):
                logger.info("Initialized MiniWorld V1.0 product records: %s", product_created)
            if recovered:
                logger.warning("Recovered %s interrupted narrative jobs", recovered)
            if narrative_created:
                logger.info("Queued %s MiniWorld narrative jobs", narrative_created)
            if conversation_recovered:
                logger.warning("Recovered %s interrupted or expired V1.4 conversation records", conversation_recovered)
            if cognition_created:
                logger.info("Initialized %s isolated V1.5 cognition states", cognition_created)
            if cognition_recovered:
                logger.warning("Recovered %s interrupted V1.5 reflection tasks", cognition_recovered)

    def _create_default_world(self, session: Session) -> None:
        config = (self.world_config or NewWorldConfig()) if self.product_enabled else NewWorldConfig()
        profiles = apply_preset_to_profiles(NPC_PROFILES, config)
        state = WorldState(
            id=1, total_minutes=480, paused=False, speed=config.speed,
            seed=config.seed, random_counter=0,
        )
        session.add(state)
        for profile in profiles:
            session.add(NPC(**profile, current_action="Idle", action_end_minute=480))
        session.flush()
        for source in profiles:
            for target in profiles:
                if source["id"] == target["id"]:
                    continue
                score = 3 + ((source["id"] * 7 + target["id"] * 3) % 16)
                session.add(Relationship(from_npc_id=source["id"], to_npc_id=target["id"], score=score))
        session.flush()
        ensure_default_goals(
            session,
            list(session.scalars(select(NPC).order_by(NPC.id))),
            state.total_minutes,
        )
        add_event(
            session, ClockSnapshot(480), "SYSTEM", "MiniWorld 世界初始化完成",
            metadata={"seed": config.seed},
        )

    async def tick(self) -> bool:
        async with self.lock:
            with self.session_factory() as session:
                advanced = self._tick_session(session)
                if not advanced:
                    return False
                session.commit()
                return True

    def _decide_for_npc(
        self,
        session: Session,
        npc: NPC,
        npcs: list[NPC],
        clock: ClockSnapshot,
        random_service: RandomService,
    ) -> tuple[Any, dict[str, list[NPC]], dict[str, Any] | None]:
        """Build the Engine-owned current candidates; callers choose which RNG stream to use."""

        occupants = {
            location: [person for person in npcs if person.current_location == location]
            for location in LOCATIONS
        }
        career_context = (
            career_budget_context(session, npc, clock.total_minutes)
            if self.career_budget_enabled else None
        )
        rhythm_context = community_context(session, npc, clock) if self.community_enabled else None
        group_context = None
        if self.social_life_enabled:
            try:
                group_context = social_life_context(session, npc, clock.total_minutes)
            except Exception:
                logger.exception(
                    "V0.8 context unavailable for NPC %s; using V0.7 decision context", npc.id
                )
        decision = decide(
            npc, clock, occupants, random_service, build_goal_context(session, npc),
            build_economy_context(session, npc) if self.economy_enabled else None,
            career_context, rhythm_context, group_context,
        )
        return decision, occupants, group_context

    def _resolve_waiting_takeover(
        self,
        session: Session,
        turn: AgentTakeoverTurn,
        npc: NPC,
        npcs: list[NPC],
        clock: ClockSnapshot,
        state: WorldState,
    ) -> bool:
        """Start one validated action or one explicit Utility fallback, never network work."""

        now = datetime.now(timezone.utc)
        failure = turn.last_error_code if turn.worker_state == "failed" else None
        if npc.id not in self.agent_takeover_npc_ids:
            failure = "takeover_disabled"
        if turn.state == "waiting" and failure is None and not deadline_expired(
            turn, now, clock.total_minutes
        ):
            return False
        validation_random = RandomService(state.seed, state.random_counter)
        decision, _occupants, group_context = self._decide_for_npc(
            session, npc, npcs, clock, validation_random
        )
        if failure is None and turn.state == "ready" and turn.agent_decision_json:
            advice = json.loads(turn.agent_decision_json)
            validation = validate_latest_action(
                session,
                npc,
                clock,
                decision,
                action=advice.get("action", ""),
                target=advice.get("target"),
                snapshot_options=json.loads(turn.options_json),
                dialogue=advice.get("dialogue"),
                social_context=group_context,
                valid_until_minute=turn.valid_until_minute,
            )
            if validation["legal"]:
                action = advice["action"]
                params = validation["params"] or {}
                start_action(npc, action, clock)
                mark_turn_executing(
                    turn,
                    source="agent",
                    action=action,
                    target=advice.get("target"),
                    params=params,
                    started_minute=clock.total_minutes,
                    end_minute=npc.action_end_minute,
                    execution_validation=validation,
                )
                return True
            failure = validation["reason_code"]
            if failure.startswith("snapshot_"):
                failure = failure.removeprefix("snapshot_")
        elif failure is None:
            failure = "decision_expired"

        available = {
            candidate.action for candidate in decision.candidates if candidate.available
        }
        fallback_action = (
            turn.utility_action if turn.utility_action in available else decision.chosen_action
        )
        start_action(npc, fallback_action, clock)
        mark_turn_executing(
            turn,
            source="utility_fallback",
            action=fallback_action,
            target=None,
            params={},
            started_minute=clock.total_minutes,
            end_minute=npc.action_end_minute,
            execution_validation={
                "legal": True,
                "reason_code": "fresh_utility_fallback",
                "latest_available": sorted(available),
            },
            fallback_reason_code=failure or "provider_error",
        )
        if turn.job_id is not None:
            job = session.get(AgentDecisionJob, turn.job_id)
            if job is not None and job.status in {"pending", "processing"}:
                job.status = "failed"
                job.last_error_code = failure or "takeover_fallback"
                job.completed_at = now
        return True

    def _begin_takeover_turn(
        self,
        session: Session,
        npc: NPC,
        clock: ClockSnapshot,
        state: WorldState,
        decision: Any,
        occupants: dict[str, list[NPC]],
        group_context: dict[str, Any] | None,
    ) -> None:
        decision_record = DecisionLog(
            npc_id=npc.id,
            world_day=clock.day,
            world_time=clock.time_text,
            chosen_action=decision.chosen_action,
            candidates_json=json.dumps(
                [candidate.to_dict() for candidate in decision.candidates], ensure_ascii=False
            ),
            reason_json=json.dumps(decision.reason, ensure_ascii=False),
        )
        session.add(decision_record)
        session.flush([decision_record])
        options = build_action_options(
            session, npc, clock, decision, social_context=group_context
        )
        provider_status = self.agent_generator.status()
        valid_minutes = max(
            30,
            int((self.agent_generator.settings.timeout_seconds + 2.0) * max(1, state.speed) / 10 + 1) * 10,
        )
        deadline = datetime.now(timezone.utc) + timedelta(
            seconds=self.agent_generator.settings.timeout_seconds + 2.0
        )
        job_id = None
        if provider_status["available"]:
            with session.no_autoflush:
                enqueue_agent_decision(
                    session, decision_record, npc, clock, occupants, decision
                )
            session.flush()
            job = session.scalar(
                select(AgentDecisionJob).where(
                    AgentDecisionJob.decision_id == decision_record.id
                )
            )
            if job is not None:
                perception = json.loads(job.perception_json)
                previous_turn = session.scalar(
                    select(AgentTakeoverTurn).where(
                        AgentTakeoverTurn.npc_id == npc.id,
                        AgentTakeoverTurn.state == "completed",
                        AgentTakeoverTurn.agent_decision_json.is_not(None),
                    ).order_by(AgentTakeoverTurn.id.desc()).limit(1)
                )
                if previous_turn is not None:
                    previous_advice = json.loads(previous_turn.agent_decision_json or "{}")
                    perception.setdefault("plans", []).append({
                        "kind": "agent_plan",
                        "intention": previous_advice.get("intention"),
                        "items": previous_advice.get("plan", []),
                    })
                grouped: dict[str, dict[str, Any]] = {}
                for option in options:
                    item = grouped.setdefault(
                        option["action"],
                        {
                            "action": option["action"],
                            "target_location": option["params"].get("target_location"),
                            "allowed_targets": [],
                            "description": option["description"],
                        },
                    )
                    if option["target"] is not None:
                        item["allowed_targets"].append(option["target"])
                perception["schema_version"] = "1.3"
                perception["available_actions"] = list(grouped.values())
                job.perception_json = json.dumps(
                    perception, ensure_ascii=False, separators=(",", ":")
                )
                job_id = job.id
        turn = create_waiting_turn(
            session,
            decision_id=decision_record.id,
            npc_id=npc.id,
            created_minute=clock.total_minutes,
            valid_until_minute=clock.total_minutes + valid_minutes,
            response_deadline_at=deadline,
            options=options,
            utility_action=decision.chosen_action,
            utility_target=None,
            utility_reason=decision.reason,
            job_id=job_id,
            fallback_reason_code=None if job_id is not None else provider_status["reason"],
        )
        if job_id is None:
            start_action(npc, decision.chosen_action, clock)
            mark_turn_executing(
                turn,
                source="utility_fallback",
                action=decision.chosen_action,
                target=None,
                params={},
                started_minute=clock.total_minutes,
                end_minute=npc.action_end_minute,
                execution_validation={"legal": True, "reason_code": "utility_current"},
                fallback_reason_code=provider_status["reason"],
            )
        else:
            npc.current_action = "Idle"
            npc.pending_location = None
            npc.action_end_minute = turn.valid_until_minute

    def _tick_session(self, session: Session) -> bool:
        """One complete Engine tick. Batch simulation calls this same fact path."""
        state = session.get(WorldState, 1)
        if state is None or state.paused:
            return False
        state.total_minutes += TICK_MINUTES
        state.updated_at = datetime.now(timezone.utc)
        clock = ClockSnapshot(state.total_minutes)
        if state.total_minutes % 1440 == 0:
            add_event(session, clock, "TIME", f"第 {clock.day} 天开始了：{clock.weekday}")
        random_service = RandomService(state.seed, state.random_counter)
        npcs = list(session.scalars(select(NPC).order_by(NPC.id)))
        completed_any_action = any(npc.action_end_minute <= state.total_minutes for npc in npcs)
        last_event_id = (
            session.scalar(select(func.max(Event.id))) or 0
            if completed_any_action else 0
        )
        conversation_event_ids: set[int] = set()
        for npc in npcs:
            apply_passive_drift(npc)
        if self.economy_enabled:
            process_housing_costs(session, npcs, clock)
        if self.career_budget_enabled:
            process_career_budget_cycles(session, npcs, clock, random_service)
        if self.community_enabled:
            process_restocking(session, clock)
        if self.social_life_enabled and state.total_minutes % 60 == 0:
            try:
                with session.begin_nested():
                    process_social_life_cycles(session, npcs, clock)
            except Exception:
                logger.exception("V0.8 periodic cycle failed; continuing with V0.7 behavior")
        for npc in npcs:
            takeover_turn = (
                active_takeover_turn(session, npc.id)
                if self.agent_takeover_enabled and npc.id in SUPPORTED_NPC_IDS else None
            )
            if takeover_turn is not None and takeover_turn.state in {"waiting", "ready"}:
                if (
                    takeover_turn.state == "ready"
                    or takeover_turn.worker_state == "failed"
                    or deadline_expired(takeover_turn, datetime.now(timezone.utc), state.total_minutes)
                ):
                    self._resolve_waiting_takeover(
                        session, takeover_turn, npc, npcs, clock, state
                    )
                continue
            if npc.action_end_minute > state.total_minutes:
                continue
            completed_action = npc.current_action
            event_id_before_action = session.scalar(select(func.max(Event.id))) or 0
            action_params = None
            if takeover_turn is not None and takeover_turn.state in {
                "agent_executing", "fallback_executing"
            }:
                action_params = json.loads(takeover_turn.final_params_json or "{}")
            complete_action(
                session, npc, clock, random_service,
                self.economy_enabled, self.career_budget_enabled, self.community_enabled,
                action_params,
            )
            if takeover_turn is not None and takeover_turn.state in {
                "agent_executing", "fallback_executing"
            }:
                mark_turn_completed(takeover_turn, clock.total_minutes)
            if self.social_life_enabled and completed_action == "Socialize":
                session.flush()
                try:
                    with session.begin_nested():
                        social_event = session.scalar(
                            select(Event).where(
                                Event.id > event_id_before_action,
                                Event.event_type == "SOCIAL",
                                Event.npc_id == npc.id,
                                Event.target_npc_id.is_not(None),
                            ).order_by(Event.id).limit(1)
                        )
                        if social_event is not None:
                            record_social_interaction(session, social_event, state.total_minutes)
                            if self.agent_conversations_enabled:
                                conversation = enqueue_social_conversation(
                                    session,
                                    social_event,
                                    enabled_npc_ids=self.agent_takeover_npc_ids,
                                    settings=self.conversation_generator.settings,
                                )
                                if conversation is not None and conversation.status in {
                                    "active", "ready_for_settlement", "completed"
                                }:
                                    conversation_event_ids.add(social_event.id)
                except Exception:
                    logger.exception("V0.8 social interaction failed; preserving committed V0.7 action facts")
            decision, occupants, group_context = self._decide_for_npc(
                session, npc, npcs, clock, random_service
            )
            if npc.id in self.agent_takeover_npc_ids:
                self._begin_takeover_turn(
                    session, npc, clock, state, decision, occupants, group_context
                )
            elif self.agent_enabled and self.agent_generator.provider is not None and npc.id == 1:
                decision_record = DecisionLog(
                    npc_id=npc.id,
                    world_day=clock.day,
                    world_time=clock.time_text,
                    chosen_action=decision.chosen_action,
                    candidates_json=json.dumps(
                        [candidate.to_dict() for candidate in decision.candidates], ensure_ascii=False
                    ),
                    reason_json=json.dumps(decision.reason, ensure_ascii=False),
                )
                session.add(decision_record)
                session.flush([decision_record])
                with session.no_autoflush:
                    enqueue_agent_decision(
                        session, decision_record, npc, clock, occupants, decision
                    )
                start_action(npc, decision.chosen_action, clock)
            else:
                start_action(npc, decision.chosen_action, clock)
                session.add(
                    DecisionLog(
                        npc_id=npc.id,
                        world_day=clock.day,
                        world_time=clock.time_text,
                        chosen_action=decision.chosen_action,
                        candidates_json=json.dumps(
                            [candidate.to_dict() for candidate in decision.candidates], ensure_ascii=False
                        ),
                        reason_json=json.dumps(decision.reason, ensure_ascii=False),
                    )
                )
        state.random_counter = random_service.counter
        if self.life_story_enabled and state.total_minutes % 60 == 0:
            try:
                with session.begin_nested():
                    process_life_story_cycles(
                        session, npcs, clock, seed=state.seed,
                        random_counter=state.random_counter,
                    )
            except Exception:
                logger.exception("V0.9 periodic cycle failed; continuing with exact V0.8 behavior")
        if self.product_enabled and state.total_minutes % 60 == 0:
            try:
                with session.begin_nested():
                    process_product_cycles(session, state)
            except Exception:
                logger.exception("V1.0 periodic cycle failed; continuing with exact V0.9 behavior")
        if self.cognition_schema_available:
            try:
                with session.begin_nested():
                    evaluate_plan_progress(session, clock)
                    if self.agent_cognition_enabled:
                        enqueue_due_reflections(
                            session, clock, self.agent_cognition_npc_ids,
                            self.reflection_generator.settings,
                        )
            except Exception:
                logger.exception("V1.5 cognition boundary failed; world facts continue safely")
        ready_conversation = session.scalar(
            select(AgentConversation.id).where(
                AgentConversation.status == "ready_for_settlement"
            ).limit(1)
        )
        if self.agent_conversations_enabled or ready_conversation is not None:
            try:
                with session.begin_nested():
                    settle_ready_conversations(session, state.total_minutes)
            except Exception:
                logger.exception("V1.4 Engine settlement failed; world facts continue safely")
        if completed_any_action:
            session.flush()
            enqueue_event_jobs(
                session,
                last_event_id,
                state.total_minutes,
                suppress_dialogue_event_ids=conversation_event_ids,
            )
            enqueue_memory_summary_jobs(session, state.total_minutes)
        return True

    async def run_ticks(self, ticks: int, *, commit_interval: int = 144) -> dict[str, Any]:
        """Run real ten-minute ticks with fewer durable commits for offline validation."""
        if ticks < 1 or ticks > 100_000:
            raise ValueError("ticks 必须在 1–100000 之间")
        if commit_interval < 1 or commit_interval > 1440:
            raise ValueError("commit_interval 必须在 1–1440 之间")
        started = datetime.now(timezone.utc)
        completed = 0
        async with self.lock:
            with self.session_factory() as session:
                for index in range(ticks):
                    if not self._tick_session(session):
                        break
                    completed += 1
                    if (index + 1) % commit_interval == 0:
                        session.commit()
                session.commit()
                state = session.get(WorldState, 1)
                total_minutes = state.total_minutes if state else None
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        return {
            "ticks": completed,
            "simulated_minutes": completed * TICK_MINUTES,
            "total_minutes": total_minutes,
            "elapsed_seconds": round(elapsed, 6),
            "ticks_per_second": round(completed / elapsed, 3) if elapsed else 0.0,
            "commit_interval": commit_interval,
            "fact_path": "full-engine-tick",
        }

    async def get_delay(self) -> float:
        with self.session_factory() as session:
            state = session.get(WorldState, 1)
            speed = state.speed if state else 1
            paused = state.paused if state else True
        return 0.2 if paused else 1.0 / speed

    async def set_paused(self, paused: bool) -> dict[str, Any]:
        async with self.lock:
            with self.session_factory() as session:
                state = session.get(WorldState, 1)
                if state is None:
                    raise RuntimeError("世界尚未初始化")
                state.paused = paused
                clock = ClockSnapshot(state.total_minutes)
                add_event(session, clock, "SYSTEM", "世界已暂停" if paused else "世界已继续运行")
                session.commit()
        return await self.world_snapshot()

    async def set_speed(self, speed: int) -> dict[str, Any]:
        if speed not in VALID_SPEEDS:
            raise ValueError(f"速度只能是 {sorted(VALID_SPEEDS)} 之一")
        async with self.lock:
            with self.session_factory() as session:
                state = session.get(WorldState, 1)
                if state is None:
                    raise RuntimeError("世界尚未初始化")
                state.speed = speed
                add_event(session, ClockSnapshot(state.total_minutes), "SYSTEM", f"世界速度已设为 {speed}×")
                session.commit()
        return await self.world_snapshot()

    async def reset(self) -> dict[str, Any]:
        if self.model_runtime._state_snapshot()[0] in {"online", "paused"}:
            await self.stop_runtime(emergency=True, reason="operator_emergency_stop")
        async with self.lock:
            with self.session_factory() as session:
                for model in (
                    AgentPlan,
                    AgentSubjectiveBelief,
                    AgentReflection,
                    AgentReflectionSource,
                    AgentReflectionTask,
                    AgentCognitionState,
                    AgentConversationAudit,
                    AgentConversationParticipantResult,
                    AgentConversationTurn,
                    AgentConversationTask,
                    AgentConversation,
                    AgentTakeoverTurn,
                    AgentDecisionArtifact,
                    AgentDecisionJob,
                    BalanceAudit,
                    WorldStatistic,
                    OnboardingProgress,
                    NarrativeArtifact,
                    NarrativeJob,
                    CausalLink,
                    ReplayCheckpoint,
                    StorySummary,
                    LifeMilestone,
                    StoryState,
                    SharedExpense,
                    JointActivity,
                    SocialCommitment,
                    SocialInvitation,
                    SocialAudit,
                    CohousingHousehold,
                    FriendCircle,
                    SocialProfile,
                    SocialBond,
                    HousingUpgradeRecord,
                    TrainingRecord,
                    FacilityUsage,
                    RestockEvent,
                    StoreStock,
                    WorkAttendance,
                    WorkSchedule,
                    CommunityInstitution,
                    WeeklyEconomicReport,
                    PerformanceReview,
                    CareerTransition,
                    PersonalBudget,
                    CareerDevelopment,
                    EconomicTransaction,
                    InventoryItem,
                    StoreListing,
                    NPCSkill,
                    EmploymentProfile,
                    Housing,
                    ItemDefinition,
                    Store,
                    Memory,
                    DecisionLog,
                    Event,
                    LongTermGoal,
                    Relationship,
                    NPC,
                    WorldState,
                ):
                    session.execute(delete(model))
                self._create_default_world(session)
                session.flush()
                state = session.get(WorldState, 1)
                npcs = list(session.scalars(select(NPC).order_by(NPC.id)))
                if self.economy_enabled:
                    ensure_economy_data(session, npcs, state.total_minutes)
                if self.career_budget_enabled:
                    ensure_career_budget_data(session, npcs, state.total_minutes)
                if self.community_enabled:
                    ensure_community_data(session, npcs, state.total_minutes)
                if self.social_life_enabled:
                    ensure_social_life_data(session, npcs, state.total_minutes)
                if self.life_story_enabled:
                    ensure_life_story_data(session, npcs, state.total_minutes)
                if self.product_enabled:
                    product = session.get(ProductState, 1)
                    if product is not None:
                        product.initialized_minute = state.total_minutes
                        product.last_statistics_minute = state.total_minutes
                        product.last_balance_minute = state.total_minutes
                        product.updated_minute = state.total_minutes
                    ensure_product_data(
                        session, state, self.world_config,
                        getattr(session.get_bind(), "_miniworld_upgrade_context", {}),
                    )
                if self.agent_cognition_enabled:
                    ensure_cognition_states(session, self.agent_cognition_npc_ids, state.total_minutes)
                ensure_goal_narrative_jobs(session, npcs, state.total_minutes)
                session.commit()
        logger.warning("MiniWorld reset to defaults")
        return await self.world_snapshot()

    async def world_snapshot(self) -> dict[str, Any]:
        from simulation.dashboard import world_data

        async with self.lock:
            with self.session_factory() as session:
                return world_data(session)

    async def list_npcs(self) -> list[dict[str, Any]]:
        async with self.lock:
            with self.session_factory() as session:
                return [npc_to_dict(npc) for npc in session.scalars(select(NPC).order_by(NPC.id))]

    async def get_npc(self, npc_id: int) -> dict[str, Any] | None:
        from simulation.dashboard import npc_core_data

        async with self.lock:
            with self.session_factory() as session:
                npc = session.get(NPC, npc_id)
                if npc is None:
                    return None
                return npc_core_data(session, npc)

    async def latest_decision(self, npc_id: int) -> dict[str, Any] | None:
        from simulation.dashboard import decision_data

        async with self.lock:
            with self.session_factory() as session:
                return decision_data(session, npc_id)

    async def list_goals(self, npc_id: int | None = None) -> list[dict[str, Any]]:
        async with self.lock:
            with self.session_factory() as session:
                query = select(NPC).order_by(NPC.id)
                if npc_id is not None:
                    query = query.where(NPC.id == npc_id)
                npcs = list(session.scalars(query))
                goals: list[dict[str, Any]] = []
                for npc in npcs:
                    for snapshot in goal_snapshots(session, npc):
                        goals.append({"npc_id": npc.id, "npc_name": npc.name, **snapshot})
                return goals

    async def list_memories(
        self,
        npc_id: int,
        *,
        limit: int = 50,
        min_importance: int = 1,
        emotion: str | None = None,
    ) -> list[dict[str, Any]]:
        async with self.lock:
            with self.session_factory() as session:
                query = select(Memory).where(
                    Memory.npc_id == npc_id,
                    Memory.importance >= min_importance,
                )
                if emotion is not None:
                    query = query.where(Memory.emotion == emotion)
                records = list(
                    session.scalars(
                        query.order_by(Memory.timestamp.desc(), Memory.id.desc()).limit(limit)
                    )
                )
                names = {npc.id: npc.name for npc in session.scalars(select(NPC))}
                memories: list[dict[str, Any]] = []
                for record in records:
                    clock = ClockSnapshot(record.timestamp)
                    memories.append(
                        {
                            "id": record.id,
                            "npc_id": record.npc_id,
                            "npc_name": names.get(record.npc_id),
                            "content": record.content,
                            "importance": record.importance,
                            "emotion": record.emotion,
                            "timestamp": record.timestamp,
                            "world_day": clock.day,
                            "world_time": clock.time_text,
                            "time_label": clock.label,
                            "related_npc_id": record.related_npc_id,
                            "related_npc_name": names.get(record.related_npc_id),
                        }
                    )
                return memories

    async def list_events(self, limit: int = 100) -> list[dict[str, Any]]:
        from simulation.dashboard import events_data

        async with self.lock:
            with self.session_factory() as session:
                return events_data(session, limit)

    async def list_relationships(self) -> list[dict[str, Any]]:
        async with self.lock:
            with self.session_factory() as session:
                names = {npc.id: npc.name for npc in session.scalars(select(NPC))}
                records = list(session.scalars(select(Relationship).order_by(Relationship.from_npc_id, Relationship.to_npc_id)))
                return [{"id": item.id, "from_npc_id": item.from_npc_id, "from_name": names[item.from_npc_id], "to_npc_id": item.to_npc_id, "to_name": names[item.to_npc_id], "score": item.score} for item in records]

    async def economy_status(self) -> dict[str, Any]:
        async with self.lock:
            with self.session_factory() as session:
                if not self.economy_enabled:
                    return {"enabled": False, "mode": "legacy", "stores": 0, "items": 0, "transactions": 0}
                return {
                    "enabled": True,
                    "mode": "v0.5",
                    "stores": session.scalar(select(func.count()).select_from(Store)) or 0,
                    "items": session.scalar(select(func.count()).select_from(ItemDefinition)) or 0,
                    "transactions": session.scalar(select(func.count()).select_from(EconomicTransaction)) or 0,
                }

    async def list_stores(self) -> list[dict[str, Any]]:
        async with self.lock:
            with self.session_factory() as session:
                return store_catalog_snapshot(session) if self.economy_enabled else []

    async def list_professions(self) -> list[dict[str, Any]]:
        async with self.lock:
            with self.session_factory() as session:
                profiles = {
                    profile.profession_key: profile
                    for profile in session.scalars(select(EmploymentProfile))
                }
                return [
                    {
                        "key": key,
                        "label": definition["label"],
                        "employer": definition["employer"],
                        "base_wage": definition["base_wage"],
                        "primary_skill": definition["skill"],
                        "active_workers": session.scalar(
                            select(func.count()).select_from(EmploymentProfile).where(
                                EmploymentProfile.profession_key == key
                            )
                        ) or 0,
                    }
                    for key, definition in PROFESSIONS.items()
                ] if self.economy_enabled else []

    async def get_npc_economy(self, npc_id: int) -> dict[str, Any] | None:
        async with self.lock:
            with self.session_factory() as session:
                npc = session.get(NPC, npc_id)
                if npc is None:
                    return None
                if not self.economy_enabled:
                    return {"enabled": False, "mode": "legacy", "npc_id": npc.id, "npc_name": npc.name, "balance": round(npc.money, 2)}
                return {"enabled": True, "mode": "v0.5", **npc_economy_snapshot(session, npc)}

    async def career_budget_status(self) -> dict[str, Any]:
        async with self.lock:
            with self.session_factory() as session:
                if not self.career_budget_enabled:
                    return {"enabled": False, "mode": "v0.5-compatible", "careers": 0, "budgets": 0, "reports": 0}
                return {
                    "enabled": True,
                    "mode": "v0.6",
                    "careers": session.scalar(select(func.count()).select_from(CareerDevelopment)) or 0,
                    "budgets": session.scalar(select(func.count()).select_from(PersonalBudget)) or 0,
                    "reports": session.scalar(select(func.count()).select_from(WeeklyEconomicReport)) or 0,
                }

    async def get_npc_career(self, npc_id: int) -> dict[str, Any] | None:
        async with self.lock:
            with self.session_factory() as session:
                npc = session.get(NPC, npc_id)
                if npc is None:
                    return None
                if not self.career_budget_enabled:
                    return {"enabled": False, "mode": "v0.5-compatible", "npc_id": npc.id, "npc_name": npc.name}
                snapshot = career_snapshot(session, npc, session.get(WorldState, 1).total_minutes)
                return {"enabled": snapshot is not None, "mode": "v0.6" if snapshot else "v0.5-compatible", **(snapshot or {"npc_id": npc.id, "npc_name": npc.name})}

    async def get_npc_budget(self, npc_id: int) -> dict[str, Any] | None:
        async with self.lock:
            with self.session_factory() as session:
                npc = session.get(NPC, npc_id)
                if npc is None:
                    return None
                if not self.career_budget_enabled:
                    return {"enabled": False, "mode": "v0.5-compatible", "npc_id": npc.id, "npc_name": npc.name}
                snapshot = budget_snapshot(session, npc, session.get(WorldState, 1).total_minutes)
                return {"enabled": snapshot is not None, "mode": "v0.6" if snapshot else "v0.5-compatible", **(snapshot or {"npc_id": npc.id, "npc_name": npc.name})}

    async def list_weekly_reports(self, npc_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        async with self.lock:
            with self.session_factory() as session:
                return report_snapshots(session, npc_id, limit) if self.career_budget_enabled else []

    async def community_status(self) -> dict[str, Any]:
        async with self.lock:
            with self.session_factory() as session:
                if not self.community_enabled:
                    return {
                        "enabled": False, "mode": "v0.6-compatible", "institutions": 0,
                        "schedules": 0, "stock_items": 0, "training_records": 0,
                        "housing_upgrades": 0,
                    }
                return {
                    "enabled": True, "mode": "v0.7",
                    "institutions": session.scalar(select(func.count()).select_from(CommunityInstitution)) or 0,
                    "schedules": session.scalar(select(func.count()).select_from(WorkSchedule)) or 0,
                    "stock_items": session.scalar(select(func.count()).select_from(StoreStock)) or 0,
                    "training_records": session.scalar(select(func.count()).select_from(TrainingRecord)) or 0,
                    "housing_upgrades": session.scalar(select(func.count()).select_from(HousingUpgradeRecord)) or 0,
                }

    async def list_institutions(self) -> list[dict[str, Any]]:
        async with self.lock:
            with self.session_factory() as session:
                state = session.get(WorldState, 1)
                if not self.community_enabled or state is None:
                    return []
                return institution_snapshots(session, ClockSnapshot(state.total_minutes))

    async def list_stock(self) -> list[dict[str, Any]]:
        async with self.lock:
            with self.session_factory() as session:
                return stock_snapshots(session) if self.community_enabled else []

    async def get_npc_rhythm(self, npc_id: int) -> dict[str, Any] | None:
        async with self.lock:
            with self.session_factory() as session:
                npc = session.get(NPC, npc_id)
                if npc is None:
                    return None
                if not self.community_enabled:
                    return {
                        "enabled": False, "mode": "v0.6-compatible",
                        "npc_id": npc.id, "npc_name": npc.name,
                    }
                state = session.get(WorldState, 1)
                snapshot = npc_rhythm_snapshot(session, npc, ClockSnapshot(state.total_minutes)) if state else None
                return {
                    "enabled": snapshot is not None,
                    "mode": "v0.7" if snapshot else "v0.6-compatible",
                    **(snapshot or {"npc_id": npc.id, "npc_name": npc.name}),
                }

    async def social_life_status(self) -> dict[str, Any]:
        async with self.lock:
            with self.session_factory() as session:
                if not self.social_life_enabled:
                    return {
                        "enabled": False, "mode": "v0.7-compatible", "bonds": 0,
                        "active_circles": 0, "planned_commitments": 0, "joint_activities": 0,
                        "active_households": 0, "shared_expenses": 0,
                    }
                return {
                    "enabled": True, "mode": "v0.8",
                    "bonds": session.scalar(select(func.count()).select_from(SocialBond)) or 0,
                    "active_circles": session.scalar(select(func.count()).select_from(FriendCircle).where(FriendCircle.active.is_(True))) or 0,
                    "planned_commitments": session.scalar(select(func.count()).select_from(SocialCommitment).where(SocialCommitment.status == "planned")) or 0,
                    "joint_activities": session.scalar(select(func.count()).select_from(JointActivity)) or 0,
                    "active_households": session.scalar(select(func.count()).select_from(CohousingHousehold).where(CohousingHousehold.active.is_(True))) or 0,
                    "shared_expenses": session.scalar(select(func.count()).select_from(SharedExpense)) or 0,
                }

    async def list_social_bonds(self) -> list[dict[str, Any]]:
        async with self.lock:
            with self.session_factory() as session:
                return bond_snapshots(session) if self.social_life_enabled else []

    async def list_friend_circles(self) -> list[dict[str, Any]]:
        async with self.lock:
            with self.session_factory() as session:
                return circle_snapshots(session) if self.social_life_enabled else []

    async def list_commitments(self) -> list[dict[str, Any]]:
        async with self.lock:
            with self.session_factory() as session:
                return commitment_snapshots(session) if self.social_life_enabled else []

    async def list_households(self) -> list[dict[str, Any]]:
        async with self.lock:
            with self.session_factory() as session:
                return household_snapshots(session) if self.social_life_enabled else []

    async def get_npc_social_life(self, npc_id: int) -> dict[str, Any] | None:
        async with self.lock:
            with self.session_factory() as session:
                npc = session.get(NPC, npc_id)
                if npc is None:
                    return None
                if not self.social_life_enabled:
                    return {"enabled": False, "mode": "v0.7-compatible", "npc_id": npc.id, "npc_name": npc.name}
                snapshot = npc_social_snapshot(session, npc)
                return {
                    "enabled": snapshot is not None,
                    "mode": "v0.8" if snapshot else "v0.7-compatible",
                    **(snapshot or {"npc_id": npc.id, "npc_name": npc.name}),
                }

    async def life_story_status(self) -> dict[str, Any]:
        async with self.lock:
            with self.session_factory() as session:
                if not self.life_story_enabled or session.get(StoryState, 1) is None:
                    return {
                        "enabled": False, "mode": "v0.8-compatible", "milestones": 0,
                        "weekly_summaries": 0, "monthly_summaries": 0,
                        "causal_links": 0, "replay_checkpoints": 0,
                    }
                return {
                    "enabled": True, "mode": "v0.9",
                    "milestones": session.scalar(select(func.count()).select_from(LifeMilestone)) or 0,
                    "weekly_summaries": session.scalar(select(func.count()).select_from(StorySummary).where(StorySummary.period_type == "week")) or 0,
                    "monthly_summaries": session.scalar(select(func.count()).select_from(StorySummary).where(StorySummary.period_type == "month")) or 0,
                    "causal_links": session.scalar(select(func.count()).select_from(CausalLink)) or 0,
                    "replay_checkpoints": session.scalar(select(func.count()).select_from(ReplayCheckpoint)) or 0,
                }

    async def list_milestones(
        self, npc_id: int | None = None, milestone_type: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        async with self.lock:
            with self.session_factory() as session:
                return milestone_snapshots(session, npc_id, milestone_type, limit) if self.life_story_enabled else []

    async def list_story_summaries(
        self, period_type: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        async with self.lock:
            with self.session_factory() as session:
                return summary_snapshots(session, period_type, limit) if self.life_story_enabled else []

    async def get_causal_chain(self, milestone_id: int) -> dict[str, Any] | None:
        async with self.lock:
            with self.session_factory() as session:
                return causal_chain_snapshot(session, milestone_id) if self.life_story_enabled else None

    async def get_npc_timeline(self, npc_id: int, limit: int = 100) -> dict[str, Any] | None:
        async with self.lock:
            with self.session_factory() as session:
                npc = session.get(NPC, npc_id)
                if npc is None:
                    return None
                if not self.life_story_enabled or session.get(StoryState, 1) is None:
                    return {"enabled": False, "mode": "v0.8-compatible", "npc_id": npc.id, "npc_name": npc.name, "milestones": []}
                return {
                    "enabled": True, "mode": "v0.9", "npc_id": npc.id, "npc_name": npc.name,
                    "milestones": milestone_snapshots(session, npc.id, None, limit),
                }

    async def replay_life_story(
        self, start_minute: int | None = None, end_minute: int | None = None, seed: int | None = None
    ) -> dict[str, Any]:
        async with self.lock:
            with self.session_factory() as session:
                state = session.get(WorldState, 1)
                story = session.get(StoryState, 1)
                if not self.life_story_enabled or state is None or story is None:
                    return {"enabled": False, "mode": "v0.8-compatible", "milestones": [], "summaries": [], "checkpoints": []}
                replay_seed = state.seed if seed is None else seed
                if replay_seed != state.seed:
                    raise ValueError(f"回放 seed 必须与世界 seed {state.seed} 一致")
                start = story.initialized_minute if start_minute is None else start_minute
                end = state.total_minutes + 1 if end_minute is None else end_minute
                return {"enabled": True, **replay_story(session, start, end, replay_seed)}

    async def productization_status(self) -> dict[str, Any]:
        async with self.lock:
            with self.session_factory() as session:
                status = product_status(session, self.product_enabled)
                if status["enabled"] and self.save_manager is not None:
                    status["active_slot"] = self.save_manager.active_slot()
                return status

    async def world_statistics(self) -> dict[str, Any]:
        async with self.lock:
            with self.session_factory() as session:
                if not self.product_enabled or session.get(ProductState, 1) is None:
                    return {"enabled": False, "mode": "v0.9-compatible", "metrics": {}, "sources": {}}
                return latest_statistics(session)

    async def balance_status(self) -> dict[str, Any]:
        async with self.lock:
            with self.session_factory() as session:
                if not self.product_enabled or session.get(ProductState, 1) is None:
                    return {"enabled": False, "mode": "v0.9-compatible", "status": "unavailable", "violations": []}
                return latest_balance(session)

    async def list_upgrade_reports(self) -> list[dict[str, Any]]:
        async with self.lock:
            with self.session_factory() as session:
                return upgrade_reports(session) if self.product_enabled else []

    async def get_onboarding(self) -> dict[str, Any]:
        async with self.lock:
            with self.session_factory() as session:
                if not self.product_enabled:
                    return {"enabled": False, "mode": "v0.9-compatible", "steps": []}
                return onboarding_snapshot(session)

    async def set_onboarding(self, request: OnboardingRequest) -> dict[str, Any]:
        async with self.lock:
            with self.session_factory() as session:
                if not self.product_enabled:
                    raise RuntimeError("V1.0 产品层未启用")
                return update_onboarding(session, request)

    async def list_save_slots(self) -> dict[str, Any]:
        if not self.product_enabled or self.save_manager is None:
            return {"enabled": False, "mode": "v0.9-compatible", "active_slot": None, "slots": []}
        return self.save_manager.list_slots()

    async def create_save_slot(self, request: CreateSaveRequest) -> dict[str, Any]:
        if not self.product_enabled or self.save_manager is None:
            raise RuntimeError("V1.0 多存档未启用")
        async with self.lock:
            return self.save_manager.create_slot(request)

    async def export_save_slot(self, slot_id: str) -> dict[str, Any]:
        if not self.product_enabled or self.save_manager is None:
            raise RuntimeError("V1.0 数据导出未启用")
        async with self.lock:
            return self.save_manager.export_slot(slot_id)

    async def import_save_slot(self, request: ImportSaveRequest) -> dict[str, Any]:
        if not self.product_enabled or self.save_manager is None:
            raise RuntimeError("V1.0 数据导入未启用")
        async with self.lock:
            return self.save_manager.import_export(request)

    async def process_narrative_jobs(self, limit: int = 10) -> int:
        async with self.narrative_lock:
            return await process_jobs(self.session_factory, self.narrative_generator, limit)

    async def process_agent_decision_jobs(self, limit: int = 5) -> int:
        if not self.agent_enabled and not self.agent_takeover_npc_ids:
            return 0
        if isinstance(self.agent_generator.provider, RuntimeProvider):
            if self.model_runtime._state_snapshot()[0] != "online":
                return 0
        async with self.agent_lock:
            if self.agent_takeover_npc_ids:
                return await process_takeover_jobs(
                    self.session_factory,
                    self.agent_generator,
                    limit,
                    max_concurrency=self.agent_worker_concurrency,
                    eligible_npc_ids=self.agent_takeover_npc_ids,
                )
            if self.agent_shadow_requested:
                return await process_agent_jobs(
                    self.session_factory, self.agent_generator, limit
                )
            return 0

    async def process_agent_conversation_jobs(self, limit: int = 5) -> int:
        if isinstance(self.conversation_generator.provider, RuntimeProvider):
            if self.model_runtime._state_snapshot()[0] != "online":
                return 0
        async with self.conversation_lock:
            return await process_conversation_tasks(
                self.session_factory, self.conversation_generator, limit
            )

    async def recover_agent_conversation_jobs(self) -> int:
        async with self.conversation_lock:
            with self.session_factory() as session:
                if not V14_TABLE_NAMES.issubset(set(inspect(session.get_bind()).get_table_names())):
                    return 0
                recovered = recover_conversation_tasks(session)
                session.commit()
                return recovered

    async def set_agent_conversations(self, enabled: bool) -> dict[str, Any]:
        self.agent_conversations_enabled = bool(self.product_enabled and enabled)
        return await self.agent_conversation_status()

    async def agent_conversation_status(self) -> dict[str, Any]:
        with self.session_factory() as session:
            counts = {
                status: session.scalar(
                    select(func.count()).select_from(AgentConversation).where(
                        AgentConversation.status == status
                    )
                ) or 0
                for status in (
                    "active", "ready_for_settlement", "completed", "failed", "cancelled", "expired"
                )
            }
            tasks = {
                status: session.scalar(
                    select(func.count()).select_from(AgentConversationTask).where(
                        AgentConversationTask.status == status
                    )
                ) or 0
                for status in ("pending", "processing", "completed", "discarded")
            }
        active_depth = tasks["pending"] + tasks["processing"]
        return {
            "enabled": self.agent_conversations_enabled,
            "mode": "multi_round" if self.agent_conversations_enabled else "v1.3_legacy_dialogue",
            "version": "1.4.0",
            "enabled_npc_ids": sorted(self.agent_takeover_npc_ids),
            "provider": self.conversation_generator.status(),
            "bounds": {
                "turns": {"minimum": 3, "maximum": 6},
                "utterance_characters": 280,
                "max_concurrency": self.conversation_generator.settings.max_concurrency,
                "queue_depth": active_depth,
                "queue_limit": min(10, self.conversation_generator.settings.max_active_conversations),
                "bounded": active_depth <= min(10, self.conversation_generator.settings.max_active_conversations),
            },
            "authority": {
                "facts": "simulation_engine_only",
                "model": "validated_text_only",
                "hidden_reasoning_requested": False,
            },
            "counts": counts,
            "tasks": tasks,
        }

    async def list_agent_conversations(
        self,
        *,
        npc_id: int | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            query = select(AgentConversation)
            if npc_id is not None:
                query = query.where(or_(
                    AgentConversation.actor_npc_id == npc_id,
                    AgentConversation.target_npc_id == npc_id,
                ))
            if status is not None:
                query = query.where(AgentConversation.status == status)
            rows = list(session.scalars(
                query.order_by(AgentConversation.id.desc()).limit(min(max(limit, 1), 100))
            ))
            return [conversation_snapshot(session, row) for row in rows]

    async def get_agent_conversation(self, conversation_id: int) -> dict[str, Any] | None:
        with self.session_factory() as session:
            row = session.get(AgentConversation, conversation_id)
            return conversation_snapshot(session, row) if row is not None else None

    async def cancel_agent_conversation(self, conversation_id: int) -> dict[str, Any] | None:
        async with self.conversation_lock:
            with self.session_factory() as session:
                row = session.get(AgentConversation, conversation_id)
                if row is None:
                    return None
                cancel_conversation(session, conversation_id)
                session.commit()
                return conversation_snapshot(session, row)

    async def agent_conversation_safety_check(self) -> dict[str, Any]:
        with self.session_factory() as session:
            return conversation_safety_check(
                session, min(10, self.conversation_generator.settings.max_active_conversations)
            )

    async def process_agent_reflection_jobs(self, limit: int = 5) -> int:
        if not self.agent_cognition_enabled:
            return 0
        if isinstance(self.reflection_generator.provider, RuntimeProvider):
            if self.model_runtime._state_snapshot()[0] != "online":
                return 0
        async with self.cognition_lock:
            return await process_reflection_tasks(
                self.session_factory, self.reflection_generator, limit
            )

    async def recover_agent_reflection_jobs(self) -> int:
        async with self.cognition_lock:
            with self.session_factory() as session:
                if not V15_TABLE_NAMES.issubset(set(inspect(session.get_bind()).get_table_names())):
                    return 0
                recovered = recover_reflection_tasks(session)
                session.commit()
                return recovered

    async def set_agent_cognition(self, enabled: bool) -> dict[str, Any]:
        self.agent_cognition_npc_ids = (
            set(SUPPORTED_NPC_IDS) if self.product_enabled and enabled else set()
        )
        self.agent_cognition_enabled = bool(self.agent_cognition_npc_ids)
        if self.agent_cognition_enabled:
            with self.session_factory() as session:
                state = session.get(WorldState, 1)
                ensure_cognition_states(
                    session, self.agent_cognition_npc_ids, state.total_minutes if state else 480
                )
                session.commit()
        return await self.agent_cognition_status()

    async def set_npc_agent_cognition(self, npc_id: int, enabled: bool) -> dict[str, Any]:
        if npc_id not in SUPPORTED_NPC_IDS:
            raise ValueError("unsupported_npc")
        if self.product_enabled and enabled:
            self.agent_cognition_npc_ids.add(npc_id)
            with self.session_factory() as session:
                state = session.get(WorldState, 1)
                ensure_cognition_states(session, {npc_id}, state.total_minutes if state else 480)
                session.commit()
        else:
            self.agent_cognition_npc_ids.discard(npc_id)
        self.agent_cognition_enabled = bool(self.agent_cognition_npc_ids)
        snapshot = await self.get_agent_cognition(npc_id)
        assert snapshot is not None
        snapshot["enabled"] = npc_id in self.agent_cognition_npc_ids
        return snapshot

    async def agent_cognition_status(self) -> dict[str, Any]:
        with self.session_factory() as session:
            task_counts = {
                status: session.scalar(select(func.count()).select_from(AgentReflectionTask).where(
                    AgentReflectionTask.status == status
                )) or 0
                for status in ("pending", "processing", "completed", "cancelled", "discarded")
            }
            reflection_count = session.scalar(select(func.count()).select_from(AgentReflection)) or 0
            belief_count = session.scalar(select(func.count()).select_from(AgentSubjectiveBelief)) or 0
            plan_counts = {
                status: session.scalar(select(func.count()).select_from(AgentPlan).where(
                    AgentPlan.status == status
                )) or 0
                for status in ("pending", "in_progress", "completed", "failed", "expired", "cancelled")
            }
        active = task_counts["pending"] + task_counts["processing"]
        return {
            "enabled": self.agent_cognition_enabled,
            "version": "1.5.0",
            "enabled_npc_ids": sorted(self.agent_cognition_npc_ids),
            "global_enabled": self.agent_cognition_npc_ids == set(SUPPORTED_NPC_IDS),
            "provider": self.reflection_generator.status(),
            "bounds": {
                "daily_reflections_per_npc": self.reflection_generator.settings.max_reflections_per_day,
                "max_concurrency": self.reflection_generator.settings.max_concurrency,
                "queue_depth": active,
                "queue_limit": self.reflection_generator.settings.queue_limit,
                "bounded": active <= self.reflection_generator.settings.queue_limit,
                "plan_steps_per_reflection": {"minimum": 1, "maximum": 3},
            },
            "counts": {"reflections": reflection_count, "beliefs": belief_count, "tasks": task_counts, "plans": plan_counts},
            "authority": {
                "facts": "simulation_engine_only", "beliefs": "subjective_only",
                "plans": "non_executable_engine_monitored", "hidden_reasoning_requested": False,
            },
        }

    async def get_agent_cognition(self, npc_id: int) -> dict[str, Any] | None:
        with self.session_factory() as session:
            result = cognition_snapshot(session, npc_id)
            if result is not None:
                result["enabled"] = npc_id in self.agent_cognition_npc_ids
            return result

    async def list_agent_reflections(self, npc_id: int, limit: int = 30) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            rows = list(session.scalars(select(AgentReflection).where(
                AgentReflection.npc_id == npc_id
            ).order_by(AgentReflection.id.desc()).limit(min(max(limit, 1), 100))))
            return [reflection_snapshot(session, row) for row in rows]

    async def list_agent_plans(self, npc_id: int, limit: int = 100) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            rows = list(session.scalars(select(AgentPlan).where(
                AgentPlan.npc_id == npc_id
            ).order_by(AgentPlan.id.desc()).limit(min(max(limit, 1), 200))))
            return [plan_snapshot(row) for row in rows]

    async def cancel_agent_reflection_task(self, task_id: int) -> dict[str, Any] | None:
        async with self.cognition_lock:
            with self.session_factory() as session:
                task = session.get(AgentReflectionTask, task_id)
                if task is None:
                    return None
                cancel_reflection_task(session, task_id)
                session.commit()
                return {
                    "id": task.id, "npc_id": task.npc_id, "reflection_day": task.reflection_day,
                    "trigger_type": task.trigger_type, "status": task.status,
                    "attempts": task.attempts, "error": task.last_error_code,
                }

    async def agent_cognition_safety_check(self) -> dict[str, Any]:
        with self.session_factory() as session:
            return cognition_safety_check(session, self.reflection_generator.settings.queue_limit)

    async def recover_agent_decision_jobs(self) -> int:
        if not self.product_enabled:
            return 0
        async with self.agent_lock:
            with self.session_factory() as session:
                state = session.get(WorldState, 1)
                recovered = recover_takeover_leases(
                    session,
                    now=datetime.now(timezone.utc),
                    world_minute=state.total_minutes if state else 0,
                ) if V12_TABLE_NAMES.issubset(
                    set(inspect(session.get_bind()).get_table_names())
                ) else 0
                recovered += reset_interrupted_agent_jobs(session)
                session.commit()
                return recovered

    async def agent_status(self) -> dict[str, Any]:
        with self.session_factory() as session:
            result = agent_status_snapshot(
                session, self.agent_shadow_requested, self.agent_generator
            )
            if 1 in self.agent_takeover_npc_ids:
                result["mode"] = "takeover"
                result["authority"] = "engine_validated_takeover"
                result["enabled"] = True
            return result

    async def set_agent_takeover(self, enabled: bool) -> dict[str, Any]:
        """V1.2 compatibility alias: toggle Alice only, never broaden scope."""

        await self.set_npc_agent_takeover(1, enabled)
        return await self.agent_takeover_status()

    async def set_npc_agent_takeover(self, npc_id: int, enabled: bool) -> dict[str, Any]:
        if npc_id not in SUPPORTED_NPC_IDS:
            raise ValueError("unsupported_npc")
        if self.product_enabled and enabled:
            self.agent_takeover_npc_ids.add(npc_id)
            self.agent_takeover_enabled = True
            self.agent_enabled = True
        else:
            self.agent_takeover_npc_ids.discard(npc_id)
        self.agent_takeover_requested = 1 in self.agent_takeover_npc_ids
        if not self.agent_takeover_npc_ids and not self.agent_shadow_requested:
            # Existing durable turns remain visible and are resolved as explicit
            # takeover_disabled fallbacks by the Engine on its next tick.
            self.agent_enabled = False
        return await self.latest_agent_control_v13(npc_id)

    async def set_all_agent_takeovers(self, enabled: bool) -> dict[str, Any]:
        self.agent_takeover_npc_ids = (
            set(SUPPORTED_NPC_IDS) if self.product_enabled and enabled else set()
        )
        self.agent_takeover_requested = 1 in self.agent_takeover_npc_ids
        if self.agent_takeover_npc_ids:
            self.agent_takeover_enabled = True
            self.agent_enabled = True
        elif not self.agent_shadow_requested:
            self.agent_enabled = False
        return await self.agent_takeover_overview()

    async def agent_takeover_status(self) -> dict[str, Any]:
        with self.session_factory() as session:
            counts = {
                state: session.scalar(
                    select(func.count()).select_from(AgentTakeoverTurn).where(
                        AgentTakeoverTurn.state == state,
                        AgentTakeoverTurn.npc_id == 1,
                    )
                ) or 0
                for state in ("waiting", "ready", "agent_executing", "fallback_executing", "completed")
            }
            current = latest_takeover_snapshot(session, 1)
        return {
            "enabled": 1 in self.agent_takeover_npc_ids,
            "mode": "takeover" if 1 in self.agent_takeover_npc_ids else "disabled",
            "target_npc_id": 1,
            "target_npc_name": "Alice",
            "provider": self.agent_generator.status(),
            "authority": "engine_validated_takeover" if 1 in self.agent_takeover_npc_ids else "advisory_only",
            "counts": counts,
            "current": current,
            "enabled_npc_ids": sorted(self.agent_takeover_npc_ids),
            "global_enabled": self.agent_takeover_npc_ids == set(SUPPORTED_NPC_IDS),
        }

    def _agent_control_snapshot(self, session: Session, npc_id: int) -> dict[str, Any]:
        snapshot = latest_takeover_snapshot(session, npc_id)
        queue = {
            status: session.scalar(
                select(func.count()).select_from(AgentDecisionJob).join(
                    AgentTakeoverTurn, AgentTakeoverTurn.job_id == AgentDecisionJob.id
                ).where(
                    AgentDecisionJob.npc_id == npc_id,
                    AgentDecisionJob.status == status,
                    AgentTakeoverTurn.state.in_(("waiting", "ready")),
                )
            ) or 0
            for status in ("pending", "processing")
        }
        recent = takeover_audit_snapshots(session, npc_id, 3)
        agent = snapshot.get("agent") if snapshot else None
        final = snapshot.get("final") if snapshot else None
        return {
            "supported": npc_id in SUPPORTED_NPC_IDS,
            "npc_id": npc_id,
            "status": "idle" if snapshot is None else snapshot["state"],
            "enabled": npc_id in self.agent_takeover_npc_ids,
            "turn": snapshot,
            "queue": {**queue, "depth": queue["pending"] + queue["processing"], "limit": 1},
            "plan": (agent or {}).get("plan", []),
            "emotion": (agent or {}).get("emotion"),
            "final": final,
            "fallback": None if not final else final.get("fallback_reason_code"),
            "recent_audits": recent,
        }

    async def latest_agent_control(self, npc_id: int) -> dict[str, Any]:
        """V1.2 endpoint shape: Alice is the sole supported legacy target."""

        if npc_id != 1:
            return {"supported": False, "status": "unsupported", "npc_id": npc_id}
        return await self.latest_agent_control_v13(npc_id)

    async def latest_agent_control_v13(self, npc_id: int) -> dict[str, Any]:
        with self.session_factory() as session:
            return self._agent_control_snapshot(session, npc_id)

    def _agent_takeover_overview_snapshot(
        self, session: Session, *, npcs: list[NPC] | None = None
    ) -> dict[str, Any]:
        people = npcs if npcs is not None else list(
            session.scalars(select(NPC).where(NPC.id.in_(SUPPORTED_NPC_IDS)).order_by(NPC.id))
        )
        controls = []
        for npc in people:
            if npc.id not in SUPPORTED_NPC_IDS:
                continue
            control = self._agent_control_snapshot(session, npc.id)
            control["npc_name"] = npc.name
            controls.append(control)
        counts = {
            state: session.scalar(
                select(func.count()).select_from(AgentTakeoverTurn).where(
                    AgentTakeoverTurn.state == state
                )
            ) or 0
            for state in ("waiting", "ready", "agent_executing", "fallback_executing", "completed")
        }
        queue_depth = sum(item["queue"]["depth"] for item in controls)
        return {
            "enabled": bool(self.agent_takeover_npc_ids),
            "global_enabled": self.agent_takeover_npc_ids == set(SUPPORTED_NPC_IDS),
            "enabled_npc_ids": sorted(self.agent_takeover_npc_ids),
            "mode": "takeover" if self.agent_takeover_npc_ids else "disabled",
            "authority": "engine_validated_takeover",
            "provider": self.agent_generator.status(),
            "worker": {
                "max_concurrency": self.agent_worker_concurrency,
                "queue_depth": queue_depth,
                "queue_limit": len(SUPPORTED_NPC_IDS),
                "bounded": queue_depth <= len(SUPPORTED_NPC_IDS),
                "fairness": "oldest-active-turn-per-npc",
            },
            "counts": counts,
            "npcs": controls,
        }

    async def agent_takeover_overview(self) -> dict[str, Any]:
        with self.session_factory() as session:
            return self._agent_takeover_overview_snapshot(session)

    async def agent_audits(self, npc_id: int, limit: int = 50) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            return takeover_audit_snapshots(session, npc_id, limit)

    async def runtime_status(self) -> dict[str, Any]:
        return self.model_runtime.status()

    async def configure_runtime_provider(self, *, api_key: str, base_url: str, model: str) -> dict[str, Any]:
        result = await self.model_runtime.configure_provider(
            api_key=api_key,
            base_url=base_url,
            model=model,
        )
        provider = RuntimeProvider(self.model_runtime)
        agent_settings = AgentSettings(
            api_key=self.model_runtime.settings.api_key,
            base_url=self.model_runtime.settings.base_url,
            model=self.model_runtime.settings.model,
            timeout_seconds=self.model_runtime.settings.timeout_seconds,
            max_attempts=self.model_runtime.settings.max_attempts,
        )
        async with self.agent_lock:
            self.agent_generator.settings = agent_settings
            self.agent_generator.provider = provider
            self.conversation_generator.agent_settings = agent_settings
            self.conversation_generator.provider = provider
            self.reflection_generator.agent_settings = agent_settings
            self.reflection_generator.provider = provider
        return result

    async def start_runtime(self, npc_ids: set[int] | None = None) -> dict[str, Any]:
        selected = set(SUPPORTED_NPC_IDS if npc_ids is None else npc_ids)
        result = await self.model_runtime.transition("start", npc_ids=selected)
        self.agent_takeover_npc_ids = set(selected)
        self.agent_takeover_enabled = bool(selected)
        self.agent_takeover_requested = 1 in selected
        self.agent_enabled = bool(selected or self.agent_shadow_requested)
        self.agent_conversations_enabled = bool(selected)
        self.agent_cognition_npc_ids = set(selected)
        self.agent_cognition_enabled = bool(selected)
        if selected:
            with self.session_factory() as session:
                state = session.get(WorldState, 1)
                ensure_cognition_states(session, selected, state.total_minutes if state else 480)
                session.commit()
        return result

    async def pause_runtime(self) -> dict[str, Any]:
        return await self.model_runtime.transition("pause")

    async def resume_runtime(self) -> dict[str, Any]:
        return await self.model_runtime.transition("resume")

    async def stop_runtime(self, *, emergency: bool = False, reason: str | None = None) -> dict[str, Any]:
        action = "emergency_stop" if emergency else "stop"
        result = await self.model_runtime.transition(action, reason=reason)
        self.agent_conversations_enabled = False
        self.agent_cognition_npc_ids.clear()
        self.agent_cognition_enabled = False
        # Settle every already-authorized durable item without another provider call.
        async with self.lock:
            async with self.agent_lock:
                with self.session_factory() as session:
                    state = session.get(WorldState, 1)
                    if state is not None:
                        clock = ClockSnapshot(state.total_minutes)
                        for turn in list(session.scalars(select(AgentTakeoverTurn).where(
                            AgentTakeoverTurn.state.in_(("waiting", "ready"))
                        ))):
                            failure = "emergency_stop" if emergency else "runtime_stopped"
                            mark_turn_worker_failed(turn, failure)
                            job = session.get(AgentDecisionJob, turn.job_id) if turn.job_id else None
                            if job is not None and job.status in {"pending", "processing"}:
                                job.status = "failed"
                                job.last_error_code = failure
                                job.completed_at = datetime.now(timezone.utc)
                            # Stop is a cancellation boundary, not another world
                            # decision.  Do not run Utility or start a new action
                            # while cleaning up operator control.  Close the audit
                            # explicitly as unexecuted; the next Engine tick will
                            # make a fresh Utility decision after control is off.
                            turn.state = "completed"
                            turn.final_source = "utility_fallback"
                            turn.final_action = turn.utility_action
                            turn.final_target = None
                            turn.final_params_json = "{}"
                            turn.execution_validation_json = json.dumps({
                                "legal": False,
                                "reason_code": failure,
                                "latest_available": [],
                            }, ensure_ascii=False, separators=(",", ":"))
                            turn.fallback_reason_code = failure
                            turn.completion_json = json.dumps({
                                "status": "cancelled_before_execution",
                                "reason_code": failure,
                            }, ensure_ascii=False, separators=(",", ":"))
                            turn.action_started_minute = None
                            turn.action_end_minute = None
                            turn.action_completed_minute = clock.total_minutes
                    session.commit()
        self.agent_takeover_npc_ids.clear()
        self.agent_takeover_enabled = False
        self.agent_takeover_requested = False
        self.agent_enabled = self.agent_shadow_requested
        async with self.conversation_lock:
            with self.session_factory() as session:
                for row in list(session.scalars(select(AgentConversation).where(
                    AgentConversation.status.in_(("active", "ready_for_settlement"))
                ))):
                    cancel_conversation(session, row.id)
                session.commit()
        async with self.cognition_lock:
            with self.session_factory() as session:
                for row in list(session.scalars(select(AgentReflectionTask).where(
                    AgentReflectionTask.status.in_(("pending", "processing"))
                ))):
                    cancel_reflection_task(session, row.id)
                session.commit()
        return result

    async def set_runtime_npc(self, npc_id: int, enabled: bool) -> dict[str, Any]:
        result = await self.model_runtime.set_npc(npc_id, enabled)
        await self.set_npc_agent_takeover(npc_id, enabled)
        await self.set_npc_agent_cognition(npc_id, enabled)
        return result

    async def update_runtime_budget(self, values: dict[str, Any]) -> dict[str, Any]:
        return await self.model_runtime.update_budget(values)

    async def reset_runtime_budget(self) -> dict[str, Any]:
        return await self.model_runtime.reset_budget()

    async def runtime_consistency(self) -> dict[str, Any]:
        return self.model_runtime.consistency()

    async def dashboard_snapshot(self, groups: Iterable[str]) -> dict[str, Any]:
        from simulation.dashboard import build_dashboard_snapshot

        return await build_dashboard_snapshot(self, groups)

    async def dashboard_npc_snapshot(
        self, npc_id: int, sections: Iterable[str]
    ) -> dict[str, Any]:
        from simulation.dashboard import build_npc_dashboard_snapshot

        return await build_npc_dashboard_snapshot(self, npc_id, sections)

    async def latest_agent_shadow(self, npc_id: int) -> dict[str, Any]:
        with self.session_factory() as session:
            return agent_shadow_snapshot(
                session, npc_id, self.agent_enabled, self.agent_generator
            )

    async def narrative_status(self) -> dict[str, Any]:
        from simulation.dashboard import narrative_status_data

        with self.session_factory() as session:
            return narrative_status_data(self, session)

    async def list_narratives(
        self,
        kind: str,
        *,
        npc_id: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        from simulation.dashboard import narratives_data

        async with self.lock:
            with self.session_factory() as session:
                return narratives_data(session, kind, npc_id=npc_id, limit=limit)


async def simulation_loop(service: WorldService, stop_event: asyncio.Event) -> None:
    logger.info("Simulation loop started")
    while not stop_event.is_set():
        try:
            delay = await service.get_delay()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
                break
            except TimeoutError:
                await service.tick()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Simulation loop error")
            await asyncio.sleep(1)
    logger.info("Simulation loop stopped")


async def narrative_loop(service: WorldService, stop_event: asyncio.Event) -> None:
    logger.info("Narrative loop started")
    while not stop_event.is_set():
        try:
            processed = await service.process_narrative_jobs(limit=10)
            delay = 0.1 if processed else 1.0
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
                break
            except TimeoutError:
                continue
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Narrative loop error")
            await asyncio.sleep(1)
    logger.info("Narrative loop stopped")


async def agent_loop(service: WorldService, stop_event: asyncio.Event) -> None:
    logger.info("Agent worker started")
    recovered = await service.recover_agent_decision_jobs()
    if recovered:
        logger.warning("Recovered %s interrupted Agent jobs", recovered)
    while not stop_event.is_set():
        try:
            processed = await service.process_agent_decision_jobs(limit=5)
            delay = 0.1 if processed else 1.0
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
                break
            except TimeoutError:
                continue
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Agent worker error")
            await asyncio.sleep(1)
    logger.info("Agent worker stopped")


async def conversation_loop(service: WorldService, stop_event: asyncio.Event) -> None:
    logger.info("V1.4 conversation worker started")
    recovered = await service.recover_agent_conversation_jobs()
    if recovered:
        logger.warning("Recovered %s interrupted V1.4 conversation jobs", recovered)
    while not stop_event.is_set():
        try:
            processed = await service.process_agent_conversation_jobs(limit=5)
            delay = 0.1 if processed else 1.0
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
                break
            except TimeoutError:
                continue
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("V1.4 conversation worker error")
            await asyncio.sleep(1)
    logger.info("V1.4 conversation worker stopped")


async def cognition_loop(service: WorldService, stop_event: asyncio.Event) -> None:
    logger.info("V1.5 cognition worker started")
    recovered = await service.recover_agent_reflection_jobs()
    if recovered:
        logger.warning("Recovered %s interrupted V1.5 reflection tasks", recovered)
    while not stop_event.is_set():
        try:
            processed = await service.process_agent_reflection_jobs(limit=5)
            delay = 0.1 if processed else 1.0
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
                break
            except TimeoutError:
                continue
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("V1.5 cognition worker error")
            await asyncio.sleep(1)
    logger.info("V1.5 cognition worker stopped")
