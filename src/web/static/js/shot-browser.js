/**
 * AVE Studio -- Shot Browser Modal (US-009)
 *
 * Alpine.js v3 component factory that powers the shot-swap modal in the
 * timeline view. Registers itself as `window.shotBrowser` so the timeline
 * template can bind via `x-data="shotBrowser()"` without needing ES
 * modules or a build step -- same pattern as timeline.js /
 * review-chart.js / edit-controls.js.
 *
 * Responsibilities
 * ----------------
 *  1. Receive a `footageIndexPath` + `target` position from the parent
 *     edit-controls scope (via `x-effect` on the template) -- the modal
 *     is rendered inside the timeline `<section>` so it can read the
 *     edit-controls mixin state directly.
 *  2. Debounce user input in the search field (200 ms) and POST nothing
 *     until the user has typed at least one non-whitespace character.
 *     Fetches GET /api/footage/search with the current query +
 *     footage_index_path.
 *  3. Render the results list with the fields the endpoint returns:
 *     display_label, description, transcript snippet, relevance_score,
 *     roll_type. Clicking a result dispatches `shot-swap-result` up to
 *     the edit-controls mixin via a callback passed in from the
 *     template, which then patches the edit plan entry in place.
 *  4. Close on ESC key or on a click to the backdrop overlay, clearing
 *     local state so the next open starts from a clean slate.
 *
 * Integration surface with edit-controls
 * --------------------------------------
 * The parent `editControls()` mixin owns `swapTarget` (number: entry
 * position, or -1 when no modal is open) and exposes:
 *
 *   applyShotSwap(shotObject) -- patches the entry at swapTarget
 *   closeSwapModal()          -- flips swapTarget back to -1
 *
 * Inside the shot-browser `x-data`, the template plumbs these via an
 * x-effect that copies `swapTarget` and `jobFootageIndexPath` from the
 * parent scope, then calls `shotBrowser`-local methods from the UI.
 */
