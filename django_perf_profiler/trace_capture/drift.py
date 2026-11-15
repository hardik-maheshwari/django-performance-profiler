"""
trace_capture.drift
-------------------
Phase 3: regression gate. Reduce a run to a BRANCH-INVARIANT fingerprint, then
diff two fingerprints (master vs branch) so the expected call-graph restructure
never shows as noise. Every delta is surfaced explicitly -- feature vs. bug is
the human's call, never a silent pass/fail.

The fingerprint keeps only what a behaviour-preserving refactor must NOT change:

  SQL      normalized query structures + counts + orm_xray kind. Excludes
           file/line/func (the placement that drifts by design), time, params.
  outcome  response status / redirect target / flash messages, or the raised
           exception type.
  logs     the application's own log lines (behaviour a refactor must
           preserve verbatim) -- EXCLUDES the dynamic_life_saver / sql_spy /
           es_spy tool output, which differs by design.

Order is a SOFT flag, not hard drift: a refactor may legitimately reorder
independent queries, so same-multiset-different-order can be a feature.
"""

import difflib
import json
import logging
import re

from .strategies import Strategy, _ensure_parent

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Normalization: strip volatile-but-not-behavioural bits so two runs of the
# SAME scenario on two branches compare equal. Extend this list as needed --
# a debug tool, not a compiler; over-scrubbing only risks hiding a real change,
# so keep each pattern tight.
# --------------------------------------------------------------------------
_SCRUBBERS = (
    (re.compile(r"0x[0-9a-fA-F]+"), "0xADDR"),  # memory addresses in reprs
    (
        re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
        "<uuid>",
    ),  # primary keys, request ids
    (re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?"), "<ts>"),  # timestamps
    (re.compile(r"\bid=\d+"), "id=N"),  # object reprs: new users get fresh pks per run
)


def normalize(text):
    if text is None:
        return None
    for pattern, repl in _SCRUBBERS:
        text = pattern.sub(repl, text)
    return text


# --------------------------------------------------------------------------
# Per-run fingerprint.
# --------------------------------------------------------------------------
def build_fingerprint(run):
    spy = run.spy_state or {}
    tracked = spy.get("tracked_queries", {})
    # dict insertion order == first-appearance order of distinct structures.
    structures = [
        {"sql": normalize(str(h)), "count": q.get("count"), "kind": q.get("kind")}
        for h, q in tracked.items()
    ]
    return {
        "label": run.label,
        "sql": {
            "structures": structures,
            "order": [s["sql"] for s in structures],
            "total_queries": spy.get("total_queries"),
        },
        "outcome": _outcome(run),
        "logs": [normalize(line) for line in (run.domain_logs or [])],
    }


def _outcome(run):
    if run.exception is not None:
        return {
            "kind": "exception",
            "type": type(run.exception).__name__,
            "detail": normalize(str(run.exception)),
        }
    resp = run.response
    if resp is None:
        return {"kind": "none"}
    redirect = getattr(resp, "url", None)
    if redirect is None:
        try:
            redirect = resp.get("Location")  # HttpResponse header fallback
        except Exception:
            redirect = None
    return {
        "kind": "response",
        "status": getattr(resp, "status_code", None),
        "redirect": normalize(redirect) if redirect else None,
        "messages": [normalize(m) for m in _snapshot_messages(run.request)],
    }


def _snapshot_messages(request):
    """Flash messages WITHOUT consuming them -- reads the storage's queued list
    directly, so the harness's own later iteration still sees them."""
    if request is None:
        return []
    store = getattr(request, "_messages", None)
    if store is None:
        return []
    queued = getattr(store, "_queued_messages", None)
    if queued is None:
        queued = getattr(store, "_loaded_messages", []) or []
    out = []
    for m in queued:
        try:
            out.append(str(m.message))
        except Exception:
            out.append(str(m))
    return out


class FingerprintStrategy(Strategy):
    """Phase 3: write the branch-invariant fingerprint as JSON for later diffing."""

    def __init__(self, out_path):
        self.out_path = out_path

    def render(self, run):
        fp = build_fingerprint(run)
        _ensure_parent(self.out_path)
        with open(self.out_path, "w", encoding="utf-8") as f:
            json.dump(fp, f, indent=2)
        logger.info("FingerprintStrategy -> %s", self.out_path)


# --------------------------------------------------------------------------
# Cross-run comparison.
# --------------------------------------------------------------------------
HARD = "HARD"  # a behaviour difference -> review: feature or bug?
SOFT = "SOFT"  # likely benign (order-only)


def _delta(axis, severity, detail):
    return {"axis": axis, "severity": severity, "detail": detail}


def compare(base, cand):
    """Structured diff of two fingerprints -> list of deltas (empty == no drift)."""
    deltas = []
    _diff_sql(base, cand, deltas)
    _diff_outcome(base, cand, deltas)
    _diff_logs(base, cand, deltas)
    return deltas


def _diff_sql(base, cand, deltas):
    b = {s["sql"]: s for s in base.get("sql", {}).get("structures", [])}
    c = {s["sql"]: s for s in cand.get("sql", {}).get("structures", [])}

    for sql in b.keys() - c.keys():
        deltas.append(_delta("sql", HARD, "query no longer fired: %s" % _snip(sql)))
    for sql in c.keys() - b.keys():
        deltas.append(_delta("sql", HARD, "new query fired: %s" % _snip(sql)))
    for sql in b.keys() & c.keys():
        if b[sql]["count"] != c[sql]["count"]:
            deltas.append(
                _delta(
                    "sql",
                    HARD,
                    "count %s -> %s for: %s" % (b[sql]["count"], c[sql]["count"], _snip(sql)),
                )
            )
        if b[sql]["kind"] != c[sql]["kind"]:
            deltas.append(
                _delta(
                    "sql",
                    HARD,
                    "orm kind %s -> %s for: %s" % (b[sql]["kind"], c[sql]["kind"], _snip(sql)),
                )
            )

    # Order (soft): compare the sequence of structures present in BOTH.
    common = b.keys() & c.keys()
    b_order = [s for s in base.get("sql", {}).get("order", []) if s in common]
    c_order = [s for s in cand.get("sql", {}).get("order", []) if s in common]
    if b_order != c_order:
        deltas.append(_delta("sql", SOFT, "query order changed (same set of queries)"))


def _diff_outcome(base, cand, deltas):
    b, c = base.get("outcome", {}), cand.get("outcome", {})
    if b.get("kind") != c.get("kind"):
        deltas.append(_delta("outcome", HARD, "kind %s -> %s" % (b.get("kind"), c.get("kind"))))
        return  # different shape -> field-by-field comparison is meaningless
    for field in ("status", "redirect", "type", "detail"):
        if b.get(field) != c.get(field):
            deltas.append(
                _delta("outcome", HARD, "%s %r -> %r" % (field, b.get(field), c.get(field)))
            )
    if b.get("messages") != c.get("messages"):
        deltas.append(
            _delta("outcome", HARD, "messages %r -> %r" % (b.get("messages"), c.get("messages")))
        )


def _diff_logs(base, cand, deltas):
    b, c = base.get("logs", []), cand.get("logs", [])
    if b == c:
        return
    # If it's the same lines in a different order, that's soft.
    if sorted(b) == sorted(c):
        deltas.append(_delta("logs", SOFT, "log lines reordered (same set)"))
        return
    for line in difflib.unified_diff(b, c, lineterm="", n=0):
        if line.startswith("-") and not line.startswith("---"):
            deltas.append(_delta("logs", HARD, "log removed: %s" % line[1:].strip()))
        elif line.startswith("+") and not line.startswith("+++"):
            deltas.append(_delta("logs", HARD, "log added:   %s" % line[1:].strip()))


def _snip(text, n=90):
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n] + " …"


