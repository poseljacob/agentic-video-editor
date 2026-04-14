/**
 * AVE Studio -- Review Chart (US-006)
 *
 * Alpine.js v3 component factory that drives the reviewer scorecard view.
 * Registers itself as `window.reviewChart` so the template can bind via
 * `x-data="reviewChart()"` without needing ES modules or a build step --
 * same pattern as brief-builder.js / progress.js / video-player.js.
 *
 * Responsibilities:
 *   1. Receive a job id from the shared Alpine root scope and fetch
 *      GET /api/jobs/{job_id}/review on demand.
 *   2. Parse the server response shape landed in Phase 1 (commit 09a554a):
 *        {
 *          "review": { adherence, pacing, visual_quality, watchability,
 *                      overall, feedback } | null,
 *          "retries_used": 0,
 *          "feedback_history": [ "string", "string", ... ]
 *        }
 *   3. Lazy-build a Chart.js v4 radar chart over the 5 ReviewScore
 *      dimensions, pinned to a 0..1 scale so it doesn't auto-scale based
 *      on data (the PRD specifies min=0 / max=1 / step=0.2 -- required for
 *      the edge cases of "all zeros" and "all ones").
 *   4. Render the reviewer feedback string in a styled card.
 *   5. When `retries_used > 0`, surface each attempt's feedback string in
 *      a collapsible history section.
 *   6. Handle 409 (job exists but has no result yet) as a friendly
 *      "no review yet" state rather than an error, and 404 / network as
 *      actionable errors.
 *   7. Destroy the Chart.js instance on jobId change / unmount to avoid
 *      the canvas-reuse error Chart.js throws if the same <canvas> gets
 *      a second controller.
 */
