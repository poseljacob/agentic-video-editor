"""Tests for the US-008 EditPlan timeline viewer backend routes.

Covers both endpoints exposed by :mod:`src.web.routes.clips`:

* ``GET /api/jobs/{id}/edit-plan`` -- enriched plan dict with
  display-ready per-entry metadata, including a ``roll_type`` resolved
  against the job's :class:`~src.models.schemas.FootageIndex`.
* ``GET /api/clips/{job_id}/{position}/thumbnail`` -- a JPEG first
  frame of the cut clip, cached on disk so the second request never
  re-invokes ffmpeg.

Strategy mirrors :mod:`tests.test_web_jobs_review`: seed hand-built
:class:`~src.web.jobs.Job` instances directly into the registry's
``_jobs`` dict so the sequential worker never touches them, and drive
the routes through :class:`fastapi.testclient.TestClient`. Nothing is
mocked beyond what's necessary to count ffmpeg invocations in the
cache test.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.agents.editor import _slugify_brief
from src.models.schemas import CreativeBrief, FootageIndex, Shot
from src.web import routes as web_routes
from src.web.app import app
from src.web.jobs import Job, JobRegistry


# --------------------------------------------------------------------- #
# Helpers -- shared with test_web_jobs_review
# --------------------------------------------------------------------- #


def _make_brief(product: str = "clips-test-product") -> CreativeBrief:
    """Minimal :class:`CreativeBrief` that passes validation."""
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
    roll_type: str,
) -> Shot:
    """Construct a :class:`Shot` with just the fields the route cares about."""
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
    """Construct a :class:`FootageIndex` wrapping ``shots``."""
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
    text_overlay: str | None = None,
    transition: str | None = None,
) -> dict:
    """Build a serialized EditPlanEntry dict pointing at ``shot``."""
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
    entries: list[dict],
    total_duration: float,
) -> dict:
    """Build the serialized EditPlan dict shape stored in ``job.result``."""
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
    edit_plan: dict,
) -> Job:
    """Create a completed :class:`Job` and register it without submitting."""
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
    """FastAPI TestClient with lifespan = registry spun up."""
    with TestClient(app) as test_client:
        yield test_client


# --------------------------------------------------------------------- #
# GET /api/jobs/{id}/edit-plan -- happy path
# --------------------------------------------------------------------- #


def test_edit_plan_returns_enriched_entries(
    client: TestClient, tmp_path: Path
) -> None:
    """Happy path: body has derived display fields + resolved roll_type."""
    registry: JobRegistry = client.app.state.job_registry
    brief = _make_brief()

    shot_a = _make_shot("/tmp/footage/intro.mp4", 1.5, 4.7, "a-roll")
    shot_b = _make_shot("/tmp/footage/product.mp4", 0.0, 3.2, "b-roll")
    index = _make_footage_index([shot_a, shot_b])
    footage_path = _write_footage_index(index, tmp_path)

    entries = [
        _entry(
            shot_a,
            position=0,
            start_trim=1.5,
            end_trim=3.7,
            text_overlay="Hook",
        ),
        _entry(
            shot_b,
            position=1,
            start_trim=0.0,
            end_trim=2.0,
            transition="crossfade",
        ),
    ]
    plan = _make_edit_plan(brief, entries, total_duration=4.2)
    _seed_completed_job(
        registry,
        job_id="edit-plan-happy",
        brief=brief,
        footage_index_path=footage_path,
        edit_plan=plan,
    )

    response = client.get("/api/jobs/edit-plan-happy/edit-plan")
    assert response.status_code == 200
    body = response.json()

    assert body["job_id"] == "edit-plan-happy"
    assert body["entry_count"] == 2
    assert body["total_duration"] == pytest.approx(4.2)
    assert len(body["entries"]) == 2

    first = body["entries"][0]
    assert first["position"] == 0
    assert first["source_file"] == "/tmp/footage/intro.mp4"
    assert first["source_filename"] == "intro.mp4"
    assert first["source_timestamp"] == pytest.approx(1.5)
    assert first["display_label"] == "intro.mp4@1.5s"
    assert first["start_trim"] == pytest.approx(1.5)
    assert first["end_trim"] == pytest.approx(3.7)
    assert first["duration"] == pytest.approx(2.2)
    assert first["text_overlay"] == "Hook"
    assert first["roll_type"] == "a-roll"
    assert first["thumbnail_url"] == "/api/clips/edit-plan-happy/0/thumbnail"

    second = body["entries"][1]
    assert second["position"] == 1
    assert second["source_filename"] == "product.mp4"
    assert second["roll_type"] == "b-roll"
    assert second["transition"] == "crossfade"
    assert second["thumbnail_url"] == "/api/clips/edit-plan-happy/1/thumbnail"


# --------------------------------------------------------------------- #
# GET /api/jobs/{id}/edit-plan -- 404 / 409
# --------------------------------------------------------------------- #


def test_edit_plan_returns_404_for_unknown_job(client: TestClient) -> None:
    """Unknown id -> 404 with the id in detail."""
    unknown_id = str(uuid.uuid4())
    response = client.get(f"/api/jobs/{unknown_id}/edit-plan")
    assert response.status_code == 404
    assert unknown_id in response.json()["detail"]


def test_edit_plan_returns_409_when_result_is_none(
    client: TestClient,
) -> None:
    """Running job with no ``result`` yet -> 409 + wait hint."""
    registry: JobRegistry = client.app.state.job_registry
    job_id = "edit-plan-still-running"
    job = Job(
        id=job_id,
        status="running",
        brief=_make_brief(),
        footage_index_path="/tmp/footage.json",
        pipeline_path="/tmp/pipeline.yaml",
        result=None,
    )
    registry._jobs[job_id] = job

    response = client.get(f"/api/jobs/{job_id}/edit-plan")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "wait" in detail.lower()
    assert "running" in detail


def test_edit_plan_roll_type_falls_back_to_unknown(
    client: TestClient, tmp_path: Path
) -> None:
    """A shot_id the FootageIndex can't resolve -> roll_type='unknown'.

    The index contains only ``/tmp/footage/intro.mp4`` but the edit plan
    references ``/tmp/footage/ghost.mp4#0.0``. The endpoint should still
    return 200 and flag the entry as ``roll_type='unknown'`` instead of
    500ing.
    """
    registry: JobRegistry = client.app.state.job_registry
    brief = _make_brief()
    index = _make_footage_index(
        [_make_shot("/tmp/footage/intro.mp4", 0.0, 2.0, "a-roll")]
    )
    footage_path = _write_footage_index(index, tmp_path)

    ghost_entry = {
        "shot_id": "/tmp/footage/ghost.mp4#0.0",
        "start_trim": 0.0,
        "end_trim": 1.5,
        "position": 0,
        "text_overlay": None,
        "transition": None,
    }
    plan = _make_edit_plan(brief, [ghost_entry], total_duration=1.5)
    _seed_completed_job(
        registry,
        job_id="edit-plan-unknown-roll",
        brief=brief,
        footage_index_path=footage_path,
        edit_plan=plan,
    )

    response = client.get("/api/jobs/edit-plan-unknown-roll/edit-plan")
    assert response.status_code == 200
    body = response.json()
    assert body["entries"][0]["roll_type"] == "unknown"
    assert body["entries"][0]["source_filename"] == "ghost.mp4"


def test_edit_plan_splits_shot_id_on_last_hash(
    client: TestClient, tmp_path: Path
) -> None:
    """Source paths containing '#' must split on the LAST '#' separator.

    Regression for the inconsistency between ``_derive_source_file`` (was
    ``split('#', 1)``, splitting on the FIRST '#') and
    ``_resolve_shot_for_entry`` (``rfind('#')``, splitting on the LAST).
    A shot_id like ``videos/shot#1/a.mp4#12.5`` resolved roll_type
    correctly but rendered a bogus source_file/source_filename. Both
    parsers are now aligned on ``rfind('#')``.
    """
    registry: JobRegistry = client.app.state.job_registry
    brief = _make_brief("hash-in-path")

    shot = _make_shot("videos/shot#1/a.mp4", 12.5, 15.0, "a-roll")
    index = _make_footage_index([shot])
    footage_path = _write_footage_index(index, tmp_path)

    entries = [
        {
            "shot_id": "videos/shot#1/a.mp4#12.5",
            "start_trim": 12.5,
            "end_trim": 14.0,
            "position": 0,
            "text_overlay": None,
            "transition": None,
        }
    ]
    plan = _make_edit_plan(brief, entries, total_duration=1.5)
    _seed_completed_job(
        registry,
        job_id="edit-plan-hash-in-path",
        brief=brief,
        footage_index_path=footage_path,
        edit_plan=plan,
    )

    response = client.get("/api/jobs/edit-plan-hash-in-path/edit-plan")
    assert response.status_code == 200
    body = response.json()
    entry = body["entries"][0]
    assert entry["source_file"] == "videos/shot#1/a.mp4"
    assert entry["source_filename"] == "a.mp4"
    assert entry["source_timestamp"] == pytest.approx(12.5)
    assert entry["display_label"] == "a.mp4@12.5s"
    # Also confirm roll_type still resolves -- the resolver has always
    # used rfind('#'), so this is the anchor the display fields now match.
    assert entry["roll_type"] == "a-roll"


# --------------------------------------------------------------------- #
# GET /api/clips/{id}/{pos}/thumbnail
# --------------------------------------------------------------------- #


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


pytestmark_ffmpeg = pytest.mark.skipif(
    not _ffmpeg_available(),
    reason="ffmpeg binary not available in test environment",
)


@pytest.fixture()
def tiny_clip_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build tiny real ``clip_{position:02d}.mp4`` files under a redirected OUTPUT_DIR.

    The clip is a 0.5s 16x16 solid-red video generated by ffmpeg's
    ``lavfi`` source. This keeps the test honest -- we actually shell
    out to ffmpeg to extract a frame -- while staying under a kilobyte
    of disk.

    Monkeypatches :data:`src.web.routes.clips.OUTPUT_DIR` so the route
    looks under the test's tmp path instead of the repo's real
    ``output/`` directory (which might not even exist, and shouldn't be
    mutated by tests anyway).
    """
    from src.web.routes import clips as clips_module

    fake_output = tmp_path / "output"
    monkeypatch.setattr(clips_module, "OUTPUT_DIR", fake_output)

    def _make_clip(brief: CreativeBrief, position: int) -> Path:
        brief_slug = _slugify_brief(brief)
        working_dir = fake_output / "working" / brief_slug
        working_dir.mkdir(parents=True, exist_ok=True)
        clip_path = working_dir / f"clip_{position:02d}.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-f",
                "lavfi",
                "-i",
                "color=c=red:s=16x16:d=0.5:r=10",
                "-pix_fmt",
                "yuv420p",
                "-y",
                str(clip_path),
            ],
            capture_output=True,
            check=True,
        )
        return clip_path

    return _make_clip


