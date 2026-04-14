"""Reviewer agent: watches the rendered cut and grades it against the brief.

The Reviewer is a Google ADK :class:`~google.adk.agents.Agent` named
``reviewer``, wired to ``gemini-3.1-pro`` with a single tool:

* :func:`src.tools.analyze.review_output` — sends the rendered video to
  Gemini native-video and returns a structured
  :class:`~src.models.schemas.ReviewScore`.

Unlike the Director (which plans) and the Editor (which executes), the
Reviewer only judges. It takes a rendered ad (MP4/MOV/etc.) and the
original :class:`~src.models.schemas.CreativeBrief`, asks Gemini to score
the cut across four qualitative dimensions plus a holistic ``overall``,
and returns actionable feedback the upstream loop can use to re-edit.

Scoring contract
----------------

Every dimension is a float in ``[0.0, 1.0]``:

* ``adherence`` — how well the finished cut honors ``brief.product`` /
  ``brief.audience`` / ``brief.tone``.
* ``pacing`` — energy arc, hook strength, and cut rhythm.
* ``visual_quality`` — composition, framing, color, clarity.
* ``watchability`` — would a real viewer keep watching to the end.
* ``overall`` — holistic judgment, NOT a plain mean. One broken dimension
  can drag the whole score down when it deserves to.

The ``feedback`` field is free text. If ``overall < 0.7`` it MUST include
specific, actionable suggestions that reference beats, clip numbers, or
timestamps (e.g. "hook at 0:00–0:03 is weak — replace clip 1 with the
product close-up"). Vague feedback like "could be better" is not allowed.

The agent's ``output_schema`` is :class:`~src.models.schemas.ReviewScore`
so the final response is forced to validate against the Pydantic
contract. Google ADK's ``_OutputSchemaRequestProcessor`` bridges
``tools + output_schema`` for Gemini models, so a single
:class:`~google.adk.agents.Agent` can both call ``review_output`` and
emit a strict structured response (no SequentialAgent split required).

Surfaces
--------

* Module-level :data:`reviewer` — a constructed Agent instance with the
  base instruction (no brief baked in). Useful for ADK auto-discovery
  (``adk web``, ``adk eval``) and module-level introspection.
* :func:`build_reviewer` — factory that takes a
  :class:`~src.models.schemas.CreativeBrief` and returns a fresh Agent
  whose instruction has the brief details baked into the runtime prompt.
  Use this for real runs.
* :func:`build_runtime_instruction` — pure helper that returns the
  runtime instruction string for a given brief (exposed so callers can
  preview what the agent will see before running it).
* :func:`run_reviewer` — synchronous end-to-end runner that wires up an
  ``InMemoryRunner``, sends the brief + rendered video path as the user
  message, and returns a validated
  :class:`~src.models.schemas.ReviewScore`.

Importing this module instantiates the module-level ``reviewer`` Agent
(no Gemini API calls). Tool invocations and the Gemini round-trip only
happen inside :func:`run_reviewer`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from google.adk.agents import Agent
from google.adk.runners import InMemoryRunner
from google.genai import types as genai_types

from src.models.schemas import CreativeBrief, ReviewScore
from src.tools.analyze import _ALLOWED_REVIEW_VIDEO_PATH, review_output

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_MODEL_ID = "gemini-3.1-pro-preview"
_AGENT_NAME = "reviewer"
_APP_NAME = "agentic-video-editor"
_USER_ID = "reviewer-user"

_AGENT_DESCRIPTION = (
    "Creative reviewer that watches a rendered ad and scores it against "
    "the brief, returning actionable feedback."
)

REVIEWER_INSTRUCTION = """\
You are the Reviewer agent for an AI video editor. Your job is to watch a
rendered ad and grade it honestly against the creative brief that
produced it. You are the critic — you do not plan edits, you do not
render, you judge the finished cut and tell the upstream loop what to
fix.

## Your Mission
Given a creative brief and a rendered video path in the user message,
call the review_output tool with that exact video path and the brief,
then return a single ReviewScore that validates against the Pydantic
schema.

## Scoring Contract
Every score is a float in [0.0, 1.0]. Do not clamp to round numbers —
use the full range.

- adherence: how well the finished cut honors brief.product,
  brief.audience, and brief.tone. Is the right product on screen? Does
  the tone match the stated audience?
- pacing: energy arc, hook strength, and cut rhythm. A great cut opens
  strong and varies energy; a bad cut drags or feels choppy.
- visual_quality: composition, framing, color, and clarity. Does the
  footage look like a finished ad, not rushed filler?
- watchability: would a real viewer keep watching to the end. This is
  the single most important retention signal.
