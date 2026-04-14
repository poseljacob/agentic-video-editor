/**
 * AVE Studio -- Re-render Controls (US-010)
 *
 * Alpine.js v3 mixin that extends the US-008 timeline view + US-009 edit
 * controls with the "save the edited plan, fire a fresh editor-only
 * render, stream progress, then swap the player to the new MP4" flow.
 * Registers itself as `window.reRenderControls` so the timeline template
 * can mix it into the existing `timelineView() + editControls()` scope
 * via `Object.assign(timelineView(), editControls(), reRenderControls())`.
 *
 * Why a mixin instead of a sibling component
 * ------------------------------------------
 * The re-render flow needs read/write access to fields owned by
 * editControls (`plan`, `dirty`, `errors`, `planErrors`, `saving`,
 * `hasAnyError()`, `savePlan()`, `jobId`) and to fields owned by
 * timelineView (`jobId`, `_loadedJobId`, `_maybeFetch`). Hosting it in a
 * sibling Alpine component would force a `$dispatch` round trip through
 * the DOM for every read AND would have to duplicate the PUT body
 * builder. Spreading this object into the same `x-data` keeps every
 * field on a single scope and lets `triggerReRender()` call
 * `await this.savePlan()` directly to reuse the existing PUT pipeline.
 *
 * Responsibilities
 * ----------------
 *  1. Track render state -- `rendering`, `renderError`, the streamed
 *     `renderProgressLines`, the panel-open toggle, the in-flight child
 *     job id, the version list, and the active version index.
 *  2. Seed the version list from the parent job's `final_video_path`
 *     once the timeline view's `jobId` is known so the user always has
 *     at least one entry in the list (labelled "Original").
 *  3. `triggerReRender()` -- delegate the PUT to `savePlan()`, then POST
 *     the same EditPlan body to `/api/jobs/{id}/re-render`. Surfaces
 *     404 / 409 / 422 / network errors via the `renderError` banner.
 *  4. `subscribeToRender()` -- open `/ws/jobs/{new_id}`, append progress
 *     lines into `renderProgressLines` (capped so a runaway job can't
 *     OOM the page), and on `result` swap the video player + push a new
 *     entry into `renders`.
 *  5. `selectRender()` -- swap the active video back to any prior render.
 *     Dispatches a `re-render-active-changed` window event so any
 *     scope-isolated player component can pick up the new src.
 *  6. `triggerReviewOnly()` -- POST `/api/jobs/{active}/review-only`
 *     and stream the resulting reviewer-only job. On completion
 *     dispatches a `review-updated` event so the radar chart can
 *     refetch.
 *
 * Coding style
 * ------------
 * Mirrors edit-controls.js: heavy JSDoc, defensive try/catch on every
 * fetch, no ES2023+ syntax that breaks older Safari, no third-party
 * libraries. The WebSocket lifecycle copies the pattern in progress.js /
 * chat.js so a brief re-submit mid-stream tears the socket down cleanly.
 */
