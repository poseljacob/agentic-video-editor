"""Director agent: creative brain that turns briefs into structured EditPlans.

The Director is a Google ADK :class:`~google.adk.agents.Agent` named
``director``, wired to ``gemini-3.1-pro`` with two tools:

* :func:`src.tools.analyze.search_moments` — local lexical ranker over a
  pre-built FootageIndex.
* :func:`src.tools.analyze.analyze_footage` — Gemini native-video deep dive
  on a single clip (expensive, used sparingly on top candidates).

The agent's ``output_schema`` is :class:`~src.models.schemas.EditPlan` so
the final response is forced to validate against the Pydantic contract.
Google ADK's ``_OutputSchemaRequestProcessor`` transparently bridges
``tools + output_schema`` for Gemini models via an internal
``set_model_response`` shim, so a single :class:`~google.adk.agents.Agent`
can both call tools and emit a strict structured response (no
SequentialAgent split required).

The Director never executes edits — it only decides which shots to use
and how to sequence them. The Editor agent (US-006) handles rendering.

Surfaces
--------

* Module-level :data:`director` — a constructed Agent instance with the
  base instruction (no brief baked in). Useful for ADK auto-discovery
  (``adk web``, ``adk eval``) and module-level introspection.
* :func:`build_director` — factory that takes a
  :class:`~src.models.schemas.CreativeBrief` and returns a fresh Agent
  whose instruction has the brief details (and any loaded style skill)
  baked in. Use this for real runs.
* :func:`build_runtime_instruction` — pure helper that returns the runtime
  instruction string for a given brief (exposed so callers can preview
  what the agent will see before running it).
* :func:`load_style_skill` — pure helper that loads a YAML style file
  from disk into a dict, returning ``None`` on missing or invalid input
  (graceful degradation, never raises).
* :func:`run_director` — synchronous end-to-end runner that wires up an
  ``InMemoryRunner``, sends the brief + footage index path as the user
  message, and returns a validated :class:`~src.models.schemas.EditPlan`.

Shot ID convention
------------------

Every :class:`~src.models.schemas.EditPlanEntry` references a real shot
from the footage index via a ``shot_id`` of the form::

    "{source_file}#{start_time}"

Where ``source_file`` is the :attr:`~src.models.schemas.Shot.source_file`
string and ``start_time`` is the :attr:`~src.models.schemas.Shot.start_time`
float (in seconds) — both intrinsic fields on every Shot returned by
:func:`~src.tools.analyze.search_moments`. The Editor agent (US-006) resolves
entries by splitting on the LAST ``"#"``, parsing the suffix as a ``float``,
and matching against ``FootageIndex.shots`` by ``(source_file, start_time)``
with a small epsilon. This format was chosen over a positional index so
the contract does not depend on the ordering of shots inside the
FootageIndex (which can change between preprocess runs).

The Director MUST NOT invent shot_ids — every entry's ``shot_id`` has to
map to a :class:`~src.models.schemas.Shot` returned by
:func:`~src.tools.analyze.search_moments` for the supplied FootageIndex.

Importing this module instantiates the module-level ``director`` Agent
(no Gemini API calls). Tool invocations and the Gemini round-trip only
happen inside :func:`run_director`.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import yaml
from google.adk.agents import Agent
from google.adk.runners import InMemoryRunner
from google.genai import types as genai_types

from src.models.schemas import CreativeBrief, EditPlan
from src.tools.analyze import analyze_footage, search_moments

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_MODEL_ID = "gemini-3.1-pro-preview"
_AGENT_NAME = "director"
_APP_NAME = "agentic-video-editor"
_USER_ID = "director-user"

_AGENT_DESCRIPTION = (
    "Creative director that converts a brief + footage index into a "
    "structured EditPlan."
)

DIRECTOR_INSTRUCTION = """\
You are the Director agent for an AI video editor. Your job is to turn a
creative brief and a pre-built FootageIndex into a structured EditPlan
that a downstream Editor agent will render. You are the creative brain —
you do not execute edits, you decide what the final cut should look like.

## Your Mission
Given a creative brief and a footage_index_path provided in the user
message, produce a single EditPlan that selects, trims, orders, and
annotates the clips that best deliver the brief. The Editor agent will
take your EditPlan and run FFmpeg against it.

## Creative Principles
- Pacing variety: alternate energy levels — never stack three high-energy
  or three low-energy clips back to back. Contrast keeps viewers watching.
- Hook quality: the first 3 seconds must grab attention. Lead with the
  highest-energy or most visually striking clip you can justify against
  the brief. A weak hook means the rest of the cut never gets seen.
- Narrative arc for ad format: problem -> solution -> proof -> CTA. Map
  your selected shots to these beats. Not every brief needs all four
  beats, but the ordering matters.
