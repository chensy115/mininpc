from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import time
import zipfile
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from database.models import (
    BalanceAudit,
    CareerDevelopment,
    DataTransferAudit,
    DecisionLog,
    NPC,
    OnboardingProgress,
    ProductState,
    UpgradeReport,
    WeeklyEconomicReport,
    WorldState,
    WorldStatistic,
)
from simulation.clock import ClockSnapshot


PRODUCT_VERSION = "1.0.0"
V10_TABLE_NAMES = {
    "product_state",
    "world_statistics",
    "balance_audits",
    "upgrade_reports",
    "onboarding_progress",
    "data_transfer_audits",
}
OLD_TABLE_NAMES = {
    "world_state", "npcs", "relationships", "events", "decisions", "memories",
    "long_term_goals", "narrative_jobs", "narrative_artifacts", "employment_profiles",
    "npc_skills", "stores", "item_definitions", "store_listings", "inventory_items",
    "housing", "economic_transactions", "career_development", "performance_reviews",
    "career_transitions", "personal_budgets", "weekly_economic_reports",
    "community_institutions", "work_schedules", "work_attendance", "store_stock",
    "restock_events", "facility_usage", "training_records", "housing_upgrade_records",
    "social_bonds", "social_invitations", "social_commitments", "friend_circles",
    "joint_activities", "cohousing_households", "shared_expenses", "social_audits",
    "social_profiles", "story_state", "life_milestones", "causal_links",
    "story_summaries", "replay_checkpoints",
}
STATISTICS_INTERVAL = 1440
BALANCE_INTERVAL = 1440
DECISION_WINDOW = 500
SLOT_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
EXPORT_PATTERN = re.compile(r"^[0-9a-f]{32}$")

PRESETS: dict[str, dict[str, Any]] = {
    "balanced": {
        "label": "均衡起步",
        "description": "与 V0.9 默认世界完全一致的安全起点。",
        "money_multiplier": 1.0,
        "ambition_delta": 0.0,
        "kindness_delta": 0.0,
        "social_need_delta": 0.0,
    },
    "career_focus": {
        "label": "职业成长",
        "description": "仅在新世界中提高初始进取与纪律倾向，不增加职业种类。",
        "money_multiplier": 1.1,
        "ambition_delta": 0.05,
        "discipline_delta": 0.05,
        "kindness_delta": 0.0,
        "social_need_delta": 0.0,
    },
    "community_focus": {
        "label": "社区联结",
        "description": "仅在新世界中提高初始社交需要与善意倾向，不增加地点。",
        "money_multiplier": 1.0,
        "ambition_delta": 0.0,
        "kindness_delta": 0.05,
        "social_need_delta": 8.0,
    },
}


class NewWorldConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    world_name: str = Field(default="我的 MiniWorld", min_length=1, max_length=60)
    preset: Literal["balanced", "career_focus", "community_focus"] = "balanced"
    seed: int = Field(default=42, ge=0, le=2_147_483_647)
    speed: Literal[1, 5, 20] = 1

    @field_validator("world_name")
    @classmethod
    def validate_world_name(cls, value: str) -> str:
        if any(ord(char) < 32 for char in value):
            raise ValueError("世界名称不能包含控制字符")
        return value


class CreateSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot_id: str
    config: NewWorldConfig = Field(default_factory=NewWorldConfig)

    @field_validator("slot_id")
    @classmethod
    def validate_slot(cls, value: str) -> str:
        if not SLOT_PATTERN.fullmatch(value) or value == "primary":
            raise ValueError("存档名须为小写字母开头的 1–32 位字母、数字、_ 或 -，且不能是 primary")
        return value


class ImportSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    export_id: str
    target_slot: str

    @field_validator("export_id")
    @classmethod
    def validate_export_id(cls, value: str) -> str:
        if not EXPORT_PATTERN.fullmatch(value):
            raise ValueError("export_id 格式无效")
        return value

    @field_validator("target_slot")
    @classmethod
    def validate_target(cls, value: str) -> str:
        if not SLOT_PATTERN.fullmatch(value) or value == "primary":
            raise ValueError("导入必须明确指定非 primary 的安全目标存档")
        return value


class OnboardingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    completed_steps: list[Literal["observe", "inspect_decision", "review_statistics", "manage_save"]]
    dismissed: bool = False


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def apply_preset_to_profiles(
    profiles: Iterable[dict[str, Any]], config: NewWorldConfig
) -> tuple[dict[str, Any], ...]:
    """Return new dictionaries; legacy profile constants are never modified."""
    preset = PRESETS[config.preset]
    configured: list[dict[str, Any]] = []
    for original in profiles:
        profile = dict(original)
        profile["money"] = round(float(profile["money"]) * preset["money_multiplier"], 2)
        profile["ambition"] = round(_clamp(float(profile["ambition"]) + preset["ambition_delta"], 0, 1), 2)
        profile["kindness"] = round(_clamp(float(profile["kindness"]) + preset["kindness_delta"], 0, 1), 2)
        if "discipline_delta" in preset:
            profile["discipline"] = round(_clamp(float(profile["discipline"]) + preset["discipline_delta"], 0, 1), 2)
        profile["social_need"] = round(_clamp(float(profile["social_need"]) + preset["social_need_delta"], 0, 100), 2)
        configured.append(profile)
    return tuple(configured)


def ensure_product_data(
    session: Session,
    state: WorldState,
    config: NewWorldConfig | None = None,
    upgrade_context: dict[str, Any] | None = None,
) -> dict[str, int]:
    created = {"product_state": 0, "upgrade_report": 0, "onboarding": 0}
    product = session.get(ProductState, 1)
    resolved = config or NewWorldConfig()
    if product is None:
        product = ProductState(
            id=1,
            schema_version=PRODUCT_VERSION,
            world_name=resolved.world_name,
            preset_key=resolved.preset,
            config_json=_canonical(resolved.model_dump()),
            initialized_minute=state.total_minutes,
            last_statistics_minute=state.total_minutes,
            last_balance_minute=state.total_minutes,
            updated_minute=state.total_minutes,
        )
        session.add(product)
        created["product_state"] = 1
    if session.get(OnboardingProgress, 1) is None:
        session.add(OnboardingProgress(id=1, completed_steps_json="[]", dismissed=False))
        created["onboarding"] = 1

    context = upgrade_context or {}
    before = context.get("before_sql", {})
    after = context.get("after_sql", {})
    preserved = sorted(name for name in before if before.get(name) == after.get(name))
    changed = sorted(name for name in before if name in after and before.get(name) != after.get(name))
    added = sorted(name for name in after if name not in before)
    key = f"v1.0:{_digest({'before': sorted(before), 'added': added})[:24]}"
    existing_v10_report = session.scalar(
        select(UpgradeReport.id).where(UpgradeReport.to_version == PRODUCT_VERSION).limit(1)
    )
    if existing_v10_report is None:
        session.add(
            UpgradeReport(
                report_key=key,
                from_version="0.9.0" if set(before) >= OLD_TABLE_NAMES else "new-or-legacy",
                to_version=PRODUCT_VERSION,
                world_minute=state.total_minutes,
                added_tables_json=_canonical(added),
                preserved_tables_json=_canonical(preserved),
                checks_json=_canonical({
                    "old_schema_changed": changed,
                    "old_schema_preserved": not bool(changed),
                    "v10_schema_complete": V10_TABLE_NAMES.issubset(set(after)),
                    "migration_kind": "additive",
                }),
            )
        )
        created["upgrade_report"] = 1
    return created


