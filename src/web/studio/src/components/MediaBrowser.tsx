"use client";

import { useEffect, useRef, useCallback } from "react";
import { useMediaStore } from "@/stores/mediaStore";
import { useUiStore } from "@/stores/uiStore";
import { toMediaUrl } from "@/lib/media";
import { FolderOpen, Search, Loader2 } from "lucide-react";

export function MediaBrowser() {
  const { catalog, searchResults, query, filter, loading, fetchCatalog, searchFootage, setQuery, setFilter } =
    useMediaStore();
  const setSourceMonitorSrc = useUiStore((s) => s.setSourceMonitorSrc);
  const debounceRef = useRef<ReturnType<typeof setTimeout>>(undefined);

  useEffect(() => {
    fetchCatalog();
  }, [fetchCatalog]);

  const handleSearch = useCallback(
    (value: string) => {
      setQuery(value);
      clearTimeout(debounceRef.current);
      debounceRef.current = setTimeout(() => {
        searchFootage(value);
      }, 200);
    },
    [setQuery, searchFootage],
  );

  const shots = query.trim() ? searchResults : catalog;
  const filtered =
    filter === "all" ? shots : shots.filter((s) => s.roll_type === filter);

  const rollBadge = (rollType: string) => {
    switch (rollType) {
      case "a-roll":
        return "bg-blue-500/20 text-blue-400";
      case "b-roll":
        return "bg-emerald-500/20 text-emerald-400";
      default:
        return "bg-slate-500/20 text-slate-400";
    }
  };

  return (
    <div className="h-full flex flex-col bg-background border-r border-border">
      <div className="px-3 py-1.5 text-xs font-medium text-muted border-b border-border flex items-center gap-1.5">
        <FolderOpen className="w-3 h-3" />
        Media
      </div>

      {/* Search */}
      <div className="px-2 py-2 border-b border-border">
        <div className="relative">
          <Search className="absolute left-2 top-1/2 -translate-y-1/2 w-3 h-3 text-muted" />
          <input
            value={query}
            onChange={(e) => handleSearch(e.target.value)}
            placeholder="Search shots..."
            className="w-full pl-7 pr-2 py-1 text-xs rounded border border-border bg-surface text-foreground focus:outline-none focus:border-accent"
          />
        </div>

        {/* Filters */}
        <div className="flex gap-1 mt-1.5">
          {(["all", "a-roll", "b-roll"] as const).map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`px-2 py-0.5 rounded text-[10px] font-medium transition-colors ${
                filter === f
                  ? "bg-accent/20 text-accent"
                  : "text-muted hover:text-foreground"
              }`}
            >
              {f === "all" ? "All" : f}
            </button>
          ))}
        </div>
      </div>

      {/* Shot list */}
      <div className="flex-1 overflow-y-auto">
        {loading && filtered.length === 0 && (
          <div className="text-center py-6 text-muted">
            <Loader2 className="w-4 h-4 animate-spin mx-auto" />
          </div>
        )}

        {filtered.map((shot, i) => (
          <button
            key={`${shot.shot_id}-${i}`}
            onClick={() => {
              setSourceMonitorSrc(toMediaUrl(shot.source_file));
            }}
            className="w-full text-left px-2 py-2 border-b border-border hover:bg-surface-hover transition-colors"
          >
            <div className="flex items-center gap-1.5 mb-0.5">
              <span className={`px-1.5 py-0.5 rounded text-[9px] font-medium ${rollBadge(shot.roll_type)}`}>
                {shot.roll_type}
              </span>
              <span className="text-xs font-medium truncate">{shot.display_label}</span>
            </div>
            <p className="text-[10px] text-muted truncate">
              {shot.duration.toFixed(1)}s
              {shot.description ? ` -- ${shot.description}` : ""}
            </p>
            {shot.transcript && (
              <p className="text-[10px] text-muted/60 truncate mt-0.5">
                &ldquo;{shot.transcript.slice(0, 80)}&rdquo;
              </p>
            )}
          </button>
        ))}

        {!loading && filtered.length === 0 && (
          <p className="text-center py-6 text-muted text-xs">No shots found</p>
        )}
      </div>
    </div>
  );
}
