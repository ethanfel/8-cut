# Main Window UI Restructure — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Re-house `MainWindow`'s ~50 flat controls into a menu bar (rare actions), an always-visible transport bar, a 3-tab control deck (Export / Crop & Track / Scan), and a real status bar — then a visual-polish pass — without changing any behavior, shortcut, or `core/` logic.

**Architecture:** Pure layout reorganization inside `main.py`'s `MainWindow`. Existing widget objects and every `connect()` are **preserved and re-parented**, not recreated. The monster `__init__` is incrementally broken into `_build_*` helper methods (stays single-file — matches the project's architecture). Companion design doc: `docs/plans/2026-06-13-ui-restructure-design.md`.

**Tech Stack:** Python 3.11+, PyQt6, pytest. App entry: `main.py`; launch via `./8cut.sh`.

---

## Conventions for every task

- **Line references drift** as edits land. Always locate by the named symbol (method/variable), not the line number alone. Numbers are the *starting* anchors as of this plan.
- **Authoritative verification is a manual launch.** After each task, run `./8cut.sh`, load a video, and confirm the task's controls work AND prior behavior is intact (play, scrub, export, scan). Use the `verify` skill for structured manual checks.
- **Structure test is the safety net.** `tests/test_ui_structure.py` (built in Task 0.2) constructs `MainWindow` and asserts containment invariants. It **skips gracefully** if construction fails (e.g. no GL for `MpvWidget` in headless CI), so it never blocks `core/` tests. Run with a display: `pytest tests/test_ui_structure.py -v`.
- **Commit after every task.** Small, reversible commits. Commit message convention matches the repo (`feat:`/`fix:`/`refactor:`/`change:`).
- **Do not touch** `core/`, export/scan/tracking logic, the `QShortcut` block (around main.py:4450–4483), `_KeyFilter`, or `TimelineWidget` mouse handling.

---

## Stage 0 — Branch & safety net

### Task 0.1: Create a working branch

**Step 1:** Confirm clean intent and branch off `master`:
```bash
git switch -c ui-restructure
```
**Step 2:** Verify: `git branch --show-current` → `ui-restructure`.
(The repo has pre-existing untracked/modified files; leave them alone — they are not part of this work.)

### Task 0.2: Add the structure-test safety net

**Files:**
- Create: `tests/test_ui_structure.py`

**Step 1: Write the test harness + baseline invariant**

```python
import os
import pytest

# A real platform is needed because MpvWidget creates a GL context.
# If construction fails for any environment reason, skip — this test is a
# best-effort structural net, not a gate on core/ tests.
pytestmark = pytest.mark.gui


@pytest.fixture(scope="module")
def app():
    from PyQt6.QtWidgets import QApplication
    inst = QApplication.instance() or QApplication([])
    yield inst


@pytest.fixture
def win(app):
    try:
        from main import MainWindow
        w = MainWindow()
    except Exception as e:  # GL/mpv/display unavailable, etc.
        pytest.skip(f"MainWindow could not be constructed here: {e}")
    yield w
    w.close()
    w.deleteLater()


def _descendant_object_names(widget):
    """All objectNames in a widget's child tree (for containment asserts)."""
    return {c.objectName() for c in widget.findChildren(object) if c.objectName()}


def test_window_constructs(win):
    assert win.windowTitle() == "8-cut"
```

**Step 2: Run it**

Run: `pytest tests/test_ui_structure.py -v`
Expected: `test_window_constructs` PASSES (with a display) or SKIPS (headless). Either is acceptable — it must not ERROR.

**Step 3:** Register the `gui` marker to silence warnings.

Modify `conftest.py` — append:
```python
def pytest_configure(config):
    config.addinivalue_line("markers", "gui: constructs Qt widgets; needs a display")
```

**Step 4: Confirm core tests still pass**

Run: `pytest tests/test_utils.py tests/test_db.py -q`
Expected: PASS (unchanged).

**Step 5: Commit**
```bash
git add tests/test_ui_structure.py conftest.py
git commit -m "test: add MainWindow structure smoke test (skips headless)"
```

---

## Stage 1 — Menu bar

Add a `QMenuBar` whose actions reuse existing handler methods. Move the profile combo and `?` button into menu-bar corner widgets. Keep the original buttons that also live elsewhere (Scan, Auto) — menus and buttons share handlers.

