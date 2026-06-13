# Multi-pane Control Deck — Design + Plan Addendum

> Addendum to `2026-06-13-ui-restructure-design.md` / `-implementation.md`. Same branch (`ui-restructure`), same constraints (preserve behavior; reorg/feature only; no `core/` changes).

**Goal:** Let the control-deck panels (Export / Crop & Track / Scan) optionally show **side-by-side as resizable columns** instead of one-at-a-time tabs — mirroring the existing playlist pin→side-by-side pattern.

> **Revision (post-use, 2026-06-13):** The first implementation showed unpinned panels as a "leftover" tab-column so nothing was hidden — but in use, pinning 2 panels then displayed 3 columns, which read as "all three pinned" and was confusing (and inconsistent with what persisted). **Revised behavior:** the split view shows **exactly the pinned panels** as columns (pin 2 → 2 columns, pin 3 → 3). Unpinned panels are not shown as columns. Because the right-click-tab "Show side-by-side" gesture only works in tabbed mode, an always-available **View ▸ Side-by-side panels ▸ Export / Crop / Scan** submenu of checkable toggles is the way to pin/unpin any panel (including adding a 3rd while already in split view). The `if leftovers:` block below is removed; the View submenu + its sync in `_refresh_deck_layout` replace it.

**Mirror these existing playlist members** (study them — the deck is a simpler, fixed-3-panel version): `_PlaylistTabBar` (main.py:3284), `_refresh_layout` (~4872), `_on_pin_toggle`/`_on_unpin` (~4942), `_detach_all_pws`/`_clear_split_container` (~4861), and the `_list_stack`/`_split_container` setup (~3916–3923).

---

## Design

### Panel identity
The deck's three pages (`_tab_export`, `_tab_crop`, `_tab_scan`) each get three attributes (set in `_build_control_deck`):
- `_pinned: bool = False`
- `_label: str` — "Export" / "Crop & Track" / "Scan"
- `_deck_key: str` — "export" / "crop" / "scan" (stable key for persistence)

Keep an ordered list `self._deck_panels = [self._tab_export, self._tab_crop, self._tab_scan]` for deterministic column order.

### Tab bar
New `class _DeckTabBar(QTabBar)` (minimal version of `_PlaylistTabBar`): on `contextMenuEvent`, show a checkable "Show side-by-side" action reflecting the page's `_pinned`, and emit `pin_toggle_requested(idx)` when chosen. No rename/folder. Install via `self._control_deck.setTabBar(_DeckTabBar())` in `_build_control_deck` and connect `pin_toggle_requested → self._on_deck_pin_toggle`.

### Stacked container (mirrors `_list_stack`)
Wrap the deck so it can swap between tabbed and split views:
- `self._deck_split_container = QWidget()` with an `QHBoxLayout` (`_deck_split_layout`, margins 0, spacing 2).
- `self._deck_stack = QStackedWidget()`; page 0 = `self._control_deck`, page 1 = `self._deck_split_container`.
- In `right_layout`, mount `self._deck_stack` where `self._control_deck` is currently added (replace that one `addWidget`).

### `_refresh_deck_layout()` (mirrors `_refresh_layout`)
```
pinned = [p for p in self._deck_panels if p._pinned]
guard self._deck_loading = True  (avoid re-entrant signals)
detach all panels (setParent(None)); self._control_deck.clear(); clear _deck_split_layout
if len(pinned) >= 2:
    splitter = QSplitter(Horizontal); splitter.setChildrenCollapsible(False)
    leftovers = []
    for panel in self._deck_panels:        # preserve deck order
        if panel._pinned:
            col = QWidget(); v = QVBoxLayout(col) (0 margins)
            header = label(panel._label, bold) + "✕" button (unpin, fixed 18x18,
                     tooltip "Return to tabs", clicked → self._on_deck_unpin(panel))
            header fixed height ~22
            panel.setVisible(True)          # reparented pages start hidden
            v.addWidget(header); v.addWidget(panel, 1)
            splitter.addWidget(col)
        else:
            leftovers.append(panel)
    if leftovers:                            # keep unpinned reachable as a tab-column
        lt = QTabWidget(); lt.setDocumentMode(True)
        for panel in leftovers:
            panel.setVisible(True); lt.addTab(panel, panel._label)
        splitter.addWidget(lt)
    splitter.setSizes([1000]*splitter.count())
    _deck_split_layout.addWidget(splitter)
    self._deck_stack.setCurrentWidget(self._deck_split_container)
else:
    for panel in self._deck_panels:          # fixed order
        self._control_deck.addTab(panel, panel._label)
    self._deck_stack.setCurrentWidget(self._control_deck)
restore self._deck_loading
```

