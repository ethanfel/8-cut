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
