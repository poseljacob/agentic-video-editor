"""FastAPI application for AVE Studio.

Serves the Alpine.js + Tailwind UI shell, exposes a health endpoint, and
mounts static + media directories for the browser to consume. Also owns the
in-memory :class:`~src.web.jobs.JobRegistry` that runs pipeline executions
as sequential background tasks (US-002).
"""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src.web.jobs import JobRegistry
from src.web.routes.clips import router as clips_router
from src.web.routes.config import router as config_router
from src.web.routes.feedback import router as feedback_router
from src.web.routes.footage import router as footage_router
from src.web.routes.jobs import router as jobs_router
from src.web.routes.browse import router as browse_router
from src.web.routes.projects import router as projects_router
from src.web.routes.render import router as render_router
from src.web.routes.ws import router as ws_router

REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = Path(__file__).resolve().parent / "static"
OUTPUT_DIR = REPO_ROOT / "output"

# Ensure the media mount target exists before StaticFiles validates it at
# import time. StaticFiles will raise if the directory is missing.
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Bring the :class:`JobRegistry` up on startup and down on shutdown.

    The registry's sequential asyncio worker must run inside the event
    loop that FastAPI starts, so we create and start it here rather than
    at module import time. Request handlers pull the live instance off
    ``app.state.job_registry`` via the :func:`get_registry` dependency.
    """
    registry = JobRegistry()
    await registry.start()
    app.state.job_registry = registry
    try:
        yield
    finally:
        await registry.stop()


app = FastAPI(
    title="AVE Studio",
    version="0.1.0",
    description="Agentic Video Editor web UI and API layer.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8000", "http://127.0.0.1:8000", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/media", StaticFiles(directory=OUTPUT_DIR), name="media")

app.include_router(config_router)
app.include_router(jobs_router)
app.include_router(feedback_router)
app.include_router(render_router)
app.include_router(clips_router)
app.include_router(footage_router)
app.include_router(browse_router)
app.include_router(projects_router)
app.include_router(ws_router)


@app.get("/api/health")
async def health() -> dict[str, str]:
    """Lightweight liveness probe used by the UI and deploy checks."""
    return {"status": "ok"}


@app.get("/")
async def index() -> FileResponse:
    """Serve the Alpine.js + Tailwind UI shell."""
    return FileResponse(STATIC_DIR / "index.html")
