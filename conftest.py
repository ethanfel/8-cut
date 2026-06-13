import sys, os
sys.path.insert(0, os.path.dirname(__file__))


def pytest_configure(config):
    config.addinivalue_line("markers", "gui: constructs Qt widgets; needs a display")
