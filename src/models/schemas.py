from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class CreativeBrief(BaseModel):
    product: str
    audience: str
    tone: str
    duration_seconds: int
    style_ref: str | None = None


class WordTimestamp(BaseModel):
    word: str
    start: float
    end: float


class Shot(BaseModel):
    source_file: str
    start_time: float
    end_time: float
    description: str
    energy_level: int
    relevance_score: float
    transcript: str
    words: list[WordTimestamp] = Field(default_factory=list)
    roll_type: str = Field(
        default="unknown",
        description=(
            "Footage category: 'a-roll' (on-camera talent / talking head), "
            "'b-roll' (cutaway / product / texture / environment), or "
            "'unknown'. Auto-detected from the source directory name during "
            "preprocessing."
        ),
    )


class FootageIndex(BaseModel):
    source_dir: str
    shots: list[Shot]
    total_duration: float
    created_at: datetime


class EditPlanEntry(BaseModel):
    shot_id: str
    start_trim: float
    end_trim: float
    position: int
    text_overlay: str | None = None
    transition: str | None = None


class EditPlan(BaseModel):
    brief: CreativeBrief
    entries: list[EditPlanEntry]
    music_path: str | None = None
    total_duration: float


class ReviewScore(BaseModel):
    adherence: float
    pacing: float
    visual_quality: float
    watchability: float
    overall: float
    feedback: str
