import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from itte.config import settings
from itte.api.routes import router
from itte.db import repository as repo
from itte.memory.vector_store import VectorStore, periodic_rebuild_loop
from itte.core.risk_engine import RiskEngine
from itte.observability import configure_logging, logger

@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.log_level)

    logger.info("itte_startup_begin")

    repo.init_db()

    vector_store = VectorStore()
    memory_rows = repo.list_memory_items()

    await vector_store.initialize(memory_rows)

    app.state.vector_store = vector_store
    app.state.risk_engine = RiskEngine(vector_store)

    rebuild_task = asyncio.create_task(
        periodic_rebuild_loop(
            vector_store=vector_store,
            load_memory_rows=repo.list_memory_items,
            interval_seconds=settings.memory_rebuild_interval_seconds,
        )
    )

    logger.info("itte_startup_complete")

    try:
        yield
    finally:
        logger.info("itte_shutdown_begin")
        rebuild_task.cancel()

        try:
            await rebuild_task
        except asyncio.CancelledError:
            pass

        logger.info("itte_shutdown_complete")

app = FastAPI(
    title="ITTE MVP",
    description="Self-evolving risk brain for AI engineering.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(router)
