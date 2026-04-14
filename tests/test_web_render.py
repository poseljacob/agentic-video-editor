"""Tests for the US-010 editor-only + reviewer-only job routes.

Covers the two endpoints wired by :mod:`src.web.routes.render`:

* ``POST /api/jobs/{job_id}/re-render`` -- validates a user-edited
  :class:`EditPlan` against the parent job's
  :class:`~src.models.schemas.FootageIndex` and spawns an
  ``editor-only`` child that invokes :func:`run_editor` directly
  (no Director, no Reviewer).
* ``POST /api/jobs/{job_id}/review-only`` -- spawns a ``reviewer-only``
  child that runs :func:`run_reviewer` against the parent's latest
  rendered MP4 without re-rendering anything.

Plus direct coverage of the worker-side sync methods
:meth:`JobRegistry._run_editor_only_sync` and
:meth:`JobRegistry._run_reviewer_only_sync`, monkeypatching the two
agent helpers so the happy path runs deterministically without
touching Gemini or ffmpeg.

Strategy mirrors the sibling
:mod:`tests.test_web_feedback` test module:

* :class:`fastapi.testclient.TestClient` with the real lifespan so
  :class:`JobRegistry` is attached to ``app.state.job_registry``.
* Seed jobs directly into ``registry._jobs`` so the sequential worker
  never picks them up (we want the route + registry gates exercised in
  isolation from actual worker execution).
* Write a real :class:`FootageIndex` to ``tmp_path`` so the PUT
  validation helpers
  (:func:`_load_footage_index_or_422`,
  :func:`_validate_edit_plan_against_index`) reused by the re-render
  route actually load something real -- the happy path otherwise
  hinges on disk I/O that the feedback tests never needed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.models.schemas import (
    CreativeBrief,
    EditPlan,
    FootageIndex,
    ReviewScore,
    Shot,
)
from src.pipeline.runner import PipelineResult
from src.web import jobs as jobs_module
from src.web.app import app
from src.web.jobs import Job, JobRegistry


# --------------------------------------------------------------------- #
# Helpers -- shaped to match tests/test_web_clips.py + test_web_feedback
# --------------------------------------------------------------------- #


def _make_brief(product: str = "render-test-product") -> CreativeBrief:
    """Minimal :class:`CreativeBrief` that passes pydantic validation."""
    return CreativeBrief(
        product=product,
        audience="test-audience",
        tone="energetic",
        duration_seconds=15,
    )


def _make_shot(
    source_file: str,
    start_time: float,
    end_time: float,
    roll_type: str = "a-roll",
) -> Shot:
    """Construct a :class:`Shot` with the fields routes + validators read."""
    return Shot(
        source_file=source_file,
        start_time=start_time,
        end_time=end_time,
        description="fixture shot",
        energy_level=5,
        relevance_score=0.5,
        transcript="",
        words=[],
        roll_type=roll_type,
    )


def _make_footage_index(shots: list[Shot]) -> FootageIndex:
    """Wrap ``shots`` in a :class:`FootageIndex` with a sensible total."""
    return FootageIndex(
        source_dir="/tmp/fixture-footage",
        shots=shots,
        total_duration=sum(s.end_time - s.start_time for s in shots),
        created_at=datetime.now(timezone.utc),
    )


def _write_footage_index(index: FootageIndex, tmp_path: Path) -> str:
    """Serialize ``index`` to a tmp JSON file and return its path."""
    path = tmp_path / "footage.json"
    path.write_text(index.model_dump_json())
    return str(path)


def _entry(
    shot: Shot,
    position: int,
    start_trim: float | None = None,
    end_trim: float | None = None,
) -> dict:
    """Build a serialized ``EditPlanEntry`` dict pointing at ``shot``."""
    return {
        "shot_id": f"{shot.source_file}#{shot.start_time}",
        "start_trim": shot.start_time if start_trim is None else start_trim,
        "end_trim": shot.end_time if end_trim is None else end_trim,
        "position": position,
        "text_overlay": None,
        "transition": None,
    }


def _make_edit_plan_payload(
    brief: CreativeBrief,
    entries: list[dict],
    total_duration: float,
) -> dict:
    """Build the serialized EditPlan dict the route accepts as its body."""
    return {
        "brief": brief.model_dump(),
        "entries": entries,
        "music_path": None,
        "total_duration": total_duration,
    }


def _seed_completed_parent(
    registry: JobRegistry,
    *,
    job_id: str,
    brief: CreativeBrief,
    footage_index_path: str,
    edit_plan: dict,
    final_video_path: str,
) -> Job:
    """Seed a completed parent :class:`Job` directly into ``registry._jobs``.

    The sequential worker never sees this job because we bypass
    :meth:`JobRegistry.submit` -- we just want the registry to contain
    a parent that the re-render / review-only routes can hang a child
    off of.
    """
    job = Job(
        id=job_id,
        status="completed",
        brief=brief,
        footage_index_path=footage_index_path,
        pipeline_path="/tmp/pipeline.yaml",
        result={
            "edit_plan": edit_plan,
            "final_video_path": final_video_path,
            "review": None,
            "retries_used": 0,
            "warnings": [],
            "feedback_history": [],
        },
    )
    registry._jobs[job_id] = job
    return job


@pytest.fixture()
def client():
    """FastAPI TestClient bound to a lifespan that spins up the registry."""
    with TestClient(app) as test_client:
        yield test_client


# --------------------------------------------------------------------- #
# POST /api/jobs/{id}/re-render
# --------------------------------------------------------------------- #


def test_re_render_returns_404_for_unknown_parent(client: TestClient) -> None:
    """A random parent id returns 404 before any plan validation runs.

    The registry is empty at lifespan startup, so a fresh UUID is
    guaranteed to miss the lookup. The route must short-circuit with
    the id echoed in the detail string to match the sibling feedback
    endpoint.
    """
    unknown_id = str(uuid.uuid4())
    response = client.post(
        f"/api/jobs/{unknown_id}/re-render",
        json={
            "brief": _make_brief().model_dump(),
            "entries": [],
            "music_path": None,
            "total_duration": 15.0,
        },
    )
    assert response.status_code == 404
    assert unknown_id in response.json()["detail"]


def test_re_render_returns_409_when_parent_has_no_result(
    client: TestClient,
) -> None:
    """A parent that is still pending / running returns 409 Conflict.

    Re-rendering requires the parent to have a completed result so
    the child inherits the right context. We seed a pending parent
    with no result to exercise the first gate in the route (not the
    registry -- we want the route's own 409 short-circuit tested).
    """
    registry: JobRegistry = client.app.state.job_registry
    parent = Job(
        id="render-parent-pending",
        status="pending",
        brief=_make_brief(),
        footage_index_path="/tmp/footage.json",
        pipeline_path="/tmp/pipeline.yaml",
    )
    registry._jobs[parent.id] = parent

    response = client.post(
        f"/api/jobs/{parent.id}/re-render",
        json={
            "brief": _make_brief().model_dump(),
            "entries": [],
            "music_path": None,
            "total_duration": 15.0,
        },
    )
    assert response.status_code == 409
    assert "no result" in response.json()["detail"]


def test_re_render_returns_422_for_schema_invalid_body(
    client: TestClient, tmp_path: Path
) -> None:
    """Bad-shape body fails at layer 1 (pydantic ``model_validate``).

    The parent has a valid result so the 409 gate passes; the body is
    missing required fields (``brief``, ``entries``, etc.) so pydantic
    kicks it back. We assert 422 and that the response body carries
    the pydantic-style error list the frontend already knows how to
    render.
    """
    registry: JobRegistry = client.app.state.job_registry
    shot = _make_shot("/tmp/footage/intro.mp4", 0.0, 3.0)
    index = _make_footage_index([shot])
    footage_path = _write_footage_index(index, tmp_path)
    brief = _make_brief()
    parent = _seed_completed_parent(
        registry,
        job_id="render-parent-schema",
        brief=brief,
        footage_index_path=footage_path,
        edit_plan=_make_edit_plan_payload(brief, [_entry(shot, 0)], 3.0),
        final_video_path="/tmp/parent.mp4",
    )

    response = client.post(
        f"/api/jobs/{parent.id}/re-render",
        json={"nope": "not an edit plan"},
    )
    assert response.status_code == 422
    # FastAPI error shape: list of dicts with 'loc'/'msg'/'type'.
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    assert any("brief" in err.get("loc", []) for err in detail)


def test_re_render_returns_422_for_unresolvable_shot(
    client: TestClient, tmp_path: Path
) -> None:
    """Schema-valid body with an unknown shot_id fails at layer 2/3.

    The pydantic coercion passes (the body is a valid EditPlan
    structurally), but the shot_id points at a source file that does
    not appear in the parent's footage index. The validation helpers
    imported from :mod:`src.web.routes.jobs` should catch this and
    return a 422 with a per-field ``shot_not_found`` error.
    """
    registry: JobRegistry = client.app.state.job_registry
    shot = _make_shot("/tmp/footage/intro.mp4", 0.0, 3.0)
    index = _make_footage_index([shot])
    footage_path = _write_footage_index(index, tmp_path)
    brief = _make_brief()
    parent = _seed_completed_parent(
        registry,
        job_id="render-parent-unresolved",
        brief=brief,
        footage_index_path=footage_path,
        edit_plan=_make_edit_plan_payload(brief, [_entry(shot, 0)], 3.0),
        final_video_path="/tmp/parent.mp4",
    )

    bogus_entry = {
        "shot_id": "/tmp/footage/never-existed.mp4#99.9",
        "start_trim": 0.0,
        "end_trim": 1.0,
        "position": 0,
        "text_overlay": None,
        "transition": None,
    }
    response = client.post(
        f"/api/jobs/{parent.id}/re-render",
        json=_make_edit_plan_payload(brief, [bogus_entry], 1.0),
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    # At least one error must target the offending shot_id field and
    # carry the shot_not_found type from the validation helpers.
    assert any(
        err.get("type") == "value_error.shot_not_found"
        and "shot_id" in err.get("loc", [])
        for err in detail
    )


def test_re_render_happy_path_creates_editor_only_child(
    client: TestClient, tmp_path: Path
) -> None:
    """Happy path: 202 + editor-only child lands in the registry.

    Asserts:

    * The response envelope matches ``{job_id, status, parent_job_id}``
      so the frontend can subscribe to ``/ws/jobs/{new_id}`` without
      parsing any additional metadata.
    * The child :class:`Job` is in the registry with
      ``job_type="editor-only"``, ``parent_job_id`` pointing at the
      parent, and ``_editor_plan`` populated with the validated
      :class:`EditPlan` instance.
    * The parent's ``result["edit_plan"]`` is **unchanged** -- the
      whole point of re-render (vs PUT) is that the previous render
      stays available so the frontend can build a version list.
    """
    registry: JobRegistry = client.app.state.job_registry
    shot_a = _make_shot("/tmp/footage/intro.mp4", 0.0, 4.0)
    shot_b = _make_shot("/tmp/footage/outro.mp4", 2.0, 6.0)
    index = _make_footage_index([shot_a, shot_b])
    footage_path = _write_footage_index(index, tmp_path)
    brief = _make_brief()
    original_entries = [_entry(shot_a, 0), _entry(shot_b, 1)]
    parent = _seed_completed_parent(
        registry,
        job_id="render-parent-happy",
        brief=brief,
        footage_index_path=footage_path,
        edit_plan=_make_edit_plan_payload(
            brief, original_entries, 8.0
        ),
        final_video_path="/tmp/parent-final.mp4",
    )
    original_plan_snapshot = dict(parent.result["edit_plan"])

    # Submit a MODIFIED plan: reverse the order and tighten the trims.
    modified_entries = [
        _entry(shot_b, 0, start_trim=2.5, end_trim=5.5),
        _entry(shot_a, 1, start_trim=0.5, end_trim=3.5),
    ]
    response = client.post(
        f"/api/jobs/{parent.id}/re-render",
        json=_make_edit_plan_payload(brief, modified_entries, 6.0),
    )
    assert response.status_code == 202
    body = response.json()
    assert set(body.keys()) == {"job_id", "status", "parent_job_id"}
    assert body["parent_job_id"] == parent.id
    assert body["status"] == "pending"
    assert body["job_id"] != parent.id

    child = registry.get(body["job_id"])
    assert child is not None
    assert child.job_type == "editor-only"
    assert child.parent_job_id == parent.id
    assert child.brief == parent.brief
    assert child.footage_index_path == parent.footage_index_path
    assert child.pipeline_path == parent.pipeline_path
    # The modified plan should be attached verbatim to the child.
    assert isinstance(child._editor_plan, EditPlan)
    assert len(child._editor_plan.entries) == 2
    assert child._editor_plan.entries[0].position == 0
    assert child._editor_plan.entries[0].start_trim == 2.5
    assert child._editor_plan.total_duration == 6.0

    # Parent preservation: its result.edit_plan dict is untouched by
    # the re-render route so previous renders remain addressable.
    assert parent.result["edit_plan"] == original_plan_snapshot
    assert parent.result["final_video_path"] == "/tmp/parent-final.mp4"


# --------------------------------------------------------------------- #
# POST /api/jobs/{id}/review-only
# --------------------------------------------------------------------- #


def test_review_only_returns_404_for_unknown_parent(
    client: TestClient,
) -> None:
    """Unknown parent id short-circuits to 404 before touching the registry."""
    unknown_id = str(uuid.uuid4())
    response = client.post(f"/api/jobs/{unknown_id}/review-only")
    assert response.status_code == 404
    assert unknown_id in response.json()["detail"]


def test_review_only_returns_409_when_parent_has_no_final_video(
    client: TestClient,
) -> None:
    """A completed parent with ``final_video_path=None`` fails the gate.

    The reviewer needs an actual MP4 to score, so
    :meth:`JobRegistry.submit_reviewer_only` raises ``ValueError``
    which the route maps to 409. We seed a parent with a result blob
    that omits the video path.
    """
    registry: JobRegistry = client.app.state.job_registry
    parent = Job(
        id="review-parent-no-video",
        status="completed",
        brief=_make_brief(),
        footage_index_path="/tmp/footage.json",
        pipeline_path="/tmp/pipeline.yaml",
        result={
            "edit_plan": None,
            "final_video_path": None,
            "review": None,
            "retries_used": 0,
            "warnings": [],
            "feedback_history": [],
        },
    )
    registry._jobs[parent.id] = parent

    response = client.post(f"/api/jobs/{parent.id}/review-only")
    assert response.status_code == 409
    assert "final_video_path" in response.json()["detail"]


def test_review_only_returns_409_when_final_video_missing_from_disk(
    client: TestClient,
) -> None:
    """Parent has a path but the file is gone -> 409.

    This guards the common case where an output directory got pruned
    between the parent run and the reviewer-only request. The worker
    would fail anyway; we prefer to fail fast in the registry gate
    with a descriptive message.
    """
    registry: JobRegistry = client.app.state.job_registry
    parent = Job(
        id="review-parent-ghost-video",
        status="completed",
        brief=_make_brief(),
        footage_index_path="/tmp/footage.json",
        pipeline_path="/tmp/pipeline.yaml",
        result={
            "edit_plan": None,
            "final_video_path": "/tmp/definitely-not-there.mp4",
            "review": None,
            "retries_used": 0,
            "warnings": [],
            "feedback_history": [],
        },
    )
    registry._jobs[parent.id] = parent

    response = client.post(f"/api/jobs/{parent.id}/review-only")
    assert response.status_code == 409
    assert "does not exist" in response.json()["detail"]


def test_review_only_happy_path_creates_reviewer_only_child(
    client: TestClient, tmp_path: Path
) -> None:
    """Happy path: 202 + reviewer-only child lands in the registry.

    Uses a tmp file as the stand-in for the rendered MP4 so the
    on-disk existence check inside
    :meth:`JobRegistry.submit_reviewer_only` passes without us having
    to monkeypatch filesystem access.
    """
    registry: JobRegistry = client.app.state.job_registry
    fake_video = tmp_path / "fake-final.mp4"
    fake_video.write_bytes(b"not a real mp4 but exists on disk")
    parent = Job(
        id="review-parent-happy",
        status="completed",
        brief=_make_brief(),
        footage_index_path="/tmp/footage.json",
        pipeline_path="/tmp/pipeline.yaml",
        result={
            "edit_plan": None,
            "final_video_path": str(fake_video),
            "review": None,
            "retries_used": 0,
            "warnings": [],
            "feedback_history": [],
        },
    )
    registry._jobs[parent.id] = parent

    response = client.post(f"/api/jobs/{parent.id}/review-only")
    assert response.status_code == 202
    body = response.json()
    assert set(body.keys()) == {"job_id", "status", "parent_job_id"}
    assert body["parent_job_id"] == parent.id
    assert body["status"] == "pending"

    child = registry.get(body["job_id"])
    assert child is not None
    assert child.job_type == "reviewer-only"
    assert child.parent_job_id == parent.id
    assert child.brief == parent.brief
    assert child._reviewer_target_video == str(fake_video)


# --------------------------------------------------------------------- #
# Worker-thread editor-only happy path (stubbed agent)
# --------------------------------------------------------------------- #


def test_run_editor_only_sync_calls_editor_and_emits_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_run_editor_only_sync`` calls :func:`run_editor` and frames progress.

    We monkeypatch ``run_editor`` at the :mod:`src.web.jobs` module
    level so the sync path never touches ffmpeg. Assertions:

    * :func:`run_editor` is called once with the stashed
      ``_editor_plan`` and the job's inherited ``footage_index_path``.
    * The returned :class:`PipelineResult` carries the same plan, the
      stubbed video path, ``review=None``, and the job's feedback
      history (inherited from the parent).
    * The job's progress log contains the
      ``[editor-only] step 1 -- editor`` framing line the UI's step
      indicator depends on.
    """
    plan = EditPlan(
        brief=_make_brief(),
        entries=[],
        music_path=None,
        total_duration=10.0,
    )
    job = Job(
        id="editor-only-happy",
        status="pending",
        brief=_make_brief(),
        footage_index_path="/tmp/footage.json",
        pipeline_path="/tmp/pipeline.yaml",
        job_type="editor-only",
        parent_job_id="editor-only-parent",
        feedback_history=["reviewer: tighten"],
    )
    job._editor_plan = plan

    editor_calls: list[tuple] = []

    def _fake_editor(edit_plan, footage_index_path):
        editor_calls.append((edit_plan, footage_index_path))
        return "/tmp/rerender-out.mp4"

    monkeypatch.setattr(jobs_module, "run_editor", _fake_editor)

    registry = JobRegistry()
    result = registry._run_editor_only_sync(job)

    assert len(editor_calls) == 1
    assert editor_calls[0] == (plan, job.footage_index_path)

    assert isinstance(result, PipelineResult)
    assert result.edit_plan is plan
    assert result.final_video_path == "/tmp/rerender-out.mp4"
    assert result.review is None
    assert result.retries_used == 0
    assert result.warnings == []
    assert result.feedback_history == ["reviewer: tighten"]

    # Strip the ``[<iso>] `` prefix and confirm our framing line is
    # present in order.
    framing = [
        line.split("] ", 1)[-1]
        for line in job.progress_log
        if "[editor-only]" in line
    ]
    assert framing == ["[editor-only] step 1 -- editor"]


# --------------------------------------------------------------------- #
# Worker-thread reviewer-only happy path (stubbed agent)
# --------------------------------------------------------------------- #


def test_run_reviewer_only_sync_calls_reviewer_and_emits_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_run_reviewer_only_sync`` calls :func:`run_reviewer` and frames progress.

    Monkeypatches ``run_reviewer`` on :mod:`src.web.jobs` so the sync
    path returns a deterministic :class:`ReviewScore`. Asserts:

    * :func:`run_reviewer` is called once with the job's brief and the
      stashed ``_reviewer_target_video`` path.
    * The returned :class:`PipelineResult` has ``edit_plan=None`` and
      ``final_video_path`` echoing the reviewed MP4 (so the
      ``_serialize_result`` output is uniform with the editor-only
      case).
    * The ``review`` field contains our stub.
    * The framing line ``[reviewer-only] step 1 -- reviewer`` lands in
      the progress log.
    """
    stub_review = ReviewScore(
        adherence=0.95,
        pacing=0.9,
        visual_quality=0.85,
        watchability=0.92,
        overall=0.91,
        feedback="solid",
    )

    job = Job(
        id="reviewer-only-happy",
        status="pending",
        brief=_make_brief(),
        footage_index_path="/tmp/footage.json",
        pipeline_path="/tmp/pipeline.yaml",
        job_type="reviewer-only",
        parent_job_id="reviewer-only-parent",
    )
    job._reviewer_target_video = "/tmp/fake-for-review.mp4"

    reviewer_calls: list[tuple] = []

    def _fake_reviewer(brief, video_path):
        reviewer_calls.append((brief, video_path))
        return stub_review

    monkeypatch.setattr(jobs_module, "run_reviewer", _fake_reviewer)

    registry = JobRegistry()
    result = registry._run_reviewer_only_sync(job)

    assert len(reviewer_calls) == 1
    assert reviewer_calls[0] == (job.brief, "/tmp/fake-for-review.mp4")

    assert isinstance(result, PipelineResult)
    assert result.edit_plan is None
    assert result.final_video_path == "/tmp/fake-for-review.mp4"
    assert result.review is stub_review
    assert result.retries_used == 0
    assert result.warnings == []
    assert result.feedback_history == []

    framing = [
        line.split("] ", 1)[-1]
        for line in job.progress_log
        if "[reviewer-only]" in line
    ]
    assert framing == ["[reviewer-only] step 1 -- reviewer"]


# --------------------------------------------------------------------- #
# Job dataclass contract: new job types in summary + to_dict
# --------------------------------------------------------------------- #


def test_job_summary_and_to_dict_expose_editor_only_job_type() -> None:
    """``Job.summary`` + ``Job.to_dict`` must carry the new ``job_type`` value.

    The sidebar list and detail view both rely on ``job_type`` to
    render editor-only / reviewer-only rows differently (and to link
    them back to their parent). Locking the contract here prevents a
    later dataclass refactor from silently dropping it.
    """
    editor_job = Job(
        id="summary-editor",
        status="pending",
        brief=_make_brief(),
        footage_index_path="/tmp/footage.json",
        pipeline_path="/tmp/pipeline.yaml",
        job_type="editor-only",
        parent_job_id="summary-parent",
    )
    summary = editor_job.summary()
    assert summary["job_type"] == "editor-only"
    assert summary["parent_job_id"] == "summary-parent"
    payload = editor_job.to_dict()
    assert payload["job_type"] == "editor-only"
    assert payload["parent_job_id"] == "summary-parent"

    reviewer_job = Job(
        id="summary-reviewer",
        status="pending",
        brief=_make_brief(),
        footage_index_path="/tmp/footage.json",
        pipeline_path="/tmp/pipeline.yaml",
        job_type="reviewer-only",
        parent_job_id="summary-parent",
    )
    assert reviewer_job.summary()["job_type"] == "reviewer-only"
    assert reviewer_job.to_dict()["job_type"] == "reviewer-only"
