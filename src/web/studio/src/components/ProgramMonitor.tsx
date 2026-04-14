"use client";

import { useRef, useState, useEffect } from "react";
import { useUiStore } from "@/stores/uiStore";
import { useJobStore } from "@/stores/jobStore";
import { Tv } from "lucide-react";

export function ProgramMonitor() {
  const programSrc = useUiStore((s) => s.programMonitorSrc);
  const finalVideoPath = useJobStore((s) => s.finalVideoPath);
  const setProgramMonitorSrc = useUiStore((s) => s.setProgramMonitorSrc);
  const videoRef = useRef<HTMLVideoElement>(null);
  const [error, setError] = useState(false);

  // Auto-load the latest render.
  useEffect(() => {
    if (finalVideoPath) {
      setProgramMonitorSrc(finalVideoPath);
      setError(false);
    }
  }, [finalVideoPath, setProgramMonitorSrc]);

  const src = programSrc;

  return (
    <div className="h-full flex flex-col bg-background">
      <div className="px-3 py-1.5 text-xs font-medium text-muted border-b border-border flex items-center gap-1.5">
        <Tv className="w-3 h-3" />
        Program
      </div>
      <div className="flex-1 flex items-center justify-center bg-black/40 overflow-hidden">
        {src && !error ? (
          <video
            ref={videoRef}
            src={src}
            controls
            className="w-full h-full object-contain"
            onError={() => setError(true)}
          />
        ) : (
          <p className="text-muted text-xs">Run a pipeline to see the output</p>
        )}
      </div>
    </div>
  );
}
