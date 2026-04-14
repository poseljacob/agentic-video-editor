import { create } from "zustand";
import type { Project } from "@/types/api";
import * as api from "@/lib/api";

interface ProjectState {
  projects: Project[];
  loading: boolean;
  error: string;
  fetchProjects: () => Promise<void>;
  createProject: (name: string, footageDir: string) => Promise<string>;
  deleteProject: (id: string) => Promise<void>;
  pollProject: (id: string) => Promise<Project>;
}

export const useProjectStore = create<ProjectState>((set, get) => ({
  projects: [],
  loading: false,
  error: "",

  fetchProjects: async () => {
    set({ loading: true, error: "" });
    try {
      const projects = await api.getProjects();
      set({ projects, loading: false });
    } catch (e) {
      set({ error: (e as Error).message, loading: false });
    }
  },

  createProject: async (name, footageDir) => {
    const res = await api.createProject(name, footageDir);
    await get().fetchProjects();
    return res.id;
  },

  deleteProject: async (id) => {
    await api.deleteProject(id);
    set({ projects: get().projects.filter((p) => p.id !== id) });
  },

  pollProject: async (id) => {
    const project = await api.getProject(id);
    const existing = get().projects;
    const found = existing.some((p) => p.id === id);
    set({
      projects: found
        ? existing.map((p) => (p.id === id ? project : p))
        : [...existing, project],
    });
    return project;
  },
}));
