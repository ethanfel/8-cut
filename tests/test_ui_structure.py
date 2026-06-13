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


def test_pinning_two_panels_switches_to_split(win):
    # Pin two panels directly (avoid the toggle handler so no QSettings write
    # leaks into other test windows) and refresh.
    win._tab_export._pinned = True
    win._tab_crop._pinned = True
    win._refresh_deck_layout()
    assert win._deck_stack.currentWidget() is win._deck_split_container
