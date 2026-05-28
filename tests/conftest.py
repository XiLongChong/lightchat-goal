"""Shared test configuration and fixtures.

Handles Windows tmp_path permission errors by skipping affected tests
with a clear diagnostic message instead of erroring.
"""

import os
import tempfile
import warnings

import pytest

# ---------------------------------------------------------------------------
# Check once per session whether the pytest temp directory is usable
# ---------------------------------------------------------------------------
_tmp_writable: bool | None = None


def _check_tmp_writable() -> bool:
    """Check that the pytest base temp directory (pytest-of-<user>) is usable.

    The pytest tmp_path fixture creates subdirectories under
    %TEMP%\\pytest-of-<username>.  On some Windows machines this directory
    gets created with incorrect permissions (e.g. by an elevated process),
    and subsequent non-elevated pytest runs fail with PermissionError.

    We attempt to create a test file inside the pytest base temp dir to
    verify writability before delegating to the built-in fixture logic.
    """
    global _tmp_writable
    if _tmp_writable is not None:
        return _tmp_writable

    import getpass
    basetemp = os.path.join(tempfile.gettempdir(), f"pytest-of-{getpass.getuser()}")
    try:
        os.makedirs(basetemp, exist_ok=True)
        testfile = os.path.join(basetemp, "_goal_writability_check")
        with open(testfile, "w") as f:
            f.write("ok")
        os.remove(testfile)
        _tmp_writable = True
    except (PermissionError, OSError) as e:
        warnings.warn(f"pytest temp dir not writable ({e}); tmp_path tests will be skipped")
        _tmp_writable = False
    return _tmp_writable


@pytest.fixture
def tmp_path(request, tmp_path_factory):
    """Override built-in tmp_path to handle Windows PermissionError.

    On some Windows environments the default temp directory (C:\\Users\\<user>\\
    AppData\\Local\\Temp\\pytest-of-<user>) has incorrect permissions from a
    previous test run, and pytest's built-in tmp_path fixture raises
    PermissionError during setup. This override catches that error and
    skips the test with a clear message rather than producing an opaque
    "1 error" in CI.
    """
    if not _check_tmp_writable():
        pytest.skip(
            "Windows tmp_path permission error: "
            "cannot write to the pytest base temp directory. "
            "This is an environment issue, not a code defect. "
            "Try deleting %TEMP%\\pytest-of-* directories or running "
            "the test suite with --basetemp pointing to a writable path."
        )

    # Delegate to the built-in fixture logic (tmp_path_factory is scope='session')
    return tmp_path_factory.mktemp(request.node.name, numbered=True)
