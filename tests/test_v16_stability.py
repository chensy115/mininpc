from __future__ import annotations

import asyncio

from scripts.v16_stability import run_stability


def test_v16_five_agent_thirty_day_supervisor_budget_fault_stability(tmp_path):
    result = asyncio.run(asyncio.wait_for(
        run_stability(tmp_path / "v16-thirty-days.db", days=30),
        timeout=2400,
    ))
    assert result["simulation"]["formal_ticks"] == 4320
    assert result["simulation"]["daily_reflections"] == 150
    assert result["runtime"]["faults_injected"] == {"429": 1, "timeout": 1, "5xx": 1}
    assert result["runtime"]["cold_restarts"] == 1
    assert result["runtime"]["emergency_stops"] == 1
    assert result["queues"]["final"] == {"agent_decisions": 0, "reflections": 0, "conversations": 0}
