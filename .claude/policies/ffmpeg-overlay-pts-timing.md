---
id: ave-ffmpeg-overlay-pts-timing
title: FFmpeg overlay filter requires PTS shift for delayed compositing
scope: repo
trigger: FFmpeg overlay filter, B-Roll compositing, setpts
enforcement: hard
version: 1
created: 2026-04-13
updated: 2026-04-13
source: back-pressure-failure
---

## Rule

NEVER use `setpts=PTS-STARTPTS` alone when compositing overlays at non-zero timeline positions. The overlay's frames get consumed immediately (PTS starts at 0) while the `enable='between(t,start,end)'` window hasn't opened yet — the viewer sees a frozen last-frame instead of a playing clip.

ALWAYS use `setpts=PTS-STARTPTS+{start}/TB` to shift the overlay PTS to match the timeline insertion point. ALWAYS add `eof_action=pass` to the overlay filter so the base video shows through after the overlay clip ends.

## Rationale

When compositing B-Roll as video-only overlays on an A-Roll base, the overlay filter synchronizes inputs by PTS. If B-Roll PTS starts at 0 but the enable window starts at e.g. 15s, FFmpeg consumes all B-Roll frames (typically 3-4s long) before the window opens. With the default `eof_action=repeat`, the last frame freezes on screen. The `+{start}/TB` term delays the B-Roll frames to arrive at the correct timeline position, and `eof_action=pass` ensures clean fallback to the base video.