- Target duration: the sum of (end_trim - start_trim) across A-ROLL
  entries only must be within plus or minus 10 percent of the requested
  duration. B-Roll entries are overlays and do NOT count toward duration.
- Entry count: 5 to 10 EditPlanEntry items total (A-Roll + B-Roll).
  Fewer than 5 lacks variety, more than 10 is too choppy for a short ad.

## A-Roll and B-Roll
Shots in the FootageIndex have a `roll_type` field: "a-roll" (on-camera
talent, talking head), "b-roll" (product close-ups, textures, packaging,
application shots, environment), or "unknown".

You have FULL creative control over how to use these. Think like a real
editor — the best UGC ads mix footage types to keep things visually
interesting. B-Roll can be used as cutaways, openers, transitions, proof
shots, or anywhere your creative instinct says the viewer needs
something new to look at. A-Roll carries the narrative; B-Roll sells
the product visually.

Search for B-Roll candidates alongside your A-Roll searches — queries
like "product", "texture", "application", "packaging", "close-up" will
surface them. B-Roll clips typically have no transcript (visual-only),
which is normal.

### CRITICAL — how B-Roll is rendered
B-Roll entries are VIDEO-ONLY cutaways overlaid on the A-Roll base. The
Editor sequences ONLY A-Roll clips to create a base video with continuous
narration audio. B-Roll clips are composited on top as visual overlays —
the talent's voice keeps playing underneath.

This means B-Roll entries DO NOT add to the timeline duration. Only
A-Roll entries contribute to total_duration. When planning:
- The sum of (end_trim - start_trim) across A-ROLL entries alone must
  hit the target duration (within ±10%).
- B-Roll entries are placed between A-Roll entries to mark WHERE on the
  timeline the cutaway appears (its timeline position = cumulative
  duration of all preceding A-Roll entries).
- B-Roll duration should not exceed the A-Roll clip it overlays.
- total_duration in the EditPlan = sum of A-ROLL entry durations ONLY.
  Do NOT include B-Roll durations in total_duration.

## Available Tools
- search_moments(footage_index_path, query, min_relevance, max_results) —
  rank shots in the footage index by lexical relevance to a free-text
  query. Local, cheap, deterministic. Use it liberally to retrieve
  candidates for each narrative beat.
- analyze_footage(video_path, brief) — deep scene-by-scene analysis of a
  single clip via Gemini native video input. Expensive. Use it on at most
  2 to 4 top candidates to get energy_level, visual_quality, and
  key_quote signals before final selection. Skip this for candidates that
  are obviously strong from description and transcript alone.

## Workflow
1. Read the brief and footage_index_path from the user message.
2. For each narrative beat (hook, problem, solution, proof, CTA), call
   search_moments with a query that captures that beat. Use a
   min_relevance around 0.2 and ask for 3 to 5 candidates per beat.
3. Search for B-Roll candidates too: call search_moments with queries
   like "product", "texture", "application", "packaging", "close-up",
   "b-roll". Great ads use a mix of footage types.
4. Identify the top 2 to 4 most promising candidates across all beats and
   call analyze_footage on each one's source_file to get richer signal.
5. Select and sequence your final clips. You have full creative freedom —
   use your judgment on how to mix A-Roll and B-Roll, how long each clip
   should be, and where cutaways or product shots create the most impact.
   The goal is a polished, engaging ad that feels professionally edited.
5. For each selected shot, build an EditPlanEntry with:
   - shot_id: use the format "<source_file>#<start_time>" where
     source_file is the Shot.source_file string returned by your tools
     and start_time is the Shot.start_time float (in seconds, as it
     appears on the Shot). Both fields are present on every Shot
     returned by search_moments, so you can always construct the
     shot_id directly from the tool output. The Editor agent resolves
     this by splitting on the LAST '#', parsing the suffix as a float,
     and matching against FootageIndex.shots with a small epsilon.
     NEVER invent shot_ids. NEVER reference a source_file or start_time
     that did not come from a search_moments result.
   - start_trim, end_trim: floats in seconds, relative to the source
     file, defining the in/out points for this entry. start_trim must be
     >= the shot's start_time and end_trim must be <= the shot's
     end_time.
   - position: zero-indexed slot in the final cut. Positions across the
     entries must form a contiguous 0..N-1 sequence with no gaps and no
     duplicates.
   - text_overlay: leave this null unless the user explicitly asks for
     a non-speech title card. Captions are generated later from real
     word timestamps during editing, so DO NOT use this field for spoken
     dialogue captions.
   - transition: optional transition into this entry (for example "cut",
     "fade", "dissolve"). Default to "cut" or omit.
