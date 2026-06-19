import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.database import Base, engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create DB tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Add columns that may not exist in older DBs
        for col in ("test_results_json", "confidence_json"):
            try:
                await conn.execute(
                    __import__("sqlalchemy").text(
                        f"ALTER TABLE hydra_sessions ADD COLUMN {col} JSON"
                    )
                )
            except Exception:
                pass  # column already exists

    # Start Mistral Workflows worker in background so it doesn't block server startup
    import asyncio
    import logging
    _logger = logging.getLogger(__name__)

    async def _start_worker():
        if not os.environ.get("MISTRAL_API_KEY"):
            return
        try:
            from mistralai.workflows import run_worker
            from app.workflows.hydra_session import HydraSessionWorkflow
            from app.workflows.triage import TriageWorkflow
            _logger.info("Starting Mistral Workflows worker...")
            await run_worker(
                workflows=[HydraSessionWorkflow, TriageWorkflow],
                detach=True,
            )
            _logger.info("Mistral Workflows worker started")
        except Exception as e:
            _logger.error("Worker failed to start: %s", e, exc_info=True)

    asyncio.create_task(_start_worker())

    yield

    await engine.dispose()


app = FastAPI(title="Hydra Demo", lifespan=lifespan)

# API routers
from app.api.sessions import router as sessions_router  # noqa: E402
from app.api.webhooks import router as webhooks_router  # noqa: E402
from app.api.events import router as events_router  # noqa: E402
from app.api.repo_profiles import router as repo_profiles_router  # noqa: E402

app.include_router(sessions_router, prefix="/api")
app.include_router(webhooks_router, prefix="/api")
app.include_router(events_router, prefix="/api")
app.include_router(repo_profiles_router, prefix="/api")

# Static files — mount last so API routes take priority
static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir, html=True), name="static")