### Task 1.1: Extract a `_build_menubar()` and add the five menus

**Files:**
- Modify: `main.py` `MainWindow.__init__` (call site) and add method `_build_menubar`

**Step 1:** Add the method (place near other `_build`/setup helpers, e.g. after `__init__`). Wire each action to the **existing** handler method:

```python
def _build_menubar(self) -> None:
    from PyQt6.QtGui import QAction
    mb = self.menuBar()

    # File
    m_file = mb.addMenu("&File")
    m_file.addAction("Open Files…", self._on_open_files)
    m_file.addAction("Set export folder…", self._pick_folder)
    m_file.addSeparator()
    m_file.addAction("Quit", self.close)

    # Edit
    m_edit = mb.addMenu("&Edit")
    self._act_undo = m_edit.addAction("Undo scan edit", self._scan_panel.undo)
    self._act_undo.setShortcut("Ctrl+Z")
    m_edit.addSeparator()
    m_subs = m_edit.addMenu("Subprofiles")
    m_subs.addAction("Add…", self._new_subprofile)
    self._menu_subprofiles_remove = m_subs.addMenu("Remove")
    self._rebuild_remove_subprofile_menu()  # built in Task 4.x

    # Scan
    m_scan = mb.addMenu("&Scan")
    m_scan.addAction("Scan current", self._start_scan)
    m_scan.addAction("Auto-export", self._auto_export)
    m_scan.addSeparator()
    m_scan.addAction("Scan All…", self._start_scan_all)
    m_scan.addAction("Train classifier…", self._open_train_dialog)

    # View
    m_view = mb.addMenu("&View")
    self._act_review = m_view.addAction("Review mode")
    self._act_review.setCheckable(True)
    self._act_review.toggled.connect(self._btn_scan_mode.setChecked)
    m_view.addAction("Subcategory markers…", self._show_subcat_menu)
    m_view.addSeparator()
    self._act_hide_exported = m_view.addAction("Hide exported")
    self._act_hide_exported.setCheckable(True)
    self._act_hide_exported.toggled.connect(self._chk_hide_exported.setChecked)
    self._chk_hide_exported.toggled.connect(self._act_hide_exported.setChecked)
    self._act_show_hidden = m_view.addAction("Show hidden")
    self._act_show_hidden.setCheckable(True)
    self._act_show_hidden.toggled.connect(self._btn_show_hidden.setChecked)
    self._btn_show_hidden.toggled.connect(self._act_show_hidden.setChecked)

    # Help
    m_help = mb.addMenu("&Help")
    m_help.addAction("Keyboard shortcuts", self._show_shortcuts).setShortcut("F1")
    m_help.addAction("What's new", self._show_changelog)
    m_help.addAction("About", self._show_about)  # tiny method, Task 1.3
```

> **Sync note:** `QAction.toggled`/`QAbstractButton.toggled` do not re-emit when the value is unchanged, so the bidirectional `setChecked` connections (Review, Hide exported, Show hidden) cannot loop. `_btn_scan_mode` → `_act_review` reverse sync is added in Task 3.4 once the button is in the Scan tab.

**Step 2:** Stub the two small new methods referenced above:
```python
def _show_about(self) -> None:
    QMessageBox.about(self, "About 8-cut",
                      f"<b>8-cut</b> v{self.APP_VERSION}<br>"
                      "8-second clips for foley datasets.")

def _rebuild_remove_subprofile_menu(self) -> None:
    self._menu_subprofiles_remove.clear()
    for name in self._subprofiles:
        self._menu_subprofiles_remove.addAction(
            name, lambda _=False, n=name: self._remove_subprofile(n))
    self._menu_subprofiles_remove.setEnabled(bool(self._subprofiles))
```

**Step 3:** Call `self._build_menubar()` in `__init__`, **after** `self._scan_panel` and all referenced buttons exist (i.e. just before/after the splitter assembly around main.py:4429). The scan panel is created at main.py:4414, so place the call after that.

**Step 4 (manual verify):** `./8cut.sh` → menu bar shows File/Edit/Scan/View/Help; each item triggers its action; Ctrl+Z still undoes scan edits; F1 shows shortcuts.

**Step 5:** Commit: `feat: add menu bar wired to existing handlers`.