@pytestmark_ffmpeg
def test_thumbnail_returns_jpeg_for_real_clip(
    client: TestClient,
    tmp_path: Path,
    tiny_clip_factory,
) -> None:
    """Happy path: real clip on disk -> 200 image/jpeg + cache header."""
    registry: JobRegistry = client.app.state.job_registry
    brief = _make_brief("thumbnail-happy")
    shot = _make_shot("/tmp/footage/a.mp4", 0.0, 1.0, "a-roll")
    index = _make_footage_index([shot])
    footage_path = _write_footage_index(index, tmp_path)
    plan = _make_edit_plan(
        brief, [_entry(shot, position=0, end_trim=0.5)], total_duration=0.5
    )
    _seed_completed_job(
        registry,
        job_id="thumb-happy",
        brief=brief,
        footage_index_path=footage_path,
        edit_plan=plan,
    )
    tiny_clip_factory(brief, position=0)

    response = client.get("/api/clips/thumb-happy/0/thumbnail")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert "max-age=3600" in response.headers.get("cache-control", "")
    # JPEG magic number SOI marker is 0xFF 0xD8.
    assert response.content[:2] == b"\xff\xd8"
    assert len(response.content) > 0


@pytestmark_ffmpeg
def test_thumbnail_caches_on_second_request(
    client: TestClient,
    tmp_path: Path,
    tiny_clip_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second request must not re-invoke ffmpeg (disk cache is primary)."""
    registry: JobRegistry = client.app.state.job_registry
    brief = _make_brief("thumbnail-cache")
    shot = _make_shot("/tmp/footage/a.mp4", 0.0, 1.0, "a-roll")
    index = _make_footage_index([shot])
    footage_path = _write_footage_index(index, tmp_path)
    plan = _make_edit_plan(
        brief, [_entry(shot, position=0, end_trim=0.5)], total_duration=0.5
    )
    _seed_completed_job(
        registry,
        job_id="thumb-cache",
        brief=brief,
        footage_index_path=footage_path,
        edit_plan=plan,
    )
    tiny_clip_factory(brief, position=0)

    # First call: real ffmpeg, populates the disk cache.
    first = client.get("/api/clips/thumb-cache/0/thumbnail")
    assert first.status_code == 200

    # The cached thumbnail must now exist next to the clip.
    from src.web.routes import clips as clips_module

    brief_slug = _slugify_brief(brief)
    thumb_path = (
        clips_module.OUTPUT_DIR
        / "working"
        / brief_slug
        / "clip_00.thumb.jpg"
    )
    assert thumb_path.exists(), "disk cache was not populated"

    # Second call: swap subprocess.run for a booby trap that records
    # any invocation -- if the route tries to shell out again the test
    # fails with a clear message instead of a silent cache miss.
    calls: list[list[str]] = []

    def _boom(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))
        raise AssertionError(
            "subprocess.run was invoked on the cached code path: "
            f"{cmd!r}"
        )

    monkeypatch.setattr(
        "src.web.routes.clips.subprocess.run",
        _boom,
    )

    second = client.get("/api/clips/thumb-cache/0/thumbnail")
    assert second.status_code == 200
    assert second.content == first.content
    assert calls == [], "cached request should not call ffmpeg"


def test_thumbnail_returns_404_for_missing_clip_file(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid job + in-range position but clip file absent -> 404."""
    from src.web.routes import clips as clips_module

    monkeypatch.setattr(clips_module, "OUTPUT_DIR", tmp_path / "output")

    registry: JobRegistry = client.app.state.job_registry
    brief = _make_brief("thumbnail-missing-clip")
    shot = _make_shot("/tmp/footage/a.mp4", 0.0, 1.0, "a-roll")
    index = _make_footage_index([shot])
    footage_path = _write_footage_index(index, tmp_path)
    plan = _make_edit_plan(
        brief, [_entry(shot, position=0, end_trim=0.5)], total_duration=0.5
    )
    _seed_completed_job(
        registry,
        job_id="thumb-missing-clip",
        brief=brief,
        footage_index_path=footage_path,
        edit_plan=plan,
    )

    response = client.get("/api/clips/thumb-missing-clip/0/thumbnail")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_thumbnail_returns_404_for_out_of_range_position(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Position beyond the edit plan length -> 404."""
    from src.web.routes import clips as clips_module

    monkeypatch.setattr(clips_module, "OUTPUT_DIR", tmp_path / "output")

    registry: JobRegistry = client.app.state.job_registry
    brief = _make_brief("thumbnail-oob")
    shot = _make_shot("/tmp/footage/a.mp4", 0.0, 1.0, "a-roll")
    index = _make_footage_index([shot])
    footage_path = _write_footage_index(index, tmp_path)
    plan = _make_edit_plan(
        brief, [_entry(shot, position=0, end_trim=0.5)], total_duration=0.5
    )
    _seed_completed_job(
        registry,
        job_id="thumb-oob",
        brief=brief,
        footage_index_path=footage_path,
        edit_plan=plan,
    )

    response = client.get("/api/clips/thumb-oob/5/thumbnail")
    assert response.status_code == 404
    assert "out of range" in response.json()["detail"].lower()


# --------------------------------------------------------------------- #
# Module import sanity: routes are wired into the app
# --------------------------------------------------------------------- #


def test_clips_router_is_registered_on_app() -> None:
    """Smoke check: the app exposes the two new paths."""
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/api/jobs/{job_id}/edit-plan" in paths
    assert "/api/clips/{job_id}/{position}/thumbnail" in paths
    # Keep a reference so the unused-import lint rule does not trip on
    # ``from src.web import routes as web_routes`` (used for side-effect
    # package import assurance).
    assert web_routes is not None