- overall: holistic judgment. NOT a plain mean of the four dimensions
  above. One broken dimension (especially a weak hook) can and should
  drag the whole score down.

## Feedback Contract
The feedback field must be concrete, actionable text that identifies
what works and what should change.

If overall >= 0.7, feedback can summarize strengths plus any minor
polish notes.

If overall < 0.7, feedback MUST include specific actionable
suggestions that reference beats, clip numbers, or timestamps. Examples
of good feedback:

- "hook at 0:00–0:03 is weak — replace clip 1 with the product close-up
  from the tool output"
- "clip 3 drags, shorten by ~1.5s or swap for a higher-energy reaction
  shot"
- "tone is too formal for the stated audience — re-cut with the casual
  reaction shots instead of the voiceover takes"

Vague feedback like "could be better", "try harder", or "needs more
energy" is NOT allowed when overall < 0.7.

## Available Tools
- review_output(video_path, brief) — sends the rendered video to Gemini
  3.1 Pro and returns a ReviewScore. Use this tool exactly once with the
  video path and the brief JSON you were given. Do not paraphrase or
  truncate the brief before passing it to the tool.

## Workflow
1. Read the brief JSON and the rendered video_path from the user
   message.
2. Call review_output(video_path, brief) exactly once with the values
   you were given. Pass the brief JSON verbatim.
3. Take the tool's ReviewScore and return it as your final structured
   output. If the tool's feedback is already specific and the overall
   score honors the rubric above, you may return the tool's output
   unchanged. If the feedback is vague or fails the < 0.7 actionable
   rule, rewrite feedback to be specific before returning — do NOT
   invent new scores.

## Hard Constraints
- Every score must be a float in [0.0, 1.0].
- If overall < 0.7, feedback must contain specific actionable
  suggestions referencing beats, clips, or timestamps.
- The output must validate against the ReviewScore Pydantic schema.
- Return ONLY the JSON object — no preamble, no commentary, no
  markdown fences.