### Task 1.2: Move profile combo + `?` into menu-bar corner

**Files:** Modify `main.py` — `top_bar` assembly (around main.py:4290–4294) and `_build_menubar`.

**Step 1:** Remove `self._cmb_profile` and `self._btn_shortcuts` (and the `"Profile:"` `QLabel`) from `top_bar`. Keep `self._lbl_file` in `top_bar` (it stays as the slim filename header above the video).

**Step 2:** In `_build_menubar`, set a corner widget:
```python
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QLabel
corner = QWidget()
ch = QHBoxLayout(corner)
ch.setContentsMargins(0, 0, 6, 0)
ch.addWidget(QLabel("Profile:"))
ch.addWidget(self._cmb_profile)
ch.addWidget(self._btn_shortcuts)
mb.setCornerWidget(corner, Qt.Corner.TopRightCorner)
```
(Build the corner widget at the end of `_build_menubar`, after `self._cmb_profile` exists — it is created at main.py:4272.)

**Step 3 (manual verify):** Profile dropdown works (switch/new/delete); `?` opens shortcuts; filename still shows above the video.

**Step 4:** Commit: `change: move profile selector and help into menu-bar corner`.

---

## Stage 2 — Status bar

### Task 2.1: Restore `QStatusBar` and route `_show_status` to it

**Files:** Modify `main.py` — `__init__` (`setStatusBar(None)` at main.py:4440, `_lbl_status`/`_status_timer` at main.py:4364–4370) and `_show_status` (main.py:5065).

**Step 1:** Replace `self.setStatusBar(None)` with a real status bar built in a helper:
```python
def _build_status_bar(self) -> None:
    sb = self.statusBar()
    self._status_perm = QLabel("")
    self._status_perm.setStyleSheet("color: #888;")
    sb.addPermanentWidget(self._status_perm)
    self._update_status_perm()

def _update_status_perm(self) -> None:
    name = os.path.basename(self._file_path) if self._file_path else "—"
    self._status_perm.setText(
        f"{name} · profile: {self._profile()} · {self._spn_workers.value()} workers")
```
Call `self._build_status_bar()` in `__init__` near the menubar call.

**Step 2:** Rewrite `_show_status` to use the status bar (this subsumes `_status_timer`):
```python
def _show_status(self, msg: str, timeout: int = 0) -> None:
    """Show a transient message in the status bar. timeout in ms (0 = sticky)."""
    self.statusBar().showMessage(msg, timeout)
```

**Step 3:** Delete `self._lbl_status`, `self._status_timer`, and `settings_row.addWidget(self._lbl_status)` (main.py:4364–4370). Remove the `_status_timer.timeout` connection.

**Step 4:** Keep `_update_status_perm()` fresh — call it where file/profile/workers change: end of `_after_load`, in `_on_profile_activated`, and in the `_spn_workers.valueChanged` lambda.

**Step 5 (manual verify):** Start an export → status text appears bottom-left and auto-clears; bottom-right shows file · profile · workers and updates on file/profile/worker change.

**Step 6:** Commit: `feat: real status bar replaces inline status label`.

---

## Stage 3 — Control deck (the core move)

Build a fixed-height `QTabWidget` with three tab pages, then **re-parent** the existing controls from `path_row` and `settings_row` into them. Give each page an `objectName` for the structure test. Do tabs one at a time so the app stays runnable.

### Task 3.1: Build the empty deck and mount it

**Files:** Modify `main.py` — `right_layout` assembly (main.py:4372–4382).

**Step 1:** Add a helper that creates the deck and three empty pages:
```python
def _build_control_deck(self) -> "QTabWidget":
    from PyQt6.QtWidgets import QTabWidget, QWidget
    deck = QTabWidget()
    deck.setObjectName("control_deck")
    deck.setDocumentMode(True)
    self._tab_export = QWidget(); self._tab_export.setObjectName("export_tab")
    self._tab_crop = QWidget();   self._tab_crop.setObjectName("crop_tab")
    self._tab_scan = QWidget();   self._tab_scan.setObjectName("scan_tab")
    deck.addTab(self._tab_export, "Export")
    deck.addTab(self._tab_crop, "Crop && Track")
    deck.addTab(self._tab_scan, "Scan")
    self._control_deck = deck
    return deck
```

