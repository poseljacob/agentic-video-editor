/**
 * AVE Studio -- Chat Feedback (US-007)
 *
 * Alpine.js v3 component factory that powers the post-video chat refinement
 * view. Registers itself as `window.chatView` so the template can bind via
 * `x-data="chatView()"` without needing ES modules or a build step -- same
 * pattern as brief-builder.js / progress.js / video-player.js / review-chart.js.
 *
 * Responsibilities
 * ----------------
 *
 *  1. Seed the chat session from the shared root scope's `promotedJobId`
 *     (or `currentJobId` as a fallback) so the user who just finished a
 *     pipeline run can immediately start iterating on it.
 *  2. Fetch the initial job via `GET /api/jobs/{id}` to populate the first
 *     version entry (video + review + any reviewer-generated feedback) and
 *     the initial transcript.
 *  3. Accept free-text feedback, POST it to
 *     `POST /api/jobs/{currentJobId}/feedback`, and surface the 404 / 409 /
 *     network errors as system-chat messages (not crashes).
 *  4. Open a WebSocket to `/ws/jobs/{new-job-id}` for the feedback re-run and
 *     stream progress lines + a four-step indicator (mirroring the main
 *     progressView component but inline in the chat panel).
 *  5. On terminal completion, fetch the new job, append a system chat
 *     response (new video + new ReviewScore summary), push the new render
 *     into the `versions` array, and flip `activeVersionId` so the video
 *     player element swaps to the new source.
 *  6. Track every prior render in `versions` so the user can flip back to
 *     an older cut via a dropdown -- selecting an older version mutates
 *     `activeVersionId` only, so chat.js owns the player source entirely
 *     and never mutates the shared root scope's `promotedJobId`.
 *  7. Keep the full accumulated feedback history visible in the transcript
 *     across multiple rounds -- the backend already accumulates
 *     `feedback_history` on each re-run, we just render it chronologically.
 *  8. Disable the send button while a feedback re-run is streaming so the
 *     user cannot pile on and race the server.
 *
 * Scope & integration
 * -------------------
 *
 * This component is intentionally self-contained. It does NOT touch
 * progress.js, review-chart.js, video-player.js, or brief-builder.js -- it
 * speaks to the same REST + WebSocket endpoints the other components use
 * and renders its own video / log / chart surfaces inline. The only shared
 * state it reads is the Alpine root's `promotedJobId` (the last job the
 * brief-builder promoted into the review/chat views), which it mirrors
 * into its own `rootJobId` via `$watch` so switching briefs resets the
 * chat session to the new job.
 */
