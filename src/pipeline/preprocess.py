"""Pre-processing pipeline: scene detection + transcription.

Walks an input directory of video files, splits each clip into shots using
PySceneDetect, transcribes audio with Faster-Whisper, aligns word-level
timestamps to shot boundaries, and emits a serialized FootageIndex JSON.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from src.models.schemas import FootageIndex, Shot, WordTimestamp

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

VIDEO_SUFFIXES = {".mov", ".mp4", ".m4v", ".mkv"}

#: Directory name patterns used to auto-detect roll type.
_ROLL_TYPE_PATTERNS: dict[str, str] = {
    "a-roll": "a-roll",
    "a_roll": "a-roll",
    "aroll": "a-roll",
    "b-roll": "b-roll",
    "b_roll": "b-roll",
    "broll": "b-roll",
}


def _detect_roll_type(video_path: Path) -> str:
    """Infer 'a-roll', 'b-roll', or 'unknown' from parent directory name."""
    for parent in video_path.parents:
        name_lower = parent.name.lower().replace(" ", "")
        if name_lower in _ROLL_TYPE_PATTERNS:
            return _ROLL_TYPE_PATTERNS[name_lower]
    return "unknown"


def _log(msg: str) -> None:
    """Emit progress to stderr."""
    print(msg, file=sys.stderr, flush=True)


def _detect_shots(
    video_path: Path, scene_threshold: float = 27.0
) -> list[tuple[float, float]]:
    """Detect shots in a video using PySceneDetect ContentDetector.

    Args:
        video_path: Path to the video file.
        scene_threshold: ContentDetector threshold (default 27.0).

    Returns:
        List of (start_seconds, end_seconds) tuples. If no cuts are detected,
        returns a single shot spanning the entire video duration.
    """
    from scenedetect import ContentDetector, SceneManager, open_video

    video = open_video(str(video_path))
    scene_manager = SceneManager()
    scene_manager.add_detector(ContentDetector(threshold=scene_threshold))
    scene_manager.detect_scenes(video, show_progress=False)
    scene_list = scene_manager.get_scene_list()

    if not scene_list:
        try:
            duration = video.duration.get_seconds()
        except Exception:
            duration = 0.0
        return [(0.0, float(duration))]

    return [
        (float(start.get_seconds()), float(end.get_seconds()))
        for start, end in scene_list
    ]


def _transcribe_words(
    model: WhisperModel, video_path: Path
) -> list[tuple[float, float, str]]:
    """Transcribe a video and return word-level timestamps.

    Args:
        model: A pre-loaded Faster-Whisper model.
        video_path: Path to the video file.

    Returns:
        List of (start_seconds, end_seconds, word_text) tuples across the
        entire file. Returns empty list if no speech is detected.
    """
    segments, _info = model.transcribe(
        str(video_path), word_timestamps=True, temperature=0
    )
    words: list[tuple[float, float, str]] = []
    for segment in segments:
        seg_words = getattr(segment, "words", None) or []
        for w in seg_words:
            if w.start is None or w.end is None:
                continue
            words.append((float(w.start), float(w.end), str(w.word)))
    return words


def _normalize_word_text(text: str) -> str:
    """Collapse whitespace in a transcribed word while keeping punctuation."""
    return " ".join(text.split())


def _words_to_text(words: list[WordTimestamp]) -> str:
    """Render a token stream into readable caption/transcript text."""
    if not words:
        return ""

    no_leading_space = {".", ",", "!", "?", ";", ":", "%", ")", "]", "}"}
    no_trailing_space = {"(", "[", "{", "$", "#"}

    parts: list[str] = []
    for word in words:
        token = word.word
        if not token:
            continue
        if parts and token not in no_leading_space:
            prev = parts[-1]
            if prev not in no_trailing_space:
                parts.append(" ")
        parts.append(token)
    return "".join(parts).strip()


def _words_for_shot(
    words: list[tuple[float, float, str]],
    shot_start: float,
    shot_end: float,
) -> list[WordTimestamp]:
    """Return word timestamps whose midpoint falls within the shot window."""
    selected: list[WordTimestamp] = []
    for w_start, w_end, text in words:
        midpoint = (w_start + w_end) / 2.0
        if shot_start <= midpoint < shot_end:
            normalized = _normalize_word_text(text)
            if not normalized:
                continue
            start = max(shot_start, w_start)
            end = min(shot_end, w_end)
            if end <= start:
                continue
            selected.append(
                WordTimestamp(word=normalized, start=float(start), end=float(end))
            )
    return selected


def _process_video(
    model: WhisperModel,
    video_path: Path,
    scene_threshold: float = 27.0,
) -> list[Shot]:
    """Detect shots in a video and assemble Shot objects with aligned transcripts."""
    shot_ranges = _detect_shots(video_path, scene_threshold=scene_threshold)
    try:
        words = _transcribe_words(model, video_path)
    except Exception as exc:
        _log(f"[preprocess]   no transcript for {video_path.name}: {exc}")
        words = []

    roll_type = _detect_roll_type(video_path)
    shots: list[Shot] = []
    for shot_start, shot_end in shot_ranges:
        shot_words = _words_for_shot(words, shot_start, shot_end)
        transcript = _words_to_text(shot_words)
        shots.append(
            Shot(
                source_file=str(video_path),
                start_time=shot_start,
                end_time=shot_end,
                description="",
                energy_level=0,
                relevance_score=0.0,
                transcript=transcript,
                words=shot_words,
                roll_type=roll_type,
            )
        )
    return shots


def preprocess_footage(
    input_dir: str,
    output_path: str,
    *,
    scene_threshold: float = 27.0,
    whisper_model_size: str = "base",
) -> FootageIndex:
    """Pre-process a directory of footage into a FootageIndex.

    Walks ``input_dir`` recursively for video files, runs PySceneDetect to
    find shot boundaries, transcribes each file with Faster-Whisper, aligns
    word-level timestamps to shot ranges, and serializes the resulting
    FootageIndex to ``output_path`` as JSON.

    Args:
        input_dir: Directory containing source video files.
        output_path: File path to write the serialized FootageIndex JSON.
        scene_threshold: PySceneDetect ContentDetector threshold.
        whisper_model_size: Faster-Whisper model size (default "base").

    Returns:
        The constructed FootageIndex (also written to ``output_path``).

    Raises:
        FileNotFoundError: If ``input_dir`` does not exist.
        NotADirectoryError: If ``input_dir`` is not a directory.
    """
    from faster_whisper import WhisperModel

    input_root = Path(input_dir)
    if not input_root.exists():
        raise FileNotFoundError(f"input_dir does not exist: {input_dir}")
    if not input_root.is_dir():
        raise NotADirectoryError(f"input_dir is not a directory: {input_dir}")

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    video_files = sorted(
        p
        for p in input_root.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES
    )

    _log(
        f"[preprocess] scanning {input_root} — found {len(video_files)} video file(s)"
    )

    if not video_files:
        _log("[preprocess] WARNING: no video files found")

    _log(f"[preprocess] loading Faster-Whisper model '{whisper_model_size}'...")
    model = WhisperModel(whisper_model_size, device="cpu", compute_type="int8")

    all_shots: list[Shot] = []
    total = len(video_files)
    for idx, video_path in enumerate(video_files, start=1):
        _log(f"[{idx}/{total}] processing {video_path.name}...")
        try:
            shots = _process_video(
                model, video_path, scene_threshold=scene_threshold
            )
            _log(f"[{idx}/{total}]   -> {len(shots)} shot(s)")
            all_shots.extend(shots)
        except Exception as exc:
            _log(f"[{idx}/{total}]   ERROR processing {video_path.name}: {exc}")
            continue

    total_duration = sum(
        max(0.0, shot.end_time - shot.start_time) for shot in all_shots
    )

    footage_index = FootageIndex(
        source_dir=str(input_root),
        shots=all_shots,
        total_duration=total_duration,
        created_at=datetime.now(timezone.utc),
    )

    out_path.write_text(
        footage_index.model_dump_json(indent=2),
        encoding="utf-8",
    )
    _log(
        f"[preprocess] wrote FootageIndex with {len(all_shots)} shot(s), "
        f"total_duration={total_duration:.2f}s -> {out_path}"
    )

    return footage_index


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(
            "Usage: python -m src.pipeline.preprocess <input_dir> <output_path>",
            file=sys.stderr,
        )
        sys.exit(1)
    preprocess_footage(sys.argv[1], sys.argv[2])
