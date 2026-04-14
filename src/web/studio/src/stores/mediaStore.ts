import { create } from "zustand";
import type { ShotSearchResult } from "@/types/api";
import * as api from "@/lib/api";

interface MediaState {
  catalog: ShotSearchResult[];
  searchResults: ShotSearchResult[];
  query: string;
  footageIndexPath: string;
  loading: boolean;
  error: string;
  filter: "all" | "a-roll" | "b-roll";

  setFootageIndexPath: (path: string) => void;
  fetchCatalog: () => Promise<void>;
  searchFootage: (query: string) => Promise<void>;
  setQuery: (query: string) => void;
  setFilter: (filter: MediaState["filter"]) => void;
  reset: () => void;
}

export const useMediaStore = create<MediaState>((set, get) => ({
  catalog: [],
  searchResults: [],
  query: "",
  footageIndexPath: "",
  loading: false,
  error: "",
  filter: "all",

  setFootageIndexPath: (path) => set({ footageIndexPath: path }),

  fetchCatalog: async () => {
    const { footageIndexPath } = get();
    if (!footageIndexPath) return;
    set({ loading: true, error: "" });
    try {
      const data = await api.getCatalog(footageIndexPath);
      set({ catalog: data.results, loading: false });
    } catch (e) {
      set({ error: (e as Error).message, loading: false });
    }
  },

  searchFootage: async (query) => {
    const { footageIndexPath } = get();
    if (!footageIndexPath || !query.trim()) {
      set({ searchResults: [], query });
      return;
    }
    set({ loading: true, query });
    try {
      const data = await api.searchFootage(query, footageIndexPath);
      set({ searchResults: data.results, loading: false });
    } catch (e) {
      set({ error: (e as Error).message, loading: false });
    }
  },

  setQuery: (query) => set({ query }),
  setFilter: (filter) => set({ filter }),
  reset: () => set({ catalog: [], searchResults: [], query: "", footageIndexPath: "", filter: "all" }),
}));
