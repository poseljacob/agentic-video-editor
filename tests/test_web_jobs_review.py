"""Tests for the ``GET /api/jobs/{id}/review`` route (US-006).

The review endpoint returns the :class:`ReviewScore` that the pipeline
reviewer produced for a completed job, along with the retry bookkeeping
(``retries_used`` and ``feedback_history``) the review loop populates when
the reviewer sends the director back for another pass.

These tests exercise the real FastAPI router via
:class:`fastapi.testclient.TestClient` -- nothing is mocked. The strategy
mirrors :mod:`tests.test_web_ws`:

* Seed :attr:`src.web.jobs.JobRegistry._jobs` directly with hand-built
  :class:`Job` instances so we never touch ``run_pipeline`` (which talks
  to Gemini + ffmpeg and is orders of magnitude too slow to belong in a
  unit test back-pressure loop).
* Construct :class:`CreativeBrief` as a real Pydantic model (same
  ``_make_brief`` helper as ``test_web_ws``) so we exercise the actual
  ``Job`` dataclass without field-level assumptions.
* Use the ``TestClient`` context manager so the app's lifespan runs --
  the registry is attached to ``app.state.job_registry`` during lifespan
  startup. The sequential worker it spawns never picks up any of our
  seeded jobs because we bypass ``submit`` (which is what pushes onto the
  worker queue); our jobs live purely in the ``_jobs`` dict.

Four cases:

1. ``200`` with a fully populated ``review`` dict, ``retries_used=0``,
   and an empty ``feedback_history``. Asserts the response body matches
   the serialized :class:`PipelineResult` shape exactly.
2. ``200`` with ``review=None`` and a non-empty ``feedback_history``
   to make sure the null review passes straight through and the retry
   metadata is preserved (the JS side converts this into a "no review
   yet" empty state via the same 409 path, but the HTTP contract still
   has to honor the shape).
3. ``404`` for a random UUID. Asserts the detail string includes the
   id so the UI can surface a useful error banner.
4. ``409`` for a job whose ``result`` is still ``None`` (pending or
   running). Asserts the detail mentions the status and tells the
   client to "wait".
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from src.models.schemas import CreativeBrief
from src.web.app import app
from src.web.jobs import Job, JobRegistry


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #


def _make_brief() -> CreativeBrief:
    """Minimal brief that satisfies :class:`CreativeBrief` validation.

    Kept identical to the ``test_web_ws.py`` helper so the two files
    share a single mental model of "what a synthetic job looks like."
    """
    return CreativeBrief(
        product="test-product",
        audience="test-audience",
        tone="energetic",
        duration_seconds=15,
    )


def _make_job(job_id: str, result: dict | None, status: str = "completed") -> Job:
    """Construct a bare :class:`Job` with no running worker behind it.

    ``result`` is inserted as-is so each test case can hand us the exact
    shape it wants the route to echo back. ``status`` defaults to
    ``completed`` because that is the happy-path state; the 409 test
    overrides it to ``running`` to simulate a job that has not reached
    the reviewer step yet.
    """
    return Job(
        id=job_id,
        status=status,
        brief=_make_brief(),
        footage_index_path="/tmp/footage.json",
        pipeline_path="/tmp/pipeline.yaml",
        result=result,
    )


@pytest.fixture()
def client():
    """FastAPI TestClient bound to a lifespan that spins up the registry.

    The ``with`` block is load-bearing: it's what actually runs the
    ``lifespan`` context manager that attaches a :class:`JobRegistry`
    instance onto ``app.state.job_registry``. Tests reach into that
    registry directly to seed jobs (same pattern as ``test_web_ws``).
    """
    with TestClient(app) as test_client:
        yield test_client


# --------------------------------------------------------------------- #
# 200 -- fully populated review
# --------------------------------------------------------------------- #


def test_get_review_returns_full_review_payload(client: TestClient) -> None:
    """Happy path: seeded job with a full ReviewScore round-trips exactly.

    The route is a thin adapter over ``job.result``, so the test's job
    is to assert the exact shape the frontend will rely on:

    * ``review`` is the nested ReviewScore dict untouched.
    * ``retries_used`` is 0 (one-shot review -- no retries).
    * ``feedback_history`` is an empty list (no retries -> no history).
    """
    registry: JobRegistry = client.app.state.job_registry
    job_id = "review-happy-path"
    review = {
        "adherence": 0.9,
        "pacing": 0.85,
        "visual_quality": 0.8,
        "watchability": 0.92,
        "overall": 0.87,
        "feedback": "Strong cut overall -- tighten the middle section.",
    }
    job = _make_job(
        job_id,
        result={
            "edit_plan": None,
            "final_video_path": "/tmp/final.mp4",
            "review": review,
            "retries_used": 0,
            "warnings": [],
            "feedback_history": [],
        },
    )
    registry._jobs[job_id] = job

    response = client.get(f"/api/jobs/{job_id}/review")

    assert response.status_code == 200
    body = response.json()
    # The endpoint strips the rest of the PipelineResult fields and
    # surfaces only the three review-relevant keys.
    assert set(body.keys()) == {"review", "retries_used", "feedback_history"}
    assert body["review"] == review
    assert body["retries_used"] == 0
    assert body["feedback_history"] == []


# --------------------------------------------------------------------- #
# 200 -- null review with populated history
# --------------------------------------------------------------------- #


def test_get_review_returns_null_review_with_history(client: TestClient) -> None:
    """A completed job with ``review=None`` still returns 200 + history.

    This case happens when the reviewer bailed out after the retry
    budget was exhausted: the pipeline finishes (``status='completed'``)
    but ``result['review']`` is ``None`` because no passing score was
    ever produced. The retry history is still valuable to the UI -- it
    lets the collapsible "History" section show every attempt's
    feedback even when the final chart is blank.

    The contract: pass-through. The route should not coerce ``None`` to
    an empty dict, and it should not drop the history list.
    """
    registry: JobRegistry = client.app.state.job_registry
    job_id = "review-null-with-history"
    history = ["first attempt: too slow", "second attempt: still drifting"]
    job = _make_job(
        job_id,
        result={
            "edit_plan": None,
            "final_video_path": "/tmp/final.mp4",
            "review": None,
            "retries_used": 2,
            "warnings": [],
            "feedback_history": history,
        },
    )
    registry._jobs[job_id] = job

    response = client.get(f"/api/jobs/{job_id}/review")

    assert response.status_code == 200
    body = response.json()
    assert body["review"] is None
    assert body["retries_used"] == 2
    # The route calls ``list(...)`` on the stored history so the response
    # body is a fresh list -- assert equality, not identity.
    assert body["feedback_history"] == history


# --------------------------------------------------------------------- #
# 404 -- unknown job id
# --------------------------------------------------------------------- #


def test_get_review_returns_404_for_unknown_job(client: TestClient) -> None:
    """Requesting a random UUID returns 404 with the id in the detail.

    The endpoint uses ``registry.get`` to look up jobs, so an unknown id
    simply never appears in the ``_jobs`` dict. We intentionally do NOT
    seed the registry -- fresh lifespan, empty registry, immediate miss.

    We assert the detail *contains* the id rather than comparing the full
    string so the test stays stable against harmless message tweaks.
    """
    unknown_id = str(uuid.uuid4())

    response = client.get(f"/api/jobs/{unknown_id}/review")

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert unknown_id in detail


# --------------------------------------------------------------------- #
# 409 -- job exists but has no result yet
# --------------------------------------------------------------------- #


def test_get_review_returns_409_when_result_is_none(client: TestClient) -> None:
    """A running job with ``result=None`` returns 409, not 404 or 500.

    409 is the right code because the resource exists (the job id is
    valid) but is not in a state where the requested sub-resource can
    be served -- the classic "wait for it" case.

    The frontend depends on two pieces of detail text:

    * the word ``wait``, which it surfaces in a muted banner ("No review
      yet... wait for the pipeline"),
    * the current job status, so the user knows whether the run is
      still in flight or wedged.

    Both must be present in the detail string.
    """
    registry: JobRegistry = client.app.state.job_registry
    job_id = "review-still-running"
    job = _make_job(job_id, result=None, status="running")
    registry._jobs[job_id] = job

    response = client.get(f"/api/jobs/{job_id}/review")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "wait" in detail.lower()
    assert "running" in detail