def compute_statistics(session: Session, state: WorldState, *, window: int = DECISION_WINDOW) -> dict[str, Any]:
    npcs = list(session.scalars(select(NPC).order_by(NPC.id)))
    npc_count = len(npcs)
    max_decision = session.scalar(select(func.max(DecisionLog.id))) or 0
    lower = max(0, max_decision - max(1, min(window, 5000)) + 1)
    recent = list(
        session.scalars(
            select(DecisionLog).where(DecisionLog.id >= lower).order_by(DecisionLog.id)
        )
    ) if max_decision else []
    actions = Counter(row.chosen_action for row in recent)
    decision_count = len(recent)
    top_action, top_count = actions.most_common(1)[0] if actions else (None, 0)
    entropy = 0.0
    if decision_count:
        entropy = -sum((count / decision_count) * math.log2(count / decision_count) for count in actions.values())
    career_rows = list(session.scalars(select(CareerDevelopment)))
    employed = sum(row.employment_status == "employed" for row in career_rows)
    reports = list(
        session.scalars(select(WeeklyEconomicReport).order_by(WeeklyEconomicReport.id.desc()).limit(max(npc_count, 1)))
    )
    money = [float(npc.money) for npc in npcs]
    needs = [
        value
        for npc in npcs
        for value in (float(npc.energy), float(npc.hunger), float(npc.mood), float(npc.social_need))
    ]
    metrics = {
        "npc_count": npc_count,
        "world_day": ClockSnapshot(state.total_minutes).day,
        "money": {
            "total": round(sum(money), 2),
            "average": round(sum(money) / npc_count, 2) if npc_count else 0.0,
            "minimum": round(min(money), 2) if money else 0.0,
            "maximum": round(max(money), 2) if money else 0.0,
        },
        "needs": {
            "minimum": round(min(needs), 2) if needs else 0.0,
            "maximum": round(max(needs), 2) if needs else 0.0,
        },
        "employment_rate": round(employed / len(career_rows), 4) if career_rows else None,
        "economic_pressure_average": round(
            sum(float(row.economic_pressure) for row in reports) / len(reports), 2
        ) if reports else None,
        "decisions": {
            "window": decision_count,
            "from_id": lower if recent else None,
            "to_id": max_decision if recent else None,
            "top_action": top_action,
            "top_action_share": round(top_count / decision_count, 4) if decision_count else 0.0,
            "entropy_bits": round(entropy, 4),
            "counts": dict(sorted(actions.items())),
        },
    }
    sources = {
        "as_of_world_minute": state.total_minutes,
        "tables": {
            "population_and_needs": "npcs",
            "employment": "career_development",
            "economic_pressure": "weekly_economic_reports (latest bounded rows)",
            "decision_mix": f"decisions primary-key window <= {min(window, 5000)}",
        },
        "method": "deterministic SQLAlchemy aggregation; no LLM and no random source",
    }
    return {"metrics": metrics, "sources": sources, "facts_digest": _digest({"metrics": metrics, "sources": sources})}


BALANCE_THRESHOLDS: dict[str, Any] = {
    "needs_range": [0.0, 100.0],
    "money_range": [-10_000.0, 1_000_000.0],
    "minimum_employment_rate": 0.4,
    "maximum_top_action_share": 0.85,
    "decision_share_minimum_sample": 20,
    "maximum_economic_pressure": 100.0,
}


def evaluate_balance(statistics: dict[str, Any]) -> dict[str, Any]:
    metrics = statistics["metrics"]
    violations: list[dict[str, Any]] = []
    needs = metrics["needs"]
    money = metrics["money"]
    if needs["minimum"] < 0 or needs["maximum"] > 100:
        violations.append({"metric": "needs", "severity": "critical", "reason": "NPC 状态越过 0–100 硬边界"})
    if money["minimum"] < BALANCE_THRESHOLDS["money_range"][0] or money["maximum"] > BALANCE_THRESHOLDS["money_range"][1]:
        violations.append({"metric": "money", "severity": "critical", "reason": "金钱越过产品数值硬边界"})
    employment = metrics["employment_rate"]
    if employment is not None and employment < BALANCE_THRESHOLDS["minimum_employment_rate"]:
        violations.append({"metric": "employment_rate", "severity": "warning", "reason": "就业率低于 40% 收敛守护线"})
    decision = metrics["decisions"]
    if decision["window"] >= BALANCE_THRESHOLDS["decision_share_minimum_sample"] and decision["top_action_share"] > BALANCE_THRESHOLDS["maximum_top_action_share"]:
        violations.append({"metric": "top_action_share", "severity": "warning", "reason": "单一行动占比超过 85%"})
    pressure = metrics["economic_pressure_average"]
    if pressure is not None and pressure > BALANCE_THRESHOLDS["maximum_economic_pressure"]:
        violations.append({"metric": "economic_pressure_average", "severity": "warning", "reason": "平均经济压力超过 100"})
    status = "critical" if any(item["severity"] == "critical" for item in violations) else "warning" if violations else "healthy"
    return {
        "status": status,
        "metrics": metrics,
        "thresholds": BALANCE_THRESHOLDS,
        "violations": violations,
        "policy": "observe-and-guard; V1.0 never rewrites V0.1–V0.9 facts to chase a target",
    }


