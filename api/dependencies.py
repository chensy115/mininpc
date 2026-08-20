from __future__ import annotations

from simulation.world import WorldService


_service: WorldService | None = None


def configure_world_service(service: WorldService) -> None:
    global _service
    _service = service


def get_world_service() -> WorldService:
    if _service is None:
        raise RuntimeError("World service is not configured")
    return _service

