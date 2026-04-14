/**
 * AVE Studio -- Edit Controls (US-009)
 *
 * Alpine.js v3 helper component that extends the US-008 timeline viewer
 * with pointed edit controls: per-entry trim sliders, drag-to-reorder,
 * inline text-overlay editing, delete, shot swap, and a save button that
 * PUTs the modified plan back to the server.
 *
 * Registers itself as `window.editControls` so the timeline template can
 * mix it into the existing `timelineView()` scope via `x-data`. We expose
 * a factory that returns a plain-object mixin rather than a standalone
 * Alpine component because the edit surface has to read + write the same
 * `plan.entries` array the timeline view already renders -- running a
 * sibling component would require a second fetch round trip + a coupling
 * layer. Instead we spread this object into the timeline template's
 * `x-data` alongside `timelineView()`, and its methods mutate `this.plan`
 * directly.
 *
 * Responsibilities
 * ----------------
 *  1. Track edit mode state (`editing`, `dirty`, per-entry `errors`,
 *     and the pending `shot_swap_target` index).
 *  2. Provide setter helpers that Alpine `@input` handlers can call
 *     to mutate an entry's `start_trim`, `end_trim`, or `text_overlay`
 *     while enforcing client-side bound checks against the shot's
 *     `[start_time, end_time]` window (duplicates the server's
 *     semantic validation so users see errors immediately instead of
 *     waiting for a 422).
 *  3. Drag-to-reorder via the native HTML5 drag/drop API -- no third
 *     party libraries. The list is keyed on `position`, so after a
 *     successful drop we rewrite every entry's `position` to its new
 *     array index (0..N-1 contiguous) and bump `dirty = true`.
 *  4. Delete an entry and reindex the survivors.
 *  5. Save: serialize the modified plan into the full EditPlan shape
 *     the PUT endpoint expects (the GET /edit-plan response drops
 *     display fields + `brief` + `music_path`, so we merge those back
 *     in from the job record captured at init time) and PUT it. On
 *     422, parse the FastAPI `detail: [{loc, msg, type}]` shape and
 *     attach each error to the offending entry's field error map.
 *
 * Coding style
 * ------------
 * No ES modules, no TypeScript, no build step. Plain ES2021 that runs in
 * the same script-ordered execution as timeline.js. Registered on
 * `window.editControls` so Alpine's `x-data="{...timelineView(),
 * ...editControls()}"` can pick it up after the deferred script runs.
 */