def _write_snapshot(session: Session, state: WorldState) -> tuple[WorldStatistic, BalanceAudit]:
    statistics = compute_statistics(session, state)
    snapshot_key = f"day:{ClockSnapshot(state.total_minutes).day}:{state.total_minutes}"
    snapshot = session.scalar(select(WorldStatistic).where(WorldStatistic.snapshot_key == snapshot_key))
    if snapshot is None:
        snapshot = WorldStatistic(
            snapshot_key=snapshot_key,
            world_minute=state.total_minutes,
            metrics_json=_canonical(statistics["metrics"]),
            sources_json=_canonical(statistics["sources"]),
            facts_digest=statistics["facts_digest"],
        )
        session.add(snapshot)
    balance = evaluate_balance(statistics)
    audit = session.scalar(select(BalanceAudit).where(BalanceAudit.audit_key == snapshot_key))
    if audit is None:
        audit = BalanceAudit(
            audit_key=snapshot_key,
            world_minute=state.total_minutes,
            status=balance["status"],
            metrics_json=_canonical(balance["metrics"]),
            thresholds_json=_canonical(balance["thresholds"]),
            violations_json=_canonical(balance["violations"]),
            facts_digest=_digest(balance),
        )
        session.add(audit)
    return snapshot, audit


def process_product_cycles(session: Session, state: WorldState) -> dict[str, int]:
    product = session.get(ProductState, 1)
    if product is None:
        return {"statistics": 0, "balance": 0}
    created = {"statistics": 0, "balance": 0}
    if state.total_minutes - product.last_statistics_minute >= STATISTICS_INTERVAL:
        before_stats = session.scalar(select(func.count()).select_from(WorldStatistic)) or 0
        before_audits = session.scalar(select(func.count()).select_from(BalanceAudit)) or 0
        _write_snapshot(session, state)
        session.flush()
        created["statistics"] = int((session.scalar(select(func.count()).select_from(WorldStatistic)) or 0) > before_stats)
        created["balance"] = int((session.scalar(select(func.count()).select_from(BalanceAudit)) or 0) > before_audits)
        product.last_statistics_minute = state.total_minutes
        product.last_balance_minute = state.total_minutes
        product.updated_minute = state.total_minutes
    return created


def product_status(session: Session, enabled: bool) -> dict[str, Any]:
    product = session.get(ProductState, 1) if enabled else None
    if product is None:
        return {
            "enabled": False,
            "mode": "v0.9-compatible",
            "version": PRODUCT_VERSION,
            "statistics_snapshots": 0,
            "balance_audits": 0,
        }
    return {
        "enabled": True,
        "mode": "v1.0",
        "version": PRODUCT_VERSION,
        "world_name": product.world_name,
        "preset": product.preset_key,
        "config": json.loads(product.config_json),
        "statistics_snapshots": session.scalar(select(func.count()).select_from(WorldStatistic)) or 0,
        "balance_audits": session.scalar(select(func.count()).select_from(BalanceAudit)) or 0,
        "upgrade_reports": session.scalar(select(func.count()).select_from(UpgradeReport)) or 0,
        "data_transfers": session.scalar(select(func.count()).select_from(DataTransferAudit)) or 0,
    }


def latest_statistics(session: Session) -> dict[str, Any]:
    state = session.get(WorldState, 1)
    if state is None:
        raise RuntimeError("世界尚未初始化")
    result = compute_statistics(session, state)
    return {"enabled": True, "mode": "v1.0", "world_minute": state.total_minutes, **result}


def latest_balance(session: Session) -> dict[str, Any]:
    statistics = latest_statistics(session)
    return {"enabled": True, "mode": "v1.0", "world_minute": statistics["world_minute"], **evaluate_balance(statistics)}


