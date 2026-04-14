"use client";

import { useEffect } from "react";
import { useParams } from "next/navigation";
import { useProjectStore } from "@/stores/projectStore";
import { useMediaStore } from "@/stores/mediaStore";
import { useJobStore } from "@/stores/jobStore";
import { useTimelineStore } from "@/stores/timelineStore";
import * as api from "@/lib/api";
import { useJobStream } from "@/hooks/useJobStream";
import { Toolbar } from "@/components/Toolbar";
import { SourceMonitor } from "@/components/SourceMonitor";
import { ProgramMonitor } from "@/components/ProgramMonitor";
import { MediaBrowser } from "@/components/MediaBrowser";
import { Timeline } from "@/components/timeline/Timeline";
import { Inspector } from "@/components/inspector/Inspector";
import { Console } from "@/components/Console";
import { RunPipelineDialog } from "@/components/dialogs/RunPipelineDialog";
import { ChatPanel } from "@/components/ChatPanel";
import { useUiStore } from "@/stores/uiStore";

export default function EditorPage() {
  const params = useParams<{ projectId: string }>();
  const projectId = params.projectId;
  const { pollProject } = useProjectStore();
  const project = useProjectStore((s) => s.projects.find((p) => p.id === projectId));
  const consoleOpen = useUiStore((s) => s.consoleOpen);
  const chatOpen = useUiStore((s) => s.chatOpen);
  const currentJobId = useJobStore((s) => s.currentJobId);

  // Fetch project details, populate media store, and restore latest job.
  useEffect(() => {
    if (!projectId) return;
    pollProject(projectId).then((p) => {
      if (p.footage_index_path) {
        useMediaStore.getState().setFootageIndexPath(p.footage_index_path);
        useMediaStore.getState().fetchCatalog();
      }
    });

    // Restore the latest completed job if no active job.
    if (!useJobStore.getState().currentJobId) {
      api.getJobs().then((jobs) => {
        const completed = jobs.filter((j) => j.status === "completed");
        if (completed.length === 0) return;
        const latest = completed[completed.length - 1];
        useJobStore.getState().setCurrentJobId(latest.id);
        api.getJob(latest.id).then((job) => {
          if (job.result) {
            useJobStore.getState().setResult(job.result);
          }
          useTimelineStore.getState().fetchEditPlan(latest.id);
        });
      });
    }
  }, [projectId, pollProject]);

  // Stream pipeline progress.
  useJobStream(currentJobId);

  return (
    <div className="h-full flex flex-col">
      <Toolbar projectName={project?.name ?? "Loading..."} projectId={projectId} />

      {/* Top: monitors */}
      <div className="flex flex-1 min-h-0">
        <div className="w-[35%] min-w-[200px] border-r border-border">
          <SourceMonitor />
        </div>
        <div className="flex-1">
          <ProgramMonitor />
        </div>
      </div>

      {/* Bottom: workspace */}
      <div className="flex flex-[1.4] min-h-0">
        <div className="w-[220px] min-w-[180px] shrink-0">
          <MediaBrowser />
        </div>
        <div className="flex-1 min-w-0">
          <Timeline />
        </div>
        <div className="w-[260px] min-w-[200px] shrink-0">
          <Inspector projectId={projectId} />
        </div>
      </div>

      {/* Console */}
      {consoleOpen && (
        <div className="h-[160px] shrink-0">
          <Console />
        </div>
      )}

      <RunPipelineDialog projectId={projectId} />
      {chatOpen && <ChatPanel />}
    </div>
  );
}
