import { create } from "zustand";

interface UiState {
  consoleOpen: boolean;
  chatOpen: boolean;
  sourceMonitorSrc: string | null;
  programMonitorSrc: string | null;
  runDialogOpen: boolean;

  toggleConsole: () => void;
  toggleChat: () => void;
  setSourceMonitorSrc: (src: string | null) => void;
  setProgramMonitorSrc: (src: string | null) => void;
  setRunDialogOpen: (open: boolean) => void;
}

export const useUiStore = create<UiState>((set) => ({
  consoleOpen: false,
  chatOpen: false,
  sourceMonitorSrc: null,
  programMonitorSrc: null,
  runDialogOpen: false,

  toggleConsole: () => set((s) => ({ consoleOpen: !s.consoleOpen })),
  toggleChat: () => set((s) => ({ chatOpen: !s.chatOpen })),
  setSourceMonitorSrc: (src) => set({ sourceMonitorSrc: src }),
  setProgramMonitorSrc: (src) => set({ programMonitorSrc: src }),
  setRunDialogOpen: (open) => set({ runDialogOpen: open }),
}));