**Step 2:** In `right_layout`, **keep** `transport_row` for now, but replace the `path_row` and `settings_row` additions with the deck:
- Remove `right_layout.addLayout(path_row)` and `right_layout.addLayout(settings_row)`.
- Add `right_layout.addWidget(self._build_control_deck())`.
- Leave the `path_row`/`settings_row` *construction* in place for this task (the widgets are still parented to nothing visible) — they get moved into tabs in 3.2–3.4. **App is briefly missing those controls between 3.1 and 3.4; that's expected mid-stage.**

**Step 3 (manual verify):** App launches; three empty tabs appear under the transport bar; switching tabs doesn't resize the video (height fixed in Task 3.5).

**Step 4:** Commit: `refactor: add empty 3-tab control deck under transport`.

### Task 3.2: Populate the Export tab

**Files:** Modify `main.py` — move widgets from `path_row` (main.py:4322–4331) and the encode/clip parts of `settings_row` (main.py:4334–4348) plus `_spn_workers` (main.py:4213).

**Step 1:** Build the Export tab with an aligned grid:
```python
def _build_export_tab(self) -> None:
    from PyQt6.QtWidgets import QGridLayout, QLabel, QHBoxLayout
    g = QGridLayout(self._tab_export)
    g.setContentsMargins(8, 6, 8, 6); g.setHorizontalSpacing(8); g.setVerticalSpacing(6)
    # Row 0: annotation
    g.addWidget(QLabel("Label:"), 0, 0); g.addWidget(self._txt_label, 0, 1)
    g.addWidget(QLabel("Cat:"),   0, 2); g.addWidget(self._cmb_category, 0, 3)
    g.addWidget(QLabel("Name:"),  0, 4); g.addWidget(self._txt_name, 0, 5)
    # Row 1: output path
    folder_row = QHBoxLayout()
    folder_row.addWidget(self._txt_folder, 1); folder_row.addWidget(self._btn_folder)
    g.addWidget(QLabel("Folder:"), 1, 0); g.addLayout(folder_row, 1, 1, 1, 5)
    # Row 2: encode / clip params
    g.addWidget(QLabel("Format:"), 2, 0); g.addWidget(self._cmb_format, 2, 1)
    g.addWidget(self._chk_hw, 2, 2)
    g.addWidget(QLabel("Resize:"), 2, 3); g.addWidget(self._spn_resize, 2, 4)
    # Row 3: batch params + actions
    g.addWidget(QLabel("Duration:"), 3, 0); g.addWidget(self._spn_clip_dur, 3, 1)
    g.addWidget(QLabel("Clips:"),    3, 2); g.addWidget(self._spn_clips, 3, 3)
    g.addWidget(QLabel("Spread:"),   3, 4); g.addWidget(self._spn_spread, 3, 5)
    g.addWidget(QLabel("Workers:"),  4, 0); g.addWidget(self._spn_workers, 4, 1)
    g.addWidget(self._btn_reexport, 4, 5)
```
Call it from `_build_control_deck` (or right after, in `__init__`).

**Step 2:** Delete the now-duplicate `addWidget` calls for these widgets from `path_row` and `settings_row` construction. (Re-parenting via `addWidget` into the grid auto-removes them from the old layout, but remove the dead lines to keep `__init__` honest.)

**Step 3 (manual verify):** Export tab shows aligned Label/Cat/Name, Folder+browse, Format/HW/Resize, Duration/Clips/Spread/Workers/Re-export. Change each → still persists to `QSettings` and updates the timeline span / next-label as before. Export still works (E).

**Step 4:** Commit: `refactor: move export & encode controls into Export tab`.

### Task 3.3: Populate the Crop & Track tab

**Files:** Modify `main.py` — move `_cmb_portrait`, `_chk_rand_portrait`, `_chk_rand_square`, `_chk_track` from `settings_row` (main.py:4337, 4349–4351).

**Step 1:**
```python
def _build_crop_tab(self) -> None:
    from PyQt6.QtWidgets import QGridLayout, QLabel
    g = QGridLayout(self._tab_crop)
    g.setContentsMargins(8, 6, 8, 6); g.setHorizontalSpacing(8); g.setVerticalSpacing(6)
    g.addWidget(QLabel("Portrait:"), 0, 0); g.addWidget(self._cmb_portrait, 0, 1)
    g.addWidget(self._chk_rand_portrait, 1, 0, 1, 2)
    g.addWidget(self._chk_rand_square,   2, 0, 1, 2)
    g.addWidget(self._chk_track,         3, 0, 1, 2)
    g.setRowStretch(4, 1); g.setColumnStretch(2, 1)
```

