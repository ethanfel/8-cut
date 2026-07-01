import pytest

# Redirect QSettings to a throwaway dir BEFORE any MainWindow is constructed, so
# these GUI tests can never read or clobber the user's real ~/.config/8cut.conf
# (constructing MainWindow loads — and on window close re-saves — the playlist
# tabs; a test mutating tab state would otherwise persist into the real session).
import tempfile as _tempfile
from PyQt6.QtCore import QSettings as _QSettings
_QS_DIR = _tempfile.mkdtemp(prefix="8cut-test-qs-")
_QSettings.setPath(_QSettings.Format.NativeFormat, _QSettings.Scope.UserScope, _QS_DIR)
_QSettings.setPath(_QSettings.Format.IniFormat, _QSettings.Scope.UserScope, _QS_DIR)

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
    # Deterministic deck state regardless of any persisted side-by-side layout
    # (construction restores deck_pinned from QSettings).
    for _p in w._deck_panels:
        _p._pinned = False
    w._refresh_deck_layout()
    yield w
    w.close()
    w.deleteLater()


def test_window_constructs(win):
    assert win.windowTitle().startswith("8-cut")


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


def test_menu_only_buttons_not_in_deck(win):
    from PyQt6.QtWidgets import QPushButton
    deck_btns = win._control_deck.findChildren(QPushButton)
    assert win._btn_train not in deck_btns
    assert win._btn_scan_all not in deck_btns
    assert win._btn_hide_subcats not in deck_btns


def test_deck_stack_exists(win):
    # The deck is wrapped in a stack so it can swap tabbed <-> side-by-side.
    # Default (nothing pinned) shows the tabbed control deck.
    assert win._deck_stack is not None
    assert win._deck_stack.currentWidget() is win._control_deck


def _split_columns(win):
    """Widgets of the splitter actually mounted in the layout (not findChild,
    which can return a stale deleteLater'd splitter)."""
    from PyQt6.QtWidgets import QSplitter
    item = win._deck_split_layout.itemAt(0)
    spl = item.widget() if item else None
    assert isinstance(spl, QSplitter)
    return [spl.widget(i) for i in range(spl.count())]


def test_pinning_two_panels_shows_exactly_two_columns(win):
    # Pin two panels directly (avoid the toggle handler so no QSettings write
    # leaks into other test windows) and refresh.
    from PyQt6.QtWidgets import QTabWidget
    win._tab_export._pinned = True
    win._tab_crop._pinned = True
    win._refresh_deck_layout()
    assert win._deck_stack.currentWidget() is win._deck_split_container
    cols = _split_columns(win)
    assert len(cols) == 2                                    # only the pinned ones
    assert not any(isinstance(c, QTabWidget) for c in cols)  # no leftover tab-column


def test_side_by_side_menu_pins_third_panel(win):
    # In split mode the View ▸ Side-by-side menu is the way to pin a 3rd panel
    # (there's no tab bar to right-click). Suppress the QSettings save via the
    # _deck_loading guard so this doesn't leak into other windows.
    win._tab_export._pinned = True
    win._tab_scan._pinned = True
    win._refresh_deck_layout()
    assert len(_split_columns(win)) == 2
    act = next(a for a, p in win._deck_pin_actions if p is win._tab_crop)
    win._deck_loading = True            # suppress _save_deck_layout
    try:
        act.trigger()                   # simulate clicking the menu item
    finally:
        win._deck_loading = False
    assert win._tab_crop._pinned is True
    assert len(_split_columns(win)) == 3


def test_duplicate_tab(win):
    # Right-click → Duplicate tab: clones files into a new tab with an adapted
    # name + adapted own folder, no file moves. Suppress QSettings writes via
    # _loading_tabs so the test can't touch the real session.
    win._loading_tabs = True
    try:
        src = win._pws[0]
        src._label = "AlexisCrystal"
        src._dest_folder = "/data/alexis/"   # trailing slash, like real folders
        n_before = len(win._pws)
        win._on_duplicate_tab(win._playlist_tabs.indexOf(src))
    finally:
        win._loading_tabs = False
    assert len(win._pws) == n_before + 1
    dup = win._pws[-1]
    assert dup._label == "AlexisCrystal copy"
    # sibling, not a child: ".../alexis/" -> ".../alexis_copy" (not ".../alexis/_copy")
    assert dup._dest_folder == "/data/alexis_copy"


def test_tab_mode_defaults_foley(win):
    # Fresh tabs use the Foley pipeline; sessions/tabs without a stored mode
    # load unchanged.
    assert win._pws
    for pw in win._pws:
        assert pw._mode == "foley"


def test_tab_mode_toggle(win):
    # Right-click → "LTX-2 mode" flips the per-tab mode and the displayed title
    # gains a [LTX2] badge (without mutating pw._label). Suppress QSettings
    # writes via _loading_tabs so the test can't touch the real session.
    win._loading_tabs = True
    try:
        win._on_tab_mode_toggle(win._playlist_tabs.indexOf(win._pws[0]))
    finally:
        win._loading_tabs = False
    assert win._pws[0]._mode == "ltx2"
    assert win._tab_title(win._pws[0]).endswith("[LTX2]")


