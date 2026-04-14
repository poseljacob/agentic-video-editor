from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.models.schemas import FootageIndex, Shot, WordTimestamp
from src.pipeline.preprocess import _words_for_shot, _words_to_text
from src.tools.captions import generate_ass_captions, has_words_in_window


class PreprocessWordAlignmentTests(unittest.TestCase):
    def test_words_for_shot_preserves_timing_and_formats_transcript(self) -> None:
        words = [
            (0.00, 0.18, " I"),
            (0.19, 0.36, "always"),
            (0.37, 0.54, "taken"),
            (0.55, 0.70, "care"),
            (0.71, 0.80, "!"),
            (1.20, 1.35, " Later"),
        ]

        shot_words = _words_for_shot(words, 0.0, 0.85)

        self.assertEqual(
            [word.word for word in shot_words],
            ["I", "always", "taken", "care", "!"],
        )
        self.assertEqual(_words_to_text(shot_words), "I always taken care!")


class CaptionGenerationTests(unittest.TestCase):
    def test_generate_ass_captions_writes_highlighted_dialogue_events(self) -> None:
        shot = Shot(
            source_file="/tmp/source.mov",
            start_time=10.0,
            end_time=12.5,
            description="",
            energy_level=0,
            relevance_score=0.0,
            transcript="I always taken care",
            words=[
                WordTimestamp(word="I", start=10.00, end=10.14),
                WordTimestamp(word="always", start=10.15, end=10.42),
                WordTimestamp(word="taken", start=10.44, end=10.66),
                WordTimestamp(word="care", start=10.68, end=10.88),
            ],
        )
        footage_index = FootageIndex(
            source_dir="/tmp",
            shots=[shot],
            total_duration=2.5,
            created_at="2026-04-13T00:00:00Z",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            index_path = Path(tmpdir) / "footage_index.json"
            ass_path = Path(tmpdir) / "captions.ass"
            index_path.write_text(
                footage_index.model_dump_json(indent=2),
                encoding="utf-8",
            )

            output = generate_ass_captions(
                footage_index_path=str(index_path),
                shot_id="/tmp/source.mov#10.0",
                clip_start=10.0,
                clip_end=11.0,
                output=str(ass_path),
            )

            self.assertEqual(output, str(ass_path))
            self.assertTrue(has_words_in_window(shot, 10.0, 11.0))

            contents = ass_path.read_text(encoding="utf-8")
            self.assertIn("Dialogue:", contents)
            self.assertIn(r"{\1c&H4ACFFF&}", contents)
            self.assertIn(r"{\1c&H4ACFFF&}I{\r} always taken care", contents)
            self.assertIn(r"I always taken {\1c&H4ACFFF&}care{\r}", contents)


if __name__ == "__main__":
    unittest.main()
