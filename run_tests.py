"""Minimal stdlib test runner.

The suite is written pytest-style (plain `test_*` functions + `conn`/`dbpath`
fixtures). This runner lets it run with nothing but the standard library, so
`python run_tests.py` works on a clean machine. If pytest is installed,
`pytest` also works — the fixtures live in tests/conftest.py and are mirrored
here. No third-party dependency is required to verify the project.
"""

import importlib.util
import os
import sys
import tempfile
import traceback
import types

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from smartdialer import db as _db  # noqa: E402


# --- fixtures (mirror tests/conftest.py) ---------------------------------
def make_dbpath():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    _db.init_db(path)
    return path


def cleanup_dbpath(path):
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


def build_fixtures(params):
    """Provide whatever subset of (dbpath, conn) a test asks for."""
    provided = {}
    closers = []
    path = None
    if "dbpath" in params or "conn" in params:
        path = make_dbpath()
    if "dbpath" in params:
        provided["dbpath"] = path
    if "conn" in params:
        c = _db.connect(path)
        provided["conn"] = c
        closers.append(c.close)
    return provided, closers, path


def load_module(filepath):
    name = "t_" + os.path.splitext(os.path.basename(filepath))[0]
    spec = importlib.util.spec_from_file_location(name, filepath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run():
    test_dir = os.path.join(ROOT, "tests")
    files = sorted(
        os.path.join(test_dir, f)
        for f in os.listdir(test_dir)
        if f.startswith("test_") and f.endswith(".py")
    )
    passed = failed = 0
    failures = []
    for f in files:
        mod = load_module(f)
        for attr in sorted(dir(mod)):
            if not attr.startswith("test_"):
                continue
            fn = getattr(mod, attr)
            if not isinstance(fn, types.FunctionType):
                continue
            params = fn.__code__.co_varnames[: fn.__code__.co_argcount]
            provided, closers, path = build_fixtures(params)
            try:
                fn(**provided)
                passed += 1
                print(f"  PASS {os.path.basename(f)}::{attr}")
            except Exception:
                failed += 1
                failures.append((f, attr, traceback.format_exc()))
                print(f"  FAIL {os.path.basename(f)}::{attr}")
            finally:
                for close in closers:
                    try:
                        close()
                    except Exception:
                        pass
                if path:
                    cleanup_dbpath(path)

    print(f"\n{passed} passed, {failed} failed")
    if failures:
        print("\n--- failures ---")
        for f, attr, tb in failures:
            print(f"\n{os.path.basename(f)}::{attr}\n{tb}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run())
