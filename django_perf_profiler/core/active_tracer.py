"""
core.active_tracer
------------------
The cooperation seam between the dynamic call tracer (producer) and the spies
(consumers: sql_spy, es_spy). Neither side imports the other: the tracer
registers itself here while it runs, and the spies talk only to this module's
stable API.

When a spy and the tracer are both active on a (cheap) endpoint, the spy
attributes each query / ES op to the tracer's current first-party frame and
pushes its cost back onto it, giving per-function SQL / ES / Python time. When
only a spy runs (e.g. an expensive endpoint with no tracer), every call here is
a no-op / returns None and the spy falls back to its own inspect.stack()
attribution.

A registered tracer must implement:
    current_location() -> (qualname, rel_file, lineno) | None
    note_sql(duration, count=1) -> None
    note_es(duration, count=1) -> None
"""

import contextvars

_active = contextvars.ContextVar("core_active_tracer", default=None)


def set_active(tracer):
    """The tracer registers itself for the duration of its run.
    Returns a token to hand back to clear()."""
    return _active.set(tracer)


def clear(token):
    _active.reset(token)


def get():
    return _active.get()


def current_location():
    """Attribution of the innermost first-party frame, or None when no tracer is
    active (the spy then falls back to inspect.stack)."""
    t = _active.get()
    return t.current_location() if t is not None else None


def note_sql(duration, count=1):
    """Record one query's cost against the active tracer's current frame.
    No-op when no tracer is active."""
    t = _active.get()
    if t is not None:
        t.note_sql(duration, count)


def note_es(duration, count=1):
    """Record one Elasticsearch op's client wall time against the active
    tracer's current frame. No-op when no tracer is active."""
    t = _active.get()
    if t is not None:
        t.note_es(duration, count)
