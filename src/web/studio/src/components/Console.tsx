"use client";

import { useEffect, useRef } from "react";
import { useJobStore } from "@/stores/jobStore";
import { Terminal, AlertCircle } from "lucide-react";

export function Console() {
  const progressLines = useJobStore((s) => s.progressLines);
  const pipelineStatus = useJobStore((s) => s.pipelineStatus);
  const error = useJobStore((s) => s.error);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [progressLines.length]);

  return (
    <div className="h-full flex flex-col bg-background border-t border-border">
      <div className="px-3 py-1 text-xs font-medium text-muted border-b border-border flex items-center gap-1.5 shrink-0">
        <Terminal className="w-3 h-3" />
        Console
        {pipelineStatus === "running" && (
          <span className="ml-auto text-amber-400 text-[10px]">Running...</span>
        )}
        {pipelineStatus === "completed" && (
          <span className="ml-auto text-accent text-[10px]">Done</span>
        )}
        {pipelineStatus === "failed" && (
          <span className="ml-auto text-destructive text-[10px]">Failed</span>
        )}
      </div>

      <div
        ref={scrollRef}
        className="flex-1 overflow-y-auto font-mono text-[11px] p-2 space-y-0.5"
      >
        {progressLines.length === 0 && !error && (
          <p className="text-muted">Waiting for pipeline output...</p>
        )}

        {progressLines.map((entry, i) => (
          <div key={i} className="flex gap-2">
            <span className="text-muted/50 shrink-0 select-none">
              {new Date(entry.timestamp).toLocaleTimeString()}
            </span>
            <span className="text-foreground/80 whitespace-pre-wrap break-all">
              {entry.line}
            </span>
          </div>
        ))}

        {error && (
          <div className="flex items-start gap-1.5 text-destructive mt-1">
            <AlertCircle className="w-3 h-3 mt-0.5 shrink-0" />
            <span>{error}</span>
          </div>
        )}
      </div>
    </div>
  );
}
