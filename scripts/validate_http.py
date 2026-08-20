from __future__ import annotations

import argparse
import json
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


WORLD_KEYS = {
    "day", "weekday", "time", "label", "total_minutes", "paused", "speed", "locations",
}
NPC_KEYS = {
    "id", "name", "age", "job", "current_location", "current_action",
    "action_end_minute", "money", "states", "personality", "relationships",
}
NPC_IDS = {1, 2, 3, 4, 5}
CONTROL_KEYS = {
    "supported", "npc_id", "status", "enabled", "turn", "queue", "plan",
    "emotion", "final", "fallback", "recent_audits",
}


def fetch(
    base_url: str,
    path: str,
    *,
    parse_json: bool = True,
    method: str = "GET",
    body: Any = None,
):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        response_body = response.read()
        if response.status != 200:
            raise RuntimeError(f"{path} returned HTTP {response.status}")
        return (json.loads(response_body) if parse_json else response_body), response.status


def put_enabled(base_url: str, path: str, enabled: bool) -> dict[str, Any]:
    payload, _ = fetch(base_url, path, method="PUT", body={"enabled": enabled})
    return payload


def control_shape_ok(control: dict[str, Any], npc_id: int) -> bool:
    queue = control.get("queue")
    audits = control.get("recent_audits")
    fallback = control.get("fallback")
    final = control.get("final")
    fallback_matches_final = (
        final is None
        or fallback == final.get("fallback_reason_code")
        or (fallback is None and final.get("fallback_reason_code") is None)
    )
    return (
        CONTROL_KEYS <= set(control)
        and control.get("supported") is True
        and control.get("npc_id") == npc_id
        and isinstance(queue, dict)
        and {"pending", "processing", "depth", "limit"} <= set(queue)
        and 0 <= int(queue["depth"]) <= int(queue["limit"])
        and isinstance(control.get("plan"), list)
        and isinstance(audits, list)
        and (fallback is None or isinstance(fallback, str))
        and fallback_matches_final
    )


def provider_is_test_safe(provider: dict[str, Any]) -> bool:
    if not provider.get("available"):
        return True
    name = str(provider.get("provider") or "").lower()
    return any(marker in name for marker in ("fake", "mock", "deterministic", "test"))


