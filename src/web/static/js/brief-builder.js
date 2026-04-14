/**
 * AVE Studio — Brief Builder (US-004)
 *
 * Alpine.js v3 component factory that powers the creative-brief form.
 * Registers itself as `window.briefBuilder` so the template can bind via
 * `x-data="briefBuilder()"` without needing ES modules or a build step.
 *
 * Responsibilities:
 *   1. Fetch /api/styles, /api/pipelines, /api/footage-indexes in parallel
 *      on init() and populate dropdown option arrays.
 *   2. Hold form state for the CreativeBrief + job config.
 *   3. Client-side validate required text fields, pipeline selection, and
 *      footage index selection. Style is optional (maps to null).
 *   4. Assemble the exact POST /api/jobs payload shape and submit it.
 *   5. Surface loading / error / success states to the UI.
 */
(function () {
  "use strict";

  // Single source of truth for range slider bounds so UI and payload agree.
  const DURATION_MIN = 15;
  const DURATION_MAX = 60;
  const DURATION_DEFAULT = 30;

  /**
   * Build the initial Alpine state object. Exposed as a factory (not a
   * singleton) so each `x-data="briefBuilder()"` binding gets its own
   * isolated state — important if the brief-builder section is ever
   * re-rendered or duplicated.
   */
  function briefBuilder() {
    return {
      // --- CreativeBrief fields ---
      brief: {
        product: "",
        audience: "",
        tone: "",
        duration_seconds: DURATION_DEFAULT,
        style_ref: "",
      },

      // --- Job-level config (outside the brief object) ---
      pipeline_path: "",
      footage_index_path: "",

      // --- Dropdown option arrays, populated by init() ---
      styles: [],
      pipelines: [],
      footageIndexes: [],

      // --- Range slider bounds exposed to the template ---
      durationMin: DURATION_MIN,
      durationMax: DURATION_MAX,

      // --- Async UI state ---
      loadingOptions: true,
      submitting: false,
      error: "",
      lastJobId: "",

      /**
       * Alpine lifecycle hook. Fire all three GETs in parallel so the form
       * is usable as fast as possible. A single failure sets `error` but
       * still unblocks `loadingOptions` so the user can retry manually by
       * reloading — we don't want to leave the form wedged.
       */
      async init() {
        this.loadingOptions = true;
        this.error = "";
        try {
          const [stylesRes, pipelinesRes, footageRes] = await Promise.all([
            fetch("/api/styles"),
            fetch("/api/pipelines"),
            fetch("/api/footage-indexes"),
          ]);

          if (!stylesRes.ok) {
            throw new Error(`Failed to load styles (HTTP ${stylesRes.status})`);
          }
          if (!pipelinesRes.ok) {
            throw new Error(
              `Failed to load pipelines (HTTP ${pipelinesRes.status})`
            );
          }
          if (!footageRes.ok) {
            throw new Error(
              `Failed to load footage indexes (HTTP ${footageRes.status})`
            );
          }

          const [styles, pipelines, footageIndexes] = await Promise.all([
            stylesRes.json(),
            pipelinesRes.json(),
            footageRes.json(),
          ]);

          this.styles = Array.isArray(styles) ? styles : [];
          this.pipelines = Array.isArray(pipelines) ? pipelines : [];
          this.footageIndexes = Array.isArray(footageIndexes)
            ? footageIndexes
            : [];

          // Auto-select when there is exactly one option — the browser
          // renders it visually but never fires a change event, so x-model
          // stays empty. Pre-setting the value fixes the disabled Run button.
          if (this.pipelines.length === 1) {
            this.pipeline_path = this.pipelines[0].path;
          }
          if (this.footageIndexes.length === 1) {
            this.footage_index_path = this.footageIndexes[0].path;
          }
        } catch (err) {
          this.error =
            err && err.message
              ? `Could not load form options: ${err.message}`
              : "Could not load form options.";
        } finally {
          this.loadingOptions = false;
        }
      },

      /**
       * Human-readable label for a footage index option — we want the
       * shot count visible in the dropdown so the user can spot the right
       * index at a glance.
       */
      footageLabel(entry) {
        if (!entry) return "";
        const count = Number.isFinite(entry.shot_count) ? entry.shot_count : 0;
        return `${entry.name} (${count} shots)`;
      },

      /**
       * Returns true if the form is currently valid to submit.
       * - product, audience, tone: non-empty after trim
       * - pipeline_path: non-empty
       * - footage_index_path: non-empty
       * - style_ref: optional (no constraint)
       * - duration is clamped by the range input natively
       */
      canSubmit() {
        if (this.submitting) return false;
        if (this.loadingOptions) return false;
        if (!this.brief.product || !this.brief.product.trim()) return false;
        if (!this.brief.audience || !this.brief.audience.trim()) return false;
        if (!this.brief.tone || !this.brief.tone.trim()) return false;
        if (!this.pipeline_path) return false;
        if (!this.footage_index_path) return false;
        return true;
      },

      /**
       * Assemble the POST /api/jobs payload with the exact shape the
       * FastAPI route expects. `style_ref` collapses to null when empty
       * because the backend model types it as `str | None`, and pydantic
       * will reject an empty-string-coerced-to-None only if we send it
       * that way — safer to emit `null` explicitly.
       */
      buildPayload() {
        const styleRef = this.brief.style_ref ? this.brief.style_ref : null;
        return {
          brief: {
            product: this.brief.product.trim(),
            audience: this.brief.audience.trim(),
            tone: this.brief.tone.trim(),
            duration_seconds: Number(this.brief.duration_seconds),
            style_ref: styleRef,
          },
          footage_index_path: this.footage_index_path,
          pipeline_path: this.pipeline_path,
        };
      },

      /**
       * POST the assembled payload to /api/jobs. On success, store the
       * returned job_id and surface it in the UI. Navigation to the
       * progress view is intentionally out of scope here — that's US-005.
       */
      async submit() {
        if (!this.canSubmit()) return;
        this.submitting = true;
        this.error = "";
        this.lastJobId = "";

        const payload = this.buildPayload();

        try {
          const response = await fetch("/api/jobs", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });

          if (!response.ok) {
            // Try to surface the FastAPI error body if present.
            let detail = `HTTP ${response.status}`;
            try {
              const body = await response.json();
              if (body && body.detail) {
                detail = typeof body.detail === "string"
                  ? body.detail
                  : JSON.stringify(body.detail);
              }
            } catch (_parseErr) {
              // Non-JSON error body; keep the status-code fallback.
            }
            throw new Error(detail);
          }

          const body = await response.json();
          const jobId = body && (body.job_id || body.id);
          if (!jobId) {
            throw new Error("Server accepted the job but returned no job_id.");
          }
          this.lastJobId = jobId;
          // eslint-disable-next-line no-console
          console.log(`[brief-builder] Job ${jobId} queued`, body);
        } catch (err) {
          this.error =
            err && err.message
              ? `Failed to queue job: ${err.message}`
              : "Failed to queue job.";
        } finally {
          this.submitting = false;
        }
      },
    };
  }

  // Expose as a global so Alpine's x-data="briefBuilder()" can find it.
  // No ES-module export needed — this script is loaded via a plain
  // <script> tag alongside the Alpine CDN bundle.
  window.briefBuilder = briefBuilder;
})();
