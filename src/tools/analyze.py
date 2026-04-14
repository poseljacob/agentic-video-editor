"""Analysis tools for the Director agent.

Two pure tool functions:

* :func:`analyze_footage` — sends a single video clip to Gemini 3.1 Pro for
  scene-by-scene analysis using the model's native video input. Returns a
  list of plain dicts (one per detected scene) with timing, description,
  rated dimensions, and a key spoken quote.

* :func:`search_moments` — purely local, dependency-free ranking over a
  pre-built :class:`FootageIndex`. Scores each shot's description and
  transcript against a free-text query using a normalized token-overlap
  score, filters by ``min_relevance`` (0–1), and returns up to
  ``max_results`` shots sorted by descending relevance.

The functions are written so Google ADK's auto tool detection can pick
them up: full type annotations and Google-style docstrings on every public
parameter and return value.
"""

from __future__ import annotations

import contextvars
import os
import re
import time
from pathlib import Path

from pydantic import BaseModel, Field

from src.models.schemas import FootageIndex, ReviewScore, Shot

# --------------------------------------------------------------------------- #
# Prompt-injection defense: canonical video path binding
# --------------------------------------------------------------------------- #
#
# The Reviewer agent's prompt includes untrusted brief text and then lets the
# model choose the ``video_path`` argument to ``review_output``. Without a
# binding, a crafted brief could coerce the model into calling
# ``review_output("/etc/passwd", ...)`` and exfiltrate arbitrary readable
# local files to Gemini via the File API or inline bytes upload.
#
# ``run_reviewer`` resolves the pre-approved rendered cut path BEFORE invoking
# the agent and stores it here. ``review_output`` compares the model-supplied
# ``video_path`` against the bound canonical path (resolved for symlink /
# relative-path parity) and refuses any mismatch. The ContextVar scopes the
# binding to a single ``run_reviewer`` call via ``set`` / ``reset``, so
# bindings never leak across concurrent callers.
#
# When the ContextVar is unset (``None``) — e.g. direct tool invocation in a
# trusted test harness — the guard is skipped and the existing path-existence
# check still applies. Callers that want the guard must set it.
_ALLOWED_REVIEW_VIDEO_PATH: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_ALLOWED_REVIEW_VIDEO_PATH", default=None
)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_GEMINI_MODEL = "gemini-3.1-pro-preview"

# Files larger than this must be uploaded via the Gemini File API rather than
# inlined as bytes. Inline payloads are subject to per-request size limits and
# 20 MB is the conventional safe ceiling.
_INLINE_MAX_BYTES = 20 * 1024 * 1024

# How long to wait for an uploaded file to reach ACTIVE state before giving up.
_FILE_ACTIVE_TIMEOUT_SECONDS = 60.0
_FILE_ACTIVE_POLL_INTERVAL = 1.0

# Map common video extensions to their canonical mime types so we never lie
# about the payload format (e.g. .mov footage uses QuickTime).
_MIME_TYPE_BY_EXT: dict[str, str] = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".m4v": "video/x-m4v",
    ".mkv": "video/x-matroska",
}
_DEFAULT_MIME_TYPE = "video/mp4"

# Trivial English stopwords removed from query/document tokens before scoring.
# Kept intentionally tiny — we want lexical recall, not a real NLP pipeline.
_STOPWORDS: frozenset[str] = frozenset({"a", "an", "the", "of", "to", "and", "or"})


# --------------------------------------------------------------------------- #
# Internal response schema for Gemini structured output
# --------------------------------------------------------------------------- #


class _SceneAnalysis(BaseModel):
    """Schema Gemini must populate for each detected scene.

    Mirrors the public dict shape returned by :func:`analyze_footage`. Used
    as ``response_schema`` so we never parse free-text output.
    """

    start_time: float = Field(..., description="Scene start time in seconds.")
    end_time: float = Field(..., description="Scene end time in seconds.")
    description: str = Field(..., description="One-sentence scene description.")
    energy_level: int = Field(..., ge=1, le=5, description="Energy 1 (calm) – 5 (frenetic).")
    visual_quality: int = Field(..., ge=1, le=5, description="Visual quality 1 (poor) – 5 (excellent).")
    relevance_to_brief: int = Field(..., ge=1, le=5, description="Relevance to the brief 1 – 5.")
    key_quote: str = Field("", description="Most notable spoken line; empty if none.")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _mime_type_for(video_path: str) -> str:
    """Return the canonical mime type for a video file based on its extension."""
    suffix = Path(video_path).suffix.lower()
    return _MIME_TYPE_BY_EXT.get(suffix, _DEFAULT_MIME_TYPE)


