"use client";

import { useState, useEffect, useCallback } from "react";
import { useUiStore } from "@/stores/uiStore";
import { useJobStore } from "@/stores/jobStore";
import { useTimelineStore } from "@/stores/timelineStore";
import { useProjectStore } from "@/stores/projectStore";
import * as api from "@/lib/api";
import type { PipelineEntry } from "@/types/api";
import { X, Loader2, ChevronDown, ChevronRight } from "lucide-react";

interface RunPipelineDialogProps {
  projectId: string;
}

export function RunPipelineDialog({ projectId }: RunPipelineDialogProps) {
  const open = useUiStore((s) => s.runDialogOpen);
  const setOpen = useUiStore((s) => s.setRunDialogOpen);
  const submitJob = useJobStore((s) => s.submitJob);
  const fetchEditPlan = useTimelineStore((s) => s.fetchEditPlan);
  const project = useProjectStore((s) => s.projects.find((p) => p.id === projectId));

  const [pipelines, setPipelines] = useState<PipelineEntry[]>([]);
  const [pipelinePath, setPipelinePath] = useState("");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [product, setProduct] = useState("");
  const [audience, setAudience] = useState("");
  const [tone, setTone] = useState("energetic");
  const [duration, setDuration] = useState(30);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (open) {
      api.getPipelines().then((p) => {
        setPipelines(p);
        if (p.length > 0 && !pipelinePath) setPipelinePath(p[0].path);
      }).catch(() => {});
    }
  }, [open, pipelinePath]);

  const handleSubmit = useCallback(async () => {
    if (!project?.footage_index_path || !pipelinePath) return;
    setSubmitting(true);
    setError("");
    try {
      const jobId = await submitJob({
        brief: {
          product: product || project.name || "Product",
          audience: audience || "General",
          tone,
          duration_seconds: duration,
          style_ref: null,
        },
        footage_index_path: project.footage_index_path,
        pipeline_path: pipelinePath,
      });

      setOpen(false);
      useUiStore.getState().toggleConsole();

      // Poll for completion and load edit plan.
      const poll = setInterval(async () => {
        try {
          const job = await api.getJob(jobId);
          if (job.status === "completed") {
            clearInterval(poll);
            fetchEditPlan(jobId);
          } else if (job.status === "failed") {
            clearInterval(poll);
          }
        } catch {
          clearInterval(poll);
        }
      }, 3000);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  }, [product, audience, tone, duration, pipelinePath, project, submitJob, fetchEditPlan, setOpen]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div className="w-full max-w-sm bg-surface border border-border rounded-lg shadow-xl">
        <div className="flex items-center justify-between px-4 py-3 border-b border-border">
          <h2 className="text-sm font-semibold">Run Pipeline</h2>
          <button
            onClick={() => setOpen(false)}
            className="p-1 rounded hover:bg-surface-hover text-muted"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-4 space-y-3 text-xs">
          {/* Pipeline selector */}
          {pipelines.length > 1 && (
            <label className="block">
              <span className="text-muted font-medium">Pipeline</span>
              <select
                value={pipelinePath}
                onChange={(e) => setPipelinePath(e.target.value)}
                className="w-full mt-1 px-2 py-1.5 rounded border border-border bg-background text-foreground focus:outline-none focus:border-accent"
              >
                {pipelines.map((p) => (
                  <option key={p.path} value={p.path}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>
          )}

          {/* Footage info */}
          <div className="text-[11px] text-muted bg-background/50 rounded p-2.5">
            <span className="font-medium text-foreground">{project?.name ?? "Project"}</span>
            <span className="ml-2 text-muted">
              {project?.shot_count ?? 0} shots
              {pipelines.length === 1 && (
                <span> -- {pipelines[0].name} pipeline</span>
              )}
            </span>
          </div>

          {/* Advanced toggle */}
          <button
            onClick={() => setShowAdvanced(!showAdvanced)}
            className="flex items-center gap-1 text-muted hover:text-foreground transition-colors"
          >
            {showAdvanced ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
            <span className="text-[11px]">Advanced options</span>
          </button>

          {showAdvanced && (
            <div className="space-y-3 pl-1 border-l-2 border-border ml-1">
              <label className="block pl-2">
                <span className="text-muted font-medium">Product</span>
                <input
                  value={product}
                  onChange={(e) => setProduct(e.target.value)}
                  placeholder={project?.name || "Auto-detected from project"}
                  className="w-full mt-1 px-2 py-1.5 rounded border border-border bg-background text-foreground focus:outline-none focus:border-accent"
                />
              </label>

              <label className="block pl-2">
                <span className="text-muted font-medium">Audience</span>
                <input
                  value={audience}
                  onChange={(e) => setAudience(e.target.value)}
                  placeholder="General"
                  className="w-full mt-1 px-2 py-1.5 rounded border border-border bg-background text-foreground focus:outline-none focus:border-accent"
                />
              </label>

              <div className="flex gap-3 pl-2">
                <label className="flex-1 block">
                  <span className="text-muted font-medium">Tone</span>
                  <select
                    value={tone}
                    onChange={(e) => setTone(e.target.value)}
                    className="w-full mt-1 px-2 py-1.5 rounded border border-border bg-background text-foreground focus:outline-none focus:border-accent"
                  >
                    <option value="energetic">Energetic</option>
                    <option value="calm">Calm</option>
                    <option value="professional">Professional</option>
                    <option value="playful">Playful</option>
                    <option value="dramatic">Dramatic</option>
                  </select>
                </label>

                <label className="flex-1 block">
                  <span className="text-muted font-medium">Duration (s)</span>
                  <input
                    type="number"
                    min={5}
                    max={300}
                    value={duration}
                    onChange={(e) => setDuration(parseInt(e.target.value) || 30)}
                    className="w-full mt-1 px-2 py-1.5 rounded border border-border bg-background text-foreground focus:outline-none focus:border-accent"
                  />
                </label>
              </div>
            </div>
          )}

          {error && (
            <p className="text-destructive text-[10px]">{error}</p>
          )}
        </div>

        <div className="flex justify-end gap-2 px-4 py-3 border-t border-border">
          <button
            onClick={() => setOpen(false)}
            className="px-3 py-1.5 rounded border border-border text-xs hover:bg-surface-hover transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={submitting || !pipelinePath}
            className="flex items-center gap-1.5 px-4 py-1.5 rounded bg-accent hover:bg-accent-hover text-black text-xs font-medium disabled:opacity-50 transition-colors"
          >
            {submitting && <Loader2 className="w-3 h-3 animate-spin" />}
            Run
          </button>
        </div>
      </div>
    </div>
  );
}
