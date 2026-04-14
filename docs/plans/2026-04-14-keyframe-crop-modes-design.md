# Keyframe crop modes design

## Problem

Currently, crop keyframes only store position (time, center). The random portrait and random square checkboxes apply globally to the entire batch. When a batch spans a scene change (e.g. wide landscape to close-up), portrait crop may only make sense for part of the span.

## Solution

Extend keyframes to snapshot the full crop state — position, ratio, and random crop flags — so each sub-clip in a batch inherits crop settings from the latest keyframe at or before its start time.

## Keyframe data

Expand from `(time, center)` to `(time, center, ratio, rand_portrait, rand_square)`:

- `time` (float) — absolute time in seconds
- `center` (float) — horizontal crop position, 0.0 to 1.0
- `ratio` (str | None) — portrait combo value: `None`, `"9:16"`, `"4:5"`, or `"1:1"`
- `rand_portrait` (bool) — random portrait checkbox state
- `rand_square` (bool) — random square checkbox state

## Setting keyframes

Same interaction as today: click the crop bar while in lock mode. The click now snapshots the current center, portrait combo selection, rand_portrait checkbox, and rand_square checkbox into the keyframe.

## Export application

For each sub-clip job:

1. Find the latest keyframe where `kt <= start_time + 0.05`.
2. Apply its `center` and `ratio` to the job.
3. Collect the effective `rand_portrait` and `rand_square` flags.
4. After all keyframes are resolved, apply random crop selection only to sub-clips whose effective flags are set. The random selection (`n_random = max(1, eligible_count // 3)`) operates within each flag group independently.

When no keyframes exist, behavior is unchanged (global checkboxes apply to all clips).

## Timeline diamond colors

Each keyframe diamond on the timeline is color-coded by its random crop flags:

- No random flags — gold (current color, `#ffb400`)
- Portrait only — red (`QColor(220, 60, 60)`)
- Square only — blue (`QColor(60, 180, 220)`)
- Both — split diamond: left half red, right half blue

## Playback preview in lock mode

When scrubbing in lock mode, `_on_seek_changed` already updates the crop bar preview from keyframes. This extends to also update the portrait combo and random checkboxes to reflect the effective state at the current playback position, so the user sees what each region's settings are.

## Clearing

Toggling lock off clears all keyframes (existing behavior, unchanged).
