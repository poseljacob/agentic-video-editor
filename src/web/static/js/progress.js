/**
 * AVE Studio — Progress View (US-005)
 *
 * Alpine.js v3 component factory that drives the live pipeline progress
 * dashboard. Registers itself as `window.progressView` so the template can
 * bind via `x-data="progressView()"` without needing ES modules or a build
 * step — same pattern as brief-builder.js (US-004).
 *
 * Responsibilities:
 *   1. Receive a job id (already created by brief-builder) and open exactly
 *      one WebSocket to /ws/jobs/{job_id}.
 *   2. Parse the three wire message shapes emitted by src/web/routes/ws.py:
 *        - {"type": "progress", "line": "...", "timestamp": "..."}
 *        - {"type": "status",   "status": "completed"|"failed", "error"?: "..."}
 *        - {"type": "result",   "data": { ...serialized PipelineResult... }}
 *   3. Render progress lines in a terminal-style scrolling container with
 *      auto-scroll to bottom.
 *   4. Parse `[pipeline] step N — {agent}` lines and highlight the active
 *      step in a Director → TrimRefiner → Editor → Reviewer indicator.
 *   5. Transition spinner → checkmark on completion, X on failure.
 *   6. On completion, derive the playable video src from
 *      result.data.final_video_path (absolute fs path → /media/ url).
 *   7. On failure, surface the error plus the last 10 log lines.
 *   8. Clean up the WebSocket in the Alpine destroy() hook and whenever a
 *      new job is watched.
 */
