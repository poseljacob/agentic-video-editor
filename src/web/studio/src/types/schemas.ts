/** Mirrors src/models/schemas.py exactly. */

export interface CreativeBrief {
  product: string;
  audience: string;
  tone: string;
  duration_seconds: number;
  style_ref: string | null;
}

export interface WordTimestamp {
  word: string;
  start: number;
  end: number;
}

export interface Shot {
  source_file: string;
  start_time: number;
  end_time: number;
  description: string;
  energy_level: number;
  relevance_score: number;
  transcript: string;
  words: WordTimestamp[];
  roll_type: string;
}

export interface FootageIndex {
  source_dir: string;
  shots: Shot[];
  total_duration: number;
  created_at: string;
}

export interface EditPlanEntry {
  shot_id: string;
  start_trim: number;
  end_trim: number;
  position: number;
  text_overlay: string | null;
  transition: string | null;
}

export interface EditPlan {
  brief: CreativeBrief;
  entries: EditPlanEntry[];
  music_path: string | null;
  total_duration: number;
}

export interface ReviewScore {
  adherence: number;
  pacing: number;
  visual_quality: number;
  watchability: number;
  overall: number;
  feedback: string;
}
