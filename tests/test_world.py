from __future__ import annotations

import asyncio

from database.models import WorldState
from simulation.clock import ClockSnapshot, WorldClock
from simulation.world import WorldService


def test_clock_crosses_day_and_week():
    clock = WorldClock(total_minutes=6 * 1440 + 23 * 60 + 50)
    snapshot = clock.advance(20)
    assert snapshot.day == 8
    assert snapshot.weekday == "星期一"
    assert snapshot.time_text == "00:10"


def test_world_tick_creates_decisions_and_persists(world_service):
    asyncio.run(world_service.tick())
    snapshot = asyncio.run(world_service.world_snapshot())
    assert snapshot["time"] == "08:10"
    assert sum(len(items) for items in snapshot["locations"].values()) == 5
    assert asyncio.run(world_service.latest_decision(1)) is not None

    restored = WorldService(world_service.session_factory)
    restored.initialize()
    restored_snapshot = asyncio.run(restored.world_snapshot())
    assert restored_snapshot["total_minutes"] == snapshot["total_minutes"]


def test_pause_speed_and_reset(world_service):
    paused = asyncio.run(world_service.set_paused(True))
    assert paused["paused"] is True
    assert asyncio.run(world_service.tick()) is False
    sped_up = asyncio.run(world_service.set_speed(20))
    assert sped_up["speed"] == 20
    reset = asyncio.run(world_service.reset())
    assert reset["label"] == "第 1 天 · 星期一 · 08:00"
    assert reset["speed"] == 1

    with world_service.session_factory() as session:
        assert session.get(WorldState, 1) is not None
