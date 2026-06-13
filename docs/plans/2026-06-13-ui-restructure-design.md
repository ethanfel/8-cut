# Main Window UI Restructure — Design

**Goal:** Reorganize the `MainWindow` UI in `main.py` from a flat wall of ~50 always-visible controls into a legible, grouped layout — a menu bar for rare actions, a tabbed control deck for settings, an always-visible transport bar, and a real status bar — plus a visual polish pass. Keep every existing behavior, shortcut, and mouse interaction working.

**Scope:** Reorganization **and** visual polish. **Not** an interaction-model change — single-key shortcuts, timeline mouse overloading, and the export/scan logic are untouched.

**Audience:** Single power user. Optimize for density and speed. The goal is *order, not hiding*: keep everything fast to reach; push only genuinely rare actions into menus.

**Runs in:** Python/Qt client (`main.py`), `MainWindow` class only. No `core/` changes.

---

## Problem (from audit)

- **No information architecture.** No menu bar, no toolbar; status bar explicitly disabled (`setStatusBar(None)`, main.py:4440). Every function is a permanently-visible widget at equal weight.
- **`settings_row` overloaded** (main.py:4334–4370): 24 widgets in one non-wrapping `QHBoxLayout` spanning three unrelated domains (encode/clip params, export variants, audio-scan ML). Needs >1500px; window opens at 1100px.
- **Stranded controls** — e.g. the workers spinbox sits between Cancel and Delete in the transport row (main.py:4316).
- **Weak feedback** — only an 11px `#888` status label at the far-right end of the overflowing settings row (main.py:4364).
- **Flat visual hierarchy** — single Fusion stylesheet, scattered inline `setStyleSheet` state swaps, no primary/secondary distinction, no grouping.

---

## Chosen approach: Tabbed control deck

The 3-pane horizontal splitter (Queue · Center · Scan results) is unchanged. The center column is restructured:

```
╔═ File   Edit   Scan   View   Help ═══════════════════ Profile:[default▾]  [?] ╗  menu bar (+ corner widgets)
║ ┌Queue──┐ │ current_file.mp4                          │ ┌ Scan results ─────┐ ║
║ │+Open  │ │ ┌──────────────────────────────────────┐ │ │ [model tabs]      │ ║
║ │filter │ │ │             VIDEO (mpv)               │ │ │ version▾          │ ║
║ │┌List┬+┐│ │ │                                      │ │ │ start  end  score │ ║
║ ││f1  ││ │ │ └──────────────────────────────────────┘ │ │ ...               │ ║
║ ││f2  ││ │ │ [════════════ timeline ════════════════] │ │                   │ ║
║ │└────┘ ││ │ [════════════ crop bar ════════════════] │ │ [Neg] [Export]    │ ║
║ └───────┘ │ ┌─ transport (always visible) ──────────┐ │ └───────────────────┘ ║
║           │ │▶ ⏸ x2 x4 🔒  --/--   ···  [Export] +₁+₂ Cancel  Delete│         ║
║           │ ├─[ Export ]─[ Crop & Track ]─[ Scan ]──┤  ← control deck (tabs)  ║
║           │ │  (controls for the active tab here)   │                         ║
║           │ └───────────────────────────────────────┘                         ║
╠═══════════════════════════════════════════════════════════════════════════════╣
║ Ready.                                  current file · profile: default · 8 wk ║  status bar
╚═══════════════════════════════════════════════════════════════════════════════╝
```

**Why tabbed deck:** Replaces the three stacked rows with a compact tab strip. The transport bar (most-used controls) stays always visible above the tabs; settings group by concern behind tabs. Trade-off accepted: viewing Scan + Export controls simultaneously costs a tab switch.

---

## Control mapping

Every current control has an explicit home; nothing is removed.

### Menu bar (rare / batch / management)

| Menu | Items |
|------|-------|
| **File** | Open Files… · Set export folder… · Quit |
| **Edit** | Undo *(Ctrl+Z → `_scan_panel.undo`)* · Subprofiles ▸ (Add… / Remove…) |
| **Scan** | Scan current · Auto-export · Scan All… · Train classifier… |
| **View** | Review mode ✓ · Subcategory markers ▸ · Hide exported ✓ · Show hidden ✓ |
| **Help** | Keyboard shortcuts *(? / F1)* · What's new · About |
| *corner (right)* | Profile ▾ · `?` |

