"""Final-render tool: H.264 MP4 export with aspect-ratio-safe scaling."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _ensure_parent(output: str) -> None:
    """Create the parent directory for ``output`` if missing."""
    Path(output).parent.mkdir(parents=True, exist_ok=True)


def _require_file(path: str, kind: str) -> None:
    """Raise ``FileNotFoundError`` if ``path`` does not exist on disk."""
    if not Path(path).exists():
        raise FileNotFoundError(f"{kind} not found: {path}")


def _parse_resolution(resolution: str) -> tuple[int, int]:
    """Parse a ``WxH`` resolution string into a ``(width, height)`` tuple.

    Args:
        resolution: A string like ``"1080x1920"``.

    Returns:
        Tuple of integer ``(width, height)``.

    Raises:
        ValueError: If ``resolution`` is malformed or non-positive.
    """
    parts = resolution.lower().split("x")
    if len(parts) != 2:
        raise ValueError(
            f"resolution must be 'WxH' (e.g. '1080x1920'), got '{resolution}'"
        )
    try:
        width, height = int(parts[0]), int(parts[1])
    except ValueError as e:
        raise ValueError(
            f"resolution components must be integers, got '{resolution}'"
        ) from e
    if width <= 0 or height <= 0:
        raise ValueError(
            f"resolution components must be positive, got '{resolution}'"
        )
    return width, height


def render_final(
    video: str,
    output: str,
    resolution: str = "1080x1920",
) -> str:
    """Re-encode ``video`` to H.264 MP4 at ``resolution`` for delivery.

    Uses ``libx264`` with ``preset medium`` and ``crf 23`` plus AAC audio at
    128 kbps. The scale filter preserves the source aspect ratio
    (``force_original_aspect_ratio=decrease``) and pads the result with
    black bars (``pad``) so the output exactly matches the requested
    dimensions without distortion. The default ``1080x1920`` produces a
    vertical 9:16 video suited for TikTok/Reels.

    Args:
        video: Input video path.
        output: Destination path for the rendered MP4.
        resolution: Target ``WxH`` resolution (default ``"1080x1920"``).

    Returns:
        The ``output`` path on success.

    Raises:
        FileNotFoundError: If ``video`` is missing.
        ValueError: If ``resolution`` is malformed.
        RuntimeError: If ffmpeg fails.
    """
    _require_file(video, "Video")
    width, height = _parse_resolution(resolution)
    _ensure_parent(output)

    # setsar=1 normalizes the sample aspect ratio so non-square-pixel sources
    # (e.g. anamorphic 1080i, some phone cameras) display at the intended
    # aspect ratio rather than the raw pixel grid.
    scale_filter = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        f"setsar=1"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        video,
        "-vf",
        scale_filter,
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        output,
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"FFmpeg failed: {e.stderr}") from e
    return output
