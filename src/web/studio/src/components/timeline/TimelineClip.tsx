"use client";

import { useCallback, useRef } from "react";
import { useSortable } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import type { EnrichedEditPlanEntry } from "@/types/api";
import { useTimelineStore } from "@/stores/timelineStore";
import { Type } from "lucide-react";

interface TimelineClipProps {
  entry: EnrichedEditPlanEntry;
  index: number;
  isSelected: boolean;
  pxPerSecond: number;
  onClick: () => void;
}

export function TimelineClip({ entry, index, isSelected, pxPerSecond, onClick }: TimelineClipProps) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } =
    useSortable({ id: index });
  const updateEntry = useTimelineStore((s) => s.updateEntry);

  const dragRef = useRef<{
    side: "left" | "right";
    startX: number;
    origStart: number;
    origEnd: number;
  } | null>(null);

  const duration = entry.end_trim - entry.start_trim;
  const widthPx = Math.max(40, duration * pxPerSecond);

  const handleTrimStart = useCallback(
    (side: "left" | "right", e: React.PointerEvent) => {
      e.stopPropagation();
      e.preventDefault();
      const el = e.currentTarget as HTMLElement;
      el.setPointerCapture(e.pointerId);
      dragRef.current = {
        side,
        startX: e.clientX,
        origStart: entry.start_trim,
        origEnd: entry.end_trim,
      };
    },
    [entry.start_trim, entry.end_trim],
  );

  const handleTrimMove = useCallback(
    (e: React.PointerEvent) => {
      if (!dragRef.current) return;
      const dx = e.clientX - dragRef.current.startX;
      const dt = dx / pxPerSecond;

      if (dragRef.current.side === "left") {
        const newStart = Math.max(0, Math.min(dragRef.current.origEnd - 0.1, dragRef.current.origStart + dt));
        updateEntry(index, { start_trim: Math.round(newStart * 10) / 10 });
      } else {
        const newEnd = Math.max(dragRef.current.origStart + 0.1, dragRef.current.origEnd + dt);
        updateEntry(index, { end_trim: Math.round(newEnd * 10) / 10 });
      }
    },
    [index, pxPerSecond, updateEntry],
  );

  const handleTrimEnd = useCallback(() => {
    dragRef.current = null;
  }, []);

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.4 : 1,
    width: `${widthPx}px`,
  };

  const borderColor = isSelected
    ? "border-accent"
    : entry.roll_type === "a-roll"
    ? "border-blue-500/60"
    : entry.roll_type === "b-roll"
    ? "border-emerald-500/60"
    : "border-slate-600";

  const bgColor =
    entry.roll_type === "a-roll"
      ? "bg-blue-500/15"
      : entry.roll_type === "b-roll"
      ? "bg-emerald-500/15"
      : "bg-slate-500/15";

  const fillColor =
    entry.roll_type === "a-roll"
      ? "bg-blue-500/30"
      : entry.roll_type === "b-roll"
      ? "bg-emerald-500/30"
      : "bg-slate-500/30";

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`group relative shrink-0 rounded border-2 ${borderColor} ${bgColor} select-none flex flex-col justify-between overflow-hidden ${
        isDragging ? "z-50 shadow-lg" : ""
      }`}
    >
      {/* Left trim handle */}
      <div
        className="absolute left-0 top-0 bottom-0 w-2 cursor-col-resize z-10 hover:bg-white/20 active:bg-white/30 transition-colors"
        onPointerDown={(e) => handleTrimStart("left", e)}
        onPointerMove={handleTrimMove}
        onPointerUp={handleTrimEnd}
        onPointerCancel={handleTrimEnd}
      >
        <div className="absolute left-0.5 top-1/2 -translate-y-1/2 w-0.5 h-6 bg-white/40 rounded-full opacity-0 group-hover:opacity-100 transition-opacity" />
      </div>

      {/* Main clickable/draggable area */}
      <div
        {...attributes}
        {...listeners}
        onClick={onClick}
        className="flex-1 flex flex-col justify-between px-3 py-1.5 cursor-grab active:cursor-grabbing"
      >
        {/* Top fill bar showing proportional fill */}
        <div className={`absolute top-0 left-0 h-1 ${fillColor}`} style={{ width: "100%" }} />

        <div className="mt-1">
          <div className="text-[11px] font-medium truncate">{entry.display_label}</div>
          <div className="text-[10px] text-muted">{duration.toFixed(1)}s</div>
        </div>

        {entry.text_overlay && (
          <div className="flex items-center gap-0.5 mt-1">
            <Type className="w-2.5 h-2.5 text-amber-400" />
            <span className="text-[9px] text-amber-400 truncate">{entry.text_overlay}</span>
          </div>
        )}

        <div className="mt-auto pt-1">
          <span className={`inline-block px-1 py-0.5 rounded text-[8px] font-medium ${
            entry.roll_type === "a-roll"
              ? "bg-blue-500/20 text-blue-400"
              : entry.roll_type === "b-roll"
              ? "bg-emerald-500/20 text-emerald-400"
              : "bg-slate-500/20 text-slate-400"
          }`}>
            {entry.roll_type}
          </span>
        </div>
      </div>

      {/* Right trim handle */}
      <div
        className="absolute right-0 top-0 bottom-0 w-2 cursor-col-resize z-10 hover:bg-white/20 active:bg-white/30 transition-colors"
        onPointerDown={(e) => handleTrimStart("right", e)}
        onPointerMove={handleTrimMove}
        onPointerUp={handleTrimEnd}
        onPointerCancel={handleTrimEnd}
      >
        <div className="absolute right-0.5 top-1/2 -translate-y-1/2 w-0.5 h-6 bg-white/40 rounded-full opacity-0 group-hover:opacity-100 transition-opacity" />
      </div>
    </div>
  );
}
