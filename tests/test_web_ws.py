"""Tests for the WebSocket progress stream (US-003).

These tests exercise three layers:

1. :class:`src.web.jobs.Job` subscriber plumbing in isolation — making
   sure ``subscribe`` / ``_record_progress`` / ``finalize`` / the
   stream-end sentinel all behave correctly without needing to spin up
   a FastAPI app.
2. The FastAPI WebSocket route end-to-end via
   :class:`fastapi.testclient.TestClient`, covering unknown ids,
   replay of existing progress, terminal messaging, and multi-subscriber
   fan-out.
3. The replay path for an already-terminal job (completed and failed).

All tests seed the :class:`JobRegistry` directly to avoid spinning up
``run_pipeline`` — that function talks to Gemini and ffmpeg and is
orders of magnitude too slow (and too network-dependent) to be part of
a unit-test back-pressure loop.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from src.models.schemas import CreativeBrief
from src.web.app import app
from src.web.jobs import _STREAM_END, Job, JobRegistry


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #


def _make_brief() -> CreativeBrief:
    """Minimal brief that satisfies :class:`CreativeBrief` validation."""
    return CreativeBrief(
        product="test-product",
        audience="test-audience",
        tone="energetic",
        duration_seconds=15,
    )


def _make_job(job_id: str = "job-1") -> Job:
    """Construct a bare :class:`Job` with no running worker behind it."""
    return Job(
        id=job_id,
        status="pending",
        brief=_make_brief(),
        footage_index_path="/tmp/footage.json",
        pipeline_path="/tmp/pipeline.yaml",
    )


# --------------------------------------------------------------------- #
# Unit tests: Job subscriber plumbing (no FastAPI)
# --------------------------------------------------------------------- #


def test_subscribe_returns_empty_replay_for_new_job() -> None:
    """A fresh job has nothing to replay and is not terminal yet."""
    job = _make_job()
    queue, replay, terminal = job.subscribe()

    assert replay == []
    assert terminal is False
    assert isinstance(queue, asyncio.Queue)


def test_record_progress_fans_out_to_subscribers() -> None:
    """Events recorded after subscribe land on every registered queue."""
    job = _make_job()
    q1, _, _ = job.subscribe()
    q2, _, _ = job.subscribe()

    job._record_progress("line one", "2025-01-01T00:00:00+00:00")
    job._record_progress("line two", "2025-01-01T00:00:01+00:00")

    # Both queues should have received both events in order.
    evt1a = q1.get_nowait()
    evt1b = q1.get_nowait()
    evt2a = q2.get_nowait()
    evt2b = q2.get_nowait()

    assert evt1a == {
        "type": "progress",
        "line": "line one",
        "timestamp": "2025-01-01T00:00:00+00:00",
    }
    assert evt1b["line"] == "line two"
    assert evt2a == evt1a
    assert evt2b == evt1b


def test_subscribe_snapshot_captures_pre_existing_entries() -> None:
    """A subscriber that joins mid-run sees every prior event via replay."""
    job = _make_job()
    job._record_progress("first", "2025-01-01T00:00:00+00:00")
    job._record_progress("second", "2025-01-01T00:00:01+00:00")

    queue, replay, terminal = job.subscribe()

    assert [entry["line"] for entry in replay] == ["first", "second"]
    assert terminal is False
    # The queue itself should be empty — past events are delivered via
    # the replay list, not pushed retroactively.
    assert queue.empty()


def test_finalize_emits_terminal_events_and_sentinel() -> None:
    """Completing a job pushes status, result, and stream-end in order."""
    job = _make_job()
    queue, _, _ = job.subscribe()

    job.result = {"edit_plan": None, "final_video_path": "/tmp/out.mp4"}
    job.status = "completed"
    job.finalize(
        {"type": "status", "status": "completed"},
        {"type": "result", "data": job.result},
    )

    assert queue.get_nowait() == {"type": "status", "status": "completed"}
    assert queue.get_nowait() == {
        "type": "result",
        "data": {"edit_plan": None, "final_video_path": "/tmp/out.mp4"},
    }
    assert queue.get_nowait() is _STREAM_END


def test_finalize_is_idempotent() -> None:
    """Calling finalize twice does not double-publish terminal events."""
    job = _make_job()
    queue, _, _ = job.subscribe()

    job.finalize({"type": "status", "status": "completed"})
    job.finalize({"type": "status", "status": "completed"})

    assert queue.get_nowait() == {"type": "status", "status": "completed"}
    assert queue.get_nowait() is _STREAM_END
    assert queue.empty()


def test_enqueue_terminal_for_completed_job() -> None:
    """A late subscriber can recover terminal state from stored fields."""
    job = _make_job()
    job.status = "completed"
    job.result = {"edit_plan": None, "final_video_path": "/tmp/out.mp4"}
    # Simulate that finalize already ran — the late subscriber misses
    # the original broadcast and has to synthesize its own copy.
    job._terminal = True

    queue, _, terminal = job.subscribe()
    assert terminal is True

    job.enqueue_terminal(queue)

    assert queue.get_nowait() == {"type": "status", "status": "completed"}
    assert queue.get_nowait() == {
        "type": "result",
        "data": {"edit_plan": None, "final_video_path": "/tmp/out.mp4"},
    }
    assert queue.get_nowait() is _STREAM_END


def test_enqueue_terminal_for_failed_job() -> None:
    """A failed job surfaces the error in the synthesized status message."""
    job = _make_job()
    job.status = "failed"
    job.error = "BoomError: something exploded"
    job._terminal = True

    queue, _, terminal = job.subscribe()
    assert terminal is True

    job.enqueue_terminal(queue)

    assert queue.get_nowait() == {
        "type": "status",
        "status": "failed",
        "error": "BoomError: something exploded",
    }
    assert queue.get_nowait() is _STREAM_END


def test_remove_subscriber_stops_events() -> None:
    """Unregistering a queue means further publishes skip it."""
    job = _make_job()
    queue, _, _ = job.subscribe()

    job._record_progress("first", "2025-01-01T00:00:00+00:00")
    job.remove_subscriber(queue)
    job._record_progress("second", "2025-01-01T00:00:01+00:00")

    assert queue.get_nowait()["line"] == "first"
    assert queue.empty()


# --------------------------------------------------------------------- #
# WebSocket integration tests
# --------------------------------------------------------------------- #


@pytest.fixture()
def client():
    """FastAPI TestClient bound to a lifespan that spins up the registry."""
    with TestClient(app) as test_client:
        yield test_client


def test_ws_unknown_job_closes_with_4004(client: TestClient) -> None:
    """Connecting to a nonexistent job id closes with code 4004."""
    with pytest.raises(Exception) as exc_info:
        with client.websocket_connect("/ws/jobs/does-not-exist") as ws:
            ws.receive_json()

    # Starlette raises WebSocketDisconnect when the server closes.
    from starlette.websockets import WebSocketDisconnect

    assert isinstance(exc_info.value, WebSocketDisconnect)
    assert exc_info.value.code == 4004


def test_ws_replays_existing_progress_log_on_connect(
    client: TestClient,
) -> None:
    """A client connecting to a live job gets every past progress entry."""
    registry: JobRegistry = client.app.state.job_registry
    job = _make_job("ws-replay")
    # Seed the registry directly so we never run the real pipeline.
    registry._jobs[job.id] = job
    job._record_progress("step one", "2025-01-01T00:00:00+00:00")
    job._record_progress("step two", "2025-01-01T00:00:01+00:00")

    # Finalize the job so the server closes the WS after replay instead
    # of waiting forever for live events that will never arrive.
    job.status = "completed"
    job.result = {"edit_plan": None, "final_video_path": "/tmp/out.mp4"}
    job.finalize(
        {"type": "status", "status": "completed"},
        {"type": "result", "data": job.result},
    )

    with client.websocket_connect(f"/ws/jobs/{job.id}") as ws:
        first = ws.receive_json()
        second = ws.receive_json()
        status = ws.receive_json()
        result = ws.receive_json()

    assert first == {
        "type": "progress",
        "line": "step one",
        "timestamp": "2025-01-01T00:00:00+00:00",
    }
    assert second["line"] == "step two"
    assert status == {"type": "status", "status": "completed"}
    assert result["type"] == "result"
    assert result["data"]["final_video_path"] == "/tmp/out.mp4"


def test_ws_streams_terminal_completed_and_result(
    client: TestClient,
) -> None:
    """Completed job: terminal status + result messages reach the client."""
    registry: JobRegistry = client.app.state.job_registry
    job = _make_job("ws-completed")
    registry._jobs[job.id] = job
    job.status = "completed"
    job.result = {
        "edit_plan": None,
        "final_video_path": "/tmp/final.mp4",
        "review": None,
        "retries_used": 0,
        "warnings": [],
        "feedback_history": [],
    }
    job.finalize(
        {"type": "status", "status": "completed"},
        {"type": "result", "data": job.result},
    )

    with client.websocket_connect(f"/ws/jobs/{job.id}") as ws:
        status = ws.receive_json()
        result = ws.receive_json()

    assert status == {"type": "status", "status": "completed"}
    assert result == {"type": "result", "data": job.result}


def test_ws_streams_terminal_failed_with_error(client: TestClient) -> None:
    """Failed job: terminal status message includes the error string."""
    registry: JobRegistry = client.app.state.job_registry
    job = _make_job("ws-failed")
    registry._jobs[job.id] = job
    job.status = "failed"
    job.error = "RuntimeError: pipeline exploded"
    job.finalize(
        {
            "type": "status",
            "status": "failed",
            "error": job.error,
        }
    )

    with client.websocket_connect(f"/ws/jobs/{job.id}") as ws:
        status = ws.receive_json()

    assert status == {
        "type": "status",
        "status": "failed",
        "error": "RuntimeError: pipeline exploded",
    }


def test_ws_supports_multiple_simultaneous_subscribers(
    client: TestClient,
) -> None:
    """Two clients connected to the same job see identical replay streams."""
    registry: JobRegistry = client.app.state.job_registry
    job = _make_job("ws-multi")
    registry._jobs[job.id] = job
    job._record_progress("alpha", "2025-01-01T00:00:00+00:00")
    job._record_progress("beta", "2025-01-01T00:00:01+00:00")
    job.status = "completed"
    job.result = {"final_video_path": "/tmp/out.mp4"}
    job.finalize(
        {"type": "status", "status": "completed"},
        {"type": "result", "data": job.result},
    )

    def _drain(ws) -> list[dict]:
        out: list[dict] = []
        out.append(ws.receive_json())  # progress alpha
        out.append(ws.receive_json())  # progress beta
        out.append(ws.receive_json())  # status completed
        out.append(ws.receive_json())  # result
        return out

    with client.websocket_connect(f"/ws/jobs/{job.id}") as ws1:
        messages_a = _drain(ws1)

    with client.websocket_connect(f"/ws/jobs/{job.id}") as ws2:
        messages_b = _drain(ws2)

    assert messages_a == messages_b
    assert messages_a[0]["line"] == "alpha"
    assert messages_a[1]["line"] == "beta"
    assert messages_a[2] == {"type": "status", "status": "completed"}
    assert messages_a[3]["type"] == "result"


def test_ws_fans_out_live_event_to_concurrent_subscribers(
    client: TestClient,
) -> None:
    """Two WS clients held open AT THE SAME TIME both receive one live event.

    The existing ``test_ws_supports_multiple_simultaneous_subscribers``
    covers sequential replay consistency — it connects ``ws1``, drains it,
    closes it, *then* opens ``ws2``. That proves replay is
    deterministic but does not exercise the queue fan-out path for
    concurrently-registered subscribers, which is what AC#8 ("Multiple
    clients can subscribe to the same job simultaneously") actually
    demands.

    This test holds both sockets open with nested context managers, emits
    a live progress event from the handler's event loop thread via the
    TestClient anyio portal (same trick the live-progress test uses), and
    asserts both sockets receive the identical event before either one
    closes.
    """
    registry: JobRegistry = client.app.state.job_registry
    job = _make_job("ws-concurrent-fanout")
    registry._jobs[job.id] = job

    # Capture the handler-side event loop so ``_record_progress`` can
    # schedule ``put_nowait`` onto the correct loop via
    # ``call_soon_threadsafe``. Without this the publish path would
    # fall through to the direct ``put_nowait`` branch — which would
    # still work for this test, but we want to exercise the real
    # worker-thread path.
    def _capture_loop() -> None:
        job._event_loop = asyncio.get_running_loop()

    client.portal.call(_capture_loop)

    def _emit_live_line() -> None:
        job._record_progress("fanout-1", "2025-01-01T00:00:10+00:00")

    def _finalize() -> None:
        job.status = "completed"
        job.result = {"final_video_path": "/tmp/out.mp4"}
        job.finalize(
            {"type": "status", "status": "completed"},
            {"type": "result", "data": job.result},
        )

    with client.websocket_connect(f"/ws/jobs/{job.id}") as ws1:
        with client.websocket_connect(f"/ws/jobs/{job.id}") as ws2:
            # Both sockets are subscribed at this point. Emit one live
            # progress event — the job's publish fan-out should put it
            # on BOTH subscriber queues.
            client.portal.call(_emit_live_line)

            msg_a = ws1.receive_json()
            msg_b = ws2.receive_json()

            expected = {
                "type": "progress",
                "line": "fanout-1",
                "timestamp": "2025-01-01T00:00:10+00:00",
            }
            assert msg_a == expected
            assert msg_b == expected

            # Finalize so both sockets close cleanly inside their
            # context managers.
            client.portal.call(_finalize)

            status_a = ws1.receive_json()
            result_a = ws1.receive_json()
            status_b = ws2.receive_json()
            result_b = ws2.receive_json()

    assert status_a == {"type": "status", "status": "completed"}
    assert status_b == status_a
    assert result_a["type"] == "result"
    assert result_b["type"] == "result"


def test_ws_streams_live_progress_to_active_subscriber(
    client: TestClient,
) -> None:
    """Events recorded after the connect also flow through the socket.

    TestClient runs the ASGI app on a background anyio portal — the
    real ``JobRegistry`` worker thread does the same dance for live
    pipelines — so we use ``client.portal.call`` to run the publish
    from *inside* the handler's event loop thread. That exercises the
    full ``_record_progress`` → ``_publish_locked`` path without the
    cross-thread marshalling complexity asyncio.to_thread would add.
    """
    registry: JobRegistry = client.app.state.job_registry
    job = _make_job("ws-live")
    registry._jobs[job.id] = job

    def _capture_loop() -> None:
        job._event_loop = asyncio.get_running_loop()

    client.portal.call(_capture_loop)

    def _emit_live_line() -> None:
        job._record_progress("live-1", "2025-01-01T00:00:05+00:00")

    def _finalize() -> None:
        job.status = "completed"
        job.result = {"final_video_path": "/tmp/out.mp4"}
        job.finalize(
            {"type": "status", "status": "completed"},
            {"type": "result", "data": job.result},
        )

    with client.websocket_connect(f"/ws/jobs/{job.id}") as ws:
        # Emit a progress line from the portal thread (where the
        # handler's event loop lives) so the handler's ``queue.get``
        # wakes up and forwards the message.
        client.portal.call(_emit_live_line)
        live_msg = ws.receive_json()

        # Finalize to close the socket cleanly.
        client.portal.call(_finalize)
        status_msg = ws.receive_json()
        result_msg = ws.receive_json()

    assert live_msg == {
        "type": "progress",
        "line": "live-1",
        "timestamp": "2025-01-01T00:00:05+00:00",
    }
    assert status_msg == {"type": "status", "status": "completed"}
    assert result_msg["type"] == "result"
