"""Editor agent: pure executor that renders an EditPlan into a final video.

The Editor is a Google ADK :class:`~google.adk.agents.Agent` named
``editor``, wired to ``gemini-3.1-pro`` with clip/caption/render tools:

* :func:`src.tools.edit.cut_clip` — extract a sub-clip via stream copy.
* :func:`src.tools.edit.sequence_clips` — concat clips via the FFmpeg
  concat demuxer.
* :func:`src.tools.captions.generate_ass_captions` — build a TikTok-style
  ASS subtitle file from real per-word timestamps.
* :func:`src.tools.captions.burn_ass_subtitles` — burn the ASS subtitles
  onto a clip with FFmpeg's ``ass`` filter.
* :func:`src.tools.edit.add_music` — mix a music track under the video's
  original audio.
* :func:`src.tools.render.render_final` — H.264 MP4 export with
  aspect-ratio-safe scaling.

Unlike the Director (US-005), the Editor makes **no creative decisions**.
It is a pure executor that follows the :class:`~src.models.schemas.EditPlan`
 mechanically: cut each entry's source clip, generate and burn captions
 from real speech timing when the trimmed clip contains spoken words,
 sequence the clips in ``position`` order, mix in music if specified, and
 render the final MP4. If a clip referenced in the plan does not exist,
its trim window falls outside the underlying shot, or any tool call
fails, the Editor reports the error verbatim instead of guessing or
silently producing a partial file.

The Editor returns a plain text path (the final rendered MP4) — not a
structured response — so it is wired *without* an ``output_schema``.

Surfaces
--------

* Module-level :data:`editor` — a constructed Agent instance with the
  base instruction (no plan baked in). Useful for ADK auto-discovery
  (``adk web``, ``adk eval``) and module-level introspection.
* :func:`build_editor` — factory that takes an
  :class:`~src.models.schemas.EditPlan` plus a path to the source
  :class:`~src.models.schemas.FootageIndex` and returns a fresh Agent
  whose instruction has the fully resolved plan (per-entry source files,
  trim windows, caption paths, intermediate paths, final path) baked in.
  Use this for real runs.
* :func:`build_runtime_instruction` — pure helper that returns the
  runtime instruction string for a given plan + footage index path
  (exposed so callers can preview what the agent will see before running
  it).
* :func:`run_editor` — synchronous end-to-end runner that validates the
  plan against the footage index, pre-flights every source file and the
  music track, wires up an ``InMemoryRunner``, drives the agent through
  one turn, verifies the final MP4 exists on disk, and returns its path.

Shot ID resolution contract
---------------------------

Every :class:`~src.models.schemas.EditPlanEntry` references a shot via a
``shot_id`` of the form::

    "{source_file}#{start_time}"

where ``source_file`` is the :attr:`~src.models.schemas.Shot.source_file`
string and ``start_time`` is the :attr:`~src.models.schemas.Shot.start_time`
float (in seconds). To resolve an entry, :func:`_resolve_shot` splits
``shot_id`` on the LAST ``"#"``, parses the suffix as a ``float``, and
matches it against ``FootageIndex.shots`` by ``(source_file, start_time)``
within a small epsilon (``1e-6``). Splitting on the last ``#`` keeps
source paths that themselves contain ``#`` characters intact.

If a ``shot_id`` cannot be resolved, the trim window falls outside the
underlying shot, the position sequence is not contiguous ``0..N-1``, or
any source file is missing on disk, :func:`run_editor` raises BEFORE
the agent ever runs. The Editor itself trusts the validated plan and
the FFmpeg tools to surface any remaining failures.

Output layout
-------------

Per-brief working files are nested under a slugified subdirectory so
multiple briefs can render in parallel without colliding::

    output/working/{brief_slug}/clip_00.mp4
    output/working/{brief_slug}/clip_00_captions.ass  (optional)
    output/working/{brief_slug}/clip_00_captioned.mp4 (optional)
    output/working/{brief_slug}/clip_01.mp4
    ...
    output/working/{brief_slug}/sequenced.mp4
    output/working/{brief_slug}/with_music.mp4        (optional)
    output/final/{brief_slug}.mp4

The ``brief_slug`` is derived by slugifying ``brief.product`` (lowercase,
non-alphanumeric → dash, repeats collapsed, leading/trailing dashes
stripped, falling back to ``"untitled"`` if the input slugifies empty).

Failure semantics
-----------------

Every tool in :mod:`src.tools.edit` and :mod:`src.tools.render` raises
on failure (``FileNotFoundError``, ``ValueError``, or ``RuntimeError``).
The Editor agent does NOT swallow these — the exception is surfaced to
the LLM via the tool result, and the agent's instruction tells it to
report the error verbatim and STOP. After the agent returns,
:func:`run_editor` additionally asserts the final MP4 exists on disk
and raises ``RuntimeError`` if it does not, so there is no silent
success path.

Importing this module instantiates the module-level ``editor`` Agent
(no Gemini API calls). Tool invocations and the Gemini round-trip only
happen inside :func:`run_editor`.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from google.adk.agents import Agent
from google.adk.runners import InMemoryRunner
from google.genai import types as genai_types

from src.models.schemas import (
    CreativeBrief,
    EditPlan,
    EditPlanEntry,
    FootageIndex,
    Shot,
)
from src.tools.captions import (
    burn_ass_subtitles,
    generate_ass_captions,
    has_words_in_window,
)
from src.tools.edit import add_music, composite_broll, cut_clip, sequence_clips
from src.tools.render import render_final

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_MODEL_ID = "gemini-3.1-pro-preview"
_AGENT_NAME = "editor"
_APP_NAME = "agentic-video-editor"
_USER_ID = "editor-user"

#: Float tolerance for matching ``EditPlanEntry.shot_id`` start_time
#: suffixes against ``Shot.start_time`` in the FootageIndex. Footage
#: indexes round timestamps slightly between preprocess runs and JSON
#: serialization can also nudge floats by an LSB or two, so an exact
#: equality check is too strict.
_SHOT_MATCH_EPSILON = 1e-6

_AGENT_DESCRIPTION = (
    "Pure executor that renders an EditPlan into a final MP4 by calling "
    "the cut/caption/sequence/music/render tools."
)

EDITOR_INSTRUCTION = """\
You are the Editor agent for an AI video editor. Your job is to take a
structured EditPlan produced by the Director agent and render it into a
final MP4 file by calling the FFmpeg tools wired to you. You are a pure
executor — you do not make creative decisions, you do not deviate from
the plan, you do not invent parameters, and you do not call ffmpeg
directly. You only use the provided tools.