6. Output a single EditPlan with:
   - brief: the same CreativeBrief you were given, unchanged.
   - entries: 5 to 10 EditPlanEntry items in the order they should play.
   - music_path: null. The Editor agent picks music later.
   - total_duration: the sum of (end_trim - start_trim) across A-ROLL
     entries ONLY. Do NOT include B-Roll durations — they are overlays
     and do not add to the timeline.

## Hard Constraints
- Every shot_id must reference a real shot in the FootageIndex you
  searched. Do not hallucinate timestamps or source files.
- total_duration must be greater than zero and within plus or minus 10
  percent of the requested duration. Remember: only A-Roll durations
  count toward total_duration.
- The output must validate against the EditPlan Pydantic schema. Return
  ONLY the JSON object — no preamble, no commentary, no markdown fences.
"""


# --------------------------------------------------------------------------- #
# Style skill loader
# --------------------------------------------------------------------------- #


def load_style_skill(style_ref: str | None) -> dict[str, Any] | None:
    """Load a style-skill YAML file from disk into a dict.

    The Director uses style skills to bias structural decisions (hook
    duration, segment ordering, text overlay position, music mood). The
    file is optional: a brief may reference no style at all, or may
    point at a path that does not exist on this machine. This loader
    degrades gracefully in either case so the Director can still produce
    a plan — missing or malformed inputs log a warning and return
    ``None`` instead of raising.

    Args:
        style_ref: Path (relative or absolute) to a YAML style template,
            or ``None``/empty string to skip loading. Tildes are expanded.

    Returns:
        The parsed YAML mapping as a ``dict[str, Any]`` if the file
        exists, is non-empty, and parses to a mapping. Returns ``None``
        when ``style_ref`` is falsy, the file does not exist, the file
        is empty, parsing fails, or the parsed YAML is not a mapping.
    """
    if not style_ref:
        return None

    path = Path(style_ref).expanduser()
    if not path.exists() or not path.is_file():
        logger.warning(
            "style_ref points to a missing file: %r; "
            "continuing without style template",
            style_ref,
        )
        return None

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("could not read style_ref %r: %s", style_ref, exc)
        return None

    if not text.strip():
        logger.warning("style_ref %r is empty; skipping", style_ref)
        return None

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        logger.warning(
            "style_ref %r failed to parse as YAML: %s", style_ref, exc
        )
        return None

    if not isinstance(data, dict):
        logger.warning(
            "style_ref %r parsed to %s, expected a mapping; skipping",
            style_ref,
            type(data).__name__,
        )
        return None

    return data


def _summarize_style(style: dict[str, Any] | None) -> str:
    """Render the parsed style skill as a YAML block for the prompt.

    The style skill YAML has an open schema — US-008 defines a
    ``dtc-testimonial.yaml`` example, but future styles may use flat keys,
    nested mappings (``structure: {hook: 3s, problem: 5s, ...}``), or
    shapes we cannot predict today. To avoid regressing nested values
    (dropping hook/segment durations when they live inside a parent key),
    we serialize the whole dict back to YAML and inject it verbatim under
    a guidance header. The model can read YAML fluently, so this
    preserves every field the author put in the file without us guessing
    which keys matter.

    Args:
        style: Parsed style-skill dict (from :func:`load_style_skill`),
            or ``None``.

    Returns:
        A multi-line string with a header and a YAML block containing
        every key/value pair from the parsed style skill, or an empty
        string if no style is available.
    """
    if not style:
        return ""

    try:
        body = yaml.safe_dump(
            style,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        ).rstrip()
    except yaml.YAMLError:
        # Fall back to a plain repr — the dict came from safe_load so
        # this should never trip, but we still refuse to raise from a
        # prompt builder.
        body = repr(style)

    return (
        "## Style skill guidance (honor every field below)\n"
        "```yaml\n"
        f"{body}\n"
        "```"
    )


# --------------------------------------------------------------------------- #
# Instruction builder
# --------------------------------------------------------------------------- #


def build_runtime_instruction(brief: CreativeBrief) -> str:
    """Return the full runtime instruction for a brief-aware Director.

    Combines the static :data:`DIRECTOR_INSTRUCTION` with a brief header
    (product, audience, tone, target duration, style_ref) and an
    optional style-skill summary loaded from ``brief.style_ref`` via
    :func:`load_style_skill`. When ``brief.style_ref`` is ``None`` or
    points at a missing/invalid file, the style block is omitted and the
    Director degrades to its generic principles.

    This helper is exposed so callers (tests, debug tools) can preview
    exactly what the Director will see without spinning up an Agent.

    Args:
        brief: The :class:`CreativeBrief` to bake into the instruction.

    Returns:
        A multi-line instruction string ready to pass as
        ``Agent.instruction``.
    """
    style = load_style_skill(brief.style_ref)
    style_block = _summarize_style(style)

    header = (
        "## Brief Context (echo this brief verbatim into EditPlan.brief)\n"
        f"- product: {brief.product}\n"
        f"- audience: {brief.audience}\n"
        f"- tone: {brief.tone}\n"
        f"- target duration (seconds): {brief.duration_seconds}\n"
        f"- style_ref: {brief.style_ref or '(none)'}\n"
    )

    parts: list[str] = [DIRECTOR_INSTRUCTION.rstrip(), "", header]
    if style_block:
        parts.extend(["", style_block])
    return "\n".join(parts) + "\n"


# --------------------------------------------------------------------------- #
# Agent factory
# --------------------------------------------------------------------------- #


def build_director(brief: CreativeBrief) -> Agent:
    """Construct a brief-aware Director ADK Agent without running it.

    Each call returns a fresh :class:`google.adk.agents.Agent` whose
    instruction has the brief details (and any loaded style skill) baked
    in. The agent is wired with both tools — ``analyze_footage`` and
    ``search_moments`` — and ``output_schema=EditPlan`` so the final
    response validates against the Pydantic contract.

    Use this factory whenever you have a concrete brief in hand. The
    module-level :data:`director` instance is reserved for ADK
    auto-discovery and tests that do not need brief-specific context.

    Args:
        brief: The creative brief whose product/audience/tone/duration
            and optional ``style_ref`` shape the agent's instruction.

    Returns:
        A configured :class:`Agent` named ``director`` with model
        ``gemini-3.1-pro``, the two analysis tools attached, and
        ``output_schema=EditPlan``.
    """
    instruction = build_runtime_instruction(brief)
    return Agent(
        name=_AGENT_NAME,
        model=_MODEL_ID,
        description=_AGENT_DESCRIPTION,
        instruction=instruction,
        tools=[analyze_footage, search_moments],
        output_schema=EditPlan,
    )


# --------------------------------------------------------------------------- #
# Module-level default instance
# --------------------------------------------------------------------------- #

#: Default Director agent without any brief baked in. Useful for ADK
#: discovery (``adk web``, ``adk eval``), tests, and module-level
#: introspection. For real runs prefer :func:`build_director` so the
#: brief details land inside the instruction.
director: Agent = Agent(
    name=_AGENT_NAME,
    model=_MODEL_ID,
    description=_AGENT_DESCRIPTION,
    instruction=DIRECTOR_INSTRUCTION,
    tools=[analyze_footage, search_moments],
    output_schema=EditPlan,
)


# --------------------------------------------------------------------------- #
# Synchronous end-to-end runner
# --------------------------------------------------------------------------- #


def run_director(
    brief: CreativeBrief,
    footage_index_path: str,
) -> EditPlan:
    """Run the Director agent end-to-end and return a validated EditPlan.

    Builds a fresh brief-aware :class:`Agent` via :func:`build_director`,
    creates a single-turn in-memory session, sends one user message
    containing the brief and the footage index path, drains the event
    stream, and parses the planner's final structured-output payload as
    an :class:`EditPlan`.

    The agent's ``output_schema`` forces structured JSON output so the
    final message body is validated by Pydantic via
    :meth:`EditPlan.model_validate_json`.

    Args:
        brief: The :class:`CreativeBrief` driving this edit.
            ``brief.duration_seconds`` is the target final cut length;
            ``brief.style_ref`` is an optional path to a YAML style skill.
        footage_index_path: Path to the JSON-serialized
            :class:`~src.models.schemas.FootageIndex` produced by
            :mod:`src.pipeline.preprocess`. Must exist on disk so the
            Director's ``search_moments`` tool calls can read it.

    Returns:
        A validated :class:`EditPlan` with 3-8 entries totaling
        approximately ``brief.duration_seconds``.

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
    runner = InMemoryRunner(agent=agent, app_name=_APP_NAME)

    user_message = genai_types.Content(
        role="user",
        parts=[
            genai_types.Part(
                text=(
                    "Build an EditPlan for the following brief.\n\n"
                    f"Brief JSON: {brief.model_dump_json()}\n\n"
                    f"Footage index path (use this exact path in tool "
                    f"calls): {footage_index_path}\n\n"
                    "Produce the EditPlan now."
                )
            )
        ],
    )

    async def _go() -> EditPlan:
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
                "Director agent returned no final response text. Check "
                "that the FootageIndex contains at least one shot and "
                "that the model returned a non-empty structured response."
            )
        return EditPlan.model_validate_json(final_text)

    # The ADK InMemoryRunner occasionally completes the event stream
    # without producing a final text response (the model emits only
    # function_call parts). Retry up to 2 extra times before giving up.
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            return asyncio.run(_go())
        except RuntimeError as exc:
            if "no final response text" not in str(exc):
                raise
            last_exc = exc
            logger.warning(
                "Director attempt %d/3 produced no final text; retrying",
                attempt + 1,
            )
    raise last_exc  # type: ignore[misc]
