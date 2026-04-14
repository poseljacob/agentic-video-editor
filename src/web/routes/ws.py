"""WebSocket route for live pipeline progress streaming (US-003).

The single endpoint here — ``/ws/jobs/{job_id}`` — lets the UI subscribe
to a running pipeline job and receive progress updates as they happen.
It is a read-only channel: the client never sends anything back, and the
server closes the connection cleanly as soon as the job reaches a
terminal state.

Protocol
--------

On connect:

1. The server looks the job id up in the :class:`JobRegistry`. If no
   such job exists, the connection is closed immediately with
   WebSocket close code ``4004``.
2. The server accepts the handshake and atomically subscribes to the
   job via :meth:`src.web.jobs.Job.subscribe`. ``subscribe`` returns a
   fresh :class:`asyncio.Queue`, a point-in-time copy of every
   structured progress entry captured so far, and a flag indicating
   whether the job had already reached a terminal state.
3. The server replays every snapshot entry to the client as
   ``{"type": "progress", "line": ..., "timestamp": ...}``.
4. If the job was already terminal, the server enqueues the matching
   terminal status + result (or failure) and the stream-end sentinel
   via :meth:`src.web.jobs.Job.enqueue_terminal`, drains them from the
   queue, and closes the connection.
5. Otherwise the server loops reading from the queue and forwarding
   every event dict as JSON. When the sentinel
   :data:`~src.web.jobs._STREAM_END` arrives (published by the worker
   after it has already emitted the terminal status + result events),
   the loop exits and the connection is closed cleanly.

Threading
---------

The subscriber queues live on the main asyncio event loop. The
pipeline runs in a worker thread via :func:`asyncio.to_thread`, so
writes from the stdout-capture callback are marshalled back through
:meth:`asyncio.AbstractEventLoop.call_soon_threadsafe` by
:meth:`src.web.jobs.Job.publish`. This file never reads or writes
across threads — it only touches the queue on the loop that owns it.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from src.web.jobs import _STREAM_END, JobRegistry

logger = logging.getLogger(__name__)

router = APIRouter()


#: WebSocket close code used when the client requests an unknown job id.
#: Codes in the ``4000–4999`` range are application-defined per RFC 6455.
UNKNOWN_JOB_CLOSE_CODE = 4004

#: WebSocket close code used when the handler itself crashes. Matches the
#: RFC 6455 "internal server error" code so browser devtools surface it
#: clearly.
INTERNAL_ERROR_CLOSE_CODE = 1011


def _get_registry(websocket: WebSocket) -> JobRegistry | None:
    """Fetch the :class:`JobRegistry` from app state, if it exists.

    Mirrors the logic in :func:`src.web.routes.jobs.get_registry` but
    for the WebSocket side of FastAPI — we cannot raise
    ``HTTPException`` from a WS handler, so the caller decides how to
    respond (close the socket).
    """
    return getattr(websocket.app.state, "job_registry", None)


def _progress_message(entry: dict[str, str]) -> dict[str, Any]:
    """Normalize a stored progress entry into the wire format.

    Each entry stored on :attr:`src.web.jobs.Job._progress_entries` is
    already in the ``{"line", "timestamp"}`` shape; this helper just
    tacks on the discriminator ``type`` field so the client sees a
    uniform message schema whether the entry came from replay or the
    live queue.
    """
    return {
        "type": "progress",
        "line": entry.get("line", ""),
        "timestamp": entry.get("timestamp", ""),
    }


@router.websocket("/ws/jobs/{job_id}")
async def stream_job(websocket: WebSocket, job_id: str) -> None:
    """Stream replay + live progress for a single pipeline job.

    See the module docstring for the full protocol. The handler is
    intentionally defensive: any unexpected exception closes the socket
    cleanly so a buggy pipeline run can never leak file descriptors or
    leave zombie connections hanging in uvicorn's state.
    """
    registry = _get_registry(websocket)
    if registry is None:
        # Server still starting up or already shutting down. Accept the
        # handshake first so the application-defined close code (4004)
        # survives the trip back to the browser — uvicorn's websockets
        # transport maps any pre-accept ``websocket.close`` to an HTTP
        # 403 response and drops the code, which would hide the
        # "unknown job" signal from real clients.
        await websocket.accept()
        await websocket.close(code=UNKNOWN_JOB_CLOSE_CODE)
        return

    job = registry.get(job_id)
    if job is None:
        # Same rationale as above: accept the handshake so the 4004
        # close code actually reaches the client instead of being
        # collapsed into a generic 403 by the uvicorn websockets layer.
        await websocket.accept()
        await websocket.close(code=UNKNOWN_JOB_CLOSE_CODE)
        return

    await websocket.accept()

    # ``subscribe`` atomically snapshots the replay buffer AND registers
    # the new queue under the job's lock, so ``_record_progress`` cannot
    # slip a new entry between those two steps. Any event that lands
    # after ``subscribe`` returns is guaranteed to flow through the
    # queue; any event visible in the replay is guaranteed NOT to have
    # been put on the queue (because the job's publish fan-out snapshots
    # the subscriber list while holding the same lock).
    queue, replay, terminal_at_subscribe = job.subscribe()

    try:
        # Replay every progress entry captured so far with its original
        # timestamp. The client sees these as a burst of ``progress``
        # messages before the live feed begins.
        for entry in replay:
            await websocket.send_json(_progress_message(entry))

        # If the job had already terminated before our subscribe,
        # ``finalize`` had already run, which means the live queue will
        # never receive any future events. Synthesize the terminal
        # messages from the job's stored state so the client sees the
        # same wire format it would for a live job. ``enqueue_terminal``
        # also pushes the stream-end sentinel so the drain loop below
        # exits cleanly.
        if terminal_at_subscribe:
            job.enqueue_terminal(queue)

        # Drain events until the worker (or ``enqueue_terminal``) pushes
        # the stream-end sentinel. Everything else on the queue is a
        # dict matching one of the wire formats and can be forwarded
        # straight through.
        while True:
            event = await queue.get()
            if event is _STREAM_END:
                break
            await websocket.send_json(event)

    except WebSocketDisconnect:
        # Client hung up — nothing to do beyond the ``finally`` block's
        # subscriber unregister. Do not try to send a close frame.
        return
    except Exception:  # noqa: BLE001 - log and close, never crash uvicorn
        logger.exception("WebSocket stream for job %s crashed", job_id)
        try:
            await websocket.close(code=INTERNAL_ERROR_CLOSE_CODE)
        except Exception:  # pragma: no cover - best effort
            pass
        return
    finally:
        job.remove_subscriber(queue)

    # Clean close after the live loop (or replay of a terminal job)
    # finished normally. The try/except around ``close`` handles the
    # (rare) case where the client already shut down in parallel.
    try:
        await websocket.close()
    except Exception:  # pragma: no cover - best effort
        pass
