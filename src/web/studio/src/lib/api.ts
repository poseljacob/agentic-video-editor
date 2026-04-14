/** Typed fetch wrapper for the AVE Studio FastAPI backend. */

import type {
  CreateJobRequest,
  CreateJobResponse,
  EnrichedEditPlan,
  FeedbackResponse,
  FootageIndexEntry,
  Job,
  JobReview,
  JobSummary,
  PipelineEntry,
  Project,
  ShotSearchResult,
  StyleEntry,
} from "@/types/api";
import type { EditPlanEntry } from "@/types/schemas";

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, init);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch { /* non-JSON */ }
    throw new Error(detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

// --- Browse ---

export interface BrowseEntry {
  name: string;
  path: string;
  type: "dir" | "file";
}

export interface BrowseResult {
  current: string;
  parent: string | null;
  dirs: BrowseEntry[];
  files: BrowseEntry[];
  video_count: number;
}

export const browseDirectory = (path: string) =>
  request<BrowseResult>(`/api/browse?path=${encodeURIComponent(path)}`);

// --- Projects ---

export const getProjects = () => request<Project[]>("/api/projects");

export const getProject = (id: string) => request<Project>(`/api/projects/${id}`);

export const createProject = (name: string, footage_dir: string) =>
  request<{ id: string; name: string; status: string }>("/api/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, footage_dir }),
  });

export const deleteProject = (id: string) =>
  request<void>(`/api/projects/${id}`, { method: "DELETE" });

// --- Config ---

export const getStyles = () => request<StyleEntry[]>("/api/styles");
export const getPipelines = () => request<PipelineEntry[]>("/api/pipelines");
export const getFootageIndexes = () => request<FootageIndexEntry[]>("/api/footage-indexes");

// --- Jobs ---

export const getJobs = () => request<JobSummary[]>("/api/jobs");

export const getJob = (id: string) => request<Job>(`/api/jobs/${id}`);

export const createJob = (body: CreateJobRequest) =>
  request<CreateJobResponse>("/api/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

export const getJobReview = (id: string) =>
  request<JobReview>(`/api/jobs/${id}/review`);

// --- Edit plan ---

export const getEditPlan = (jobId: string) =>
  request<EnrichedEditPlan>(`/api/jobs/${jobId}/edit-plan`);

export const updateEditPlan = (
  jobId: string,
  body: { brief: object; music_path: string | null; total_duration: number; entries: EditPlanEntry[] },
) =>
  request<{ edit_plan: object }>(`/api/jobs/${jobId}/edit-plan`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

// --- Footage ---

export const searchFootage = (query: string, footageIndexPath: string) =>
  request<{ query: string; count: number; results: ShotSearchResult[] }>(
    `/api/footage/search?query=${encodeURIComponent(query)}&footage_index_path=${encodeURIComponent(footageIndexPath)}`,
  );

export const getCatalog = (footageIndexPath: string) =>
  request<{ count: number; results: ShotSearchResult[] }>(
    `/api/footage/catalog?footage_index_path=${encodeURIComponent(footageIndexPath)}`,
  );

// --- Feedback ---

export const postFeedback = (jobId: string, message: string) =>
  request<FeedbackResponse>(`/api/jobs/${jobId}/feedback`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });

// --- Re-render ---

export const postReRender = (jobId: string, editPlanBody: object) =>
  request<{ job_id: string; status: string; parent_job_id: string }>(
    `/api/jobs/${jobId}/re-render`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(editPlanBody),
    },
  );

export const postReviewOnly = (jobId: string) =>
  request<{ job_id: string; status: string; parent_job_id: string }>(
    `/api/jobs/${jobId}/review-only`,
    { method: "POST" },
  );