(function () {
  "use strict";

  /**
   * Display labels for the feedback re-run step indicator. The order
   * matches the backend's `[feedback-rerun] step N -- {agent}` framing
   * lines emitted by `JobRegistry._run_feedback_rerun_sync`. Steps are
   * 1-based in the progress log (step 1 = director, 2 = trim_refiner,
   * 3 = editor, 4 = reviewer), so we subtract 1 to index into this array.
   */
  const FEEDBACK_STEP_NAMES = ["Director", "TrimRefiner", "Editor", "Reviewer"];

  /**
   * Regex for the feedback-rerun step boundary lines. Matches lines of
   * the form `[feedback-rerun] step N -- {agent}`. The number is the
   * authoritative ordering signal -- the indicator keys on it (not the
   * agent name), so a future rename on the Python side does not break
   * the UI. Accepts any run of dashes between the number and agent name
   * in case a future refactor uses em-dashes or single hyphens.
   */
  const FEEDBACK_STEP_LINE_RE =
    /\[feedback-rerun\]\s+step\s+(\d+)\s*[\u2014\u2013\-]+\s*([A-Za-z_][\w]*)/;

  /**
   * WebSocket close code the server uses for unknown job ids. Matches the
   * UNKNOWN_JOB_CLOSE_CODE constant in src/web/routes/ws.py. Must stay in
   * sync with progress.js's copy.
   */
  const UNKNOWN_JOB_CLOSE_CODE = 4004;

  /**
   * Delay between poll attempts in `loadInitialJob` when the parent job
   * is still pending or running. The poll stops as soon as the parent
   * reaches a terminal status, or when `_resetSession` promotes a new
   * root job id (so a new brief submit cancels any in-flight poll).
   */
  const INITIAL_JOB_POLL_MS = 1500;

  /**
   * ReviewScore dimensions we surface in the chat response message so the
   * user can see at a glance what changed between rounds. Same ordering
   * as video-player.js / review-chart.js / progress.js -- `overall` last.
   */
  const REVIEW_DIMENSIONS = [
    { key: "adherence", label: "Adherence" },
    { key: "pacing", label: "Pacing" },
    { key: "visual_quality", label: "Visual quality" },
    { key: "watchability", label: "Watchability" },
    { key: "overall", label: "Overall" },
  ];

  /**
   * Normalize an absolute pipeline `final_video_path` into a URL the
   * browser can load through the FastAPI `/media/` static mount. Copied
   * verbatim from progress.js::toMediaUrl -- we keep a local copy instead
   * of importing so chat.js has no load-order coupling with progress.js.
   *
   * Returns an empty string when the input is missing or unparseable --
   * the caller renders a "video unavailable" fallback in that case.
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
   * Format a ReviewScore dimension value for display. Mirrors the helper
   * used in progress.js / video-player.js so the chat panel feels
   * consistent with the rest of the app.
   */
  function formatScore(review, key) {
    if (!review) return "n/a";
    const value = review[key];
    if (value === undefined || value === null) return "n/a";
    return typeof value === "number" ? value.toFixed(2) : String(value);
  }

  /**
   * Build a compact multi-line summary of a ReviewScore for a system
   * chat message. Used when the feedback re-run completes so the user
   * can immediately compare scores against the prior render.
   */
  function summarizeReview(review) {
    if (!review) return "";
    const parts = REVIEW_DIMENSIONS.map((dim) => {
      return `${dim.label}: ${formatScore(review, dim.key)}`;
    });
    return parts.join(" | ");
  }

  /**
   * Build the initial Alpine state for a chatView instance. Factory (not
   * singleton) so each `x-data="chatView()"` binding gets isolated state.
   * The chat section is hidden via x-show on its parent, so Alpine keeps
   * a single instance alive across view flips -- which is exactly what
   * lets the user switch between Chat / Review / Progress tabs without
   * losing the transcript or the WebSocket stream.
   */
  function chatView() {
    return {
      // --- Job tracking ---
      /**
       * UUID of the very first job the chat session was seeded with. Set
       * once from $root.promotedJobId (or currentJobId) and used to detect
       * when the user submitted a NEW brief so the chat transcript can be
       * reset to the new job.
       */
      rootJobId: "",

      /**
       * UUID of the most recently COMPLETED job in the chain. New
       * feedback POSTs go to `POST /api/jobs/{currentJobId}/feedback`,
       * and a successful re-run flips this to the child job id so the
       * next feedback round chains off the latest cut.
       */
      currentJobId: "",

      /**
       * UUID of the child job currently streaming over the WebSocket, if
       * any. Non-empty while a feedback re-run is in flight; cleared when
       * the WebSocket reports a terminal status.
       */
      inFlightJobId: "",

      // --- Chat input + transcript ---
      /**
       * Two-way bound to the chat textarea. Cleared on successful
       * submit so the user can immediately start typing the next message.
       */
      message: "",

      /**
       * Chronological chat transcript. Each entry is one of:
       *   - {id, role: 'user',   text, timestamp}
       *   - {id, role: 'system', text, videoPath?, videoUrl?, review?, jobId?, timestamp}
       *   - {id, role: 'error',  text, timestamp}
       * `id` is a monotonic counter so Alpine's :key is stable.
       */
      transcript: [],

      /**
       * All renders produced in this chat session, oldest first. The
       * initial entry comes from the rootJobId fetch; each successful
       * feedback re-run appends a new entry. Shape:
       *   {jobId, videoPath, videoUrl, review, createdAt, label}
       */
      versions: [],

      /**
       * Job id of the version currently displayed in the <video> player.
       * Mutated by `selectVersion(id)` and auto-updated to the newest
       * entry whenever a feedback re-run completes. The template drives
       * the <video src> off this field so the browser picks up the swap
       * without any custom event plumbing.
       */
      activeVersionId: "",

      // --- Progress streaming state (only used during an in-flight re-run) ---
      /**
       * Live progress lines from the current feedback re-run WebSocket.
       * Cleared when a new re-run starts so the panel only shows the
       * latest stream.
       */
      progressLines: [],

      /**
       * 1-based step index for the feedback re-run, matching the
       * `[feedback-rerun] step N -- {agent}` framing the backend emits.
       * 0 = no step seen yet, 1..4 = that step is active,
       * 5 = all steps complete. Drives the inline step indicator.
       */
      feedbackStep: 0,

      /**
       * One of:
       *   - "idle"      : no feedback re-run has been submitted yet, or
       *                   the last one finished.
       *   - "running"   : WebSocket open, streaming progress.
       *   - "completed" : last re-run succeeded.
       *   - "failed"    : last re-run failed.
       */
      progressStatus: "idle",

      /**
       * Last re-run error message, if any. Shown in the inline progress
       * banner in addition to the error message added to the transcript.
       */
      errorMessage: "",

      /**
       * Flag set while the first `GET /api/jobs/{id}` is in flight so
       * the template can show a "loading chat session" state instead of
       * a blank transcript.
       */
      loadingInitial: false,

      // --- Template helper ---
      /** Dimension list exposed for the inline review summary cards. */
      dimensions: REVIEW_DIMENSIONS,

      /** Video-player fallback flag. Flips true on the <video> error event. */
      videoLoadError: false,

      // --- Bookkeeping ---
      /** Active WebSocket instance, or null when idle. */
      _socket: null,

      /** Tracks whether a terminal status frame was seen before the close. */
      _terminalSeen: false,

      /**
       * Set true by `_teardownSocket` right before calling `socket.close()`
       * so the trailing `close` event fired by the browser after an
       * explicit teardown (e.g. from `_resetSession` on a mid-stream brief
       * submit) does NOT fall through to `_handleClose`'s abnormal-close
       * branch and write a spurious "WebSocket closed before terminal
       * status" error into the freshly reset chat session.
       */
      _suppressNextClose: false,

      /** Monotonic counter for transcript entry ids (stable :key values). */
      _nextTranscriptId: 1,

      // ---------------------------------------------------------------- #
      // Lifecycle                                                         #
      // ---------------------------------------------------------------- #

      /**
       * Alpine lifecycle hook. Kept as a no-op placeholder -- seeding
       * happens via the `x-effect="seedFromRoot(promotedJobId, currentJobId)"`
       * binding on the wrapper element (see index.html) because Alpine 3
       * does NOT expose the parent scope via `this` inside a nested
       * component's `init()` method (see alpinejs/alpine#1711). The
       * effect expression has the merged scope chain and forwards the
       * root values into `seedFromRoot` below.
       *
       * Alpine v3 invokes `init()` automatically on mount -- the template
       * does NOT need `x-init="init()"` (and adding one would double-fire
       * the effect).
       */
      init() {
        // Intentionally empty: seeding is driven by `x-effect` below.
      },

      /**
       * Forward the root scope's `promotedJobId` / `currentJobId` into
       * the local chatView state. Called from the wrapper's `x-effect`
       * binding, so it runs on mount AND on every subsequent change to
       * either root-scope value. Idempotent: only resets the session
       * when a NEW job id lands, so re-running on unrelated reactive
       * updates is cheap and does not stomp on an in-flight re-run.
       */
      seedFromRoot(promotedJobId, currentJobIdArg) {
        // Prefer promotedJobId (stable, never cleared on terminal state)
        // over currentJobId (cleared by progressView to re-enable the
        // Run button). If neither is set, leave rootJobId empty and the
        // empty-state message in the template will explain what to do.
        const promoted = typeof promotedJobId === "string" ? promotedJobId : "";
        const current =
          typeof currentJobIdArg === "string" ? currentJobIdArg : "";
        const next = promoted || current || "";
        if (!next) return;
        if (next === this.rootJobId) return;
        this._resetSession(next);
        this.loadInitialJob(next);
      },

      /**
       * Alpine destroy hook. Closes any live WebSocket so the chat view
       * does not leak a stream when the component unmounts (Alpine calls
       * this when an ancestor x-if flips false). We also call
       * `_teardownSocket` from `_handleClose` / `_resetSession` so most
       * real teardown already happens before destroy runs.
       */
      destroy() {
        this._teardownSocket();
      },

      /**
       * Clear ALL chat-scoped state for a fresh root job. Called when
       * the root scope promotes a new job id (e.g. second brief submit)
       * so the user does not see a stale transcript from the prior job.
       */
      _resetSession(nextJobId) {
        this._teardownSocket();
        this.rootJobId = nextJobId;
        this.currentJobId = nextJobId;
        this.inFlightJobId = "";
        this.message = "";
        this.transcript = [];
        this.versions = [];
        this.activeVersionId = "";
        this.progressLines = [];
        this.feedbackStep = 0;
        this.progressStatus = "idle";
        this.errorMessage = "";
        this.videoLoadError = false;
        this._terminalSeen = false;
      },

      /**
       * Fetch `GET /api/jobs/{jobId}` and populate the first version +
       * opening transcript entry. Called from `init()` on session seed
       * and again whenever the root scope promotes a new job.
       */
      async loadInitialJob(jobId) {
        if (!jobId) return;
        this.loadingInitial = true;
        let response;
        try {
          response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, {
            headers: { Accept: "application/json" },
          });
        } catch (err) {
          this.loadingInitial = false;
          const detail =
            err && err.message ? err.message : "network error";
          this._appendError(
            `Could not load initial job ${jobId}: ${detail}`
          );
          return;
        }
        this.loadingInitial = false;

        if (!response.ok) {
          let detail = "";
          try {
            const body = await response.json();
            if (body && typeof body.detail === "string") {
              detail = body.detail;
            }
          } catch (_err) {
            // non-JSON body -- fall through to status text.
          }
          this._appendError(
            detail ||
              `Failed to load job ${jobId} (${response.status} ${response.statusText}).`
          );
          return;
        }

        let payload;
        try {
          payload = await response.json();
        } catch (_err) {
          this._appendError("Initial job response was not valid JSON.");
          return;
        }
        if (!payload || typeof payload !== "object") {
          this._appendError("Initial job response was empty or malformed.");
          return;
        }

        // If the parent job has not finished yet, poll until it does.
        // Committing the initial Version 1 entry against a pending
        // payload would cache an empty videoUrl forever because the
        // `seedFromRoot` guard prevents a second seed for the same id.
        // The poll self-cancels if `_resetSession` promotes a new root
        // job id (e.g. the user submits a new brief).
        const status = typeof payload.status === "string" ? payload.status : "";
        if (status !== "completed" && status !== "failed") {
          if (jobId !== this.rootJobId) return;
          this.loadingInitial = true;
          setTimeout(() => {
            if (jobId !== this.rootJobId) return;
            this.loadInitialJob(jobId);
          }, INITIAL_JOB_POLL_MS);
          return;
        }

        const result = payload.result && typeof payload.result === "object"
          ? payload.result
          : null;
        const videoPath =
          result && typeof result.final_video_path === "string"
            ? result.final_video_path
            : "";
        const videoUrl = toMediaUrl(videoPath);
        const review =
          result && result.review && typeof result.review === "object"
            ? result.review
            : null;

        const version = {
          jobId,
          videoPath,
          videoUrl,
          review,
          createdAt:
            typeof payload.completed_at === "string"
              ? payload.completed_at
              : typeof payload.created_at === "string"
              ? payload.created_at
              : "",
          label: "Version 1 (initial render)",
        };
        this.versions = [version];
        this.activeVersionId = jobId;
        this.videoLoadError = false;

        // Opening system message: describe the initial render + any
        // reviewer feedback from the first run. Users seeing the chat
        // tab for the first time should see SOME content, even if the
        // job has no video / no review.
        const openingLines = ["Initial render loaded."];
        if (review && typeof review.feedback === "string" && review.feedback) {
          openingLines.push(`Reviewer feedback: ${review.feedback}`);
        }
        const scoreSummary = summarizeReview(review);
        if (scoreSummary) {
          openingLines.push(`Scores -- ${scoreSummary}`);
        }
        // Also surface any pre-existing feedback_history from the parent
        // job (e.g. the reviewer retry loop already produced some
        // feedback strings). These become user-style entries in the
        // transcript so later rounds see the full chain.
        const history =
          payload && Array.isArray(payload.feedback_history)
            ? payload.feedback_history.filter((s) => typeof s === "string")
            : [];

        this.transcript = [];
        for (const entry of history) {
          this._appendTranscript({
            role: "user",
            text: entry,
            timestamp: "",
          });
        }
        this._appendTranscript({
          role: "system",
          text: openingLines.join("\n"),
          videoPath,
          videoUrl,
          review,
          jobId,
          timestamp: version.createdAt,
        });
      },

      // ---------------------------------------------------------------- #
      // Feedback submit                                                   #
      // ---------------------------------------------------------------- #

      /**
       * POST the chat textarea contents to the feedback endpoint and,
       * on 202, open a WebSocket to the new child job id to stream its
       * progress. Surfaces 404 / 409 / network errors as system-chat
       * messages instead of crashing the Alpine component.
       */
      async sendFeedback() {
        const trimmed = (this.message || "").trim();
        if (!trimmed) return;
        if (this.isInFlight()) return;
        if (!this.currentJobId) {
          this._appendError(
            "No job to attach feedback to. Run a pipeline first."
          );
          return;
        }

        // Optimistically append the user's message to the transcript so
        // they see it land immediately. If the POST fails we leave it in
        // place + append an error message so the failure is anchored in
        // the same conversational flow.
        const userEntry = this._appendTranscript({
          role: "user",
          text: trimmed,
          timestamp: new Date().toISOString(),
        });
        this.message = "";

        let response;
        try {
          response = await fetch(
            `/api/jobs/${encodeURIComponent(this.currentJobId)}/feedback`,
            {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                Accept: "application/json",
              },
              body: JSON.stringify({ message: trimmed }),
            }
          );
        } catch (err) {
          const detail =
            err && err.message ? err.message : "network error";
          this._appendError(`Could not submit feedback: ${detail}`);
          return;
        }

        if (response.status === 404) {
          let detail = "";
          try {
            const body = await response.json();
            if (body && typeof body.detail === "string") {
              detail = body.detail;
            }
          } catch (_err) {
            // non-JSON body -- fall back to status text
          }
          this._appendError(
            detail ||
              `Parent job ${this.currentJobId} was not found (404).`
          );
          return;
        }

        if (response.status === 409) {
          let detail = "";
          try {
            const body = await response.json();
            if (body && typeof body.detail === "string") {
              detail = body.detail;
            }
          } catch (_err) {
            // fall through
          }
          this._appendError(
            detail ||
              "Parent job is not ready for feedback (must be completed with an edit plan)."
          );
          return;
        }

        if (!response.ok) {
          this._appendError(
            `Feedback submit failed (${response.status} ${response.statusText}).`
          );
          return;
        }

        let payload;
        try {
          payload = await response.json();
        } catch (_err) {
          this._appendError("Feedback response was not valid JSON.");
          return;
        }

        if (!payload || typeof payload.job_id !== "string" || !payload.job_id) {
          this._appendError("Feedback response missing a job_id.");
          return;
        }

        // Acknowledge the submit in the transcript so the user sees
        // what's happening even before the first progress line lands.
        this._appendTranscript({
          role: "system",
          text: `Queued feedback re-run (job ${payload.job_id}). Streaming progress...`,
          jobId: payload.job_id,
          timestamp: new Date().toISOString(),
        });

        // Reset the in-flight progress strip and open the WebSocket.
        this.inFlightJobId = payload.job_id;
        this.progressLines = [];
        this.feedbackStep = 0;
        this.progressStatus = "running";
        this.errorMessage = "";
        this._terminalSeen = false;
        this._openProgressStream(payload.job_id);

        // Suppress the unused-variable lint (userEntry is returned for
        // future use, e.g. editing / retry semantics).
        void userEntry;
      },

      // ---------------------------------------------------------------- #
      // WebSocket streaming                                               #
      // ---------------------------------------------------------------- #

      /**
       * Open a WebSocket to `/ws/jobs/{newJobId}` and start streaming
       * progress. Same wire format as progress.js -- see that file's
       * comments for the three frame shapes.
       */
      _openProgressStream(newJobId) {
        const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
        const url = `${proto}//${window.location.host}/ws/jobs/${encodeURIComponent(
          newJobId
        )}`;

        let socket;
        try {
          socket = new WebSocket(url);
        } catch (err) {
          // Invalid URL / CSP / etc. Surface the failure as a terminal
          // error and clear the in-flight state so the send button
          // re-enables.
          const detail = err && err.message ? err.message : String(err);
          this.progressStatus = "failed";
          this.errorMessage = `Could not open WebSocket: ${detail}`;
          this.inFlightJobId = "";
          this._appendError(this.errorMessage);
          return;
        }
        this._socket = socket;

        socket.addEventListener("message", (event) => {
          this._handleMessage(event.data);
        });
        socket.addEventListener("error", () => {
          // Browsers don't give us a usable error payload here -- the
          // `close` handler is where we actually decide if this was a
          // failure or a clean wrap-up.
          // eslint-disable-next-line no-console
          console.warn("[chat] WebSocket error event fired");
        });
        socket.addEventListener("close", (event) => {
          this._handleClose(event);
        });
      },

      /**
       * Parse and dispatch one JSON frame from the server. Unknown types
       * are dropped (with a console warning) rather than crashing the
       * view.
       */
      _handleMessage(raw) {
        let payload;
        try {
          payload = JSON.parse(raw);
        } catch (_err) {
          // eslint-disable-next-line no-console
          console.warn("[chat] Dropping non-JSON WebSocket frame", raw);
          return;
        }
        if (!payload || typeof payload !== "object") return;

        switch (payload.type) {
          case "progress":
            this._appendProgressFrame(payload);
            break;
          case "status":
            this._handleStatus(payload);
            break;
          case "result":
            // The result frame carries the serialized PipelineResult.
            // We don't commit anything yet -- the completion path waits
            // until _handleStatus(completed) fires so the fetch below
            // sees the full Job.to_dict() shape, which also includes the
            // accumulated feedback_history. Stash the result locally so
            // _finalizeRerun has it even if the follow-up GET fails.
            this._lastResult =
              payload.data && typeof payload.data === "object"
                ? payload.data
                : null;
            break;
          default:
            // eslint-disable-next-line no-console
            console.warn("[chat] Unknown message type", payload.type);
        }
      },

      /**
       * Append a single progress line and update the feedback-rerun
       * step indicator if the line matches the canonical framing.
       */
      _appendProgressFrame(payload) {
        const line = typeof payload.line === "string" ? payload.line : "";
        const timestamp =
          typeof payload.timestamp === "string" ? payload.timestamp : "";
        this.progressLines.push({ line, timestamp });
        this._parseFeedbackStep(line);
        // Auto-scroll the inline log pane to the bottom on next render
        // so the user always sees the latest line.
        if (typeof this.$nextTick === "function") {
          this.$nextTick(() => {
            const el = this.$refs && this.$refs.chatLogPane;
            if (el) {
              el.scrollTop = el.scrollHeight;
            }
          });
        }
      },

      /**
       * Update `feedbackStep` when a `[feedback-rerun] step N -- ...`
       * line lands. Clamps to `FEEDBACK_STEP_NAMES.length` in case a
       * future runner adds a 5th step before this file is updated.
       */
      _parseFeedbackStep(line) {
        if (!line) return;
        const match = line.match(FEEDBACK_STEP_LINE_RE);
        if (!match) return;
        const stepNumber = parseInt(match[1], 10);
        if (!Number.isFinite(stepNumber) || stepNumber < 1) return;
        this.feedbackStep = Math.min(stepNumber, FEEDBACK_STEP_NAMES.length);
      },

      /**
       * Handle a `{type: 'status', ...}` frame. Mirrors progress.js's
       * logic but defers the final state mutation until `_handleClose`
       * runs (via `_terminalSeen`) so we never race the trailing
       * `result` frame before closing.
       *
       * On completion: kick off the follow-up fetch in `_finalizeRerun`
       * so the system message sees the full Job.to_dict() shape (with
       * feedback_history appended).
       * On failure: append a system error message immediately and keep
       * the inline banner visible.
       */
      _handleStatus(payload) {
        const status = payload.status;
        if (status === "completed") {
          this._terminalSeen = true;
          this.progressStatus = "completed";
          this.feedbackStep = FEEDBACK_STEP_NAMES.length;
          // Defer the system message + version append until after the
          // GET /api/jobs/{id} call in _finalizeRerun. That way the chat
          // entry carries the full feedback_history even if the result
          // frame was missing a field.
          this._finalizeRerun();
        } else if (status === "failed") {
          this._terminalSeen = true;
          this.progressStatus = "failed";
          const detail =
            typeof payload.error === "string" && payload.error
              ? payload.error
              : "Pipeline re-run failed with no error message.";
          this.errorMessage = detail;
          this._appendTranscript({
            role: "error",
            text: `Feedback re-run failed: ${detail}`,
            timestamp: new Date().toISOString(),
            jobId: this.inFlightJobId,
          });
        }
      },

      /**
       * Interpret the WebSocket close event. Single point where we clear
       * `inFlightJobId` so the send button re-enables for ANY terminal
       * outcome -- clean completion, clean failure, 4004 unknown-job, or
       * an abnormal disconnect before a terminal status frame arrived.
       *
       * Clearing here (not in `_handleStatus`) matches the pattern
       * progress.js uses to avoid tearing the socket down before the
       * trailing `result` frame is delivered.
       */
      _handleClose(event) {
        this._socket = null;

        // If the caller explicitly tore the socket down (e.g. a brief
        // re-submit mid-stream calls _resetSession -> _teardownSocket),
        // swallow the trailing close event so it does not write a
        // spurious error into the fresh chat session. Reset the flag
        // and clear inFlightJobId so the UI is ready for the next run.
        if (this._suppressNextClose) {
          this._suppressNextClose = false;
          this.inFlightJobId = "";
          return;
        }

        if (this._terminalSeen) {
          this.inFlightJobId = "";
          return;
        }

        if (event && event.code === UNKNOWN_JOB_CLOSE_CODE) {
          this.progressStatus = "failed";
          this.errorMessage = `Unknown job id: ${this.inFlightJobId}`;
          this._appendTranscript({
            role: "error",
            text: this.errorMessage,
            timestamp: new Date().toISOString(),
            jobId: this.inFlightJobId,
          });
          this.inFlightJobId = "";
          return;
        }

        // Abnormal close -- WebSocket died before a terminal status.
        this.progressStatus = "failed";
        const codeSuffix =
          event && typeof event.code === "number" ? ` (code ${event.code})` : "";
        this.errorMessage =
          "WebSocket closed before the pipeline reported a terminal status" +
          codeSuffix +
          ".";
        this._appendTranscript({
          role: "error",
          text: this.errorMessage,
          timestamp: new Date().toISOString(),
          jobId: this.inFlightJobId,
        });
        this.inFlightJobId = "";
      },

      /**
       * Tear down the active WebSocket, if any. Idempotent. Uses close
       * code 1000 so the server-side handler sees a clean disconnect.
       */
      _teardownSocket() {
        const socket = this._socket;
        if (!socket) return;
        this._socket = null;
        try {
          if (
            socket.readyState === WebSocket.OPEN ||
            socket.readyState === WebSocket.CONNECTING
          ) {
            // Mark the close we are about to trigger as expected so
            // `_handleClose` skips the abnormal-close / unknown-job
            // branches for THIS specific close event. Only set the
            // flag when we actually call close() -- otherwise a later
            // genuine close event could be suppressed by a stale flag.
            this._suppressNextClose = true;
            socket.close(1000, "chat view teardown");
          }
        } catch (_err) {
          // Best-effort.
        }
      },

      /**
       * Fetch the newly completed job, build a new version entry, append
       * the system response to the transcript, and chain `currentJobId`
       * to the new job so the next feedback round targets it.
       *
       * Called from `_handleStatus` on the completed branch. If the
       * follow-up GET fails for any reason we still surface a system
       * message (using the cached result frame) so the user sees
       * SOMETHING land -- the UX cost of a silent drop is worse than a
       * partial summary.
       */
      async _finalizeRerun() {
        const jobId = this.inFlightJobId;
        if (!jobId) return;
        const cachedResult = this._lastResult;
        this._lastResult = null;

        let payload = null;
        try {
          const response = await fetch(
            `/api/jobs/${encodeURIComponent(jobId)}`,
            { headers: { Accept: "application/json" } }
          );
          if (response.ok) {
            payload = await response.json();
          }
        } catch (_err) {
          // Swallow -- we'll fall back to cachedResult below.
        }

        // Prefer the authoritative Job.to_dict() payload; fall back to
        // the cached result frame if the GET failed.
        const result =
          payload && payload.result && typeof payload.result === "object"
            ? payload.result
            : cachedResult;
        const videoPath =
          result && typeof result.final_video_path === "string"
            ? result.final_video_path
            : "";
        const videoUrl = toMediaUrl(videoPath);
        const review =
          result && result.review && typeof result.review === "object"
            ? result.review
            : null;

        // Build a chat-friendly summary of the new render.
        const lines = [`Re-run complete (job ${jobId}).`];
        const scoreSummary = summarizeReview(review);
        if (scoreSummary) {
          lines.push(`Scores -- ${scoreSummary}`);
        }
        if (review && typeof review.feedback === "string" && review.feedback) {
          lines.push(`Reviewer feedback: ${review.feedback}`);
        }
        if (!videoUrl) {
          lines.push("Video unavailable -- check the progress log above.");
        }

        // Stamp version entry + commit to the chat transcript.
        const nextIndex = this.versions.length + 1;
        const createdAt =
          (payload && typeof payload.completed_at === "string"
            ? payload.completed_at
            : "") || new Date().toISOString();
        const version = {
          jobId,
          videoPath,
          videoUrl,
          review,
          createdAt,
          label: `Version ${nextIndex}`,
        };
        this.versions = [...this.versions, version];
        // Swap the video player to the new render. We also flip
        // videoLoadError back to false so a prior load failure does not
        // stick when the source changes.
        this.activeVersionId = jobId;
        this.videoLoadError = false;
        // Chain subsequent feedback rounds off the new job.
        this.currentJobId = jobId;
        this.inFlightJobId = "";

        this._appendTranscript({
          role: "system",
          text: lines.join("\n"),
          videoPath,
          videoUrl,
          review,
          jobId,
          timestamp: createdAt,
        });
      },

      // ---------------------------------------------------------------- #
      // Template helpers                                                  #
      // ---------------------------------------------------------------- #

      /**
       * Append one entry to the transcript with a stable id so Alpine's
       * :key binding doesn't churn the DOM. Returns the entry for
       * callers that want to reference it later (e.g. retry semantics).
       */
      _appendTranscript(entry) {
        const id = this._nextTranscriptId;
        this._nextTranscriptId += 1;
        const full = Object.assign({ id }, entry);
        this.transcript.push(full);
        return full;
      },

      /**
       * Shorthand for appending an error-role transcript entry AND
       * surfacing it on the inline banner. Keeps error handling DRY
       * across the submit / fetch / WebSocket paths.
       */
      _appendError(text) {
        this.errorMessage = text;
        this._appendTranscript({
          role: "error",
          text,
          timestamp: new Date().toISOString(),
        });
      },

      /**
       * Switch the video player to a specific version. Called from the
       * version dropdown / list. Does not touch `currentJobId` -- the
       * next feedback round still chains off the latest re-run.
       */
      selectVersion(jobId) {
        if (!jobId) return;
        const match = this.versions.find((v) => v.jobId === jobId);
        if (!match) return;
        this.activeVersionId = jobId;
        this.videoLoadError = false;
      },

      /** Lookup the full version object for `activeVersionId`. */
      activeVersion() {
        if (!this.activeVersionId) return null;
        return (
          this.versions.find((v) => v.jobId === this.activeVersionId) || null
        );
      },

      /** URL the <video> element should load for the active version. */
      activeVideoUrl() {
        const version = this.activeVersion();
        return version ? version.videoUrl : "";
      },

      /** ReviewScore the inline summary cards should render. */
      activeReview() {
        const version = this.activeVersion();
        return version ? version.review : null;
      },

      /**
       * True when we have something worth showing in the <video>
       * element -- a non-empty URL and no prior load failure.
       */
      hasActiveVideo() {
        return Boolean(this.activeVideoUrl()) && !this.videoLoadError;
      },

      /** True when a feedback re-run is currently streaming. */
      isInFlight() {
        return Boolean(this.inFlightJobId);
      },

      /** Disable the send button when input is empty or a re-run is live. */
      canSend() {
        if (this.isInFlight()) return false;
        if (!this.currentJobId) return false;
        return (this.message || "").trim().length > 0;
      },

      /** Format one score value for the inline summary cards. */
      scoreValue(key) {
        return formatScore(this.activeReview(), key);
      },

      /** True when the active version has a usable ReviewScore. */
      hasActiveReview() {
        return Boolean(this.activeReview());
      },

      /**
       * Called from the <video> element's `error` event listener. Flips
       * the component into its fallback state so the UI explains why
       * playback failed rather than showing a broken element.
       */
      onVideoError() {
        this.videoLoadError = true;
        // eslint-disable-next-line no-console
        console.warn(
          "[chat] <video> element failed to load",
          this.activeVideoUrl()
        );
      },

      /**
       * Step indicator classification for a 0-based step index. Mirrors
       * progress.js::stepState but uses 1-based `feedbackStep` under the
       * hood to match the backend's framing line numbering.
       */
      stepNames: FEEDBACK_STEP_NAMES,
      stepState(index) {
        if (this.progressStatus === "failed" && this.feedbackStep - 1 === index) {
          return "failed";
        }
        if (this.feedbackStep <= 0) return "pending";
        if (index < this.feedbackStep - 1) return "done";
        if (index === this.feedbackStep - 1) {
          return this.progressStatus === "completed" ? "done" : "active";
        }
        return "pending";
      },
    };
  }

  // Expose as a global so Alpine's x-data="chatView()" can find it. No
  // ES-module export needed -- this script is loaded via a plain
  // <script defer> tag that executes BEFORE the Alpine CDN script, just
  // like brief-builder.js / progress.js / video-player.js / review-chart.js
  // (see index.html for the ordering rationale).
  window.chatView = chatView;
})();