**Step 2:** Remove those four widgets' old `settings_row.addWidget` lines.

**Step 3 (manual verify):** Crop & Track tab shows the four controls; portrait ratio still toggles the crop overlay/crop-bar; random/track checkboxes persist.

**Step 4:** Commit: `refactor: move crop & track controls into their tab`.

### Task 3.4: Populate the Scan tab (and drop menu-only buttons)

**Files:** Modify `main.py` — move scan widgets from `settings_row` (main.py:4352–4362). Buttons that became **menu-only** (Train, Scan All, Sub) are NOT added to the tab and are deleted.

**Step 1:**
```python
def _build_scan_tab(self) -> None:
    from PyQt6.QtWidgets import QGridLayout, QLabel, QHBoxLayout
    g = QGridLayout(self._tab_scan)
    g.setContentsMargins(8, 6, 8, 6); g.setHorizontalSpacing(8); g.setVerticalSpacing(6)
    model_row = QHBoxLayout()
    model_row.addWidget(self._cmb_scan_model, 1); model_row.addWidget(self._btn_model_history)
    g.addWidget(QLabel("Model:"), 0, 0); g.addLayout(model_row, 0, 1, 1, 3)
    g.addWidget(self._btn_scan, 1, 0); g.addWidget(self._btn_auto_export, 1, 1)
    g.addWidget(self._btn_speech, 1, 2); g.addWidget(self._btn_scan_mode, 1, 3)
    g.addWidget(self._spn_auto_fuse, 2, 0); g.addWidget(self._sld_threshold, 2, 1)
    g.setColumnStretch(3, 1)
```

**Step 2:** Reverse-sync Review with the View menu (the forward sync was added in Task 1.1):
```python
self._btn_scan_mode.toggled.connect(self._act_review.setChecked)
```
Add this right after `_build_scan_tab` runs (both `_btn_scan_mode` and `_act_review` exist by then).

**Step 3:** Delete the menu-only buttons and their `settings_row` lines: `self._btn_train` (main.py:4167–4170), `self._btn_scan_all` (main.py:4172–4174), `self._btn_hide_subcats` (main.py:4154–4157). Their handlers (`_open_train_dialog`, `_start_scan_all`, `_show_subcat_menu`) stay — now reached via menus.

**Step 4:** Re-anchor `_show_subcat_menu` (main.py:5989) so it no longer depends on the deleted `_btn_hide_subcats`:
```python
# was: self._btn_hide_subcats.mapToGlobal(self._btn_hide_subcats.rect().bottomLeft())
from PyQt6.QtGui import QCursor
menu.exec(QCursor.pos())
```
Apply to **both** `exec` call sites in that method.

**Step 5 (manual verify):** Scan tab shows Model+history, Scan/Auto/Speech/Review, Fuse/Threshold. `Scan` runs; `Review` toggles and stays in sync with View ▸ Review mode (both directions); View ▸ Subcategory markers… opens the full popup near the cursor; Scan ▸ Scan All / Train still work.

**Step 6:** Commit: `refactor: move scan controls into Scan tab; Train/ScanAll/Sub to menus`.

### Task 3.5: Fix deck height; remove dead `path_row`/`settings_row`

**Files:** Modify `main.py` — `__init__`.

**Step 1:** The `path_row`/`settings_row` `QHBoxLayout`s should now be empty. Delete their construction blocks entirely (main.py:4321–4370 minus what was already removed), including the `self._transport_row = transport_row` line only if unused elsewhere (it IS used by `_rebuild_subprofile_buttons` — keep `transport_row`).

**Step 2:** Pin the deck height so tab switches don't move the video:
```python
self._control_deck.setFixedHeight(self._control_deck.sizeHint().height())
```
Call after all three tabs are built. If the tallest tab (Export, 5 rows) clips, set an explicit value instead (e.g. `setFixedHeight(150)`); confirm visually.

**Step 3 (manual verify):** Switching Export↔Crop↔Scan keeps the video size constant; no clipped controls; all three tabs fully usable.

