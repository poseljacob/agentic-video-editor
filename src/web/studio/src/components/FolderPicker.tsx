"use client";

import { useState, useEffect, useCallback } from "react";
import * as api from "@/lib/api";
import type { BrowseEntry } from "@/lib/api";
import {
  Folder,
  FolderOpen,
  ChevronUp,
  Film,
  Check,
  Loader2,
  X,
} from "lucide-react";

interface FolderPickerProps {
  onSelect: (path: string) => void;
  onCancel: () => void;
}

export function FolderPicker({ onSelect, onCancel }: FolderPickerProps) {
  const [currentPath, setCurrentPath] = useState("~");
  const [dirs, setDirs] = useState<BrowseEntry[]>([]);
  const [files, setFiles] = useState<BrowseEntry[]>([]);
  const [parentPath, setParentPath] = useState<string | null>(null);
  const [videoCount, setVideoCount] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const browse = useCallback(async (path: string) => {
    setLoading(true);
    setError("");
    try {
      const result = await api.browseDirectory(path);
      setCurrentPath(result.current);
      setDirs(result.dirs);
      setFiles(result.files);
      setParentPath(result.parent);
      setVideoCount(result.video_count);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    browse(currentPath);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-[60]">
      <div className="bg-surface border border-border rounded-lg w-full max-w-lg flex flex-col max-h-[80vh]">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-border shrink-0">
          <h3 className="text-sm font-semibold">Select Footage Folder</h3>
          <button onClick={onCancel} className="p-1 rounded hover:bg-surface-hover text-muted">
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Current path */}
        <div className="px-4 py-2 bg-background/50 border-b border-border text-xs font-mono text-muted truncate shrink-0">
          {currentPath}
        </div>

        {/* Directory listing */}
        <div className="flex-1 overflow-y-auto min-h-0">
          {loading && (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="w-5 h-5 animate-spin text-muted" />
            </div>
          )}

          {error && (
            <div className="px-4 py-3 text-destructive text-xs">{error}</div>
          )}

          {!loading && !error && (
            <>
              {/* Parent directory */}
              {parentPath && (
                <button
                  onClick={() => browse(parentPath)}
                  className="w-full flex items-center gap-2 px-4 py-2 text-xs hover:bg-surface-hover transition-colors text-muted"
                >
                  <ChevronUp className="w-3.5 h-3.5" />
                  <span>..</span>
                </button>
              )}

              {/* Subdirectories */}
              {dirs.map((dir) => (
                <button
                  key={dir.path}
                  onClick={() => browse(dir.path)}
                  className="w-full flex items-center gap-2 px-4 py-2 text-xs hover:bg-surface-hover transition-colors text-left"
                >
                  <Folder className="w-3.5 h-3.5 text-amber-400 shrink-0" />
                  <span className="truncate">{dir.name}</span>
                </button>
              ))}

              {/* Video files (shown but not clickable -- just for context) */}
              {files.map((file) => (
                <div
                  key={file.path}
                  className="flex items-center gap-2 px-4 py-1.5 text-xs text-muted/60"
                >
                  <Film className="w-3.5 h-3.5 shrink-0" />
                  <span className="truncate">{file.name}</span>
                </div>
              ))}

              {dirs.length === 0 && files.length === 0 && (
                <p className="px-4 py-6 text-center text-xs text-muted">Empty directory</p>
              )}
            </>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between px-4 py-3 border-t border-border shrink-0">
          <div className="text-xs text-muted">
            {videoCount > 0 && (
              <span className="flex items-center gap-1">
                <Film className="w-3 h-3" />
                {videoCount} video{videoCount !== 1 ? "s" : ""} in this folder
              </span>
            )}
          </div>
          <div className="flex gap-2">
            <button
              onClick={onCancel}
              className="px-3 py-1.5 rounded text-xs text-muted hover:text-foreground transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={() => onSelect(currentPath)}
              className="flex items-center gap-1.5 px-4 py-1.5 rounded bg-accent hover:bg-accent-hover text-black text-xs font-medium transition-colors"
            >
              <Check className="w-3 h-3" />
              Select This Folder
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
