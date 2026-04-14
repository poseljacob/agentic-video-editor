"use client";

import { useTimelineStore } from "@/stores/timelineStore";
import type { EnrichedEditPlanEntry } from "@/types/api";
import { Scissors, Trash2, Film } from "lucide-react";

interface ClipPanelProps {
  entry: EnrichedEditPlanEntry;
  index: number;
  jobId: string | null;
}

export function ClipPanel({ entry, index, jobId }: ClipPanelProps) {
  const updateEntry = useTimelineStore((s) => s.updateEntry);
  const deleteEntry = useTimelineStore((s) => s.deleteEntry);
  const savePlan = useTimelineStore((s) => s.savePlan);
  const isDirty = useTimelineStore((s) => s.isDirty);

  const duration = entry.end_trim - entry.start_trim;

  return (
    <div className="p-3 space-y-4 text-xs">
      {/* Shot info */}
      <div>
        <h3 className="font-medium text-foreground mb-1">{entry.display_label}</h3>
        <div className="flex items-center gap-1.5 mb-2">
          <span
            className={`px-1.5 py-0.5 rounded text-[9px] font-medium ${
              entry.roll_type === "a-roll"
                ? "bg-blue-500/20 text-blue-400"
                : entry.roll_type === "b-roll"
                ? "bg-emerald-500/20 text-emerald-400"
                : "bg-slate-500/20 text-slate-400"
            }`}
          >
            {entry.roll_type}
          </span>
          <span className="text-muted">{duration.toFixed(1)}s</span>
        </div>
        <p className="text-muted text-[10px] break-all">{entry.source_filename}</p>
      </div>

      {/* Trim controls */}
      <div>
        <div className="flex items-center gap-1 mb-2 text-muted">
          <Scissors className="w-3 h-3" />
          <span className="font-medium">Trim</span>
        </div>

        <label className="block mb-2">
          <span className="text-muted">Start (s)</span>
          <input
            type="number"
            step={0.1}
            min={0}
            max={entry.end_trim - 0.1}
            value={entry.start_trim}
            onChange={(e) =>
              updateEntry(index, { start_trim: parseFloat(e.target.value) || 0 })
            }
            className="w-full mt-0.5 px-2 py-1 rounded border border-border bg-surface text-foreground focus:outline-none focus:border-accent"
          />
        </label>

        <label className="block mb-2">
          <span className="text-muted">End (s)</span>
          <input
            type="number"
            step={0.1}
            min={entry.start_trim + 0.1}
            value={entry.end_trim}
            onChange={(e) =>
              updateEntry(index, { end_trim: parseFloat(e.target.value) || 0 })
            }
            className="w-full mt-0.5 px-2 py-1 rounded border border-border bg-surface text-foreground focus:outline-none focus:border-accent"
          />
        </label>
      </div>

      {/* Text overlay */}
      <div>
        <label className="block">
          <span className="text-muted font-medium">Text Overlay</span>
          <input
            type="text"
            value={entry.text_overlay ?? ""}
            onChange={(e) =>
              updateEntry(index, {
                text_overlay: e.target.value || null,
              })
            }
            placeholder="None"
            className="w-full mt-0.5 px-2 py-1 rounded border border-border bg-surface text-foreground focus:outline-none focus:border-accent"
          />
        </label>
      </div>

      {/* Transition */}
      <div>
        <label className="block">
          <span className="text-muted font-medium">Transition</span>
          <select
            value={entry.transition ?? ""}
            onChange={(e) =>
              updateEntry(index, {
                transition: e.target.value || null,
              })
            }
            className="w-full mt-0.5 px-2 py-1 rounded border border-border bg-surface text-foreground focus:outline-none focus:border-accent"
          >
            <option value="">None</option>
            <option value="crossfade">Crossfade</option>
            <option value="fade">Fade</option>
            <option value="cut">Cut</option>
          </select>
        </label>
      </div>

      {/* Metadata */}
      <div className="space-y-1 text-[10px] text-muted">
        <div className="flex items-center gap-1">
          <Film className="w-3 h-3" />
          <span>Shot ID: {entry.shot_id}</span>
        </div>
        <div>Position: {entry.position}</div>
        <div>Source timestamp: {entry.source_timestamp?.toFixed(1) ?? "--"}s</div>
      </div>

      <div className="border-t border-border pt-3 space-y-2">
        {isDirty && jobId && (
          <button
            onClick={() => savePlan(jobId)}
            className="w-full px-3 py-1.5 rounded bg-accent hover:bg-accent-hover text-black text-xs font-medium transition-colors"
          >
            Save Changes
          </button>
        )}

        <button
          onClick={() => deleteEntry(index)}
          className="w-full flex items-center justify-center gap-1.5 px-3 py-1.5 rounded border border-destructive/50 text-destructive hover:bg-destructive/10 text-xs font-medium transition-colors"
        >
          <Trash2 className="w-3 h-3" />
          Remove Clip
        </button>
      </div>
    </div>
  );
}