def upgrade_reports(session: Session) -> list[dict[str, Any]]:
    rows = list(session.scalars(select(UpgradeReport).order_by(UpgradeReport.id.desc()).limit(20)))
    return [{
        "id": row.id,
        "from_version": row.from_version,
        "to_version": row.to_version,
        "world_minute": row.world_minute,
        "added_tables": json.loads(row.added_tables_json),
        "preserved_tables": json.loads(row.preserved_tables_json),
        "checks": json.loads(row.checks_json),
        "created_at": row.created_at.isoformat(),
    } for row in rows]


ONBOARDING_STEPS = [
    {"key": "observe", "title": "观察世界", "detail": "查看时间、地点与五位 NPC 的实时状态。"},
    {"key": "inspect_decision", "title": "理解一次决策", "detail": "打开 NPC 决策理由，确认 Utility AI 而非 LLM 选择行动。"},
    {"key": "review_statistics", "title": "检查统计与平衡", "detail": "查看来源、窗口、阈值与守护结果。"},
    {"key": "manage_save", "title": "保护存档", "detail": "创建隔离存档或导出包；导入永不覆盖 primary。"},
]


def onboarding_snapshot(session: Session) -> dict[str, Any]:
    row = session.get(OnboardingProgress, 1)
    completed = json.loads(row.completed_steps_json) if row else []
    return {
        "enabled": True,
        "mode": "v1.0",
        "completed_steps": completed,
        "dismissed": bool(row.dismissed) if row else False,
        "steps": [{**step, "completed": step["key"] in completed} for step in ONBOARDING_STEPS],
    }


def update_onboarding(session: Session, request: OnboardingRequest) -> dict[str, Any]:
    row = session.get(OnboardingProgress, 1)
    if row is None:
        row = OnboardingProgress(id=1)
        session.add(row)
    ordered = [step["key"] for step in ONBOARDING_STEPS if step["key"] in set(request.completed_steps)]
    row.completed_steps_json = _canonical(ordered)
    row.dismissed = request.dismissed
    row.updated_at = datetime.now(timezone.utc)
    session.commit()
    return onboarding_snapshot(session)


class SaveOwnershipError(RuntimeError):
    pass


