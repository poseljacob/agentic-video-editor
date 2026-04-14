"""Trim Refiner: two-pass cut-point precision via short-clip Gemini analysis.

Takes the Director's rough EditPlan, extracts short (~6 s) probe clips
around each entry's IN and OUT trim points, sends them to Gemini in
parallel for frame-level timing, and returns a refined EditPlan with
tighter cuts.

The Director owns creative decisions (which clips, what order, narrative
arc). The Trim Refiner only tightens timing — it never changes shot
selection, ordering, or any other creative field.

Public API
----------

* :func:`refine_plan` — takes an ``EditPlan`` + ``footage_index_path``,
  returns a new ``EditPlan`` with refined ``start_trim`` / ``end_trim``
  on every entry.
"""

from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from pydantic import BaseModel, Field

from src.models.schemas import EditPlan, EditPlanEntry, FootageIndex, Shot

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_GEMINI_MODEL = "gemini-3.1-pro-preview"

#: Seconds of context on each side of a trim point.
_PROBE_MARGIN_SECONDS = 3.0

#: Max workers for parallel Gemini calls (one per trim point).
_MAX_WORKERS = 6

#: Files larger than this are uploaded via the File API.
_INLINE_MAX_BYTES = 20 * 1024 * 1024

_FILE_ACTIVE_TIMEOUT_SECONDS = 60.0
_FILE_ACTIVE_POLL_INTERVAL = 1.0

_MIME_TYPE_BY_EXT: dict[str, str] = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".m4v": "video/x-m4v",
    ".mkv": "video/x-matroska",
}
_DEFAULT_MIME_TYPE = "video/mp4"


# --------------------------------------------------------------------------- #
# Structured output schema
# --------------------------------------------------------------------------- #


class TrimRefinement(BaseModel):
    """Gemini's answer for a single trim-point probe."""

    refined_timestamp: float = Field(
        ...,
        description=(
            "The precise timestamp (in seconds, relative to the start of "
            "the probe clip) where the action/speech meaningfully begins "
            "(for IN points) or naturally completes (for OUT points)."
        ),
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="How confident the model is in the refined timestamp.",
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _log(msg: str) -> None:
    print(msg, flush=True)


def _mime_type_for(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return _MIME_TYPE_BY_EXT.get(suffix, _DEFAULT_MIME_TYPE)


def _require_api_key() -> str:
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY environment variable is not set.")
    return api_key


def _wait_for_file_active(client: object, file_obj: object) -> object:
    deadline = time.monotonic() + _FILE_ACTIVE_TIMEOUT_SECONDS
    current = file_obj
    while True:
        state = getattr(getattr(current, "state", None), "name", None) or str(
            getattr(current, "state", "")
        )
        if state == "ACTIVE":
            return current
        if state == "FAILED":
            raise RuntimeError(
                f"Gemini file upload failed for {getattr(current, 'name', '<unknown>')}"
            )
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Timed out waiting for uploaded video to become ACTIVE (last state={state!r})"
            )
        time.sleep(_FILE_ACTIVE_POLL_INTERVAL)
        current = client.files.get(name=current.name)  # type: ignore[attr-defined]


def _extract_probe_clip(
    source: str, start: float, end: float, output: str
) -> str:
    """Extract a short probe clip via FFmpeg stream copy."""
    import subprocess

    Path(output).parent.mkdir(parents=True, exist_ok=True)
    duration = end - start
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start}",
        "-i", source,
        "-t", f"{duration}",
        "-c", "copy",
        output,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"FFmpeg probe extraction failed: {e.stderr}") from e
    return output


