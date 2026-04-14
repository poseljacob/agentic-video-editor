"""Tests for the US-009 ``PUT /api/jobs/{id}/edit-plan`` route.

Covers the edit-plan update endpoint added in :mod:`src.web.routes.jobs`:

* Happy path: a valid revised plan is persisted back onto
  ``job.result["edit_plan"]`` and returned in the response.
* Validation failures return HTTP 422 with FastAPI-shaped error
  bodies (``{"detail": [{"loc": [...], "msg": ..., "type": ...}]}``)
  describing which field was wrong. Four failure modes are exercised:
    * unknown ``shot_id`` (404-shaped "shot_not_found" error)
    * out-of-bounds ``start_trim`` / ``end_trim``
    * non-contiguous ``position`` values
    * missing ``shot_id`` field (pydantic coercion layer)
* 404 for an unknown job id.
* 409 for a job with ``result=None`` (still running / failed).

Strategy mirrors :mod:`tests.test_web_clips`: seed hand-built
:class:`~src.web.jobs.Job` instances directly into the registry's
``_jobs`` dict so the sequential worker never picks them up, and
drive the routes through :class:`fastapi.testclient.TestClient`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.models.schemas import CreativeBrief, FootageIndex, Shot
from src.web.app import app
from src.web.jobs import Job, JobRegistry


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #


def _make_brief(product: str = "edit-plan-put-test") -> CreativeBrief:
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
    return Shot(
        source_file=source_file,
        start_time=start_time,
        end_time=end_time,
        description="fixture shot",
        energy_level=3,
        relevance_score=0.5,
        transcript="",
        words=[],
        roll_type=roll_type,
    )


def _make_footage_index(shots: list[Shot]) -> FootageIndex:
    return FootageIndex(
        source_dir="/tmp/fixture-footage",
        shots=shots,
        total_duration=sum(s.end_time - s.start_time for s in shots),
        created_at=datetime.now(timezone.utc),
    )


def _write_footage_index(index: FootageIndex, tmp_path: Path) -> str:
    path = tmp_path / "footage.json"
    path.write_text(index.model_dump_json())
    return str(path)


def _entry(
    shot: Shot,
    position: int,
    start_trim: float | None = None,
    end_trim: float | None = None,
    text_overlay: str | None = None,
    transition: str | None = None,
) -> dict[str, Any]:
    return {
        "shot_id": f"{shot.source_file}#{shot.start_time}",
        "start_trim": shot.start_time if start_trim is None else start_trim,
        "end_trim": shot.end_time if end_trim is None else end_trim,
        "position": position,
        "text_overlay": text_overlay,
        "transition": transition,
    }


def _make_edit_plan(
    brief: CreativeBrief,
    entries: list[dict[str, Any]],
    total_duration: float,
) -> dict[str, Any]:
    return {
        "brief": brief.model_dump(),
        "entries": entries,
        "music_path": None,
        "total_duration": total_duration,
    }


def _seed_completed_job(
    registry: JobRegistry,
    *,
    job_id: str,
    brief: CreativeBrief,
    footage_index_path: str,
    edit_plan: dict[str, Any],
) -> Job:
    job = Job(
        id=job_id,
        status="completed",
        brief=brief,
        footage_index_path=footage_index_path,
        pipeline_path="/tmp/pipeline.yaml",
        result={
            "edit_plan": edit_plan,
            "final_video_path": "/tmp/final.mp4",
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
    with TestClient(app) as test_client:
        yield test_client


def _base_fixture(
    registry: JobRegistry, tmp_path: Path, job_id: str
) -> tuple[Shot, Shot, CreativeBrief, str]:
    """Build two shots + a seeded completed job. Returns (shot_a, shot_b, brief, footage_path)."""
    brief = _make_brief()
    shot_a = _make_shot("/tmp/footage/intro.mp4", 1.5, 4.7, "a-roll")
    shot_b = _make_shot("/tmp/footage/product.mp4", 0.0, 3.2, "b-roll")
    index = _make_footage_index([shot_a, shot_b])
    footage_path = _write_footage_index(index, tmp_path)

    initial_plan = _make_edit_plan(
        brief,
        [
            _entry(shot_a, position=0, start_trim=1.5, end_trim=3.7),
            _entry(shot_b, position=1, start_trim=0.0, end_trim=2.0),
        ],
        total_duration=4.2,
    )
    _seed_completed_job(
        registry,
        job_id=job_id,
        brief=brief,
        footage_index_path=footage_path,
        edit_plan=initial_plan,
    )
    return shot_a, shot_b, brief, footage_path


# --------------------------------------------------------------------- #
# PUT /api/jobs/{id}/edit-plan -- happy path
# --------------------------------------------------------------------- #


def test_put_edit_plan_persists_valid_plan(
    client: TestClient, tmp_path: Path
) -> None:
    """Valid revised plan -> 200, returned body matches, job.result updates."""
    registry: JobRegistry = client.app.state.job_registry
    shot_a, shot_b, brief, _ = _base_fixture(registry, tmp_path, "put-happy")

    # Reorder (swap positions) + tighten trim on shot_a.
    revised = _make_edit_plan(
        brief,
        [
            _entry(shot_b, position=0, start_trim=0.2, end_trim=2.5),
            _entry(
                shot_a,
                position=1,
                start_trim=2.0,
                end_trim=3.5,
                text_overlay="Hook revised",
            ),
        ],
        total_duration=3.8,
    )

    response = client.put("/api/jobs/put-happy/edit-plan", json=revised)
    assert response.status_code == 200
    body = response.json()
    saved = body["edit_plan"]

    assert saved["total_duration"] == pytest.approx(3.8)
    assert len(saved["entries"]) == 2
    assert saved["entries"][0]["position"] == 0
    assert saved["entries"][0]["shot_id"] == f"{shot_b.source_file}#{shot_b.start_time}"
    assert saved["entries"][1]["text_overlay"] == "Hook revised"

    # And the job record must now reflect the update so subsequent GETs
    # see the new plan.
    job = registry.get("put-happy")
    assert job is not None
    assert job.result is not None
    persisted = job.result["edit_plan"]
    assert persisted["entries"][0]["position"] == 0
    assert persisted["entries"][1]["start_trim"] == pytest.approx(2.0)


# --------------------------------------------------------------------- #
# PUT /api/jobs/{id}/edit-plan -- validation failures
# --------------------------------------------------------------------- #


def test_put_edit_plan_invalid_shot_id_returns_422(
    client: TestClient, tmp_path: Path
) -> None:
    """A shot_id that doesn't resolve against the index -> 422."""
    registry: JobRegistry = client.app.state.job_registry
    shot_a, _, brief, _ = _base_fixture(registry, tmp_path, "put-bad-shot")

    bad = _make_edit_plan(
        brief,
        [
            _entry(shot_a, position=0, start_trim=1.5, end_trim=3.0),
            {
                "shot_id": "/tmp/footage/ghost.mp4#9.9",
                "start_trim": 0.0,
                "end_trim": 1.0,
                "position": 1,
                "text_overlay": None,
                "transition": None,
            },
        ],
        total_duration=2.5,
    )

    response = client.put("/api/jobs/put-bad-shot/edit-plan", json=bad)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    # At least one error references the ghost shot on entries[1].shot_id.
    matches = [
        err
        for err in detail
        if err.get("loc") == ["body", "entries", 1, "shot_id"]
    ]
    assert matches, f"expected shot_id error, got {detail!r}"
    assert "ghost" in matches[0]["msg"] or "resolve" in matches[0]["msg"].lower()
    assert matches[0]["type"] == "value_error.shot_not_found"


