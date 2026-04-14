"use client";

import { useRouter } from "next/navigation";
import {
  ArrowLeft,
  Play,
  RotateCcw,
  Star,
  Terminal,
  MessageSquare,
  Save,
  Loader2,
} from "lucide-react";
import { useUiStore } from "@/stores/uiStore";
import { useJobStore } from "@/stores/jobStore";
import { useTimelineStore } from "@/stores/timelineStore";
import * as api from "@/lib/api";

interface ToolbarProps {
  projectName: string;
  projectId: string;
}

export function Toolbar({ projectName, projectId }: ToolbarProps) {
  const router = useRouter();
  const setRunDialogOpen = useUiStore((s) => s.setRunDialogOpen);
  const toggleConsole = useUiStore((s) => s.toggleConsole);
  const toggleChat = useUiStore((s) => s.toggleChat);
  const consoleOpen = useUiStore((s) => s.consoleOpen);
  const chatOpen = useUiStore((s) => s.chatOpen);

  const pipelineStatus = useJobStore((s) => s.pipelineStatus);
  const currentJobId = useJobStore((s) => s.currentJobId);
  const isDirty = useTimelineStore((s) => s.isDirty);
  const saving = useTimelineStore((s) => s.saving);
  const saveFlash = useTimelineStore((s) => s.saveFlash);
  const savePlan = useTimelineStore((s) => s.savePlan);

  const isRunning = pipelineStatus === "running" || pipelineStatus === "connecting";

  const statusLabel = () => {
    switch (pipelineStatus) {
      case "running":
      case "connecting":
        return "Running";
      case "completed":
        return "Done";
      case "failed":
        return "Failed";
      default:
        return "Idle";
    }
  };

  const statusColor = () => {
    switch (pipelineStatus) {
      case "running":
      case "connecting":
        return "text-amber-400";
      case "completed":
        return "text-accent";
      case "failed":
        return "text-destructive";
      default:
        return "text-muted";
    }
  };

  return (
    <div className="flex items-center gap-2 px-3 py-2 border-b border-border bg-surface shrink-0">
      <button
        onClick={() => router.push("/")}
        className="p-1.5 rounded hover:bg-surface-hover text-muted hover:text-foreground transition-colors"
        title="Back to projects"
      >
        <ArrowLeft className="w-4 h-4" />
      </button>

      <span className="font-semibold text-sm truncate max-w-48">{projectName}</span>

      <div className={`text-xs font-medium ml-2 ${statusColor()}`}>
        {isRunning && <Loader2 className="w-3 h-3 animate-spin inline mr-1" />}
        {statusLabel()}
      </div>

      <div className="flex-1" />

      {/* Actions */}
      <button
        onClick={() => setRunDialogOpen(true)}
        disabled={isRunning}
        className="flex items-center gap-1.5 px-3 py-1.5 rounded bg-accent hover:bg-accent-hover text-black text-xs font-medium disabled:opacity-50 transition-colors"
      >
        <Play className="w-3 h-3" />
        Run Pipeline
      </button>

      {currentJobId && (
        <button
          onClick={async () => {
            if (!currentJobId) return;
            const plan = useTimelineStore.getState().plan;
            if (!plan) return;
            try {
              const res = await api.postReRender(currentJobId, {
                entries: plan.entries.map((e) => ({
                  shot_id: e.shot_id,
                  start_trim: e.start_trim,
                  end_trim: e.end_trim,
                  position: e.position,
                  text_overlay: e.text_overlay,
                  transition: e.transition,
                })),
              });
              useJobStore.getState().setCurrentJobId(res.job_id);
              useJobStore.getState().setStatus("connecting");
              useUiStore.getState().toggleConsole();
            } catch { /* handled by store */ }
          }}
          disabled={!isDirty || isRunning}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded border border-border text-xs font-medium hover:bg-surface-hover disabled:opacity-40 transition-colors"
        >
          <RotateCcw className="w-3 h-3" />
          Re-render
        </button>
      )}

      {currentJobId && (
        <button
          onClick={async () => {
            if (!currentJobId) return;
            try {
              const res = await api.postReviewOnly(currentJobId);
              useJobStore.getState().setCurrentJobId(res.job_id);
              useJobStore.getState().setStatus("connecting");
            } catch { /* handled by store */ }
          }}
          disabled={isRunning}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded border border-border text-xs font-medium hover:bg-surface-hover disabled:opacity-40 transition-colors"
        >
          <Star className="w-3 h-3" />
          Review
        </button>
      )}

      {isDirty && currentJobId && (
        <button
          onClick={() => savePlan(currentJobId)}
          disabled={saving}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded border border-accent/50 text-accent text-xs font-medium hover:bg-accent/10 disabled:opacity-50 transition-colors"
        >
          {saving ? <Loader2 className="w-3 h-3 animate-spin" /> : <Save className="w-3 h-3" />}
          Save
        </button>
      )}

      {saveFlash && (
        <span className="text-xs text-accent">{saveFlash}</span>
      )}

      <div className="w-px h-5 bg-border mx-1" />

      <button
        onClick={toggleConsole}
        className={`p-1.5 rounded transition-colors ${consoleOpen ? "bg-surface-hover text-accent" : "text-muted hover:text-foreground hover:bg-surface-hover"}`}
        title="Toggle console"
      >
        <Terminal className="w-4 h-4" />
      </button>

      <button
        onClick={toggleChat}
        className={`p-1.5 rounded transition-colors ${chatOpen ? "bg-surface-hover text-accent" : "text-muted hover:text-foreground hover:bg-surface-hover"}`}
        title="Toggle chat"
      >
        <MessageSquare className="w-4 h-4" />
      </button>
    </div>
  );
}
