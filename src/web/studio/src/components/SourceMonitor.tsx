"use client";

import { useRef, useState } from "react";
import { useUiStore } from "@/stores/uiStore";
import { Monitor } from "lucide-react";

export function SourceMonitor() {
  const src = useUiStore((s) => s.sourceMonitorSrc);
  const videoRef = useRef<HTMLVideoElement>(null);
  const [error, setError] = useState(false);

  return (
    <div className="h-full flex flex-col bg-background border-r border-border">
      <div className="px-3 py-1.5 text-xs font-medium text-muted border-b border-border flex items-center gap-1.5">
        <Monitor className="w-3 h-3" />
        Source
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
          <p className="text-muted text-xs">Select a clip to preview</p>
        )}
      </div>
    </div>
  );
}