(function () {
  "use strict";

  /**
   * Debounce interval for the search input. 200 ms is short enough to
   * feel instant on a fast local search and long enough to collapse
   * typical typing into a single request. Matches the story spec.
   */
  const SEARCH_DEBOUNCE_MS = 200;

  /**
   * Build the shot-browser state. Factory form matches the style of
   * timelineView() / reviewChart() / editControls() so the module
   * surface is uniform across the static/js directory.
   */
  function shotBrowser() {
    return {
      // ------------------------------------------------------------- #
      // Props (plumbed in from the parent edit-controls scope)         #
      // ------------------------------------------------------------- #

      /** -1 when closed; entry position when open. */
      swapTargetLocal: -1,

      /** Mirror of the job's footage_index_path from GET /api/jobs. */
      footagePath: "",

      // ------------------------------------------------------------- #
      // Local state                                                    #
      // ------------------------------------------------------------- #

      /** Current text in the search input. */
      query: "",

      /** Array of shot result objects returned by the search endpoint. */
      results: [],

      /** True while a fetch is in flight. */
      loading: false,

      /** Error banner text (404 / 422 / network). Empty string when clean. */
      error: "",

      /** True after the first non-empty query has been submitted. */
      hasSearched: false,

      /** Debounce handle returned by setTimeout; cleared on cancel. */
      _debounceHandle: null,

      /** Monotonically-incrementing request id to ignore stale responses. */
      _requestSeq: 0,

      // ------------------------------------------------------------- #
      // Lifecycle                                                      #
      // ------------------------------------------------------------- #

      /**
       * Alpine lifecycle hook. Watches `swapTargetLocal` so the modal
       * resets its search state every time it opens (fresh query, no
       * stale results from a prior swap) and attaches/detaches the
       * global ESC handler so the user can dismiss the modal without
       * mouse interaction.
       */
      init() {
        this.$watch("swapTargetLocal", (next, prev) => {
          if (next !== -1 && prev === -1) {
            this._onOpen();
          } else if (next === -1 && prev !== -1) {
            this._onClose();
          }
        });
      },

      /** True when the modal should be visible. */
      isOpen() {
        return this.swapTargetLocal !== -1;
      },

      /**
       * Handle an input event on the search field. Debounces so a
       * quick burst of typing collapses into a single request, then
       * dispatches the actual fetch.
       */
      onInput(event) {
        if (event && event.target) {
          this.query = String(event.target.value || "");
        }
        if (this._debounceHandle) {
          clearTimeout(this._debounceHandle);
          this._debounceHandle = null;
        }
        const trimmed = this.query.trim();
        if (trimmed === "") {
          this.results = [];
          this.error = "";
          this.loading = false;
          this.hasSearched = false;
          return;
        }
        this._debounceHandle = setTimeout(() => {
          this._debounceHandle = null;
          this._runSearch(trimmed);
        }, SEARCH_DEBOUNCE_MS);
      },

      /**
       * Pick a result: returns the shot object for the template to
       * hand to the parent's `applyShotSwap` handler. We don't call
       * the parent method directly from JS because Alpine's nested
       * x-data scopes don't expose the parent via `this` -- instead
       * the template wraps the click handler in a direct call to
       * `applyShotSwap(shot)`, which Alpine resolves via its normal
       * scope chain (inner x-data falls through to outer x-data
       * when a name is missing).
       *
       * This helper is kept so the template can say `pickResult` in
       * case someone wants to add side effects (analytics, etc) on
       * top of the parent call in the future without editing the
       * template.
       */
      pickResult(shot) {
        return shot;
      },

      /**
       * Cancel helper -- the template calls `closeSwapModal()`
       * directly on the outer scope via Alpine's scope chain, so
       * this is just a noop placeholder in case a future consumer
       * wants to hook pre-close behavior.
       */
      cancel() {
        // noop: template wires @click directly to the parent's
        // closeSwapModal() via Alpine scope chain.
      },

      // ------------------------------------------------------------- #
      // Private helpers                                                #
      // ------------------------------------------------------------- #

      /**
       * Run the search fetch. Uses a monotonic `_requestSeq` to drop
       * stale responses: if the user types again before the previous
       * request resolves, the newer request bumps the seq and the
       * older `then`-handler discards its result (prevents race
       * conditions where the old query's results stomp on the new
       * query's display).
       */
      async _runSearch(query) {
        if (!this.footagePath) {
          this.error =
            "No footage_index_path available for this job; cannot search.";
          return;
        }
        this.loading = true;
        this.error = "";
        this.hasSearched = true;
        const mySeq = ++this._requestSeq;

        let response;
        try {
          response = await fetch(
            `/api/footage/search?query=${encodeURIComponent(query)}` +
              `&footage_index_path=${encodeURIComponent(this.footagePath)}`,
            { headers: { Accept: "application/json" } }
          );
        } catch (err) {
          if (mySeq !== this._requestSeq) return;
          this.loading = false;
          this.error =
            err && err.message
              ? `Could not reach the footage search endpoint: ${err.message}`
              : "Could not reach the footage search endpoint.";
          return;
        }

        if (mySeq !== this._requestSeq) return;

        if (response.status === 422) {
          this.loading = false;
          let msg = "Query failed validation.";
          try {
            const body = await response.json();
            if (Array.isArray(body.detail) && body.detail[0]?.msg) {
              msg = body.detail[0].msg;
            }
          } catch (_err) {
            // fall through with generic message
          }
          this.error = msg;
          this.results = [];
          return;
        }

        if (response.status === 404) {
          this.loading = false;
          let detail = "Footage index not found for this job.";
          try {
            const body = await response.json();
            if (body && typeof body.detail === "string") {
              detail = body.detail;
            }
          } catch (_err) {
            // fall through with generic message
          }
          this.error = detail;
          this.results = [];
          return;
        }

        if (!response.ok) {
          this.loading = false;
          this.error = `Search failed (${response.status} ${response.statusText}).`;
          this.results = [];
          return;
        }

        let payload;
        try {
          payload = await response.json();
        } catch (_err) {
          this.loading = false;
          this.error = "Search response was not valid JSON.";
          this.results = [];
          return;
        }

        this.loading = false;
        this.results = Array.isArray(payload.results) ? payload.results : [];
      },

      _onOpen() {
        this.query = "";
        this.results = [];
        this.error = "";
        this.loading = false;
        this.hasSearched = false;
        this._requestSeq++;
        // The ESC keydown listener is registered in the template via
        // `@keydown.escape.window` on the modal wrapper so Alpine's
        // scope chain resolves `closeSwapModal()` against the parent
        // edit-controls mixin without any manual cross-scope bridge.
      },

      _onClose() {
        if (this._debounceHandle) {
          clearTimeout(this._debounceHandle);
          this._debounceHandle = null;
        }
        this._requestSeq++;
      },

      // ------------------------------------------------------------- #
      // Template helpers                                               #
      // ------------------------------------------------------------- #

      /**
       * Format a relevance score (0..1) as a percentage string for
       * the result row's badge. Falls back to "--" on non-numeric.
       */
      formatRelevance(score) {
        const n = Number(score);
        if (!Number.isFinite(n)) return "--";
        return `${Math.round(n * 100)}%`;
      },

      /**
       * Truncate a description or transcript for the result row so
       * the list stays compact even for long shots.
       */
      snippet(text, max) {
        if (!text) return "";
        const s = String(text);
        const limit = Number.isFinite(max) ? max : 120;
        if (s.length <= limit) return s;
        return s.slice(0, limit - 1) + "\u2026";
      },
    };
  }

  // Expose as a global so the Alpine template can bind
  // `x-data="shotBrowser()"` on the modal wrapper. No ES-module
  // export needed -- this script is loaded via a plain <script defer>
  // tag, same pattern as timeline.js / edit-controls.js.
  window.shotBrowser = shotBrowser;
})();
