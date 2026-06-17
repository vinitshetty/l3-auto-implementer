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

    # Start Mistral Workflows worker — connects to Mistral's Temporal server,
    # your laptop acts as the worker node executing activities locally
    worker_task = None
    if os.environ.get("MISTRAL_API_KEY"):
        try:
            from mistralai.workflows import run_worker
            from app.workflows.hydra_session import HydraSessionWorkflow
            from app.workflows.triage import TriageWorkflow
            import logging
            logging.getLogger(__name__).info("Starting Mistral Workflows worker...")
            worker_task = await run_worker(
                workflows=[HydraSessionWorkflow, TriageWorkflow],
                detach=True,
            )
            logging.getLogger(__name__).info("Mistral Workflows worker started")
        except Exception as e:
            import logging
            logging.getLogger(__name__).error("Worker failed to start: %s", e, exc_info=True)

    yield

    await engine.dispose()


app = FastAPI(title="Hydra Demo", lifespan=lifespan)

# API routers
from app.api.sessions import router as sessions_router  # noqa: E402
from app.api.webhooks import router as webhooks_router  # noqa: E402
from app.api.events import router as events_router  # noqa: E402

app.include_router(sessions_router, prefix="/api")
app.include_router(webhooks_router, prefix="/api")
app.include_router(events_router, prefix="/api")

# Static files — mount last so API routes take priority
static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir, html=True), name="static")
