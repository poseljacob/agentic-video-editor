"use client";

import { useTimelineStore } from "@/stores/timelineStore";
import { useJobStore } from "@/stores/jobStore";
import { ClipPanel } from "./ClipPanel";
import { ProjectPanel } from "./ProjectPanel";
import { Settings2 } from "lucide-react";

interface InspectorProps {
  projectId: string;
}

export function Inspector({ projectId }: InspectorProps) {
  const selectedIndex = useTimelineStore((s) => s.selectedIndex);
  const plan = useTimelineStore((s) => s.plan);
  const currentJobId = useJobStore((s) => s.currentJobId);

  const selectedEntry =
    selectedIndex !== null && plan ? plan.entries[selectedIndex] ?? null : null;

  return (
    <div className="h-full flex flex-col bg-background border-l border-border">
      <div className="px-3 py-1.5 text-xs font-medium text-muted border-b border-border flex items-center gap-1.5">
        <Settings2 className="w-3 h-3" />
        {selectedEntry ? "Clip" : "Project"}
      </div>

      <div className="flex-1 overflow-y-auto">
        {selectedEntry && selectedIndex !== null ? (
          <ClipPanel entry={selectedEntry} index={selectedIndex} jobId={currentJobId} />
        ) : (
          <ProjectPanel projectId={projectId} />
        )}
      </div>
    </div>
  );
}