def _send_probe_to_gemini(
    clip_path: str,
    point_type: str,
    original_trim: float,
    probe_start_in_source: float,
) -> float:
    """Send a probe clip to Gemini and return the refined source-timeline timestamp.

    Args:
        clip_path: Path to the short probe clip.
        point_type: ``"IN"`` or ``"OUT"``.
        original_trim: The Director's original trim timestamp (source timeline).
        probe_start_in_source: Where the probe clip starts in the source timeline.

    Returns:
        Refined timestamp in the source file's timeline.
    """
    from google import genai
    from google.genai import types

    api_key = _require_api_key()
    client = genai.Client(api_key=api_key)

    path = Path(clip_path)
    mime_type = _mime_type_for(clip_path)
    size = path.stat().st_size

    if size > _INLINE_MAX_BYTES:
        uploaded = client.files.upload(
            file=str(path),
            config=types.UploadFileConfig(mime_type=mime_type),
        )
        uploaded = _wait_for_file_active(client, uploaded)
        video_part = types.Part.from_uri(
            file_uri=uploaded.uri, mime_type=mime_type,
        )
    else:
        video_part = types.Part.from_bytes(
            data=path.read_bytes(), mime_type=mime_type,
        )

    if point_type == "IN":
        task = (
            "Find the exact timestamp where meaningful action or speech "
            "BEGINS in this clip. Ignore any dead air, silence, or "
            "stillness at the start. The timestamp should be the first "
            "frame where the speaker starts talking, moves purposefully, "
            "or visual action starts. Report the timestamp in seconds "
            "relative to the START of this clip (0.0 = first frame)."
        )
    else:
        task = (
            "Find the exact timestamp where the speaker's thought or "
            "visual action naturally COMPLETES in this clip. The cut "
            "should feel clean — not mid-word or mid-gesture. Report "
            "the timestamp in seconds relative to the START of this "
            "clip (0.0 = first frame)."
        )

    prompt = (
        "You are a professional video editor fine-tuning cut points.\n\n"
        f"This is a short probe clip (~6 seconds) around a proposed "
        f"{point_type} point.\n\n"
        f"{task}\n\n"
        "Return ONLY the JSON object matching the schema — no commentary."
    )

    response = client.models.generate_content(
        model=_GEMINI_MODEL,
        contents=[video_part, prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=TrimRefinement,
        ),
    )

    parsed = getattr(response, "parsed", None)
    if not isinstance(parsed, TrimRefinement):
        raw_text = getattr(response, "text", None)
        if not raw_text:
            _log(f"[trim_refiner] no response for {point_type} probe, keeping original")
            return original_trim
        parsed = TrimRefinement.model_validate_json(raw_text)

    # Map probe-relative timestamp back to source timeline.
    refined_source_ts = probe_start_in_source + parsed.refined_timestamp

    _log(
        f"[trim_refiner] {point_type} point: "
        f"original={original_trim:.2f}s -> refined={refined_source_ts:.2f}s "
        f"(confidence={parsed.confidence:.2f})"
    )
    return refined_source_ts


def _slugify(text: str) -> str:
    lowered = text.strip().lower()
    dashed = re.sub(r"\s+", "-", lowered)
    cleaned = re.sub(r"[^a-z0-9-]", "", dashed)
    return re.sub(r"-+", "-", cleaned).strip("-") or "untitled"


# --------------------------------------------------------------------------- #
# Shot resolution (mirrors editor.py logic)
# --------------------------------------------------------------------------- #

_SHOT_MATCH_EPSILON = 1e-6


