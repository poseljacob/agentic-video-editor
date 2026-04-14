"""Tests for the ``POST /api/jobs/{id}/feedback`` route + feedback-rerun plumbing (US-007).

US-007 added a chat-driven feedback loop on top of the existing job
system: the user reviews a completed cut, types free-text feedback,
and the backend spins up a new ``feedback-rerun`` job that inherits the
parent's brief / footage / pipeline context and re-runs
Director -> trim_refiner -> editor -> reviewer with the accumulated
feedback history.

These tests follow the pattern established in
:mod:`tests.test_web_jobs_review` and :mod:`tests.test_web_ws`:

* Use :class:`fastapi.testclient.TestClient` with the real app lifespan
  so :class:`JobRegistry` is attached to ``app.state.job_registry``.
* Seed jobs directly into ``registry._jobs`` so we never touch
  ``run_pipeline`` or the real Gemini-backed agents.
* Exercise :meth:`JobRegistry.submit_feedback_rerun` and the
  :class:`Job` dataclass fields directly for the in-process unit tests.
* For ``_run_feedback_rerun_sync`` coverage, monkeypatch the four agent
  helpers at the ``src.web.jobs`` module level so the worker path can
  run deterministically without spawning a worker loop at all.

Test layout:

1. ``POST`` returns 404 for an unknown parent.
2. ``POST`` returns 409 when the parent is still running / pending.
3. ``POST`` returns 409 when the parent completed but has no
   ``edit_plan`` in its result.
4. ``POST`` returns 202 + the expected body on the happy path, with the
   child job landing in the registry with inherited context and an
   accumulated feedback history.
5. ``POST`` with an empty / whitespace-only message returns 422 from
   pydantic's ``min_length`` validator.
6. ``POST`` with a message longer than ``max_length`` returns 422.
7. Feedback accumulation chains correctly across multiple rounds via
   direct :meth:`JobRegistry.submit_feedback_rerun` calls (no HTTP
   layer) -- the second child's history includes every earlier message
   in chronological order.
8. :meth:`Job.summary` exposes ``job_type`` + ``parent_job_id``.
9. :meth:`Job.to_dict` exposes ``feedback_history``.
10. :meth:`JobRegistry._run_feedback_rerun_sync` happy path with the
    four agent helpers stubbed: returns a populated
    :class:`PipelineResult` whose ``feedback_history`` echoes the job's
    accumulated history, and emits the four ``[feedback-rerun] step N``
    framing lines on the job's progress log.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from src.models.schemas import CreativeBrief, EditPlan, ReviewScore
from src.pipeline.runner import PipelineResult
from src.web import jobs as jobs_module
from src.web.app import app
from src.web.jobs import Job, JobRegistry


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #


def _make_brief() -> CreativeBrief:
    """Minimal brief identical to the one used in the sibling test files."""
    return CreativeBrief(
        product="test-product",
        audience="test-audience",
        tone="energetic",
        duration_seconds=15,
    )


def _edit_plan_payload() -> dict:
    """Serialized :class:`EditPlan` for a parent job's result blob.

    The feedback route only checks that ``result['edit_plan']`` is
    non-None, so we build a structurally valid EditPlan dict so the
    result passes both the route's 409 gate and any later
    ``model_dump`` comparisons.
    """
    return EditPlan(
        brief=_make_brief(),
        entries=[],
        music_path=None,
        total_duration=15.0,
    ).model_dump()


def _make_completed_parent(
    job_id: str,
    *,
    feedback_history: list[str] | None = None,
    job_feedback_history: list[str] | None = None,
) -> Job:
    """Construct a completed parent :class:`Job` ready for a feedback re-run.

    ``feedback_history`` lands in the serialized ``result`` blob (where
    the real :func:`run_pipeline` stores it). ``job_feedback_history``
    lands on the ``Job.feedback_history`` attribute -- the second chat
    round and onwards reads from there instead of the result blob.
    """
    return Job(
        id=job_id,
        status="completed",
        brief=_make_brief(),
        footage_index_path="/tmp/footage.json",
        pipeline_path="/tmp/pipeline.yaml",
        job_type="full-pipeline",
        parent_job_id=None,
        feedback_history=list(job_feedback_history or []),
        result={
            "edit_plan": _edit_plan_payload(),
            "final_video_path": "/tmp/parent.mp4",
            "review": None,
            "retries_used": 0,
            "warnings": [],
            "feedback_history": list(feedback_history or []),
        },
    )


@pytest.fixture()
def client():
    """FastAPI TestClient bound to a lifespan that spins up the registry."""
    with TestClient(app) as test_client:
        yield test_client


# --------------------------------------------------------------------- #
# HTTP contract tests
# --------------------------------------------------------------------- #


def test_post_feedback_returns_404_for_unknown_parent(client: TestClient) -> None:
    """A random parent id returns 404 with the id in the detail string.

    No seeding happens before the request -- the registry is empty at
    lifespan startup and the unknown id never appears in ``_jobs``.
    Matches the 404 shape used by the review endpoint.
    """
    unknown_id = str(uuid.uuid4())

    response = client.post(
        f"/api/jobs/{unknown_id}/feedback",
        json={"message": "tighten the middle"},
    )

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert unknown_id in detail


def test_post_feedback_returns_409_for_pending_parent(client: TestClient) -> None:
    """A parent that is still pending / running returns 409 Conflict.

    The re-run path needs a finished :class:`EditPlan` to feed back
    into the Director, which only exists once the parent reaches the
    ``completed`` state. We seed a parent stuck in ``pending`` with no
    result yet to exercise the first gate in
    :meth:`JobRegistry.submit_feedback_rerun`.
    """
    registry: JobRegistry = client.app.state.job_registry
    parent = Job(
        id="feedback-parent-pending",
        status="pending",
        brief=_make_brief(),
        footage_index_path="/tmp/footage.json",
        pipeline_path="/tmp/pipeline.yaml",
    )
    registry._jobs[parent.id] = parent

    response = client.post(
        f"/api/jobs/{parent.id}/feedback",
        json={"message": "swap the intro clip"},
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "not completed" in detail
    assert "pending" in detail


def test_post_feedback_returns_409_for_completed_parent_without_plan(
    client: TestClient,
) -> None:
    """A completed parent whose result is missing ``edit_plan`` returns 409.

    This models the (admittedly rare) case where the pipeline finished
    but did not produce a plan -- e.g. a pre-Director abort where the
    job still flipped to ``completed`` because of a custom pipeline
    that only ran preprocessing. The feedback route cannot do anything
    useful without a plan, so the gate fires.
    """
    registry: JobRegistry = client.app.state.job_registry
    parent = Job(
        id="feedback-parent-no-plan",
        status="completed",
        brief=_make_brief(),
        footage_index_path="/tmp/footage.json",
        pipeline_path="/tmp/pipeline.yaml",
        result={
            "edit_plan": None,
            "final_video_path": "/tmp/parent.mp4",
            "review": None,
            "retries_used": 0,
            "warnings": [],
            "feedback_history": [],
        },
    )
    registry._jobs[parent.id] = parent

    response = client.post(
        f"/api/jobs/{parent.id}/feedback",
        json={"message": "more pacing"},
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "edit_plan" in detail


def test_post_feedback_202_creates_feedback_rerun_child(
    client: TestClient,
) -> None:
    """Happy path: completed parent + new message returns 202 + child id.

    Asserts the response envelope the frontend relies on:

    * ``job_id`` differs from the parent id.
    * ``status`` is ``pending`` (the worker has not picked it up yet).
    * ``parent_job_id`` echoes the parent.

    Also assert the child landed in the registry with the correct
    inherited context and an accumulated ``feedback_history`` that
    carries the parent's reviewer history plus the new user message.
    """
    registry: JobRegistry = client.app.state.job_registry
    parent = _make_completed_parent(
        "feedback-parent-happy",
        feedback_history=["reviewer: tighten pacing"],
    )
    registry._jobs[parent.id] = parent

    response = client.post(
        f"/api/jobs/{parent.id}/feedback",
        json={"message": "swap the hero shot"},
    )

    assert response.status_code == 202
    body = response.json()
    assert set(body.keys()) == {"job_id", "status", "parent_job_id"}
    assert body["parent_job_id"] == parent.id
    assert body["status"] == "pending"
    assert body["job_id"] != parent.id

    child = registry.get(body["job_id"])
    assert child is not None
    assert child.job_type == "feedback-rerun"
    assert child.parent_job_id == parent.id
    assert child.brief == parent.brief
    assert child.footage_index_path == parent.footage_index_path
    assert child.pipeline_path == parent.pipeline_path
    # The child history is the parent's reviewer feedback plus the new
    # user message appended, in order. This is the contract the
    # Director call downstream depends on.
    assert child.feedback_history == [
        "reviewer: tighten pacing",
        "swap the hero shot",
    ]


def test_post_feedback_empty_message_returns_422(client: TestClient) -> None:
    """Whitespace-only / empty messages fail pydantic ``min_length=1``.

    We send two payloads -- literal empty string and whitespace-only --
    and assert both are rejected by validation before the route even
    runs. The whitespace case is a regression guard: if someone later
    swaps ``min_length`` for a ``strip``-then-check pattern, the
    whitespace form should still fail because the frontend should
    never submit it.

    Note: the literal ``""`` case is the one pydantic's ``min_length=1``
    definitively catches. The whitespace-only case is caught by the
    server-side ``strip()`` check in :meth:`submit_feedback_rerun` and
    surfaces as a 409 instead -- we accept either rejection code below.
    """
    registry: JobRegistry = client.app.state.job_registry
    parent = _make_completed_parent("feedback-parent-empty")
    registry._jobs[parent.id] = parent

    # Empty literal string -- blocked by min_length=1.
    response_empty = client.post(
        f"/api/jobs/{parent.id}/feedback",
        json={"message": ""},
    )
    assert response_empty.status_code == 422

    # Whitespace-only -- blocked either by pydantic (if min_length is
    # later tightened to strip-then-check) or by the ``strip()`` guard
    # inside ``submit_feedback_rerun`` (409 with "must not be empty").
    response_ws = client.post(
        f"/api/jobs/{parent.id}/feedback",
        json={"message": "   \n\t  "},
    )
    assert response_ws.status_code in (409, 422)
    if response_ws.status_code == 409:
        assert "empty" in response_ws.json()["detail"]


def test_post_feedback_oversized_message_returns_422(client: TestClient) -> None:
    """Messages longer than ``max_length=4000`` fail pydantic validation.

    This caps the blast radius of pasted logs, runaway prompts, or a
    hostile client dumping MB of text -- the Director call would
    tokenize the whole thing otherwise.
    """
    registry: JobRegistry = client.app.state.job_registry
    parent = _make_completed_parent("feedback-parent-oversized")
    registry._jobs[parent.id] = parent

    oversized = "x" * 4001
    response = client.post(
        f"/api/jobs/{parent.id}/feedback",
        json={"message": oversized},
    )

    assert response.status_code == 422


# --------------------------------------------------------------------- #
# Registry-level feedback accumulation
# --------------------------------------------------------------------- #


def test_feedback_history_accumulates_across_rounds() -> None:
    """Direct :meth:`submit_feedback_rerun` across three chat rounds.

    The HTTP layer only wires one round at a time, so we exercise the
    accumulation logic against the registry directly:

    1. Parent finishes with ``result['feedback_history']`` populated by
       the reviewer retry loop.
    2. First child inherits the reviewer history, appends the user's
       round-1 message, and lands in the registry.
    3. We simulate the worker completing child1 (so its own
       ``feedback_history`` attribute is the source of truth going
       forward, not the result blob).
    4. Second child inherits child1's full history and appends round 2.
    5. Assert the round-2 child's history contains every prior entry
       in chronological order.

    This keeps the integration test focused on the accumulation rule
    without having to spin up the worker queue.
    """
    registry = JobRegistry()
    parent = _make_completed_parent(
        "accum-parent",
        feedback_history=["reviewer: pacing too slow"],
    )
    registry._jobs[parent.id] = parent

    child1 = registry.submit_feedback_rerun(parent, "swap the hero shot")
    assert child1.feedback_history == [
        "reviewer: pacing too slow",
        "swap the hero shot",
    ]

    # Simulate worker completion -- the child is now a "parent" for
    # the next round. Its attribute-level ``feedback_history`` is what
    # the next submit call should read, not the result blob.
    child1.status = "completed"
    child1.result = {
        "edit_plan": _edit_plan_payload(),
        "final_video_path": "/tmp/child1.mp4",
        "review": None,
        "retries_used": 0,
        "warnings": [],
        # Intentionally DIFFERENT from the attribute history to prove
        # the attribute wins for non-first-round parents.
        "feedback_history": ["should-not-be-used"],
    }

    child2 = registry.submit_feedback_rerun(child1, "punch up the outro music")
    assert child2.feedback_history == [
        "reviewer: pacing too slow",
        "swap the hero shot",
        "punch up the outro music",
    ]
    assert child2.parent_job_id == child1.id
    assert child2.job_type == "feedback-rerun"


# --------------------------------------------------------------------- #
# Job dataclass serialization
# --------------------------------------------------------------------- #


def test_job_summary_includes_job_type_and_parent_job_id() -> None:
    """``Job.summary`` must expose the new ``job_type`` + ``parent_job_id``.

    The list view on the frontend uses ``job_type`` to label
    feedback-rerun rows differently and ``parent_job_id`` to link them
    back to the row they revise. If either key disappears, the
    ``GET /api/jobs`` response silently loses the chat lineage metadata.
    """
    job = Job(
        id="summary-child",
        status="pending",
        brief=_make_brief(),
        footage_index_path="/tmp/footage.json",
        pipeline_path="/tmp/pipeline.yaml",
        job_type="feedback-rerun",
        parent_job_id="summary-parent",
        feedback_history=["reviewer: tighten"],
    )

    summary = job.summary()

    assert summary["job_type"] == "feedback-rerun"
    assert summary["parent_job_id"] == "summary-parent"
    # Sanity: the pre-existing summary keys still exist so we have not
    # accidentally regressed the rest of the payload.
    assert summary["id"] == "summary-child"
    assert summary["status"] == "pending"


def test_job_to_dict_includes_feedback_history() -> None:
    """``Job.to_dict`` returns a snapshot of the accumulated feedback.

    The detail view reads the full ``feedback_history`` list so the
    chat transcript can show every round the job has seen. The
    attribute is a ``list[str]`` but ``to_dict`` must call ``list()``
    so later mutations by the worker thread do not leak into the
    already-serialized response body.
    """
    history = ["reviewer: pacing", "user: swap hero shot"]
    job = Job(
        id="todict-job",
        status="completed",
        brief=_make_brief(),
        footage_index_path="/tmp/footage.json",
        pipeline_path="/tmp/pipeline.yaml",
        job_type="feedback-rerun",
        parent_job_id="todict-parent",
        feedback_history=history,
        result={"final_video_path": "/tmp/out.mp4"},
    )

    payload = job.to_dict()

    assert payload["feedback_history"] == history
    assert payload["job_type"] == "feedback-rerun"
    assert payload["parent_job_id"] == "todict-parent"
    # Snapshot semantics: mutating the original list does NOT update
    # the already-serialized payload.
    history.append("post-serialize")
    assert payload["feedback_history"] == [
        "reviewer: pacing",
        "user: swap hero shot",
    ]


# --------------------------------------------------------------------- #
# Worker-thread feedback-rerun happy path (stubbed agents)
# --------------------------------------------------------------------- #


def test_run_feedback_rerun_sync_invokes_agents_and_emits_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_run_feedback_rerun_sync`` calls all four agents and frames progress.

    We monkeypatch the four helpers imported at the top of
    :mod:`src.web.jobs` so the happy path runs without touching
    Gemini, ffmpeg, or the filesystem. ``_with_transient_retry`` is a
    plain wrapper that invokes the function once on success, so the
    stubs flow through it unchanged.

    Assertions:

    * Each of the four helpers is called exactly once with the
      expected args derived from the job's inherited context + the
      accumulated feedback joined with ``\\n\\n``.
    * The returned :class:`PipelineResult` carries the refined plan,
      the stubbed video path, and the stubbed review.
    * The returned result's ``feedback_history`` matches the job's
      attribute-level history (not the parent's result blob).
    * The job's progress log contains all four
      ``[feedback-rerun] step N`` framing lines in order -- the UI's
      step indicator depends on these strings.
    """
    # Arrange: seed a feedback-rerun job by hand so we don't need the
    # worker loop at all. The registry method already has unit
    # coverage above; here we focus purely on the worker-side sequence.
    job = Job(
        id="rerun-happy-path",
        status="pending",
        brief=_make_brief(),
        footage_index_path="/tmp/footage.json",
        pipeline_path="/tmp/pipeline.yaml",
        job_type="feedback-rerun",
        parent_job_id="rerun-parent",
        feedback_history=["reviewer: pacing", "user: swap hero shot"],
    )

    stub_plan = EditPlan(
        brief=_make_brief(),
        entries=[],
        music_path=None,
        total_duration=15.0,
    )
    stub_refined = EditPlan(
        brief=_make_brief(),
        entries=[],
        music_path=None,
        total_duration=14.0,
    )
    stub_video_path = "/tmp/rerun-final.mp4"
    stub_review = ReviewScore(
        adherence=0.9,
        pacing=0.85,
        visual_quality=0.8,
        watchability=0.92,
        overall=0.87,
        feedback="looks better",
    )

    director_calls: list[dict] = []
    refine_calls: list[tuple] = []
    editor_calls: list[tuple] = []
    reviewer_calls: list[tuple] = []

    def _fake_director(brief, footage_index_path, *, feedback):
        director_calls.append(
            {
                "brief": brief,
                "footage_index_path": footage_index_path,
                "feedback": feedback,
            }
        )
        return stub_plan

    def _fake_refine(plan, footage_index_path):
        refine_calls.append((plan, footage_index_path))
        return stub_refined

    def _fake_editor(plan, footage_index_path):
        editor_calls.append((plan, footage_index_path))
        return stub_video_path

    def _fake_reviewer(brief, video_path):
        reviewer_calls.append((brief, video_path))
        return stub_review

    monkeypatch.setattr(jobs_module, "_run_director_with_feedback", _fake_director)
    monkeypatch.setattr(jobs_module, "refine_plan", _fake_refine)
    monkeypatch.setattr(jobs_module, "run_editor", _fake_editor)
    monkeypatch.setattr(jobs_module, "run_reviewer", _fake_reviewer)

    # Act: drive the re-run sync method directly, same as the worker
    # would (minus the stdout redirection, which we don't need for the
    # explicit ``_record_progress`` framing lines we're asserting on).
    registry = JobRegistry()
    result = registry._run_feedback_rerun_sync(job)

    # Assert: agents were called once each with the expected args.
    assert len(director_calls) == 1
    director_call = director_calls[0]
    assert director_call["brief"] == job.brief
    assert director_call["footage_index_path"] == job.footage_index_path
    assert director_call["feedback"] == "reviewer: pacing\n\nuser: swap hero shot"

    assert len(refine_calls) == 1
    assert refine_calls[0] == (stub_plan, job.footage_index_path)

    assert len(editor_calls) == 1
    assert editor_calls[0] == (stub_refined, job.footage_index_path)

    assert len(reviewer_calls) == 1
    assert reviewer_calls[0] == (job.brief, stub_video_path)

    # Assert: the returned PipelineResult has the expected payload and
    # echoes the job's feedback history (so the UI can show every
    # prior round in the detail view).
    assert isinstance(result, PipelineResult)
    assert result.edit_plan is stub_refined
    assert result.final_video_path == stub_video_path
    assert result.review is stub_review
    assert result.retries_used == 0
    assert result.warnings == []
    assert result.feedback_history == [
        "reviewer: pacing",
        "user: swap hero shot",
    ]

    # Assert: the four framing lines landed in the progress log in
    # order. We strip the ``[<iso timestamp>] `` prefix that
    # ``_record_progress`` adds so the assertion is stable against
    # the actual clock values.
    framing = [
        line.split("] ", 1)[-1]
        for line in job.progress_log
        if "[feedback-rerun]" in line
    ]
    assert framing == [
        "[feedback-rerun] step 1 -- director (with feedback, history_len=2)",
        "[feedback-rerun] step 2 -- trim_refiner",
        "[feedback-rerun] step 3 -- editor",
        "[feedback-rerun] step 4 -- reviewer",
    ]
