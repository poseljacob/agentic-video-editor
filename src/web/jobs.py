"""In-memory background job system for pipeline execution.

AVE Studio runs ``src.pipeline.runner.run_pipeline`` as a background job so
the FastAPI UI stays responsive while a cut is being produced (5–15 minutes
on typical footage). US-002 explicitly forbids an external job queue — no
Celery, no Redis, no Dramatiq — so this module implements everything in
process with ``asyncio`` primitives.

Design
------

* :class:`JobRegistry` owns a ``dict[str, Job]`` keyed by UUID. New jobs are
  submitted via :meth:`JobRegistry.submit` which constructs a :class:`Job`
  in the ``pending`` state and pushes its id onto an internal
  :class:`asyncio.Queue`.
* A single worker task — started by :meth:`JobRegistry.start` and stopped by
  :meth:`JobRegistry.stop` — consumes the queue one id at a time, so jobs
  always execute sequentially. This matches the PRD constraint that
  ``run_pipeline`` is compute-heavy and we do not want overlapping runs
  fighting for the Gemini quota or the ffmpeg CPU budget.
* The pipeline runner is synchronous, so the worker invokes it via
  :func:`asyncio.to_thread`. The wrapped function installs a per-thread
  stdout capture via :class:`contextlib.redirect_stdout` so every line the
  pipeline prints is appended to :attr:`Job.progress_log` with an ISO-8601
  timestamp prefix. Because ``redirect_stdout`` uses thread-local state and
  each call runs in a dedicated worker thread, the stream never leaks into
  unrelated FastAPI request handlers.
* Jobs always run with ``human_approval=False``: there is no stdin in a web
  context. The UI is responsible for any pre-run confirmation the user
  wants to see.
* Failures are captured on the job (``error`` + ``status='failed'``) rather
  than crashing the worker, so one bad run cannot take down the queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.agents.editor import run_editor
from src.agents.reviewer import run_reviewer
from src.agents.trim_refiner import refine_plan
from src.models.schemas import CreativeBrief, EditPlan
from src.pipeline.runner import (
    PipelineResult,
    _run_director_with_feedback,
    _with_transient_retry,
    run_pipeline,
)

logger = logging.getLogger(__name__)


JobStatus = str  # one of: "pending", "running", "completed", "failed"


def _utcnow() -> datetime:
    """Return a timezone-aware UTC ``datetime`` for timestamp fields."""
    return datetime.now(timezone.utc)


def _iso(ts: datetime | None) -> str | None:
    """Render a ``datetime`` as ISO-8601 or ``None``."""
    return ts.isoformat() if ts is not None else None


class _ProgressLogStream(io.TextIOBase):
    """Line-buffered stdout proxy that appends to a :class:`Job` progress log.

    The pipeline runner calls ``print(..., flush=True)`` for every log line,
    but ``redirect_stdout`` still sees each ``write`` call as one fragment,
    which may or may not correspond to a full line. We buffer until the next
    newline, split on ``\\n``, and emit one progress entry per complete line
    so ``progress_log`` stays human-readable.

    Each emitted line is prefixed with ``[<iso timestamp>]`` so the UI can
    show when a step began without needing separate metadata. A parallel
    structured entry is also recorded on :attr:`Job._progress_entries` and
    published to every registered WebSocket subscriber so US-003 can stream
    live updates without re-parsing the prefixed string form.
    """

    def __init__(self, job: "Job") -> None:
        super().__init__()
        self._job = job
        self._buffer = ""

    # TextIOBase requires these to be present/True for ``print`` to work.
    def writable(self) -> bool:  # pragma: no cover - stdlib protocol
        return True

    def write(self, data: str) -> int:
        if not data:
            return 0
        self._buffer += data
        # Flush every complete line we have so far, leaving any partial
        # trailing fragment in the buffer for the next ``write``.
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._emit(line)
        return len(data)

    def flush(self) -> None:
        if self._buffer:
            self._emit(self._buffer)
            self._buffer = ""

    def _emit(self, line: str) -> None:
        # Strip any trailing carriage returns that printf-style writers leave
        # behind. Empty lines are preserved so the UI sees the same spacing
        # the CLI would.
        line = line.rstrip("\r")
        self._job._record_progress(line, _iso(_utcnow()) or "")


#: Sentinel pushed onto a subscriber queue to signal "no more events".
#: The WebSocket handler treats this as "close the connection cleanly".
_STREAM_END: Any = object()


@dataclass
class Job:
    """Runtime state for a single pipeline execution.

    Instances are mutated in place by the worker thread — callers that read
    these fields from the asyncio event loop should treat lists like
    ``progress_log`` as snapshots (a subsequent read may see more entries).

    Attributes:
        id: UUID4 string identifier assigned at submit time. Used as the
            dict key inside :class:`JobRegistry` and as the path parameter
            for ``GET /api/jobs/{id}``.
        status: One of ``pending``, ``running``, ``completed``, ``failed``.
        brief: The :class:`CreativeBrief` the caller supplied in the POST
            body. Stored as a model instance so the worker can hand it
            straight to ``run_pipeline``.
        footage_index_path: Filesystem path to the preprocessed
            :class:`FootageIndex` JSON the Director will read.
        pipeline_path: Filesystem path to the YAML pipeline manifest the
            runner will execute.
        progress_log: Timestamped lines captured from ``run_pipeline``
            stdout. Appended to by the worker thread via
            :class:`_ProgressLogStream`.
        result: Serialized :class:`PipelineResult` payload once the run
            completes, or ``None`` while pending/running. Stored as a
            plain ``dict`` so ``GET /api/jobs/{id}`` can return it
            directly.
        error: Exception message if the run failed, else ``None``.
        created_at: Timestamp when :meth:`JobRegistry.submit` was called.
        started_at: Timestamp when the worker moved the job to ``running``.
        completed_at: Timestamp when the worker finished (success or fail).

    Private attributes (not serialized):
        _progress_entries: Parallel structured copy of ``progress_log``
            where each item is ``{"line": str, "timestamp": str}``. Used
            as the replay buffer for new WebSocket subscribers — it keeps
            the raw message separate from the human-readable prefixed form
            so the UI does not need to regex the timestamp back out.
        _subscribers: Live :class:`asyncio.Queue` instances registered by
            WebSocket handlers. The worker thread fans new events out to
            each queue via :meth:`publish`, which marshals the writes
            back onto the main event loop via ``call_soon_threadsafe``.
        _event_loop: Reference to the asyncio event loop that owns the
            subscriber queues. Captured by the worker before dispatching
            to :func:`asyncio.to_thread` so the worker thread can schedule
            thread-safe writes back onto it.
        _terminal: Flag set once the terminal stream-end sentinel has
            been published — guards against double-closing subscribers
            if the worker tries to terminate twice.
    """

    id: str
    status: JobStatus
    brief: CreativeBrief
    footage_index_path: str
    pipeline_path: str
    #: Execution path this job should take. ``"full-pipeline"`` runs the
    #: standard :func:`run_pipeline` flow. ``"feedback-rerun"`` re-runs the
    #: Director (with accumulated feedback) -> trim_refiner -> editor ->
    #: reviewer sequence directly, without starting from the preprocess /
    #: director happy path.
    job_type: str = "full-pipeline"
    #: ID of the parent job when this job is a feedback-triggered re-run.
    #: ``None`` for jobs created via ``POST /api/jobs``.
    parent_job_id: str | None = None
    #: Accumulated feedback history carried into this job's execution.
    #: For ``"feedback-rerun"`` jobs this is the parent's feedback history
    #: with the latest user message appended, and is joined with ``\n\n``
    #: before being handed to :func:`_run_director_with_feedback`.
    feedback_history: list[str] = field(default_factory=list)
    progress_log: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    _progress_entries: list[dict[str, str]] = field(default_factory=list)
    _subscribers: list[asyncio.Queue[Any]] = field(default_factory=list)
    _event_loop: asyncio.AbstractEventLoop | None = None
    _terminal: bool = False
    #: Parsed :class:`EditPlan` instance attached by
    #: :meth:`JobRegistry.submit_editor_rerun` for ``"editor-only"`` jobs.
    #: The worker path :meth:`JobRegistry._run_editor_only_sync` hands this
    #: plan straight to :func:`run_editor` without re-coercion so validation
    #: only happens once in the route layer. Always ``None`` for other job
    #: types.
    _editor_plan: EditPlan | None = None
    #: Absolute path to the rendered MP4 that a ``"reviewer-only"`` job
    #: should score. Set by :meth:`JobRegistry.submit_reviewer_only` from
    #: the parent's ``result["final_video_path"]`` so the worker path
    #: :meth:`JobRegistry._run_reviewer_only_sync` does not have to walk
    #: the parent dict at execution time. Always ``None`` for other job
    #: types.
    _reviewer_target_video: str | None = None
    #: Protects compound operations on ``_progress_entries`` and
    #: ``_subscribers`` that need to be atomic across the worker thread
    #: and the event loop. Used by :meth:`_record_progress` (append +
    #: fan-out) and :meth:`subscribe` (snapshot + register) to close the
    #: race window where an event could be appended-but-not-fanned-out
    #: between a subscriber's snapshot read and its queue registration.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def summary(self) -> dict[str, Any]:
        """Return the compact status payload used by ``GET /api/jobs``."""
        return {
            "id": self.id,
            "status": self.status,
            "job_type": self.job_type,
            "parent_job_id": self.parent_job_id,
            "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at),
            "completed_at": _iso(self.completed_at),
            "brief_product": self.brief.product,
            "progress_lines": len(self.progress_log),
        }

    def to_dict(self) -> dict[str, Any]:
        """Return the full job state used by ``GET /api/jobs/{id}``.

        The caller reads a snapshot of ``progress_log`` so later appends by
        the worker thread do not mutate the response body after it has been
        handed to FastAPI's JSON encoder.
        """
        return {
            "id": self.id,
            "status": self.status,
            "job_type": self.job_type,
            "parent_job_id": self.parent_job_id,
            "brief": self.brief.model_dump(),
            "footage_index_path": self.footage_index_path,
            "pipeline_path": self.pipeline_path,
            "feedback_history": list(self.feedback_history),
            "progress_log": list(self.progress_log),
            "result": self.result,
            "error": self.error,
            "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at),
            "completed_at": _iso(self.completed_at),
        }

    # ------------------------------------------------------------------ #
    # Progress + subscriber plumbing (US-003)
    # ------------------------------------------------------------------ #

    def progress_entries_snapshot(self) -> list[dict[str, str]]:
        """Return a snapshot of every structured progress entry captured.

        Used by the WebSocket handler for replay on connect. Returning a
        fresh ``list`` shields the caller from subsequent appends by the
        worker thread.
        """
        return list(self._progress_entries)

    def add_subscriber(self, queue: "asyncio.Queue[Any]") -> None:
        """Register a WebSocket subscriber queue on this job.

        Most callers should prefer :meth:`subscribe` instead, which
        atomically snapshots the replay buffer and registers the queue
        under the job's lock. This bare register method is kept for
        tests and callers that do not need a consistent replay.
        """
        with self._lock:
            self._subscribers.append(queue)

    def remove_subscriber(self, queue: "asyncio.Queue[Any]") -> None:
        """Remove a subscriber queue (safe if already gone)."""
        with self._lock:
            try:
                self._subscribers.remove(queue)
            except ValueError:
                pass

    def subscribe(
        self,
    ) -> tuple["asyncio.Queue[Any]", list[dict[str, str]], bool]:
        """Atomically register a new subscriber and snapshot the replay buffer.

        Returns a tuple ``(queue, replay, terminal)`` where ``queue`` is a
        fresh :class:`asyncio.Queue`, ``replay`` is a point-in-time copy
        of every :attr:`_progress_entries` item the new subscriber should
        flush before reading live events, and ``terminal`` is True if the
        job had already reached a terminal state when the subscription
        was taken out. When ``terminal`` is True the caller must drive the
        queue to completion by calling :meth:`enqueue_terminal` (this
        method intentionally does not push the terminal messages itself
        so the caller can replay the full progress log first without
        racing the queue reader).

        The whole operation runs under :attr:`_lock` so
        :meth:`_record_progress` cannot append a new entry in the gap
        between the snapshot read and the subscriber registration. Any
        events that land after :meth:`subscribe` returns are guaranteed
        to be published to the queue because the queue is already in
        ``_subscribers``.
        """
        queue: asyncio.Queue[Any] = asyncio.Queue()
        with self._lock:
            replay = list(self._progress_entries)
            self._subscribers.append(queue)
            terminal = self._terminal
        return queue, replay, terminal

    def enqueue_terminal(self, queue: "asyncio.Queue[Any]") -> None:
        """Push terminal status + result/error + stream-end onto a queue.

        Used by the WebSocket handler when it subscribes to a job that is
        already in a ``completed`` or ``failed`` state. The replay loop
        has already sent every progress entry, so all that is left is to
        mirror what :meth:`JobRegistry._run_job_sync` would have emitted
        at the moment the job transitioned to terminal.

        Calls ``queue.put_nowait`` directly because this runs on the same
        event loop that owns the queue (it is invoked from the WebSocket
        coroutine, not the worker thread).
        """
        if self.status == "completed":
            queue.put_nowait({"type": "status", "status": "completed"})
            if self.result is not None:
                queue.put_nowait({"type": "result", "data": self.result})
        elif self.status == "failed":
            queue.put_nowait(
                {
                    "type": "status",
                    "status": "failed",
                    "error": self.error,
                }
            )
        queue.put_nowait(_STREAM_END)

    def _record_progress(self, line: str, timestamp: str) -> None:
        """Append a progress line to both the prefixed log and entries list.

        Also fans the event out to every registered WebSocket subscriber.
        Called from the worker thread (inside ``redirect_stdout``) and from
        :meth:`JobRegistry._run_job_sync` for synthetic framing lines, so
        the append + publish pair runs under :attr:`_lock` — this is what
        lets :meth:`subscribe` take a consistent snapshot without missing
        or duplicating events.
        """
        entry = {"line": line, "timestamp": timestamp}
        event = {"type": "progress", **entry}
        with self._lock:
            self.progress_log.append(f"[{timestamp}] {line}")
            self._progress_entries.append(entry)
            self._publish_locked(event)

    def publish(self, event: Any) -> None:
        """Fan out an event to every registered subscriber queue.

        Safe to call from either the event loop thread or the worker
        thread. Acquires :attr:`_lock` so the subscriber list snapshot
        used for iteration is consistent with :meth:`subscribe` and
        :meth:`remove_subscriber`.
        """
        with self._lock:
            self._publish_locked(event)

    def _publish_locked(self, event: Any) -> None:
        """Lock-held variant of :meth:`publish`.

        Callers MUST already hold :attr:`_lock`. When called from a
        worker thread, each per-queue ``put_nowait`` is scheduled onto
        the captured event loop via
        :meth:`asyncio.AbstractEventLoop.call_soon_threadsafe` so the
        :class:`asyncio.Queue` internals only ever run on the loop that
        owns them.
        """
        loop = self._event_loop
        # Snapshot to avoid "list changed size during iteration" — even
        # though the lock prevents concurrent mutation, we still iterate
        # a copy so ``remove_subscriber`` can be called from inside a
        # publish callback (none do today, but it's cheap insurance).
        for queue in list(self._subscribers):
            if loop is None:
                # No loop captured yet — this only happens for events
                # emitted before :meth:`JobRegistry._run_job_sync` set
                # ``_event_loop`` (e.g. unit tests). Fall back to direct
                # put_nowait, which is safe on the same thread.
                try:
                    queue.put_nowait(event)
                except Exception:  # pragma: no cover - defensive
                    pass
                continue
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:
                # Loop already closed — nothing to fan out to.
                continue

    def _close_stream(self) -> None:
        """Publish the stream-end sentinel to every subscriber.

        Idempotent. After this call the job will not publish any further
        events and WebSocket handlers will see ``_STREAM_END`` on their
        queue, break out of the read loop, and close the connection.
        """
        with self._lock:
            if self._terminal:
                return
            self._terminal = True
            self._publish_locked(_STREAM_END)

    def finalize(self, *events: Any) -> None:
        """Publish the terminal events and the stream-end sentinel atomically.

        Takes :attr:`_lock` once for the whole transition so a late
        :meth:`subscribe` cannot land between the status/result emits
        and the stream-end sentinel — the caller will either see
        ``terminal=False`` (and therefore receive the events through
        the queue) or ``terminal=True`` (and replay them from the
        existing state via :meth:`enqueue_terminal`).
        """
        with self._lock:
            if self._terminal:
                return
            for event in events:
                self._publish_locked(event)
            self._terminal = True
            self._publish_locked(_STREAM_END)


def _serialize_result(result: PipelineResult) -> dict[str, Any]:
    """Convert a :class:`PipelineResult` into a JSON-ready dict.

    ``edit_plan`` and ``review`` are Pydantic models on the dataclass, so
    we call ``model_dump()`` on them when present. Scalar fields pass
    through unchanged.
    """
    return {
        "edit_plan": (
            result.edit_plan.model_dump() if result.edit_plan is not None else None
        ),
        "final_video_path": result.final_video_path,
        "review": (
            result.review.model_dump() if result.review is not None else None
        ),
        "retries_used": result.retries_used,
        "warnings": list(result.warnings),
        "feedback_history": list(result.feedback_history),
    }


class JobRegistry:
    """In-memory registry + sequential asyncio worker for pipeline jobs.

    Lifecycle
    ---------

    * :meth:`start` creates the background worker task. Must be awaited
      before any job can make progress. Safe to call once — repeated
      calls are no-ops.
    * :meth:`stop` cancels the worker task and waits for it to unwind.
      Idempotent — safe to call on shutdown even if ``start`` was never
      called.
    * :meth:`submit` is synchronous from the caller's POV: it creates a
      :class:`Job`, records it, and schedules it on the queue. The
      returned object is safe to read immediately (status will be
      ``pending`` until the worker picks it up).
    * :meth:`get` / :meth:`list_jobs` provide read-only views for the
      GET routes.

    Thread safety
    -------------

    ``_jobs`` is only mutated from the event loop thread: submissions come
    from FastAPI request handlers (also on the event loop) and the worker
    itself only mutates the :class:`Job` dataclass it pulled from the dict
    (not the dict structure). The worker thread (created by
    :func:`asyncio.to_thread`) does mutate ``Job.progress_log`` and other
    scalar fields concurrently with read paths; Python lists/dataclasses
    are safe under the GIL for these operations and the JSON encoder
    reads a snapshot via :meth:`Job.to_dict`.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._started = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Spawn the background worker task.

        No-op if already started. Safe to call from a FastAPI lifespan
        handler.
        """
        if self._started:
            return
        self._started = True
        self._worker = asyncio.create_task(
            self._worker_loop(), name="ave-studio-job-worker"
        )
        logger.info("JobRegistry worker started")

    async def stop(self) -> None:
        """Cancel the worker task and wait for it to exit.

        Idempotent. Any pending jobs still on the queue are abandoned —
        the PRD scope is a single-process dev/preview server, so process
        exit wipes state anyway.
        """
        if not self._started:
            return
        self._started = False
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        logger.info("JobRegistry worker stopped")

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def submit(
        self,
        brief: CreativeBrief,
        footage_index_path: str,
        pipeline_path: str,
    ) -> Job:
        """Create a new pending job and enqueue it for the worker."""
        job = Job(
            id=str(uuid.uuid4()),
            status="pending",
            brief=brief,
            footage_index_path=footage_index_path,
            pipeline_path=pipeline_path,
        )
        self._jobs[job.id] = job
        self._queue.put_nowait(job.id)
        logger.info("JobRegistry submitted job %s", job.id)
        return job

    def submit_feedback_rerun(
        self,
        parent: "Job",
        user_message: str,
    ) -> Job:
        """Create a feedback-triggered pipeline re-run child of ``parent``.

        The child inherits the parent's brief, footage index path, and
        pipeline manifest path so the Director sees the same creative
        context as the original run. Its ``feedback_history`` is built by
        copying the parent's accumulated feedback (falling back to the
        parent ``PipelineResult.feedback_history`` on the first chat
        round) and appending the new ``user_message``. The
        :meth:`_run_job_sync` worker path branches on ``job_type`` and
        runs the Director -> trim_refiner -> editor -> reviewer sequence
        directly, mirroring the retry-loop section of
        :func:`src.pipeline.runner.run_pipeline`.

        Raises:
            ValueError: If the parent is not in ``"completed"`` state, has
                no ``result`` yet, or is missing an ``edit_plan`` /
                ``footage_index_path``. The route layer maps this to a
                409 Conflict response.
        """
        if parent.status != "completed":
            raise ValueError(
                f"parent job {parent.id!r} is not completed "
                f"(status={parent.status!r}); cannot run feedback re-run"
            )
        if parent.result is None:
            raise ValueError(
                f"parent job {parent.id!r} has no result payload; "
                "cannot run feedback re-run"
            )
        if parent.result.get("edit_plan") is None:
            raise ValueError(
                f"parent job {parent.id!r} has no edit_plan in result; "
                "cannot run feedback re-run"
            )
        if not parent.footage_index_path:
            raise ValueError(
                f"parent job {parent.id!r} has no footage_index_path; "
                "cannot run feedback re-run"
            )

        cleaned_message = user_message.strip()
        if not cleaned_message:
            raise ValueError("feedback message must not be empty")

        # The FIRST chat round sees whatever feedback the reviewer retry
        # loop accumulated on the parent (exposed via
        # ``PipelineResult.feedback_history``). Subsequent chat rounds
        # chain off their parent's already-populated
        # :attr:`Job.feedback_history` so each round's Director call
        # sees every prior message in chronological order.
        if parent.feedback_history:
            history = list(parent.feedback_history)
        else:
            history = list(parent.result.get("feedback_history", []))
        history.append(cleaned_message)

        job = Job(
            id=str(uuid.uuid4()),
            status="pending",
            brief=parent.brief,
            footage_index_path=parent.footage_index_path,
            pipeline_path=parent.pipeline_path,
            job_type="feedback-rerun",
            parent_job_id=parent.id,
            feedback_history=history,
        )
        self._jobs[job.id] = job
        self._queue.put_nowait(job.id)
        logger.info(
            "JobRegistry submitted feedback-rerun job %s (parent=%s, "
            "history_len=%d)",
            job.id,
            parent.id,
            len(history),
        )
        return job

    def submit_editor_rerun(
        self,
        parent: "Job",
        modified_plan: EditPlan,
    ) -> Job:
        """Create an ``editor-only`` re-render child from ``modified_plan``.

        Used by ``POST /api/jobs/{id}/re-render`` (US-010) to render a
        user-edited :class:`EditPlan` without re-running the Director or
        Reviewer. The child inherits the parent's brief / footage index /
        pipeline manifest so the frontend and the reviewer-only path (if
        invoked later) still see the same creative context, and the
        ``modified_plan`` is stashed on :attr:`Job._editor_plan` for
        :meth:`_run_editor_only_sync` to hand straight to
        :func:`run_editor`.

        Importantly, the parent's ``result`` is **not** mutated. The
        previous render stays available at its original
        ``result["final_video_path"]`` so the UI can offer a version
        history of renders for the session. The child's PipelineResult
        stores the new MP4 path on its own ``result`` once the worker
        finishes.

        Raises:
            ValueError: If the parent is not in the ``"completed"`` state,
                has no ``result`` yet, or is missing ``footage_index_path``.
                The route layer maps these to a 409 Conflict response so
                the client gets a clear "can't re-render this parent"
                signal instead of a 500.
        """
        if parent.status != "completed":
            raise ValueError(
                f"parent job {parent.id!r} is not completed "
                f"(status={parent.status!r}); cannot run editor-only re-render"
            )
        if parent.result is None:
            raise ValueError(
                f"parent job {parent.id!r} has no result payload; "
                "cannot run editor-only re-render"
            )
        if not parent.footage_index_path:
            raise ValueError(
                f"parent job {parent.id!r} has no footage_index_path; "
                "cannot run editor-only re-render"
            )

        job = Job(
            id=str(uuid.uuid4()),
            status="pending",
            brief=parent.brief,
            footage_index_path=parent.footage_index_path,
            pipeline_path=parent.pipeline_path,
            job_type="editor-only",
            parent_job_id=parent.id,
            feedback_history=list(parent.feedback_history),
        )
        job._editor_plan = modified_plan
        self._jobs[job.id] = job
        self._queue.put_nowait(job.id)
        logger.info(
            "JobRegistry submitted editor-only re-render job %s (parent=%s, "
            "entries=%d)",
            job.id,
            parent.id,
            len(modified_plan.entries),
        )
        return job

    def submit_reviewer_only(self, parent: "Job") -> Job:
        """Create a ``reviewer-only`` child that re-scores the parent's MP4.

        Used by ``POST /api/jobs/{id}/review-only`` (US-010) to run the
        Reviewer against the parent's latest rendered video without
        re-running the Director or Editor. Useful when the user wants a
        fresh quality score for a plan that was manually edited + rendered
        via ``editor-only``.

        Inherits the parent's brief / footage / pipeline path so the
        :class:`Job` shape stays uniform, but the video to score is
        captured onto :attr:`Job._reviewer_target_video` so the worker
        path does not have to re-dig the parent's result blob. The
        parent's result itself is left untouched.

        Raises:
            ValueError: If the parent is not in ``"completed"`` state, has
                no ``result``, has no ``final_video_path`` in its result,
                or the referenced MP4 does not exist on disk. The route
                layer maps this to 409 Conflict.
        """
        if parent.status != "completed":
            raise ValueError(
                f"parent job {parent.id!r} is not completed "
                f"(status={parent.status!r}); cannot run reviewer-only"
            )
        if parent.result is None:
            raise ValueError(
                f"parent job {parent.id!r} has no result payload; "
                "cannot run reviewer-only"
            )
        video_path = parent.result.get("final_video_path")
        if not video_path:
            raise ValueError(
                f"parent job {parent.id!r} has no final_video_path in "
                "result; cannot run reviewer-only"
            )
        # Importing here keeps the module-level import list stable and
        # scopes the Path dependency to the failure-gate hot path.
        from pathlib import Path as _Path

        if not _Path(video_path).exists():
            raise ValueError(
                f"parent job {parent.id!r} final_video_path "
                f"{video_path!r} does not exist on disk; cannot run "
                "reviewer-only"
            )

        job = Job(
            id=str(uuid.uuid4()),
            status="pending",
            brief=parent.brief,
            footage_index_path=parent.footage_index_path,
            pipeline_path=parent.pipeline_path,
            job_type="reviewer-only",
            parent_job_id=parent.id,
            feedback_history=list(parent.feedback_history),
        )
        job._reviewer_target_video = video_path
        self._jobs[job.id] = job
        self._queue.put_nowait(job.id)
        logger.info(
            "JobRegistry submitted reviewer-only job %s (parent=%s, "
            "video=%s)",
            job.id,
            parent.id,
            video_path,
        )
        return job

    def get(self, job_id: str) -> Job | None:
        """Look up a job by id."""
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[Job]:
        """Return all known jobs in submission order (oldest first)."""
        # ``created_at`` is always set at construction, so sorting by it
        # gives a stable oldest-first order without relying on dict
        # insertion order across Python versions.
        return sorted(self._jobs.values(), key=lambda j: j.created_at)

    # ------------------------------------------------------------------ #
    # Worker internals
    # ------------------------------------------------------------------ #

    async def _worker_loop(self) -> None:
        """Main worker loop — pull job ids and run them sequentially.

        Pipeline failures are captured on the :class:`Job` instance so the
        loop keeps running. A :class:`asyncio.CancelledError` from
        :meth:`stop` is the only way out.
        """
        try:
            while True:
                job_id = await self._queue.get()
                job = self._jobs.get(job_id)
                if job is None:
                    # Should not happen — ``submit`` always writes to the
                    # dict before enqueueing — but guard anyway so a bug
                    # does not deadlock the worker.
                    self._queue.task_done()
                    continue
                # Capture the running event loop so the worker thread
                # (spawned by ``asyncio.to_thread``) can marshal subscriber
                # writes back onto the loop that owns the queues.
                job._event_loop = asyncio.get_running_loop()
                try:
                    await asyncio.to_thread(self._run_job_sync, job)
                except Exception as exc:  # noqa: BLE001 - belt and suspenders
                    # ``_run_job_sync`` already captures exceptions onto
                    # the job, but if something escapes (e.g. OOM in
                    # to_thread), still mark the job failed so the caller
                    # sees an error instead of a forever-running status.
                    logger.exception("JobRegistry worker caught %s", exc)
                    job.status = "failed"
                    job.error = f"{type(exc).__name__}: {exc}"
                    job.completed_at = _utcnow()
                    # Make sure every subscriber hears the terminal story
                    # even if ``_run_job_sync`` never reached its publish
                    # section. ``finalize`` is idempotent so calling it
                    # here when the inner function already finalized is a
                    # no-op.
                    job.finalize(
                        {
                            "type": "status",
                            "status": "failed",
                            "error": job.error,
                        }
                    )
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            logger.info("JobRegistry worker loop cancelled")
            raise

    def _run_job_sync(self, job: Job) -> None:
        """Run one pipeline job on the current (worker) thread.

        This runs inside :func:`asyncio.to_thread`, so ``sys.stdout`` can
        be redirected safely for the duration of the call without
        affecting the FastAPI event loop or any other request handler.

        On success, :attr:`Job.result` is populated with the serialized
        :class:`PipelineResult` and :attr:`Job.status` is set to
        ``completed``. On failure, the exception message is stored on
        :attr:`Job.error` and the status is set to ``failed``. Either way
        :attr:`Job.completed_at` is stamped.
        """
        job.status = "running"
        job.started_at = _utcnow()
        job._record_progress(
            f"[ave-studio] job {job.id} started",
            _iso(job.started_at) or "",
        )

        stream = _ProgressLogStream(job)
        try:
            with contextlib.redirect_stdout(stream):
                if job.job_type == "feedback-rerun":
                    result = self._run_feedback_rerun_sync(job)
                elif job.job_type == "editor-only":
                    result = self._run_editor_only_sync(job)
                elif job.job_type == "reviewer-only":
                    result = self._run_reviewer_only_sync(job)
                else:
                    result = run_pipeline(
                        pipeline_path=job.pipeline_path,
                        brief=job.brief,
                        footage_index_path=job.footage_index_path,
                        human_approval=False,
                    )
            # Flush any trailing partial line that never got a newline.
            stream.flush()
            job.result = _serialize_result(result)
            job.status = "completed"
            job.completed_at = _utcnow()
            job._record_progress(
                f"[ave-studio] job {job.id} completed",
                _iso(job.completed_at) or "",
            )
            job.finalize(
                {"type": "status", "status": "completed"},
                {"type": "result", "data": job.result},
            )
        except Exception as exc:  # noqa: BLE001 - we want every failure
            # Flush any partial output captured before the exception so the
            # UI can see the last log lines that preceded the crash.
            try:
                stream.flush()
            except Exception:  # pragma: no cover - defensive
                pass
            job.error = f"{type(exc).__name__}: {exc}"
            job.status = "failed"
            job.completed_at = _utcnow()
            job._record_progress(
                f"[ave-studio] job {job.id} failed: {job.error}",
                _iso(job.completed_at) or "",
            )
            job.finalize(
                {
                    "type": "status",
                    "status": "failed",
                    "error": job.error,
                }
            )
            logger.exception("Pipeline job %s failed", job.id)

    def _run_feedback_rerun_sync(self, job: Job) -> PipelineResult:
        """Execute the feedback re-run sequence for ``job``.

        Mirrors the retry-loop body inside :func:`run_pipeline` (see
        ``src/pipeline/runner.py`` lines ~780-808): build a revised
        ``EditPlan`` via :func:`_run_director_with_feedback` using the
        accumulated feedback, then drive that plan through
        :func:`refine_plan` -> :func:`run_editor` -> :func:`run_reviewer`.

        Runs inside the same ``redirect_stdout(stream)`` context the
        caller installs, so any ``print`` the imported helpers emit is
        captured by :class:`_ProgressLogStream` and fanned out to every
        WebSocket subscriber. Synthetic framing lines are published via
        :meth:`Job._record_progress` so the existing step-indicator
        parsing the UI relies on keeps working without any runner
        changes.

        Raises any exception the imported helpers raise -- the caller's
        ``except`` branch converts that to ``status="failed"`` and
        finalizes the job like a normal pipeline failure.
        """
        combined_feedback = "\n\n".join(job.feedback_history)
        job._record_progress(
            f"[feedback-rerun] step 1 -- director (with feedback, "
            f"history_len={len(job.feedback_history)})",
            _iso(_utcnow()) or "",
        )
        # Each helper is wrapped in _with_transient_retry to match the
        # main run_pipeline retry loop (see src/pipeline/runner.py). Without
        # this, a 502/503/429 from Gemini during a chat refinement would
        # hard-fail the child job while the same error during a full pipeline
        # run would be retried with backoff -- a reliability regression that
        # Codex flagged during US-007 review.
        revised_plan = _with_transient_retry(
            _run_director_with_feedback,
            job.brief,
            job.footage_index_path,
            feedback=combined_feedback,
        )

        job._record_progress(
            "[feedback-rerun] step 2 -- trim_refiner",
            _iso(_utcnow()) or "",
        )
        refined = _with_transient_retry(
            refine_plan, revised_plan, job.footage_index_path
        )

        job._record_progress(
            "[feedback-rerun] step 3 -- editor",
            _iso(_utcnow()) or "",
        )
        video_path = _with_transient_retry(
            run_editor, refined, job.footage_index_path
        )

        job._record_progress(
            "[feedback-rerun] step 4 -- reviewer",
            _iso(_utcnow()) or "",
        )
        review = _with_transient_retry(run_reviewer, job.brief, video_path)

        return PipelineResult(
            edit_plan=refined,
            final_video_path=video_path,
            review=review,
            retries_used=0,
            warnings=[],
            feedback_history=list(job.feedback_history),
        )

    def _run_editor_only_sync(self, job: Job) -> PipelineResult:
        """Run :func:`run_editor` against ``job._editor_plan`` and return a result.

        Mirrors the editor step inside :meth:`_run_feedback_rerun_sync`:
        we emit a single ``[editor-only] step 1 -- editor`` framing line
        so the UI's step parser lights up, wrap the call in
        :func:`_with_transient_retry` so Gemini/ffmpeg hiccups retry with
        backoff instead of hard-failing the child, and package the
        rendered MP4 path into a :class:`PipelineResult` that
        :func:`_serialize_result` knows how to flatten.

        The returned result's ``edit_plan`` is the same pydantic instance
        the route layer validated, so ``job.result["edit_plan"]`` on the
        child echoes the user-edited plan the client submitted -- no
        additional validation needed because the route already ran the
        PUT helpers before enqueueing.

        ``feedback_history`` is carried over from the job (which the
        registry populated from the parent's history) so a subsequent
        ``/feedback`` or ``/review-only`` call on this child still sees
        the chat lineage.

        Raises any exception :func:`run_editor` raises -- the caller's
        ``except`` branch converts that to ``status="failed"``.
        """
        if job._editor_plan is None:  # pragma: no cover - defensive
            raise ValueError(
                f"editor-only job {job.id!r} has no _editor_plan; "
                "submit_editor_rerun must set it before enqueueing"
            )

        job._record_progress(
            "[editor-only] step 1 -- editor",
            _iso(_utcnow()) or "",
        )
        video_path = _with_transient_retry(
            run_editor, job._editor_plan, job.footage_index_path
        )

        return PipelineResult(
            edit_plan=job._editor_plan,
            final_video_path=video_path,
            review=None,
            retries_used=0,
            warnings=[],
            feedback_history=list(job.feedback_history),
        )

    def _run_reviewer_only_sync(self, job: Job) -> PipelineResult:
        """Run :func:`run_reviewer` against ``job._reviewer_target_video``.

        Emits a single ``[reviewer-only] step 1 -- reviewer`` framing
        line, wraps the call in :func:`_with_transient_retry` for Gemini
        transient-error resilience, and returns a :class:`PipelineResult`
        with ``edit_plan=None`` (nothing was re-planned) and
        ``review`` populated. :func:`_serialize_result` handles the None
        edit_plan case natively, so the resulting ``job.result`` will be
        ``{"edit_plan": None, "final_video_path": <same mp4>, "review":
        {...}, ...}``.

        Raises any exception :func:`run_reviewer` raises -- the caller's
        ``except`` branch converts that to ``status="failed"``.
        """
        if not job._reviewer_target_video:  # pragma: no cover - defensive
            raise ValueError(
                f"reviewer-only job {job.id!r} has no _reviewer_target_video; "
                "submit_reviewer_only must set it before enqueueing"
            )

        job._record_progress(
            "[reviewer-only] step 1 -- reviewer",
            _iso(_utcnow()) or "",
        )
        review = _with_transient_retry(
            run_reviewer, job.brief, job._reviewer_target_video
        )

        return PipelineResult(
            edit_plan=None,
            final_video_path=job._reviewer_target_video,
            review=review,
            retries_used=0,
            warnings=[],
            feedback_history=[],
        )