class SaveOwnership:
    """Cross-process single-writer marker used only by V1.0 simulation loops."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path.resolve()
        self.lock_path = self.db_path.with_suffix(self.db_path.suffix + ".writer.lock")
        self.token: str | None = None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def claim(self) -> dict[str, Any]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(16)
        payload = {
            "pid": os.getpid(),
            "token": token,
            "db_path": str(self.db_path),
            "claimed_at": datetime.now(timezone.utc).isoformat(),
        }
        for _attempt in range(2):
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                self.token = token
                return payload
            except FileExistsError:
                try:
                    existing = json.loads(self.lock_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    raise SaveOwnershipError("存档写入锁存在且无法安全验证")
                if self._pid_alive(int(existing.get("pid", -1))):
                    raise SaveOwnershipError(f"存档已由 PID {existing.get('pid')} 的 Simulation Loop 占用")
                self.lock_path.unlink()
        raise SaveOwnershipError("无法取得存档写入所有权")

    def release(self) -> None:
        if self.token is None or not self.lock_path.exists():
            return
        try:
            existing = json.loads(self.lock_path.read_text(encoding="utf-8"))
            if existing.get("token") == self.token and int(existing.get("pid", -1)) == os.getpid():
                self.lock_path.unlink()
        finally:
            self.token = None


class SaveManager:
    """Path-constrained, atomic save creation and transfer manager."""

    def __init__(self, root: Path, active_db_path: Path) -> None:
        self.root = root.resolve()
        self.data_dir = (self.root / "data").resolve()
        self.saves_dir = (self.data_dir / "saves").resolve()
        self.exports_dir = (self.data_dir / "exports").resolve()
        self.active_db_path = active_db_path.resolve()
        self.saves_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)

    def slot_path(self, slot_id: str) -> Path:
        if slot_id == "primary":
            return (self.data_dir / "world.db").resolve()
        if not SLOT_PATTERN.fullmatch(slot_id):
            raise ValueError("存档名不符合安全规则")
        path = (self.saves_dir / f"{slot_id}.db").resolve()
        if path.parent != self.saves_dir:
            raise ValueError("存档路径越界")
        return path

    def active_slot(self) -> str:
        if self.active_db_path == self.slot_path("primary"):
            return "primary"
        for path in self.saves_dir.glob("*.db"):
            if path.resolve() == self.active_db_path:
                return path.stem
        return "external"

    @staticmethod
    def _db_summary(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {"exists": False}
        try:
            with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as connection:
                state = connection.execute("SELECT total_minutes, seed FROM world_state WHERE id=1").fetchone()
                product_row = connection.execute(
                    "SELECT world_name, preset_key FROM product_state WHERE id=1"
                ).fetchone() if connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='product_state'"
                ).fetchone() else None
                integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            return {
                "exists": True,
                "size_bytes": path.stat().st_size,
                "total_minutes": state[0] if state else None,
                "seed": state[1] if state else None,
                "world_name": product_row[0] if product_row else None,
                "preset": product_row[1] if product_row else None,
                "quick_check": integrity,
            }
        except sqlite3.Error as exc:
            return {"exists": True, "size_bytes": path.stat().st_size, "error": str(exc)}

    def list_slots(self) -> dict[str, Any]:
        active = self.active_slot()
        candidates = [("primary", self.slot_path("primary"))]
        candidates.extend((path.stem, path.resolve()) for path in sorted(self.saves_dir.glob("*.db")))
        slots = []
        for slot_id, path in candidates:
            summary = self._db_summary(path)
            if summary.get("exists"):
                slots.append({"slot_id": slot_id, "active": slot_id == active, **summary})
        return {
            "enabled": True,
            "mode": "v1.0",
            "active_slot": active,
            "slots": slots,
            "activation": "设置 MINIWORLD_SAVE_SLOT 后重启；运行中的写入存档不热切换。",
        }

    def create_slot(self, request: CreateSaveRequest) -> dict[str, Any]:
        target = self.slot_path(request.slot_id)
        if target.exists():
            raise FileExistsError("目标存档已存在，拒绝覆盖")
        fd, temporary_name = tempfile.mkstemp(prefix=f".{request.slot_id}-", suffix=".db", dir=self.saves_dir)
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            from database.database import create_database
            from simulation.world import WorldService

            engine, factory = create_database(temporary)
            try:
                service = WorldService(factory, world_config=request.config)
                service.initialize()
            finally:
                engine.dispose()
            os.replace(temporary, target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return {"slot_id": request.slot_id, "active": False, **self._db_summary(target)}

    def export_slot(self, slot_id: str) -> dict[str, Any]:
        source = self.slot_path(slot_id)
        if not source.exists():
            raise FileNotFoundError("存档不存在")
        export_id = secrets.token_hex(16)
        package = self.exports_dir / f"{export_id}.mworld"
        with tempfile.TemporaryDirectory(dir=self.exports_dir) as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            snapshot = temp_dir / "world.db"
            with closing(sqlite3.connect(source)) as source_db, closing(sqlite3.connect(snapshot)) as destination_db:
                source_db.backup(destination_db)
            validation = validate_database(snapshot)
            digest = _file_digest(snapshot)
            manifest = {
                "format": "miniworld-save",
                "format_version": 1,
                "product_version": PRODUCT_VERSION,
                "source_slot": slot_id,
                "database_sha256": digest,
                "database_size": snapshot.stat().st_size,
                "tables": validation["tables"],
                "exported_at": datetime.now(timezone.utc).isoformat(),
            }
            with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                archive.writestr("manifest.json", _canonical(manifest))
                archive.write(snapshot, "world.db")
        return {"export_id": export_id, "slot_id": slot_id, "size_bytes": package.stat().st_size, "manifest": manifest}

    def export_path(self, export_id: str) -> Path:
        if not EXPORT_PATTERN.fullmatch(export_id):
            raise ValueError("export_id 格式无效")
        path = (self.exports_dir / f"{export_id}.mworld").resolve()
        if path.parent != self.exports_dir or not path.exists():
            raise FileNotFoundError("导出包不存在")
        return path

    def import_export(self, request: ImportSaveRequest) -> dict[str, Any]:
        package = self.export_path(request.export_id)
        target = self.slot_path(request.target_slot)
        if target == self.active_db_path:
            raise ValueError("禁止导入到当前活动存档")
        if target.exists():
            raise FileExistsError("目标存档已存在，拒绝覆盖")
        fd, temporary_name = tempfile.mkstemp(prefix=f".{request.target_slot}-", suffix=".db", dir=self.saves_dir)
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            with zipfile.ZipFile(package) as archive:
                names = set(archive.namelist())
                if names != {"manifest.json", "world.db"}:
                    raise ValueError("导入包只能包含 manifest.json 与 world.db")
                info = archive.getinfo("world.db")
                if info.file_size > 2_000_000_000 or info.compress_size <= 0 or info.file_size / info.compress_size > 200:
                    raise ValueError("导入包大小或压缩比超过安全限制")
                manifest = json.loads(archive.read("manifest.json"))
                if manifest.get("format") != "miniworld-save" or manifest.get("format_version") != 1:
                    raise ValueError("导入包格式不受支持")
                with archive.open("world.db") as source, temporary.open("wb") as destination:
                    shutil.copyfileobj(source, destination, length=1024 * 1024)
            if _file_digest(temporary) != manifest.get("database_sha256"):
                raise ValueError("导入包数据库摘要不匹配")
            validation = validate_database(temporary)
            if not validation["valid"]:
                raise ValueError(f"导入数据库校验失败: {validation}")
            # Upgrade is performed on the isolated temporary file, never on primary.
            from database.database import create_database
            engine, factory = create_database(temporary)
            try:
                with factory() as session:
                    session.add(DataTransferAudit(
                        operation="import",
                        transfer_id=_digest({"export_id": request.export_id, "target": request.target_slot}),
                        target_slot=request.target_slot,
                        status="validated",
                        manifest_json=_canonical(manifest),
                        error_text=None,
                    ))
                    session.commit()
            finally:
                engine.dispose()
            validate_database(temporary, require_v10=True)
            os.replace(temporary, target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return {"slot_id": request.target_slot, "active": False, "imported_from": request.export_id, **self._db_summary(target)}


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_database(path: Path, *, require_v10: bool = False) -> dict[str, Any]:
    with closing(sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        tables = sorted(row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ))
        required = {"world_state", "npcs"} | (V10_TABLE_NAMES if require_v10 else set())
        missing = sorted(required - set(tables))
    result = {
        "valid": integrity == "ok" and not foreign_keys and not missing,
        "integrity_check": integrity,
        "foreign_key_errors": len(foreign_keys),
        "tables": tables,
        "missing_required_tables": missing,
    }
    if not result["valid"]:
        raise ValueError(f"数据库校验失败: {result}")
    return result


def resolve_active_database(project_root: Path) -> tuple[str, Path]:
    explicit = os.getenv("MINIWORLD_DB_PATH")
    if explicit:
        return "external", Path(explicit).resolve()
    slot = os.getenv("MINIWORLD_SAVE_SLOT", "primary").strip()
    if slot == "primary":
        return slot, (project_root / "data" / "world.db").resolve()
    if not SLOT_PATTERN.fullmatch(slot):
        raise RuntimeError("MINIWORLD_SAVE_SLOT 不符合安全存档名规则")
    path = (project_root / "data" / "saves" / f"{slot}.db").resolve()
    if not path.exists():
        raise RuntimeError("指定存档不存在；请先通过 V1.0 存档 API 创建，拒绝隐式空存档")
    return slot, path


def performance_evidence(service: Any, ticks: int, elapsed_seconds: float, commit_interval: int) -> dict[str, Any]:
    rate = ticks / elapsed_seconds if elapsed_seconds > 0 else 0.0
    return {
        "ticks": ticks,
        "simulated_minutes": ticks * 10,
        "elapsed_seconds": round(elapsed_seconds, 6),
        "ticks_per_second": round(rate, 3),
        "commit_interval": commit_interval,
        "optimization": "same full Engine tick path with bounded commits; no fact deletion or rule skipping",
    }