(function () {
  "use strict";

  /**
   * Canonical step order for the pipeline. The indicator renders these in
   * order and highlights whichever one is currently running (or marks it
   * done once a later step begins).
   *
   * These labels are DISPLAY-ONLY and are independent of the lowercase
   * agent names emitted by the runner (e.g. `director`, `trim_refiner`,
   * `editor`, `reviewer`). The indicator is keyed on the step NUMBER from
   * the `[pipeline] step N — {agent}` log line, NOT on string-matching the
   * agent name — so renaming an agent on the Python side does not break
   * the progress dashboard. The step number is 0-based and maps directly
   * to `STEP_NAMES[N]`.
   */
  const STEP_NAMES = ["Director", "TrimRefiner", "Editor", "Reviewer"];

  /**
   * Regex for the progress lines the pipeline runner emits to announce a
   * step boundary. The canonical form uses an em-dash (U+2014) between the
   * step number and the agent name, but older runs used an ASCII dash and
   * some environments may substitute it on copy/paste — accept all three
   * dash variants so the indicator keeps working without coupling the UI
   * to a single character.
   *
   * Note: only the "step N — {agent} starting" shape is matched. The
   * step-END log (`[pipeline] step — {agent} done in ...`) intentionally
   * omits the number, so this regex won't match it — that's correct,
   * step-start is the authoritative boundary for the indicator.
   *
   * Example matches:
   *   "[pipeline] step 0 — director starting"
   *   "[pipeline] step 1 - trim_refiner starting"
   *   "[pipeline] step 2 -- editor starting"
   */
  const STEP_LINE_RE = /\[pipeline\]\s+step\s+(\d+)\s*[—–\-]+\s*([A-Za-z_][\w]*)/;

  /**
   * WebSocket close code the server uses for unknown job ids. Matches the
   * UNKNOWN_JOB_CLOSE_CODE constant in src/web/routes/ws.py.
   */
  const UNKNOWN_JOB_CLOSE_CODE = 4004;

  /**
   * How many tail lines to show alongside the error banner on failure.
   * Matches the acceptance-criteria "last 10 progress lines for context".
   */
  const ERROR_TAIL_LINES = 10;

  /**
   * Normalize an absolute pipeline `final_video_path` into a URL the
   * browser can load through the FastAPI `/media/` static mount.
   *
   * The runner stores an absolute filesystem path (see
   * src/pipeline/runner.py PipelineResult.final_video_path) — e.g.
   *   /Users/.../agentic-video-editor/output/final/slug.mp4
   *
   * FastAPI mounts the repo's ./output directory at /media (see
   * src/web/app.py), so we just split on the first "output/" segment and
   * prepend "/media/". If the input already looks like a /media/ URL, pass
   * it straight through.
   *
   * Returns an empty string when the input is missing or unparseable — the
   * caller decides how to surface that to the user.
   */
  function toMediaUrl(rawPath) {
    if (!rawPath || typeof rawPath !== "string") return "";
    if (rawPath.startsWith("/media/")) return rawPath;
    const marker = "output/";
    const idx = rawPath.indexOf(marker);
    if (idx === -1) return "";
    const tail = rawPath.slice(idx + marker.length);
    if (!tail) return "";
    return "/media/" + tail;
  }

  /**
   * Build the initial Alpine state object. Exposed as a factory so each
   * `x-data="progressView()"` binding gets isolated state — important
   * because the Alpine root uses x-show to hide/show this section.
   */
  function progressView() {
    return {
      // --- Job tracking ---
      jobId: "",
      // One of: "idle" | "connecting" | "running" | "completed" | "failed"
      status: "idle",
      error: "",

      // --- Progress log (array of {line, timestamp}) ---
      entries: [],

      // --- Step indicator state ---
      stepNames: STEP_NAMES,
      /**
       * Index of the step currently running, 0-based to match the
       * `[pipeline] step N — {agent}` numbering the runner emits (which
       * comes from `enumerate(manifest.steps)` — see src/pipeline/runner.py).
       *
       * Values:
       *   -1                    → no step seen yet (all steps pending)
       *    0..STEP_NAMES.length-1 → that step is active
       *    STEP_NAMES.length    → pipeline completed (all steps done)
       */
      currentStep: -1,

      // --- Pipeline result (once status === "completed") ---
      result: null,
      videoSrc: "",

      // --- WebSocket bookkeeping ---
      _socket: null,
      _terminalSeen: false,

      /**
       * Alpine lifecycle: start watching the jobId declared via x-init.
       * The template passes the id through the shared Alpine root state,
       * so this hook is what actually opens the socket.
       */
      init() {
        this.$watch("jobId", (next, prev) => {
          // Any change (including clearing) tears down the old socket first.
          this._teardownSocket();
          if (next && next !== prev) {
            this._reset();
            this._connect(next);
          }
        });
        // If the template already set jobId before init fired, honor it.
        if (this.jobId) {
          this._reset();
          this._connect(this.jobId);
        }
      },

      /**
       * Alpine destroy hook — closes the socket when the component unmounts
       * (e.g. the user navigates away from the progress section). Alpine
       * calls this automatically for components inside an `x-if` that flips
       * to false; we also explicitly call `_teardownSocket` from the watcher
       * so a jobId change has the same effect.
       */
      destroy() {
        this._teardownSocket();
      },

      /**
       * Reset local state for a fresh job. Clears the log, step indicator,
       * result payload, and error banner so the UI doesn't accidentally
       * show stale data from a previous run.
       */
      _reset() {
        this.status = "connecting";
        this.error = "";
        this.entries = [];
        this.currentStep = -1;
        this.result = null;
        this.videoSrc = "";
        this._terminalSeen = false;
      },

      /**
       * Open a WebSocket to /ws/jobs/{jobId}. Uses the same scheme/host as
       * the current page so dev (http → ws) and any future TLS deploy
       * (https → wss) both work without reconfiguration.
       */
      _connect(jobId) {
        const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
        const url = `${proto}//${window.location.host}/ws/jobs/${encodeURIComponent(
          jobId
        )}`;

        let socket;
        try {
          socket = new WebSocket(url);
        } catch (err) {
          // Invalid URL (shouldn't happen), CSP violation, etc. Surface it.
          this.status = "failed";
          this.error =
            err && err.message
              ? `Could not open WebSocket: ${err.message}`
              : "Could not open WebSocket.";
          return;
        }

        this._socket = socket;

        socket.addEventListener("message", (event) => {
          this._handleMessage(event.data);
        });

        socket.addEventListener("error", () => {
          // Browser doesn't give us a usable error payload on the "error"
          // event — the `close` handler below is where we actually decide
          // whether this was a failure or a clean terminal close.
          // eslint-disable-next-line no-console
          console.warn("[progress] WebSocket error event fired");
        });

        socket.addEventListener("close", (event) => {
          this._handleClose(event);
        });
      },

      /**
       * Parse and dispatch a single JSON frame from the server. Any frame
       * we don't recognize is dropped (with a console warning) rather than
       * crashing the view — we'd rather keep the connection alive than
       * show a broken UI for a harmless future extension.
       */
      _handleMessage(raw) {
        let payload;
        try {
          payload = JSON.parse(raw);
        } catch (err) {
          // eslint-disable-next-line no-console
          console.warn("[progress] Dropping non-JSON WebSocket frame", raw);
          return;
        }
        if (!payload || typeof payload !== "object") return;

        switch (payload.type) {
          case "progress":
            this._appendProgress(payload);
            // The first progress frame also flips us from "connecting"
            // into "running" so the spinner is correct.
            if (this.status === "connecting") {
              this.status = "running";
            }
            break;
          case "status":
            this._handleStatus(payload);
            break;
          case "result":
            this.result = payload.data || null;
            this.videoSrc = this._deriveVideoSrc(this.result);
            break;
          default:
            // eslint-disable-next-line no-console
            console.warn("[progress] Unknown message type", payload.type);
        }
      },

      /**
       * Append a progress entry to the rolling log, scroll the terminal
       * pane to the bottom, and update the step indicator if the line
       * matches the canonical `[pipeline] step N — {agent}` format.
       */
      _appendProgress(payload) {
        const line = typeof payload.line === "string" ? payload.line : "";
        const timestamp =
          typeof payload.timestamp === "string" ? payload.timestamp : "";
        this.entries.push({ line, timestamp });
        this._parseStep(line);
        // Defer scroll until Alpine has had a chance to render the new row.
        this.$nextTick(() => {
          const el = this.$refs.logPane;
          if (el) {
            el.scrollTop = el.scrollHeight;
          }
        });
      },

      /**
       * Update `currentStep` when a step boundary line is seen. We key on
       * the step NUMBER rather than the agent name so the indicator stays
       * in sync even if a pipeline run ever includes an agent name we
       * don't display (e.g. a future diagnostic step) — the number is the
       * authoritative ordering signal.
       *
       * The runner emits 0-based indices via `enumerate(manifest.steps)`,
       * so step 0 = Director, 1 = TrimRefiner, 2 = Editor, 3 = Reviewer.
       * Any number beyond STEP_NAMES.length-1 is clamped to the final
       * display slot so a 5th step (if one is ever added before this file
       * is updated) still marks the four known steps as done.
       */
      _parseStep(line) {
        if (!line) return;
        const match = line.match(STEP_LINE_RE);
        if (!match) return;
        const stepNumber = parseInt(match[1], 10);
        if (!Number.isFinite(stepNumber) || stepNumber < 0) return;
        this.currentStep = Math.min(stepNumber, STEP_NAMES.length - 1);
      },

      /**
       * Handle a `{type: "status", ...}` frame from the server. Sets the
       * terminal state so the spinner disappears and -- on failure -- stores
       * the server-provided error message for the alert banner.
       *
       * We do NOT clear `currentJobId` here even though this marks the
       * logical end of the job. The parent Alpine root binds `jobId` on
       * this component to `currentJobId`, so clearing it synchronously
       * would fire the jobId watcher, which calls _teardownSocket() --
       * closing the socket BEFORE the server's follow-up `result` frame
       * is delivered. The net effect would be a completed job with no
       * video src or review scores. Instead, we defer the Run-button
       * re-enable to _handleClose, which runs after the server has
       * finished sending status -> result -> close in that order.
       *
       * Also note: we do NOT clear `this.result` or `this.videoSrc`
       * here because the completed state still renders the rendered
       * video and review panel from them.
       */
      _handleStatus(payload) {
        const status = payload.status;
        if (status === "completed") {
          this._terminalSeen = true;
          this.status = "completed";
          // Once the pipeline declares itself done, mark all steps as
          // complete so the indicator doesn't get stuck showing the final
          // step as "active". Setting currentStep === STEP_NAMES.length
          // means every `index < currentStep` check in stepState() is
          // true, so all steps render as "done".
          this.currentStep = STEP_NAMES.length;
        } else if (status === "failed") {
          this._terminalSeen = true;
          this.status = "failed";
          this.error =
            typeof payload.error === "string" && payload.error
              ? payload.error
              : "Pipeline failed with no error message.";
        }
      },

      /**
       * Clear the Alpine root scope's `currentJobId` so the brief-builder
       * Run button re-enables after a terminal state. Alpine's nested
       * scope inheritance makes `this.currentJobId` refer to the parent
       * when the child doesn't define its own — assigning it writes
       * through to the root, which is where the Run button's
       * `:disabled="... || !!currentJobId"` check reads from.
       *
       * The brief-builder → progressView bridge uses `promotedJobId` on
       * the root (see index.html x-effect) to avoid re-promoting the same
       * completed job after we clear currentJobId. We do NOT touch
       * `promotedJobId` here — leaving it set to the finished job id
       * is exactly what stops the x-effect from firing again.
       */
      _clearRootJobId() {
        // Guard for the standalone case (no parent scope) — Alpine
        // components nested inside <main x-data=...> always see the root
        // fields via scope inheritance, but a future unit-test harness
        // might render progressView in isolation.
        if (typeof this.currentJobId !== "undefined") {
          this.currentJobId = "";
        }
      },

      /**
       * Interpret the WebSocket close event. This is the single point where
       * we re-enable the Run button for ANY terminal outcome -- clean
       * completion, clean failure, 4004 unknown job, or an abnormal
       * disconnect. Doing it here (rather than in _handleStatus) guarantees
       * the server has already delivered status -> result -> close before
       * we clear `currentJobId`, so the jobId watcher-driven
       * _teardownSocket() never fires mid-stream and never drops the
       * `result` frame that carries the video src + review scores.
       *
       * Close outcomes:
       *   - _terminalSeen == true  -> clean wrap-up, do not overwrite
       *     status/error set by _handleStatus
       *   - code == 4004           -> server's unknown-job signal
       *   - anything else          -> abnormal disconnect before a
       *     terminal status frame arrived
       *
       * In all three cases we call _clearRootJobId() so the brief-builder
       * Run button re-enables. Previously the abnormal-close branches
       * (4004 and generic) set `status: "failed"` without clearing
       * currentJobId, which wedged the Run button permanently.
       */
      _handleClose(event) {
        // Drop the reference so `_teardownSocket` doesn't try to close a
        // socket that's already closed.
        this._socket = null;

        if (this._terminalSeen) {
          // Clean path: status + result already delivered before the
          // close frame. Re-enable the Run button now that the server is
          // definitively done with this job.
          this._clearRootJobId();
          return;
        }

        if (event.code === UNKNOWN_JOB_CLOSE_CODE) {
          this.status = "failed";
          this.error = `Unknown job id: ${this.jobId}`;
          this._clearRootJobId();
          return;
        }

        // Normal WebSocket close code per RFC 6455 is 1000. Anything else
        // (or a 1000 without a terminal status) means the stream died
        // before the pipeline finished.
        this.status = "failed";
        this.error =
          "WebSocket closed before the pipeline reported a terminal status" +
          (event.code ? ` (code ${event.code})` : "") +
          ".";
        this._clearRootJobId();
      },

      /**
       * Tear down the active WebSocket, if any. Safe to call repeatedly.
       * Uses close code 1000 so the server-side handler sees a clean
       * disconnect rather than an abnormal failure.
       */
      _teardownSocket() {
        const socket = this._socket;
        if (!socket) return;
        this._socket = null;
        try {
          // Only call close() if the socket hasn't already transitioned
          // to CLOSING/CLOSED — close() is a no-op in those states per
          // the spec, but the try/catch keeps us safe against older
          // browsers that throw InvalidStateError.
          if (
            socket.readyState === WebSocket.OPEN ||
            socket.readyState === WebSocket.CONNECTING
          ) {
            socket.close(1000, "client navigated away");
          }
        } catch (_err) {
          // Best-effort — if close throws we can't do anything useful.
        }
      },

      /**
       * Turn a `result.data` payload into the URL the <video> element
       * should load. Prefers `final_video_path`; falls back to an empty
       * string if the field is missing so the template can show its
       * "video unavailable" fallback instead of a broken <source>.
       */
      _deriveVideoSrc(result) {
        if (!result || typeof result !== "object") return "";
        const url = toMediaUrl(result.final_video_path);
        if (!url) {
          // eslint-disable-next-line no-console
          console.warn(
            "[progress] result.data.final_video_path missing or unparseable — video will be unavailable",
            result.final_video_path
          );
        }
        return url;
      },

      // ---------------------------------------------------------------- #
      // Template helpers                                                  #
      // ---------------------------------------------------------------- #

      /**
       * True while we're waiting on the WebSocket to deliver a terminal
       * status. Used to drive spinner visibility and the Run button's
       * disabled state in the parent component.
       */
      isRunning() {
        return this.status === "connecting" || this.status === "running";
      },

      /** Has the job finished successfully? */
      isCompleted() {
        return this.status === "completed";
      },

      /** Has the job failed? */
      isFailed() {
        return this.status === "failed";
      },

      /**
       * Step indicator classification for a 0-based step index (matching
       * the runner's `[pipeline] step N —` numbering).
       *   - "done"    → an earlier step (render as a check / filled dot)
       *   - "active"  → the current step (render with a spinner / ring)
       *   - "pending" → hasn't started yet (muted)
       *   - "failed"  → in-flight step at the moment of pipeline failure
       *
       * Treats currentStep === -1 (no step seen) as "all pending" and
       * currentStep === STEP_NAMES.length as "all done" — see
       * _handleStatus for why we bump currentStep to STEP_NAMES.length
       * on completion.
       */
      stepState(index) {
        if (this.status === "failed" && this.currentStep === index) {
          // Show the step that was in flight at the moment of failure as
          // "failed" so the user can see where the pipeline stopped.
          return "failed";
        }
        if (this.currentStep < 0) return "pending";
        if (index < this.currentStep) return "done";
        if (index === this.currentStep) return "active";
        return "pending";
      },

      /**
       * Return the last N progress lines, for the failure banner context.
       * Kept as a method (not a computed property) because Alpine doesn't
       * memoize and we want cheap re-evaluation each render.
       */
      tailLines(n) {
        const count = Number.isFinite(n) ? n : ERROR_TAIL_LINES;
        if (this.entries.length <= count) return this.entries;
        return this.entries.slice(this.entries.length - count);
      },

      /**
       * Summary score lookup for the review panel. Returns a display
       * string ("n/a" if missing) so the template can `x-text` each
       * dimension without an x-if wrapper for every value.
       */
      reviewValue(key) {
        if (!this.result || !this.result.review) return "n/a";
        const value = this.result.review[key];
        if (value === undefined || value === null) return "n/a";
        return typeof value === "number" ? value.toFixed(2) : String(value);
      },
    };
  }

  // Expose as a global so Alpine's x-data="progressView()" can find it.
  // No ES-module export needed — this script is loaded via a plain
  // <script defer> tag that executes BEFORE the Alpine CDN script, just
  // like brief-builder.js (see index.html for the ordering rationale).
  window.progressView = progressView;
})();
