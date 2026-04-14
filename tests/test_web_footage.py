"""Tests for the US-009 footage routes.

Covers :mod:`src.web.routes.footage`:

* ``GET /api/footage/search`` -- lexical shot search wrapping
  :func:`src.tools.analyze.search_moments`.
* ``GET /api/footage/catalog`` -- full shot catalog access without
  lexical search.

Strategy mirrors :mod:`tests.test_web_clips`: build real
:class:`~src.models.schemas.FootageIndex` JSON on disk via a
``tmp_path`` fixture and exercise the route through
:class:`fastapi.testclient.TestClient`. Nothing is mocked -- the
ranker is fast enough that round-tripping through disk + HTTP is
sub-millisecond per test.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.models.schemas import FootageIndex, Shot
from src.web.app import app


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #


def _make_shot(
    source_file: str,
    start_time: float,
    end_time: float,
    description: str = "",
    transcript: str = "",
    roll_type: str = "a-roll",
) -> Shot:
    """Construct a :class:`Shot` with just the fields the search needs."""
    return Shot(
        source_file=source_file,
        start_time=start_time,
        end_time=end_time,
        description=description,
        energy_level=3,
        relevance_score=0.5,
        transcript=transcript,
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
    """Serialize ``index`` to a tmp JSON file and return its path."""
    path = tmp_path / "footage.json"
    path.write_text(index.model_dump_json())
    return str(path)


@pytest.fixture()
def client():
    """FastAPI TestClient with lifespan = registry spun up."""
    with TestClient(app) as test_client:
        yield test_client


# --------------------------------------------------------------------- #
# GET /api/footage/search -- happy path
# --------------------------------------------------------------------- #


def test_search_returns_ranked_shots(
    client: TestClient, tmp_path: Path
) -> None:
    """Happy path: query matches two shots, ranked by relevance."""
    index = _make_footage_index(
        [
            _make_shot(
                "/tmp/footage/hero.mp4",
                0.0,
                3.0,
                description="woman holds a bright red sneaker close to camera",
                transcript="look at these amazing sneakers",
                roll_type="b-roll",
            ),
            _make_shot(
                "/tmp/footage/testimonial.mp4",
                5.0,
                9.5,
                description="woman speaks directly to camera in kitchen",
                transcript="i love these new running sneakers",
                roll_type="a-roll",
            ),
            _make_shot(
                "/tmp/footage/landscape.mp4",
                0.0,
                4.2,
                description="sweeping drone shot of desert mountains",
                transcript="",
                roll_type="b-roll",
            ),
        ]
    )
    footage_path = _write_footage_index(index, tmp_path)

    response = client.get(
        "/api/footage/search",
        params={
            "query": "sneakers",
            "footage_index_path": footage_path,
        },
    )
    assert response.status_code == 200
    body = response.json()

    assert body["query"] == "sneakers"
    assert body["footage_index_path"] == footage_path
    # Two shots match ("sneakers" appears in description or transcript
    # of both hero.mp4 and testimonial.mp4), the landscape shot does
    # not.
    assert body["count"] == 2
    returned_files = {r["source_file"] for r in body["results"]}
    assert returned_files == {
        "/tmp/footage/hero.mp4",
        "/tmp/footage/testimonial.mp4",
    }

    first = body["results"][0]
    assert "shot_id" in first
    assert "start_time" in first
    assert "end_time" in first
    assert "duration" in first
    assert "source_filename" in first
    assert "roll_type" in first
    assert "relevance_score" in first
    assert "display_label" in first
    # Relevance scores are in [0, 1] floats.
    for result in body["results"]:
        assert 0.0 <= result["relevance_score"] <= 1.0

    # The shot_id must match the ``source_file#start_time`` convention
    # the PUT validator expects.
    for result in body["results"]:
        assert result["shot_id"] == f"{result['source_file']}#{result['start_time']}"


def test_catalog_returns_all_shots_without_search(
    client: TestClient, tmp_path: Path
) -> None:
    """Catalog returns every shot, including shots a stopword query misses."""
    index = _make_footage_index(
        [
            _make_shot(
                "/tmp/footage/quiet.mp4",
                0.0,
                1.5,
                description="",
                transcript="",
                roll_type="unknown",
            ),
            _make_shot(
                "/tmp/footage/product.mp4",
                2.0,
                5.25,
                description="macro shot of packaging",
                transcript="",
                roll_type="b-roll",
            ),
            _make_shot(
                "/tmp/footage/founder.mp4",
                8.0,
                12.0,
                description="founder speaks to camera",
                transcript="we built this for creators",
                roll_type="a-roll",
            ),
        ]
    )
    footage_path = _write_footage_index(index, tmp_path)

    response = client.get(
        "/api/footage/catalog",
        params={"footage_index_path": footage_path},
    )
    assert response.status_code == 200
    body = response.json()

    assert body["footage_index_path"] == footage_path
    assert body["count"] == 3
    assert [r["source_file"] for r in body["results"]] == [
        "/tmp/footage/quiet.mp4",
        "/tmp/footage/product.mp4",
        "/tmp/footage/founder.mp4",
    ]

    first = body["results"][0]
    for field in (
        "shot_id",
        "start_time",
        "end_time",
        "duration",
        "description",
        "source_file",
        "display_label",
        "roll_type",
    ):
        assert field in first
    assert first["shot_id"] == "/tmp/footage/quiet.mp4#0.0"
    assert first["duration"] == 1.5


def test_search_empty_results_is_200(
    client: TestClient, tmp_path: Path
) -> None:
    """Zero matches is a successful response with an empty ``results``."""
    index = _make_footage_index(
        [_make_shot("/tmp/footage/a.mp4", 0.0, 1.0, description="red cube")]
    )
    footage_path = _write_footage_index(index, tmp_path)

    response = client.get(
        "/api/footage/search",
        params={
            "query": "submarine periscope",
            "footage_index_path": footage_path,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 0
    assert body["results"] == []


# --------------------------------------------------------------------- #
# GET /api/footage/search -- validation failures
# --------------------------------------------------------------------- #


def test_search_empty_query_returns_422(
    client: TestClient, tmp_path: Path
) -> None:
    """Whitespace-only query -> 422 with a field-level error on query."""
    index = _make_footage_index(
        [_make_shot("/tmp/footage/a.mp4", 0.0, 1.0)]
    )
    footage_path = _write_footage_index(index, tmp_path)

    response = client.get(
        "/api/footage/search",
        params={
            "query": "   ",
            "footage_index_path": footage_path,
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    assert any(
        "query" in err.get("loc", []) and "empty" in err.get("msg", "").lower()
        for err in detail
    )


def test_search_missing_footage_index_returns_404(
    client: TestClient, tmp_path: Path
) -> None:
    """Non-existent footage_index_path -> 404."""
    bogus = tmp_path / "does-not-exist.json"
    response = client.get(
        "/api/footage/search",
        params={
            "query": "anything",
            "footage_index_path": str(bogus),
        },
    )
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_search_malformed_footage_index_returns_404(
    client: TestClient, tmp_path: Path
) -> None:
    """A file that is not a valid FootageIndex -> 404 with a parse hint."""
    bogus = tmp_path / "garbage.json"
    bogus.write_text('{"not": "a footage index"}')

    response = client.get(
        "/api/footage/search",
        params={
            "query": "anything",
            "footage_index_path": str(bogus),
        },
    )
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert "footageindex" in detail.lower() or "valid" in detail.lower()


def test_search_missing_query_param_returns_422(
    client: TestClient, tmp_path: Path
) -> None:
    """Missing ``query`` query parameter -> FastAPI 422."""
    index = _make_footage_index(
        [_make_shot("/tmp/footage/a.mp4", 0.0, 1.0)]
    )
    footage_path = _write_footage_index(index, tmp_path)

    response = client.get(
        "/api/footage/search",
        params={"footage_index_path": footage_path},
    )
    assert response.status_code == 422


def test_search_missing_footage_index_path_param_returns_422(
    client: TestClient,
) -> None:
    """Missing ``footage_index_path`` query parameter -> FastAPI 422."""
    response = client.get(
        "/api/footage/search",
        params={"query": "sneakers"},
    )
    assert response.status_code == 422


# --------------------------------------------------------------------- #
# Router wiring
# --------------------------------------------------------------------- #


def test_footage_router_is_registered_on_app() -> None:
    """Smoke check: the app exposes the new search path."""
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/api/footage/search" in paths
