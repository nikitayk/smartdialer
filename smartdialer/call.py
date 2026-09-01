"""Call lifecycle and provider-event ingestion.

Three independent guarantees stack here, because the assignment throws three
different kinds of bad input at us (duplicate, out-of-order, terminal-then-more):

  1. Idempotency by event id. Every provider event carries a provider_event_id.
     Before doing anything we INSERT OR IGNORE it into processed_events. If the
     insert affects 0 rows we've seen it before -> log and stop. This makes a
     redelivered event a no-op regardless of current state. (The decision log
     described state-based dedup and *also* carried provider_event_id in its
     schema/tests; those two didn't quite agree, so I reconciled them by making
     the event id the real dedup gate. Flagged, not silently changed.)

  2. State CAS. The actual transition is UPDATE ... WHERE state=<expected>. An
     event that doesn't fit the current state (out-of-order) matches 0 rows.

  3. Terminal immutability. COMPLETED/FAILED/CANCELLED are never the 'from' of
     any transition, so once terminal a call can't be moved by any later event.

Anything that doesn't apply is written to anomaly_log — never silently dropped,
because the rate of weird events is itself the provider-health signal.
"""

from .clock import now

QUEUED = "QUEUED"
RESERVED = "RESERVED"
INITIATED = "INITIATED"
RINGING = "RINGING"
ANSWERED = "ANSWERED"
CONNECTED = "CONNECTED"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"

STATES = {QUEUED, RESERVED, INITIATED, RINGING, ANSWERED, CONNECTED,
          COMPLETED, FAILED, CANCELLED}
TERMINAL = {COMPLETED, FAILED, CANCELLED}

# Internal transitions (driven by our own worker, not the provider).
ALLOWED = {
    (QUEUED, RESERVED),
    (RESERVED, INITIATED),
    (INITIATED, RINGING),
    (RINGING, ANSWERED),
    (ANSWERED, CONNECTED),
    (CONNECTED, COMPLETED),
    (INITIATED, FAILED),
    (RINGING, FAILED),
    (ANSWERED, FAILED),
    (CONNECTED, FAILED),
    (QUEUED, CANCELLED),
    (RESERVED, CANCELLED),
}

# Provider event -> the (from -> to) edge it drives.
EVENT_EDGE = {
    "RINGING": (INITIATED, RINGING),
    "ANSWERED": (RINGING, ANSWERED),
    "COMPLETED": (CONNECTED, COMPLETED),
    "FAILED": None,   # special: can fail from several states
}


def create_call(conn, call_id, borrower_id, agent_id=None, provider=None):
    conn.execute(
        "INSERT INTO calls (id, borrower_id, agent_id, provider, state, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (call_id, borrower_id, agent_id, provider, QUEUED, now(), now()),
    )


def get_state(conn, call_id):
    row = conn.execute("SELECT state FROM calls WHERE id=?", (call_id,)).fetchone()
    return row["state"] if row else None


def _log_anomaly(conn, call_id, event, frm, worker_id, event_id, reason):
    conn.execute(
        "INSERT INTO anomaly_log (entity_type, entity_id, attempted_event, "
        "from_state, worker_id, provider_event_id, reason, created_at) "
        "VALUES ('call', ?, ?, ?, ?, ?, ?, ?)",
        (call_id, event, frm, worker_id, event_id, reason, now()),
    )


def internal_transition(conn, call_id, expected_from, to) -> bool:
    """Worker-driven CAS transition (RESERVED, INITIATED, ...)."""
    if (expected_from, to) not in ALLOWED:
        raise ValueError(f"illegal call transition {expected_from}->{to}")
    extra = ", answered_at=?" if to == ANSWERED else ""
    params = [to, now()]
    if to == ANSWERED:
        params.append(now())
    params += [call_id, expected_from]
    cur = conn.execute(
        f"UPDATE calls SET state=?, updated_at=?{extra} WHERE id=? AND state=?",
        params,
    )
    return cur.rowcount == 1


def apply_event(conn, call_id, event_type, provider_event_id, worker_id="system") -> str:
    """Ingest one provider event. Returns 'applied' | 'duplicate' | 'rejected'.

    This is the single choke point for everything the provider sends us.
    """
    # (1) Idempotency gate — dedup by event id, state-independent.
    cur = conn.execute(
        "INSERT OR IGNORE INTO processed_events "
        "(provider_event_id, call_id, event_type, processed_at) VALUES (?, ?, ?, ?)",
        (provider_event_id, call_id, event_type, now()),
    )
    if cur.rowcount == 0:
        _log_anomaly(conn, call_id, event_type, None, worker_id,
                     provider_event_id, "duplicate_event_id")
        return "duplicate"

    row = conn.execute("SELECT state FROM calls WHERE id=?", (call_id,)).fetchone()
    if row is None:
        _log_anomaly(conn, call_id, event_type, None, worker_id,
                     provider_event_id, "unknown_call")
        return "rejected"
    frm = row["state"]

    # (3) Terminal immutability — cheap explicit guard + good anomaly reason.
    if frm in TERMINAL:
        _log_anomaly(conn, call_id, event_type, frm, worker_id,
                     provider_event_id, "event_after_terminal")
        return "rejected"

    # FAILED can arrive from several non-terminal states.
    if event_type == "FAILED":
        cur = conn.execute(
            "UPDATE calls SET state=?, updated_at=? WHERE id=? AND state IN "
            "(?,?,?,?)",
            (FAILED, now(), call_id, INITIATED, RINGING, ANSWERED, CONNECTED),
        )
        if cur.rowcount == 1:
            return "applied"
        _log_anomaly(conn, call_id, event_type, frm, worker_id,
                     provider_event_id, "failed_from_unexpected_state")
        return "rejected"

    edge = EVENT_EDGE.get(event_type)
    if edge is None:
        _log_anomaly(conn, call_id, event_type, frm, worker_id,
                     provider_event_id, "unknown_event_type")
        return "rejected"

    expected_from, to = edge
    # (2) State CAS — out-of-order events don't match and get logged.
    extra = ", answered_at=?" if to == ANSWERED else ""
    params = [to, now()]
    if to == ANSWERED:
        params.append(now())
    params += [call_id, expected_from]
    cur = conn.execute(
        f"UPDATE calls SET state=?, updated_at=?{extra} WHERE id=? AND state=?",
        params,
    )
    if cur.rowcount == 1:
        return "applied"
    _log_anomaly(conn, call_id, event_type, frm, worker_id,
                 provider_event_id, "out_of_order_or_wrong_state")
    return "rejected"


def force_fail(conn, call_id) -> bool:
    """Force a non-terminal call to FAILED (used on dial cancel / agent gone)."""
    cur = conn.execute(
        "UPDATE calls SET state=?, updated_at=? WHERE id=? AND state NOT IN (?,?,?)",
        (FAILED, now(), call_id, COMPLETED, FAILED, CANCELLED),
    )
    return cur.rowcount == 1
