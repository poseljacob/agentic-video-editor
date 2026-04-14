/**
 * AVE Studio -- Timeline Viewer (US-008)
 *
 * Alpine.js v3 component factory that drives the EditPlan timeline view.
 * Registers itself as `window.timelineView` so the template can bind via
 * `x-data="timelineView()"` without needing ES modules or a build step --
 * same pattern as brief-builder.js / progress.js / video-player.js /
 * review-chart.js / chat.js.
 *
 * Responsibilities:
 *   1. Receive a job id from the shared Alpine root scope and fetch
 *      GET /api/jobs/{job_id}/edit-plan on demand.
 *   2. Parse the server response shape:
 *        {
 *          "job_id": "uuid",
 *          "total_duration": 45.2,
 *          "entry_count": 7,
 *          "entries": [
 *            {
 *              "position": 0,
 *              "shot_id": "path/to/clip.mp4#12.5",
 *              "source_file": "path/to/clip.mp4",
 *              "source_filename": "clip.mp4",
 *              "source_timestamp": 12.5,
 *              "display_label": "clip.mp4@12.5s",
 *              "start_trim": 0.0,
 *              "end_trim": 3.2,
 *              "duration": 3.2,
 *              "text_overlay": "Hook line" | null,
 *              "transition": null,
 *              "roll_type": "a-roll" | "b-roll" | "unknown",
 *              "thumbnail_url": "/api/clips/{job_id}/0/thumbnail"
 *            }
 *          ]
 *        }
 *   3. Handle 409 (job exists but has no edit plan yet) as a friendly
 *      "no edit plan yet" state rather than an error, and 404 / network
 *      as actionable errors.
 *   4. Gate fetches so re-entering the Timeline tab does NOT refetch the
 *      same plan -- tracked via an internal `_loadedJobId` tracker that
 *      mirrors the review-chart.js "refetch only on jobId change" pattern.
 */
