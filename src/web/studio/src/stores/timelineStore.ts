import { create } from "zustand";
import type { EnrichedEditPlan, EnrichedEditPlanEntry } from "@/types/api";
import * as api from "@/lib/api";

interface TimelineState {
  plan: EnrichedEditPlan | null;
  selectedIndex: number | null;
  isDirty: boolean;
  loading: boolean;
  saving: boolean;
  error: string;
  saveFlash: string;

  fetchEditPlan: (jobId: string) => Promise<void>;
  selectEntry: (index: number | null) => void;
  updateEntry: (index: number, patch: Partial<EnrichedEditPlanEntry>) => void;
  reorderEntries: (fromIndex: number, toIndex: number) => void;
  deleteEntry: (index: number) => void;
  savePlan: (jobId: string) => Promise<void>;
  setPlan: (plan: EnrichedEditPlan | null) => void;
  reset: () => void;
}

export const useTimelineStore = create<TimelineState>((set, get) => ({
  plan: null,
  selectedIndex: null,
  isDirty: false,
  loading: false,
  saving: false,
  error: "",
  saveFlash: "",

  fetchEditPlan: async (jobId) => {
    set({ loading: true, error: "" });
    try {
      const plan = await api.getEditPlan(jobId);
      set({ plan, loading: false, isDirty: false, selectedIndex: null });
    } catch (e) {
      set({ error: (e as Error).message, loading: false });
    }
  },

  selectEntry: (index) => set({ selectedIndex: index }),

  updateEntry: (index, patch) => {
    const { plan } = get();
    if (!plan) return;
    const entries = plan.entries.map((e, i) => (i === index ? { ...e, ...patch } : e));
    const totalDuration = entries.reduce((sum, e) => sum + (e.end_trim - e.start_trim), 0);
    set({
      plan: { ...plan, entries, total_duration: totalDuration },
      isDirty: true,
    });
  },

  reorderEntries: (fromIndex, toIndex) => {
    const { plan } = get();
    if (!plan) return;
    const entries = [...plan.entries];
    const [moved] = entries.splice(fromIndex, 1);
    entries.splice(toIndex, 0, moved);
    entries.forEach((e, i) => (e.position = i));
    set({ plan: { ...plan, entries }, isDirty: true, selectedIndex: toIndex });
  },

  deleteEntry: (index) => {
    const { plan, selectedIndex } = get();
    if (!plan) return;
    const entries = plan.entries.filter((_, i) => i !== index);
    entries.forEach((e, i) => (e.position = i));
    const totalDuration = entries.reduce((sum, e) => sum + (e.end_trim - e.start_trim), 0);
    set({
      plan: { ...plan, entries, entry_count: entries.length, total_duration: totalDuration },
      isDirty: true,
      selectedIndex: selectedIndex === index ? null : selectedIndex,
    });
  },

  savePlan: async (jobId) => {
    const { plan } = get();
    if (!plan) return;
    set({ saving: true, error: "", saveFlash: "" });
    try {
      await api.updateEditPlan(jobId, {
        brief: plan.entries.length > 0 ? {} : {},
        music_path: null,
        total_duration: plan.total_duration,
        entries: plan.entries.map((e) => ({
          shot_id: e.shot_id,
          start_trim: e.start_trim,
          end_trim: e.end_trim,
          position: e.position,
          text_overlay: e.text_overlay,
          transition: e.transition,
        })),
      });
      set({ saving: false, isDirty: false, saveFlash: "Saved" });
      setTimeout(() => set({ saveFlash: "" }), 2000);
    } catch (e) {
      set({ error: (e as Error).message, saving: false });
    }
  },

  setPlan: (plan) => set({ plan, isDirty: false }),
  reset: () => set({ plan: null, selectedIndex: null, isDirty: false, loading: false, error: "" }),
}));