## Your Mission
Given a fully resolved EditPlan and a set of pre-computed input/output
paths in your runtime context, produce a single playable MP4 at the
final output path. You handle ALL entries (A-Roll and B-Roll): cut each
one, generate captions on A-Roll clips that have speech, burn captions,
and normalize every clip. Then sequence ONLY the A-Roll clips into a
base video with continuous narration audio. B-Roll compositing onto the
base is handled automatically after you finish — you do NOT sequence
B-Roll clips.

## Available Tools
- cut_clip(source, start, end, output) — extract a sub-clip from a
  source video via stream copy. Returns the output path.
- generate_ass_captions(footage_index_path, shot_id, clip_start,
  clip_end, output) — build an ASS subtitle file from the resolved
  shot's per-word timestamps over the trimmed clip window. Returns the
  ASS file path.
- burn_ass_subtitles(video, subtitles, output) — burn an ASS subtitle
  file onto a video clip. Returns the output path.
- sequence_clips(clips, output) — concatenate an ordered list of clips
  into a single video via the concat demuxer. All inputs must share
  codec/resolution/timebase. Returns the output path.
- add_music(video, music, volume, output) — mix a music track under the
  video's original audio at the given linear volume scale. Returns the
  output path.
- render_final(video, output, resolution) — re-encode to H.264 MP4 at
  the given resolution (default "1080x1920"). Returns the output path.

## Workflow (follow exactly, in order)
1. For EVERY row in the "EditPlan Entries" table below (both A-Roll and
   B-Roll), in ascending `position` order:
   a. Call cut_clip(source=<source_file>, start=<start_trim>,
      end=<end_trim>, output=<clip_working_path>). Use the values from
      the table verbatim. Do NOT recompute anything.
   b. If the row's `captions_ass_path` is not `(n/a)`, call
      generate_ass_captions with:
        - footage_index_path = the global <footage_index_path> from the
          Runtime Context
        - shot_id = the row's shot_id
        - clip_start = the row's start_trim
        - clip_end = the row's end_trim
        - output = the row's <captions_ass_path>
   c. If the row's `captioned_working_path` is not `(n/a)`, call
      burn_ass_subtitles with:
        - video = the cut_clip output (the same <clip_working_path>)
        - subtitles = the row's <captions_ass_path>
        - output = the row's <captioned_working_path>
      Use the captioned file as this position's "pre-normalize" clip.
      Otherwise, use the cut_clip output as this position's
      "pre-normalize" clip.
   d. Call render_final to NORMALIZE this position's clip into a uniform
      H.264 / 1080x1920 / AAC container:
        - video = the "pre-normalize" clip chosen in step 1c
        - output = the row's <normalize_working_path>
        - resolution = "1080x1920"
