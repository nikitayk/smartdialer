"""Single, swappable source of 'now'.

Real code uses wall-clock time. The simulator swaps in a virtual clock via
set_source() so that sweeper TTLs (30s) and the abandon guard (2s) are
meaningful in a fast tick loop instead of being pinned to real seconds. Tests
use the default wall clock and are unaffected.
"""

import time

_source = time.time


def now() -> float:
    return _source()


def set_source(fn) -> None:
    global _source
    _source = fn


def reset_source() -> None:
    global _source
    _source = time.time