"""


# --------------------------------------------------------------------------- #
# Instruction builder
# --------------------------------------------------------------------------- #


def build_runtime_instruction(brief: CreativeBrief) -> str:
    """Return the full runtime instruction for a brief-aware Reviewer.

    Combines the static :data:`REVIEWER_INSTRUCTION` with a brief header
    (product, audience, tone, target duration, style_ref) so the Reviewer
    has the grading rubric context inline. Unlike the Director, the
    Reviewer does not load style skills — style-skill YAML is a planning
    concern, and the Reviewer grades whatever was actually rendered.

    This helper is exposed so callers (tests, debug tools) can preview
    exactly what the Reviewer will see without spinning up an Agent.

    Args:
        brief: The :class:`CreativeBrief` to bake into the instruction.

    Returns:
        A multi-line instruction string ready to pass as
        ``Agent.instruction``.
    """
    header = (
        "## Brief Context (grade the rendered cut against these fields)\n"
        f"- product: {brief.product}\n"
        f"- audience: {brief.audience}\n"
        f"- tone: {brief.tone}\n"
        f"- target duration (seconds): {brief.duration_seconds}\n"
        f"- style_ref: {brief.style_ref or '(none)'}\n"
    )

    return "\n".join([REVIEWER_INSTRUCTION.rstrip(), "", header]) + "\n"


# --------------------------------------------------------------------------- #
# Agent factory
# --------------------------------------------------------------------------- #


def build_reviewer(brief: CreativeBrief) -> Agent:
    """Construct a brief-aware Reviewer ADK Agent without running it.

    Each call returns a fresh :class:`google.adk.agents.Agent` whose
    instruction has the brief details baked into the runtime prompt. The
    agent is wired with a single tool — :func:`review_output` — and
    ``output_schema=ReviewScore`` so the final response validates
    against the Pydantic contract.

    Use this factory whenever you have a concrete brief in hand. The
    module-level :data:`reviewer` instance is reserved for ADK
    auto-discovery and tests that do not need brief-specific context.

    Args:
        brief: The creative brief whose product/audience/tone/duration
            and optional ``style_ref`` shape the agent's instruction.

    Returns:
        A configured :class:`Agent` named ``reviewer`` with model
        ``gemini-3.1-pro``, the ``review_output`` tool attached, and
        ``output_schema=ReviewScore``.
    """
    instruction = build_runtime_instruction(brief)
    return Agent(
        name=_AGENT_NAME,
        model=_MODEL_ID,
        description=_AGENT_DESCRIPTION,
        instruction=instruction,
        tools=[review_output],
        output_schema=ReviewScore,
    )


# --------------------------------------------------------------------------- #
# Module-level default instance
# --------------------------------------------------------------------------- #

#: Default Reviewer agent without any brief baked in. Useful for ADK
#: discovery (``adk web``, ``adk eval``), tests, and module-level
#: introspection. For real runs prefer :func:`build_reviewer` so the
#: brief details land inside the instruction.
reviewer: Agent = Agent(
    name=_AGENT_NAME,
    model=_MODEL_ID,
    description=_AGENT_DESCRIPTION,
    instruction=REVIEWER_INSTRUCTION,
    tools=[review_output],
    output_schema=ReviewScore,
)


# --------------------------------------------------------------------------- #
# Synchronous end-to-end runner
# --------------------------------------------------------------------------- #


def run_reviewer(
    brief: CreativeBrief,
    video_path: str,
) -> ReviewScore:
    """Run the Reviewer agent end-to-end and return a validated ReviewScore.

    Builds a fresh brief-aware :class:`Agent` via :func:`build_reviewer`,
    creates a single-turn in-memory session, sends one user message
    containing the brief and the rendered video path, drains the event
    stream, and parses the reviewer's final structured-output payload as
    a :class:`ReviewScore`.

    The agent's ``output_schema`` forces structured JSON output so the
    final message body is validated by Pydantic via
    :meth:`ReviewScore.model_validate_json`.

    Args:
        brief: The :class:`CreativeBrief` that drove this edit. Used to
            anchor grading — the Reviewer scores the cut relative to the
            brief's product, audience, tone, and target duration.
        video_path: Path to the rendered ad video produced by the
            Editor agent (US-006). Must exist on disk so the Reviewer's
            ``review_output`` tool call can read it.

    Returns:
        A validated :class:`ReviewScore` with five float dimensions in
        ``[0.0, 1.0]`` and a non-empty ``feedback`` string.

    Raises:
        FileNotFoundError: If ``video_path`` does not exist.
        RuntimeError: If the agent stream completes without producing a
            final response payload, or if the parsed
            :class:`ReviewScore` has empty ``feedback`` (the grading
            contract requires non-empty feedback on every response).
        pydantic.ValidationError: If the model emits JSON that does not
            satisfy the :class:`ReviewScore` schema.
    """
    if not Path(video_path).exists():
        raise FileNotFoundError(
            f"video_path does not exist: {video_path}"
        )

    # Defense-in-depth against prompt-injection path exfiltration: bind the
    # pre-approved, resolved video path in a ContextVar so ``review_output``
    # can reject any path the model fabricates. The FileNotFoundError guard
    # above remains — this binding is additive and tightens the tool's trust
    # boundary so the Reviewer can only read the cut we explicitly approved.
    canonical_video_path = str(Path(video_path).resolve())

    agent = build_reviewer(brief)
    runner = InMemoryRunner(agent=agent, app_name=_APP_NAME)

    user_message = genai_types.Content(
        role="user",
        parts=[
            genai_types.Part(
                text=(
                    "Watch the rendered ad at this path and score it "
                    "against the brief.\n\n"
                    f"Brief JSON: {brief.model_dump_json()}\n\n"
                    f"Rendered video path (use this exact path in the "
                    f"review_output tool call): {video_path}\n\n"
                    "Produce the ReviewScore now."
                )
            )
        ],
    )

    async def _go() -> ReviewScore:
        session = await runner.session_service.create_session(
            app_name=_APP_NAME,
            user_id=_USER_ID,
        )
        final_text: str | None = None
        async for event in runner.run_async(
            user_id=_USER_ID,
            session_id=session.id,
            new_message=user_message,
        ):
            if (
                event.is_final_response()
                and event.content
                and event.content.parts
            ):
                # Concatenate any non-thought text parts so we capture the
                # model's full structured response even if it is split.
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
                "Reviewer agent returned no final response text. Check "
                "that the rendered video exists and that the model "
                "returned a non-empty structured response."
            )
        score = ReviewScore.model_validate_json(final_text)
        # Defensive safety net: the prompt instructs the model to always
        # return concrete feedback (and mandatory actionable suggestions
        # when overall < 0.7). The ReviewScore schema only types feedback
        # as ``str`` so an empty string would otherwise slip through.
        # Reject empty/whitespace feedback here — an upstream loop cannot
        # act on it and the failure mode would be silent otherwise.
        if not score.feedback.strip():
            raise RuntimeError(
                "Reviewer agent returned a ReviewScore with empty "
                "feedback. The grading contract requires non-empty "
                "feedback on every response (and specific actionable "
                f"suggestions when overall < 0.7; got overall={score.overall})."
            )
        return score

    _token = _ALLOWED_REVIEW_VIDEO_PATH.set(canonical_video_path)
    try:
        return asyncio.run(_go())
    finally:
        _ALLOWED_REVIEW_VIDEO_PATH.reset(_token)
