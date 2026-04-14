"use client";

import { useProjectStore } from "@/stores/projectStore";
import { useJobStore } from "@/stores/jobStore";
import { ReviewRadar } from "./ReviewRadar";
import { Folder, Clock, Film } from "lucide-react";

interface ProjectPanelProps {
  projectId: string;
}

export function ProjectPanel({ projectId }: ProjectPanelProps) {
  const project = useProjectStore((s) => s.projects.find((p) => p.id === projectId));
  const review = useJobStore((s) => s.review);
  const result = useJobStore((s) => s.result);

  if (!project) {
    return (
      <div className="p-3 text-xs text-muted">Loading project...</div>
    );
  }

  return (
    <div className="p-3 space-y-4 text-xs">
      {/* Project info */}
      <div>
        <h3 className="font-medium text-foreground mb-2">{project.name}</h3>
        <div className="space-y-1.5 text-muted">
          <div className="flex items-center gap-1.5">
            <Folder className="w-3 h-3" />
            <span className="break-all text-[10px]">{project.footage_dir}</span>
          </div>
          <div className="flex items-center gap-1.5">
            <Film className="w-3 h-3" />
            <span>{project.shot_count} shots</span>
          </div>
          <div className="flex items-center gap-1.5">
            <Clock className="w-3 h-3" />
            <span>{project.total_duration.toFixed(1)}s total</span>
          </div>
        </div>
      </div>

      {/* Review radar */}
      {review && (
        <div className="border-t border-border pt-3">
          <h4 className="font-medium text-foreground mb-2">Review Scores</h4>
          <ReviewRadar review={review} />
          <p className="text-muted text-[10px] mt-2">Overall: {review.overall.toFixed(2)}</p>
          {review.feedback && (
            <p className="text-muted text-[10px] mt-1 italic">{review.feedback}</p>
          )}
        </div>
      )}

      {/* Result info */}
      {result && (
        <div className="border-t border-border pt-3">
          <h4 className="font-medium text-foreground mb-1">Last Result</h4>
          <div className="text-[10px] text-muted space-y-0.5">
            {typeof result.final_video_path === "string" && (
              <p>Video: {result.final_video_path.split("/").pop()}</p>
            )}
            {typeof result.edit_plan_path === "string" && (
              <p>Plan: {result.edit_plan_path.split("/").pop()}</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