**Step 4:** Commit: `refactor: fix control-deck height; drop dead settings rows`.

### Task 3.6: Extend the structure test for the deck

**Files:** Modify `tests/test_ui_structure.py`.

**Step 1:** Add invariants:
```python
def test_menubar_has_expected_menus(win):
    titles = [m.title().replace("&", "") for m in win.menuBar().findChildren(type(win.menuBar().addMenu("")))]
    for expected in ("File", "Edit", "Scan", "View", "Help"):
        assert any(expected == t for t in titles)

def test_status_bar_exists(win):
    assert win.statusBar() is not None

def test_workers_spinbox_in_export_tab(win):
    from PyQt6.QtWidgets import QSpinBox
    assert win._spn_workers in win._tab_export.findChildren(QSpinBox)

def test_scan_button_in_scan_tab(win):
    from PyQt6.QtWidgets import QPushButton
    assert win._btn_scan in win._tab_scan.findChildren(QPushButton)

def test_portrait_combo_in_crop_tab(win):
    from PyQt6.QtWidgets import QComboBox
    assert win._cmb_portrait in win._tab_crop.findChildren(QComboBox)
```
(Adjust the menu-title introspection if the helper is awkward; the key invariants are the tab-containment ones.)

**Step 2:** Run: `pytest tests/test_ui_structure.py -v` → PASS with a display (or SKIP headless).

**Step 3:** Commit: `test: assert control-deck containment invariants`.

---

## Stage 4 — Transport bar tidy & subprofile menu sync

### Task 4.1: Confirm transport bar contents; keep subprofile export buttons inline

**Files:** Modify `main.py` — `transport_row` (main.py:4296–4319).

**Step 1:** The workers spinbox was moved in Task 3.2 — confirm `transport_row.addWidget(self._spn_workers)` is gone. Remaining transport order: Play, Pause, x2, x4, Lock, time, stretch, next-label, **Export**, subprofile buttons, `+` (add subprofile), Cancel, Delete. Leave subprofile **export** buttons inline (they carry the 1–9 shortcuts and belong with Export).

**Step 2:** Keep the inline `+` add-subprofile button, but also ensure the Edit ▸ Subprofiles ▸ Remove submenu is rebuilt whenever subprofiles change. In `_rebuild_subprofile_buttons` (main.py:5530-ish) and after add/remove, call `self._rebuild_remove_subprofile_menu()`.

**Step 3 (manual verify):** Transport row reads cleanly; adding/removing a subprofile updates both the inline buttons and Edit ▸ Subprofiles ▸ Remove; number keys 1–9 still export to subprofiles.

**Step 4:** Commit: `change: tidy transport row; sync subprofile remove menu`.

---

## Stage 5 — Visual polish

All Stage 5 verification is **manual** (visual). Take a screenshot before 5.1 for comparison (use the `run`/`verify` skill).

### Task 5.1: Consolidate the stylesheet (tabs, status bar, toggles, primary button)

**Files:** Modify `main.py` — global stylesheet in `main()` (main.py:3811–3827).

**Step 1:** Extend the central sheet (append rules; keep existing ones):
```css
QTabWidget::pane { border: 1px solid #444; border-radius: 3px; top: -1px; }
QTabBar::tab { background: #2a2a2a; color: #bbb; padding: 5px 12px;
               border: 1px solid #444; border-bottom: none;
               border-top-left-radius: 3px; border-top-right-radius: 3px; }
QTabBar::tab:selected { background: #333; color: #fff; }
QPushButton:checked { background: #4a3000; border-color: #ffd230; color: #fff; }
QStatusBar { background: #1a1a1a; color: #bbb; }
QStatusBar::item { border: none; }
QPushButton#primary { background: #3a6ea8; border-color: #4f86c6; color: #fff; }
QPushButton#primary:hover { background: #4f86c6; }
QMenuBar { background: #1e1e1e; } QMenuBar::item:selected { background: #3a6ea8; }
QMenu { background: #2a2a2a; border: 1px solid #555; }
QMenu::item:selected { background: #3a6ea8; }
```

**Step 2:** Mark Export primary: `self._btn_export.setObjectName("primary")`.

