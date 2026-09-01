"""Single source of 'now'.

Every timestamp in the system comes from here so that timeouts, the sweeper
and the tests all reason about the same clock. Times are epoch seconds
(float). All workers in V1 run on one host, so a shared monotonic-ish wall
clock is fine; cross-host clock skew is explicitly out of scope for V1 and
called out in the scale write-up.
"""

import time


def now() -> float:
    return time.time()
