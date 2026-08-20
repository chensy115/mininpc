from __future__ import annotations

import os
import logging
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from database.models import (
    AgentConversation,
    AgentConversationAudit,
    AgentConversationParticipantResult,
    AgentConversationTask,
    AgentConversationTurn,
    AgentCognitionState,
    AgentDecisionArtifact,
    AgentDecisionJob,
    AgentPlan,
    AgentReflection,
    AgentReflectionSource,
    AgentReflectionTask,
    AgentSubjectiveBelief,
    AgentTakeoverTurn,
    ModelBudgetConfig,
    ModelCallAudit,
    ModelCircuitState,
    ModelRuntimeState,
    ModelRuntimeAudit,
    BalanceAudit,
    Base,
    CausalLink,
    DataTransferAudit,
    LifeMilestone,
    OnboardingProgress,
    ProductState,
    ReplayCheckpoint,
    StoryState,
    StorySummary,
    UpgradeReport,
    WorldStatistic,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "world.db"
logger = logging.getLogger(__name__)
V09_TABLES = (
    StoryState.__table__, LifeMilestone.__table__, CausalLink.__table__,
    StorySummary.__table__, ReplayCheckpoint.__table__,
)
V10_TABLES = (
    ProductState.__table__, WorldStatistic.__table__, BalanceAudit.__table__,
    UpgradeReport.__table__, OnboardingProgress.__table__, DataTransferAudit.__table__,
)
V11_TABLES = (AgentDecisionJob.__table__, AgentDecisionArtifact.__table__)
V12_TABLES = (AgentTakeoverTurn.__table__,)
V12_TABLE_NAMES = {table.name for table in V12_TABLES}
V13_TABLES = V12_TABLES
V14_TABLES = (
    AgentConversation.__table__, AgentConversationTask.__table__,
    AgentConversationTurn.__table__, AgentConversationParticipantResult.__table__,
    AgentConversationAudit.__table__,
)
V14_TABLE_NAMES = {table.name for table in V14_TABLES}
V15_TABLES = (
    AgentCognitionState.__table__, AgentReflectionTask.__table__,
    AgentReflectionSource.__table__, AgentReflection.__table__,
    AgentSubjectiveBelief.__table__, AgentPlan.__table__,
)
V15_TABLE_NAMES = {table.name for table in V15_TABLES}
V16_TABLES = (
    ModelRuntimeState.__table__, ModelBudgetConfig.__table__,
    ModelCircuitState.__table__, ModelCallAudit.__table__,
    ModelRuntimeAudit.__table__,
)
V16_TABLE_NAMES = {table.name for table in V16_TABLES}


def _table_sql(engine: Engine) -> dict[str, str]:
    with engine.connect() as connection:
        return {
            str(name): str(sql)
            for name, sql in connection.execute(
                text(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            )
        }


def _upgrade_v13_takeover_table(engine: Engine) -> bool:
    """Remove the V1.2 Alice-only CHECK while preserving every audit row.

    SQLite cannot drop a CHECK constraint in place. The rewrite is transactional,
    preserves the exact column set, and is a no-op for fresh/already-upgraded DBs.
    """

    sql = _table_sql(engine).get("agent_takeover_turns", "")
    if "ck_agent_takeover_alice_only" not in sql and "npc_id = 1" not in sql:
        return False
    table = AgentTakeoverTurn.__table__
    columns = ", ".join(f'"{column.name}"' for column in table.columns)
    backup = "agent_takeover_turns_v12_backup"
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        transaction = connection.begin()
        try:
            existing_backup = connection.exec_driver_sql(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (backup,)
            ).scalar_one_or_none()
            if existing_backup is not None:
                raise RuntimeError("stale V1.3 takeover migration backup exists")
            connection.exec_driver_sql(
                f'ALTER TABLE "agent_takeover_turns" RENAME TO "{backup}"'
            )
            connection.exec_driver_sql("DROP INDEX IF EXISTS uq_agent_takeover_active_npc")
            table.create(connection)
            connection.exec_driver_sql(
                f'INSERT INTO "agent_takeover_turns" ({columns}) '
                f'SELECT {columns} FROM "{backup}"'
            )
            old_count = connection.exec_driver_sql(
                f'SELECT COUNT(*) FROM "{backup}"'
            ).scalar_one()
            new_count = connection.exec_driver_sql(
                'SELECT COUNT(*) FROM "agent_takeover_turns"'
            ).scalar_one()
            if old_count != new_count:
                raise RuntimeError("V1.3 takeover migration row-count mismatch")
            connection.exec_driver_sql(f'DROP TABLE "{backup}"')
            transaction.commit()
        except Exception:
            transaction.rollback()
            raise
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()
    return True


def create_database(db_path: str | Path | None = None) -> tuple[Engine, sessionmaker[Session]]:
    configured = db_path or os.getenv("MINIWORLD_DB_PATH") or DEFAULT_DB_PATH
    path = Path(configured).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 30.0},
    )
    before_sql = _table_sql(engine)

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    # V0.9 migration is isolated: a failure must not prevent the exact V0.8
    # schema and simulation from opening. WorldService verifies completeness
    # before enabling any V0.9 reads or writes.
    old_tables = [
        table for table in Base.metadata.sorted_tables
        if table not in V09_TABLES and table not in V10_TABLES
        and table not in V11_TABLES and table not in V12_TABLES
        and table not in V14_TABLES and table not in V15_TABLES
        and table not in V16_TABLES
    ]
    Base.metadata.create_all(engine, tables=old_tables)
    try:
        Base.metadata.create_all(engine, tables=list(V09_TABLES))
    except SQLAlchemyError:
        logger.exception("V0.9 additive schema creation failed; V0.8 database remains available")
    # V1.0 schema is separately isolated. A partial or failed product migration
    # leaves the exact V0.9 engine and all of its facts usable.
    try:
        Base.metadata.create_all(engine, tables=list(V10_TABLES))
    except SQLAlchemyError:
        logger.exception("V1.0 additive schema creation failed; V0.9 database remains available")
    # V1.1 shadow-agent tables are additive and never gate V1.0 startup.
    try:
        Base.metadata.create_all(engine, tables=list(V11_TABLES))
    except SQLAlchemyError:
        logger.exception("V1.1 additive schema creation failed; V1.0 database remains available")
    # V1.2 takeover orchestration is isolated from both legacy facts and the
    # V1.1 advisory queue. A failed migration leaves exact V1.1 available.
    try:
        Base.metadata.create_all(engine, tables=list(V12_TABLES))
    except SQLAlchemyError:
        logger.exception("V1.2 additive schema creation failed; V1.1 remains available")
    # V1.3 reuses the durable V1.2 table but widens its ownership constraint
    # from Alice to all five NPCs. Existing audit rows survive the idempotent
    # SQLite table rewrite. A failure leaves the V1.2 table transaction intact.
    try:
        if _upgrade_v13_takeover_table(engine):
            logger.info("Upgraded takeover audit schema for five V1.3 NPC agents")
    except (SQLAlchemyError, RuntimeError):
        logger.exception("V1.3 takeover migration failed; V1.2 data remains available")
    # V1.4 conversations are wholly additive. They never rewrite a legacy table.
    try:
        Base.metadata.create_all(engine, tables=list(V14_TABLES))
    except SQLAlchemyError:
        logger.exception("V1.4 additive conversation schema failed; V1.3 remains available")
    # V1.5 cognition is wholly additive. No legacy or V1.4 table is rewritten.
    try:
        Base.metadata.create_all(engine, tables=list(V15_TABLES))
    except SQLAlchemyError:
        logger.exception("V1.5 additive cognition schema failed; V1.4 remains available")
    # V1.6 runtime, budget, circuit and metadata-only call audit are additive.
    try:
        Base.metadata.create_all(engine, tables=list(V16_TABLES))
    except SQLAlchemyError:
        logger.exception("V1.6 additive runtime schema failed; V1.5 remains available")
    after_sql = _table_sql(engine)
    setattr(engine, "_miniworld_upgrade_context", {"before_sql": before_sql, "after_sql": after_sql})
    return engine, sessionmaker(bind=engine, expire_on_commit=False)