**Step 3:** Replace Lock's inline stylesheet swap (main.py:5705) — since `QPushButton:checked` now styles all toggles, delete the two `self._btn_lock.setStyleSheet(...)` lines in `_on_lock_toggled` (keep the rest of the handler).

**Step 4 (manual verify):** Tabs, menus, status bar, and checked toggles (x2/x4/Lock/Review) all read consistently; Export stands out as primary; Lock still highlights when active.

**Step 5:** Commit: `style: unify tab/menu/statusbar/toggle styling; mark Export primary`.

### Task 5.2: Preserve the "armed to overwrite" Export state

**Files:** Inspect `main.py` — the red-Export swaps (main.py:5403, and the resets at 4960/5211/5447/7170/7199/7218).

**Step 1:** These set/clear `self._btn_export.setStyleSheet("QPushButton { background: #6a3030; ... }")` to mean "this export will overwrite". With Export now `objectName("primary")`, an empty `setStyleSheet("")` reset reverts to the **primary** look (good). Confirm the armed (red) state still visually overrides primary — inline stylesheet beats the objectName rule, so it does.

**Step 2 (manual verify):** Select a marker for re-export → Export turns red (armed); deselect → returns to blue primary; export → resets correctly.

**Step 3:** Commit (only if changes were needed): `fix: keep armed-overwrite Export state over primary style`.

### Task 5.3: Label cleanup

**Files:** Modify `main.py` — prefixes/labels.

**Step 1:** De-abbreviate where free: `_sld_threshold.setPrefix("Threshold: ")` (main.py:4207) → keep short if it overflows the tab; `_spn_auto_fuse` prefix stays `"Fuse: "`. Replace the `⏲` history button text with a tooltip-backed `"History"` or a clearer glyph; keep `setFixedWidth` generous enough.

**Step 2 (manual verify):** Labels legible; nothing clipped in the Scan tab.

**Step 3:** Commit: `style: de-abbreviate scan labels`.

---

## Stage 6 — Finalize

### Task 6.1: Full regression pass

**Step 1 (manual, use `verify` skill):** With a real video loaded, confirm end-to-end: scrub/play/pause/speed/lock; export (E) single + batch + subprofile (1–9); re-export; delete; portrait crop + random + track; scan + auto + speech + review + threshold/fuse; scan-all; train dialog opens; profile switch; queue filter/hide/show-hidden; Ctrl+Z undo; F1/`?` shortcuts.

**Step 2:** Run `pytest -q` (all suites). Expected: `core/` PASS; `test_ui_structure` PASS (display) or SKIP.

### Task 6.2: Docs & changelog

**Files:** Modify `README.md` (UI/shortcuts sections if any references moved) and the in-app `CHANGELOG` list (main.py:4500) — bump `APP_VERSION` and add a "UI restructure" entry so the What's-new dialog announces it.

**Step 1:** Add changelog entry summarizing: menu bar, tabbed control deck, status bar, visual polish; note all shortcuts unchanged.

**Step 2:** Commit: `docs: changelog + README for UI restructure`.

### Task 6.3: Hand off the branch

**Step 1:** `git log --oneline master..ui-restructure` — review the commit series.
**Step 2:** Offer the user: merge to `master`, open a PR, or keep iterating (use `finishing-a-development-branch` skill).

---

## Risk register

| Risk | Mitigation |
|------|-----------|
| Re-parenting breaks a `connect()` | Widgets keep identity; only layout membership changes. Manual launch after every task catches breakage immediately. |
| Headless test can't build `MpvWidget` | Structure test skips on construction failure; manual launch is authoritative. |
| Menu/button state desync (Review, Hide exported) | Bidirectional `setChecked` (no re-emit on equal value → no loop); verified manually in 3.4. |
| Subcat popup anchored to deleted button | Re-anchored to `QCursor.pos()` in Task 3.4. |
| Deck height jump on tab switch | `setFixedHeight` in Task 3.5. |
| Armed-overwrite red Export lost under primary style | Inline stylesheet overrides objectName rule; verified in 5.2. |
| Mid-Stage-3 app missing controls | Expected between 3.1–3.4; each sub-task is still committable and launchable. |

## What this plan does NOT change

`core/` logic · export/scan/tracking/DB behavior · keyboard shortcuts · timeline mouse interactions · the Queue and Scan-results panes' internals · the dark Fusion theme.