def format_report(base, cand, deltas):
    hard = [d for d in deltas if d["severity"] == HARD]
    soft = [d for d in deltas if d["severity"] == SOFT]
    out = [
        "=" * 72,
        "DRIFT: %s" % base.get("label", "?"),
        "  baseline vs candidate",
        "=" * 72,
    ]
    if not deltas:
        out.append("  NO DRIFT -- SQL, outcome and logs are identical after normalization.")
        return "\n".join(out)

    out.append(
        "  %d difference(s): %d HARD (review), %d SOFT (order-only)"
        % (len(deltas), len(hard), len(soft))
    )
    out.append("  Each HARD delta is a behaviour change -> decide: feature or bug?")
    for group, title in ((hard, "HARD -- review"), (soft, "SOFT -- likely benign")):
        if not group:
            continue
        out.append("")
        out.append("  %s:" % title)
        for d in group:
            out.append("    [%s] %s" % (d["axis"], d["detail"]))
    return "\n".join(out)


def load_fingerprint(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def compare_files(baseline_path, candidate_path):
    base, cand = load_fingerprint(baseline_path), load_fingerprint(candidate_path)
    return format_report(base, cand, compare(base, cand))


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3:
        print("usage: python -m django_perf_profiler.trace_capture.drift <baseline.json> <candidate.json>")
        sys.exit(2)
    print(compare_files(sys.argv[1], sys.argv[2]))