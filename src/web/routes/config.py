"""FastAPI routes that expose brief-builder configuration options (US-004).

The AVE Studio brief builder needs to populate three dropdowns:

* style presets  — every YAML file under ``styles/``
* pipeline manifests — every YAML file under ``pipelines/``
* footage indexes — every ``footage_index*.json`` under ``output/``

These three ``GET`` endpoints enumerate those directories on demand and
return stable, alphabetically sorted lists so the UI render order is
deterministic. The endpoints are intentionally side-effect free: they
only read from disk, never write, and they degrade gracefully when the
directories are missing (returning ``[]`` instead of 404) or when a
footage-index file is unreadable (skipping it with a logged warning).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Resolve the repo root the same way :mod:`src.web.app` does so the three
# asset directories line up regardless of the current working directory
# when the server is started.
REPO_ROOT = Path(__file__).resolve().parents[3]
STYLES_DIR = REPO_ROOT / "styles"
PIPELINES_DIR = REPO_ROOT / "pipelines"
OUTPUT_DIR = REPO_ROOT / "output"

router = APIRouter(tags=["config"])


class StyleEntry(BaseModel):
    """Single entry in the ``GET /api/styles`` response."""

    name: str
    path: str


class PipelineEntry(BaseModel):
    """Single entry in the ``GET /api/pipelines`` response."""

    name: str
    path: str


class FootageIndexEntry(BaseModel):
    """Single entry in the ``GET /api/footage-indexes`` response.

    ``shot_count`` is derived from the serialized
    :class:`~src.models.schemas.FootageIndex` payload. ``created_at`` is
    the ISO8601 timestamp embedded in the file, falling back to the
    file's mtime (in UTC) when the payload has no timestamp.
    """

    name: str
    path: str
    shot_count: int
    created_at: str


def _list_yaml_entries(directory: Path, rel_prefix: str) -> list[dict[str, str]]:
    """Return one ``{name, path}`` dict per ``*.yaml`` file in ``directory``.

    ``name`` is the filename stem; ``path`` is a POSIX-style relative
    path from the repo root (e.g. ``styles/dtc-testimonial.yaml``). The
    result is sorted alphabetically by ``name`` so callers can rely on
    render order being deterministic. A missing directory produces an
    empty list rather than an error — the UI should treat "no presets"
    as a normal state.
    """
    if not directory.is_dir():
        return []
    entries: list[dict[str, str]] = []
    for yaml_path in directory.glob("*.yaml"):
        if not yaml_path.is_file():
            continue
        entries.append(
            {
                "name": yaml_path.stem,
                "path": f"{rel_prefix}/{yaml_path.name}",
            }
        )
    entries.sort(key=lambda entry: entry["name"])
    return entries


@router.get("/api/styles", response_model=list[StyleEntry])
async def list_styles() -> list[StyleEntry]:
    """Enumerate every style preset YAML under ``styles/``."""
    return [StyleEntry(**entry) for entry in _list_yaml_entries(STYLES_DIR, "styles")]


@router.get("/api/pipelines", response_model=list[PipelineEntry])
async def list_pipelines() -> list[PipelineEntry]:
    """Enumerate every pipeline manifest YAML under ``pipelines/``."""
    return [
        PipelineEntry(**entry)
        for entry in _list_yaml_entries(PIPELINES_DIR, "pipelines")
    ]


@router.get("/api/footage-indexes", response_model=list[FootageIndexEntry])
async def list_footage_indexes() -> list[FootageIndexEntry]:
    """Enumerate every ``footage_index*.json`` file under ``output/``.

    For each file we parse the JSON, count shots, and extract the
    embedded ``created_at`` timestamp. Files with invalid JSON are
    skipped with a logged warning so a single bad file doesn't take the
    entire endpoint down. If the payload has no timestamp we fall back
    to the file's ``mtime`` in UTC.
    """
    if not OUTPUT_DIR.is_dir():
        return []

    entries: list[FootageIndexEntry] = []

    # Collect index files: legacy top-level + project-scoped.
    index_paths: list[Path] = list(OUTPUT_DIR.glob("footage_index*.json"))
    projects_dir = OUTPUT_DIR / "projects"
    if projects_dir.is_dir():
        index_paths.extend(projects_dir.glob("*/footage_index.json"))

    for index_path in index_paths:
        if not index_path.is_file():
            continue
        try:
            with index_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Skipping unreadable footage index %s: %s", index_path.name, exc
            )
            continue

        if not isinstance(data, dict):
            logger.warning(
                "Skipping footage index %s: expected object, got %s",
                index_path.name,
                type(data).__name__,
            )
            continue

        shots = data.get("shots")
        shot_count = len(shots) if isinstance(shots, list) else 0

        created_at_raw = data.get("created_at")
        if isinstance(created_at_raw, str) and created_at_raw:
            created_at = created_at_raw
        else:
            mtime = datetime.fromtimestamp(
                index_path.stat().st_mtime, tz=timezone.utc
            )
            created_at = mtime.isoformat()

        # Compute a repo-relative path for both top-level and project indexes.
        try:
            rel_path = str(index_path.relative_to(REPO_ROOT))
        except ValueError:
            rel_path = str(index_path)

        entries.append(
            FootageIndexEntry(
                name=index_path.stem,
                path=rel_path,
                shot_count=shot_count,
                created_at=created_at,
            )
        )

    entries.sort(key=lambda entry: entry.name)
    return entries
