"use client";

import { useSortable } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import type { EnrichedEditPlanEntry } from "@/types/api";
import { Type } from "lucide-react";

interface TimelineClipProps {
  entry: EnrichedEditPlanEntry;
  index: number;
  isSelected: boolean;
  totalDuration: number;
  onClick: () => void;
}

export function TimelineClip({ entry, index, isSelected, totalDuration, onClick }: TimelineClipProps) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } =
    useSortable({ id: index });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.5 : 1,
  };

  const duration = entry.end_trim - entry.start_trim;
  // Min width 60px, scale proportionally.
  const widthPx = Math.max(60, (duration / Math.max(totalDuration, 1)) * 800);

  const borderColor = () => {
    if (isSelected) return "border-accent";
    switch (entry.roll_type) {
      case "a-roll":
        return "border-blue-500/60";
      case "b-roll":
        return "border-emerald-500/60";
      default:
        return "border-slate-600";
    }
  };

  const bgColor = () => {
    switch (entry.roll_type) {
      case "a-roll":
        return "bg-blue-500/10";
      case "b-roll":
        return "bg-emerald-500/10";
      default:
        return "bg-slate-500/10";
    }
  };

  return (
    <div
      ref={setNodeRef}
      style={{ ...style, width: `${widthPx}px` }}
      {...attributes}
      {...listeners}
      onClick={onClick}
      className={`shrink-0 rounded border-2 ${borderColor()} ${bgColor()} cursor-pointer hover:brightness-110 transition-all flex flex-col justify-between p-1.5 select-none`}
    >
      <div>
        <div className="text-[10px] font-medium truncate">{entry.display_label}</div>
        <div className="text-[9px] text-muted">{duration.toFixed(1)}s</div>
      </div>

      {entry.text_overlay && (
        <div className="flex items-center gap-0.5 mt-1">
          <Type className="w-2.5 h-2.5 text-amber-400" />
          <span className="text-[8px] text-amber-400 truncate">{entry.text_overlay}</span>
        </div>
      )}

      <div className="mt-auto">
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
  );
}
