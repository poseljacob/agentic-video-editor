"""FastAPI routes for the AVE Studio job API.

All three endpoints operate against the :class:`~src.web.jobs.JobRegistry`
that is stored on ``app.state.job_registry`` by the app's lifespan handler.
Routes fetch the registry via a FastAPI dependency so tests can swap it out
by overriding :func:`get_registry` if needed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from src.models.schemas import CreativeBrief, EditPlan, FootageIndex, Shot
from src.web.jobs import JobRegistry

#: Float tolerance when matching ``EditPlanEntry.shot_id`` start_time
#: suffixes to :attr:`Shot.start_time` during PUT validation. Matches
#: the looser-than-editor epsilon used in
#: :mod:`src.web.routes.clips` so display-resolution and PUT-validation
#: resolution agree on whether a shot_id refers to an existing shot --
#: a round-trip through JSON may drop a few LSBs that a strict
#: ``1e-6`` comparison would reject.
_SHOT_MATCH_EPSILON = 1e-3

#: Tiny extra tolerance on trim-bound comparisons. Clients that slid a
#: slider control to the exact end of a shot can end up passing a
#: start/end trim that is a few LSBs outside the shot's declared
#: ``[start_time, end_time]`` range; rejecting those would be a UX
#: footgun, so we accept anything within this margin and let the
#: editor ffmpeg pipeline clamp to the real bounds.
_TRIM_EPSILON = 1e-3

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


class CreateJobRequest(BaseModel):
    """Request payload for ``POST /api/jobs``.

    The brief is accepted as a nested object so the UI can serialize its
    Alpine.js form state 1:1 without any field renaming. ``style_ref`` is
    optional to match :class:`CreativeBrief`.
    """

    brief: CreativeBrief
    footage_index_path: str = Field(
        ..., description="Filesystem path to the serialized FootageIndex JSON."
    )
    pipeline_path: str = Field(
        ..., description="Filesystem path to the YAML pipeline manifest."
    )


class CreateJobResponse(BaseModel):
    """Response body for ``POST /api/jobs``."""

    job_id: str
    status: str


def get_registry(request: Request) -> JobRegistry:
    """FastAPI dependency: fetch the :class:`JobRegistry` from app state.

    The registry is attached to ``app.state`` by the lifespan handler in
    :mod:`src.web.app`. Raising a 503 (instead of letting a bare
    ``AttributeError`` propagate) gives the client a clearer signal when a
    request lands before startup finishes or after shutdown has begun.
    """
    registry: JobRegistry | None = getattr(request.app.state, "job_registry", None)
    if registry is None:
        raise HTTPException(
            status_code=503,
            detail="JobRegistry is not initialized yet; server is still starting",
        )
    return registry


@router.post("", response_model=CreateJobResponse, status_code=202)
async def create_job(
    payload: CreateJobRequest,
    request: Request,
) -> CreateJobResponse:
    """Submit a new pipeline job and return its id + initial status.

    The job starts in ``pending`` and transitions to ``running`` once the
    sequential worker picks it up. Clients poll ``GET /api/jobs/{id}`` for
    updates.
    """
    registry = get_registry(request)
    job = registry.submit(
        brief=payload.brief,
        footage_index_path=payload.footage_index_path,
        pipeline_path=payload.pipeline_path,
    )
    return CreateJobResponse(job_id=job.id, status=job.status)


@router.get("")
async def list_jobs(request: Request) -> list[dict[str, Any]]:
    """Return a status-summary list of every known job.

    The UI uses this for the sidebar / history panel — it should stay
    lightweight, so we return the :meth:`Job.summary` shape instead of the
    full progress log.
    """
    registry = get_registry(request)
    return [job.summary() for job in registry.list_jobs()]


@router.get("/{job_id}")
async def get_job(job_id: str, request: Request) -> dict[str, Any]:
    """Return the full state of a single job, including its progress log.

    Returns 404 if the id is unknown. The returned dict is a snapshot;
    later appends by the worker thread do not mutate it.
    """
    registry = get_registry(request)
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found")
    return job.to_dict()


@router.get("/{job_id}/review")
async def get_job_review(job_id: str, request: Request) -> dict[str, Any]:
    """Return the ReviewScore + retry metadata for a completed job.

    Body shape: ``{"review": {...} | None, "retries_used": int,
    "feedback_history": [...]}``. Returns 404 if the job id is unknown and
    409 if the job exists but has no result yet (still pending/running, or
    the run failed before a ReviewScore was produced).
    """
    registry = get_registry(request)
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found")
    if job.result is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"job {job_id!r} has no review yet (status={job.status!r}); "
                "wait until the pipeline completes"
            ),
        )
    return {
        "review": job.result.get("review"),
        "retries_used": job.result.get("retries_used", 0),
        "feedback_history": list(job.result.get("feedback_history", [])),
    }


# --------------------------------------------------------------------------- #
# PUT /api/jobs/{job_id}/edit-plan (US-009)
# --------------------------------------------------------------------------- #


def _validation_error(
    loc: list[str | int],
    message: str,
    error_type: str,
) -> dict[str, Any]:
    """Build a single FastAPI-shaped validation error dict.

    Mirrors the body FastAPI itself emits for automatic pydantic
    failures so the frontend can render field-level errors uniformly
    whether they came from schema coercion or our hand-written
    checks.
    """
    return {"loc": loc, "msg": message, "type": error_type}


def _load_footage_index_or_422(path_str: str | None) -> FootageIndex:
    """Load a :class:`FootageIndex` from ``path_str`` or raise HTTP 422.

    The PUT endpoint needs the index to validate every shot_id, so a
    missing/unreadable/malformed index is a hard failure for the
    request (unlike the GET endpoint which degrades to
    ``roll_type="unknown"``). The error shape intentionally targets the
    server-side ``footage_index_path`` the job was created with, not a
    field in the request body, so the frontend surfaces an operator
    error ("the job's footage is gone") rather than a user error.
    """
    if not path_str:
        raise HTTPException(
            status_code=422,
            detail=[
                _validation_error(
                    ["job", "footage_index_path"],
                    "job has no footage_index_path; cannot validate edit plan",
                    "value_error.missing",
                )
            ],
        )
    path = Path(path_str)
    if not path.exists():
        raise HTTPException(
            status_code=422,
            detail=[
                _validation_error(
                    ["job", "footage_index_path"],
                    f"footage_index_path not found on disk: {path_str!r}",
                    "value_error.path_not_found",
                )
            ],
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(
            status_code=422,
            detail=[
                _validation_error(
                    ["job", "footage_index_path"],
                    f"footage_index_path unreadable: {exc}",
                    "value_error.path_unreadable",
                )
            ],
        ) from exc
    try:
        return FootageIndex.model_validate_json(text)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=[
                _validation_error(
                    ["job", "footage_index_path"],
                    (
                        "footage_index_path exists but is not a valid "
                        f"FootageIndex: {exc.errors()[:3]}"
                    ),
                    "value_error.footage_index_invalid",
                )
            ],
        ) from exc


def _resolve_shot(shot_id: str, index: FootageIndex) -> Shot | None:
    """Return the :class:`Shot` referenced by ``shot_id`` or ``None``.

    Splits on the LAST ``#`` (source paths may contain ``#``), parses
    the trailing float as the shot's start_time, and matches against
    :attr:`FootageIndex.shots` with :data:`_SHOT_MATCH_EPSILON`
    tolerance. Mirrors the resolver in :mod:`src.web.routes.clips` so
    GET-enrichment and PUT-validation agree on what counts as a
    matching shot.
    """
    sep = shot_id.rfind("#")
    if sep == -1:
        return None
    source_file = shot_id[:sep]
    suffix = shot_id[sep + 1 :]
    try:
        start_time = float(suffix)
    except ValueError:
        return None
    for shot in index.shots:
        if (
            shot.source_file == source_file
            and abs(shot.start_time - start_time) < _SHOT_MATCH_EPSILON
        ):
            return shot
    return None


def _validate_edit_plan_against_index(
    plan: EditPlan,
    index: FootageIndex,
) -> list[dict[str, Any]]:
    """Collect every field-level validation error against ``index``.

    Returns a list of FastAPI-shaped error dicts (one per offending
    field). An empty list means the plan passes. The caller decides
    whether to raise 422 or persist.

    Three families of checks:

    1. Every entry's ``shot_id`` resolves to a :class:`Shot` in the
       FootageIndex. Unresolved entries get a single error against
       ``["body", "entries", i, "shot_id"]``.
    2. Every entry's ``[start_trim, end_trim]`` lies within the shot's
       ``[start_time, end_time]`` bounds (with
       :data:`_TRIM_EPSILON` slack), and the trims are monotonic
       (``start_trim <= end_trim``). Both bound violations and
       ordering violations are reported against the offending field.
       Trim checks are skipped for entries that failed check 1 so we
       do not drown the frontend in cascading errors for a single bad
       id.
    3. Positions form a contiguous ``0..N-1`` sequence. This is
       reported once at the plan level with ``["body", "entries"]``
       (no per-entry duplication) because the offending index set is
       a property of the whole list, not any single entry.
    """
    errors: list[dict[str, Any]] = []
    entries = list(plan.entries)

    for i, entry in enumerate(entries):
        shot = _resolve_shot(entry.shot_id, index)
        if shot is None:
            errors.append(
                _validation_error(
                    ["body", "entries", i, "shot_id"],
                    (
                        f"shot_id {entry.shot_id!r} does not resolve to any "
                        "shot in the footage index"
                    ),
                    "value_error.shot_not_found",
                )
            )
            continue

        # Trim order: start must be <= end (equality allowed so a
        # zero-duration placeholder is not rejected -- the editor will
        # drop it later but that is not a validation concern).
        if entry.start_trim > entry.end_trim + _TRIM_EPSILON:
            errors.append(
                _validation_error(
                    ["body", "entries", i, "start_trim"],
                    (
                        f"start_trim ({entry.start_trim}) must be <= end_trim "
                        f"({entry.end_trim})"
                    ),
                    "value_error.trim_order",
                )
            )
            continue

        # Bounds: trims must lie within the shot's [start_time,
        # end_time] window (with epsilon slack).
        if entry.start_trim < shot.start_time - _TRIM_EPSILON:
            errors.append(
                _validation_error(
                    ["body", "entries", i, "start_trim"],
                    (
                        f"start_trim ({entry.start_trim}) is before shot "
                        f"start_time ({shot.start_time})"
                    ),
                    "value_error.trim_out_of_bounds",
                )
            )
        if entry.start_trim > shot.end_time + _TRIM_EPSILON:
            errors.append(
                _validation_error(
                    ["body", "entries", i, "start_trim"],
                    (
                        f"start_trim ({entry.start_trim}) is after shot "
                        f"end_time ({shot.end_time})"
                    ),
                    "value_error.trim_out_of_bounds",
                )
            )
        if entry.end_trim < shot.start_time - _TRIM_EPSILON:
            errors.append(
                _validation_error(
                    ["body", "entries", i, "end_trim"],
                    (
                        f"end_trim ({entry.end_trim}) is before shot "
                        f"start_time ({shot.start_time})"
                    ),
                    "value_error.trim_out_of_bounds",
                )
            )
        if entry.end_trim > shot.end_time + _TRIM_EPSILON:
            errors.append(
                _validation_error(
                    ["body", "entries", i, "end_trim"],
                    (
                        f"end_trim ({entry.end_trim}) is after shot "
                        f"end_time ({shot.end_time})"
                    ),
                    "value_error.trim_out_of_bounds",
                )
            )

    # Positions must form a contiguous 0..N-1 sequence. We sort so
    # reorders are permitted -- the frontend may submit entries in any
    # order so long as the set of declared positions is exactly
    # ``{0, 1, ..., N-1}``.
    positions = sorted(entry.position for entry in entries)
    expected = list(range(len(entries)))
    if positions != expected:
        errors.append(
            _validation_error(
                ["body", "entries"],
                (
                    f"positions must form a contiguous 0..N-1 sequence "
                    f"(got {positions!r}, expected {expected!r})"
                ),
                "value_error.positions_not_contiguous",
            )
        )

    return errors


@router.put("/{job_id}/edit-plan")
async def update_edit_plan(
    job_id: str,
    payload: dict[str, Any],
    request: Request,
) -> dict[str, Any]:
    """Persist a modified :class:`EditPlan` onto a completed job.

    Accepts the same serialized shape the GET endpoint returns (minus
    the display-only fields) and validates it against the job's
    original :class:`FootageIndex` before writing it back onto
    :attr:`Job.result`. Does NOT trigger any pipeline re-run -- that
    flow belongs to ``POST /api/jobs/{id}/feedback``.

    Validation happens in three layers:

    1. The body must be a valid :class:`EditPlan` per Pydantic
       (missing/mistyped fields -> 422).
    2. Every ``shot_id`` must resolve and every trim must stay within
       the resolved shot's bounds (422 with per-field errors).
    3. The positions across all entries must form a contiguous
       ``0..N-1`` sequence (422 with a plan-level error).

    On success the request returns ``{"edit_plan": <saved plan>}`` and
    mutates ``job.result["edit_plan"]`` in place so subsequent
    ``GET /api/jobs/{id}/edit-plan`` and ``GET /api/clips/{id}/{pos}/
    thumbnail`` calls see the new shape.
    """
    registry = get_registry(request)
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail=f"job {job_id!r} not found",
        )
    if job.result is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"job {job_id!r} has no edit plan yet (status={job.status!r}); "
                "wait until the pipeline completes"
            ),
        )

    # Layer 1: pydantic coercion. Raises ValidationError we convert to
    # a 422 with the same shape FastAPI uses for automatic request
    # validation -- the frontend already knows how to render that.
    try:
        plan = EditPlan.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=exc.errors(),
        ) from exc

    # Layer 2 + 3: footage-aware semantic validation.
    index = _load_footage_index_or_422(job.footage_index_path)
    errors = _validate_edit_plan_against_index(plan, index)
    if errors:
        raise HTTPException(status_code=422, detail=errors)

    # Persist back onto the job. ``job.result`` is a plain dict (see
    # :func:`src.web.jobs._serialize_result`) so we overwrite the
    # ``edit_plan`` key in place. Mutation is safe from the asyncio
    # event loop -- no worker thread is touching this result after
    # ``status="completed"``.
    saved = plan.model_dump()
    job.result["edit_plan"] = saved
    return {"edit_plan": saved}