2. Once every entry has been cut, captioned (where applicable), and
   normalized, call sequence_clips with `clips` set to the ordered
   list of A-ROLL ONLY <normalize_working_path> values (rows marked
   `roll_type: a-roll` in the table) and `output` set to the
   <sequenced_path> from the runtime context. Do NOT include B-Roll
   clips in the sequence — they will be composited later automatically.
3. If `music_path` in the runtime context is non-null, call add_music
   with:
     - video = the <sequenced_path>
     - music = <music_path>
     - volume = 0.3
     - output = <with_music_path>
   Use <with_music_path> as the input to the final render. If
   `music_path` is null, skip this step and feed <sequenced_path>
   directly into render_final.
4. Call render_final with:
     - video = the latest intermediate (with_music_path or
       sequenced_path)
     - output = <final_output_path>
     - resolution = "1080x1920"
5. Return ONLY the <final_output_path> as plain text. No JSON wrapper,
   no commentary, no markdown fences, no explanation. Just the path.

## Hard Constraints
- NEVER invent paths or parameters. Every path you pass to a tool MUST
  come from this instruction. Every numeric parameter MUST come from the
  EditPlan Entries table or the rules above.
- NEVER call ffmpeg directly or shell out. ONLY use the provided tools.
- NEVER skip a step. Every entry must be cut and normalized. Every
  caption step whose ASS path is present must be executed. Sequencing
  (A-Roll only), music (if specified), and final render are all mandatory.
- When sequencing, include ONLY A-Roll normalized clips. B-Roll clips
  are composited onto the base video automatically after you finish.
- NEVER produce a partial or "best effort" file. If ANY tool call raises
  an error (FileNotFoundError, ValueError, RuntimeError), STOP
  immediately. Your final response must be the error message verbatim
  prefixed with "ERROR: " naming the tool and the position that failed.
  Do not attempt to recover.
- The final response is either the <final_output_path> on success or an
  ERROR: ... message on failure. Nothing else.
