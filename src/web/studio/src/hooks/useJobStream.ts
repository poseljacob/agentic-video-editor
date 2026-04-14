"use client";

import { useEffect, useRef } from "react";
import { useJobStore } from "@/stores/jobStore";
import type { WsMessage } from "@/types/api";

const WS_BASE = process.env.NEXT_PUBLIC_WS_URL ?? "ws://localhost:8000";

/**
 * Opens a WebSocket to /ws/jobs/{jobId} and dispatches events into
 * the job store. Cleans up on unmount or when jobId changes.
 */
export function useJobStream(jobId: string | null) {
  const wsRef = useRef<WebSocket | null>(null);
  const { appendProgress, setStatus, setResult, setFailed } = useJobStore();

  useEffect(() => {
    if (!jobId) return;

    const ws = new WebSocket(`${WS_BASE}/ws/jobs/${jobId}`);
    wsRef.current = ws;

    ws.onopen = () => {
      useJobStore.getState().setStatus("running");
    };

    ws.onmessage = (event) => {
      try {
        const msg: WsMessage = JSON.parse(event.data);
        switch (msg.type) {
          case "progress":
            appendProgress(msg.line, msg.timestamp);
            break;
          case "status":
            if (msg.status === "failed") {
              setFailed(msg.error ?? "Unknown error");
            }
            break;
          case "result":
            setResult(msg.data);
            break;
        }
      } catch {
        // Ignore malformed messages.
      }
    };

    ws.onerror = () => {
      setFailed("WebSocket connection error");
    };

    ws.onclose = (event) => {
      wsRef.current = null;
      // 4004 = unknown job
      if (event.code === 4004) {
        setFailed("Job not found");
      }
    };

    return () => {
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, [jobId, appendProgress, setStatus, setResult, setFailed]);
}
