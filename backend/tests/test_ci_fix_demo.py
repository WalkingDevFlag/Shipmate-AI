"""Throwaway test for the live CI-fix-loop demo. Deliberately broken so CI goes
red; the CIWatcher should fetch this file, ask Coder for a fix, and turn it
green. Safe to delete after the demo."""


def add(a, b):
    return a + b


def test_add_demo():
    # DELIBERATELY WRONG: 2 + 2 == 4, not 5. CI will fail here.
    assert add(2, 2) == 5