def main() -> None:
    parser = argparse.ArgumentParser(description="MiniWorld V1.6 HTTP acceptance validator")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--output", type=Path, default=Path("logs/v16-http-validation.json"))
    parser.add_argument("--db-path", type=Path)
    args = parser.parse_args()

    checks: dict[str, bool] = {}
    details: dict[str, object] = {}
    for path in ("/", "/docs"):
        body, status = fetch(args.base_url, path, parse_json=False)
        checks[f"{path}_http_200"] = status == 200 and bool(body)

    openapi, _ = fetch(args.base_url, "/openapi.json")
    details["openapi_version"] = openapi["info"]["version"]
    details["openapi_paths"] = len(openapi["paths"])
    checks["openapi_v16"] = openapi["info"]["version"] == "1.6.0"
    checks["v14_paths_present"] = {
        "/api/agents/takeover", "/api/agents/{npc_id}/control",
        "/api/agent-conversations/status", "/api/agent-conversations/check",
        "/api/conversations", "/api/conversations/{conversation_id}",
        "/api/conversations/{conversation_id}/cancel",
        "/api/npcs/{npc_id}/conversations",
    } <= set(openapi["paths"])
    checks["v15_paths_present"] = {
        "/api/agent-cognition/status", "/api/agent-cognition/check",
        "/api/agents/{npc_id}/cognition", "/api/npcs/{npc_id}/reflections",
        "/api/npcs/{npc_id}/beliefs", "/api/npcs/{npc_id}/plans",
        "/api/reflection-tasks/{task_id}/cancel",
    } <= set(openapi["paths"])
    checks["v16_paths_present"] = {
        "/api/runtime", "/api/runtime/health", "/api/runtime/consistency",
        "/api/runtime/start", "/api/runtime/pause", "/api/runtime/resume",
        "/api/runtime/stop", "/api/runtime/emergency-stop",
        "/api/runtime/npcs/{npc_id}", "/api/runtime/budget",
        "/api/runtime/budget/reset",
    } <= set(openapi["paths"])
    checks["dashboard_snapshot_paths_present"] = {
        "/api/dashboard/snapshot",
        "/api/dashboard/npcs/{npc_id}/snapshot",
    } <= set(openapi["paths"])
    checks["openapi_87_paths"] = len(openapi["paths"]) == 87

    runtime, _ = fetch(args.base_url, "/api/runtime")
    runtime_health, _ = fetch(args.base_url, "/api/runtime/health")
    runtime_consistency, _ = fetch(args.base_url, "/api/runtime/consistency")
    checks["v16_default_safe_zero_call"] = (
        runtime.get("mode") == "safe"
        and runtime.get("configured") is False
        and runtime.get("enabled_npc_ids") == []
        and runtime.get("recent_calls") == []
    )
    checks["v16_secret_redaction"] = (
        runtime.get("provider", {}).get("key") == {"configured": False}
        and "api_key" not in json.dumps(runtime).lower()
        and "bearer" not in json.dumps(runtime).lower()
    )
    checks["v16_health_world_fallback"] = (
        runtime_health.get("ok") is True
        and runtime_health.get("world_continues_on_provider_failure") is True
    )
    checks["v16_consistency"] = (
        runtime_consistency.get("ok") is True
        and runtime_consistency.get("raw_prompt_or_response_columns") is False
        and runtime_consistency.get("secret_columns") is False
    )
    checks["v16_authority_boundary"] = runtime.get("authority") == "simulation_engine_only"
    details["runtime"] = runtime

    dashboard, _ = fetch(
        args.base_url,
        "/api/dashboard/snapshot?groups=runtime,world,npcs,pulse",
    )
    dashboard_modules = dashboard.get("modules", {})
    world_scoped = [dashboard_modules.get(name, {}) for name in ("world", "npcs", "pulse")]
    checks["dashboard_snapshot_envelope"] = (
        dashboard.get("schema_version") == "1.0"
        and isinstance(dashboard.get("snapshot_id"), str)
        and isinstance(dashboard.get("captured_at"), str)
        and isinstance(dashboard.get("world_minute"), int)
        and set(dashboard_modules) == {"runtime", "world", "npcs", "pulse"}
    )
    checks["dashboard_world_boundary"] = all(
        module.get("status") == "ok"
        and module.get("snapshot_id") == dashboard.get("snapshot_id")
        and module.get("world_minute") == dashboard.get("world_minute")
        for module in world_scoped
    )
    dashboard_runtime = dashboard_modules.get("runtime", {})
    checks["dashboard_runtime_boundary"] = (
        dashboard_runtime.get("status") == "ok"
        and isinstance(dashboard_runtime.get("generation"), int)
        and isinstance(dashboard_runtime.get("observed_at"), str)
    )
    serialized_dashboard = json.dumps(dashboard).lower()
    checks["dashboard_privacy_boundary"] = all(
        marker not in serialized_dashboard
        for marker in ('"api_key"', '"prompt"', '"response"', '"chain_of_thought"')
    )

    npc_dashboard, _ = fetch(
        args.base_url,
        "/api/dashboard/npcs/1/snapshot?sections=overview,decision",
    )
    npc_modules = npc_dashboard.get("modules", {})
    checks["dashboard_npc_snapshot"] = (
        npc_dashboard.get("schema_version") == "1.0"
        and set(npc_modules) == {"overview", "decision"}
        and all(
            module.get("status") == "ok"
            and module.get("snapshot_id") == npc_dashboard.get("snapshot_id")
            and module.get("world_minute") == npc_dashboard.get("world_minute")
            for module in npc_modules.values()
        )
    )
    details["dashboard_snapshot"] = {
        "snapshot_id": dashboard.get("snapshot_id"),
        "world_minute": dashboard.get("world_minute"),
        "module_statuses": {
            name: module.get("status") for name, module in dashboard_modules.items()
        },
        "npc_snapshot_id": npc_dashboard.get("snapshot_id"),
    }

    world, _ = fetch(args.base_url, "/api/world")
    npc, _ = fetch(args.base_url, "/api/npcs/1")
    checks["legacy_world_exact_shape"] = set(world) == WORLD_KEYS
    checks["legacy_npc_exact_shape"] = set(npc) == NPC_KEYS
    details["world"] = {
        key: world[key] for key in ("day", "time", "total_minutes", "paused", "speed")
    }
    details["npc_1"] = {key: npc[key] for key in ("id", "name", "age", "job")}

    agent_status, _ = fetch(args.base_url, "/api/agent/status")
    agent_shadow, _ = fetch(args.base_url, "/api/npcs/1/agent-shadow")
    provider = agent_status.get("provider", {})
    safe_to_enable = provider_is_test_safe(provider)
    checks["agent_default_off"] = (
        agent_status.get("enabled") is False
        and agent_status.get("mode") == "disabled"
    )
    checks["legacy_agent_status_exact_shape"] = set(agent_status) == {
        "enabled", "mode", "target_npc_id", "target_npc_name",
        "provider", "jobs", "authority",
    }
    checks["agent_advisory_only"] = agent_status.get("authority") == "advisory_only"
    checks["alice_shadow_endpoint"] = (
        agent_shadow.get("supported") is True and agent_shadow.get("status") == "disabled"
    )
    checks["provider_safe_for_offline_acceptance"] = safe_to_enable
    details["agent"] = {
        "mode": agent_status.get("mode"),
        "provider_available": provider.get("available"),
        "provider": provider.get("provider"),
        "provider_reason": provider.get("reason"),
        "authority": agent_status.get("authority"),
    }

    legacy_takeover, _ = fetch(args.base_url, "/api/agent/takeover")
    legacy_alice, _ = fetch(args.base_url, "/api/npcs/1/agent-control")
    legacy_bob, _ = fetch(args.base_url, "/api/npcs/2/agent-control")
    checks["legacy_takeover_default_off"] = legacy_takeover.get("enabled") is False
    checks["legacy_alice_control_supported"] = legacy_alice.get("supported") is True
    checks["legacy_bob_control_stays_unsupported"] = (
        legacy_bob == {"supported": False, "status": "unsupported", "npc_id": 2}
    )

    overview, _ = fetch(args.base_url, "/api/agents/takeover")
    controls = overview.get("npcs", [])
    controls_by_id = {item.get("npc_id"): item for item in controls}
    checks["five_agent_overview_default_off"] = (
        overview.get("enabled") is False
        and overview.get("global_enabled") is False
        and overview.get("enabled_npc_ids") == []
        and set(controls_by_id) == NPC_IDS
    )
    checks["five_control_shapes"] = all(
        control_shape_ok(controls_by_id.get(npc_id, {}), npc_id) for npc_id in NPC_IDS
    )
    worker = overview.get("worker", {})
    checks["bounded_worker_visible"] = (
        {"max_concurrency", "queue_depth", "queue_limit", "bounded", "fairness"} <= set(worker)
        and worker.get("bounded") is True
        and int(worker.get("queue_depth", -1)) <= int(worker.get("queue_limit", -2))
    )

    audit_counts: dict[str, int] = {}
    for npc_id in sorted(NPC_IDS):
        control, _ = fetch(args.base_url, f"/api/agents/{npc_id}/control")
        audits, _ = fetch(args.base_url, f"/api/npcs/{npc_id}/agent-audits?limit=3")
        checks[f"npc_{npc_id}_control"] = control_shape_ok(control, npc_id)
        checks[f"npc_{npc_id}_audits"] = (
            isinstance(audits, list)
            and all(item.get("npc_id") == npc_id for item in audits)
        )
        checks[f"npc_{npc_id}_fallback_field"] = (
            "fallback" in control
            and (control["fallback"] is None or isinstance(control["fallback"], str))
        )
        audit_counts[str(npc_id)] = len(audits)
    details["audit_counts"] = audit_counts

    conversation_status, _ = fetch(args.base_url, "/api/agent-conversations/status")
    conversation_check, _ = fetch(args.base_url, "/api/agent-conversations/check")
    conversations, _ = fetch(args.base_url, "/api/conversations")
    npc_conversations, _ = fetch(args.base_url, "/api/npcs/1/conversations")
    checks["v14_default_off"] = (
        conversation_status.get("enabled") is False
        and conversation_status.get("mode") == "v1.3_legacy_dialogue"
    )
    checks["v14_bounds_visible"] = (
        conversation_status.get("bounds", {}).get("turns") == {"minimum": 3, "maximum": 6}
        and conversation_status.get("bounds", {}).get("bounded") is True
        and conversation_status.get("authority", {}).get("facts") == "simulation_engine_only"
        and conversation_status.get("authority", {}).get("hidden_reasoning_requested") is False
    )
    checks["v14_read_only_check"] = (
        conversation_check.get("ok") is True
        and conversation_check.get("private_context_exposed_by_api") is False
        and conversation_check.get("model_fact_authority") is False
    )
    checks["v14_empty_lists"] = conversations == [] and npc_conversations == []
    enabled_conversations = put_enabled(args.base_url, "/api/agent-conversations/status", True)
    disabled_conversations = put_enabled(args.base_url, "/api/agent-conversations/status", False)
    checks["v14_safe_switch"] = (
        enabled_conversations.get("enabled") is True
        and disabled_conversations.get("enabled") is False
    )
    details["conversation"] = {
        "provider": conversation_status.get("provider"),
        "bounds": conversation_status.get("bounds"),
        "counts": conversation_status.get("counts"),
    }

    cognition_status, _ = fetch(args.base_url, "/api/agent-cognition/status")
    cognition_check, _ = fetch(args.base_url, "/api/agent-cognition/check")
    cognition, _ = fetch(args.base_url, "/api/agents/1/cognition")
    reflections, _ = fetch(args.base_url, "/api/npcs/1/reflections")
    beliefs, _ = fetch(args.base_url, "/api/npcs/1/beliefs")
    plans, _ = fetch(args.base_url, "/api/npcs/1/plans")
    cognition_provider = cognition_status.get("provider", {})
    checks["v15_default_off"] = (
        cognition_status.get("enabled") is False
        and cognition_status.get("global_enabled") is False
        and cognition_status.get("enabled_npc_ids") == []
        and cognition_status.get("version") == "1.5.0"
    )
    checks["v15_bounds_visible"] = (
        cognition_status.get("bounds", {}).get("bounded") is True
        and cognition_status.get("bounds", {}).get("daily_reflections_per_npc") == 2
        and cognition_status.get("bounds", {}).get("plan_steps_per_reflection") == {
            "minimum": 1, "maximum": 3,
        }
    )
    checks["v15_authority_boundary"] = (
        cognition_status.get("authority", {}).get("facts") == "simulation_engine_only"
        and cognition_status.get("authority", {}).get("beliefs") == "subjective_only"
        and cognition_status.get("authority", {}).get("plans") == "non_executable_engine_monitored"
        and cognition_status.get("authority", {}).get("hidden_reasoning_requested") is False
    )
    checks["v15_read_only_check"] = (
        cognition_check.get("ok") is True
        and cognition_check.get("model_fact_authority") is False
        and cognition_check.get("hidden_reasoning_requested") is False
    )
    checks["v15_isolated_empty_state"] = (
        cognition.get("npc_id") == 1
        and cognition.get("enabled") is False
        and reflections == [] and beliefs == [] and plans == []
    )
    cognition_provider_safe = provider_is_test_safe(cognition_provider)
    checks["v15_provider_safe_for_offline_acceptance"] = cognition_provider_safe
    if cognition_provider_safe:
        try:
            npc_enabled = put_enabled(args.base_url, "/api/agents/2/cognition", True)
            subset, _ = fetch(args.base_url, "/api/agent-cognition/status")
            checks["v15_per_npc_switch"] = (
                npc_enabled.get("enabled") is True
                and subset.get("enabled_npc_ids") == [2]
            )
            put_enabled(args.base_url, "/api/agents/2/cognition", False)
            all_enabled = put_enabled(args.base_url, "/api/agent-cognition/status", True)
            all_disabled = put_enabled(args.base_url, "/api/agent-cognition/status", False)
            checks["v15_global_switch_restored_off"] = (
                all_enabled.get("global_enabled") is True
                and set(all_enabled.get("enabled_npc_ids", [])) == NPC_IDS
                and all_disabled.get("global_enabled") is False
                and all_disabled.get("enabled_npc_ids") == []
            )
        finally:
            put_enabled(args.base_url, "/api/agent-cognition/status", False)
    details["cognition"] = {
        "provider": cognition_provider,
        "bounds": cognition_status.get("bounds"),
        "counts": cognition_status.get("counts"),
        "authority": cognition_status.get("authority"),
        "check": cognition_check,
    }

    # Only no-key or explicit fake/mock providers may be enabled by this validator.
    # This guarantees the acceptance script cannot spend money or call a real model.
    if safe_to_enable:
        try:
            bob_enabled = put_enabled(args.base_url, "/api/agents/2/control", True)
            subset, _ = fetch(args.base_url, "/api/agents/takeover")
            old_alice, _ = fetch(args.base_url, "/api/agent/takeover")
            checks["per_npc_switch"] = (
                bob_enabled.get("enabled") is True
                and subset.get("enabled_npc_ids") == [2]
                and old_alice.get("enabled") is False
            )
            put_enabled(args.base_url, "/api/agents/2/control", False)

            all_enabled = put_enabled(args.base_url, "/api/agents/takeover", True)
            checks["global_switch_all_five"] = (
                all_enabled.get("global_enabled") is True
                and set(all_enabled.get("enabled_npc_ids", [])) == NPC_IDS
                and all(item.get("enabled") is True for item in all_enabled.get("npcs", []))
            )
            all_disabled = put_enabled(args.base_url, "/api/agents/takeover", False)
            checks["global_switch_restored_off"] = (
                all_disabled.get("global_enabled") is False
                and all_disabled.get("enabled_npc_ids") == []
            )

            old_enabled = put_enabled(args.base_url, "/api/agent/takeover", True)
            legacy_scope, _ = fetch(args.base_url, "/api/agents/takeover")
            checks["legacy_alice_switch_scope"] = (
                old_enabled.get("enabled") is True
                and legacy_scope.get("enabled_npc_ids") == [1]
            )
            old_disabled = put_enabled(args.base_url, "/api/agent/takeover", False)
            checks["legacy_switch_restored_off"] = old_disabled.get("enabled") is False
        finally:
            # Best-effort safety cleanup on an isolated acceptance server/database.
            put_enabled(args.base_url, "/api/agents/takeover", False)

    modes = {
        "/api/economy": "v0.5",
        "/api/career-budget": "v0.6",
        "/api/community-rhythm": "v0.7",
        "/api/social-life": "v0.8",
        "/api/life-story": "v0.9",
        "/api/product": "v1.0",
    }
    details["modes"] = {}
    for path, expected in modes.items():
        payload, _ = fetch(args.base_url, path)
        actual = payload.get("mode")
        details["modes"][path] = actual
        checks[f"mode_{expected}"] = actual == expected

    presets, _ = fetch(args.base_url, "/api/world-presets")
    statistics, _ = fetch(args.base_url, "/api/world-statistics")
    balance, _ = fetch(args.base_url, "/api/balance")
    upgrades, _ = fetch(args.base_url, "/api/upgrade-reports")
    onboarding, _ = fetch(args.base_url, "/api/onboarding")
    saves, _ = fetch(args.base_url, "/api/saves")
    checks.update(
        {
            "three_finite_presets": len(presets) == 3,
            "statistics_traceable": bool(statistics.get("sources", {}).get("tables")),
            "balance_guard_present": balance.get("status") in {"healthy", "warning", "critical"},
            "upgrade_report_passed": bool(upgrades) and all(
                report.get("checks", {}).get("migration_kind") == "additive"
                and report.get("checks", {}).get("old_schema_preserved") is True
                and report.get("checks", {}).get("v10_schema_complete") is True
                and not report.get("checks", {}).get("old_schema_changed")
                for report in upgrades
            ),
            "onboarding_available": isinstance(onboarding.get("steps"), list),
            "save_context_valid": saves.get("active_slot") in {"primary", "external"},
        }
    )
    details["balance_status"] = balance.get("status")
    details["upgrade_reports"] = len(upgrades)
    details["active_slot"] = saves.get("active_slot")

    if args.db_path is not None:
        db_path = args.db_path.resolve()
        connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        try:
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            v15_tables = {
                "agent_cognition_states", "agent_reflection_tasks",
                "agent_reflection_sources", "agent_reflections",
                "agent_subjective_beliefs", "agent_plans",
            }
            v16_tables = {
                "model_runtime_state", "model_budget_config", "model_circuit_states",
                "model_call_audits", "model_runtime_audits",
            }
            active_queues = {
                "agent_decisions": connection.execute(
                    "SELECT COUNT(*) FROM agent_decision_jobs "
                    "WHERE status IN ('pending', 'processing')"
                ).fetchone()[0],
                "reflections": connection.execute(
                    "SELECT COUNT(*) FROM agent_reflection_tasks "
                    "WHERE status IN ('pending', 'processing')"
                ).fetchone()[0],
                "conversations": connection.execute(
                    "SELECT COUNT(*) FROM agent_conversation_tasks "
                    "WHERE status IN ('pending', 'processing')"
                ).fetchone()[0],
            }
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_key_errors = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        finally:
            connection.close()
        checks["database_69_tables"] = len(tables) == 69
        checks["database_v15_tables_complete"] = v15_tables <= tables
        checks["database_v16_tables_complete"] = v16_tables <= tables
        checks["database_integrity"] = integrity == "ok"
        checks["database_foreign_keys"] = foreign_key_errors == 0
        checks["database_queues_drained"] = all(value == 0 for value in active_queues.values())
        details["database"] = {
            "path": str(db_path), "tables": len(tables),
            "v15_tables": sorted(v15_tables & tables),
            "v16_tables": sorted(v16_tables & tables),
            "integrity_check": integrity, "foreign_key_errors": foreign_key_errors,
            "active_queues": active_queues,
        }

    report = {
        "version": "1.6.0",
        "base_url": args.base_url,
        "checks": checks,
        "details": details,
        "passed": all(checks.values()),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except urllib.error.URLError as exc:
        raise SystemExit(f"HTTP validation failed: {exc}") from exc