(function () {
  "use strict";

  /**
   * Canonical ReviewScore dimensions to surface on the radar chart. The
   * order matches src/models/schemas.py::ReviewScore and places `overall`
   * last so it reads as the summary of the four contributing dimensions.
   * `key` matches the Pydantic field name; `label` is the human-readable
   * axis label the Chart.js tooltip shows.
   */
  const DIMENSIONS = [
    { key: "adherence", label: "Adherence" },
    { key: "pacing", label: "Pacing" },
    { key: "visual_quality", label: "Visual Quality" },
    { key: "watchability", label: "Watchability" },
    { key: "overall", label: "Overall" },
  ];

  /**
   * Radar fill + border color. Emerald to match the rest of the UI (the
   * brief-builder submit button and progress indicator both use the
   * Tailwind emerald-500 / emerald-600 palette). Semi-transparent fill so
   * the grid lines underneath stay visible.
   */
  const RADAR_FILL = "rgba(16, 185, 129, 0.25)";
  const RADAR_BORDER = "rgba(16, 185, 129, 1)";
  const RADAR_POINT = "rgba(16, 185, 129, 1)";

  /**
   * Build the initial Alpine state object. Exposed as a factory so each
   * `x-data="reviewChart()"` binding gets isolated state -- the review
   * section is hidden via x-show, so Alpine keeps one instance alive, but
   * a future plan-mode variant that renders multiple charts on the same
   * page would still work without sharing Chart.js instances.
   */
  function reviewChart() {
    return {
      // --- Job tracking (mirrored from the root scope via x-effect) ---
      jobId: "",

      /**
       * Mirror of the root scope's `view` field, plumbed in via x-effect
       * on the template. Used to drive a "refetch when the user returns
       * to the Review tab" watcher so a 409 "no review yet" state picked
       * up mid-run is replaced with real data after the pipeline finishes
       * and the user flips back to the tab. See init().
       */
      activeView: "",

      // --- Fetched state ---
      review: null,
      retriesUsed: 0,
      feedbackHistory: [],

      // --- UI state ---
      loading: false,
      error: "",
      /**
       * Distinguishes "server says no review yet" (409) from an actual
       * error. The template shows a muted empty-state message for this
       * flag and a red error banner for `error`.
       */
      noReviewYet: false,

      // --- Chart.js bookkeeping ---
      /** Live Chart.js instance reference, or null when no chart is drawn. */
      _chart: null,

      /** Dimension list exposed to the template for feedback-card labels. */
      dimensions: DIMENSIONS,

      /**
       * Alpine lifecycle hook. Watches `jobId` so the component refetches
       * whenever the shared root scope promotes a new job. Alpine v3
       * calls this automatically on mount -- do NOT add `x-init="init()"`
       * to the template or the watcher will register twice and double-fire
       * every fetch (same rule as progress.js / video-player.js).
       */
      init() {
        this.$watch("jobId", (next, prev) => {
          // Any change (including clearing) tears down the old chart
          // first so a canvas reuse error can't happen.
          this._destroyChart();
          this._reset();
          if (next && next !== prev) {
            this._fetchReview(next);
          }
        });
        // Edge case: the user may have opened the Review tab while the
        // pipeline was still running, in which case the first fetch
        // returned a 409 "no review yet" and `noReviewYet` stuck. The
        // jobId never changes (promotedJobId is stable across the
        // completed job), so the jobId watcher above won't re-fire.
        // Instead, refetch whenever the user flips back to the Review
        // tab from somewhere else AND we're in a retry-worthy state
        // (empty / errored). Normal happy-path review renders skip this
        // because `hasReview()` short-circuits the check.
        this.$watch("activeView", (next, prev) => {
          if (next !== "review" || prev === "review") return;
          if (!this.jobId) return;
          if (this.loading) return;
          if (this.noReviewYet || this.error) {
            this._fetchReview(this.jobId);
          }
        });

        // US-010: apply a fresh review when a reviewer-only re-run
        // completes. The re-render mixin dispatches `review-updated`
        // with `{detail: {jobId, review, retriesUsed, feedbackHistory}}`
        // after a successful POST /api/jobs/{id}/review-only.
        //
        // We do NOT refetch from `GET /api/jobs/{this.jobId}/review`:
        // the reviewer-only run writes its score onto a NEW child job's
        // result, not the originally promoted parent's, so a refetch
        // would return stale data. Instead, apply the payload from
        // detail directly -- the data is already sourced from the
        // canonical PipelineResult dump on the WebSocket result frame.
        //
        // We also no longer gate on `targetJobId === this.jobId`,
        // because for Re-renders the dispatched jobId is the editor-only
        // child's id which by construction does not match the chart's
        // tracked promoted-parent id. Any review-only run on any render
        // in the active session should update the chart -- the chart
        // shows one review at a time per the AC.
        //
        // Stored on the instance so destroy() can detach it and a
        // teardown of the timeline view doesn't leak the listener.
        this._reviewUpdatedHandler = (event) => {
          const detail =
            event && typeof event === "object" && event.detail
              ? event.detail
              : null;
          if (!detail) return;
          if (!this.jobId) return;
          if (detail.review && typeof detail.review === "object") {
            this._applyReview({
              review: detail.review,
              retries_used: detail.retriesUsed,
              feedback_history: detail.feedbackHistory,
            });
          }
        };
        window.addEventListener("review-updated", this._reviewUpdatedHandler);

        // If the template assigned jobId before init fired, honor it.
        if (this.jobId) {
          this._fetchReview(this.jobId);
        }
      },

      /**
       * Alpine destroy hook. Alpine calls this when the component
       * unmounts (e.g. an x-if ancestor flips false). We also call
       * _destroyChart() from the jobId watcher so a job change has the
       * same effect without waiting for unmount.
       */
      destroy() {
        this._destroyChart();
        if (this._reviewUpdatedHandler) {
          window.removeEventListener(
            "review-updated",
            this._reviewUpdatedHandler
          );
          this._reviewUpdatedHandler = null;
        }
      },

      /**
       * Reset local state for a fresh fetch. Keeps jobId intact -- the
       * caller is the one who just set it, so overwriting it here would
       * cause an infinite $watch loop.
       */
      _reset() {
        this.review = null;
        this.retriesUsed = 0;
        this.feedbackHistory = [];
        this.loading = false;
        this.error = "";
        this.noReviewYet = false;
      },

      /**
       * Fetch GET /api/jobs/{jobId}/review and dispatch the three
       * interesting outcomes:
       *   - 200 with a non-null review  -> render chart + feedback
       *   - 200 with a null review      -> treat as "no review yet"
       *   - 409                         -> "no review yet" (pending/failed)
       *   - 404 / network / parse error -> error banner
       */
      async _fetchReview(jobId) {
        this.loading = true;
        this.error = "";
        this.noReviewYet = false;

        let response;
        try {
          response = await fetch(
            `/api/jobs/${encodeURIComponent(jobId)}/review`,
            { headers: { Accept: "application/json" } }
          );
        } catch (err) {
          // Network failure, CORS, DNS, etc. -- fetch itself threw.
          this.loading = false;
          this.error =
            err && err.message
              ? `Could not reach the review endpoint: ${err.message}`
              : "Could not reach the review endpoint.";
          return;
        }

        // 409 -- the job exists but has no result yet. Surface as an
        // empty state, not a hard error, so a user who opens the review
        // tab mid-run sees something friendlier than a red banner.
        if (response.status === 409) {
          this.loading = false;
          this.noReviewYet = true;
          return;
        }

        if (!response.ok) {
          // 404 (unknown job) and any 5xx land here.
          this.loading = false;
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
            `Review request failed (${response.status} ${response.statusText}).`;
          return;
        }

        let payload;
        try {
          payload = await response.json();
        } catch (err) {
          this.loading = false;
          this.error = "Review response was not valid JSON.";
          return;
        }

        this.loading = false;
        this._applyReview(payload);
      },

      /**
       * Apply a review payload to local state and (re)draw the chart.
       * Shared by `_fetchReview` (after the GET resolves) and by the
       * `review-updated` window event listener used by US-010 reviewer-
       * only re-runs. The shape matches `GET /api/jobs/{id}/review`:
       *   {review: {...} | null, retries_used, feedback_history}
       *
       * Sets `noReviewYet` when `review` is missing so the same empty
       * state path used by 409 responses also fires here.
       */
      _applyReview(payload) {
        const review =
          payload && typeof payload === "object" ? payload.review : null;
        this.review = review && typeof review === "object" ? review : null;
        this.retriesUsed =
          payload && Number.isFinite(payload.retries_used)
            ? payload.retries_used
            : 0;
        this.feedbackHistory =
          payload && Array.isArray(payload.feedback_history)
            ? payload.feedback_history.filter((s) => typeof s === "string")
            : [];

        if (!this.review) {
          // No review payload -- treat the same as a 409 "no review
          // yet" so the empty state renders.
          this.noReviewYet = true;
          return;
        }

        // A successful apply clears the empty / error states left over
        // from any prior fetch attempts.
        this.noReviewYet = false;
        this.error = "";

        // Defer chart creation to the next tick so Alpine has finished
        // rendering the <canvas> element that appears under the x-show.
        this.$nextTick(() => {
          this._renderChart();
        });
      },

      /**
       * Create a Chart.js radar chart on the component's `$refs.canvas`
       * element. Always destroys the prior chart first so a second fetch
       * against a fresh jobId doesn't hit the Chart.js canvas-reuse
       * error ("Canvas is already in use").
       */
      _renderChart() {
        const canvas = this.$refs.canvas;
        if (!canvas) {
          // The canvas isn't in the DOM yet -- usually because x-show on
          // the review section is still false. Nothing to do; the next
          // fetch will re-run this path after Alpine mounts the canvas.
          return;
        }
        if (typeof window.Chart !== "function") {
          // Chart.js hasn't loaded. Script ordering in index.html puts
          // Chart.js before Alpine, so this is a configuration bug --
          // surface it clearly instead of silently showing a blank box.
          this.error =
            "Chart.js failed to load -- cannot render the review radar.";
          return;
        }

        this._destroyChart();

        const data = DIMENSIONS.map((dim) => {
          const value = this.review ? this.review[dim.key] : null;
          return typeof value === "number" ? value : 0;
        });

        this._chart = new window.Chart(canvas, {
          type: "radar",
          data: {
            labels: DIMENSIONS.map((dim) => dim.label),
            datasets: [
              {
                label: "ReviewScore",
                data,
                backgroundColor: RADAR_FILL,
                borderColor: RADAR_BORDER,
                pointBackgroundColor: RADAR_POINT,
                pointBorderColor: "#0f172a",
                pointHoverBackgroundColor: "#0f172a",
                pointHoverBorderColor: RADAR_BORDER,
                borderWidth: 2,
              },
            ],
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
              legend: { display: false },
              tooltip: {
                // Default tooltip is fine per the PRD -- just make sure
                // the label is the human-friendly dimension name (which
                // Chart.js takes from `labels` by default).
                callbacks: {
                  label: (ctx) => {
                    const value =
                      typeof ctx.parsed === "object" && ctx.parsed !== null
                        ? ctx.parsed.r
                        : ctx.parsed;
                    const formatted =
                      typeof value === "number" ? value.toFixed(2) : value;
                    return `${ctx.label}: ${formatted}`;
                  },
                },
              },
            },
            scales: {
              r: {
                // Pin the scale so the chart doesn't auto-rescale when
                // the data happens to all be zeros or all be ones --
                // required by the PRD edge cases.
                min: 0,
                max: 1,
                ticks: {
                  stepSize: 0.2,
                  color: "rgba(148, 163, 184, 0.9)", // slate-400
                  backdropColor: "transparent",
                  showLabelBackdrop: false,
                },
                grid: { color: "rgba(148, 163, 184, 0.25)" },
                angleLines: { color: "rgba(148, 163, 184, 0.25)" },
                pointLabels: {
                  color: "rgba(226, 232, 240, 0.95)", // slate-200
                  font: { size: 12 },
                },
              },
            },
          },
        });
      },

      /**
       * Destroy the active Chart.js instance, if any. Safe to call
       * repeatedly. Chart.js raises an error on the next `new Chart(...)`
       * against the same canvas unless the previous instance was
       * destroyed first -- this is why every state transition routes
       * through here.
       */
      _destroyChart() {
        const chart = this._chart;
        if (!chart) return;
        this._chart = null;
        try {
          chart.destroy();
        } catch (_err) {
          // Best-effort: if destroy throws we can't do anything useful.
        }
      },

      // ---------------------------------------------------------------- #
      // Template helpers                                                  #
      // ---------------------------------------------------------------- #

      /** True when a usable ReviewScore is available to render. */
      hasReview() {
        return Boolean(this.review);
      },

      /** True when a retry history exists and is worth surfacing. */
      hasHistory() {
        return this.retriesUsed > 0 && this.feedbackHistory.length > 0;
      },

      /**
       * Format a single ReviewScore dimension value for the feedback
       * card grid. Returns "n/a" on missing values so the template can
       * render each row without a per-field x-if wrapper.
       */
      dimensionValue(key) {
        if (!this.review) return "n/a";
        const value = this.review[key];
        if (value === undefined || value === null) return "n/a";
        return typeof value === "number" ? value.toFixed(2) : String(value);
      },
    };
  }

  // Expose as a global so Alpine's x-data="reviewChart()" can find it.
  // No ES-module export needed -- this script is loaded via a plain
  // <script defer> tag that executes BEFORE the Alpine CDN script, just
  // like brief-builder.js / progress.js / video-player.js.
  window.reviewChart = reviewChart;
})();
