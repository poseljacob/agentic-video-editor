"use client";

import { useCallback, useRef, useState, useEffect } from "react";
import {
  DndContext,
  closestCenter,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
  DragOverlay,
} from "@dnd-kit/core";
import {
  SortableContext,
  horizontalListSortingStrategy,
} from "@dnd-kit/sortable";
import { useTimelineStore } from "@/stores/timelineStore";
import { useUiStore } from "@/stores/uiStore";
import { TimelineClip } from "./TimelineClip";
import { toMediaUrl } from "@/lib/media";
import { Layers, ZoomIn, ZoomOut, Plus } from "lucide-react";
import type { EnrichedEditPlanEntry } from "@/types/api";

const MIN_PX_PER_SEC = 30;
const MAX_PX_PER_SEC = 300;
const DEFAULT_PX_PER_SEC = 80;

export function Timeline() {
  const plan = useTimelineStore((s) => s.plan);
  const selectedIndex = useTimelineStore((s) => s.selectedIndex);
  const selectEntry = useTimelineStore((s) => s.selectEntry);
  const reorderEntries = useTimelineStore((s) => s.reorderEntries);
  const addEntry = useTimelineStore((s) => s.addEntry);
  const setSourceMonitorSrc = useUiStore((s) => s.setSourceMonitorSrc);

  const [pxPerSecond, setPxPerSecond] = useState(DEFAULT_PX_PER_SEC);
  const [dragOverTimeline, setDragOverTimeline] = useState(false);
  const trackRef = useRef<HTMLDivElement>(null);

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } }),
  );

  const handleDragEnd = useCallback(
    (event: DragEndEvent) => {
      const { active, over } = event;
      if (over && active.id !== over.id) {
        const fromIndex = Number(active.id);
        const toIndex = Number(over.id);
        reorderEntries(fromIndex, toIndex);
      }
    },
    [reorderEntries],
  );

  // Handle drops from media browser (via native drag/drop)
  const handleExternalDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setDragOverTimeline(false);
      const raw = e.dataTransfer.getData("application/json");
      if (!raw) return;
      try {
        const shot = JSON.parse(raw) as {
          shot_id: string;
          source_file: string;
          source_filename: string;
          start_time: number;
          end_time: number;
          duration: number;
          description: string;
          roll_type: string;
          display_label: string;
        };
        const entry: EnrichedEditPlanEntry = {
          position: 0,
          shot_id: shot.shot_id,
          source_file: shot.source_file,
          source_filename: shot.source_filename,
          source_timestamp: shot.start_time,
          display_label: shot.display_label,
          start_trim: shot.start_time,
          end_trim: shot.end_time,
          duration: shot.duration,
          text_overlay: null,
          transition: null,
          roll_type: shot.roll_type,
          thumbnail_url: "",
        };
        addEntry(entry);
      } catch {
        // ignore malformed data
      }
    },
    [addEntry],
  );

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
    setDragOverTimeline(true);
  }, []);

  const handleDragLeave = useCallback(() => {
    setDragOverTimeline(false);
  }, []);

  // Zoom with scroll wheel (Cmd/Ctrl + scroll)
  useEffect(() => {
    const el = trackRef.current;
    if (!el) return;
    const handler = (e: WheelEvent) => {
      if (e.metaKey || e.ctrlKey) {
        e.preventDefault();
        setPxPerSecond((prev) =>
          Math.min(MAX_PX_PER_SEC, Math.max(MIN_PX_PER_SEC, prev - e.deltaY * 0.5)),
        );
      }
    };
    el.addEventListener("wheel", handler, { passive: false });
    return () => el.removeEventListener("wheel", handler);
  }, []);

  const entries = plan?.entries ?? [];
  const totalDuration = plan?.total_duration ?? 0;
  const rulerSeconds = Math.ceil(totalDuration) + 2;
  // Tick interval adapts to zoom level
  const tickInterval = pxPerSecond >= 100 ? 1 : pxPerSecond >= 50 ? 2 : 5;

  return (
    <div className="h-full flex flex-col bg-background">
      {/* Header */}
      <div className="px-3 py-1.5 text-xs font-medium text-muted border-b border-border flex items-center gap-1.5">
        <Layers className="w-3 h-3" />
        Timeline
        {plan && (
          <span className="ml-2 text-[10px]">
            {entries.length} clips -- {totalDuration.toFixed(1)}s
          </span>
        )}

        {/* Zoom controls */}
        <div className="ml-auto flex items-center gap-1">
          <button
            onClick={() => setPxPerSecond((p) => Math.max(MIN_PX_PER_SEC, p - 20))}
            className="p-0.5 hover:text-foreground transition-colors"
            title="Zoom out"
          >
            <ZoomOut className="w-3.5 h-3.5" />
          </button>
          <div className="w-16 h-1 bg-border rounded-full relative">
            <div
              className="absolute top-0 left-0 h-full bg-accent/60 rounded-full"
              style={{ width: `${((pxPerSecond - MIN_PX_PER_SEC) / (MAX_PX_PER_SEC - MIN_PX_PER_SEC)) * 100}%` }}
            />
          </div>
          <button
            onClick={() => setPxPerSecond((p) => Math.min(MAX_PX_PER_SEC, p + 20))}
            className="p-0.5 hover:text-foreground transition-colors"
            title="Zoom in"
          >
            <ZoomIn className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      {/* Ruler */}
      {entries.length > 0 && (
        <div className="h-6 border-b border-border bg-surface relative overflow-hidden shrink-0" style={{ minWidth: `${rulerSeconds * pxPerSecond}px` }}>
          {Array.from({ length: Math.floor(rulerSeconds / tickInterval) + 1 }, (_, i) => {
            const sec = i * tickInterval;
            const left = sec * pxPerSecond;
            const isMajor = sec % 5 === 0;
            return (
              <div key={sec} className="absolute top-0 bottom-0" style={{ left: `${left}px` }}>
                <div className={`absolute bottom-0 w-px ${isMajor ? "h-3 bg-muted/60" : "h-1.5 bg-muted/30"}`} />
                {isMajor && (
                  <span className="absolute top-0.5 left-1 text-[9px] text-muted whitespace-nowrap">
                    {sec}s
                  </span>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Track area */}
      <div
        ref={trackRef}
        className={`flex-1 overflow-x-auto overflow-y-hidden transition-colors ${
          dragOverTimeline ? "bg-accent/5 ring-1 ring-inset ring-accent/30" : ""
        }`}
        onDrop={handleExternalDrop}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
      >
        {entries.length > 0 ? (
          <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
            <SortableContext items={entries.map((_, i) => i)} strategy={horizontalListSortingStrategy}>
              <div className="flex items-stretch h-full p-2 gap-0.5 min-w-max">
                {entries.map((entry, i) => (
                  <TimelineClip
                    key={`${entry.shot_id}-${i}`}
                    entry={entry}
                    index={i}
                    isSelected={selectedIndex === i}
                    pxPerSecond={pxPerSecond}
                    onClick={() => {
                      selectEntry(i);
                      if (entry.source_file) {
                        setSourceMonitorSrc(toMediaUrl(entry.source_file));
                      }
                    }}
                  />
                ))}
              </div>
            </SortableContext>
          </DndContext>
        ) : (
          <div className="h-full flex items-center justify-center">
            {dragOverTimeline ? (
              <div className="flex flex-col items-center gap-2 text-accent">
                <Plus className="w-6 h-6" />
                <p className="text-xs font-medium">Drop clip here</p>
              </div>
            ) : (
              <p className="text-muted text-xs">
                {plan
                  ? "Drag clips from the media browser to add them"
                  : "Run a pipeline to populate the timeline"}
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
