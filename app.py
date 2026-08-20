from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.dependencies import configure_world_service
from api.agent import router as agent_router
from api.dashboard import router as dashboard_router
from api.runtime import router as runtime_router
from api.npc import router as npc_router
from api.world import router as world_router
from database.database import create_database
from simulation.world import WorldService
from simulation.runtime_v16 import RuntimeSupervisor
from simulation.productization import (
    SaveManager,
    SaveOwnership,
    resolve_active_database,
)


ROOT = Path(__file__).resolve().parent
(ROOT / "logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler(ROOT / "logs" / "app.log", encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

ACTIVE_SLOT, ACTIVE_DB_PATH = resolve_active_database(ROOT)
engine, SessionLocal = create_database(ACTIVE_DB_PATH)
save_manager = SaveManager(ROOT, ACTIVE_DB_PATH)
world_service = WorldService(SessionLocal, save_manager=save_manager)
world_service.initialize()
configure_world_service(world_service)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    loops_enabled = os.getenv("MINIWORLD_BACKGROUND_LOOPS_ENABLED", "true").strip().lower() not in {
        "0", "false", "no", "off",
    }
    supervisor = RuntimeSupervisor(world_service)
    ownership = SaveOwnership(ACTIVE_DB_PATH)
    if loops_enabled:
        ownership.claim()
        await supervisor.start()
    logger.info("MiniWorld application started (background loops enabled=%s)", loops_enabled)
    try:
        yield
    finally:
        await supervisor.stop()
        ownership.release()
        engine.dispose()
        logger.info("MiniWorld application stopped")


app = FastAPI(title="MiniWorld", version="1.6.0", lifespan=lifespan)
app.include_router(world_router)
app.include_router(npc_router)
app.include_router(agent_router)
app.include_router(runtime_router)
app.include_router(dashboard_router)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


if __name__ == "__main__":
    # A single worker deliberately owns the simulation loop. Reload is disabled to
    # prevent two development processes from advancing the same world.
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=False, workers=1)
