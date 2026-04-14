"""POST /api/jobs/{id}/re-render and /review-only (US-010).

This module wires two "partial-pipeline" endpoints on top of the
sequential :class:`~src.web.jobs.JobRegistry` worker used by the rest
of AVE Studio:

* ``POST /api/jobs/{job_id}/re-render`` takes a user-edited
  :class:`EditPlan`, validates it against the parent job's
  :class:`FootageIndex` using the same helpers the PUT route uses
  (``_load_footage_index_or_422`` + ``_validate_edit_plan_against_index``
  from :mod:`src.web.routes.jobs`), and enqueues an ``"editor-only"``
  child job. The worker path calls :func:`run_editor` directly --
  no Director, no Reviewer -- so B-Roll compositing and trim logic go
  through the same renderer the full pipeline uses without any branching.
  The parent job's ``result`` is **not** mutated, so previous renders
  stay addressable by job id and the frontend can maintain a version
  history for the chat session.
* ``POST /api/jobs/{job_id}/review-only`` enqueues a ``"reviewer-only"``
  child job that calls :func:`run_reviewer` against the parent's latest
  rendered MP4. Useful after a manual plan edit + re-render where the
  user wants a fresh quality score without triggering another full
  Director retry loop.

Both endpoints mirror the contract of
:mod:`src.web.routes.feedback`: ``202 Accepted`` on success with
``{"job_id", "status", "parent_job_id"}``, ``404`` for unknown parents,
``409`` when the parent is not yet completed / missing the context the
re-run needs, and ``422`` when the submitted :class:`EditPlan` fails
pydantic coercion or semantic validation against the footage index.

All registry mutation routes through
:meth:`JobRegistry.submit_editor_rerun` and
:meth:`JobRegistry.submit_reviewer_only`, which each raise
``ValueError`` on invalid parents; this module converts those to
``409`` HTTP responses.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from src.models.schemas import EditPlan
from src.web.routes.jobs import (
    _load_footage_index_or_422,
    _validate_edit_plan_against_index,
    get_registry,
)

router = APIRouter(prefix="/api/jobs", tags=["render"])


class ReRenderResponse(BaseModel):
    """Response body for ``POST /api/jobs/{id}/re-render``.

    Mirrors :class:`~src.web.routes.feedback.FeedbackResponse` so the
    frontend can handle both feedback-rerun and editor-only re-render
    responses with a single parser.
    """

    job_id: str = Field(
        ..., description="UUID of the new editor-only re-render job."
    )
    status: str = Field(
        ..., description="Initial status, always ``pending``."
    )
    parent_job_id: str = Field(
        ...,
        description="UUID of the parent job whose plan this re-render revises.",
    )


@router.post(
    "/{job_id}/re-render",
    status_code=202,
    response_model=ReRenderResponse,
)
async def post_re_render(
    job_id: str,
    payload: dict[str, Any],
    request: Request,
) -> ReRenderResponse:
    """Validate a modified :class:`EditPlan` and enqueue an editor-only job.

    The body is the same serialized :class:`EditPlan` shape the GET
    endpoint returns -- the frontend can submit the output of its local
    edit state directly without any transformation. Validation happens
    in the same three layers the PUT route uses:

    1. Pydantic coercion via :meth:`EditPlan.model_validate`. Bad
       types / missing required fields surface as a ``422`` with the
       same error-list shape FastAPI uses for automatic body
       validation.
    2. :func:`_load_footage_index_or_422` loads the parent job's
       :class:`FootageIndex` from disk. A missing / unreadable /
       malformed index is surfaced as ``422`` with a server-side
       ``job.footage_index_path`` error so the user sees "the job's
       footage is gone" instead of a client-side validation error.
    3. :func:`_validate_edit_plan_against_index` walks every entry to
       check shot_id resolution, trim bounds, and the contiguous
       position sequence. Per-field errors are returned in the same
       shape as layer 1.

    Only if all three pass do we call
    :meth:`JobRegistry.submit_editor_rerun` to enqueue the job. The
    parent job's ``result`` is never mutated by this route -- the
    previous render stays available under its original id so the
    frontend can build a render-version list.

    Returns ``202`` with ``{"job_id", "status", "parent_job_id"}`` so
    the client can subscribe to ``/ws/jobs/{new_id}`` and stream
    progress through the same plumbing used for full-pipeline and
    feedback-rerun jobs.
    """
    registry = get_registry(request)
    parent = registry.get(job_id)
    if parent is None:
        raise HTTPException(
            status_code=404,
            detail=f"job {job_id!r} not found",
        )
    if parent.result is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"job {job_id!r} has no result yet "
                f"(status={parent.status!r}); cannot re-render"
            ),
        )

    # Layer 1: pydantic coercion. Surfaces as 422 with the same error
    # shape FastAPI uses for automatic request validation so the
    # frontend can render field-level messages uniformly.
    try:
        plan = EditPlan.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=exc.errors(),
        ) from exc

    # Layers 2 + 3: footage-aware semantic validation. Reuses the PUT
    # helpers (imported from src.web.routes.jobs) so backend validation
    # stays consistent with the PUT path even if the client skipped the
    # save step and went straight to re-render.
    index = _load_footage_index_or_422(parent.footage_index_path)
    errors = _validate_edit_plan_against_index(plan, index)
    if errors:
        raise HTTPException(status_code=422, detail=errors)

    try:
        child = registry.submit_editor_rerun(parent, plan)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return ReRenderResponse(
        job_id=child.id,
        status=child.status,
        parent_job_id=parent.id,
    )


class ReviewOnlyResponse(BaseModel):
    """Response body for ``POST /api/jobs/{id}/review-only``.

    Identical envelope shape to :class:`ReRenderResponse` so the
    frontend's "new job spawned" handler can process both.
    """

    job_id: str = Field(
        ..., description="UUID of the new reviewer-only scoring job."
    )
    status: str = Field(
        ..., description="Initial status, always ``pending``."
    )
    parent_job_id: str = Field(
        ...,
        description="UUID of the parent job whose MP4 this job scores.",
    )


@router.post(
    "/{job_id}/review-only",
    status_code=202,
    response_model=ReviewOnlyResponse,
)
async def post_review_only(
    job_id: str,
    request: Request,
) -> ReviewOnlyResponse:
    """Enqueue a reviewer-only scoring pass on the parent's latest MP4.

    No request body: all the context the Reviewer needs
    (:class:`CreativeBrief` + path to the rendered video) lives on
    the parent :class:`Job`. The registry method
    :meth:`JobRegistry.submit_reviewer_only` verifies the parent is
    ``"completed"``, has a ``final_video_path`` in its result, and
    that the referenced MP4 actually exists on disk -- any missing
    precondition surfaces as a ``409 Conflict`` so the client gets a
    descriptive error instead of a 500 when the worker inevitably
    fails.

    Returns ``202`` with ``{"job_id", "status", "parent_job_id"}`` so
    the frontend can subscribe to ``/ws/jobs/{new_id}`` and watch the
    scoring run stream.
    """
    registry = get_registry(request)
    parent = registry.get(job_id)
    if parent is None:
        raise HTTPException(
            status_code=404,
            detail=f"job {job_id!r} not found",
        )
    try:
        child = registry.submit_reviewer_only(parent)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return ReviewOnlyResponse(
        job_id=child.id,
        status=child.status,
        parent_job_id=parent.id,
    )