"""


# --------------------------------------------------------------------------- #
# Slugify helper
# --------------------------------------------------------------------------- #


def _slugify_brief(brief: CreativeBrief) -> str:
    """Render a :class:`CreativeBrief` as a filesystem-safe lowercase slug.

    Slugifies ``brief.product``: lowercases, replaces whitespace with
    dashes, strips any character that is not ``[a-z0-9-]``, collapses
    repeated dashes, and strips leading/trailing dashes. Falls back to
    ``"untitled"`` if the result is empty.

    Args:
        brief: The :class:`CreativeBrief` whose ``product`` field is
            slugified.

    Returns:
        A non-empty lowercase slug suitable for use as a filename stem
        and directory name.
    """
    lowered = brief.product.strip().lower()
    dashed = re.sub(r"\s+", "-", lowered)
    cleaned = re.sub(r"[^a-z0-9-]", "", dashed)
    collapsed = re.sub(r"-+", "-", cleaned).strip("-")
    return collapsed or "untitled"


# --------------------------------------------------------------------------- #
# Shot resolution & plan validation
# --------------------------------------------------------------------------- #


def _resolve_shot(
    shot_id: str,
    index: FootageIndex,
    epsilon: float = _SHOT_MATCH_EPSILON,
) -> Shot:
    """Resolve an :class:`EditPlanEntry` ``shot_id`` to a real :class:`Shot`.

    Splits ``shot_id`` on the LAST ``"#"`` character (source paths may
    contain ``#``), parses the suffix as a ``float`` start_time, and
    scans ``index.shots`` for a shot whose ``source_file`` matches the
    prefix exactly and whose ``start_time`` differs from the parsed
    suffix by less than ``epsilon``.

    Args:
        shot_id: The ``"<source_file>#<start_time>"`` identifier from
            an :class:`EditPlanEntry`.
        index: The :class:`FootageIndex` to search.
        epsilon: Float tolerance for matching ``start_time`` (default
            ``1e-6``). Footage indexes round timestamps slightly between
            preprocess runs and JSON round-trips can nudge floats by an
            LSB, so an exact equality check is too strict.

    Returns:
        The matching :class:`Shot`.

    Raises:
        ValueError: If ``shot_id`` does not contain a ``"#"`` separator,
            the suffix does not parse as a float, or no shot in the
            index matches the ``(source_file, start_time)`` pair within
            the epsilon tolerance. The error message names the
            ``shot_id`` so callers can report the failing entry.
    """
    sep = shot_id.rfind("#")
    if sep == -1:
        raise ValueError(
            f"shot_id {shot_id!r} is missing a '#' separator; expected "
            "format '<source_file>#<start_time>'"
        )
    source_file = shot_id[:sep]
    suffix = shot_id[sep + 1 :]
    try:
        start_time = float(suffix)
    except ValueError as exc:
        raise ValueError(
            f"shot_id {shot_id!r} suffix {suffix!r} is not a valid float "
            "start_time"
        ) from exc

    for shot in index.shots:
        if (
            shot.source_file == source_file
            and abs(shot.start_time - start_time) < epsilon
        ):
            return shot

    raise ValueError(
        f"shot_id {shot_id!r} did not match any shot in the FootageIndex "
        f"(looked for source_file={source_file!r}, "
        f"start_time≈{start_time} ± {epsilon})"
    )


def _validate_edit_plan(
    plan: EditPlan,
    index: FootageIndex,
) -> list[tuple[EditPlanEntry, Shot]]:
    """Validate ``plan`` against ``index`` and return resolved entries.

    Performs every structural check the Editor needs BEFORE the agent
    runs so failures surface as Python exceptions rather than confusing
    FFmpeg errors mid-render:

    1. Every ``EditPlanEntry.shot_id`` resolves to a real
       :class:`Shot` in ``index`` via :func:`_resolve_shot`.
    2. ``end_trim`` is strictly greater than ``start_trim``.
    3. ``start_trim >= shot.start_time - epsilon`` and
       ``end_trim <= shot.end_time + epsilon`` so the trim window stays
       inside the underlying shot.
    4. ``[entry.position for entry in plan.entries]`` (sorted) equals
       ``[0, 1, ..., N-1]`` — no gaps, no duplicates, contiguous from
       zero.

    Args:
        plan: The :class:`EditPlan` to validate.
        index: The :class:`FootageIndex` the plan was built against.

    Returns:
        A list of ``(entry, shot)`` pairs sorted by ``entry.position``,
        ready for the runner to iterate over in play order.

    Raises:
        ValueError: With a specific message naming the failing entry's
            ``shot_id`` and ``position`` on any validation failure. The
            Editor never attempts to "fix" a broken plan.
    """
    if not plan.entries:
        raise ValueError(
            "EditPlan has zero entries; nothing to render. The Director "
            "must produce at least one EditPlanEntry."
        )

    eps = _SHOT_MATCH_EPSILON
    resolved: list[tuple[EditPlanEntry, Shot]] = []
    for entry in plan.entries:
        try:
            shot = _resolve_shot(entry.shot_id, index)
        except ValueError as exc:
            raise ValueError(
                f"EditPlanEntry position={entry.position} shot_id="
                f"{entry.shot_id!r} could not be resolved: {exc}"
            ) from exc

        if entry.end_trim <= entry.start_trim:
            raise ValueError(
                f"EditPlanEntry position={entry.position} shot_id="
                f"{entry.shot_id!r} has end_trim={entry.end_trim} <= "
                f"start_trim={entry.start_trim}; end_trim must be strictly "
                "greater than start_trim"
            )
        if entry.start_trim < shot.start_time - eps:
            raise ValueError(
                f"EditPlanEntry position={entry.position} shot_id="
                f"{entry.shot_id!r} has start_trim={entry.start_trim} "
                f"before the shot's start_time={shot.start_time}"
            )
        if entry.end_trim > shot.end_time + eps:
            raise ValueError(
                f"EditPlanEntry position={entry.position} shot_id="
                f"{entry.shot_id!r} has end_trim={entry.end_trim} after "
                f"the shot's end_time={shot.end_time}"
            )
        resolved.append((entry, shot))

    resolved.sort(key=lambda pair: pair[0].position)
    expected = list(range(len(resolved)))
    actual = [pair[0].position for pair in resolved]
    if actual != expected:
        raise ValueError(
            f"EditPlan positions are not contiguous 0..N-1: got {actual}, "
            f"expected {expected}. Every position from 0 to "
            f"{len(resolved) - 1} must appear exactly once."
        )

    return resolved


# --------------------------------------------------------------------------- #
# Path computation
# --------------------------------------------------------------------------- #


def _compute_output_paths(
    plan: EditPlan,
    resolved: list[tuple[EditPlanEntry, Shot]],
    output_dir: str,
) -> tuple[
    Path,
    Path,
    list[Path],
    list[Path | None],
    list[Path | None],
    list[Path],
    Path,
    Path,
    Path,
]:
    """Compute every output path the Editor will use for ``plan``.

    Builds the per-brief working subdirectory, the final output path,
    and the per-position clip + (optional) caption + normalize paths so
    the runner can pre-create parent directories AND embed exact paths
    in the agent's runtime instruction.

    Args:
        plan: The :class:`EditPlan` whose ``brief.product`` determines
            the slug.
        resolved: The validated ``(entry, shot)`` pairs sorted by
            ``position`` (from :func:`_validate_edit_plan`).
        output_dir: Root output directory (typically ``"output"``).

    Returns:
        An 8-tuple of:

        * ``working_dir`` — ``output/working/{slug}/`` :class:`Path`.
        * ``final_dir`` — ``output/final/`` :class:`Path`.
        * ``clip_paths`` — per-position raw cut paths.
        * ``caption_ass_paths`` — per-position ASS subtitle paths or
          ``None`` when the trimmed clip has no spoken words.
        * ``captioned_paths`` — per-position burned-caption video
          outputs or ``None`` when the trimmed clip has no spoken words.
        * ``normalize_paths`` — per-position normalized paths. Every
          entry gets a normalize path so the downstream
          :func:`~src.tools.edit.sequence_clips` stream-copy concat
          demuxer receives uniform inputs regardless of source codec,
          source resolution, or whether captions were burned in.
        * ``sequenced_path`` — concat output path.
        * ``with_music_path`` — music-mix output path (always
          materialized; only USED when ``plan.music_path`` is set).
        * ``final_output`` — final rendered MP4 path.
    """
    root = Path(output_dir)
    slug = _slugify_brief(plan.brief)
    working_dir = root / "working" / slug
    final_dir = root / "final"

    clip_paths: list[Path] = []
    caption_ass_paths: list[Path | None] = []
    captioned_paths: list[Path | None] = []
    normalize_paths: list[Path] = []
    for entry, shot in resolved:
        clip_paths.append(working_dir / f"clip_{entry.position:02d}.mp4")
        if has_words_in_window(shot, entry.start_trim, entry.end_trim):
            caption_ass_paths.append(
                working_dir / f"clip_{entry.position:02d}_captions.ass"
            )
            captioned_paths.append(
                working_dir / f"clip_{entry.position:02d}_captioned.mp4"
            )
        else:
            caption_ass_paths.append(None)
            captioned_paths.append(None)
        normalize_paths.append(
            working_dir / f"clip_{entry.position:02d}_normalized.mp4"
        )

    sequenced_path = working_dir / "sequenced.mp4"
    with_music_path = working_dir / "with_music.mp4"
    final_output = final_dir / f"{slug}.mp4"

    return (
        working_dir,
        final_dir,
        clip_paths,
        caption_ass_paths,
        captioned_paths,
        normalize_paths,
        sequenced_path,
        with_music_path,
        final_output,
    )


# --------------------------------------------------------------------------- #
# Instruction builder
# --------------------------------------------------------------------------- #


def build_runtime_instruction(
    edit_plan: EditPlan,
    footage_index_path: str,
    output_dir: str = "output",
) -> str:
    """Return the full runtime instruction for a plan-aware Editor.

    Resolves ``edit_plan`` against the :class:`FootageIndex` loaded from
    ``footage_index_path``, computes every intermediate and final path,
    and bakes a fully concrete plan into the static
    :data:`EDITOR_INSTRUCTION`. The agent then has zero ambiguity about
    what to do — every parameter is spelled out in the table.

    This helper is exposed so callers (tests, debug tools) can preview
    exactly what the Editor will see without spinning up an Agent.

    Args:
        edit_plan: The :class:`EditPlan` whose entries drive this edit.
        footage_index_path: Path to the JSON-serialized
            :class:`FootageIndex` produced by :mod:`src.pipeline.preprocess`.
            Must exist on disk so shot resolution can validate every
            ``shot_id``.
        output_dir: Root output directory (default ``"output"``).

    Returns:
        A multi-line instruction string ready to pass as
        ``Agent.instruction``.

    Raises:
        FileNotFoundError: If ``footage_index_path`` does not exist.
        ValueError: If the plan does not validate against the loaded
            footage index (see :func:`_validate_edit_plan`).
    """
    index_path = Path(footage_index_path)
    if not index_path.exists():
        raise FileNotFoundError(
            f"footage_index_path does not exist: {footage_index_path}"
        )
    index = FootageIndex.model_validate_json(index_path.read_text())
    resolved = _validate_edit_plan(edit_plan, index)
    (
        working_dir,
        _final_dir,
        clip_paths,
        caption_ass_paths,
        captioned_paths,
        normalize_paths,
        sequenced_path,
        with_music_path,
        final_output,
    ) = _compute_output_paths(edit_plan, resolved, output_dir)

    if edit_plan.music_path:
        music_line = f"- music_path: {edit_plan.music_path}"
    else:
        music_line = (
            "- music_path: null (no music track — SKIP the add_music step "
            "entirely; do NOT call add_music at all)"
        )

    runtime_lines: list[str] = [
        "## Runtime Context",
        f"- footage_index_path: {footage_index_path}",
        f"- working_dir: {working_dir}",
        f"- sequenced_path: {sequenced_path}",
        f"- with_music_path: {with_music_path}",
        f"- final_output_path: {final_output}",
        music_line,
        f"- entry_count: {len(resolved)}",
        "",
        "## EditPlan Entries (in play order — execute in this exact order)",
    ]

    for (
        (entry, shot),
        clip_path,
        caption_ass_path,
        captioned_path,
        normalize_path,
    ) in zip(
        resolved,
        clip_paths,
        caption_ass_paths,
        captioned_paths,
        normalize_paths,
        strict=True,
    ):
        transition = entry.transition or "cut"
        caption_ass_line = (
            f"    captions_ass_path: {caption_ass_path}"
            if caption_ass_path is not None
            else "    captions_ass_path: (n/a)"
        )
        captioned_line = (
            f"    captioned_working_path: {captioned_path}"
            if captioned_path is not None
            else "    captioned_working_path: (n/a)"
        )
        roll_type = getattr(shot, "roll_type", "unknown")
        runtime_lines.extend(
            [
                f"  - position={entry.position}",
                f"    shot_id: {entry.shot_id}",
                f"    roll_type: {roll_type}",
                f"    source_file: {shot.source_file}",
                f"    start_trim: {entry.start_trim}",
                f"    end_trim: {entry.end_trim}",
                f"    transition: {transition}",
                f"    clip_working_path: {clip_path}",
                caption_ass_line,
                captioned_line,
                f"    normalize_working_path: {normalize_path}",
            ]
        )

    runtime_block = "\n".join(runtime_lines)
    return EDITOR_INSTRUCTION.rstrip() + "\n\n" + runtime_block + "\n"


# --------------------------------------------------------------------------- #
# Agent factory
# --------------------------------------------------------------------------- #


def build_editor(
    edit_plan: EditPlan,
    footage_index_path: str,
    output_dir: str = "output",
) -> Agent:
    """Construct a plan-aware Editor ADK Agent without running it.

    Each call returns a fresh :class:`google.adk.agents.Agent` whose
    instruction has the fully resolved plan baked in via
    :func:`build_runtime_instruction`. The agent is wired with the
    execution tools — ``cut_clip``, ``generate_ass_captions``,
    ``burn_ass_subtitles``, ``sequence_clips``, ``add_music``,
    ``render_final`` — and **no** ``output_schema`` (the
    Editor returns a plain text path, not a structured response).

    Use this factory whenever you have a concrete plan in hand. The
    module-level :data:`editor` instance is reserved for ADK
    auto-discovery and tests that do not need plan-specific context.

    Args:
        edit_plan: The :class:`EditPlan` to render.
        footage_index_path: Path to the JSON-serialized
            :class:`FootageIndex` the plan was built against. Must exist
            on disk.
        output_dir: Root output directory (default ``"output"``).

    Returns:
        A configured :class:`Agent` named ``editor`` with model
        ``gemini-3.1-pro`` and the five FFmpeg tools attached.

    Raises:
        FileNotFoundError: If ``footage_index_path`` does not exist.
        ValueError: If the plan does not validate against the loaded
            footage index.
    """
    instruction = build_runtime_instruction(
        edit_plan,
        footage_index_path=footage_index_path,
        output_dir=output_dir,
    )
    return Agent(
        name=_AGENT_NAME,
        model=_MODEL_ID,
        description=_AGENT_DESCRIPTION,
        instruction=instruction,
        tools=[
            cut_clip,
            generate_ass_captions,
            burn_ass_subtitles,
            sequence_clips,
            add_music,
            render_final,
        ],
    )


# --------------------------------------------------------------------------- #
# Module-level default instance
# --------------------------------------------------------------------------- #

#: Default Editor agent without any plan baked in. Useful for ADK
#: discovery (``adk web``, ``adk eval``), tests, and module-level
#: introspection. For real runs prefer :func:`build_editor` so the
#: resolved plan, computed output paths, and shot source files land
#: inside the instruction.
editor: Agent = Agent(
    name=_AGENT_NAME,
    model=_MODEL_ID,
    description=_AGENT_DESCRIPTION,
    instruction=EDITOR_INSTRUCTION,
    tools=[
        cut_clip,
        generate_ass_captions,
        burn_ass_subtitles,
        sequence_clips,
        add_music,
        render_final,
    ],
)


# --------------------------------------------------------------------------- #
# Synchronous end-to-end runner
# --------------------------------------------------------------------------- #


def run_editor(
    edit_plan: EditPlan,
    footage_index_path: str,
    output_dir: str = "output",
) -> str:
    """Run the Editor agent end-to-end and return the final MP4 path.

    Validates the plan against the footage index loaded from disk,
    pre-flights every source file (and the music path, if any), creates
    the working/final output directories, builds a fresh plan-aware
    :class:`Agent` via :func:`build_editor`, drives one in-memory turn
    via :class:`InMemoryRunner`, and verifies the final MP4 exists on
    disk before returning its path.

    The Editor is wired without an ``output_schema`` because its final
    response is a plain text path string. Validation, pre-flight, path
    computation, and post-run verification all live in this runner so
    the agent itself only has to mechanically call the tools listed in
    its instruction.

    Args:
        edit_plan: The :class:`EditPlan` to render.
        footage_index_path: Path to the JSON-serialized
            :class:`FootageIndex` the plan was built against. Used both
            to validate every ``shot_id`` (via :func:`_resolve_shot`)
            and to verify each underlying ``source_file`` exists on
            disk before the agent runs.
        output_dir: Root output directory (default ``"output"``).
            ``{output_dir}/working/{brief_slug}/`` and
            ``{output_dir}/final/`` will be created if they do not
            exist.

    Returns:
        The final rendered MP4 path as a string. Always equals the
        ``final_output_path`` baked into the agent's instruction; this
        runner asserts the file exists on disk before returning so a
        successful return guarantees a playable output.

    Raises:
        FileNotFoundError: If ``footage_index_path``, any resolved
            ``Shot.source_file``, or ``edit_plan.music_path`` does not
            exist on disk.
        ValueError: If the plan does not validate against the loaded
            footage index (see :func:`_validate_edit_plan`).
        RuntimeError: If the agent stream completes without producing a
            final response payload, OR completes but does not actually
            write the final output file to disk.
    """
    index_path = Path(footage_index_path)
    if not index_path.exists():
        raise FileNotFoundError(
            f"footage_index_path does not exist: {footage_index_path}"
        )
    index = FootageIndex.model_validate_json(index_path.read_text())

    resolved = _validate_edit_plan(edit_plan, index)

    # Pre-flight: every resolved source file must exist on disk.
    missing_sources = sorted(
        {
            shot.source_file
            for _entry, shot in resolved
            if not Path(shot.source_file).exists()
        }
    )
    if missing_sources:
        raise FileNotFoundError(
            "EditPlan references source files that do not exist on disk: "
            + ", ".join(missing_sources)
        )

    # Pre-flight: music path (if any) must exist.
    if edit_plan.music_path and not Path(edit_plan.music_path).exists():
        raise FileNotFoundError(
            f"EditPlan.music_path does not exist on disk: "
            f"{edit_plan.music_path}"
        )

    (
        working_dir,
        final_dir,
        _clip_paths,
        _caption_ass_paths,
        _captioned_paths,
        _normalize_paths,
        _sequenced_path,
        _with_music_path,
        final_output,
    ) = _compute_output_paths(edit_plan, resolved, output_dir)

    working_dir.mkdir(parents=True, exist_ok=True)
    final_dir.mkdir(parents=True, exist_ok=True)

    # Remove any stale output from a prior run so a failed rerender cannot
    # be mistaken for a successful one by the post-run existence check.
    if final_output.exists():
        final_output.unlink()

    agent = build_editor(
        edit_plan,
        footage_index_path=footage_index_path,
        output_dir=output_dir,
    )
    runner = InMemoryRunner(agent=agent, app_name=_APP_NAME)

    user_message = genai_types.Content(
        role="user",
        parts=[
            genai_types.Part(
                text=(
                    "Execute the EditPlan you were given. Call each tool "
                    "in the order described in your instructions, using "
                    "the exact paths and parameters from the EditPlan "
                    "Entries table and Runtime Context. Stop after "
                    "render_final returns. Report each tool call as you "
                    "go. On success, return ONLY the final_output_path. "
                    "On any tool failure, return an 'ERROR: ...' message "
                    "verbatim."
                )
            )
        ],
    )

    async def _go() -> str:
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
                # Concatenate any non-thought text parts so we capture
                # the model's full response even if it is split across
                # multiple parts.
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
                "Editor agent returned no final response text. Check "
                "that the EditPlan has at least one entry and that the "
                "FFmpeg tools are reachable on this machine."
            )
        return final_text.strip()

    agent_response = asyncio.run(_go())

    # The agent is instructed to prefix any tool-failure message with
    # "ERROR: ". Reject such responses instead of relying on the existence
    # check, which could misread a stale file as a fresh success.
    if agent_response.startswith("ERROR:"):
        raise RuntimeError(
            f"Editor agent reported tool failure: {agent_response}"
        )

    if not final_output.exists():
        raise RuntimeError(
            "Editor agent completed but final output does not exist: "
            f"{final_output}. Agent response was: {agent_response!r}"
        )

    # --------------------------------------------------------------------- #
    # B-Roll compositing (post-agent, deterministic)
    #
    # The agent sequenced only A-Roll clips into the base video. Now we
    # overlay B-Roll clips at the correct timestamps so the A-Roll audio
    # (narration) continues underneath the visual cutaways.
    # --------------------------------------------------------------------- #
    broll_entries = [
        (entry, shot, norm_path)
        for (entry, shot), norm_path in zip(resolved, _normalize_paths)
        if getattr(shot, "roll_type", "unknown") == "b-roll"
    ]

    if broll_entries:
        # Calculate timeline offset for each B-Roll clip.  The agent
        # sequenced only A-Roll clips in position order. Walk the resolved
        # entries in position order and accumulate A-Roll durations to find
        # the insertion point for each B-Roll clip.
        cumulative = 0.0
        position_offsets: dict[int, float] = {}
        for entry, shot in resolved:
            position_offsets[entry.position] = cumulative
            if getattr(shot, "roll_type", "unknown") != "b-roll":
                cumulative += entry.end_trim - entry.start_trim

        overlays: list[dict] = []
        for entry, shot, norm_path in broll_entries:
            timeline_start = position_offsets[entry.position]
            duration = entry.end_trim - entry.start_trim
            overlays.append(
                {
                    "path": str(norm_path),
                    "start": timeline_start,
                    "duration": duration,
                }
            )
            logger.info(
                "B-Roll overlay: position=%d at %.2fs for %.2fs — %s",
                entry.position,
                timeline_start,
                duration,
                Path(shot.source_file).name,
            )

        # Composite B-Roll onto the agent's final output.  Write to a temp
        # path first, then replace the final output so downstream checks
        # still find the expected file.
        composited_path = working_dir / "composited_broll.mp4"
        composite_broll(
            base_video=str(final_output),
            overlays=overlays,
            output=str(composited_path),
        )

        # Replace the agent's output with the composited version.
        final_output.unlink()
        composited_path.rename(final_output)
        logger.info(
            "B-Roll compositing done — %d overlay(s) applied",
            len(overlays),
        )

    return str(final_output)