def test_ltx2_params_none_for_foley(win):
    # A Foley tab feeds no LTX-2 ffmpeg params into export. Set the mode
    # explicitly: a prior test's closeEvent can persist an ltx2 tab into the
    # shared (throwaway) QSettings, so don't rely on the loaded default here.
    win._playlist._mode = "foley"
    assert win._ltx2_export_params() is None


def test_ltx2_params_for_ltx2_tab(win):
    # An ltx2-mode active tab: _ltx2_export_params returns the 25fps / ÷32 /
    # exact-frames kwargs, and _apply_mode_to_controls swaps the length control
    # (Duration hidden, frames shown). short_side defaults to 512 when unset.
    win._spn_resize.setValue(0)            # force the 512 LTX-2 default path
    win._pws[0]._mode = "ltx2"
    win._active_pw = win._pws[0]
    win._playlist_tabs.setCurrentWidget(win._pws[0])
    win._spn_frames.setValue(201)
    win._apply_mode_to_controls()

    assert win._ltx2_export_params() == {
        "target_fps": 25.0,
        "snap32": True,
        "frames": 201,
        "duration": 201 / 25,
        "short_side": 512,
    }
    # In offscreen, isVisibleTo(win) may be False for both; assert via the
    # show/hide flag that the Duration control is hidden in ltx2 mode.
    assert win._spn_clip_dur.isHidden()
    assert not win._spn_frames.isHidden()


def test_duplicate_preserves_ltx2_mode(win):
    # Duplicating an LTX-2 tab must yield an LTX-2 tab (mode is copied alongside
    # the folder fields). Suppress QSettings writes via _loading_tabs.
    win._loading_tabs = True
    try:
        src = win._pws[0]
        src._mode = "ltx2"
        win._on_duplicate_tab(win._playlist_tabs.indexOf(src))
    finally:
        win._loading_tabs = False
    dup = win._pws[-1]
    assert dup._mode == "ltx2"


def test_frames_snaps_to_legal(win):
    # A typed (illegal) frame count snaps to the nearest legal 8k+1 value so the
    # displayed value == the exported value and is always a valid LTX-2 clip.
    win._spn_frames.setValue(100)
    win._snap_frames_to_legal()              # the editingFinished slot
    assert win._spn_frames.value() == 97     # nearest 8k+1 to 100
    assert (win._spn_frames.value() - 1) % 8 == 0


def test_export_base_name_handles_trailing_slash(win):
    # A folder ending in "/" must still yield the real base name, else
    # subprofile naming breaks ("_blowjob" instead of "mp4_blowjob").
    win._txt_folder.setText("/x/AlexisCrystal/mp4/")
    assert win._export_base_name() == "mp4"
    win._txt_folder.setText("/x/AlexisCrystal/mp4")
    assert win._export_base_name() == "mp4"


def test_subprofile_button_visibility_exact_match(win):
    # A subcategory's export button must track ITS folder exactly. A ghost
    # "_blowjob" (empty-base leftover) or an unrelated "mp4_no_clap" must NOT
    # hide the "blowjob"/"clap" buttons (the old fuzzy endswith() match did,
    # so enabling a subcategory never revealed its export button).
    win._txt_folder.setText("/x/AlexisCrystal/mp4")
    win._subprofiles = ["blowjob", "clap"]
    win._rebuild_subprofile_buttons()
    btns = {b.text().removeprefix("▸ "): b for b in win._subprofile_btns}

    win._hidden_subcats = {"_blowjob", "mp4_no_clap"}
    win._apply_subcat_visibility()
    assert not btns["blowjob"].isHidden()   # ghost "_blowjob" must not hide it
    assert not btns["clap"].isHidden()      # "mp4_no_clap" must not hide "clap"

    win._hidden_subcats = {"mp4_blowjob"}    # exact folder -> hidden
    win._apply_subcat_visibility()
    assert btns["blowjob"].isHidden()
    assert not btns["clap"].isHidden()


def test_extract_audio_controls_exist(win):
    from PyQt6.QtWidgets import QPushButton, QDoubleSpinBox
    assert isinstance(win._btn_extract_audio, QPushButton)
    assert isinstance(win._spn_audio_len, QDoubleSpinBox)
    # Disabled until a file is loaded.
    assert not win._btn_extract_audio.isEnabled()
    # Arrows step by 1s and there's no practical upper cap (long audio areas).
    assert win._spn_audio_len.singleStep() == 1.0
    assert win._spn_audio_len.maximum() >= 3600.0


def test_audio_region_tracks_cursor_and_length(win):
    # The teal audio band spans [cursor, cursor + length]; changing the length
    # or moving the cursor moves the band. Fake a loaded file so the guard in
    # _update_audio_region passes.
    win._file_path = "/x/video.mp4"
    win._cursor = 10.0
    win._spn_audio_len.setValue(4.0)     # fires _on_audio_len_changed
    assert win._timeline._audio_region == (10.0, 14.0)
    win._cursor = 20.0
    win._update_audio_region()
    assert win._timeline._audio_region == (20.0, 24.0)
    # No file -> band cleared.
    win._file_path = ""
    win._update_audio_region()
    assert win._timeline._audio_region is None
