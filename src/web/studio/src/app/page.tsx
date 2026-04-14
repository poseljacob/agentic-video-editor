"use client";

import { useEffect, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import { useProjectStore } from "@/stores/projectStore";
import { FolderPicker } from "@/components/FolderPicker";
import {
  FolderOpen,
  Plus,
  Trash2,
  Film,
  Loader2,
  AlertCircle,
  CheckCircle2,
} from "lucide-react";

export default function ProjectPicker() {
  const router = useRouter();
  const { projects, loading, error, fetchProjects, createProject, deleteProject } =
    useProjectStore();
  const [showCreate, setShowCreate] = useState(false);
  const [name, setName] = useState("");
  const [footageDir, setFootageDir] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState("");
  const [showFolderPicker, setShowFolderPicker] = useState(false);

  useEffect(() => {
    fetchProjects();
  }, [fetchProjects]);

  // Poll preprocessing projects.
  useEffect(() => {
    const preprocessing = projects.filter((p) => p.status === "preprocessing");
    if (preprocessing.length === 0) return;
    const timer = setInterval(() => {
      preprocessing.forEach((p) => useProjectStore.getState().pollProject(p.id));
    }, 2000);
    return () => clearInterval(timer);
  }, [projects]);

  const handleCreate = useCallback(async () => {
    if (!name.trim() || !footageDir.trim()) return;
    setCreating(true);
    setCreateError("");
    try {
      await createProject(name.trim(), footageDir.trim());
      setShowCreate(false);
      setName("");
      setFootageDir("");
    } catch (e) {
      setCreateError((e as Error).message);
    } finally {
      setCreating(false);
    }
  }, [name, footageDir, createProject]);

  const statusIcon = (status: string) => {
    switch (status) {
      case "preprocessing":
        return <Loader2 className="w-4 h-4 animate-spin text-amber-400" />;
      case "ready":
        return <CheckCircle2 className="w-4 h-4 text-accent" />;
      case "failed":
        return <AlertCircle className="w-4 h-4 text-destructive" />;
      default:
        return null;
    }
  };

  return (
    <div className="h-full flex flex-col items-center justify-center p-8">
      <div className="w-full max-w-2xl">
        <div className="flex items-center gap-3 mb-8">
          <Film className="w-8 h-8 text-accent" />
          <h1 className="text-3xl font-bold tracking-tight">AVE Studio</h1>
        </div>

        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-medium text-muted">Projects</h2>
          <button
            onClick={() => setShowCreate(true)}
            className="flex items-center gap-2 px-3 py-1.5 rounded bg-accent hover:bg-accent-hover text-black text-sm font-medium transition-colors"
          >
            <Plus className="w-4 h-4" />
            New Project
          </button>
        </div>

        {loading && projects.length === 0 && (
          <div className="text-center py-12 text-muted">
            <Loader2 className="w-6 h-6 animate-spin mx-auto mb-2" />
            Loading projects...
          </div>
        )}

        {error && (
          <div className="text-center py-4 text-destructive text-sm">{error}</div>
        )}

        {!loading && projects.length === 0 && !error && (
          <div className="text-center py-12 border border-dashed border-border rounded-lg">
            <FolderOpen className="w-10 h-10 mx-auto mb-3 text-muted" />
            <p className="text-muted">No projects yet. Create one to get started.</p>
          </div>
        )}

        <div className="space-y-2">
          {projects.map((p) => (
            <div
              key={p.id}
              className={`flex items-center gap-3 p-4 rounded-lg border transition-colors ${
                p.status === "ready"
                  ? "border-border hover:border-border-hover cursor-pointer bg-surface hover:bg-surface-hover"
                  : "border-border bg-surface opacity-80"
              }`}
              onClick={() => p.status === "ready" && router.push(`/editor/${p.id}`)}
            >
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  {statusIcon(p.status)}
                  <span className="font-medium truncate">{p.name}</span>
                </div>
                <p className="text-xs text-muted mt-1 truncate">{p.footage_dir}</p>
              </div>
              <div className="text-right text-xs text-muted shrink-0">
                {p.status === "ready" && <span>{p.shot_count} shots</span>}
                {p.status === "preprocessing" && <span>Indexing...</span>}
                {p.status === "failed" && (
                  <span className="text-destructive">Failed</span>
                )}
              </div>
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  deleteProject(p.id);
                }}
                className="p-1 text-muted hover:text-destructive transition-colors"
              >
                <Trash2 className="w-4 h-4" />
              </button>
            </div>
          ))}
        </div>

        {/* Create dialog */}
        {showCreate && (
          <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50">
            <div className="bg-surface border border-border rounded-lg p-6 w-full max-w-md">
              <h3 className="text-lg font-semibold mb-4">New Project</h3>

              <label className="block text-sm text-muted mb-1">Project name</label>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. Summer Campaign"
                className="w-full px-3 py-2 rounded border border-border bg-background text-foreground mb-4 focus:outline-none focus:border-accent"
              />

              <label className="block text-sm text-muted mb-1">Footage directory</label>
              <button
                type="button"
                onClick={() => setShowFolderPicker(true)}
                className="w-full flex items-center gap-2 px-3 py-2 rounded border border-border bg-background text-left mb-4 hover:border-border-hover transition-colors"
              >
                <FolderOpen className="w-4 h-4 text-muted shrink-0" />
                {footageDir ? (
                  <span className="font-mono text-sm truncate">{footageDir}</span>
                ) : (
                  <span className="text-muted text-sm">Click to browse...</span>
                )}
              </button>

              {createError && (
                <p className="text-destructive text-sm mb-3">{createError}</p>
              )}

              <div className="flex justify-end gap-2">
                <button
                  onClick={() => setShowCreate(false)}
                  className="px-4 py-2 rounded text-sm text-muted hover:text-foreground transition-colors"
                >
                  Cancel
                </button>
                <button
                  onClick={handleCreate}
                  disabled={creating || !name.trim() || !footageDir.trim()}
                  className="px-4 py-2 rounded bg-accent hover:bg-accent-hover text-black text-sm font-medium disabled:opacity-50 transition-colors"
                >
                  {creating ? (
                    <Loader2 className="w-4 h-4 animate-spin" />
                  ) : (
                    "Create"
                  )}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>

      {showFolderPicker && (
        <FolderPicker
          onSelect={(path) => {
            setFootageDir(path);
            setShowFolderPicker(false);
          }}
          onCancel={() => setShowFolderPicker(false)}
        />
      )}
    </div>
  );
}
