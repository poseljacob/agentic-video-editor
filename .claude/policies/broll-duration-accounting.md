---
id: ave-broll-duration-accounting
title: B-Roll entries do not add to timeline duration
scope: repo
trigger: Director planning, EditPlan total_duration, B-Roll entries
enforcement: hard
version: 1
created: 2026-04-13
updated: 2026-04-13
source: back-pressure-failure
---

## Rule

NEVER include B-Roll entry durations in EditPlan `total_duration`. B-Roll entries are video-only overlays composited on the A-Roll base — they occupy no timeline of their own. Only A-Roll entry durations contribute to the final video length.

The Director's A-Roll entries alone must sum to the target duration (within +/-10%). If B-Roll durations are counted, the rendered video will be shorter than planned (by the total B-Roll duration) because the Editor sequences only A-Roll clips.

## Rationale

The Editor sequences A-Roll clips for continuous narration audio, then composites B-Roll as visual overlays on top. If the Director plans 30s total but 7s is B-Roll, only 23s of A-Roll gets sequenced — producing a video that cuts off early with no natural ending. The Director must plan enough A-Roll to fill the target duration independently.
