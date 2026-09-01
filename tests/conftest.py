"""Fixtures for pytest. If pytest isn't installed, importing this module still
succeeds (the stdlib run_tests.py provides its own equivalents), so the test
files import cleanly either way."""

import os
import tempfile

from smartdialer import db as _db

try:
    import pytest

    @pytest.fixture
    def dbpath():
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        _db.init_db(path)
        yield path
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(path + suffix)
            except OSError:
                pass

    @pytest.fixture
    def conn(dbpath):
        c = _db.connect(dbpath)
        yield c
        c.close()

except ImportError:  # pragma: no cover - stdlib runner path
    pass
