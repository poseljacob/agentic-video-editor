/**
 * AVE Studio — Video Player (US-005)
 *
 * Alpine.js v3 component factory that wraps an HTML5 <video> element plus
 * a ReviewScore summary block. Registered as `window.videoPlayer` so the
 * progress view template can nest it via `x-data="videoPlayer()"` without
 * needing ES modules or a build step — same pattern as brief-builder.js
 * and progress.js.
 *
 * Scope & integration
 * -------------------
 *
 * This component is intentionally small. It does NOT open WebSockets, POST
 * jobs, or parse pipeline progress — all of that lives in progress.js. The
 * video player is nested INSIDE the progressView section, so it reads the
 * playable URL and the ReviewScore from its parent Alpine scope (the
 * `videoSrc` and `result` fields on `progressView`). The parent is the
 * single source of truth for "what video should we show right now?"; this
 * component is just the presentation layer.
 *
 * Responsibilities:
 *   1. Render a <video controls preload="metadata"> bound to an external
 *      src string provided by the parent.
 *   2. Track its own local `loadError` state so it can show a graceful
 *      "video unavailable" fallback when the browser fails to load the
 *      source (or when the parent never derived a URL).
 *   3. Render the 5 ReviewScore dimensions (adherence, pacing,
 *      visual_quality, watchability, overall) as numeric cards. The
 *      dimension list is declared here so the template can x-for over it
 *      without repeating itself.
 */
(function () {
  "use strict";

  /**
   * Canonical ReviewScore dimensions to surface on the results card. The
   * order is deliberate — `overall` comes last so it reads as the summary
   * of the four contributing dimensions above it. `label` is what the
   * user sees; `key` matches the Pydantic field name on
   * src/models/schemas.py::ReviewScore.
   */
  const REVIEW_DIMENSIONS = [
    { key: "adherence", label: "Adherence" },
    { key: "pacing", label: "Pacing" },
    { key: "visual_quality", label: "Visual quality" },
    { key: "watchability", label: "Watchability" },
    { key: "overall", label: "Overall" },
  ];

  /**
   * Build the initial Alpine state for a video-player instance. Factory
   * (not singleton) so each nested <video> gets isolated state — important
   * because the same progress view may be re-entered after running a
   * second job.
   */
  function videoPlayer() {
    return {
      // --- Surface-level state ---

      /**
       * Set to true when the <video> element fires its `error` event,
       * OR when the parent never derived a usable src. The template uses
       * this to swap the <video> for a fallback message.
       */
      loadError: false,

      /** Dimensions to render in the ReviewScore card. */
      dimensions: REVIEW_DIMENSIONS,

      // ---------------------------------------------------------------- #
      // Lifecycle                                                         #
      // ---------------------------------------------------------------- #

      /**
       * Alpine lifecycle hook. Watches the parent's `videoSrc` so that a
       * fresh job (which will set a new URL) resets the error state and
       * re-attempts load. Uses optional chaining through `$data` so the
       * component still works in isolation (e.g. a future standalone
       * review page that stubs out the parent scope).
       */
      init() {
        // If the parent exposes `videoSrc` (it does — see progress.js),
        // re-run our load-state reset whenever it changes so a second
        // pipeline run doesn't inherit the first run's error banner.
        if (typeof this.$watch === "function") {
          try {
            this.$watch("videoSrc", () => {
              this.loadError = false;
            });
          } catch (_err) {
            // `$watch` throws synchronously if the expression isn't
            // resolvable in this component's scope — harmless in the
            // standalone case, ignore and carry on.
          }
        }
      },

      // ---------------------------------------------------------------- #
      // Template helpers                                                  #
      // ---------------------------------------------------------------- #

      /**
       * True when we have something worth rendering in the <video>
       * element — a non-empty src and no prior load failure. Read from
       * the parent scope via `this.videoSrc` (Alpine exposes parent
       * fields on nested child components automatically).
       */
      hasVideo() {
        return Boolean(this.videoSrc) && !this.loadError;
      },

      /**
       * Called by the <video> element's `error` event listener. Flips
       * the component into its fallback state so the UI explains why
       * playback failed rather than showing a broken element.
       */
      onVideoError() {
        this.loadError = true;
        // eslint-disable-next-line no-console
        console.warn(
          "[video-player] <video> element failed to load",
          this.videoSrc
        );
      },

      /**
       * Pull a single ReviewScore dimension off the parent's `result`
       * payload and format it for display. Mirrors the same helper on
       * progress.js so either component can render the scores, but
       * keeping it duplicated here lets the video-player work even if
       * the parent's `reviewValue()` ever diverges.
       *
       * Returns:
       *   - "n/a" when the dimension is missing
       *   - a 2-decimal numeric string when present
       */
      reviewValue(key) {
        const result = this.result;
        if (!result || !result.review) return "n/a";
        const value = result.review[key];
        if (value === undefined || value === null) return "n/a";
        return typeof value === "number" ? value.toFixed(2) : String(value);
      },

      /**
       * True when the parent has a usable ReviewScore to render. Used to
       * hide the summary card until the pipeline actually produces one
       * (e.g. during a running job).
       */
      hasReview() {
        return Boolean(this.result && this.result.review);
      },
    };
  }

  // Expose as a global so Alpine's x-data="videoPlayer()" can find it.
  // No ES-module export needed — this script is loaded via a plain
  // <script defer> tag that executes BEFORE the Alpine CDN script (see
  // index.html for the ordering rationale — same rule as brief-builder.js
  // and progress.js).
  window.videoPlayer = videoPlayer;
})();
