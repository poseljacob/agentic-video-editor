import { create } from "zustand";
import type { ReviewScore } from "@/types/schemas";
import * as api from "@/lib/api";
import { toMediaUrl } from "@/lib/media";
import type { CreateJobRequest } from "@/types/api";

interface ProgressEntry {
  line: string;
  timestamp: string;
}

interface JobState {
  currentJobId: string | null;
  pipelineStatus: "idle" | "connecting" | "running" | "completed" | "failed";
  progressLines: ProgressEntry[];
  result: Record<string, unknown> | null;
  review: ReviewScore | null;
  error: string | null;
  finalVideoPath: string | null;

  submitJob: (body: CreateJobRequest) => Promise<string>;
  setCurrentJobId: (id: string | null) => void;
  appendProgress: (line: string, timestamp: string) => void;
  setStatus: (status: JobState["pipelineStatus"]) => void;
  setResult: (data: Record<string, unknown>) => void;
  setFailed: (error: string) => void;
  reset: () => void;
  fetchReview: (jobId: string) => Promise<void>;
}

export const useJobStore = create<JobState>((set) => ({
  currentJobId: null,
  pipelineStatus: "idle",
  progressLines: [],
  result: null,
  review: null,
  error: null,
  finalVideoPath: null,

  submitJob: async (body) => {
    const res = await api.createJob(body);
    set({
      currentJobId: res.job_id,
      pipelineStatus: "connecting",
      progressLines: [],
      result: null,
      review: null,
      error: null,
      finalVideoPath: null,
    });
    return res.job_id;
  },

  setCurrentJobId: (id) => set({ currentJobId: id }),

  appendProgress: (line, timestamp) =>
    set((s) => ({ progressLines: [...s.progressLines, { line, timestamp }] })),

  setStatus: (status) => set({ pipelineStatus: status }),

  setResult: (data) => {
    const videoPath = (data.final_video_path as string) || null;
    const review = (data.review as ReviewScore) || null;
    set({
      pipelineStatus: "completed",
      result: data,
      review,
      finalVideoPath: videoPath ? toMediaUrl(videoPath) : null,
    });
  },

  setFailed: (error) => set({ pipelineStatus: "failed", error }),

  reset: () =>
    set({
      currentJobId: null,
      pipelineStatus: "idle",
      progressLines: [],
      result: null,
      review: null,
      error: null,
      finalVideoPath: null,
    }),

  fetchReview: async (jobId) => {
    try {
      const data = await api.getJobReview(jobId);
      if (data.review) set({ review: data.review });
    } catch { /* ignore */ }
  },
}));