def _require_api_key() -> str:
    """Read ``GOOGLE_API_KEY`` from the environment or raise a clear error."""
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GOOGLE_API_KEY environment variable is not set. "
            "Export it before calling any analyze.py tool "
            "(analyze_footage / review_output): "
            "`export GOOGLE_API_KEY=...`."
        )
    return api_key


def _wait_for_file_active(client: object, file_obj: object) -> object:
    """Poll an uploaded Gemini file until it reaches ACTIVE state.

    Args:
        client: The ``google.genai`` client used to perform the upload.
        file_obj: The :class:`~google.genai.types.File` returned by ``files.upload``.

    Returns:
        The refreshed file object once its state is ``ACTIVE``.

    Raises:
        RuntimeError: If the file does not become ACTIVE within
            ``_FILE_ACTIVE_TIMEOUT_SECONDS`` or transitions to a FAILED state.
    """
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
                "Timed out waiting for uploaded video to become ACTIVE "
                f"(last state={state!r})"
            )
        time.sleep(_FILE_ACTIVE_POLL_INTERVAL)
        current = client.files.get(name=current.name)  # type: ignore[attr-defined]


def _tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumerics, and drop trivial stopwords.

    Used for the local search ranker. Deterministic, dependency-free, and
    intentionally simple — good enough to catch obvious lexical overlap
    against shot descriptions and transcripts.
    """
    out: list[str] = []
    buf: list[str] = []
    for ch in text.lower():
        if ch.isalnum():
            buf.append(ch)
        else:
            if buf:
                token = "".join(buf)
                if token not in _STOPWORDS:
                    out.append(token)
                buf.clear()
    if buf:
        token = "".join(buf)
        if token not in _STOPWORDS:
            out.append(token)
    return out


def _score_shot(query_tokens: list[str], shot: Shot) -> float:
    """Compute a 0–1 lexical relevance score for a shot against the query.

    The score is the fraction of distinct query tokens that appear at least
    once in the shot's description or transcript. This is bounded in [0, 1]
    and stable, making the ``min_relevance`` filter trivially interpretable.
    """
    if not query_tokens:
        return 0.0
    distinct_query = set(query_tokens)
    # Build the haystack from description, transcript, roll_type, and the
    # source filename (stem).  B-Roll clips often have no transcript but
    # their filename encodes content (e.g. "ProductPackaging", "TextureHighlight").
    filename_stem = Path(shot.source_file).stem if shot.source_file else ""
    # Split CamelCase/dash-separated filename into tokens
    filename_tokens = re.sub(r"([A-Z])", r" \1", filename_stem)
    filename_tokens = filename_tokens.replace("-", " ").replace("_", " ")
    haystack = (
        set(_tokenize(shot.description))
        | set(_tokenize(shot.transcript))
        | set(_tokenize(getattr(shot, "roll_type", "")))
        | set(_tokenize(filename_tokens))
    )
    if not haystack:
        return 0.0
    hits = sum(1 for t in distinct_query if t in haystack)
    return hits / len(distinct_query)


def _validate_review_score(score: ReviewScore) -> ReviewScore:
    """Enforce the ReviewScore contract on any parsed Gemini output.

    The :class:`ReviewScore` schema only types its score fields as
    ``float`` and ``feedback`` as ``str``, so Pydantic on its own will
    happily accept values outside ``[0.0, 1.0]`` or an empty feedback
    string. ``review_output`` calls this helper on every return path
    (both the structured ``parsed`` branch and the raw-text fallback)
    so direct tool callers get the same guarantees the Reviewer agent's
    final-response check enforces.

    Args:
        score: A :class:`ReviewScore` freshly parsed from Gemini.

    Returns:
        The same ``score`` instance, unchanged, so callers can inline it
        in a ``return`` expression.

    Raises:
        RuntimeError: If any of the five score fields falls outside
            ``[0.0, 1.0]`` or if ``feedback`` is empty / whitespace-only.
    """
    for field_name in (
        "adherence",
        "pacing",
        "visual_quality",
        "watchability",
        "overall",
    ):
        value = getattr(score, field_name)
        if not (0.0 <= value <= 1.0):
            raise RuntimeError(
                f"review_output: {field_name}={value} out of [0.0, 1.0] range"
            )
    if not score.feedback.strip():
        raise RuntimeError(
            "review_output: feedback is empty — model must supply actionable text"
        )
    return score


# --------------------------------------------------------------------------- #
# Public tool functions
# --------------------------------------------------------------------------- #


def analyze_footage(video_path: str, brief: str) -> list[dict]:
    """Analyze a video clip scene-by-scene with Gemini 3.1 Pro.

    Sends ``video_path`` to Gemini via the unified ``google.genai`` SDK with
    a structured-output schema so the response is parsed directly into a
    list of scene dicts. Uses inline bytes for files up to 20 MB and the
    Gemini File API (with ACTIVE-state polling) for anything larger.

    Args:
        video_path: Absolute or relative path to a video file (.mp4/.mov/
            .m4v/.mkv). The mime type is inferred from the extension.
        brief: Free-text creative brief used to prime the model for the
            ``relevance_to_brief`` rating and the overall scene framing.

    Returns:
        A list of plain dicts, one per detected scene. Each dict has the
        keys ``start_time`` (float seconds), ``end_time`` (float seconds),
        ``description`` (str), ``energy_level`` (int 1–5),
        ``visual_quality`` (int 1–5), ``relevance_to_brief`` (int 1–5),
        and ``key_quote`` (str, possibly empty).

    Raises:
        RuntimeError: If ``GOOGLE_API_KEY`` is not set, the upload fails,
            or the response cannot be coerced into the schema.
        FileNotFoundError: If ``video_path`` does not exist on disk.
    """
    from google import genai
    from google.genai import types

    path = Path(video_path)
    if not path.exists():
        raise FileNotFoundError(f"video not found: {video_path}")

    api_key = _require_api_key()
    client = genai.Client(api_key=api_key)

    mime_type = _mime_type_for(video_path)
    size = path.stat().st_size

    if size > _INLINE_MAX_BYTES:
        uploaded = client.files.upload(
            file=str(path),
            config=types.UploadFileConfig(mime_type=mime_type),
        )
        uploaded = _wait_for_file_active(client, uploaded)
        video_part = types.Part.from_uri(
            file_uri=uploaded.uri,
            mime_type=mime_type,
        )
    else:
        video_part = types.Part.from_bytes(
            data=path.read_bytes(),
            mime_type=mime_type,
        )

    prompt = (
        "You are a senior video editor analyzing raw footage for an ad campaign.\n"
        "Creative brief:\n"
        f"{brief}\n\n"
        "Watch the entire clip and break it into distinct scenes. For each scene, "
        "report start_time and end_time in seconds (relative to the start of the "
        "clip), a one-sentence description, an energy_level (1=calm, 5=frenetic), "
        "a visual_quality rating (1=poor, 5=excellent), a relevance_to_brief "
        "rating (1=off-brief, 5=ideal), and the most notable spoken line as "
        "key_quote (empty string if there is no speech). Return a JSON array."
    )

    response = client.models.generate_content(
        model=_GEMINI_MODEL,
        contents=[video_part, prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=list[_SceneAnalysis],
        ),
    )

    parsed = getattr(response, "parsed", None)
    if parsed is None:
        # Fall back to validating the raw text payload through Pydantic so we
        # still raise on schema violations rather than returning garbage.
        import json

        raw_text = getattr(response, "text", None)
        if not raw_text:
            raise RuntimeError(
                "Gemini response had no parsed payload and no text body"
            )
        parsed = [_SceneAnalysis.model_validate(item) for item in json.loads(raw_text)]

    return [scene.model_dump() for scene in parsed]


def search_moments(
    footage_index_path: str,
    query: str,
    min_relevance: float,
    max_results: int,
) -> list[Shot]:
    """Rank shots in a FootageIndex by lexical relevance to a free-text query.

    Loads a serialized :class:`FootageIndex` from disk, scores every shot's
    description and transcript against ``query`` using a deterministic
    token-overlap ranker (lowercased, alphanumeric tokenization, trivial
    stopwords removed), filters by ``min_relevance``, and returns at most
    ``max_results`` shots sorted by descending score.

    The ranker is intentionally local and dependency-free: no LLM calls,
    no network. Score is the fraction of distinct query tokens that appear
    in the shot's description ∪ transcript, so it always lies in ``[0, 1]``
    and ``min_relevance`` is interpretable as "fraction of query terms that
    must match".

    Args:
        footage_index_path: Path to a JSON file produced by
            :mod:`src.pipeline.preprocess` (i.e. ``FootageIndex.model_dump_json``).
        query: Free-text query describing the desired moment.
        min_relevance: Minimum lexical score in ``[0.0, 1.0]``. Shots scoring
            below this are dropped before truncation.
        max_results: Maximum number of shots to return. Must be >= 0; a value
            of 0 yields an empty list.

    Returns:
        A list of :class:`Shot` objects (the originals from the loaded
        FootageIndex, not copies or dicts) sorted by descending relevance.
        Length is ``min(max_results, num_matching_shots)``.

    Raises:
        FileNotFoundError: If ``footage_index_path`` does not exist.
        pydantic.ValidationError: If the file is not a valid FootageIndex.
    """
    if max_results <= 0:
        return []

    index_path = Path(footage_index_path)
    if not index_path.exists():
        raise FileNotFoundError(
            f"footage_index_path does not exist: {footage_index_path}"
        )

    index = FootageIndex.model_validate_json(index_path.read_text(encoding="utf-8"))

    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    scored: list[tuple[float, int, Shot]] = []
    for idx, shot in enumerate(index.shots):
        score = _score_shot(query_tokens, shot)
        if score >= min_relevance:
            # Include the original index as a stable tiebreaker so equal
            # scores preserve input order deterministically.
            scored.append((score, idx, shot))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return [shot for _score, _idx, shot in scored[:max_results]]


def review_output(video_path: str, brief: str) -> ReviewScore:
    """Score a rendered ad against a creative brief via Gemini 3.1 Pro.

    Uploads ``video_path`` to Gemini and asks the model to act as a senior
    creative director, watching the full rendered cut and grading it on four
    qualitative dimensions plus an ``overall`` holistic judgment. The output
    is constrained to the :class:`~src.models.schemas.ReviewScore` Pydantic
    schema so callers can trust the shape without defensive parsing.

    Scoring contract (every score is a float in ``[0.0, 1.0]``):

    * ``adherence``: how well the cut honors ``brief.product`` / ``audience``
      / ``tone``. Is the right product on screen, is the tone consistent?
    * ``pacing``: energy arc, hook strength, and cut rhythm. A great cut
      opens strong and varies energy; a bad cut drags or feels choppy.
    * ``visual_quality``: composition, framing, color, clarity. Does the
      footage look like a finished ad, not rushed filler?
    * ``watchability``: would a real viewer keep watching to the end? This
      is the single most important retention signal.
    * ``overall``: holistic judgment — may deviate from the mean when a
      single dimension dominates (e.g., a broken hook tanks the whole cut).

    The ``feedback`` field is free text, but if ``overall < 0.7`` it MUST
    contain specific, actionable suggestions referencing what is wrong and
    what to try instead (e.g., "hook at 0:00–0:03 is weak — replace with
    the product close-up", "clip 3 drags, shorten by ~1.5s"). The prompt
    enforces this so downstream agents can act on the feedback directly.

    Files up to :data:`_INLINE_MAX_BYTES` are sent as inline bytes; larger
    files go through the Gemini File API with ACTIVE-state polling, exactly
    like :func:`analyze_footage`.

    Args:
        video_path: Absolute or relative path to the rendered ad video
            (.mp4 / .mov / .m4v / .mkv). The mime type is inferred from
            the extension.
        brief: The creative brief as a string — typically
            ``CreativeBrief.model_dump_json()`` so the model sees every
            field, but any descriptive text is accepted.

    Returns:
        A validated :class:`~src.models.schemas.ReviewScore` instance with
        five float dimensions in ``[0.0, 1.0]`` and a non-empty
        ``feedback`` string.

    Raises:
        FileNotFoundError: If ``video_path`` does not exist on disk.
        RuntimeError: If ``GOOGLE_API_KEY`` is not set, the upload fails,
            or Gemini returns no parsed payload and no text body.
        pydantic.ValidationError: If the model emits JSON that does not
            satisfy the :class:`ReviewScore` schema.
    """
    from google import genai
    from google.genai import types

    # Prompt-injection guard: if ``run_reviewer`` (or another trusted caller)
    # has bound a canonical video path for this call, reject any mismatch
    # BEFORE reading bytes or hitting the File API. This prevents a crafted
    # brief from coercing the agent into uploading arbitrary local files.
    # When no binding is present (``None``), direct tool invocation is still
    # allowed — the existence check below applies either way.
    _allowed = _ALLOWED_REVIEW_VIDEO_PATH.get()
    if _allowed is not None:
        supplied_abs = str(Path(video_path).resolve())
        if supplied_abs != _allowed:
            raise RuntimeError(
                f"review_output: video_path mismatch — expected {_allowed!r}, "
                f"got {supplied_abs!r}. The Reviewer agent may only operate "
                "on the pre-approved rendered cut."
            )

    path = Path(video_path)
    if not path.exists():
        raise FileNotFoundError(f"video not found: {video_path}")

    api_key = _require_api_key()
    client = genai.Client(api_key=api_key)

    mime_type = _mime_type_for(video_path)
    size = path.stat().st_size

    if size > _INLINE_MAX_BYTES:
        uploaded = client.files.upload(
            file=str(path),
            config=types.UploadFileConfig(mime_type=mime_type),
        )
        uploaded = _wait_for_file_active(client, uploaded)
        video_part = types.Part.from_uri(
            file_uri=uploaded.uri,
            mime_type=mime_type,
        )
    else:
        video_part = types.Part.from_bytes(
            data=path.read_bytes(),
            mime_type=mime_type,
        )

    prompt = (
        "You are a senior creative director reviewing a finished ad against "
        "its brief. Watch the entire rendered video and grade it honestly.\n\n"
        "Creative brief:\n"
        f"{brief}\n\n"
        "Score each dimension as a float in [0.0, 1.0] (0.0 = terrible, "
        "1.0 = flawless). Do NOT clamp scores to round numbers — use the "
        "full range.\n"
        "- adherence: how well the cut honors the brief's product, "
        "audience, and tone.\n"
        "- pacing: energy arc, hook strength, and cut rhythm.\n"
        "- visual_quality: composition, framing, color, and clarity.\n"
        "- watchability: would a real viewer keep watching to the end.\n"
        "- overall: holistic judgment, NOT a plain average — let a broken "
        "dimension drag the whole score when it deserves to.\n\n"
        "Write the 'feedback' field as concrete, actionable text that "
        "identifies what works and what should change. If overall < 0.7, "
        "feedback MUST include specific actionable suggestions that "
        "reference beats, timestamps, or clip numbers (for example: "
        "'hook at 0:00–0:03 is weak — replace clip 1 with the product "
        "close-up', 'clip 3 drags, shorten by ~1.5s or swap for a higher "
        "energy moment', 'tone is too formal for the stated audience — "
        "re-cut with the casual reaction shots'). Never return vague "
        "feedback like 'could be better'.\n\n"
        "Return ONLY the JSON object matching the ReviewScore schema — no "
        "preamble, no commentary, no markdown fences."
    )

    response = client.models.generate_content(
        model=_GEMINI_MODEL,
        contents=[video_part, prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ReviewScore,
        ),
    )

    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, ReviewScore):
        return _validate_review_score(parsed)

    raw_text = getattr(response, "text", None)
    if not raw_text:
        raise RuntimeError(
            "Gemini response had no parsed payload and no text body"
        )
    return _validate_review_score(ReviewScore.model_validate_json(raw_text))