### Toggle handlers (mirror `_on_pin_toggle`/`_on_unpin`)
- `_on_deck_pin_toggle(idx)`: `panel = self._control_deck.widget(idx)` (only valid in tabbed mode — pin is only offered there); flip `panel._pinned`; if now pinned and `<2` pinned, `_show_status("Pin another panel to show them side-by-side", 3500)`; `_refresh_deck_layout()`; `_save_deck_layout()`.
- `_on_deck_unpin(panel)`: `panel._pinned = False`; `_refresh_deck_layout()`; `_save_deck_layout()`.

### Persistence
- `_save_deck_layout()`: `self._settings.setValue("deck_pinned", [p._deck_key for p in self._deck_panels if p._pinned])`.
- Restore at the end of `__init__` (after the deck + menubar exist): read `deck_pinned` (handle str/list like the subprofiles loader at main.py:3867), set each panel's `_pinned`, then `_refresh_deck_layout()` once.

### Height
The deck pages now also render with a 22px header in split mode. After building, set the stack's minimum height to fit the tallest **split-mode** column (header + Export content) so split mode never clips: compute once via `self._deck_stack.setMinimumHeight(...)` using `sizeHint`, and keep vertical size policy `Fixed` (as the deck has now). Switching INTO split mode may change the deck height slightly (deliberate user action — acceptable); switching tabs within tabbed mode must still not jump. Reuse the existing height-pin logic — apply it to `_deck_stack` instead of `_control_deck`.

---

## Implementation tasks (bite-sized, commit per task)

**Task M.1 — scaffolding (no behavior change yet).** Add `_DeckTabBar`; in `_build_control_deck` set it on the deck, set `_pinned/_label/_deck_key` on the three pages, build `self._deck_panels`, create `_deck_split_container`/`_deck_split_layout`/`_deck_stack`, and mount `_deck_stack` in `right_layout` instead of `_control_deck`. Connect `pin_toggle_requested` to a stub. App still behaves as plain tabs. Verify: `import main`, structure tests 6/6, and a probe that `_deck_stack.currentWidget() is _control_deck`.

**Task M.2 — split rendering.** Implement `_refresh_deck_layout`, `_detach_deck_panels`, `_clear_deck_split`, `_on_deck_pin_toggle`, `_on_deck_unpin`. Verify with a probe: set two panels `_pinned=True`, call `_refresh_deck_layout()`, assert stack shows `_deck_split_container`, the splitter has 3 columns (2 pinned + 1 leftover QTabWidget), and all three panels are visible/parented; unpin one → back to `_control_deck` with 3 tabs in order.

**Task M.3 — persistence.** Add `_save_deck_layout()` + restore block in `__init__`. Verify a probe round-trips a pinned set through QSettings (use an isolated QSettings scope in the test if needed) without error and that restore calls refresh exactly once.

**Task M.4 — height + tests.** Apply the height-pin to `_deck_stack`; confirm split mode doesn't clip the tallest column. Add structure tests: `test_deck_stack_exists`, and `test_pinning_two_panels_switches_to_split` (programmatically pin 2, refresh, assert `_deck_stack.currentWidget() is _deck_split_container`).

## Verification note
Env quirk (same as the restructure): bare `python -c` constructing `MainWindow` segfaults on mpv GL; run checks under the pytest fixture and `LD_PRELOAD=/usr/lib/libstdc++.so.6 QT_QPA_PLATFORM=offscreen`. Visual confirmation (drag dividers, pin/unpin gestures, persistence across real launches) is the user's, done at the end.

## Risks
- **Reparenting hidden pages:** QTabWidget hides non-current pages; reparented panels must be `setVisible(True)` in split columns (same gotcha the playlist documents at main.py:4909-4911).
- **Signal re-entrancy:** guard with `_deck_loading` during refresh.
- **Pin offered in split mode:** `_on_deck_pin_toggle` reads `_control_deck.widget(idx)`, which is only meaningful in tabbed mode. The ✕ header is the unpin path in split mode — don't rely on the context menu there.
- **Height jump on mode toggle:** acceptable (deliberate); tab-switch-within-tabs must remain jump-free.
