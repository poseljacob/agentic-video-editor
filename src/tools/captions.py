"""ASS caption generation from per-word timestamps."""

from __future__ import annotations

from pathlib import Path

from pysubs2 import Alignment, Color, SSAEvent, SSAFile, SSAStyle

from src.models.schemas import FootageIndex, Shot, WordTimestamp

_SHOT_MATCH_EPSILON = 1e-6
_MAX_WORDS_PER_PHRASE = 4
_MIN_WORDS_PER_PHRASE = 2
_MAX_CHARS_PER_PHRASE = 28
_MAX_PHRASE_DURATION_SECONDS = 1.8
_MAX_INTER_WORD_GAP_SECONDS = 0.45
_TERMINAL_PUNCTUATION = (".", "!", "?", ";")
_HIGHLIGHT_TAG = r"{\1c&H4ACFFF&}"
_RESET_TAG = r"{\r}"


def _require_file(path: str, kind: str) -> None:
    if not Path(path).exists():
        raise FileNotFoundError(f"{kind} not found: {path}")


def _format_tokens(tokens: list[str]) -> str:
    """Join caption tokens into readable text."""
    if not tokens:
        return ""

    no_leading_space = {".", ",", "!", "?", ";", ":", "%", ")", "]", "}"}
    no_trailing_space = {"(", "[", "{", "$", "#"}

    parts: list[str] = []
    for token in tokens:
        if not token:
            continue
        if parts and token not in no_leading_space:
            prev = parts[-1]
            if prev not in no_trailing_space:
                parts.append(" ")
        parts.append(token)
    return "".join(parts).strip()


def _resolve_shot(footage_index: FootageIndex, shot_id: str) -> Shot:
    sep = shot_id.rfind("#")
    if sep == -1:
        raise ValueError(
            f"shot_id {shot_id!r} is missing a '#' separator; expected "
            "format '<source_file>#<start_time>'"
        )
    source_file = shot_id[:sep]
    start_time = float(shot_id[sep + 1 :])

    for shot in footage_index.shots:
        if (
            shot.source_file == source_file
            and abs(shot.start_time - start_time) < _SHOT_MATCH_EPSILON
        ):
            return shot
    raise ValueError(f"shot_id {shot_id!r} not found in FootageIndex")


def _words_for_window(
    shot: Shot,
    clip_start: float,
    clip_end: float,
) -> list[WordTimestamp]:
    """Select shot words whose midpoint lies within the trimmed clip."""
    selected: list[WordTimestamp] = []
    for word in shot.words:
        midpoint = (word.start + word.end) / 2.0
        if not (clip_start <= midpoint < clip_end):
            continue
        start = max(clip_start, word.start) - clip_start
        end = min(clip_end, word.end) - clip_start
        if end <= start:
            continue
        selected.append(
            WordTimestamp(word=word.word, start=float(start), end=float(end))
        )
    return selected


def has_words_in_window(shot: Shot, clip_start: float, clip_end: float) -> bool:
    """Return True when a trimmed clip contains spoken words to caption."""
    return bool(_words_for_window(shot, clip_start, clip_end))


def _should_break_phrase(
    current: list[WordTimestamp],
    next_word: WordTimestamp | None,
) -> bool:
    if next_word is None:
        return True
    if len(current) < _MIN_WORDS_PER_PHRASE:
        return False

    current_text = _format_tokens([word.word for word in current])
    current_duration = current[-1].end - current[0].start
    gap = next_word.start - current[-1].end
    last_token = current[-1].word

    return (
        len(current) >= _MAX_WORDS_PER_PHRASE
        or len(current_text) >= _MAX_CHARS_PER_PHRASE
        or current_duration >= _MAX_PHRASE_DURATION_SECONDS
        or gap >= _MAX_INTER_WORD_GAP_SECONDS
        or last_token.endswith(_TERMINAL_PUNCTUATION)
    )


def _group_words_into_phrases(words: list[WordTimestamp]) -> list[list[WordTimestamp]]:
    """Group relative word timestamps into short readable caption phrases."""
    if not words:
        return []

    phrases: list[list[WordTimestamp]] = []
    current: list[WordTimestamp] = []

    for index, word in enumerate(words):
        current.append(word)
        next_word = words[index + 1] if index + 1 < len(words) else None
        if _should_break_phrase(current, next_word):
            phrases.append(current)
            current = []

    if current:
        phrases.append(current)

    if len(phrases) >= 2 and len(phrases[-1]) == 1:
        phrases[-2].extend(phrases.pop())

    return phrases


def _highlighted_phrase_text(
    phrase: list[WordTimestamp],
    highlight_index: int,
) -> str:
    tokens: list[str] = []
    for index, word in enumerate(phrase):
        token = word.word
        if index == highlight_index:
            token = f"{_HIGHLIGHT_TAG}{token}{_RESET_TAG}"
        tokens.append(token)
    return _format_tokens(tokens)


def _build_subtitle_file() -> SSAFile:
    subs = SSAFile()
    subs.info["PlayResX"] = "1080"
    subs.info["PlayResY"] = "1920"
    subs.styles["Default"] = SSAStyle(
        fontname="Helvetica",
        fontsize=44,
        primarycolor=Color(255, 255, 255, 0),
        secondarycolor=Color(74, 207, 255, 0),
        outlinecolor=Color(0, 0, 0, 0),
        backcolor=Color(0, 0, 0, 0),
        bold=True,
        outline=3,
        shadow=0,
        alignment=Alignment.BOTTOM_CENTER,
        marginl=80,
        marginr=80,
        marginv=220,
    )
    return subs


def _to_ms(seconds: float) -> int:
    return max(0, int(round(seconds * 1000)))


def generate_ass_captions(
    footage_index_path: str,
    shot_id: str,
    clip_start: float,
    clip_end: float,
    output: str,
) -> str:
    """Write an ASS subtitle file for a trimmed clip using shot word timings."""
    _require_file(footage_index_path, "Footage index")
    if clip_end <= clip_start:
        raise ValueError(
            f"clip_end ({clip_end}) must be greater than clip_start ({clip_start})"
        )

    index = FootageIndex.model_validate_json(
        Path(footage_index_path).read_text(encoding="utf-8")
    )
    shot = _resolve_shot(index, shot_id)
    words = _words_for_window(shot, clip_start, clip_end)
    if not words:
        raise ValueError(
            f"shot_id {shot_id!r} has no captionable words in "
            f"[{clip_start}, {clip_end})"
        )

    subs = _build_subtitle_file()
    for phrase in _group_words_into_phrases(words):
        phrase_end = phrase[-1].end
        for index, word in enumerate(phrase):
            next_start = phrase[index + 1].start if index + 1 < len(phrase) else phrase_end
            end_time = max(word.end, next_start)
            subs.events.append(
                SSAEvent(
                    start=_to_ms(word.start),
                    end=_to_ms(end_time),
                    text=_highlighted_phrase_text(phrase, index),
                )
            )

    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subs.save(str(output_path))
    return str(output_path)


def burn_ass_subtitles(video: str, subtitles: str, output: str) -> str:
    """Burn an ASS subtitle file into a video via FFmpeg's ass filter."""
    import subprocess

    _require_file(video, "Video")
    _require_file(subtitles, "Subtitle file")

    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    subtitle_path = str(Path(subtitles).resolve()).replace("\\", r"\\").replace(":", r"\:")
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        video,
        "-vf",
        f"ass={subtitle_path}",
        "-c:a",
        "copy",
        str(output_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"FFmpeg failed: {exc.stderr}") from exc
    return str(output_path)
