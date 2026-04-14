"""POST /api/jobs/{id}/feedback -- chat feedback triggers a pipeline re-run.

The endpoint accepts a free-text user message describing what the user
wants changed about the rendered video, constructs a new
``feedback-rerun`` :class:`~src.web.jobs.Job` that inherits the parent's
brief / footage / pipeline context, and returns ``202 Accepted`` with
the new job id so the UI can subscribe to ``/ws/jobs/{new-id}`` and
stream progress for the re-run exactly like a normal pipeline run.

Contract:

* ``POST /api/jobs/{id}/feedback`` body: ``{"message": "..."}``.
* Success response (202): ``{"job_id": "<uuid>", "status": "pending",
  "parent_job_id": "<uuid>"}``.
* ``404`` if the parent job id is unknown.
* ``409`` if the parent is not yet ``completed`` or is missing the
  ``edit_plan`` / ``footage_index_path`` the re-run needs as input.

All registry mutation happens through
:meth:`JobRegistry.submit_feedback_rerun`, which builds the
accumulated ``feedback_history`` the child should see and enqueues it
on the same sequential worker used by :meth:`JobRegistry.submit` so
only one pipeline runs at a time.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src.web.routes.jobs import get_registry

router = APIRouter(prefix="/api/jobs", tags=["feedback"])


class FeedbackRequest(BaseModel):
    """Request payload for ``POST /api/jobs/{id}/feedback``."""

    message: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        description=(
            "User feedback to inject into the Director on the re-run. "
            "Concatenated with prior feedback history and passed verbatim "
            "to ``_run_director_with_feedback``."
        ),
    )


class FeedbackResponse(BaseModel):
    """Response body for ``POST /api/jobs/{id}/feedback``."""

    job_id: str = Field(..., description="UUID of the new feedback-rerun job.")
    status: str = Field(..., description="Initial status, always ``pending``.")
    parent_job_id: str = Field(
        ..., description="UUID of the job whose output this re-run revises."
    )


@router.post(
    "/{job_id}/feedback",
    status_code=202,
    response_model=FeedbackResponse,
)
async def post_feedback(
    job_id: str,
    payload: FeedbackRequest,
    request: Request,
) -> FeedbackResponse:
    """Submit chat feedback for ``job_id`` and enqueue a pipeline re-run.

    The parent job must be in ``"completed"`` state with an ``edit_plan``
    in its serialized result -- the re-run path inside
    :meth:`JobRegistry._run_feedback_rerun_sync` calls
    :func:`_run_director_with_feedback` and feeds the revised plan
    through trim_refiner -> editor -> reviewer, so there must be a
    finished plan to build on. Pending / running / failed parents return
    409 Conflict.

    The child's ``feedback_history`` is the parent's existing history
    with ``payload.message`` appended, so each subsequent chat round
    sees every prior user message plus the original reviewer feedback
    (if any) in chronological order.
    """
    registry = get_registry(request)
    parent = registry.get(job_id)
    if parent is None:
        raise HTTPException(
            status_code=404,
            detail=f"job {job_id!r} not found",
        )
    try:
        child = registry.submit_feedback_rerun(parent, payload.message.strip())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FeedbackResponse(
        job_id=child.id,
        status=child.status,
        parent_job_id=parent.id,
    )