def test_put_edit_plan_out_of_bounds_trim_returns_422(
    client: TestClient, tmp_path: Path
) -> None:
    """A trim outside the shot's [start_time, end_time] -> 422."""
    registry: JobRegistry = client.app.state.job_registry
    shot_a, shot_b, brief, _ = _base_fixture(registry, tmp_path, "put-oob-trim")

    # shot_a has start_time=1.5 and end_time=4.7. Push end_trim well
    # past 4.7 so the bounds check trips.
    bad = _make_edit_plan(
        brief,
        [
            _entry(shot_a, position=0, start_trim=1.5, end_trim=9.9),
            _entry(shot_b, position=1, start_trim=0.0, end_trim=2.0),
        ],
        total_duration=10.4,
    )

    response = client.put("/api/jobs/put-oob-trim/edit-plan", json=bad)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    # The error must point at entries[0].end_trim.
    matches = [
        err
        for err in detail
        if err.get("loc") == ["body", "entries", 0, "end_trim"]
    ]
    assert matches, f"expected end_trim error, got {detail!r}"
    assert matches[0]["type"] == "value_error.trim_out_of_bounds"


def test_put_edit_plan_out_of_bounds_start_trim_returns_422(
    client: TestClient, tmp_path: Path
) -> None:
    """A start_trim BEFORE the shot's start_time also trips the bounds check."""
    registry: JobRegistry = client.app.state.job_registry
    shot_a, shot_b, brief, _ = _base_fixture(
        registry, tmp_path, "put-oob-start-trim"
    )

    # shot_a.start_time=1.5; push start_trim to 0.5 which is before
    # the shot starts.
    bad = _make_edit_plan(
        brief,
        [
            _entry(shot_a, position=0, start_trim=0.5, end_trim=3.0),
            _entry(shot_b, position=1, start_trim=0.0, end_trim=2.0),
        ],
        total_duration=4.5,
    )

    response = client.put("/api/jobs/put-oob-start-trim/edit-plan", json=bad)
    assert response.status_code == 422
    detail = response.json()["detail"]
    matches = [
        err
        for err in detail
        if err.get("loc") == ["body", "entries", 0, "start_trim"]
    ]
    assert matches, f"expected start_trim error, got {detail!r}"
    assert matches[0]["type"] == "value_error.trim_out_of_bounds"