(function () {
  "use strict";

  /**
   * Cap on the number of progress lines we retain in memory. The
   * editor-only re-run is the same ffmpeg pipeline as a full pipeline
   * minus the Director / TrimRefiner / Reviewer steps, so a 30s spot
   * usually emits 50 to 200 lines. Capping at 1000 leaves headroom for
   * a verbose ffmpeg `-loglevel info` run without ever letting the page
   * grow unbounded if the runner ever spins or the user leaves the tab
   * open across many renders.
   */
  const MAX_PROGRESS_LINES = 1000;

  /**
   * WebSocket close code the server uses for unknown job ids. Matches
   * UNKNOWN_JOB_CLOSE_CODE in src/web/routes/ws.py and the local copies
   * in progress.js + chat.js.
   */
  const UNKNOWN_JOB_CLOSE_CODE = 4004;

  /**
   * Normalize an absolute pipeline `final_video_path` into a URL the
   * browser can load through the FastAPI `/media/` static mount. Copied
   * verbatim from progress.js::toMediaUrl + chat.js::toMediaUrl so this
   * file has zero load-order coupling with either of them.
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
   * Build the re-render mixin. Factory form matches editControls() /
   * timelineView() / reviewChart() so the module surface is uniform
   * across the static/js directory and the spread in `x-data` looks the
   * same as the others.
   */
  function reRenderControls() {
    return {
      // ------------------------------------------------------------- #
      // State                                                          #
      // ------------------------------------------------------------- #

      /**
       * True while a re-render is in flight (POST sent OR WebSocket
       * still streaming). Disables the Re-render button so the user
       * can't double-click and race two POSTs at the same parent job.
       */
      rendering: false,

      /**
       * Plan-level error banner text for the re-render flow. Distinct
       * from `planErrors` (owned by editControls) so the user can tell
       * a save failure apart from a render failure.
       */
      renderError: "",

      /**
       * Streamed progress lines for the in-flight render. Each entry
       * is `{line, timestamp}` so the inline log pane can render the
       * same shape progress.js / chat.js emit.
       */
      renderProgressLines: [],

      /**
       * Toggle for the inline progress panel under the timeline. Set
       * true on render start so the user sees the log appear. The user
       * can still collapse it manually after completion.
       */
      renderProgressOpen: false,

      /**
       * UUID of the editor-only child job currently streaming over the
       * WebSocket, if any. Empty string when no render is in flight.
       */
      currentRenderJobId: "",

      /**
       * Ordered list of every render produced in this session, oldest
       * first. Each entry is
       *   {job_id, final_video_path, video_url, label, created_at}
       * The first entry is seeded from the parent job and labelled
       * "Original"; each successful re-render appends `Re-render #N`.
       */
      renders: [],

      /**
       * Index into `renders` of the version currently shown in the
       * video player. Defaults to 0 (the original render).
       */
      activeRenderIndex: 0,

      /**
       * True while a reviewer-only POST + WebSocket round trip is in
       * flight. Disables the Review button so the user can't pile on.
       */
      reviewing: false,

      /**
       * Banner text for the latest reviewer-only failure, if any.
       * Distinct from `renderError` so the two flows can fail
       * independently.
       */
      reviewError: "",

      /**
       * UUID of the in-flight reviewer-only child job, if any. Used by
       * `subscribeToReview` to filter incoming WebSocket frames.
       */
      reviewJobId: "",

      // --- Internal bookkeeping (not reactive) ---

      /**
       * Tracks which parent job id has already had its "Original"
       * entry seeded so the `x-effect` on `jobId` does not re-seed on
       * every reactive tick. Mirrors the `_loadedJobId` pattern in
       * timeline.js.
       */
      _seededJobId: "",

      /** Active render WebSocket, or null when idle. */
      _renderSocket: null,

      /** Active reviewer-only WebSocket, or null when idle. */
      _reviewSocket: null,

      /**
       * True between `_teardownRenderSocket()` and the trailing close
       * event so `_handleRenderClose` can swallow a teardown-induced
       * close without writing a spurious abnormal-close error. Matches
       * the `_suppressNextClose` pattern in chat.js.
       */
      _suppressRenderClose: false,
      _suppressReviewClose: false,

      /**
       * True once a terminal status frame (`completed` or `failed`) has
       * landed for the active render socket. The `result` frame and the
       * `close` frame are still allowed to arrive after this; the flag
       * is just so the close handler knows the wrap-up was clean and
       * does NOT write a "stream died early" error.
       */
      _renderTerminalSeen: false,
      _reviewTerminalSeen: false,

      // ------------------------------------------------------------- #
      // Lifecycle                                                      #
      // ------------------------------------------------------------- #

      /**
       * Seed the version list from the parent job's `final_video_path`.
       * Called from the timeline template's `x-effect` whenever
       * `jobId` becomes truthy. Idempotent: only fetches the parent job
       * once per id, and skips entirely if `renders` already contains
       * an entry whose `job_id` matches.
       *
       * Why we don't read from `editControls.enterEditMode()`'s cached
       * `this.jobBrief` -- we want the "Original" entry to render even
       * when the user has not opened edit mode, so we hit
       * GET /api/jobs/{id} ourselves rather than relying on a cache
       * that might be empty.
       */
      async seedOriginalRender() {
        const id = this.jobId;
        if (!id) return;
        if (id === this._seededJobId) return;
        this._seededJobId = id;

        // Tear down any sockets left open by a render or review-only
        // job that targeted the PRIOR parent. Without this, late
        // `result` / `close` frames from those streams could mutate
        // state we just reset for the new parent (e.g. push a stale
        // entry into `renders` or flip `rendering` back on). The
        // teardown helpers are idempotent + flip the suppress flags so
        // the trailing close events don't write spurious errors.
        this._teardownRenderSocket();
        this._teardownReviewSocket();
        this.rendering = false;

        // Drop any prior session's renders + reset the active index
        // so flipping to a new brief gives a fresh version list. The
        // `_seededJobId` guard above prevents this from running twice
        // for the same job.
        this.renders = [];
        this.activeRenderIndex = 0;
        this.renderProgressLines = [];
        this.renderProgressOpen = false;
        this.currentRenderJobId = "";
        this.renderError = "";
        this.reviewing = false;
        this.reviewError = "";
        this.reviewJobId = "";

        let response;
        try {
          response = await fetch(`/api/jobs/${encodeURIComponent(id)}`, {
            headers: { Accept: "application/json" },
          });
        } catch (_err) {
          // Non-fatal. The version list will start empty and a
          // successful re-render will still append a new entry.
          return;
        }
        if (!response.ok) return;

        let payload;
        try {
          payload = await response.json();
        } catch (_err) {
          return;
        }
        if (!payload || typeof payload !== "object") return;

        const result =
          payload.result && typeof payload.result === "object"
            ? payload.result
            : null;
        const finalVideoPath =
          result && typeof result.final_video_path === "string"
            ? result.final_video_path
            : "";
        if (!finalVideoPath) return;

        // Guard against re-seeding on a refetch of the same job after
        // the user already kicked off a render -- if ANY entry already
        // matches this job id, leave the list alone.
        if (this.renders.some((r) => r.job_id === id)) return;

        const createdAt =
          (typeof payload.completed_at === "string" && payload.completed_at) ||
          (typeof payload.created_at === "string" && payload.created_at) ||
          "";

        this.renders = [
          {
            job_id: id,
            final_video_path: finalVideoPath,
            video_url: toMediaUrl(finalVideoPath),
            label: "Original",
            created_at: createdAt,
          },
        ];
        this.activeRenderIndex = 0;
        // Broadcast so any listener (e.g. an out-of-scope videoPlayer
        // instance) can pick up the seeded src without polling.
        this._dispatchActiveChanged();
      },

      // ------------------------------------------------------------- #
      // Re-render flow                                                 #
      // ------------------------------------------------------------- #

      /**
       * Kick off a re-render: PUT the modified plan via the existing
       * `savePlan()` (which handles 422 -> per-field errors) and then,
       * if the save succeeded, POST the SAME body to
       * `/api/jobs/{parent}/re-render`. Subscribes to the new child
       * job's WebSocket so progress streams into the inline panel.
       */
      async triggerReRender() {
        if (!this.plan || !this.jobId) return;
        if (this.rendering) return;
        if (this.saving) return;
        this.renderError = "";

        // Snapshot the EditPlan body BEFORE calling savePlan(), then use
        // the snapshot for the subsequent POST. This closes a race where
        // the user mutates trims / text / order while the PUT is in
        // flight: savePlan() would clear `dirty` for the stale PUT,
        // then a fresh _buildEditPlanBody() after the await would send
        // the NEWER plan to /re-render -- desynchronizing the parent's
        // persisted edit_plan from the child's rendered edit_plan.
        // The snapshot IS what the server will have persisted once the
        // PUT resolves (savePlan() uses the same builder contract), so
        // POSTing the same immutable object guarantees parent edit_plan
        // and child edit_plan match.
        const body = this._buildEditPlanBody();
        if (!body) {
          this.renderError =
            "Could not build the EditPlan body to re-render -- missing brief or entries.";
          return;
        }

        // Step 1: delegate the PUT + per-field validation to
        // editControls.savePlan(). It already mutates `errors` /
        // `planErrors` / `dirty` correctly, so we just await and then
        // bail out if anything went sideways.
        await this.savePlan();
        if (this.dirty) return;
        if (typeof this.hasAnyError === "function" && this.hasAnyError()) return;
        if (Array.isArray(this.planErrors) && this.planErrors.length > 0) return;

        // Race guard: if the user mutated the plan between our snapshot
        // and savePlan()'s completion, the snapshot no longer matches
        // what savePlan() persisted. Rather than silently rendering a
        // stale version, abort with a clear message so the user can try
        // again once the board has settled.
        const postSaveBody = this._buildEditPlanBody();
        if (
          !postSaveBody ||
          JSON.stringify(postSaveBody) !== JSON.stringify(body)
        ) {
          this.renderError =
            "Plan changed while saving -- please click Re-render again.";
          return;
        }

        this.rendering = true;
        this.renderProgressLines = [];
        this.renderProgressOpen = true;
        this._renderTerminalSeen = false;

        let response;
        try {
          response = await fetch(
            `/api/jobs/${encodeURIComponent(this.jobId)}/re-render`,
            {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                Accept: "application/json",
              },
              body: JSON.stringify(body),
            }
          );
        } catch (err) {
          this.rendering = false;
          this.renderError =
            err && err.message
              ? `Could not reach the re-render endpoint: ${err.message}`
              : "Could not reach the re-render endpoint.";
          return;
        }

        if (response.status === 202) {
          let payload = null;
          try {
            payload = await response.json();
          } catch (_err) {
            // Fall through -- handled below.
          }
          if (!payload || typeof payload.job_id !== "string" || !payload.job_id) {
            this.rendering = false;
            this.renderError = "Re-render response missing a job_id.";
            return;
          }
          this.currentRenderJobId = payload.job_id;
          this.subscribeToRender(payload.job_id);
          return;
        }

        // 404 / 409 / 422 / 5xx -- pull the FastAPI detail out and
        // surface it on the banner. We do NOT route 422 details into
        // the per-field error map here because savePlan() already
        // covered the validation surface; if we see a 422 here it means
        // something between the save and the re-render desynchronized
        // (e.g. the parent job's footage moved on disk) and a banner is
        // the right place for that message.
        this.rendering = false;
        let detail = "";
        try {
          const errBody = await response.json();
          if (errBody && typeof errBody.detail === "string") {
            detail = errBody.detail;
          } else if (errBody && Array.isArray(errBody.detail)) {
            detail = errBody.detail
              .map((e) => (e && e.msg ? e.msg : JSON.stringify(e)))
              .join("; ");
          }
        } catch (_err) {
          // non-JSON body -- fall back to status text
        }
        this.renderError =
          detail || `Re-render failed (${response.status} ${response.statusText}).`;
      },

      /**
       * Open a WebSocket to /ws/jobs/{newJobId} and stream progress
       * frames into the inline panel. Mirrors the lifecycle pattern in
       * progress.js: handle progress / status / result frames, defer
       * the "rendering done" flip until the close event so the trailing
       * `result` frame is never dropped.
       */
      subscribeToRender(newJobId) {
        if (!newJobId) return;
        // Tear down any prior socket -- one render at a time.
        this._teardownRenderSocket();

        const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
        const url = `${proto}//${window.location.host}/ws/jobs/${encodeURIComponent(
          newJobId
        )}`;

        let socket;
        try {
          socket = new WebSocket(url);
        } catch (err) {
          this.rendering = false;
          this.currentRenderJobId = "";
          this.renderError =
            err && err.message
              ? `Could not open render WebSocket: ${err.message}`
              : "Could not open render WebSocket.";
          return;
        }
        this._renderSocket = socket;

        socket.addEventListener("message", (event) => {
          this._handleRenderMessage(event.data);
        });
        socket.addEventListener("error", () => {
          // The browser does not give us a usable error payload; the
          // close handler is where we actually decide whether this was
          // a clean wrap-up or a failure.
          // eslint-disable-next-line no-console
          console.warn("[re-render] WebSocket error event fired");
        });
        socket.addEventListener("close", (event) => {
          this._handleRenderClose(event);
        });
      },

      /**
       * Parse and dispatch a single JSON frame from the render
       * WebSocket. Unknown types are dropped (with a console warning)
       * rather than crashing the view.
       */
      _handleRenderMessage(raw) {
        let payload;
        try {
          payload = JSON.parse(raw);
        } catch (_err) {
          // eslint-disable-next-line no-console
          console.warn("[re-render] Dropping non-JSON WebSocket frame", raw);
          return;
        }
        if (!payload || typeof payload !== "object") return;

        switch (payload.type) {
          case "progress":
            this._appendRenderProgress(payload);
            break;
          case "status":
            this._handleRenderStatus(payload);
            break;
          case "result":
            this._finalizeRender(payload.data || null);
            break;
          default:
            // eslint-disable-next-line no-console
            console.warn("[re-render] Unknown message type", payload.type);
        }
      },

      /**
       * Append a single progress entry to the rolling log. Caps the
       * array length so a runaway render or a long-running session of
       * many edits does not let the page grow unbounded. Also
       * auto-scrolls the panel to the bottom so the user always sees
       * the latest line.
       */
      _appendRenderProgress(payload) {
        const line = typeof payload.line === "string" ? payload.line : "";
        const timestamp =
          typeof payload.timestamp === "string" ? payload.timestamp : "";
        this.renderProgressLines.push({ line, timestamp });
        if (this.renderProgressLines.length > MAX_PROGRESS_LINES) {
          // Drop the oldest lines so the cap holds. Slicing once is
          // O(n) but only happens after the cap is reached, so the
          // amortized cost is well under one append per drop.
          this.renderProgressLines = this.renderProgressLines.slice(
            this.renderProgressLines.length - MAX_PROGRESS_LINES
          );
        }
        if (typeof this.$nextTick === "function") {
          this.$nextTick(() => {
            const el = this.$refs && this.$refs.renderLogPane;
            if (el) {
              el.scrollTop = el.scrollHeight;
            }
          });
        }
      },

      /**
       * Handle a {type: "status", ...} frame. Defer the "rendering
       * done" flip until `_handleRenderClose` runs so the trailing
       * `result` frame still has a chance to land.
       */
      _handleRenderStatus(payload) {
        const status = payload && payload.status;
        if (status === "completed") {
          this._renderTerminalSeen = true;
          // Don't flip `rendering` yet -- the result frame is still
          // in flight and we want the spinner to stay visible until
          // the new video actually swaps in.
        } else if (status === "failed") {
          this._renderTerminalSeen = true;
          this.renderError =
            typeof payload.error === "string" && payload.error
              ? payload.error
              : "Render failed with no error message.";
          // Flip `rendering` here so the user can retry without
          // waiting for the close handler to fire.
          this.rendering = false;
        }
      },

      /**
       * Apply a successful render's result to the version list +
       * video player. Called from the WebSocket message handler when
       * a `result` frame lands. Adds a new entry to `renders`, flips
       * the active index to it, exits edit mode, and broadcasts the
       * src change.
       */
      _finalizeRender(data) {
        if (!data || typeof data !== "object") {
          this.renderError =
            "Render completed but the server did not return a result payload.";
          return;
        }
        const finalVideoPath =
          typeof data.final_video_path === "string"
            ? data.final_video_path
            : "";
        if (!finalVideoPath) {
          this.renderError =
            "Render completed but no final_video_path was returned.";
          return;
        }
        const videoUrl = toMediaUrl(finalVideoPath);
        const renderIndexLabel =
          this.renders.filter((r) => r.label !== "Original").length + 1;
        const entry = {
          job_id: this.currentRenderJobId,
          final_video_path: finalVideoPath,
          video_url: videoUrl,
          label: `Re-render #${renderIndexLabel}`,
          created_at: new Date().toISOString(),
        };
        this.renders = [...this.renders, entry];
        this.activeRenderIndex = this.renders.length - 1;
        this.renderError = "";
        // Exit edit mode so the card strip reflects the just-rendered
        // plan (if savePlan() succeeded the strip is already in sync,
        // but flipping `editing` back off matches the AC).
        this.editing = false;
        // Broadcast so any out-of-scope video player picks up the
        // new src.
        this._dispatchActiveChanged();
      },

      /**
       * Interpret the close event for the render WebSocket. Single
       * point where `rendering` flips back to false on a clean
       * completion path. Mirrors the `_handleClose` pattern in
       * progress.js + chat.js: tearing down the socket from
       * `subscribeToRender` (when starting a new run) sets a suppress
       * flag so the trailing close event is swallowed.
       */
      _handleRenderClose(event) {
        this._renderSocket = null;
        if (this._suppressRenderClose) {
          this._suppressRenderClose = false;
          this.rendering = false;
          this.currentRenderJobId = "";
          return;
        }
        if (this._renderTerminalSeen) {
          this.rendering = false;
          this.currentRenderJobId = "";
          return;
        }
        // Abnormal close before a terminal status -- the stream died
        // mid-render. Surface it on the banner so the user knows the
        // job did not complete.
        if (event && event.code === UNKNOWN_JOB_CLOSE_CODE) {
          this.renderError = `Unknown render job id: ${this.currentRenderJobId}`;
        } else {
          const codeSuffix =
            event && typeof event.code === "number"
              ? ` (code ${event.code})`
              : "";
          this.renderError =
            "Render WebSocket closed before the pipeline reported a terminal status" +
            codeSuffix +
            ".";
        }
        this.rendering = false;
        this.currentRenderJobId = "";
      },

      /**
       * Tear down the render WebSocket if any. Idempotent. Flips the
       * suppress flag so `_handleRenderClose` swallows the trailing
       * close event triggered by the explicit close() call.
       */
      _teardownRenderSocket() {
        const socket = this._renderSocket;
        if (!socket) return;
        this._renderSocket = null;
        try {
          if (
            socket.readyState === WebSocket.OPEN ||
            socket.readyState === WebSocket.CONNECTING
          ) {
            this._suppressRenderClose = true;
            socket.close(1000, "re-render teardown");
          }
        } catch (_err) {
          // Best-effort.
        }
      },

      // ------------------------------------------------------------- #
      // Version list                                                   #
      // ------------------------------------------------------------- #

      /**
       * Switch the player to the render at the given index. Bounds
       * checked; out-of-range indexes are dropped silently. Dispatches
       * a `re-render-active-changed` window event so any listener can
       * pick up the new src.
       */
      selectRender(index) {
        if (!Array.isArray(this.renders)) return;
        const idx = Number(index);
        if (!Number.isInteger(idx) || idx < 0 || idx >= this.renders.length) {
          return;
        }
        if (idx === this.activeRenderIndex) return;
        this.activeRenderIndex = idx;
        this._dispatchActiveChanged();
      },

      /**
       * Currently-active render entry, or null when the version list
       * is empty. Used by the template's `x-text` / `:src` bindings so
       * the video player + dropdown both read from a single helper.
       */
      activeRender() {
        if (!Array.isArray(this.renders) || this.renders.length === 0) {
          return null;
        }
        const idx = this.activeRenderIndex;
        if (idx < 0 || idx >= this.renders.length) return null;
        return this.renders[idx];
      },

      /** URL the inline <video> element should load for the active render. */
      activeRenderVideoUrl() {
        const entry = this.activeRender();
        return entry ? entry.video_url : "";
      },

      /** True when there is at least one render entry available. */
      hasAnyRender() {
        return Array.isArray(this.renders) && this.renders.length > 0;
      },

      // ------------------------------------------------------------- #
      // Review-only flow                                               #
      // ------------------------------------------------------------- #

      /**
       * POST /api/jobs/{active}/review-only against the currently
       * playing render and stream the resulting reviewer-only job.
       * On completion dispatches a `review-updated` window event the
       * radar chart can listen for to refetch.
       */
      async triggerReviewOnly() {
        if (this.reviewing) return;
        const target = this.activeRender();
        if (!target || !target.job_id) {
          this.reviewError = "No render available to review.";
          return;
        }
        this.reviewing = true;
        this.reviewError = "";

        let response;
        try {
          response = await fetch(
            `/api/jobs/${encodeURIComponent(target.job_id)}/review-only`,
            {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                Accept: "application/json",
              },
            }
          );
        } catch (err) {
          this.reviewing = false;
          this.reviewError =
            err && err.message
              ? `Could not reach the review-only endpoint: ${err.message}`
              : "Could not reach the review-only endpoint.";
          return;
        }

        if (response.status === 202) {
          let payload = null;
          try {
            payload = await response.json();
          } catch (_err) {
            // fall through
          }
          if (!payload || typeof payload.job_id !== "string" || !payload.job_id) {
            this.reviewing = false;
            this.reviewError = "Review-only response missing a job_id.";
            return;
          }
          this.reviewJobId = payload.job_id;
          this.subscribeToReview(payload.job_id, target.job_id);
          return;
        }

        this.reviewing = false;
        let detail = "";
        try {
          const errBody = await response.json();
          if (errBody && typeof errBody.detail === "string") {
            detail = errBody.detail;
          } else if (errBody && Array.isArray(errBody.detail)) {
            detail = errBody.detail
              .map((e) => (e && e.msg ? e.msg : JSON.stringify(e)))
              .join("; ");
          }
        } catch (_err) {
          // non-JSON body -- fall back to status text
        }
        this.reviewError =
          detail ||
          `Review-only failed (${response.status} ${response.statusText}).`;
      },

      /**
       * Open a WebSocket for the reviewer-only child job. Mirrors
       * `subscribeToRender` -- we only need progress + status frames
       * here because the reviewer does not produce a new video, but
       * we keep the structure parallel so a future refactor that
       * unifies the two streamers is trivial.
       *
       * `parentJobId` is the id of the render the review is scored
       * against; we forward it on the `review-updated` event so any
       * listener can refetch the right job's review payload.
       */
      subscribeToReview(reviewJobId, parentJobId) {
        if (!reviewJobId) return;
        this._teardownReviewSocket();
        this._reviewTerminalSeen = false;

        const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
        const url = `${proto}//${window.location.host}/ws/jobs/${encodeURIComponent(
          reviewJobId
        )}`;

        let socket;
        try {
          socket = new WebSocket(url);
        } catch (err) {
          this.reviewing = false;
          this.reviewJobId = "";
          this.reviewError =
            err && err.message
              ? `Could not open review WebSocket: ${err.message}`
              : "Could not open review WebSocket.";
          return;
        }
        this._reviewSocket = socket;

        socket.addEventListener("message", (event) => {
          this._handleReviewMessage(event.data, parentJobId);
        });
        socket.addEventListener("error", () => {
          // eslint-disable-next-line no-console
          console.warn("[re-render] Review WebSocket error event fired");
        });
        socket.addEventListener("close", (event) => {
          this._handleReviewClose(event);
        });
      },

      _handleReviewMessage(raw, parentJobId) {
        let payload;
        try {
          payload = JSON.parse(raw);
        } catch (_err) {
          // eslint-disable-next-line no-console
          console.warn(
            "[re-render] Dropping non-JSON review WebSocket frame",
            raw
          );
          return;
        }
        if (!payload || typeof payload !== "object") return;

        switch (payload.type) {
          case "progress":
            // We don't render reviewer progress lines in the panel
            // -- the reviewer is short-running and the existing
            // radar chart is the primary feedback channel. Silent
            // drop is correct.
            break;
          case "status":
            this._handleReviewStatus(payload, parentJobId);
            break;
          case "result":
            this._finalizeReview(payload.data || null, parentJobId);
            break;
          default:
            // eslint-disable-next-line no-console
            console.warn("[re-render] Unknown review message type", payload.type);
        }
      },

      _handleReviewStatus(payload, parentJobId) {
        const status = payload && payload.status;
        if (status === "completed") {
          this._reviewTerminalSeen = true;
        } else if (status === "failed") {
          this._reviewTerminalSeen = true;
          this.reviewError =
            typeof payload.error === "string" && payload.error
              ? payload.error
              : "Review-only run failed with no error message.";
          this.reviewing = false;
        }
        // Suppress unused-arg lint -- parentJobId is read in the
        // `_finalizeReview` path, not here.
        void parentJobId;
      },

      /**
       * On a successful reviewer-only run, dispatch a `review-updated`
       * window event carrying the full review payload so any listener
       * (e.g. the existing radar chart component) can apply it directly
       * WITHOUT refetching.
       *
       * Why we pass the payload instead of a refetch hint: the
       * reviewer-only child writes its score to the CHILD job's result,
       * NOT the parent's. A `GET /api/jobs/{parentJobId}/review` would
       * return stale data and break AC7. The data is already on the
       * `result` WebSocket frame -- thread it straight through.
       *
       * The event detail is
       *   {jobId, review, retriesUsed, feedbackHistory}
       * where `jobId` is the parent render's id (informational only --
       * the listener should not gate on it because the chart's tracked
       * id is the originally promoted parent and may not match for
       * Re-renders).
       */
      _finalizeReview(data, parentJobId) {
        const review =
          data && data.review && typeof data.review === "object"
            ? data.review
            : null;
        const retriesUsed =
          data && Number.isFinite(data.retries_used) ? data.retries_used : 0;
        const feedbackHistory =
          data && Array.isArray(data.feedback_history)
            ? data.feedback_history.filter((s) => typeof s === "string")
            : [];
        try {
          window.dispatchEvent(
            new CustomEvent("review-updated", {
              detail: {
                jobId: parentJobId || this.reviewJobId,
                review,
                retriesUsed,
                feedbackHistory,
              },
            })
          );
        } catch (_err) {
          // Older browsers may throw on CustomEvent constructor in
          // weird contexts -- fall back to a plain Event so the
          // listener still fires (without detail) instead of dropping
          // the signal entirely.
          try {
            const fallback = document.createEvent("Event");
            fallback.initEvent("review-updated", true, true);
            window.dispatchEvent(fallback);
          } catch (_fallbackErr) {
            // Best effort.
          }
        }
      },

      _handleReviewClose(event) {
        this._reviewSocket = null;
        if (this._suppressReviewClose) {
          this._suppressReviewClose = false;
          this.reviewing = false;
          this.reviewJobId = "";
          return;
        }
        if (this._reviewTerminalSeen) {
          this.reviewing = false;
          this.reviewJobId = "";
          return;
        }
        if (event && event.code === UNKNOWN_JOB_CLOSE_CODE) {
          this.reviewError = `Unknown review job id: ${this.reviewJobId}`;
        } else {
          const codeSuffix =
            event && typeof event.code === "number"
              ? ` (code ${event.code})`
              : "";
          this.reviewError =
            "Review WebSocket closed before reporting a terminal status" +
            codeSuffix +
            ".";
        }
        this.reviewing = false;
        this.reviewJobId = "";
      },

      _teardownReviewSocket() {
        const socket = this._reviewSocket;
        if (!socket) return;
        this._reviewSocket = null;
        try {
          if (
            socket.readyState === WebSocket.OPEN ||
            socket.readyState === WebSocket.CONNECTING
          ) {
            this._suppressReviewClose = true;
            socket.close(1000, "review teardown");
          }
        } catch (_err) {
          // Best-effort.
        }
      },

      // ------------------------------------------------------------- #
      // Helpers                                                        #
      // ------------------------------------------------------------- #

      /**
       * Build the EditPlan body the PUT and POST /re-render endpoints
       * both expect. Returns `null` when the prerequisites are missing
       * (no plan, no entries, no cached brief from the editControls
       * mixin) so the caller can show a banner.
       *
       * Shape MUST match the body editControls.savePlan() builds. If
       * that ever drifts the re-render POST will start failing 422 --
       * keep them in sync.
       */
      _buildEditPlanBody() {
        if (!this.plan || !Array.isArray(this.plan.entries)) return null;
        if (!this.jobBrief) return null;

        const totalDuration = this.plan.entries.reduce((sum, entry) => {
          const start = Number(entry.start_trim);
          const end = Number(entry.end_trim);
          if (!Number.isFinite(start) || !Number.isFinite(end)) return sum;
          return sum + (end - start);
        }, 0);

        return {
          brief: this.jobBrief,
          music_path: this.jobMusicPath,
          total_duration: Number(totalDuration) || 0,
          entries: this.plan.entries.map((entry) => ({
            shot_id: entry.shot_id,
            start_trim: Number(entry.start_trim),
            end_trim: Number(entry.end_trim),
            position: Number(entry.position),
            text_overlay: entry.text_overlay == null ? null : entry.text_overlay,
            transition: entry.transition == null ? null : entry.transition,
          })),
        };
      },

      /**
       * Dispatch a `re-render-active-changed` window event with the
       * newly-active render entry so listeners (e.g. an out-of-scope
       * video player or a future preview thumbnail strip) can pick up
       * the change without polling. Falls back to a plain Event if the
       * CustomEvent constructor is unavailable.
       */
      _dispatchActiveChanged() {
        const entry = this.activeRender();
        if (!entry) return;
        try {
          window.dispatchEvent(
            new CustomEvent("re-render-active-changed", {
              detail: {
                jobId: entry.job_id,
                videoUrl: entry.video_url,
                finalVideoPath: entry.final_video_path,
                label: entry.label,
              },
            })
          );
        } catch (_err) {
          try {
            const fallback = document.createEvent("Event");
            fallback.initEvent("re-render-active-changed", true, true);
            window.dispatchEvent(fallback);
          } catch (_fallbackErr) {
            // Best effort.
          }
        }
      },

      /**
       * True when the Re-render button should be enabled. Mirrors the
       * Save button's gate (must be dirty, no per-field errors, not
       * already saving) and adds "no render is in flight". Exposed as
       * a method so the template can `:disabled="!canReRender()"`.
       */
      canReRender() {
        if (!this.plan) return false;
        if (this.rendering || this.saving) return false;
        if (typeof this.hasAnyError === "function" && this.hasAnyError()) {
          return false;
        }
        if (Array.isArray(this.planErrors) && this.planErrors.length > 0) {
          return false;
        }
        return Boolean(this.dirty);
      },

      /**
       * True when the Review button should be enabled. The button is
       * only meaningful once at least one render exists in the version
       * list, and is disabled while a reviewer-only job is in flight.
       */
      canReviewOnly() {
        if (this.reviewing) return false;
        return this.hasAnyRender();
      },
    };
  }

  // Expose as a global so the Alpine template can spread this mixin
  // alongside `timelineView()` + `editControls()` via
  // `x-data="Object.assign(timelineView(), editControls(), reRenderControls())"`.
  // No ES-module export needed -- this script is loaded via a plain
  // <script defer> tag that executes BEFORE the Alpine CDN script,
  // same pattern as edit-controls.js / timeline.js / chat.js.
  window.reRenderControls = reRenderControls;
})();