(function () {
  "use strict";

  /**
   * Build the initial Alpine state object. Exposed as a factory so each
   * `x-data="timelineView()"` binding gets isolated state -- the timeline
   * section is hidden via x-show, so Alpine keeps one instance alive
   * across view flips, but a future plan-mode variant that renders
   * multiple timelines on the same page would still work without
   * cross-contamination.
   */
  function timelineView() {
    return {
      // --- Job tracking (mirrored from the root scope via x-effect) ---
      jobId: "",

      /**
       * Mirror of the root scope's `view` field, plumbed in via x-effect
       * on the template. Used to gate the initial fetch so we only hit
       * the endpoint once the user actually navigates to the Timeline
       * tab, and to drive a "refetch when the user returns to the
       * Timeline tab after an error" watcher (same pattern as
       * review-chart.js::init).
       */
      activeView: "",

      // --- Fetched state ---
      /** Full response body from GET /api/jobs/{id}/edit-plan, or null. */
      plan: null,

      // --- UI state ---
      loading: false,
      /**
       * Error banner text for 404 / network / parse failures. Empty
       * string when no error is active. Distinct from `noPlanYet` below
       * so the template can render a muted empty-state for 409 and a
       * red-ish banner for real errors.
       */
      error: "",

      /**
       * Distinguishes "server says no edit plan yet" (409) from an
       * actual error. The template shows a friendly muted message for
       * this flag so a user who opens the Timeline tab mid-run does
       * not see a hard error banner.
       */
      noPlanYet: false,

      // --- Fetch gating ---
      /**
       * Tracks which jobId we have already fetched (or are currently
       * fetching). Used to make repeated tab switches on the same job
       * idempotent -- without this, every x-effect pass on activeView
       * would trigger a redundant refetch against the same endpoint.
       * Cleared on jobId change via the $watch registered in init().
       */
      _loadedJobId: "",

      /**
       * Alpine lifecycle hook. Watches `jobId` so the component refetches
       * whenever the shared root scope promotes a new job, and watches
       * `activeView` so the first fetch only happens once the user is
       * actually on the Timeline tab. Alpine v3 calls this automatically
       * on mount -- do NOT add `x-init="init()"` to the template or the
       * watcher will register twice and double-fire every fetch (same
       * rule as review-chart.js / progress.js / video-player.js).
       */
      init() {
        this.$watch("jobId", (next, prev) => {
          // Any change (including clearing) resets local state so a
          // stale plan from a previous job can't flash through.
          this._reset();
          this._loadedJobId = "";
          if (next && next !== prev) {
            this._maybeFetch();
          }
        });
        // Refetch on tab re-entry if we're in a retry-worthy state
        // (no plan loaded, or 409 / error on the last attempt). Happy
        // path renders short-circuit via `_loadedJobId === jobId`, so
        // returning to a successfully-loaded tab does NOT refetch.
        this.$watch("activeView", (next, prev) => {
          if (next !== "timeline" || prev === "timeline") return;
          this._maybeFetch();
        });
        // If the template assigned jobId + activeView before init
        // fired, honor them.
        this._maybeFetch();
      },

      /**
       * Gate the actual fetch. Only runs when:
       *   - the user is on the Timeline tab (activeView === 'timeline')
       *   - a job id is set
       *   - we haven't already loaded (or failed a terminal load of)
       *     this exact jobId
       *   - there isn't a fetch already in flight
       *
       * Mirrors the reviewChart()::_fetchReview gating -- the goal is
       * "enter tab once, fetch once; enter tab again, no-op".
       */
      _maybeFetch() {
        if (this.activeView !== "timeline") return;
        if (!this.jobId) return;
        if (this.loading) return;
        if (this._loadedJobId === this.jobId && (this.plan || this.noPlanYet)) {
          return;
        }
        this._fetchPlan(this.jobId);
      },

      /**
       * Reset local state for a fresh fetch. Keeps jobId intact -- the
       * caller is the one who just set it, so overwriting it here would
       * cause an infinite $watch loop.
       */
      _reset() {
        this.plan = null;
        this.loading = false;
        this.error = "";
        this.noPlanYet = false;
      },

      /**
       * Fetch GET /api/jobs/{jobId}/edit-plan and dispatch the
       * interesting outcomes:
       *   - 200 with a valid plan       -> render the timeline
       *   - 409                          -> "no plan yet" empty state
       *   - 404                          -> friendly "job not found"
       *   - network / parse error        -> user-readable error banner
       *
       * Exposed as `load(jobId)` too so tests / external callers can
       * force a fetch without touching internal state.
       */
      async load(jobId) {
        this.jobId = jobId;
        return this._fetchPlan(jobId);
      },

      async _fetchPlan(jobId) {
        this.loading = true;
        this.error = "";
        this.noPlanYet = false;
        this.plan = null;

        let response;
        try {
          response = await fetch(
            `/api/jobs/${encodeURIComponent(jobId)}/edit-plan`,
            { headers: { Accept: "application/json" } }
          );
        } catch (err) {
          this.loading = false;
          this._loadedJobId = jobId;
          this.error =
            err && err.message
              ? `Could not reach the edit-plan endpoint: ${err.message}`
              : "Could not reach the edit-plan endpoint.";
          return;
        }

        // 409 -- the job exists but has no edit plan yet (still running
        // or failed before the editor step). Surface as an empty state
        // so the user sees something friendlier than a red banner.
        if (response.status === 409) {
          this.loading = false;
          this._loadedJobId = jobId;
          this.noPlanYet = true;
          return;
        }

        if (response.status === 404) {
          this.loading = false;
          this._loadedJobId = jobId;
          this.error = "Job not found.";
          return;
        }

        if (!response.ok) {
          this.loading = false;
          this._loadedJobId = jobId;
          let detail = "";
          try {
            const body = await response.json();
            if (body && typeof body.detail === "string") {
              detail = body.detail;
            }
          } catch (_err) {
            // non-JSON body -- fall back to status text
          }
          this.error =
            detail ||
            `Edit plan request failed (${response.status} ${response.statusText}).`;
          return;
        }

        let payload;
        try {
          payload = await response.json();
        } catch (_err) {
          this.loading = false;
          this._loadedJobId = jobId;
          this.error = "Edit plan response was not valid JSON.";
          return;
        }

        this.loading = false;
        this._loadedJobId = jobId;

        if (!payload || typeof payload !== "object") {
          this.error = "Edit plan response was malformed.";
          return;
        }

        // Defensive: the backend promises a well-formed shape, but a
        // future refactor could regress this -- fall through to a
        // friendly empty state instead of throwing on a missing field.
        const entries = Array.isArray(payload.entries) ? payload.entries : [];
        this.plan = {
          job_id: typeof payload.job_id === "string" ? payload.job_id : jobId,
          total_duration: Number.isFinite(payload.total_duration)
            ? payload.total_duration
            : 0,
          entry_count: Number.isFinite(payload.entry_count)
            ? payload.entry_count
            : entries.length,
          entries,
        };
      },

      // ---------------------------------------------------------------- #
      // Template helpers                                                  #
      // ---------------------------------------------------------------- #

      /**
       * Format a duration in seconds as a fixed-precision label with
       * one decimal and an "s" suffix. Used for start_trim, end_trim,
       * duration, and total_duration in the card metadata grid.
       */
      formatTime(seconds) {
        if (seconds === null || seconds === undefined) return "n/a";
        const n = Number(seconds);
        if (!Number.isFinite(n)) return "n/a";
        return `${n.toFixed(1)}s`;
      },

      /**
       * Tailwind classes for the roll-type pill badge on each card.
       * a-roll = blue, b-roll = green, anything else (including
       * "unknown" and missing values) = slate. Kept as three discrete
       * return values so Tailwind's JIT can statically detect them.
       */
      rollBadgeClasses(rollType) {
        if (rollType === "a-roll") {
          return "bg-blue-500/20 text-blue-200 ring-1 ring-blue-400/40";
        }
        if (rollType === "b-roll") {
          return "bg-green-500/20 text-green-200 ring-1 ring-green-400/40";
        }
        return "bg-slate-500/20 text-slate-300 ring-1 ring-slate-400/40";
      },

      /**
       * Tailwind classes for the whole-card left border tint so the
       * roll type is visible even when the badge is offscreen. Mirrors
       * the palette in `rollBadgeClasses` above.
       */
      cardClasses(rollType) {
        if (rollType === "a-roll") {
          return "border-l-4 border-blue-400";
        }
        if (rollType === "b-roll") {
          return "border-l-4 border-green-400";
        }
        return "border-l-4 border-slate-500";
      },
    };
  }

  // Expose as a global so Alpine's x-data="timelineView()" can find it.
  // No ES-module export needed -- this script is loaded via a plain
  // <script defer> tag that executes BEFORE the Alpine CDN script, just
  // like brief-builder.js / progress.js / video-player.js / review-chart.js.
  window.timelineView = timelineView;
})();
