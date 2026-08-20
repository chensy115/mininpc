from __future__ import annotations

from database.models import NPC
from simulation.clock import ClockSnapshot
from simulation.decision import decide
from simulation.random_service import RandomService


def make_npc(**overrides) -> NPC:
    values = dict(
        id=1, name="Test", age=30, job="Developer", current_location="Home",
        current_action="Idle", action_end_minute=480, money=100,
        energy=70, hunger=30, mood=60, social_need=30, work_satisfaction=70,
        extroversion=.5, kindness=.5, ambition=.7, risk_tolerance=.4, discipline=.8,
    )
    values.update(overrides)
    return NPC(**values)


def candidate(decision, action):
    return next(item for item in decision.candidates if item.action == action)


def test_high_hunger_significantly_increases_eat_score():
    hungry = make_npc(hunger=90)
    full = make_npc(hunger=10)
    occupants = {"Home": [hungry], "Office": [], "Cafe": [], "Park": []}
    hungry_score = candidate(decide(hungry, ClockSnapshot(12 * 60), occupants, RandomService(42)), "Eat").raw_score
    occupants["Home"] = [full]
    full_score = candidate(decide(full, ClockSnapshot(12 * 60), occupants, RandomService(42)), "Eat").raw_score
    assert hungry_score - full_score >= 100


def test_low_energy_increases_sleep_score():
    tired = make_npc(energy=10)
    rested = make_npc(energy=90)
    occupants = {"Home": [tired], "Office": [], "Cafe": [], "Park": []}
    tired_score = candidate(decide(tired, ClockSnapshot(23 * 60), occupants, RandomService(42)), "Sleep").raw_score
    occupants["Home"] = [rested]
    rested_score = candidate(decide(rested, ClockSnapshot(23 * 60), occupants, RandomService(42)), "Sleep").raw_score
    assert tired_score > rested_score


def test_working_hours_raise_work_score():
    npc = make_npc(current_location="Office")
    occupants = {"Home": [], "Office": [npc], "Cafe": [], "Park": []}
    daytime = candidate(decide(npc, ClockSnapshot(10 * 60), occupants, RandomService(42)), "Work").raw_score
    nighttime = candidate(decide(npc, ClockSnapshot(21 * 60), occupants, RandomService(42)), "Work").raw_score
    assert daytime - nighttime == 65


def test_socialize_unavailable_without_another_npc():
    npc = make_npc(current_location="Cafe", social_need=100, extroversion=1)
    occupants = {"Home": [], "Office": [], "Cafe": [npc], "Park": []}
    social = candidate(decide(npc, ClockSnapshot(12 * 60), occupants, RandomService(42)), "Socialize")
    assert social.available is False