*Hard Negatives and Dataset Stats remain inside the Train dialog (main.py:682, 762) — not surfaced separately. Profile new/delete remains driven by the profile combo's `activated` handler.*

### Transport bar (always visible — playback + one-press export actions)

`▶ Play · ⏸ Pause · x2 · x4 · 🔒 Lock · --/-- time · ⟨stretch⟩ · next-preview · **Export** · subprofile buttons ₁₂… · Cancel · Delete`

### Control deck — Export tab
`Label · Category · Name · Folder + browse · Format · HW encode · Resize · Duration · Clips · Spread · Workers · Re-export`

### Control deck — Crop & Track tab
`Portrait ratio · 1 random portrait · 1 random square · Track subject`

### Control deck — Scan tab
`Scan model ▾ · ⏲ history · Scan · Auto · Speech · Review · Fuse · Threshold`

### Left pane (Queue) — unchanged
`+ Open · filter · Hide exported · Show hidden · list tabs (tabbed / side-by-side)`

### Right pane (Scan results) — unchanged structurally

### Decisions
- **Train** → Scan menu only (no deck button).
- **Subcategory markers ("Sub")** → View menu submenu (off the deck).
- Items appearing in both a menu and a visible control (Hide exported, Review, Scan, Auto) share one handler and stay synced.

---

## Status bar

Restores `QStatusBar` (removes `setStatusBar(None)`):
- **Left**: transient feedback — `Exporting 2/3…`, `Scan complete · 14 regions`, `Ready.` — with an optional inline `QProgressBar` for export/scan runs. Replaces `_lbl_status` and the `_status_timer` clear logic.
- **Right (permanent widget)**: `current file · profile: <name> · <n> workers`.

---

## Visual polish

Extends the existing dark Fusion theme — no theme change.

1. **Aligned tab layouts** — each deck tab uses `QFormLayout`/grid so `label : control` pairs align in columns (biggest legibility win vs. today's ragged horizontal runs).
2. **Primary/secondary button weight** — **Export** gets an accent style (blue, reusing `#3a6ea8`); Cancel/Delete read as secondary/destructive. The existing **red Export = "armed to overwrite"** state (main.py:5403) is preserved as a distinct state layered on top.
3. **Consistent toggle states** — x2 / x4 / 🔒 Lock / Review are checkable; one global `:checked` style replaces Lock's ad-hoc inline `#4a3000` swap (main.py:5705).
4. **Spacing rhythm** — uniform margins/spacing; **fixed deck height** (= tallest tab) so the video never resizes on tab switch.
5. **Label cleanup** — de-abbreviate where cheap (`Thr→Threshold`, `Dur→Duration`); replace cryptic `⏲` with a clearer history affordance.
6. **One stylesheet block** — fold scattered inline `setStyleSheet` calls into the central sheet (tabs, separators, status bar, toggles, primary button); keep per-widget overrides only for genuine state changes (overwrite-armed Export).

---

## Implementation notes & risks

- **Preserve all signal wiring.** Controls are re-parented into new layouts, but every existing `connect()` and the controls' object identities are kept — this is a layout move, not a rewrite of handlers.
- **Preserve all shortcuts.** The `QShortcut` block (main.py:4450–4483) and `_KeyFilter` focus suppression are untouched. Menu items reuse the same handler methods and may display the matching shortcut text.
- **Fixed deck height** prevents video-area jump when switching tabs.
- **Synced menu/button state** — checkable menu items (Review, Hide exported) and their visible toggles must reflect each other; route both through the existing handler and update both widgets.
- **Profile combo** moves to a menu-bar corner widget but keeps its existing `activated` → new/delete/switch logic intact.
- Risk: re-parenting a large `__init__` is error-prone. Mitigate by moving controls in small, independently-runnable stages (menu bar → status bar → deck tabs → transport bar → polish), launching the app after each.

---

## What this does NOT do

- No change to export, scan, tracking, or DB logic — `core/` untouched.
- No change to keyboard shortcuts or timeline mouse interactions.
- No theme change — stays dark Fusion.
- No new features — every control already exists; this is rehousing + polish.
- No change to the Queue or Scan-results panes' internal structure.
