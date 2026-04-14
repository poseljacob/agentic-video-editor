"""US-009 routes -- footage search/catalog access for the shot-swap UI.

Endpoints:

* ``GET /api/footage/search?query=...&footage_index_path=...`` -- thin
  HTTP wrapper around :func:`src.tools.analyze.search_moments`. Loads
  the :class:`~src.models.schemas.FootageIndex` from
  ``footage_index_path``, ranks shots against ``query`` using the same
  deterministic lexical ranker the Director agent uses, and returns a
  JSON list of matches in a shape the frontend shot-browser can render
  directly (shot_id, source_file, source_filename, start_time,
  end_time, duration, description, transcript, roll_type,
  relevance_score).
* ``GET /api/footage/catalog?footage_index_path=...`` -- loads the
  same :class:`FootageIndex` and returns every shot without applying
  lexical search. This gives the edit UI a complete local bounds cache
  without relying on stopword-prone broad search queries.

The endpoint is intentionally read-only and side-effect free -- swapping
shots in an :class:`~src.models.schemas.EditPlan` happens through
``PUT /api/jobs/{id}/edit-plan``; this route only feeds the picker.

Error shape matches the conventions used elsewhere in ``src/web``:

* ``422`` when ``query`` is empty or whitespace-only. Uses the standard
  FastAPI validation-error body with a ``loc`` pointing at the query
  parameter so the frontend can surface a field-level message.
* ``404`` when ``footage_index_path`` is missing, unreadable, or fails
  to parse as a :class:`FootageIndex`. The distinction between "file
  absent" and "file malformed" is surfaced in ``detail`` so operators
  can fix whichever they hit.
* ``200`` with ``{"query": ..., "results": [...], "count": N}``
  otherwise. ``results`` is empty when nothing matched -- that is NOT
  an error.

Defaults for ``min_relevance`` and ``max_results`` are wired as query
parameters so the frontend can tune recall vs. precision per picker
interaction without a backend change.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import ValidationError

from src.models.schemas import FootageIndex, Shot
from src.tools.analyze import _score_shot, _tokenize, search_moments

router = APIRouter(prefix="/api/footage", tags=["footage"])


#: Default minimum lexical relevance for the picker. Matches the
#: Director's typical call -- low enough to surface partial matches so
#: users can still manually pick near-misses, high enough that pure
#: stopword hits do not pollute the list.
_DEFAULT_MIN_RELEVANCE = 0.1

#: Default cap on results returned to the picker UI. The frontend
#: renders a scrollable list, so we do not need more than this per
#: query -- users who want a different result should refine the query.
_DEFAULT_MAX_RESULTS = 20


def _validation_error(field: str, message: str, error_type: str) -> list[dict[str, Any]]:
    """Build a FastAPI-shaped validation error body for ``HTTPException.detail``.

    FastAPI's own 422 responses use ``{"detail": [{"loc": [...],
    "msg": ..., "type": ...}]}`` so matching that shape here keeps the
    frontend error-rendering path uniform across hand-raised and
    automatic validation failures.
    """
    return [
        {
            "loc": ["query", field],
            "msg": message,
            "type": error_type,
        }
    ]


def _source_filename(source_file: str) -> str:
    """Return the bare filename for a source path.

    ``os.path.basename`` (not :meth:`pathlib.Path.name`) because the
    stored path may use a different separator than the server's host
    OS -- basename handles both styles without attempting normalization.
    """
    return os.path.basename(source_file) if source_file else ""


def _load_footage_index(path: Path, footage_index_path: str) -> FootageIndex:
    """Load ``path`` as a :class:`FootageIndex` or raise route-level 404."""
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=(
                f"footage_index_path not found on disk: {footage_index_path!r}"
            ),
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"footage_index_path unreadable: {exc}",
        ) from exc
    try:
        return FootageIndex.model_validate_json(text)
    except ValidationError as exc:
        raise HTTPException(
            status_code=404,
            detail=(
                f"footage_index_path exists but is not a valid FootageIndex: "
                f"{exc.errors()[:3]}"
            ),
        ) from exc


def _shot_result(shot: Shot, relevance_score: float | None = None) -> dict[str, Any]:
    """Return the display-ready shot payload used by footage endpoints."""
    duration = max(0.0, shot.end_time - shot.start_time)
    result: dict[str, Any] = {
        "shot_id": f"{shot.source_file}#{shot.start_time}",
        "source_file": shot.source_file,
        "source_filename": _source_filename(shot.source_file),
        "start_time": shot.start_time,
        "end_time": shot.end_time,
        "duration": duration,
        "description": shot.description,
        "transcript": shot.transcript,
        "roll_type": shot.roll_type,
        "display_label": (
            f"{_source_filename(shot.source_file)}@{shot.start_time:.1f}s"
        ),
    }
    if relevance_score is not None:
        result["relevance_score"] = round(relevance_score, 4)
    return result


@router.get("/catalog")
async def catalog_footage(
    footage_index_path: str = Query(
        ...,
        description=(
            "Filesystem path to a serialized FootageIndex JSON file. "
            "Returns every shot without applying search relevance."
        ),
    ),
) -> dict[str, Any]:
    """Return every shot in ``footage_index_path`` without lexical search."""
    path = Path(footage_index_path)
    index = _load_footage_index(path, footage_index_path)
    results = [_shot_result(shot) for shot in index.shots]
    return {
        "footage_index_path": str(path),
        "count": len(results),
        "results": results,
    }


@router.get("/search")
async def search_footage(
    query: str = Query(
        ...,
        description=(
            "Free-text query describing the desired moment. Tokenized "
            "against each shot's description, transcript, roll_type, "
            "and filename stem."
        ),
    ),
    footage_index_path: str = Query(
        ...,
        description=(
            "Filesystem path to a serialized FootageIndex JSON file. "
            "Typically the same value passed to ``POST /api/jobs``."
        ),
    ),
    min_relevance: float = Query(
        _DEFAULT_MIN_RELEVANCE,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum lexical score in [0.0, 1.0]. Shots scoring below "
            "this are dropped before truncation."
        ),
    ),
    max_results: int = Query(
        _DEFAULT_MAX_RESULTS,
        ge=1,
        le=200,
        description=(
            "Maximum number of ranked shots to return. Results come "
            "back sorted by descending relevance."
        ),
    ),
) -> dict[str, Any]:
    """Rank shots in ``footage_index_path`` against ``query``.

    Wraps :func:`src.tools.analyze.search_moments` so the picker UI
    does not need to know about the ranker or the FootageIndex file
    format. Returns a JSON object with the echoed query plus a
    ``results`` list containing each matching shot in display-ready
    form.
    """
    cleaned_query = query.strip()
    if not cleaned_query:
        raise HTTPException(
            status_code=422,
            detail=_validation_error(
                "query",
                "query must not be empty or whitespace-only",
                "value_error.empty",
            ),
        )

    path = Path(footage_index_path)

    # We could let ``search_moments`` raise here and convert the
    # exception in a wrapper, but doing it inline lets us tell the
    # caller the difference between "file missing" (404) and "file is
    # not a valid FootageIndex" (404 with a different detail message).
    try:
        matches = search_moments(
            footage_index_path=str(path),
            query=cleaned_query,
            min_relevance=min_relevance,
            max_results=max_results,
        )
    except FileNotFoundError as exc:
        # Defensive: ``path.exists()`` already guarded against the most
        # common case, but ``search_moments`` also raises on missing
        # files so we map both to the same 404.
        raise HTTPException(
            status_code=404,
            detail=f"footage_index_path not found on disk: {footage_index_path!r}",
        ) from exc
    except ValidationError as exc:
        raise HTTPException(
            status_code=404,
            detail=(
                f"footage_index_path exists but is not a valid FootageIndex: "
                f"{exc.errors()[:3]}"
            ),
        ) from exc

    # Compute per-shot relevance scores again so the response carries
    # them -- ``search_moments`` returns bare :class:`Shot` objects and
    # does not expose the internal score. Re-running the local ranker
    # is O(n) over the filtered list and keeps the tool function's
    # public contract unchanged.
    query_tokens = _tokenize(cleaned_query)

    results: list[dict[str, Any]] = []
    for shot in matches:
        score = _score_shot(query_tokens, shot)
        results.append(_shot_result(shot, score))

    return {
        "query": cleaned_query,
        "footage_index_path": str(path),
        "count": len(results),
        "results": results,
    }


__all__ = ["router"]
