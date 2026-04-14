/** API-specific types for requests, responses, and WebSocket messages. */

import type { CreativeBrief, ReviewScore } from "./schemas";

// --- Projects ---

export interface Project {
  id: string;
  name: string;
  footage_dir: string;
  footage_index_path: string;
  status: "preprocessing" | "ready" | "failed";
  shot_count: number;
  total_duration: number;
  created_at: string;
  error: string | null;
}

// --- Jobs ---

export interface JobSummary {
  id: string;
  status: "pending" | "running" | "completed" | "failed";
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  brief_product: string;
  progress_lines: number;
}

export interface Job {
  id: string;
  status: "pending" | "running" | "completed" | "failed";
  brief: CreativeBrief;
  footage_index_path: string;
  pipeline_path: string;
  job_type: string;
  parent_job_id: string | null;
  feedback_history: string[];
  progress_log: string[];
  result: Record<string, unknown> | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
}

export interface CreateJobRequest {
  brief: CreativeBrief;
  footage_index_path: string;
  pipeline_path: string;
}

export interface CreateJobResponse {
  job_id: string;
  status: string;
}

// --- Config ---

export interface StyleEntry {
  name: string;
  path: string;
}

export interface PipelineEntry {
  name: string;
  path: string;
}

export interface FootageIndexEntry {
  name: string;
  path: string;
  shot_count: number;
  created_at: string;
}

// --- Footage search ---

export interface ShotSearchResult {
  shot_id: string;
  source_file: string;
  source_filename: string;
  start_time: number;
  end_time: number;
  duration: number;
  description: string;
  transcript: string;
  roll_type: string;
  display_label: string;
  relevance_score: number;
}

// --- Edit plan (enriched from GET /api/jobs/{id}/edit-plan) ---

export interface EnrichedEditPlanEntry {
  position: number;
  shot_id: string;
  source_file: string;
  source_filename: string;
  source_timestamp: number;
  display_label: string;
  start_trim: number;
  end_trim: number;
  duration: number;
  text_overlay: string | null;
  transition: string | null;
  roll_type: string;
  thumbnail_url: string;
}

export interface EnrichedEditPlan {
  job_id: string;
  total_duration: number;
  entry_count: number;
  entries: EnrichedEditPlanEntry[];
}

// --- WebSocket messages ---

export interface WsProgressMsg {
  type: "progress";
  line: string;
  timestamp: string;
}

export interface WsStatusMsg {
  type: "status";
  status: "completed" | "failed";
  error?: string;
}

export interface WsResultMsg {
  type: "result";
  data: Record<string, unknown>;
}

export type WsMessage = WsProgressMsg | WsStatusMsg | WsResultMsg;

// --- Feedback ---

export interface FeedbackResponse {
  job_id: string;
  status: string;
  parent_job_id: string;
}

// --- Review ---

export interface JobReview {
  review: ReviewScore | null;
  retries_used: number;
  feedback_history: string[];
}