def test_put_edit_plan_non_contiguous_positions_returns_422(
    client: TestClient, tmp_path: Path
) -> None:
    """Positions must form a contiguous 0..N-1 sequence."""
    registry: JobRegistry = client.app.state.job_registry
    shot_a, shot_b, brief, _ = _base_fixture(
        registry, tmp_path, "put-bad-positions"
    )

    # Positions [0, 2] instead of [0, 1] -- the plan-level check trips.
    bad = _make_edit_plan(
        brief,
        [
            _entry(shot_a, position=0, start_trim=1.5, end_trim=3.7),
            _entry(shot_b, position=2, start_trim=0.0, end_trim=2.0),
        ],
        total_duration=4.2,
    )

    response = client.put("/api/jobs/put-bad-positions/edit-plan", json=bad)
    assert response.status_code == 422
    detail = response.json()["detail"]
    matches = [
        err
        for err in detail
        if err.get("loc") == ["body", "entries"]
    ]
    assert matches, f"expected positions error, got {detail!r}"
    assert matches[0]["type"] == "value_error.positions_not_contiguous"


def test_put_edit_plan_missing_shot_id_returns_422(
    client: TestClient, tmp_path: Path
) -> None:
    """Pydantic layer: missing required field -> 422 via model_validate."""
    registry: JobRegistry = client.app.state.job_registry
    _, _, brief, _ = _base_fixture(registry, tmp_path, "put-missing-field")

    bad = {
        "brief": brief.model_dump(),
        "entries": [
            {
                # shot_id intentionally omitted
                "start_trim": 0.0,
                "end_trim": 1.0,
                "position": 0,
            }
        ],
        "music_path": None,
        "total_duration": 1.0,
    }

    response = client.put("/api/jobs/put-missing-field/edit-plan", json=bad)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    # The pydantic error body uses the full ``loc`` path through the
    # EditPlan model -- we just confirm some error mentions shot_id.
    assert any(
        "shot_id" in str(err.get("loc", [])) for err in detail
    ), f"expected shot_id missing-field error, got {detail!r}"


# --------------------------------------------------------------------- #
# PUT /api/jobs/{id}/edit-plan -- 404 / 409
# --------------------------------------------------------------------- #


def test_put_edit_plan_unknown_job_returns_404(client: TestClient) -> None:
    """Unknown job id -> 404 with id in detail."""
    unknown_id = str(uuid.uuid4())
    response = client.put(
        f"/api/jobs/{unknown_id}/edit-plan",
        json={
            "brief": _make_brief().model_dump(),
            "entries": [],
            "music_path": None,
            "total_duration": 0.0,
        },
    )
    assert response.status_code == 404
    assert unknown_id in response.json()["detail"]


def test_put_edit_plan_running_job_returns_409(client: TestClient) -> None:
    """Running job with result=None -> 409 + wait hint."""
    registry: JobRegistry = client.app.state.job_registry
    job_id = "put-still-running"
    job = Job(
        id=job_id,
        status="running",
        brief=_make_brief(),
        footage_index_path="/tmp/footage.json",
        pipeline_path="/tmp/pipeline.yaml",
        result=None,
    )
    registry._jobs[job_id] = job

    response = client.put(
        f"/api/jobs/{job_id}/edit-plan",
        json={
            "brief": _make_brief().model_dump(),
            "entries": [],
            "music_path": None,
            "total_duration": 0.0,
        },
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "wait" in detail.lower()
    assert "running" in detail
