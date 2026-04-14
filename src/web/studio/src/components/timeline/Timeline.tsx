"use client";

import { useCallback } from "react";
import {
  DndContext,
  closestCenter,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  horizontalListSortingStrategy,
} from "@dnd-kit/sortable";
import { useTimelineStore } from "@/stores/timelineStore";
import { useUiStore } from "@/stores/uiStore";
import { TimelineClip } from "./TimelineClip";
import { toMediaUrl } from "@/lib/media";
import { Layers } from "lucide-react";

export function Timeline() {
  const plan = useTimelineStore((s) => s.plan);
  const selectedIndex = useTimelineStore((s) => s.selectedIndex);
  const selectEntry = useTimelineStore((s) => s.selectEntry);
  const reorderEntries = useTimelineStore((s) => s.reorderEntries);
  const setSourceMonitorSrc = useUiStore((s) => s.setSourceMonitorSrc);

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
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

  const entries = plan?.entries ?? [];

  return (
    <div className="h-full flex flex-col bg-background">
      <div className="px-3 py-1.5 text-xs font-medium text-muted border-b border-border flex items-center gap-1.5">
        <Layers className="w-3 h-3" />
        Timeline
        {plan && (
          <span className="ml-auto text-[10px]">
            {entries.length} clips -- {plan.total_duration.toFixed(1)}s
          </span>
        )}
      </div>

      {/* Ruler */}
      {plan && entries.length > 0 && (
        <div className="h-5 border-b border-border bg-surface flex items-end px-2">
          {Array.from({ length: Math.ceil(plan.total_duration) + 1 }, (_, i) => {
            const pct = (i / plan.total_duration) * 100;
            return (
              <div
                key={i}
                className="absolute text-[8px] text-muted"
                style={{ left: `${pct}%` }}
              >
                {i % 5 === 0 ? `${i}s` : ""}
              </div>
            );
          })}
        </div>
      )}

      {/* Track */}
      <div className="flex-1 overflow-x-auto overflow-y-hidden">
        {entries.length > 0 ? (
          <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
            <SortableContext items={entries.map((_, i) => i)} strategy={horizontalListSortingStrategy}>
              <div className="flex items-stretch h-full p-2 gap-1 min-w-max">
                {entries.map((entry, i) => (
                  <TimelineClip
                    key={`${entry.shot_id}-${entry.position}`}
                    entry={entry}
                    index={i}
                    isSelected={selectedIndex === i}
                    totalDuration={plan!.total_duration}
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
            <p className="text-muted text-xs">
              {plan ? "Empty timeline" : "Run a pipeline to populate the timeline"}
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
