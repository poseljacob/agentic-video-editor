"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import { useUiStore } from "@/stores/uiStore";
import { useJobStore } from "@/stores/jobStore";
import { useTimelineStore } from "@/stores/timelineStore";
import * as api from "@/lib/api";
import { X, Send, Loader2, MessageSquare } from "lucide-react";

interface ChatMessage {
  role: "user" | "system";
  text: string;
}

export function ChatPanel() {
  const toggleChat = useUiStore((s) => s.toggleChat);
  const currentJobId = useJobStore((s) => s.currentJobId);
  const fetchEditPlan = useTimelineStore((s) => s.fetchEditPlan);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages.length]);

  const handleSend = useCallback(async () => {
    if (!input.trim() || !currentJobId || sending) return;
    const text = input.trim();
    setInput("");
    setMessages((prev) => [...prev, { role: "user", text }]);
    setSending(true);

    try {
      const res = await api.postFeedback(currentJobId, text);
      setMessages((prev) => [
        ...prev,
        { role: "system", text: `Feedback submitted. New job: ${res.job_id}` },
      ]);

      // The feedback creates a new job -- stream it and load results when done.
      useJobStore.getState().setCurrentJobId(res.job_id);
      useJobStore.getState().setStatus("connecting");

      const poll = setInterval(async () => {
        try {
          const job = await api.getJob(res.job_id);
          if (job.status === "completed") {
            clearInterval(poll);
            fetchEditPlan(res.job_id);
            setMessages((prev) => [...prev, { role: "system", text: "Revision complete." }]);
          } else if (job.status === "failed") {
            clearInterval(poll);
            setMessages((prev) => [
              ...prev,
              { role: "system", text: `Failed: ${job.error ?? "Unknown error"}` },
            ]);
          }
        } catch {
          clearInterval(poll);
        }
      }, 3000);
    } catch (e) {
      setMessages((prev) => [
        ...prev,
        { role: "system", text: `Error: ${(e as Error).message}` },
      ]);
    } finally {
      setSending(false);
    }
  }, [input, currentJobId, sending, fetchEditPlan]);

  return (
    <div className="fixed right-0 top-0 bottom-0 w-80 z-40 bg-surface border-l border-border flex flex-col shadow-xl">
      <div className="flex items-center justify-between px-3 py-2 border-b border-border shrink-0">
        <div className="flex items-center gap-1.5 text-xs font-medium">
          <MessageSquare className="w-3 h-3" />
          Feedback Chat
        </div>
        <button onClick={toggleChat} className="p-1 rounded hover:bg-surface-hover text-muted">
          <X className="w-4 h-4" />
        </button>
      </div>

      <div ref={scrollRef} className="flex-1 overflow-y-auto p-3 space-y-2">
        {messages.length === 0 && (
          <p className="text-muted text-xs text-center mt-8">
            Send feedback to revise the edit.
            {!currentJobId && " Run a pipeline first."}
          </p>
        )}

        {messages.map((msg, i) => (
          <div
            key={i}
            className={`text-xs p-2 rounded ${
              msg.role === "user"
                ? "bg-accent/10 text-foreground ml-4"
                : "bg-surface-hover text-muted mr-4"
            }`}
          >
            {msg.text}
          </div>
        ))}
      </div>

      <div className="p-2 border-t border-border shrink-0">
        <div className="flex gap-1.5">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && handleSend()}
            placeholder={currentJobId ? "Type feedback..." : "Run a pipeline first"}
            disabled={!currentJobId || sending}
            className="flex-1 px-2 py-1.5 text-xs rounded border border-border bg-background text-foreground focus:outline-none focus:border-accent disabled:opacity-50"
          />
          <button
            onClick={handleSend}
            disabled={!currentJobId || !input.trim() || sending}
            className="p-1.5 rounded bg-accent hover:bg-accent-hover text-black disabled:opacity-50 transition-colors"
          >
            {sending ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
            ) : (
              <Send className="w-3.5 h-3.5" />
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
