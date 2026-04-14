"""US-008 routes -- EditPlan timeline viewer backend.

Two endpoints power the timeline UI:

* ``GET /api/jobs/{job_id}/edit-plan`` returns the stored
  :class:`~src.models.schemas.EditPlan` for a completed job, enriched
  per-entry with the display metadata the timeline needs (derived
  ``source_filename``, ``source_timestamp``, ``display_label``,
  ``duration``, ``roll_type`` resolved against the job's
  :class:`~src.models.schemas.FootageIndex`, and a thumbnail URL).
* ``GET /api/clips/{job_id}/{position}/thumbnail`` returns a JPEG of the
  first frame of the cut clip at ``position`` for the given job. The
  clip file is produced by the editor at
  ``output/working/{brief_slug}/clip_{position:02d}.mp4``; the thumbnail
  is extracted via a one-shot ffmpeg subprocess and cached on disk next
  to the clip so repeat requests do not re-invoke ffmpeg.

Both endpoints read from :class:`~src.web.jobs.JobRegistry` via the
existing :func:`src.web.routes.jobs.get_registry` dependency and never
mutate job state.

Design notes
------------

The ``roll_type`` resolution inside the edit-plan endpoint uses the same
"split shot_id on the LAST ``#`` and match (source_file, start_time)
within a small epsilon" convention as
:func:`src.agents.editor._resolve_shot`, but inlined so a single bad
entry (malformed id, missing shot, unreadable index) degrades to
``"unknown"`` instead of raising. The timeline must render even for
partially broken plans, so no per-entry failure is allowed to 500 the
whole request.

The FootageIndex is loaded exactly once per request handler invocation
and then used as a lookup table for every entry. If the index file is
missing or fails to parse, every entry silently falls back to
``"unknown"`` roll_type -- this is much more useful to a frontend than
a 500.

Thumbnails are cached on disk at
``output/working/{brief_slug}/clip_{position:02d}.thumb.jpg``. Disk
cache is the primary mechanism (survives worker restarts); there is no
in-memory cache because FastAPI workers may be recycled and a cold
cache hit is still ~50ms vs. ~300ms+ for a fresh ffmpeg extraction.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from src.agents.editor import _slugify_brief
from src.models.schemas import FootageIndex, Shot
from src.web.jobs import Job
from src.web.routes.jobs import get_registry

#: Repo root -- resolved relative to this file so the code works no
#: matter what ``cwd`` the server was launched from. Matches the
#: ``parents[2]`` depth used in :mod:`src.web.app` (this file is at
#: ``src/web/routes/clips.py`` so ``parents[3]`` is the repo root).
REPO_ROOT = Path(__file__).resolve().parents[3]
OUTPUT_DIR = REPO_ROOT / "output"

#: Float tolerance when matching ``EditPlanEntry.shot_id`` start_time
#: suffixes to ``Shot.start_time``. Slightly looser than the editor's
#: ``1e-6`` because the shot_id string may round-trip through JSON and
#: lose a few LSBs, and "wrong roll_type label" is a strictly cosmetic
#: failure so we prefer more matches over fewer.
_SHOT_MATCH_EPSILON = 1e-3

router = APIRouter(tags=["clips"])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _load_footage_index(path_str: str | None) -> FootageIndex | None:
    """Parse a :class:`FootageIndex` from disk, swallowing any failure.

    Returns ``None`` if the path is missing / unreadable / malformed.
    The edit-plan endpoint uses ``None`` as the signal to default every
    entry's ``roll_type`` to ``"unknown"``.
    """
    if not path_str:
        return None
    try:
        text = Path(path_str).read_text()
    except OSError:
        return None
    try:
        return FootageIndex.model_validate_json(text)
    except Exception:
        return None


def _resolve_shot_for_entry(
    shot_id: str,
    index: FootageIndex | None,
) -> Shot | None:
    """Return the :class:`Shot` referenced by ``shot_id`` or ``None``.

    Mirrors the split-on-last-``#`` convention used by
    :func:`src.agents.editor._resolve_shot` but NEVER raises: malformed
    ids, missing separators, unparseable suffixes, missing indexes, and
    unmatched ids all collapse to ``None``. Callers use the sentinel to
    default the entry's ``roll_type`` to ``"unknown"``.
    """
    if index is None:
        return None
    sep = shot_id.rfind("#")
    if sep == -1:
        return None
    source_file = shot_id[:sep]
    suffix = shot_id[sep + 1 :]
    try:
        start_time = float(suffix)
    except ValueError:
        return None
    for shot in index.shots:
        if (
            shot.source_file == source_file
            and abs(shot.start_time - start_time) < _SHOT_MATCH_EPSILON
        ):
            return shot
    return None


def _derive_source_timestamp(shot_id: str) -> float:
    """Parse the numeric suffix from a shot_id, defaulting to ``0.0``.

    Used for the display label even when roll_type resolution has
    failed -- a badly formed id still gets a sensible label instead of
    an exception bubbling up to the request handler.
    """
    sep = shot_id.rfind("#")
    if sep == -1:
        return 0.0
    try:
        return float(shot_id[sep + 1 :])
    except ValueError:
        return 0.0


def _build_entry_payload(
    entry: dict[str, Any],
    job_id: str,
    index: FootageIndex | None,
) -> dict[str, Any]:
    """Shape a single :class:`EditPlanEntry` dict for the timeline UI.

    ``entry`` is the serialized dict form produced by
    :func:`src.web.jobs._serialize_result` -- ie. already ``model_dump``'d.
    We derive the display fields here instead of on the frontend so the
    contract is stable and testable server-side.
    """
    shot_id = entry.get("shot_id") or ""
    # Split on the LAST '#' to match _resolve_shot_for_entry -- source
    # paths may contain '#' characters, so the separator is always the
    # final occurrence.
    _sep = shot_id.rfind("#") if shot_id else -1
    source_file = shot_id[:_sep] if _sep != -1 else shot_id
    # ``os.path.basename`` (not Path.name) because the shot_id may hold a
    # POSIX path on a Windows server or vice versa; stdlib basename
    # handles both separators without trying to normalize.
    source_filename = os.path.basename(source_file) if source_file else ""
    source_timestamp = _derive_source_timestamp(shot_id)
    display_label = f"{source_filename}@{source_timestamp:.1f}s"

    shot = _resolve_shot_for_entry(shot_id, index)
    roll_type = shot.roll_type if shot is not None else "unknown"

    start_trim = float(entry.get("start_trim", 0.0))
    end_trim = float(entry.get("end_trim", 0.0))
    position = int(entry.get("position", 0))
    return {
        "position": position,
        "shot_id": shot_id,
        "source_file": source_file,
        "source_filename": source_filename,
        "source_timestamp": source_timestamp,
        "display_label": display_label,
        "start_trim": start_trim,
        "end_trim": end_trim,
        "duration": end_trim - start_trim,
        "text_overlay": entry.get("text_overlay"),
        "transition": entry.get("transition"),
        "roll_type": roll_type,
        "thumbnail_url": f"/api/clips/{job_id}/{position}/thumbnail",
    }


def _require_completed_job_with_plan(
    job: Job | None,
    job_id: str,
) -> dict[str, Any]:
    """Validate ``job`` is completed with a serialized ``edit_plan``.

    Returns the ``edit_plan`` dict on success. Raises 404/409 otherwise,
    matching the detail-message conventions in
    :mod:`tests.test_web_jobs_review` (detail includes id + wait hint).
    """
    if job is None:
        raise HTTPException(
            status_code=404,
            detail=f"job {job_id!r} not found",
        )
    if job.status != "completed" or job.result is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"job {job_id!r} has no edit plan yet (status={job.status!r}); "
                "wait until the pipeline completes"
            ),
        )
    edit_plan = job.result.get("edit_plan")
    if edit_plan is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"job {job_id!r} completed without an edit_plan; nothing to "
                "display"
            ),
        )
    return edit_plan


# --------------------------------------------------------------------------- #
# GET /api/jobs/{job_id}/edit-plan
# --------------------------------------------------------------------------- #


@router.get("/api/jobs/{job_id}/edit-plan")
async def get_edit_plan(job_id: str, request: Request) -> dict[str, Any]:
    """Return the enriched EditPlan for a completed job.

    Each entry is augmented with the display fields the timeline UI
    needs (source filename + timestamp, duration, roll_type, thumbnail
    URL). Entries come back sorted by ``position`` ascending.
    """
    registry = get_registry(request)
    job = registry.get(job_id)
    edit_plan = _require_completed_job_with_plan(job, job_id)
    assert job is not None  # narrowed by the helper above

    # Load the FootageIndex exactly once per request; every entry's
    # roll_type lookup reuses this single parsed model.
    index = _load_footage_index(job.footage_index_path)

    raw_entries = list(edit_plan.get("entries") or [])
    raw_entries.sort(key=lambda e: int(e.get("position", 0)))
    entries = [_build_entry_payload(e, job_id, index) for e in raw_entries]

    return {
        "job_id": job_id,
        "total_duration": float(edit_plan.get("total_duration", 0.0)),
        "entry_count": len(entries),
        "entries": entries,
    }


# --------------------------------------------------------------------------- #
# GET /api/clips/{job_id}/{position}/thumbnail
# --------------------------------------------------------------------------- #


def _clip_paths(job: Job, position: int) -> tuple[Path, Path]:
    """Return ``(clip_path, thumb_path)`` for a job + position.

    Both paths live under ``output/working/{brief_slug}/`` so the
    thumbnail sits next to its source clip -- makes cache invalidation
    trivial (delete the brief dir and every cache entry goes with it).
    """
    brief_slug = _slugify_brief(job.brief)
    working_dir = OUTPUT_DIR / "working" / brief_slug
    clip_path = working_dir / f"clip_{position:02d}.mp4"
    thumb_path = working_dir / f"clip_{position:02d}.thumb.jpg"
    return clip_path, thumb_path


def _extract_first_frame(clip_path: Path) -> bytes:
    """Run ffmpeg once to pull a single JPEG-encoded frame out of ``clip_path``.

    Uses ``-ss 0`` before ``-i`` for fast seek + ``-frames:v 1`` to cap
    the output at one frame. ``-f image2 -vcodec mjpeg pipe:1`` writes
    the JPEG bytes to stdout so we do not need a scratch file -- the
    caller is responsible for persisting to the disk cache.

    Passes argv as a list with ``shell=False`` (the default): the clip
    path comes from filesystem composition, not user input, but the
    list form is the right habit regardless.
    """
    cmd = [
        "ffmpeg",
        "-ss",
        "0",
        "-i",
        str(clip_path),
        "-frames:v",
        "1",
        "-f",
        "image2",
        "-vcodec",
        "mjpeg",
        "pipe:1",
    ]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"ffmpeg binary not available: {exc}",
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr_tail = (exc.stderr or b"").decode("utf-8", errors="replace")[-512:]
        raise HTTPException(
            status_code=500,
            detail=f"ffmpeg failed to extract first frame: {stderr_tail}",
        ) from exc
    return completed.stdout


@router.get("/api/clips/{job_id}/{position}/thumbnail")
async def get_clip_thumbnail(
    job_id: str,
    position: int,
    request: Request,
) -> Response:
    """Return a JPEG first-frame thumbnail for clip ``position`` of ``job_id``.

    Cached on disk under the brief's working directory so repeat
    requests never re-invoke ffmpeg. Browser-side caching is nudged via
    ``Cache-Control: public, max-age=3600`` so timeline scrolls do not
    re-fetch on every viewport shift.
    """
    registry = get_registry(request)
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail=f"job {job_id!r} not found",
        )
    if job.status != "completed" or job.result is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"job {job_id!r} has no rendered clips yet "
                f"(status={job.status!r}); wait until the pipeline completes"
            ),
        )

    # Bounds-check ``position`` against the stored edit plan so a bad
    # request gets a 404 instead of silently 500ing on a missing file.
    edit_plan = job.result.get("edit_plan") or {}
    raw_entries = edit_plan.get("entries") or []
    if position < 0 or position >= len(raw_entries):
        raise HTTPException(
            status_code=404,
            detail=(
                f"clip position {position} out of range for job {job_id!r} "
                f"(edit plan has {len(raw_entries)} entries)"
            ),
        )

    clip_path, thumb_path = _clip_paths(job, position)

    if thumb_path.exists():
        jpeg_bytes = thumb_path.read_bytes()
    else:
        if not clip_path.exists():
            raise HTTPException(
                status_code=404,
                detail=(
                    f"clip file for job {job_id!r} position {position} not "
                    f"found on disk at {clip_path}"
                ),
            )
        jpeg_bytes = _extract_first_frame(clip_path)
        # Persist the cache. Best-effort: if the write fails (permission,
        # disk full, race with another writer), still serve the bytes we
        # have in memory so the request does not fail end-to-end.
        try:
            thumb_path.parent.mkdir(parents=True, exist_ok=True)
            thumb_path.write_bytes(jpeg_bytes)
        except OSError:
            pass

    return Response(
        content=jpeg_bytes,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=3600"},
    )