(function () {
  "use strict";

  /**
   * Float tolerance when client-side validating trim bounds. Matches
   * the server-side `_TRIM_EPSILON` in
   * src/web/routes/jobs.py so our preflight catches exactly the same
   * shapes the server would reject (no false greens). A slightly
   * looser value would let a 422 slip through; a tighter value would
   * reject shapes the server accepts.
   */
  const TRIM_EPSILON = 1e-3;

  /**
   * Build the edit-controls mixin. Factory form matches the style of
   * timelineView() / reviewChart() / chatView() so the module surface
   * is uniform across the static/js directory.
   */
  function editControls() {
    return {
      // ------------------------------------------------------------- #
      // State                                                          #
      // ------------------------------------------------------------- #

      /**
       * True once the user clicks "Edit" -- gates the trim/delete/swap
       * controls so they are hidden in read-only mode and the card
       * strip stays visually identical to US-008 when nobody is
       * editing. Clicking Save or Cancel flips it back to false.
       */
      editing: false,

      /**
       * True when at least one local mutation has been applied since
       * the plan was last fetched. Drives the Save button's disabled
       * state and the "unsaved changes" indicator next to the header.
       */
      dirty: false,

      /**
       * Transient per-entry error map, keyed by position. Each value
       * is an object `{field: message}` so the template can render a
       * field-level error beside the offending input. Cleared on
       * every successful save + whenever the affected field is edited
       * again (so the red ring disappears as soon as the user types
       * a valid value).
       */
      errors: {},

      /**
       * Plan-level error strings (positions-not-contiguous,
       * brief/music_path issues, network failures). Rendered in a
       * single banner above the card strip.
       */
      planErrors: [],

      /**
       * True while a PUT is in flight. Disables Save to prevent
       * double-submits; the button flips back to enabled on
       * success + 422 + network failure.
       */
      saving: false,

      /**
       * Success flash -- cleared after a few seconds via setTimeout
       * so the user gets visual confirmation the PUT landed without
       * having to re-inspect the card strip.
       */
      saveFlash: "",

      /**
       * The entry position currently targeted by the shot-swap modal,
       * or -1 when no modal is open. The shot-browser component reads
       * this via `x-show` and `$dispatch`es `shot-swap-result` back
       * when the user picks a replacement.
       */
      swapTarget: -1,

      /**
       * Snapshot of the job's `footage_index_path`, captured once
       * from GET /api/jobs/{id} when editing starts. The shot-browser
       * needs this to query GET /api/footage/search?footage_index_path=...
       * and the save path uses the same index to build the PUT body's
       * `brief` + `music_path` fields (which the edit-plan GET drops).
       */
      jobFootageIndexPath: "",

      /**
       * Cached copy of the `brief` sub-object from GET /api/jobs/{id}.
       * Required on the PUT body (EditPlan.brief is non-optional)
       * and not returned by the GET /edit-plan enrichment path.
       */
      jobBrief: null,

      /**
       * Cached copy of `result.edit_plan.music_path` from GET
       * /api/jobs/{id}. May be null -- the backend treats it as
       * optional. Not returned by GET /edit-plan.
       */
      jobMusicPath: null,

      /**
       * Cached index of shots keyed by shot_id for client-side trim
       * bound checks. Populated from the same GET /api/jobs/{id}
       * prefetch that seeds brief/music_path. Values are
       * `{start_time, end_time}`.
       */
      shotBounds: {},

      /**
       * Sticky flag set to true after any local edit that changes
       * positions. Serves as the "thumbnails may be stale" banner --
       * the original clip files were rendered in the old order, so a
       * reordered plan's per-position thumbnails will point at the
       * wrong frame until the pipeline reruns.
       */
      thumbnailsStale: false,

      /**
       * Internal drag-drop source index. Set in dragStart, cleared
       * after drop. Not reactive -- Alpine does not need to watch it.
       */
      _dragFromIndex: -1,

      // ------------------------------------------------------------- #
      // Lifecycle                                                      #
      // ------------------------------------------------------------- #

      /**
       * Pre-load the brief / music_path / footage_index_path from
       * GET /api/jobs/{id}. Called from the template's
       * `@click` on the Edit button the first time the user enters
       * edit mode -- we don't do this in timelineView.init() because
       * we only need it when the user actually wants to modify the
       * plan.
       *
       * Also builds the shot-bounds lookup table from the
       * FootageIndex so trim validation can be done client-side.
       */
      async enterEditMode() {
        if (this.editing) return;
        const jobId = this.jobId;
        if (!jobId) return;

        this.planErrors = [];
        this.errors = {};

        try {
          const jobResp = await fetch(
            `/api/jobs/${encodeURIComponent(jobId)}`,
            { headers: { Accept: "application/json" } }
          );
          if (!jobResp.ok) {
            this.planErrors.push(
              `Could not load job metadata (${jobResp.status} ${jobResp.statusText}).`
            );
            return;
          }
          const jobBody = await jobResp.json();
          this.jobBrief = jobBody && jobBody.brief ? jobBody.brief : null;
          this.jobFootageIndexPath =
            typeof jobBody.footage_index_path === "string"
              ? jobBody.footage_index_path
              : "";
          this.jobMusicPath =
            jobBody && jobBody.result && jobBody.result.edit_plan
              ? jobBody.result.edit_plan.music_path ?? null
              : null;
        } catch (err) {
          this.planErrors.push(
            err && err.message
              ? `Could not load job metadata: ${err.message}`
              : "Could not load job metadata."
          );
          return;
        }

        // Fetch the FootageIndex so we can validate trims locally and
        // expose shot bounds to the shot-browser (for swap previews).
        if (this.jobFootageIndexPath) {
          try {
            const catalog = await fetch(
              `/api/footage/catalog?footage_index_path=${encodeURIComponent(
                this.jobFootageIndexPath
              )}`,
              { headers: { Accept: "application/json" } }
            );
            if (catalog.ok) {
              const catalogBody = await catalog.json();
              const results = Array.isArray(catalogBody.results)
                ? catalogBody.results
                : [];
              const bounds = {};
              for (const shot of results) {
                if (typeof shot.shot_id === "string") {
                  bounds[shot.shot_id] = {
                    start_time: Number(shot.start_time),
                    end_time: Number(shot.end_time),
                  };
                }
              }
              this.shotBounds = bounds;
            }
          } catch (_err) {
            // Non-fatal: client-side validation will simply be
            // skipped for shots that are not in the cache, and
            // server-side validation will still catch them on PUT.
            this.shotBounds = this.shotBounds || {};
          }
        }

        this.editing = true;
      },

      /**
       * Exit edit mode and discard local changes. The simplest way
       * to guarantee a clean re-entry is to force the timeline
       * viewer to refetch: clear `_loadedJobId` and call the
       * existing `_maybeFetch` helper so the server-backed plan
       * replaces whatever mutation the user did locally.
       */
      cancelEdit() {
        this.editing = false;
        this.dirty = false;
        this.errors = {};
        this.planErrors = [];
        this.swapTarget = -1;
        // Force a refetch so the local plan is replaced with the
        // server's last-saved version.
        this._loadedJobId = "";
        this._maybeFetch();
      },

      // ------------------------------------------------------------- #
      // Entry mutations                                                #
      // ------------------------------------------------------------- #

      /**
       * Update an entry's start_trim. Validates client-side against
       * the shot bounds and sets/clears `errors[position].start_trim`.
       */
      setStartTrim(entry, raw) {
        const value = Number(raw);
        if (!Number.isFinite(value)) {
          this._setEntryError(entry.position, "start_trim", "Must be a number.");
          return;
        }
        this._clearEntryError(entry.position, "start_trim");
        entry.start_trim = value;
        entry.duration = this._computeDuration(entry);
        this.dirty = true;
        this._validateEntryBounds(entry);
      },

      /**
       * Update an entry's end_trim. See setStartTrim.
       */
      setEndTrim(entry, raw) {
        const value = Number(raw);
        if (!Number.isFinite(value)) {
          this._setEntryError(entry.position, "end_trim", "Must be a number.");
          return;
        }
        this._clearEntryError(entry.position, "end_trim");
        entry.end_trim = value;
        entry.duration = this._computeDuration(entry);
        this.dirty = true;
        this._validateEntryBounds(entry);
      },

      /**
       * Update an entry's text_overlay. Empty string clears it to
       * `null` to match the PUT semantics (the backend treats null
       * and empty-string differently; null is "no overlay" while
       * empty string would render a blank overlay).
       */
      setTextOverlay(entry, raw) {
        const value = typeof raw === "string" ? raw.trim() : "";
        entry.text_overlay = value === "" ? null : value;
        this.dirty = true;
      },

      /**
       * Delete an entry and reindex the survivors 0..N-1.
       */
      deleteEntry(position) {
        if (!this.plan || !Array.isArray(this.plan.entries)) return;
        const idx = this.plan.entries.findIndex(
          (e) => e.position === position
        );
        if (idx === -1) return;
        this.plan.entries.splice(idx, 1);
        this._reindex();
        this.plan.entry_count = this.plan.entries.length;
        // Purge the deleted position's errors -- the object still
        // holds a key for it which would never be cleared otherwise.
        delete this.errors[position];
        this.dirty = true;
        this.thumbnailsStale = true;
      },

      /**
       * Begin a shot swap. Sets `swapTarget` to the offending
       * position so the shot-browser modal (in shot-browser.js)
       * opens against that entry.
       */
      openSwapModal(position) {
        this.swapTarget = position;
      },

      /**
       * Close the shot swap modal without picking a result.
       */
      closeSwapModal() {
        this.swapTarget = -1;
      },

      /**
       * Apply a shot-swap result coming back from the shot-browser
       * modal. Replaces the target entry's shot_id, start_trim,
       * end_trim, and source_file/source_filename display fields
       * with the new shot's values. Preserves position + transition
       * + text_overlay so the user's narrative survives the swap.
       */
      applyShotSwap(shot) {
        if (this.swapTarget === -1) return;
        if (!this.plan || !Array.isArray(this.plan.entries)) return;
        const idx = this.plan.entries.findIndex(
          (e) => e.position === this.swapTarget
        );
        if (idx === -1) return;
        const entry = this.plan.entries[idx];
        entry.shot_id = shot.shot_id;
        entry.start_trim = Number(shot.start_time);
        entry.end_trim = Number(shot.end_time);
        entry.source_file = shot.source_file;
        entry.source_filename = shot.source_filename;
        entry.display_label = shot.display_label;
        entry.roll_type = shot.roll_type;
        entry.duration = Number(shot.duration);
        // Record bounds so future trim edits validate locally.
        this.shotBounds[shot.shot_id] = {
          start_time: Number(shot.start_time),
          end_time: Number(shot.end_time),
        };
        // Clear any residual errors on this entry.
        delete this.errors[entry.position];
        this.swapTarget = -1;
        this.dirty = true;
        this.thumbnailsStale = true;
      },

      // ------------------------------------------------------------- #
      // Drag + drop reorder                                            #
      // ------------------------------------------------------------- #

      /**
       * Begin a drag on an entry card. Stash the source index in
       * `_dragFromIndex` so drop can look it up. We also set a
       * dataTransfer payload so the browser's default "move" cursor
       * renders -- the string itself is not read back.
       */
      dragStart(event, index) {
        if (!this.editing) return;
        this._dragFromIndex = index;
        if (event && event.dataTransfer) {
          event.dataTransfer.effectAllowed = "move";
          try {
            event.dataTransfer.setData("text/plain", String(index));
          } catch (_err) {
            // Some browsers throw on setData in test environments;
            // the visual cursor is cosmetic so we can swallow this.
          }
        }
      },

      /**
       * Allow drop on a card. Without preventDefault the browser
       * refuses to fire the drop handler.
       */
      dragOver(event) {
        if (!this.editing) return;
        if (event) {
          event.preventDefault();
          if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
        }
      },

      /**
       * Drop handler: reorder the entries array so the dragged card
       * lands at the target index, then rewrite every position to
       * match array order (contiguous 0..N-1 invariant required by
       * the server's validator).
       */
      drop(event, targetIndex) {
        if (!this.editing) return;
        if (event) event.preventDefault();
        const from = this._dragFromIndex;
        this._dragFromIndex = -1;
        if (from === -1 || from === targetIndex) return;
        if (!this.plan || !Array.isArray(this.plan.entries)) return;
        const entries = this.plan.entries;
        if (from < 0 || from >= entries.length) return;
        if (targetIndex < 0 || targetIndex >= entries.length) return;
        const [moved] = entries.splice(from, 1);
        entries.splice(targetIndex, 0, moved);
        this._reindex();
        this.dirty = true;
        this.thumbnailsStale = true;
      },

      // ------------------------------------------------------------- #
      // Save                                                           #
      // ------------------------------------------------------------- #

      /**
       * Serialize the local plan state into the EditPlan shape the
       * PUT endpoint expects, send it, and dispatch the outcome:
       *   - 200 -> flash "Saved" + reset dirty
       *   - 422 -> parse per-field errors and surface them
       *   - 404 / 409 / network -> plan-level error banner
       */
      async savePlan() {
        if (!this.plan || !this.jobId) return;
        if (this.saving) return;
        this.saving = true;
        this.errors = {};
        this.planErrors = [];

        const totalDuration = this.plan.entries.reduce((sum, entry) => {
          const start = Number(entry.start_trim);
          const end = Number(entry.end_trim);
          if (!Number.isFinite(start) || !Number.isFinite(end)) return sum;
          return sum + (end - start);
        }, 0);
        this.plan.total_duration = totalDuration;

        const body = {
          brief: this.jobBrief,
          music_path: this.jobMusicPath,
          total_duration: Number(this.plan.total_duration) || 0,
          entries: this.plan.entries.map((entry) => ({
            shot_id: entry.shot_id,
            start_trim: Number(entry.start_trim),
            end_trim: Number(entry.end_trim),
            position: Number(entry.position),
            text_overlay: entry.text_overlay == null ? null : entry.text_overlay,
            transition: entry.transition == null ? null : entry.transition,
          })),
        };

        let response;
        try {
          response = await fetch(
            `/api/jobs/${encodeURIComponent(this.jobId)}/edit-plan`,
            {
              method: "PUT",
              headers: {
                "Content-Type": "application/json",
                Accept: "application/json",
              },
              body: JSON.stringify(body),
            }
          );
        } catch (err) {
          this.saving = false;
          this.planErrors.push(
            err && err.message
              ? `Could not reach the edit-plan endpoint: ${err.message}`
              : "Could not reach the edit-plan endpoint."
          );
          return;
        }

        if (response.status === 200) {
          let payload = null;
          try {
            payload = await response.json();
          } catch (_err) {
            // Non-JSON 200 -- unlikely but recoverable. Keep the
            // current local state since the server accepted it.
          }
          this.saving = false;
          this.dirty = false;
          this.saveFlash = "Saved.";
          setTimeout(() => {
            this.saveFlash = "";
          }, 2500);
          // Mutate plan.total_duration / entries from the server's
          // canonical shape so any numeric coercion the server did
          // (rounding, null-vs-missing normalization) lands in the
          // UI without a refetch.
          if (payload && payload.edit_plan) {
            const saved = payload.edit_plan;
            if (Number.isFinite(saved.total_duration)) {
              this.plan.total_duration = Number(saved.total_duration);
            }
          }
          return;
        }

        if (response.status === 422) {
          this.saving = false;
          let detail = [];
          try {
            const body422 = await response.json();
            if (Array.isArray(body422.detail)) {
              detail = body422.detail;
            }
          } catch (_err) {
            // fall through to generic error
          }
          if (detail.length === 0) {
            this.planErrors.push(
              "Plan failed validation but the server did not report any fields."
            );
            return;
          }
          for (const err of detail) {
            const loc = Array.isArray(err.loc) ? err.loc : [];
            // Per-entry errors: ["body", "entries", i, "<field>"]
            if (
              loc.length >= 4 &&
              loc[0] === "body" &&
              loc[1] === "entries" &&
              typeof loc[2] === "number"
            ) {
              const idx = loc[2];
              const entry = this.plan.entries[idx];
              if (entry) {
                this._setEntryError(
                  entry.position,
                  String(loc[3]),
                  String(err.msg || "Invalid.")
                );
              } else {
                this.planErrors.push(
                  `Entry ${idx}: ${err.msg || "invalid"}`
                );
              }
              continue;
            }
            // Plan-level errors: ["body", "entries"] or anything else.
            this.planErrors.push(String(err.msg || JSON.stringify(err)));
          }
          return;
        }

        this.saving = false;
        let detail = "";
        try {
          const errBody = await response.json();
          if (errBody && typeof errBody.detail === "string") {
            detail = errBody.detail;
          } else if (errBody && Array.isArray(errBody.detail)) {
            detail = errBody.detail
              .map((e) => e.msg || JSON.stringify(e))
              .join("; ");
          }
        } catch (_err) {
          // non-JSON body -- fall back to status text
        }
        this.planErrors.push(
          detail ||
            `Save failed (${response.status} ${response.statusText}).`
        );
      },

      // ------------------------------------------------------------- #
      // Template helpers                                               #
      // ------------------------------------------------------------- #

      /** True when the given field on the given entry has an error. */
      entryError(position, field) {
        const map = this.errors[position];
        return map && map[field] ? map[field] : "";
      },

      /** True when ANY entry has an error set. Disables the Save button. */
      hasAnyError() {
        for (const key in this.errors) {
          if (!Object.prototype.hasOwnProperty.call(this.errors, key)) continue;
          const fields = this.errors[key];
          for (const f in fields) {
            if (Object.prototype.hasOwnProperty.call(fields, f) && fields[f]) {
              return true;
            }
          }
        }
        return false;
      },

      /** Stringify the shot bounds for a given entry for the min/max attrs. */
      shotStartBound(entry) {
        const bound = entry && this.shotBounds[entry.shot_id];
        if (!bound) return null;
        return bound.start_time;
      },
      shotEndBound(entry) {
        const bound = entry && this.shotBounds[entry.shot_id];
        if (!bound) return null;
        return bound.end_time;
      },

      // ------------------------------------------------------------- #
      // Private helpers                                                #
      // ------------------------------------------------------------- #

      /**
       * Rewrite every entry's `position` field to match its array
       * index so the contiguous 0..N-1 invariant holds after any
       * reorder / delete / swap operation. Preserves error maps by
       * rebuilding the `errors` object against the new positions.
       */
      _reindex() {
        if (!this.plan || !Array.isArray(this.plan.entries)) return;
        const newErrors = {};
        this.plan.entries.forEach((entry, i) => {
          const oldPos = entry.position;
          entry.position = i;
          if (this.errors[oldPos]) {
            newErrors[i] = this.errors[oldPos];
          }
        });
        this.errors = newErrors;
      },

      _computeDuration(entry) {
        const start = Number(entry.start_trim);
        const end = Number(entry.end_trim);
        if (!Number.isFinite(start) || !Number.isFinite(end)) return 0;
        return Math.max(0, end - start);
      },

      /**
       * Client-side trim validation mirror of the server's
       * `_validate_edit_plan_against_index` check in
       * src/web/routes/jobs.py. Set `errors[position][field]` with a
       * friendly message for out-of-bounds / mis-ordered trims so
       * the user sees the error inline before they ever click Save.
       */
      _validateEntryBounds(entry) {
        const bound = this.shotBounds[entry.shot_id];
        if (!bound) return; // server will validate
        const start = Number(entry.start_trim);
        const end = Number(entry.end_trim);
        if (start > end + TRIM_EPSILON) {
          this._setEntryError(
            entry.position,
            "start_trim",
            `Start must be <= end (${end.toFixed(2)}s).`
          );
          return;
        }
        if (start < bound.start_time - TRIM_EPSILON) {
          this._setEntryError(
            entry.position,
            "start_trim",
            `Must be >= ${bound.start_time.toFixed(2)}s.`
          );
          return;
        }
        if (start > bound.end_time + TRIM_EPSILON) {
          this._setEntryError(
            entry.position,
            "start_trim",
            `Must be <= ${bound.end_time.toFixed(2)}s.`
          );
          return;
        }
        if (end < bound.start_time - TRIM_EPSILON) {
          this._setEntryError(
            entry.position,
            "end_trim",
            `Must be >= ${bound.start_time.toFixed(2)}s.`
          );
          return;
        }
        if (end > bound.end_time + TRIM_EPSILON) {
          this._setEntryError(
            entry.position,
            "end_trim",
            `Must be <= ${bound.end_time.toFixed(2)}s.`
          );
          return;
        }
        // All good -- clear both trim errors.
        this._clearEntryError(entry.position, "start_trim");
        this._clearEntryError(entry.position, "end_trim");
      },

      _setEntryError(position, field, message) {
        const map = this.errors[position] || {};
        map[field] = message;
        // Reassign so Alpine notices the mutation on nested objects.
        this.errors = { ...this.errors, [position]: map };
      },

      _clearEntryError(position, field) {
        const map = this.errors[position];
        if (!map || !map[field]) return;
        delete map[field];
        if (Object.keys(map).length === 0) {
          const copy = { ...this.errors };
          delete copy[position];
          this.errors = copy;
        } else {
          this.errors = { ...this.errors, [position]: map };
        }
      },
    };
  }

  // Expose as a global so the Alpine template can spread this mixin
  // alongside `timelineView()` via
  // `x-data="Object.assign(timelineView(), editControls())"`.
  // No ES-module export needed -- this script is loaded via a plain
  // <script defer> tag that executes BEFORE the Alpine CDN script,
  // same pattern as timeline.js / review-chart.js / chat.js.
  window.editControls = editControls;
})();
