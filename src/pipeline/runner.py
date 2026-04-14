"""Pipeline runner: YAML-driven Director → Editor → Reviewer orchestration.

This module is the glue between the three agent modules shipped in
``src.agents.director``, ``src.agents.editor``, and ``src.agents.reviewer``.
It reads a YAML pipeline manifest (see ``pipelines/ugc-ad.yaml``),
executes each step in order, honors optional human-approval gates, and
runs a retry loop that feeds Reviewer feedback back to the Director when
the rendered cut falls below a configured score threshold.

The runner intentionally does **not** reimplement any tool logic — it
calls the public ``run_director`` / ``run_editor`` / ``run_reviewer``
synchronous wrappers and composes their results. The one exception is
retry feedback: ``run_director`` has no parameter for reviewer feedback,
so :func:`_run_director_with_feedback` builds a fresh Agent via
``build_director`` and sends an augmented user message that appends the
feedback history. That helper never modifies the Director module and
mirrors the event-draining loop used by ``run_director`` itself.

Failure semantics
-----------------

* A broken pipeline manifest (unknown agent, bad retry_if block, missing
  steps list) raises ``ValueError`` immediately.
* If the retry budget is exhausted and the final review still trips the
  ``retry_if`` predicate, the runner logs a WARNING and returns a
  :class:`PipelineResult` with the best-effort artifacts plus a non-empty
  ``warnings`` list. It does **not** raise — AC 8 treats this as a
  complete-with-low-score outcome, not a pipeline failure.
* If the user declines the human-approval gate the runner returns a
  :class:`PipelineResult` with ``edit_plan`` set, ``final_video_path``
  and ``review`` left as ``None``, and a warning recording the abort.
* Any exception raised by ``run_director`` / ``run_editor`` /
  ``run_reviewer`` (file-not-found, schema validation, tool failures)
  propagates — those are real bugs the caller should see, not soft
  retryable conditions.

Logging is plain ``print()`` to stdout to match the rest of the project
(see ``src/pipeline/preprocess.py``).
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from google.adk.runners import InMemoryRunner
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field, ValidationError

from src.agents.director import (
    _APP_NAME as _DIRECTOR_APP_NAME,
)
from src.agents.director import (
    _USER_ID as _DIRECTOR_USER_ID,
)
from src.agents.director import (
    build_director,
    run_director,
)
from src.agents.editor import run_editor
from src.agents.reviewer import run_reviewer
from src.agents.trim_refiner import refine_plan
from src.models.schemas import CreativeBrief, EditPlan, ReviewScore

# --------------------------------------------------------------------------- #
# Transient error retry
# --------------------------------------------------------------------------- #

_TRANSIENT_DELAYS = (30, 60, 120)  # seconds — exponential-ish backoff


def _with_transient_retry(fn, *args, **kwargs):
    """Call *fn* with retries on transient Gemini errors (502, 503, 429).

    Catches ``google.genai.errors.ServerError`` (5xx) and
    ``google.genai.errors.ClientError`` (429 rate-limit) and waits
    before retrying, up to ``len(_TRANSIENT_DELAYS)`` attempts.
    """
    for attempt, delay in enumerate(_TRANSIENT_DELAYS):
        try:
            return fn(*args, **kwargs)
        except (genai_errors.ServerError, genai_errors.ClientError) as exc:
            _log(
                f"[pipeline] transient error (attempt {attempt + 1}/"
                f"{len(_TRANSIENT_DELAYS) + 1}): {exc}"
            )
            _log(f"[pipeline] waiting {delay}s before retry …")
            time.sleep(delay)
    # Final attempt — let exceptions propagate
    return fn(*args, **kwargs)


# --------------------------------------------------------------------------- #
# Typed manifest models
# --------------------------------------------------------------------------- #


_ALLOWED_AGENTS: frozenset[str] = frozenset({"director", "editor", "reviewer", "trim_refiner"})
_ALLOWED_METRICS: frozenset[str] = frozenset(
    {"adherence", "pacing", "visual_quality", "watchability", "overall"}
)
_ALLOWED_OPERATORS: frozenset[str] = frozenset({"<", "<="})


class RetryIf(BaseModel):
    """Retry predicate for a pipeline step.

    Declares which :class:`ReviewScore` metric to inspect, the comparison
    operator, the threshold, the maximum number of retries allowed, and
    which upstream agent receives the Reviewer feedback on retry. Today
    only ``feedback_target: director`` is supported — the runner raises
    ``ValueError`` on any other value.
    """

    metric: Literal[
        "adherence", "pacing", "visual_quality", "watchability", "overall"
    ] = "overall"
    operator: Literal["<", "<="] = "<"
    threshold: float = 0.7
    max_retries: int = 2
    feedback_target: Literal["director"] = "director"


class PipelineStep(BaseModel):
    """One step in a pipeline manifest.

    Unknown keys are rejected at validation time. The :func:`_load_pipeline`
    helper preprocesses raw YAML with :func:`_strip_unknown_keys` so
    unexpected fields produce a stdout warning instead of a hard error.
    """

    agent: Literal["director", "editor", "reviewer", "trim_refiner"]
    gate: Literal["human_approval"] | None = None
    retry_if: RetryIf | None = None

    model_config = {"extra": "forbid"}


class PipelineManifest(BaseModel):
    """Typed view of a pipeline YAML manifest."""

    name: str = "unnamed-pipeline"
    description: str | None = None
    steps: list[PipelineStep] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #


@dataclass
class PipelineResult:
    """Outcome of a full :func:`run_pipeline` invocation.

    Attributes:
        edit_plan: The last :class:`EditPlan` produced by the Director,
            or ``None`` if the pipeline aborted before the Director ran.
        final_video_path: Absolute path to the last rendered MP4 the
            Editor produced, or ``None`` if the pipeline aborted before
            the Editor ran (e.g. the human declined the approval gate).
        review: The last :class:`ReviewScore` produced by the Reviewer,
            or ``None`` if the pipeline aborted before the Reviewer ran.
        retries_used: How many retry iterations the Reviewer loop
            consumed. Zero if the first pass passed the retry predicate
            or the pipeline had no Reviewer step.
        warnings: Non-fatal notices the runner wants to surface (retry
            budget exhausted, human declined gate, unknown manifest
            keys, etc.). Empty on a clean happy path.
        feedback_history: All Reviewer feedback strings gathered across
            retry attempts, in chronological order. Useful for debugging
            retry regressions.
    """

    edit_plan: EditPlan | None = None
    final_video_path: str | None = None
    review: ReviewScore | None = None
    retries_used: int = 0
    warnings: list[str] = field(default_factory=list)
    feedback_history: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Logging helpers
# --------------------------------------------------------------------------- #


def _log(msg: str) -> None:
    """Write an informational line to stdout (flushed)."""
    print(msg, flush=True)


def _log_warning(msg: str) -> None:
    """Write a prefixed warning line to stdout."""
    print(f"[pipeline] WARNING: {msg}", flush=True)


def _log_step_start(agent: str, index: int) -> None:
    _log(f"[pipeline] step {index} — {agent} starting")


def _log_step_end(agent: str, duration: float, summary: str) -> None:
    _log(
        f"[pipeline] step — {agent} done in {duration:.2f}s — {summary}"
    )


# --------------------------------------------------------------------------- #
# Manifest loading
# --------------------------------------------------------------------------- #


_KNOWN_MANIFEST_KEYS: frozenset[str] = frozenset(
    {"name", "description", "steps"}
)
_KNOWN_STEP_KEYS: frozenset[str] = frozenset({"agent", "gate", "retry_if"})
_KNOWN_RETRY_KEYS: frozenset[str] = frozenset(
    {"metric", "operator", "threshold", "max_retries", "feedback_target"}
)


def _strip_unknown_keys(raw: dict[str, Any]) -> dict[str, Any]:
    """Strip unknown top-level/step/retry keys with stdout warnings.

    Returns a new dict containing only the keys Pydantic will accept.
    Each dropped key logs a warning so manifest authors see the typo
    without the pipeline crashing.
    """
    cleaned: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in _KNOWN_MANIFEST_KEYS:
            _log_warning(
                f"pipeline manifest has unknown top-level key {key!r}; "
                "ignoring"
            )
            continue
        cleaned[key] = value

    steps_raw = cleaned.get("steps")
    if isinstance(steps_raw, list):
        cleaned_steps: list[dict[str, Any]] = []
        for step_index, step in enumerate(steps_raw):
            if not isinstance(step, dict):
                raise ValueError(
                    f"pipeline step at index {step_index} is not a mapping: "
                    f"{step!r}"
                )
            cleaned_step: dict[str, Any] = {}
            for sk, sv in step.items():
                if sk not in _KNOWN_STEP_KEYS:
                    _log_warning(
                        f"pipeline step {step_index} has unknown key "
                        f"{sk!r}; ignoring"
                    )
                    continue
                cleaned_step[sk] = sv
            retry_raw = cleaned_step.get("retry_if")
            if isinstance(retry_raw, dict):
                cleaned_retry: dict[str, Any] = {}
                for rk, rv in retry_raw.items():
                    if rk not in _KNOWN_RETRY_KEYS:
                        _log_warning(
                            f"pipeline step {step_index} retry_if has "
                            f"unknown key {rk!r}; ignoring"
                        )
                        continue
                    cleaned_retry[rk] = rv
                cleaned_step["retry_if"] = cleaned_retry
            cleaned_steps.append(cleaned_step)
        cleaned["steps"] = cleaned_steps

    return cleaned


def _load_pipeline(pipeline_path: str) -> PipelineManifest:
    """Load and validate a pipeline YAML manifest from disk.

    Args:
        pipeline_path: Filesystem path to a YAML manifest (relative or
            absolute — tildes are expanded).

    Returns:
        A validated :class:`PipelineManifest` with ``steps`` populated.

    Raises:
        FileNotFoundError: If the path does not exist.
        ValueError: If the YAML parses to a non-mapping, declares zero
            steps, or fails Pydantic validation (unknown step agent,
            bad retry operator, etc.).
    """
    path = Path(pipeline_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(
            f"pipeline_path does not exist: {pipeline_path}"
        )

    text = path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(text)
    if parsed is None:
        raise ValueError(
            f"pipeline manifest {pipeline_path!r} is empty"
        )
    if not isinstance(parsed, dict):
        raise ValueError(
            f"pipeline manifest {pipeline_path!r} must be a mapping, got "
            f"{type(parsed).__name__}"
        )

    cleaned = _strip_unknown_keys(parsed)

    try:
        manifest = PipelineManifest.model_validate(cleaned)
    except ValidationError as exc:
        raise ValueError(
            f"pipeline manifest {pipeline_path!r} failed validation: {exc}"
        ) from exc

    if not manifest.steps:
        raise ValueError(
            f"pipeline manifest {pipeline_path!r} declares zero steps"
        )

    for step in manifest.steps:
        if step.agent not in _ALLOWED_AGENTS:
            raise ValueError(
                f"pipeline manifest {pipeline_path!r} has unknown agent "
                f"{step.agent!r} (allowed: {sorted(_ALLOWED_AGENTS)})"
            )
        if step.retry_if is not None:
            if step.retry_if.metric not in _ALLOWED_METRICS:
                raise ValueError(
                    f"retry_if.metric {step.retry_if.metric!r} is not one "
                    f"of {sorted(_ALLOWED_METRICS)}"
                )
            if step.retry_if.operator not in _ALLOWED_OPERATORS:
                raise ValueError(
                    f"retry_if.operator {step.retry_if.operator!r} is not "
                    f"one of {sorted(_ALLOWED_OPERATORS)}"
                )
            if step.retry_if.max_retries < 0:
                raise ValueError(
                    "retry_if.max_retries must be >= 0, got "
                    f"{step.retry_if.max_retries}"
                )

    return manifest


# --------------------------------------------------------------------------- #
# Retry predicate
# --------------------------------------------------------------------------- #


def _metric_value(review: ReviewScore, metric: str) -> float:
    """Read the scalar score for a named metric off a ReviewScore."""
    return float(getattr(review, metric))


def _should_retry(review: ReviewScore | None, retry_if: RetryIf | None) -> bool:
    """Return True when the review trips the retry_if predicate.

    A ``None`` ``review`` or ``None`` ``retry_if`` both return False —
    without a score we cannot evaluate the predicate, and without a
    retry rule there is nothing to retry.
    """
    if review is None or retry_if is None:
        return False
    value = _metric_value(review, retry_if.metric)
    if retry_if.operator == "<":
        return value < retry_if.threshold
    if retry_if.operator == "<=":
        return value <= retry_if.threshold
    return False


# --------------------------------------------------------------------------- #
# Director retry helper (with reviewer feedback)
# --------------------------------------------------------------------------- #


def _run_director_with_feedback(
    brief: CreativeBrief,
    footage_index_path: str,
    feedback: str,
) -> EditPlan:
    """Re-run the Director with Reviewer feedback injected into the prompt.

    This helper mirrors the event-drain + structured-output-parse pattern
    in ``src.agents.director.run_director`` but appends a "Reviewer
    feedback from previous attempt" block to the user message so the
    Director sees the concrete issues it must address in the revised
    EditPlan. The Director's :func:`build_director` is imported and
    called directly — no module surgery on ``director.py``.

    Args:
        brief: The same :class:`CreativeBrief` that drove the first
            attempt. The brief is intentionally NOT mutated so downstream
            validation (e.g. :class:`EditPlan.brief` echo) keeps working.
        footage_index_path: Path to the serialized
            :class:`~src.models.schemas.FootageIndex`. Must exist on disk
            so the Director's ``search_moments`` tool calls can read it.
        feedback: One or more Reviewer feedback strings concatenated by
            the caller (newest at the end). Injected verbatim into the
            user message.

    Returns:
        A validated :class:`EditPlan` produced by the retry-aware run.

    Raises:
        FileNotFoundError: If ``footage_index_path`` does not exist.
        RuntimeError: If the agent stream completes without producing a
            final response payload.
        pydantic.ValidationError: If the model emits JSON that does not
            satisfy the :class:`EditPlan` schema.
    """
    if not Path(footage_index_path).exists():
        raise FileNotFoundError(
            f"footage_index_path does not exist: {footage_index_path}"
        )

    agent = build_director(brief)
    runner = InMemoryRunner(agent=agent, app_name=_DIRECTOR_APP_NAME)

    feedback_block = (
        "\n\n## Reviewer feedback from previous attempt "
        "(address these issues in the new plan)\n"
        f"{feedback.strip()}\n"
        "\nProduce a revised EditPlan that directly addresses the feedback "
        "above. Keep the same brief and footage index — only change shot "
        "selection, ordering, trims, or overlays as needed to fix the "
        "issues called out."
    )

    user_message = genai_types.Content(
        role="user",
        parts=[
            genai_types.Part(
                text=(
                    "Build a REVISED EditPlan for the following brief.\n\n"
                    f"Brief JSON: {brief.model_dump_json()}\n\n"
                    f"Footage index path (use this exact path in tool "
                    f"calls): {footage_index_path}\n\n"
                    "Produce the revised EditPlan now."
                    + feedback_block
                )
            )
        ],
    )

    async def _go() -> EditPlan:
        session = await runner.session_service.create_session(
            app_name=_DIRECTOR_APP_NAME,
            user_id=_DIRECTOR_USER_ID,
        )
        final_text: str | None = None
        async for event in runner.run_async(
            user_id=_DIRECTOR_USER_ID,
            session_id=session.id,
            new_message=user_message,
        ):
            if (
                event.is_final_response()
                and event.content
                and event.content.parts
            ):
                text = "".join(
                    part.text
                    for part in event.content.parts
                    if getattr(part, "text", None)
                    and not getattr(part, "thought", False)
                )
                if text.strip():
                    final_text = text
        if not final_text:
            raise RuntimeError(
                "Director retry returned no final response text. Check "
                "that the FootageIndex still contains shots and that the "
                "model returned a non-empty structured response."
            )
        return EditPlan.model_validate_json(final_text)

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            return asyncio.run(_go())
        except RuntimeError as exc:
            if "no final response text" not in str(exc):
                raise
            last_exc = exc
            _log(
                f"[pipeline] director retry attempt {attempt + 1}/3 "
                "produced no final text; retrying"
            )
    raise last_exc  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Human approval gate
# --------------------------------------------------------------------------- #


def _summarize_edit_plan(plan: EditPlan) -> str:
    """Render a short, human-readable summary of an EditPlan for stdout."""
    lines = [
        "--- EditPlan ---",
        f"product: {plan.brief.product}",
        f"audience: {plan.brief.audience}",
        f"tone: {plan.brief.tone}",
        f"target duration: {plan.brief.duration_seconds}s",
        f"total duration: {plan.total_duration:.2f}s",
        f"entries ({len(plan.entries)}):",
    ]
    for entry in sorted(plan.entries, key=lambda e: e.position):
        duration = entry.end_trim - entry.start_trim
        overlay = f" overlay={entry.text_overlay!r}" if entry.text_overlay else ""
        lines.append(
            f"  [{entry.position}] {entry.shot_id} "
            f"trim=({entry.start_trim:.2f}, {entry.end_trim:.2f}) "
            f"dur={duration:.2f}s{overlay}"
        )
    lines.append("--- end EditPlan ---")
    return "\n".join(lines)


def _prompt_human_approval(plan: EditPlan) -> bool:
    """Print the edit plan and block until the user types y/n on stdin.

    Returns ``True`` when the user approves (``y``/``yes``, case
    insensitive), ``False`` when they decline (``n``/``no``). Any other
    input re-prompts up to three times before defaulting to declined.
    """
    _log(_summarize_edit_plan(plan))
    for _ in range(3):
        try:
            answer = input(
                "[pipeline] approve this EditPlan and proceed to Editor? "
                "[y/n]: "
            ).strip().lower()
        except EOFError:
            _log_warning(
                "human_approval gate got EOF on stdin; declining by default"
            )
            return False
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        _log_warning(f"unrecognized answer {answer!r}; expected y/n")
    _log_warning("no valid answer after 3 prompts; declining by default")
    return False


# --------------------------------------------------------------------------- #
# Step summaries
# --------------------------------------------------------------------------- #


def _summarize_director(plan: EditPlan | None) -> str:
    if plan is None:
        return "no plan produced"
    return (
        f"plan with {len(plan.entries)} entries, "
        f"total_duration={plan.total_duration:.2f}s"
    )


def _summarize_editor(video_path: str | None) -> str:
    if not video_path:
        return "no video produced"
    return f"rendered {video_path}"


def _summarize_reviewer(review: ReviewScore | None) -> str:
    if review is None:
        return "no review produced"
    return (
        f"overall={review.overall:.2f} "
        f"adherence={review.adherence:.2f} "
        f"pacing={review.pacing:.2f} "
        f"visual_quality={review.visual_quality:.2f} "
        f"watchability={review.watchability:.2f}"
    )


# --------------------------------------------------------------------------- #
# Main entrypoint
# --------------------------------------------------------------------------- #


def run_pipeline(
    pipeline_path: str,
    brief: CreativeBrief,
    footage_index_path: str,
    *,
    human_approval: bool = True,
) -> PipelineResult:
    """Execute a YAML-defined agent pipeline end to end.

    Reads the manifest at ``pipeline_path``, validates it against the
    :class:`PipelineManifest` schema, and walks the declared steps in
    order. Each step resolves to one of three agents:

    * ``director`` — calls :func:`src.agents.director.run_director` on
      the initial attempt and :func:`_run_director_with_feedback` on
      retries. Honors an optional ``gate: human_approval`` that blocks
      on stdin before proceeding. If the user declines, the pipeline
      returns early with a warning and no final video. When
      ``human_approval=False`` every ``gate: human_approval`` is
      auto-approved and no stdin read happens — use this for tests and
      non-interactive runs.
    * ``editor`` — calls :func:`src.agents.editor.run_editor` against
      the current :class:`EditPlan` and records the rendered MP4 path.
    * ``reviewer`` — calls :func:`src.agents.reviewer.run_reviewer`
      against the current video. If ``retry_if`` is declared and the
      score trips the predicate, the runner feeds the Reviewer feedback
      back into the Director (via :func:`_run_director_with_feedback`),
      re-runs the Editor, and re-reviews, up to ``max_retries`` times.
      After the retry budget is exhausted, a low score is logged as a
      warning rather than raising.

    Each step prints a start line, a duration, and a short summary of
    its output to stdout. The return value is a :class:`PipelineResult`
    that carries the final plan, video path, review, retry count,
    warnings, and full feedback history so callers can inspect the run.

    Args:
        pipeline_path: Path to a YAML pipeline manifest (see
            ``pipelines/ugc-ad.yaml`` for the canonical example).
        brief: The :class:`CreativeBrief` driving this run. Passed
            unchanged to the Director and Reviewer — the Editor reads
            brief context off the plan itself.
        footage_index_path: Path to the JSON-serialized
            :class:`~src.models.schemas.FootageIndex` produced by
            :mod:`src.pipeline.preprocess`. Must exist on disk.
        human_approval: When ``True`` (default), any step with
            ``gate: human_approval`` prints a human-readable plan
            summary and blocks on stdin for a ``y/n`` confirmation
            before the Editor runs. When ``False``, the gate is
            auto-approved, the summary is still printed, and stdin is
            not touched — intended for non-interactive test runs and
            automation. Keyword-only so callers cannot accidentally
            bypass the gate by passing positional arguments.

    Returns:
        A :class:`PipelineResult` describing what the pipeline produced
        and any warnings it raised. AC 8: a retry-budget exhaustion is
        reflected in ``warnings`` and does NOT raise.

    Raises:
        FileNotFoundError: If ``pipeline_path`` or
            ``footage_index_path`` does not exist.
        ValueError: If the manifest fails validation or an editor step
            is reached before a plan exists / a reviewer step is
            reached before a video exists.
        RuntimeError: Propagated from any underlying agent runner
            (e.g. the Editor reporting a tool failure).
    """
    if not Path(footage_index_path).exists():
        raise FileNotFoundError(
            f"footage_index_path does not exist: {footage_index_path}"
        )

    manifest = _load_pipeline(pipeline_path)
    _log(
        f"[pipeline] loaded manifest {manifest.name!r} with "
        f"{len(manifest.steps)} step(s) from {pipeline_path}"
    )

    result = PipelineResult()

    for step_index, step in enumerate(manifest.steps):
        _log_step_start(step.agent, step_index)
        t0 = time.monotonic()

        if step.agent == "director":
            # Only run the Director on the first director step; downstream
            # retries are driven from inside the reviewer branch via
            # _run_director_with_feedback so we do not re-plan twice in
            # the same pass.
            if result.edit_plan is None:
                result.edit_plan = _with_transient_retry(
                    run_director, brief, footage_index_path
                )
            if step.gate == "human_approval":
                if human_approval:
                    approved = _prompt_human_approval(result.edit_plan)
                else:
                    # Non-interactive bypass: still print the plan summary
                    # so the run log captures what the Editor will execute,
                    # but skip the stdin read entirely. Tests and batch
                    # pipelines pass human_approval=False to get here.
                    _log(_summarize_edit_plan(result.edit_plan))
                    _log(
                        "[pipeline] human_approval gate auto-approved "
                        "(human_approval=False kwarg)"
                    )
                    approved = True
                if not approved:
                    warning = (
                        "human_approval gate declined; aborting pipeline "
                        "before editor"
                    )
                    _log_warning(warning)
                    result.warnings.append(warning)
                    _log_step_end(
                        step.agent,
                        time.monotonic() - t0,
                        _summarize_director(result.edit_plan),
                    )
                    return result
            _log_step_end(
                step.agent,
                time.monotonic() - t0,
                _summarize_director(result.edit_plan),
            )
            continue

        if step.agent == "trim_refiner":
            if result.edit_plan is None:
                raise ValueError(
                    "pipeline step 'trim_refiner' reached before any "
                    "director step produced an EditPlan"
                )
            result.edit_plan = _with_transient_retry(
                refine_plan, result.edit_plan, footage_index_path
            )
            _log_step_end(
                step.agent,
                time.monotonic() - t0,
                f"refined {len(result.edit_plan.entries)} entries, "
                f"total_duration={result.edit_plan.total_duration:.2f}s",
            )
            continue

        if step.agent == "editor":
            if result.edit_plan is None:
                raise ValueError(
                    "pipeline step 'editor' reached before any director "
                    "step produced an EditPlan"
                )
            result.final_video_path = _with_transient_retry(
                run_editor, result.edit_plan, footage_index_path
            )
            _log_step_end(
                step.agent,
                time.monotonic() - t0,
                _summarize_editor(result.final_video_path),
            )
            continue

        if step.agent == "reviewer":
            if result.final_video_path is None:
                raise ValueError(
                    "pipeline step 'reviewer' reached before any editor "
                    "step produced a rendered video"
                )
            result.review = _with_transient_retry(
                run_reviewer, brief, result.final_video_path
            )
            _log(
                f"[pipeline] initial reviewer score: "
                f"{_summarize_reviewer(result.review)}"
            )

            retry_if = step.retry_if
            while (
                _should_retry(result.review, retry_if)
                and retry_if is not None
                and result.retries_used < retry_if.max_retries
            ):
                result.retries_used += 1
                assert result.review is not None  # for type-checkers
                result.feedback_history.append(result.review.feedback)
                metric_value = _metric_value(result.review, retry_if.metric)
                _log_warning(
                    f"reviewer {retry_if.metric}={metric_value:.2f} "
                    f"{retry_if.operator} {retry_if.threshold} — retry "
                    f"{result.retries_used}/{retry_if.max_retries}"
                )
                combined_feedback = "\n\n".join(result.feedback_history)
                revised_plan = _with_transient_retry(
                    _run_director_with_feedback,
                    brief,
                    footage_index_path,
                    feedback=combined_feedback,
                )
                _log(_summarize_edit_plan(revised_plan))
                result.edit_plan = _with_transient_retry(
                    refine_plan, revised_plan, footage_index_path
                )
                result.final_video_path = _with_transient_retry(
                    run_editor, result.edit_plan, footage_index_path
                )
                result.review = _with_transient_retry(
                    run_reviewer, brief, result.final_video_path
                )
                _log(
                    f"[pipeline] retry {result.retries_used} reviewer "
                    f"score: {_summarize_reviewer(result.review)}"
                )

            if _should_retry(result.review, retry_if) and retry_if is not None:
                assert result.review is not None
                final_metric = _metric_value(result.review, retry_if.metric)
                warning = (
                    f"retry budget exhausted after "
                    f"{result.retries_used}/{retry_if.max_retries} "
                    f"attempts; final {retry_if.metric}={final_metric:.2f} "
                    f"still {retry_if.operator} {retry_if.threshold}"
                )
                _log_warning(warning)
                result.warnings.append(warning)

            _log_step_end(
                step.agent,
                time.monotonic() - t0,
                _summarize_reviewer(result.review),
            )
            continue

        # Unreachable because the manifest loader rejects unknown agents,
        # but kept as a defensive final branch.
        raise ValueError(f"Unknown pipeline step agent: {step.agent!r}")

    _log(
        f"[pipeline] pipeline {manifest.name!r} complete — "
        f"retries_used={result.retries_used} warnings={len(result.warnings)}"
    )
    if result.warnings:
        for w in result.warnings:
            print(f"[pipeline] WARNING (final): {w}", flush=True, file=sys.stdout)

    return result
