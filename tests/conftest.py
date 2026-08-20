from __future__ import annotations

import pytest

from database.database import create_database
from simulation.world import WorldService


@pytest.fixture
def world_service(tmp_path):
    engine, session_factory = create_database(tmp_path / "test-world.db")
    service = WorldService(session_factory)
    service.initialize()
    yield service
    engine.dispose()