def _resolve_shot(shot_id: str, index: FootageIndex) -> Shot:
    sep = shot_id.rfind("#")
    if sep == -1:
        raise ValueError(f"shot_id {shot_id!r} missing '#' separator")
    source_file = shot_id[:sep]
    start_time = float(shot_id[sep + 1:])
    for shot in index.shots:
        if (
            shot.source_file == source_file
            and abs(shot.start_time - start_time) < _SHOT_MATCH_EPSILON
        ):
            return shot
    raise ValueError(f"shot_id {shot_id!r} not found in FootageIndex")


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def refine_plan(
    plan: EditPlan,
    footage_index_path: str,
    output_dir: str = "output",
) -> EditPlan:
    """Refine trim points in *plan* using short-clip Gemini analysis.

    For each entry, extracts ~6 s probe clips around the IN and OUT
    trim points, sends them to Gemini in parallel, and returns a new
    EditPlan with tightened timestamps. Shot selection, ordering,
    overlays, and all other creative fields are preserved unchanged.

    Args:
        plan: The Director's rough EditPlan.
        footage_index_path: Path to the FootageIndex JSON.
        output_dir: Root output directory for temp probe clips.

    Returns:
        A new EditPlan with refined ``start_trim`` / ``end_trim``.
    """
    index_path = Path(footage_index_path)
    if not index_path.exists():
        raise FileNotFoundError(f"footage_index_path does not exist: {footage_index_path}")
    index = FootageIndex.model_validate_json(index_path.read_text(encoding="utf-8"))

    slug = _slugify(plan.brief.product)
    probe_dir = Path(output_dir) / "working" / slug / "trim_probes"
    probe_dir.mkdir(parents=True, exist_ok=True)

    _log(f"[trim_refiner] refining {len(plan.entries)} entries in parallel")
    t0 = time.monotonic()

    # Build work items: (entry_index, point_type, entry, shot)
    # Also store resolved shots for transcript lookup when building entries.
    resolved_shots: dict[int, Shot] = {}
    work_items: list[tuple[int, str, EditPlanEntry, Shot]] = []
    for i, entry in enumerate(plan.entries):
        shot = _resolve_shot(entry.shot_id, index)
        resolved_shots[i] = shot
        work_items.append((i, "IN", entry, shot))
        work_items.append((i, "OUT", entry, shot))

    # Results keyed by (entry_index, point_type) -> refined timestamp
    refined: dict[tuple[int, str], float] = {}

    def _refine_one(
        entry_idx: int,
        point_type: str,
        entry: EditPlanEntry,
        shot: Shot,
    ) -> tuple[int, str, float]:
        if point_type == "IN":
            original = entry.start_trim
        else:
            original = entry.end_trim

        # Compute probe window, clamped to shot boundaries.
        probe_start = max(shot.start_time, original - _PROBE_MARGIN_SECONDS)
        probe_end = min(shot.end_time, original + _PROBE_MARGIN_SECONDS)

        # Ensure minimum probe duration of 1s.
        if probe_end - probe_start < 1.0:
            _log(
                f"[trim_refiner] entry {entry_idx} {point_type}: "
                f"probe too short ({probe_end - probe_start:.2f}s), keeping original"
            )
            return (entry_idx, point_type, original)

        probe_path = str(
            probe_dir / f"probe_{entry_idx}_{point_type.lower()}.mp4"
        )

        try:
            _extract_probe_clip(shot.source_file, probe_start, probe_end, probe_path)
            result_ts = _send_probe_to_gemini(
                probe_path, point_type, original, probe_start
            )
        except Exception as exc:
            _log(
                f"[trim_refiner] entry {entry_idx} {point_type} failed: "
                f"{exc}; keeping original={original:.2f}s"
            )
            return (entry_idx, point_type, original)
        finally:
            # Clean up probe clip.
            try:
                Path(probe_path).unlink(missing_ok=True)
            except OSError:
                pass

        # Clamp to shot boundaries and sanity-check.
        result_ts = max(shot.start_time, min(shot.end_time, result_ts))
        return (entry_idx, point_type, result_ts)

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {
            pool.submit(_refine_one, idx, pt, entry, shot): (idx, pt)
            for idx, pt, entry, shot in work_items
        }
        for future in as_completed(futures):
            entry_idx, point_type, ts = future.result()
            refined[(entry_idx, point_type)] = ts

    # Build refined entries while preserving all non-timing fields.
    new_entries: list[EditPlanEntry] = []
    for i, entry in enumerate(plan.entries):
        new_start = refined.get((i, "IN"), entry.start_trim)
        new_end = refined.get((i, "OUT"), entry.end_trim)

        # Ensure start < end after refinement.
        if new_start >= new_end:
            _log(
                f"[trim_refiner] entry {i}: refined start ({new_start:.2f}) "
                f">= end ({new_end:.2f}), reverting to original trims"
            )
            new_start = entry.start_trim
            new_end = entry.end_trim

        shot = resolved_shots[i]
        new_entries.append(
            entry.model_copy(update={"start_trim": new_start, "end_trim": new_end})
        )
        _log(
            f"[trim_refiner] entry {i}: "
            f"trim ({entry.start_trim:.2f}, {entry.end_trim:.2f}) -> "
            f"({new_start:.2f}, {new_end:.2f})"
            f" shot={shot.source_file!r}"
        )

    # Recompute total_duration from refined trims.
    total_dur = sum(e.end_trim - e.start_trim for e in new_entries)

    elapsed = time.monotonic() - t0
    _log(f"[trim_refiner] done in {elapsed:.1f}s — total_duration {total_dur:.2f}s")

    # Clean up probe directory.
    try:
        probe_dir.rmdir()
    except OSError:
        pass

    return plan.model_copy(
        update={"entries": new_entries, "total_duration": total_dur}
    )
