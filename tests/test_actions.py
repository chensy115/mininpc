from __future__ import annotations

from sqlalchemy import select

from database.models import Event, NPC
from simulation.actions import complete_action
from simulation.clock import ClockSnapshot
from simulation.random_service import RandomService


def complete(world_service, action: str):
    with world_service.session_factory() as session:
        npc = session.get(NPC, 1)
        npc.current_action = action
        npc.current_location = {"Sleep": "Home", "Eat": "Cafe", "Work": "Office"}.get(action, npc.current_location)
        before = {"energy": npc.energy, "hunger": npc.hunger, "money": npc.money}
        complete_action(session, npc, ClockSnapshot(600), RandomService(42))
        session.commit()
        event = session.scalar(select(Event).where(Event.npc_id == npc.id).order_by(Event.id.desc()))
        return before, {"energy": npc.energy, "hunger": npc.hunger, "money": npc.money}, event


def test_eat_reduces_hunger_and_money(world_service):
    before, after, event = complete(world_service, "Eat")
    assert after["hunger"] < before["hunger"]
    assert after["money"] < before["money"]
    assert event.event_type == "EAT"


def test_sleep_increases_energy(world_service):
    before, after, event = complete(world_service, "Sleep")
    assert after["energy"] > before["energy"]
    assert event.event_type == "SLEEP"


def test_work_increases_money_and_reduces_energy(world_service):
    before, after, event = complete(world_service, "Work")
    assert after["money"] > before["money"]
    assert after["energy"] < before["energy"]
    assert event.event_type == "WORK"

